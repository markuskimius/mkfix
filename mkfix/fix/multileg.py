"""Multileg orders: one order over several instruments at once — a spread,
a calendar roll — sent as NewOrderMultileg (35=AB) and replaced with
MultilegOrderCancelReplace (35=AC), its legs in NoLegs(555). This is the
pure part; engine.py sends and records.

The messages are FIX 4.3's; 4.1 and 4.2 carry the subset a multileg order
needs (tools/overlays/FIX4?.multileg.json), without Parties or the legs'
nested groups. A leg's Put/Call rides in LegPutOrCall(1358) where the
version has it (5.0 SP1 on), else in LegCFICode(608).

ExecutionReports tell the two kinds apart by MultiLegReportingType(442):
3 reports the whole order, 2 one leg (named by LegRefID(654) in a
one-instance NoLegs, its price in LegLastPx(637)). A leg's report moves
the leg's fills, never the order's.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, TYPE_CHECKING

from mkfix.fix.instrument import (COVERED, INSTRUMENT_COLS, NUMERIC_COLS, OPEN_CLOSE, PUT_OR_CALL, derived_cfi,
                                  format_number, instrument_text, normalize_instrument)

if TYPE_CHECKING:
    from mkfix.fix.dictionary import FixDictionary
    from mkfix.fix.message import FixMessage

# MultiLegReportingType(442).
WHOLE, LEG = "3", "2"
# A leg's instrument: the order's instrument columns bar the underlying,
# which a leg names by its own LegSymbol.
LEG_INSTRUMENT_COLS = tuple(c for c in INSTRUMENT_COLS if not c.startswith("underlying_"))
# What a leg row holds beside its instrument: its terms, and its own fills
# from the leg reports.
LEG_TERM_COLS = ("leg_ref_id", "symbol", "ratio", "side", "side_code", "position_effect", "covered", "leg_price")
LEG_FILL_COLS = ("cum_qty", "avg_price", "last_qty", "last_price")
# The tags a NoLegs instance carries, in 4.4's member order.
LEG_TAGS = ("600", "602", "603", "608", "609", "610", "611", "612", "614", "616", "623", "624", "564", "565",
            "654", "566", "637", "1358")
# Read into leg rows and the order's own columns: never echoed as extra tags.
CONSUMED_LEG_TAGS = frozenset({"555", "563", "442", *LEG_TAGS})
SIDES = {"Buy": "1", "Sell": "2"}


def supports(dictionary: FixDictionary) -> bool:
    """Whether the version can send a multileg order at all."""
    return dictionary.msg_type_name("AB") != "AB" and dictionary.defines("555")


def _side_code(value: Any) -> str:
    text = str(value or "").strip()
    for word, code in SIDES.items():
        if text.lower() == word.lower():
            return code
    return text


def normalize_leg(terms: Mapping[str, Any], seq: int) -> dict[str, Any]:
    """A leg as typed — a dialog grid's row, a macro's terms, a saved
    strategy's — as row values. `open_close`/`covered_uncovered` are taken
    for the leg's position effect and cover too."""
    put_call = str(terms.get("put_or_call") or "").strip()
    # The letter a strategy's name uses (250/260 C) is taken as the word.
    put_call = {"C": "Call", "P": "Put"}.get(put_call.upper(), put_call)
    instrument = normalize_instrument({**terms, "put_or_call": put_call, "open_close": terms.get("position_effect") or terms.get("open_close"),
                                       "covered_uncovered": terms.get("covered") or terms.get("covered_uncovered")})
    leg: dict[str, Any] = {c: instrument[c] for c in LEG_INSTRUMENT_COLS}
    side_code = _side_code(terms.get("side") or terms.get("side_code"))
    ratio = terms.get("ratio")
    price = terms.get("leg_price")
    leg.update({
        "seq": seq,
        "leg_ref_id": str(terms.get("leg_ref_id") or seq),
        "symbol": str(terms.get("symbol") or "").strip(),
        "ratio": float(ratio) if ratio not in (None, "") else 1.0,
        "side_code": side_code,
        "side": next((w for w, c in SIDES.items() if c == side_code), side_code),
        "position_effect": instrument["open_close"],
        "covered": instrument["covered_uncovered"],
        "leg_price": float(price) if price not in (None, "") else None,
    })
    leg["instrument"] = instrument_text(leg)
    return leg


def normalize_legs(legs: Any) -> list[dict[str, Any]]:
    """Legs as given: the dialog's grid (JSON text) or a list of dicts; rows
    with nothing in them are dropped, and each leg needs its symbol."""
    if isinstance(legs, str):
        legs = json.loads(legs) if legs.strip() else []
    rows = [dict(leg) for leg in legs or [] if isinstance(leg, Mapping)]
    rows = [r for r in rows if any(str(v).strip() for v in r.values() if v is not None)]
    out = []
    for n, row in enumerate(rows, 1):
        leg = normalize_leg(row, n)
        if not leg["symbol"]:
            raise ValueError(f"Leg {n} has no symbol")
        out.append(leg)
    return out


def check_legs(legs: list[dict[str, Any]]) -> None:
    if len(legs) < 2:
        raise ValueError("A multileg order has two legs or more")
    refs = [leg["leg_ref_id"] for leg in legs]
    if len(set(refs)) != len(refs):
        raise ValueError("Each leg needs its own LegRefID")
    for leg in legs:
        if leg["ratio"] <= 0:
            raise ValueError(f"Leg {leg['seq']}: the ratio is a positive quantity; the side says which way")


def leg_pairs(dictionary: FixDictionary, leg: Mapping[str, Any]) -> list[tuple[str, str]]:
    """One NoLegs instance for this version, LegSymbol first; only the tags
    the dictionary defines, as everywhere else."""
    d = dictionary
    pairs: list[tuple[str, str]] = [("600", leg["symbol"])]
    for col, tag in (("security_id", "602"), ("security_id_source", "603")):
        if leg.get(col):
            pairs.append((tag, leg[col]))
    put_call = PUT_OR_CALL.get(leg.get("put_or_call") or "")
    cfi = leg.get("cfi_code") or ""
    if put_call and not cfi and not d.defines("1358"):
        cfi = derived_cfi(leg)
    if cfi:
        pairs.append(("608", cfi))
    if leg.get("security_type"):
        pairs.append(("609", leg["security_type"]))
    maturity = leg.get("maturity") or ""
    if maturity:
        pairs.append(("610", maturity[:6]))
        if len(maturity) == 8 and maturity.isdigit():
            pairs.append(("611", maturity))
    for col, tag in (("strike_price", "612"), ("multiplier", "614")):
        if leg.get(col) is not None:
            pairs.append((tag, format_number(leg[col])))
    if leg.get("security_exchange"):
        pairs.append(("616", leg["security_exchange"]))
    pairs.append(("623", format_number(leg.get("ratio") or 1)))
    if leg.get("side_code"):
        pairs.append(("624", leg["side_code"]))
    for col, words, tag in (("position_effect", OPEN_CLOSE, "564"), ("covered", COVERED, "565")):
        if leg.get(col):
            pairs.append((tag, words.get(leg[col], leg[col])))
    pairs.append(("654", str(leg["leg_ref_id"])))
    if leg.get("leg_price") is not None:
        pairs.append(("566", format_number(leg["leg_price"])))
    if put_call and d.defines("1358"):
        pairs.append(("1358", put_call))
    return [(t, v) for t, v in pairs if d.defines(t)]


def group_pairs(dictionary: FixDictionary, legs: list[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """NoLegs(555) and every leg's instance, in the dictionary's member order."""
    group = dictionary.group("555") or {}
    rank = {tag: n for n, tag in enumerate(group.get("members", []))}
    pairs: list[tuple[str, str]] = [("555", str(len(legs)))]
    for leg in legs:
        own = leg_pairs(dictionary, leg)
        pairs += sorted(own, key=lambda p: (p[0] != "600", rank.get(p[0], len(rank))))
    return pairs


def legs_of(msg: FixMessage, dictionary: FixDictionary) -> list[dict[str, Any]]:
    """The legs a message carries, as row values; `leg_last_px` is a leg
    report's LegLastPx(637)."""
    from mkfix.fix.families import group_instances
    instances = group_instances(msg, "555", LEG_TAGS, dictionary)
    legs = []
    for n, inst in enumerate(instances, 1):
        cfi = inst.get("608", "")
        put_call = next((w for w, c in PUT_OR_CALL.items() if c == inst.get("1358")), "")
        if not put_call and cfi[:1] == "O":
            put_call = {"C": "Call", "P": "Put"}.get(cfi[1:2], "")
        terms = {
            "symbol": inst.get("600", ""), "security_id": inst.get("602", ""),
            "security_id_source": inst.get("603", ""), "cfi_code": cfi, "security_type": inst.get("609", ""),
            "maturity": inst.get("611") or inst.get("610", ""), "strike_price": inst.get("612"),
            "multiplier": inst.get("614"), "security_exchange": inst.get("616", ""), "put_or_call": put_call,
            "ratio": inst.get("623"), "side": inst.get("624", ""), "position_effect": inst.get("564", ""),
            "covered": inst.get("565", ""), "leg_ref_id": inst.get("654") or str(n), "leg_price": inst.get("566"),
        }
        if not terms["security_type"] and cfi[:1] in ("F", "O"):
            terms["security_type"] = "FUT" if cfi[0] == "F" else "OPT"
        leg = normalize_leg(terms, n)
        if cfi and cfi == derived_cfi({**leg, "cfi_code": ""}):
            leg["cfi_code"] = ""        # only the Put/Call leg_pairs spelled as a code
        last_px = inst.get("637")
        leg["leg_last_px"] = float(last_px) if last_px not in (None, "") else None
        legs.append(leg)
    return legs


def legs_text(legs: list[Mapping[str, Any]]) -> str:
    """What the blotters show for the legs: `+1 ES Dec26 / -1 ES Mar27`,
    each leg's ratio signed by its side."""
    def one(leg: Mapping[str, Any]) -> str:
        sign = "-" if leg.get("side_code") in ("2", "5", "6") else "+"
        return f"{sign}{format_number(leg.get('ratio') or 1)} {leg.get('instrument') or leg.get('symbol')}"
    return " / ".join(one(leg) for leg in legs)


def strategy_symbol(legs: list[Mapping[str, Any]]) -> str:
    """The order's own Symbol(55) when none is given: the legs' one symbol,
    else the first leg's."""
    symbols = {leg["symbol"] for leg in legs}
    return legs[0]["symbol"] if legs and len(symbols) != 1 else next(iter(symbols), "")


def legs_json(legs: list[Mapping[str, Any]]) -> str:
    """Legs as a saved strategy keeps them: the New Multileg grid's cells,
    so the words the rows keep go back to the codes its dropdowns offer."""
    keep = ("symbol", *LEG_INSTRUMENT_COLS, "ratio", "side_code", "position_effect", "covered", "leg_price",
            "leg_ref_id")
    codes = {"put_or_call": PUT_OR_CALL, "position_effect": OPEN_CLOSE, "covered": COVERED}
    rows = []
    for leg in legs:
        row = {}
        for col in keep:
            value = leg.get(col)
            if value in (None, ""):
                continue
            if col in codes:
                value = codes[col].get(value, value)
            row["side" if col == "side_code" else col] = value
        rows.append(row)
    return json.dumps(rows)


__all__ = ["WHOLE", "LEG", "LEG_INSTRUMENT_COLS", "LEG_TERM_COLS", "LEG_FILL_COLS", "LEG_TAGS", "CONSUMED_LEG_TAGS",
           "supports", "normalize_leg", "normalize_legs", "check_legs", "leg_pairs", "group_pairs", "legs_of",
           "legs_text", "strategy_symbol", "legs_json", "NUMERIC_COLS"]
