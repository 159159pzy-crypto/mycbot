"""Accurately typed immutable JSON values with ordinary JSON wire serialization."""

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import JsonValue, PlainSerializer, RootModel, ValidateAs

type FrozenJsonScalar = str | int | float | bool | None
type FrozenJsonValue = (
    FrozenJsonScalar | FrozenJsonObject | tuple[FrozenJsonValue, ...]
)


class FrozenJsonObject(Mapping[str, FrozenJsonValue]):
    """A read-only mapping whose values are recursively immutable."""

    __slots__ = ("_data",)

    def __init__(self, values: Mapping[str, FrozenJsonValue]) -> None:
        self._data: Mapping[str, FrozenJsonValue] = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> FrozenJsonValue:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenJsonObject({dict(self._data)!r})"


def freeze_json(value: JsonValue) -> FrozenJsonValue:
    if isinstance(value, dict):
        return FrozenJsonObject({key: freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(child) for child in value)
    return value


def freeze_json_object(value: dict[str, JsonValue]) -> FrozenJsonObject:
    return FrozenJsonObject({key: freeze_json(child) for key, child in value.items()})


def thaw_json(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, FrozenJsonObject):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


def thaw_json_object(value: FrozenJsonObject) -> dict[str, JsonValue]:
    return {key: thaw_json(child) for key, child in value.items()}


class _JsonValueInput(RootModel[JsonValue]):
    pass


def freeze_validated_json(value: _JsonValueInput) -> FrozenJsonValue:
    return freeze_json(value.root)


def empty_frozen_json_object() -> FrozenJsonObject:
    return FrozenJsonObject({})


FrozenJsonValueField = Annotated[
    FrozenJsonValue,
    ValidateAs(_JsonValueInput, freeze_validated_json),
    PlainSerializer(thaw_json, return_type=JsonValue),
]
FrozenJsonObjectValue = Annotated[
    FrozenJsonObject,
    ValidateAs(dict[str, JsonValue], freeze_json_object),
    PlainSerializer(thaw_json_object, return_type=dict[str, JsonValue]),
]
