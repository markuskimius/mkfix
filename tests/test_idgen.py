"""Tests for the prefixed ID generator and its persisted state."""

import re
import tomllib
from pathlib import Path

import pytest
import pytest_asyncio

from mkio.change_bus import ChangeBus
from mkio.database import Database
from mkio.writer import WriteBatcher

from mkfix.fix.idgen import IdGenerator, _instance_code

TABLES = tomllib.loads(
    (Path(__file__).parent.parent / "mkfix" / "mkfix.toml").read_text()
)["tables"]

ID_RE = re.compile(r"^(RT|OR|EX|TR)(..)(\d{8})$")


@pytest_asyncio.fixture
async def stack():
    db = Database(path=":memory:", tables=TABLES, config={})
    await db.start()
    writer = WriteBatcher(db, ChangeBus())
    await writer.start()
    yield db, writer
    await writer.stop()
    await db.stop()


class TestInstanceCode:
    def test_first_two_chars_uppercased(self):
        assert _instance_code("mark") == "MA"
        assert _instance_code("Alice") == "AL"

    def test_short_usernames_padded_with_x(self):
        assert _instance_code("m") == "MX"
        assert _instance_code("") == "XX"


class TestIdGenerator:
    @pytest.mark.asyncio
    async def test_id_format(self, stack):
        db, writer = stack
        gen = IdGenerator(db, writer)
        an_id = await gen.next_id("RT")
        m = ID_RE.match(an_id)
        assert m, an_id
        assert m.group(2) == gen.instance_id
        assert m.group(3) == "00000001", "the counter starts at 1"

    @pytest.mark.asyncio
    async def test_counters_increment_per_prefix(self, stack):
        db, writer = stack
        gen = IdGenerator(db, writer)
        first_rt = await gen.next_id("RT")
        second_rt = await gen.next_id("RT")
        first_ex = await gen.next_id("EX")
        assert first_rt.endswith("00000001")
        assert second_rt.endswith("00000002")
        assert first_ex.endswith("00000001"), "each prefix counts independently"

    @pytest.mark.asyncio
    async def test_counters_survive_restart(self, stack):
        db, writer = stack
        gen = IdGenerator(db, writer)
        await gen.next_id("RT")
        await gen.next_id("RT")

        reborn = IdGenerator(db, writer)
        next_rt = await reborn.next_id("RT")
        assert next_rt.endswith("00000003"), "counters continue across restarts"
