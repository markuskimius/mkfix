"""Tests for the prefixed ID generator and its persisted state."""

import getpass
import re
import tomllib
from pathlib import Path

import pytest
import pytest_asyncio

from mkio.change_bus import ChangeBus
from mkio.database import Database
from mkio.writer import WriteBatcher

from mkfix.fix.idgen import IdGenerator, _instance_code, validate_instance_code

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

    def test_explicit_code_is_used_verbatim(self):
        assert validate_instance_code("Q7") == "Q7"
        assert validate_instance_code("ab") == "ab"

    @pytest.mark.parametrize("bad", ["", "A", "ABC", "A-", "é1", "A B"])
    def test_explicit_code_must_be_two_alphanumerics(self, bad):
        with pytest.raises(ValueError, match="instance code"):
            validate_instance_code(bad)


class TestIdGenerator:
    @pytest.mark.asyncio
    async def test_explicit_instance_code_overrides_username(self, stack):
        db, writer = stack
        gen = IdGenerator(db, writer, instance_code="Q7")
        assert await gen.next_id("RT") == "RTQ700000001"
        assert gen.instance_id == "Q7"
        assert gen.instance_source == "saved"

    @pytest.mark.asyncio
    async def test_default_code_comes_from_username(self, stack):
        db, writer = stack
        gen = IdGenerator(db, writer)
        await gen.start()
        assert gen.instance_id == _instance_code(getpass.getuser())
        assert gen.instance_source == "username"

    @pytest.mark.asyncio
    async def test_explicit_code_persists_until_changed_or_cleared(self, stack):
        """-i is remembered: later runs without it keep the code, a new code
        replaces it, and '' goes back to the username default."""
        db, writer = stack
        await IdGenerator(db, writer, instance_code="Q7").start()
        cur = await db.read_conn.execute("SELECT value FROM fix_settings WHERE key = 'instance_code'")
        assert (await cur.fetchone())["value"] == "Q7"
        await cur.close()

        reborn = IdGenerator(db, writer)
        await reborn.start()
        assert reborn.instance_id == "Q7"
        assert reborn.instance_source == "saved"

        await IdGenerator(db, writer, instance_code="Z9").start()
        again = IdGenerator(db, writer)
        await again.start()
        assert again.instance_id == "Z9"

        cleared = IdGenerator(db, writer, instance_code="")
        await cleared.start()
        assert cleared.instance_id == _instance_code(getpass.getuser())
        assert cleared.instance_source == "username"
        cur = await db.read_conn.execute("SELECT value FROM fix_settings WHERE key = 'instance_code'")
        assert (await cur.fetchone())["value"] == "", "cleared, not deleted: '' means username"
        await cur.close()
        after = IdGenerator(db, writer)
        await after.start()
        assert after.instance_id == _instance_code(getpass.getuser())
        assert after.instance_source == "username"

    @pytest.mark.asyncio
    async def test_counters_survive_a_code_change(self, stack):
        db, writer = stack
        first = IdGenerator(db, writer, instance_code="Q7")
        assert await first.next_id("RT") == "RTQ700000001"
        second = IdGenerator(db, writer, instance_code="Z9")
        assert await second.next_id("RT") == "RTZ900000002"

    def test_bad_explicit_code_rejected_at_construction(self, stack):
        db, writer = stack
        with pytest.raises(ValueError):
            IdGenerator(db, writer, instance_code="TOOLONG")

    @pytest.mark.asyncio
    async def test_saved_code_is_validated_on_read(self, stack):
        """A hand-edited fix_settings row can't smuggle a malformed code into
        the ID format; it is ignored like an empty one."""
        db, writer = stack
        await db.write_conn.execute(
            "INSERT INTO fix_settings (key, value) VALUES ('instance_code', 'TOOLONG')"
        )
        await db.write_conn.commit()
        gen = IdGenerator(db, writer)
        await gen.start()
        assert gen.instance_id == _instance_code(getpass.getuser())
        assert gen.instance_source == "username"

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
