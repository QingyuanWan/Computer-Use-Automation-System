"""Guard: every hand-authored artifact under test_artifacts/ must validate against the CURRENT schema.

These fixtures are used by manual/end-to-end (Phase B) runs and are easy to let rot when the model evolves
(e.g. a nested `action:` shape, an undeclared `{{param}}`, or a top-level `checkpoint:` all silently break
replay only at run time). Loading each one here fails fast in CI instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import ArtifactStorage

_DIR = Path(__file__).resolve().parents[2] / "test_artifacts"
_YAMLS = sorted(_DIR.glob("*.yaml")) if _DIR.exists() else []


@pytest.mark.skipif(not _YAMLS, reason="no test_artifacts/*.yaml present")
@pytest.mark.parametrize("path", _YAMLS, ids=lambda p: p.name)
def test_fixture_artifact_validates(path):
    art = ArtifactStorage(_DIR).load_from_path(path)
    assert art.metadata.capability_name
    for step in art.steps:                       # flat discriminated union, never a nested action dict
        assert isinstance(step.action, str)


def test_planned_intervention_fixture_shape():
    """The Phase-B planned-intervention fixture: parameterized login + a human_input pause + a final
    checkpoint attached to a step (not the artifact)."""
    p = _DIR / "phaseB_planned_intervention_test.yaml"
    if not p.exists():
        pytest.skip("planned-intervention fixture absent")
    art = ArtifactStorage(_DIR).load_from_path(p)
    assert any(s.action == "human_input" for s in art.steps)
    assert {"caller_username", "caller_password"} <= set(art.parameters.properties)
    assert [s.id for s in art.steps if getattr(s, "checkpoint", None)]   # checkpoint attached to a step
