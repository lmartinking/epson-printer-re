#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pyyaml",
#     "pysnmp>=7.1.20",
#     "pysnmp_sync_adapter>=1.1.0",
#     "text-console>=2.1.0",
#     "tk-date-entry>=1.1.0",
#     "pyprintlpr>=1.1.1",
#     "epson_escp2>=1.0.4",
#     "epson-usb>=0.1.0",
#     "pyusb>=1.2",
# ]
# ///
"""Dump the XP-442 || EEPROM via epson_print_conf over SNMP.

Usage:
    uv run --script dump_eeprom.py                       # dump to stdout
    uv run --script dump_eeprom.py -o dump.txt           # dump to file
    uv run --script dump_eeprom.py --diff baseline.txt   # dump and diff against baseline
    uv run --script dump_eeprom.py --detect-key          # brute-force the read_key

Output format (one line per address):
    0xADDR HH       hex address, space, hex byte value
    0xADDR --       read failed at that address

Requires: the epson_print_conf submodule (`git submodule update --init`)
          (pysnmp, pysnmp_sync_adapter, pyyaml, pyprintlpr, epson_escp2, etc.)

Printer: scanner.lan (Epson XP-442, read_key [1, 5])
"""
import sys, os, time, argparse, itertools, re, logging

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'epson_print_conf'))
from epson_print_conf import EpsonPrinter

HOSTNAME = "scanner.lan"
READ_KEY = [1, 5]
CHUNK = 256
END = 0xFFFF


def make_printer(read_key=None):
    conf = {
        "XP-442": {
            "read_key": read_key or READ_KEY,
            "write_key": b"Unknown_",
        }
    }
    return EpsonPrinter(conf_dict=conf, model="XP-442", hostname=HOSTNAME)


def check_connectivity(printer):
    test = printer.dump_eeprom(start=0, end=3)
    if all(v is None for v in test.values()):
        print("ERROR: cannot read EEPROM — check read_key and connectivity",
              file=sys.stderr)
        sys.exit(1)


def dump_full(printer, label="dump"):
    data = {}
    total_chunks = (END + 1) // CHUNK
    for i in range(0, END + 1, CHUNK):
        chunk_num = i // CHUNK + 1
        if chunk_num % 16 == 1 or chunk_num == total_chunks:
            print(f"  [{label}] chunk {chunk_num}/{total_chunks}: "
                  f"0x{i:04X}–0x{i+CHUNK-1:04X}", file=sys.stderr, flush=True)
        chunk = printer.dump_eeprom(start=i, end=i + CHUNK - 1)
        none_count = sum(1 for v in chunk.values() if v is None)
        if none_count == CHUNK:
            print(f"    all None at chunk {chunk_num} — past end of EEPROM, "
                  f"stopping", file=sys.stderr, flush=True)
            break
        data.update(chunk)
    return data


def format_dump(data):
    lines = []
    for addr in sorted(data.keys()):
        val = data[addr]
        if val is None:
            lines.append(f"0x{addr:04X} --")
        else:
            lines.append(f"0x{addr:04X} {val:02X}")
    return "\n".join(lines) + "\n"


def load_dump(path):
    data = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            addr = int(parts[0], 16)
            val = None if parts[1] == "--" else int(parts[1], 16)
            data[addr] = val
    return data


def diff_dumps(baseline, post):
    common = set(baseline.keys()) & set(post.keys())
    diffs = []
    for addr in sorted(common):
        v1, v2 = baseline[addr], post[addr]
        if v1 != v2:
            diffs.append((addr, v1, v2))
    return diffs


def print_diff(diffs, baseline):
    if not diffs:
        print("NO DIFFERENCES")
        return

    print(f"{len(diffs)} changed address(es):\n")
    print(f"{'Address':>10}  {'Baseline':>10}  {'Post':>10}  {'B(chr)':>6}  {'P(chr)':>6}")
    print(f"{'-------':>10}  {'--------':>10}  {'----':>10}  {'-----':>6}  {'-----':>6}")
    for addr, v1, v2 in diffs:
        s1 = f"0x{v1:02X}" if v1 is not None else "--"
        s2 = f"0x{v2:02X}" if v2 is not None else "--"
        c1 = chr(v1) if v1 is not None and 0x20 <= v1 < 0x7F else '.'
        c2 = chr(v2) if v2 is not None and 0x20 <= v2 < 0x7F else '.'
        print(f"  0x{addr:04X}  {s1:>10}  {s2:>10}  {c1:>6}  {c2:>6}")

    print(f"\nContext (baseline ±8 bytes around each change):")
    for addr, v1, v2 in diffs:
        lo, hi = max(0, addr - 8), min(END, addr + 8)
        hex_parts, ascii_parts = [], []
        for a in range(lo, hi + 1):
            v = baseline.get(a)
            marker = "**" if a == addr else "  "
            if v is None:
                hex_parts.append(f"{marker}--")
                ascii_parts.append(".")
            else:
                hex_parts.append(f"{marker}{v:02X}")
                ascii_parts.append(chr(v) if 0x20 <= v < 0x7F else '.')
        print(f"\n  0x{addr:04X} (0x{v1:02X} -> 0x{v2:02X}):")
        print(f"    {lo:#06x}: {''.join(hex_parts)}")
        print(f"    ASCII:  {''.join(ascii_parts)}")


def detect_key():
    """Brute-force the read_key. Narrow search first, then full."""
    printer = make_printer(read_key=[0, 0])

    # Connectivity check
    print("Checking printer connectivity...", file=sys.stderr, flush=True)
    printer.parm['read_key'] = [0, 0]
    oid = printer.eeprom_oid_read_address(0x00, label="connectivity_check")
    if oid is None:
        print("ERROR: cannot build EEPROM read query", file=sys.stderr)
        sys.exit(1)
    _, probe = printer.fetch_oid_values(oid, label="connectivity_check")[0]
    if not probe:
        print("ERROR: printer not responding to SNMP EEPROM reads",
              file=sys.stderr)
        sys.exit(1)
    print("Printer responding.", file=sys.stderr, flush=True)

    def try_key(x, y):
        printer.parm['read_key'] = [x, y]
        return printer._read_cell_for_scan("detect_key")

    # Phase 1: narrow — all first bytes, second byte 0–10
    # Known XP-4xx keys: [1,5], [85,5], [133,5], [49,6]
    combos = list(itertools.product(range(256), range(0, 11)))
    print(f"Phase 1: narrow search ({len(combos)} combos, second byte 0-10)",
          file=sys.stderr, flush=True)
    for i, (x, y) in enumerate(combos, 1):
        if i == 1 or i % 256 == 0:
            print(f"  [{i}/{len(combos)}] trying [{x}, {y}]",
                  file=sys.stderr, flush=True)
        try:
            val, answered = try_key(x, y)
        except Exception:
            continue
        if not answered:
            continue
        if val is not None:
            print(f"read_key = [{x}, {y}]")
            print(f"EEPROM[0x0000] = 0x{val}", file=sys.stderr)
            return

    # Phase 2: full search, skip already-tried
    combos_full = [(x, y) for x, y in itertools.product(range(256), range(256))
                   if y > 10]
    print(f"\nPhase 2: full search ({len(combos_full)} remaining combos)",
          file=sys.stderr, flush=True)
    silent = 0
    for i, (x, y) in enumerate(combos_full, 1):
        if i == 1 or i % 256 == 0:
            print(f"  [{i}/{len(combos_full)}] trying [{x}, {y}]",
                  file=sys.stderr, flush=True)
        try:
            val, answered = try_key(x, y)
        except Exception:
            continue
        if not answered:
            silent += 1
            if silent >= 10:
                print("ABORT: printer stopped responding", file=sys.stderr)
                sys.exit(1)
            continue
        silent = 0
        if val is not None:
            print(f"read_key = [{x}, {y}]")
            print(f"EEPROM[0x0000] = 0x{val}", file=sys.stderr)
            return

    print("FAILED: no valid read_key found", file=sys.stderr)
    sys.exit(1)


def main():
    global HOSTNAME
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-o', '--output', help='Write dump to file')
    parser.add_argument('--diff', metavar='BASELINE',
                        help='Dump and diff against a baseline file')
    parser.add_argument('--detect-key', action='store_true',
                        help='Brute-force the read_key instead of dumping')
    parser.add_argument('-a', '--address', default=HOSTNAME,
                        help=f'Printer hostname (default: {HOSTNAME})')
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    HOSTNAME = args.address

    if args.detect_key:
        detect_key()
        return

    printer = make_printer()
    print("Checking connectivity...", file=sys.stderr, flush=True)
    check_connectivity(printer)
    print("OK", file=sys.stderr, flush=True)

    t0 = time.time()
    data = dump_full(printer)
    elapsed = time.time() - t0
    print(f"Done: {len(data)} addresses in {elapsed:.1f}s", file=sys.stderr,
          flush=True)

    output = format_dump(data)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"Saved to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)

    if args.diff:
        print(f"\nLoading baseline from {args.diff}...", file=sys.stderr)
        baseline = load_dump(args.diff)
        diffs = diff_dumps(baseline, data)
        print_diff(diffs, baseline)


if __name__ == "__main__":
    main()
