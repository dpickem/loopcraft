"""Tests for the M2 loopctl commands: init, auth, and apply."""

from __future__ import annotations

import json
from pathlib import Path

from loopcraft import cli
from loopcraft import deploy
from loopcraft.runners import capabilities as caps
from loopcraft.runners.base import PreflightReport


class _OkRunner:
    """Stub adapter whose preflight always passes (for apply planning)."""

    vendor = "m2ok"

    def preflight(self, loop, config):  # noqa: ANN001
        """Report a passing preflight."""
        return PreflightReport(vendor=self.vendor, ok=True, problems=[])


class _FailRunner(_OkRunner):
    """Stub adapter whose preflight always fails."""

    vendor = "m2fail"

    def preflight(self, loop, config):  # noqa: ANN001
        """Report a failing preflight."""
        return PreflightReport(vendor=self.vendor, ok=False, problems=["missing token"])


def _demo_source(monkeypatch, tmp_path: Path, manifest_text: str, filename: str = "demo.yaml") -> Path:
    """Build a temp source tree with one skill + manifest; point loopctl at it."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "loops" / filename).write_text(manifest_text, encoding="utf-8")
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    return source


_CRON_MANIFEST = (
    "id: demo\n"
    "name: Demo\n"
    "cadence: {type: cron, at: '0 9 * * 1-5'}\n"
    "logic: {skill: skills/demo/SKILL.md}\n"
)


# --- init --------------------------------------------------------------------


def test_init_bootstraps_memory_tree(monkeypatch, tmp_path: Path, capsys) -> None:
    """init creates the ledger/runs/artifacts/staging dirs and reports source ok."""
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    rc = cli.main(["--json", "init", "--no-git"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["data"]["ok"] is True
    assert payload["data"]["git"] == "skipped"
    mem = tmp_path / "mem"
    assert (mem / "ledger" / "runs").is_dir()
    assert (mem / "artifacts").is_dir()
    assert (mem / "var" / "systemd").is_dir()


def test_init_is_idempotent(monkeypatch, tmp_path: Path, capsys) -> None:
    """A second init reports nothing new was created but stays ok."""
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    assert cli.main(["init", "--no-git"]) == 0
    capsys.readouterr()
    rc = cli.main(["--json", "init", "--no-git"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["data"]["created"] == []


def test_init_fails_when_source_missing_loops(monkeypatch, tmp_path: Path, capsys) -> None:
    """init reports a source tree without a loops/ directory as not ok."""
    source = tmp_path / "src"
    source.mkdir()
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    rc = cli.main(["--json", "init", "--no-git"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["source_ok"] is False


# --- auth --------------------------------------------------------------------


def test_auth_reports_ok_when_all_probes_pass(monkeypatch, tmp_path: Path, capsys) -> None:
    """auth aggregates the fleet's deps and reports ok when every probe passes."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "depends_on: {auth: [nv-tools], apis: [arxiv]}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    monkeypatch.setitem(caps.AUTH_PROBES, "nv-tools", lambda config: None)
    monkeypatch.setitem(caps.API_PROBES, "arxiv", lambda config: None)
    rc = cli.main(["--json", "auth"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["missing"] == []


def test_auth_reports_missing_with_guidance(monkeypatch, tmp_path: Path, capsys) -> None:
    """A failing probe or unset env var is reported as missing, with guidance."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "depends_on: {auth: [nv-tools], env: [DEMO_TOKEN]}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    monkeypatch.setitem(caps.AUTH_PROBES, "nv-tools", lambda config: "nv-tools missing")
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    rc = cli.main(["--json", "auth"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert set(payload["data"]["missing"]) == {"nv-tools", "DEMO_TOKEN"}
    nv = next(i for i in payload["data"]["items"] if i["name"] == "nv-tools")
    assert nv["guidance"]


# --- apply -------------------------------------------------------------------


def test_apply_dry_run_plans_without_writing(monkeypatch, tmp_path: Path, capsys) -> None:
    """apply --dry-run plans units and writes nothing."""
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _OkRunner())
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    rc = cli.main(["--json", "apply", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["data"]["ok"] is True
    assert "loop-demo.timer" in payload["data"]["planned_units"]
    assert not (tmp_path / "mem" / "var" / "systemd").exists()


def test_apply_renders_units(monkeypatch, tmp_path: Path, capsys) -> None:
    """apply renders the service + timer into the staging directory."""
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _OkRunner())
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    rc = cli.main(["--json", "apply"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    staged = tmp_path / "mem" / "var" / "systemd"
    assert (staged / "loop-demo.service").exists()
    assert (staged / "loop-demo.timer").exists()
    assert payload["data"]["written"]


def test_apply_reports_unmet_dependency_at_apply(monkeypatch, tmp_path: Path, capsys) -> None:
    """Exit criterion: an unmet dependency is reported at apply (nonzero)."""
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _FailRunner())
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    rc = cli.main(["--json", "apply"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["data"]["preflight_problems"] == ["demo: missing token"]
    # Units still render (files are harmless); deployment is what's blocked.
    assert (tmp_path / "mem" / "var" / "systemd" / "loop-demo.timer").exists()


def test_apply_refuses_install_with_unmet_dependency(monkeypatch, tmp_path: Path, capsys) -> None:
    """apply --install refuses to enable units when the plan has unmet deps."""
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _FailRunner())
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    rc = cli.main(["--json", "apply", "--install"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"].get("installed") is False


# --- fleet -------------------------------------------------------------------


def test_fleet_renders_table(monkeypatch, tmp_path: Path, capsys) -> None:
    """fleet prints a box-drawn table listing each loop and its trigger."""
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    rc = cli.main(["fleet"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "LOOP" in out and "INSTALLED" in out
    assert "demo" in out
    assert "0 9 * * 1-5" in out
    assert "never" in out


def test_fleet_json_envelope(monkeypatch, tmp_path: Path, capsys) -> None:
    """fleet --json returns structured rows with install state."""
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    rc = cli.main(["--json", "fleet"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["command"] == "fleet"
    loop = next(l for l in payload["data"]["loops"] if l["id"] == "demo")
    assert loop["installed"] == "no"
    assert loop["trigger"] == "0 9 * * 1-5"
    assert loop["last_status"] is None


def test_fleet_reports_staged_after_apply(monkeypatch, tmp_path: Path, capsys) -> None:
    """After apply renders units, fleet marks the loop as staged."""
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _OkRunner())
    _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    assert cli.main(["apply"]) == 0
    capsys.readouterr()
    rc = cli.main(["--json", "fleet"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    loop = next(l for l in payload["data"]["loops"] if l["id"] == "demo")
    assert loop["installed"] == "staged"


def test_fleet_degrades_on_broken_manifest(monkeypatch, tmp_path: Path, capsys) -> None:
    """A broken manifest makes fleet nonzero but still tabulates valid loops."""
    source = _demo_source(monkeypatch, tmp_path, _CRON_MANIFEST)
    (source / "loops" / "broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    rc = cli.main(["--json", "fleet"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert any(l["id"] == "demo" for l in payload["data"]["loops"])
    assert any("broken.yaml" in p for p in payload["data"]["problems"])


def test_apply_structural_problem_writes_nothing(monkeypatch, tmp_path: Path, capsys) -> None:
    """A structural manifest problem blocks rendering entirely."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "outputs: ['linear:project/Daily']\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    rc = cli.main(["--json", "apply", "--skip-preflight"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["manifest_problems"]
    assert not (tmp_path / "mem" / "var" / "systemd").exists()
