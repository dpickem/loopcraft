"""Tests for the loopctl CLI commands, exit codes, and JSON envelope."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from loopcraft import cli
from loopcraft.config import LoopcraftConfig
from loopcraft.runners import register_runner
from loopcraft import worktree
from loopcraft.runners.base import PreflightReport, RunResult, RunStatus

REPO_ROOT = Path(__file__).resolve().parents[1]


class StubRunner:
    """A vendor adapter that writes declared outputs without calling a real CLI."""

    vendor = "stub"

    def preflight(self, loop, config):  # noqa: ANN001
        """Report a passing preflight (no external checks)."""
        return PreflightReport(vendor=self.vendor, ok=True, problems=[])

    def run(self, loop, ctx):  # noqa: ANN001
        """Write each declared output and a log, then report done."""
        produced = []
        for path in ctx.resolved_outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# stub digest\n- action: reply to X", encoding="utf-8")
            produced.append(str(path))
        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.log_path.write_text("stub run", encoding="utf-8")
        return RunResult(status=RunStatus.DONE, exit_code=0, log_path=ctx.log_path, outputs=produced)


def _env(monkeypatch, tmp_path: Path) -> None:
    """Point loopctl at the repo source tree and a temp memory tree."""
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(REPO_ROOT))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))


def _demo_source(monkeypatch, tmp_path: Path, manifest_text: str, filename: str = "demo.yaml") -> Path:
    """Build a temp source tree with one skill and one manifest; point loopctl at it."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    skill_dir = source / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "loops" / filename).write_text(manifest_text, encoding="utf-8")
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    return source


def test_validate_repo_loops(monkeypatch, tmp_path: Path, capsys) -> None:
    """`loopctl validate` reports all shipped manifests as valid."""
    _env(monkeypatch, tmp_path)
    rc = cli.main(["validate"])
    assert rc == 0
    assert "all manifests valid" in capsys.readouterr().out


def test_list_includes_slack_triage(monkeypatch, tmp_path: Path, capsys) -> None:
    """`loopctl list` includes the slack-triage loop."""
    _env(monkeypatch, tmp_path)
    rc = cli.main(["list"])
    assert rc == 0
    assert "slack-triage" in capsys.readouterr().out


def test_run_end_to_end_with_stub_runner(monkeypatch, tmp_path: Path, capsys) -> None:
    """`loopctl run` stages assets, writes outputs, and records the run."""
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
    # Finding 4 (review 05): produced outputs and the declared contract are
    # recorded as distinct fields with one semantic across statuses.
    assert "state/slack/triage-latest.md" in data["declared_outputs"]


def test_run_prunes_old_worktrees(monkeypatch, tmp_path: Path) -> None:
    """Old per-run worktrees are pruned to keep-last while records persist."""
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
    """A failed preflight records a failed run and does not execute."""
    _env(monkeypatch, tmp_path)

    class FailingRunner(StubRunner):
        """Stub runner whose preflight always fails."""

        vendor = "failing"

        def preflight(self, loop, config):  # noqa: ANN001
            """Report a failing preflight."""
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
    # Finding 4 (review 05): execution never started, so nothing was produced;
    # the declared contract is preserved in its own field.
    assert data["outputs"] == []
    assert "state/slack/triage-latest.md" in data["declared_outputs"]


def test_run_dry_run_with_stub(monkeypatch, tmp_path: Path, capsys) -> None:
    """A dry run reports preflight OK and writes no outputs."""
    _env(monkeypatch, tmp_path)
    register_runner("stub", StubRunner)
    rc = cli.main(["run", "slack-triage", "--vendor", "stub", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "preflight: OK" in out
    # Dry run must not write outputs.
    assert not (tmp_path / "mem" / "ledger" / "slack" / "triage-latest.md").exists()


def test_unknown_loop_returns_error(monkeypatch, tmp_path: Path) -> None:
    """Running an unknown loop exits with code 2."""
    _env(monkeypatch, tmp_path)
    rc = cli.main(["run", "does-not-exist"])
    assert rc == 2


def test_run_refuses_recursive_same_loop_invocation(monkeypatch, tmp_path: Path, capsys) -> None:
    """PR review: a loop re-entering `loopctl run` for itself is refused.

    The control plane marks the executing loop via LOOPCRAFT_ACTIVE_LOOP; the
    guard is programmatic, not just skill wording.
    """
    _env(monkeypatch, tmp_path)
    register_runner("stub", StubRunner)
    monkeypatch.setenv("LOOPCRAFT_ACTIVE_LOOP", "slack-triage")

    rc = cli.main(["--json", "run", "slack-triage", "--vendor", "stub"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert "recursion guard" in payload["data"]["error"]
    # No run record: the recursive attempt never starts.
    assert not (tmp_path / "mem" / "ledger" / "runs").exists()


def test_run_allows_nested_run_of_a_different_loop(monkeypatch, tmp_path: Path) -> None:
    """The recursion guard only blocks re-entry into the *same* loop."""
    _env(monkeypatch, tmp_path)
    register_runner("stub", StubRunner)
    monkeypatch.setenv("LOOPCRAFT_ACTIVE_LOOP", "some-other-loop")

    rc = cli.main(["run", "slack-triage", "--vendor", "stub"])
    assert rc == 0


def test_active_loop_env_is_handed_to_the_run(monkeypatch, tmp_path: Path) -> None:
    """The control plane marks the executing loop in the child environment."""
    _env(monkeypatch, tmp_path)
    seen_env: dict[str, str] = {}

    class EnvCapturingRunner(StubRunner):
        """Stub that records the env handed to the run context."""

        vendor = "envcap"

        def run(self, loop, ctx):  # noqa: ANN001
            """Capture ctx.env, then behave like the stub runner."""
            seen_env.update(ctx.env)
            return super().run(loop, ctx)

    register_runner("envcap", EnvCapturingRunner)
    rc = cli.main(["run", "slack-triage", "--vendor", "envcap"])
    assert rc == 0
    assert seen_env["LOOPCRAFT_ACTIVE_LOOP"] == "slack-triage"


@pytest.mark.parametrize(
    "bad_id",
    ["../outside", "/etc/passwd", "demo/../../x", "state/../escape", "Demo", "a b"],
)
def test_run_rejects_non_canonical_loop_ids(monkeypatch, tmp_path: Path, capsys, bad_id: str) -> None:
    """Finding 1 (review 05): the CLI loop selector is not a file-path input."""
    _env(monkeypatch, tmp_path)
    rc = cli.main(["--json", "run", bad_id])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert "invalid loop id" in payload["data"]["error"]


def test_run_rejects_filename_id_mismatch(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 1 (review 05): the advertised id and the executed id must agree."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: other\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    rc = cli.main(["--json", "run", "demo"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert "does not match filename stem" in payload["data"]["error"]


def test_worktree_dir_and_prune_reject_escaping_ids(tmp_path: Path) -> None:
    """Finding 1 (review 05): worktree paths are contained under the scratch root."""
    config = LoopcraftConfig(source_path=tmp_path / "s", memory_path=tmp_path / "m")
    for bad_id in ("/tmp/loopcraft-escaped", "../../escape"):
        with pytest.raises(ValueError, match="escapes"):
            worktree.worktree_dir(config, bad_id, "run-id")
        with pytest.raises(ValueError, match="escapes"):
            worktree.prune_loop_worktrees(config, bad_id, keep_last=1)
    # An absolute run id must not escape either.
    with pytest.raises(ValueError, match="escapes"):
        worktree.worktree_dir(config, "demo", "/tmp/loopcraft-escaped")


def test_worktree_dir_and_prune_reject_symlinked_loop_dir(tmp_path: Path) -> None:
    """Finding 1 (review 06): a symlinked worktree parent cannot redirect create/prune."""
    config = LoopcraftConfig(source_path=tmp_path / "s", memory_path=tmp_path / "m")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = worktree.worktrees_root(config)
    root.mkdir(parents=True)
    (root / "demo").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        worktree.worktree_dir(config, "demo", "run-id")
    with pytest.raises(ValueError, match="escapes"):
        worktree.prune_loop_worktrees(config, "demo", keep_last=0)


def test_run_reports_malformed_yaml_as_structured_error(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 3 (review 05): broken YAML yields a JSON failure, not a traceback."""
    _demo_source(monkeypatch, tmp_path, "id: [unclosed\n")
    rc = cli.main(["--json", "run", "demo"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert "invalid YAML" in payload["data"]["error"]


def test_run_reports_schema_invalid_manifest_as_structured_error(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 3 (review 05): a Pydantic schema error becomes a command failure."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "tier: boss\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    rc = cli.main(["--json", "run", "demo"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert "tier" in payload["data"]["error"]


def test_list_and_status_surface_broken_manifests(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 3 (review 05): list/status degrade instead of hiding broken loops."""
    source = _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    (source / "loops" / "broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")

    for command in ("list", "status"):
        rc = cli.main(["--json", command])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 1, command
        assert payload["ok"] is False
        # The valid loop is still reported as partial data.
        assert any(loop["id"] == "demo" for loop in payload["data"]["loops"])
        assert any("broken.yaml" in problem for problem in payload["data"]["problems"])


def test_run_fails_structured_when_content_config_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 2 (review 05): a missing content.config fails before the agent starts."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "content: {config: config/does-not-exist.yaml}\n"
        "outputs: ['state/demo/out.md']\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    # The stub runner's preflight always passes, so this exercises the staging
    # backstop rather than the shared capability check.
    register_runner("stub", StubRunner)
    rc = cli.main(["--json", "run", "demo", "--vendor", "stub"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["phase"] == "staging"
    assert any("content.config not found" in p for p in payload["data"]["problems"])

    records = list((tmp_path / "mem" / "ledger" / "runs").glob("*.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["outputs"] == []
    assert data["declared_outputs"] == ["state/demo/out.md"]


def test_staging_failure_honors_worktree_retention(monkeypatch, tmp_path: Path) -> None:
    """Finding 2 (review 06): keep_last=0 also prunes a failed staging worktree."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "content: {config: config/does-not-exist.yaml}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    monkeypatch.setenv("LOOPCRAFT_WORKTREE_KEEP_LAST", "0")
    register_runner("stub", StubRunner)
    rc = cli.main(["run", "demo", "--vendor", "stub"])
    assert rc == 1

    loop_worktrees = tmp_path / "mem" / "var" / "worktrees" / "demo"
    assert not loop_worktrees.exists() or list(loop_worktrees.iterdir()) == []
    # The failed attempt is still durable history even though its worktree is gone.
    records = list((tmp_path / "mem" / "ledger" / "runs").glob("*.json"))
    assert len(records) == 1


def test_run_normalizes_preflight_exception(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 2 (review 06): a raising preflight becomes a failed run record."""
    _env(monkeypatch, tmp_path)

    class ExplodingPreflightRunner(StubRunner):
        """Stub whose preflight raises instead of returning a report."""

        vendor = "boom-preflight"

        def preflight(self, loop, config):  # noqa: ANN001
            """Raise to simulate a broken adapter."""
            raise RuntimeError("adapter exploded")

    register_runner("boom-preflight", ExplodingPreflightRunner)
    rc = cli.main(["--json", "run", "slack-triage", "--vendor", "boom-preflight"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["phase"] == "preflight"
    assert any("preflight raised RuntimeError" in p for p in payload["data"]["problems"])

    records = list((tmp_path / "mem" / "ledger" / "runs").glob("*.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["outputs"] == []


def test_run_normalizes_runner_exception(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 2 (review 06): a raising adapter yields a failed record + traceback log."""
    _env(monkeypatch, tmp_path)

    class ExplodingRunner(StubRunner):
        """Stub whose run() raises mid-execution."""

        vendor = "boom-run"

        def run(self, loop, ctx):  # noqa: ANN001
            """Raise to simulate an unexpected adapter fault."""
            raise RuntimeError("subprocess fell over")

    register_runner("boom-run", ExplodingRunner)
    rc = cli.main(["--json", "run", "slack-triage", "--vendor", "boom-run"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["phase"] == "execution"
    assert any("runner raised RuntimeError" in p for p in payload["data"]["problems"])

    records = list((tmp_path / "mem" / "ledger" / "runs").glob("*.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["outputs"] == []
    # The traceback is preserved for diagnosis in the run log.
    assert data["log_path"] and "RuntimeError: subprocess fell over" in Path(
        data["log_path"]
    ).read_text(encoding="utf-8")


def test_deps_check_loop_reports_semantically_invalid_manifest(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Finding 4 (review 06): deps check --loop cannot say OK for an invalid loop."""
    _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "outputs: ['linear:project/Daily']\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    rc = cli.main(["--json", "deps", "check", "--loop", "demo"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert any(
        "external sink" in problem
        for problem in payload["data"]["preflight"]["invalid_manifest"]
    )


def test_deps_check_loop_normalizes_preflight_exception(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 2 (review 07): a raising adapter cannot crash deps check --loop.

    The same exception boundary used by `run` must apply on the dependency-check
    path, so the public CLI emits the JSON envelope instead of a traceback.
    """
    _env(monkeypatch, tmp_path)

    class ExplodingPreflightRunner(StubRunner):
        """Stub whose preflight raises instead of returning a report."""

        vendor = "codex"

        def preflight(self, loop, config):  # noqa: ANN001
            """Raise to simulate a broken adapter."""
            raise RuntimeError("boom")

    monkeypatch.setattr(cli, "get_runner", lambda vendor: ExplodingPreflightRunner())
    rc = cli.main(["--json", "deps", "check", "--loop", "slack-triage"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["command"] == "deps.check"
    assert payload["ok"] is False
    assert any(
        "preflight raised RuntimeError: boom" in problem
        for problem in payload["data"]["preflight"]["problems"]
    )


def test_dry_run_surfaces_invalid_content_config(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 4 (review 07): a malformed content config fails preflight, not the agent."""
    source = _demo_source(
        monkeypatch,
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "content: {config: config/demo.yaml}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    (source / "config").mkdir()
    (source / "config" / "demo.yaml").write_text("sources: [unclosed\n", encoding="utf-8")

    from loopcraft.runners import capabilities
    from loopcraft.runners.base import PreflightReport

    class CapabilityRunner(StubRunner):
        """Stub whose preflight runs the shared runtime-neutral checks."""

        vendor = "capstub"

        def preflight(self, loop, config):  # noqa: ANN001
            """Delegate to the shared capability check."""
            problems = capabilities.check_declared_capabilities(loop, config)
            return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)

    register_runner("capstub", CapabilityRunner)
    rc = cli.main(["--json", "run", "demo", "--vendor", "capstub", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any(
        "content config invalid" in problem
        for problem in payload["data"]["preflight"]["problems"]
    )


def test_logs_refuses_external_log_path(monkeypatch, tmp_path: Path, capsys) -> None:
    """Review 05 (finding 20): a run record whose log_path is outside the log
    root is refused instead of read."""
    _env(monkeypatch, tmp_path)
    runs_dir = tmp_path / "mem" / "ledger" / "runs"
    runs_dir.mkdir(parents=True)
    external = tmp_path / "secret.txt"
    external.write_text("top secret", encoding="utf-8")
    (runs_dir / "slack-triage__x.json").write_text(
        json.dumps(
            {
                "run_id": "20260101T000000Z-deadbeef",
                "loop": "slack-triage",
                "vendor": "codex",
                "status": "done",
                "started_at": "2026-01-01T00:00:00+00:00",
                "log_path": str(external),
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(["--json", "logs", "slack-triage"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert "outside allowed root" in payload["data"]["error"]
    assert "top secret" not in json.dumps(payload)


def test_dotenv_loaded_from_source_root(monkeypatch, tmp_path: Path) -> None:
    """Review 05 (finding 19): .env is loaded from the source tree, not cwd."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "loops" / "demo.yaml").write_text(
        "id: demo\nname: Demo\ncadence: {type: cron, at: '0 9 * * *'}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
        encoding="utf-8",
    )
    (source / ".env").write_text("LOOPCRAFT_TEST_MARKER=from-source-env\n", encoding="utf-8")
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    monkeypatch.delenv("LOOPCRAFT_TEST_MARKER", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd is NOT the source root

    assert cli.main(["list"]) == 0
    assert os.environ.get("LOOPCRAFT_TEST_MARKER") == "from-source-env"


def test_status_survives_corrupt_run_record(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 5 (review 06): one schema-invalid history file cannot crash status."""
    _env(monkeypatch, tmp_path)
    runs_dir = tmp_path / "mem" / "ledger" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "slack-triage__corrupt.json").write_text(
        '{"loop": "slack-triage", "status": 42}\n', encoding="utf-8"
    )
    rc = cli.main(["status"])
    assert rc == 0
    assert "slack-triage" in capsys.readouterr().out


def test_list_json_envelope(monkeypatch, tmp_path: Path, capsys) -> None:
    """`--json list` emits a consistent envelope listing the loops."""
    _env(monkeypatch, tmp_path)
    rc = cli.main(["--json", "list"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "list"
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert any(loop["id"] == "slack-triage" for loop in payload["data"]["loops"])


def test_run_dry_run_json_envelope(monkeypatch, tmp_path: Path, capsys) -> None:
    """`--json run --dry-run` emits the preflight/outputs in the envelope."""
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


def test_deps_check_optional_missing_does_not_fail(monkeypatch, tmp_path: Path, capsys) -> None:
    """Missing optional/future-runtime binaries are reported but never fail the check."""
    config = LoopcraftConfig(
        source_path=tmp_path / "s",
        memory_path=tmp_path / "m",
        dependencies={"git": "git"},
        optional_dependencies={"claude": "claude"},
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    rc = cli._cmd_deps_check(config, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["missing"] == []
    assert payload["data"]["optional_missing"] == ["claude"]


def test_deps_check_required_missing_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    """A missing required M1 binary fails the check."""
    config = LoopcraftConfig(
        source_path=tmp_path / "s",
        memory_path=tmp_path / "m",
        dependencies={"git": "git"},
        optional_dependencies={},
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    rc = cli._cmd_deps_check(config, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["missing"] == ["git"]


def test_deps_check_loop_runs_preflight(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 2: deps check --loop surfaces the loop's declared-dep preflight."""
    _env(monkeypatch, tmp_path)
    from loopcraft.runners import capabilities as capabilities_module

    monkeypatch.setitem(capabilities_module.AUTH_PROBES, "nv-tools", lambda config: "auth bundle 'nv-tools': boom")
    rc = cli.main(["deps", "check", "--loop", "slack-triage"])
    out = capsys.readouterr().out
    assert "preflight slack-triage" in out
    assert "boom" in out
    assert rc == 1
