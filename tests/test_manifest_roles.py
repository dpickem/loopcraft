"""Tests for the multi-model `roles` manifest block and its validation (M3.5)."""

from __future__ import annotations

from loopcraft.manifest import ExecutionMode, LoopManifest, Vendor


def _base(**overrides) -> dict:
    """Return a minimal valid manifest mapping with ``overrides`` merged in."""
    base = {
        "id": "build-ship",
        "name": "Build/ship",
        "cadence": {"type": "cron", "at": "0 9 * * *"},
        "tier": "propose",
        "outputs": ["state/build/out.md"],
        "roles": {
            "implementer": {"agent": "agents/implementer.md", "vendor": "codex", "model": "gpt-5.5"},
            "reviewer": {"agent": "agents/reviewer.md", "vendor": "claude", "model": "opus"},
        },
    }
    base.update(overrides)
    return base


def test_roles_parse_and_order_preserved() -> None:
    """Roles parse into typed models and keep declaration order."""
    manifest = LoopManifest.from_dict(_base())
    assert manifest.is_multi_model
    assert manifest.execution == ExecutionMode.INTER_STAGE  # default
    names = [name for name, _ in manifest.ordered_roles()]
    assert names == ["implementer", "reviewer"]
    assert manifest.roles["reviewer"].vendor == Vendor.CLAUDE


def test_role_vendor_inheritance() -> None:
    """A role with no vendor inherits runtime.vendor, then the global default."""
    manifest = LoopManifest.from_dict(
        _base(
            runtime={"vendor": "claude"},
            roles={"solo": {"agent": "agents/implementer.md"}},
        )
    )
    role = manifest.roles["solo"]
    assert manifest.role_vendor(role, "codex") == "claude"  # from runtime.vendor
    manifest2 = LoopManifest.from_dict(_base(roles={"solo": {"agent": "agents/implementer.md"}}))
    assert manifest2.role_vendor(manifest2.roles["solo"], "codex") == "codex"  # global default


def test_roles_make_top_level_skill_optional() -> None:
    """A roles loop validates with no top-level logic.skill."""
    manifest = LoopManifest.from_dict(_base())
    assert manifest.validate() == []


def test_single_model_still_requires_skill() -> None:
    """Without roles, a missing logic.skill is still a validation error."""
    manifest = LoopManifest.from_dict(
        {
            "id": "x",
            "name": "X",
            "cadence": {"type": "cron", "at": "0 9 * * *"},
            "outputs": ["state/x/out.md"],
        }
    )
    assert any("logic.skill" in problem for problem in manifest.validate())


def test_empty_roles_rejected() -> None:
    """A declared-but-empty roles mapping is a validation error."""
    manifest = LoopManifest.from_dict(_base(roles={}))
    # An empty dict is falsy, so it is treated as a single-model loop that now
    # lacks a skill; either way validation must fail.
    assert manifest.validate()


def test_bad_agent_path_rejected() -> None:
    """A role agent path that escapes the source tree is rejected."""
    manifest = LoopManifest.from_dict(
        _base(roles={"impl": {"agent": "../../etc/passwd", "vendor": "codex"}})
    )
    assert any("roles.impl.agent" in problem for problem in manifest.validate())


def test_role_outputs_state_path_ok() -> None:
    """A role may declare its own state/... outputs (e.g. reviewer notes)."""
    manifest = LoopManifest.from_dict(
        _base(
            roles={
                "implementer": {"agent": "agents/implementer.md", "vendor": "codex"},
                "reviewer": {
                    "agent": "agents/reviewer.md",
                    "vendor": "claude",
                    "outputs": ["state/build/reviews/{{run_id}}.md"],
                },
            }
        )
    )
    assert manifest.validate() == []
    assert manifest.roles["reviewer"].outputs == ["state/build/reviews/{{run_id}}.md"]


def test_role_outputs_reject_non_state_prefix() -> None:
    """A role output without the state/ prefix is rejected."""
    manifest = LoopManifest.from_dict(
        _base(
            roles={
                "reviewer": {"agent": "agents/reviewer.md", "outputs": ["reviews/out.md"]},
            }
        )
    )
    assert any("roles.reviewer.outputs" in problem for problem in manifest.validate())


def test_intra_run_cross_provider_requires_cursor() -> None:
    """Intra-run with two explicit vendors and a non-Cursor harness is rejected."""
    manifest = LoopManifest.from_dict(
        _base(execution="intra-run", runtime={"vendor": "codex"})
    )
    assert any("intra-run cross-provider" in problem for problem in manifest.validate())


def test_intra_run_cross_provider_on_cursor_ok() -> None:
    """Intra-run cross-provider validates when the harness is Cursor."""
    manifest = LoopManifest.from_dict(
        _base(execution="intra-run", runtime={"vendor": "cursor"})
    )
    assert manifest.validate() == []
