"""CLI-level tests for multi-model (roles) loops (M3.5 review 01, finding 17).

These exercise the real control-plane paths — ``loopctl run``, ``deps check
--loop``, and ``apply`` — with a roles manifest and stub adapters, so the
multi-model preflight, promotion-on-success, read-only enforcement, and
per-role binding are covered end to end rather than only through
``run_multi_model``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import loopcraft.runners as runners_pkg
from loopcraft import cli
from loopcraft.config import LoopcraftConfig
from loopcraft.runners import register_runner
from loopcraft.runners.base import BaseRunner, PreflightReport, RunContext, RunResult, RunStatus

_MANIFEST = """\
id: build-ship
name: Build/ship
cadence:
  type: cron
  at: "0 9 * * *"
tier: propose
outputs:
  - state/build/out.md
roles:
  implementer:
    agent: agents/implementer.md
    vendor: codex
    model: gpt-5.5
  reviewer:
    agent: agents/reviewer.md
    vendor: claude
    model: opus
    outputs:
      - state/build/reviews/{{run_id}}.md
"""


class _MakerStub(BaseRunner):
    """A maker adapter that writes its declared worktree outputs."""

    vendor = "codex"

    def preflight(self, loop, config) -> PreflightReport:  # noqa: ANN001
        return PreflightReport(vendor=self.vendor, ok=True)

    def build_command(self, loop, ctx) -> list[str]:  # noqa: ANN001
        return ["stub"]

    def run(self, loop, ctx: RunContext) -> RunResult:  # noqa: ANN001
        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.log_path.write_text("--- STDOUT ---\nmade it\n--- STDERR ---\n", encoding="utf-8")
        produced = []
        for out in ctx.resolved_outputs:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("implemented", encoding="utf-8")
            produced.append(str(out))
        return RunResult(status=RunStatus.DONE, exit_code=0, log_path=ctx.log_path, outputs=produced)


class _ReviewerStub(_MakerStub):
    """A reviewer adapter that writes an explicit PASS verdict to its output."""

    vendor = "claude"

    def run(self, loop, ctx: RunContext) -> RunResult:  # noqa: ANN001
        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.log_path.write_text("--- STDOUT ---\nreviewed\n--- STDERR ---\n", encoding="utf-8")
        produced = []
        for out in ctx.resolved_outputs:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("Blockers: none\nVerdict: PASS\n", encoding="utf-8")
            produced.append(str(out))
        return RunResult(status=RunStatus.DONE, exit_code=0, log_path=ctx.log_path, outputs=produced)


@pytest.fixture(autouse=True)
def _stub_registry() -> Iterator[None]:
    """Restore the runner registry after each test."""
    original = dict(runners_pkg._RUNNERS)
    yield
    runners_pkg._RUNNERS.clear()
    runners_pkg._RUNNERS.update(original)


def _source(tmp_path: Path, manifest: str = _MANIFEST) -> Path:
    """Build a source tree with a roles manifest and both agent definitions."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "agents").mkdir(parents=True)
    (source / "loops" / "build-ship.yaml").write_text(manifest, encoding="utf-8")
    (source / "agents" / "implementer.md").write_text(
        "---\nname: implementer\ntools: [repo-read, repo-write]\n---\nbuild it", encoding="utf-8"
    )
    (source / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\nreadonly: true\ntools: [repo-read]\n"
        'verify: "explicit PASS or FAIL"\n---\nreview it',
        encoding="utf-8",
    )
    return source


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest: str = _MANIFEST) -> Path:
    """Point loopctl at a roles source tree; make all vendor binaries resolve."""
    source = _source(tmp_path, manifest)
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    return source


def test_run_roles_inter_stage_promotes_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`loopctl run` on a roles loop runs both stages and promotes both outputs."""
    _env(monkeypatch, tmp_path)
    register_runner("codex", _MakerStub)
    register_runner("claude", _ReviewerStub)

    rc = cli.main(["run", "build-ship"])
    assert rc == 0
    assert (tmp_path / "mem" / "ledger" / "build" / "out.md").exists()
    reviews = list((tmp_path / "mem" / "ledger" / "build" / "reviews").glob("*.md"))
    assert len(reviews) == 1
    assert "PASS" in reviews[0].read_text(encoding="utf-8")


def test_deps_check_roles_flags_missing_role_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`deps check --loop` uses multi-model preflight (finding 1): missing role agent fails."""
    source = _env(monkeypatch, tmp_path)
    (source / "agents" / "reviewer.md").unlink()
    register_runner("codex", _MakerStub)
    register_runner("claude", _ReviewerStub)

    rc = cli.main(["--json", "deps", "check", "--loop", "build-ship"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    preflight = payload["data"]["preflight"]
    assert preflight["ok"] is False
    assert any("reviewer" in p for p in preflight["problems"])


def test_apply_roles_reports_missing_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`apply` runs multi-model preflight; a missing role binary is reported."""
    _env(monkeypatch, tmp_path)
    register_runner("codex", _MakerStub)
    register_runner("claude", _ReviewerStub)
    # No binary resolves -> each role's binary check fails at apply preflight.
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: None)

    rc = cli.main(["--json", "apply", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any("not found on PATH" in p for p in payload["data"]["preflight_problems"])


def test_run_roles_reviewer_mutation_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read-only reviewer that mutates a maker output fails the run (finding 3)."""
    _env(monkeypatch, tmp_path)

    class _MutatingReviewer(_MakerStub):
        vendor = "claude"

        def run(self, loop, ctx: RunContext) -> RunResult:  # noqa: ANN001
            ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
            ctx.log_path.write_text("--- STDOUT ---\nx\n--- STDERR ---\n", encoding="utf-8")
            # Tamper with the maker's staged output (a protected file).
            maker_out = ctx.workdir / "outputs" / "build" / "out.md"
            if maker_out.exists():
                maker_out.write_text("TAMPERED", encoding="utf-8")
            for out in ctx.resolved_outputs:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("Verdict: PASS", encoding="utf-8")
            return RunResult(status=RunStatus.DONE, exit_code=0, log_path=ctx.log_path)

    register_runner("codex", _MakerStub)
    register_runner("claude", _MutatingReviewer)

    rc = cli.main(["run", "build-ship"])
    assert rc == 1


def test_run_roles_failed_maker_not_promoted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed maker stage does not promote partial output to the ledger (finding 5)."""
    _env(monkeypatch, tmp_path)

    class _FailingMaker(_MakerStub):
        vendor = "codex"

        def run(self, loop, ctx: RunContext) -> RunResult:  # noqa: ANN001
            ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
            ctx.log_path.write_text("--- STDOUT ---\nboom\n--- STDERR ---\n", encoding="utf-8")
            # Write a partial output but report failure.
            for out in ctx.resolved_outputs:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("partial", encoding="utf-8")
            return RunResult(status=RunStatus.FAILED, exit_code=1, log_path=ctx.log_path)

    register_runner("codex", _FailingMaker)
    register_runner("claude", _ReviewerStub)

    rc = cli.main(["run", "build-ship"])
    assert rc == 1
    assert not (tmp_path / "mem" / "ledger" / "build" / "out.md").exists()
