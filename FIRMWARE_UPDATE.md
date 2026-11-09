# Firmware update — Program Update Mode

Consolidated reverse-engineering notes for the XP-442 firmware update path.
Covers the host-side updater (`EPFWUPD.exe`), the printer-side PUM ROM
(loader in `ipl_0_seg1`), the D4 credit-accounting gate, and the end-to-end
flash sequence.

---

## 1. Overview

### Entering Program Update Mode

Power off the printer, then hold **STOP + LEFT ARROW + DOWN ARROW + POWER**
for ~3–5 seconds until the LCD reads **"Program Update Mode"** in white text.
The printer re-enumerates on USB as:

| Field | Value |
|-------|-------|
| VID | `04b8` |
| PID | `0007` |
| Manufacturer | `EPSON` |
| Product | `USB2.0 Printer (Hi-speed)` |
| 1284.4 Device ID | `MFG:EPSON;CMD:ESCPL2J,BDC,D4;MDL:MFP-IPL;CLS:PRINTER;` |
| Bulk endpoints | IN `0x85`, OUT `0x04`, interface 0 |

The key differentiator is `MDL:MFP-IPL` — normal mode uses `MFP` or `MFP-????`.

### The two D4 sockets

The PUM ROM exposes exactly two 1284.4 service names (at file offsets
`0xf9cc`/`0xf9d7` in `ipl_0_seg1`):

| Socket name | Socket ID | Purpose |
|-------------|-----------|---------|
| `EPSON-CTRL` | `0x02` | Binary command/response channel |
| `EPSON-DATA` | `0x40` | Bulk firmware payload upload |

No `fwu:*`, `IPL-*`, or `PROGRAM-*` sockets exist. The `fwu:ctrl` socket
(used by some models in normal mode) returns `GetSocketID status=0x0a,
sid=0xff` (unknown) in PUM.

### Two updater paths in EPFWUPD.exe

Epson's Windows updater implements two distinct protocols and selects based
on the target:

| Path | Socket(s) | Command encoding | Used by |
|------|-----------|------------------|---------|
| **Path A — `LO:` (Loader)** | `EPSON-CTRL` + `EPSON-DATA` | Binary `<op:2><len:u16 LE><arg>` | **XP-442 PUM** |
| Path B — `fwup` / IS-Header | `EPSON-IPL` | `fwupx%07X` + ENPC framing | Other models / normal mode |

Path B is irrelevant to XP-442 PUM — the PUM ROM contains zero occurrences
of `fwup`, `EPSON-IPL`, `INVD`, or `#nrd`. It is documented in §8 for
reference only.

### The firmware image

The `.rcx` contains two payload blocks `Z_1` and `Z_2` (sizes 7,483,392 and
8,400,896 bytes). `Z_1` starts with a recognisable header:

```
+0x00..+0x08   "EPSON IPL" (9 ASCII bytes — D4 framing consumed by loader)
+0x09          0x01
+0x0A          0x05
+0x0B, +0x0C   0x52 0x4F  ("RO")
+0x0D          <identity tag byte>
+0x0E, +0x0F   0x6C 0x00  (LE u16, must equal 3 × 0x24 = 0x6C)
+0x10..+0x33   Record 0  (36 bytes, type must == 1)
+0x34..+0x57   Record 1  (36 bytes, type must == 2)
+0x58..+0x7B   Record 2  (36 bytes, type must be 0 or 3)
+0x7C..        Concatenated payloads (size1 ∥ size2 ∥ padding to 0x1000)
```

Each 0x24-byte record:

```
+0x00 (u16 LE)  type       (rec0=1, rec1=2, rec2=0|3)
+0x02 (u16 LE)  checksum   (byte-wise sum mod 0x10000 of that record's payload)
+0x04 (u16 LE)  subtype
+0x08 (u32 LE)  dest_addr  (flash destination)
+0x0C (u32 LE)  size       (payload byte count)
+0x10..+0x23    reserved
```

Two checksums are validated on flush: `sum(payload[0..size1]) == csum1` and
`sum(payload[size1..size1+size2]) == csum2`. Getting one right but not the
other can leave the printer partially written.

---

## 2. Host-Side Protocol — The LO Flash Sequence

### CTRL command encoding

Every control command is a **5-byte binary frame**:

```
<opcode: 2 ASCII bytes> <arglen: u16 little-endian> <arg bytes>
```

This is **not** the ASCII colon form (`vi:FF`, `gm:01`) — that encoding
difference is why prior attempts using the colon form misbehaved.

Confirmed commands (bytes exact):

| Purpose | Wire bytes | ASCII | Notes |
|---------|-----------|-------|-------|
| Status/handshake | `73 74 01 00 01` | `st` arg=`01` | |
| Version query 4 | `76 69 01 00 04` | `vi` arg=`04` | |
| Version query 0 | `76 69 01 00 00` | `vi` arg=`00` | |
| Identity | `76 69 01 00 FF` | `vi` arg=`FF` | Reply contains `ROIPLJ` |
| Version query 7 | `76 69 01 00 07` | `vi` arg=`07` | |
| Flash prep | `66 6C 01 00 01` | `fl` arg=`01` | Reply gated — must be "ready" |
| Go/commit | `67 6D 01 00 01` | `gm` arg=`01` | Reply must arm loader (state=2) |
| Firm-update-end | (5-byte frame) | | Sent after DATA streaming |

The `fl` → `gm` transition is gated: `fl`'s reply is compared against a
readiness token; only if ready does the updater send `gm`.

### Data streaming

1. `OpenChannel("EPSON-DATA")` — gets MTU (typically `0x8000`).
2. Chunk size = `MTU - 6` (6 = D4 packet header).
3. Stream raw `Z_1` bytes, then raw `Z_2` bytes, in chunks. **The final
   chunk of each block sets the EOF/last flag** in the D4 header.
4. No `fwup` header, no per-chunk framing — raw firmware bytes.

### Complete inferred sequence

```
OpenChannel(EPSON-CTRL)
  CTRL: st  01 00 01               (73 74 01 00 01)  → read reply
  CTRL: vi  01 00 04               (76 69 01 00 04)  → read reply
  CTRL: vi  01 00 00               (76 69 01 00 00)  → read reply
  CTRL: vi  01 00 FF               (76 69 01 00 FF)  → reply "ROIPLJ"
  CTRL: vi  01 00 07               (76 69 01 00 07)  → read reply
  CTRL: fl  01 00 01               (66 6C 01 00 01)  → read reply, gate on "ready"
  CTRL: gm  01 00 01               (67 6D 01 00 01)  → read reply, must arm state=2
OpenChannel(EPSON-DATA)
  DATA: stream Z_1 raw, chunks of (MTU-6), last chunk EOF-flagged
  DATA: stream Z_2 raw, same
  CTRL: firm-update-end             → read reply
CloseChannel(EPSON-DATA)
CloseChannel(EPSON-CTRL)
```

### D4 GetSocketID wire format

```
00 00 <len_hi> <len_lo> 01 00 09 <ASCII service name...>
```

Reply: socket ID at byte [8]; result `0x8900` = OK, `0x890a` = unknown name.

---

## 3. Printer-Side — The PUM ROM Loader

### D4 command dispatcher

`PumD4_DispatchGmViFvCommand` @ **`0x0000909c`** is the main command router.
It reads the command buffer:

```
+0x0C = command byte 0 ('g'/'v'/'f')
+0x0D = command byte 1 ('m'/'i'/'v')
+0x0E = frame-type byte (must be 0x01)
+0x0F = reserved (must be 0x00 for gm/vi)
+0x10 = 1-byte argument
+0x11..+0x14 = optional payload (for fv:XX:05:00:03 setter)
```

If `+0x0E != 0x01` or `+0x0F != 0x00`, the reply is `?:NA;\f`.

### `gm:XX` — flash-write mode selector

Sets a mode byte in RAM via `PumState_SetFlashModeByte` @ `0x0000bc28`:

| Command | Mode set | Guard | OK reply |
|---------|----------|-------|----------|
| `gm:01` | **3** (actively flashing) | mode == 0 | `gm:01:OK;` |
| `gm:03` | 5 (finish/release) | none | `gm:03:OK;` |
| `gm:81` | **1** (arm verify) | mode == 0 | `gm:81:OK;` |
| `gm:83` | 5 | none | `gm:83:OK;` |
| `gm:91` | **2** (arm erase) | mode ∈ {0,1} | `gm:91:OK;` |
| `gm:93` | 5 | none | `gm:93:OK;` |

`01`, `81`, `91` are three operations on the same firmware slot — not
different slots. There is no second physical slot exposed at this level.

`gm:01` is **asynchronous** — it returns `OK` immediately after arming
mode 3. Poll `fv:10` or watch the LCD for completion.

### `vi:XX` — version/identity

| Command | Reply |
|---------|-------|
| `vi:FF` | `vi:FF:RO<4 bytes at RAM 0x40109fd0>;\f` (when mode == 0) |
| `vi:XX` (any other) | `vi:NA;\f` |

### `fv:XX` — firmware version / bulk-read / config

| Command | Behaviour |
|---------|-----------|
| `fv:03` | Mode = 5 (release); reply `fv:03:OK;\f` |
| `fv:10` | Status enum from `PumFlash_GetSlotStatusEnum` |
| `fv:11` | 4-hex-char version from hardware read |
| `fv:12` | 0x94 bytes — metadata block (only after mode transitions) |
| `fv:13` | 0x228 bytes — larger metadata block (mode==3 only) |
| `fv:XX:05:00:03:AABBCCDD` | 4-byte config write, sets mode 5 |

### Staging buffer model

Data from `EPSON-DATA` goes to a **RAM staging buffer**, not directly to
flash:

- Base address from `*(DAT_0000db34 + 8)` = `DAT_0000db6c`
- `*DAT_00009584` (RAM `0x4011C170`) tracks bytes received
- At `>= 0x1000` bytes (4 KiB), `PumRcx_ParseHeaderInStagingBuffer` @
  `0x00009888` runs and populates slot metadata
- `*DAT_0000987c` = `size1 + size2 + 0x1000` (expected total)
- The erase → write → verify sequence is kicked off by `gm:01` setting
  mode 3; a background task polls the mode byte and transitions the LCD

### LCD state machine

| LCD text | Meaning |
|----------|---------|
| `EPSON PRINTER` | Power-on splash |
| `Program Update Mode` | PUM entered |
| `IPL Update Mode` | Sub-mode for updating the IPL itself |
| `Recovery Mode` | Normal FW invalid |
| `Now Reading File` | Mode=3 armed, staging buffer accumulating |
| `Now Erasing Rom Data` | Flash erase in progress |
| `Now Comparing Data` | Pre-write checksum verify |
| `Now Writing Rom Data` | Flash program in progress |
| `Now Verifying Rom` | Post-write read-back verify |
| `Finished!` | Complete |
| `Turn off` | Prompt to power-cycle |
| `FILE READ ERR` | `PumState_SetErrorByte(2)` — checksum mismatch |
| `FILE IS NOT FOUND` | Staging buffer under-filled when state machine ran |
| `FATAL ERROR` | Generic loader trap |
| `PREVIOUS UPDATE ERR` | Previous flash session died mid-write |
| `ROM IS NOT WRITABLE` | Flash WEL/lock bit test failed |
| `CONTACT OUR SUPPORT` | Terminal failure (paired with FATAL/PREVIOUS) |

---

## 4. Data Channel — Credit Accounting and the Gate

### The credit counter: `state[+0xea]`

The printer's per-channel RX credit counter for `EPSON-DATA` (socket `0x40`)
tracks how many more Data packets it will accept. It starts at **zero** on
every `OpenChannel`:

```
0xaac4:  strh r8, [r4, #0xea]    ; r8=0
```

The **only** code that raises it above zero is `FUN_0000ad84`
(CreditRequest handler) at `0xaee8`:

```
state[0xea] += granted
```

When credit hits zero, `FUN_0000b1c8` returns error code 1, which triggers
`FUN_0000a060(state, 0x40, 0x81)` — emitting a `0x81` "No credit" error
**and hard-resetting both channels** (see §4.3).

### The credit-grant gate: `state[+0x1574]`

Credit grants are capped by `FUN_00009e34`, which reads a flag byte at
`state + 0x1574` (via `DAT_00009884`):

```c
switch (state[0x1574]) {
    case 0: return 0;                          // closed — grants nothing
    case 1: return MTU;                        // one-shot: grant MTU-6
    case 2:                                    // streaming
        left = *DAT_0000987c - *DAT_00009584;  // bytes still needed
        return min(left, MTU-6);
    default: return 0;
}
```

**Exhaustive access map for `state[+0x1574]`:**

| Address | Kind | Value | Function |
|---------|------|-------|----------|
| `0x8d7c` | store | **0** | `PumD4_InitProtocolStateStruct` |
| `0x981c` | store | **0** | `FUN_000097ac` (EOM cleanup) |
| `0x9e34` | load | (read) | `FUN_00009e34` (credit-grant gate) |
| `0x9b80` | load | (read) | `PumD4_HandlePayloadArrival` |
| `0x9c64` | store | **2** (conditional) | Same, gated on flag already being 1 |

**No code in the loader writes 1 to this flag.** The only non-zero write
(value 2) is gated on the flag already being 1, which never happens.
The flag=1 and flag=2 code paths are **dead code** in this ROM as shipped.

### The hard reset on credit exhaustion

`FUN_0000a060` is catastrophic — on any credit error it:

1. Emits a `0x7f` error frame with the error code
2. Closes **both** `EPSON-CTRL` and `EPSON-DATA`
3. Zeros ALL D4 counters for all three sockets
4. Sets "need Init handshake" flags

The `state[+0xd6] |= 0x20` latch stays set for the rest of the session,
silently dropping all subsequent packets. **There is no graceful recovery**
short of restarting the entire 1284.4 handshake.

### Packet arrival flow with flag=0 (the shipped state)

```
DATA packet arrives (SSID=0x40)
  → 0x9b20 (outer packet handler)
  → validator OK
  → state[+0x1574] = 0
  → cmp #1 → not equal → 0x9c30
  → cmp #2 → not equal → 0x9c3c
  → PumState_SetErrorByte(2)   ← LCD "FILE READ ERR"
  → state[+0xd6] |= 0x20      ← latch — all further packets silently dropped
```

No wire error is emitted. The symptom is "silence on the wire, plus LCD
change." This is NOT the `0x81` credit error — it's a separate,
earlier rejection.

### The CreditRequest packet format issue

dirty4's `CreditRequest` sends only 4 payload bytes (`>BBH`); the ROM
expects 7 — two 16-bit amounts (min_credit, max_credit):

Correct wire format for a streaming CreditRequest on socket `0x40`:

```
00 00 00 0d 01 00 04 40 40 ff ff 01 00
```

(PSID=0, SSID=0, length=0x000d, credit=1, control=0, cmd=0x04,
reply_psid=0x40, target=0x40, min=0xffff, max=0x0100)

### Validator error taxonomy

| Condition | Error code |
|-----------|------------|
| PSID ∉ {0, 2, 0x40} | 0x80 "Malformed" |
| SSID = 0, length ≤ 5 | 0x80 "Malformed" |
| SSID = 0, length > 0x40 | 0x83 "Packet too big" |
| SSID = {2,0x40}, channel closed | 0x84 "Channel not open" |
| SSID = {2,0x40}, length ≤ 5 | 0x80 "Malformed" |
| SSID = {2,0x40}, length > MTU | 0x83 "Packet too big" |
| Credit would overflow TX counter | 0x86 "Credit overflow" |

---

## 5. The Open Question — Does the Binary Handshake Arm the Gate?

The critical hypothesis: **the binary `fl`/`gm` handshake that the real
Epson updater sends (§2) may be what sets `state[+0x1574]` to 1** — the
"external nudge" the DATA path needs. Prior attempts used the wrong command
encoding (ASCII colon form instead of binary frames), so this was never
tested.

If so, the full sequence works naturally:
1. Binary `st`/`vi`/`fl`/`gm` on CTRL arms the gate
2. `OpenChannel(EPSON-DATA)` returns MTU `0x8000`, initial credit 0
3. `CreditRequest` now grants (flag=1 → MTU-6 per request)
4. First 4 KiB triggers header parse → `*DAT_0000987c` set
5. Flag promotes to 2 → streaming grants for remaining bytes
6. Data streams to completion

If the binary handshake does NOT arm the gate, the alternatives are:
- **UART root shell** — write `1` to `state+0x1574` at address
  `0x5013D574` before the transfer
- **Patch the loader** — modify `PumD4_InitProtocolStateStruct` to
  initialise the flag to 1 (12 bytes of ARM)
- **Find a different transport** — raw bulk-OUT streaming, PJL, or an
  undiscovered socket

The `epsflasher_mac.py` tool implements the binary LO sequence and is
built to test this on hardware. Run with:

```
uv run --script ./epsflasher_mac.py FWF729TL_remote.rcx --only-first-block -d
```

---

## 6. Upstream epsflasher — Why It Was Wrong

The upstream `epsflasher` (for XP-240) used this sequence:

```
vi:04 → wait vi:04:OK       (XP-442 returns vi:NA; — vi:04 is undefined)
fl:01 → wait fl:01:OK       (XP-442 has no 'fl' in ASCII colon form)
gm:01 → wait gm:01:OK       (sent BEFORE data — must come AFTER)
raw firmware bytes           (sent AFTER gm:01 — XP-442 needs it BEFORE)
```

Every assumption was wrong for XP-442:
- No `fl` verb exists in the colon-form dispatcher
- Order is reversed: data-then-command (colon form) or handshake-then-data
  (binary form)
- No inline data path from `EPSON-CTRL`; a separate `EPSON-DATA` socket
- The colon-form dispatcher is a different code path from the binary frame
  handler
- Checksums are enforced at header-parse time

---

## 7. Ghidra Address Book

### Named functions (loader.bin / rtos_core.bin)

| Address | Name | Purpose |
|---------|------|---------|
| `0x0000909c` | `PumD4_DispatchGmViFvCommand` | Main D4 ASCII command router |
| `0x00009888` | `PumRcx_ParseHeaderInStagingBuffer` | RCX header parse at 4 KiB threshold |
| `0x00009a08` | `PumD4_HandleEpsonDataChannelRx` | DATA socket RX — credit consume + header parse |
| `0x000097ac` | `PumD4_HandleEpsonDataChannelEom` | DATA end-of-message — cleanup, `state[+0x1574]=0` |
| `0x000096f8` | *(D4 RX — CTRL socket 0x02)* | Dispatches to `PumD4_DispatchGmViFvCommand` |
| `0x00009a9c` | *(D4 control-plane dispatch)* | Routes to command handlers |
| `0x00009b20` | `PumD4_HandlePayloadArrival` | Outer packet handler — flag check, SSID routing |
| `0x0000ad84` | `PumD4_HandleCreditRequest` | CreditRequest — the only path that increments `state[+0xea]` |
| `0x0000ad20` | `PumD4_ComputeCreditGrantForSocket` | Grant computation capped by flag gate |
| `0x00009e34` | `PumD4_GetCreditGrantGate` | Reads `state[+0x1574]` → returns 0 / MTU / bytes_left |
| `0x0000a968` | `PumD4_ClassifyCreditRequestAmounts` | Classifies (min, max) pair from CreditRequest |
| `0x0000a060` | `PumD4_EmitErrorAndResetState` | Error reply + hard reset of all D4 state |
| `0x0000b4cc` | `PumD4_ValidateIncomingHeader` | Incoming packet validator (PSID/SSID/length/credit) |
| `0x0000b1c8` | *(Credit consume)* | Per-channel RX/TX credit decrement gate |
| `0x0000bc10` | `PumState_SetErrorByte` | Sets error byte → LCD error messages |
| `0x0000bc04` | `PumState_GetErrorByte` | Reads error byte |
| `0x0000bc28` | `PumState_SetFlashModeByte` | Sets mode byte polled by flash state machine |
| `0x0000bc1c` | `PumState_GetFlashModeByte` | Reads mode byte |
| `0x0000d19c` | `PumFlash_GetSlotStatusEnum` | Classifies slot firmware → enum 0..5 |
| `0x0000d334` | `PumFlash_SetSlotBaseAddress` | Sets staging slot base |
| `0x0000d3e8` | `PumFlash_VerifySlotChecksum` | Byte-sum verify; header byte 0==0x88 skips (recovery) |
| `0x0000d944` | `PumFlash_InitUpdateSubsystem` | Top-level PUM init |
| `0x00008d70` | `PumD4_InitProtocolStateStruct` | D4 state init; zeroes `state[+0x1574]` |
| `0x0000ea28` | `Rtos_MemcpyForwardBytes` | 4-way-unrolled memcpy |
| `0x0000dc28` | `Rtos_MemcmpReturningMatchLen` | Byte-wise memcmp returning match count |

### Ghidra bookmarks (EPFWUPD.exe, imagebase 0x400000)

| Address | Purpose |
|---------|---------|
| `0x0042070b` | Send `st` |
| `0x00420b7e` | Send `vi FF` (siblings: vi 04/00/07) |
| `0x00421191` | OpenChannel(EPSON-CTRL) |
| `0x0042123e` | Send `fl` then `gm`, reply gating |
| `0x00421677` | OpenChannel(EPSON-DATA) |
| `0x004216de` | Stream `Z_n` over EPSON-DATA |
| `0x00421565` | Firm-update-end |
| `0x0041d74c` | D4 GetSocketID packet builder/parser |

### Key RAM addresses and state offsets

| Address / Offset | Meaning |
|-----------------|---------|
| `0x5013C000` | D4 state-buffer base (`*DAT_0000957c`) |
| `0x5013D574` | `state+0x1574` — the credit-grant gate flag byte |
| `0x4011C170` | Bytes-received counter (`*DAT_00009584`) |
| `0x4011C16C` | Expected-total counter (`*DAT_0000987c`) |
| `state+0x84` | Jump-table index — selects next-dispatch handler |
| `state+0xd6` | Validator/RX error latch (bit 0x20 = stop processing) |
| `state+0xd7` bit 0 | Channel 0x40 open |
| `state+0xd7` bit 1 | Channel 0x02 open |
| `state+0xdc` | Ch 0 RX credit |
| `state+0xde` | Ch 0 TX credit |
| `state+0xea` | **Ch 0x40 RX credit — the counter that stalls at 0** |
| `state+0xec` | Ch 0x40 TX credit |
| `state+0xf0` | Ch 0x40 MTU |
| `state+0xf8` | Ch 0x02 RX credit |
| `state+0xfa` | Ch 0x02 TX credit |
| `state+0xfe` | Ch 0x02 MTU |
| `state+0x1574` | Credit-grant gate (0=closed, 1=one-shot, 2=streaming) |
| `state+0x1576` | Overflow-tail size (ushort) |

### ROM string addresses

| File offset | RAM address | Content |
|-------------|-------------|---------|
| `0xf26f` | | `EPSON PRINTER ROM` |
| `0xf286` | | `EPSON PRINTER` (LCD splash) |
| `0xf2a9` | | `Program Update Mode` |
| `0xf311`–`0xf39b` | | LCD progress messages |
| `0xf46a`–`0xf4dd` | | Error messages (FILE READ ERR, etc.) |
| `0xf8ec`–`0xf9c5` | | Protocol reply templates |
| `0xf9cc` | `0x4010a0b4` | `EPSON-DATA` |
| `0xf9d7` | `0x4010a0bf` | `EPSON-CTRL` |
| `0xf9e5` | `0x4010a0cd` | 1284.4 device-ID string |
| | `0x4012c000` | LCD line RAM buffer base (scratch, not a string) |
| | `0x4012c66c` | `fv:12` return buffer (0x87 bytes) |
| | `0x4012d2dc` | Slot 0 header metadata |

---

## 8. Path B — fwup / IS-Header (Reference Only)

Not used by XP-442 PUM. Over a single D4 channel (`EPSON-IPL`, or an
ENPC/network socket):

1. 12-byte IS-Header: `sprintf("fwupx%07X", totalSize)` → ASCII `fwup`
   tag + hex size
2. Stream payload in MTU-sized chunks; ENPC variant adds 20-byte header
   per chunk with 12-byte ack
3. 64-byte IS reply for completion: `fwup` = ok, `INVD`/`UNKN` = reject,
   `#nrd` token + disposition, `#err` = error

Ghidra (EPFWUPD.exe):
- `0x0041eba3` / `0x0041e6b9` — fwup over D4
- `0x0042e7b4` — fwup over ENPC
- `0x0046197e` — IS-Header builder
- `0x00461ad0` — IS reply parser
- `0x004191ed` — job descriptor (socket `EPSON-IPL`, class `"Firmware"`)

---

## 9. What Was Disproven

| Belief | Reality |
|--------|---------|
| epsflasher's `vi:04 → fl:01 → gm:01 → data` sequence works | `fl` and `vi:04` don't exist in this ROM's colon-form dispatcher; order is wrong |
| `gm:01`/`81`/`91` are different slots | They're three operations on the same slot |
| The colon-form ASCII dispatcher is the only command interface | The binary `<op><len><arg>` format is the real protocol |
| PUM never grants credit | `FUN_0000ad84` grants credit — but only when `state[+0x1574]` is armed |
| `ensure_credit = lambda: None` is safe | It prevents the only mechanism for raising `state[+0xea]` above 0, causing immediate `0x81` rejection |
| `initial_credit=0` means the printer won't accept data | It means CreditRequest is mandatory — the printer demands explicit credit negotiation |
| `max_credit=0xffff` is a lie | It's the real overflow cap, not an initial grant |
| `fwu:ctrl` / `EPSON-IPL` are available in PUM | They don't exist in the PUM ROM — those are for other models/modes |
| The `0x80` error is from the data primer packet | It's from the CreditRequest — dirty4's packet was 2 bytes too short |
