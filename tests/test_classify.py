from pathlib import Path

import pytest

from aho.classify import HYBRID, LLM, LOCAL, classify, extract_commands, llm_review, scan
from aho.specs import Workflow, load_path

EX = Path(__file__).resolve().parent.parent / "examples"

EXPECTED = {
    "Disk Space Check": LOCAL,
    "Nightly Backup Verify": LOCAL,
    "Coffee Bean Price Alert": LOCAL,
    "Website Uptime": LOCAL,
    "Biweekly Timesheet Reminder": LOCAL,
    "GitHub Release Watch": LOCAL,
    "TLS Certificate Expiry": LOCAL,
    "Blog Feed Watcher": HYBRID,
    "Industry News Digest": LLM,
    "Session Context Hygiene": LLM,
}


def test_examples_md():
    wfs = load_path(EX / "workflows")
    got = {v.name: v.verdict for v in scan(wfs)}
    assert got == EXPECTED
    assert sum(1 for v in got.values() if v != LLM) == 8   # "8 of 10 can leave the agent"


def test_examples_yaml_and_json():
    yv = {v.name: v for v in scan(load_path(EX / "workflows.yaml"))}
    assert yv["Rotate Logs"].verdict == LOCAL
    assert yv["Sync Notes Folder"].verdict == LOCAL
    assert not yv["Quarter-end Report"].schedulable
    jv = {v.name: v.verdict for v in scan(load_path(EX / "workflows.json"))}
    assert jv == {"Daily Weather Ping": LOCAL, "Inbox Triage": LLM}


def test_every_verdict_has_reason():
    for v in scan(load_path(EX / "workflows")):
        assert v.reason


def test_negation_is_ignored():
    wf = Workflow("x", "0 8 * * *", body="Run `python3 check.py`. No subagent, no\n council, no research. "
                                         "Send a Telegram message with the output.")
    assert classify(wf).verdict == LOCAL


def test_session_control_only_counts_at_top():
    long_body = "1. Run `python3 remind.py` and send a Telegram message with its output.\n" + "x " * 400 + \
        "\nNote: the auto /compact cron keeps this session alive."
    assert classify(Workflow("r", "0 9 * * 1", body=long_body)).verdict == LOCAL
    assert classify(Workflow("c", "50 */2 * * *", description="Trigger /compact every 2h")).verdict == LLM


def test_connector_makes_hybrid():
    wf = Workflow("p", "0 9 * * *", body="Call the broker connector, compare with the previous run, "
                                         "write state to last.json.")
    v = classify(wf)
    assert v.verdict == HYBRID and "direct API" in v.reason


def test_follow_reference(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\nschedule: \"0 9 * * *\"\n---\n"
                                   "Analyse the market, decide and recommend a trade.\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("---\nname: b\nschedule: \"0 21 * * *\"\n---\n"
                                   "Follow `a.md` exactly.\n", encoding="utf-8")
    vs = {v.name: v.verdict for v in scan(load_path(tmp_path))}
    assert vs == {"a": LLM, "b": LLM}


def test_extract_commands():
    text = "Run:\n```bash\nexport TZ=UTC\npython3 tool.py \\\n  --flag\n```\nThen `curl -s https://x.example/api`."
    assert extract_commands(text) == ["python3 tool.py --flag", "curl -s https://x.example/api"]


def test_llm_review_pluggable(monkeypatch):
    wf = Workflow("h", "0 9 * * *", body="Fetch the feed with `curl -s https://x.example/feed` and summarise new posts.")
    v = classify(wf)
    assert v.verdict == HYBRID
    cmd = 'python -c "print(\'{\\"verdict\\": \\"LLM\\", \\"reason\\": \\"needs taste\\"}\')"'
    v = llm_review(v, wf, cmd=cmd)
    assert v.verdict == LLM and "needs taste" in v.llm_review


def test_llm_review_off_without_cmd(monkeypatch):
    monkeypatch.delenv("AHO_LLM_CMD", raising=False)
    wf = Workflow("h", "0 9 * * *", body="run `python3 a.py` and summarise it")
    v = llm_review(classify(wf), wf)
    assert v.llm_review.startswith("skipped")


@pytest.mark.parametrize("sched", ["on_session_start", "0 9 L * *", ""])
def test_unschedulable(sched):
    v = classify(Workflow("s", sched, body="run `python3 x.py`"))
    assert not v.schedulable and v.verdict == LLM
