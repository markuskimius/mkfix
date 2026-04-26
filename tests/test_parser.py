"""Tests for the streaming FIX TCP parser."""

import asyncio

import pytest

from mkfix.fix.message import SOH
from mkfix.fix.parser import FixStreamParser


def _make_fix_msg(**fields) -> bytes:
    """Build a raw FIX message with SOH delimiters."""
    parts = []
    for tag, val in fields.items():
        parts.append(f"{tag}={val}{SOH}")
    return "".join(parts).encode()


def _make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_parse_single_message():
    raw = _make_fix_msg(**{"8": "FIX.4.2", "35": "A", "10": "000"})
    reader = _make_reader(raw)
    parser = FixStreamParser(reader)
    msg = await parser.read_message()
    assert msg["8"] == "FIX.4.2"
    assert msg["35"] == "A"
    assert msg["10"] == "000"


@pytest.mark.asyncio
async def test_parse_two_messages():
    msg1 = _make_fix_msg(**{"8": "FIX.4.2", "35": "A", "10": "100"})
    msg2 = _make_fix_msg(**{"8": "FIX.4.2", "35": "0", "10": "200"})
    reader = _make_reader(msg1 + msg2)
    parser = FixStreamParser(reader)

    first = await parser.read_message()
    assert first["35"] == "A"

    second = await parser.read_message()
    assert second["35"] == "0"


@pytest.mark.asyncio
async def test_parse_disconnected():
    reader = _make_reader(b"")
    parser = FixStreamParser(reader)
    with pytest.raises(ConnectionError):
        await parser.read_message()


@pytest.mark.asyncio
async def test_parse_partial_then_complete():
    raw = _make_fix_msg(**{"8": "FIX.4.2", "35": "D", "55": "AAPL", "10": "123"})
    mid = len(raw) // 2
    reader = asyncio.StreamReader()
    parser = FixStreamParser(reader)

    async def feed():
        await asyncio.sleep(0.01)
        reader.feed_data(raw[:mid])
        await asyncio.sleep(0.01)
        reader.feed_data(raw[mid:])

    asyncio.get_event_loop().create_task(feed())
    msg = await parser.read_message()
    assert msg["35"] == "D"
    assert msg["55"] == "AAPL"


@pytest.mark.asyncio
async def test_aiter():
    msg1 = _make_fix_msg(**{"8": "FIX.4.2", "35": "A", "10": "100"})
    msg2 = _make_fix_msg(**{"8": "FIX.4.2", "35": "5", "10": "200"})
    reader = _make_reader(msg1 + msg2)
    parser = FixStreamParser(reader)

    messages = []
    async for msg in parser:
        messages.append(msg)

    assert len(messages) == 2
    assert messages[0]["35"] == "A"
    assert messages[1]["35"] == "5"
