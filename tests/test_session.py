"""Tests for FIX session logon sequence number handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mkfix.fix.message import FixMessage, FixMessageFactory, SOH
from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.session import FixSession


def _make_engine():
    engine = MagicMock()
    engine.record_message = AsyncMock()
    engine.update_session_state = AsyncMock()
    engine.load_session_state = AsyncMock(return_value=None)
    engine.on_app_message = AsyncMock()
    return engine


def _make_session(engine, sender="SENDER", target="TARGET", **overrides):
    config = {
        "session_id": "TEST",
        "fix_version": "FIX.4.2",
        "sender_comp_id": sender,
        "target_comp_id": target,
        "host": "",
        "port": 9876,
        "heartbeat_interval": 30,
        "reset_on_logon": 0,
        **overrides,
    }
    return FixSession(engine, config)


def _make_logon_msg(seq_num=1, reset=False):
    """Create a Logon message as it would appear on the wire."""
    dictionary = FixDictionary("FIX.4.2")
    msg = FixMessage({"35": "A", "98": "0", "108": "30"})
    if reset:
        msg["141"] = "Y"
    msg.sendprep(dictionary, "PEER", "SELF", seq_num)
    return msg


class TestInitiatorLogonSeqNums:
    """Initiator must advance _rx_seq_num after receiving acceptor's Logon."""

    @pytest.mark.asyncio
    async def test_fresh_session(self):
        engine = _make_engine()
        session = _make_session(engine, host="127.0.0.1")

        logon_response = _make_logon_msg(seq_num=1)
        socket = MagicMock()
        socket.closed = False
        socket.read = AsyncMock(return_value=logon_response)
        socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
        socket.close = AsyncMock()
        session._socket = socket

        await session._do_initiator_logon()

        assert session._rx_seq_num == 2
        assert session._tx_seq_num == 2

    @pytest.mark.asyncio
    async def test_continued_session(self):
        """Initiator resumes with higher seq nums from previous session."""
        engine = _make_engine()
        session = _make_session(engine, host="127.0.0.1")
        session._tx_seq_num = 5
        session._rx_seq_num = 5

        logon_response = _make_logon_msg(seq_num=5)
        socket = MagicMock()
        socket.closed = False
        socket.read = AsyncMock(return_value=logon_response)
        socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
        socket.close = AsyncMock()
        session._socket = socket

        await session._do_initiator_logon()

        assert session._rx_seq_num == 6
        assert session._tx_seq_num == 6

    @pytest.mark.asyncio
    async def test_reset_on_logon(self):
        engine = _make_engine()
        session = _make_session(engine, host="127.0.0.1", reset_on_logon=1)

        logon_response = _make_logon_msg(seq_num=1)
        socket = MagicMock()
        socket.closed = False
        socket.read = AsyncMock(return_value=logon_response)
        socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
        socket.close = AsyncMock()
        session._socket = socket
        session._tx_seq_num = 10
        session._rx_seq_num = 10

        await session._do_initiator_logon()

        assert session._rx_seq_num == 2
        assert session._tx_seq_num == 2


class TestAcceptorLogonSeqNums:
    """Acceptor must advance _rx_seq_num after receiving initiator's Logon."""

    @pytest.mark.asyncio
    async def test_fresh_session(self):
        engine = _make_engine()
        session = _make_session(engine)

        logon_msg = _make_logon_msg(seq_num=1)
        socket = MagicMock()
        socket.closed = False
        socket.read = AsyncMock(return_value=logon_msg)
        socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
        socket.close = AsyncMock()
        session._socket = socket

        await session._do_acceptor_logon()

        assert session._rx_seq_num == 2
        assert session._tx_seq_num == 2

    @pytest.mark.asyncio
    async def test_continued_session(self):
        engine = _make_engine()
        session = _make_session(engine)
        session._tx_seq_num = 5
        session._rx_seq_num = 5

        logon_msg = _make_logon_msg(seq_num=5)
        socket = MagicMock()
        socket.closed = False
        socket.read = AsyncMock(return_value=logon_msg)
        socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
        socket.close = AsyncMock()
        session._socket = socket

        await session._do_acceptor_logon()

        assert session._rx_seq_num == 6
        assert session._tx_seq_num == 6

    @pytest.mark.asyncio
    async def test_reset_seq_num_flag(self):
        """When initiator sends 141=Y, acceptor resets then advances past logon."""
        engine = _make_engine()
        session = _make_session(engine)
        session._tx_seq_num = 10
        session._rx_seq_num = 10

        logon_msg = _make_logon_msg(seq_num=1, reset=True)
        socket = MagicMock()
        socket.closed = False
        socket.read = AsyncMock(return_value=logon_msg)
        socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
        socket.close = AsyncMock()
        session._socket = socket

        await session._do_acceptor_logon()

        assert session._rx_seq_num == 2
        assert session._tx_seq_num == 2

    @pytest.mark.asyncio
    async def test_acceptor_rx_higher_than_logon(self):
        """If acceptor's rx is higher than logon seq, don't regress."""
        engine = _make_engine()
        session = _make_session(engine)
        session._tx_seq_num = 10
        session._rx_seq_num = 10

        logon_msg = _make_logon_msg(seq_num=5)
        socket = MagicMock()
        socket.closed = False
        socket.read = AsyncMock(return_value=logon_msg)
        socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
        socket.close = AsyncMock()
        session._socket = socket

        await session._do_acceptor_logon()

        assert session._rx_seq_num == 10
        assert session._tx_seq_num == 11
