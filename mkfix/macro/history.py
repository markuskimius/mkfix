"""A macro from history: the macro that would have played our part in what
an order — or an IOI, an advert, an allocation — has already been through.

The recorder follows what is done while it listens; this reads what was
kept. The messages are the record: `fix_messages` holds every one as it
crossed the wire, stamped, and the row's recorded versions give the chain
of IDs it went by. They are walked in order into the recorder's own
timeline — what the counterparty sent is *heard*, what we sent is *done* —
and the recorder's writer writes it, so a macro from history reads exactly
as a recording of the same thing would: event-triggered, requests answered
the same way every time as `when` rules, time only where nothing came
between two actions (or everywhere, with ``delays``).

What is ours depends on the side, as it does everywhere: on the market
side a received order's ExecutionReports and OrderCancelRejects are ours,
and the IOIs, adverts and allocations we sent; on the client side the
orders we sent with their requests and DKs, the Acks that answered a
received allocation, and the orders that answered a received IOI.

A report the language has no action for — a PendingCancel, an Expired, a
DoneForDay sent by hand-made extra tags or by Message Replay — is left out
and the header says so.
"""

from __future__ import annotations

import calendar
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from mkfix.fix import lists, multileg
from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.events import report_kinds
from mkfix.fix.families import (ALLOC_ACCEPTING, CONSUMED_ADVERT_TAGS, CONSUMED_ALLOC_ACK_TAGS, CONSUMED_ALLOC_TAGS,
                                CONSUMED_IOI_TAGS, CONSUMED_QUOTE_TAGS, CONSUMED_RESPONSE_TAGS,
                                CONSUMED_RFQ_REQUEST_TAGS, CONSUMED_RFQ_TAGS, advert_columns, allocation_columns,
                                ioi_columns, quote_columns, rfq_columns, rfq_request_columns)
from mkfix.fix.instrument import INSTRUMENT_TAGS, POSITION_TAGS, instrument_of
from mkfix.fix.message import (CONSUMED_EXEC_TAGS, CONSUMED_ORDER_TAGS, FixMessage, client_of, extra_pairs_of,
                               format_extra_tags, parse_fix)

from . import vocab
from .recorder import _HEARD, _QUIET, Recorder, _number, _Step, _Timeline

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine

# Beyond the tags the engine maps to columns, the ones it writes itself on
# each kind of message: none of them is an extra tag somebody typed.
_OURS = {
    "D": CONSUMED_ORDER_TAGS | {"126", "432"},
    "G": CONSUMED_ORDER_TAGS | {"126", "432"},
    "F": CONSUMED_ORDER_TAGS | {"125"},
    "AB": CONSUMED_ORDER_TAGS | {"126", "432"} | multileg.CONSUMED_LEG_TAGS,
    # The list messages: the ListID and what each says as its own.
    "L": frozenset({"66", "58"}), "K": frozenset({"66", "58"}), "M": frozenset({"66", "58"}),
    "N": frozenset({"66", "429", "431", "82", "83", "68", "73", "11", "14", "39", "151", "84", "6", "444", "58",
                    "60"}),
    "AC": CONSUMED_ORDER_TAGS | {"126", "432"} | multileg.CONSUMED_LEG_TAGS,
    "8": CONSUMED_EXEC_TAGS | {"378", "103"} | multileg.CONSUMED_LEG_TAGS,
    "9": frozenset({"11", "37", "39", "41", "58", "60", "102", "434"}),
    "Q": frozenset({"11", "17", "31", "32", "37", "38", "54", "55", "58", "127"}) | INSTRUMENT_TAGS | POSITION_TAGS,
    "6": CONSUMED_IOI_TAGS,
    "7": CONSUMED_ADVERT_TAGS,
    "J": CONSUMED_ALLOC_TAGS,
    "P": CONSUMED_ALLOC_ACK_TAGS,
    "R": CONSUMED_RFQ_TAGS,
    "S": CONSUMED_QUOTE_TAGS,
    "Z": frozenset({"131", "117", "298", "295", "55", "58"}),
    "AG": frozenset({"131", "644", "658", "146", "55", "58"}),
    "AJ": CONSUMED_RESPONSE_TAGS,
    "AH": CONSUMED_RFQ_REQUEST_TAGS,
}
# QuoteRespType(694): what a response we sent does, and what one we heard is.
_RESPONSE_VERB = {"1": "hit", "11": "hit", "2": "counter", "6": "pass quote"}
_RESPONSE_HEARD = {"1": "hit", "11": "hit", "2": "countered", "6": "passed", "3": "expired", "8": "expired"}
# A family's message type, the tags of its ID and of the one it supersedes,
# the columns they are kept in, and what makes a row's columns of a message.
_FAMILY = {
    vocab.IOI: ("6", "23", "26", "28", ("ioi_id", "ioi_ref_id"), ioi_columns),
    vocab.ADVERT: ("7", "2", "3", "5", ("adv_id", "adv_ref_id"), advert_columns),
    vocab.ALLOCATION: ("J", "70", "72", "71", ("alloc_id", "ref_alloc_id", "pending_alloc_id"), allocation_columns),
}
# IOITransType(28) and AdvTransType(5) spell New/Replace/Cancel N/R/C; AllocTransType(71) 0/1/2.
_TRANS = {"N": "new", "R": "replace", "C": "cancel", "0": "new", "1": "replace", "2": "cancel"}
_ACK_HEARD = {"0": "accepted", "1": "rejected", "2": "rejected", "3": "received", "4": "incomplete", "5": "rejected"}
# What finishes an order, heard: the end of its part in a list.
_ENDS = ("filled", "canceled", "rejected", "expired", "done for day")
_ORDER_ANSWERS = {"ack": "accept", "rejected": "reject", "replaced": "accept", "restated": "restate"}
# A sent trade's report by what it is, whatever the dialect calls it.
_REPORT_OF = {"PartialFill": "fill", "Fill": "fill", "Trade": "fill", "Correct": "corrected",
              "TradeCorrect": "corrected", "Cancel": "busted", "TradeCancel": "busted"}
_MARGIN = 5.0          # a message is stamped when it is recorded, its row when it is written: never far apart
SOH = "\x01"


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    return time.strftime("%Y%m%d-%H:%M:%S", time.gmtime(whole)) + f".{int((seconds - whole) * 1000):03d}"


def _seconds(stamp: str) -> float:
    """A FIX timestamp as seconds since the epoch; 0 for one that is none."""
    try:
        whole, _, fraction = stamp.partition(".")
        return calendar.timegm(time.strptime(whole, "%Y%m%d-%H:%M:%S")) + (float("0." + fraction) if fraction else 0.0)
    except ValueError:
        return 0.0


class _Trades:
    """The order's trades as they stood at each moment of the walk: what
    `last trade`, `first trade` and `trade where …` would have found."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def find(self, exec_id: str) -> dict[str, Any] | None:
        return next((t for t in self.rows if exec_id and exec_id in t["ids"]), None)

    def fill(self, exec_id: str, qty: Any, price: Any) -> dict[str, Any]:
        trade = {"ids": {exec_id}, "qty": qty, "price": price, "busted": False}
        self.rows.append(trade)
        return trade

    def target(self, trade: dict[str, Any]) -> str:
        """The trade by the words a macro names it with — among the live
        ones, as the runner looks — or nothing for a busted one."""
        live = [t for t in self.rows if not t["busted"]]
        if trade not in live:
            return ""
        if trade is live[-1]:
            return "last trade"
        if trade is live[0]:
            return "first trade"
        return f"trade where trade.last_qty == {_number(trade['qty'])} and trade.last_price == {_number(trade['price'])}"


class FromHistory(Recorder):
    """Writes; never listens."""

    def __init__(self, engine: FixEngine, side: str, delays: bool = False) -> None:
        if side not in vocab.MACRO_SIDES:
            raise ValueError(f"A macro is of the client side or the market side, not {side!r}")
        self.engine, self.side, self.session, self.delays = engine, side, "", delays
        self.timelines = {}
        self.left_out: list[str] = []
        self._unsubscribe = None

    # -- reading -----------------------------------------------------------------------------

    async def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = await self.engine.db.read_conn.execute(sql, params)
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _versions(self, table: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        """The row as it stood at each of its recorded versions, then as it stands."""
        try:
            kept = await self._fetch(f"SELECT * FROM {table}__history WHERE id = ? ORDER BY _mkio_version", (row["id"],))
        except Exception:                    # a table that keeps no history
            kept = []
        return [*kept, row]

    def _dictionary(self, session_id: str, msg: FixMessage) -> FixDictionary:
        session = self.engine.sessions.get(session_id)
        return session.dictionary if session else FixDictionary(msg.get("8", "") or "FIX.4.2")

    def _client(self, session_id: str, msg: FixMessage) -> tuple[str, list[tuple[str, str]]]:
        """The client a message names, and the pairs that carry it when they
        are the ones the engine stamps — which are not extra tags."""
        session = self.engine.sessions.get(session_id)
        if session is None:
            return "", []
        specs = self.engine._client_specs(session)
        client = client_of(msg, specs)
        return client, list(session.factory.client_fields(client, specs).items())

    def _extras(self, session_id: str, msg: FixMessage) -> tuple[str, str]:
        """A message's client and its extra tags as they would be typed:
        what it carried beyond what the engine writes on one of its kind."""
        client, stamped = self._client(session_id, msg)
        pairs = extra_pairs_of(msg, self._dictionary(session_id, msg), _OURS.get(msg.get("35", ""), frozenset()))
        for pair in stamped:
            if pair in pairs:
                pairs.remove(pair)
        return client, format_extra_tags(pairs)

    async def _messages(self, session_id: str, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        """The session's messages that match, oldest first."""
        rows = await self._fetch(
            f"SELECT id, timestamp, direction, msg_type, raw_message FROM fix_messages "
            f"WHERE session_id = ? AND ({where}) ORDER BY id", (session_id, *params))
        for row in rows:
            row["msg"] = parse_fix(row["raw_message"])
            row["at"] = _seconds(row["timestamp"])
        return rows

    @staticmethod
    def _life(messages: list[dict[str, Any]], begins: Any, born: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """The message that began the row and the ones that followed it,
        up to the next beginning. An ID comes round again — a counterparty's
        ClOrdIDs repeat from day to day — so of the messages that could have
        begun it, it is the last one from before the row was written; a row
        a report created (Message Replay sends around the engine) was
        written long after."""
        starts = [m for m in messages if begins(m)]
        if not starts:
            return None, []
        limit = _stamp(_seconds(born) + _MARGIN) if _seconds(born) else ""
        first = next((m for m in reversed(starts) if not limit or m["timestamp"] <= limit), starts[0])
        after = next((m["id"] for m in starts if m["id"] > first["id"]), None)
        return first, [m for m in messages if m["id"] > first["id"] and (after is None or m["id"] < after)]

    def _said(self, msg: FixMessage, session_id: str, *, text: bool = True) -> dict[str, Any]:
        """The terms every action has: its text and its extra tags."""
        _, extra = self._extras(session_id, msg)
        return {"text": msg.get("58", "") if text else "", "extra": extra}

    def _leave_out(self, what: str) -> None:
        if what not in self.left_out:
            self.left_out.append(what)

    # -- an order ----------------------------------------------------------------------------

    @staticmethod
    def _order_terms(msg: FixMessage, client: str, extra: str) -> dict[str, Any]:
        """A NewOrderSingle's or a replace request's terms, by the columns
        an order's row keeps them in."""
        return {"symbol": msg.get("55", ""), "side_code": msg.get("54", ""), "order_qty": msg.get("38", ""),
                "entered_qty": msg.get("38", ""), "ord_type_code": msg.get("40", ""),
                "price": msg.get("44") or None, "entered_price": msg.get("44") or None, "tif_code": msg.get("59", ""),
                "expire_time": msg.get("126", ""), "expire_date": msg.get("432", ""), "client": client,
                "handl_inst_code": msg.get("21", ""), "sent_text": msg.get("58", ""), "extra_tags": extra,
                **instrument_of(msg)}

    async def _order(self, row: dict[str, Any], in_list_at: float | None = None) -> _Timeline | None:
        """An order's timeline. One sent inside a NewOrderList has no
        NewOrderSingle of its own: ``in_list_at`` is when its list went, and
        its terms are the row's."""
        session_id, sent = row["session_id"], row["direction"] == "TX"
        versions = await self._versions("fix_orders", row)
        chain = sorted({v[c] for v in versions for c in ("cl_ord_id", "pending_cl_ord_id", "orig_cl_ord_id") if v.get(c)})
        marks = ",".join("?" * len(chain))
        born = min((v["created_at"] for v in versions if v.get("created_at")), default="")
        messages = await self._messages(session_id, f"cl_ord_id IN ({marks})", tuple(chain))
        execs = sorted({m["msg"].get("17", "") for m in messages if m["msg_type"] == "8"} - {""})
        if execs:
            # A DontKnowTrade names its trade, not the order.
            known = {m["id"] for m in messages}
            disputes = await self._messages(session_id, f"msg_type = 'Q' AND exec_id IN ({','.join('?' * len(execs))})",
                                            tuple(execs))
            messages = sorted(messages + [m for m in disputes if m["id"] not in known], key=lambda m: m["id"])
        every = messages
        first, messages = self._life(
            messages, lambda m: m["msg_type"] in ("D", "AB") and m["direction"] == ("TX" if sent else "RX"), born)
        if first is None and in_list_at is not None:
            messages = [m for m in every if m["at"] >= in_list_at]
            line = _Timeline(dict(row), in_list_at, subject=vocab.ORDER)
        elif first is None:
            self._leave_out(f"{row['cl_ord_id']}: its NewOrderSingle is not among the messages kept")
            return None
        else:
            client, extra = self._extras(session_id, first["msg"])
            line = _Timeline({**row, **self._order_terms(first["msg"], client, extra)}, first["at"],
                             subject=vocab.ORDER)
        if first is not None and first["msg_type"] == "AB":
            # A multileg order: the legs it went out with, and what it asked for.
            line.order["_legs"] = multileg.legs_of(first["msg"], self._dictionary(session_id, first["msg"]))
            line.order["multileg_rpt_type"] = first["msg"].get("563", "")
        trades = _Trades()
        restated = await self._restated(row) if not sent else {}
        accepted = dict(line.order)                       # the terms last accepted: what a replace changes
        asked: dict[str, dict[str, Any]] = {}             # each replace request's terms, by its ClOrdID
        heard = _HEARD[(vocab.ORDER, vocab.CLIENT if sent else vocab.MARKET)]
        for m in messages:
            msg, kind, mine = m["msg"], m["msg_type"], (m["direction"] == "TX")
            step: _Step | None = None
            if kind == "8":
                step = self._report(line, m, trades, restated, mine, heard)
                if not mine and step is not None and step.name == "replaced":
                    accepted.update(asked.get(msg.get("11", ""), {}))
            elif kind == "9":
                step = _Step(m["at"], "did", "reject", self._said(msg, session_id), stamp=m["timestamp"]) if mine \
                    else _Step(m["at"], "heard", "cancel rejected")
            elif kind in ("F", "G", "AC"):
                request = "cancel" if kind == "F" else "replace"
                if not mine:
                    step = _Step(m["at"], "heard", request)
                elif sent:
                    step = _Step(m["at"], "did", request, self._request(msg, session_id, kind, accepted, asked),
                                 stamp=m["timestamp"])
                    if kind == "AC":
                        step.legs = multileg.legs_of(msg, self._dictionary(session_id, msg))
            elif kind == "Q":
                trade = trades.find(msg.get("17", ""))
                if not mine:
                    step = _Step(m["at"], "heard", "dk")
                elif trade is None or not trades.target(trade):
                    self._leave_out(f"{row['cl_ord_id']}: a DontKnowTrade of {msg.get('17', '')}, which is no live trade of the order")
                else:
                    step = _Step(m["at"], "did", "dk", {"reason": msg.get("127", ""), **self._said(msg, session_id)},
                                 stamp=m["timestamp"], target=trades.target(trade))
            if step is not None and (step.kind == "did" or step.name in heard):
                step.terms = {k: v for k, v in step.terms.items() if v not in (None, "")}
                line.steps.append(step)
        return line

    def _request(self, msg: FixMessage, session_id: str, kind: str, accepted: dict[str, Any],
                 asked: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """A request's terms as the verb takes them; a replace's are the ones
        that differ from the terms last accepted, as the recorder writes it."""
        client, extra = self._extras(session_id, msg)
        if kind == "F":
            return {"text": msg.get("58", ""), "extra": extra}
        asked[msg.get("11", "")] = self._order_terms(msg, client, extra)
        data = {"qty": msg.get("38", ""), "ord_type": msg.get("40", ""), "price": msg.get("44", ""),
                "tif": msg.get("59", ""), "expire_time": msg.get("126", "") or msg.get("432", ""), "client": client,
                "handl_inst": msg.get("21", ""), "text": msg.get("58", ""), "extra_tags": extra}
        return self._terms("replace", SimpleNamespace(detail={"data": data}, prev=accepted))

    async def _restated(self, row: dict[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any] | None]]:
        """The order's sent trades by every ExecID they went by: the version
        of the trade's row that carried it and the one before. A report that
        says again what its trade's row already said is a re-notification —
        on the wire it is a fill, a correction or a bust like any other."""
        found: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
        for trade in await self._fetch("SELECT * FROM fix_executions WHERE session_id = ? AND order_id = ? "
                                       "AND direction = 'TX' ORDER BY id", (row["session_id"], row["order_id"])):
            last = None
            for version in await self._versions("fix_executions", trade):
                if last is not None and version["exec_id"] == last["exec_id"]:
                    continue                 # the same report written again: a DK marked on it
                found.setdefault(version["exec_id"], (version, last))
                last = version
        return found

    def _report(self, line: _Timeline, m: dict[str, Any], trades: _Trades,
                restated: dict[str, tuple[dict[str, Any], dict[str, Any] | None]], mine: bool,
                heard: tuple[str, ...]) -> _Step | None:
        """An ExecutionReport: what was heard, or the action that sent it."""
        msg, session_id, who = m["msg"], line.order["session_id"], line.order["cl_ord_id"]
        kinds = report_kinds(msg)
        kind, exec_id, ref = kinds[0], msg.get("17", ""), msg.get("19", "")
        qty, price = msg.get("32", ""), msg.get("31", "")
        said = self._said(msg, session_id) if mine else {}
        if msg.get("442") == multileg.LEG:
            # One leg's report: a fill of that leg, never the order's `filled`.
            if kind != "fill":
                return None
            trades.fill(exec_id, qty, price)
            leg = (multileg.legs_of(msg, self._dictionary(session_id, msg)) or [{}])[0].get("leg_ref_id", "")
            if mine:
                return _Step(m["at"], "did", "fill", {"leg": leg, "qty": qty, "price": price, **said},
                             stamp=m["timestamp"])
            return _Step(m["at"], "heard", "fill", stamp=m["timestamp"])
        step = _Step(m["at"], "did" if mine else "heard", "filled" if "filled" in kinds else kind, stamp=m["timestamp"])
        if kind in ("fill", "corrected", "busted"):
            version, before = restated.get(exec_id, (None, None))
            again = mine and before is not None \
                and _REPORT_OF.get(version["exec_type"]) == _REPORT_OF.get(before["exec_type"], "?") \
                and (version["exec_ref_id"] or "") == (before["exec_ref_id"] or "")
            trade = trades.find(before["exec_id"]) if again else trades.find(ref) if kind != "fill" else None
            if again or kind != "fill":
                target = trades.target(trade) if trade else ""
                if trade is not None:
                    trade["ids"].add(exec_id)
                if mine and not target:
                    self._leave_out(f"{who}: a report on {ref or exec_id}, which is no live trade of the order")
                    return None
                if again:
                    step.name, step.terms, step.target = "renotify", said, target
                    return step
                if mine and kind == "corrected":
                    step.name, step.terms, step.target = "correct", {"qty": qty, "price": price, **said}, target
                elif mine:
                    step.name, step.terms, step.target = "bust", said, target
                if trade is not None and kind == "corrected":
                    trade["qty"], trade["price"] = qty or trade["qty"], price or trade["price"]
                elif trade is not None:
                    trade["busted"] = True
                return step
            trades.fill(exec_id, qty, price)
            if mine:
                step.name, step.terms = "fill", {"qty": qty, "price": price, **said}
            return step
        if not mine:
            return step
        if kind == "canceled":
            # Answering a request it names the ClOrdID superseded; nobody asked for one that names none.
            step.name, step.terms = ("accept" if msg.get("41") else "unsol cxl"), said
        elif kind in _ORDER_ANSWERS:
            step.name, step.terms = _ORDER_ANSWERS[kind], said
            if kind == "restated":
                step.terms = {"qty": msg.get("38", ""), "price": msg.get("44", ""), "reason": msg.get("378", ""), **said}
        else:
            name = self._dictionary(session_id, msg).enum_name("150", msg.get("150", "")) or msg.get("150", "") or "?"
            self._leave_out(f"{who}: an ExecutionReport the language has no action for ({name})")
            return None
        return step

    # -- a list ------------------------------------------------------------------------------------

    async def _list(self, row: dict[str, Any]) -> _Timeline | None:
        """A list: its own messages (NewOrderList, ListStatus, ListExecute,
        ListCancelRequest, ListStatusRequest) and its orders'. Sent, its
        orders become the `new list`'s `order` lines — those that went
        later, `add order`s — each with what happened to it. Received, what
        was done to it is read from what went out at once: a ListStatus
        answering a request, or every working order acknowledged, filled or
        canceled together (`accept list`, `fill all`, `cancel list orders`)."""
        session_id, sent = row["session_id"], row["direction"] == "TX"
        mark = f"{SOH}66={row['list_id']}{SOH}"
        messages = await self._messages(session_id, "msg_type IN ('E', 'N', 'L', 'K', 'M', 'D') AND instr(raw_message, ?) > 0",
                                        (mark,))
        mine_first = [m for m in messages if m["msg_type"] in ("E", "D") and m["direction"] == ("TX" if sent else "RX")]
        if not mine_first:
            self._leave_out(f"{row['list_id']}: its orders' messages are not among the messages kept")
            return None
        start = mine_first[0]["at"]
        # The mode it went out in: an order that joined it later makes the row's `E+D`.
        line = _Timeline({**row, "mode": "E" if mine_first[0]["msg_type"] == "E" else "D"}, start,
                         subject=vocab.LIST)
        members = await self._fetch("SELECT * FROM fix_orders WHERE session_id = ? AND direction = ? AND list_id = ? "
                                    "ORDER BY list_seq_no, id", (session_id, row["direction"], row["list_id"]))
        return await (self._sent_list(line, messages, members) if sent else self._received_list(line, messages, members))

    async def _sent_list(self, line: _Timeline, messages: list[dict[str, Any]],
                         members: list[dict[str, Any]]) -> _Timeline:
        start, session_id = line.started, line.order["session_id"]
        heard = _HEARD[(vocab.LIST, vocab.CLIENT)]
        # The orders that went with the list, then any that joined it later: an `add order` each.
        sends = {m["msg"].get("11", ""): m["at"] for m in messages if m["msg_type"] == "D" and m["direction"] == "TX"}
        timelines: list[tuple[float, _Timeline]] = []
        for member in members:
            at = sends.get(member["cl_ord_id"], start)
            timeline = await self._order(member, in_list_at=start if member["cl_ord_id"] not in sends else None)
            if timeline is None:
                continue
            # The ListID each carried is the list's to say.
            own = timeline.order.get("extra_tags") or ""
            timeline.order["extra_tags"] = "|".join(p for p in own.split("|")
                                                    if p and p != f"66={line.order['list_id']}")
            self.timelines[(vocab.ORDER, member["id"])] = timeline
            timelines.append((at, timeline))
            if at - start >= _QUIET:
                client, extra = self._extras(session_id, next(m["msg"] for m in messages
                                                              if m["msg"].get("11") == member["cl_ord_id"]))
                # Its ListID is the `add order`'s own.
                extra = "|".join(p for p in extra.split("|") if p and p != f"66={line.order['list_id']}")
                terms = self._creator_terms(vocab.ORDER, {**member, "client": client, "extra_tags": extra})
                line.steps.append(_Step(at, "did", "add order", {k: v for k, v in terms.items() if v not in (None, "")},
                                        stamp=_stamp(at)))
        for m in messages:
            msg, kind, mine = m["msg"], m["msg_type"], m["direction"] == "TX"
            step: _Step | None = None
            if kind in ("L", "K", "M") and mine:
                verb = {"L": "execute list", "K": "cancel list", "M": "request list status"}[kind]
                terms = {k: v for k, v in self._said(msg, session_id).items() if v}
                step = _Step(m["at"], "did", verb, terms, stamp=m["timestamp"])
            elif kind == "N" and not mine:
                status, status_type = msg.get("431", ""), msg.get("429", "")
                name = "rejected" if status == lists.REJECT else "accepted" if status_type == lists.ACK \
                    else "executing" if status == lists.EXECUTING or status_type == lists.EXEC_STARTED else "status"
                step = _Step(m["at"], "heard", name)
            if step is not None and (step.kind == "did" or step.name in heard):
                line.steps.append(step)
        # `done`: the first moment every order sent by then had finished — an order
        # that joined later does not undo it.
        ended = {id(t): next((s.at for s in t.steps if s.kind == "heard" and s.name in _ENDS), None)
                 for _, t in timelines}
        for t_end in sorted(e for e in ended.values() if e is not None):
            if all(ended[id(t)] is not None and ended[id(t)] <= t_end for at, t in timelines if at <= t_end):
                line.steps.append(_Step(t_end, "heard", "done"))
                break
        line.steps.sort(key=lambda s: s.at)
        return line

    async def _received_list(self, line: _Timeline, messages: list[dict[str, Any]],
                             members: list[dict[str, Any]]) -> _Timeline:
        session_id, heard = line.order["session_id"], _HEARD[(vocab.LIST, vocab.MARKET)]
        requests = {"L": "execute", "K": "cancel", "M": "status request"}
        answered_status = False
        for m in messages:
            msg, kind, mine = m["msg"], m["msg_type"], m["direction"] == "TX"
            if kind in requests and not mine:
                if requests[kind] in heard:
                    line.steps.append(_Step(m["at"], "heard", requests[kind]))
                answered_status = kind == "M"
            elif kind == "N" and mine:
                status, status_type = msg.get("431", ""), msg.get("429", "")
                if answered_status and status_type == lists.RESPONSE:
                    answered_status = False           # mkfix answers a ListStatusRequest itself
                    continue
                # A ListStatus says its text in ListStatusText (444) from 4.2.
                said = {**self._said(msg, session_id), "text": msg.get("444", "") or msg.get("58", "")}
                if status_type in (lists.ACK, lists.EXEC_STARTED, lists.ALL_DONE_TYPE):
                    verb = "reject list" if status == lists.REJECT else "accept list"
                    terms = said
                else:
                    verb = "list status"
                    terms = {"status_type": status_type, "list_status": status, **said}
                line.steps.append(_Step(m["at"], "did", verb, {k: v for k, v in terms.items() if v},
                                        stamp=m["timestamp"]))
        # What the market did to all of the list's orders at once.
        ids = [o["cl_ord_id"] for o in members]
        if ids:
            reports = await self._messages(session_id, f"msg_type = '8' AND direction = 'TX' AND cl_ord_id IN "
                                                       f"({','.join('?' * len(ids))})", tuple(ids))
            batches: list[list[dict[str, Any]]] = []
            for r in reports:
                if batches and r["at"] - batches[-1][-1]["at"] < _QUIET:
                    batches[-1].append(r)
                else:
                    batches.append([r])
            said = {s.at for s in line.steps if s.kind == "did"}
            for batch in batches:
                kinds = {report_kinds(r["msg"])[0] for r in batch}
                at = batch[0]["at"]
                if kinds == {"ack"} and not any(abs(a - at) < _QUIET for a in said):
                    line.steps.append(_Step(at, "did", "accept list", {}, stamp=batch[0]["timestamp"]))
                elif kinds == {"fill"} and (len({r["msg"].get("11") for r in batch}) > 1 or len(ids) == 1):
                    prices = {r["msg"].get("31", "") for r in batch}
                    terms = {"price": prices.pop()} if len(prices) == 1 else {}
                    line.steps.append(_Step(at, "did", "fill all", terms, stamp=batch[0]["timestamp"]))
                elif kinds == {"canceled"} and not any(r["msg"].get("41") for r in batch):
                    line.steps.append(_Step(at, "did", "cancel list orders", {}, stamp=batch[0]["timestamp"]))
                elif kinds - {"ack"}:
                    self._leave_out(f"{line.order['list_id']}: its orders were worked one by one — "
                                    "Macro… on Received Orders writes them")
        line.steps.sort(key=lambda s: s.at)
        return line

    # -- an IOI, an advert, an allocation --------------------------------------------------------

    async def _family(self, subject: str, row: dict[str, Any]) -> _Timeline | None:
        msg_type, id_tag, ref_tag, trans_tag, id_cols, columns = _FAMILY[subject]
        table, session_id, sent = vocab.SUBJECT_TABLES[subject], row["session_id"], row["direction"] == "TX"
        name = row[vocab.SUBJECT_IDS[subject]]
        versions = await self._versions(table, row)
        chain = {v[c] for v in versions for c in id_cols if v.get(c)}
        born = min((v["timestamp"] for v in versions if v.get("timestamp")), default="")
        kinds = {"J": "'J', 'P'", "6": "'6', 'D'"}.get(msg_type, f"'{msg_type}'")
        # The messages naming one of its IDs: narrowed by the text first, then read.
        named = " OR ".join(["instr(raw_message, ?) > 0"] * len(chain)) or "0"
        marks = tuple(f"{SOH}{tag}={value}{SOH}" for value in sorted(chain) for tag in (id_tag,))
        if msg_type == "6":
            named += "".join(" OR instr(raw_message, ?) > 0" for _ in chain)
            marks += tuple(f"{SOH}23={value}{SOH}" for value in sorted(chain))
        messages = [m for m in await self._messages(session_id, f"msg_type IN ({kinds}) AND ({named})", marks)
                    if (m["msg"].get(id_tag, "") in chain if m["msg_type"] != "D"
                        else m["msg"].get("23", "") in chain and m["direction"] == "TX")]
        first, messages = self._life(
            messages, lambda m: m["msg_type"] == msg_type and m["direction"] == ("TX" if sent else "RX")
            and _TRANS.get(m["msg"].get(trans_tag, "")) == "new", born)
        if first is None:
            self._leave_out(f"{name}: the message that began it is not among the messages kept")
            return None

        def as_row(msg: FixMessage) -> dict[str, Any]:
            client, extra = self._extras(session_id, msg)
            return {**columns(msg, self._dictionary(session_id, msg)), "client": client, "extra_tags": extra,
                    "sent_text": msg.get("58", "")}

        line = _Timeline({**row, **as_row(first["msg"])}, first["at"], subject=subject)
        heard = _HEARD[(subject, vocab.CLIENT if sent else vocab.MARKET)]
        for m in messages:
            msg, mine = m["msg"], m["direction"] == "TX"
            step: _Step | None = None
            if m["msg_type"] == "P":
                status = msg.get("87", "")
                if not mine:
                    step = _Step(m["at"], "heard", _ACK_HEARD.get(status, "acked"))
                else:
                    verb = "accept allocation" if status in ALLOC_ACCEPTING else "reject allocation"
                    step = _Step(m["at"], "did", verb, {"status": status, "reason": msg.get("88", ""),
                                                        **self._said(msg, session_id)}, stamp=m["timestamp"])
                    step.terms = {k: v for k, v in step.terms.items() if k in vocab.VERBS[verb].terms}
            elif m["msg_type"] == "D":
                # The order that answered a received IOI: the engine names the IOI on it, so 23 is no extra tag.
                client, extra = self._extras(session_id, msg)
                terms = self._creator_terms(vocab.ORDER, self._order_terms(msg, client, extra))
                step = _Step(m["at"], "did", "new", terms, stamp=m["timestamp"])
            else:
                what = _TRANS.get(msg.get(trans_tag, ""), "")
                if what not in ("replace", "cancel"):
                    continue
                if not mine:
                    # An IOI's or an advert's is done when it is said; an allocation's is a request.
                    step = _Step(m["at"], "heard", what if subject == vocab.ALLOCATION else what + ("d" if what == "replace" else "ed"))
                elif what == "cancel":
                    step = _Step(m["at"], "did", f"cancel {subject}", self._said(msg, session_id), stamp=m["timestamp"])
                else:
                    step = _Step(m["at"], "did", f"replace {subject}", self._creator_terms(subject, as_row(msg)),
                                 stamp=m["timestamp"])
            if step is not None and (step.kind == "did" or step.name in heard):
                step.terms = {k: v for k, v in step.terms.items() if v not in (None, "")}
                line.steps.append(step)
        return line

    # -- an RFQ, a quote, an RFQ request -----------------------------------------------------

    def _quote_terms(self, msg: FixMessage, session_id: str) -> dict[str, Any]:
        """A quote's terms as `quote`, `requote` and `new quote` take them;
        `valid` the seconds from its sending to its ValidUntilTime."""
        valid = _seconds(msg.get("62", "")) - _seconds(msg.get("52", "")) if msg.get("62") and msg.get("52") else 0
        return {"bid": msg.get("132", ""), "offer": msg.get("133", ""), "bid_size": msg.get("134", ""),
                "offer_size": msg.get("135", ""), "valid": round(valid) if valid > 0 else "",
                "quote_type": msg.get("537", ""), **self._said(msg, session_id)}

    def _response(self, m: dict[str, Any], session_id: str) -> _Step | None:
        """A QuoteResponse we sent, as the action that sent it."""
        msg = m["msg"]
        verb = _RESPONSE_VERB.get(msg.get("694", ""))
        if verb is None:
            self._leave_out(f"{msg.get('117', '')}: a QuoteResponse the language has no action for "
                            f"({msg.get('694', '')})")
            return None
        terms: dict[str, Any] = self._said(msg, session_id)
        if verb == "hit":
            terms.update(side=msg.get("54", ""), qty=msg.get("38", ""), price=msg.get("44", ""))
        elif verb == "counter":
            terms.update(bid=msg.get("132", ""), offer=msg.get("133", ""), bid_size=msg.get("134", ""),
                         offer_size=msg.get("135", ""))
        return _Step(m["at"], "did", verb, terms, stamp=m["timestamp"])

    async def _negotiation(self, subject: str, row: dict[str, Any]) -> _Timeline | None:
        """An RFQ (its QuoteRequest and every quote and answer since) or a
        stream of unsolicited quotes, from the messages naming its IDs."""
        session_id, sent = row["session_id"], row["direction"] == "TX"
        versions = await self._versions("fix_rfqs", row)
        quotes = sorted({v[c] for v in versions for c in ("quote_id", "quote_ref_id") if v.get(c)})
        born = min((v["timestamp"] for v in versions if v.get("timestamp")), default="")
        marks = tuple(f"{SOH}117={q}{SOH}" for q in quotes)
        if row["quote_req_id"]:
            marks += (f"{SOH}131={row['quote_req_id']}{SOH}",)
        named = " OR ".join(["instr(raw_message, ?) > 0"] * len(marks)) or "0"
        messages = await self._messages(session_id, f"msg_type IN ('R', 'S', 'Z', 'AG', 'AI', 'AJ', 'D') AND ({named})",
                                        marks)
        # An RFQ begins with its QuoteRequest; a stream of quotes with its first quote, the others being requotes.
        opener = "R" if subject == vocab.RFQ else "S"
        started = versions[0]["quote_id"] if subject == vocab.QUOTE else ""
        first, messages = self._life(
            messages, lambda m: m["msg_type"] == opener and m["direction"] == ("TX" if sent else "RX")
            and (not started or m["msg"].get("117") == started), born)
        if first is None:
            self._leave_out(f"{row[vocab.SUBJECT_IDS[subject]]}: the message that began it is not among the messages kept")
            return None
        client, extra = self._extras(session_id, first["msg"])
        columns = rfq_columns if subject == vocab.RFQ else quote_columns
        begun = {**columns(first["msg"], self._dictionary(session_id, first["msg"])), "client": client,
                 "sent_text": first["msg"].get("58", "")}
        if subject == vocab.RFQ:
            begun["extra_tags"] = extra
        else:
            begun.update(quote_extra_tags=extra, bid_px=first["msg"].get("132"), offer_px=first["msg"].get("133"),
                         bid_size=first["msg"].get("134"), offer_size=first["msg"].get("135"),
                         side_code=first["msg"].get("54", ""), order_qty=first["msg"].get("38") or None)
        line = _Timeline({**row, **begun}, first["at"], subject=subject)
        heard = _HEARD[(subject, vocab.CLIENT if sent else vocab.MARKET)]
        # Which way the quotes go: the market sends them, so they are ours on an RFQ we received or a quote we sent.
        quoting = (subject == vocab.RFQ) != sent
        quoted = subject == vocab.QUOTE
        for m in messages:
            msg, kind, mine = m["msg"], m["msg_type"], m["direction"] == "TX"
            step: _Step | None = None
            if kind == "S":
                if mine and quoting:
                    verb = "quote" if subject == vocab.RFQ else "requote"
                    step = _Step(m["at"], "did", verb, self._quote_terms(msg, session_id), stamp=m["timestamp"])
                elif not mine and not quoting:
                    step = _Step(m["at"], "heard", "requoted" if quoted else "quoted")
                quoted = True
            elif kind == "Z":
                step = _Step(m["at"], "did", "cancel quote", self._said(msg, session_id), stamp=m["timestamp"]) \
                    if mine else _Step(m["at"], "heard", "canceled")
            elif kind == "AG":
                step = _Step(m["at"], "did", "reject rfq", {"reason": msg.get("658", ""), **self._said(msg, session_id)},
                             stamp=m["timestamp"]) if mine else _Step(m["at"], "heard", "rejected")
            elif kind == "AI" and not mine:
                step = _Step(m["at"], "heard", "expired" if msg.get("297") == "7" else "status")
            elif kind == "AJ":
                if mine:
                    step = self._response(m, session_id)
                else:
                    step = _Step(m["at"], "heard", _RESPONSE_HEARD.get(msg.get("694", ""), "response"))
            elif kind == "D":
                if mine:
                    client, extra = self._extras(session_id, msg)
                    step = _Step(m["at"], "did", "new",
                                 self._creator_terms(vocab.ORDER, self._order_terms(msg, client, extra)),
                                 stamp=m["timestamp"])
                else:
                    step = _Step(m["at"], "heard", "hit")
            if step is not None and (step.kind == "did" or step.name in heard):
                step.terms = {k: v for k, v in step.terms.items() if v not in (None, "")}
                line.steps.append(step)
        return line

    async def _rfq_request(self, row: dict[str, Any]) -> _Timeline | None:
        """An RFQ request, its unsubscribe, and the RFQs that answered it."""
        session_id, sent = row["session_id"], row["direction"] == "TX"
        marks = (f"{SOH}644={row['rfq_req_id']}{SOH}",)
        messages = await self._messages(session_id, "msg_type IN ('AH', 'R') AND instr(raw_message, ?) > 0", marks)
        first, messages = self._life(
            messages, lambda m: m["msg_type"] == "AH" and m["msg"].get("263") != "2"
            and m["direction"] == ("TX" if sent else "RX"), row["timestamp"])
        if first is None:
            self._leave_out(f"{row['rfq_req_id']}: its RFQRequest is not among the messages kept")
            return None
        client, extra = self._extras(session_id, first["msg"])
        line = _Timeline({**row, **rfq_request_columns(first["msg"], self._dictionary(session_id, first["msg"])),
                          "client": client, "extra_tags": extra}, first["at"], subject=vocab.RFQ_REQUEST)
        heard = _HEARD[(vocab.RFQ_REQUEST, vocab.CLIENT if sent else vocab.MARKET)]
        for m in messages:
            msg, mine = m["msg"], m["direction"] == "TX"
            step: _Step | None = None
            if m["msg_type"] == "AH" and msg.get("263") == "2":
                _, extra = self._extras(session_id, msg)
                step = _Step(m["at"], "did", "unsubscribe", {"extra": extra}, stamp=m["timestamp"]) if mine \
                    else _Step(m["at"], "heard", "unsubscribed")
            elif m["msg_type"] == "R" and mine and not sent:
                client, extra = self._extras(session_id, msg)
                terms = self._creator_terms(vocab.RFQ, {**rfq_columns(msg, self._dictionary(session_id, msg)),
                                                        "client": client, "sent_text": msg.get("58", ""),
                                                        "extra_tags": extra})
                step = _Step(m["at"], "did", "rfq", terms, stamp=m["timestamp"])
            if step is not None and (step.kind == "did" or step.name in heard):
                step.terms = {k: v for k, v in step.terms.items() if v not in (None, "")}
                line.steps.append(step)
        return line

    # -- writing -----------------------------------------------------------------------------

    async def write(self, subject: str, row_ids: list[int]) -> dict[str, Any]:
        """The macro for these rows of ``subject``'s table: `{source, orders,
        actions, left_out}`. Every row has to be of this side — sent by it,
        or received by it — as the rows of one blotter are."""
        if subject not in vocab.SUBJECTS:
            raise ValueError(f"A macro is about an order, an IOI, an advert, an allocation, an RFQ, a quote or an "
                             f"RFQ request, not {subject!r}")
        table, id_col = vocab.SUBJECT_TABLES[subject], vocab.SUBJECT_IDS[subject]
        wanted = list(dict.fromkeys(int(r) for r in row_ids))
        if not wanted:
            raise ValueError(f"Choose the {vocab.PLURALS[subject]} to write a macro from")
        rows = await self._fetch(f"SELECT * FROM {table} WHERE id IN ({','.join('?' * len(wanted))}) ORDER BY id", tuple(wanted))
        if table == "fix_rfqs":
            rows = [r for r in rows if r["origin"] == subject]
        if len(rows) != len(wanted):
            gone = sorted(set(wanted) - {r["id"] for r in rows})
            raise ValueError(f"No {subject} with row {', '.join(map(str, gone))}: archived, perhaps")
        direction = "TX" if self._sends(subject) else "RX"
        for row in rows:
            if row["direction"] != direction:
                raise ValueError(f"{row[id_col]} was {'sent' if row['direction'] == 'TX' else 'received'}: "
                                 f"a macro for it is a {'market' if self.side == 'client' else 'client'} macro")
        lines = []
        for row in rows:
            if subject == vocab.ORDER:
                line = await self._order(row)
            elif subject in (vocab.RFQ, vocab.QUOTE):
                line = await self._negotiation(subject, row)
            elif subject == vocab.RFQ_REQUEST:
                line = await self._rfq_request(row)
            elif subject == vocab.LIST:
                line = await self._list(row)
            else:
                line = await self._family(subject, row)
            # What we received and never answered has nothing to write, as in a recording.
            if line is not None and (line.sent or any(s.kind == "did" for s in line.steps)):
                lines.append(line)
            elif line is not None:
                self._leave_out(f"{row[id_col]}: nothing was done to it")
        names = [row[id_col] for row in rows]
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        notes = tuple(f"# Left out — {what}." for what in self.left_out)
        result = await self._write(lines, f"from the history of {shown}",
                                   nothing="# Nothing to write: " + ("none of them was answered by this side."
                                                                     if not self._sends(subject) else "their messages are gone."),
                                   notes=notes, took="it took")
        result["left_out"] = list(self.left_out)
        return result

    async def _target(self, line: _Timeline, step: _Step) -> str:          # every step comes with its target
        return step.target or "last trade"

