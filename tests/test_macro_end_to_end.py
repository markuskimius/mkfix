"""End-to-end macros: blocks of both sides in one file and one run, with a
session a side, offered what arrives before the runs of either side."""

import pytest

from mkfix import macro
from mkfix.macro import vocab
from mkfix.macro.instance import COMPLETED, FAILED, PASSED
from mkfix.macro.runner import MacroError
from mkfix.macro.store import MacroManager

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_sending import LinkedSession, pair  # noqa: F401

NEW = "new symbol: 'IBM', side: buy, qty: 100, price: 10"
BOTH = (f"on order\n    accept\n    fill qty: order.leaves_qty, price: order.price\n    pass 'filled ${{order.cl_ord_id}}'\n"
        f"run\n    {NEW}\n    expect filled within 5s\n    pass 'heard ${{order.cl_ord_id}}'\n")


def parsed(text, side="end-to-end"):
    kept, found = macro.check(text, side=side)
    assert found == [], [str(d) for d in found]
    return kept


class TestOneRun:
    @pytest.mark.asyncio
    async def test_both_sides_of_an_order_are_macros_of_one_run(self, pair):
        run = pair.runner.arm(parsed(BOTH), session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(5)
        (sent,), (received,) = await pair.orders("TX"), await pair.orders("RX")
        assert run.side == "end-to-end" and (run.session, run.market_session) == ("LOOP-CLI", "LOOP-MKT")
        assert [(i.block.kind, i.row["session_id"], i.status, i.message) for i in run.instances] == [
            ("client", "LOOP-CLI", PASSED, f"heard {sent['cl_ord_id']}"),
            ("market", "LOOP-MKT", PASSED, f"filled {received['cl_ord_id']}")]
        assert run.verdict == PASSED and run.status == "finished", "a test is over when what it sent is done"
        # and takes nothing more: what arrives now is for whoever else is armed
        await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "IBM", "side": "1",
                                                     "qty": "1", "price": "1"})
        await pair.runner.settle()
        assert len(run.instances) == 2 and pair.runner.offered("end-to-end") == []

    @pytest.mark.asyncio
    async def test_it_is_not_over_while_the_other_side_is_in_the_middle_of_its_lines(self, pair):
        text = ("on order\n    when cancel\n        accept\n        after 2s\n        pass 'the venue had the last word'\n"
                "    accept\n"
                f"run\n    {NEW}\n    expect ack within 2s\n    cancel\n    expect canceled within 2s\n    pass\n")
        run = pair.runner.arm(parsed(text), session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(1)
        client, venue = run.instances
        assert (client.status, venue.status, run.status) == (PASSED, "listening", "armed"), \
            "its main flow is done and it only listens — but a `when` of it is still answering"
        await pair.advance(3)
        assert (venue.status, venue.message, run.status) == (PASSED, "the venue had the last word", "finished")

    @pytest.mark.asyncio
    async def test_one_that_only_waits_stays_armed_and_those_left_listening_are_stopped_with_the_test(self, pair):
        waits = pair.runner.arm(parsed("on order where symbol == 'ZZZZ'\n    accept\non ioi\n    stop\n"),
                                market_session="LOOP-MKT")
        text = ("on order\n    when cancel\n        accept\n    accept\n"
                f"run\n    {NEW}\n    expect ack within 2s\n    pass\n")
        run = pair.runner.arm(parsed(text), session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(3)
        assert [(i.block.kind, i.status) for i in run.instances] == [("client", PASSED), ("market", "stopped")]
        assert (run.status, run.verdict) == ("finished", PASSED) and waits.status == "armed"

    @pytest.mark.asyncio
    async def test_a_session_a_side(self, pair):
        """Each block sends and is offered on the session of its side; a
        session a `run` names gives way to the one chosen, as always."""
        engine = pair.engine
        other_cli, other_mkt = LinkedSession(engine, "CLI-2"), LinkedSession(engine, "MKT-2")
        other_cli.peer, other_mkt.peer = other_mkt, other_cli
        engine.sessions.update({"CLI-2": other_cli, "MKT-2": other_mkt})
        text = (f"on order\n    accept\nrun on LOOP-CLI\n    {NEW}\n    expect ack within 2s\n    pass\n"
                "run\n    ioi symbol: 'IBM', side: buy, qty: 'L'\n    pass\n"
                "on ioi\n    log 'IOI on ${ioi.session_id}'\n")
        run = pair.runner.arm(parsed(text), session="CLI-2", market_session="MKT-2")
        await pair.advance(3)
        assert {(i.kind, i.block.kind): i.row["session_id"] for i in run.instances} == {
            ("order", "client"): "CLI-2", ("order", "market"): "MKT-2",
            ("ioi", "client"): "MKT-2", ("ioi", "market"): "CLI-2"}, \
            "the families turn round: the market side sends an IOI, the client side receives it"
        assert [t for *_, t in run.log] == ["IOI on CLI-2"]
        # what arrives on another session is not this run's
        await engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "IBM", "side": "1", "qty": "1", "price": "1"})
        await pair.runner.settle()
        assert len(run.instances) == 4

    @pytest.mark.asyncio
    async def test_a_session_left_out_is_every_session_for_what_waits_and_asked_for_what_sends(self, pair):
        text = f"on order\n    accept\nrun\n    {NEW}\n    pass\n"
        with pytest.raises(MacroError, match="line 3: `run` names no session, so choose the client session to send on"):
            pair.runner.arm(parsed(text), market_session="LOOP-MKT")
        with pytest.raises(MacroError, match="line 1: `run` names no session, so choose the market session to send on"):
            pair.runner.arm(parsed("run\n    ioi symbol: 'A', side: buy, qty: 'L'\nrun on LOOP-CLI\n    " + NEW + "\n"),
                            session="LOOP-CLI")
        run = pair.runner.arm(parsed(text), session="LOOP-CLI")
        await pair.advance(1)
        assert [i.row["session_id"] for i in run.instances] == ["LOOP-CLI", "LOOP-MKT"], "any session's orders are offered"
        with pytest.raises(MacroError, match="is a client macro: it has one session"):
            pair.runner.arm(parsed(f"run\n    {NEW}\n", side="client"), session="LOOP-CLI", market_session="LOOP-MKT")
        with pytest.raises(MacroError, match="`run` on NOPE: no such session"):
            pair.runner.arm(parsed("run\n    ioi symbol: 'A', side: buy, qty: 'L'\n"), session="LOOP-CLI",
                            market_session="NOPE")

    @pytest.mark.asyncio
    async def test_a_test_comes_before_the_desks_that_happen_to_be_armed(self, pair):
        desk = pair.arm("on order\n    reject text: 'the desk took it'\n")
        run = pair.runner.arm(parsed(BOTH.replace("on order", "on order where symbol == 'IBM'")),
                              session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(5)
        assert [i.status for i in run.instances] == [PASSED, PASSED] and desk.instances == [], \
            "armed after the desk, and offered its own order first"
        # what its `where` does not take is the desk's, as before
        await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "ZZZZ", "side": "1",
                                                     "qty": "1", "price": "1"})
        await pair.advance(1)
        assert len(desk.instances) == 1 and len(run.instances) == 2
        by_symbol = {o["symbol"]: o for o in await pair.orders("RX")}
        assert (by_symbol["ZZZZ"]["sent_text"], by_symbol["IBM"]["status"]) == ("the desk took it", "Filled")
        # among themselves end-to-end runs are offered in the order armed, and each kind has its own line
        waiting = "on order where symbol == 'AAPL'\n    reject text: '{}'\non ioi\n    stop\n"
        first, second = (pair.runner.arm(parsed(waiting.format(n)), market_session="LOOP-MKT") for n in ("first", "second"))
        assert [r.id for r in pair.runner.offered("end-to-end")] == [first.id, second.id], "the test itself is over"
        assert [r.id for r in pair.runner.offered("market")] == [desk.id]
        await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "AAPL", "side": "1",
                                                     "qty": "1", "price": "1"})
        await pair.runner.settle()
        assert (len(first.instances), len(second.instances), len(desk.instances)) == (1, 0, 1)
        assert pair.runner.move(second, up=True) and [r.id for r in pair.runner.offered("end-to-end")] == [second.id, first.id]

    @pytest.mark.asyncio
    async def test_what_the_sides_say_to_each_other_stays_in_the_run(self, pair):
        text = ("share mode = 'accept'\n"
                "on order\n    if shared.mode == 'reject'\n        reject text: 'told to'\n        stop\n    accept\n"
                "    signal 'taken' with order.cl_ord_id\n"
                f"run\n    {NEW}\n    expect signal 'taken' within 2s\n    let heard = event.value\n"
                "    share mode = 'reject'\n    signal 'next'\n"
                "    pass IF(heard == order.cl_ord_id and event.sender.session_id == 'LOOP-MKT', 'the venue said so', 'no')\n"
                f"on signal 'next'\n    {NEW}\n    expect rejected within 2s\n    pass order.text\n")
        run = pair.runner.arm(parsed(text), session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(5)
        assert [(i.block.kind, i.status, i.message) for i in run.instances] == [
            ("client", PASSED, "the venue said so"), ("market", COMPLETED, ""),
            ("client", PASSED, "told to"), ("market", "stopped", "")]
        assert run.shared == {"mode": "reject"}

    @pytest.mark.asyncio
    async def test_a_block_a_signal_starts_sends_on_the_session_of_its_side(self, pair):
        """The venue's allocation of an order the client's block sent: with
        the market session chosen it goes out there, whoever signalled."""
        text = (f"run\n    {NEW}\n    expect filled within 5s\n    signal 'done'\n    pass\n"
                "on order\n    accept\n    fill qty: order.leaves_qty, price: order.price\n"
                "on signal 'done'\n    allocate symbol: 'IBM', side: buy, qty: 100, avg_price: 10, accounts: 'A 100'\n"
                "on allocation\n    accept allocation\n    pass\n")
        run = pair.runner.arm(parsed(text), session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(5)
        (sent,) = await _fetch_all(pair.db, "SELECT * FROM fix_allocations WHERE direction = 'TX'")
        assert (sent["session_id"], sent["status"]) == ("LOOP-MKT", "Accepted")
        assert sorted((i.kind, vocab.side_of(i.block.kind, i.kind)) for i in run.instances) == [
            ("allocation", "client"), ("allocation", "market"), ("order", "client"), ("order", "market")]


class TestTheExamples:
    @pytest.mark.asyncio
    async def test_end_to_end_order(self, pair):
        run = pair.arm("end-to-end-order", session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(15)
        (sent,), (received,) = await pair.orders("TX"), await pair.orders("RX")
        assert [(i.block.kind, i.status, i.message) for i in run.instances] == [
            ("client", PASSED, f"{sent['cl_ord_id']} cancelled with 50 done"),
            ("market", PASSED, f"asked to cancel {received['cl_ord_id']} with 250 left")]
        assert (sent["status"], sent["order_qty"], sent["cum_qty"]) == ("Canceled", 300.0, 50.0)

    @pytest.mark.asyncio
    async def test_told_to_reject(self, pair):
        run = pair.arm("told-to-reject", session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(15)
        first, second = await pair.orders("TX")
        assert (first["status"], second["status"], second["text"]) == (
            "Filled", "Rejected", f"halted after {first['cl_ord_id']}")
        assert run.verdict == PASSED and FAILED not in {i.status for i in run.instances}
        assert [i.message for i in run.instances if i.message] == [f"refused as told: halted after {first['cl_ord_id']}"]
        assert run.shared == {"mode": "reject", "why": f"halted after {first['cl_ord_id']}"}

    def test_they_are_end_to_end_and_the_rest_are_not(self):
        from mkfix.macro.store import EXAMPLES
        sides = {p.stem: macro.check(p.read_text(encoding="utf-8"))[0].side for p in EXAMPLES.glob("*.macro")}
        assert {name for name, side in sides.items() if side == "end-to-end"} == {"end-to-end-order", "told-to-reject"}
        assert set(sides.values()) == set(vocab.MACRO_KINDS)


class TestTheManager:
    @pytest.mark.asyncio
    async def test_saved_checked_run_and_shown_as_its_own_kind(self, pair):
        manager = MacroManager(pair.engine, pair.runner)
        pair.engine.macros = manager
        await manager.start()
        try:
            checked = await manager.check(BOTH, "end-to-end")
            assert (checked["side"], checked["errors"], checked["sends"], checked["needs_session"],
                    checked["needs_market_session"]) == ("end-to-end", 0, True, True, False)
            assert [b["kind"] for b in checked["blocks"]] == ["market", "client"]
            # a draft of one side kept in the end-to-end editor is an end-to-end macro
            await manager.save("half", f"run\n    {NEW}\n", "end-to-end")
            await manager.save("both", BOTH, "end-to-end")
            await manager.save("desk", "on order\n    accept\n", "market")
            kept = {r["name"]: (r["side"], r["problems"], r["needs_session"]) for r in await _fetch_all(
                pair.db, "SELECT * FROM fix_macros")}
            assert kept == {"half": ("end-to-end", 0, 1), "both": ("end-to-end", 0, 1), "desk": ("market", 0, 0)}
            with pytest.raises(ValueError, match="An end-to-end macro is already called 'both'"):
                await manager.save("both", "on order\n    accept\n", "market")
            for side, where in (("client", "End-to-end"), ("market", "End-to-end")):
                with pytest.raises(ValueError, match=f"'both' is an end-to-end macro: start it from {where} Macros"):
                    await manager.arm("both", side=side, session="LOOP-CLI")
            with pytest.raises(ValueError, match="'desk' is a market macro: start it from Market Macros"):
                await manager.arm("desk", side="end-to-end")
            with pytest.raises(ValueError, match="'desk' is a market macro: it has one session"):
                await manager.arm("desk", side="market", market_session="LOOP-MKT")
            with pytest.raises(ValueError, match="No session named 'NOPE'"):
                await manager.arm("both", side="end-to-end", session="LOOP-CLI", market_session="NOPE")
            started = await manager.arm("both", side="end-to-end", session="LOOP-CLI", market_session="LOOP-MKT")
            assert started["side"] == "end-to-end"
            await pair.advance(5)
            await manager.flush()
            (run,) = await _fetch_all(pair.db, "SELECT * FROM fix_macro_runs")
            assert (run["side"], run["session"], run["market_session"], run["status"], run["verdict"], run["orders"],
                    run["passed"], run["priority"]) == ("end-to-end", "LOOP-CLI", "LOOP-MKT", "finished", "passed", 2, 2, 0)
            assert run["ended_at"], "over when what it sent was done"
            held = await _fetch_all(pair.db, "SELECT side, session_id, subject, status FROM fix_macro_orders ORDER BY id")
            assert [tuple(h.values()) for h in held] == [("end-to-end", "LOOP-CLI", "order", "passed"),
                                                         ("end-to-end", "LOOP-MKT", "order", "passed")]
            log = await _fetch_all(pair.db, "SELECT side FROM fix_macro_log")
            assert {line["side"] for line in log} == {"end-to-end"}
            (sent,), (received,) = await pair.orders("TX"), await pair.orders("RX")
            assert sent["macro"] == received["macro"] == f"both #{run['id']}"
            assert [e["name"] for e in manager.examples("end-to-end")] == ["end-to-end-order", "told-to-reject"]
            assert "end-to-end-order" not in [e["name"] for e in manager.examples("client")]
            assert manager.example("told-to-reject")["side"] == "end-to-end"
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_paused_resumed_and_stopped_by_name_and_by_kind_like_a_sides(self, pair):
        manager = MacroManager(pair.engine, pair.runner)
        pair.engine.macros = manager
        await manager.start()
        try:
            slow = ("on order\n    accept\n    after 5s\n    log 'half way'\n    after 60s\n"
                    "    fill qty: order.leaves_qty, price: order.price\n"
                    f"run\n    {NEW}\n    expect filled within 5m\n    pass\n")
            await manager.save("slow test", slow, "end-to-end")
            started = await manager.arm("slow test", side="end-to-end", session="LOOP-CLI", market_session="LOOP-MKT")
            await pair.advance(1)
            assert (await manager.pause_macro("slow test"))["paused"] == 1
            await pair.advance(20)
            await manager.flush()
            (row,) = await _fetch_all(pair.db, "SELECT status, passed FROM fix_macro_runs")
            assert (row["status"], row["passed"]) == ("paused", 0), "parked before its next line"
            assert [r.id for r in manager._side_runs("end-to-end", paused=True)] == [manager._runs_by_row[started["run_id"]].id]
            with pytest.raises(ValueError):
                manager._side_runs("client", str(started["run_id"]))
            assert (await manager.resume_macro("slow test"))["resumed"] == 1
            await pair.advance(1)
            await manager.flush()
            assert [r["text"] for r in await _fetch_all(pair.db, "SELECT text FROM fix_macro_log")] == ["half way"]
            await manager.stop_macro("slow test")
            await manager.flush()
            (row,) = await _fetch_all(pair.db, "SELECT status, priority FROM fix_macro_runs")
            assert (row["status"], row["priority"]) == ("stopped", 0)
            held = await _fetch_all(pair.db, "SELECT status FROM fix_macro_orders")
            assert {h["status"] for h in held} == {"stopped"}
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_a_macro_from_history_is_a_sides(self, pair):
        """Macro… on a blotter writes this side's part: a blotter is of one side."""
        manager = MacroManager(pair.engine, pair.runner)
        await manager.start()
        try:
            with pytest.raises(ValueError, match="of the client side or the market side, not 'end-to-end'"):
                await manager.from_history("end-to-end", "order", [1], name="x", save=True)
            with pytest.raises(ValueError, match="An end-to-end recording is of|No end-to-end recording is under way"):
                await manager.record_stop("end-to-end", "x")
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_the_runs_tree_has_both_sessions(self, stack):
        import tomllib
        from pathlib import Path
        import mkfix
        db, writer, engine = stack
        toml = tomllib.loads((Path(mkfix.__file__).parent / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        assert "market_session" in toml["tables"]["fix_macro_runs"]["columns"]
        sql = toml["services"]["macro_runs_tree"]["sql"]
        conn = db.write_conn
        await conn.execute("INSERT INTO fix_macro_runs (macro, side, session, market_session) VALUES ('t', 'end-to-end', 'C', 'M')")
        await conn.execute("INSERT INTO fix_macro_orders (run_id, order_row, side, session_id) VALUES (1, 5, 'end-to-end', 'M')")
        await conn.commit()
        run, order = await _fetch_all(db, sql)
        assert (run["kind"], run["session"], run["market_session"]) == ("run", "C", "M")
        assert (order["kind"], order["session"], order["market_session"]) == ("order", "M", "")
