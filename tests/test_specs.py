from aho.specs import _parse_subset, load_path


def test_frontmatter_md(tmp_path):
    (tmp_path / "a.md").write_text('---\nname: Alpha\nschedule: "*/5 * * * *"\ndescription: d # c\n---\nbody\n',
                                   encoding="utf-8")
    (tmp_path / "no_front.md").write_text("# just notes\n", encoding="utf-8")
    (tmp_path / "x.md.paused").write_text('---\nname: P\nschedule: "0 1 * * *"\n---\n', encoding="utf-8")
    wfs = load_path(tmp_path)
    assert [(w.name, w.schedule, w.description, w.body.strip()) for w in wfs] == [("Alpha", "*/5 * * * *", "d", "body")]


def test_yaml_subset_block_scalar():
    data = _parse_subset(
        "workflows:\n  - name: A\n    schedule: '0 1 * * *'\n    task: |\n      line one\n      line two\n"
        "  - name: B\n    schedule: \"0 2 * * *\"\n    command: echo hi\n")
    assert data == [{"name": "A", "schedule": "0 1 * * *", "task": "line one\nline two"},
                    {"name": "B", "schedule": "0 2 * * *", "command": "echo hi"}]


def test_json_list_ignores_entries_without_schedule(tmp_path):
    (tmp_path / "w.json").write_text('[{"name": "a", "schedule": "0 1 * * *"}, {"name": "state"}]', encoding="utf-8")
    (tmp_path / "state.json").write_text('{"last": 1}', encoding="utf-8")
    assert [w.name for w in load_path(tmp_path)] == ["a"]


def test_specs_are_never_modified(tmp_path):
    p = tmp_path / "a.md"
    p.write_text('---\nname: A\nschedule: "0 1 * * *"\n---\nrun `python3 x.py`\n', encoding="utf-8")
    before = (p.read_bytes(), p.stat().st_mtime_ns)
    load_path(tmp_path)
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before
