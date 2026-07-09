"""Tests for multi-model (maker/checker) orchestration (M3.5).

Uses a recording fake runner registered in place of the real vendor adapters so
the inter-stage and intra-run composition can be asserted without invoking any
CLI subprocess.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict

import pytest

import loopcraft.runners as runners_pkg
from loopcraft.config import LoopcraftConfig
from loopcraft.manifest import LoopManifest
from loopcraft.orchestrator import build_execution_plan, preflight_multi_model, run_multi_model
from loopcraft.runners import RunContext
from loopcraft.runners.base import BaseRunner, PreflightReport, RunResult, RunStatus


class CallRecord(TypedDict):
    """One recorded fake-runner invocation."""

    vendor: str
    model: str | None
    skill: str | None
    roles: dict[str, Any] | None
    extra_context: str
    write_outputs: list[str]


#: Records every fake-runner invocation across a test (reset by the fixture).
_CALLS: list[CallRecord] = []


class _FakeRunner(BaseRunner):
    """Records invocations and simulates a successful run per vendor."""

    vendor = "fake"

    def preflight(self, loop: LoopManifest, config: LoopcraftConfig) -> PreflightReport:
        return PreflightReport(vendor=self.vendor, ok=True)

    def build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
        return ["fake"]

    def run(self, loop: LoopManifest, ctx: RunContext) -> RunResult:
        vendor = str(loop.runtime.vendor) if loop.runtime.vendor else "?"
        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
        stdout = f"OUTPUT from {vendor}"
        ctx.log_path.write_text(
            f"$ fake\n\n--- PROMPT ---\np\n\n--- STDOUT ---\n{stdout}\n--- STDERR ---\n\n",
            encoding="utf-8",
        )
        # The runner writes only inside the worktree; the control plane promotes
        # these to the ledger.
        produced: list[str] = []
        for out in ctx.resolved_outputs:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("done", encoding="utf-8")
            produced.append(str(out))
        _CALLS.append(
            {
                "vendor": vendor,
                "model": loop.runtime.model,
                "skill": loop.logic.skill,
                "roles": loop.roles,
                "extra_context": ctx.extra_context,
                "write_outputs": produced,
            }
        )
        return RunResult(status=RunStatus.DONE, log_path=ctx.log_path, outputs=produced)


def _make_runner(name: str) -> type[BaseRunner]:
    """Build a fake runner subclass whose ``vendor`` is ``name``."""
    return type(f"_Fake_{name}", (_FakeRunner,), {"vendor": name})


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Swap the runner registry for fakes and reset recorded calls."""
    _CALLS.clear()
    original = dict(runners_pkg._RUNNERS)
    for name in ("codex", "claude", "cursor"):
        runners_pkg._RUNNERS[name] = _make_runner(name)
    yield
    runners_pkg._RUNNERS.clear()
    runners_pkg._RUNNERS.update(original)


def _source(tmp_path: Path) -> Path:
    """Create a temp source tree with implementer + reviewer agent defs."""
    source = tmp_path / "src"
    (source / "agents").mkdir(parents=True)
    (source / "agents" / "implementer.md").write_text(
        "---\nname: implementer\nreadonly: false\n---\nmake the change", encoding="utf-8"
    )
    (source / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\nreadonly: true\n---\nreview the change", encoding="utf-8"
    )
    return source


def _config(tmp_path: Path) -> LoopcraftConfig:
    return LoopcraftConfig(source_path=_source(tmp_path), memory_path=tmp_path / "mem")


def _manifest(**overrides: Any) -> LoopManifest:
    base: dict[str, Any] = {
        "id": "demo",
        "name": "Demo",
        "cadence": {"type": "cron", "at": "0 9 * * *"},
        "tier": "propose",
        "outputs": ["state/demo/out.md"],
        "roles": {
            "implementer": {"agent": "agents/implementer.md", "vendor": "codex", "model": "gpt-5.5"},
            "reviewer": {"agent": "agents/reviewer.md", "vendor": "claude", "model": "opus"},
        },
    }
    base.update(overrides)
    return LoopManifest.from_dict(base)


def _ctx(config: LoopcraftConfig, tmp_path: Path) -> RunContext:
    return RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "wt" / "run.log",
        resolved_outputs=[config.resolve_state_path("state/demo/out.md")],
        env={},
    )


def test_inter_stage_runs_roles_in_order_with_per_role_binding(tmp_path: Path) -> None:
    """Inter-stage runs implementer then reviewer, each on its own vendor/model."""
    config = _config(tmp_path)
    result = run_multi_model(_manifest(), config, _ctx(config, tmp_path), "codex")

    assert result.status == RunStatus.DONE
    assert [c["vendor"] for c in _CALLS] == ["codex", "claude"]
    assert _CALLS[0]["model"] == "gpt-5.5"
    assert _CALLS[1]["model"] == "opus"
    # Each stage carries its own agent behavior via the prompt context (not a
    # top-level skill file), so verify/read-only policy travel with the role.
    assert _CALLS[0]["skill"] is None
    assert "## Role: implementer" in _CALLS[0]["extra_context"]
    assert "make the change" in _CALLS[0]["extra_context"]
    assert "## Role: reviewer" in _CALLS[1]["extra_context"]


def test_inter_stage_hands_output_to_reviewer(tmp_path: Path) -> None:
    """The maker owns the declared outputs; the reviewer gets them as handoff."""
    config = _config(tmp_path)
    result = run_multi_model(_manifest(), config, _ctx(config, tmp_path), "codex")

    impl, review = _CALLS
    assert impl["write_outputs"]  # maker writes the declared output
    assert review["write_outputs"] == []  # read-only reviewer owns none here
    assert "OUTPUT from codex" in review["extra_context"]  # prior-stage handoff
    assert "READ-ONLY" in review["extra_context"]
    assert "out.md" in review["extra_context"]  # the maker's ledger path is shown
    # The maker's output is promoted to the ledger (control-plane provenance).
    assert result.outputs == [str(config.resolve_state_path("state/demo/out.md"))]
    assert config.resolve_state_path("state/demo/out.md").exists()


def test_inter_stage_reviewer_owns_its_review_output(tmp_path: Path) -> None:
    """A read-only reviewer with its own outputs writes them (not the maker's)."""
    config = _config(tmp_path)
    manifest = _manifest(
        roles={
            "implementer": {"agent": "agents/implementer.md", "vendor": "codex", "model": "gpt-5.5"},
            "reviewer": {
                "agent": "agents/reviewer.md",
                "vendor": "claude",
                "model": "opus",
                "outputs": ["state/demo/reviews/{{run_id}}.md"],
            },
        }
    )
    ctx = _ctx(config, tmp_path)
    ctx.env["LOOPCRAFT_RUN_ID"] = "20260101T000000Z"
    result = run_multi_model(manifest, config, ctx, "codex")

    # Maker owns the loop output; reviewer owns only its own review-notes path.
    # Both are promoted to the ledger.
    out_ledger = config.resolve_state_path("state/demo/out.md")
    review_ledger = config.resolve_state_path("state/demo/reviews/20260101T000000Z.md")
    assert out_ledger.exists()
    assert review_ledger.exists()
    assert set(result.outputs) == {str(out_ledger), str(review_ledger)}
    # The reviewer wrote only its own review notes, not the maker's output.
    _, review = _CALLS
    assert review["write_outputs"] == [
        str((tmp_path / "wt" / "outputs" / "demo" / "reviews" / "20260101T000000Z.md").resolve())
    ]


def test_intra_run_compiles_subagents_and_runs_once(tmp_path: Path) -> None:
    """Intra-run compiles role sub-agents and invokes a single Cursor harness."""
    config = _config(tmp_path)
    manifest = _manifest(execution="intra-run", runtime={"vendor": "cursor"})
    result = run_multi_model(manifest, config, _ctx(config, tmp_path), "codex")

    assert result.status == RunStatus.DONE
    assert len(_CALLS) == 1  # one harness invocation
    assert _CALLS[0]["vendor"] == "cursor"
    assert _CALLS[0]["roles"] is None  # harness runs single-model with sub-agents on disk
    assert "intra-run" in _CALLS[0]["extra_context"]
    wt = tmp_path / "wt"
    assert (wt / ".cursor/agents/implementer.md").exists()
    assert (wt / ".cursor/agents/reviewer.md").exists()


def test_override_vendor_applies_to_inherited_role(tmp_path: Path) -> None:
    """A --vendor override retargets an inherited role but not an explicit one."""
    config = _config(tmp_path)
    manifest = _manifest(
        roles={
            "implementer": {"agent": "agents/implementer.md"},  # inherits vendor
            "reviewer": {"agent": "agents/reviewer.md", "vendor": "claude"},  # explicit
        }
    )
    plan, problems = build_execution_plan(manifest, config, "codex", override_vendor="cursor")
    by_name = {stage.name: stage.vendor for stage in plan.stages}
    assert by_name["implementer"] == "cursor"  # inherited role honors the override
    assert by_name["reviewer"] == "claude"  # explicit role is untouched


def test_preflight_ok_when_binaries_and_agents_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Roles preflight passes when adapters, binaries, and agent files resolve."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    problems = preflight_multi_model(_manifest(), _config(tmp_path), "codex")
    assert problems == []


def test_preflight_flags_missing_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A role whose agent file is missing is reported."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    manifest = _manifest(
        roles={
            "implementer": {"agent": "agents/ghost.md", "vendor": "codex"},
            "reviewer": {"agent": "agents/reviewer.md", "vendor": "claude"},
        }
    )
    problems = preflight_multi_model(manifest, _config(tmp_path), "codex")
    assert any("ghost.md" in p for p in problems)


def test_preflight_flags_intra_run_cross_provider_without_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intra-run cross-provider with inherited (non-Cursor) harness is flagged."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    # Roles pin codex + claude; no runtime.vendor, so the harness is the default.
    manifest = _manifest(execution="intra-run")
    problems = preflight_multi_model(manifest, _config(tmp_path), "codex")
    assert any("intra-run cross-provider" in p for p in problems)
