"""Storage-layer errors (ADR-008: storage is a leaf module). Callers can distinguish 'not found' from
'malformed YAML' from a Pydantic schema-validation error (which propagates unchanged from src/models)."""
from __future__ import annotations


class ArtifactStorageError(Exception):
    """Base class for storage errors."""


class ArtifactNotFoundError(ArtifactStorageError):
    """No artifact file exists for the requested capability_name / path."""

    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(f"no artifact found for {ref!r}")


class ArtifactParseError(ArtifactStorageError):
    """The file is not readable as a valid artifact document (YAML syntax error, non-mapping top level, or
    an unsupported schema version). Carries the file path + the underlying cause (PyYAML errors already
    include a line/column mark)."""

    def __init__(self, path, underlying) -> None:
        self.path = str(path)
        self.underlying = underlying
        super().__init__(f"failed to parse artifact at {self.path}: {underlying}")
