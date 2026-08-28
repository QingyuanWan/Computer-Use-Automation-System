"""Phase-3 discovery-time escalation contracts (items 8-11) — mocked LLM + mocked executor.

Only fires when the injected handler is interactive (a human is reachable); the stub-based termination tests
in test_loop.py prove the non-interactive path is unchanged. Covered here:
  - item 8 : 3 consecutive locator fails (D2) -> escalate; Resume records a HumanInputAction + continues,
             Abort -> status 'aborted'. Plus the counter reset (a success clears the run).
  - item 9 : max steps without finish -> escalate; Resume grants extra step budget, Abort -> 'aborted'.
  - item 10: a safety violation -> escalate (Resume/Abort as above).
  - item 11: every escalation that Resumes/Takes-over emits a HumanInputAction step (D3), so replay
             deterministically re-triggers the same human intervention point.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.agent import DiscoveryAgent
from src.models import CapabilityType
from src.executor import ActionResult, LocatorResolutionError
from src.replay import EscalationHandler, EscalationOutcome
from src.replay.results import SafetyViolationError


# ---------------- fakes ----------------

def _tool(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def _usage(it=10, ot=5, cr=0, cw=0):
    return SimpleNamespace(input_tokens=it, output_tokens=ot,
                           cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


class _Resp:
    def __init__(self, blocks):
        self.content = blocks
        self.usage = _usage()

    def model_dump(self):
        return {"content": [getattr(b, "name", "text") for b in self.content]}


class _FakeMessages:
    def __init__(self, responses):
        self._it = iter(responses)
        self.snapshots: list = []

    async def create(self, **kwargs):
        self.snapshots.append(kwargs)
        return next(self._it)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


class _Handler(EscalationHandler):
    """Interactive discovery handler: records each reason shown, returns a scripted action (last repeats)."""
    is_interactive = True

    def __init__(self, *actions):
        self._actions = list(actions) or ["abort"]
        self.calls: list[str] = []

    async def escalate(self, ctx) -> EscalationOutcome:
        self.calls.append(ctx.reason)
        i = min(len(self.calls) - 1, len(self._actions) - 1)
        return EscalationOutcome(action=self._actions[i], operator_note="did it")


def _fake_executor(action_side_effect, aria="URL: http://t/start\nsome page state"):
    ex = MagicMock()
    page = MagicMock()
    page.url = "http://target.test/start"
    body = MagicMock()
    body.aria_snapshot = AsyncMock(return_value=aria)   # constant -> never exhausts (escalation re-observes too)
    page.locator = MagicMock(return_value=body)
    page.screenshot = AsyncMock(return_value=b"PNG")
    page.goto = AsyncMock()
    ex.page = page
    ex.execute_action = AsyncMock(side_effect=list(action_side_effect))
    return ex


def _agent(responses, actions, tmp_path, handler, **kw):
    client = _FakeClient(responses)
    ex = _fake_executor(actions)
    agent = DiscoveryAgent(executor=ex, client=client, evidence_root=tmp_path / "ev",
                           escalation_handler=handler, **kw)
    return agent, client


_CLICK = lambda i: _Resp([_tool(f"t{i}", "click", {"locator": {"css": "#x"}})])
_READ = lambda i: _Resp([_tool(f"t{i}", "read_text", {"locator": {"css": "#b"}})])
_FINISH = lambda i: _Resp([_tool(f"t{i}", "finish",
                                 {"result": {"v": "Z"}, "success_observed_phrases": ["some page state"]})])
_LRE = lambda: LocatorResolutionError("no match", screenshot_path="/tmp/x.png")
_OK = lambda: ActionResult(status="success", action="read_text", text="Z", resulting_url="u")


def _human_steps(artifact):
    return [s for s in artifact.steps if s.action == "human_input"]


# ---------------- item 8: consecutive locator fails ----------------

async def test_contract8_three_locator_fails_abort(tmp_path):
    agent, client = _agent([_CLICK(1), _CLICK(2), _CLICK(3)], [_LRE(), _LRE(), _LRE()], tmp_path, _Handler("abort"))
    res = await agent.discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "aborted" and res.artifact is None
    assert agent.escalation_handler.calls == ["locator_exhausted"]
    assert len(client.messages.snapshots) == 3          # escalated on the 3rd fail, then stopped


async def test_contract8_three_locator_fails_resume_records_human_input(tmp_path):
    agent, _ = _agent([_CLICK(1), _CLICK(2), _CLICK(3), _READ(4), _FINISH(5)],
                      [_LRE(), _LRE(), _LRE(), _OK()], tmp_path, _Handler("resume"))
    res = await agent.discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "success"
    assert agent.escalation_handler.calls == ["locator_exhausted"]
    hi = _human_steps(res.artifact)
    assert len(hi) == 1 and hi[0].reason == "locator_exhausted"   # item 11: reactive escalation -> planned step


async def test_contract8_counter_resets_on_success(tmp_path):
    # fail, SUCCESS (resets), fail, fail -> only 2 fails since the last success -> never escalates
    agent, _ = _agent([_CLICK(1), _READ(2), _CLICK(3), _CLICK(4), _FINISH(5)],
                      [_LRE(), _OK(), _LRE(), _LRE()], tmp_path, _Handler("abort"))
    res = await agent.discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "success"
    assert agent.escalation_handler.calls == []                  # threshold never reached
    assert _human_steps(res.artifact) == []


# ---------------- item 9: max steps without finish ----------------

async def test_contract9_max_steps_abort(tmp_path):
    agent, client = _agent([_READ(1), _READ(2), _READ(3)], [_OK(), _OK(), _OK()], tmp_path,
                           _Handler("abort"), max_steps=3)
    res = await agent.discover("never finishes", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "aborted" and res.artifact is None
    assert agent.escalation_handler.calls == ["max_steps"]
    assert len(client.messages.snapshots) == 3


async def test_contract9_max_steps_resume_grants_extra_steps(tmp_path):
    # max_steps=2; turn 2 hits budget -> escalate 'max_steps' -> resume grants +5 -> turn 3 finishes
    agent, client = _agent([_READ(1), _READ(2), _FINISH(3)], [_OK(), _OK()], tmp_path,
                           _Handler("resume"), max_steps=2)
    # generate_hints=False keeps the snapshot count precise to the loop (the secondary hint call is off).
    res = await agent.discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read, generate_hints=False)
    assert res.status == "success"
    assert agent.escalation_handler.calls == ["max_steps"]
    assert len(client.messages.snapshots) == 3          # 2 (original budget) + 1 extra granted step
    hi = _human_steps(res.artifact)
    assert len(hi) == 1 and hi[0].reason == "max_steps"


# ---------------- item 10: safety violation ----------------

async def test_contract10_safety_violation_abort(tmp_path):
    agent, client = _agent([_CLICK(1)], [SafetyViolationError("blocked")], tmp_path, _Handler("abort"))
    res = await agent.discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "aborted" and res.artifact is None
    assert agent.escalation_handler.calls == ["safety_violation"]
    assert len(client.messages.snapshots) == 1          # escalated on the first (only) safety hit


async def test_contract10_safety_violation_resume_records_human_input(tmp_path):
    agent, _ = _agent([_CLICK(1), _READ(2), _FINISH(3)], [SafetyViolationError("blocked"), _OK()],
                      tmp_path, _Handler("resume"))
    res = await agent.discover("g", "http://target.test/start", "cap", capability_type=CapabilityType.read)
    assert res.status == "success"
    assert agent.escalation_handler.calls == ["safety_violation"]
    hi = _human_steps(res.artifact)
    assert len(hi) == 1 and hi[0].reason == "safety_violation"
