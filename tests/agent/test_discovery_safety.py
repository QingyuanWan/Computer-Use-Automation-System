"""D3: discovery-time safety enforcement. The DiscoveryAgent runs the SAME SafetyGate.check_action the replay
executor runs, in _handle_tool BEFORE dispatch. An off-allowlist domain (or unsanctioned action type) is a
TERMINAL block during discovery — status="safety_blocked", no escalation. All mocked — no browser, no LLM."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.agent import DiscoveryAgent
from src.safety import SafetyGate


def _executor():
    """A duck-typed discovery executor whose execute_action always 'succeeds' (mocked); used to prove the
    gate lets an allowlisted action through to dispatch."""
    return SimpleNamespace(
        page=SimpleNamespace(),
        execute_action=AsyncMock(return_value=SimpleNamespace(
            status="success", resulting_url="https://parabank.parasoft.com/parabank/overview.htm", text=None)))


async def test_discovery_blocks_offdomain_navigate(tmp_path):
    ag = DiscoveryAgent(_executor(), client=SimpleNamespace(), evidence_root=tmp_path, safety_gate=SafetyGate())
    pending = []
    block = SimpleNamespace(name="navigate", id="t1", input={"url": "https://evil.example.com/steal"})
    r = await ag._handle_tool(block, "s1", 1, scope=None, consecutive={}, recorded=[], pending_results=pending)
    assert r["status"] == "safety_blocked"
    assert "evil.example.com" in r["detail"]
    assert pending and "BLOCKED (safety)" in pending[0]["text"]
    ag.executor.execute_action.assert_not_awaited()   # blocked BEFORE any dispatch


async def test_discovery_allows_allowlisted_navigate(tmp_path):
    ex = _executor()
    ag = DiscoveryAgent(ex, client=SimpleNamespace(), evidence_root=tmp_path, safety_gate=SafetyGate())
    block = SimpleNamespace(name="navigate", id="t1",
                            input={"url": "https://parabank.parasoft.com/parabank/overview.htm"})
    r = await ag._handle_tool(block, "s1", 1, scope=None, consecutive={}, recorded=[], pending_results=[])
    assert r["status"] == "success"
    ex.execute_action.assert_awaited_once()           # on-allowlist -> reaches dispatch


async def test_discovery_no_gate_is_backward_compatible(tmp_path):
    # Default (no gate injected) preserves the pre-D3 behavior: no safety check, dispatch runs as before.
    ex = _executor()
    ag = DiscoveryAgent(ex, client=SimpleNamespace(), evidence_root=tmp_path)   # no safety_gate
    block = SimpleNamespace(name="navigate", id="t1", input={"url": "https://evil.example.com/anything"})
    r = await ag._handle_tool(block, "s1", 1, scope=None, consecutive={}, recorded=[], pending_results=[])
    assert r["status"] == "success"                   # no gate -> not blocked (unchanged legacy behavior)
    ex.execute_action.assert_awaited_once()
