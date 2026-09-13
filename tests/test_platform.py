"""Guards for what only breaks on Windows.

Python's default text encoding there is the ANSI code page, which cannot
hold the em-dashes and box rules in mkfix.toml, app.json and the sources,
so every text read or write in the repository has to name its encoding;
and a checkout with autocrlf must not turn the files the tests read
verbatim into something they no longer match, so .gitattributes pins LF.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(
    [*(ROOT / "mkfix").rglob("*.py"), *(ROOT / "tools").glob("*.py"), *(ROOT / "tests").glob("*.py")]
)


def _text_calls_without_encoding(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            kind = "open"
        elif isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            kind = func.attr
        else:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        if kind == "open":
            mode = node.args[1] if len(node.args) > 1 else None
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode = kw.value
            if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                continue
        missing.append(f"{path.relative_to(ROOT)}:{node.lineno} {kind}()")
    return missing


def test_every_text_read_and_write_names_its_encoding():
    assert SOURCES, "no sources found"
    missing = [m for path in SOURCES for m in _text_calls_without_encoding(path)]
    assert missing == [], "text I/O relying on the platform default encoding:\n" + "\n".join(missing)


def test_repository_text_is_utf8():
    """What the encoding parameter is for: the files carry characters the
    ANSI code page has no room for, so a default-encoded read gets them
    wrong, and the standard dictionaries stay ASCII-only so a hand edit can
    never depend on the reader's encoding."""
    for name in ("mkfix/mkfix.toml", "mkfix/static/app.json"):
        text = (ROOT / name).read_bytes().decode("utf-8")
        assert any(ord(c) > 127 for c in text), name
    for path in sorted((ROOT / "mkfix" / "fix" / "dictionary_data").glob("*.json")):
        path.read_bytes().decode("ascii")


def test_checkout_pins_lf_line_endings():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "* text=auto eol=lf" in rules
    assert "*.db binary" in rules
    for name in ("mkfix/mkfix.toml", "mkfix/static/app.json", "mkfix/static/index.html"):
        assert b"\r" not in (ROOT / name).read_bytes(), name
