# Firmware layout — Epson XP-442 (FWF729TL)

## File Info
- **File**: FWF729TL.rcx
- **Size**: 15,884,579 bytes (15.15 MB)
- **Date**: Sep 24, 2019
- **Firmware Version**: 20.24.RO20J9.FY16 (from RCX header)
- **Architecture**: dual-domain ARM SoC ("SoC09S") — ARM1136JF-S main/print domain (T-Kernel RTOS, Epson-extended) + ARM926EJ-S network domain (Linux). See [ARCHITECTURE.md](ARCHITECTURE.md). The ARM1136 RTOS banner is confirmed in-firmware (`Start ARM1136JF-S ... RTOS`). T-Kernel identity confirmed via strings and SVC table in [CROM_APPLICATION.md](CROM_APPLICATION.md).

## RCX Header (0x0000 - 0x0125)

Magic: `RCX` (52 43 58)

### Sections
- `[A]` - Version/metadata (1="2", 2="11", 13="3", 16="*.RO*.FY16")
- `[B]` - Configuration flags (TRUE/FALSE values, sizes: 10, 400)
- `[D]` - Package info: "FW Package" / "Firmware"
- `[Z_1]` - Partition 1: 7,483,392 bytes (0x723000)
- `[Z_2]` - Partition 2: 8,400,896 bytes (0x803000)
- Z_1 + Z_2 = 15,884,288 bytes (file is 291 bytes larger = header)

## Firmware Structure (Two IPL Blocks)

The firmware contains **two complete firmware images**, each with its own IPL header:

| Block | Content | Size |
|-------|---------|------|
| **Block 0** | ARM1136 RTOS core **+** ARM926 Linux (kernel + 2× SquashFS) — **both domains** | ~7.1 MB |
| **Block 1** | 3× CROM streams: WebConfig web UI + front-panel UI + graphics (ARM1136 main-domain app/resources) | ~8.0 MB |

> **Correction (was "Block 0 = printer, Block 1 = scanner"):** Block 0's segment
> holds *both* execution domains — the ARM1136JF-S RTOS core occupies the first
> ~1.1 MB (before the Linux uImage), followed by the ARM926 Linux image. Block 1
> is **not** a scanner/Linux image: it is three CROM-compressed streams holding the
> ARM1136 main-domain application/resources (web-admin UI, front-panel UI,
> graphics/fonts). Decompressed via `uncrom.py` — see the Block 1 section below and
> [ARCHITECTURE.md](ARCHITECTURE.md).

### Extracted Files (via `extract_firmware.py`)
```
rcx_header          291 B    RCX metadata (INI format)
ipl_0_hdr           4 KB     Block 0 IPL header (0x100 bytes)
ipl_0_seg0          8 KB     Block 0 IPL code (bootloader)
ipl_0_seg1          7.1 MB   RTOS core + Linux uImage + 2x SquashFS
rtos_core.bin       1.1 MB   ARM1136 RTOS core (isolated, 0x0–0x110400)
ipl_1_hdr           4 KB     Block 1 IPL header
ipl_1_seg0          8 KB     Block 1 IPL code
ipl_1_seg1          8.0 MB   3x CROM streams (main-domain app: web UI, panel UI, graphics)
```

### IPL Header Structure (in extracted ipl_0_hdr)
- "EPSON IPL" signature at offset 0x00 (9 bytes)
- Version/magic at 0x09: 01 05 52 4f ("RO")
- Segment record count at 0x0D: 0x006c = 108 bytes (3 records × 0x24 bytes)
- Segment records start at 0x10:

| Record | Offset | f0 | Checksum | Length | SHA-1 |
|--------|--------|----|----------|--------|-------|
| 0 | 0x10 | 1 | 0xd971 | 8,192 B (IPL code) | all zeros (not used) |
| 1 | 0x34 | 2 | 0xd165 | 7,471,104 B (firmware) | f1b1c60e... (verified) |
| 2 | 0x58 | 0 | 0x0000 | 0 (empty) | all zeros |

- Checksum = sum(segment_bytes) & 0xFFFF (verified for both records)
- SHA-1 = SHA-1(segment_data + SHA-1(header[:0x0E])) (verified for record 1)

## Block 0 — ipl_0_seg1 layout

| Offset (in segment) | Content | Details |
|---------------------|---------|---------|
| `0x0` | **ARM1136 RTOS core** | ARM vector table @ `0x0`; `BooSt for SoC09S 1.0.3 (2008/08/01)` @ `0x62`; `Start ARM1136JF-S ... RTOS` @ `0x88`; `EPSON PRINTER ROM` @ `0xf26f`. Uncompressed ARM code (entropy 4.7–5.6), one compressed sub-section ~`0x40000`, then padding to the uImage. |
| `0x110400` (1,115,136) | **uImage kernel** | ARM Linux 2.6.35.8-arm1, 1,041,428 bytes, uncompressed, created 2017-08-02 |
| `0x230000` (2,293,760) | **SquashFS 1** | xz compressed, 426 inodes, 1,180,420 bytes, created 2017-08-02 (rootfs) |
| `0x370000` (3,604,480) | **SquashFS 2** | xz compressed, 35 inodes, 2,852,622 bytes, created 2018-10-03 (network module: `nwsoc`) |

## Block 1 — ipl_1_seg1 (CROM-compressed, main-domain app/resources)

Header: `RO20J9` @ `0x0`, `TXW01K` @ `0x8`. The body is **three concatenated CROM
streams** (each `CROM` magic + JPEG-Huffman/LZ payload), separated by `0xff`
padding. Decompress with `uncrom.py` (feed each from its `CROM` magic). These are
the **ARM1136 main-domain application + resources** — *not* Linux, not a scanner
image, and no filesystem (flat images the RTOS loads).

| CROM stream | seg1 offset | Compressed | Decompressed | Contents |
|-------------|-------------|-----------|--------------|----------|
| A | `0x10` | ~2.48 MB | 5,657,680 B | **WebConfig web-admin app**: HTML/JS/CSS (`HTML/TOP/INDEX.HTML`, `HTML/NET/LAN.HTML`, `HTML/MENT/FIRMUP.HTML`, `SCRIPT/*.JS`, `*.CSS`), ARM app code, admin-password endpoints (`/ADVANCED/ADMIN_PASSWORDSET_*`), Epson Connect/AirPrint/GCP. Bundled OSS: FreeType, zlib, CMU/net-SNMP, Info-ZIP. |
| B | `0x330000` | ~1.68 MB | 4,148,304 B | **Front-panel UI app**: ARM code + localized LCD strings (scan/copy/print/ink/firmware, many languages), embedded TrueType fonts, PNG icon. |
| C | `0x530000` | ~1.27 MB | 2,324,208 B | **Graphics/resource bundle**: numbered PNG icons (`*.PNG`, authored in Photoshop CS5 — XMP metadata present), TrueType fonts, ICC color profile. |

> **Correction — crypto is STANDARD, not proprietary.** Earlier notes reported a
> "modified AES S-Box" and "custom SHA-256 initial hash values" in Block 1. Those
> were **artifacts of scanning the *compressed* stream** plus a constant
> misidentification. Verified against the decompressed streams:
> - AES S-box in stream A @ `0x376520` is **standard** (`63 7c 77 7b …`; index 9 = `0x01`, index 11 = `0x2b` — both standard). The "byte 9 = 2b" claim was an off-by-two miscount.
> - The "custom H values" `982f8a42 91443771 …` are the **SHA-256 round constants K[0..7], byte-swapped** (exact match), not modified init values. The *standard* SHA-256 init-H `6a09e667 …` is also present (stream A @ `0x32e864`).
>
> Conclusion: Block 1 uses stock AES + SHA-256 (normal crypto lib, consistent with HTTPS in the web app). No evidence of proprietary crypto here.

## RCX Signing

Each IPL block is signed with **SHA-1** (not SHA-256):

- **2-byte checksum** — simple sum of body bytes (stored at segment_record + 0x02)
- **20-byte SHA-1 hash** — of body + header hash (stored at segment_record + 0x10)
- Each IPL header contains 3 segment records (0x24 bytes each)
- Header hash = SHA-1 of first 0x0E bytes of IPL header (after "EPSON IPL" signature)

The `CA-SIGNED` string at `0x981dc7` (near PEM/PKCS12 markers) confirms certificate-based signing.

### Verification Results (Block 0)
- Segment 0 checksum: 0xd971 (computed) == 0xd971 (stored) ✓
- Segment 1 checksum: 0xd165 (computed) == 0xd165 (stored) ✓
- Segment 1 SHA-1: f1b1c60e9499e04dc3cd06bce8076e67342557ba (computed) == f1b1c60e... (stored) ✓

### ~~Modified Constants~~ — RETRACTED (crypto is standard)
The earlier "modified AES S-Box / custom SHA-256 H values" finding is **retracted**;
it was a compressed-data artifact + constant misID. See the correction box under
[Block 1](#block-1--ipl_1_seg1-crom-compressed-main-domain-appresources). Block 1
contains stock AES and SHA-256 (the `982f8a42 …` sequence is SHA-256 round
constants K byte-swapped, and standard init-H `6a09e667 …` is also present).

## X.509 Certificates Found
222 potential ASN.1 structures found, including:
- 0x3c54 (1,282 bytes)
- 0x3ce8 (58,624 bytes)
- 0x6ac9 (512 bytes)
- Multiple ~5,552 byte structures (0x9fb0 - 0xa0d0)
- Several ~57,856 byte structures

## Extraction Examples

### 0. One-command reproducible extract (recommended)

```bash
./extract_firmware.sh                       # uses FWF729TL.efu in the repo root
./extract_firmware.sh path/to/other.efu     # or point at any .efu / .rcx
```

Produces exactly the layout the analysis docs reference:

```
rcx_output/                       IPL blocks + decompressed Block 1 CROM streams
  rcx_header, ipl_0_{hdr,seg0,seg1}, ipl_1_{hdr,seg0,seg1}
  block1_streamA_web.bin          WebConfig admin UI (5,657,680 B)
  block1_streamB_panel.bin        front-panel UI     (4,148,304 B)
  block1_streamC_gfx.bin          icons/fonts        (2,324,208 B)
squashfs_rootfs1/                 Linux rootfs (240 files; ~134 /dev nodes elided on macOS)
squashfs_rootfs2/                 network module (nwsoc, epidc.ko, bcm43143)
```

Requires: `python3`, `unsquashfs` (4.5+), and the abrasive/epson-reversing
toolkit submodule at `./epson-reversing/` (or `EPSON_REVERSING=/path`). See
[extract_firmware.py](extract_firmware.py) for the Python entry point (has
`--skip-crom`, `--skip-squashfs`, `--out DIR`, `--toolkit DIR` flags) and
[extract_firmware.sh](extract_firmware.sh) for the wrapper.

The individual steps below still work if you want to reproduce by hand.

### 1. Extract RCX IPL blocks (works without modifications)
```bash
# Extract firmware blocks from .rcx file
python3 epson-reversing/rcx_unpack FWF729TL.rcx rcx_output/

# Results in:
#   rcx_output/rcx_header      - RCX metadata
#   rcx_output/ipl_0_hdr       - Main firmware IPL header (4 KB)
#   rcx_output/ipl_0_seg0      - Main firmware IPL code (8 KB)
#   rcx_output/ipl_0_seg1      - Main firmware (7.1 MB)
#   rcx_output/ipl_1_hdr       - Block 1 IPL header (4 KB)
#   rcx_output/ipl_1_seg0      - Block 1 IPL code (8 KB)
#   rcx_output/ipl_1_seg1      - Block 1 CROM streams (8.0 MB)
```

### 2. Extract firmware from .efu (Epson updater package)
```bash
# .efu is a ZIP containing the .rcx
unzip FWF729TL.efu
# Extracts: FWF729TL.rcx (15,884,579 bytes)
```

### 3. Fix checksums after modifying firmware (works without modifications)
```bash
# Requires: pip3 install tqdm
python3 epson-reversing/rcx_correct FWF729TL.rcx FWF729TL_corrected.rcx

# Fixes:
#   - 2-byte checksums (sum of segment bytes)
#   - 20-byte SHA-1 hashes (segment data + header hash)
```

### 4. Decompress Block 1 CROM streams (requires: pip3 install tqdm)
`uncrom.py` expects the input to **start at a `CROM` magic**, and its `unpack_all`
loop stops at the first `0xff` padding — so each of Block 1's three streams must be
carved from its own offset (`0x10`, `0x330000`, `0x530000`) and run separately.

```bash
# uncrom.py needs tqdm; pip3 install tqdm (or shim it)

# Stream A — WebConfig web-admin UI  (-> *.0.bin = 5,657,680 B)
dd if=rcx_output/ipl_1_seg1 of=streamA.bin bs=1 skip=$((0x10))
python3 epson-reversing/uncrom.py streamA.bin      # streamA.bin.0.bin

# Stream B — front-panel UI app      (-> *.0.bin = 4,148,304 B)
dd if=rcx_output/ipl_1_seg1 of=streamB.bin bs=1 skip=$((0x330000))
python3 epson-reversing/uncrom.py streamB.bin      # streamB.bin.0.bin

# Stream C — graphics / fonts        (-> *.0.bin = 2,324,208 B)
dd if=rcx_output/ipl_1_seg1 of=streamC.bin bs=1 skip=$((0x530000))
python3 epson-reversing/uncrom.py streamC.bin      # streamC.bin.0.bin
```

Pre-extracted copies are checked in (see the Block 1 table above for contents):

| File | Size | SHA-256 |
|------|------|---------|
| `rcx_output/block1_streamA_web.bin` | 5,657,680 B | `29697869bd4ec2e1…1b91d267` |
| `rcx_output/block1_streamB_panel.bin` | 4,148,304 B | `a61ef58628ffac6d…1a8eec4a` |
| `rcx_output/block1_streamC_gfx.bin` | 2,324,208 B | `61ddc09b3ec0d40e…56845f92` |

> Note: `uncrom.py` uses `tqdm` only for a progress bar in `execute_items()`. If you
> can't `pip3 install tqdm`, put a stub `tqdm.py` (`def tqdm(it,*a,**k): return it`)
> on `PYTHONPATH`.

### 5. Flash firmware to printer via USB

On Linux (uses `/dev/usb/lp0` via the kernel `usblp` driver):
```bash
python3 epson-reversing/epsflasher FWF729TL.rcx --only-first-block
```

On macOS (upstream doesn't work — no `/dev/usb/lp*`; use the libusb1 wrapper):
```bash
brew install libusb
pip3 install --user libusb1 tqdm
uv run --script ./epsflasher_mac.py FWF729TL.rcx --only-first-block
```

See [REMOTE_ACCESS.md](notes/REMOTE_ACCESS.md) for the full walk-through (IPL-mode
entry, the ~2–5 min silent-without-`tqdm` write, macOS kernel-driver detach
via libusb, rollback, and troubleshooting).

## Tool Status (tested)

| Tool | Works? | Notes |
|------|--------|-------|
| `rcx_unpack` | Yes | No modifications needed |
| `rcx_correct` | Yes | Requires `pip3 install tqdm` |
| `uncrom.py` | Yes | Requires `pip3 install tqdm` |
| `epsflasher` (upstream) | Linux only | Opens `/dev/usb/lp0`; hard-fails on IPL-mode `vi:NA` reply |
| `epsflasher_mac.py` | Yes (IPL mode) | libusb1 wrapper; accepts `vi:NA`; userspace kernel-driver detach; needs `tqdm` to show progress |
| `dirty4.py` | Yes (via wrapper) | IEEE 1284.4 stack; unmodified — `os` module monkey-patched by the mac wrapper |
| `rcx.py` | Yes | Parses `.rcx` header + block sizes (used by both flashers) |

## Ghidra MCP (interactive disassembly of `nwsoc`)

A Ghidra MCP is wired into this project for interactive RE (decompile, xrefs,
create/rename functions) driven from the assistant. It is a **two-part** system:

- The MCP entry (`.mcp.json`, server `ghidra`) launches the
  [`bethington/ghidra-mcp`](https://github.com/bethington/ghidra-mcp) **bridge**
  via `uv`. The bridge only proxies — it speaks HTTP to a **GhidraMCP plugin
  running inside Ghidra** on `127.0.0.1:8089`.
- So Ghidra itself must be open **with a program loaded**, or every tool returns
  `"No program loaded."` Sanity check: `curl -s http://127.0.0.1:8089/check_connection`.

**Bring-up:**

1. Start Ghidra (12.x); confirm the GhidraMCP plugin's HTTP server is on `:8089`
   (Edit → Tool Options → GhidraMCP HTTP Server).
2. Open the `xp-442` Ghidra project and import/open the target. `nwsoc` imports
   as a clean ELF at image **base `0x8000`** — matching this repo's
   `VA = file_offset + 0x8000` convention, so all documented VAs map 1:1.
   (Raw blobs — `ipl_0_seg1` RTOS core, `block1_streamA_web.bin` — are
   headerless: import as `ARM:LE:32` with the correct base, not auto-detected.)

**Caveat — vtable-dispatched functions are not auto-marked.** Ghidra
auto-analysis does not create function starts for routines reached only via
`blx [rX,#off]` (C++ vtable dispatch), which is exactly the telnet write path:
`PNRSCBaseSetU32` (`0x3214c`), `PLeoNVRamDB` vtable[12] setter (`0x32b18`),
`WriteSetup` serializer (`0xbd750`). `get_function_by_address` returns
"No function found" there until you `create_function` / disassemble at the
address first. This is why the earlier static RE re-segmented functions manually
from the full set of `bl`/`blx` targets.

## Recovery Modes (from epsflasher README)

| Mode | Access | USB ID | Flashing |
|------|--------|--------|----------|
| Standard Recovery | Stop + Color Copy + WiFi + Power | Normal | Full firmware (may crash if flashing entire RCX) |
| IPL Mode | Stop + Color Copy + Info + Power | 04b8:0007 | Single block only (`--only-first-block`) |

IPL Mode detection: responds to `vi` command with `vi:NA`

## Companion Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — dual-domain SoC (ARM1136 RTOS + ARM926 Linux), EPIDC inter-domain link, D4 protocol/channels, COMIF.
- [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md) — PNRSC config registry (category/item IDs), SNMP-availability mapping, TELNET activation-vector trace, runtime-enable findings + open decision.
- [ENPC.md](ENPC.md) — EpsonNet Config wire protocol (UDP 3289, BSUSB, BSCOM), full field catalog decoded from EpsonNet Config V4's shipped templates, ENDU container format. Curated subset of PNRSC — the network-exposed slice of the registry.
- [NETWORK_SERVICES.md](NETWORK_SERVICES.md) — network services inventory, telnet, live device profile.
- [NVRAM_EEPROM_RECON.md](notes/NVRAM_EEPROM_RECON.md) — safe EEPROM recon plan to locate the telnet NVRAM byte via `epson_print_conf`'s `||` command; phase-by-phase, risk-labelled, with rollback per step.
- [NVRAM_ADDRESS_STATIC_RE.md](notes/NVRAM_ADDRESS_STATIC_RE.md) — companion static-RE plan: determine the same NVRAM address analytically from `nwsoc` disassembly, without touching the device. Complementary to NVRAM_EEPROM_RECON.md.
- [ROOTFS.md](ROOTFS.md) — Block 0 SquashFS analysis (rootfs + network module).

## Summary
- Two IPL blocks. Block 0 carries **both** domains (ARM1136 RTOS core + ARM926 Linux); Block 1 is **CROM-compressed** main-domain application/resources.
- RTOS core is uncompressed ARM at `ipl_0_seg1:0x0`; Linux uImage + 2× SquashFS follow.
- Block 1 = 3 CROM streams (WebConfig web UI, front-panel UI, graphics/fonts) — ARM1136 main-domain app/resources, using **standard** AES + SHA-256 (earlier "custom crypto" claim retracted).
- IPL blocks signed with SHA-1 checksums (2-byte sum + 20-byte SHA-1); certificate-based signing (PKCS12/PEM).
- Network stack/services run in `nwsoc` (Linux domain); config in the PNRSC registry, bridged to the main domain via COMIF over D4.
- Existing toolchain available for extraction, modification, re-signing, and flashing.
