"""A log replayed end to end: the engine loads and summarizes it, Start
picks a direction, and what the target session puts on the wire reaches
the other end of a linked pair — the client's orders land in Received
Orders on the acceptor, the venue's ExecutionReports drive Sent Orders and
Received Trades on the initiator — as the *session's* messages."""

import asyncio
import json

import pytest
import pytest_asyncio

from mkfix.fix import replay
from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessageFactory, parse_fix
from tests.test_engine import StubSession, _fetch_all, stack  # noqa: F401


class LinkedSession(StubSession):
    """What this end sends goes over sendprep (the wire) and into the other
    end's engine handler, the way a fast counterparty answers."""

    def __init__(self, engine, session_id, sender, target):
        super().__init__(session_id)
        self.dictionary = FixDictionary("FIX.4.4")
        self.factory = FixMessageFactory(self.dictionary, sender, target)
        self.engine, self.peer, self.wire = engine, None, []
        self.seq = 1

    async def send_message(self, msg):
        msg.sendprep(self.dictionary, self.factory.sender, self.factory.target, self.seq)
        self.seq += 1
        self.sent.append(msg)
        self.wire.append(msg.to_pipe_string())
        if self.peer is not None:
            await self.engine.on_app_message(self.peer, msg["35"], parse_fix(msg.to_pipe_string()))
        return msg


@pytest_asyncio.fixture
async def pair(stack):
    db, writer, engine = stack
    cli = LinkedSession(engine, "Client", "Client", "Server")
    mkt = LinkedSession(engine, "Server", "Server", "Client")
    cli.peer, mkt.peer = mkt, cli
    engine.sessions.update({"Client": cli, "Server": mkt})
    await db.write_conn.execute(
        "INSERT INTO fix_sessions (session_id, fix_version, sender_comp_id, target_comp_id, host, port) "
        "VALUES ('Client', 'FIX.4.4', 'Client', 'Server', '127.0.0.1', 9876), "
        "('Server', 'FIX.4.4', 'Server', 'Client', '', 9876)")
    await db.write_conn.commit()
    yield db, engine, cli, mkt
    for task in list(engine._replay_tasks.values()):
        await task.stop()


async def _settle(engine, job_id, timeout=5.0):
    """Wait for the job's task to end; its last progress write lands before it does."""
    task = engine._replay_tasks.get(job_id)
    if task and task._task:
        await asyncio.wait_for(task._task, timeout)


async def _job(db, job_id):
    (row,) = await _fetch_all(db, f"SELECT * FROM fix_replay_jobs WHERE id = {job_id}")
    return row


class TestLoad:
    @pytest.mark.asyncio
    async def test_load_summarizes_the_file_and_ticks_every_type(self, pair):
        db, engine, cli, mkt = pair
        got = await engine.load_replay(example="two-sided-day")
        job = await _job(db, got["job_id"])
        assert (job["status"], job["name"], job["file_path"]) == ("loaded", "A day between a client and a venue, both directions", "example:two-sided-day")
        assert job["total_messages"] == got["count"] == 28
        assert job["pairs"] == "PRODCLI→PRODVENUE 11, PRODVENUE→PRODCLI 17"
        assert job["msg_filter"] == "8,9,D,F,G,Q"
        assert (job["first_time"], job["last_time"]) == ("20260921-08:00:00.000", "20260921-16:30:00.000")
        assert job["default_direction"] == "", "two pairs and no session yet: Start must ask"
        assert json.loads(job["summary"])["names"]["D"] == "NewOrderSingle"

    @pytest.mark.asyncio
    async def test_a_one_directional_file_needs_no_choice(self, pair):
        db, engine, cli, mkt = pair
        got = await engine.load_replay(name="flow", example="order-flow")
        job = await _job(db, got["job_id"])
        assert (job["name"], job["default_direction"]) == ("flow", "")
        assert job["pairs"] == "(no CompIDs) 6"

    @pytest.mark.asyncio
    async def test_load_refuses_what_it_cannot_read(self, pair, tmp_path):
        db, engine, cli, mkt = pair
        with pytest.raises(ValueError, match="No such file"):
            await engine.load_replay(file_path=str(tmp_path / "missing.log"))
        empty = tmp_path / "admin-only.log"
        empty.write_text("8=FIX.4.4|35=A|49=A|56=B|98=0|108=30|\n", encoding="latin-1")
        with pytest.raises(ValueError, match="No FIX application messages"):
            await engine.load_replay(file_path=str(empty))
        with pytest.raises(ValueError, match="Give a file path"):
            await engine.load_replay()


class TestConfigure:
    @pytest.mark.asyncio
    async def test_configure_preselects_the_sessions_own_direction(self, pair, tmp_path):
        db, engine, cli, mkt = pair
        log = tmp_path / "ours.log"
        log.write_text("8=FIX.4.4|35=D|49=Client|56=Server|11=1|55=IBM|54=1|38=1|40=1|\n"
                       "8=FIX.4.4|35=8|49=Server|56=Client|11=1|37=O|17=E|150=0|39=0|55=IBM|54=1|38=1|151=1|14=0|6=0|\n",
                       encoding="latin-1")
        job_id = (await engine.load_replay(file_path=str(log)))["job_id"]
        await engine.configure_replay(job_id, target_session="Client", speed=2, msg_filter="D,8",
                                      time_from="09:00", time_to="17:00:00", max_gap=5)
        job = await _job(db, job_id)
        assert job["default_direction"] == "Client→Server"
        assert (job["speed"], job["max_gap"], job["time_from"], job["time_to"]) == (2.0, 5.0, "09:00", "17:00:00")
        await engine.configure_replay(job_id, target_session="Server")
        assert (await _job(db, job_id))["default_direction"] == "Server→Client"

    @pytest.mark.asyncio
    async def test_configure_refusals(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        with pytest.raises(ValueError, match="Unknown session"):
            await engine.configure_replay(job_id, target_session="Nobody")
        with pytest.raises(ValueError, match="no Z messages"):
            await engine.configure_replay(job_id, target_session="Client", msg_filter="D,Z")
        with pytest.raises(ValueError, match="HH:MM"):
            await engine.configure_replay(job_id, target_session="Client", time_from="soon")
        with pytest.raises(ValueError, match="negative"):
            await engine.configure_replay(job_id, target_session="Client", speed=-1)
        with pytest.raises(ValueError, match="Unknown replay job"):
            await engine.configure_replay(999)


class TestStart:
    @pytest.mark.asyncio
    async def test_start_needs_a_session_a_direction_and_something_to_send(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        with pytest.raises(ValueError, match="Configure the job with a session"):
            await engine.start_replay(job_id)
        await engine.configure_replay(job_id, target_session="Client")
        with pytest.raises(ValueError, match="Choose a direction"):
            await engine.start_replay(job_id)
        with pytest.raises(ValueError, match="holds no"):
            await engine.start_replay(job_id, "A→B")
        await engine.configure_replay(job_id, target_session="Client", msg_filter="8")
        with pytest.raises(ValueError, match="Nothing to replay"):
            await engine.start_replay(job_id, "PRODCLI→PRODVENUE")
        cli.is_active = False
        await engine.configure_replay(job_id, target_session="Client")
        with pytest.raises(ValueError, match="not active"):
            await engine.start_replay(job_id, "PRODCLI→PRODVENUE")

    @pytest.mark.asyncio
    async def test_the_clients_side_replays_as_the_initiator(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        await engine.configure_replay(job_id, target_session="Client", speed=0)
        got = await engine.start_replay(job_id, "PRODCLI→PRODVENUE")
        assert got == {"selected": 11, "direction": "PRODCLI→PRODVENUE"}
        await _settle(engine, job_id)

        job = await _job(db, job_id)
        assert (job["status"], job["sent_messages"], job["selected_messages"], job["direction"]) == \
            ("completed", 11, 11, "PRODCLI→PRODVENUE")

        # the wire carries the session's header, not the log's
        for line in cli.wire:
            assert "|49=Client|56=Server|" in line and "PROD" not in line.split("|11=")[0]
            assert "43=" not in line and "122=" not in line and "369=" not in line
        assert [w["34"] for w in cli.sent] == [str(n) for n in range(1, 12)], "one sequence number each"
        assert all(w["52"].startswith("2") and w["52"] != "20260921-08:00:00.000" for w in cli.sent)
        routed = next(w for w in cli.sent if w["11"] == "PRD-3")
        assert (routed["50"], routed["57"], routed["115"]) == ("DESK1", "ALGO", "FUND7")
        assert "|453=1|448=TRADER1|447=D|452=11|" in routed.to_pipe_string()
        assert routed["60"] != "20260921-08:05:00.000", "TransactTime is the send time"
        # the venue end only answers: PRD-5 cancels PRD-4, a replace nobody accepted
        assert [(m["35"], m["41"]) for m in mkt.sent] == [("9", "PRD-4")]

        # ...and the acceptor received them as orders and requests
        received = await _fetch_all(db, "SELECT cl_ord_id, symbol, status, pending_action FROM fix_orders "
                                        "WHERE direction = 'RX' ORDER BY id")
        assert [r["cl_ord_id"] for r in received] == ["PRD-1", "PRD-2", "PRD-3", "PRD-6", "PRD-8", "PRD-9", "PRD-10"]
        assert received[2]["pending_action"] == "Replace", "PRD-4 replaced PRD-3 and PRD-5 then asked to cancel"
        assert sorted(w["35"] for w in cli.sent) == sorted("D D D D D D D G F F Q".split())

    @pytest.mark.asyncio
    async def test_the_venues_side_replays_as_the_acceptor(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        await engine.configure_replay(job_id, target_session="Server", speed=0)
        got = await engine.start_replay(job_id, "PRODVENUE→PRODCLI")
        assert got["selected"] == 17
        await _settle(engine, job_id)
        assert (await _job(db, job_id))["status"] == "completed"

        for line in mkt.wire:
            assert "|49=Server|56=Client|" in line
        # the client end took the reports as its own orders' lifecycle
        sent = await _fetch_all(db, "SELECT cl_ord_id, symbol, status, cum_qty FROM fix_orders WHERE direction = 'TX' ORDER BY id")
        by_id = {r["cl_ord_id"]: r for r in sent}
        assert by_id["PRD-1"]["status"] == "Filled" and by_id["PRD-1"]["cum_qty"] == 100
        assert by_id["PRD-8"]["status"] == "Rejected"
        assert "PRD-5" in by_id and by_id["PRD-5"]["status"] == "Canceled", "the replace and cancel renamed the chain"
        trades = await _fetch_all(db, "SELECT exec_id, last_qty FROM fix_executions WHERE direction = 'RX' ORDER BY id")
        # E-14 corrected E-13, so the live row carries E-14 and E-13 is in its history
        assert {t["exec_id"] for t in trades} >= {"E-2", "E-3", "E-5", "E-10", "E-14"}
        history = await _fetch_all(db, "SELECT exec_id FROM fix_executions__history")
        assert "E-13" in {t["exec_id"] for t in history}
        assert cli.sent == []

    @pytest.mark.asyncio
    async def test_types_and_window_narrow_the_run(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        await engine.configure_replay(job_id, target_session="Client", speed=0, msg_filter="D",
                                      time_from="08:00", time_to="12:00")
        got = await engine.start_replay(job_id, "PRODCLI→PRODVENUE")
        await _settle(engine, job_id)
        assert got["selected"] == 5
        assert [w["11"] for w in cli.sent] == ["PRD-1", "PRD-2", "PRD-3", "PRD-6", "PRD-8"]

    @pytest.mark.asyncio
    async def test_a_headerless_log_replays_with_the_whole_header(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="order-flow"))["job_id"]
        await engine.configure_replay(job_id, target_session="Client", speed=0)
        await engine.start_replay(job_id)          # one blank direction: nothing to choose
        await _settle(engine, job_id)
        assert len(cli.sent) == 6
        assert cli.wire[0].startswith("8=FIX.4.4|9=") and "|35=D|49=Client|56=Server|34=1|52=" in cli.wire[0]
        received = await _fetch_all(db, "SELECT cl_ord_id FROM fix_orders WHERE direction = 'RX' ORDER BY id")
        assert [r["cl_ord_id"] for r in received] == ["REPLAY-001", "REPLAY-002", "REPLAY-003", "REPLAY-006"]

    @pytest.mark.asyncio
    async def test_pause_resume_stop_and_delete(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        # slow enough to catch mid-flight: 1 s per log second, capped gaps
        await engine.configure_replay(job_id, target_session="Client", speed=1, max_gap=0.2)
        await engine.start_replay(job_id, "PRODCLI→PRODVENUE")
        with pytest.raises(ValueError, match="already running"):
            await engine.start_replay(job_id, "PRODCLI→PRODVENUE")
        with pytest.raises(ValueError, match="stop it before changing"):
            await engine.configure_replay(job_id, target_session="Client")
        await asyncio.sleep(0.05)
        await engine.pause_replay(job_id)
        assert (await _job(db, job_id))["status"] == "paused"
        sent_at_pause = len(cli.sent)
        await asyncio.sleep(0.5)
        assert len(cli.sent) == sent_at_pause, "paused means nothing goes out"
        await engine.resume_replay(job_id)
        assert (await _job(db, job_id))["status"] == "running"
        await asyncio.sleep(0.05)
        await engine.stop_replay(job_id)
        job = await _job(db, job_id)
        assert job["status"] == "stopped" and 0 < job["sent_messages"] < 11
        with pytest.raises(ValueError, match="not playing"):
            await engine.pause_replay(job_id)
        await engine.delete_replay(job_id)
        assert await _fetch_all(db, "SELECT id FROM fix_replay_jobs") == []

    @pytest.mark.asyncio
    async def test_a_completed_job_starts_again_from_the_top(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="pipe-raw"))["job_id"]
        await engine.configure_replay(job_id, target_session="Client", speed=0)
        await engine.start_replay(job_id)
        await _settle(engine, job_id)
        await engine.start_replay(job_id)
        await _settle(engine, job_id)
        assert [w["11"] for w in cli.sent] == ["RAW-1", "RAW-2", "RAW-3"] * 2
        assert (await _job(db, job_id))["sent_messages"] == 3

    @pytest.mark.asyncio
    async def test_a_session_dropping_mid_replay_ends_it_in_error(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        await engine.configure_replay(job_id, target_session="Client", speed=1, max_gap=0.1)
        await engine.start_replay(job_id, "PRODCLI→PRODVENUE")
        await asyncio.sleep(0.05)
        cli.is_active = False
        await _settle(engine, job_id)
        job = await _job(db, job_id)
        assert (job["status"], job["error_text"]) == ("error", "Session disconnected during replay")

    @pytest.mark.asyncio
    async def test_archiving_a_playing_job_is_refused(self, pair):
        db, engine, cli, mkt = pair
        job_id = (await engine.load_replay(example="two-sided-day"))["job_id"]
        await engine.configure_replay(job_id, target_session="Client", speed=1, max_gap=1)
        await engine.start_replay(job_id, "PRODCLI→PRODVENUE")
        with pytest.raises(ValueError, match="is playing"):
            await engine.check_archive({"fix_replay_jobs": [{"id": job_id}]})
        await engine.stop_replay(job_id)
        await engine.check_archive({"fix_replay_jobs": [{"id": job_id}]})
