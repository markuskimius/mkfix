"""Tests for FIX data dictionary."""

from mkfix.fix.dictionary import FixDictionary


class TestFixDictionary:
    def setup_method(self):
        self.d = FixDictionary("FIX.4.2")

    def test_begin_string(self):
        assert self.d.begin_string() == "FIX.4.2"

    def test_tag_name_known(self):
        assert self.d.tag_name("35") == "MsgType"
        assert self.d.tag_name("55") == "Symbol"
        assert self.d.tag_name("49") == "SenderCompID"

    def test_tag_name_unknown(self):
        assert self.d.tag_name("99999") == "99999"

    def test_enum_name_known(self):
        assert self.d.enum_name("54", "1") == "Buy"
        assert self.d.enum_name("54", "2") == "Sell"

    def test_enum_name_unknown_value(self):
        assert self.d.enum_name("54", "Z") == "Z"

    def test_enum_name_unknown_tag(self):
        assert self.d.enum_name("99999", "X") == "X"

    def test_msg_type_name(self):
        assert self.d.msg_type_name("D") == "NewOrderSingle"
        assert self.d.msg_type_name("8") == "ExecutionReport"
        assert self.d.msg_type_name("A") == "Logon"

    def test_msg_type_name_unknown(self):
        assert self.d.msg_type_name("ZZ") == "ZZ"

    def test_msg_category(self):
        assert self.d.msg_category("A") == "ADMIN"
        assert self.d.msg_category("D") == "APP"

    def test_msg_category_unknown(self):
        assert self.d.msg_category("ZZ") == "APP"

    def test_is_special(self):
        assert self.d.is_special("8")
        assert self.d.is_special("9")
        assert self.d.is_special("10")
        assert not self.d.is_special("35")

    def test_is_header(self):
        assert self.d.is_header("8")
        assert self.d.is_header("35")
        assert self.d.is_header("49")
        assert not self.d.is_header("55")

    def test_is_trailer(self):
        assert self.d.is_trailer("10")
        assert not self.d.is_trailer("35")

    def test_header_tags_order(self):
        assert self.d.header_tags[0] == "8"
        assert "35" in self.d.header_tags

    def test_unknown_version_fallback(self):
        d = FixDictionary("FIX.9.9")
        assert d.tag_name("35") == "35"
        assert d.msg_type_name("D") == "D"
        assert d.begin_string() == "FIX.9.9"

    def test_ord_type_enums(self):
        assert self.d.enum_name("40", "1") == "Market"
        assert self.d.enum_name("40", "2") == "Limit"

    def test_ord_status_enums(self):
        assert self.d.enum_name("39", "0") == "New"
        assert self.d.enum_name("39", "2") == "Filled"

    def test_tif_enums(self):
        assert self.d.enum_name("59", "0") == "Day"
        assert self.d.enum_name("59", "1") == "GoodTillCancel"
