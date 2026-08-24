"""Tests for FIX data dictionary."""

from mkfix.fix.dictionary import (FixDictionary, custom_names, merge_dictionary,
                                  register_custom, unregister_custom)


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

    def test_field_type(self):
        assert self.d.field_type("52") == "UTCTIMESTAMP"
        assert self.d.field_type("38") == "QTY"
        assert self.d.field_type("99999") == "STRING"

    def test_groups(self):
        g = self.d.group("382")
        assert g is not None
        assert g["delim"] == "375"
        assert "375" in g["members"]
        assert self.d.group("55") is None


class TestStandardVersions:
    def test_all_standard_versions_ship(self):
        from mkfix.fix.dictionary import STANDARD_VERSIONS

        for version in STANDARD_VERSIONS:
            d = FixDictionary(version)
            assert d.tag_name("35") == "MsgType", version
            assert d.msg_type_name("A") == "Logon", version
            assert d.header_tags[0] == "8", version
            assert d.trailer_tags[-1] == "10", version
            assert d.groups, version

    def test_fix50_uses_fixt_transport(self):
        for version in ("FIX.5.0", "FIX.5.0SP1", "FIX.5.0SP2"):
            d = FixDictionary(version)
            assert d.begin_string() == "FIXT.1.1", version
            assert d.is_header("1128"), version  # ApplVerID
            assert d.msg_type_name("D") == "NewOrderSingle", version

    def test_fix4x_begin_string_is_version(self):
        for version in ("FIX.4.0", "FIX.4.1", "FIX.4.2", "FIX.4.3", "FIX.4.4"):
            assert FixDictionary(version).begin_string() == version

    def test_load_is_cached(self):
        a = FixDictionary("FIX.4.4")
        b = FixDictionary("FIX.4.4")
        assert a.fields is b.fields

    def test_fix44_party_group(self):
        d = FixDictionary("FIX.4.4")
        g = d.group("453")
        assert g["delim"] == "448"
        assert set(g["members"]) >= {"448", "447", "452", "802"}


class TestCustomDictionaries:
    def teardown_method(self):
        for name in list(custom_names()):
            unregister_custom(name)

    def test_delta_over_base(self):
        register_custom("MY42", "FIX.4.2", {
            "fields": {
                "1": {"name": "ClientAccount", "type": "STRING"},
                "5001": {"name": "OurAlgoId", "type": "STRING"},
                "58": None,
            },
            "enums": {"40": {"X": "PeggedSpecial"}, "54": {"9": None}},
            "messages": {"D": {"name": "OrderSingle", "category": "app"}},
        })
        d = FixDictionary("MY42")
        assert d.tag_name("1") == "ClientAccount"     # overridden
        assert d.tag_name("5001") == "OurAlgoId"      # added
        assert d.tag_name("58") == "58"               # removed
        assert d.tag_name("55") == "Symbol"           # inherited
        assert d.enum_name("40", "X") == "PeggedSpecial"
        assert d.enum_name("40", "2") == "Limit"      # union keeps base codes
        assert d.enum_name("54", "9") == "9"          # removed code
        assert d.enum_name("54", "1") == "Buy"
        assert d.msg_type_name("D") == "OrderSingle"
        assert d.begin_string() == "FIX.4.2"          # inherited from base
        assert d.group("382") is not None             # groups inherited
        assert d.is_header("35")

    def test_standalone(self):
        register_custom("BARE", None, {
            "begin_string": "FIX.4.2",
            "header": ["8", "9", "35", "49", "56", "34", "52"],
            "trailer": ["10"],
            "fields": {"35": {"name": "MsgType", "type": "STRING"}},
        })
        d = FixDictionary("BARE")
        assert d.tag_name("35") == "MsgType"
        assert d.tag_name("55") == "55"               # nothing inherited
        assert d.begin_string() == "FIX.4.2"
        assert d.is_header("49")

    def test_unregister_restores_standard_resolution(self):
        register_custom("TWEAK", "FIX.4.2", {"fields": {"55": {"name": "Ticker", "type": "STRING"}}})
        assert FixDictionary("TWEAK").tag_name("55") == "Ticker"
        unregister_custom("TWEAK")
        assert FixDictionary("TWEAK").tag_name("55") == "55"

    def test_custom_group_definition(self):
        register_custom("GRP", "FIX.4.2", {
            "fields": {"9001": {"name": "NoCustomThings", "type": "NUMINGROUP"},
                       "9002": {"name": "CustomThing", "type": "STRING"}},
            "groups": {"9001": {"delim": "9002", "members": ["9002"]}},
        })
        d = FixDictionary("GRP")
        assert d.group("9001") == {"delim": "9002", "members": ["9002"]}

    def test_merge_removes_whole_enum_tag(self):
        merged = merge_dictionary({"enums": {"54": {"1": "Buy"}}}, {"enums": {"54": None}})
        assert merged["enums"] == {}

    def test_register_replaces_previous(self):
        register_custom("RE", "FIX.4.2", {"fields": {"55": {"name": "A", "type": "STRING"}}})
        register_custom("RE", "FIX.4.2", {"fields": {"55": {"name": "B", "type": "STRING"}}})
        assert FixDictionary("RE").tag_name("55") == "B"
