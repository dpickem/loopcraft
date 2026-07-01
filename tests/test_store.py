from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft.config import LoopcraftConfig, StatePathError, safe_state_relpath
from loopcraft.store import RunRecord, Store


def _config(tmp_path: Path) -> LoopcraftConfig:
    return LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        default_vendor="codex",
    )


def test_state_prefix_maps_into_ledger(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config)
    path = store.write_state("state/slack/triage-latest.md", "# hi\n")
    assert path == config.memory_path / "ledger" / "slack" / "triage-latest.md"
    assert path.read_text(encoding="utf-8") == "# hi\n"
    assert store.state_exists("state/slack/triage-latest.md")
    assert store.read_state("state/slack/triage-latest.md") == "# hi\n"


def test_ledger_relative_path_without_prefix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config)
    path = store.write_state("research/themes.md", "x")
    assert path == config.memory_path / "ledger" / "research" / "themes.md"


def test_append_jsonl(tmp_path: Path) -> None:
    store = Store(_config(tmp_path))
    store.append_jsonl("research/queue.jsonl", {"a": 1})
    store.append_jsonl("research/queue.jsonl", {"a": 2})
    text = store.read_state("research/queue.jsonl")
    assert text is not None and text.count("\n") == 2


@pytest.mark.parametrize(
    "bad",
    ["/tmp/out.md", "state/../../escape.md", "../escape.md", "state/..", ""],
)
def test_resolve_state_path_rejects_escapes(tmp_path: Path, bad: str) -> None:
    config = _config(tmp_path)
    with pytest.raises(StatePathError):
        config.resolve_state_path(bad)


def test_store_write_rejects_escaping_path(tmp_path: Path) -> None:
    store = Store(_config(tmp_path))
    with pytest.raises(StatePathError):
        store.write_state("../../escape.md", "x")
    with pytest.raises(StatePathError):
        store.append_jsonl("/tmp/abs.jsonl", {"a": 1})


def test_safe_state_relpath_strips_prefixes() -> None:
    assert safe_state_relpath("state/slack/x.md") == "slack/x.md"
    assert safe_state_relpath("ledger/runs/y.json") == "runs/y.json"
    assert safe_state_relpath("research/themes.md") == "research/themes.md"


def test_resolve_state_template_expands_run_id_and_date(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = config.resolve_state_template(
        "state/slack/history/{{date}}/{{run_id}}.md",
        run_id="20260701T120000Z-abc12345",
        date="2026-07-01",
    )
    assert path == (
        config.memory_path
        / "ledger"
        / "slack"
        / "history"
        / "2026-07-01"
        / "20260701T120000Z-abc12345.md"
    )


def test_record_and_read_runs(tmp_path: Path) -> None:
    store = Store(_config(tmp_path))
    rid = store.new_run_id()
    record = RunRecord(
        run_id=rid,
        loop="slack-triage",
        vendor="codex",
        model="gpt-5.5-medium",
        status="done",
        started_at="2026-06-30T00:00:00+00:00",
    )
    out = store.record_run(record)
    assert out.exists()
    latest = store.latest_run("slack-triage")
    assert latest is not None
    assert latest.run_id == rid
    assert latest.status == "done"
    assert store.latest_run("other") is None


def test_config_worktree_keep_last_defaults_and_clamps(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "loopcraft.toml").write_text(
        'memory_path = "mem"\nworktree_keep_last = 500\n', encoding="utf-8"
    )
    monkeypatch.delenv("LOOPCRAFT_WORKTREE_KEEP_LAST", raising=False)
    config = LoopcraftConfig.load(source)
    assert config.worktree_keep_last == 100

    monkeypatch.setenv("LOOPCRAFT_WORKTREE_KEEP_LAST", "2")
    assert LoopcraftConfig.load(source).worktree_keep_last == 2

    monkeypatch.setenv("LOOPCRAFT_WORKTREE_KEEP_LAST", "-5")
    assert LoopcraftConfig.load(source).worktree_keep_last == 0
