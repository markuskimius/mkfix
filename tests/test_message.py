"""Tests for FIX message parsing, serialization, and factory."""

from mkfix.fix.message import FixMessage, FixMessageFactory, parse_fix, _checksum, SOH
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
