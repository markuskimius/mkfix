"""FIX data dictionary: tag names, enum values, message types."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).parent / "dictionary_data"


class FixDictionary:
    def __init__(self, version: str = "FIX.4.2"):
        self.version = version
        self._data = self._load(version)
        self.fields: dict[str, dict[str, str]] = self._data.get("fields", {})
        self.enums: dict[str, dict[str, str]] = self._data.get("enums", {})
        self.messages: dict[str, dict[str, str]] = self._data.get("messages", {})
        self.header_tags: list[str] = self._data.get("header", [])
        self.trailer_tags: list[str] = self._data.get("trailer", [])
        self._special_tags = {"8", "9", "10"}
        self._header_set = set(self.header_tags)
        self._trailer_set = set(self.trailer_tags)

    @staticmethod
    def _load(version: str) -> dict[str, Any]:
        filename = version.replace(".", "") + ".json"
        path = _DATA_DIR / filename
        if not path.exists():
            return {"fields": {}, "enums": {}, "messages": {}, "header": [], "trailer": []}
        with open(path) as f:
            return json.load(f)

    def tag_name(self, tag: str) -> str:
        info = self.fields.get(str(tag))
        return info["name"] if info else str(tag)

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

    def is_special(self, tag: str) -> bool:
        return str(tag) in self._special_tags

    def is_header(self, tag: str) -> bool:
        return str(tag) in self._header_set

    def is_trailer(self, tag: str) -> bool:
        return str(tag) in self._trailer_set

    def begin_string(self) -> str:
        return self.version
