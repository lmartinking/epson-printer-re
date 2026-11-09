#!/usr/bin/env python3
"""Export / apply Ghidra annotations through the GhidraMCP HTTP bridge.

Annotations are function names, labels and comments, stored as JSON with no
firmware bytes, so they can be published while the firmware cannot.

    annotations.py export [-o annotations/<program>.json]
    annotations.py apply  annotations/rtos_core.json [--dry-run]

Needs the GhidraMCP plugin serving the target program (default
http://localhost:8089). Standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

FORMAT_VERSION = 1
COMMENT_KINDS = ("plate", "pre", "eol", "post", "repeatable")
# Names Ghidra generates itself; these are not annotations.
AUTO_NAME = re.compile(
    r"^(thunk_)?(EXT_)?(FUN|DAT|LAB|PTR|SUB|UNK|EXT|OFF)_|^switch(data)?D_|^caseD_|^s_|^u_"
)


class Bridge:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _open(self, req):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read().decode()
        except urllib.error.URLError as e:
            sys.exit(f"error: cannot reach GhidraMCP at {self.base} ({e.reason}).\n"
                     "Start Ghidra with the GhidraMCP plugin and open the program.")
        try:
            return json.loads(body)
        except ValueError:
            sys.exit(f"error: unexpected response from {req.full_url}: {body[:200]}")

    def get(self, path, **q):
        url = self.base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        return self._open(urllib.request.Request(url))

    def post(self, path, **body):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return self._open(req)


def program_info(b):
    meta = b.get("/get_metadata")
    return {
        "name": meta["program_name"],
        "language": meta["language"],
        "imageBase": "0x" + meta["base_address"],
        "size": meta["total_memory_size"],
        "sha256": file_sha256(meta.get("executable_path")),
        "_path": meta.get("executable_path"),
    }


def file_sha256(path):
    if path and os.path.isfile(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


def fetch_functions(b):
    return b.get("/find_functions", limit=0)["functions"]


def fetch_comment(b, addr):
    c = b.get("/get_comment", address=addr)
    return {k: c[k] for k in COMMENT_KINDS if c.get(k)}


def cmd_export(args):
    b = Bridge(args.url)
    info = program_info(b)
    path = info.pop("_path")
    if info["sha256"] is None:
        print(f"warning: cannot read {path}; SHA-256 will be null", file=sys.stderr)

    funcs = fetch_functions(b)
    data = b.get("/list_program_items", kind="data_items", limit=0)["items"]

    # Thunks that merely inherit their target's name are Ghidra's doing, not ours.
    real_names = {f["name"] for f in funcs if not f["is_thunk"]}
    symbols = []
    for f in funcs:
        if f["is_external"] or AUTO_NAME.match(f["name"]):
            continue
        if f["is_thunk"] and f["name"] in real_names:
            continue
        symbols.append({"address": "0x" + f["address"], "name": f["name"], "kind": "function"})
    for d in data:
        if not AUTO_NAME.match(d["label"]):
            symbols.append({"address": "0x" + d["address"], "name": d["label"], "kind": "label"})
    symbols.sort(key=lambda s: int(s["address"], 16))

    # The bridge cannot list commented addresses, so probe every function
    # entry and every named data label.
    probe = sorted({"0x" + f["address"] for f in funcs}
                   | {s["address"] for s in symbols if s["kind"] == "label"})
    comments = []
    with ThreadPoolExecutor(16) as ex:
        for addr, c in zip(probe, ex.map(lambda a: fetch_comment(b, a), probe)):
            if c:
                comments.append({"address": addr, **c})

    out = {"formatVersion": FORMAT_VERSION, "program": info,
           "symbols": symbols, "comments": comments}
    dest = args.output or os.path.join("annotations", info["name"].rsplit(".", 1)[0] + ".json")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"{dest}: {len(symbols)} symbols, {len(comments)} comments "
          f"(probed {len(probe)} addresses)")


def cmd_apply(args):
    with open(args.file) as f:
        ann = json.load(f)
    if ann.get("formatVersion") != FORMAT_VERSION:
        sys.exit(f"error: unsupported formatVersion {ann.get('formatVersion')}")
    b = Bridge(args.url)
    want, have = ann["program"], program_info(b)

    problems = []
    if want["language"] != have["language"]:
        problems.append(f"language is {have['language']}, annotations expect {want['language']}")
    if want["imageBase"] != have["imageBase"]:
        problems.append(f"base address is {have['imageBase']}, annotations expect {want['imageBase']}")
    if want["size"] != have["size"]:
        problems.append(f"program size is {have['size']}, annotations expect {want['size']}")
    if want.get("sha256") and have["sha256"] and want["sha256"] != have["sha256"]:
        problems.append("the imported file's SHA-256 differs from the one annotated")
    if problems:
        for p in problems:
            print("MISMATCH:", p, file=sys.stderr)
        if not args.force:
            sys.exit("error: this does not look like the annotated firmware. "
                     "Use --force to apply anyway.")

    funcs = {"0x" + f["address"]: f for f in fetch_functions(b)}
    done = {"renamed": 0, "created": 0, "labels": 0, "comments": 0, "skipped": 0}
    verb = "would " if args.dry_run else ""

    def report(kind, msg):
        done[kind] += 1
        print(f"  {verb}{msg}")

    for s in ann["symbols"]:
        addr, name = s["address"], s["name"]
        if s["kind"] == "function":
            cur = funcs.get(addr)
            if cur is None:
                if not args.dry_run:
                    r = b.post("/create_function", address=addr, name=name)
                    if r.get("error"):
                        print(f"  FAILED create_function {addr}: {r['error']}", file=sys.stderr)
                        done["skipped"] += 1
                        continue
                report("created", f"create function {name} at {addr}")
            elif cur["name"] != name:
                if not args.dry_run:
                    r = b.post("/rename_function", old_name=addr, new_name=name, strict_mode="off")
                    if r.get("error"):
                        print(f"  FAILED rename {addr}: {r['error']}", file=sys.stderr)
                        done["skipped"] += 1
                        continue
                report("renamed", f"rename {cur['name']} -> {name} at {addr}")
        else:
            if not args.dry_run:
                r = b.post("/create_label", address=addr, name=name)
                if r.get("error") or r.get("success") is False:
                    print(f"  FAILED label {addr}: {r.get('error', r)}", file=sys.stderr)
                    done["skipped"] += 1
                    continue
            report("labels", f"label {name} at {addr}")

    for c in ann["comments"]:
        addr = c["address"]
        current = fetch_comment(b, addr)
        for kind in COMMENT_KINDS:
            if kind in c and current.get(kind) != c[kind]:
                if not args.dry_run:
                    r = b.post("/set_comment", address=addr, comment=c[kind], type=kind)
                    if r.get("error"):
                        print(f"  FAILED comment {addr}: {r['error']}", file=sys.stderr)
                        done["skipped"] += 1
                        continue
                report("comments", f"{kind} comment at {addr}")

    print(f"{'Dry run: ' if args.dry_run else ''}{done['renamed']} renames, {done['created']} functions created, "
          f"{done['labels']} labels, {done['comments']} comments, {done['skipped']} failed")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("GHIDRA_MCP_URL", "http://localhost:8089"),
                   help="GhidraMCP base URL (env GHIDRA_MCP_URL, default http://localhost:8089)")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write annotations of the open program to JSON")
    e.add_argument("-o", "--output", help="output file (default annotations/<program>.json)")
    e.set_defaults(fn=cmd_export)
    a = sub.add_parser("apply", help="apply an annotations JSON to the open program")
    a.add_argument("file")
    a.add_argument("--dry-run", action="store_true", help="show changes without making them")
    a.add_argument("--force", action="store_true", help="apply even if the program does not match")
    a.set_defaults(fn=cmd_apply)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
