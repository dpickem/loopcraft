"""Tests for the Codex runner preflight, command/prompt build, and run loop."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from loopcraft.config import LoopcraftConfig
from loopcraft.manifest import LoopManifest
from loopcraft.runners import capabilities as capabilities_module
from loopcraft.runners import base as runner_base_module
from loopcraft.runners import available_vendors, get_runner
from loopcraft.runners.base import (
    RunContext,
    RunStatus,
)
from loopcraft.runners.claude import ClaudeRunner
from loopcraft.runners.codex import CodexRunner
from loopcraft.runners.cursor import CursorRunner


def _config(tmp_path: Path) -> LoopcraftConfig:
    """Return a config with a demo skill + verify file staged under tmp_path."""
    source = tmp_path / "src"
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("do the thing", encoding="utf-8")
    (source / "skills" / "demo" / "verify.md").write_text("out exists", encoding="utf-8")
    return LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")


def _manifest(**overrides) -> LoopManifest:
    """Return a demo loop manifest, with ``overrides`` merged in."""
    base = {
        "id": "demo",
        "name": "Demo",
        "description": "d",
        "runtime": {"vendor": "codex", "model": "gpt-5.5-medium"},
        "cadence": {"type": "cron", "at": "0 9 * * *"},
        "tier": "observe",
        "outputs": ["state/demo/out.md"],
        "logic": {
            "skill": "skills/demo/SKILL.md",
            "verify": "skills/demo/verify.md",
        },
    }
    base.update(overrides)
    return LoopManifest.from_dict(base)


def test_preflight_reports_missing_skill(tmp_path: Path) -> None:
    """Preflight fails when the declared skill file does not exist."""
    config = _config(tmp_path)
    manifest = _manifest(logic={"skill": "skills/ghost/SKILL.md"})
    report = CodexRunner().preflight(manifest, config)
    assert not report.ok
    assert any("skill not found" in p for p in report.problems)


def test_preflight_reports_missing_env(tmp_path: Path) -> None:
    """Preflight reports a required env var that is not set."""
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"env": ["LOOPCRAFT_DEFINITELY_UNSET_VAR"]})
    report = CodexRunner().preflight(manifest, config)
    assert any("LOOPCRAFT_DEFINITELY_UNSET_VAR" in p for p in report.problems)


def test_run_writes_log_and_reports_done(tmp_path: Path, monkeypatch) -> None:
    """A run that produces its output writes a log and reports done."""
    config = _config(tmp_path)
    manifest = _manifest()
    output = config.resolve_state_path("state/demo/out.md")
    ctx = RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "wt" / "run.log",
        resolved_outputs=[output],
    )
    ctx.workdir.mkdir(parents=True)

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        """Stubbed subprocess.run that writes the declared output and exits 0."""
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# digest\n- item", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner_base_module.subprocess, "run", fake_run)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == RunStatus.DONE
    assert result.exit_code == 0
    assert str(output) in result.outputs
    assert ctx.log_path.exists()
    assert "PROMPT" in ctx.log_path.read_text(encoding="utf-8")


def test_run_flags_stale_unrefreshed_output(tmp_path: Path, monkeypatch) -> None:
    """Finding 1 (review 02): a clean exit that did not rewrite an output is not done."""
    config = _config(tmp_path)
    manifest = _manifest()
    output = config.resolve_state_path("state/demo/out.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("stale content from a prior run", encoding="utf-8")

    ctx = RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "wt" / "run.log",
        resolved_outputs=[output],
    )
    ctx.workdir.mkdir(parents=True)

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        """Stubbed subprocess.run that exits 0 without touching the output."""
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner_base_module.subprocess, "run", fake_run)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == RunStatus.FAILED
    assert any("not refreshed this run" in p for p in result.problems)
    assert str(output) not in result.outputs


def test_run_flags_missing_output(tmp_path: Path, monkeypatch) -> None:
    """A clean exit that never produced a declared output is a failure."""
    config = _config(tmp_path)
    manifest = _manifest()
    output = config.resolve_state_path("state/demo/out.md")
    ctx = RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "wt" / "run.log",
        resolved_outputs=[output],
    )
    ctx.workdir.mkdir(parents=True)

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        """Stubbed subprocess.run that exits 0 producing nothing."""
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner_base_module.subprocess, "run", fake_run)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == RunStatus.FAILED
    assert any("not produced" in p for p in result.problems)


def test_run_handles_missing_codex_binary(tmp_path: Path, monkeypatch) -> None:
    """A missing codex binary yields a failed result with exit code 127."""
    config = _config(tmp_path)
    manifest = _manifest()
    output = config.resolve_state_path("state/demo/out.md")
    ctx = RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "wt" / "run.log",
        resolved_outputs=[output],
    )
    ctx.workdir.mkdir(parents=True)

    def boom(cmd, **kwargs):  # noqa: ANN001
        """Stubbed subprocess.run that raises FileNotFoundError."""
        raise FileNotFoundError("codex")

    monkeypatch.setattr(runner_base_module.subprocess, "run", boom)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == RunStatus.FAILED
    assert result.exit_code == 127


def test_build_prompt_embeds_valid_skill(tmp_path: Path) -> None:
    """The built prompt embeds the resolved skill text."""
    config = _config(tmp_path)
    manifest = _manifest()
    ctx = RunContext(config=config, workdir=tmp_path, log_path=tmp_path / "l")
    prompt = CodexRunner().build_prompt(manifest, ctx)
    assert "do the thing" in prompt


def test_build_prompt_uses_safe_source_resolver(tmp_path: Path) -> None:
    """Finding 2 (review 03): prompt skill loading goes through resolve_source_path."""
    config = _config(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("TOPSECRET", encoding="utf-8")
    manifest = _manifest(logic={"skill": "../secret.md", "verify": "x"})
    ctx = RunContext(config=config, workdir=tmp_path, log_path=tmp_path / "l")
    prompt = CodexRunner().build_prompt(manifest, ctx)
    assert "TOPSECRET" not in prompt


def test_build_command_includes_model(tmp_path: Path) -> None:
    """The built command passes the pinned model to codex."""
    config = _config(tmp_path)
    manifest = _manifest()
    ctx = RunContext(config=config, workdir=tmp_path, log_path=tmp_path / "l")
    cmd = CodexRunner().build_command(manifest, ctx)
    assert cmd[0] == "codex"
    assert "--model" in cmd and "gpt-5.5-medium" in cmd


def test_build_command_scopes_sandbox_not_bypass(tmp_path: Path) -> None:
    """Hardening: writes are confined to workspace-write + the output dirs."""
    config = _config(tmp_path)
    manifest = _manifest()
    out = config.resolve_state_path("state/demo/out.md")
    ctx = RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "l",
        resolved_outputs=[out],
    )
    cmd = CodexRunner().build_command(manifest, ctx)
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in cmd
    assert "--add-dir" in cmd
    assert str(out.parent.resolve()) in cmd


# --- Finding 2: preflight validates declared auth / apis / model -------------


def test_preflight_unknown_auth_bundle_reported(tmp_path: Path) -> None:
    """An auth bundle with no registered probe is reported."""
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"auth": ["mystery-bundle"]})
    report = CodexRunner().preflight(manifest, config)
    assert any("mystery-bundle" in p for p in report.problems)


def test_preflight_auth_probe_failure_reported(tmp_path: Path, monkeypatch) -> None:
    """A failing auth probe surfaces its message in the report."""
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"auth": ["nv-tools"]})
    monkeypatch.setitem(capabilities_module.AUTH_PROBES, "nv-tools", lambda config: "auth bundle 'nv-tools': boom")
    report = CodexRunner().preflight(manifest, config)
    assert any("boom" in p for p in report.problems)


def test_preflight_api_probe_failure_reported(tmp_path: Path, monkeypatch) -> None:
    """A failing API probe surfaces its message in the report."""
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"apis": ["slack"]})
    monkeypatch.setitem(capabilities_module.API_PROBES, "slack", lambda config: "api 'slack': not configured")
    report = CodexRunner().preflight(manifest, config)
    assert any("slack" in p for p in report.problems)


def test_preflight_passes_when_probes_ok(tmp_path: Path, monkeypatch) -> None:
    """Preflight passes when the binary, probes, skill, and verify are OK."""
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"auth": ["nv-tools"], "apis": ["slack"]})
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    monkeypatch.setitem(capabilities_module.AUTH_PROBES, "nv-tools", lambda config: None)
    monkeypatch.setitem(capabilities_module.API_PROBES, "slack", lambda config: None)
    report = CodexRunner().preflight(manifest, config)
    assert report.ok, report.problems


def test_preflight_flags_unrecognized_model(tmp_path: Path) -> None:
    """Preflight flags a model id that does not look like a Codex model."""
    config = _config(tmp_path)
    manifest = _manifest(runtime={"vendor": "codex", "model": "opus"})
    report = CodexRunner().preflight(manifest, config)
    assert any("opus" in p and "model" in p for p in report.problems)


# --- Finding 5: runtime budget is enforced via subprocess timeout ------------


def test_run_timeout_returns_stalled(tmp_path: Path, monkeypatch) -> None:
    """Exceeding budget.max_runtime aborts the run and reports stalled."""
    config = _config(tmp_path)
    manifest = _manifest(budget={"max_runtime": "5m"})
    output = config.resolve_state_path("state/demo/out.md")
    ctx = RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "wt" / "run.log",
        resolved_outputs=[output],
    )
    ctx.workdir.mkdir(parents=True)

    def fake_timeout(cmd, **kwargs):  # noqa: ANN001
        """Stubbed subprocess.run that raises TimeoutExpired at the budget."""
        assert kwargs.get("timeout") == 300
        raise runner_base_module.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(runner_base_module.subprocess, "run", fake_timeout)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == RunStatus.STALLED
    assert result.exit_code is None
    assert any("max_runtime" in p for p in result.problems)
    assert ctx.log_path.exists()


# --- M3: Claude + Cursor adapters + shared prompt (portability) --------------


def _ctx(config: LoopcraftConfig, tmp_path: Path) -> RunContext:
    """A minimal RunContext for prompt/command tests."""
    return RunContext(
        config=config,
        workdir=tmp_path / "wt",
        log_path=tmp_path / "wt" / "run.log",
        resolved_outputs=[config.resolve_state_path("state/demo/out.md")],
    )


def test_registry_exposes_all_three_vendors(tmp_path: Path) -> None:
    """codex, claude, and cursor all resolve to their adapters."""
    # Subset (not equality): other test modules register stub vendors into the
    # shared registry, so only assert the shipped adapters are present.
    assert {"claude", "codex", "cursor"}.issubset(available_vendors())
    assert isinstance(get_runner("codex"), CodexRunner)
    assert isinstance(get_runner("claude"), ClaudeRunner)
    assert isinstance(get_runner("cursor"), CursorRunner)


def test_prompt_is_identical_across_vendors(tmp_path: Path) -> None:
    """The prompt is vendor-neutral: one manifest yields the same prompt everywhere."""
    config = _config(tmp_path)
    manifest = _manifest()
    ctx = _ctx(config, tmp_path)
    codex_prompt = CodexRunner().build_prompt(manifest, ctx)
    assert ClaudeRunner().build_prompt(manifest, ctx) == codex_prompt
    assert CursorRunner().build_prompt(manifest, ctx) == codex_prompt


def test_claude_preflight_flags_missing_binary(tmp_path: Path, monkeypatch) -> None:
    """Claude preflight fails when the claude CLI is not resolvable."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: None)
    report = ClaudeRunner().preflight(_manifest(runtime={"vendor": "claude"}), _config(tmp_path))
    assert not report.ok
    assert any("claude CLI not found" in p for p in report.problems)


def test_claude_preflight_passes(tmp_path: Path, monkeypatch) -> None:
    """Claude preflight passes with the binary present and a valid alias model."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    report = ClaudeRunner().preflight(
        _manifest(runtime={"vendor": "claude", "model": "sonnet"}), _config(tmp_path)
    )
    assert report.ok, report.problems


def test_claude_flags_unrecognized_model(tmp_path: Path, monkeypatch) -> None:
    """A gpt-* model is flagged as not a Claude model."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    report = ClaudeRunner().preflight(
        _manifest(runtime={"vendor": "claude", "model": "gpt-5.5"}), _config(tmp_path)
    )
    assert any("not a recognized Claude model" in p for p in report.problems)


def test_claude_build_command(tmp_path: Path) -> None:
    """Claude renders a headless argv with model, effort, and writable roots."""
    config = _config(tmp_path)
    manifest = _manifest(runtime={"vendor": "claude", "model": "opus", "reasoning_effort": "high"})
    cmd = ClaudeRunner().build_command(manifest, _ctx(config, tmp_path))
    assert cmd[:2] == ["claude", "-p"]
    assert "--model" in cmd and "opus" in cmd
    assert "--effort" in cmd and "high" in cmd
    assert "--add-dir" in cmd
    assert "--permission-mode" in cmd


def test_cursor_preflight_flags_missing_binary(tmp_path: Path, monkeypatch) -> None:
    """Cursor preflight fails when cursor-agent is not resolvable."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: None)
    report = CursorRunner().preflight(_manifest(runtime={"vendor": "cursor"}), _config(tmp_path))
    assert not report.ok
    assert any("cursor-agent not found" in p for p in report.problems)


def test_cursor_accepts_any_model(tmp_path: Path, monkeypatch) -> None:
    """Cursor is cross-provider, so it does not reject a gpt-*/claude-* model."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    # No declared outputs, so the ledger-output limitation does not apply here.
    report = CursorRunner().preflight(
        _manifest(runtime={"vendor": "cursor", "model": "gpt-5.5"}, outputs=[]), _config(tmp_path)
    )
    assert report.ok, report.problems


def test_cursor_supports_ledger_outputs(tmp_path: Path, monkeypatch) -> None:
    """Cursor preflight accepts a ledger-writing loop (M3.5 writable-root grant)."""
    monkeypatch.setattr(LoopcraftConfig, "which", lambda self, name: f"/usr/bin/{name}")
    report = CursorRunner().preflight(
        _manifest(runtime={"vendor": "cursor"}, outputs=["state/demo/out.md"]), _config(tmp_path)
    )
    assert report.ok, report.problems


def test_cursor_build_command(tmp_path: Path) -> None:
    """Cursor renders a headless argv with the pinned model."""
    config = _config(tmp_path)
    manifest = _manifest(runtime={"vendor": "cursor", "model": "gpt-5.5"})
    cmd = CursorRunner().build_command(manifest, _ctx(config, tmp_path))
    assert cmd[:2] == ["cursor-agent", "-p"]
    assert "--model" in cmd and "gpt-5.5" in cmd


def test_cursor_grants_writable_root_for_ledger_outputs(tmp_path: Path) -> None:
    """With declared outputs, Cursor disables the sandbox to grant the writes."""
    config = _config(tmp_path)
    manifest = _manifest(runtime={"vendor": "cursor"})
    cmd = CursorRunner().build_command(manifest, _ctx(config, tmp_path))
    assert "--force" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "disabled"


def test_cursor_keeps_sandbox_without_outputs(tmp_path: Path) -> None:
    """With no external outputs, Cursor keeps the default sandbox (no --force)."""
    config = _config(tmp_path)
    manifest = _manifest(runtime={"vendor": "cursor"}, outputs=[])
    ctx = RunContext(config=config, workdir=tmp_path / "wt", log_path=tmp_path / "wt" / "run.log")
    cmd = CursorRunner().build_command(manifest, ctx)
    assert "--sandbox" not in cmd
    assert "--force" not in cmd
