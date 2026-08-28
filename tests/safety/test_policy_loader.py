"""§3.4 configurable allowlist: load_policy reads a JSON file, else falls back to the in-code default."""
import json
from pathlib import Path

from src.safety import PARABANK_POLICY, load_policy
from src.safety.policy import SafetyPolicy


def test_absent_file_falls_back_to_default(tmp_path):
    assert load_policy(tmp_path / "nope.json") is PARABANK_POLICY


def test_valid_file_loads_policy(tmp_path):
    p = tmp_path / "safety_policy.json"
    p.write_text(json.dumps({"allowed_domains": ["example.com"],
                             "allowed_actions": ["click", "navigate"]}), encoding="utf-8")
    pol = load_policy(p)
    assert isinstance(pol, SafetyPolicy)
    assert pol.allowed_domains == frozenset({"example.com"})
    assert pol.allowed_actions == frozenset({"click", "navigate"})


def test_malformed_file_falls_back(tmp_path):
    p = tmp_path / "safety_policy.json"
    p.write_text("{ not valid json", encoding="utf-8")
    assert load_policy(p) is PARABANK_POLICY


def test_empty_lists_fall_back(tmp_path):
    # a broken config must never silently widen (or empty) the allowlist -> collapse to the safe default
    p = tmp_path / "safety_policy.json"
    p.write_text(json.dumps({"allowed_domains": [], "allowed_actions": []}), encoding="utf-8")
    assert load_policy(p) is PARABANK_POLICY


def test_committed_example_matches_in_code_default():
    root = Path(__file__).resolve().parent.parent.parent
    pol = load_policy(root / "safety_policy.example.json")
    assert pol.allowed_domains == PARABANK_POLICY.allowed_domains
    assert pol.allowed_actions == PARABANK_POLICY.allowed_actions
