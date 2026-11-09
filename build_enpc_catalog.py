#!/usr/bin/env python3
"""Decode Epson ENPC template files and build the field catalog.

Runs against Epson's on-disk template set (as shipped inside EpsonNet Config's
app bundle) and produces:

    <out>/*.txt        one decoded string+token dump per ENDU file
    <out>/CATALOG.tsv  every fully-qualified ENPC.<cat>_<item>[_<sub>][.field]
                       path, with which templates carry it
    <out>/ITEMS.tsv    per-(cat,item,sub) summary rolled up for tables

Usage:
    build_enpc_catalog.py all <template-set-dir> <output-dir>
    build_enpc_catalog.py decode <one.dat>            # dev-only: dump one file

See ENPC.md and the wrapper build_enpc_catalog.sh in the repo root.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import os
import re
import struct
import sys
from typing import Iterable

# ---------------------------------------------------------------------------
# ENDU container reader
# ---------------------------------------------------------------------------
# Layout observed in every .dat/.ens file:
#   0x00  'E' 'N' 'D' 'U'
#   0x40  01 00 00 00 <length:BE32> 0000 0000 0000 0000
#   0x50  <body of `length` bytes>
#
# Body = string-table + structure-tokens interleaved. Strings are length-prefixed;
# if any byte >= 0x80 the whole run is bitwise-complement (XOR 0xFF) encoded.
# Structure tokens use a handful of short big-endian tuples that reference
# strings by their appearance order.


def _deobf(b: bytes) -> bytes:
    if b and any(x >= 0x80 for x in b):
        return bytes(x ^ 0xFF for x in b)
    return b


def decode_endu(path: str):
    """Return (strings, tokens) or None if the file is not ENDU-framed."""
    data = open(path, "rb").read()
    if data[:4] != b"ENDU":
        return None
    length = struct.unpack(">I", data[0x44:0x48])[0]
    body = data[0x50 : 0x50 + length]

    strings: list[str] = []
    tokens: list[tuple] = []
    i = 0
    n = len(body)
    while i < n:
        b = body[i]
        if b in (0x0A, 0x20):
            tokens.append(("WS",))
            i += 1
            continue
        if b in (0x10, 0x11, 0x20, 0x30) and i + 2 < n and body[i + 1] == 0x00:
            kind = {0x10: "OPEN", 0x11: "CLOSE", 0x20: "ATTR", 0x30: "MARK"}[b]
            tokens.append((kind, body[i + 2]))
            i += 3
            continue
        if b == 0x00 and i + 1 < n:
            tokens.append(("VAL", body[i + 1]))
            i += 2
            continue
        if b > 0 and i + 1 + b <= n:
            raw = body[i + 1 : i + 1 + b]
            dec = _deobf(raw)
            if all(9 <= x <= 126 or x == 0 for x in dec):
                try:
                    s = dec.decode("utf-8").rstrip("\x00")
                except UnicodeDecodeError:
                    s = None
                if s and all(c.isprintable() or c in "\t" for c in s):
                    strings.append(s)
                    tokens.append(("STR", len(strings) - 1))
                    i += 1 + b
                    continue
        tokens.append(("SKIP", b))
        i += 1
    return strings, tokens


def extract_strings(path: str) -> list[str]:
    """Fast path: strings only, no token list."""
    r = decode_endu(path)
    return r[0] if r else []


# ---------------------------------------------------------------------------
# Field-path scanner
# ---------------------------------------------------------------------------
# Matches ENPC.<CC>_<IIII>[_<SS>][.<field-path>] where the field-path may
# contain dotted subfields (array indices + inner names).
_FIELD_RE = re.compile(
    r"ENPC\.([0-9A-F]{2})_([0-9A-F]{4})(?:_([0-9A-F]{2}))?"
    r"(?:\.([A-Za-z0-9_.]+))?"
)


def _template_from_basename(base: str) -> str:
    """DeviceInfo_<CODE>.dat.txt -> <CODE>. Anything else returns the basename."""
    if base.startswith("DeviceInfo_") and base.endswith((".dat.txt", ".ens.txt")):
        return (
            base.replace("DeviceInfo_", "")
            .replace(".dat.txt", "")
            .replace(".ens.txt", "")
        )
    return base


# ---------------------------------------------------------------------------
# Pipeline: decode -> catalog -> item summary
# ---------------------------------------------------------------------------


def decode_all(src_dir: str, out_dir: str) -> int:
    """Decode every .dat/.ens ENDU file into <out_dir>/<basename>.txt."""
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(
        glob.glob(os.path.join(src_dir, "*.dat"))
        + glob.glob(os.path.join(src_dir, "*.ens"))
    )
    written = 0
    for f in files:
        r = decode_endu(f)
        if r is None:
            continue
        strings, tokens = r
        base = os.path.basename(f)
        out_path = os.path.join(out_dir, base + ".txt")
        with open(out_path, "w") as out:
            out.write(f"# {base}: {len(strings)} strings\n\n## STRINGS\n")
            for i, s in enumerate(strings):
                out.write(f"[{i:4d}] {s}\n")
            out.write("\n## TOKENS\n")
            for t in tokens:
                if t[0] == "STR":
                    out.write(f"STR({t[1]}) = {strings[t[1]]!r}\n")
                else:
                    out.write(f"{t}\n")
        written += 1
    return written


def build_catalog(decoded_dir: str, out_tsv: str) -> int:
    """Emit CATALOG.tsv: one row per (cat, item, sub, field_path)."""
    per_path: dict[tuple, set[str]] = collections.defaultdict(set)
    for f in sorted(glob.glob(os.path.join(decoded_dir, "*.txt"))):
        base = os.path.basename(f)
        tpl = _template_from_basename(base) or base
        text = open(f).read()
        for m in _FIELD_RE.finditer(text):
            cat, item, sub, field = m.group(1), m.group(2), m.group(3), m.group(4)
            per_path[(cat, item, sub or "", field or "")].add(tpl)

    with open(out_tsv, "w") as w:
        w.write("cat\titem\tsub\tfield_path\tntemplates\ttemplates_sample\n")
        for (cat, item, sub, fp), tpls in sorted(per_path.items()):
            sample = ",".join(sorted(tpls)[:5])
            w.write(f"{cat}\t{item}\t{sub}\t{fp}\t{len(tpls)}\t{sample}\n")
    return len(per_path)


def build_items(catalog_tsv: str, out_tsv: str) -> int:
    """Roll CATALOG.tsv up into ITEMS.tsv: one row per (cat, item, sub)."""
    per_item: dict[tuple, dict] = collections.OrderedDict()
    with open(catalog_tsv) as f:
        rd = csv.reader(f, delimiter="\t")
        next(rd)  # header
        for cat, item, sub, fp, n, tpls in rd:
            key = (cat, item, sub)
            if key not in per_item:
                per_item[key] = {
                    "primary": set(),
                    "paths": 0,
                    "ntemplates": 0,
                    "templates": set(),
                }
            primary = re.split(r"\.", fp)[0] if fp else ""
            if primary and not primary.isdigit():
                per_item[key]["primary"].add(primary)
            per_item[key]["paths"] += 1
            per_item[key]["ntemplates"] = max(per_item[key]["ntemplates"], int(n))
            per_item[key]["templates"].update(tpls.split(","))

    with open(out_tsv, "w") as w:
        w.write(
            "cat\titem\tsub\tprimary_names\tsubfield_count\ttemplate_count\tsample_templates\n"
        )
        for (cat, item, sub), v in per_item.items():
            primary = ",".join(sorted(v["primary"])) or "(base)"
            samples = ",".join(sorted(v["templates"])[:4])
            w.write(
                f"{cat}\t{item}\t{sub}\t{primary}\t{v['paths']}\t{v['ntemplates']}\t{samples}\n"
            )
    return len(per_item)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_all(src: str, out: str) -> int:
    if not os.path.isdir(src):
        print(f"error: source dir does not exist: {src}", file=sys.stderr)
        return 2
    n_dec = decode_all(src, out)
    n_cat = build_catalog(out, os.path.join(out, "CATALOG.tsv"))
    n_items = build_items(
        os.path.join(out, "CATALOG.tsv"), os.path.join(out, "ITEMS.tsv")
    )
    print(f"decoded {n_dec} ENDU files -> {out}/*.txt")
    print(f"catalog: {n_cat} paths          -> {out}/CATALOG.tsv")
    print(f"items:   {n_items} (cat,item)   -> {out}/ITEMS.tsv")
    return 0


def _cmd_decode(path: str) -> int:
    r = decode_endu(path)
    if r is None:
        print(f"error: not an ENDU file: {path}", file=sys.stderr)
        return 2
    strings, tokens = r
    print(f"# {path}: {len(strings)} strings")
    print("## STRINGS")
    for i, s in enumerate(strings):
        print(f"[{i:4d}] {s}")
    print("\n## TOKENS")
    for t in tokens:
        if t[0] == "STR":
            print(f"STR({t[1]}) = {strings[t[1]]!r}")
        else:
            print(t)
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Decode Epson ENPC templates and build the field catalog."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_all = sub.add_parser("all", help="decode + build catalog + build item summary")
    p_all.add_argument("src", help="TemplateSet/Default directory (ENDU .dat/.ens files)")
    p_all.add_argument("out", help="output dir (created if missing)")

    p_dec = sub.add_parser("decode", help="dump strings+tokens for one ENDU file")
    p_dec.add_argument("path")

    args = p.parse_args(argv)
    if args.cmd == "all":
        return _cmd_all(args.src, args.out)
    if args.cmd == "decode":
        return _cmd_decode(args.path)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
