"""FIX message: parse, compose, serialize, checksum."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, NamedTuple

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.instrument import INSTRUMENT_TAGS, POSITION_TAGS, instrument_pairs

SOH = chr(1)


class FixMessage:
    """A single FIX message as an ordered dict of tag->value pairs.

    `extra` holds user-supplied (tag, value) pairs applied at sendprep time.
    Unlike `fields`, it is an ordered list that allows duplicate tags, which
    is all a repeating group is on the wire.
    """

    def __init__(self, fields: dict[str, str] | None = None,
                 pairs: list[tuple[str, str]] | None = None,
                 raw: bytes | None = None):
        self.fields: dict[str, str] = {}
        self.extra: list[tuple[str, str]] = []
        # The exact bytes this message had on the wire: set by the stream
        # parser on receive and by the socket on send. What gets recorded.
        self.raw: bytes | None = raw
        # Parsed messages carry their ordered wire pairs (duplicates included —
        # repeating groups); sendprep replaces them with the composed output.
        self._pairs: list[tuple[str, str]] | None = (
            [(str(t), str(v)) for t, v in pairs] if pairs is not None else None
        )
        if fields:
            for k, v in fields.items():
                self.fields[str(k)] = str(v)

    def __setitem__(self, key: str, value: Any) -> None:
        if value is not None:
            self.fields[str(key)] = str(value)

    def __getitem__(self, key: str) -> str | None:
        return self.fields.get(str(key))

    def __contains__(self, key: str) -> bool:
        return str(key) in self.fields

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.fields.get(str(key), default)

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.fields.get(str(key))
        if v is None:
            return default
        try:
            return int(v)
        except ValueError:
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        v = self.fields.get(str(key))
        if v is None:
            return default
        try:
            return float(v)
        except ValueError:
            return default

    def sendprep(self, dictionary: FixDictionary, sender: str, target: str, seq_num: int,
                 timestamp_precision: str | None = None) -> None:
        """Prepare message for sending: add header/trailer, body length, checksum.

        `extra` pairs are applied by wire position with these rules:
        - empty value deletes the tag (even a computed one like 52)
        - a tag appearing once in extras that the message would emit anyway
          replaces the value in place (works on 34, 52, even 9/10)
        - everything else appends in given order: header tags at the end of
          the header block, trailer tags before 10, the rest after the body —
          duplicates allowed, which is how repeating groups go on the wire
        """
        deleted = {t for t, v in self.extra if v == ""}
        live = [(t, v) for t, v in self.extra if v != ""]
        counts: dict[str, int] = {}
        for t, _ in live:
            counts[t] = counts.get(t, 0) + 1

        computed = {"8", "9", "10", "49", "56", "34", "52"}
        overrides: dict[str, str] = {}
        appends: list[tuple[str, str]] = []
        for t, v in live:
            if counts[t] == 1 and (t in self.fields or t in computed):
                overrides[t] = v
            else:
                appends.append((t, v))

        header: list[tuple[str, str]] = []
        for tag in dictionary.header_tags:
            if dictionary.is_special(tag) or tag in deleted:
                continue
            if tag in overrides:
                header.append((tag, overrides.pop(tag)))
            elif tag in self.fields:
                header.append((tag, self.fields[tag]))
            elif tag == "49":
                header.append((tag, sender))
            elif tag == "56":
                header.append((tag, target))
            elif tag == "34":
                header.append((tag, str(seq_num)))
            elif tag == "52":
                precision = timestamp_precision or standard_precision(dictionary.begin_string())
                header.append((tag, _fix_timestamp(precision)))
        header += [(t, v) for t, v in appends
                   if dictionary.is_header(t) and not dictionary.is_special(t)]

        body: list[tuple[str, str]] = []
        for tag, value in self.fields.items():
            if dictionary.is_header(tag) or dictionary.is_trailer(tag) or tag in deleted:
                continue
            body.append((tag, overrides.pop(tag, value)))
        body += [(t, v) for t, v in appends
                 if not dictionary.is_header(t) and not dictionary.is_trailer(t)]

        trailer: list[tuple[str, str]] = []
        for tag in dictionary.trailer_tags:
            if dictionary.is_special(tag) or tag in deleted:
                continue
            if tag in overrides:
                trailer.append((tag, overrides.pop(tag)))
            elif tag in self.fields:
                trailer.append((tag, self.fields[tag]))
        trailer += [(t, v) for t, v in appends
                    if dictionary.is_trailer(t) and not dictionary.is_special(t)]

        pairs = header + body + trailer
        body_length = len(_serialize_pairs(pairs))

        final: list[tuple[str, str]] = []
        if "8" not in deleted:
            final.append(("8", overrides.get("8") or self.fields.get("8") or dictionary.begin_string()))
        if "9" not in deleted:
            final.append(("9", overrides.get("9", str(body_length))))
        final += pairs
        if "10" not in deleted:
            if "10" in overrides:
                final.append(("10", overrides["10"]))
            else:
                checksum = _checksum(_serialize_pairs(final))
                final.append(("10", f"{checksum:03d}"))

        self._pairs = final
        self.fields = dict(final)

    def _items(self) -> list[tuple[str, str]]:
        return self._pairs if self._pairs is not None else list(self.fields.items())

    def serialize_without_checksum(self) -> bytes:
        return _serialize_pairs([(t, v) for t, v in self._items() if t != "10"])

    def serialize(self) -> bytes:
        return _serialize_pairs(self._items())

    def to_pipe_string(self) -> str:
        return "|".join(f"{tag}={value}" for tag, value in self._items())

    def to_wire_string(self) -> str:
        """The message exactly as it was (or would be) on the wire, SOH
        delimiters included, as a latin-1 string so every byte survives a
        trip through SQLite TEXT and JSON. This is what gets stored; the
        pipe form is only a rendering."""
        raw = self.raw if self.raw is not None else self.serialize()
        return raw.decode("latin-1")

    def retransmit_copy(self, dictionary: FixDictionary, sending_time: str) -> FixMessage:
        """A PossDup copy of a message that already went out: same pairs and
        MsgSeqNum (repeating groups intact), PossDupFlag(43)=Y, the original
        SendingTime moved to OrigSendingTime(122), a fresh 52, and BodyLength
        and CheckSum recomputed."""
        pairs = [(t, v) for t, v in self._items()
                 if t not in ("8", "9", "10", "43", "122")]
        orig_time = self.fields.get("52", "")
        header: list[tuple[str, str]] = []
        for t, v in pairs:
            if t == "52":
                if dictionary.defines("43"):
                    header.append(("43", "Y"))
                header.append(("52", sending_time))
                if dictionary.defines("122") and orig_time:
                    header.append(("122", orig_time))
            else:
                header.append((t, v))
        pairs = header
        if "52" not in self.fields and dictionary.defines("43"):
            pairs.insert(0, ("43", "Y"))

        body_length = len(_serialize_wire(pairs))
        final = [("8", self.fields.get("8") or dictionary.begin_string()),
                 ("9", str(body_length))] + pairs
        final.append(("10", f"{_checksum(_serialize_wire(final)):03d}"))
        copy = FixMessage(dict(final), pairs=final)
        copy.raw = _serialize_wire(final)
        return copy

    def __str__(self) -> str:
        return self.to_pipe_string()


class FixMessageFactory:
    """Creates FixMessage instances with session-level defaults."""

    def __init__(self, dictionary: FixDictionary, sender: str, target: str,
                 timestamp_precision: str | None = None):
        self.dictionary = dictionary
        self.sender = sender
        self.target = target
        self.timestamp_precision = (
            timestamp_precision or standard_precision(dictionary.begin_string()))

    def _now(self) -> str:
        return _fix_timestamp(self.timestamp_precision)

    def _strip_legacy_body_time(self, fields: dict[str, str]) -> None:
        # TransactTime(60) joined D/F/G/9 in FIX 4.2; earlier versions
        # don't define it on those messages.
        if self.dictionary.begin_string() in ("FIX.4.0", "FIX.4.1"):
            fields.pop("60", None)

    def _add_expiry(self, fields: dict[str, str], expire_time: str, expire_date: str,
                    precision: str = "") -> None:
        """ExpireTime(126) and ExpireDate(432) go out only when given and the
        dictionary defines them — 432 joined in FIX 4.2. Values arrive as
        FIX stamps or as the dialog's ISO forms (see `normalize_expire_time`)."""
        expire_time, expire_date = self.expiry(expire_time, expire_date, precision)
        if expire_time and self.dictionary.defines("126"):
            fields["126"] = expire_time
        if expire_date and self.dictionary.defines("432"):
            fields["432"] = expire_date

    def _add_handling(self, fields: dict[str, str], text: str | None) -> None:
        """HandlInst(21) rides only when given and defined (a custom
        dictionary may remove it); Text(58) only when given."""
        if not fields.get("21") or not self.dictionary.defines("21"):
            fields.pop("21", None)
        if text:
            fields["58"] = text

    def expiry(self, expire_time: str, expire_date: str, precision: str = "") -> tuple[str, str]:
        """(ExpireTime, ExpireDate) as they would go out. The order dialog has
        one Expire field whose time is optional: a bare date arriving as
        `expire_time` is an ExpireDate, not an ExpireTime at midnight."""
        if is_bare_date(expire_time):
            expire_date = expire_date or expire_time
            expire_time = ""
        return (self.expire_time_stamp(expire_time, precision),
                normalize_expire_date(expire_date))

    def expire_time_stamp(self, value: str, precision: str = "") -> str:
        """The ExpireTime this factory would send: `''` precision means the
        session's own timestamp precision."""
        return normalize_expire_time(value, precision or self.timestamp_precision,
                                     explicit=bool(precision))

    def wire_exec_codes(self, exec_trans_type: str, exec_type: str) -> tuple[str | None, str | None]:
        """Translate FIX 4.2-style execution codes to this dictionary's wire.

        Callers pass 4.2 semantics: ExecTransType(20) 0/1/2 (New/Cancel/
        Correct) plus a 4.2 ExecType(150). Versions defining tag 20 (<= 4.2)
        pass through unchanged; versions without it (4.3+) express fills as
        150=F and corrects/busts as 150=G/H; FIX 4.0 has no ExecType at all.
        """
        d = self.dictionary
        trans_type = exec_trans_type if d.defines("20") else None
        if not d.defines("150"):
            return trans_type, None
        wire = exec_type
        if trans_type is None:
            if exec_trans_type == "1" and d.has_enum("150", "H"):
                wire = "H"
            elif exec_trans_type == "2" and d.has_enum("150", "G"):
                wire = "G"
            elif exec_type in ("1", "2") and d.has_enum("150", "F"):
                wire = "F"
        return trans_type, wire

    def create(self, fields: dict[str, str] | None = None) -> FixMessage:
        msg = FixMessage(fields)
        if "8" not in msg.fields:
            msg["8"] = self.dictionary.begin_string()
        return msg

    def client_fields(self, client: str, specs: list[ClientTag]) -> dict[str, str]:
        """The pairs that put `client` where this counterparty reads it: the
        first spec the dictionary defines, else the first spec as written (a
        custom tag). A group spec goes out as a one-instance group — the
        counter where the dictionary knows it, the member, PartyIDSource(447)
        as D (proprietary) where defined since the Parties group requires it,
        then the qualifier. Header tags (115) land in the header at sendprep."""
        if not client:
            return {}
        spec = next((s for s in specs if self.dictionary.defines(s.tag)), specs[0])
        if spec.qualifier is None:
            return {spec.tag: client}
        qualifier_tag, qualifier_value = spec.qualifier
        fields: dict[str, str] = {}
        counter = next((c for c, g in self.dictionary.groups.items()
                        if g.get("delim") == spec.tag), None)
        if counter:
            fields[counter] = "1"
        fields[spec.tag] = client
        if spec.tag == "448" and self.dictionary.defines("447"):
            fields["447"] = "D"
        fields[qualifier_tag] = qualifier_value
        return fields

    def stamp_client(self, msg: FixMessage, client: str, specs: list[ClientTag]) -> None:
        """Put `client` on an outgoing message. Applied before `extra`, so an
        extra tag naming the same tag overrides it at sendprep."""
        for tag, value in self.client_fields(client, specs).items():
            msg[tag] = value

    def stamp_instrument(self, msg: FixMessage, row: Mapping[str, Any], originating: bool = True) -> None:
        """Put an instrument (row values, see instrument.py) on an outgoing
        message in this version's spelling. Applied before `extra`, so an
        extra tag naming one of its tags overrides it at sendprep."""
        for tag, value in instrument_pairs(self.dictionary, row, originating):
            msg[tag] = value

    def heartbeat(self, test_req_id: str | None = None) -> FixMessage:
        fields: dict[str, str] = {"35": "0"}
        if test_req_id:
            fields["112"] = test_req_id
        return self.create(fields)

    def test_request(self, test_req_id: str) -> FixMessage:
        return self.create({"35": "1", "112": test_req_id})

    def logon(self, heartbeat_interval: int = 30, reset_seq_num: bool = False) -> FixMessage:
        fields: dict[str, str] = {
            "35": "A",
            "98": "0",
            "108": str(heartbeat_interval),
        }
        if reset_seq_num and self.dictionary.defines("141"):
            fields["141"] = "Y"
        if self.dictionary.defines("1137"):
            fields["1137"] = _APPL_VER_IDS.get(self.dictionary.version, "9")
        return self.create(fields)

    def logout(self, text: str | None = None) -> FixMessage:
        fields: dict[str, str] = {"35": "5"}
        if text:
            fields["58"] = text
        return self.create(fields)

    def resend_request(self, begin_seq: int, end_seq: int = 0) -> FixMessage:
        return self.create({"35": "2", "7": str(begin_seq), "16": str(end_seq)})

    def sequence_reset(self, new_seq: int, gap_fill: bool = False) -> FixMessage:
        fields: dict[str, str] = {"35": "4", "36": str(new_seq)}
        if gap_fill:
            fields["123"] = "Y"
            fields["43"] = "Y"
        return self.create(fields)

    def reject(self, ref_seq_num: int, text: str | None = None, reason: int | None = None) -> FixMessage:
        fields: dict[str, str] = {"35": "3", "45": str(ref_seq_num)}
        if text:
            fields["58"] = text
        if reason is not None:
            fields["373"] = str(reason)
        return self.create(fields)

    def new_order_single(
        self,
        cl_ord_id: str,
        symbol: str,
        side: str,
        qty: float,
        ord_type: str = "2",
        price: float | None = None,
        tif: str = "0",
        account: str | None = None,
        handl_inst: str = "1",
        expire_time: str = "",
        expire_date: str = "",
        expire_precision: str = "",
        text: str | None = None,
        **extra: str,
    ) -> FixMessage:
        fields: dict[str, str] = {
            "35": "D",
            "11": cl_ord_id,
            "55": symbol,
            "54": side,
            "38": str(int(qty)),
            "40": ord_type,
            "59": tif,
            "21": handl_inst,
            "60": self._now(),
        }
        if price is not None:
            fields["44"] = str(price)
        if account:
            fields["1"] = account
        self._add_expiry(fields, expire_time, expire_date, expire_precision)
        self._add_handling(fields, text)
        fields.update(extra)
        self._strip_legacy_body_time(fields)
        return self.create(fields)

    def cancel_request(
        self,
        cl_ord_id: str,
        orig_cl_ord_id: str,
        symbol: str,
        side: str,
        qty: float = 0,
        text: str | None = None,
    ) -> FixMessage:
        fields: dict[str, str] = {
            "35": "F",
            "11": cl_ord_id,
            "41": orig_cl_ord_id,
            "55": symbol,
            "54": side,
            "60": self._now(),
        }
        if qty:
            fields["38"] = str(int(qty))
        if self.dictionary.begin_string() == "FIX.4.0":
            fields["125"] = "F"  # CxlType, required on a 4.0 OrderCancelRequest
        if text:
            fields["58"] = text
        self._strip_legacy_body_time(fields)
        return self.create(fields)

    def execution_report(
        self,
        order_id: str,
        cl_ord_id: str,
        exec_id: str,
        exec_trans_type: str,
        exec_type: str,
        ord_status: str,
        symbol: str,
        side: str,
        qty: float,
        last_qty: float = 0.0,
        last_price: float = 0.0,
        cum_qty: float = 0.0,
        avg_price: float = 0.0,
        leaves_qty: float = 0.0,
        exec_ref_id: str | None = None,
        text: str | None = None,
        **extra: str,
    ) -> FixMessage:
        trans_type, wire_exec_type = self.wire_exec_codes(exec_trans_type, exec_type)
        fields: dict[str, str] = {
            "35": "8",
            "37": order_id,
            "11": cl_ord_id,
            "17": exec_id,
        }
        if trans_type is not None:
            fields["20"] = trans_type
        if wire_exec_type is not None:
            fields["150"] = wire_exec_type
        fields.update({
            "39": ord_status,
            "55": symbol,
            "54": side,
            "38": str(int(qty)),
            "32": str(int(last_qty)),
            "31": str(last_price),
            "14": str(int(cum_qty)),
            "6": str(avg_price),
        })
        if self.dictionary.defines("151"):
            fields["151"] = str(int(leaves_qty))
        fields["60"] = self._now()
        if exec_ref_id:
            fields["19"] = exec_ref_id
        if text:
            fields["58"] = text
        fields.update(extra)
        return self.create(fields)

    def cancel_replace_request(
        self,
        cl_ord_id: str,
        orig_cl_ord_id: str,
        symbol: str,
        side: str,
        qty: float,
        ord_type: str = "2",
        price: float | None = None,
        tif: str | None = None,
        handl_inst: str = "1",
        expire_time: str = "",
        expire_date: str = "",
        expire_precision: str = "",
        text: str | None = None,
        **extra: str,
    ) -> FixMessage:
        fields: dict[str, str] = {
            "35": "G",
            "11": cl_ord_id,
            "41": orig_cl_ord_id,
            "55": symbol,
            "54": side,
            "38": str(int(qty)),
            "40": ord_type,
            "21": handl_inst,
            "60": self._now(),
        }
        if price is not None:
            fields["44"] = str(price)
        if tif is not None:
            fields["59"] = tif
        self._add_expiry(fields, expire_time, expire_date, expire_precision)
        self._add_handling(fields, text)
        fields.update(extra)
        self._strip_legacy_body_time(fields)
        return self.create(fields)

    def order_cancel_reject(
        self,
        cl_ord_id: str,
        orig_cl_ord_id: str,
        ord_status: str,
        response_to: str,
        order_id: str = "",
        text: str | None = None,
        reason: str = "",
    ) -> FixMessage:
        """OrderCancelReject (35=9); response_to is CxlRejResponseTo(434):
        1 = cancel request, 2 = cancel/replace request. `reason` is
        CxlRejReason(102), sent where the dictionary defines the tag."""
        fields: dict[str, str] = {
            "35": "9",
            "37": order_id or "NONE",
            "11": cl_ord_id,
            "41": orig_cl_ord_id,
            "39": ord_status,
            "434": response_to,
            "60": self._now(),
        }
        if text:
            fields["58"] = text
        if reason and self.dictionary.defines("102"):
            fields["102"] = reason
        if not self.dictionary.defines("434"):
            fields.pop("434")
        self._strip_legacy_body_time(fields)
        return self.create(fields)

    def _legacy_body(self) -> bool:
        """FIX 4.0 through 4.2: the versions whose IOI carries no
        TransactTime(60) — it joined the message in 4.3."""
        return self.dictionary.begin_string() in ("FIX.4.0", "FIX.4.1", "FIX.4.2")

    def ioi(
        self,
        ioi_id: str,
        trans_type: str,
        symbol: str,
        side: str,
        qty: str,
        price: float | None = None,
        ref_id: str = "",
        valid_until: str = "",
        qlty_ind: str = "",
        natural_flag: str = "",
        qualifiers: list[str] | tuple[str, ...] = (),
        currency: str = "",
        text: str | None = None,
    ) -> FixMessage:
        """IOI (35=6): IOITransType(28) N/C/R with IOIRefID(26) naming the one
        replaced or canceled. IOIQty(27) takes a number or S/M/L and goes as
        given; the quality indicator, natural flag and qualifier group ride
        only where defined and given; TransactTime(60) from FIX 4.3."""
        fields: dict[str, str] = {"35": "6", "23": ioi_id, "28": trans_type}
        if ref_id and self.dictionary.defines("26"):
            fields["26"] = ref_id
        fields.update({"55": symbol, "54": side, "27": qty})
        if price is not None:
            fields["44"] = str(price)
        if currency:
            fields["15"] = currency
        if valid_until:
            fields["62"] = valid_until
        if qlty_ind and self.dictionary.defines("25"):
            fields["25"] = qlty_ind
        if natural_flag in ("Y", "N") and self.dictionary.defines("130"):
            fields["130"] = natural_flag
        if text:
            fields["58"] = text
        if not self._legacy_body():
            fields["60"] = self._now()
        msg = self.create(fields)
        qualifiers = [q for q in qualifiers if q]
        if qualifiers and self.dictionary.defines("199"):
            msg.extra = [("199", str(len(qualifiers)))] + [("104", q) for q in qualifiers]
        return msg

    def advertisement(
        self,
        adv_id: str,
        trans_type: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None = None,
        ref_id: str = "",
        currency: str = "",
        trade_date: str = "",
        last_mkt: str = "",
        text: str | None = None,
    ) -> FixMessage:
        """Advertisement (35=7): AdvTransType(5) N/C/R with AdvRefID(3);
        AdvSide(4) is B/S/X/T, Shares/Quantity(53) the size."""
        fields: dict[str, str] = {"35": "7", "2": adv_id, "5": trans_type}
        if ref_id:
            fields["3"] = ref_id
        fields.update({"55": symbol, "4": side, "53": _qty(qty)})
        if price is not None:
            fields["44"] = str(price)
        if currency:
            fields["15"] = currency
        if trade_date:
            fields["75"] = normalize_expire_date(trade_date)
        fields["60"] = self._now()
        if text:
            fields["58"] = text
        if last_mkt:
            fields["30"] = last_mkt
        return self.create(fields)

    def allocation_instruction(
        self,
        alloc_id: str,
        trans_type: str,
        symbol: str,
        side: str,
        qty: float,
        avg_price: float,
        trade_date: str = "",
        alloc_type: str = "",
        ref_alloc_id: str = "",
        orders: list[dict[str, str]] | None = None,
        execs: list[dict[str, str]] | None = None,
        allocs: list[dict[str, str]] | None = None,
        text: str | None = None,
    ) -> FixMessage:
        """AllocationInstruction (35=J; Allocation through FIX 4.2):
        AllocTransType(71) 0/1/2 with RefAllocID(72) naming the one replaced
        or canceled, AllocType(626) where defined (4.3+), TradeDate(75)
        today when not given. The three groups — NoOrders(73), NoExecs(124),
        NoAllocs(78) — ride as ordered pairs on `extra`, each instance's
        members as `families.ALLOC_GROUPS` lists them."""
        from mkfix.fix.families import ALLOC_GROUPS, group_pairs
        fields: dict[str, str] = {"35": "J", "70": alloc_id, "71": trans_type}
        if ref_alloc_id:
            fields["72"] = ref_alloc_id
        if alloc_type and self.dictionary.defines("626"):
            fields["626"] = alloc_type
        fields.update({
            "55": symbol, "54": side, "53": _qty(qty), "6": str(avg_price),
            "75": normalize_expire_date(trade_date) or self._today(),
            "60": self._now(),
        })
        if text:
            fields["58"] = text
        msg = self.create(fields)
        given = {"orders": orders or [], "execs": execs or [], "allocs": allocs or []}
        for column, (counter, members) in ALLOC_GROUPS.items():
            msg.extra += group_pairs(counter, members, given[column], self.dictionary)
        return msg

    def allocation_ack(
        self,
        alloc_id: str,
        status: str,
        trade_date: str = "",
        rej_code: str = "",
        text: str | None = None,
    ) -> FixMessage:
        """AllocationInstructionAck (35=P; AllocationACK through FIX 4.2)
        answering the AllocID(70) named: AllocStatus(87), AllocRejCode(88)
        when given, TradeDate(75) the allocation's or today."""
        fields: dict[str, str] = {
            "35": "P", "70": alloc_id, "75": normalize_expire_date(trade_date) or self._today(),
            "60": self._now(), "87": status,
        }
        if rej_code:
            fields["88"] = rej_code
        if text:
            fields["58"] = text
        return self.create(fields)

    def _today(self) -> str:
        return self._now()[:8]

    # ── RFQs and quotes ──────────────────────────────────────────────
    # FIX 4.0/4.1 carry a QuoteRequest's instrument in the body; from 4.2 it
    # rides one NoRelatedSym(146) instance, where Side, OrderQty and the
    # request's types live too. The later messages come with the versions:
    # QuoteCancel 4.2, QuoteRequestReject 4.3, QuoteResponse 4.4 — the
    # engine refuses one the session's dictionary does not define.

    def _flat_quote_request(self) -> bool:
        return self.dictionary.begin_string() in ("FIX.4.0", "FIX.4.1")

    def quote_request(
        self,
        quote_req_id: str,
        symbol: str,
        side: str = "",
        qty: float | None = None,
        quote_request_type: str = "",
        quote_type: str = "",
        currency: str = "",
        text: str | None = None,
    ) -> FixMessage:
        """QuoteRequest (35=R): QuoteReqID(131) and one instrument. Side and
        OrderQty ride when given (a blank side asks a two-way price);
        QuoteRequestType(303), QuoteType(537), Currency and TransactTime
        only where the version's group defines them."""
        fields: dict[str, str] = {"35": "R", "131": quote_req_id}
        if text:
            fields["58"] = text
        if self._flat_quote_request():
            fields["55"] = symbol
            if side:
                fields["54"] = side
            if qty:
                fields["38"] = _qty(qty)
            return self.create(fields)
        msg = self.create(fields)
        pairs = [("146", "1"), ("55", symbol)]
        for tag, value in (("303", quote_request_type), ("537", quote_type), ("54", side),
                           ("38", _qty(qty) if qty else ""), ("15", currency), ("60", self._now())):
            if value and self.dictionary.defines(tag):
                pairs.append((tag, value))
        msg.extra = pairs
        return msg

    def quote(
        self,
        quote_id: str,
        symbol: str,
        quote_req_id: str = "",
        bid_px: float | None = None,
        offer_px: float | None = None,
        bid_size: float | None = None,
        offer_size: float | None = None,
        valid_until: str = "",
        quote_type: str = "",
        side: str = "",
        qty: float | None = None,
        currency: str = "",
        text: str | None = None,
    ) -> FixMessage:
        """Quote (35=S): QuoteID(117), the QuoteReqID(131) it answers (none
        for an unsolicited quote), bid and offer. QuoteType(537) joined in
        4.3, Side and OrderQty in 4.4, TransactTime and Currency in 4.2."""
        fields: dict[str, str] = {"35": "S"}
        if quote_req_id:
            fields["131"] = quote_req_id
        fields["117"] = quote_id
        if quote_type and self.dictionary.defines("537"):
            fields["537"] = quote_type
        fields["55"] = symbol
        modern = self.dictionary.begin_string() not in ("FIX.4.0", "FIX.4.1", "FIX.4.2", "FIX.4.3")
        if modern and side:
            fields["54"] = side
        if modern and qty:
            fields["38"] = _qty(qty)
        for tag, value in (("132", bid_px), ("133", offer_px), ("134", bid_size), ("135", offer_size)):
            if value is not None:
                fields[tag] = _qty(value) if tag in ("134", "135") else str(value)
        if valid_until:
            fields["62"] = valid_until
        if not self._flat_quote_request():
            fields["60"] = self._now()
            if currency:
                fields["15"] = currency
        if text:
            fields["58"] = text
        return self.create(fields)

    def quote_cancel(self, quote_id: str, symbol: str, quote_req_id: str = "",
                     text: str | None = None) -> FixMessage:
        """QuoteCancel (35=Z, FIX 4.2+) of one instrument's quote:
        QuoteCancelType(298)=1 with the symbol in NoQuoteEntries(295)."""
        fields: dict[str, str] = {"35": "Z"}
        if quote_req_id:
            fields["131"] = quote_req_id
        fields.update({"117": quote_id, "298": "1"})
        if text and self.dictionary.defines("58"):
            fields["58"] = text
        msg = self.create(fields)
        msg.extra = [("295", "1"), ("55", symbol)]
        return msg

    def quote_request_reject(self, quote_req_id: str, symbol: str, reason: str,
                             text: str | None = None) -> FixMessage:
        """QuoteRequestReject (35=AG, FIX 4.3+): the QuoteReqID refused,
        QuoteRequestRejectReason(658) and the instrument group it requires."""
        fields: dict[str, str] = {"35": "AG", "131": quote_req_id, "658": reason}
        if text:
            fields["58"] = text
        msg = self.create(fields)
        msg.extra = [("146", "1"), ("55", symbol)]
        return msg

    def rfq_request(self, rfq_req_id: str, symbols: list[str], subscription_type: str = "1",
                    quote_request_type: str = "", quote_type: str = "") -> FixMessage:
        """RFQRequest (35=AH, FIX 4.3+): RFQReqID(644), one NoRelatedSym(146)
        instance per instrument — each carrying the request and quote
        types when given — then SubscriptionRequestType(263)."""
        msg = self.create({"35": "AH", "644": rfq_req_id})
        pairs: list[tuple[str, str]] = [("146", str(len(symbols)))] if symbols else []
        for symbol in symbols:
            pairs.append(("55", symbol))
            for tag, value in (("303", quote_request_type), ("537", quote_type)):
                if value and self.dictionary.defines(tag):
                    pairs.append((tag, value))
        if subscription_type:
            pairs.append(("263", subscription_type))
        msg.extra = pairs
        return msg

    def quote_response(
        self,
        quote_resp_id: str,
        quote_id: str,
        resp_type: str,
        symbol: str,
        cl_ord_id: str = "",
        side: str = "",
        qty: float | None = None,
        ord_type: str = "",
        price: float | None = None,
        bid_px: float | None = None,
        offer_px: float | None = None,
        bid_size: float | None = None,
        offer_size: float | None = None,
        text: str | None = None,
    ) -> FixMessage:
        """QuoteResponse (35=AJ, FIX 4.4+): QuoteRespID(693) answering
        QuoteID(117) by QuoteRespType(694) — a Hit carries the order it
        makes (ClOrdID, Side, OrderQty, OrdType, Price), a Counter its bid
        and offer, a Pass nothing more."""
        fields: dict[str, str] = {"35": "AJ", "693": quote_resp_id, "117": quote_id, "694": resp_type}
        if cl_ord_id:
            fields["11"] = cl_ord_id
        fields["55"] = symbol
        if side:
            fields["54"] = side
        if qty:
            fields["38"] = _qty(qty)
        for tag, value in (("132", bid_px), ("133", offer_px), ("134", bid_size), ("135", offer_size)):
            if value is not None:
                fields[tag] = _qty(value) if tag in ("134", "135") else str(value)
        fields["60"] = self._now()
        if ord_type:
            fields["40"] = ord_type
        if price is not None:
            fields["44"] = str(price)
        if text:
            fields["58"] = text
        return self.create(fields)

    def dont_know_trade(
        self,
        order_id: str,
        exec_id: str,
        dk_reason: str,
        symbol: str,
        side: str,
        qty: float,
        last_qty: float = 0.0,
        last_price: float = 0.0,
        text: str | None = None,
    ) -> FixMessage:
        """DontKnowTrade (35=Q) answering a received ExecutionReport: OrderID(37)
        and ExecID(17) are the counterparty's own identifiers for the trade,
        DKReason(127) the code; LastShares/LastPx ride only when known."""
        fields: dict[str, str] = {
            "35": "Q",
            "37": order_id,
            "17": exec_id,
            "127": dk_reason,
            "55": symbol,
            "54": side,
            "38": str(int(qty)),
        }
        if last_qty:
            fields["32"] = str(int(last_qty))
        if last_price:
            fields["31"] = str(last_price)
        if text:
            fields["58"] = text
        return self.create(fields)


# Where the client rides, per counterparty: an ordered list of tag specs, the
# first one present on a message naming the client. A spec is a tag, or a
# repeating-group member qualified by a sibling — `448[452=3]` is the PartyID
# whose PartyRole is 3 (ClientID, the FIX 4.3+ form). The default chain covers
# the standard places in order: the Parties group, ClientID(109) through 4.2,
# OnBehalfOfCompID(115) in the header, Account(1).
DEFAULT_CLIENT_TAGS = "448[452=3],109,115,1"

_CLIENT_SPEC_RE = re.compile(r"^(\d+)(?:\[(\d+)=([^\]]*)\])?$")


class ClientTag(NamedTuple):
    tag: str
    qualifier: tuple[str, str] | None = None


def _qty(value: float) -> str:
    """A quantity on the wire: whole numbers without a fraction."""
    return str(int(value)) if float(value) == int(value) else str(value)


def parse_client_tags(spec: str) -> list[ClientTag]:
    """Parse a session's client_tags: comma-separated `tag` or
    `tag[qualifier=value]` specs, blank meaning DEFAULT_CLIENT_TAGS."""
    text = (spec or "").strip() or DEFAULT_CLIENT_TAGS
    specs: list[ClientTag] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        match = _CLIENT_SPEC_RE.match(part)
        if not match:
            raise ValueError(
                f"Invalid client tag {part!r} (expected a tag, or tag[qualifier=value])")
        tag, qualifier_tag, qualifier_value = match.groups()
        specs.append(ClientTag(tag, (qualifier_tag, qualifier_value) if qualifier_tag else None))
    if not specs:
        raise ValueError("Client tags name no tag")
    return specs


def client_of(msg: FixMessage, specs: list[ClientTag]) -> str:
    """The client a message names: the first spec present. A qualified spec
    reads the ordered wire pairs — the member's value stands until its
    qualifier is met, which is how a group instance lies on the wire."""
    items = msg._items()
    for spec in specs:
        if spec.qualifier is None:
            value = msg.get(spec.tag, "")
            if value:
                return value
            continue
        qualifier_tag, qualifier_value = spec.qualifier
        current = ""
        for tag, value in items:
            if tag == spec.tag:
                current = value
            elif tag == qualifier_tag and value == qualifier_value and current:
                return current
    return ""


def parse_extra_tags(text: str) -> list[tuple[str, str]]:
    """Parse user-supplied extra tags: pipe- or SOH-delimited tag=value pairs,
    order and duplicates preserved. An empty value ("21=") marks a deletion."""
    if not text or not text.strip():
        return []
    sep = SOH if SOH in text else "|"
    pairs: list[tuple[str, str]] = []
    for part in text.split(sep):
        part = part.strip()
        if not part:
            continue
        tag, eq, value = part.partition("=")
        tag = tag.strip()
        if not eq or not tag.isdigit():
            raise ValueError(f"Invalid extra tag pair: {part!r} (expected tag=value)")
        pairs.append((tag, value))
    return pairs


def _serialize_pairs(pairs: list[tuple[str, str]]) -> bytes:
    return "".join(f"{tag}={value}{SOH}" for tag, value in pairs).encode()


def _serialize_wire(pairs: list[tuple[str, str]]) -> bytes:
    """Serializer for values that came off the wire: the parser decodes
    bytes as latin-1, so latin-1 is the encoding that gives them back."""
    return "".join(f"{tag}={value}{SOH}" for tag, value in pairs).encode("latin-1")


def parse_fix(data: bytes | str) -> FixMessage:
    """Parse a FIX message from raw bytes or a string.

    SOH is the delimiter whenever one is present, so a value holding a
    literal `|` survives; the pipe form is accepted only when no SOH exists
    (typed input, log files, rows recorded before wire storage). The ordered
    wire pairs ride along (duplicates preserved — that's a repeating group);
    `fields` stays the last-wins dict view for lookups."""
    if isinstance(data, bytes):
        text = data.decode("latin-1")
    else:
        text = data

    sep = SOH if SOH in text else "|"
    fields: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for pair in text.split(sep):
        pair = pair.strip()
        if not pair:
            continue
        eq = pair.find("=")
        if eq > 0:
            fields[pair[:eq]] = pair[eq + 1:]
            pairs.append((pair[:eq], pair[eq + 1:]))
    raw = data if isinstance(data, bytes) else (text.encode("latin-1") if sep == SOH else None)
    return FixMessage(fields, pairs=pairs, raw=raw)


# Tags of an inbound order or cancel/replace request the engine consumes into
# order columns (and regenerates itself on the answering message) — everything
# else on the message is a custom tag worth echoing back. 23 and 117 name the
# IOI or quote an order answers (fix_orders.ioi_id / quote_id); the
# instrument's tags go to its columns (instrument.py).
CONSUMED_ORDER_TAGS = frozenset({
    "11", "21", "23", "37", "38", "40", "41", "44", "54", "55", "58", "59", "60", "99", "117",
}) | INSTRUMENT_TAGS | POSITION_TAGS


# The same for an ExecutionReport: what the engine maps into order and trade
# columns; the rest is what the trade row keeps as its extra tags.
CONSUMED_EXEC_TAGS = frozenset({
    "6", "11", "14", "17", "19", "20", "31", "32", "37", "38", "39", "40", "41",
    "44", "54", "55", "58", "59", "60", "99", "126", "150", "151", "432",
}) | INSTRUMENT_TAGS | POSITION_TAGS


def extra_pairs_of(msg: FixMessage, dictionary: FixDictionary,
                   exclude: frozenset[str] = CONSUMED_ORDER_TAGS) -> list[tuple[str, str]]:
    """Ordered custom tags of a parsed message: everything that is neither
    header/trailer nor a tag the engine maps into order columns."""
    return [(t, v) for t, v in msg._items()
            if not dictionary.is_header(t) and not dictionary.is_trailer(t)
            and t not in exclude]


def format_extra_tags(pairs: list[tuple[str, str]]) -> str:
    """Inverse of parse_extra_tags: pipe-delimited tag=value pairs."""
    return "|".join(f"{t}={v}" for t, v in pairs)


# DefaultApplVerID(1137) codes for the FIXT.1.1 Logon, keyed by app version.
_APPL_VER_IDS = {
    "FIX.5.0": "7",
    "FIX.5.0SP1": "8",
    "FIX.5.0SP2": "9",
}

_PRECISION_DIGITS = {
    "second": 0,
    "millisecond": 3,
    "microsecond": 6,
    "nanosecond": 9,
    "picosecond": 12,
}


_ISO_INSTANT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,12}))?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$")
_FIX_STAMP_RE = re.compile(r"^(\d{8}-\d{2}:\d{2}:\d{2})(?:\.(\d+))?$")


def normalize_expire_time(value: str, precision: str = "millisecond", explicit: bool = True) -> str:
    """ExpireTime(126) as it should go on the wire. An ISO-8601 instant — what
    the order dialog's picker submits, `Z`/offset or naive-as-UTC — becomes a
    UTC FIX stamp at `precision` (fraction digits zero-padded or cut; whole
    seconds for "second"). A stamp already in FIX form is re-padded only when
    the precision was asked for (`explicit`), otherwise sent as written; any
    other text passes through verbatim, a test tool's escape hatch."""
    value = (value or "").strip()
    if not value:
        return ""
    digits = _PRECISION_DIGITS.get(precision, 3)
    m = _ISO_INSTANT_RE.match(value)
    if m:
        year, month, day, hour, minute = (int(x) for x in m.groups()[:5])
        second = int(m.group(6) or 0)
        frac = m.group(7) or ""
        offset = m.group(8)
        local = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        if offset and offset != "Z":
            sign = 1 if offset[0] == "+" else -1
            hh, mm = int(offset[1:3]), int(offset[-2:])
            local -= sign * timedelta(hours=hh, minutes=mm)
        base = local.strftime("%Y%m%d-%H:%M:%S")
    else:
        m = _FIX_STAMP_RE.match(value)
        if not m or not explicit:
            return value
        base, frac = m.group(1), m.group(2) or ""
    if digits == 0:
        return base
    return f"{base}." + frac.ljust(digits, "0")[:digits]


def is_bare_date(value: str) -> bool:
    """`YYYY-MM-DD` (the date picker) or `YYYYMMDD` (LocalMktDate)."""
    return re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", (value or "").strip()) is not None


def normalize_expire_date(value: str) -> str:
    """ExpireDate(432) as LocalMktDate: the date picker's `YYYY-MM-DD`
    becomes `YYYYMMDD`; anything else passes through verbatim."""
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value.replace("-", "")
    return value


def standard_precision(begin_string: str) -> str:
    """The protocol-standard timestamp granularity for a FIX version:
    whole seconds through FIX 4.1, milliseconds from FIX 4.2 on."""
    return "second" if begin_string in ("FIX.4.0", "FIX.4.1") else "millisecond"


def _fix_timestamp(precision: str = "millisecond") -> str:
    """UTC timestamp in FIX format at the given granularity. The clock ends at
    nanoseconds; picosecond output zero-pads beyond that — the wire format is
    the point, not the resolution."""
    digits = _PRECISION_DIGITS.get(precision, 3)
    secs, frac_ns = divmod(time.time_ns(), 1_000_000_000)
    base = time.strftime("%Y%m%d-%H:%M:%S", time.gmtime(secs))
    if digits == 0:
        return base
    return f"{base}." + f"{frac_ns:09d}".ljust(digits, "0")[:digits]


def _checksum(data: bytes) -> int:
    total = 0
    for b in data:
        total += b
    return total % 256
