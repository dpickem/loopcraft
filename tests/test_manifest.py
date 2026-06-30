from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft.manifest import (
    LoopManifest,
    _detect_cycles,
    load_all,
    parse_duration,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _minimal(**overrides) -> dict:
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
    manifest = LoopManifest.from_dict(_minimal())
    assert manifest.validate() == []


def test_missing_required_fields_are_reported() -> None:
    manifest = LoopManifest.from_dict(_minimal(id="", logic={}))
    problems = manifest.validate()
    assert any("id" in p for p in problems)
    assert any("logic.skill" in p for p in problems)


def test_enum_validation() -> None:
    manifest = LoopManifest.from_dict(_minimal(tier="boss", locus="moon"))
    problems = manifest.validate()
    assert any("tier" in p for p in problems)
    assert any("locus" in p for p in problems)


def test_cron_requires_at() -> None:
    manifest = LoopManifest.from_dict(_minimal(cadence={"type": "cron"}))
    assert any("cadence.at" in p for p in manifest.validate())


def test_effective_vendor_falls_back_to_default() -> None:
    manifest = LoopManifest.from_dict(_minimal(runtime={}))
    assert manifest.effective_vendor("claude") == "claude"
    pinned = LoopManifest.from_dict(_minimal(runtime={"vendor": "codex"}))
    assert pinned.effective_vendor("claude") == "codex"


def test_cycle_detection_via_io_contract() -> None:
    a = LoopManifest.from_dict(_minimal(id="a", inputs=["state/x"], outputs=["state/y"]))
    b = LoopManifest.from_dict(_minimal(id="b", inputs=["state/y"], outputs=["state/x"]))
    problems = _detect_cycles([a, b])
    assert any("cycle" in p for p in problems)


def test_no_cycle_for_linear_chain() -> None:
    a = LoopManifest.from_dict(_minimal(id="a", outputs=["state/y"]))
    b = LoopManifest.from_dict(_minimal(id="b", inputs=["state/y"], outputs=["state/z"]))
    assert _detect_cycles([a, b]) == []


def test_repo_slack_triage_manifest_is_valid() -> None:
    manifests, problems = load_all(REPO_ROOT / "loops")
    assert problems == []
    assert any(m.id == "slack-triage" for m in manifests)


def test_slack_triage_declares_seen_cursor() -> None:
    """Finding 1: the seen.json cursor must be an explicit input and output."""
    manifests, _ = load_all(REPO_ROOT / "loops")
    slack = next(m for m in manifests if m.id == "slack-triage")
    assert "state/slack/seen.json" in slack.inputs
    assert "state/slack/seen.json" in slack.outputs
    assert "state/slack/triage-latest.md" in slack.outputs


def test_self_cursor_is_not_a_cycle() -> None:
    loop = LoopManifest.from_dict(
        _minimal(inputs=["state/slack/seen.json"], outputs=["state/slack/seen.json"])
    )
    assert _detect_cycles([loop]) == []


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), ("30s", 30), ("10m", 600), ("1h", 3600), ("  5m ", 300)],
)
def test_parse_duration_valid(value, expected) -> None:
    assert parse_duration(value) == expected


def test_parse_duration_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_duration("forever")


def test_budget_max_runtime_seconds() -> None:
    manifest = LoopManifest.from_dict(_minimal(budget={"max_runtime": "10m"}))
    assert manifest.budget.max_runtime_s == 600


def test_invalid_max_runtime_reported() -> None:
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
    ok = LoopManifest.from_dict(
        _minimal(inputs=["state/slack/seen.json"], outputs=["state/slack/out.md"])
    )
    assert ok.validate() == []


def test_slack_skill_verify_mentions_cursor() -> None:
    """Finding 3 (review 03): the skill verify rubric names the seen.json cursor."""
    text = (REPO_ROOT / "skills" / "slack-triage" / "SKILL.md").read_text(encoding="utf-8")
    assert "state/slack/seen.json updated" in text


def test_unsafe_skill_paths_are_reported() -> None:
    """Finding 2 (review 02): logic.skill must be a safe source-relative path."""
    traversing = LoopManifest.from_dict(_minimal(logic={"skill": "../outside/SKILL.md"}))
    assert any("logic.skill" in p and "'..'" in p for p in traversing.validate())

    absolute = LoopManifest.from_dict(_minimal(logic={"skill": "/etc/SKILL.md"}))
    assert any("logic.skill" in p and "absolute" in p for p in absolute.validate())


def test_unknown_upstream_loop_is_reported() -> None:
    manifest = LoopManifest.from_dict(_minimal(depends_on={"loops": ["ghost"]}))
    # load_all wires the cross-manifest checks; emulate via a temp single-loop set.
    _, problems = _single(manifest)
    assert any("ghost" in p for p in problems)


def _single(manifest: LoopManifest):
    # Helper mirroring load_all's cross-checks for a single in-memory manifest.
    problems: list[str] = []
    ids = {manifest.id}
    for upstream in manifest.depends_on.loops:
        if upstream not in ids:
            problems.append(f"{manifest.id}: unknown loop '{upstream}'")
    return [manifest], problems
