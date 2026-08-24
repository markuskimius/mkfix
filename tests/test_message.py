"""Tests for FIX message parsing, serialization, and factory."""

import pytest

from mkfix.fix.message import (
    extra_pairs_of,
    format_extra_tags,
    FixMessage, FixMessageFactory, parse_fix, parse_extra_tags, _checksum, SOH,
    _fix_timestamp, standard_precision,
)
from mkfix.fix.dictionary import FixDictionary


class TestFixMessage:
    def test_fields_access(self):
        msg = FixMessage({"35": "D", "55": "AAPL"})
        assert msg["35"] == "D"
        assert msg["55"] == "AAPL"
        assert msg["999"] is None

    def test_set_and_get(self):
        msg = FixMessage()
        msg["35"] = "A"
        msg["108"] = 30
        assert msg["35"] == "A"
        assert msg["108"] == "30"

    def test_set_none_ignored(self):
        msg = FixMessage()
        msg["35"] = None
        assert "35" not in msg

    def test_contains(self):
        msg = FixMessage({"35": "D"})
        assert "35" in msg
        assert "999" not in msg

    def test_get_int(self):
        msg = FixMessage({"34": "42", "35": "D"})
        assert msg.get_int("34") == 42
        assert msg.get_int("999") == 0
        assert msg.get_int("999", 5) == 5
        assert msg.get_int("35", 0) == 0

    def test_get_float(self):
        msg = FixMessage({"44": "150.25", "35": "D"})
        assert msg.get_float("44") == 150.25
        assert msg.get_float("999") == 0.0
        assert msg.get_float("35", 0.0) == 0.0

    def test_to_pipe_string(self):
        msg = FixMessage({"8": "FIX.4.2", "35": "D"})
        assert msg.to_pipe_string() == "8=FIX.4.2|35=D"

    def test_serialize(self):
        msg = FixMessage({"8": "FIX.4.2", "35": "A"})
        raw = msg.serialize()
        assert raw == f"8=FIX.4.2{SOH}35=A{SOH}".encode()

    def test_serialize_without_checksum(self):
        msg = FixMessage({"8": "FIX.4.2", "35": "A", "10": "123"})
        raw = msg.serialize_without_checksum()
        assert b"10=" not in raw
        assert f"8=FIX.4.2{SOH}35=A{SOH}".encode() == raw


class TestParseFix:
    def test_parse_soh_delimited(self):
        raw = f"8=FIX.4.2{SOH}35=D{SOH}55=AAPL{SOH}"
        msg = parse_fix(raw)
        assert msg["8"] == "FIX.4.2"
        assert msg["35"] == "D"
        assert msg["55"] == "AAPL"

    def test_parse_pipe_delimited(self):
        msg = parse_fix("8=FIX.4.2|35=D|55=AAPL")
        assert msg["8"] == "FIX.4.2"
        assert msg["35"] == "D"
        assert msg["55"] == "AAPL"

    def test_parse_bytes(self):
        raw = f"8=FIX.4.2{SOH}35=A{SOH}".encode("latin-1")
        msg = parse_fix(raw)
        assert msg["8"] == "FIX.4.2"
        assert msg["35"] == "A"

    def test_parse_empty_values(self):
        msg = parse_fix("8=FIX.4.2|35=D|58=")
        assert msg["58"] == ""

    def test_parse_trailing_pipe(self):
        msg = parse_fix("8=FIX.4.2|35=D|")
        assert msg["8"] == "FIX.4.2"
        assert msg["35"] == "D"


class TestChecksum:
    def test_known_value(self):
        data = f"8=FIX.4.2{SOH}9=5{SOH}35=A{SOH}".encode()
        cs = _checksum(data)
        assert 0 <= cs <= 255

    def test_zero_pad(self):
        dictionary = FixDictionary("FIX.4.2")
        msg = FixMessage({"35": "A", "98": "0", "108": "30"})
        msg.sendprep(dictionary, "SENDER", "TARGET", 1)
        assert len(msg["10"]) == 3


class TestSendprep:
    def test_adds_header_trailer(self):
        dictionary = FixDictionary("FIX.4.2")
        msg = FixMessage({"35": "D", "55": "AAPL", "54": "1"})
        msg.sendprep(dictionary, "SENDER", "TARGET", 42)
        assert msg["8"] == "FIX.4.2"
        assert msg["9"] is not None
        assert msg["49"] == "SENDER"
        assert msg["56"] == "TARGET"
        assert msg["34"] == "42"
        assert msg["52"] is not None
        assert msg["10"] is not None
        assert msg["55"] == "AAPL"

    def test_body_length_is_correct(self):
        dictionary = FixDictionary("FIX.4.2")
        msg = FixMessage({"35": "A", "98": "0", "108": "30"})
        msg.sendprep(dictionary, "S", "T", 1)
        raw = msg.serialize()
        text = raw.decode("latin-1")
        parts = text.split(SOH)
        body_start = text.index(SOH, text.index("9=")) + 1
        checksum_start = text.index("10=")
        body = text[body_start:checksum_start]
        assert int(msg["9"]) == len(body.encode("latin-1"))

    def test_checksum_is_correct(self):
        dictionary = FixDictionary("FIX.4.2")
        msg = FixMessage({"35": "0"})
        msg.sendprep(dictionary, "S", "T", 1)
        without_cs = msg.serialize_without_checksum()
        expected = _checksum(without_cs)
        assert int(msg["10"]) == expected


class TestParseExtraTags:
    def test_pipe_delimited_preserves_order_and_duplicates(self):
        pairs = parse_extra_tags("382=2|375=A|375=B")
        assert pairs == [("382", "2"), ("375", "A"), ("375", "B")]

    def test_soh_delimited(self):
        assert parse_extra_tags(f"58=hi{SOH}44=1.5") == [("58", "hi"), ("44", "1.5")]

    def test_empty_value_is_deletion_marker(self):
        assert parse_extra_tags("21=") == [("21", "")]

    def test_whitespace_and_empty_input(self):
        assert parse_extra_tags("") == []
        assert parse_extra_tags("   ") == []
        assert parse_extra_tags(" 58=hi | 44=1.5 ") == [("58", "hi"), ("44", "1.5")]

    def test_value_may_contain_equals(self):
        assert parse_extra_tags("58=a=b") == [("58", "a=b")]

    def test_malformed_pairs_raise(self):
        with pytest.raises(ValueError):
            parse_extra_tags("58")
        with pytest.raises(ValueError):
            parse_extra_tags("abc=1")


def _wire_pairs(msg: FixMessage) -> list[tuple[str, str]]:
    parts = msg.serialize().decode("latin-1").split(SOH)
    return [tuple(p.split("=", 1)) for p in parts if p]


class TestSendprepExtraTags:
    def setup_method(self):
        self.dictionary = FixDictionary("FIX.4.2")

    def _prep(self, fields, extra):
        msg = FixMessage(fields)
        msg.extra = parse_extra_tags(extra)
        msg.sendprep(self.dictionary, "SENDER", "TARGET", 42)
        return msg

    def test_body_tag_appended_after_body(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "5001=X")
        tags = [t for t, _ in _wire_pairs(msg)]
        assert tags.index("5001") > tags.index("55")
        assert tags.index("5001") < tags.index("10")
        assert msg["5001"] == "X"

    def test_repeating_group_preserves_order_and_duplicates(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "382=2|375=A|375=B")
        pairs = _wire_pairs(msg)
        i = pairs.index(("382", "2"))
        assert pairs[i + 1] == ("375", "A")
        assert pairs[i + 2] == ("375", "B")
        assert msg.to_pipe_string().count("375=") == 2

    def test_override_existing_body_tag_in_place(self):
        msg = self._prep({"35": "D", "55": "AAPL", "54": "1"}, "54=2")
        pairs = _wire_pairs(msg)
        assert pairs.count(("54", "2")) == 1
        assert ("54", "1") not in pairs
        tags = [t for t, _ in pairs]
        assert tags.count("54") == 1
        assert tags.index("54") == tags.index("55") + 1, "override keeps the tag's position"

    def test_override_computed_header_tags(self):
        msg = self._prep({"35": "D"}, "34=999|52=20990101-00:00:00.000|49=EVIL")
        assert msg["34"] == "999"
        assert msg["52"] == "20990101-00:00:00.000"
        assert msg["49"] == "EVIL"
        tags = [t for t, _ in _wire_pairs(msg)]
        assert tags.count("34") == 1
        assert tags.count("49") == 1

    def test_header_append_lands_in_header_block(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "43=Y")
        tags = [t for t, _ in _wire_pairs(msg)]
        assert tags.index("43") < tags.index("55")
        assert tags.index("43") > tags.index("34")

    def test_trailer_tags_land_before_checksum(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "93=4|89=SIGN")
        tags = [t for t, _ in _wire_pairs(msg)]
        assert tags.index("93") > tags.index("55")
        assert tags.index("89") == tags.index("93") + 1
        assert tags.index("10") == len(tags) - 1

    def test_delete_body_tag(self):
        msg = self._prep({"35": "D", "55": "AAPL", "21": "1"}, "21=")
        assert "21" not in msg
        assert "21=" not in msg.to_pipe_string()

    def test_delete_computed_header_tag(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "52=")
        assert "52" not in msg

    def test_body_length_includes_extras(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "382=2|375=A|375=B")
        text = msg.serialize().decode("latin-1")
        body_start = text.index(SOH, text.index("9=")) + 1
        body = text[body_start:text.index("10=")]
        assert int(msg["9"]) == len(body.encode("latin-1"))

    def test_checksum_valid_with_extras(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "5001=X|375=A|375=B")
        assert int(msg["10"]) == _checksum(msg.serialize_without_checksum())

    def test_explicit_bodylength_and_checksum_override(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "9=9999|10=123")
        assert msg["9"] == "9999"
        assert msg["10"] == "123"

    def test_delete_wins_over_override_of_same_tag(self):
        msg = self._prep({"35": "D", "55": "AAPL"}, "55=|55=MSFT")
        assert "55" not in msg

    def test_no_extras_is_unchanged_behavior(self):
        plain = FixMessage({"35": "D", "55": "AAPL", "54": "1"})
        plain.sendprep(self.dictionary, "SENDER", "TARGET", 42)
        tags = [t for t, _ in _wire_pairs(plain)]
        assert tags[0] == "8"
        assert tags[1] == "9"
        assert tags[-1] == "10"
        assert int(plain["10"]) == _checksum(plain.serialize_without_checksum())


class TestFixMessageFactory:
    def setup_method(self):
        self.dictionary = FixDictionary("FIX.4.2")
        self.factory = FixMessageFactory(self.dictionary, "MKFIX", "PEER")

    def test_heartbeat(self):
        msg = self.factory.heartbeat()
        assert msg["35"] == "0"

    def test_heartbeat_with_test_req_id(self):
        msg = self.factory.heartbeat(test_req_id="TR-123")
        assert msg["35"] == "0"
        assert msg["112"] == "TR-123"

    def test_test_request(self):
        msg = self.factory.test_request("TR-456")
        assert msg["35"] == "1"
        assert msg["112"] == "TR-456"

    def test_logon(self):
        msg = self.factory.logon(heartbeat_interval=30)
        assert msg["35"] == "A"
        assert msg["108"] == "30"
        assert msg["98"] == "0"
        assert msg.get("141") is None

    def test_logon_with_reset(self):
        msg = self.factory.logon(reset_seq_num=True)
        assert msg["141"] == "Y"

    def test_logout(self):
        msg = self.factory.logout()
        assert msg["35"] == "5"

    def test_logout_with_text(self):
        msg = self.factory.logout(text="Session terminated")
        assert msg["58"] == "Session terminated"

    def test_resend_request(self):
        msg = self.factory.resend_request(5, 10)
        assert msg["35"] == "2"
        assert msg["7"] == "5"
        assert msg["16"] == "10"

    def test_sequence_reset(self):
        msg = self.factory.sequence_reset(100, gap_fill=True)
        assert msg["35"] == "4"
        assert msg["36"] == "100"
        assert msg["123"] == "Y"
        assert msg["43"] == "Y"

    def test_reject(self):
        msg = self.factory.reject(5, text="Bad field", reason=1)
        assert msg["35"] == "3"
        assert msg["45"] == "5"
        assert msg["58"] == "Bad field"
        assert msg["373"] == "1"

    def test_new_order_single(self):
        msg = self.factory.new_order_single(
            cl_ord_id="ORD-001", symbol="MSFT", side="1",
            qty=100, ord_type="2", price=350.50, tif="0",
        )
        assert msg["35"] == "D"
        assert msg["11"] == "ORD-001"
        assert msg["55"] == "MSFT"
        assert msg["54"] == "1"
        assert msg["38"] == "100"
        assert msg["40"] == "2"
        assert msg["44"] == "350.5"
        assert msg["59"] == "0"
        assert msg["60"] is not None

    def test_new_order_single_market(self):
        msg = self.factory.new_order_single(
            cl_ord_id="ORD-002", symbol="AAPL", side="2",
            qty=50, ord_type="1",
        )
        assert msg["40"] == "1"
        assert msg.get("44") is None

    def test_cancel_request(self):
        msg = self.factory.cancel_request(
            cl_ord_id="CXL-001", orig_cl_ord_id="ORD-001",
            symbol="AAPL", side="1",
        )
        assert msg["35"] == "F"
        assert msg["11"] == "CXL-001"
        assert msg["41"] == "ORD-001"

    def test_cancel_replace_request(self):
        msg = self.factory.cancel_replace_request(
            cl_ord_id="CRX-001", orig_cl_ord_id="ORD-001",
            symbol="AAPL", side="1", qty=200, price=155.0,
        )
        assert msg["35"] == "G"
        assert msg["11"] == "CRX-001"
        assert msg["41"] == "ORD-001"
        assert msg["38"] == "200"
        assert msg["44"] == "155.0"
        assert "59" not in msg.fields, "TIF is sent on a replace only when given"

    def test_cancel_replace_request_with_tif(self):
        msg = self.factory.cancel_replace_request(
            cl_ord_id="CRX-001", orig_cl_ord_id="ORD-001",
            symbol="AAPL", side="1", qty=200, price=155.0, tif="1",
        )
        assert msg["59"] == "1"

    def test_execution_report_fill(self):
        msg = self.factory.execution_report(
            order_id="O-001", cl_ord_id="ORD-001", exec_id="E-001",
            exec_trans_type="0", exec_type="1", ord_status="1",
            symbol="AAPL", side="1", qty=100,
            last_qty=40, last_price=150.25, cum_qty=40,
            avg_price=150.25, leaves_qty=60,
        )
        assert msg["35"] == "8"
        assert msg["37"] == "O-001"
        assert msg["11"] == "ORD-001"
        assert msg["17"] == "E-001"
        assert msg["20"] == "0"
        assert msg["150"] == "1"
        assert msg["39"] == "1"
        assert msg["38"] == "100"
        assert msg["32"] == "40"
        assert msg["31"] == "150.25"
        assert msg["14"] == "40"
        assert msg["151"] == "60"
        assert msg["60"] is not None
        assert msg.get("19") is None
        assert msg.get("58") is None

    def test_execution_report_bust_references_original(self):
        msg = self.factory.execution_report(
            order_id="O-001", cl_ord_id="ORD-001", exec_id="E-002",
            exec_trans_type="1", exec_type="0", ord_status="0",
            symbol="AAPL", side="1", qty=100,
            exec_ref_id="E-001", text="busted",
        )
        assert msg["20"] == "1"
        assert msg["19"] == "E-001"
        assert msg["58"] == "busted"


class TestParsedPairs:
    def test_parse_fix_preserves_duplicate_tags_in_order(self):
        raw = "8=FIX.4.2|35=D|11=C1|382=2|375=A|375=B|10=000"
        msg = parse_fix(raw)
        assert msg.to_pipe_string() == raw, "serialization keeps duplicates and order"
        assert msg["375"] == "B", "the dict view stays last-wins for lookups"

    def test_stream_parser_preserves_duplicate_tags(self):
        from mkfix.fix.parser import FixStreamParser
        soh = chr(1)
        raw = "8=FIX.4.2|35=D|11=C1|375=A|375=B|10=000".replace("|", soh).encode()
        msg = FixStreamParser._parse(raw)
        assert msg.to_pipe_string() == "8=FIX.4.2|35=D|11=C1|375=A|375=B|10=000"


class TestExtraPairsOf:
    def setup_method(self):
        from mkfix.fix.dictionary import FixDictionary
        self.dictionary = FixDictionary("FIX.4.2")

    def test_excludes_header_trailer_and_consumed_tags(self):
        raw = ("8=FIX.4.2|9=100|35=D|49=A|56=B|34=2|52=20260823-00:00:00|"
               "11=C1|21=1|55=AAPL|54=1|38=100|40=2|44=150|59=0|60=20260823-00:00:00|"
               "1=ACCT|100=XNAS|382=2|375=A|375=B|10=123")
        pairs = extra_pairs_of(parse_fix(raw), self.dictionary)
        assert pairs == [("1", "ACCT"), ("100", "XNAS"), ("382", "2"),
                         ("375", "A"), ("375", "B")]

    def test_format_round_trips_through_parse_extra_tags(self):
        pairs = [("382", "2"), ("375", "A"), ("375", "B")]
        assert parse_extra_tags(format_extra_tags(pairs)) == pairs

    def test_format_empty(self):
        assert format_extra_tags([]) == ""


class TestTimestampPrecision:
    def test_granularity_formats(self):
        import re
        cases = {
            "second": r"^\d{8}-\d{2}:\d{2}:\d{2}$",
            "millisecond": r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{3}$",
            "microsecond": r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{6}$",
            "nanosecond": r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{9}$",
            "picosecond": r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{12}$",
        }
        for precision, pattern in cases.items():
            assert re.match(pattern, _fix_timestamp(precision)), precision

    def test_picosecond_pads_beyond_clock(self):
        assert _fix_timestamp("picosecond").split(".")[1].endswith("000")

    def test_default_is_millisecond(self):
        assert len(_fix_timestamp().split(".")[1]) == 3

    def test_unknown_precision_falls_back_to_millisecond(self):
        assert len(_fix_timestamp("bogus").split(".")[1]) == 3

    def test_standard_precision_by_version(self):
        assert standard_precision("FIX.4.0") == "second"
        assert standard_precision("FIX.4.1") == "second"
        assert standard_precision("FIX.4.2") == "millisecond"
        assert standard_precision("FIX.4.4") == "millisecond"
        assert standard_precision("FIXT.1.1") == "millisecond"

    def test_factory_defaults_to_protocol_standard(self):
        import re
        f40 = FixMessageFactory(FixDictionary("FIX.4.0"), "S", "T")
        f42 = FixMessageFactory(FixDictionary("FIX.4.2"), "S", "T")
        assert f40.timestamp_precision == "second"
        assert f42.timestamp_precision == "millisecond"
        msg40 = f40.new_order_single("C1", "AAPL", "1", 100)
        msg42 = f42.new_order_single("C1", "AAPL", "1", 100)
        assert re.match(r"^\d{8}-\d{2}:\d{2}:\d{2}$", msg40["60"])
        assert re.match(r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{3}$", msg42["60"])

    def test_factory_override_wins(self):
        f = FixMessageFactory(FixDictionary("FIX.4.0"), "S", "T",
                              timestamp_precision="nanosecond")
        msg = f.new_order_single("C1", "AAPL", "1", 100)
        assert len(msg["60"].split(".")[1]) == 9

    def test_sendprep_sending_time_follows_precision(self):
        import re
        dictionary = FixDictionary("FIX.4.2")
        msg = FixMessage({"35": "0"})
        msg["8"] = dictionary.begin_string()
        msg.sendprep(dictionary, "S", "T", 1, timestamp_precision="microsecond")
        assert re.match(r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{6}$", msg["52"])

    def test_sendprep_defaults_to_protocol_standard(self):
        import re
        d40 = FixDictionary("FIX.4.0")
        msg = FixMessage({"35": "0"})
        msg["8"] = d40.begin_string()
        msg.sendprep(d40, "S", "T", 1)
        assert re.match(r"^\d{8}-\d{2}:\d{2}:\d{2}$", msg["52"])
