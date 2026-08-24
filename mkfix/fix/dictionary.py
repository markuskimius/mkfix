"""FIX data dictionary: tag names, enum values, message types."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).parent / "dictionary_data"

STANDARD_VERSIONS = (
    "FIX.4.0",
    "FIX.4.1",
    "FIX.4.2",
    "FIX.4.3",
    "FIX.4.4",
    "FIX.5.0",
    "FIX.5.0SP1",
    "FIX.5.0SP2",
)


@lru_cache(maxsize=None)
def _load_data(version: str) -> dict[str, Any]:
    filename = version.replace(".", "") + ".json"
    path = _DATA_DIR / filename
    if not path.exists():
        return {"fields": {}, "enums": {}, "messages": {}, "header": [], "trailer": []}
    with open(path) as f:
        return json.load(f)


# User-defined dictionaries, registered by name at engine startup and on
# create/update. FixDictionary checks here before the shipped standard files,
# so a custom name resolves anywhere a version string is accepted.
_custom: dict[str, dict[str, Any]] = {}
_custom_meta: dict[str, dict[str, Any]] = {}


def merge_dictionary(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Resolve a delta over a base document; the delta wins.

    fields/messages/groups merge per tag (a null entry removes it), enums merge
    per tag per code (union, null removes), header/trailer/begin_string replace
    wholesale when present.
    """
    data: dict[str, Any] = {
        "version": delta.get("version", base.get("version", "")),
        "begin_string": delta.get("begin_string", base.get("begin_string", "")),
        "header": list(delta.get("header", base.get("header", []))),
        "trailer": list(delta.get("trailer", base.get("trailer", []))),
    }
    for key in ("fields", "messages", "groups"):
        merged = dict(base.get(key, {}))
        for tag, entry in delta.get(key, {}).items():
            if entry is None:
                merged.pop(tag, None)
            else:
                merged[tag] = entry
        data[key] = merged
    enums = {tag: dict(values) for tag, values in base.get("enums", {}).items()}
    for tag, values in delta.get("enums", {}).items():
        if values is None:
            enums.pop(tag, None)
            continue
        current = enums.setdefault(tag, {})
        for code, name in values.items():
            if name is None:
                current.pop(code, None)
            else:
                current[code] = name
        if not current:
            enums.pop(tag, None)
    data["enums"] = enums
    return data


def register_custom(name: str, base_version: str | None, doc: dict[str, Any]) -> None:
    """Register (or replace) a custom dictionary.

    With base_version the doc is a delta over that standard; without it the doc
    stands alone. Sessions pick the result up on their next (re)build.
    """
    base = _load_data(base_version) if base_version else {}
    data = merge_dictionary(base, doc)
    if not data.get("begin_string"):
        data["begin_string"] = "FIX.4.2"
    data["version"] = name
    _custom[name] = data
    _custom_meta[name] = {"base_version": base_version or "", "doc": doc}


def unregister_custom(name: str) -> None:
    _custom.pop(name, None)
    _custom_meta.pop(name, None)


def custom_names() -> list[str]:
    return sorted(_custom)


def custom_meta(name: str) -> dict[str, Any] | None:
    return _custom_meta.get(name)


class FixDictionary:
    def __init__(self, version: str = "FIX.4.2"):
        self.version = version
        self._data = _custom.get(version) or _load_data(version)
        self.fields: dict[str, dict[str, str]] = self._data.get("fields", {})
        self.enums: dict[str, dict[str, str]] = self._data.get("enums", {})
        self.messages: dict[str, dict[str, str]] = self._data.get("messages", {})
        self.header_tags: list[str] = self._data.get("header", [])
        self.trailer_tags: list[str] = self._data.get("trailer", [])
        self.groups: dict[str, dict[str, Any]] = self._data.get("groups", {})
        self._begin_string: str = self._data.get("begin_string", version)
        self._special_tags = {"8", "9", "10"}
        self._header_set = set(self.header_tags)
        self._trailer_set = set(self.trailer_tags)

    def tag_name(self, tag: str) -> str:
        info = self.fields.get(str(tag))
        return info["name"] if info else str(tag)

    def field_type(self, tag: str) -> str:
        info = self.fields.get(str(tag))
        return info.get("type", "STRING") if info else "STRING"

    def enum_name(self, tag: str, value: str) -> str:
        tag_enums = self.enums.get(str(tag))
        if tag_enums:
            return tag_enums.get(str(value), str(value))
        return str(value)

    def enum_code(self, tag: str, name: str) -> str:
        tag_enums = self.enums.get(str(tag))
        if tag_enums:
            for code, enum_name in tag_enums.items():
                if enum_name == name:
                    return code
        return str(name)

    def msg_type_name(self, msg_type: str) -> str:
        info = self.messages.get(msg_type)
        return info["name"] if info else msg_type

    def msg_category(self, msg_type: str) -> str:
        info = self.messages.get(msg_type)
        return info.get("category", "app").upper() if info else "APP"

    def group(self, counter_tag: str) -> dict[str, Any] | None:
        return self.groups.get(str(counter_tag))

    def is_special(self, tag: str) -> bool:
        return str(tag) in self._special_tags

    def is_header(self, tag: str) -> bool:
        return str(tag) in self._header_set

    def is_trailer(self, tag: str) -> bool:
        return str(tag) in self._trailer_set

    def begin_string(self) -> str:
        return self._begin_string
