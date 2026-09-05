"""Tests for FIX log file parser."""

import tempfile
from datetime import datetime
from pathlib import Path

from mkfix.fix.message import SOH
from mkfix.fix.replay import parse_log_file, _extract_timestamp_and_raw, _parse_fix_timestamp


class TestParseFixTimestamp:
    def test_with_millis(self):
        ts = _parse_fix_timestamp("20240115-14:30:45.123")
        assert ts == datetime(2024, 1, 15, 14, 30, 45, 123000)

    def test_without_millis(self):
        ts = _parse_fix_timestamp("20240115-14:30:45")
        assert ts == datetime(2024, 1, 15, 14, 30, 45)

    def test_invalid(self):
        assert _parse_fix_timestamp("garbage") is None


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
    def _write_temp(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_pipe_delimited(self):
        path = self._write_temp(
            "8=FIX.4.2|35=D|55=AAPL|54=1\n"
            "8=FIX.4.2|35=8|55=AAPL|150=0\n"
        )
        msgs = parse_log_file(path)
        assert len(msgs) == 2
        assert msgs[0].msg_type == "D"
        assert msgs[1].msg_type == "8"
        Path(path).unlink()

    def test_soh_delimited(self):
        line = f"8=FIX.4.2{SOH}35=A{SOH}49=SENDER{SOH}56=TARGET{SOH}10=000{SOH}"
        path = self._write_temp(line + "\n")
        msgs = parse_log_file(path)
        assert len(msgs) == 1
        assert msgs[0].msg_type == "A"
        assert SOH in msgs[0].raw and "|" not in msgs[0].raw, "SOH logs replay exactly"
        Path(path).unlink()

    def test_quickfix_timestamped(self):
        path = self._write_temp(
            "20240115-09:30:00.000 : 8=FIX.4.2|35=D|55=MSFT\n"
            "20240115-09:30:01.500 : 8=FIX.4.2|35=8|55=MSFT\n"
        )
        msgs = parse_log_file(path)
        assert len(msgs) == 2
        assert msgs[0].timestamp == datetime(2024, 1, 15, 9, 30, 0)
        assert msgs[1].timestamp == datetime(2024, 1, 15, 9, 30, 1, 500000)
        Path(path).unlink()

    def test_empty_lines_skipped(self):
        path = self._write_temp(
            "\n\n8=FIX.4.2|35=D|55=AAPL\n\n\n"
        )
        msgs = parse_log_file(path)
        assert len(msgs) == 1
        Path(path).unlink()

    def test_non_fix_lines_skipped(self):
        path = self._write_temp(
            "# This is a comment\n"
            "some random text\n"
            "8=FIX.4.2|35=D|55=AAPL\n"
        )
        msgs = parse_log_file(path)
        assert len(msgs) == 1
        Path(path).unlink()

    def test_timestamp_from_sending_time(self):
        path = self._write_temp(
            "8=FIX.4.2|35=D|52=20240115-10:00:00.000|55=AAPL\n"
        )
        msgs = parse_log_file(path)
        assert len(msgs) == 1
        assert msgs[0].timestamp == datetime(2024, 1, 15, 10, 0, 0)
        Path(path).unlink()

    def test_empty_file(self):
        path = self._write_temp("")
        msgs = parse_log_file(path)
        assert len(msgs) == 0
        Path(path).unlink()
