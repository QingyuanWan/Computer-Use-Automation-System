"""Unit tests for the discovery loop (mocked LLM + mocked executor — no real API, no Playwright).

Covers behavior-contract items 1 (caching observable), 2 (ARIA-only default + screenshot triggers),
3 (finish requires success_observed_phrases), 8 (loop terminates: max_steps + repeated failures).
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent import DiscoveryAgent, FinishValidationError
from src.agent.agent import _validate_finish
from src.models import CapabilityType
from src.executor import ActionResult, LocatorResolutionError


# ---------------- fakes ----------------

def _tool(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def _usage(it=10, ot=5, cr=0, cw=0):
    return SimpleNamespace(input_tokens=it, output_tokens=ot,
                           cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


class _Resp:
    def __init__(self, blocks, usage):
        self.content = blocks
        self.usage = usage

    def model_dump(self):
        return {"content": [getattr(b, "name", "text") for b in self.content]}


class _FakeMessages:
    def __init__(self, responses):
        self._it = iter(responses)
        self.snapshots: list[dict] = []

    async def create(self, **kwargs):
        self.snapshots.append(copy.deepcopy(kwargs))   # snapshot BEFORE later in-place mutation
        return next(self._it)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _fake_executor(aria_list, action_side_effect):
    ex = MagicMock()
    page = MagicMock()
    page.url = "http://target.test/start"
    body = MagicMock()
    body.aria_snapshot = AsyncMock(side_effect=list(aria_list))
    page.locator = MagicMock(return_value=body)
    page.screenshot = AsyncMock(return_value=b"PNGDATA")
    page.goto = AsyncMock()
    ex.page = page
    ex.execute_action = AsyncMock(side_effect=list(action_side_effect))
    return ex


def _has_image(user_msg) -> bool:
    for blk in user_msg["content"]:
        if blk.get("type") == "image":
            return True
        if blk.get("type") == "tool_result":
            for inner in blk.get("content", []):
                if isinstance(inner, dict) and inner.get("type") == "image":
                    return True
    return False


def _agent(client, ex, tmp_path, **kw):
    return DiscoveryAgent(executor=ex, client=client, evidence_root=tmp_path / "ev", **kw)


# ---------------- item 3: finish validation (pure) ----------------

def test_finish_requires_success_phrases():
    with pytest.raises(FinishValidationError):
        _validate_finish({"result": {"x": 1}})                       # missing
    with pytest.raises(FinishValidationError):
        _validate_finish({"result": {"x": 1}, "success_observed_phrases": []})   # empty
    _validate_finish({"result": {"x": 1}, "success_observed_phrases": ["ok"]})   # valid: no raise


# ---------------- items 1, 2(default), + happy emission ----------------

async def test_happy_loop_caching_and_aria_default(tmp_path):
    responses = [
        _Resp([_tool("t1", "navigate", {"url": "http://target.test/overview"})], _usage(cr=0)),
        _Resp([_tool("t2", "read_text", {"locator": {"css": "#b"}})], _usage(cr=100)),
        _Resp([_tool("t3", "finish", {"result": {"balance": "$415.50"},
                                      "success_observed_phrases": ["Balance: $415.50"]})], _usage(cr=200)),
    ]
    aria = ["URL: .../start\nlogin page",
            "URL: .../overview\nAccounts Overview",
            "URL: .../overview\nAccount 13899 — Balance: $415.50"]
    actions = [ActionResult(status="success", action="navigate", resulting_url="http://target.test/overview"),
               ActionResult(status="success", action="read_text", text="$415.50",
                            resulting_url="http://target.test/overview")]
    client = _FakeClient(responses)
    ex = _fake_executor(aria, actions)

    # generate_hints=False: this test exercises the discovery LOOP (caching, ARIA default), not the secondary
    # hint-generation LLM call — keeping it off makes the snapshot assertions below precise to the loop.
    res = await _agent(client, ex, tmp_path).discover("look up balance", "http://target.test/start", "lookup",
                                                      capability_type=CapabilityType.read, generate_hints=False)

    assert res.status == "success"
    assert res.artifact is not None and res.artifact.metadata.validated is False
    assert res.artifact.metadata.capability_type.value == "read"
    assert {c.name for c in res.artifact.captures} == {"balance"}
    cps = [s.checkpoint for s in res.artifact.steps if s.checkpoint is not None]
    assert cps and cps[0].success.required_phrases == ["Balance: {{balance}}"]

    # item 1: caching configured on the last (turn-3) request
    last = client.messages.snapshots[-1]
    assert last["system"][0].get("cache_control") == {"type": "ephemeral"}
    assert last["tools"][-1].get("cache_control") == {"type": "ephemeral"}
    assert last["messages"][-1]["content"][-1].get("cache_control") == {"type": "ephemeral"}
    assert res.usage["cache_read"] == 300              # observable cache reads (simulated) from turn 2+

    # item 2 (default): first turn's observation carried NO screenshot
    assert not _has_image(client.messages.snapshots[0]["messages"][0])

    # evidence recorded per step
    from pathlib import Path
    assert (Path(res.evidence_dir) / "step_01_observation_aria.txt").exists()


# ---------------- item 2: screenshot trigger after a 0-match ----------------

async def test_screenshot_trigger_on_zero_match(tmp_path):
    responses = [
        _Resp([_tool("t1", "click", {"locator": {"css": "#missing"}})], _usage()),
        _Resp([_tool("t2", "read_text", {"locator": {"css": "#b"}})], _usage(cr=50)),
        _Resp([_tool("t3", "finish", {"result": {"v": "X"}, "success_observed_phrases": ["X value"]})], _usage()),
    ]
    aria = ["obs1 login", "obs2 form", "obs3 X value here"]
    actions = [LocatorResolutionError("no match", aria="a", screenshot_path="/tmp/x.png"),
               ActionResult(status="success", action="read_text", text="X", resulting_url="u")]
    client = _FakeClient(responses)
    res = await _agent(client, _fake_executor(aria, actions), tmp_path).discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)

    assert res.status == "success"
    assert not _has_image(client.messages.snapshots[0]["messages"][-1])   # turn 1: no screenshot
    assert _has_image(client.messages.snapshots[1]["messages"][-1])       # turn 2: screenshot injected after 0-match


# ---------------- item 2: screenshot trigger on explicit request ----------------

async def test_screenshot_trigger_on_request(tmp_path):
    responses = [
        _Resp([_tool("t1", "request_screenshot", {"reason": "ambiguous"})], _usage()),
        _Resp([_tool("t2", "read_text", {"locator": {"css": "#b"}})], _usage(cr=50)),
        _Resp([_tool("t3", "finish", {"result": {"v": "Y"}, "success_observed_phrases": ["Y value"]})], _usage()),
    ]
    aria = ["obs1", "obs2", "obs3 Y value here"]
    actions = [ActionResult(status="success", action="read_text", text="Y", resulting_url="u")]
    client = _FakeClient(responses)
    res = await _agent(client, _fake_executor(aria, actions), tmp_path).discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)

    assert res.status == "success"
    assert not _has_image(client.messages.snapshots[0]["messages"][-1])   # turn 1: no screenshot
    assert _has_image(client.messages.snapshots[1]["messages"][-1])       # turn 2: screenshot after request


# ---------------- robustness: a raw backend exception is recoverable, not a crash ----------------

async def test_raw_executor_exception_is_recoverable(tmp_path):
    # turn 1 type_text raises a RAW (non-ExecutorError) exception (e.g. Playwright fill on a <tr>);
    # the loop must record it and continue, then finish on a later turn.
    responses = [
        _Resp([_tool("t1", "type_text", {"locator": {"role": "row", "name": "First Name:"}, "value": "John"})], _usage()),
        _Resp([_tool("t2", "type_text", {"locator": {"role": "textbox", "nth": 2}, "value": "John"})], _usage(cr=50)),
        _Resp([_tool("t3", "finish", {"result": {"v": "Z"}, "success_observed_phrases": ["Z value"]})], _usage()),
    ]
    aria = ["obs1", "obs2", "obs3 Z value here"]
    actions = [RuntimeError("Locator.fill: Element is not an <input>"),           # raw backend error
               ActionResult(status="success", action="type_text", value="John", resulting_url="u")]
    client = _FakeClient(responses)
    res = await _agent(client, _fake_executor(aria, actions), tmp_path).discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "success"       # did not crash on the raw exception


# ---------------- item 8a: max_steps termination ----------------

async def test_loop_terminates_on_max_steps(tmp_path):
    responses = [_Resp([_tool(f"t{i}", "read_text", {"locator": {"css": "#b"}})], _usage(cr=10)) for i in range(6)]
    actions = [ActionResult(status="success", action="read_text", text="z", resulting_url="u") for _ in range(6)]
    aria = [f"obs{i}" for i in range(10)]
    client = _FakeClient(responses)
    res = await _agent(client, _fake_executor(aria, actions), tmp_path, max_steps=3).discover(
        "never finishes", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "max_steps"
    assert res.artifact is None
    assert len(client.messages.snapshots) == 3          # exactly max_steps LLM calls


# ---------------- item 8b: repeated-failure termination ----------------

async def test_loop_terminates_on_repeated_failures(tmp_path):
    responses = [_Resp([_tool(f"t{i}", "click", {"locator": {"css": "#x"}})], _usage()) for i in range(6)]
    actions = [LocatorResolutionError("no match", screenshot_path="/tmp/x.png") for _ in range(6)]
    aria = [f"obs{i}" for i in range(10)]
    client = _FakeClient(responses)
    res = await _agent(client, _fake_executor(aria, actions), tmp_path,
                       max_steps=25, max_repeated_failures=3).discover("stuck", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "stuck"
    assert res.artifact is None
    assert len(client.messages.snapshots) == 3          # stopped after 3 consecutive click failures
