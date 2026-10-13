"""Provider adapters: the request each one sends, and how its reply is read back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ga_agent.demo_model import DemoModel
from ga_agent.llm import (
    AnthropicModel,
    CassetteMiss,
    Message,
    ModelError,
    OllamaModel,
    OpenAIModel,
    ReplayModel,
    ToolSpec,
    model_from_spec,
)

TOOLS = [ToolSpec("query_warehouse", "Run a query.", {"type": "object", "properties": {}})]
CONVERSATION: list[Message] = [
    {"role": "system", "content": "be careful"},
    {"role": "user", "content": "how many?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "q1", "name": "query_warehouse", "arguments": {"sql": "SELECT 1"}}],
    },
    {"role": "tool", "tool_call_id": "q1", "name": "query_warehouse", "content": '{"rows": [[1]]}'},
]


def client(
    reply: dict[str, Any], seen: list[dict[str, Any]], status: int = 200
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(status, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ollama_tools_and_tool_results() -> None:
    seen: list[dict[str, Any]] = []
    reply = {
        "message": {
            "content": "",
            "tool_calls": [
                {"function": {"name": "query_warehouse", "arguments": {"sql": "SELECT 2"}}}
            ],
        },
        "prompt_eval_count": 50,
        "eval_count": 7,
    }
    model = OllamaModel("qwen3:8b", base_url="http://ollama", client=client(reply, seen))
    out = await model.chat(CONVERSATION, TOOLS)
    body = seen[0]["body"]
    assert seen[0]["url"] == "http://ollama/api/chat"
    assert body["options"]["temperature"] == 0 and body["options"]["seed"] == 0
    assert body["think"] is False
    assert body["tools"][0]["function"]["name"] == "query_warehouse"
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == {"sql": "SELECT 1"}
    assert body["messages"][3] == {
        "role": "tool",
        "content": '{"rows": [[1]]}',
        "tool_name": "query_warehouse",
    }
    assert out.tool_calls[0].arguments == {"sql": "SELECT 2"}
    assert (out.tokens_in, out.tokens_out) == (50, 7)


async def test_ollama_structured_output_drops_tools() -> None:
    seen: list[dict[str, Any]] = []
    model = OllamaModel(
        "qwen3:8b", base_url="http://ollama", client=client({"message": {"content": "{}"}}, seen)
    )
    out = await model.chat(CONVERSATION, TOOLS, schema={"type": "object"})
    assert seen[0]["body"]["format"] == {"type": "object"}
    assert "tools" not in seen[0]["body"]
    assert out.content == "{}"


async def test_ollama_failure_is_a_model_error() -> None:
    model = OllamaModel("qwen3:8b", base_url="http://ollama", client=client({}, [], status=500))
    with pytest.raises(ModelError, match="ollama request failed"):
        await model.chat(CONVERSATION, TOOLS)


async def test_openai_mapping() -> None:
    seen: list[dict[str, Any]] = []
    reply = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "query_warehouse",
                                "arguments": '{"sql": "SELECT 3"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4},
    }
    model = OpenAIModel("gpt-test", client=client(reply, seen))
    out = await model.chat(CONVERSATION, TOOLS)
    body = seen[0]["body"]
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"sql": "SELECT 1"}'
    assert body["messages"][3]["tool_call_id"] == "q1"
    assert out.tool_calls[0].arguments == {"sql": "SELECT 3"}
    await model.chat(CONVERSATION, TOOLS, schema={"type": "object"})
    assert seen[1]["body"]["response_format"]["type"] == "json_schema"
    assert "tools" not in seen[1]["body"]


async def test_openai_failure_is_a_model_error() -> None:
    model = OpenAIModel("gpt-test", client=client({}, [], status=429))
    with pytest.raises(ModelError):
        await model.chat(CONVERSATION, TOOLS)


async def test_anthropic_mapping() -> None:
    seen: list[dict[str, Any]] = []
    reply = {
        "content": [
            {"type": "text", "text": "checking"},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "query_warehouse",
                "input": {"sql": "SELECT 4"},
            },
        ],
        "usage": {"input_tokens": 11, "output_tokens": 5},
    }
    model = AnthropicModel("claude-test", client=client(reply, seen))
    out = await model.chat(CONVERSATION, TOOLS)
    body = seen[0]["body"]
    assert body["system"] == "be careful"
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "q1",
        "content": '{"rows": [[1]]}',
    }
    assert out.content == "checking"
    assert out.tool_calls[0].arguments == {"sql": "SELECT 4"}


async def test_anthropic_structured_output_uses_a_forced_tool() -> None:
    seen: list[dict[str, Any]] = []
    reply = {
        "content": [
            {"type": "tool_use", "id": "tu_2", "name": "emit", "input": {"status": "answered"}}
        ],
        "usage": {},
    }
    model = AnthropicModel("claude-test", client=client(reply, seen))
    out = await model.chat(CONVERSATION, TOOLS, schema={"type": "object"})
    assert seen[0]["body"]["tool_choice"] == {"type": "tool", "name": "emit"}
    assert json.loads(out.content) == {"status": "answered"}


async def test_anthropic_failure_is_a_model_error() -> None:
    model = AnthropicModel("claude-test", client=client({}, [], status=401))
    with pytest.raises(ModelError):
        await model.chat(CONVERSATION, TOOLS)


async def test_unparseable_tool_arguments_are_kept_for_the_record() -> None:
    reply = {
        "message": {
            "tool_calls": [
                {"function": {"name": "query_warehouse", "arguments": "{not json"}},
                {"function": {"name": "query_warehouse", "arguments": "[1]"}},
                {"function": {"name": "query_warehouse"}},
            ]
        }
    }
    model = OllamaModel("m", base_url="http://o", client=client(reply, []))
    out = await model.chat(CONVERSATION, TOOLS)
    assert [c.arguments for c in out.tool_calls] == [
        {"_unparsed": "{not json"},
        {"_value": [1]},
        {},
    ]


async def test_a_replay_miss_is_loud(tmp_path: Path) -> None:
    cassette = tmp_path / "empty.jsonl"
    cassette.write_text("\n")
    with pytest.raises(CassetteMiss, match="re-record"):
        await ReplayModel(cassette, "ollama:qwen3:8b").chat(CONVERSATION, TOOLS)


def test_model_specs(tmp_path: Path) -> None:
    cassette = tmp_path / "c.jsonl"
    cassette.write_text("")
    assert isinstance(model_from_spec("demo"), DemoModel)
    assert model_from_spec("ollama:qwen3:8b").name == "ollama:qwen3:8b"
    assert model_from_spec("openai:gpt-test").name == "openai:gpt-test"
    assert model_from_spec("anthropic:claude-test").name == "anthropic:claude-test"
    assert model_from_spec(f"replay:{cassette}@ollama:qwen3:8b").name == "ollama:qwen3:8b"
    with pytest.raises(ValueError, match="unknown model spec"):
        model_from_spec("mystery:1")
