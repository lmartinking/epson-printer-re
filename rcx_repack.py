#!/usr/bin/env python3
"""Repack extracted firmware components back into a valid signed .rcx file.

Inverse of extract_firmware.py / rcx_unpack.  Given the directory layout that
rcx_unpack produces, this:

  1. Reads each extracted IPL header, segment, and the RCX header.
  2. Recomputes the 2-byte checksum and 20-byte SHA-1 for every non-empty
     segment record.
  3. Updates the Z_1 and Z_2 partition lengths in the RCX header to match the
     actual assembled block sizes.
  4. Assembles the final .rcx and prints a verification summary.

IPL header layout (4096 bytes):
  [0x00-0x08]  "EPSON IPL"  (9-byte magic)
  [0x09-0x0C]  version / magic bytes
  [0x0E-0x0F]  segment_record_bytes (little-endian H, typically 0x6c = 3×0x24)
  [0x10+]      3 × 0x24-byte segment records:
    [0x00-0x01]  f0           (H)
    [0x02-0x03]  checksum     (H)  — sum(segment_bytes) & 0xFFFF
    [0x04-0x05]  f4           (H)
    [0x06-0x07]  padding      (H)
    [0x08-0x09]  f8           (H)
    [0x0A-0x0B]  padding      (H)
    [0x0C-0x0F]  length       (L)
    [0x10-0x23]  SHA-1 digest (20 bytes) — SHA1(segment_bytes + SHA1(hdr[:0x0E]))

Block assembly order:
  [RCX header (291 B)] [Block 0: ipl_hdr + seg0 + seg1] [Block 1: ipl_hdr + seg0 + seg1]

The Z_1 / Z_2 lengths in the RCX header must reflect the actual on-disk sizes of
each block (ipl_hdr + all non-empty segments).

Usage:
    python3 rcx_repack.py <input_dir> <output.rcx>

Example:
    python3 rcx_repack.py rcx_output/ FWF729TL_repacked.rcx
"""
from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RCX_HEADER_SIZE = 291
IPL_HEADER_SIZE = 0x1000
SEGMENT_RECORD_SIZE = 0x24
SHA1_DIGEST_SIZE = 20
EXPECTED_SEGMENT_RECORD_COUNT = 3


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def read_file(path: Path) -> bytearray:
    with open(path, "rb") as f:
        return bytearray(f.read())


def parse_ipl_header(hdr: bytearray) -> list[tuple[int, int, int]]:
    """Parse non-empty segment records from an IPL header.

    Returns a list of (record_index, offset_in_header, length) while validating
    the magic and the record count encoded at offset 0x0E.
    """
    if hdr[:9] != b"EPSON IPL":
        raise ValueError(f"bad IPL magic: {hdr[:9]!r}")

    rec_bytes, = struct.unpack("<H", hdr[0x0E:0x10])
    if rec_bytes % SEGMENT_RECORD_SIZE:
        raise ValueError(
            f"segment_record_bytes is not a multiple of 0x{SEGMENT_RECORD_SIZE:x}: "
            f"0x{rec_bytes:x}"
        )
    num_records = rec_bytes // SEGMENT_RECORD_SIZE
    if num_records != EXPECTED_SEGMENT_RECORD_COUNT:
        raise ValueError(
            "unexpected record count "
            f"{num_records} (expected {EXPECTED_SEGMENT_RECORD_COUNT})"
        )

    segments: list[tuple[int, int, int]] = []
    for record_index in range(num_records):
        base = 0x10 + SEGMENT_RECORD_SIZE * record_index
        length, = struct.unpack("<L", hdr[base + 0x0C : base + 0x10])
        if length > 0:
            segments.append((record_index, base, length))

    return segments


# ---------------------------------------------------------------------------
# Signing helpers
# ---------------------------------------------------------------------------


def compute_checksum(data: bytes) -> int:
    """2-byte checksum: sum of all bytes, masked to 16 bits."""
    return sum(data) & 0xFFFF


def compute_sha1(data: bytes, header_hash: bytes) -> bytes:
    """20-byte SHA-1: SHA1(segment_data + SHA1(ipl_header[:0x0E]))."""
    h = hashlib.sha1(data)
    h.update(header_hash)
    return h.digest()


def header_hash(hdr: bytearray) -> bytes:
    """SHA-1 of the first 0x0E bytes of an IPL header."""
    return hashlib.sha1(hdr[:0x0E]).digest()


# ---------------------------------------------------------------------------
# Core repack logic
# ---------------------------------------------------------------------------


def recompute_segment(hdr: bytearray, seg_data: bytes) -> tuple[int, bytes]:
    """Recompute checksum and SHA-1 for one segment record.

    Returns (new_checksum, new_sha1_digest).
    """
    cs = compute_checksum(seg_data)
    hh = header_hash(hdr)
    sha = compute_sha1(seg_data, hh)
    return cs, sha


def update_ipl_header(
    hdr: bytearray, seg_data_map: dict[int, bytes]
) -> None:
    """Rewrite lengths, checksums, and SHA-1 fields in an IPL header.

    seg_data_map: record_index -> segment_bytes  (only non-empty records needed)
    """
    record_bytes, = struct.unpack("<H", hdr[0x0E:0x10])
    if record_bytes != EXPECTED_SEGMENT_RECORD_COUNT * SEGMENT_RECORD_SIZE:
        raise ValueError(
            "unexpected segment record bytes "
            f"0x{record_bytes:x} "
            f"(expected 0x{EXPECTED_SEGMENT_RECORD_COUNT * SEGMENT_RECORD_SIZE:x})"
        )

    hh = header_hash(hdr)

    for rec_idx in range(EXPECTED_SEGMENT_RECORD_COUNT):
        rec_base = 0x10 + SEGMENT_RECORD_SIZE * rec_idx
        length, = struct.unpack("<L", hdr[rec_base + 0x0C : rec_base + 0x10])
        if length == 0:
            # Zero-length record: wipe checksum and SHA-1 to zero.
            struct.pack_into("<H", hdr, rec_base + 0x02, 0)
            hdr[rec_base + 0x10 : rec_base + 0x24] = b"\x00" * SHA1_DIGEST_SIZE
            continue

        seg_data = seg_data_map.get(rec_idx)
        if seg_data is None:
            raise ValueError(
                f"missing segment data for record {rec_idx} "
                f"(header claims length {length})"
            )

        # Keep the record length in sync with the actual extracted component.
        struct.pack_into("<L", hdr, rec_base + 0x0C, len(seg_data))
        cs, sha = recompute_segment(hdr, seg_data)
        struct.pack_into("<H", hdr, rec_base + 0x02, cs)
        hdr[rec_base + 0x10 : rec_base + 0x24] = sha


def update_rcx_z_lengths(rcx_header: bytearray, z1_size: int, z2_size: int) -> None:
    """Update the Z_1/5 and Z_2/5 INI values in the RCX header.

    The header is ASCII INI; we do a simple text substitution so the rest of
    the header is preserved verbatim.
    """

    def replace_section(text: str, section: str, key: str, value: str) -> str:
        """Replace key=value in the given INI section, preserving surrounding text."""
        sect_marker = "[" + section + "]"
        idx = text.index(sect_marker)
        next_sect = text.find("\n[", idx + 1)
        if next_sect == -1:
            next_sect = len(text)
        block = text[idx:next_sect]
        # Find the key line within the section block.
        # Lines use \r\n, so split on \r\n to keep things clean.
        old_line = None
        for line in block.split("\r\n"):
            if line.startswith(key + "="):
                old_line = line
                break
        if old_line is None:
            raise ValueError(f"key {key!r} not found in section [{section}]")
        new_line = key + "=\"" + value + "\""
        text = text.replace(old_line + "\r\n", new_line + "\r\n", 1)
        return text

    text = rcx_header.decode("ascii")
    text = replace_section(text, "Z_1", "5", str(z1_size))
    text = replace_section(text, "Z_2", "5", str(z2_size))
    rcx_header[:] = text.encode("ascii")


def assemble_rcx(
    input_dir: Path, output_path: Path
) -> None:
    """Build the .rcx file from extracted components and write *output_path*."""

    # --- Read inputs --------------------------------------------------------
    rcx_header = read_file(input_dir / "rcx_header")
    ipl_0_hdr = read_file(input_dir / "ipl_0_hdr")
    ipl_1_hdr = read_file(input_dir / "ipl_1_hdr")
    ipl_0_seg0 = read_file(input_dir / "ipl_0_seg0")
    ipl_0_seg1 = read_file(input_dir / "ipl_0_seg1")
    ipl_1_seg0 = read_file(input_dir / "ipl_1_seg0")
    ipl_1_seg1 = read_file(input_dir / "ipl_1_seg1")

    # --- Validate inputs ----------------------------------------------------
    if len(rcx_header) != RCX_HEADER_SIZE:
        raise ValueError(
            f"rcx_header is {len(rcx_header)} B, expected {RCX_HEADER_SIZE}"
        )
    for name, hdr in [("ipl_0_hdr", ipl_0_hdr), ("ipl_1_hdr", ipl_1_hdr)]:
        if len(hdr) != IPL_HEADER_SIZE:
            raise ValueError(f"{name} is {len(hdr)} B, expected {IPL_HEADER_SIZE}")

    seg0_0_map = {0: ipl_0_seg0, 1: ipl_0_seg1}
    seg1_0_map = {0: ipl_1_seg0, 1: ipl_1_seg1}

    # Print verification summary
    def print_segment_stats(label: str, hdr: bytearray, seg_map: dict[int, bytes]) -> None:
        print(f"\n=== {label} ===")
        for rec_idx, rec_base, length in parse_ipl_header(hdr):
            seg_data = seg_map[rec_idx]
            stored_cs = struct.unpack("<H", hdr[rec_base + 0x02 : rec_base + 0x04])[0]
            stored_sha = hdr[rec_base + 0x10 : rec_base + 0x24].hex()
            comp_cs, comp_sha = recompute_segment(hdr, seg_data)
            cs_ok = "OK" if stored_cs == comp_cs else "MISMATCH"
            sha_ok = "OK" if stored_sha == comp_sha.hex() else "MISMATCH"
            print(
                f"  seg{rec_idx}: len={length:,}  "
                f"checksum=0x{stored_cs:04x} ({cs_ok})  "
                f"SHA-1={stored_sha[:16]}… ({sha_ok})"
            )

    print_segment_stats("Block 0 (before re-sign)", ipl_0_hdr, seg0_0_map)
    print_segment_stats("Block 1 (before re-sign)", ipl_1_hdr, seg1_0_map)

    # --- Re-sign headers ----------------------------------------------------
    update_ipl_header(ipl_0_hdr, seg0_0_map)
    update_ipl_header(ipl_1_hdr, seg1_0_map)

    # --- Print post-sign stats ----------------------------------------------
    print_segment_stats("Block 0 (after re-sign)", ipl_0_hdr, seg0_0_map)
    print_segment_stats("Block 1 (after re-sign)", ipl_1_hdr, seg1_0_map)

    # --- Compute block sizes and update RCX header --------------------------
    z1_size = len(ipl_0_hdr) + len(ipl_0_seg0) + len(ipl_0_seg1)
    z2_size = len(ipl_1_hdr) + len(ipl_1_seg0) + len(ipl_1_seg1)

    # Parse current Z_1 / Z_2 values from the INI header
    in_section = ""
    for line in rcx_header.decode("ascii").split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1]
        if in_section in ("Z_1", "Z_2") and stripped.startswith("5="):
            val = int(stripped.split("=", 1)[1].strip('"'))
            if in_section == "Z_1":
                orig_z1 = val
            else:
                orig_z2 = val

    update_rcx_z_lengths(rcx_header, z1_size, z2_size)

    print(f"\nRCX header Z_1: {orig_z1:,} -> {z1_size:,}")
    print(f"RCX header Z_2: {orig_z2:,} -> {z2_size:,}")

    # --- Assemble final .rcx ------------------------------------------------
    parts = [rcx_header, ipl_0_hdr, ipl_0_seg0, ipl_0_seg1, ipl_1_hdr, ipl_1_seg0, ipl_1_seg1]
    output_path.write_bytes(b"".join(parts))

    total = sum(p.__len__() for p in parts)
    print(f"\nWrote {output_path}  ({total:,} bytes)")
    print(f"  RCX header:    {len(rcx_header):,} B")
    print(f"  Block 0:       {len(ipl_0_hdr):,} + {len(ipl_0_seg0):,} + {len(ipl_0_seg1):,} = {z1_size:,} B")
    print(f"  Block 1:       {len(ipl_1_hdr):,} + {len(ipl_1_seg0):,} + {len(ipl_1_seg1):,} = {z2_size:,} B")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {sys.argv[0]} <input_dir> <output.rcx>")
        return 2
    input_dir = Path(argv[0])
    output_path = Path(argv[1])
    if not input_dir.is_dir():
        print(f"error: input directory not found: {input_dir}")
        return 1
    try:
        assemble_rcx(input_dir, output_path)
        return 0
    except Exception as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
