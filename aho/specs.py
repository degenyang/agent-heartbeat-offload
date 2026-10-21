"""Load scheduled-workflow specs from a directory or a file.

Accepted inputs
  * ``*.md``   -- Markdown with a ``---`` frontmatter block containing at least
                  ``name`` and ``schedule`` (cron expression); the body is the task prose.
                  (This is the shape used by Claude Code session-cron spec files.)
  * ``*.yaml`` / ``*.yml`` -- a list of mappings (or ``{workflows: [...]}``).
  * ``*.json`` -- same structure as YAML.

Recognised keys per workflow: ``name``, ``schedule``, ``description``, ``task``
(or ``prompt``/``body``), ``command`` (explicit shell command -> runs with no LLM).

Original files are only ever read, never modified.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Workflow:
    name: str
    schedule: str
    description: str = ""
    body: str = ""
    command: str | None = None
    source: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def slug(self) -> str:
        s = re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_").lower()
        return s or "job"

    @property
    def text(self) -> str:
        return f"{self.description}\n{self.body}\n{self.command or ''}"


# ------------------------------------------------------------ tiny YAML subset
def _scalar(v: str):
    v = v.strip()
    if not v:
        return ""
    if (v[0] == v[-1]) and v[0] in "\"'" and len(v) >= 2:
        return v[1:-1]
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_simple_yaml(text: str):
    """Parse the YAML subset we document: a top-level list of flat mappings
    (optionally under a ``workflows:`` key), or a flat mapping (frontmatter).
    Supports ``key: value``, quoted strings, ``|`` / ``>`` block scalars and
    ``#`` comments. Uses PyYAML instead when it is installed."""
    try:  # pragma: no cover - optional dependency
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - not installed, or text is not strict YAML
        return _parse_subset(text)


def _parse_subset(text: str):
    lines = text.splitlines()
    items: list[dict] = []
    top: dict = {}
    cur: dict | None = None
    in_list = False
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if stripped.startswith("- "):
            in_list = True
            cur = {}
            items.append(cur)
            stripped = stripped[2:]
            indent += 2
        m = re.match(r"([A-Za-z0-9_\-]+)\s*:\s*(.*)$", stripped)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        target = cur if in_list and cur is not None else top
        if val in ("|", ">", "|-", ">-"):
            block = []
            while i < len(lines) and (not lines[i].strip() or
                                      len(lines[i]) - len(lines[i].lstrip()) > indent):
                block.append(lines[i])
                i += 1
            pad = min((len(b) - len(b.lstrip()) for b in block if b.strip()), default=0)
            joined = [b[pad:] for b in block]
            target[key] = ("\n" if val.startswith("|") else " ").join(joined).strip()
        elif val == "" and not in_list:
            target[key] = None          # e.g. 'workflows:' header
        else:
            if "#" in val and not val.startswith(("'", '"')):
                val = val.split(" #", 1)[0]
            target[key] = _scalar(val)
    if items:
        return items if not top or list(top) == ["workflows"] else {**top, "workflows": items}
    return top


FRONT_RE = re.compile(r"\A﻿?---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def load_markdown(path: Path) -> Workflow | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = FRONT_RE.match(text)
    if not m:
        return None
    meta = parse_simple_yaml(m.group(1)) or {}
    if not isinstance(meta, dict) or "schedule" not in meta:
        return None
    return Workflow(
        name=str(meta.get("name") or path.stem),
        schedule=str(meta.get("schedule", "")).strip(),
        description=str(meta.get("description") or ""),
        body=m.group(2),
        command=meta.get("command"),
        source=str(path),
        extra={k: v for k, v in meta.items()
               if k not in ("name", "schedule", "description", "command")},
    )


def _from_mapping(d: dict, source: str) -> Workflow:
    return Workflow(
        name=str(d.get("name", "job")),
        schedule=str(d.get("schedule", "")).strip(),
        description=str(d.get("description") or ""),
        body=str(d.get("task") or d.get("prompt") or d.get("body") or ""),
        command=d.get("command"),
        source=source,
        extra={k: v for k, v in d.items()
               if k not in ("name", "schedule", "description", "command", "task", "prompt", "body")},
    )


def load_list(path: Path) -> list[Workflow]:
    text = path.read_text(encoding="utf-8", errors="replace")
    data = json.loads(text) if path.suffix.lower() == ".json" else parse_simple_yaml(text)
    if isinstance(data, dict):
        data = data.get("workflows") or []
    return [_from_mapping(d, f"{path}#{i}") for i, d in enumerate(data or [])
            if isinstance(d, dict) and d.get("schedule")]


def load_path(target: str | Path) -> list[Workflow]:
    """Load every workflow spec under ``target`` (non-recursive for directories;
    files whose names end in .paused/.disabled/.bak etc. are naturally skipped
    because only .md/.yaml/.yml/.json suffixes are read)."""
    p = Path(target)
    files = [p] if p.is_file() else sorted(x for x in p.iterdir() if x.is_file())
    out: list[Workflow] = []
    for f in files:
        suf = f.suffix.lower()
        if suf == ".md":
            wf = load_markdown(f)
            if wf:
                out.append(wf)
        elif suf in (".yaml", ".yml", ".json"):
            try:
                out.extend(load_list(f))
            except (ValueError, json.JSONDecodeError):
                continue
    return out
