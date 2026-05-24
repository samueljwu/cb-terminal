"""Stable JSON/dict serialization helpers for CB Terminal domain objects.

Inspired by OpenLIFU's explicit domain-object serialization boundary: domain
objects should round-trip through plain JSON-compatible payloads before they are
handed to storage, APIs, or audit artifacts.  This keeps dates, enums, nested
frozen dataclasses, and optional fields from leaking as ad-hoc Python objects
through the data flow.
"""

from __future__ import annotations

import json
import types
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

T = TypeVar("T", bound="DomainSerializable")


class DomainJSONEncoder(json.JSONEncoder):
    """JSON encoder for dataclasses, dates, enums, and nested containers."""

    def default(self, obj: Any) -> Any:
        converted = to_jsonable(obj)
        if converted is not obj:
            return converted
        return super().default(obj)


class DomainSerializable:
    """Mixin for canonical dict payloads used at API/storage boundaries."""

    def to_dict(self) -> dict[str, Any]:
        if not is_dataclass(self):
            raise TypeError("DomainSerializable can only serialize dataclass instances")
        return {field.name: to_jsonable(getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_dict(cls: type[T], payload: Mapping[str, Any]) -> T:
        if not isinstance(payload, Mapping):
            raise TypeError(f"Expected mapping for {cls.__name__}.from_dict, got {type(payload).__name__}")
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for field in fields(cls):
            if field.name in payload:
                kwargs[field.name] = coerce_from_jsonable(payload[field.name], hints.get(field.name, field.type))
        return cls(**kwargs)


def to_jsonable(value: Any) -> Any:
    """Return a JSON-compatible representation of a domain value."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        if isinstance(value, DomainSerializable):
            return value.to_dict()
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_jsonable(item) for item in value]
    return value


def dumps_json(value: Any, **kwargs: Any) -> str:
    """Serialize with the canonical domain encoder."""

    return json.dumps(value, cls=DomainJSONEncoder, **kwargs)


def dump_json(value: Any, fp: Any, **kwargs: Any) -> None:
    """Write JSON with the canonical domain encoder."""

    json.dump(value, fp, cls=DomainJSONEncoder, **kwargs)


def coerce_from_jsonable(value: Any, target_type: Any) -> Any:
    """Coerce a JSON-compatible value into a typed domain field."""

    if value is None:
        return None
    if target_type is Any:
        return value

    origin = get_origin(target_type)
    args = get_args(target_type)

    if origin in (types.UnionType, getattr(types, "UnionType", object)) or origin is _typing_union_origin():
        non_none_args = [arg for arg in args if arg is not type(None)]
        if not non_none_args:
            return None
        last_error: Exception | None = None
        for arg in non_none_args:
            try:
                return coerce_from_jsonable(value, arg)
            except (TypeError, ValueError) as exc:
                last_error = exc
        if last_error:
            raise last_error
        return value

    if origin in (list, Sequence):
        item_type = args[0] if args else Any
        return [coerce_from_jsonable(item, item_type) for item in value]

    if origin is dict or origin is Mapping:
        key_type = args[0] if args else Any
        value_type = args[1] if len(args) > 1 else Any
        return {
            coerce_from_jsonable(key, key_type): coerce_from_jsonable(item, value_type)
            for key, item in dict(value).items()
        }

    if origin is tuple:
        item_type = args[0] if args else Any
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(coerce_from_jsonable(item, item_type) for item in value)
        return tuple(coerce_from_jsonable(item, arg) for item, arg in zip(value, args))

    if isinstance(target_type, type) and issubclass(target_type, Enum):
        if isinstance(value, target_type):
            return value
        try:
            return target_type(value)
        except ValueError:
            return target_type[str(value)]

    if target_type is date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return date.fromisoformat(str(value))

    if target_type is datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    if isinstance(target_type, type) and is_dataclass(target_type):
        if isinstance(value, target_type):
            return value
        if issubclass(target_type, DomainSerializable):
            return target_type.from_dict(value)
        return target_type(**dict(value))

    return value


def _typing_union_origin() -> Any:
    try:
        from typing import Union

        return Union
    except Exception:  # pragma: no cover - defensive for unusual runtimes
        return object()
