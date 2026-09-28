"""Instruments: options, futures and futures options on orders and trades —
each version's spelling of them on the wire (mkfix/fix/instrument.py), the
row columns both sides keep, and the saved instruments of Config ›
Instruments."""

import pytest
import pytest_asyncio

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.instrument import (blank_instrument, derived_cfi, instrument_of, instrument_pairs,
                                  instrument_text, normalize_instrument)
from mkfix.fix.message import FixMessageFactory, parse_fix

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_replay_e2e import LinkedSession

OPTION = {"security_type": "OPT", "maturity": "20261218", "strike_price": "250", "put_or_call": "Call",
          "open_close": "Open"}
FUTURE = {"security_type": "FUT", "maturity": "202612", "multiplier": "50", "security_exchange": "XCME"}
FUTURES_OPTION = {"security_type": "OOF", "maturity": "202612", "strike_price": "5200.25", "put_or_call": "Put",
                  "underlying_security_type": "FUT", "underlying_symbol": "ES", "underlying_maturity": "202612"}


def _pairs(version, terms, originating=True):
    return instrument_pairs(FixDictionary(version), normalize_instrument(terms), originating)


# ── The pure part ──────────────────────────────────────────────────────

class TestTerms:
    def test_words_and_codes_are_spelled_one_way(self):
        row = normalize_instrument({"security_type": "opt", "put_or_call": "1", "open_close": "c",
                                    "covered_uncovered": "UNCOVERED", "strike_price": "250.5", "multiplier": ""})
        assert (row["security_type"], row["put_or_call"], row["open_close"], row["covered_uncovered"]) == \
            ("OPT", "Call", "Close", "Uncovered")
        assert row["strike_price"] == 250.5 and row["multiplier"] is None

    def test_other_keys_are_not_instrument_terms(self):
        assert set(normalize_instrument({"symbol": "X", "qty": 5})) == set(blank_instrument())


class TestWireByVersion:
    def test_fix40_has_no_security_type(self):
        assert _pairs("FIX.4.0", FUTURE) == [], "every instrument tag withheld; 77 is 4.0's too"
        assert _pairs("FIX.4.0", OPTION) == [("77", "O")]

    @pytest.mark.parametrize("version", ["FIX.4.1", "FIX.4.2"])
    def test_a_maturity_date_splits_before_43(self, version):
        pairs = _pairs(version, OPTION)
        assert pairs[:5] == [("167", "OPT"), ("200", "202612"), ("205", "18"), ("202", "250"), ("201", "1")]

    def test_the_underlying_and_multiplier_start_in_42(self):
        assert ("231", "50") not in _pairs("FIX.4.1", FUTURE)
        assert ("231", "50") in _pairs("FIX.4.2", FUTURE)

    def test_fix43_names_futures_and_options_by_cfi_code(self):
        assert _pairs("FIX.4.3", FUTURE)[:2] == [("200", "202612"), ("461", "FXXXXX")]
        pairs = dict(_pairs("FIX.4.3", OPTION))
        assert "167" not in pairs and "201" not in pairs and pairs["461"] == "OCXXXX"
        assert pairs["200"] == "20261218", "from 4.3 the month-year carries the day"

    def test_a_cfi_code_given_wins(self):
        assert dict(_pairs("FIX.4.3", {**OPTION, "cfi_code": "OCASPS"}))["461"] == "OCASPS"
        assert dict(_pairs("FIX.4.4", {**OPTION, "cfi_code": "OCASPS"}))["461"] == "OCASPS"

    def test_an_option_on_a_future_derives_its_underlying_letter(self):
        assert derived_cfi(normalize_instrument({**OPTION, "underlying_security_type": "FUT"})) == "OCXFXX"

    @pytest.mark.parametrize("version", ["FIX.4.1", "FIX.4.2", "FIX.4.3", "FIX.4.4"])
    def test_options_on_futures_are_refused_before_50(self, version):
        with pytest.raises(ValueError, match="OOF is not defined.*send OPT with Underlying Type FUT"):
            _pairs(version, FUTURES_OPTION)

    def test_an_answer_echoes_the_code_as_it_stands(self):
        assert ("167", "OOF") in _pairs("FIX.4.4", FUTURES_OPTION, originating=False)

    def test_options_on_futures_from_50(self):
        pairs = _pairs("FIX.5.0SP2", FUTURES_OPTION)
        assert pairs[:4] == [("167", "OOF"), ("200", "202612"), ("202", "5200.25"), ("201", "0")]
        assert ("310", "FUT") in pairs and ("311", "ES") in pairs and ("313", "202612") in pairs

    def test_an_option_on_a_future_on_44(self):
        pairs = dict(_pairs("FIX.4.4", {**OPTION, "underlying_security_type": "FUT", "underlying_symbol": "ES"}))
        assert (pairs["167"], pairs["310"], pairs["311"]) == ("OPT", "FUT", "ES")


class TestReading:
    def test_month_and_day_are_joined(self):
        row = instrument_of(parse_fix("8=FIX.4.2|35=D|55=AAPL|167=OPT|200=202612|205=18|202=250|201=0|"))
        assert (row["maturity"], row["strike_price"], row["put_or_call"]) == ("20261218", 250.0, "Put")
        assert row["instrument"] == "AAPL 18Dec26 250 P"

    def test_a_maturity_date_alone(self):
        assert instrument_of(parse_fix("8=FIX.4.4|35=D|55=X|167=FUT|541=20261218|"))["maturity"] == "20261218"

    def test_a_cfi_code_alone_names_the_type(self):
        row = instrument_of(parse_fix("8=FIX.4.3|35=D|55=AAPL|461=OCXFXX|200=202612|202=250|"))
        assert (row["security_type"], row["put_or_call"], row["cfi_code"]) == ("OPT", "Call", "OCXFXX")
        assert instrument_of(parse_fix("8=FIX.4.3|35=D|55=ES|461=FXXXXX|"))["security_type"] == "FUT"

    def test_a_stock(self):
        row = instrument_of(parse_fix("8=FIX.4.2|35=D|55=IBM|"))
        assert row["instrument"] == "IBM" and row["security_type"] == "" and row["strike_price"] is None


class TestDisplay:
    @pytest.mark.parametrize("row, text", [
        ({"symbol": "IBM"}, "IBM"),
        ({"symbol": "IBM", "security_type": "CS"}, "IBM"),
        ({"symbol": "ES", "security_type": "FUT", "maturity": "202612"}, "ES Dec26"),
        ({"symbol": "AAPL", "security_type": "OPT", "maturity": "20261218", "strike_price": 250.0,
          "put_or_call": "Call"}, "AAPL 18Dec26 250 C"),
        ({"symbol": "ES", "security_type": "OOF", "maturity": "202612", "strike_price": 5200.25,
          "put_or_call": "Put"}, "ES Dec26 5200.25 P"),
        ({"symbol": "XYZ", "security_type": "WAR"}, "XYZ WAR"),
        ({"symbol": "", "security_id": "US0378331005", "security_type": "CS"}, "US0378331005"),
        ({"symbol": "ES", "security_type": "FUT", "maturity": "202612w2"}, "ES 202612w2"),
    ])
    def test_text(self, row, text):
        assert instrument_text(row) == text


# ── Both ends of an order ──────────────────────────────────────────────

@pytest_asyncio.fixture
async def linked(stack, request):
    """A client session and a market session on one engine, of the version
    the test names (`@pytest.mark.parametrize("linked", [...], indirect=True)`)."""
    db, writer, engine = stack
    version = getattr(request, "param", "FIX.4.4")
    cli = LinkedSession(engine, "Client", "Client", "Server")
    mkt = LinkedSession(engine, "Server", "Server", "Client")
    for end in (cli, mkt):
        end.dictionary = FixDictionary(version)
        end.factory = FixMessageFactory(end.dictionary, end.factory.sender, end.factory.target)
    cli.peer, mkt.peer = mkt, cli
    engine.sessions.update({"Client": cli, "Server": mkt})
    yield db, engine, cli, mkt


async def _orders(db):
    return {o["session_id"]: o for o in await _fetch_all(db, "SELECT * FROM fix_orders")}


async def _trades(db):
    return {t["session_id"]: t for t in await _fetch_all(db, "SELECT * FROM fix_executions")}


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_an_option_order_and_its_fill(self, linked):
        db, engine, cli, mkt = linked
        cl_ord_id = await engine.send_new_order("Client", "AAPL", "1", 10, price=4.2, instrument=OPTION)
        assert "|167=OPT|200=20261218|202=250|201=1|77=O|" in cli.wire[-1]
        orders = await _orders(db)
        for end in ("Client", "Server"):
            o = orders[end]
            assert (o["security_type"], o["maturity"], o["strike_price"], o["put_or_call"], o["open_close"]) == \
                ("OPT", "20261218", 250.0, "Call", "Open"), end
            assert o["instrument"] == "AAPL 18Dec26 250 C", end
        assert orders["Server"]["extra_tags"] == "", "the instrument is consumed, not echoed as extras"

        await engine.accept_order("Server", cl_ord_id)
        await engine.fill_order("Server", cl_ord_id, 10, 4.2)
        assert "|167=OPT|200=20261218|202=250|201=1|77=O|" in mkt.wire[-1], "the fill names the instrument"
        trades = await _trades(db)
        for end in ("Client", "Server"):
            t = trades[end]
            assert (t["security_type"], t["strike_price"], t["instrument"]) == ("OPT", 250.0, "AAPL 18Dec26 250 C")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", ["FIX.4.3"], indirect=True)
    async def test_a_future_on_43_goes_by_cfi_code(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_new_order("Client", "ES", "1", 2, price=5200, instrument=FUTURE)
        assert "|461=FXXXXX|" in cli.wire[-1] and "|167=" not in cli.wire[-1]
        orders = await _orders(db)
        assert orders["Client"]["security_type"] == orders["Server"]["security_type"] == "FUT"
        assert orders["Server"]["instrument"] == "ES Dec26"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", ["FIX.4.2"], indirect=True)
    async def test_a_maturity_date_survives_42(self, linked):
        db, engine, cli, mkt = linked
        cl_ord_id = await engine.send_new_order("Client", "AAPL", "2", 5, price=1, instrument=OPTION)
        assert "|200=202612|205=18|" in cli.wire[-1]
        await engine.accept_order("Server", cl_ord_id)
        assert "|200=202612|205=18|" in mkt.wire[-1]
        assert {o["maturity"] for o in (await _orders(db)).values()} == {"20261218"}

    @pytest.mark.asyncio
    async def test_a_refused_security_type_spends_no_clordid(self, linked):
        db, engine, cli, mkt = linked
        with pytest.raises(ValueError, match="OOF"):
            await engine.send_new_order("Client", "ES", "1", 1, price=5, instrument=FUTURES_OPTION)
        assert cli.wire == [] and await _fetch_all(db, "SELECT * FROM fix_orders") == []
        assert (await engine.ids.next_id("RT")).endswith("00000001")

    @pytest.mark.asyncio
    async def test_an_extra_tag_overrides_and_the_row_records_what_went_out(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_new_order("Client", "ES", "1", 1, price=5, instrument=FUTURE, extra_tags="200=202703")
        assert "|200=202703|" in cli.wire[-1]
        assert {o["maturity"] for o in (await _orders(db)).values()} == {"202703"}

    @pytest.mark.asyncio
    async def test_requests_carry_the_orders_instrument(self, linked):
        db, engine, cli, mkt = linked
        cl_ord_id = await engine.send_new_order("Client", "ES", "1", 2, price=5200, instrument=FUTURE)
        await engine.accept_order("Server", cl_ord_id)
        await engine.send_cancel_replace("Client", cl_ord_id, "ES", "1", 3, price=5201)
        assert "|35=G|" in cli.wire[-1] and "|167=FUT|200=202612|" in cli.wire[-1]
        await engine.accept_replace("Server", cl_ord_id)
        live = (await _orders(db))["Client"]["cl_ord_id"]
        await engine.send_cancel("Client", live, "ES", "1")
        assert "|35=F|" in cli.wire[-1] and "|167=FUT|200=202612|" in cli.wire[-1]
        assert (await _orders(db))["Client"]["security_type"] == "FUT", "a request never rewrites it"

    @pytest.mark.asyncio
    async def test_a_report_naming_only_the_symbol_trades_the_orders_instrument(self, linked):
        db, engine, cli, mkt = linked
        cl_ord_id = await engine.send_new_order("Client", "ES", "1", 2, price=5200, instrument=FUTURE)
        await engine.on_app_message(cli, "8", parse_fix(
            f"8=FIX.4.4|35=8|37=M1|11={cl_ord_id}|17=X1|150=F|39=1|55=ES|54=1|38=2|32=1|31=5200|14=1|151=1|6=5200|"))
        trade = (await _trades(db))["Client"]
        assert (trade["security_type"], trade["maturity"], trade["instrument"]) == ("FUT", "202612", "ES Dec26")
        assert (await _orders(db))["Client"]["security_type"] == "FUT", "and the report left the order's alone"

    @pytest.mark.asyncio
    async def test_a_stock_order_is_its_symbol(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_new_order("Client", "IBM", "1", 100, price=10)
        assert "|167=" not in cli.wire[-1]
        assert {(o["instrument"], o["security_type"]) for o in (await _orders(db)).values()} == {("IBM", "")}


# ── Saved instruments ──────────────────────────────────────────────────

class TestSavedInstruments:
    @pytest.mark.asyncio
    async def test_save_edit_and_delete(self, stack):
        db, writer, engine = stack
        await engine.save_instrument("ESZ6", symbol="ES", **{**FUTURE, "security_type": "fut"})
        (row,) = await _fetch_all(db, "SELECT * FROM fix_instruments")
        assert (row["security_type"], row["multiplier"], row["instrument"]) == ("FUT", 50.0, "ES Dec26")
        await engine.save_instrument("ESZ6", symbol="ES", maturity="202703", security_type="FUT",
                                     description="rolled")
        (row,) = await _fetch_all(db, "SELECT * FROM fix_instruments")
        assert (row["maturity"], row["instrument"], row["description"]) == ("202703", "ES Mar27", "rolled")
        assert row["multiplier"] is None, "a save is the whole instrument"
        assert len(await _fetch_all(db, "SELECT * FROM fix_instruments__history")) == 2
        await engine.delete_instrument("ESZ6")
        assert await _fetch_all(db, "SELECT * FROM fix_instruments") == []

    @pytest.mark.asyncio
    async def test_a_save_needs_a_name_and_instrument_terms(self, stack):
        db, writer, engine = stack
        with pytest.raises(ValueError, match="needs a name"):
            await engine.save_instrument("  ", symbol="ES")
        with pytest.raises(ValueError, match="Not instrument terms: open_close"):
            await engine.save_instrument("X", symbol="ES", open_close="Open")

    @pytest.mark.asyncio
    async def test_any_version_may_be_saved(self, stack):
        """The send checks a security type against its session, not the save."""
        db, writer, engine = stack
        await engine.save_instrument("ES-P5200", symbol="ES", **FUTURES_OPTION)
        assert (await _fetch_all(db, "SELECT security_type FROM fix_instruments"))[0]["security_type"] == "OOF"


class TestBackfill:
    @pytest.mark.asyncio
    async def test_older_rows_show_their_symbol_once(self, stack):
        db, writer, engine = stack
        conn = db.write_conn
        await conn.execute("INSERT INTO fix_orders (cl_ord_id, session_id, symbol) VALUES ('C1', 'S1', 'IBM')")
        await conn.execute("INSERT INTO fix_executions (session_id, exec_id, symbol) VALUES ('S1', 'X1', 'IBM')")
        await conn.commit()
        await engine._backfill_instrument()
        assert (await _fetch_all(db, "SELECT instrument FROM fix_orders"))[0]["instrument"] == "IBM"
        assert (await _fetch_all(db, "SELECT instrument FROM fix_executions"))[0]["instrument"] == "IBM"
        await conn.execute("UPDATE fix_orders SET instrument = ''")
        await conn.commit()
        await engine._backfill_instrument()
        assert (await _fetch_all(db, "SELECT instrument FROM fix_orders"))[0]["instrument"] == "", "once"
