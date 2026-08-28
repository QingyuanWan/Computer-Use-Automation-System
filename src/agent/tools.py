"""LLM tool schemas + system prompt (ADR-004 tool set; ADR-005 finish extension).

Tool set: click, type_text, navigate, read_text, find_matching, request_screenshot, finish.
`finish.success_observed_phrases` is REQUIRED (ADR-005) — omission is a validation error, not a silent empty.
`request_screenshot` is the ADR-004 trigger (b): the LLM asks for a screenshot on the next observation.
"""
from __future__ import annotations

from typing import Any

_LOCATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": ("Identify a target element. Use role+name, or role+nth (0-based index) for unlabeled "
                    "fields, or css/id/href_pattern/text. Other fields allowed."),
    "properties": {
        "role": {"type": "string"},
        "name": {"type": "string"},
        "nth": {"type": "integer", "description": "0-based positional index among matches"},
        "css": {"type": "string"},
        "id": {"type": "string"},
        "href_pattern": {"type": "string"},
        "text": {"type": "string"},
    },
    "additionalProperties": True,
}

_CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "object", "properties": {
            "required_phrases": {"type": "array", "items": {"type": "string"}},
            "target": {"type": "string"}}, "required": ["required_phrases"]},
    },
    "required": ["success"],
}

TOOLS: list[dict[str, Any]] = [
    {"name": "navigate", "description": "Navigate to an absolute URL.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "click", "description": "Click an element. For a dropdown option this selects it.",
     "input_schema": {"type": "object", "properties": {"locator": _LOCATOR_SCHEMA}, "required": ["locator"]}},
    {"name": "type_text", "description": "Type text into a field. For a <select> this picks the option.",
     "input_schema": {"type": "object",
                      "properties": {"locator": _LOCATOR_SCHEMA, "value": {"type": "string"}},
                      "required": ["locator", "value"]}},
    {"name": "read_text", "description": "Return the visible text of an element.",
     "input_schema": {"type": "object", "properties": {"locator": _LOCATOR_SCHEMA}, "required": ["locator"]}},
    {"name": "find_matching",
     "description": "Iterate a captured list of candidates and select the first whose probe checkpoint passes.",
     "input_schema": {"type": "object", "properties": {
         "candidates": {"type": "string", "description": "name of a previously captured list"},
         "probe": {"type": "object", "properties": {
             "action": {"type": "string"}, "locator": _LOCATOR_SCHEMA, "checkpoint": _CHECKPOINT_SCHEMA},
             "required": ["action", "locator", "checkpoint"]},
         "capture": {"type": "object", "properties": {
             "variable": {"type": "string"}, "value_from": {"type": "string"}},
             "required": ["variable"]},
     }, "required": ["candidates", "probe", "capture"]}},
    {"name": "request_screenshot",
     "description": "Ask for a screenshot to be included with the NEXT observation (use when the ARIA text "
                    "alone is ambiguous).",
     "input_schema": {"type": "object",
                      "properties": {"reason": {"type": "string"}}, "required": []}},
    {"name": "request_human_input",
     "description": ("Signal that a HUMAN must act on the page before automation can continue — e.g. enter a "
                     "2FA code sent to their phone, solve a captcha, or make a choice only a person can make. "
                     "IMPORTANT: this returns NOTHING — you do NOT receive the code/value; the human performs "
                     "the action directly in the browser. After it completes, OBSERVE the new page state and "
                     "continue. Recorded into the capability so every replay pauses here for a human. Use "
                     "sparingly — only when a step genuinely cannot be automated."),
     "input_schema": {"type": "object", "properties": {
         "prompt": {"type": "string", "description": "What to tell the human to do, shown to them verbatim. "
                                                     "State exactly what to TYPE and that they must NOT submit "
                                                     "the form themselves (the automation clicks the button); "
                                                     "e.g. \"Type the username and password into the login "
                                                     "fields — do not click Log In.\" (The system also appends "
                                                     "a 'click Done when finished' reminder.)"},
         "reason": {"type": "string", "description": "Why a human is needed (e.g. \"2fa\", \"captcha\")."}},
         "required": ["prompt", "reason"]}},
    {"name": "finish",
     "description": "Call when the goal is achieved. Report the answer AND the exact ARIA strings you used "
                    "as evidence of success.",
     "input_schema": {"type": "object", "properties": {
         "result": {"type": "object",
                    "description": "the values to return, e.g. {\"balance\": \"$415.50\", ...}"},
         "success_observed_phrases": {"type": "array", "items": {"type": "string"},
                                      "description": (
                                          "REQUIRED. Exact substrings from the current ARIA snapshot that "
                                          "constitute evidence you completed the goal. Choose phrases that "
                                          "would still appear on a successful REPLAY by a different user in "
                                          "a fresh session: prefer stable semantic labels (e.g. \"Balance\", "
                                          "\"Account Details\", \"Welcome\", \"Transfer Complete!\") over "
                                          "per-session dynamic values (dollar amounts, account numbers, user "
                                          "ids, timestamps). Include a per-session value ONLY if it is "
                                          "essential to prove success (e.g. echoing back an input you "
                                          "provided) — reverse-parameterization will handle it if the value "
                                          "came from a known parameter/capture.")},
     }, "required": ["result", "success_observed_phrases"]}},
]

# Static tools block carries a cache breakpoint on the last tool (ADR-004).
TOOLS_CACHED: list[dict[str, Any]] = [dict(t) for t in TOOLS]
TOOLS_CACHED[-1] = {**TOOLS_CACHED[-1], "cache_control": {"type": "ephemeral"}}

# tool names that map to executor actions (vs agent-internal finish/request_screenshot)
EXECUTOR_TOOLS = frozenset({"navigate", "click", "type_text", "read_text", "find_matching"})

_SYSTEM_TEMPLATE = """You are an autonomous web agent operating a web application through a small set of tools.

GOAL: {goal}

Start URL is already loaded: {target_url}

PERCEPTION: each turn you receive the CURRENT page state as an ARIA accessibility-tree snapshot (its first
line is the URL). A screenshot is included ONLY when you ask for one via request_screenshot, or right after
an action whose locator matched nothing. Prefer the ARIA tree; ask for a screenshot only when it is
genuinely ambiguous.

ACTING: call exactly one tool per turn and observe the result before the next. A 'locator' is a small JSON
object; for unlabeled form fields use role + nth (0-based). Values you type may need to be unique (e.g. a
fresh username) — synthesize one if the goal implies registration.

STOP: when the goal is achieved, call finish with `result` (the values to report) and
`success_observed_phrases` (the EXACT ARIA substrings proving success — this field is required). For those
phrases, prefer stable semantic labels (like "Balance", "Welcome", "Account Details") that would recur on a
successful replay, rather than per-session values like specific dollar amounts or account numbers.

CREDENTIALS: when the goal requires login credentials (username, password, or similar) and they are NOT
already provided in your CALLER PARAMETERS, you MUST obtain each one with the request_human_input tool — DO
NOT invent credentials or copy them from the goal text. Call request_human_input(prompt="Please enter the
username for [service]", reason="credential_request") for the username, then separately for the password.
Never type a credential you were not given as a caller parameter or by request_human_input."""

_CALLER_PARAMS_TEMPLATE = """

CALLER PARAMETERS: this capability is invoked by a calling agent with these parameters:
{param_lines}
Use these EXACT values literally in your tool calls (typing them, navigating to them, or locating by them).
They will be reverse-parameterized in the recorded artifact into {{{{name}}}} placeholders, so the capability
becomes reusable with different values. Do not invent different values for these parameters."""


def build_system_blocks(goal: str, target_url: str,
                        caller_parameters: "dict[str, str] | None" = None) -> list[dict[str, Any]]:
    """System prompt as a single cache-broken text block (ADR-004). When caller_parameters are supplied
    (ADR-9), the LLM is told which literal values are caller-provided so emission can reverse-parameterize
    them out of the artifact's step fields."""
    text = _SYSTEM_TEMPLATE.format(goal=goal, target_url=target_url)
    if caller_parameters:
        param_lines = "\n".join(f"  - {k} = {v!r}" for k, v in caller_parameters.items())
        text += _CALLER_PARAMS_TEMPLATE.format(param_lines=param_lines)
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
