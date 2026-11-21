"""Deterministic shipped-tool catalog for static and live provider contracts.

This is intentionally test-only.  Production ``ToolRegistry.schemas()`` also
discovers live extension and MCP tools, which would make a provider canary
depend on mutable local state and may refresh external MCP servers.  Contract
tests need the complete shipped built-in surface before any runtime filtering,
so they project the registry's already-loaded built-in entries directly.
"""

from __future__ import annotations

import copy
import pathlib
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validators


ROOT_SCHEMA_COMBINATORS = ("anyOf", "oneOf", "allOf")
_LITERAL_SCHEMA_KEYS = frozenset({
    "$comment",
    "const",
    "default",
    "deprecated",
    "description",
    "enum",
    "examples",
    "readOnly",
    "title",
    "writeOnly",
})
_SUBSCHEMA_MAP_KEYS = frozenset({
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
})


def shipped_builtin_tool_schemas() -> list[dict[str, Any]]:
    """Return every shipped built-in in deterministic canonical function shape.

    Do not replace the private-entry projection with ``registry.schemas()``:
    that public runtime surface intentionally loads extensions and refreshes
    MCP discovery.  The copy keeps canary callers from mutating the registry's
    module-owned schema dictionaries.
    """
    from ouroboros.tools.registry import ToolRegistry

    repo_dir = pathlib.Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="ouroboros-provider-contract-") as tmp:
        registry = ToolRegistry(repo_dir=repo_dir, drive_root=pathlib.Path(tmp))
        schemas = [
            registry._schema_for_entry(registry._entries[name])
            for name in sorted(registry._entries)
        ]
    return copy.deepcopy(schemas)


def _assert_no_blank_enum(node: Any, *, tool_name: str, path: str) -> None:
    if isinstance(node, Mapping):
        enum = node.get("enum")
        if isinstance(enum, list):
            blank = [value for value in enum if isinstance(value, str) and not value.strip()]
            if blank:
                raise AssertionError(
                    f"{tool_name}: empty enum value at {path}: {enum!r}"
                )
        for key, value in node.items():
            _assert_no_blank_enum(
                value,
                tool_name=tool_name,
                path=f"{path}.{key}",
            )
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for index, value in enumerate(node):
            _assert_no_blank_enum(
                value,
                tool_name=tool_name,
                path=f"{path}[{index}]",
            )


def _assert_no_multitype_nonnull(node: Any, *, tool_name: str, path: str) -> None:
    if isinstance(node, Mapping):
        raw_type = node.get("type")
        if isinstance(raw_type, Sequence) and not isinstance(raw_type, (str, bytes, bytearray)):
            non_null = [t for t in raw_type if t != "null"]
            if len(non_null) >= 2:
                raise AssertionError(
                    f"{tool_name}: non-portable multi-type list at {path}: {raw_type!r}"
                )
        for key, value in node.items():
            key_str = str(key)
            if key_str in _LITERAL_SCHEMA_KEYS:
                continue
            if key_str in _SUBSCHEMA_MAP_KEYS and isinstance(value, Mapping):
                for sub_k, sub_v in value.items():
                    _assert_no_multitype_nonnull(
                        sub_v,
                        tool_name=tool_name,
                        path=f"{path}.{key_str}.{sub_k}",
                    )
                continue
            if key_str == "dependencies" and isinstance(value, Mapping):
                for dep_k, dep_v in value.items():
                    if isinstance(dep_v, Mapping):
                        _assert_no_multitype_nonnull(
                            dep_v,
                            tool_name=tool_name,
                            path=f"{path}.dependencies.{dep_k}",
                        )
                continue
            _assert_no_multitype_nonnull(
                value,
                tool_name=tool_name,
                path=f"{path}.{key_str}",
            )
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for index, value in enumerate(node):
            _assert_no_multitype_nonnull(
                value,
                tool_name=tool_name,
                path=f"{path}[{index}]",
            )


def assert_portable_tool_schemas(schemas: Sequence[Mapping[str, Any]]) -> None:
    """Apply the free complete-registry JSON Schema portability contract."""
    for tool in schemas:
        function = tool.get("function")
        if not isinstance(function, Mapping):
            raise AssertionError("<unknown>: missing function tool object")
        tool_name = str(function.get("name") or "<unknown>")
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            raise AssertionError(f"{tool_name}: parameters must be a JSON Schema object")

        validator = validators.validator_for(parameters)
        try:
            validator.check_schema(parameters)
        except jsonschema_exceptions.SchemaError as exc:
            raise AssertionError(
                f"{tool_name}: invalid JSON Schema: {exc.message}"
            ) from exc

        for keyword in ROOT_SCHEMA_COMBINATORS:
            if keyword in parameters:
                raise AssertionError(
                    f"{tool_name}: root schema keyword {keyword!r} is not portable"
                )
        _assert_no_blank_enum(parameters, tool_name=tool_name, path=tool_name)
        _assert_no_multitype_nonnull(parameters, tool_name=tool_name, path=tool_name)
