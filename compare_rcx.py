#!/usr/bin/env python3
"""Compare two RCX files byte-by-byte.

Usage: compare_rcx.py ORIGINAL.rcx REPACKED.rcx
"""
import sys


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    a = open(argv[0], 'rb').read()
    b = open(argv[1], 'rb').read()
    if len(a) != len(b):
        print(f'Sizes differ: {len(a)} vs {len(b)} bytes')
    diffs = [(i, a[i], b[i]) for i in range(min(len(a), len(b))) if a[i] != b[i]]
    print(f'Total differing bytes: {len(diffs)}')
    if diffs:
        print(f'First diff at byte offset: 0x{diffs[0][0]:x} ({diffs[0][0]})')
        print(f'Last diff at byte offset:  0x{diffs[-1][0]:x} ({diffs[-1][0]})')
        print()
        for i, orig, new in diffs:
            print(f'  offset 0x{i:06x}: original=0x{orig:02x} repacked=0x{new:02x}')
        return 1
    if len(a) == len(b):
        print('Files are byte-identical')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
