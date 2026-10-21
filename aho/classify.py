"""Rule-based screener: LOCAL / HYBRID / LLM, with a human-readable reason.

LOCAL  -- deterministic (run a script, fetch an API, compare state, send a
          fixed-format message). Runs on the OS scheduler with no LLM.
HYBRID -- mostly deterministic with an isolatable LLM step (summarise, write-up,
          translate). The local runner does the fetch/compare; the LLM step is
          only invoked when needed.
LLM    -- needs judgement every run, or operates on the agent session itself.
          Stays on the agent.

The rules are intentionally conservative: when nothing deterministic is found
the verdict is LLM (keep it on the agent) rather than guessing.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass, field

from .cronexpr import is_cron
from .specs import Workflow

LOCAL, HYBRID, LLM = "LOCAL", "HYBRID", "LLM"

_I = re.I
LOCAL_SIGNALS = {
    "runs a script": r"\b(python3?|pythonw|py|node|bash|sh|pwsh|powershell)(\.exe)?\b[^\n`]{0,120}\.(py|js|mjs|sh|ps1)\b"
                     r"|\brun (the |this )?(script|checker|command|binary)\b",
    "HTTP/API fetch": r"\bcurl\b|\bwget\b|\bInvoke-RestMethod\b|\b(GET|POST) https?://|\bfetch(es|ing)?\b[^\n]{0,40}\b(api|endpoint|url|price|feed|rss|json)\b",
    "state compare": r"\bcompar(e|es|ing)\b|\bdiff(s|ed)? against\b|\bprevious run\b|\blast[_ ]run\b|\bstate\.json\b|\bstate file\b|\bthreshold\b",
    "deterministic gate": r"(?i:\bdeterministic\b|\bobey (its|the) output\b|\bif (the )?output (is|says)\b)|\b(SKIP|FIRE|NONE)\b",
    "fixed-format notify": r"\bsend\b[^\n]{0,60}\b(telegram|whatsapp|slack|discord|email|sms|notification)\b"
                           r"|\b(fixed[- ]format|same format|template|style:)",
    "writes a file": r"\bwrite\b[^\n]{0,80}\.(json|txt|csv|log)\b|\bappend(s)? to\b",
}
LLM_HARD = {
    "analysis": r"\banaly[sz](e|es|is|ing)\b",
    "judgement": r"\bjudge?ments?\b|\buse your judge?ment\b",
    "decision": r"\bdecide\b|\bdecisions?\b",
    "recommendation": r"\brecommend(s|ation|ations)?\b",
    "council/debate": r"\bcouncil\b|\bdebate\b",
    "research": r"\bresearch\b|\binvestigate\b",
    "assessment": r"\bassess(es|ment)?\b|\bcritique\b",
    "interpretation": r"\binterpret(s|ation)?\b",
    "sub-agent": r"\bsub-?agents?\b|\bspawn (an? )?agent\b",
}
LLM_SOFT = {
    "summarise": r"\bsummar(ise|ize|ising|izing|ised|ized)\b|\b(write|give|produce) (a|an) (short |brief )?summary\b",
    "translate": r"\btranslat(e|ion)\b",
    "write-up": r"\bwrite-?up\b|\bwrite (a|an|the|one)\b[^\n]{0,30}\b(reply|post|report|summary|tweet|paragraph)\b",
    "draft/compose": r"\bdraft\b|\bcompose\b|\brewrite\b|\bin (your|its) own words\b",
    "explain": r"\bexplain\b",
}
AGENT_ONLY = {
    "agent session control": r"(?<![\w/])/compact\b|\bthis (claude |agent )?session\b|\bcontext (window|hygiene)\b",
    "agent-only tool (connector/MCP)": r"\bconnector\b|\bmcp__\w+|\bMCP tool\b",
}
HEAD_CHARS = 600     # agent-session control only counts near the top of a spec
NEGATION = re.compile(r"\b(no|not|never|don'?t|do not|without|nor|instead of)\b[^.;:]{0,25}$", _I)


def _hits(text: str, table: dict) -> dict[str, str]:
    found = {}
    for label, pat in table.items():
        for m in re.finditer(pat, text, 0 if label == "deterministic gate" else _I):
            before = text[max(0, m.start() - 40):m.start()]
            if NEGATION.search(before):
                continue
            found[label] = m.group(0).strip()[:60]
            break
    return found


CODE_BLOCK = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.S)
INLINE = re.compile(r"`([^`\n]{4,300})`")
CMD_START = re.compile(r"^(?:[A-Z_]+=\S+\s+)*(?:\S*[/\\])?(python3?|pythonw|py|node|bash|sh|curl|wget|pwsh|powershell)(\.exe)?\b", _I)


def extract_commands(text: str) -> list[str]:
    """Shell commands that appear verbatim in the spec (code blocks / inline code)."""
    cmds: list[str] = []
    for block in CODE_BLOCK.findall(text):
        joined = re.sub(r"\s*\\\n\s*", " ", block)
        for line in joined.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and (CMD_START.search(s) or s.startswith("export ")):
                cmds.append(s)
    for s in INLINE.findall(text):
        s = s.strip()
        if CMD_START.search(s) and s not in cmds:
            cmds.append(s)
    # drop bare 'export X=Y' lines unless they are the only thing
    real = [c for c in cmds if not re.fullmatch(r"export \S+=\S+", c)]
    return real or cmds


@dataclass
class Verdict:
    name: str
    slug: str
    schedule: str
    verdict: str
    reason: str
    local_signals: dict = field(default_factory=dict)
    llm_signals: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)
    explicit_command: str | None = None
    schedulable: bool = True
    source: str = ""
    llm_review: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve_follow(wf: Workflow, by_file: dict[str, Workflow]) -> str:
    """'Follow `.../other.md` exactly' -> classify using the referenced spec's text too."""
    extra = ""
    for ref in re.findall(r"([\w.\-]+\.md)\b", wf.body):
        other = by_file.get(ref.lower())
        if other and other is not wf:
            extra += "\n" + other.text
    return extra


def classify(wf: Workflow, siblings: list[Workflow] | None = None) -> Verdict:
    by_file = {os.path.basename(s.source).lower(): s for s in (siblings or [])}
    text = wf.text + _resolve_follow(wf, by_file)
    v = Verdict(name=wf.name, slug=wf.slug, schedule=wf.schedule, verdict=LLM, reason="",
                source=wf.source, explicit_command=wf.command)
    if not is_cron(wf.schedule):
        v.schedulable = False
        v.reason = f"schedule {wf.schedule!r} is not a standard 5-field cron expression (lifecycle hook or unsupported token) - stays on the agent"
        return v

    local = _hits(text, LOCAL_SIGNALS)
    hard = _hits(text, LLM_HARD)
    soft = _hits(text, LLM_SOFT)
    agent = _hits(text, {k: p for k, p in AGENT_ONLY.items() if k != "agent session control"})
    # session control counts only where the spec says what the job IS (description +
    # opening lines), not when a side note merely mentions it
    head = f"{wf.description}\n{wf.body[:HEAD_CHARS]}"
    agent.update(_hits(head, {"agent session control": AGENT_ONLY["agent session control"]}))
    v.commands = extract_commands(text)
    if v.commands:
        local.setdefault("runs a script", v.commands[0][:60])
    if wf.command:
        local["explicit command"] = str(wf.command)[:60]
    v.local_signals = local
    v.llm_signals = {**{f"hard:{k}": x for k, x in hard.items()},
                     **{f"soft:{k}": x for k, x in soft.items()},
                     **{f"agent:{k}": x for k, x in agent.items()}}

    def fmt(d):
        return ", ".join(f"{k} ({x!r})" for k, x in d.items())

    if "agent session control" in agent:
        v.verdict = LLM
        v.reason = "acts on the agent session itself (" + agent["agent session control"] + ") - cannot run without the agent"
    elif wf.command and not hard and not agent:
        v.verdict = LOCAL
        v.reason = "explicit command, no LLM step" + (f"; soft LLM words ignored: {fmt(soft)}" if soft else "")
    elif not local:
        v.verdict = LLM
        v.reason = ("no deterministic step found" + (f"; needs {fmt({**hard, **soft})}" if hard or soft else "")
                    + " - keep on the agent")
    elif len(hard) >= 2:
        v.verdict = LLM
        v.reason = f"judgement every run: {fmt(hard)}"
    elif hard or soft or agent:
        v.verdict = HYBRID
        parts = [f"local: {fmt(local)}"]
        if hard or soft:
            parts.append(f"isolatable LLM step: {fmt({**hard, **soft})}")
        if agent:
            parts.append(f"agent tool to replace with a direct API: {fmt(agent)}")
        v.reason = "; ".join(parts)
    else:
        v.verdict = LOCAL
        v.reason = f"deterministic: {fmt(local)}"
    return v


def is_borderline(v: Verdict) -> bool:
    return v.schedulable and (v.verdict == HYBRID or
                              (v.verdict == LLM and v.local_signals) or
                              (v.verdict == LOCAL and v.llm_signals))


LLM_PROMPT = """You are screening a scheduled AI-agent workflow. Decide if it can run with NO LLM.
Answer with ONE line of JSON: {{"verdict": "LOCAL|HYBRID|LLM", "reason": "<one sentence>"}}
LOCAL = deterministic (scripts, API fetch, compare, fixed-format message).
HYBRID = deterministic except one isolatable LLM step (e.g. summarise).
LLM = needs judgement every run, or controls the agent session.
Rule-based verdict so far: {verdict} ({reason}).

--- workflow: {name} (schedule {schedule}) ---
{text}
"""


def llm_review(v: Verdict, wf: Workflow, cmd: str | None = None, timeout: int = 180) -> Verdict:
    """Optional second opinion via a user-supplied command (env AHO_LLM_CMD), which
    receives the prompt on stdin and prints an answer. No provider is hardcoded.
    Examples: ``claude -p``, ``codex exec -``, ``ollama run llama3``, ``llm``."""
    cmd = cmd or os.environ.get("AHO_LLM_CMD")
    if not cmd:
        v.llm_review = "skipped: AHO_LLM_CMD not set"
        return v
    prompt = LLM_PROMPT.format(verdict=v.verdict, reason=v.reason, name=wf.name,
                               schedule=wf.schedule, text=wf.text[:6000])
    try:
        r = subprocess.run(cmd if os.name == "nt" else shlex.split(cmd), input=prompt,
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        m = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", r.stdout or "")
        if not m:
            v.llm_review = "no JSON verdict in LLM output"
            return v
        d = json.loads(m.group(0))
        new = str(d.get("verdict", "")).upper()
        if new in (LOCAL, HYBRID, LLM):
            v.llm_review = f"{new}: {d.get('reason', '')}"
            if new != v.verdict:
                # never auto-promote to LOCAL on LLM say-so alone; record the disagreement
                if new == LLM or (new == HYBRID and v.verdict == LOCAL):
                    v.reason += f" | LLM review moved {v.verdict}->{new}"
                    v.verdict = new
                else:
                    v.reason += f" | LLM review suggests {new} (not applied; review manually)"
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        v.llm_review = f"error: {e}"
    return v


def scan(workflows: list[Workflow], use_llm: bool = False) -> list[Verdict]:
    out = []
    for wf in workflows:
        v = classify(wf, workflows)
        if use_llm and is_borderline(v):
            v = llm_review(v, wf)
        out.append(v)
    return out
