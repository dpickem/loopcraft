"""Tests for the ledger store, run records, and state-path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft.config import LoopcraftConfig, StatePathError, safe_state_relpath
from loopcraft.store import RunRecord, Store


def _config(tmp_path: Path) -> LoopcraftConfig:
    """Return a LoopcraftConfig rooted at temp source/memory trees."""
    return LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        default_vendor="codex",
    )


def test_state_prefix_maps_into_ledger(tmp_path: Path) -> None:
    """A ``state/`` path resolves into the memory ledger and round-trips."""
    config = _config(tmp_path)
    store = Store(config)
    path = store.write_state("state/slack/triage-latest.md", "# hi\n")
    assert path == config.memory_path / "ledger" / "slack" / "triage-latest.md"
    assert path.read_text(encoding="utf-8") == "# hi\n"
    assert store.state_exists("state/slack/triage-latest.md")
    assert store.read_state("state/slack/triage-latest.md") == "# hi\n"


def test_ledger_relative_path_without_prefix(tmp_path: Path) -> None:
    """A bare ledger-relative path resolves under the ledger directory."""
    config = _config(tmp_path)
    store = Store(config)
    path = store.write_state("research/themes.md", "x")
    assert path == config.memory_path / "ledger" / "research" / "themes.md"


def test_append_jsonl(tmp_path: Path) -> None:
    """append_jsonl appends one line per record to a ledger JSONL file."""
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
    """Absolute, traversing, and empty state paths are rejected."""
    config = _config(tmp_path)
    with pytest.raises(StatePathError):
        config.resolve_state_path(bad)


def test_store_write_rejects_escaping_path(tmp_path: Path) -> None:
    """Store writes reject paths that would escape the ledger tree."""
    store = Store(_config(tmp_path))
    with pytest.raises(StatePathError):
        store.write_state("../../escape.md", "x")
    with pytest.raises(StatePathError):
        store.append_jsonl("/tmp/abs.jsonl", {"a": 1})


def test_store_write_rejects_symlinked_ledger_parent(tmp_path: Path) -> None:
    """Finding 1 (review 06): a ledger symlink cannot redirect writes outside.

    ``<ledger>/demo`` pointing at a directory outside the memory tree must be
    refused, not followed.
    """
    config = _config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    config.ledger_dir.mkdir(parents=True)
    (config.ledger_dir / "demo").symlink_to(outside, target_is_directory=True)

    store = Store(config)
    with pytest.raises(StatePathError, match="escapes"):
        store.write_state("state/demo/out.txt", "x")
    assert not (outside / "out.txt").exists()


def test_ledger_symlink_resolving_inside_memory_tree_is_allowed(tmp_path: Path) -> None:
    """An in-ledger symlink whose target stays under the ledger is allowed."""
    config = _config(tmp_path)
    (config.ledger_dir / "real").mkdir(parents=True)
    (config.ledger_dir / "alias").symlink_to(config.ledger_dir / "real", target_is_directory=True)

    store = Store(config)
    store.write_state("state/alias/out.txt", "x")
    assert (config.ledger_dir / "real" / "out.txt").exists()


def test_safe_state_relpath_strips_prefixes() -> None:
    """safe_state_relpath strips an optional state/ or ledger/ prefix."""
    assert safe_state_relpath("state/slack/x.md") == "slack/x.md"
    assert safe_state_relpath("ledger/runs/y.json") == "runs/y.json"
    assert safe_state_relpath("research/themes.md") == "research/themes.md"


def test_resolve_state_template_expands_run_id_and_date(tmp_path: Path) -> None:
    """resolve_state_template expands {{run_id}} and {{date}} placeholders."""
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
    """A recorded run is written with a loop-prefixed name and read back."""
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
    assert out.name.startswith("slack-triage__")
    assert out.name.endswith(f"{rid}.json")
    latest = store.latest_run("slack-triage")
    assert latest is not None
    assert latest.run_id == rid
    assert latest.status == "done"
    assert store.latest_run("other") is None


def test_reads_legacy_run_record_filenames(tmp_path: Path) -> None:
    """Run records written with the legacy filename scheme are still read."""
    store = Store(_config(tmp_path))
    rid = "20260701T120000Z-legacy"
    store.config.runs_dir.mkdir(parents=True)
    legacy = store.config.runs_dir / f"{rid}.json"
    legacy.write_text(
        """{
  "run_id": "20260701T120000Z-legacy",
  "loop": "arxiv-intel",
  "vendor": "codex",
  "model": null,
  "status": "done",
  "started_at": "2026-07-01T12:00:00+00:00"
}
""",
        encoding="utf-8",
    )

    latest = store.latest_run("arxiv-intel")
    assert latest is not None
    assert latest.run_id == rid


def test_runs_for_skips_schema_invalid_records(tmp_path: Path) -> None:
    """Finding 5 (review 06): one bad history file must not crash history reads.

    Valid JSON with the matching loop id but missing required fields or wrong
    field types is skipped like malformed JSON, so the remaining valid history
    stays readable.
    """
    store = Store(_config(tmp_path))
    store.config.runs_dir.mkdir(parents=True)
    # Wrong field type for a required field.
    (store.config.runs_dir / "demo__bad-type.json").write_text(
        '{"loop": "demo", "status": 42}\n', encoding="utf-8"
    )
    # Missing required fields entirely.
    (store.config.runs_dir / "demo__missing.json").write_text(
        '{"loop": "demo"}\n', encoding="utf-8"
    )
    # Valid JSON that is not even an object.
    (store.config.runs_dir / "demo__list.json").write_text("[1, 2]\n", encoding="utf-8")
    # One valid record among the corrupt ones.
    good = RunRecord(
        run_id="20260702T000000Z-aaaa1111",
        loop="demo",
        vendor="codex",
        status="done",
        started_at="2026-07-02T00:00:00+00:00",
    )
    store.record_run(good)

    records = store.runs_for("demo")
    assert [r.run_id for r in records] == ["20260702T000000Z-aaaa1111"]
    latest = store.latest_run("demo")
    assert latest is not None and latest.status == "done"


def test_record_run_write_is_atomic(tmp_path: Path) -> None:
    """Run records land via temp-file + rename and leave no .tmp behind."""
    store = Store(_config(tmp_path))
    record = RunRecord(
        run_id="20260702T000000Z-bbbb2222",
        loop="demo",
        vendor="codex",
        status="done",
        started_at="2026-07-02T00:00:00+00:00",
    )
    path = store.record_run(record)
    assert path.exists()
    assert not list(store.config.runs_dir.glob("*.tmp"))


def test_config_worktree_keep_last_defaults_and_clamps(tmp_path: Path, monkeypatch) -> None:
    """worktree_keep_last is clamped to 0..100 across toml/env sources."""
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


def test_config_loads_cli_dependency_table(tmp_path: Path, monkeypatch) -> None:
    """CLI dependencies load from [tool.loopcraft.dependencies] in pyproject."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "loopcraft.toml").write_text(
        'memory_path = "mem"\n',
        encoding="utf-8",
    )
    (source / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "loopcraft"',
                'version = "0.1.0"',
                "",
                "[tool.loopcraft.dependencies]",
                'codex = "/opt/codex/bin/codex"',
                'custom-tool = "custom-tool"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("LOOPCRAFT_WORKTREE_KEEP_LAST", raising=False)
    config = LoopcraftConfig.load(source)

    assert config.dependencies["codex"] == "/opt/codex/bin/codex"
    assert config.dependencies["custom-tool"] == "custom-tool"
    assert "git" not in config.dependencies
