"""FIX message: parse, compose, serialize, checksum."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from mkfix.fix.dictionary import FixDictionary

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
    ) -> FixMessage:
        """OrderCancelReject (35=9); response_to is CxlRejResponseTo(434):
        1 = cancel request, 2 = cancel/replace request."""
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
        if not self.dictionary.defines("434"):
            fields.pop("434")
        self._strip_legacy_body_time(fields)
        return self.create(fields)


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
# else on the message is a custom tag worth echoing back.
CONSUMED_ORDER_TAGS = frozenset({
    "11", "21", "37", "38", "40", "41", "44", "54", "55", "58", "59", "60", "99",
})


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
