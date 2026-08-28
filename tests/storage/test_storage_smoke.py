"""Smoke tests for src/storage YAML I/O — round-trip integrity, listing, errors, encoding.

Uses tmp_path for filesystem isolation. Round-trip equality relies on Pydantic value-equality (frozen
models), so `parse(dump(a)) == a` compares every field + nested type.
"""
from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from src.models import (
    Artifact,
    ArtifactMetadata,
    CapabilityType,
    Checkpoint,
    Locator,
    ReadTextAction,
    SuccessCriteria,
)
from src.storage import ArtifactNotFoundError, ArtifactParseError, ArtifactStorage

# A fully-populated, model-valid artifact exercising every field type: metadata (incl. sample_invocation),
# parameters (plain + generate marker + provisional param-export), captures (with source+extract using the
# `from` alias + extract.all list capture + a source=None read-bound capture), all five step actions, a
# Locator fallback chain, find_matching (Probe + url_template + candidate), and a checkpoint with
# success + expected_outcomes + a NON-ASCII phrase.
_RICH_YAML = """
version: "0.1.0"
metadata:
  capability_name: lookup_balance_rich
  capability_type: read
  sample_invocation:
    account_id: "13899"
  validated: true
  created_at: "2026-08-19T00:00:00Z"
  discovered_by_model: claude-sonnet-4-6
  target_app_hint: parabank
parameters:
  type: object
  properties:
    account_id: { type: string }
    username: { type: string, generate: unique_string, export: true }
  required: [account_id]
captures:
  - name: balance
    type: string
    source_step: read_balance
    source:
      locator: { strategy: css_id, css: "#balance" }
      extract: { pattern: "\\\\$(\\\\d+\\\\.\\\\d+)", from: text }
    export: true
    returned_as: current_balance
  - name: account_ids
    type: "string[]"
    source_step: list_accounts
    source:
      locator: { strategy: href_pattern, href_pattern: "activity.htm?id=" }
      extract: { pattern: "id=(\\\\d+)", from: href, all: true }
  - name: raw_balance
    type: string
    source_step: read_balance
steps:
  - id: enter_user
    action: type_text
    locator:
      strategy: role_nth
      role: textbox
      index: 0
      fallbacks:
        - { strategy: css_id, css: "#customer.username" }
    value: "{{account_id}}"
  - id: submit
    action: click
    locator: { strategy: role_name, role: button, name: "Search" }
  - id: list_accounts
    action: read_text
    locator: { strategy: css_id, css: "#accountTable" }
  - id: pick
    action: find_matching
    candidates: account_ids
    probe:
      action: navigate
      locator: { strategy: url_template, url_template: "activity.htm?id={{candidate}}" }
      checkpoint:
        success: { required_phrases: ["Account Details"] }
    capture: { variable: chosen, value_from: candidate, export: true }
  - id: go_detail
    action: navigate
    url: "activity.htm?id={{account_id}}"
  - id: read_balance
    action: read_text
    locator: { strategy: css_id, css: "#balance" }
    checkpoint:
      success:
        required_phrases: ["Balance:", "{{balance}}", "余额 café ☕"]
        target: "#rightPanel"
      expected_outcomes:
        - name: account_not_found
          required_phrases: ["Could not find account # {{account_id}}"]
      wait_ms: 5000
      poll_interval_ms: 500
"""


def _minimal() -> Artifact:
    return Artifact(
        version="0.1.0",
        metadata=ArtifactMetadata(capability_name="min_cap", capability_type=CapabilityType.read),
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#b"))],
    )


def _rich() -> Artifact:
    return Artifact.model_validate(yaml.safe_load(_RICH_YAML))


def _named(name: str) -> Artifact:
    return Artifact(
        version="0.1.0",
        metadata=ArtifactMetadata(capability_name=name, capability_type=CapabilityType.read),
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#b"))],
    )


# 1 ----------------------------------------------------------------------------
def test_roundtrip_minimal(tmp_path):
    store = ArtifactStorage(tmp_path)
    a = _minimal()
    path = store.save(a)
    assert path == tmp_path / "min_cap.yaml"
    assert store.load("min_cap") == a          # Pydantic value-equality across all fields


# 2 ----------------------------------------------------------------------------
def test_roundtrip_fully_populated(tmp_path):
    store = ArtifactStorage(tmp_path)
    a = _rich()
    store.save(a)
    loaded = store.load("lookup_balance_rich")
    assert loaded == a
    # spot-check nested types survived: the `from` alias, extract.all, generate marker, expected_outcomes
    bal = next(c for c in loaded.captures if c.name == "balance")
    assert bal.source.extract.from_ == "text"
    ids = next(c for c in loaded.captures if c.name == "account_ids")
    assert ids.source.extract.all is True and ids.type == "string[]"
    assert loaded.parameters.properties["username"].generate.value == "unique_string"
    read = next(s for s in loaded.steps if s.id == "read_balance")
    assert read.checkpoint.expected_outcomes[0].name == "account_not_found"


# 3 ----------------------------------------------------------------------------
def test_sample_invocation_string_fidelity(tmp_path):
    store = ArtifactStorage(tmp_path)
    a = _rich()
    path = store.save(a)
    loaded = store.load("lookup_balance_rich")
    assert loaded.metadata.sample_invocation == {"account_id": "13899"}
    assert isinstance(loaded.metadata.sample_invocation["account_id"], str)   # NOT int 13899
    # and the raw YAML quoted it so it can't be read back as an int
    assert "'13899'" in path.read_text(encoding="utf-8")


# 4 ----------------------------------------------------------------------------
def test_list_and_exists(tmp_path):
    store = ArtifactStorage(tmp_path)
    for n in ("cap_a", "cap_b", "cap_c"):
        store.save(_named(n))
    assert store.list_artifacts() == ["cap_a", "cap_b", "cap_c"]
    assert store.exists("cap_b") is True
    assert store.exists("cap_z") is False
    # loader-callable interface (ReplayEngine/CLI): load_by_name is load
    assert store.load_by_name("cap_a").metadata.capability_name == "cap_a"


# 5 ----------------------------------------------------------------------------
def test_not_found_raises(tmp_path):
    store = ArtifactStorage(tmp_path)
    with pytest.raises(ArtifactNotFoundError) as ei:
        store.load("nope")
    assert "nope" in str(ei.value)


# 6 ----------------------------------------------------------------------------
def test_malformed_yaml_raises_parse_error(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("version: '0.1.0'\nmetadata: {unbalanced: [1, 2\n", encoding="utf-8")
    store = ArtifactStorage(tmp_path)
    with pytest.raises(ArtifactParseError) as ei:
        store.load_from_path(p)
    assert "broken.yaml" in str(ei.value)


def test_unsupported_version_raises_parse_error(tmp_path):
    p = tmp_path / "future.yaml"
    p.write_text("version: '9.9.9'\nmetadata: {capability_name: x, capability_type: read}\nsteps: []\n",
                 encoding="utf-8")
    store = ArtifactStorage(tmp_path)
    with pytest.raises(ArtifactParseError) as ei:
        store.load_from_path(p)
    assert "9.9.9" in str(ei.value) and "version" in str(ei.value).lower()


def test_schema_mismatch_propagates_pydantic_error(tmp_path):
    # valid YAML, supported version, but violates the model (missing required steps) -> Pydantic error,
    # NOT ArtifactParseError — a caller can distinguish schema-invalid from malformed-file.
    p = tmp_path / "badschema.yaml"
    p.write_text("version: '0.1.0'\nmetadata: {capability_name: x, capability_type: read}\nsteps: []\n",
                 encoding="utf-8")
    store = ArtifactStorage(tmp_path)
    with pytest.raises(ValidationError):
        store.load_from_path(p)


# 7 ----------------------------------------------------------------------------
def test_overwrite_default_and_raise_on_exists(tmp_path):
    store = ArtifactStorage(tmp_path)
    store.save(_named("dup"))
    store.save(_named("dup"))                       # default: overwrite silently
    with pytest.raises(FileExistsError):
        store.save(_named("dup"), raise_on_exists=True)


# 8 ----------------------------------------------------------------------------
def test_utf8_non_ascii_preserved(tmp_path):
    store = ArtifactStorage(tmp_path)
    a = _rich()                                     # its checkpoint carries "余额 café ☕"
    path = store.save(a)
    text = path.read_text(encoding="utf-8")
    assert "余额 café ☕" in text                    # written literally (allow_unicode), not \uXXXX-escaped
    loaded = store.load("lookup_balance_rich")
    phrases = next(s for s in loaded.steps if s.id == "read_balance").checkpoint.success.required_phrases
    assert "余额 café ☕" in phrases
