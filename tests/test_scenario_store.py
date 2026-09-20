"""Scenarios in the database: saving, arming, the runner's shadow rows,
restart recovery, the archive guard, and the fix_cmd commands."""

from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from mkfix.fix.message import parse_fix
from mkfix.scenario.clock import Scheduler, VirtualClock
from mkfix.scenario.runner import ScenarioRunner
from mkfix.scenario.store import ScenarioManager, example_header
from mkfix.services.fix_command import FixCommandService

from tests.test_engine import StubSession, _fetch_all, stack  # noqa: F401

SLOW = """scenario slow
on order
    accept
    log 'working ${order.cl_ord_id}'
    while order.leaves_qty > 0
        after 1s
        fill qty: 50, price: order.price
    pass 'done'
"""


async def _manager(engine):
    clock = VirtualClock(Scheduler())
    manager = ScenarioManager(engine, ScenarioRunner(engine, clock))
    engine.scenarios = manager
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


async def _order(engine, stub, manager, cl="C1", qty=100):
    await engine.on_app_message(stub, "D", parse_fix(f"8=FIX.4.2|35=D|11={cl}|55=AAPL|54=1|38={qty}|40=2|44=10|59=0"))
    await manager.flush()


class TestScripts:
    @pytest.mark.asyncio
    async def test_save_check_load_delete(self, kit):
        db, engine, stub, manager, clock = kit
        result = await manager.save("slow", SLOW)
        assert (result["name"], result["errors"], result["diagnostics"]) == ("slow", 0, [])
        assert result["blocks"] == [{"kind": "market", "line": 2, "session": ""}]
        assert (await manager.load("slow"))["source"] == SLOW
        await manager.save("slow", SLOW.replace("50", "25"))
        versions = await _fetch_all(db, "SELECT _mkio_version FROM fix_scenarios__history ORDER BY _mkio_version")
        assert [v["_mkio_version"] for v in versions] == [1, 2], "every Save is kept"
        await manager.delete("slow")
        with pytest.raises(ValueError, match="No scenario named 'slow'"):
            await manager.load("slow")

    @pytest.mark.asyncio
    async def test_a_draft_with_problems_is_kept_and_counted(self, kit):
        db, engine, stub, manager, clock = kit
        result = await manager.save("draft", "scenario draft\non order\n    acept\n    fill qty: 1\n")
        assert result["errors"] == 2
        assert [(d["line"], d["col"], d["severity"]) for d in result["diagnostics"]] == [(3, 4, "error"), (4, 4, "error")]
        assert (await manager.load("draft"))["problems"] == 2
        with pytest.raises(ValueError, match="'draft' has 2 problem\\(s\\); the first: line 3"):
            await manager.arm("draft")

    @pytest.mark.asyncio
    async def test_the_name_and_the_script_must_agree(self, kit):
        db, engine, stub, manager, clock = kit
        with pytest.raises(ValueError, match="calls itself 'slow'; it is being saved as 'fast'"):
            await manager.save("fast", SLOW)
        with pytest.raises(ValueError, match="needs a name"):
            await manager.save("  ", SLOW)

    @pytest.mark.asyncio
    async def test_check_knows_the_sessions_and_templates_that_exist(self, kit):
        db, engine, stub, manager, clock = kit
        await engine.save_template("fill", "half", qty="50")
        text = "scenario t\non order\n    fill using 'half'\n    fill using 'hafl'\n"
        found = (await manager.check(text))["diagnostics"]
        assert [(d["line"], d["message"]) for d in found] == [(4, "No fill template named 'hafl' — did you mean 'half'?")]

    def test_examples_are_listed_by_their_headers(self):
        manager = ScenarioManager(MagicMock(), MagicMock())
        listed = {e["name"]: e for e in manager.examples()}
        assert {"auto-ack", "slow-fill", "cancel-replace-desk", "dispute-desk"} <= set(listed)
        assert all(set(e) == {"name", "title", "shows", "needs", "watch", "outcome"} for e in listed.values())
        assert listed["auto-ack"]["title"] == "Auto-acknowledge" and "250 ms" in listed["auto-ack"]["outcome"]
        one = manager.example("slow-fill")
        assert one["source"].startswith("# Slow fill\n") and "while order.leaves_qty > 0" in one["source"]
        for bad in ("nope", "../store", "/etc/passwd"):
            with pytest.raises(ValueError, match="No example named"):
                manager.example(bad)

    def test_a_header_line_may_wrap(self):
        head = example_header("# T\n#\n# Shows:   one\n#          two\n# Needs:   n\n\nscenario t\n")
        assert head == {"title": "T", "shows": "one two", "needs": "n"}


class TestRuns:
    @pytest.mark.asyncio
    async def test_arming_writes_the_run_with_what_it_ran(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.save("slow", SLOW + "\n# edited\n")
        armed = await manager.arm("slow", session="S1", seed=11, speed=2)
        (row,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert (row["id"], row["scenario"], row["version"], row["session"], row["seed"], row["speed"], row["status"]) == (
            armed["run_id"], "slow", 2, "S1", 11, 2.0, "armed")
        for refused, why in [(("slow",), "already armed"), (("nope",), "No scenario named")]:
            with pytest.raises(ValueError, match=why):
                await manager.arm(*refused)
        with pytest.raises(ValueError, match="is armed: stop its run first"):
            await manager.delete("slow")

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
        (inst,) = await _fetch_all(db, "SELECT * FROM fix_scenario_instances")
        assert (inst["run_id"], inst["scenario"], inst["cl_ord_id"], inst["symbol"], inst["block_line"]) == (
            run_id, "slow", "C1", "AAPL", 2)
        assert (inst["status"], inst["line"], inst["waiting_for"]) == ("running", 6, "after 1s")
        order = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert order["scenario"] == f"slow #{run_id}" and inst["order_row"] == order["id"]
        assert [(l["line"], l["text"], l["cl_ord_id"]) for l in await _fetch_all(db, "SELECT * FROM fix_scenario_log")] == [
            (4, "working C1", "C1")]
        (run,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert (run["orders"], run["live"], run["passed"], run["verdict"]) == (1, 1, 0, "")

        await clock.advance(2)
        await manager.flush()
        (inst,) = await _fetch_all(db, "SELECT * FROM fix_scenario_instances")
        assert (inst["status"], inst["message"], inst["actions"]) == ("passed", "done", 3)
        (run,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert (run["orders"], run["live"], run["passed"], run["failed"], run["verdict"], run["status"]) == (
            1, 0, 1, 0, "passed", "armed")
        assert (await _fetch_all(db, "SELECT text FROM fix_scenario_log"))[-1]["text"] == "passed: done"

    @pytest.mark.asyncio
    async def test_the_tag_survives_the_orders_own_writes(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.arm("slow")
        await _order(engine, stub, manager)
        await clock.advance(1)
        await manager.flush()
        order = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert order["cum_qty"] == 50.0 and order["scenario"].startswith("slow #")

    @pytest.mark.asyncio
    async def test_pause_resume_stop_detach(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        run_id = (await manager.arm("slow"))["run_id"]
        await _order(engine, stub, manager, "A", 1000)
        await _order(engine, stub, manager, "B", 1000)
        status = lambda: _fetch_all(db, "SELECT status, live FROM fix_scenario_runs")
        await manager.pause_run(run_id)
        assert (await status())[0]["status"] == "paused"
        await manager.resume_run(run_id)
        assert (await status())[0]["status"] == "armed"
        b = (await _fetch_all(db, "SELECT * FROM fix_scenario_instances WHERE cl_ord_id = 'B'"))[0]
        await manager.detach(b["order_row"])
        with pytest.raises(ValueError, match="No live script owns"):
            await manager.detach(b["order_row"])
        await manager.stop_run(run_id)
        rows = await _fetch_all(db, "SELECT cl_ord_id, status FROM fix_scenario_instances ORDER BY cl_ord_id")
        assert [(r["cl_ord_id"], r["status"]) for r in rows] == [("A", "stopped"), ("B", "detached")]
        (run,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert (run["status"], run["live"]) == ("stopped", 0) and run["ended_at"]
        await manager.stop_run(run_id)                         # Stop on a stopped run changes nothing, and says nothing
        assert (await _fetch_all(db, "SELECT status FROM fix_scenario_runs"))[0]["status"] == "stopped"
        with pytest.raises(ValueError, match="No run 99"):
            await manager.stop_run(99)
        await manager.delete("slow")

    @pytest.mark.asyncio
    async def test_a_restart_interrupts_what_was_live_and_arms_again(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        first = (await manager.arm("slow", seed=5))["run_id"]
        await _order(engine, stub, manager, qty=1000)
        await manager.stop()                                   # the process goes away without a word

        manager2, clock2 = await _manager(engine)
        try:
            runs = await _fetch_all(db, "SELECT * FROM fix_scenario_runs ORDER BY id")
            assert [(r["id"], r["status"], r["seed"]) for r in runs] == [(first, "interrupted", 5), (first + 1, "armed", 5)]
            (old,) = await _fetch_all(db, "SELECT * FROM fix_scenario_instances")
            assert (old["status"], old["message"]) == ("interrupted", "the server stopped")
            await _order(engine, stub, manager2, "C2")
            rows = await _fetch_all(db, "SELECT run_id, cl_ord_id, status FROM fix_scenario_instances ORDER BY id")
            assert [(r["run_id"], r["cl_ord_id"]) for r in rows] == [(first, "C1"), (first + 1, "C2")]
        finally:
            await manager2.stop()

    @pytest.mark.asyncio
    async def test_a_scenario_that_no_longer_checks_is_not_re_armed(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        await manager.arm("slow")
        await manager.stop()
        await manager._write("upsert_scenario", ("slow", "scenario slow\non order\n    nonsense\n", 1, "", "", None))
        manager2, _ = await _manager(engine)
        try:
            assert [r["status"] for r in await _fetch_all(db, "SELECT status FROM fix_scenario_runs")] == ["interrupted"]
            assert manager2.live_runs() == []
        finally:
            await manager2.stop()

    @pytest.mark.asyncio
    async def test_an_archive_is_refused_while_a_run_is_armed(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("slow", SLOW)
        run_id = (await manager.arm("slow"))["run_id"]
        with pytest.raises(ValueError, match="scenario slow is armed"):
            await engine.check_archive({"fix_orders": [{"id": 1}]})
        await engine.check_archive({"fix_messages": [{"id": 1}]})
        await manager.stop_run(run_id)
        await engine.check_archive({"fix_orders": [{"id": 1}], "fix_scenario_runs": [{"id": run_id}]})


class TestCommands:
    @pytest.mark.asyncio
    async def test_the_ui_drives_it_all_through_fix_cmd(self, kit):
        db, engine, stub, manager, clock = kit
        svc = FixCommandService(config={}, db=MagicMock(), change_bus=MagicMock(), writer=MagicMock())
        svc.set_engine(engine)
        ask = svc._dispatch
        vocab = (await ask("scenario_vocab", {}))["vocabulary"]
        assert "fill" in vocab["verbs"] and "TICK" in vocab["functions"]
        assert any(e["name"] == "slow-fill" for e in (await ask("list_examples", {}))["examples"])
        source = (await ask("get_example", {"name": "auto-ack"}))["source"]
        assert (await ask("check_scenario", {"source": source}))["errors"] == 0
        assert (await ask("check_scenario", {"source": "scenario x\non order\n    acept\n"}))["errors"] == 1
        assert (await ask("save_scenario", {"name": "auto-ack", "source": source}))["ok"]
        run_id = (await ask("arm_scenario", {"name": "auto-ack", "speed": "2", "seed": ""}))["run_id"]
        await _order(engine, stub, manager)
        (inst,) = await _fetch_all(db, "SELECT * FROM fix_scenario_instances")
        for command, data in [("pause_run", {"run_id": run_id}), ("resume_run", {"run_id": str(run_id)}),
                              ("detach_instance", {"order_row": inst["order_row"]}), ("stop_run", {"run_id": run_id}),
                              ("delete_scenario", {"name": "auto-ack"})]:
            assert await ask(command, data) == {"ok": True}, command
        with pytest.raises(ValueError, match="No scenario named"):
            await ask("arm_scenario", {"name": "auto-ack"})


class TestSendingRuns:
    @pytest.mark.asyncio
    async def test_a_run_that_only_sends_is_mirrored_and_ends_itself(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("one", "scenario one\nrun on S1\n    repeat 2 every 1s\n"
                                  "        new symbol: 'IBM', side: buy, qty: 10 * (n + 1), price: 5\n"
                                  "        after 1s\n        pass 'sent ${order.cl_ord_id}'\n")
        run_id = (await manager.arm("one"))["run_id"]
        await manager.flush()
        rows = await _fetch_all(db, "SELECT * FROM fix_scenario_instances")
        assert [(r["status"], r["symbol"]) for r in rows] == [("running", "IBM")], "a script is a row once it has its order"
        await clock.advance(3)
        await manager.flush()
        rows = await _fetch_all(db, "SELECT * FROM fix_scenario_instances ORDER BY id")
        assert [r["status"] for r in rows] == ["passed", "passed"] and rows[0]["message"].startswith("sent RT")
        (run,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert (run["status"], run["verdict"], run["orders"], run["live"], run["passed"]) == ("finished", "passed", 2, 0, 2)
        assert run["ended_at"]
        orders = await _fetch_all(db, "SELECT scenario, order_qty FROM fix_orders ORDER BY id")
        assert [(o["scenario"], o["order_qty"]) for o in orders] == [(f"one #{run_id}", 10.0), (f"one #{run_id}", 20.0)]
        await manager.stop_run(run_id)
        assert (await _fetch_all(db, "SELECT status FROM fix_scenario_runs"))[0]["status"] == "finished"
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
        await manager.save("one", "scenario one\nrun on S1\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n    pass\n")
        run_id = (await manager.arm("one"))["run_id"]
        await manager.flush()
        (run,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert (run["status"], run["verdict"], run["failed"], run["live"]) == ("finished", "failed", 1, 0) and run["ended_at"]
        (script,) = await _fetch_all(db, "SELECT * FROM fix_scenario_instances")
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert (order["status"], order["text"]) == ("Rejected", "Send failed: socket closed")
        assert (script["status"], script["order_row"], script["session_id"], script["line"]) == ("failed", order["id"], "S1", 3)
        assert "`new` was refused: socket closed" in script["message"]
        log = [r["text"] for r in await _fetch_all(db, "SELECT text FROM fix_scenario_log")]
        assert any("`new` was refused" in line for line in log)
        await manager.stop_run(run_id)
        assert (await _fetch_all(db, "SELECT status FROM fix_scenario_runs"))[0]["status"] == "finished"

    @pytest.mark.asyncio
    async def test_a_script_that_never_got_an_order_is_still_a_row(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("one", "scenario one\nrun on S1\n    after 1s\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n")
        await manager.arm("one")
        stub.is_active = False                                 # the session drops while the script waits
        await clock.advance(2)
        await manager.flush()
        (script,) = await _fetch_all(db, "SELECT * FROM fix_scenario_instances")
        assert (script["status"], script["order_row"], script["cl_ord_id"], script["session_id"]) == ("failed", -1, "", "S1")
        assert "Session S1 is not active" in script["message"] and await _fetch_all(db, "SELECT * FROM fix_orders") == []
        (run,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert (run["status"], run["verdict"], run["failed"]) == ("finished", "failed", 1)

    @pytest.mark.asyncio
    async def test_a_sending_script_is_not_started_on_a_session_that_is_down(self, kit):
        db, engine, stub, manager, clock = kit
        stub.is_active = False
        await manager.save("one", "scenario one\nrun on S1\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n")
        with pytest.raises(Exception, match="`run on S1`: the session is not active"):
            await manager.arm("one")
        assert await _fetch_all(db, "SELECT * FROM fix_scenario_runs") == [] and manager.live_runs() == []

    @pytest.mark.asyncio
    async def test_stop_tidies_a_row_an_earlier_process_left(self, kit):
        db, engine, stub, manager, clock = kit
        await manager._write("insert_run", ("ghost", 1, "", 1, 1.0, "armed", "20260920-17:26:35.643", None))
        await manager.stop_run(1)
        (run,) = await _fetch_all(db, "SELECT * FROM fix_scenario_runs")
        assert run["status"] == "interrupted" and run["ended_at"]

    @pytest.mark.asyncio
    async def test_a_restart_never_sends_again(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("both", "scenario both\non order\n    accept\nrun on S1\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n    wait filled\n")
        await manager.arm("both")
        await manager.flush()
        assert len(stub.sent) == 1
        await manager.stop()
        manager2, _ = await _manager(engine)
        try:
            await manager2.flush()
            assert [r["status"] for r in await _fetch_all(db, "SELECT status FROM fix_scenario_runs")] == ["interrupted"]
            assert manager2.live_runs() == [] and len(stub.sent) == 1, "waiting again is harmless; sending again is not"
        finally:
            await manager2.stop()

    @pytest.mark.asyncio
    async def test_run_on_a_session_that_is_not_there(self, kit):
        db, engine, stub, manager, clock = kit
        await manager.save("lost", "scenario lost\nrun on NOWHERE\n    new symbol: 'A', side: buy, qty: 1\n")
        with pytest.raises(ValueError, match="'lost' has 1 problem.*No session named 'NOWHERE'"):
            await manager.arm("lost")
        assert await _fetch_all(db, "SELECT * FROM fix_scenario_runs") == []

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
