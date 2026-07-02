from __future__ import annotations

import json
from pathlib import Path

from loopcraft import cli
from loopcraft.runners import register_runner
from loopcraft.runners.base import PreflightReport, RunResult, STATUS_DONE

REPO_ROOT = Path(__file__).resolve().parents[1]


class StubRunner:
    """A vendor adapter that writes declared outputs without calling a real CLI."""

    vendor = "stub"

    def preflight(self, loop, config):  # noqa: ANN001
        return PreflightReport(vendor=self.vendor, ok=True, problems=[])

    def run(self, loop, ctx):  # noqa: ANN001
        produced = []
        for path in ctx.resolved_outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# stub digest\n- action: reply to X", encoding="utf-8")
            produced.append(str(path))
        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.log_path.write_text("stub run", encoding="utf-8")
        return RunResult(status=STATUS_DONE, exit_code=0, log_path=ctx.log_path, outputs=produced)


def _env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(REPO_ROOT))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))


def test_validate_repo_loops(monkeypatch, tmp_path: Path, capsys) -> None:
    _env(monkeypatch, tmp_path)
    rc = cli.main(["validate"])
    assert rc == 0
    assert "all manifests valid" in capsys.readouterr().out


def test_list_includes_slack_triage(monkeypatch, tmp_path: Path, capsys) -> None:
    _env(monkeypatch, tmp_path)
    rc = cli.main(["list"])
    assert rc == 0
    assert "slack-triage" in capsys.readouterr().out


def test_run_end_to_end_with_stub_runner(monkeypatch, tmp_path: Path, capsys) -> None:
    _env(monkeypatch, tmp_path)
    register_runner("stub", StubRunner)

    rc = cli.main(["run", "slack-triage", "--vendor", "stub"])
    assert rc == 0

    # Exit criteria: the loop writes state/slack/triage-latest.md to the memory tree.
    output = tmp_path / "mem" / "ledger" / "slack" / "triage-latest.md"
    assert output.exists()

    # Finding 1: the declared seen.json cursor is part of the run contract too.
    cursor = tmp_path / "mem" / "ledger" / "slack" / "seen.json"
    assert cursor.exists()

    archive_outputs = list((tmp_path / "mem" / "ledger" / "slack" / "history").glob("*.md"))
    assert len(archive_outputs) == 1

    # Finding 3: the run worktree contains the loop's skill assets.
    staged_channels = list(
        (tmp_path / "mem" / "var" / "worktrees" / "slack-triage").glob(
            "*/skills/slack-triage/channels.txt"
        )
    )
    assert staged_channels, "expected channels.txt staged into the run worktree"

    # And a durable run record lands in ledger/runs/.
    runs_dir = tmp_path / "mem" / "ledger" / "runs"
    records = list(runs_dir.glob("*.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["loop"] == "slack-triage"
    assert data["status"] == "done"
    assert data["vendor"] == "stub"
    assert str(cursor) in data["outputs"]
    assert str(archive_outputs[0]) in data["outputs"]


def test_run_prunes_old_worktrees(monkeypatch, tmp_path: Path) -> None:
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("LOOPCRAFT_WORKTREE_KEEP_LAST", "2")
    register_runner("stub", StubRunner)

    for _ in range(4):
        assert cli.main(["run", "slack-triage", "--vendor", "stub"]) == 0

    worktrees = sorted((tmp_path / "mem" / "var" / "worktrees" / "slack-triage").iterdir())
    assert len(worktrees) == 2

    # Durable run records are not pruned with scratch worktrees.
    records = list((tmp_path / "mem" / "ledger" / "runs").glob("*.json"))
    assert len(records) == 4


def test_run_records_failure_on_preflight(monkeypatch, tmp_path: Path) -> None:
    _env(monkeypatch, tmp_path)

    class FailingRunner(StubRunner):
        vendor = "failing"

        def preflight(self, loop, config):  # noqa: ANN001
            return PreflightReport(vendor=self.vendor, ok=False, problems=["nope"])

    register_runner("failing", FailingRunner)
    rc = cli.main(["run", "slack-triage", "--vendor", "failing"])
    assert rc == 1

    runs_dir = tmp_path / "mem" / "ledger" / "runs"
    records = list(runs_dir.glob("*.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["problems"] == ["nope"]


def test_run_dry_run_with_stub(monkeypatch, tmp_path: Path, capsys) -> None:
    _env(monkeypatch, tmp_path)
    register_runner("stub", StubRunner)
    rc = cli.main(["run", "slack-triage", "--vendor", "stub", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "preflight: OK" in out
    # Dry run must not write outputs.
    assert not (tmp_path / "mem" / "ledger" / "slack" / "triage-latest.md").exists()


def test_unknown_loop_returns_error(monkeypatch, tmp_path: Path) -> None:
    _env(monkeypatch, tmp_path)
    rc = cli.main(["run", "does-not-exist"])
    assert rc == 2


def test_list_json_envelope(monkeypatch, tmp_path: Path, capsys) -> None:
    _env(monkeypatch, tmp_path)
    rc = cli.main(["--json", "list"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "list"
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert any(loop["id"] == "slack-triage" for loop in payload["data"]["loops"])


def test_run_dry_run_json_envelope(monkeypatch, tmp_path: Path, capsys) -> None:
    _env(monkeypatch, tmp_path)
    register_runner("stub", StubRunner)
    rc = cli.main(["--json", "run", "slack-triage", "--vendor", "stub", "--dry-run"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "run"
    assert payload["data"]["preflight"]["ok"] is True
    assert payload["data"]["resolved_outputs"]


def test_scheme_output_fails_validate_and_run(monkeypatch, tmp_path: Path) -> None:
    """Finding 3 (review 02): a scheme output is rejected by validate and by run."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "loops" / "demo.yaml").write_text(
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "outputs: ['linear:project/Daily']\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))

    assert cli.main(["validate"]) == 1
    assert cli.main(["run", "demo"]) == 2


def test_deps_check_loop_runs_preflight(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 2: deps check --loop surfaces the loop's declared-dep preflight."""
    _env(monkeypatch, tmp_path)
    from loopcraft.runners import codex as codex_module

    monkeypatch.setitem(codex_module.AUTH_PROBES, "nv-tools", lambda config: "auth bundle 'nv-tools': boom")
    rc = cli.main(["deps", "check", "--loop", "slack-triage"])
    out = capsys.readouterr().out
    assert "preflight slack-triage" in out
    assert "boom" in out
    assert rc == 1
