"""Instruments on IOIs, adverts, allocations, RFQs and quotes (0.76): the
instrument each family message carries, per version, both sides' rows, and
what an order answering one takes from it. Run over the linked pair of
tests/test_instruments.py."""

import pytest

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessageFactory

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_instruments import FUTURE, FUTURES_OPTION, OPTION, linked  # noqa: F401

CALL = {"security_type": "OPT", "maturity": "20261218", "strike_price": "250", "put_or_call": "Call"}
PUT = {**CALL, "strike_price": "240", "put_or_call": "Put"}


async def _rows(db, table, where=""):
    return await _fetch_all(db, f"SELECT * FROM {table} {where} ORDER BY id")


def _by_direction(rows):
    return {r["direction"]: r for r in rows}


class TestIoisAdvertsAllocations:
    @pytest.mark.asyncio
    async def test_an_ioi_names_its_instrument_and_a_replace_keeps_it(self, linked):
        db, engine, cli, mkt = linked
        ioi_id = await engine.send_ioi("Server", "ES", "1", "100", instrument=FUTURE)
        assert "|167=FUT|200=202612|" in mkt.wire[-1]
        rows = _by_direction(await _rows(db, "fix_iois"))
        assert {(r["security_type"], r["maturity"], r["instrument"]) for r in rows.values()} == {
            ("FUT", "202612", "ES Dec26")}
        assert rows["RX"]["extra_tags"] == "", "the instrument is consumed, not echoed as extras"
        new_id = await engine.replace_ioi("Server", ioi_id, "ES", "1", "200")
        assert "|167=FUT|200=202612|" in mkt.wire[-1], "a replace carries the IOI's instrument"
        await engine.cancel_ioi("Server", new_id)
        assert "|28=C|" in mkt.wire[-1] and "|167=FUT|" in mkt.wire[-1]
        assert {r["instrument"] for r in await _rows(db, "fix_iois")} == {"ES Dec26"}

    @pytest.mark.asyncio
    async def test_an_order_answering_an_ioi_is_in_its_instrument(self, linked):
        db, engine, cli, mkt = linked
        ioi_id = await engine.send_ioi("Server", "AAPL", "2", "10", instrument=CALL)
        await engine.send_new_order("Client", "AAPL", "1", 10, price=4, extra_tags=f"23={ioi_id}")
        assert "|167=OPT|200=20261218|202=250|201=1|" in cli.wire[-1]
        orders = _by_direction(await _rows(db, "fix_orders"))
        assert orders["TX"]["instrument"] == orders["RX"]["instrument"] == "AAPL 18Dec26 250 C"
        assert orders["TX"]["ioi_id"] == ioi_id

    @pytest.mark.asyncio
    async def test_an_order_of_its_own_instrument_keeps_it(self, linked):
        db, engine, cli, mkt = linked
        ioi_id = await engine.send_ioi("Server", "AAPL", "2", "10", instrument=CALL)
        await engine.send_new_order("Client", "AAPL", "1", 10, price=4, extra_tags=f"23={ioi_id}", instrument=PUT)
        assert {o["instrument"] for o in await _rows(db, "fix_orders")} == {"AAPL 18Dec26 240 P"}

    @pytest.mark.asyncio
    async def test_an_advert(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_advert("Server", "ES", "B", 5, price=5200, instrument=FUTURE)
        assert "|167=FUT|200=202612|231=50|207=XCME|" in mkt.wire[-1]
        assert {r["instrument"] for r in await _rows(db, "fix_adverts")} == {"ES Dec26"}

    @pytest.mark.asyncio
    async def test_an_allocation_is_in_its_orders_instrument(self, linked):
        db, engine, cli, mkt = linked
        cl = await engine.send_new_order("Client", "ES", "1", 2, price=5200, instrument={**FUTURE, "open_close": "O"})
        await engine.accept_order("Server", cl)
        await engine.fill_order("Server", cl, 2, 5200)
        await engine.send_allocation("Server", "ES", "1", 2, 5200, orders=cl, allocs="ACCT1 2")
        assert "|167=FUT|200=202612|" in mkt.wire[-1] and "|77=" not in mkt.wire[-1], \
            "the order's instrument, not its Open/Close"
        rows = _by_direction(await _rows(db, "fix_allocations"))
        assert rows["TX"]["instrument"] == rows["RX"]["instrument"] == "ES Dec26"

    @pytest.mark.asyncio
    async def test_a_received_replace_moves_the_instrument_when_accepted(self, linked):
        db, engine, cli, mkt = linked
        alloc_id = await engine.send_allocation("Server", "ES", "1", 2, 5200, allocs="ACCT1 2", instrument=FUTURE)
        await engine.accept_allocation("Client", alloc_id)
        # A replace naming another contract: the counterparty's word, parked until accepted.
        await engine.replace_allocation("Server", alloc_id, "ES", "1", 2, 5200, allocs="ACCT1 2",
                                        extra_tags="200=202703")
        received = _by_direction(await _rows(db, "fix_allocations"))["RX"]
        assert received["maturity"] == "202612" and received["pending_action"] == "Replace"
        await engine.accept_allocation("Client", received["alloc_id"])
        assert _by_direction(await _rows(db, "fix_allocations"))["RX"]["maturity"] == "202703"

    @pytest.mark.asyncio
    async def test_option_on_future_is_refused_before_an_id(self, linked):
        db, engine, cli, mkt = linked
        with pytest.raises(ValueError, match="OOF is not defined by FIX.4.4"):
            await engine.send_ioi("Server", "ES", "1", "1", instrument=FUTURES_OPTION)
        assert mkt.wire == [] and await _rows(db, "fix_iois") == []


class TestRfqsAndQuotes:
    @pytest.mark.asyncio
    async def test_an_rfq_names_its_instrument_in_the_group(self, linked):
        db, engine, cli, mkt = linked
        req = await engine.send_rfq("Client", "AAPL", side="1", qty=10, instrument=CALL)
        assert "|146=1|55=AAPL|167=OPT|200=20261218|202=250|201=1|" in cli.wire[-1]
        rfqs = _by_direction(await _rows(db, "fix_rfqs"))
        assert rfqs["TX"]["instrument"] == rfqs["RX"]["instrument"] == "AAPL 18Dec26 250 C"
        await engine.quote_rfq("Server", req, bid_px=4.1, offer_px=4.3, offer_size=10)
        assert "|35=S|" in mkt.wire[-1] and "|167=OPT|" in mkt.wire[-1], "the quote names it too"
        cl = await engine.hit_quote("Client", _by_direction(await _rows(db, "fix_rfqs"))["TX"]["quote_id"])
        assert "|35=AJ|" in cli.wire[-1] and "|167=OPT|" in cli.wire[-1]
        orders = _by_direction(await _rows(db, "fix_orders"))
        assert orders["TX"]["cl_ord_id"] == orders["RX"]["cl_ord_id"] == cl
        assert orders["TX"]["instrument"] == orders["RX"]["instrument"] == "AAPL 18Dec26 250 C"

    @pytest.mark.asyncio
    async def test_a_reject_names_the_instrument_in_its_group(self, linked):
        db, engine, cli, mkt = linked
        req = await engine.send_rfq("Client", "AAPL", instrument=CALL)
        await engine.reject_rfq("Server", req, reason="1")
        assert "|35=AG|" in mkt.wire[-1] and "|146=1|55=AAPL|167=OPT|" in mkt.wire[-1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", ["FIX.4.1"], indirect=True)
    async def test_a_flat_request_carries_it_in_the_body(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_rfq("Client", "AAPL", instrument=CALL)
        assert "|55=AAPL|167=OPT|200=202612|205=18|202=250|201=1|" in cli.wire[-1] and "|146=" not in cli.wire[-1]
        assert {r["instrument"] for r in await _rows(db, "fix_rfqs")} == {"AAPL 18Dec26 250 C"}

    @pytest.mark.asyncio
    async def test_two_series_of_one_symbol_are_two_streams(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_quote("Server", "AAPL", bid_px=4.1, offer_px=4.3, instrument=CALL)
        await engine.send_quote("Server", "AAPL", bid_px=3.0, offer_px=3.2, instrument=PUT)
        await engine.send_quote("Server", "AAPL", bid_px=4.2, offer_px=4.4, instrument=CALL)
        for direction in ("TX", "RX"):
            rows = await _rows(db, "fix_rfqs", f"WHERE direction = '{direction}'")
            assert [(r["instrument"], r["offer_px"]) for r in rows] == [
                ("AAPL 18Dec26 250 C", 4.4), ("AAPL 18Dec26 240 P", 3.2)], direction

    @pytest.mark.asyncio
    async def test_an_order_taking_a_quote_is_in_its_instrument(self, linked):
        db, engine, cli, mkt = linked
        quote_id = await engine.send_quote("Server", "ES", bid_px=5200, offer_px=5201, instrument=FUTURE)
        received = (await _rows(db, "fix_rfqs", "WHERE direction = 'RX'"))[0]
        await engine.send_new_order("Client", "ES", "1", 1, ord_type="D", price=5201,
                                    extra_tags=f"117={received['quote_id']}")
        assert received["quote_id"] == quote_id
        assert {o["instrument"] for o in await _rows(db, "fix_orders")} == {"ES Dec26"}


class TestBackfill:
    @pytest.mark.asyncio
    async def test_older_family_rows_show_their_symbol_once(self, stack):
        db, writer, engine = stack
        conn = db.write_conn
        for table, id_col in (("fix_iois", "ioi_id"), ("fix_adverts", "adv_id"), ("fix_allocations", "alloc_id")):
            await conn.execute(f"INSERT INTO {table} (session_id, {id_col}, symbol) VALUES ('S1', 'X', 'IBM')")
        await conn.execute("INSERT INTO fix_rfqs (session_id, symbol) VALUES ('S1', 'IBM')")
        await conn.commit()
        await engine._backfill_family_instrument()
        for table in ("fix_iois", "fix_adverts", "fix_allocations", "fix_rfqs"):
            assert (await _fetch_all(db, f"SELECT instrument FROM {table}"))[0]["instrument"] == "IBM", table
        await conn.execute("UPDATE fix_iois SET instrument = ''")
        await conn.commit()
        await engine._backfill_family_instrument()
        assert (await _fetch_all(db, "SELECT instrument FROM fix_iois"))[0]["instrument"] == "", "once"


def test_the_factory_leaves_quotes_and_requests_as_they_were():
    """The instrument is the engine's to add: a factory message with none
    is what it was before 0.76."""
    f = FixMessageFactory(FixDictionary("FIX.4.4"), "A", "B")
    msg = f.quote_request("Q1", "AAPL", side="1", qty=5)
    msg.sendprep(f.dictionary, "A", "B", 1)
    assert "|146=1|55=AAPL|54=1|38=5|60=" in msg.to_pipe_string()
