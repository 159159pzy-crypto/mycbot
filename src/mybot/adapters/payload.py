"""Defensive, strictly typed accessors for untyped platform JSON payloads."""

from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import JsonValue

type RawMapping = Mapping[str, object]


def as_string_mapping(value: object) -> RawMapping | None:
    if isinstance(value, Mapping):
        source = cast(Mapping[object, object], value)
        return {str(key): child for key, child in source.items()}
    return None


def get_mapping(payload: RawMapping, key: str) -> RawMapping | None:
    return as_string_mapping(payload.get(key))


def get_sequence(payload: RawMapping, key: str) -> Sequence[object]:
    value = payload.get(key)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return cast(Sequence[object], value)
    return ()


def get_str(payload: RawMapping, key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    return None


def get_int(payload: RawMapping, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def get_id(payload: RawMapping, key: str) -> str | None:
    """Return an identifier that platforms send as either string or integer."""

    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def as_json_value(value: object) -> JsonValue:
    """Coerce an arbitrary payload into plain JSON, stringifying foreign objects."""

    mapping = as_string_mapping(value)
    if mapping is not None:
        return {key: as_json_value(child) for key, child in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [as_json_value(child) for child in cast(Sequence[object], value)]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
