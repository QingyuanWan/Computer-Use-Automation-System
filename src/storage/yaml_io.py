"""ArtifactStorage — YAML artifact I/O (ADR-008: the module that owns artifact serialization).

Boundary: imports only src/models + stdlib + PyYAML. No agent/executor/replay imports (leaf module).

Serialization strategy: `Artifact.model_dump(mode="json", by_alias=True, exclude_none=True)` yields plain
JSON-ish scalars (enums → their string values; the `from_`→`from` alias applied; no None clutter), then
`yaml.safe_dump(..., sort_keys=False, default_flow_style=False, allow_unicode=True)` renders human-readable
block YAML in the model's field order (version, metadata, parameters, captures, steps). PyYAML — not ruamel —
because we GENERATE artifacts (no comment round-tripping to preserve), it is already a declared dependency
(requirements.txt), and `sort_keys=False` + block style already satisfy the readability contract.

Round-trip identity holds under Pydantic equality: excluded None fields reload to their None defaults;
number-looking strings (e.g. sample_invocation account ids) are quoted by PyYAML so they reload as `str`;
enums reload from their values; the `from` alias reloads via populate_by_name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.models import Artifact

from .errors import ArtifactNotFoundError, ArtifactParseError

_SUFFIX = ".yaml"                       # one convention: .yaml (never .yml)
_SUPPORTED_VERSION_PREFIX = "0.1"       # no cross-version migration this deliverable; other majors -> error

# Optional list fields (both default_factory=list) whose empty form is pure noise in the dumped YAML.
# Dropping them when empty is equality-safe (they reload to their [] default) and de-clutters the artifact a
# REPORT reviewer reads. NOT generalized to all empty lists: required lists (e.g. success.required_phrases)
# must never be dropped, and empty dicts could be meaningful.
_DROP_IF_EMPTY_LIST = frozenset({"fallbacks", "expected_outcomes"})


def _declutter(obj: Any) -> Any:
    """Recursively drop the known optional-empty-list keys; leave everything else untouched."""
    if isinstance(obj, dict):
        return {k: _declutter(v) for k, v in obj.items()
                if not (k in _DROP_IF_EMPTY_LIST and v == [])}
    if isinstance(obj, list):
        return [_declutter(v) for v in obj]
    return obj


class ArtifactStorage:
    def __init__(self, artifacts_dir: Path = Path("artifacts")) -> None:
        self.artifacts_dir = Path(artifacts_dir)

    # ---------------- paths ----------------

    def _path(self, capability_name: str) -> Path:
        return self.artifacts_dir / f"{capability_name}{_SUFFIX}"

    def exists(self, capability_name: str) -> bool:
        """Cheap existence check without loading/parsing."""
        return self._path(capability_name).is_file()

    def list_artifacts(self) -> list[str]:
        """capability_names (file stems) available in artifacts_dir, sorted. Empty if the dir is absent."""
        if not self.artifacts_dir.is_dir():
            return []
        return sorted(p.stem for p in self.artifacts_dir.glob(f"*{_SUFFIX}") if p.is_file())

    # ---------------- write ----------------

    def save(self, artifact: Artifact, *, raise_on_exists: bool = False) -> Path:
        """Save to <artifacts_dir>/<capability_name>.yaml (UTF-8). Overwrites by default; with
        raise_on_exists=True a pre-existing file raises FileExistsError. Returns the file path."""
        path = self._path(artifact.metadata.capability_name)
        if raise_on_exists and path.exists():
            raise FileExistsError(f"artifact already exists: {path}")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = _declutter(
            artifact.model_dump(mode="json", by_alias=True, exclude_none=True))
        text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
        path.write_text(text, encoding="utf-8")
        return path

    # ---------------- read ----------------

    def load(self, capability_name: str) -> Artifact:
        """Load by capability_name. This bound method is the `Callable[[str], Artifact]` artifact_loader
        interface (ReplayEngine / CLI). Raises ArtifactNotFoundError / ArtifactParseError; a Pydantic
        ValidationError propagates unchanged for schema mismatches."""
        path = self._path(capability_name)
        if not path.is_file():
            raise ArtifactNotFoundError(capability_name)
        return self.load_from_path(path)

    # alias matching the ADR/CLI "load_by_name" vocabulary
    load_by_name = load

    def load_from_path(self, path: Path) -> Artifact:
        """Load an artifact from an explicit path (tests / CLI --artifact-file)."""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ArtifactNotFoundError(str(path)) from None
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ArtifactParseError(path, exc) from exc
        if not isinstance(raw, dict):
            raise ArtifactParseError(path, "top-level YAML is not a mapping")
        version = raw.get("version")
        if not isinstance(version, str) or not version.startswith(_SUPPORTED_VERSION_PREFIX):
            raise ArtifactParseError(
                path, f"unsupported schema version {version!r}; this build supports "
                      f"{_SUPPORTED_VERSION_PREFIX}.x (no cross-version migration)")
        return Artifact.model_validate(raw)   # Pydantic ValidationError propagates (already informative)
