"""OpenAI Responses-API wire translation — used by the ChatGPT-backend Codex API.

Copied from smart-loop and adapted to curry-leaves imports. Used by codex.py.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from curry_leaves.core.events import Delta
from curry_leaves.core.messages import (
    AssistantMessage,
    AudioBlock,
    Content,
    FileBlock,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    Usage,
)
from curry_leaves.providers.base import Context, Model, StreamChunk, StreamDone, StreamEvent, StreamOpts


def _text_of(blocks: list[Content]) -> str:
    return "".join(b.text for b in blocks if isinstance(b, TextBlock))


def _user_content(blocks: list[Content]) -> list[dict]:
    """Translate a user turn's blocks to Responses-API content parts. Images and PDFs
    ride along as native input_image/input_file (data URIs); audio isn't supported by
    the Responses API, so it must have been rendered to text upstream (never a raw
    AudioBlock here — the codex provider isn't in build_user_message's audio set)."""
    parts: list[dict] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            parts.append({"type": "input_text", "text": b.text})
        elif isinstance(b, ImageBlock):
            url = b.source if b.kind == "url" else f"data:{b.media_type};base64,{b.source}"
            parts.append({"type": "input_image", "image_url": url})
        elif isinstance(b, FileBlock):
            if b.kind == "url":
                parts.append({"type": "input_file", "file_url": b.source})
            else:
                parts.append({
                    "type": "input_file",
                    "filename": b.filename or "attachment.pdf",
                    "file_data": f"data:{b.media_type};base64,{b.source}",
                })
        elif isinstance(b, AudioBlock):  # unreachable via chat, but degrade gracefully
            parts.append({"type": "input_text", "text": "[audio attachment omitted — unsupported by this model]"})
    if not parts:
        parts.append({"type": "input_text", "text": ""})
    return parts


def build_responses_request(ctx: Context, model: Model, opts: StreamOpts) -> dict:
    input_items: list[dict] = []
    for msg in ctx.messages:
        if msg.role == "user":
            input_items.append(
                {"type": "message", "role": "user", "content": _user_content(msg.content)}
            )
        elif msg.role == "assistant":
            text = _text_of(msg.content)
            if text:
                input_items.append(
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
                )
            for b in msg.content:
                if isinstance(b, ToolCallBlock):
                    input_items.append(
                        {"type": "function_call", "call_id": b.id, "name": b.name, "arguments": json.dumps(b.arguments)}
                    )
        elif msg.role == "tool_result":
            input_items.append(
                {"type": "function_call_output", "call_id": msg.tool_call_id, "output": _text_of(msg.content)}
            )

    body: dict = {
        "model": model.id,
        "instructions": "\n\n".join(ctx.system_prompt) if ctx.system_prompt else "",
        "input": input_items,
        "stream": True,
        "store": False,
        "parallel_tool_calls": True,
    }
    if ctx.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
                "strict": False,
            }
            for t in ctx.tools
        ]
        body["tool_choice"] = "auto"
    if opts.reasoning_effort:
        body["reasoning"] = {"effort": opts.reasoning_effort, "summary": "auto"}
    if opts.response_format:
        body["text"] = {"format": {"type": "json_object"}}
    return body


async def parse_responses_stream(events: AsyncIterator[dict]) -> AsyncIterator[StreamEvent]:
    msg = AssistantMessage()
    text = TextBlock(text="")
    thinking = ThinkingBlock(thinking="")
    have_text = have_thinking = False
    tools: dict[int, ToolCallBlock] = {}
    tool_args_raw: dict[int, str] = {}

    def rebuild() -> None:
        ordered: list[Content] = []
        if have_thinking:
            ordered.append(thinking)
        if have_text:
            ordered.append(text)
        ordered.extend(tools[i] for i in sorted(tools))
        msg.content = ordered

    async for ev in events:
        t = ev.get("type")

        if t in ("response.created", "response.in_progress"):
            resp = ev.get("response") or {}
            if msg.model is None:
                msg.model = resp.get("model")

        elif t == "response.output_text.delta":
            have_text = True
            d = ev.get("delta") or ""
            text.text += d
            rebuild()
            yield StreamChunk(delta=Delta(kind="text", block_index=0, value=d), partial=msg.model_copy(deep=True))

        elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            have_thinking = True
            d = ev.get("delta") or ""
            thinking.thinking += d
            rebuild()
            yield StreamChunk(delta=Delta(kind="thinking", block_index=0, value=d), partial=msg.model_copy(deep=True))

        elif t == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                idx = ev.get("output_index", len(tools))
                tools[idx] = ToolCallBlock(id=item.get("call_id") or item.get("id") or f"call_{idx}",
                                           name=item.get("name", ""), arguments={})
                tool_args_raw[idx] = ""
                rebuild()

        elif t == "response.function_call_arguments.delta":
            idx = ev.get("output_index", 0)
            if idx not in tools:
                tools[idx] = ToolCallBlock(id=f"call_{idx}", name="", arguments={})
                tool_args_raw[idx] = ""
            d = ev.get("delta") or ""
            tool_args_raw[idx] += d
            rebuild()
            yield StreamChunk(delta=Delta(kind="tool_args", block_index=idx, value=d), partial=msg.model_copy(deep=True))

        elif t == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                idx = ev.get("output_index", 0)
                blk = tools.setdefault(idx, ToolCallBlock(id=f"call_{idx}", name="", arguments={}))
                if item.get("call_id"):
                    blk.id = item["call_id"]
                if item.get("name"):
                    blk.name = item["name"]
                if item.get("arguments") is not None:
                    tool_args_raw[idx] = item["arguments"]

        elif t == "response.completed":
            usage = (ev.get("response") or {}).get("usage") or {}
            msg.usage = Usage(
                input=usage.get("input_tokens", 0),
                output=usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )

        elif t in ("response.failed", "error"):
            resp = ev.get("response") or {}
            err = (resp.get("error") or {}).get("message") or ev.get("message") or "Codex request failed"
            raise RuntimeError(err)

    for idx, raw in tool_args_raw.items():
        try:
            tools[idx].arguments = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            tools[idx].arguments = {}
    msg.stop_reason = "tool_use" if tools else "stop"
    rebuild()
    yield StreamDone(message=msg.model_copy(deep=True))
