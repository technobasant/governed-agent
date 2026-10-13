"""Chat models behind one small interface: Ollama by default, OpenAI and Anthropic optional.

Plus two wrappers that make the agent testable without a model in CI: `RecordingModel`
writes every reply to a cassette keyed by the exact request, and `ReplayModel` serves
them back. A cassette miss is an error, not a fallback — if the prompt changed, the
recording is stale and should be redone, not silently approximated.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

Message = dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelReply(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


class ModelError(RuntimeError):
    pass


class ChatModel(Protocol):
    name: str

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply: ...


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_unparsed": raw}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    return {}


class OllamaModel:
    """Ollama's /api/chat with native tool calling. Deterministic options by default."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
        timeout_s: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = f"ollama:{model}"
        self.model = model
        self.base_url = (
            base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        ).rstrip("/")
        self.options = {"temperature": 0, "seed": 0, "num_ctx": 16384, **(options or {})}
        self.think = think
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": m.get("content", "")}
                if m.get("tool_calls"):
                    item["tool_calls"] = [
                        {"function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in m["tool_calls"]
                    ]
                out.append(item)
            elif m["role"] == "tool":
                out.append(
                    {"role": "tool", "content": m["content"], "tool_name": m.get("name", "")}
                )
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(messages),
            "stream": False,
            "options": self.options,
            "think": self.think,
        }
        if tools and schema is None:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if schema is not None:
            body["format"] = schema
        try:
            resp = await self._client.post(f"{self.base_url}/api/chat", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelError(f"ollama request failed: {exc}") from exc
        data = resp.json()
        msg = data.get("message") or {}
        calls = [
            ToolCall(
                name=c["function"]["name"], arguments=_parse_args(c["function"].get("arguments"))
            )
            for c in msg.get("tool_calls") or []
        ]
        return ModelReply(
            content=msg.get("content") or "",
            tool_calls=calls,
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
        )


class OpenAIModel:
    """OpenAI-compatible chat completions with tools. Optional; needs OPENAI_API_KEY."""

    def __init__(self, model: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.name = f"openai:{model}"
        self.model = model
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        key = os.environ.get("OPENAI_API_KEY", "")
        self._client = client or httpx.AsyncClient(
            timeout=120, headers={"Authorization": f"Bearer {key}"}
        )

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c["arguments"]),
                                },
                            }
                            for c in m["tool_calls"]
                        ],
                    }
                )
            elif m["role"] == "tool":
                out.append(
                    {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
                )
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(messages),
            "temperature": 0,
        }
        if tools and schema is None:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "finding", "schema": schema},
            }
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelError(f"openai request failed: {exc}") from exc
        data = resp.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return ModelReply(
            content=msg.get("content") or "",
            tool_calls=[
                ToolCall(
                    id=c["id"],
                    name=c["function"]["name"],
                    arguments=_parse_args(c["function"].get("arguments")),
                )
                for c in msg.get("tool_calls") or []
            ],
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
        )


class AnthropicModel:
    """Anthropic Messages API with tools. Optional; needs ANTHROPIC_API_KEY."""

    def __init__(self, model: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.name = f"anthropic:{model}"
        self.model = model
        self._client = client or httpx.AsyncClient(
            timeout=120,
            headers={
                "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
            },
        )

    @staticmethod
    def _messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "system":
                continue
            if m["role"] == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                    for c in m.get("tool_calls") or []
                ]
                out.append(
                    {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}
                )
            elif m["role"] == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m["tool_call_id"],
                                "content": m["content"],
                            }
                        ],
                    }
                )
            else:
                out.append({"role": "user", "content": m["content"]})
        return system, out

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        system, msgs = self._messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 2048,
            "system": system,
            "messages": msgs,
            "temperature": 0,
        }
        specs = tools
        if schema is not None:
            specs = [ToolSpec("emit", "Return the result.", schema)]
            body["tool_choice"] = {"type": "tool", "name": "emit"}
        if specs:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in specs
            ]
        try:
            resp = await self._client.post("https://api.anthropic.com/v1/messages", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelError(f"anthropic request failed: {exc}") from exc
        data = resp.json()
        text = "".join(b.get("text", "") for b in data["content"] if b["type"] == "text")
        uses = [b for b in data["content"] if b["type"] == "tool_use"]
        usage = data.get("usage") or {}
        if schema is not None and uses:
            return ModelReply(
                content=json.dumps(uses[0]["input"]),
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
            )
        return ModelReply(
            content=text,
            tool_calls=[ToolCall(id=b["id"], name=b["name"], arguments=b["input"]) for b in uses],
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
        )


class ScriptedModel:
    """A deterministic model for tests and demos: a function from the request to a reply."""

    def __init__(
        self,
        handler: Callable[[list[Message], list[ToolSpec], dict[str, Any] | None], ModelReply],
        name: str = "scripted",
    ) -> None:
        self.name = name
        self._handler = handler

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        return self._handler(messages, tools, schema)


def request_key(
    model: str, messages: list[Message], tools: list[ToolSpec], schema: dict[str, Any] | None
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "tools": [[t.name, t.parameters] for t in tools],
        "schema": schema,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class CassetteMiss(ModelError):
    pass


class RecordingModel:
    """Passes through to a real model and appends every exchange to a JSONL cassette."""

    def __init__(self, inner: ChatModel, cassette: Path) -> None:
        self.inner = inner
        self.name = inner.name
        self.cassette = cassette
        cassette.parent.mkdir(parents=True, exist_ok=True)

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        reply = await self.inner.chat(messages, tools, schema=schema)
        key = request_key(self.name, messages, tools, schema)
        with self.cassette.open("a") as fh:
            fh.write(json.dumps({"key": key, "reply": reply.model_dump()}) + "\n")
        return reply


class ReplayModel:
    """Serves recorded replies. Same request in, same reply out; anything else is an error."""

    def __init__(self, cassette: Path, name: str) -> None:
        self.name = name
        self._replies: dict[str, ModelReply] = {}
        for line in cassette.read_text().splitlines():
            if line.strip():
                item = json.loads(line)
                self._replies[item["key"]] = ModelReply.model_validate(item["reply"])

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        key = request_key(self.name, messages, tools, schema)
        try:
            return self._replies[key]
        except KeyError:
            raise CassetteMiss(
                f"no recorded reply for this request ({key[:12]}); the prompt or tool results "
                "changed since the cassette was recorded — re-record it"
            ) from None


def model_from_spec(spec: str) -> ChatModel:
    """`ollama:qwen3:8b`, `openai:<model>`, `anthropic:<model>`, `replay:<cassette>@<model>`
    or `demo` (the deterministic stand-in used by the demo and the browser test)."""
    if spec == "demo":
        from ga_agent.demo_model import DemoModel

        return DemoModel()
    kind, _, rest = spec.partition(":")
    if kind == "ollama":
        return OllamaModel(rest, think=os.environ.get("GA_OLLAMA_THINK", "0") == "1")
    if kind == "openai":
        return OpenAIModel(rest)
    if kind == "anthropic":
        return AnthropicModel(rest)
    if kind == "replay":
        path, _, name = rest.partition("@")
        return ReplayModel(Path(path), name)
    raise ValueError(f"unknown model spec {spec!r}")
