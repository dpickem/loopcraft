"""Tests for the runtime-neutral capability probe registry."""

from __future__ import annotations

from pathlib import Path

from loopcraft.config import LoopcraftConfig, SchedulerConfig
from loopcraft.manifest import LoopManifest
from loopcraft.runners import capabilities


def _config(tmp_path: Path) -> LoopcraftConfig:
    """Return a config with a demo skill + verify file staged under tmp_path."""
    source = tmp_path / "src"
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "skills" / "demo" / "verify.md").write_text("done", encoding="utf-8")
    return LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")


def _manifest(**overrides) -> LoopManifest:
    """Return a demo manifest whose skill/verify assets exist under _config."""
    base = {
        "id": "demo",
        "name": "Demo",
        "cadence": {"type": "cron", "at": "0 9 * * *"},
        "logic": {"skill": "skills/demo/SKILL.md", "verify": "skills/demo/verify.md"},
    }
    base.update(overrides)
    return LoopManifest.from_dict(base)


def test_capabilities_pass_for_valid_loop(tmp_path: Path) -> None:
    """A loop with existing assets and no external deps reports no problems."""
    assert capabilities.check_declared_capabilities(_manifest(), _config(tmp_path)) == []


def test_slack_probe_executes_resolved_binary_on_scheduled_path(monkeypatch, tmp_path: Path) -> None:
    """Review 04: the live Slack probe runs the which()-resolved binary in the
    scheduled environment, not a bare command on the operator PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    nv = bindir / "nv-tools"
    nv.write_text("#!/bin/sh\n", encoding="utf-8")
    nv.chmod(0o755)
    config = (
        _config(tmp_path)
        .model_copy(update={"scheduler": SchedulerConfig(path=str(bindir))})
        .for_scheduled_preflight()
    )
    captured: dict[str, object] = {}

    def fake_run_probe(cmd, *, timeout_s, env=None):  # noqa: ANN001, ANN003
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr(capabilities, "run_probe", fake_run_probe)
    assert capabilities.probe_slack_api(config) is None
    # The probe executes the absolute resolved nv-tools, not the bare name.
    assert captured["cmd"][0] == str(nv)
    assert captured["env"]["PATH"] == str(bindir)


def test_capabilities_flag_missing_skill(tmp_path: Path) -> None:
    """A missing skill asset is reported by the shared capability check."""
    manifest = _manifest(logic={"skill": "skills/ghost/SKILL.md"})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("skill not found" in p for p in problems)


def test_capabilities_flag_missing_content_config(tmp_path: Path) -> None:
    """Finding 2 (review 05): a declared content.config must exist at preflight."""
    manifest = _manifest(content={"config": "config/does-not-exist.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("content config not found" in p for p in problems)


def test_capabilities_flag_directory_content_config(tmp_path: Path) -> None:
    """Finding 2 (review 05): a content.config resolving to a directory is a problem."""
    config = _config(tmp_path)
    (config.source_path / "config" / "dir.yaml").mkdir(parents=True)
    manifest = _manifest(content={"config": "config/dir.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("not a regular file" in p for p in problems)


def test_capabilities_accept_existing_content_config(tmp_path: Path) -> None:
    """A declared content.config that exists as a regular file passes preflight."""
    config = _config(tmp_path)
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "x.yaml").write_text("a: 1\n", encoding="utf-8")
    manifest = _manifest(content={"config": "config/x.yaml"})
    assert capabilities.check_declared_capabilities(manifest, config) == []


def test_capabilities_reject_local_only_content_config(tmp_path: Path) -> None:
    """Finding 6 (review 06): a local sibling never replaces the public file.

    The committed public config is the existence contract; a gitignored
    ``*.local.*`` override alone must fail preflight so the loop cannot pass on
    one host and break after a clean checkout.
    """
    config = _config(tmp_path)
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "x.local.yaml").write_text("a: 1\n", encoding="utf-8")
    manifest = _manifest(content={"config": "config/x.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("content config not found" in p for p in problems)


def test_capabilities_flag_malformed_yaml_content_config(tmp_path: Path) -> None:
    """Finding 4 (review 07): a content config that cannot parse fails preflight."""
    config = _config(tmp_path)
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "x.yaml").write_text("sources: [unclosed\n", encoding="utf-8")
    manifest = _manifest(content={"config": "config/x.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("content config invalid" in p for p in problems)


def test_capabilities_run_registered_content_validator(tmp_path: Path) -> None:
    """Finding 4 (review 07): shipped loops validate against their typed model.

    Well-formed YAML with a schema violation (an unknown field) must fail the
    x-intel preflight through the registered ``IntelConfig`` validator.
    """
    config = _config(tmp_path)
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "x_intel.yaml").write_text("not_a_real_field: 1\n", encoding="utf-8")
    manifest = _manifest(id="x-intel", content={"config": "config/x_intel.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("content config invalid" in p for p in problems)

    # The forbidden output override is caught by the same validator.
    (cfg_dir / "x_intel.yaml").write_text(
        "output: {latest_json: state/research/x/custom.json}\n", encoding="utf-8"
    )
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("must not override 'output'" in p for p in problems)


def test_capabilities_flag_malformed_local_override(tmp_path: Path) -> None:
    """Finding 4 (review 07): the *effective* (local) config is what preflight parses."""
    config = _config(tmp_path)
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "x.yaml").write_text("a: 1\n", encoding="utf-8")
    (cfg_dir / "x.local.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    manifest = _manifest(content={"config": "config/x.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("content config invalid" in p for p in problems)


def test_capabilities_valid_content_config_passes(tmp_path: Path) -> None:
    """A well-formed effective content config produces no validity problems."""
    config = _config(tmp_path)
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "x_intel.yaml").write_text("ranking: {top_posts: 5}\n", encoding="utf-8")
    manifest = _manifest(id="x-intel", content={"config": "config/x_intel.yaml"})
    assert capabilities.check_declared_capabilities(manifest, config) == []


def test_capabilities_reject_symlinked_local_override_escaping_source(tmp_path: Path) -> None:
    """Finding 1 (review 08): preflight contains the local sibling like staging.

    A ``*.local.*`` symlink resolving outside the source tree must be a
    preflight problem, not silently read and validated — otherwise ``--dry-run``
    reports ready for a config the real run refuses to stage.
    """
    config = _config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.yaml").write_text("a: 1\n", encoding="utf-8")
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "x.yaml").write_text("a: 1\n", encoding="utf-8")
    (cfg_dir / "x.local.yaml").symlink_to(outside / "private.yaml")

    manifest = _manifest(content={"config": "config/x.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("escapes" in p for p in problems)


def test_x_api_auth_probe_accepts_either_token(tmp_path: Path, monkeypatch) -> None:
    """Finding 3 (review 08): either X credential satisfies the auth contract."""
    config = _config(tmp_path)
    manifest = _manifest(depends_on={"auth": ["x-api"]})

    for present in ("X_API_BEARER_TOKEN", "X_API_OAUTH2_ACCESS_TOKEN"):
        monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("X_API_OAUTH2_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv(present, "token")
        assert capabilities.check_declared_capabilities(manifest, config) == [], present

    monkeypatch.setenv("X_API_BEARER_TOKEN", "token")
    monkeypatch.setenv("X_API_OAUTH2_ACCESS_TOKEN", "token")
    assert capabilities.check_declared_capabilities(manifest, config) == []

    monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("X_API_OAUTH2_ACCESS_TOKEN", raising=False)
    problems = capabilities.check_declared_capabilities(manifest, config)
    assert any("X_API_BEARER_TOKEN or X_API_OAUTH2_ACCESS_TOKEN" in p for p in problems)


def _x_snapshot_manifest() -> "LoopManifest":
    """Return an x-intel manifest declaring the shipped content config path."""
    return _manifest(id="x-intel", content={"config": "config/x_intel.yaml"})


def _write_x_config_with_snapshot(config, snapshot: str) -> None:
    """Write an X content config referencing ``snapshot`` under the temp source."""
    cfg_dir = config.source_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "x_intel.yaml").write_text(
        f"sources: {{following_snapshot: {snapshot}}}\n", encoding="utf-8"
    )


def test_capabilities_flag_missing_following_snapshot(tmp_path: Path) -> None:
    """Finding 4 (review 08): a configured snapshot must exist at preflight."""
    config = _config(tmp_path)
    _write_x_config_with_snapshot(config, "config/x_following_snapshot.local.json")
    problems = capabilities.check_declared_capabilities(_x_snapshot_manifest(), config)
    assert any("content config invalid" in p and "snapshot not found" in p for p in problems)


def test_capabilities_flag_malformed_following_snapshot(tmp_path: Path) -> None:
    """Finding 4 (review 08): a malformed or wrong-shape snapshot fails preflight."""
    config = _config(tmp_path)
    _write_x_config_with_snapshot(config, "config/x_following_snapshot.local.json")
    snapshot = config.source_path / "config" / "x_following_snapshot.local.json"

    snapshot.write_text("{not json", encoding="utf-8")
    problems = capabilities.check_declared_capabilities(_x_snapshot_manifest(), config)
    assert any("not readable JSON" in p for p in problems)

    snapshot.write_text('["just", "a", "list"]\n', encoding="utf-8")
    problems = capabilities.check_declared_capabilities(_x_snapshot_manifest(), config)
    assert any("unexpected shape" in p for p in problems)


def test_capabilities_accept_valid_following_snapshot(tmp_path: Path) -> None:
    """A valid configured snapshot passes preflight and is reported as an asset."""
    config = _config(tmp_path)
    _write_x_config_with_snapshot(config, "config/x_following_snapshot.local.json")
    (config.source_path / "config" / "x_following_snapshot.local.json").write_text(
        '{"users": [{"username": "alice"}]}\n', encoding="utf-8"
    )
    manifest = _x_snapshot_manifest()
    assert capabilities.check_declared_capabilities(manifest, config) == []
    # Finding 4 (review 08): staging learns about the snapshot via the resolver.
    assert capabilities.content_assets(manifest, config) == [
        "config/x_following_snapshot.local.json"
    ]


def test_capabilities_flag_escaping_content_config(tmp_path: Path) -> None:
    """A traversing content.config is reported as a preflight problem."""
    manifest = _manifest(content={"config": "../outside.yaml"})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("content.config" in p for p in problems)


def test_capabilities_flag_unknown_auth_and_api(tmp_path: Path) -> None:
    """Auth bundles/APIs without a registered probe are reported."""
    manifest = _manifest(depends_on={"auth": ["mystery"], "apis": ["ghost-api"]})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("mystery" in p for p in problems)
    assert any("ghost-api" in p for p in problems)


def test_capabilities_probe_registry_is_injectable(tmp_path: Path, monkeypatch) -> None:
    """A registered probe's failure message flows through the check."""
    monkeypatch.setitem(capabilities.API_PROBES, "slack", lambda config: "api 'slack': boom")
    manifest = _manifest(depends_on={"apis": ["slack"]})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("boom" in p for p in problems)
