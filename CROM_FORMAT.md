# CROM Compression Format — Epson XP-442 Firmware

Analysis of the CROM ("Compressed ROM") format used in the Epson XP-442
firmware (SoC09S platform). Based on reverse engineering of `uncrom.py`
from the [abrasive/epson-reversing](https://github.com/abrasive/epson-reversing)
toolkit, binary analysis of `rtos_core.bin`, and decompression results.

## Overview

CROM is a proprietary Epson compression format that uses **Huffman coding
combined with LZ77 copy-back** to compress ARM firmware images. The format
borrows heavily from JPEG's marker and Huffman table structure (SOI markers,
DHT tables) but is not JPEG — it compresses arbitrary binary data, not
images.

The compressed data decompresses to ARM code + data that loads into RAM
at fixed addresses (0x45xxxxxx range for the RTOS application).

## File Structure

A CROM file begins with a 4-byte magic header, followed by one or more
independent **segments**, each of which decompresses to a separate binary
blob.

```
┌────────────────────────────────────┐
│ Magic: "CROM" (4 bytes, ASCII)     │
├────────────────────────────────────┤
│ Segment 0                          │
├────────────────────────────────────┤
│ Segment 1                          │
├────────────────────────────────────┤
│ ...                                │
├────────────────────────────────────┤
│ 0xFF padding to end of flash block │
└────────────────────────────────────┘
```

End-of-data is signalled by encountering `0xFF` fill bytes or a short read
where the next SOI marker is expected.

### Segment Structure

Each segment uses JPEG-derived markers (big-endian):

```
┌─────────────────────────────────────────────────────┐
│ SOI marker: 0xFFD8 (2 bytes)                        │
│ Total segment length (4 bytes, big-endian uint32)    │
│   (includes these 4 bytes but not the 0xFFD8)       │
├─────────────────────────────────────────────────────┤
│ DHT marker: 0xFFC4 (2 bytes)                        │
│ DHT length (2 bytes, big-endian, includes self)      │
│ Huffman table data (length - 2 bytes)                │
│   Contains 3 Huffman tables (see below)              │
├─────────────────────────────────────────────────────┤
│ Copy-data marker: 0xFFB1 (2 bytes)                  │
│ Info length (2 bytes, big-endian, includes self)      │
│ Flag byte (1 byte, observed value: 0x03)             │
│ Coded bytes count (4 bytes, big-endian uint32)       │
│ Number of copy items (4 bytes, big-endian uint32)    │
│ Huffman-coded copy data (coded_bytes bytes)          │
├─────────────────────────────────────────────────────┤
│ Literal marker: 0xFFB2 (2 bytes)                    │
│ Info length (2 bytes, big-endian, includes self)      │
│ Literal data length (4 bytes, big-endian uint32)     │
│ Literal data (literal_length bytes)                  │
└─────────────────────────────────────────────────────┘
```

### Marker Summary

| Marker | JPEG Equivalent | Purpose |
|--------|----------------|---------|
| `0xFFD8` | SOI (Start of Image) | Start of segment |
| `0xFFC4` | DHT (Define Huffman Table) | Huffman table definitions |
| `0xFFB1` | (unused in JPEG) | Huffman-coded copy/control items |
| `0xFFB2` | (unused in JPEG) | Literal (uncompressed) data |

## Huffman Tables

Each segment contains exactly **3 Huffman tables**, encoded identically to
JPEG DHT (Define Huffman Table) entries:

```
For each table i (i = 0, 1, 2):
  Identifier byte: 0xF0 + i
  16 count bytes: counts[1..16] — number of codes of each bit length
  Symbol bytes: one per code, total = sum(counts)
```

The tables are decoded into 65536-entry lookup tables for fast prefix
decoding (same technique as optimized JPEG decoders).

### Table Roles

| Table | ID | Purpose | Typical symbols |
|-------|----|---------|-----------------|
| 0 | 0xF0 | **Control byte** — literal count or copy length | 0–255 (0x00–0xFF) |
| 1 | 0xF1 | **Offset low byte** — low 8 bits of copy offset | 0–255 |
| 2 | 0xF2 | **Offset high byte** — high 8 bits of copy offset | 0–255 |

### Example Table Statistics (rtos_core.bin, Segment 0)

| Table | Symbols | Code length distribution |
|-------|---------|------------------------|
| 0 (control) | 146 | Mostly 10–16 bit codes |
| 1 (offset low) | 247 | Spread across 5–16 bit codes |
| 2 (offset high) | 256 | All symbols present, 2–10 bit codes |

## Decompression Algorithm

The decompression is a two-pass process:

### Pass 1: Huffman Decode Copy Items

The Huffman-coded data in the `0xFFB1` section is decoded into `num_items`
triples of `(control, offset_low, offset_high)`:

```python
for each of num_items items:
    control   = huffman_decode(table_0, bitstream)
    offset_lo = huffman_decode(table_1, bitstream)
    offset_hi = huffman_decode(table_2, bitstream)
```

### Pass 2: Execute Items (LZ77 Reconstruction)

Each decoded triple is executed against the output buffer and the literal
data pool:

```python
offset = offset_lo | (offset_hi << 8)   # 16-bit copy-back offset

if offset == 0:
    if control == 0xFF:
        # NOP — skip (alignment/padding)
        continue
    # LITERAL: copy (control + 1) bytes from the literal pool
    count = control + 1
    output.extend(literal_data[literal_ptr : literal_ptr + count])
    literal_ptr += count

else:
    # COPY-BACK (LZ77): copy (control + 2) bytes from output history
    count = control + 2
    # Copy from output[-offset], handling overlapping copies
    # (run-length encoding when count > offset)
    while count > 0:
        chunk = min(count, offset)
        output.extend(output[-offset : -offset + chunk])
        count -= chunk
```

### Key Properties

- **Literal runs**: Up to 256 bytes at a time (control 0–254, yielding 1–255 bytes; control 255 with offset 0 is NOP)
- **Copy-back distance**: 1–65535 bytes (16-bit offset, 0 = literal mode)
- **Copy-back length**: 2–257 bytes (control 0–255, yielding 2–257 bytes)
- **Overlapping copies**: When `count > offset`, the copy wraps and repeats, implementing run-length encoding (e.g., offset=1, count=100 repeats the last byte 100 times)
- **All data is big-endian** (markers, lengths, counts) — the decompressed ARM code is little-endian

## Compression Ratio

### rtos_core.bin (Block 0)

| | Compressed | Decompressed | Ratio |
|---|-----------|-------------|-------|
| Segment 0 | 239,487 bytes | 514,144 bytes | 2.15:1 |
| Segment 1 | 7,465 bytes | 12,236 bytes | 1.64:1 |
| **Total** | **246,952 bytes** | **526,380 bytes** | **2.13:1** |

The CROM section occupies 0x40000–0x7C4B2 in the flash image (249 KB), with
the rest of the 0x40000–0x10F400 region being:
- 0x7C4B2–0x10AFFF: 0xFF padding (584 KB)
- 0x10B000–0x10EFFF: ARM MMU L1 page table (16 KB, 4096 section descriptors)
- 0x10F400–0x110400: Version tags + MiLL loader (1 KB)

### ipl_1_seg1 (Block 1)

| Stream | Decompressed | Purpose |
|--------|-------------|---------|
| A (web) | 5,657,680 bytes | WebConfig admin UI + ARM code |
| B (panel) | 4,148,304 bytes | Front-panel UI application |
| C (gfx) | 2,324,208 bytes | Graphics, fonts, icons (EFF/GIF files) |

Block 1 CROM data starts at offset 0x10 in `ipl_1_seg1` (after version tags
`RO20J9` + `TXW01K` at offset 0).

## Decompressed Segment Analysis

### Segment 0: RTOS Application Code (514 KB)

- **Load address**: ~0x45430000 (determined by self-referencing address analysis)
- **End address**: ~0x454AD860
- **Content**: ARM code — the main RTOS application
  - 11,812 BL (branch-link) instructions, of which 2,141 go outside the segment
  - External calls target 0x45B0xxxx and 0x45B5xxxx (other decompressed modules)
  - 53 LDR PC thunks at offset 0x6969C that jump back to **kernel addresses** (0x10760, 0x105CC, 0x15548, 0x10EA4 etc.) — confirming this code calls into the uncompressed kernel
  - 4,167 references to 0x454xxxxx (self-references)
  - Development/diagnostic menu strings at the end: "Color 10mm", "Skip Scan", "Push[OK]Button", "Select Develop Menu"

### Segment 1: API Vector Table (12 KB)

- **Content**: 35 jump vector entries, each 8 bytes:
  ```
  LDR PC, [PC, #-4]     ; e51ff004
  .word target_address   ; absolute address in 0x45xxxxxx
  ```
  Followed by ~90 bytes of additional ARM code (entry dispatcher).
- **Target address range**: 0x45454184–0x45E464C4
- **Address groups**:
  - 0x454xxxxx: 17 entries (core RTOS services)
  - 0x45Bxxxxx: 7 entries
  - 0x45Cxxxxx: 8 entries (memory/resource)
  - 0x45Dxxxxx: 1 entry
  - 0x45Exxxxx: 2 entries (I/O services)

This is the **RTOS API dispatch table** — the kernel's thunk table at
~0x166B8 in the uncompressed code jumps into these vectors, which then
jump to the actual implementations in the decompressed application.

## MMU Page Table (at 0x10B000)

The 16 KB region at 0x10B000–0x10EFFF in `rtos_core.bin` contains the
ARM1136 L1 page table (4096 × 4-byte section descriptors). Key mappings:

| Virtual Address | Physical Address | Size | Flags | Purpose |
|----------------|-----------------|------|-------|---------|
| 0x00000000 | 0x00000000 | 8 MB | AP=2 C=1 B=1 | Kernel code (cached, write-back) |
| 0x04000000 | 0x04000000 | 8 MB | AP=2 C=1 B=1 | Secondary RAM (cached) |
| 0x20000000 | 0x20000000 | 8 MB | AP=3 C=0 B=0 | Peripheral (uncached) |
| 0x24000000 | 0x24000000 | 8 MB | AP=3 C=0 B=0 | Peripheral (uncached) |
| 0x40000000 | 0x40000000 | 128 MB | AP=3 C=1 B=1 | **Main RTOS RAM** (cached, write-back) |
| 0x50000000 | 0x50000000 | 128 MB | AP=3 C=0 B=0 | Peripheral |
| 0x60000000 | 0x60000000 | 128 MB | AP=3 C=1 B=1 | RAM (cached) |
| 0x70000000 | 0x70000000 | 128 MB | AP=3 C=0 B=0 | Peripheral |
| 0x80000000 | 0x80000000 | 1 MB | AP=3 C=0 B=0 | Peripheral |
| 0xD0000000 | 0xD0000000 | 12 MB | AP=3 C=0 B=1 | Peripheral (write-through) |
| 0xE0000000 | 0xE0000000 | 4 MB | AP=3 C=0 B=1 | **Interrupt controller** area |
| 0xE0C00000 | 0xE0C00000 | 4 MB | AP=3 C=0 B=0 | **EPIDC control** |
| 0xF0000000 | 0xF0000000 | 255 MB | AP=3 C=0 B=1 | System registers |
| 0xFFF00000 | L2@0x45404000 | 1 MB | L2 table | Exception vectors (high vectors) |

**All mappings are identity** (VA = PA) except 0xFFF00000 which uses an
L2 page table located in the decompressed CROM area (0x45404000).

## CROM Decompression: Where and When

The CROM decompression does **not** occur in the visible kernel code
(0x0–0x3FFFF). Evidence:

1. No "CROM" string reference in the kernel code region
2. No Huffman-decode or LZ77 patterns in the kernel functions
3. The SWI vector is unused (infinite loop) — no decompression syscall
4. The MiLL loader (0x110000) only copies blocks from ROM to RAM

The decompression most likely occurs in:
- **SoC mask ROM** (on-chip boot ROM, not dumped) — the SoC09S has an
  internal ROM that initializes the system before jumping to the flash-based
  bootloader. It reads the CROM section from flash, decompresses it to RAM
  at 0x45xxxxxx, then hands off to MiLL/BooSt.
- **Or** a small stub at the very start of flash that runs before the
  kernel image is loaded — but we see no evidence of this in the dump.

The boot flow is: SoC ROM → decompress CROM → MiLL (copy kernel ROM→RAM) →
BooSt (CP15/MMU setup) → RTOS init → application start (via thunk table).

## How to Decompress and Analyze in Ghidra

### Step 1: Extract and Decompress

```bash
# Extract CROM section from rtos_core.bin
python3 -c "
with open('rcx_output/rtos_core.bin', 'rb') as f:
    f.seek(0x40000)
    data = f.read(0x10F400 - 0x40000)
with open('rtos_crom.bin', 'wb') as f:
    f.write(data)
"

# Decompress (requires tqdm or a shim)
python3 epson-reversing/uncrom.py rtos_crom.bin
# Produces: rtos_crom.bin.0.bin (514 KB, application code)
#           rtos_crom.bin.1.bin (12 KB, API vector table)
```

### Step 2: Load into Ghidra

1. **Segment 0** (application code):
   - Import `rtos_crom.bin.0.bin` as Raw Binary
   - Language: ARM:LE:32:v6
   - Base address: **0x45430000**
   - This is the main RTOS application — print engine, scanner, cloud print

2. **Segment 1** (API vector table):
   - Import `rtos_crom.bin.1.bin` as Raw Binary
   - Language: ARM:LE:32:v6
   - Base address: needs to be determined (check kernel thunk targets)
   - Contains 35 `LDR PC` entries + dispatcher code

3. **Cross-reference with kernel**:
   - The kernel at base 0x0 calls into CROM via thunks at ~0x166B8
   - The CROM code calls back into the kernel (addresses 0x0–0x3FFFF)
   - Load both in the same Ghidra project for full cross-referencing

### Step 3: Analyze

Key areas to investigate in the decompressed code:
- **0x454304C0–0x45430800**: Initialization and startup code
- **0x4549969C+**: Thunk wrappers that bridge to kernel functions
- **0x45430000+**: Beginning of application — likely dispatcher/scheduler
- The external BL targets at 0x45B0xxxx and 0x45B5xxxx suggest more
  decompressed code exists beyond what these two segments provide —
  possibly decompressed from Block 1 CROM streams or from a larger
  CROM image not captured in this extraction

## Relationship to Known Algorithms

CROM is a **custom Epson format** that combines:

1. **JPEG-style Huffman coding** (identical DHT table format, same
   prefix-free code construction, same 16-count-then-symbols encoding)
2. **LZ77 sliding-window compression** (copy-back with 16-bit offset
   window and 8-bit length, supporting overlapping copies for RLE)
3. **Separate literal pool** (unlike standard LZ77/LZSS which interleaves
   literals and copies, CROM stores all literal data contiguously at the
   end of each segment)

The closest standard algorithm is **LZSS with Huffman-coded control stream**
and a separate literal stream. The JPEG marker framing is purely structural
— there is no DCT, quantization, or image-specific processing.

## References

- [epson-reversing/uncrom.py](https://github.com/abrasive/epson-reversing) — Reference decompressor
- [RTOS_KERNEL.md](RTOS_KERNEL.md) — Kernel analysis, thunk table, boot sequence
- [ARCHITECTURE.md](ARCHITECTURE.md) — Dual-domain SoC architecture
- [FIRMWARE_LAYOUT.md](FIRMWARE_LAYOUT.md) — Firmware layout, extraction details
