"""Streaming FIX message parser for TCP connections."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from mkfix.fix.message import FixMessage, SOH


class FixStreamParser:
    """Reads complete FIX messages from an asyncio StreamReader.

    Handles partial reads by buffering until a complete message
    (ending with tag 10=xxx|) is received.
    """

    def __init__(self, reader: asyncio.StreamReader):
        self._reader = reader
        self._buffer = b""

    async def read_message(self) -> FixMessage:
        """Read one complete FIX message. Raises ConnectionError on disconnect."""
        soh = SOH.encode()

        while True:
            idx = self._find_message_end()
            if idx >= 0:
                raw = self._buffer[:idx]
                self._buffer = self._buffer[idx:]
                return self._parse(raw)

            chunk = await self._reader.read(65536)
            if not chunk:
                raise ConnectionError("FIX connection closed")
            self._buffer += chunk

    def _find_message_end(self) -> int:
        """Find the end of a complete FIX message in the buffer.

        A FIX message ends with 10=xxx<SOH>. Returns the index
        just past the trailing SOH, or -1 if not found.
        """
        soh = SOH.encode()[0]
        tag10 = b"10="

        search_start = 0
        while True:
            pos = self._buffer.find(tag10, search_start)
            if pos < 0:
                return -1

            if pos > 0 and self._buffer[pos - 1] != soh:
                search_start = pos + 3
                continue

            end = self._buffer.find(soh.to_bytes(1, "big"), pos + 3)
            if end < 0:
                return -1

            return end + 1

    @staticmethod
    def _parse(raw: bytes) -> FixMessage:
        text = raw.decode("latin-1")
        soh = SOH
        fields: dict[str, str] = {}
        pairs: list[tuple[str, str]] = []
        for pair in text.split(soh):
            if not pair:
                continue
            eq = pair.find("=")
            if eq > 0:
                fields[pair[:eq]] = pair[eq + 1:]
                pairs.append((pair[:eq], pair[eq + 1:]))
        return FixMessage(fields, pairs=pairs, raw=raw)

    async def __aiter__(self) -> AsyncIterator[FixMessage]:
        try:
            while True:
                yield await self.read_message()
        except ConnectionError:
            return
