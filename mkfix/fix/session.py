"""FIX session state machine: logon, heartbeat, logout, message routing."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessage, FixMessageFactory, _fix_timestamp, parse_fix
from mkfix.fix.transport import FixSocket, FixInitiator, FixListener

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine

# Callback type for when application messages arrive
MessageCallback = Callable[["FixSession", str, FixMessage], Awaitable[None]]

# Session-level message types: never retransmitted, a ResendRequest covering
# them is answered with a SequenceReset-GapFill instead.
ADMIN_MSG_TYPES = frozenset({"0", "1", "2", "3", "4", "5", "A"})

# EndSeqNo(16) meaning "everything after BeginSeqNo": 0, or 999999 on the
# FIX 4.0/4.1 wire.
OPEN_ENDED_END_SEQ = frozenset({0, 999999})


class FixSession:
    """Manages a single FIX session's lifecycle and protocol."""

    def __init__(
        self,
        engine: FixEngine,
        config: dict[str, Any],
    ):
        self.engine = engine
        self.config = config
        dict_name = config.get("dictionary") or config.get("fix_version", "FIX.4.2")
        self.dictionary = FixDictionary(dict_name)
        self.factory = FixMessageFactory(
            self.dictionary,
            sender=config["sender_comp_id"],
            target=config["target_comp_id"],
            timestamp_precision=config.get("timestamp_precision") or None,
        )
        self.session_id: str = config["session_id"]

        self._socket: FixSocket | None = None
        self._transport: FixInitiator | FixListener | None = None
        self._read_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._tx_seq_num: int = 1
        self._rx_seq_num: int = 1
        # fix_messages id at the last sequence reset: a resend replays only
        # rows after it, so a number reused across resets can't be confused
        self._seq_epoch: int = 0
        self._status: str = "DOWN"
        self._last_rx_time: float = 0
        self._test_req_pending: str | None = None
        self._stopping = False

    @property
    def is_active(self) -> bool:
        return self._status == "ACTIVE"

    @property
    def status(self) -> str:
        return self._status

    async def start(self) -> None:
        """Start the session (initiator or listener based on config)."""
        if self._socket or self._transport:
            return

        self._stopping = False

        # Load persisted sequence numbers
        state = await self.engine.load_session_state(self.session_id)
        if state:
            self._tx_seq_num = state.get("tx_seq_num", 1)
            self._rx_seq_num = state.get("rx_seq_num", 1)
            self._seq_epoch = state.get("seq_epoch") or 0

        if self.config.get("host"):
            self._transport = FixInitiator(self)
        else:
            self._transport = FixListener(self)

        await self._transport.start()

    async def stop(self) -> None:
        """Stop the session gracefully."""
        self._stopping = True

        if self._socket and not self._socket.closed and self._status == "ACTIVE":
            try:
                logout = self.factory.logout()
                await self._send(logout)
                await self.set_status("LOGOUT_SENT")
            except (ConnectionError, OSError):
                pass

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = None

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        self._read_task = None

        if self._socket:
            await self._socket.close()
            self._socket = None

        if self._transport:
            await self._transport.stop()
            self._transport = None

        await self.set_status("DOWN")

    async def on_connect(self, socket: FixSocket, is_initiator: bool) -> None:
        """Called by transport when TCP connection is established."""
        self._socket = socket

        try:
            if is_initiator:
                await self._do_initiator_logon()
            else:
                await self._do_acceptor_logon()

            await self.set_status("ACTIVE")
            self._read_task = asyncio.create_task(self._read_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._read_task
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            await self.set_status("ERROR", str(e))
        except asyncio.CancelledError:
            pass
        finally:
            if self._socket:
                await self._socket.close()
                self._socket = None
            if not self._stopping:
                # The connection is gone, but what remains differs by role: an
                # acceptor's listener stays registered and will logon again on
                # the next inbound connection, so it is back to LISTENING; an
                # initiator's connect task is exiting, so detach it and report
                # DOWN — start() can then run again.
                if isinstance(self._transport, FixListener):
                    await self.set_status("LISTENING")
                else:
                    self._transport = None
                    await self.set_status("DOWN")

    async def _do_initiator_logon(self) -> None:
        """Initiator: send logon, wait for response."""
        await self.set_status("LOGON_SENT")
        reset = bool(self.config.get("reset_on_logon"))
        if reset:
            await self._reset_seq_space()

        logon = self.factory.logon(
            heartbeat_interval=self.config.get("heartbeat_interval", 30),
            reset_seq_num=reset,
        )
        await self._send(logon)

        response = await asyncio.wait_for(self._socket.read(), timeout=10)
        await self._record("RX", response)

        if response["35"] != "A":
            raise ConnectionError(f"Expected Logon, got MsgType={response['35']}")

        rx_seq = response.get_int("34", 0)
        if rx_seq >= self._rx_seq_num:
            self._rx_seq_num = rx_seq + 1

    async def _do_acceptor_logon(self) -> None:
        """Acceptor: wait for logon, send response."""
        await self.set_status("LOGON_SENT")

        logon_msg = await asyncio.wait_for(self._socket.read(), timeout=10)
        await self._record("RX", logon_msg)

        if logon_msg["35"] != "A":
            raise ConnectionError(f"Expected Logon, got MsgType={logon_msg['35']}")

        if logon_msg.get("141") == "Y":
            await self._reset_seq_space()

        rx_seq = logon_msg.get_int("34", 0)
        if rx_seq >= self._rx_seq_num:
            self._rx_seq_num = rx_seq + 1

        response = self.factory.logon(
            heartbeat_interval=self.config.get("heartbeat_interval", 30),
        )
        await self._send(response)

    async def _read_loop(self) -> None:
        """Main read loop: process incoming messages."""
        while self._socket and not self._socket.closed:
            try:
                msg = await self._socket.read()
            except ConnectionError:
                return

            await self._record("RX", msg)
            self._last_rx_time = asyncio.get_event_loop().time()
            self._test_req_pending = None

            msg_type = msg["35"]
            rx_seq = msg.get_int("34", 0)
            is_poss_dup = msg.get("43") == "Y"

            if rx_seq > 0 and not is_poss_dup:
                if rx_seq > self._rx_seq_num:
                    resend = self.factory.resend_request(self._rx_seq_num, rx_seq - 1)
                    await self._send(resend)
                elif rx_seq < self._rx_seq_num:
                    logout = self.factory.logout(
                        text=f"MsgSeqNum too low, expecting {self._rx_seq_num} but received {rx_seq}",
                    )
                    await self._send(logout)
                    return

            if rx_seq >= self._rx_seq_num:
                self._rx_seq_num = rx_seq + 1

            if msg_type == "0":
                pass
            elif msg_type == "1":
                hb = self.factory.heartbeat(test_req_id=msg.get("112"))
                await self._send(hb)
            elif msg_type == "5":
                if self._status == "ACTIVE":
                    response = self.factory.logout()
                    await self._send(response)
                return
            elif msg_type == "2":
                await self._answer_resend_request(msg)
            elif msg_type == "4":
                new_seq = msg.get_int("36", 0)
                if new_seq > 0:
                    self._rx_seq_num = new_seq
            elif msg_type == "3":
                pass
            else:
                if not is_poss_dup:
                    await self.engine.on_app_message(self, msg_type, msg)

            await self._persist_seq_nums()

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats and monitor for inbound message timeout."""
        interval = self.config.get("heartbeat_interval", 30)

        while self._socket and not self._socket.closed:
            await asyncio.sleep(interval)
            if self._socket and self._socket.closed:
                return

            now = asyncio.get_event_loop().time()
            if self._last_rx_time > 0 and (now - self._last_rx_time) > interval * 1.5:
                if self._test_req_pending:
                    await self.set_status("ERROR", "Heartbeat timeout — no response to TestRequest")
                    if self._socket:
                        await self._socket.close()
                    return
                test_id = f"MKFIX-{int(now)}"
                self._test_req_pending = test_id
                tr = self.factory.test_request(test_id)
                try:
                    await self._send(tr)
                except (ConnectionError, OSError):
                    return
            else:
                hb = self.factory.heartbeat()
                try:
                    await self._send(hb)
                except (ConnectionError, OSError):
                    return

    async def _answer_resend_request(self, request: FixMessage) -> None:
        """Replay the requested range from the recorded TX messages: each
        application message goes out again as a PossDup with its original
        MsgSeqNum, while runs of admin messages (and numbers with no record)
        collapse into one SequenceReset-GapFill whose MsgSeqNum is the first
        number of the run and NewSeqNo the number after it. Nothing here
        spends a new sequence number."""
        begin = request.get_int("7", 0)
        end = request.get_int("16", 0)
        last_sent = self._tx_seq_num - 1
        if end in OPEN_ENDED_END_SEQ or end > last_sent:
            end = last_sent
        if begin < 1 or begin > last_sent or end < begin:
            reject = self.factory.reject(
                request.get_int("34", 0),
                text=f"ResendRequest range {begin}-{request.get('16', '')} outside sent range 1-{last_sent}",
            )
            await self._send(reject)
            return

        rows = await self.engine.sent_messages(self.session_id, begin, end, self._seq_epoch)
        by_seq = {int(r["seq_num"]): r for r in rows}

        gap_start: int | None = None
        for seq in range(begin, end + 1):
            row = by_seq.get(seq)
            if row is None or row["msg_type"] in ADMIN_MSG_TYPES:
                if gap_start is None:
                    gap_start = seq
                continue
            if gap_start is not None:
                await self._send_gap_fill(gap_start, seq)
                gap_start = None
            original = parse_fix(row["raw_message"])
            copy = original.retransmit_copy(self.dictionary, self.factory._now())
            await self._retransmit(copy)
        if gap_start is not None:
            await self._send_gap_fill(gap_start, end + 1 if end < last_sent else self._tx_seq_num)

    async def _send_gap_fill(self, seq_num: int, new_seq: int) -> None:
        gap_fill = self.factory.sequence_reset(new_seq=new_seq, gap_fill=True)
        if self.dictionary.defines("122"):
            gap_fill["122"] = self.factory._now()
        sent = await self._socket.write(gap_fill, seq_num)
        await self._record("TX", sent)

    async def _retransmit(self, msg: FixMessage) -> None:
        """Write an already-prepared PossDup copy and record it, leaving the
        outbound sequence number where it is."""
        sent = await self._socket.write_prepared(msg)
        await self._record("TX", sent)

    async def _reset_seq_space(self, tx: int = 1, rx: int = 1) -> None:
        self._tx_seq_num = tx
        self._rx_seq_num = rx
        self._seq_epoch = await self.engine.last_message_id()

    async def send_message(self, msg: FixMessage) -> FixMessage:
        """Send an application message. Called by the engine for outbound orders etc."""
        if not self._socket or self._socket.closed:
            raise ConnectionError(f"Session {self.session_id} is not connected")
        return await self._send(msg)

    async def _send(self, msg: FixMessage) -> FixMessage:
        """Send a message, record it, and increment sequence number."""
        sent = await self._socket.write(msg, self._tx_seq_num)
        self._tx_seq_num += 1
        await self._record("TX", sent)
        await self._persist_seq_nums()
        return sent

    async def _record(self, direction: str, msg: FixMessage) -> None:
        """Record a message to the database via the engine."""
        await self.engine.record_message(self.session_id, direction, msg)

    async def _persist_seq_nums(self) -> None:
        """Persist current sequence numbers to the database."""
        await self.engine.update_session_state(self.session_id, {
            "tx_seq_num": self._tx_seq_num,
            "rx_seq_num": self._rx_seq_num,
            "seq_epoch": self._seq_epoch,
            "last_tx_time": _fix_timestamp(),
            "last_rx_time": _fix_timestamp(),
        })

    async def set_status(self, status: str, error_text: str = "") -> None:
        """Update session status in the database."""
        self._status = status
        update: dict[str, Any] = {"status": status}
        if error_text:
            update["error_text"] = error_text
        if status == "ACTIVE":
            update["session_start"] = _fix_timestamp()
            update["error_text"] = ""
        await self.engine.update_session_state(self.session_id, update)

    def detach_transport(self, transport: FixInitiator | FixListener) -> None:
        """Called by a transport whose task is exiting on its own; a detached
        session can be started again."""
        if self._transport is transport:
            self._transport = None

    async def reset_sequence_numbers(self, tx: int = 1, rx: int = 1) -> None:
        """Manually reset sequence numbers."""
        await self._reset_seq_space(tx, rx)
        await self._persist_seq_nums()
