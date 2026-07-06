"""Tests for the bounded, allowlisted dependency probe runner."""

from __future__ import annotations

import pytest

from loopcraft.probes import PROBE_ALLOWED_BINARIES, run_probe


def test_run_probe_rejects_non_allowlisted_binary() -> None:
    """PR review: probes are not an arbitrary command runner."""
    with pytest.raises(ValueError, match="not allowlisted"):
        run_probe(["rm", "-rf", "/tmp/x"], timeout_s=1)
    with pytest.raises(ValueError, match="not allowlisted"):
        run_probe([], timeout_s=1)
    # A path to a non-allowlisted binary is rejected by basename too.
    with pytest.raises(ValueError, match="not allowlisted"):
        run_probe(["/bin/echo", "hi"], timeout_s=1)


def test_run_probe_missing_allowlisted_binary_returns_none(monkeypatch, tmp_path) -> None:
    """An allowlisted binary that is absent probes as None, not an exception."""
    monkeypatch.setenv("PATH", str(tmp_path))  # nv-tools not on this PATH
    assert "nv-tools" in PROBE_ALLOWED_BINARIES
    assert run_probe(["nv-tools", "health"], timeout_s=1) is None
