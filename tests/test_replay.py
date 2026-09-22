"""The FIX log parser and what a replayed message looks like on the wire."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from mkfix.fix import replay
from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import SOH, parse_fix
from mkfix.fix.replay import (
    ReplayMessage, ReplayTask, parse_log_file, prepare_for_send, select_messages, summarize,
    _extract_timestamp_and_raw, _parse_fix_timestamp,
)

D44 = FixDictionary("FIX.4.4")


def _write_temp(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="latin-1")
    f.write(content)
    f.close()
    return f.name


class TestParseFixTimestamp:
    def test_with_millis(self):
        ts = _parse_fix_timestamp("20240115-14:30:45.123")
        assert ts == datetime(2024, 1, 15, 14, 30, 45, 123000)

    def test_without_millis(self):
        ts = _parse_fix_timestamp("20240115-14:30:45")
        assert ts == datetime(2024, 1, 15, 14, 30, 45)

    def test_invalid(self):
        assert _parse_fix_timestamp("garbage") is None
        assert _parse_fix_timestamp("") is None


class TestExtractTimestampAndRaw:
    def test_quickfix_format(self):
        line = "20240115-14:30:45.123 : 8=FIX.4.2|35=D|55=AAPL"
        ts, raw = _extract_timestamp_and_raw(line)
        assert ts == datetime(2024, 1, 15, 14, 30, 45, 123000)
        assert raw == "8=FIX.4.2|35=D|55=AAPL"

    def test_iso_format_pipe(self):
        line = "2024-01-15 14:30:45.123 | 8=FIX.4.2|35=D|55=AAPL"
        ts, raw = _extract_timestamp_and_raw(line)
        assert ts.year == 2024
        assert raw == "8=FIX.4.2|35=D|55=AAPL"

    def test_iso_format_colon(self):
        line = "2024-01-15 14:30:45 : 8=FIX.4.2|35=D"
        ts, raw = _extract_timestamp_and_raw(line)
        assert ts == datetime(2024, 1, 15, 14, 30, 45)
        assert raw == "8=FIX.4.2|35=D"

    def test_no_timestamp(self):
        line = "8=FIX.4.2|35=D|55=AAPL"
        ts, raw = _extract_timestamp_and_raw(line)
        assert ts is None
        assert raw == "8=FIX.4.2|35=D|55=AAPL"


class TestParseLogFile:
    def test_pipe_delimited(self):
        path = _write_temp(
            "8=FIX.4.2|35=D|49=A|56=B|55=AAPL|54=1\n"
            "8=FIX.4.2|35=8|49=B|56=A|55=AAPL|150=0\n"
        )
        msgs = parse_log_file(path)
        assert [(m.msg_type, m.sender, m.target) for m in msgs] == [("D", "A", "B"), ("8", "B", "A")]

    def test_soh_delimited(self):
        path = _write_temp(f"8=FIX.4.2{SOH}35=D{SOH}55=AAPL{SOH}\n")
        msgs = parse_log_file(path)
        assert len(msgs) == 1
        assert parse_fix(msgs[0].raw)["55"] == "AAPL"

    def test_quickfix_timestamped(self):
        path = _write_temp("20240115-14:30:45.123 : 8=FIX.4.2|35=D|55=AAPL\n")
        msgs = parse_log_file(path)
        assert msgs[0].timestamp == datetime(2024, 1, 15, 14, 30, 45, 123000)

    def test_empty_and_comment_lines_skipped(self):
        path = _write_temp("# a header naming 35=D\n\n8=FIX.4.2|35=D|55=AAPL\n   \n# trailing note\n")
        assert len(parse_log_file(path)) == 1

    def test_non_fix_lines_skipped(self):
        path = _write_temp("Started session\n8=FIX.4.2|35=D|55=AAPL\nConnection closed\n")
        assert len(parse_log_file(path)) == 1

    def test_admin_messages_are_left_out(self):
        path = _write_temp("\n".join(f"8=FIX.4.2|35={t}|49=A|56=B" for t in "A 0 1 2 3 4 5 D".split()) + "\n")
        assert [m.msg_type for m in parse_log_file(path)] == ["D"]

    def test_timestamp_falls_back_to_sending_time_then_transact_time(self):
        path = _write_temp(
            "8=FIX.4.2|35=D|52=20240115-14:30:45.123|60=20240115-14:30:44.000|55=AAPL\n"
            "8=FIX.4.2|35=D|60=20240115-14:30:46.500|55=AAPL\n"
            "8=FIX.4.2|35=D|55=AAPL\n"
        )
        assert [m.timestamp for m in parse_log_file(path)] == [
            datetime(2024, 1, 15, 14, 30, 45, 123000), datetime(2024, 1, 15, 14, 30, 46, 500000), None]

    def test_empty_file(self):
        assert parse_log_file(_write_temp("")) == []


class TestSummary:
    def test_pairs_types_and_span(self):
        path = _write_temp(
            "20240115-14:30:45.000 : 8=FIX.4.4|35=D|49=CLI|56=VEN|55=AAPL\n"
            "20240115-14:30:45.041 : 8=FIX.4.4|35=8|49=VEN|56=CLI|55=AAPL\n"
            "20240115-14:30:50.000 : 8=FIX.4.4|35=D|49=CLI|56=VEN|55=IBM\n"
            "20240115-14:30:51.000 : 8=FIX.4.4|35=A|49=CLI|56=VEN\n"
        )
        s = summarize(parse_log_file(path))
        assert s["pairs"] == [{"sender": "CLI", "target": "VEN", "count": 2},
                              {"sender": "VEN", "target": "CLI", "count": 1}]
        assert s["types"] == {"8": 1, "D": 2}
        assert s["names"] == {"8": "ExecutionReport", "D": "NewOrderSingle"}
        assert (s["first"], s["last"], s["count"]) == ("20240115-14:30:45.000", "20240115-14:30:50.000", 3)
        assert replay.describe_pairs(s) == "CLI→VEN 2, VEN→CLI 1"

    def test_headerless_lines_make_one_blank_direction(self):
        s = summarize(parse_log_file(_write_temp("35=D|11=1|55=IBM\n35=F|11=2|41=1\n")))
        assert s["pairs"] == [{"sender": "", "target": "", "count": 2}]
        assert replay.direction_key("", "") == ""
        assert replay.describe_pairs(s) == "(no CompIDs) 2"


class TestSelection:
    MSGS = [
        ReplayMessage(datetime(2024, 1, 15, 9, 0, 0), "35=D|49=CLI|56=VEN", "D", "CLI", "VEN"),
        ReplayMessage(datetime(2024, 1, 15, 9, 0, 1), "35=8|49=VEN|56=CLI", "8", "VEN", "CLI"),
        ReplayMessage(datetime(2024, 1, 15, 12, 0, 0), "35=F|49=CLI|56=VEN", "F", "CLI", "VEN"),
        ReplayMessage(None, "35=G|49=CLI|56=VEN", "G", "CLI", "VEN"),
    ]

    def test_direction(self):
        assert [m.msg_type for m in select_messages(self.MSGS, "CLI→VEN")] == ["D", "F", "G"]
        assert [m.msg_type for m in select_messages(self.MSGS, "VEN→CLI")] == ["8"]
        assert len(select_messages(self.MSGS, "")) == 4

    def test_types(self):
        assert [m.msg_type for m in select_messages(self.MSGS, msg_filter="D, G")] == ["D", "G"]

    def test_window_is_by_time_of_day_and_lets_unstamped_messages_through(self):
        assert [m.msg_type for m in select_messages(self.MSGS, time_from="09:00:01")] == ["8", "F", "G"]
        assert [m.msg_type for m in select_messages(self.MSGS, time_to="09:00")] == ["D", "G"]
        assert [m.msg_type for m in select_messages(self.MSGS, time_from="10:00", time_to="13:00")] == ["F", "G"]

    def test_bad_window_is_refused(self):
        with pytest.raises(ValueError, match="HH:MM"):
            select_messages(self.MSGS, time_from="noon")


class TestPrepareForSend:
    """A logged message goes out as the session's: its own header tags,
    TransactTime now, routing tags and groups as logged."""

    LOGGED = ("8=FIX.4.4|9=200|35=D|49=PRODCLI|56=PRODVENUE|34=1042|52=20240115-14:30:45.000|"
              "43=Y|122=20240115-14:30:40.000|369=2041|50=DESK1|57=ALGO|115=FUND7|116=SUB7|128=DELIV|"
              "11=PRD-1|21=1|55=IBM|54=1|38=100|40=2|44=150.25|59=0|60=20240115-14:30:45.000|"
              "453=2|448=TRADER1|447=D|452=11|448=FIRM|447=D|452=1|58=hello|10=123|")

    def _sent(self, sender="Client", target="Server", seq=7, now="20260921-10:00:00.000"):
        msg = prepare_for_send(parse_fix(self.LOGGED), D44, now)
        msg.sendprep(D44, sender, target, seq)
        return msg

    def test_session_tags_are_the_sessions(self):
        sent = self._sent()
        assert (sent["49"], sent["56"], sent["34"]) == ("Client", "Server", "7")
        assert sent["52"] != "20240115-14:30:45.000" and sent["52"].startswith("2")
        assert sent["8"] == "FIX.4.4"

    def test_transact_time_is_now(self):
        assert self._sent(now="20260921-10:00:00.000")["60"] == "20260921-10:00:00.000"

    def test_sequence_space_tags_are_gone(self):
        sent = self._sent()
        assert "43" not in sent and "122" not in sent and "369" not in sent

    def test_routing_tags_and_body_are_as_logged(self):
        sent = self._sent()
        for tag, value in (("50", "DESK1"), ("57", "ALGO"), ("115", "FUND7"), ("116", "SUB7"),
                           ("128", "DELIV"), ("11", "PRD-1"), ("44", "150.25"), ("58", "hello")):
            assert sent[tag] == value, tag

    def test_repeating_group_and_body_order_survive(self):
        wire = self._sent().to_pipe_string()
        header, body = wire.split("|52=", 1)
        assert body.split("|", 1)[1] == (
            "11=PRD-1|21=1|55=IBM|54=1|38=100|40=2|44=150.25|59=0|60=20260921-10:00:00.000|"
            "453=2|448=TRADER1|447=D|452=11|448=FIRM|447=D|452=1|58=hello|10=" + wire.rsplit("10=", 1)[1])
        # the routing tags sit in the header, where the dictionary puts them
        assert header.startswith("8=FIX.4.4|9=")
        assert header.endswith("|35=D|49=Client|56=Server|115=FUND7|128=DELIV|34=7|50=DESK1|57=ALGO|116=SUB7")

    def test_checksum_and_length_are_recomputed(self):
        sent = self._sent()
        again = parse_fix(sent.to_pipe_string())
        assert again["9"] == sent["9"] and again["10"] == sent["10"]
        assert sent["9"] != "200" and sent["10"] != "123"

    def test_a_headerless_line_gets_the_whole_header(self):
        msg = prepare_for_send(parse_fix("35=D|11=1|55=IBM|54=1|38=100|40=1|"), D44, "now")
        msg.sendprep(D44, "Client", "Server", 1)
        assert msg.to_pipe_string().startswith("8=FIX.4.4|9=") and "|49=Client|56=Server|34=1|52=" in msg.to_pipe_string()


class TestPacing:
    def _task(self, speed, max_gap=30.0):
        return ReplayTask(1, None, [], speed=speed, max_gap=max_gap)

    def test_gaps_scale_by_speed_and_cap_at_max_gap(self):
        a, b = datetime(2024, 1, 15, 9, 0, 0), datetime(2024, 1, 15, 9, 0, 10)
        rm = ReplayMessage(b, "", "D", "", "")
        assert self._task(1.0).delay_before(rm, a) == 10.0
        assert self._task(4.0).delay_before(rm, a) == 2.5
        assert self._task(1.0, max_gap=3).delay_before(rm, a) == 3.0
        assert self._task(0).delay_before(rm, a) == 0.0
        assert self._task(1.0, max_gap=0).delay_before(rm, a) == 10.0, "0 = no cap"

    def test_no_delay_without_two_timestamps_or_going_backwards(self):
        a = datetime(2024, 1, 15, 9, 0, 0)
        assert self._task(1.0).delay_before(ReplayMessage(None, "", "D", "", ""), a) == 0.0
        assert self._task(1.0).delay_before(ReplayMessage(a, "", "D", "", ""), None) == 0.0
        assert self._task(1.0).delay_before(ReplayMessage(a, "", "D", "", ""), datetime(2024, 1, 15, 9, 1)) == 0.0


class TestExamples:
    def test_every_bundled_log_parses_summarizes_and_prepares(self):
        names = {e["name"] for e in replay.examples()}
        assert names == {"order-flow", "two-sided-day", "pipe-raw", "soh-raw", "iso-timestamped"}
        for e in replay.examples():
            assert e["title"] and e["description"], e["name"]
            msgs = parse_log_file(replay.resolve_path(f"example:{e['name']}"))
            assert msgs, e["name"]
            assert all(m.timestamp for m in msgs), f"{e['name']} must pace itself"
            assert not any(m.msg_type in replay.ADMIN_MSG_TYPES for m in msgs)
            for m in msgs:
                prepare_for_send(parse_fix(m.raw), D44, "now").sendprep(D44, "A", "B", 1)
            summarize(msgs)

    def test_the_two_sided_day_holds_both_directions_and_a_lunch_gap(self):
        msgs = parse_log_file(replay.resolve_path("example:two-sided-day"))
        s = summarize(msgs)
        assert [(p["sender"], p["target"]) for p in s["pairs"]] == [("PRODCLI", "PRODVENUE"), ("PRODVENUE", "PRODCLI")]
        assert set(s["types"]) == {"D", "G", "F", "Q", "8", "9"}
        client = select_messages(msgs, "PRODCLI→PRODVENUE")
        gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in zip(client, client[1:])]
        assert max(gaps) > 3600, "the lunch break is what max_gap is for"
        routed = next(m for m in client if "50=DESK1" in m.raw)
        assert "453=1|448=TRADER1|447=D|452=11" in routed.raw

    def test_resolve_path(self):
        assert replay.resolve_path("example:order-flow").name == "order-flow.log"
        assert replay.resolve_path(" example:../order-flow ").name == "order-flow.log", "no escaping the folder"
        with pytest.raises(ValueError, match="No bundled example"):
            replay.resolve_path("example:nothing")
        with pytest.raises(ValueError, match="Give a file path"):
            replay.resolve_path("  ")
        with pytest.raises(ValueError, match="No such file"):
            replay.resolve_path("/nowhere/at/all.log")
        own = _write_temp("35=D|55=IBM\n")
        assert replay.resolve_path(own) == Path(own)
