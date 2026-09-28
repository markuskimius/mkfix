"""The instrument an order or trade is in — options, futures and futures
options beside plain stock — between the wire and the row columns.

The rows hold one spelling whatever the version: SecurityType(167) as its
code, a maturity as ``YYYYMM`` or ``YYYYMMDD``, Put/Call and the position
terms as words. Each version spells them its own way on the wire, and only
with what its dictionary defines:

- FIX 4.0 has no SecurityType; 4.1 and 4.2 split a maturity date into
  MaturityMonthYear(200) and MaturityDay(205); from 4.3, 200 carries the
  day itself.
- FIX 4.3 replaced SecurityType's FUT and OPT, and PutOrCall(201), with
  CFICode(461): there a future or an option goes out as a CFI code (the one
  given, else one derived from its terms) and no 167.
- OOF, OOP and OOC are FIX 5.0's. A code the session's dictionary does not
  define is refused on a message we originate — on 4.x a futures option is
  an OPT whose underlying is a FUT — and sent as it stands on an answer
  echoing what the counterparty sent.

A tag the dictionary does not define is withheld, as everywhere else.
"""

from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from mkfix.fix.dictionary import FixDictionary
    from mkfix.fix.message import FixMessage

# The instrument itself, on orders, trades and saved instruments.
INSTRUMENT_COLS = (
    "security_type", "maturity", "strike_price", "put_or_call", "cfi_code",
    "underlying_symbol", "underlying_security_type", "underlying_maturity",
    "multiplier", "security_exchange", "security_id", "security_id_source",
)
# What the order does with it: open or close a position, covered or not.
POSITION_COLS = ("open_close", "covered_uncovered")
# Every column an order or trade row carries for it; `instrument` is the
# display text, written with the row.
ORDER_INSTRUMENT_COLS = INSTRUMENT_COLS + POSITION_COLS + ("instrument",)
NUMERIC_COLS = frozenset({"strike_price", "multiplier"})

# The tags read into those columns, so an inbound message's instrument is
# never echoed back as extra tags too.
INSTRUMENT_TAGS = frozenset({
    "167", "200", "205", "541", "202", "201", "461", "310", "311", "313", "314",
    "231", "207", "48", "22",
})
POSITION_TAGS = frozenset({"77", "203"})

PUT_OR_CALL = {"Put": "0", "Call": "1"}
OPEN_CLOSE = {"Open": "O", "Close": "C", "Rolled": "R", "FIFO": "F"}
COVERED = {"Covered": "0", "Uncovered": "1"}

# Security types that are options, for the display text and CFI codes.
OPTION_TYPES = frozenset({"OPT", "OOF", "OOP", "OOC"})
# What to send instead of a 5.0 code on a 4.x session.
_INSTEAD = {"OOF": "send OPT with Underlying Type FUT",
            "OOP": "send OPT with its physical underlying",
            "OOC": "send OPT with its combination as the underlying"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def blank_instrument() -> dict[str, Any]:
    """Every instrument column empty: a row with no instrument."""
    return {c: None if c in NUMERIC_COLS else "" for c in ORDER_INSTRUMENT_COLS}


def _word(value: Any, words: Mapping[str, str]) -> str:
    """A word as the rows keep it, from the word in any case or its code."""
    text = str(value or "").strip()
    if not text:
        return ""
    for word, code in words.items():
        if text.lower() == word.lower() or text.upper() == code:
            return word
    return text


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_number(value: float) -> str:
    """A strike or multiplier as the wire carries it: no trailing ``.0``."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def normalize_instrument(terms: Mapping[str, Any]) -> dict[str, Any]:
    """Terms as typed — a dialog's strings, a macro's words — as row values:
    codes upper-cased, words spelled one way, numbers as numbers. Keys other
    than the instrument columns are left out; `symbol` is the caller's."""
    row = blank_instrument()
    for col in INSTRUMENT_COLS + POSITION_COLS:
        value = terms.get(col)
        if col in NUMERIC_COLS:
            row[col] = _number(value)
        else:
            row[col] = str(value or "").strip()
    for col in ("security_type", "underlying_security_type", "cfi_code"):
        row[col] = row[col].upper()
    row["put_or_call"] = _word(row["put_or_call"], PUT_OR_CALL)
    row["open_close"] = _word(row["open_close"], OPEN_CLOSE)
    row["covered_uncovered"] = _word(row["covered_uncovered"], COVERED)
    return row


def _maturity_pairs(dictionary: FixDictionary, maturity: str, month_tag: str,
                    day_tag: str) -> list[tuple[str, str]]:
    """A maturity on the wire: a date splits into month and day where the
    version has the day tag (4.1, 4.2), else rides whole in the month tag."""
    if not maturity or not dictionary.defines(month_tag):
        return []
    if len(maturity) == 8 and maturity.isdigit() and dictionary.defines(day_tag):
        return [(month_tag, maturity[:6]), (day_tag, maturity[6:])]
    return [(month_tag, maturity)]


def derived_cfi(row: Mapping[str, Any]) -> str:
    """A CFI code (ISO 10962) standing for a future or an option, for a
    version that identifies them by CFICode(461) alone; X for what the row
    does not say. An option's fourth letter is its underlying's kind."""
    security_type = row.get("security_type") or ""
    if security_type == "FUT":
        return "FXXXXX"
    if security_type in OPTION_TYPES:
        put_call = {"Call": "C", "Put": "P"}.get(row.get("put_or_call") or "", "X")
        underlying = {"FUT": "F", "CS": "S"}.get(row.get("underlying_security_type") or "", "X")
        return f"O{put_call}X{underlying}XX"
    return ""


def instrument_pairs(dictionary: FixDictionary, row: Mapping[str, Any],
                     originating: bool = True) -> list[tuple[str, str]]:
    """The instrument's tags for a message on this dictionary's version, in
    order, from a row (or normalized terms). `originating` is a message we
    make up — a new order — where a security type the version does not
    define is refused; an answer echoes what the order carries."""
    d = dictionary
    pairs: list[tuple[str, str]] = []
    security_type = row.get("security_type") or ""
    cfi = row.get("cfi_code") or ""
    by_cfi = False
    if security_type and d.defines("167"):
        if d.has_enum("167", security_type) or not d.enums.get("167"):
            pairs.append(("167", security_type))
        elif security_type in ("FUT", "OPT") and d.defines("461"):
            by_cfi = True
        elif originating:
            instead = _INSTEAD.get(security_type)
            raise ValueError(f"SecurityType {security_type} is not defined by {d.version}"
                             + (f"; {instead}" if instead else ""))
        else:
            pairs.append(("167", security_type))
    pairs += _maturity_pairs(d, row.get("maturity") or "", "200", "205")
    strike = row.get("strike_price")
    if strike is not None and d.defines("202"):
        pairs.append(("202", format_number(strike)))
    put_call = PUT_OR_CALL.get(row.get("put_or_call") or "")
    if put_call and d.defines("201"):
        pairs.append(("201", put_call))
    elif put_call and not cfi and d.defines("461"):
        by_cfi = True
    if not cfi and by_cfi:
        cfi = derived_cfi(row)
    if cfi and d.defines("461"):
        pairs.append(("461", cfi))
    for col, tag in (("underlying_security_type", "310"), ("underlying_symbol", "311")):
        if row.get(col) and d.defines(tag):
            pairs.append((tag, row[col]))
    pairs += _maturity_pairs(d, row.get("underlying_maturity") or "", "313", "314")
    multiplier = row.get("multiplier")
    if multiplier is not None and d.defines("231"):
        pairs.append(("231", format_number(multiplier)))
    for col, tag in (("security_exchange", "207"), ("security_id", "48"), ("security_id_source", "22")):
        if row.get(col) and d.defines(tag):
            pairs.append((tag, row[col]))
    for col, words, tag in (("open_close", OPEN_CLOSE, "77"), ("covered_uncovered", COVERED, "203")):
        value = row.get(col) or ""
        if value and d.defines(tag):
            pairs.append((tag, words.get(value, value)))
    return pairs


def _maturity_of(msg: FixMessage, month_tag: str, day_tag: str, date_tag: str | None = None) -> str:
    month = msg.get(month_tag, "")
    day = msg.get(day_tag, "")
    if len(month) == 6 and day:
        return month + day.zfill(2)
    if not month and date_tag:
        return msg.get(date_tag, "")
    return month


def instrument_of(msg: FixMessage) -> dict[str, Any]:
    """The instrument a message carries, as row values with its display
    text. A 4.3 message naming a future or an option by CFICode alone gets
    its security type — and an option its Put/Call — from the code."""
    def word(tag: str, words: Mapping[str, str]) -> str:
        code = msg.get(tag, "")
        return next((w for w, c in words.items() if c == code), code)

    cfi = msg.get("461", "")
    row = {
        "security_type": msg.get("167", ""),
        "maturity": _maturity_of(msg, "200", "205", "541"),
        "strike_price": _number(msg.get("202", "")),
        "put_or_call": word("201", PUT_OR_CALL),
        "cfi_code": cfi,
        "underlying_symbol": msg.get("311", ""),
        "underlying_security_type": msg.get("310", ""),
        "underlying_maturity": _maturity_of(msg, "313", "314"),
        "multiplier": _number(msg.get("231", "")),
        "security_exchange": msg.get("207", ""),
        "security_id": msg.get("48", ""),
        "security_id_source": msg.get("22", ""),
        "open_close": word("77", OPEN_CLOSE),
        "covered_uncovered": word("203", COVERED),
    }
    if not row["security_type"] and cfi[:1] in ("F", "O"):
        row["security_type"] = "FUT" if cfi[0] == "F" else "OPT"
    if not row["put_or_call"] and cfi[:1] == "O":
        row["put_or_call"] = {"C": "Call", "P": "Put"}.get(cfi[1:2], "")
    row["instrument"] = instrument_text({**row, "symbol": msg.get("55", "")})
    return row


def carries_instrument(msg: FixMessage) -> bool:
    """Whether a message names its instrument beyond the symbol."""
    return any(msg.get(tag) for tag in INSTRUMENT_TAGS)


def _month(maturity: str) -> str:
    """``202612`` as ``Dec26``, ``20261218`` as ``18Dec26``; anything else as is."""
    if len(maturity) in (6, 8) and maturity.isdigit() and 1 <= int(maturity[4:6]) <= 12:
        month = _MONTHS[int(maturity[4:6]) - 1] + maturity[2:4]
        return maturity[6:] + month if len(maturity) == 8 else month
    return maturity


def instrument_text(row: Mapping[str, Any]) -> str:
    """What the blotters show for an instrument: the symbol for a stock,
    ``ES Dec26`` for a future, ``AAPL 18Dec26 250 C`` for an option."""
    symbol = row.get("symbol") or row.get("security_id") or ""
    security_type = row.get("security_type") or ""
    if security_type in ("", "CS"):
        return symbol
    maturity = _month(row.get("maturity") or "")
    if security_type == "FUT":
        parts = [symbol, maturity]
    elif security_type in OPTION_TYPES:
        strike = row.get("strike_price")
        parts = [symbol, maturity, format_number(strike) if strike is not None else "",
                 {"Call": "C", "Put": "P"}.get(row.get("put_or_call") or "", "")]
    else:
        parts = [symbol, security_type]
    return " ".join(p for p in parts if p)
