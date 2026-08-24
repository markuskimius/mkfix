"""Node-backed tests for the client dictionary module's group-tree parser.

Skipped when node isn't installed; the parser is pure JS shared by the
message-detail pane, so this is the one place its logic runs headless.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "mkfix" / "static"
DATA = Path(__file__).parent.parent / "mkfix" / "fix" / "dictionary_data"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def parse_tree(tmp_path: Path, version_file: str, raw: str):
    module = tmp_path / "fix-dictionary.mjs"
    module.write_text((STATIC / "fix-dictionary.js").read_text())
    script = tmp_path / "run.mjs"
    script.write_text(
        f"""
import {{ FixDictionary, parseMessageTree }} from {json.dumps(module.as_uri())};
import fs from "node:fs";
const doc = JSON.parse(fs.readFileSync({json.dumps(str(DATA / version_file))}, "utf8"));
const dict = new FixDictionary(doc);
console.log(JSON.stringify(parseMessageTree({json.dumps(raw)}, dict)));
"""
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_flat_message_stays_flat(tmp_path):
    raw = "8=FIX.4.2|9=52|35=D|11=C1|55=AAPL|54=1|38=100|40=2|44=1.5|10=000|"
    tree = parse_tree(tmp_path, "FIX42.json", raw)
    assert all(n["kind"] == "field" for n in tree)
    assert [n["tag"] for n in tree] == ["8", "9", "35", "11", "55", "54", "38", "40", "44", "10"]


def test_repeating_group_instances(tmp_path):
    raw = (
        "8=FIX.4.2|9=100|35=8|37=X|17=E1|20=0|150=2|39=2|"
        "382=2|375=BRK1|337=T1|375=BRK2|55=AAPL|54=1|10=123|"
    )
    tree = parse_tree(tmp_path, "FIX42.json", raw)
    group = next(n for n in tree if n["tag"] == "382")
    assert group["kind"] == "group"
    assert group["value"] == "2"
    assert len(group["entries"]) == 2
    assert [(f["tag"], f["value"]) for f in group["entries"][0]] == [
        ("375", "BRK1"),
        ("337", "T1"),
    ]
    assert [(f["tag"], f["value"]) for f in group["entries"][1]] == [("375", "BRK2")]
    tags_after = [n["tag"] for n in tree[tree.index(group) + 1:]]
    assert tags_after == ["55", "54", "10"]


def test_nested_groups(tmp_path):
    raw = (
        "8=FIX.4.4|9=100|35=D|11=C1|55=AAPL|54=1|"
        "453=2|448=ID1|447=D|452=1|802=1|523=SUB1|448=ID2|"
        "38=100|10=000|"
    )
    tree = parse_tree(tmp_path, "FIX44.json", raw)
    parties = next(n for n in tree if n["tag"] == "453")
    assert len(parties["entries"]) == 2
    first = parties["entries"][0]
    nested = next(n for n in first if n["tag"] == "802")
    assert nested["kind"] == "group"
    assert [(f["tag"], f["value"]) for f in nested["entries"][0]] == [("523", "SUB1")]
    assert [(f["tag"], f["value"]) for f in parties["entries"][1]] == [("448", "ID2")]


def test_unknown_group_falls_flat(tmp_path):
    raw = "8=FIX.4.0|9=52|35=8|382=1|375=BRK|55=AAPL|10=000|"
    tree = parse_tree(tmp_path, "FIX40.json", raw)
    assert all(n["kind"] == "field" for n in tree)
