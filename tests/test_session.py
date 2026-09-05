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
    engine.last_message_id = AsyncMock(return_value=0)
    engine.sent_messages = AsyncMock(return_value=[])
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
        """A user-initiated stop tears the listener down, so DOWN stays right
        — even against a peer that never confirms the Logout."""
        engine = _make_engine()
        session = _make_session(engine, logout_test_request=0, logout_timeout=0.05)
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


def _wire(msg_type, seq_num, **fields):
    """A recorded TX row as the engine stores it: exact wire bytes."""
    dictionary = FixDictionary("FIX.4.2")
    msg = FixMessage({"35": msg_type, **fields})
    msg.sendprep(dictionary, "SENDER", "TARGET", seq_num)
    return {"seq_num": seq_num, "msg_type": msg_type, "raw_message": msg.to_wire_string()}


def _resend_socket():
    """Captures both write paths: (seq_num, msg) for sendprep'd messages,
    (None, msg) for prepared retransmissions."""
    socket = MagicMock()
    socket.closed = False
    socket.close = AsyncMock()
    socket.sent = []

    async def write(msg, seq):
        msg.sendprep(FixDictionary("FIX.4.2"), "SENDER", "TARGET", seq)
        msg.raw = msg.serialize()
        socket.sent.append((seq, msg))
        return msg

    async def write_prepared(msg):
        socket.sent.append((None, msg))
        return msg

    socket.write = AsyncMock(side_effect=write)
    socket.write_prepared = AsyncMock(side_effect=write_prepared)
    return socket


def _resend_request(begin, end, seq_num=1):
    msg = FixMessage({"35": "2", "7": str(begin), "16": str(end)})
    msg.sendprep(FixDictionary("FIX.4.2"), "PEER", "SELF", seq_num)
    return msg


class TestResendRequest:
    """A ResendRequest is answered from the recorded TX messages: app
    messages go out again as PossDups under their original MsgSeqNum, admin
    runs and missing numbers collapse into GapFills stamped with the first
    number the counterparty is missing, and the outbound counter never moves."""

    def _session(self, rows, tx_seq_num):
        engine = _make_engine()
        engine.sent_messages = AsyncMock(return_value=rows)
        session = _make_session(engine)
        session._tx_seq_num = tx_seq_num
        session._seq_epoch = 7
        session._socket = _resend_socket()
        return session

    @pytest.mark.asyncio
    async def test_app_message_resent_as_possdup(self):
        original = _wire("D", 5, **{"11": "C1", "55": "AAPL", "54": "1", "38": "100", "40": "2",
                                    "58": "price|qty"})
        session = self._session([original], tx_seq_num=6)

        await session._answer_resend_request(_resend_request(5, 5))

        (seq, msg), = session._socket.sent
        assert seq is None, "a retransmission bypasses sendprep and the counter"
        assert msg["34"] == "5"
        assert msg["43"] == "Y"
        assert msg["122"] == parse_fix_field(original["raw_message"], "52")
        assert msg["58"] == "price|qty", "stored wire bytes keep a literal pipe"
        assert msg["9"] == str(len(msg.serialize_without_checksum()) - len(b"8=FIX.4.2\x01") - len(f"9={msg['9']}\x01".encode()))
        assert session._tx_seq_num == 6
        session.engine.sent_messages.assert_awaited_once_with("TEST", 5, 5, 7)

    @pytest.mark.asyncio
    async def test_admin_run_collapses_to_one_gap_fill(self):
        rows = [_wire("A", 1, **{"98": "0", "108": "30"}), _wire("0", 2), _wire("1", 3, **{"112": "T"})]
        session = self._session(rows, tx_seq_num=4)

        await session._answer_resend_request(_resend_request(1, 3))

        (seq, msg), = session._socket.sent
        assert msg["35"] == "4" and msg["123"] == "Y" and msg["43"] == "Y"
        assert seq == 1, "the GapFill carries the first number the peer is missing"
        assert msg["36"] == "4", "NewSeqNo is the next number mkfix will send"
        assert msg["122"], "a GapFill is a PossDup and carries OrigSendingTime"
        assert session._tx_seq_num == 4

    @pytest.mark.asyncio
    async def test_mixed_range(self):
        rows = [_wire("A", 1, **{"98": "0", "108": "30"}), _wire("0", 2),
                _wire("D", 3, **{"11": "C1", "55": "AAPL", "54": "1", "38": "1"}),
                _wire("0", 4),
                _wire("8", 6, **{"37": "O1", "17": "E1", "150": "0", "39": "0"})]
        session = self._session(rows, tx_seq_num=9)

        await session._answer_resend_request(_resend_request(1, 0))

        sent = [(seq, m["35"], m["34"], m.get("36")) for seq, m in session._socket.sent]
        assert sent == [
            (1, "4", "1", "3"),        # Logon + Heartbeat
            (None, "D", "3", None),    # the order, as a PossDup
            (4, "4", "4", "6"),        # Heartbeat + the unrecorded 5
            (None, "8", "6", None),
            (7, "4", "7", "9"),        # 7 and 8 have no record; open-ended, so up to tx
        ]
        assert session._tx_seq_num == 9

    @pytest.mark.asyncio
    async def test_bounded_request_ends_at_end_plus_one(self):
        session = self._session([_wire("0", 3), _wire("0", 4)], tx_seq_num=10)

        await session._answer_resend_request(_resend_request(3, 4))

        (seq, msg), = session._socket.sent
        assert (seq, msg["36"]) == (3, "5")

    @pytest.mark.asyncio
    async def test_fix40_open_ended_marker(self):
        session = self._session([_wire("0", 2)], tx_seq_num=3)
        await session._answer_resend_request(_resend_request(2, 999999))
        (seq, msg), = session._socket.sent
        assert (seq, msg["36"]) == (2, "3")

    @pytest.mark.asyncio
    async def test_begin_past_sent_range_is_rejected(self):
        session = self._session([], tx_seq_num=4)
        request = _resend_request(4, 0, seq_num=9)

        await session._answer_resend_request(request)

        (seq, msg), = session._socket.sent
        assert msg["35"] == "3" and msg["45"] == "9"
        assert seq == 4 and session._tx_seq_num == 5, "a Reject is a normal send"
        session.engine.sent_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_read_loop_routes_resend_request(self):
        engine = _make_engine()
        session = _make_session(engine)
        session._status = "ACTIVE"
        session._tx_seq_num = 3
        session._rx_seq_num = 2
        socket = _resend_socket()
        socket.read = AsyncMock(side_effect=[_resend_request(1, 2, seq_num=2), _make_logout_msg(3)])
        session._socket = socket

        await session._read_loop()

        kinds = [m["35"] for _, m in socket.sent]
        assert kinds == ["4", "5"], "GapFill for the admin range, then the logout answer"
        assert socket.sent[0][0] == 1
        assert socket.sent[1][0] == 3, "the GapFill did not consume a sequence number"


class TestSequenceEpoch:
    """Every reset of the outbound sequence space records where it starts,
    so a resend never replays a row from before the reset."""

    @pytest.mark.asyncio
    async def test_manual_reset_marks_epoch(self):
        engine = _make_engine()
        engine.last_message_id = AsyncMock(return_value=42)
        session = _make_session(engine)

        await session.reset_sequence_numbers(5, 6)

        assert (session._tx_seq_num, session._rx_seq_num, session._seq_epoch) == (5, 6, 42)
        persisted = engine.update_session_state.await_args.args[1]
        assert persisted["seq_epoch"] == 42

    @pytest.mark.asyncio
    async def test_reset_on_logon_marks_epoch(self):
        engine = _make_engine()
        engine.last_message_id = AsyncMock(return_value=23)
        session = _make_session(engine, reset_on_logon=1)
        session._tx_seq_num = 10
        session._socket = _make_socket([_make_logon_msg(seq_num=1)])

        await session._do_initiator_logon()

        assert session._seq_epoch == 23
        assert session._tx_seq_num == 2, "the reset Logon went out as 1"

    @pytest.mark.asyncio
    async def test_inbound_reset_flag_marks_epoch(self):
        engine = _make_engine()
        engine.last_message_id = AsyncMock(return_value=17)
        session = _make_session(engine)
        session._socket = _make_socket([_make_logon_msg(seq_num=1, reset=True)])

        await session._do_acceptor_logon()

        assert session._seq_epoch == 17

    @pytest.mark.asyncio
    async def test_epoch_loaded_on_start(self):
        engine = _make_engine()
        engine.load_session_state = AsyncMock(
            return_value={"tx_seq_num": 3, "rx_seq_num": 4, "seq_epoch": 9})
        session = _make_session(engine)
        transport = MagicMock()
        transport.start = AsyncMock()
        session._transport = None
        import mkfix.fix.session as mod
        orig = mod.FixListener
        mod.FixListener = lambda s: transport
        try:
            await session.start()
        finally:
            mod.FixListener = orig
        assert session._seq_epoch == 9


def parse_fix_field(raw, tag):
    from mkfix.fix.message import parse_fix
    return parse_fix(raw)[tag]


class _Peer:
    """A counterparty behind a mock socket: every message the session writes
    is answered from a per-MsgType script, replies landing on the inbox the
    session reads from. None on the inbox is the peer hanging up."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[FixMessage] = []
        self.seq = 1
        self.socket = MagicMock()
        self.socket.closed = False
        self.socket.close = AsyncMock()
        self.socket.read = self._read
        self.socket.write = self._write
        self.inbox.put_nowait(self._msg({"35": "A", "98": "0", "108": "30"}))

    def _msg(self, fields):
        msg = FixMessage(fields)
        msg.sendprep(FixDictionary("FIX.4.2"), "PEER", "SELF", self.seq)
        self.seq += 1
        return msg

    async def _read(self):
        msg = await self.inbox.get()
        if msg is None:
            raise ConnectionError("peer closed")
        return msg

    async def _write(self, msg, seq):
        msg.sendprep(FixDictionary("FIX.4.2"), "SENDER", "TARGET", seq)
        self.sent.append(msg)
        for fields in self.replies.get(msg["35"], lambda m: [])(msg):
            self.inbox.put_nowait(None if fields is None else self._msg(fields))
        return msg

    @property
    def sent_types(self):
        return [m["35"] for m in self.sent]


async def _run_until_active(session, peer):
    task = asyncio.create_task(session.on_connect(peer.socket, is_initiator=False))
    for _ in range(50):
        await asyncio.sleep(0)
        if session._status == "ACTIVE":
            return task
    raise AssertionError(f"session never went ACTIVE: {session._status}")


def _confirming_peer():
    """Answers a TestRequest with the echoing Heartbeat and a Logout with the
    confirming Logout, as the spec asks of the logout acceptor."""
    return _Peer({
        "1": lambda m: [{"35": "0", "112": m["112"]}],
        "5": lambda m: [{"35": "5"}],
    })


class TestLocalLogoutHandshake:
    """A locally initiated stop follows the session protocol's termination:
    TestRequest, Logout, then wait for the confirming Logout."""

    @pytest.mark.asyncio
    async def test_test_request_then_logout_then_waits_for_confirmation(self):
        engine = _make_engine()
        session = _make_session(engine)
        session._transport = FixListener(session)
        peer = _confirming_peer()
        task = await _run_until_active(session, peer)

        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert peer.sent_types == ["A", "1", "5"]
        assert peer.sent[1].get("112", "").startswith("MKFIX-LOGOUT-")
        rx_types = [c.args[2]["35"] for c in engine.record_message.call_args_list if c.args[1] == "RX"]
        assert rx_types == ["A", "0", "5"], "the echo Heartbeat and the confirming Logout were read"
        assert session._status == "DOWN"
        assert session._transport is None
        assert session._socket is None

    @pytest.mark.asyncio
    async def test_confirming_logout_is_not_answered_and_seq_is_persisted(self):
        engine = _make_engine()
        session = _make_session(engine)
        session._transport = FixListener(session)
        peer = _confirming_peer()
        task = await _run_until_active(session, peer)

        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert peer.sent_types.count("5") == 1, "their confirming Logout gets no Logout back"
        persisted = [c.args[1] for c in engine.update_session_state.call_args_list if "rx_seq_num" in c.args[1]]
        assert persisted[-1]["rx_seq_num"] == 4, "Logon, Heartbeat, Logout consumed 1-3"

    @pytest.mark.asyncio
    async def test_resend_request_after_our_logout_is_still_answered(self):
        """After Logout the initiator sends nothing unsolicited, but a
        ResendRequest from the acceptor must still be honored."""
        engine = _make_engine()
        session = _make_session(engine, logout_test_request=0)
        session._transport = FixListener(session)
        peer = _Peer({
            "5": lambda m: [{"35": "2", "7": "1", "16": "0"}, {"35": "5"}],
        })
        task = await _run_until_active(session, peer)

        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert peer.sent_types == ["A", "5", "4"], "the GapFill answers the resend after our Logout"
        assert session._status == "DOWN"

    @pytest.mark.asyncio
    async def test_test_request_can_be_switched_off(self):
        engine = _make_engine()
        session = _make_session(engine, logout_test_request=0)
        session._transport = FixListener(session)
        peer = _confirming_peer()
        task = await _run_until_active(session, peer)

        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert peer.sent_types == ["A", "5"]
        assert session._status == "DOWN"

    @pytest.mark.asyncio
    async def test_unanswered_test_request_does_not_block_the_logout(self):
        engine = _make_engine()
        session = _make_session(engine, heartbeat_interval=0.05)
        session._transport = FixListener(session)
        peer = _Peer({"5": lambda m: [{"35": "5"}]})
        task = await _run_until_active(session, peer)

        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert peer.sent_types == ["A", "1", "5"]
        assert session._status == "DOWN"

    @pytest.mark.asyncio
    async def test_silent_peer_times_out(self):
        engine = _make_engine()
        session = _make_session(engine, logout_test_request=0, logout_timeout=0.05)
        session._transport = FixListener(session)
        peer = _Peer()
        task = await _run_until_active(session, peer)

        started = asyncio.get_event_loop().time()
        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert peer.sent_types == ["A", "5"]
        assert 0.05 <= asyncio.get_event_loop().time() - started < 1
        assert session._status == "DOWN"

    @pytest.mark.asyncio
    async def test_peer_dropping_instead_of_confirming_ends_the_wait(self):
        engine = _make_engine()
        session = _make_session(engine, logout_test_request=0, logout_timeout=30)
        session._transport = FixListener(session)
        peer = _Peer({"5": lambda m: [None]})
        task = await _run_until_active(session, peer)

        started = asyncio.get_event_loop().time()
        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert asyncio.get_event_loop().time() - started < 1
        assert session._status == "DOWN"

    @pytest.mark.asyncio
    async def test_remote_logout_persists_the_consumed_seq_num(self):
        """The counterparty's Logout takes a sequence number like any other
        message; the read loop's early return must not skip persisting it."""
        engine = _make_engine()
        session = _make_session(engine)
        session._transport = FixListener(session)
        socket = _make_socket([_make_logon_msg(), _make_logout_msg(seq_num=2)])

        await session.on_connect(socket, is_initiator=False)
        session._heartbeat_task.cancel()
        await asyncio.gather(session._heartbeat_task, return_exceptions=True)

        persisted = [c.args[1] for c in engine.update_session_state.call_args_list if "rx_seq_num" in c.args[1]]
        assert persisted[-1]["rx_seq_num"] == 3

    @pytest.mark.asyncio
    async def test_stop_after_peer_hung_up_sends_nothing(self):
        """The peer's EOF can end the read loop before on_connect's cleanup
        runs; a stop landing in that window must not spend a TestRequest
        and a heartbeat interval on a connection that is already gone."""
        engine = _make_engine()
        session = _make_session(engine, heartbeat_interval=30)
        session._transport = FixListener(session)
        peer = _Peer()
        task = await _run_until_active(session, peer)

        peer.inbox.put_nowait(None)
        for _ in range(20):
            await asyncio.sleep(0)
            if session._read_task.done():
                break
        assert session._read_task.done()
        assert session._status == "ACTIVE", "cleanup has not run yet"

        started = asyncio.get_event_loop().time()
        await session.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert peer.sent_types == ["A"]
        assert asyncio.get_event_loop().time() - started < 1
        assert session._status == "DOWN"

    @pytest.mark.asyncio
    async def test_heartbeat_loop_exits_when_socket_is_detached(self):
        """Cleanup sets _socket to None while the loop sleeps; waking up
        into a send on None used to raise instead of returning."""
        engine = _make_engine()
        session = _make_session(engine, heartbeat_interval=0.01)
        session._socket = _make_socket([])

        loop_task = asyncio.create_task(session._heartbeat_loop())
        await asyncio.sleep(0)
        session._socket = None
        await asyncio.wait_for(loop_task, timeout=1)

        assert loop_task.exception() is None
        assert engine.record_message.call_count == 0
