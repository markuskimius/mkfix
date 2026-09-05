"""End-to-end ResendRequest over a real TCP connection: a raw counterparty
logs on to an mkfix acceptor, sends an order, mkfix accepts it, and the
counterparty then asks for everything again. What comes back must be what
the FIX session layer expects — a GapFill stamped with the first missing
number for the admin run, the ExecutionReport as a PossDup under its
original number with its stored bytes intact, and no sequence number spent."""

import asyncio
import socket

import pytest
import pytest_asyncio

from mkio.change_bus import ChangeBus
from mkio.database import Database
from mkio.writer import WriteBatcher

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.engine import FixEngine
from mkfix.fix.message import FixMessage, SOH, parse_fix
from mkfix.fix.parser import FixStreamParser
from mkfix.fix.session import FixSession
from tests.test_engine import TABLES, _fetch_all

DICT = FixDictionary("FIX.4.2")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Peer:
    """A bare FIX counterparty: writes sendprep'd messages, reads with the
    stream parser."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.parser = FixStreamParser(reader)
        self.seq = 1

    async def send(self, fields: dict[str, str]) -> FixMessage:
        msg = FixMessage(fields)
        msg.sendprep(DICT, "PEER", "MKFIX", self.seq)
        self.seq += 1
        self.writer.write(msg.serialize())
        await self.writer.drain()
        return msg

    async def read(self, timeout: float = 5) -> FixMessage:
        return await asyncio.wait_for(self.parser.read_message(), timeout)

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


@pytest_asyncio.fixture
async def acceptor():
    db = Database(path=":memory:", tables=TABLES, config={})
    await db.start()
    writer = WriteBatcher(db, ChangeBus())
    await writer.start()
    engine = FixEngine(db=db, writer=writer)
    engine._compile_ops()
    await engine._ensure_indexes()
    session = FixSession(engine, {
        "session_id": "ACC", "fix_version": "FIX.4.2", "sender_comp_id": "MKFIX",
        "target_comp_id": "PEER", "host": "", "port": _free_port(),
        "heartbeat_interval": 30, "reset_on_logon": 0,
    })
    engine.sessions["ACC"] = session
    await session.start()
    reader, w = await asyncio.open_connection("127.0.0.1", session.config["port"])
    peer = Peer(reader, w)
    try:
        yield db, engine, session, peer
    finally:
        await peer.close()
        await session.stop()
        await writer.stop()
        await db.stop()


async def _wait_for(db, sql, timeout=5):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        rows = await _fetch_all(db, sql)
        if rows:
            return rows
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {sql}")


@pytest.mark.asyncio
async def test_resend_request_replays_recorded_messages(acceptor):
    db, engine, session, peer = acceptor

    await peer.send({"35": "A", "98": "0", "108": "30"})
    logon = await peer.read()
    assert logon["35"] == "A" and logon["34"] == "1"

    await peer.send({"35": "D", "11": "C1", "21": "1", "55": "AAPL", "54": "1", "38": "100",
                     "40": "2", "44": "1.5", "59": "0", "58": "note|with|pipes",
                     "60": "20260904-10:00:00.000"})
    await _wait_for(db, "SELECT * FROM fix_orders WHERE cl_ord_id = 'C1'")
    stored = await _fetch_all(db, "SELECT raw_message FROM fix_messages WHERE msg_type = 'D'")
    assert SOH in stored[0]["raw_message"]
    assert parse_fix(stored[0]["raw_message"])["58"] == "note|with|pipes"

    await engine.accept_order("ACC", "C1", extra_tags="58=accepted")
    er = await peer.read()
    assert er["35"] == "8" and er["34"] == "2" and er["58"] == "accepted"
    assert session._tx_seq_num == 3

    await peer.send({"35": "2", "7": "1", "16": "0"})
    gap_fill = await peer.read()
    assert (gap_fill["35"], gap_fill["34"], gap_fill["36"], gap_fill["123"], gap_fill["43"]) == \
        ("4", "1", "2", "Y", "Y")
    assert gap_fill["122"]
    dup = await peer.read()
    assert dup["35"] == "8" and dup["34"] == "2" and dup["43"] == "Y"
    assert dup["122"] == er["52"]
    assert dup["58"] == "accepted", "retransmitted from the stored wire bytes"
    assert dup["11"] == er["11"] and dup["17"] == er["17"]
    assert dup.raw == dup.serialize(), "BodyLength and CheckSum are consistent"

    await peer.send({"35": "1", "112": "T1"})
    hb = await peer.read()
    assert hb["35"] == "0" and hb["112"] == "T1"
    assert hb["34"] == "3", "the replay spent no sequence numbers"
    assert session._tx_seq_num == 4

    await _wait_for(db, "SELECT 1 FROM fix_messages WHERE direction = 'TX' AND msg_type = '0'")
    tx = await _fetch_all(db, "SELECT seq_num, msg_type, raw_message FROM fix_messages "
                              "WHERE direction = 'TX' ORDER BY id")
    assert [(r["seq_num"], r["msg_type"]) for r in tx] == [
        (1, "A"), (2, "8"), (1, "4"), (2, "8"), (3, "0")]
    assert parse_fix(tx[3]["raw_message"])["43"] == "Y", "the PossDup copy is recorded as sent"
