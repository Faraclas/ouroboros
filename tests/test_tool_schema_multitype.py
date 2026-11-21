"""Offline and cross-provider contract tests for multi-type tool schema normalization."""

from __future__ import annotations

import copy

from jsonschema import Draft7Validator, Draft202012Validator

from ouroboros.copilot_responses import _copilot_chat_to_responses_payload
from ouroboros.llm import LLMClient
from ouroboros.openai_chat_custom import project_function_tools_to_openai_custom
from ouroboros.openai_chat_dispatch import sanitize_function_tools
from tests.provider_contract_catalog import (
    assert_portable_tool_schemas,
    shipped_builtin_tool_schemas,
)


def _find_tool(tools, name: str):
    return next(t for t in tools if (t.get("function") or {}).get("name") == name)


def test_sanitize_function_tools_normalizes_multitypes_recursively_without_mutation():
    raw_tools = [
        {
            "type": "function",
            "function": {
                "name": "mcp_ext_multi",
                "description": "External multi-type tool",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "check": {
                            "type": ["array", "string"],
                            "items": {"type": "string"},
                            "minItems": 1,
                            "minLength": 2,
                            "description": "Command or argv",
                            "default": ["type", ["a", "b"]],
                            "examples": [{"type": ["x", "y"]}],
                        },
                        "nullable_ids": {
                            "type": ["array", "null"],
                            "items": {"type": "string"},
                        },
                        "nested": {
                            "type": "object",
                            "additionalProperties": {
                                "type": ["integer", "string", "null"],
                                "minimum": 0,
                                "pattern": "^[0-9]+$",
                                "enum": [1, "auto", None],
                            },
                            "properties": {
                                "tuple_items": {
                                    "type": "array",
                                    "prefixItems": [
                                        {"type": ["integer", "string"]},
                                        {"type": "boolean"},
                                    ],
                                }
                            },
                        },
                        "with_defs": {
                            "$ref": "#/$defs/target",
                        },
                    },
                    "$defs": {
                        "target": {
                            "type": ["integer", "string"],
                            "const": ["not", "a", "schema"],
                        }
                    },
                },
            },
        }
    ]
    before = copy.deepcopy(raw_tools)

    sanitized = sanitize_function_tools(
        raw_tools,
        description_normalizer=LLMClient._stringify_tool_description,
    )

    assert raw_tools == before, "caller input must not be mutated"
    assert_portable_tool_schemas(sanitized)

    props = sanitized[0]["function"]["parameters"]["properties"]

    # check: array + string with type-specific keywords partitioned
    check = props["check"]
    assert "type" not in check
    assert "items" not in check
    assert "minItems" not in check
    assert "minLength" not in check
    assert check["description"] == "Command or argv"
    assert check["default"] == ["type", ["a", "b"]]
    assert check["examples"] == [{"type": ["x", "y"]}]
    assert check["anyOf"] == [
        {"type": "array", "items": {"type": "string"}, "minItems": 1},
        {"type": "string", "minLength": 2},
    ]

    # Single-type + null nullable schema is preserved
    assert props["nullable_ids"] == {"type": ["array", "null"], "items": {"type": "string"}}

    # Nested additionalProperties with null + partitioned enum
    addl = props["nested"]["additionalProperties"]
    assert "type" not in addl
    assert "enum" not in addl
    assert addl["anyOf"] == [
        {"type": "integer", "minimum": 0, "enum": [1]},
        {"type": "string", "pattern": "^[0-9]+$", "enum": ["auto"]},
        {"type": "null", "enum": [None]},
    ]

    # prefixItems normalized
    tuple_first = props["nested"]["properties"]["tuple_items"]["prefixItems"][0]
    assert tuple_first == {"anyOf": [{"type": "integer"}, {"type": "string"}]}

    # $defs normalized without touching const literal
    target_def = sanitized[0]["function"]["parameters"]["$defs"]["target"]
    assert target_def == {
        "const": ["not", "a", "schema"],
        "anyOf": [{"type": "integer"}, {"type": "string"}],
    }

    # Valid under Draft7 and Draft2020-12
    params = sanitized[0]["function"]["parameters"]
    Draft202012Validator.check_schema(params)
    validator = Draft202012Validator(params)
    assert validator.is_valid({"check": ["pytest", "-q"], "nullable_ids": None})
    assert validator.is_valid({"check": "pytest -q", "nested": {"additional": 1}})
    assert not validator.is_valid({"check": [123]})
    assert not validator.is_valid({"check": "x"})  # violates minLength=2


def test_sanitize_function_tools_normalizes_draft7_tuple_items():
    tools = [{
        "type": "function",
        "function": {
            "name": "draft7_tuple",
            "description": "test",
            "parameters": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "array",
                        "items": [{"type": ["integer", "string"]}, {"type": "boolean"}],
                    }
                },
            },
        },
    }]
    sanitized = sanitize_function_tools(
        tools, description_normalizer=LLMClient._stringify_tool_description,
    )
    assert_portable_tool_schemas(sanitized)
    params = sanitized[0]["function"]["parameters"]
    Draft7Validator.check_schema(params)
    assert params["properties"]["pair"]["items"][0] == {
        "anyOf": [{"type": "integer"}, {"type": "string"}]
    }


def test_sanitize_function_tools_preserves_existing_anyof_with_multitype():
    tools = [{
        "type": "function",
        "function": {
            "name": "existing_anyof",
            "description": "test",
            "parameters": {
                "type": "object",
                "properties": {
                    "val": {
                        "type": ["integer", "string"],
                        "anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "minLength": 2}],
                    }
                },
            },
        },
    }]
    sanitized = sanitize_function_tools(
        tools, description_normalizer=LLMClient._stringify_tool_description,
    )
    assert_portable_tool_schemas(sanitized)
    val = sanitized[0]["function"]["parameters"]["properties"]["val"]
    assert "type" not in val
    assert "allOf" in val
    validator = Draft202012Validator(sanitized[0]["function"]["parameters"])
    assert validator.is_valid({"val": 2})
    assert validator.is_valid({"val": "ab"})
    assert not validator.is_valid({"val": 0})
    assert not validator.is_valid({"val": "a"})


def test_affected_builtins_emit_portable_anyof_across_all_provider_projections():
    canonical = shipped_builtin_tool_schemas()
    assert_portable_tool_schemas(canonical)

    verify_schema = _find_tool(canonical, "verify_and_record")["function"]["parameters"]
    assert verify_schema["properties"]["check"]["anyOf"] == [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string"},
    ]
    assert "type" not in verify_schema["properties"]["check"]

    gh_read_schema = _find_tool(canonical, "github_read")["function"]["parameters"]
    assert gh_read_schema["properties"]["target"]["anyOf"] == [
        {"type": "integer"},
        {"type": "string"},
    ]

    gh_write_schema = _find_tool(canonical, "github_write")["function"]["parameters"]
    assert gh_write_schema["properties"]["target"]["anyOf"] == [
        {"type": "integer"},
        {"type": "string"},
    ]
    assert gh_write_schema["properties"]["reply_to"]["anyOf"] == [
        {"type": "integer"},
        {"type": "string"},
    ]

    # Nullable single-type+null preserved on get_task_result
    gtr_schema = _find_tool(canonical, "get_task_result")["function"]["parameters"]
    assert gtr_schema["properties"]["authority_fields"]["type"] == ["array", "null"]

    subset = [
        _find_tool(canonical, "verify_and_record"),
        _find_tool(canonical, "github_read"),
        _find_tool(canonical, "github_write"),
        _find_tool(canonical, "get_task_result"),
    ]

    # OpenAI Chat / OpenRouter / OpenAI-compatible
    client = LLMClient(api_key="test")
    for model in (
        "openai-compatible::gemini-4-hydrogen",
        "openai-compatible::gemini-4-neon",
        "openai/gpt-5.6-luna",
    ):
        target = client._resolve_remote_target(model)
        kwargs = client._build_remote_kwargs(
            target,
            [{"role": "user", "content": "hi"}],
            "medium",
            256,
            "auto",
            None,
            subset,
            skip_capability_fetch=True,
        )
        assert_portable_tool_schemas(kwargs["tools"])
        if model == "openai/gpt-5.6-luna":
            for t in kwargs["tools"]:
                assert t["function"]["strict"] is False

        # Copilot Responses projection
        resp_payload = _copilot_chat_to_responses_payload({
            "model": "gpt-6.2-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": kwargs["tools"],
        })
        assert len(resp_payload["tools"]) == len(subset)
        for rt in resp_payload["tools"]:
            Draft202012Validator.check_schema(rt["parameters"])

    # Custom-tool projection
    custom_catalog = project_function_tools_to_openai_custom(
        LLMClient._sanitize_chat_completion_tools(subset)
    )
    assert set(custom_catalog.tool_names) == {
        "verify_and_record",
        "github_read",
        "github_write",
        "get_task_result",
    }

    # Anthropic projection
    anthropic_tools = LLMClient._build_anthropic_tools(subset)
    assert len(anthropic_tools) == len(subset)
    for at in anthropic_tools:
        Draft202012Validator.check_schema(at["input_schema"])

    # GigaChat projection
    gigachat_funcs = LLMClient._gigachat_functions(subset)
    assert len(gigachat_funcs) == len(subset)
    for gf in gigachat_funcs:
        Draft202012Validator.check_schema(gf["parameters"])
        assert_portable_tool_schemas([{
            "type": "function",
            "function": {"name": gf["name"], "description": gf.get("description", ""), "parameters": gf["parameters"]},
        }])
