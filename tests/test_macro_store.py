"""Macros in the database: saving, arming, the runner's shadow rows,
restart recovery, the archive guard, and the fix_cmd commands."""

from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from mkfix.fix.message import parse_fix
from mkfix.macro.clock import Scheduler, VirtualClock
from mkfix.macro.runner import MacroRunner
from mkfix.macro.store import LOOPBACK, MacroManager, example_header
from mkfix.services.fix_command import FixCommandService

from tests.test_engine import StubSession, _fetch_all, stack  # noqa: F401

SLOW = """on order
    accept
    log 'working ${order.cl_ord_id}'
    while order.leaves_qty > 0
        after 1s
        fill qty: 50, price: order.price
    pass 'done'
"""


async def _manager(engine):
    clock = VirtualClock(Scheduler())
    manager = MacroManager(engine, MacroRunner(engine, clock))
    engine.macros = manager
    await manager.start()
    return manager, clock


@pytest_asyncio.fixture
async def kit(stack):
    db, writer, engine = stack
    stub = StubSession()
    engine.sessions["S1"] = stub
    manager, clock = await _manager(engine)
    yield db, engine, stub, manager, clock
    await manager.stop()


def _ask(engine):
    svc = FixCommandService(config={}, db=MagicMock(), change_bus=MagicMock(), writer=MagicMock())
    svc.set_engine(engine)
    return svc._dispatch


async def _order(engine, stub, manager, cl="C1", qty=100):
    await engine.on_app_message(stub, "D", parse_fix(f"8=FIX.4.2|35=D|11={cl}|55=AAPL|54=1|38={qty}|40=2|44=10|59=0"))
    await manager.flush()


class TestScripts:
    @pytest.mark.asyncio
    async def test_save_check_load_delete(self, kit):
        db, engine, stub, manager, clock = kit
        result = await manager.save("slow", SLOW)
        assert (result["errors"], result["diagnostics"]) == (0, [])
        assert result["blocks"] == [{"kind": "market", "line": 1, "session": ""}]
        assert (await manager.load("slow"))["source"] == SLOW
        await manager.save("slow", SLOW.replace("50", "25"))
        versions = await _fetch_all(db, "SELECT _mkio_version FROM fix_macros__history ORDER BY _mkio_version")
        assert [v["_mkio_version"] for v in versions] == [1, 2], "every Save is kept"
        await manager.delete("slow")
        with pytest.raises(ValueError, match="No macro named 'slow'"):
            await manager.load("slow")

    @pytest.mark.asyncio
    async def test_every_save_is_a_version_the_history_service_returns(self, kit):
        """The editors' History reads `macro_versions`: its SQL, run here
        against what saving really records."""
        import tomllib
        from pathlib import Path
        import mkfix
        db, engine, stub, manager, clock = kit
        toml = tomllib.loads((Path(mkfix.__file__).parent / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        sql = toml["services"]["macro_versions"]["sql"]
        texts = [SLOW, SLOW + "# two\n", "on order\n    nonsense\n"]
        for text in texts:
            await manager.save("slow", text)
        await manager.save("other", "on order\n    accept\n")
        row = await manager.load("slow")
        cursor = await db.read_conn.execute(sql, {"id": row["id"]})
        got = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        assert [(v["_mkio_version"], v["source"], v["problems"], v["side"]) for v in got] == [
            (3, texts[2], 1, "market"), (2, texts[1], 0, "market"), (1, texts[0], 0, "market")]
        assert all(v["name"] == "slow" and v["updated_at"] for v in got)
        assert row["_mkio_version"] == 3, "the run's `version` and the editor's live-line marks go by this"
        # deleted and written again: a fresh history, not the old one under the same name
        await manager.delete("slow")
        await manager.save("slow", SLOW)
        again = await manager.load("slow")
        cursor = await db.read_conn.execute(sql, {"id": again["id"]})
        fresh = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        assert again["id"] != row["id"] and [v["_mkio_version"] for v in fresh] == [1]

    @pytest.mark.asyncio
    async def test_a_draft_with_problems_is_kept_and_counted(self, kit):
        db, engine, stub, manager, clock = kit
        result = await manager.save("draft", "on order\n    acept\n    fill qty: 1\n")
        assert result["errors"] == 2
        assert [(d["line"], d["col"], d["severity"]) for d in result["diagnostics"]] == [(2, 4, "error"), (3, 4, "error")]
        assert (await manager.load("draft"))["problems"] == 2
        with pytest.raises(ValueError, match="'draft' has 2 problem\\(s\\); the first: line 2"):
            await manager.arm("draft")

    @pytest.mark.asyncio
    async def test_the_name_is_the_saved_one_and_a_file_name(self, kit):
        """The text carries no name, so the same text saves under any name;
        the name is a file name on export, so it is held to a plain form."""
        db, engine, stub, manager, clock = kit
        await manager.save("fast", SLOW)
        await manager.save("slow", SLOW)
        assert (await manager.load("fast"))["source"] == (await manager.load("slow"))["source"]
        assert "name" not in await manager.check(SLOW)
        with pytest.raises(ValueError, match="needs a name"):
            await manager.save("  ", SLOW)
        for bad in ("a,b", "-lead", "x/y", "q?", ":x", "a|b"):
            with pytest.raises(ValueError, match="cannot name a macro"):
                await manager.save(bad, SLOW)
        await manager.save("Desk 2026-09-22 14.30.15_v2", SLOW)
        await manager.save("Market 2026-09-22 14:30:15", SLOW), "what Stop suggests: colons and all"
        with pytest.raises(ValueError, match="No macro named 'a,b'"):
            await manager.load("a,b")

    @pytest.mark.asyncio
    async def test_an_armed_run_knows_its_macro_by_the_saved_name(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("venue", SLOW)
        await manager.arm("venue")
        assert [r.macro.name for r in manager.live_runs("venue")] == ["venue"]

    @pytest.mark.asyncio
    async def test_check_knows_the_sessions_and_templates_that_exist(self, kit):
        db, engine, stub, manager, clock = kit
        await engine.save_template("fill", "half", qty="50")
        text = "on order\n    fill using 'half'\n    fill using 'hafl'\n"
        found = (await manager.check(text))["diagnostics"]
        assert [(d["line"], d["message"]) for d in found] == [(3, "No fill template named 'hafl' — did you mean 'half'?")]

    def test_examples_are_listed_by_their_headers(self):
        manager = MacroManager(MagicMock(), MagicMock())
        listed = {e["name"]: e for e in manager.examples()}
        assert {"auto-ack", "slow-fill", "cancel-replace-desk", "dispute-desk"} <= set(listed)
        assert all(set(e) == {"name", "side", "title", "shows", "needs", "watch", "outcome"} for e in listed.values())
        assert {e["side"] for e in listed.values()} == {"client", "market"}
        client, market = manager.examples("client"), manager.examples("market")
        assert len(client) + len(market) == len(listed) and {e["side"] for e in client} == {"client"}
        assert {"loopback-client", "take-over", "order-burst"} <= {e["name"] for e in client}
        assert {"loopback-venue", "auto-ack"} <= {e["name"] for e in market}
        assert manager.example("take-over")["side"] == "client"
        assert listed["auto-ack"]["title"] == "Auto-acknowledge" and "250 ms" in listed["auto-ack"]["outcome"]
        one = manager.example("slow-fill")
        assert one["source"].startswith("# Slow fill\n") and "while order.leaves_qty > 0" in one["source"]
        for bad in ("nope", "../store", "/etc/passwd"):
            with pytest.raises(ValueError, match="No example named"):
                manager.example(bad)

    def test_a_header_line_may_wrap(self):
        head = example_header("# T\n#\n# Shows:   one\n#          two\n# Needs:   n\n\non order\n")
        assert head == {"title": "T", "shows": "one two", "needs": "n"}


class TestRuns:
    @pytest.mark.asyncio
    async def test_arming_writes_the_run_with_what_it_ran(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.save("slow", SLOW + "\n# edited\n")
        armed = await manager.arm("slow", session="S1", seed=11, speed=2)
        (row,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert (row["id"], row["macro"], row["version"], row["session"], row["seed"], row["speed"], row["status"]) == (
            armed["run_id"], "slow", 2, "S1", 11, 2.0, "armed")
        assert (row["side"], row["priority"], armed["side"]) == ("market", 1, "market")
        with pytest.raises(ValueError, match="No macro named"):
            await manager.arm("nope")
        with pytest.raises(ValueError, match="has 1 live run: stop it first"):
            await manager.delete("slow")

    @pytest.mark.asyncio
    async def test_the_runs_tree_hangs_each_order_under_its_run(self, kit):
        """`macro_runs_tree` (mkfix.toml) is the Macro Runs window's query: a
        UNION of runs and orders in one row shape. A run row keeps the runs
        table's `id` (mkio re-reads a changed run through the SQL by it) and
        an order row's id is negated so the two can never collide; `key` /
        `parent_key` are what mkui's tree nests by, and `run_id` /
        `order_row` what the log pane's link filters by — a run-level log
        line has no order row, so the link must carry the run too."""
        import tomllib
        from pathlib import Path
        db, engine, stub, manager, clock = kit
        toml = tomllib.loads((Path(__file__).parent.parent / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        service = toml["services"]["macro_runs_tree"]
        assert (service["primary_table"], service["watch_tables"], service["key"]) == (
            "fix_macro_runs", ["fix_macro_runs", "fix_macro_orders"], ["key"])
        await manager.save("slow", SLOW)
        run_id = (await manager.arm("slow"))["run_id"]
        await _order(engine, stub, manager)
        rows = {r["kind"]: r for r in await _fetch_all(db, service["sql"])}
        run, order = rows["run"], rows["order"]
        (inst,) = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
        assert (run["id"], run["key"], run["parent_key"], run["run_id"], run["order_row"]) == (run_id, f"r{run_id}", "", run_id, None)
        assert (run["macro"], run["status"], run["orders"], run["cl_ord_id"], run["line"]) == ("slow", "armed", 1, "", 0)
        assert (order["id"], order["key"], order["parent_key"], order["run_id"], order["order_row"]) == (
            -inst["id"], f"o{inst['id']}", f"r{run_id}", run_id, inst["order_row"])
        assert (order["macro"], order["status"], order["cl_ord_id"], order["symbol"], order["line"], order["waiting_for"], order["priority"]) == (
            "slow", "running", "C1", "AAPL", 5, "after 1s", None)
        assert set(run) == set(order)
        # A line the run logs about no order in particular (an order not taken, a
        # `where` that failed) has a NULL order_row: the log's link on the
        # run alone finds it, and the tree's run row broadcasts a blank there.
        (run_obj,) = [r for r in manager.runner.runs if r.id == run_id]
        manager.runner.on_log(run_obj, None, 1, "no more orders")
        await manager.flush()
        lines = await _fetch_all(db, "SELECT order_row, text FROM fix_macro_log ORDER BY id")
        assert [(l["order_row"] is None, l["text"]) for l in lines] == [(False, "working C1"), (True, "no more orders")]

    @pytest.mark.asyncio
    async def test_unknown_session(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        with pytest.raises(ValueError, match="No session named 'S9'"):
            await manager.arm("slow", session="S9")

    @pytest.mark.asyncio
    async def test_a_scripts_progress_is_mirrored(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        run_id = (await manager.arm("slow"))["run_id"]
        await _order(engine, stub, manager)
        (inst,) = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
        assert (inst["run_id"], inst["macro"], inst["cl_ord_id"], inst["symbol"], inst["block_line"]) == (
            run_id, "slow", "C1", "AAPL", 1)
        assert (inst["status"], inst["line"], inst["waiting_for"]) == ("running", 5, "after 1s")
        order = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert order["macro"] == f"slow #{run_id}" and inst["order_row"] == order["id"]
        assert [(l["line"], l["text"], l["cl_ord_id"]) for l in await _fetch_all(db, "SELECT * FROM fix_macro_log")] == [
            (3, "working C1", "C1")]
        (run,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert (run["orders"], run["live"], run["passed"], run["verdict"]) == (1, 1, 0, "")

        await clock.advance(2)
        await manager.flush()
        (inst,) = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
        assert (inst["status"], inst["message"], inst["actions"]) == ("passed", "done", 3)
        (run,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert (run["orders"], run["live"], run["passed"], run["failed"], run["verdict"], run["status"]) == (
            1, 0, 1, 0, "passed", "armed")
        assert (await _fetch_all(db, "SELECT text FROM fix_macro_log"))[-1]["text"] == "passed: done"

    @pytest.mark.asyncio
    async def test_the_tag_survives_the_orders_own_writes(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.arm("slow")
        await _order(engine, stub, manager)
        await clock.advance(1)
        await manager.flush()
        order = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert order["cum_qty"] == 50.0 and order["macro"].startswith("slow #")

    @pytest.mark.asyncio
    async def test_pause_resume_stop_detach(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        run_id = (await manager.arm("slow"))["run_id"]
        await _order(engine, stub, manager, "A", 1000)
        await _order(engine, stub, manager, "B", 1000)
        status = lambda: _fetch_all(db, "SELECT status, live FROM fix_macro_runs")
        await manager.pause_run(run_id)
        assert (await status())[0]["status"] == "paused"
        await manager.resume_run(run_id)
        assert (await status())[0]["status"] == "armed"
        b = (await _fetch_all(db, "SELECT * FROM fix_macro_orders WHERE cl_ord_id = 'B'"))[0]
        await manager.detach(b["order_row"])
        with pytest.raises(ValueError, match="No live macro owns"):
            await manager.detach(b["order_row"])
        await manager.stop_run(run_id)
        rows = await _fetch_all(db, "SELECT cl_ord_id, status FROM fix_macro_orders ORDER BY cl_ord_id")
        assert [(r["cl_ord_id"], r["status"]) for r in rows] == [("A", "stopped"), ("B", "detached")]
        (run,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert (run["status"], run["live"]) == ("stopped", 0) and run["ended_at"]
        await manager.stop_run(run_id)                         # Stop on a stopped run changes nothing, and says nothing
        assert (await _fetch_all(db, "SELECT status FROM fix_macro_runs"))[0]["status"] == "stopped"
        with pytest.raises(ValueError, match="No run 99"):
            await manager.stop_run(99)
        await manager.delete("slow")

    @pytest.mark.asyncio
    async def test_the_editor_pauses_and_resumes_a_macro_by_name(self, kit):
        """The editor's ⏸ is every playing run of the open macro; once all
        are paused, the same button resumes them."""
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        first = (await manager.arm("slow"))["run_id"]
        second = (await manager.arm("slow", session="S1"))["run_id"]
        status = lambda: _fetch_all(db, "SELECT id, status FROM fix_macro_runs ORDER BY id")
        await manager.pause_run(first)
        assert await manager.pause_macro("slow") == {"paused": 1}, "only the run still playing"
        assert [r["status"] for r in await status()] == ["paused", "paused"]
        assert await manager.pause_macro("slow") == {"paused": 0}
        assert await manager.resume_macro("slow") == {"resumed": 2}
        assert [r["status"] for r in await status()] == ["armed", "armed"]
        assert await manager.resume_macro("slow") == {"resumed": 0}
        assert await manager.pause_macro("no-such") == {"paused": 0}, "no live run, nothing to say"
        await manager.stop_macro("slow")
        assert (first, second) == tuple(r["id"] for r in await status())

    @pytest.mark.asyncio
    async def test_a_selection_is_deleted_whole_or_not_at_all(self, kit):
        """The editor's Delete of several macros: one that is live, or not a
        macro, refuses the list before anything is written."""
        db, engine, stub, manager, clock = kit
        for name in ("a", "b", "c"):
            await manager.save(name, SLOW)
        run_id = (await manager.arm("b"))["run_id"]
        names = lambda: _fetch_all(db, "SELECT name FROM fix_macros ORDER BY name")
        with pytest.raises(ValueError, match="'b' has 1 live run: stop it first"):
            await manager.delete_many(["a", "b", "c"])
        with pytest.raises(ValueError, match="No macro named 'd'"):
            await manager.delete_many(["a", "d"])
        with pytest.raises(ValueError, match="Name a macro to delete"):
            await manager.delete_many(["", "  "])
        assert [r["name"] for r in await names()] == ["a", "b", "c"], "nothing of a refused list went"
        await manager.arm("c")
        with pytest.raises(ValueError, match="'c', 'b' have live runs: stop them first"):
            await manager.delete_many(["c", "a", "b"])                   # named in the order given
        await manager.stop_run(run_id)
        await manager.stop_macro("c")
        assert await manager.delete_many(["a", " b ", "a"]) == {"deleted": 2}, "trimmed, once each"
        assert [r["name"] for r in await names()] == ["c"]
        await manager.delete("c")
        assert await names() == []
        with pytest.raises(ValueError, match="No macro named 'c'"):
            await manager.delete("c")

    @pytest.mark.asyncio
    async def test_a_restart_stops_every_macro_and_plays_nothing_again(self, kit):
        """Found in use: ▶ stayed lit after a restart, because a macro that
        only waits for orders used to be armed again. A run is something a
        person started; the server coming back is not that person."""
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.save("mind", MINDER)
        first = (await manager.arm("slow", seed=5))["run_id"]
        await manager.arm("mind")
        paused = (await manager.arm("slow", session="S1"))["run_id"]
        await manager.pause_run(paused)
        await _order(engine, stub, manager, qty=1000)
        await manager.stop()                                   # the process goes away without a word

        manager2, clock2 = await _manager(engine)
        try:
            runs = await _fetch_all(db, "SELECT * FROM fix_macro_runs ORDER BY id")
            assert [(r["id"], r["status"], r["priority"], r["live"]) for r in runs] == [
                (first, "interrupted", 0, 0), (first + 1, "interrupted", 0, 0), (paused, "interrupted", 0, 0)]
            assert all(r["ended_at"] for r in runs), "the status bar says `interrupted` for a minute by this"
            assert manager2.live_runs() == [] and manager2.runner.runs == [] and not engine.events.active
            (old,) = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
            assert (old["status"], old["message"]) == ("interrupted", "the server stopped")
            await _order(engine, stub, manager2, "C2")
            assert len(await _fetch_all(db, "SELECT * FROM fix_macro_orders")) == 1, "the next order is nobody's macro's"
            assert (await _fetch_all(db, "SELECT pending_action FROM fix_orders WHERE cl_ord_id = 'C2'"))[0]["pending_action"] == "New"
            # what the blotters' controls are fed: nothing to pause or stop, and Play… offers the macros afresh
            for side in ("market", "client"):
                assert await _ask_sql(db, "macro_run_options", side=side, paused=1) == []
                assert all(o["value"].startswith(("macro:", "needs:")) for o in await _ask_sql(db, "macro_play_options", side=side))
            again = await manager2.arm("slow")               # and playing it again is yours to do
            assert again["run_id"] == paused + 1
        finally:
            await manager2.stop()

    @pytest.mark.asyncio
    async def test_a_restart_needs_nothing_of_the_macro_it_interrupts(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.arm("slow")
        await manager.stop()
        await manager._write("upsert_macro", ("slow", "market", 0, "on order\n    nonsense\n", 1, "", "", None))
        manager2, _ = await _manager(engine)
        try:
            assert [r["status"] for r in await _fetch_all(db, "SELECT status FROM fix_macro_runs")] == ["interrupted"]
            assert manager2.live_runs() == []
        finally:
            await manager2.stop()

    @pytest.mark.asyncio
    async def test_an_archive_is_refused_while_a_run_is_armed(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        run_id = (await manager.arm("slow"))["run_id"]
        with pytest.raises(ValueError, match="macro slow is armed"):
            await engine.check_archive({"fix_orders": [{"id": 1}]})
        await engine.check_archive({"fix_messages": [{"id": 1}]})
        await manager.stop_run(run_id)
        await engine.check_archive({"fix_orders": [{"id": 1}], "fix_macro_runs": [{"id": run_id}]})


CLIENT = "run\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n    wait filled\n"
MINDER = "on sent order\n    wait filled\n"


class TestSides:
    @pytest.mark.asyncio
    async def test_a_script_is_saved_with_its_side(self, kit):
        db, engine, stub, manager, clock = kit
        assert (await manager.save("slow", SLOW))["side"] == "market"
        assert (await manager.save("send", CLIENT, "client"))["side"] == "client"
        assert (await manager.save("mind", MINDER))["side"] == "client"
        rows = {r["name"]: r["side"] for r in await _fetch_all(db, "SELECT name, side FROM fix_macros")}
        assert rows == {"slow": "market", "send": "client", "mind": "client"}
        checked = await manager.check(CLIENT)
        assert (checked["side"], checked["needs_session"], checked["session"]) == ("client", True, "")
        named = await manager.check(CLIENT.replace("run\n", "run on S1\n"))
        assert (named["needs_session"], named["session"]) == (False, "S1")
        two = await manager.check(CLIENT.replace("run\n", "run on S1\n") + "run on S2\n    new symbol: 'A', side: buy, qty: 1\n")
        assert two["session"] == "", "Run… opens on a session only when the script names exactly one"

    @pytest.mark.asyncio
    async def test_the_pane_it_is_saved_from_decides_and_a_draft_of_the_wrong_side_is_a_draft(self, kit):
        db, engine, stub, manager, clock = kit
        wrong = await manager.save("send", CLIENT, "market")
        assert wrong["side"] == "market" and wrong["errors"] == 1
        assert "This is a market macro" in wrong["diagnostics"][0]["message"]
        empty = await manager.save("blank", "", "client")
        assert empty["side"] == "client"
        with pytest.raises(ValueError, match="client side or the market side, not 'both'"):
            await manager.save("x", "", "both")
        with pytest.raises(ValueError, match="has 1 problem"):
            await manager.arm("send")

    @pytest.mark.asyncio
    async def test_one_name_for_both_sides(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW, "market")
        with pytest.raises(ValueError, match="A market macro is already called 'slow': choose another name"):
            await manager.save("slow", CLIENT.replace("send", "slow"), "client")
        assert (await manager.load("slow"))["source"] == SLOW

    @pytest.mark.asyncio
    async def test_each_sides_command_starts_only_its_own(self, kit):
        db, engine, stub, manager, clock = kit
        ask = _ask(engine)
        await manager.save("slow", SLOW)
        await manager.save("send", CLIENT)
        with pytest.raises(ValueError, match="'send' is a client macro: run it from Client Macros"):
            await ask("arm_macro", {"name": "send", "session": "S1"})
        with pytest.raises(ValueError, match="'slow' is a market macro: arm it from Market Macros"):
            await ask("run_macro", {"name": "slow"})
        with pytest.raises(Exception, match="`run` names no session"):
            await ask("run_macro", {"name": "send"})
        assert manager.live_runs() == [] and await _fetch_all(db, "SELECT * FROM fix_macro_runs") == []
        assert (await ask("arm_macro", {"name": "slow"}))["side"] == "market"
        assert (await ask("run_macro", {"name": "send", "session": "S1"}))["side"] == "client"
        await manager.flush()
        assert len(stub.sent) == 1

    @pytest.mark.asyncio
    async def test_rows_are_stamped_with_the_side(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.save("send", CLIENT.replace("    wait filled\n", "    log 'sent'\n    wait filled\n"))
        await manager.arm("slow")
        await manager.arm("send", session="S1")
        await _order(engine, stub, manager)
        for table in ("fix_macro_runs", "fix_macro_orders"):
            rows = await _fetch_all(db, f"SELECT macro, side FROM {table} ORDER BY id")
            assert sorted((r["macro"], r["side"]) for r in rows) == [("send", "client"), ("slow", "market")], table
        logged = await _fetch_all(db, "SELECT * FROM fix_macro_log ORDER BY macro")
        assert [(r["macro"], r["side"], r["text"]) for r in logged] == [
            ("send", "client", "sent"), ("slow", "market", "working C1")]

    @pytest.mark.asyncio
    async def test_rows_from_before_sides_are_given_one(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.save("send", CLIENT)
        await manager.arm("send", session="S1")
        await manager.flush()
        await manager.stop()
        conn = engine.db.write_conn
        for table in ("fix_macros", "fix_macro_runs", "fix_macro_orders", "fix_macro_log"):
            await (await conn.execute(f"UPDATE {table} SET side = ''")).close()
        await (await conn.execute("INSERT INTO fix_macro_runs (macro, side, status) VALUES ('gone', '', 'stopped')")).close()
        await conn.commit()
        manager2, _ = await _manager(engine)
        try:
            rows = {r["name"]: r["side"] for r in await _fetch_all(db, "SELECT name, side FROM fix_macros")}
            assert rows == {"slow": "market", "send": "client"}
            runs = {r["macro"]: r["side"] for r in await _fetch_all(db, "SELECT macro, side FROM fix_macro_runs")}
            assert runs == {"send": "client", "gone": "market"}, "a run whose script is gone is taken for a market run"
            assert {r["side"] for r in await _fetch_all(db, "SELECT side FROM fix_macro_orders")} == {"client"}
        finally:
            await manager2.stop()


class TestManyRuns:
    @pytest.mark.asyncio
    async def test_a_client_script_runs_as_many_times_at_once_as_asked(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("send", CLIENT)
        ids = [(await manager.arm("send", side="client", session="S1"))["run_id"] for _ in range(3)]
        await manager.flush()
        assert len(set(ids)) == 3 and len(stub.sent) == 3 and len(manager.live_runs("send")) == 3
        rows = await _fetch_all(db, "SELECT * FROM fix_macro_runs ORDER BY id")
        assert [(r["status"], r["live"], r["priority"]) for r in rows] == [("armed", 1, 0)] * 3
        with pytest.raises(ValueError, match="'send' has 3 live runs: stop them first"):
            await manager.delete("send")
        # edited while they run: each run keeps the script it started with
        await manager.save("send", CLIENT + "    pass\n")
        assert all(len(r.macro.blocks[0].body) == 2 for r in manager.live_runs("send"))
        assert await manager.stop_macro("send") == {"stopped": 3}
        rows = await _fetch_all(db, "SELECT * FROM fix_macro_runs ORDER BY id")
        assert [r["status"] for r in rows] == ["stopped"] * 3 and manager.live_runs() == []
        assert await manager.stop_macro("send") == {"stopped": 0}
        await manager.delete("send")

    @pytest.mark.asyncio
    async def test_a_waiting_script_is_armed_once_for_the_same_sessions(self, kit):
        db, engine, stub, manager, clock = kit
        engine.sessions["S2"] = StubSession("S2")
        await manager.save("slow", SLOW)
        await manager.save("mind", MINDER)
        await manager.arm("slow")
        with pytest.raises(ValueError, match="'slow' is already armed on every session: a second run there would never"):
            await manager.arm("slow")
        await manager.arm("slow", session="S1")
        await manager.arm("slow", session="S2")
        with pytest.raises(ValueError, match="'slow' is already armed on S2"):
            await manager.arm("slow", session="S2")
        await manager.arm("mind")
        with pytest.raises(ValueError, match="'mind' is already armed on every session"):
            await manager.arm("mind")
        assert len(manager.live_runs()) == 4
        await manager.stop_run(1)
        await manager.arm("slow")                       # its place is free again

    @pytest.mark.asyncio
    async def test_priority_is_the_place_in_line_and_moves(self, kit):
        db, engine, stub, manager, clock = kit
        ask = _ask(engine)
        for name, body in (("a", "reject text: 'a'"), ("b", "accept"), ("c", "reject text: 'c'")):
            await manager.save(name, f"on order\n    {body}\n")
            await manager.arm(name)
        await manager.save("mind", MINDER)
        await manager.save("send", CLIENT)
        await manager.arm("mind")
        await manager.arm("send", session="S1")

        async def places():
            rows = await _fetch_all(db, "SELECT macro, priority FROM fix_macro_runs ORDER BY id")
            return {r["macro"]: r["priority"] for r in rows}

        assert await places() == {"a": 1, "b": 2, "c": 3, "mind": 1, "send": 0}, "each side has its own line"
        await ask("move_run", {"run_id": 2, "direction": "up"})
        assert await places() == {"a": 2, "b": 1, "c": 3, "mind": 1, "send": 0}
        await _order(engine, stub, manager)
        assert engine._as_sent(stub, stub.sent[-1]).get("150") == "0", "b is offered the order first now"
        for run_id, direction, why in ((2, "up", "already first"), (3, "down", "already last"),
                                       (5, "up", "only sends orders"), (2, "sideways", "up or down")):
            with pytest.raises(ValueError, match=why):
                await manager.move_run(run_id, direction)
        await manager.stop_run(2)
        assert await places() == {"a": 1, "b": 0, "c": 2, "mind": 1, "send": 0}
        with pytest.raises(ValueError, match="Run 2 is not live"):
            await manager.move_run(2, "up")


class TestTour:
    @pytest.mark.asyncio
    async def test_the_loopback_tour_in_one_step(self, kit, monkeypatch):
        db, engine, stub, manager, clock = kit
        from tests.test_macro_sending import LinkedSession
        cli, mkt = LinkedSession(engine, "LOOP-CLI"), LinkedSession(engine, "LOOP-MKT")
        cli.peer, mkt.peer = mkt, cli

        async def setup(port=None, start=True):
            engine.sessions.update({"LOOP-CLI": cli, "LOOP-MKT": mkt})
            return {"sessions": LOOPBACK, "port": 9880, "created": [], "started": []}
        monkeypatch.setattr(manager, "setup_loopback", setup)
        await manager.save("loopback-client", manager.example("loopback-client")["source"] + "# mine\n", "client")
        first = await _ask(engine)("run_loopback_tour", {})
        assert (first["venue_run"], first["client_run"]) == (1, 2)
        saved = {r["name"]: r for r in await _fetch_all(db, "SELECT * FROM fix_macros")}
        assert saved["loopback-venue"]["side"] == "market" and saved["loopback-client"]["source"].endswith("# mine\n")
        runs = await _fetch_all(db, "SELECT macro, session, side FROM fix_macro_runs ORDER BY id")
        assert [(r["macro"], r["session"], r["side"]) for r in runs] == [
            ("loopback-venue", "LOOP-MKT", "market"), ("loopback-client", "LOOP-CLI", "client")]
        again = await manager.run_tour()
        assert (again["venue_run"], again["client_run"]) == (1, 3), "the venue is left armed; the client runs again"
        await clock.advance(12)
        await manager.flush()
        rows = await _fetch_all(db, "SELECT * FROM fix_macro_runs WHERE side = 'client' ORDER BY id")
        assert [(r["orders"], r["passed"], r["failed"]) for r in rows] == [(5, 5, 0), (5, 5, 0)]

    @pytest.mark.asyncio
    async def test_a_session_that_never_logs_on_is_said(self, kit, monkeypatch):
        db, engine, stub, manager, clock = kit
        down = StubSession("LOOP-CLI")
        down.is_active = False

        async def setup(port=None, start=True):
            engine.sessions["LOOP-CLI"] = down
            return {"sessions": LOOPBACK, "port": 9880, "created": [], "started": []}
        monkeypatch.setattr(manager, "setup_loopback", setup)
        with pytest.raises(ValueError, match="LOOP-CLI did not log on within 0.1 s: is port 9880 free"):
            await manager.run_tour(timeout=0.1)
        assert manager.live_runs() == []


def _service_sql(name):
    import tomllib
    from pathlib import Path
    import mkfix
    toml = tomllib.loads((Path(mkfix.__file__).parent / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
    return toml["services"][name]["sql"]


async def _ask_sql(db, name, **params):
    cursor = await db.read_conn.execute(_service_sql(name), params)
    rows = [dict(r) for r in await cursor.fetchall()]
    await cursor.close()
    return rows


class TestBlotterControls:
    """Play…, Pause… and Stop… on the order blotters act on a side's runs —
    all of them, or the one picked from the list the dialog is given."""

    @pytest_asyncio.fixture
    async def three(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.save("other", "on order where symbol == 'ZZ'\n    accept\n")
        await manager.save("send", CLIENT)
        await manager.save("named", CLIENT.replace("run\n", "run on S1\n"))
        await manager.save("draft", "on order\n    acept\n")
        return kit

    @pytest.mark.asyncio
    async def test_play_offers_the_sides_clean_macros_and_says_which_need_a_session(self, three):
        db, engine, stub, manager, clock = three
        market = await _ask_sql(db, "macro_play_options", side="market")
        assert [(o["value"], o["label"]) for o in market] == [("macro:other", "other"), ("macro:slow", "slow")], \
            "a draft with problems is not offered"
        client = await _ask_sql(db, "macro_play_options", side="client")
        assert [(o["value"], o["label"]) for o in client] == [
            ("macro:named", "named"), ("needs:send", "send  (asks for a session)")]
        # ticked values are joined by commas, so a name never holds one; the kind mark is split off at the
        # first colon, so a name may hold colons (the recorder's suggestion does)
        from mkfix.macro.store import NAME
        assert not NAME.fullmatch("a,b") and NAME.fullmatch("a:b") and NAME.fullmatch("my macro-2.b")
        await manager.save("Market 2026-09-22 14:30:15", SLOW)
        stamped = await _ask_sql(db, "macro_play_options", side="market")
        assert ("macro:Market 2026-09-22 14:30:15", "Market 2026-09-22 14:30:15") in [(o["value"], o["label"]) for o in stamped]
        played = await _ask(engine)("play_macro", {"side": "market", "what": "macro:Market 2026-09-22 14:30:15,macro:other"})
        assert played["started"] == 2 and [r.macro.name for r in manager.live_runs("Market 2026-09-22 14:30:15")] == ["Market 2026-09-22 14:30:15"]

    @pytest.mark.asyncio
    async def test_play_starts_a_macro_or_resumes_what_is_paused(self, three):
        db, engine, stub, manager, clock = three
        ask = _ask(engine)
        started = await ask("play_macro", {"side": "market", "what": "macro:slow", "session": "S1", "speed": "2", "seed": "7"})
        assert (started["run_id"], started["seed"], started["side"], started["started"]) == (1, 7, "market", 1)
        await ask("play_macro", {"side": "market", "what": "macro:other"})
        await ask("play_macro", {"side": "client", "what": "needs:send", "session": "S1"})
        await manager.flush()
        assert len(stub.sent) == 1
        for data, why in (({"side": "client", "what": "macro:slow"}, "'slow' is a market macro"),
                          ({"side": "client", "what": "needs:send"}, "send: line 1: `run` names no session"),
                          ({"side": "market", "what": ""}, "Choose a macro to play"),
                          ({"side": "market", "what": "nonsense"}, "Choose a macro to play"),
                          ({"what": "macro:slow"}, "Say which side")):
            with pytest.raises(Exception, match=why):
                await ask("play_macro", data)
        assert await ask("play_macro", {"side": "market", "what": "resume:all"}) == {
            "ok": True, "resumed": 0, "started": 0, "run_ids": []}
        assert await ask("pause_runs", {"side": "market"}) == {"ok": True, "paused": 2}
        offered = await _ask_sql(db, "macro_play_options", side="market")
        assert [(o["value"], o["label"]) for o in offered[:2]] == [
            ("resume:1", "Resume run #1 · slow on S1"), ("resume:2", "Resume run #2 · other")], "what is paused comes first"
        assert all(o["value"].startswith(("macro:", "needs:")) for o in await _ask_sql(db, "macro_play_options", side="client")), \
            "the other side's pauses are not offered"
        assert (await ask("play_macro", {"side": "market", "what": "resume:2"}))["resumed"] == 1
        assert [r.paused for r in manager.live_runs() if r.side == "market"] == [True, False]
        with pytest.raises(ValueError, match="Run 2 is not a live market run that is paused"):
            await ask("play_macro", {"side": "market", "what": "resume:2"})
        assert (await ask("play_macro", {"side": "market", "what": "resume:all"}))["resumed"] == 1
        rows = await _fetch_all(db, "SELECT status FROM fix_macro_runs ORDER BY id")
        assert [r["status"] for r in rows] == ["armed", "armed", "armed"]

    @pytest.mark.asyncio
    async def test_several_ticked_together_are_played_whole_or_not_at_all(self, three):
        """The Play… checklist submits its ticks joined by commas."""
        db, engine, stub, manager, clock = three
        ask = _ask(engine)
        both = await ask("play_macro", {"side": "market", "what": "macro:other, macro:slow,macro:other", "session": "S1", "seed": "5"})
        assert (both["started"], both["run_ids"], both["resumed"]) == (2, [1, 2], 0) and "run_id" not in both
        rows = await _fetch_all(db, "SELECT macro, session, seed FROM fix_macro_runs ORDER BY id")
        assert [(r["macro"], r["session"], r["seed"]) for r in rows] == [("other", "S1", 5), ("slow", "S1", 5)]
        # one of the list cannot be started: none of it is
        engine.sessions["S2"] = StubSession("S2")
        for what, why in (("macro:other,macro:nope", "No macro named 'nope'"), ("macro:other,macro:draft", "'draft' has 1 problem"),
                          ("macro:other,macro:send", "'send' is a client macro"), ("macro:other,resume:1", "not a live market run that is paused")):
            with pytest.raises(ValueError, match=why):
                await ask("play_macro", {"side": "market", "what": what, "session": "S2"})
        assert len(manager.live_runs()) == 2, "nothing of a refused list was started"
        stub.is_active = False
        with pytest.raises(ValueError, match="named: `run` on S1: the session is not active"):
            await ask("play_macro", {"side": "client", "what": "macro:named,needs:send", "session": "S1"})
        assert len(manager.live_runs()) == 2 and stub.sent == []
        stub.is_active = True
        # a paused run resumed and a macro started in one go
        await ask("pause_runs", {"side": "market", "runs": "1"})
        mixed = await ask("play_macro", {"side": "market", "what": "resume:1,macro:slow", "session": "S2"})
        assert (mixed["resumed"], mixed["started"], mixed["run_id"]) == (1, 1, 3)
        assert [r.paused for r in manager.live_runs()] == [False, False, False]
        clients = await ask("play_macro", {"side": "client", "what": "macro:named,needs:send", "session": "S1"})
        await manager.flush()
        assert clients["started"] == 2 and len(stub.sent) == 2

    @pytest.mark.asyncio
    async def test_pause_and_stop_take_one_run_or_the_whole_side(self, three):
        db, engine, stub, manager, clock = three
        ask = _ask(engine)
        for what, side in (("macro:slow", "market"), ("macro:other", "market"), ("macro:send", "client")):
            await ask("play_macro", {"side": side, "what": what, "session": "S1"})
        listed = await _ask_sql(db, "macro_run_options", side="market", paused=0)
        assert [(o["value"], o["label"]) for o in listed] == [
            ("1", "#1 · slow on S1 · playing · 0 orders"), ("2", "#2 · other on S1 · playing · 0 orders")]
        assert await ask("pause_runs", {"side": "market", "run": "2"}) == {"ok": True, "paused": 1}
        assert [o["value"] for o in await _ask_sql(db, "macro_run_options", side="market", paused=0)] == ["1"]
        both = await _ask_sql(db, "macro_run_options", side="market", paused=1)
        assert [o["label"] for o in both] == ["#1 · slow on S1 · playing · 0 orders", "#2 · other on S1 · paused · 0 orders"]
        for command, data, why in (("pause_runs", {"side": "market", "run": "2"}, "not a live market run that is playing"),
                                   ("pause_runs", {"side": "market", "run": "3"}, "Run 3 is not a live market run"),
                                   ("stop_runs", {"side": "client", "run": "1"}, "Run 1 is not a live client run"),
                                   ("stop_runs", {"side": "sideways"}, "client side or the market side")):
            with pytest.raises(ValueError, match=why):
                await ask(command, data)
        # the dialogs' checklist submits the ticked runs together, and they are taken whole or not at all
        engine.sessions["S2"] = StubSession("S2")
        await ask("play_macro", {"side": "market", "what": "macro:other", "session": "S2"})
        await ask("play_macro", {"side": "market", "what": "macro:slow", "session": "S2"})
        with pytest.raises(ValueError, match="Run 3 is not a live market run"):
            await ask("stop_runs", {"side": "market", "runs": "4,3,5"})
        with pytest.raises(ValueError, match="Run x is not a live market run"):
            await ask("stop_runs", {"side": "market", "runs": "4,x"})
        assert len([r for r in manager.live_runs() if r.side == "market"]) == 4, "nothing was stopped by a list with a wrong number in it"
        assert await ask("pause_runs", {"side": "market", "runs": "4, 5,4"}) == {"ok": True, "paused": 2}
        assert await ask("stop_runs", {"side": "market", "runs": "5,4"}) == {"ok": True, "stopped": 2}
        assert await ask("stop_runs", {"side": "market", "run": "2"}) == {"ok": True, "stopped": 1}
        assert await ask("stop_runs", {"side": "market"}) == {"ok": True, "stopped": 1}
        assert await ask("stop_runs", {"side": "market"}) == {"ok": True, "stopped": 0}
        assert [(r.macro.name, r.side) for r in manager.live_runs()] == [("send", "client")], "the other side plays on"
        assert await _ask_sql(db, "macro_run_options", side="market", paused=1) == []

    @pytest.mark.asyncio
    async def test_needs_session_follows_the_macro_as_it_is_saved(self, three):
        db, engine, stub, manager, clock = three
        rows = {r["name"]: r["needs_session"] for r in await _fetch_all(db, "SELECT name, needs_session FROM fix_macros")}
        assert rows == {"slow": 0, "other": 0, "send": 1, "named": 0, "draft": 0}
        await manager.save("send", CLIENT.replace("run\n", "run on S1\n"))
        assert (await manager.load("send"))["needs_session"] == 0


class TestCommands:
    @pytest.mark.asyncio
    async def test_the_ui_drives_it_all_through_fix_cmd(self, kit):
        db, engine, stub, manager, clock = kit
        svc = FixCommandService(config={}, db=MagicMock(), change_bus=MagicMock(), writer=MagicMock())
        svc.set_engine(engine)
        ask = svc._dispatch
        vocab = (await ask("macro_vocab", {}))["vocabulary"]
        assert "fill" in vocab["verbs"] and "TICK" in vocab["functions"]
        assert any(e["name"] == "slow-fill" for e in (await ask("list_examples", {}))["examples"])
        source = (await ask("get_example", {"name": "auto-ack"}))["source"]
        assert (await ask("check_macro", {"source": source}))["errors"] == 0
        assert (await ask("check_macro", {"source": "on order\n    acept\n"}))["errors"] == 1
        assert (await ask("save_macro", {"name": "auto-ack", "source": source}))["ok"]
        run_id = (await ask("arm_macro", {"name": "auto-ack", "speed": "2", "seed": ""}))["run_id"]
        await _order(engine, stub, manager)
        (inst,) = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
        assert await ask("pause_macro", {"name": "auto-ack"}) == {"ok": True, "paused": 1}
        assert await ask("resume_macro", {"name": "auto-ack"}) == {"ok": True, "resumed": 1}
        for command, data in [("pause_run", {"run_id": run_id}), ("resume_run", {"run_id": str(run_id)}),
                              ("detach_order", {"order_row": inst["order_row"]}), ("stop_run", {"run_id": run_id})]:
            assert await ask(command, data) == {"ok": True}, command
        assert await ask("delete_macro", {"name": "auto-ack"}) == {"ok": True, "deleted": 1}
        with pytest.raises(ValueError, match="No macro named"):
            await ask("arm_macro", {"name": "auto-ack"})
        with pytest.raises(ValueError, match="No macro named 'auto-ack'"):
            await ask("delete_macro", {"names": ["auto-ack"]})
        # the editor sends a list; a comma-joined string (a name holds no comma) is taken too
        for name in ("x", "y"):
            await ask("save_macro", {"name": name, "source": source})
        assert await ask("delete_macro", {"names": "x, y"}) == {"ok": True, "deleted": 2}
        assert await _fetch_all(db, "SELECT name FROM fix_macros") == []


class TestSendingRuns:
    @pytest.mark.asyncio
    async def test_a_run_that_only_sends_is_mirrored_and_ends_itself(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("one", "run on S1\n    repeat 2 every 1s\n"
                                  "        new symbol: 'IBM', side: buy, qty: 10 * (n + 1), price: 5\n"
                                  "        after 1s\n        pass 'sent ${order.cl_ord_id}'\n")
        run_id = (await manager.arm("one"))["run_id"]
        await manager.flush()
        rows = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
        assert [(r["status"], r["symbol"]) for r in rows] == [("running", "IBM")], "a script is a row once it has its order"
        await clock.advance(3)
        await manager.flush()
        rows = await _fetch_all(db, "SELECT * FROM fix_macro_orders ORDER BY id")
        assert [r["status"] for r in rows] == ["passed", "passed"] and rows[0]["message"].startswith("sent RT")
        (run,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert (run["status"], run["verdict"], run["orders"], run["live"], run["passed"]) == ("finished", "passed", 2, 0, 2)
        assert run["ended_at"]
        orders = await _fetch_all(db, "SELECT macro, order_qty FROM fix_orders ORDER BY id")
        assert [(o["macro"], o["order_qty"]) for o in orders] == [(f"one #{run_id}", 10.0), (f"one #{run_id}", 20.0)]
        await manager.stop_run(run_id)
        assert (await _fetch_all(db, "SELECT status FROM fix_macro_runs"))[0]["status"] == "finished"
        await manager.delete("one")

    @pytest.mark.asyncio
    async def test_a_script_that_fails_in_its_first_instant_is_recorded(self, kit):
        """Found in use: a `run on` script was started seven seconds before its
        session logged on. `new` was refused at once, the run — sending only —
        ended at once, and because the scripts were started before the run's
        row existed, none of it was written: the row said `armed` for ever,
        no log, no script row, and Stop answered "not live"."""
        db, engine, stub, manager, clock = kit
        async def refuse(msg):
            raise ConnectionError("socket closed")
        stub.send_message = refuse
        await manager.save("one", "run on S1\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n    pass\n")
        run_id = (await manager.arm("one"))["run_id"]
        await manager.flush()
        (run,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert (run["status"], run["verdict"], run["failed"], run["live"]) == ("finished", "failed", 1, 0) and run["ended_at"]
        (script,) = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert (order["status"], order["text"]) == ("Rejected", "Send failed: socket closed")
        assert (script["status"], script["order_row"], script["session_id"], script["line"]) == ("failed", order["id"], "S1", 2)
        assert "`new` was refused: socket closed" in script["message"]
        log = [r["text"] for r in await _fetch_all(db, "SELECT text FROM fix_macro_log")]
        assert any("`new` was refused" in line for line in log)
        await manager.stop_run(run_id)
        assert (await _fetch_all(db, "SELECT status FROM fix_macro_runs"))[0]["status"] == "finished"

    @pytest.mark.asyncio
    async def test_a_script_that_never_got_an_order_is_still_a_row(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("one", "run on S1\n    after 1s\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n")
        await manager.arm("one")
        stub.is_active = False                                 # the session drops while the script waits
        await clock.advance(2)
        await manager.flush()
        (script,) = await _fetch_all(db, "SELECT * FROM fix_macro_orders")
        assert (script["status"], script["order_row"], script["cl_ord_id"], script["session_id"]) == ("failed", -1, "", "S1")
        assert "Session S1 is not active" in script["message"] and await _fetch_all(db, "SELECT * FROM fix_orders") == []
        (run,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert (run["status"], run["verdict"], run["failed"]) == ("finished", "failed", 1)

    @pytest.mark.asyncio
    async def test_a_sending_script_is_not_started_on_a_session_that_is_down(self, kit):
        db, engine, stub, manager, clock = kit
        stub.is_active = False
        await manager.save("one", "run on S1\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n")
        with pytest.raises(Exception, match="`run` on S1: the session is not active"):
            await manager.arm("one")
        assert await _fetch_all(db, "SELECT * FROM fix_macro_runs") == [] and manager.live_runs() == []

    @pytest.mark.asyncio
    async def test_stop_tidies_a_row_an_earlier_process_left(self, kit):
        db, engine, stub, manager, clock = kit
        await manager._write("insert_run", ("ghost", "client", 1, "", 1, 1.0, "armed", "20260920-17:26:35.643", None))
        await manager.stop_run(1)
        (run,) = await _fetch_all(db, "SELECT * FROM fix_macro_runs")
        assert run["status"] == "interrupted" and run["ended_at"]

    @pytest.mark.asyncio
    async def test_a_restart_never_sends_again(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("both", "on sent order\n    wait filled\nrun on S1\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n    wait filled\n")
        await manager.arm("both")
        await manager.flush()
        assert len(stub.sent) == 1
        await manager.stop()
        manager2, _ = await _manager(engine)
        try:
            await manager2.flush()
            assert [r["status"] for r in await _fetch_all(db, "SELECT status FROM fix_macro_runs")] == ["interrupted"]
            assert manager2.live_runs() == [] and len(stub.sent) == 1, "waiting again is harmless; sending again is not"
        finally:
            await manager2.stop()

    @pytest.mark.asyncio
    async def test_run_on_a_session_that_is_not_there(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("lost", "run on NOWHERE\n    new symbol: 'A', side: buy, qty: 1\n")
        with pytest.raises(ValueError, match="'lost' has 1 problem.*No session named 'NOWHERE'"):
            await manager.arm("lost")
        assert await _fetch_all(db, "SELECT * FROM fix_macro_runs") == []

    @pytest.mark.asyncio
    async def test_setup_loopback_creates_the_pair_once(self, kit):
        db, engine, stub, manager, clock = kit
        first = await manager.setup_loopback(port=19999, start=False)
        assert first["created"] == ["LOOP-MKT", "LOOP-CLI"] and first["port"] == 19999
        rows = {r["session_id"]: r for r in await _fetch_all(db, "SELECT * FROM fix_sessions")}
        assert (rows["LOOP-MKT"]["host"], rows["LOOP-CLI"]["host"]) == ("", "127.0.0.1")
        assert rows["LOOP-MKT"]["sender_comp_id"] == rows["LOOP-CLI"]["target_comp_id"] == "LOOPMKT"
        assert rows["LOOP-MKT"]["port"] == rows["LOOP-CLI"]["port"] == 19999
        assert {s["session_id"] for s in await _fetch_all(db, "SELECT * FROM fix_session_state")} == {"LOOP-MKT", "LOOP-CLI"}
        again = await manager.setup_loopback(port=12345, start=False)
        assert again["created"] == [] and again["port"] == 19999, "the pair that exists keeps its port"
