"""Message assembly + prompt-cache breakpoints (ADR-004).

Caching = ephemeral cache_control on system + tools (static, in tools.py) + a ROLLING breakpoint on the last
message each turn, which incrementally caches the growing prefix so cache_read_input_tokens > 0 from turn 2.
Screenshots are attached ONLY to the current user turn and never carry a breakpoint, so a fallback screenshot
does not invalidate the cached prefix.
"""
from __future__ import annotations

from typing import Any, Optional


def block_to_dict(block: Any) -> dict[str, Any]:
    """Serialize an Anthropic response content block into a messages-API dict."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": block.type}


def _image_block(screenshot_b64: str) -> dict[str, Any]:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64}}


def build_user_turn(pending_results: Optional[list[dict[str, str]]], aria: str,
                    screenshot_b64: Optional[str]) -> dict[str, Any]:
    """The user turn: either the initial observation, or tool_result blocks answering the previous turn's
    tool calls. The observation (aria + optional screenshot) rides on the LAST content block."""
    obs_text = "Current observation (page state):\n" + aria
    if pending_results is None:                       # first turn — plain observation
        content: list[dict[str, Any]] = [{"type": "text", "text": obs_text}]
        if screenshot_b64:
            content.append(_image_block(screenshot_b64))
        return {"role": "user", "content": content}

    content = []
    for i, pr in enumerate(pending_results):
        is_last = i == len(pending_results) - 1
        if is_last:
            inner: list[dict[str, Any]] = [{"type": "text", "text": pr["text"] + "\n\n" + obs_text}]
            if screenshot_b64:
                inner.append(_image_block(screenshot_b64))
        else:
            inner = [{"type": "text", "text": pr["text"]}]
        content.append({"type": "tool_result", "tool_use_id": pr["tool_use_id"], "content": inner})
    return {"role": "user", "content": content}


def apply_cache_breakpoints(messages: list[dict[str, Any]]) -> None:
    """Strip stale breakpoints, then mark the last content block of the last message (rolling prefix cache)."""
    for m in messages:
        for blk in m["content"]:
            if isinstance(blk, dict):
                blk.pop("cache_control", None)
    if messages:
        messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
