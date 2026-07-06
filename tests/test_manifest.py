"""Tests for manifest parsing, validation, and dependency-cycle detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft.manifest import (
    LoopManifest,
    ManifestError,
    _detect_cycles,
    load_all,
    parse_duration,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _minimal(**overrides) -> dict:
    """Return a minimal valid manifest mapping, with ``overrides`` applied."""
    base = {
        "id": "demo",
        "name": "Demo",
        "description": "d",
        "runtime": {"vendor": "codex"},
        "locus": "vm",
        "cadence": {"type": "cron", "at": "0 9 * * *"},
        "tier": "observe",
        "logic": {"skill": "skills/demo/SKILL.md", "verify": "x"},
    }
    base.update(overrides)
    return base


def test_valid_manifest_has_no_problems() -> None:
    """A minimal well-formed manifest reports no validation problems."""
    manifest = LoopManifest.from_dict(_minimal())
    assert manifest.validate() == []


def test_missing_required_fields_are_reported() -> None:
    """Missing id and logic.skill are surfaced by validation."""
    manifest = LoopManifest.from_dict(_minimal(id="", logic={}))
    problems = manifest.validate()
    assert any("id" in p for p in problems)
    assert any("logic.skill" in p for p in problems)


@pytest.mark.parametrize(
    "bad_id",
    [
        "/tmp/loopcraft-escaped/run-id",  # absolute path
        "../outside",  # traversal
        "demo/../../x",  # embedded traversal
        "a/b",  # path separator
        "a b",  # whitespace
        "A-B",  # uppercase
        "a--b",  # double hyphen
        "-a",  # leading hyphen
        "a-",  # trailing hyphen
        "a_b",  # underscore
    ],
)
def test_non_canonical_loop_ids_are_rejected(bad_id: str) -> None:
    """Finding 1 (review 05): ids are lowercase hyphen-separated components."""
    manifest = LoopManifest.from_dict(_minimal(id=bad_id))
    assert any(p.startswith("id:") for p in manifest.validate()), bad_id


@pytest.mark.parametrize("good_id", ["demo", "slack-triage", "x2-intel", "a1-b2-c3"])
def test_canonical_loop_ids_are_accepted(good_id: str) -> None:
    """Canonical loop ids pass validation unchanged."""
    assert LoopManifest.from_dict(_minimal(id=good_id)).validate() == []


def test_load_all_reports_filename_id_mismatch(tmp_path: Path) -> None:
    """Finding 1 (review 05): manifest id must equal its filename stem."""
    loops = tmp_path / "loops"
    loops.mkdir()
    (loops / "demo.yaml").write_text(
        "id: other\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
        encoding="utf-8",
    )
    _, problems = load_all(loops)
    assert any("does not match filename stem" in p for p in problems)


def _write_loop(loops: Path, loop_id: str, outputs: list[str]) -> None:
    """Write a minimal valid manifest with the given id and outputs."""
    rendered = "".join(f"  - '{o}'\n" for o in outputs)
    (loops / f"{loop_id}.yaml").write_text(
        f"id: {loop_id}\n"
        f"name: {loop_id}\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        f"outputs:\n{rendered}"
        "logic: {skill: skills/demo/SKILL.md}\n",
        encoding="utf-8",
    )


def test_load_all_rejects_symlinked_manifest_escaping_loops_dir(tmp_path: Path) -> None:
    """Finding 1 (review 07): a symlinked manifest entry cannot leave loops/."""
    loops = tmp_path / "loops"
    loops.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.yaml").write_text(
        "id: evil\n"
        "name: Evil\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
        encoding="utf-8",
    )
    (loops / "evil.yaml").symlink_to(outside / "evil.yaml")

    manifests, problems = load_all(loops)
    assert not any(m.id == "evil" for m in manifests)
    assert any("escapes" in p for p in problems)


def test_load_all_reports_output_claimed_by_multiple_loops(tmp_path: Path) -> None:
    """Finding 5 (review 07): one producing loop per declared output path."""
    loops = tmp_path / "loops"
    loops.mkdir()
    _write_loop(loops, "loop-a", ["state/shared/out.md"])
    _write_loop(loops, "loop-b", ["state/shared/out.md"])

    _, problems = load_all(loops)
    assert any(
        "declared by multiple loops" in p and "loop-a" in p and "loop-b" in p
        for p in problems
    )


def test_load_all_reports_normalized_duplicate_outputs(tmp_path: Path) -> None:
    """Finding 5 (review 07): output collisions are detected on normalized paths."""
    loops = tmp_path / "loops"
    loops.mkdir()
    _write_loop(loops, "loop-a", ["state/shared/out.md"])
    # Same ledger file spelled differently; normalizes to the same path.
    _write_loop(loops, "loop-b", ["state/shared/./out.md"])

    _, problems = load_all(loops)
    assert any("declared by multiple loops" in p for p in problems)


def test_load_all_reports_duplicate_output_within_one_manifest(tmp_path: Path) -> None:
    """Finding 5 (review 07): duplicate entries inside one manifest are flagged."""
    loops = tmp_path / "loops"
    loops.mkdir()
    _write_loop(loops, "loop-a", ["state/shared/out.md", "state/shared/out.md"])

    _, problems = load_all(loops)
    assert any("declared more than once" in p for p in problems)
    # A within-manifest duplicate alone is not also a cross-loop collision.
    assert not any("declared by multiple loops" in p for p in problems)


def test_distinct_outputs_produce_no_duplicate_problems(tmp_path: Path) -> None:
    """Distinct outputs across loops stay problem-free."""
    loops = tmp_path / "loops"
    loops.mkdir()
    _write_loop(loops, "loop-a", ["state/a/out.md"])
    _write_loop(loops, "loop-b", ["state/b/out.md"])

    _, problems = load_all(loops)
    assert problems == []


def test_enum_validation() -> None:
    """Invalid enum values for tier/locus raise ManifestError."""
    with pytest.raises(ManifestError) as exc:
        LoopManifest.from_dict(_minimal(tier="boss", locus="moon"))
    assert "tier" in str(exc.value)
    assert "locus" in str(exc.value)


def test_cron_requires_at() -> None:
    """A cron cadence without ``at`` is reported as invalid."""
    manifest = LoopManifest.from_dict(_minimal(cadence={"type": "cron"}))
    assert any("cadence.at" in p for p in manifest.validate())


def test_effective_vendor_falls_back_to_default() -> None:
    """effective_vendor uses the manifest vendor, else the global default."""
    manifest = LoopManifest.from_dict(_minimal(runtime={}))
    assert manifest.effective_vendor("claude") == "claude"
    pinned = LoopManifest.from_dict(_minimal(runtime={"vendor": "codex"}))
    assert pinned.effective_vendor("claude") == "codex"


def test_cycle_detection_via_io_contract() -> None:
    """A producer/consumer loop in the I/O contract is flagged as a cycle."""
    a = LoopManifest.from_dict(_minimal(id="a", inputs=["state/x"], outputs=["state/y"]))
    b = LoopManifest.from_dict(_minimal(id="b", inputs=["state/y"], outputs=["state/x"]))
    problems = _detect_cycles([a, b]).messages()
    assert any("cycle" in p for p in problems)


def test_no_cycle_for_linear_chain() -> None:
    """A linear producer chain is not reported as a cycle."""
    a = LoopManifest.from_dict(_minimal(id="a", outputs=["state/y"]))
    b = LoopManifest.from_dict(_minimal(id="b", inputs=["state/y"], outputs=["state/z"]))
    assert _detect_cycles([a, b]).ok


def test_repo_slack_triage_manifest_is_valid() -> None:
    """All shipped manifests load cleanly and include the expected ids."""
    manifests, problems = load_all(REPO_ROOT / "loops")
    assert problems == []
    assert any(m.id == "slack-triage" for m in manifests)
    assert any(m.id == "arxiv-intel" for m in manifests)
    assert any(m.id == "x-intel" for m in manifests)


def test_slack_triage_declares_seen_cursor() -> None:
    """Finding 1: the seen.json cursor must be an explicit input and output."""
    manifests, _ = load_all(REPO_ROOT / "loops")
    slack = next(m for m in manifests if m.id == "slack-triage")
    assert "state/slack/seen.json" in slack.inputs
    assert "state/slack/seen.json" in slack.outputs
    assert "state/slack/triage-latest.md" in slack.outputs
    assert "state/slack/history/{{run_id}}.md" in slack.outputs


def test_research_intel_manifests_archive_latest_outputs() -> None:
    """The arXiv/X manifests reference content configs and archive history."""
    manifests, _ = load_all(REPO_ROOT / "loops")
    arxiv = next(m for m in manifests if m.id == "arxiv-intel")
    x_intel = next(m for m in manifests if m.id == "x-intel")
    assert arxiv.content.config == "config/arxiv_intel.yaml"
    assert "state/research/arxiv/history/{{run_id}}.md" in arxiv.outputs
    assert "state/research/arxiv/history/{{run_id}}.json" in arxiv.outputs
    assert x_intel.content.config == "config/x_intel.yaml"
    assert "state/research/x/history/{{run_id}}.md" in x_intel.outputs
    assert "state/research/x/history/{{run_id}}.json" in x_intel.outputs


def test_self_cursor_is_not_a_cycle() -> None:
    """A loop reading and writing its own cursor is not a dependency cycle."""
    loop = LoopManifest.from_dict(
        _minimal(inputs=["state/slack/seen.json"], outputs=["state/slack/seen.json"])
    )
    assert _detect_cycles([loop]).ok


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), ("30s", 30), ("10m", 600), ("1h", 3600), ("  5m ", 300)],
)
def test_parse_duration_valid(value, expected) -> None:
    """parse_duration converts s/m/h duration strings into seconds."""
    assert parse_duration(value) == expected


def test_parse_duration_invalid_raises() -> None:
    """parse_duration raises ValueError on an unparseable duration."""
    with pytest.raises(ValueError):
        parse_duration("forever")


def test_budget_max_runtime_seconds() -> None:
    """budget.max_runtime_s parses the configured runtime into seconds."""
    manifest = LoopManifest.from_dict(_minimal(budget={"max_runtime": "10m"}))
    assert manifest.budget.max_runtime_s == 600


def test_invalid_max_runtime_reported() -> None:
    """An unparseable budget.max_runtime is reported by validation."""
    manifest = LoopManifest.from_dict(_minimal(budget={"max_runtime": "soon"}))
    assert any("max_runtime" in p for p in manifest.validate())


def test_unsafe_state_paths_are_reported() -> None:
    """Finding 4: traversal/absolute output paths fail manifest validation."""
    manifest = LoopManifest.from_dict(
        _minimal(outputs=["state/../../escape.md", "/tmp/abs.md"])
    )
    problems = manifest.validate()
    assert any("'..'" in p for p in problems)
    assert any("absolute" in p for p in problems)


def test_scheme_outputs_are_rejected_in_m1() -> None:
    """Finding 3 (review 02): external sinks fail validation so run can't crash later."""
    manifest = LoopManifest.from_dict(_minimal(outputs=["linear:project/Daily"]))
    problems = manifest.validate()
    assert any("external sink" in p for p in problems)


def test_unprefixed_ledger_path_requires_state_prefix() -> None:
    """Finding 1 (review 03): manifest vocabulary is exactly 'state/...'."""
    bare = LoopManifest.from_dict(_minimal(outputs=["slack/out.md"]))
    assert any("must use the 'state/...' prefix" in p for p in bare.validate())

    ledger = LoopManifest.from_dict(_minimal(outputs=["ledger/runs/x.json"]))
    assert any("state/" in p for p in ledger.validate())


def test_state_prefixed_paths_are_accepted() -> None:
    """Properly ``state/``-prefixed inputs and outputs validate cleanly."""
    ok = LoopManifest.from_dict(
        _minimal(inputs=["state/slack/seen.json"], outputs=["state/slack/out.md"])
    )
    assert ok.validate() == []


def test_x_manifest_lets_auth_probe_own_credential_choice() -> None:
    """Finding 3 (review 08): the manifest must not pin one exact X credential.

    The runtime and the x-api auth probe accept either X_API_BEARER_TOKEN or
    X_API_OAUTH2_ACCESS_TOKEN, so the manifest delegates the requirement to the
    auth bundle instead of contradicting them with an exact env declaration.
    """
    manifests, _ = load_all(REPO_ROOT / "loops")
    x_intel = next(m for m in manifests if m.id == "x-intel")
    assert x_intel.depends_on.env == []
    assert "x-api" in x_intel.depends_on.auth


def test_slack_verify_file_mentions_cursor() -> None:
    """The dedicated verify file names the seen.json cursor as a stop condition."""
    text = (REPO_ROOT / "skills" / "slack-triage" / "verify.md").read_text(encoding="utf-8")
    assert "state/slack/seen.json" in text


def test_research_skills_do_not_self_invoke_loopctl() -> None:
    """A loop's own skill must never tell its headless run to re-enter the loop.

    Instructing the in-loop agent to run `loopctl run <self>` / `make run
    LOOP=<self>` would recurse; the research skills must use the direct CLI.
    """
    cases = [
        ("arxiv-intel", "arxiv-intelligence-reporting", "loopcraft.research_intel.arxiv.cli"),
        ("x-intel", "x-intelligence-reporting", "loopcraft.research_intel.x.cli"),
    ]
    for loop_id, skill_dir, direct_module in cases:
        text = (REPO_ROOT / "skills" / skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert f"make run LOOP={loop_id}" not in text
        assert f"loopctl run {loop_id}" not in text
        assert f"loopcraft.cli run {loop_id}" not in text
        # It must point at the deterministic direct CLI and warn against recursion.
        assert f"{direct_module} run" in text
        assert "recurse" in text.lower()


def test_shipped_loops_reference_existing_verify_files() -> None:
    """Every shipped manifest points logic.verify at a real, colocated file."""
    loops_dir = REPO_ROOT / "loops"
    manifests = [LoopManifest.load(p) for p in sorted(loops_dir.glob("*.yaml"))]
    assert manifests
    for manifest in manifests:
        assert manifest.logic.verify, f"{manifest.id} missing logic.verify"
        assert (REPO_ROOT / manifest.logic.verify).is_file()


def test_unsafe_skill_paths_are_reported() -> None:
    """Finding 2 (review 02): logic.skill must be a safe source-relative path."""
    traversing = LoopManifest.from_dict(_minimal(logic={"skill": "../outside/SKILL.md"}))
    assert any("logic.skill" in p and "'..'" in p for p in traversing.validate())

    absolute = LoopManifest.from_dict(_minimal(logic={"skill": "/etc/SKILL.md"}))
    assert any("logic.skill" in p and "absolute" in p for p in absolute.validate())


def test_unknown_upstream_loop_is_reported() -> None:
    """A depends_on.loops reference to an unknown loop is reported."""
    manifest = LoopManifest.from_dict(_minimal(depends_on={"loops": ["ghost"]}))
    # load_all wires the cross-manifest checks; emulate via a temp single-loop set.
    _, problems = _single(manifest)
    assert any("ghost" in p for p in problems)


def _single(manifest: LoopManifest):
    """Mirror load_all's cross-manifest checks for one in-memory manifest."""
    problems: list[str] = []
    ids = {manifest.id}
    for upstream in manifest.depends_on.loops:
        if upstream not in ids:
            problems.append(f"{manifest.id}: unknown loop '{upstream}'")
    return [manifest], problems
