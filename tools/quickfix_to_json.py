"""Convert QuickFIX XML data dictionaries into mkfix JSON dictionaries.

The generated files are derived from the QuickFIX project's spec files
(https://github.com/quickfix/quickfix/tree/master/spec), used under the
QuickFIX Software License 1.0. This product includes software developed by
quickfixengine.org (http://www.quickfixengine.org/).

Usage:
    python tools/quickfix_to_json.py                 # all versions, download XMLs as needed
    python tools/quickfix_to_json.py FIX.4.2 FIX.4.4 # specific versions
    python tools/quickfix_to_json.py --spec-dir ~/qf # use local XMLs, no download

Output schema (one JSON per version, e.g. FIX42.json):
    version       "FIX.4.2"
    begin_string  what tag 8 carries on the wire ("FIXT.1.1" for FIX 5.0+)
    header        ordered tag list of the standard header
    trailer       ordered tag list of the trailer
    fields        tag -> {name, type}
    enums         tag -> {code: CamelCaseName}
    messages      msgtype -> {name, category}
    groups        counter tag -> {delim, members: [tags]}  (display metadata only)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SPEC_URL = "https://raw.githubusercontent.com/quickfix/quickfix/master/spec/{}.xml"

VERSIONS: dict[str, str] = {
    "FIX.4.0": "FIX40",
    "FIX.4.1": "FIX41",
    "FIX.4.2": "FIX42",
    "FIX.4.3": "FIX43",
    "FIX.4.4": "FIX44",
    "FIX.5.0": "FIX50",
    "FIX.5.0SP1": "FIX50SP1",
    "FIX.5.0SP2": "FIX50SP2",
}
TRANSPORT_XML = "FIXT11"


def _sect(root: ET.Element, name: str) -> list[ET.Element]:
    section = root.find(name)
    return list(section) if section is not None else []


def camel(desc: str) -> str:
    return "".join(part.capitalize() for part in desc.split("_"))


def load_xml(spec_dir: Path, basename: str) -> ET.Element:
    path = spec_dir / f"{basename}.xml"
    if not path.exists():
        url = SPEC_URL.format(basename)
        print(f"  downloading {url}")
        spec_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url) as resp:
            path.write_bytes(resp.read())
    return ET.parse(path).getroot()


def field_defs(root: ET.Element) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, dict[str, str]]]:
    """Return (fields, name->tag, enums) from a <fields> section."""
    fields: dict[str, dict[str, str]] = {}
    by_name: dict[str, str] = {}
    enums: dict[str, dict[str, str]] = {}
    for f in _sect(root, "fields"):
        tag = f.get("number")
        name = f.get("name")
        if not tag or not name:
            continue
        fields[tag] = {"name": name, "type": f.get("type", "STRING")}
        by_name[name] = tag
        values = {v.get("enum"): camel(v.get("description", "")) for v in f if v.tag == "value"}
        if values:
            enums[tag] = values
    return fields, by_name, enums


def tag_list(section: ET.Element | None, by_name: dict[str, str]) -> list[str]:
    if section is None:
        return []
    out = []
    for e in section:
        if e.tag == "field" and e.get("name") in by_name:
            out.append(by_name[e.get("name")])
        elif e.tag == "group" and e.get("name") in by_name:
            out.append(by_name[e.get("name")])
    return out


def collect_groups(
    root: ET.Element,
    by_name: dict[str, str],
    groups: dict[str, dict[str, object]],
) -> None:
    """Walk messages and components, recording every repeating group's shape.

    A group's members are its direct leaf field tags (components flattened in);
    a nested group appears as its counter tag, which gets its own entry. The
    delimiter is the first leaf field. Repeat occurrences union their members,
    first occurrence wins the delimiter.
    """
    components: dict[str, ET.Element] = {
        c.get("name"): c for c in _sect(root, "components")
    }

    def leaf_tags(node: ET.Element) -> list[str]:
        """Ordered member tags of one group/component body, one level deep."""
        out: list[str] = []
        for e in node:
            name = e.get("name", "")
            if e.tag == "field":
                if name in by_name:
                    out.append(by_name[name])
            elif e.tag == "group":
                record(e)
                if name in by_name:
                    out.append(by_name[name])
            elif e.tag == "component" and name in components:
                out.extend(leaf_tags(components[name]))
        return out

    def record(group: ET.Element) -> None:
        counter = by_name.get(group.get("name", ""))
        if not counter:
            return
        members = leaf_tags(group)
        if not members:
            return
        entry = groups.setdefault(counter, {"delim": members[0], "members": []})
        known = set(entry["members"])
        entry["members"].extend(t for t in members if t not in known)

    def walk(node: ET.Element) -> None:
        for e in node:
            if e.tag == "group":
                record(e)
            elif e.tag == "component" and e.get("name") in components:
                walk(components[e.get("name")])

    for msg in _sect(root, "messages"):
        walk(msg)
    for comp in components.values():
        walk(comp)


def convert(version: str, spec_dir: Path) -> dict[str, object]:
    basename = VERSIONS[version]
    root = load_xml(spec_dir, basename)
    is_fixt = version.startswith("FIX.5")

    fields, by_name, enums = field_defs(root)
    messages = {
        m.get("msgtype"): {"name": m.get("name"), "category": m.get("msgcat", "app")}
        for m in _sect(root, "messages")
    }
    header = tag_list(root.find("header"), by_name)
    trailer = tag_list(root.find("trailer"), by_name)
    groups: dict[str, dict[str, object]] = {}
    collect_groups(root, by_name, groups)

    if is_fixt:
        t_root = load_xml(spec_dir, TRANSPORT_XML)
        t_fields, t_by_name, t_enums = field_defs(t_root)
        for tag, info in t_fields.items():
            fields.setdefault(tag, info)
        for tag, values in t_enums.items():
            enums.setdefault(tag, values)
        by_name = {**t_by_name, **by_name}
        for msgtype, info in {
            m.get("msgtype"): {"name": m.get("name"), "category": m.get("msgcat", "admin")}
            for m in _sect(t_root, "messages")
        }.items():
            messages.setdefault(msgtype, info)
        header = tag_list(t_root.find("header"), {**by_name, **t_by_name})
        trailer = tag_list(t_root.find("trailer"), {**by_name, **t_by_name})
        collect_groups(t_root, {**by_name, **t_by_name}, groups)

    return {
        "version": version,
        "begin_string": "FIXT.1.1" if is_fixt else version,
        "source": "Derived from QuickFIX spec files (quickfixengine.org); see NOTICE",
        "header": header,
        "trailer": trailer,
        "fields": dict(sorted(fields.items(), key=lambda kv: int(kv[0]))),
        "enums": dict(sorted(enums.items(), key=lambda kv: int(kv[0]))),
        "messages": messages,
        "groups": dict(sorted(groups.items(), key=lambda kv: int(kv[0]))),
    }


def apply_overlay(doc: dict[str, object], overlay_path: Path) -> None:
    """Merge a curated overlay over a converted document, overlay wins.

    fields/messages/groups merge per tag (overlay entry replaces), enums merge
    per tag per code (union, overlay wins), header/trailer/begin_string replace
    wholesale when present. Overlays keep display names stable across
    regenerations where QuickFIX's descriptions differ from the official spec.
    """
    overlay = json.loads(overlay_path.read_text())
    for key in ("fields", "messages", "groups"):
        for tag, entry in overlay.get(key, {}).items():
            doc[key][tag] = entry
    for tag, values in overlay.get("enums", {}).items():
        doc["enums"][tag] = {**doc["enums"].get(tag, {}), **values}
    for key in ("header", "trailer", "begin_string"):
        if key in overlay:
            doc[key] = overlay[key]
    for key in ("fields", "enums", "groups"):
        doc[key] = dict(sorted(doc[key].items(), key=lambda kv: int(kv[0])))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("versions", nargs="*", default=None,
                        help=f"versions to convert (default: all of {', '.join(VERSIONS)})")
    parser.add_argument("--overlay-dir", type=Path,
                        default=Path(__file__).parent / "overlays",
                        help="directory holding curated per-version overlay JSONs")
    parser.add_argument("--spec-dir", type=Path,
                        default=Path(__file__).parent / "quickfix_spec",
                        help="directory holding QuickFIX XML specs (downloaded here if absent)")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).parent.parent / "mkfix" / "fix" / "dictionary_data",
                        help="output directory for JSON dictionaries")
    args = parser.parse_args()

    versions = args.versions or list(VERSIONS)
    for version in versions:
        if version not in VERSIONS:
            print(f"unknown version {version!r}; expected one of {', '.join(VERSIONS)}",
                  file=sys.stderr)
            sys.exit(1)

    args.out.mkdir(parents=True, exist_ok=True)
    for version in versions:
        print(f"converting {version}")
        doc = convert(version, args.spec_dir)
        overlay_path = args.overlay_dir / f"{VERSIONS[version]}.json"
        if overlay_path.exists():
            print(f"  applying overlay {overlay_path}")
            apply_overlay(doc, overlay_path)
        out_path = args.out / f"{VERSIONS[version]}.json"
        with open(out_path, "w") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
        print(f"  wrote {out_path} "
              f"({len(doc['fields'])} fields, {len(doc['enums'])} enum tags, "
              f"{len(doc['messages'])} messages, {len(doc['groups'])} groups)")


if __name__ == "__main__":
    main()
