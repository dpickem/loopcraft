from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from loopcraft.config import LoopcraftConfig
from loopcraft.manifest import LoopManifest
from loopcraft.runners import codex as codex_module
from loopcraft.runners.base import (
    RunContext,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_STALLED,
)
from loopcraft.runners.codex import CodexRunner


def _config(tmp_path: Path) -> LoopcraftConfig:
    source = tmp_path / "src"
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("do the thing", encoding="utf-8")
    return LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")


def _manifest(**overrides) -> LoopManifest:
    base = {
        "id": "demo",
        "name": "Demo",
        "description": "d",
        "runtime": {"vendor": "codex", "model": "gpt-5.5-medium"},
        "cadence": {"type": "cron", "at": "0 9 * * *"},
        "tier": "observe",
        "outputs": ["state/demo/out.md"],
        "logic": {"skill": "skills/demo/SKILL.md", "verify": "out exists"},
    }
    base.update(overrides)
    return LoopManifest.from_dict(base)


def test_preflight_reports_missing_skill(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = _manifest(logic={"skill": "skills/ghost/SKILL.md"})
    report = CodexRunner().preflight(manifest, config)
    assert not report.ok
    assert any("skill not found" in p for p in report.problems)


def test_preflight_reports_missing_env(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"env": ["LOOPCRAFT_DEFINITELY_UNSET_VAR"]})
    report = CodexRunner().preflight(manifest, config)
    assert any("LOOPCRAFT_DEFINITELY_UNSET_VAR" in p for p in report.problems)


def test_run_writes_log_and_reports_done(tmp_path: Path, monkeypatch) -> None:
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
        # Emulate the agent producing its declared output.
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# digest\n- item", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(codex_module.subprocess, "run", fake_run)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == STATUS_DONE
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
        # Exit 0 but leave the pre-existing output untouched.
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(codex_module.subprocess, "run", fake_run)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == STATUS_FAILED
    assert any("not refreshed this run" in p for p in result.problems)
    assert str(output) not in result.outputs


def test_run_flags_missing_output(tmp_path: Path, monkeypatch) -> None:
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
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_module.subprocess, "run", fake_run)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == STATUS_FAILED
    assert any("not produced" in p for p in result.problems)


def test_run_handles_missing_codex_binary(tmp_path: Path, monkeypatch) -> None:
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
        raise FileNotFoundError("codex")

    monkeypatch.setattr(codex_module.subprocess, "run", boom)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == STATUS_FAILED
    assert result.exit_code == 127


def test_build_prompt_embeds_valid_skill(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = _manifest()
    ctx = RunContext(config=config, workdir=tmp_path, log_path=tmp_path / "l")
    prompt = CodexRunner()._build_prompt(manifest, ctx)
    assert "do the thing" in prompt


def test_build_prompt_uses_safe_source_resolver(tmp_path: Path) -> None:
    """Finding 2 (review 03): prompt skill loading goes through resolve_source_path."""
    config = _config(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("TOPSECRET", encoding="utf-8")
    manifest = _manifest(logic={"skill": "../secret.md", "verify": "x"})
    ctx = RunContext(config=config, workdir=tmp_path, log_path=tmp_path / "l")
    prompt = CodexRunner()._build_prompt(manifest, ctx)
    assert "TOPSECRET" not in prompt


def test_build_command_includes_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = _manifest()
    ctx = RunContext(config=config, workdir=tmp_path, log_path=tmp_path / "l")
    cmd = CodexRunner()._build_command(manifest, ctx)
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
    cmd = CodexRunner()._build_command(manifest, ctx)
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in cmd
    assert "--add-dir" in cmd
    assert str(out.parent.resolve()) in cmd


# --- Finding 2: preflight validates declared auth / apis / model -------------


def test_preflight_unknown_auth_bundle_reported(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"auth": ["mystery-bundle"]})
    report = CodexRunner().preflight(manifest, config)
    assert any("mystery-bundle" in p for p in report.problems)


def test_preflight_auth_probe_failure_reported(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"auth": ["nv-tools"]})
    monkeypatch.setitem(codex_module.AUTH_PROBES, "nv-tools", lambda: "auth bundle 'nv-tools': boom")
    report = CodexRunner().preflight(manifest, config)
    assert any("boom" in p for p in report.problems)


def test_preflight_api_probe_failure_reported(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"apis": ["slack"]})
    monkeypatch.setitem(codex_module.API_PROBES, "slack", lambda: "api 'slack': not configured")
    report = CodexRunner().preflight(manifest, config)
    assert any("slack" in p for p in report.problems)


def test_preflight_passes_when_probes_ok(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"auth": ["nv-tools"], "apis": ["slack"]})
    monkeypatch.setattr(codex_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setitem(codex_module.AUTH_PROBES, "nv-tools", lambda: None)
    monkeypatch.setitem(codex_module.API_PROBES, "slack", lambda: None)
    report = CodexRunner().preflight(manifest, config)
    assert report.ok, report.problems


def test_preflight_flags_unrecognized_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = _manifest(runtime={"vendor": "codex", "model": "opus"})
    report = CodexRunner().preflight(manifest, config)
    assert any("opus" in p and "model" in p for p in report.problems)


# --- Finding 5: runtime budget is enforced via subprocess timeout ------------


def test_run_timeout_returns_stalled(tmp_path: Path, monkeypatch) -> None:
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
        assert kwargs.get("timeout") == 300
        raise codex_module.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(codex_module.subprocess, "run", fake_timeout)
    result = CodexRunner().run(manifest, ctx)

    assert result.status == STATUS_STALLED
    assert result.exit_code is None
    assert any("max_runtime" in p for p in result.problems)
    assert ctx.log_path.exists()
