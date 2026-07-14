"""Recursively immutable JSON values with an unchanged JSON wire representation."""

from collections.abc import Iterator, Mapping
from typing import Annotated, cast

from pydantic import AfterValidator, JsonValue, PlainSerializer


class FrozenJsonObject(Mapping[str, JsonValue]):
    """A read-only mapping whose children are recursively frozen."""

    __slots__ = ("_data",)

    def __init__(self, values: Mapping[str, JsonValue]) -> None:
        self._data = dict(values)

    def __getitem__(self, key: str) -> JsonValue:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenJsonObject({self._data!r})"


def freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        frozen = FrozenJsonObject({key: freeze_json(child) for key, child in value.items()})
        return cast(JsonValue, frozen)
    if isinstance(value, list):
        return cast(JsonValue, tuple(freeze_json(child) for child in value))
    return value


def freeze_json_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    frozen = freeze_json(value)
    return cast(dict[str, JsonValue], frozen)


def thaw_json(value: JsonValue) -> JsonValue:
    if isinstance(value, FrozenJsonObject):
        return {key: thaw_json(child) for key, child in value.items()}
    raw_value: object = value
    if isinstance(raw_value, tuple):
        children = cast(tuple[object, ...], raw_value)
        return cast(JsonValue, [thaw_json(cast(JsonValue, child)) for child in children])
    return value


def thaw_json_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    thawed = thaw_json(cast(JsonValue, value))
    return cast(dict[str, JsonValue], thawed)


FrozenJsonValue = Annotated[
    JsonValue,
    AfterValidator(freeze_json),
    PlainSerializer(thaw_json, return_type=JsonValue),
]
FrozenJsonObjectValue = Annotated[
    dict[str, JsonValue],
    AfterValidator(freeze_json_object),
    PlainSerializer(thaw_json_object, return_type=dict[str, JsonValue]),
]
