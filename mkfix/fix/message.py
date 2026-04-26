"""FIX message: parse, compose, serialize, checksum."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mkfix.fix.dictionary import FixDictionary

SOH = chr(1)


class FixMessage:
    """A single FIX message as an ordered dict of tag->value pairs."""

    def __init__(self, fields: dict[str, str] | None = None):
        self.fields: dict[str, str] = {}
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

    def sendprep(self, dictionary: FixDictionary, sender: str, target: str, seq_num: int) -> None:
        """Prepare message for sending: add header/trailer, body length, checksum."""
        prepped: dict[str, str] = {}

        for tag in dictionary.header_tags:
            if dictionary.is_special(tag):
                continue
            if tag in self.fields:
                prepped[tag] = self.fields[tag]
            elif tag == "49":
                prepped[tag] = sender
            elif tag == "56":
                prepped[tag] = target
            elif tag == "34":
                prepped[tag] = str(seq_num)
            elif tag == "52":
                prepped[tag] = _fix_timestamp()

        for tag, value in self.fields.items():
            if not dictionary.is_header(tag) and not dictionary.is_trailer(tag):
                prepped[tag] = value

        for tag in dictionary.trailer_tags:
            if not dictionary.is_special(tag) and tag in self.fields:
                prepped[tag] = self.fields[tag]

        self.fields = prepped
        body = self._serialize_body()
        body_length = len(body)

        final: dict[str, str] = {
            "8": self.fields.get("8", dictionary.begin_string()),
            "9": str(body_length),
        }
        final.update(self.fields)
        self.fields = final

        checksum = _checksum(self.serialize_without_checksum())
        self.fields["10"] = f"{checksum:03d}"

    def _serialize_body(self) -> bytes:
        parts = []
        for tag, value in self.fields.items():
            parts.append(f"{tag}={value}{SOH}")
        return "".join(parts).encode()

    def serialize_without_checksum(self) -> bytes:
        parts = []
        for tag, value in self.fields.items():
            if tag != "10":
                parts.append(f"{tag}={value}{SOH}")
        return "".join(parts).encode()

    def serialize(self) -> bytes:
        parts = []
        for tag, value in self.fields.items():
            parts.append(f"{tag}={value}{SOH}")
        return "".join(parts).encode()

    def to_pipe_string(self) -> str:
        parts = []
        for tag, value in self.fields.items():
            parts.append(f"{tag}={value}")
        return "|".join(parts)

    def __str__(self) -> str:
        return self.to_pipe_string()


class FixMessageFactory:
    """Creates FixMessage instances with session-level defaults."""

    def __init__(self, dictionary: FixDictionary, sender: str, target: str):
        self.dictionary = dictionary
        self.sender = sender
        self.target = target

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
        if reset_seq_num:
            fields["141"] = "Y"
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
            "60": _fix_timestamp(),
        }
        if price is not None:
            fields["44"] = str(price)
        if account:
            fields["1"] = account
        fields.update(extra)
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
            "60": _fix_timestamp(),
        }
        if qty:
            fields["38"] = str(int(qty))
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
            "60": _fix_timestamp(),
        }
        if price is not None:
            fields["44"] = str(price)
        fields.update(extra)
        return self.create(fields)


def parse_fix(data: bytes | str) -> FixMessage:
    """Parse a FIX message from raw bytes or pipe-delimited string."""
    if isinstance(data, bytes):
        text = data.decode("latin-1")
    else:
        text = data

    sep = SOH if SOH in text else "|"
    fields: dict[str, str] = {}
    for pair in text.split(sep):
        pair = pair.strip()
        if not pair:
            continue
        eq = pair.find("=")
        if eq > 0:
            fields[pair[:eq]] = pair[eq + 1:]
    return FixMessage(fields)


def _fix_timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d-%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _checksum(data: bytes) -> int:
    total = 0
    for b in data:
        total += b
    return total % 256
