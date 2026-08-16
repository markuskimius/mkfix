"""Tests for FIX session logon sequence number handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mkfix.fix.message import FixMessage, FixMessageFactory, SOH
from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.session import FixSession
from mkfix.fix.transport import FixInitiator, FixListener


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


def _make_logout_msg(seq_num=2):
    dictionary = FixDictionary("FIX.4.2")
    msg = FixMessage({"35": "5"})
    msg.sendprep(dictionary, "PEER", "SELF", seq_num)
    return msg


def _make_socket(reads):
    socket = MagicMock()
    socket.closed = False
    socket.read = AsyncMock(side_effect=reads)
    socket.write = AsyncMock(side_effect=lambda msg, seq: msg)
    socket.close = AsyncMock()
    return socket


class TestRemoteLogoutStatus:
    """After the counterparty logs out, status must reflect what the session
    is actually doing: an acceptor keeps listening, an initiator is down."""

    async def _cancel_heartbeat(self, session):
        session._heartbeat_task.cancel()
        await asyncio.gather(session._heartbeat_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_acceptor_returns_to_listening(self):
        engine = _make_engine()
        session = _make_session(engine)
        session._transport = FixListener(session)
        socket = _make_socket([_make_logon_msg(), _make_logout_msg()])

        await session.on_connect(socket, is_initiator=False)

        assert session._status == "LISTENING"
        assert session._transport is not None, "the listener stays registered"
        assert session._socket is None
        await self._cancel_heartbeat(session)

    @pytest.mark.asyncio
    async def test_initiator_goes_down_and_detaches(self):
        engine = _make_engine()
        session = _make_session(engine, host="127.0.0.1")
        session._transport = FixInitiator(session)
        socket = _make_socket([_make_logon_msg(), _make_logout_msg()])

        await session.on_connect(socket, is_initiator=True)

        assert session._status == "DOWN"
        assert session._transport is None, "a dead initiator must not block start()"
        await self._cancel_heartbeat(session)

    @pytest.mark.asyncio
    async def test_initiator_retry_limit_detaches(self, monkeypatch):
        engine = _make_engine()
        session = _make_session(engine, host="127.0.0.1")
        transport = FixInitiator(session)
        session._transport = transport

        async def refuse(*args, **kwargs):
            raise OSError("refused")

        monkeypatch.setattr("mkfix.fix.transport.CONNECTION_RETRY_LIMIT", 1)
        monkeypatch.setattr(asyncio, "open_connection", refuse)

        await transport._connect_loop()

        assert session._status == "ERROR"
        assert session._transport is None, "a dead initiator must not block start()"

    @pytest.mark.asyncio
    async def test_stop_while_active_still_reports_down(self):
        """A user-initiated stop tears the listener down, so DOWN stays right."""
        engine = _make_engine()
        session = _make_session(engine)
        session._transport = FixListener(session)
        socket = _make_socket([_make_logon_msg()])

        reads = 0
        async def read():
            nonlocal reads
            reads += 1
            if reads == 1:
                return _make_logon_msg()
            await asyncio.Event().wait()
        socket.read = read

        connect_task = asyncio.create_task(session.on_connect(socket, is_initiator=False))
        for _ in range(20):
            await asyncio.sleep(0)
            if session._status == "ACTIVE":
                break
        assert session._status == "ACTIVE"

        await session.stop()
        await asyncio.gather(connect_task, return_exceptions=True)

        assert session._status == "DOWN"
        assert session._transport is None
