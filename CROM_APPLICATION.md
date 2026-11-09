# CROM Application Code — High-Level Map

Analysis of the decompressed CROM application from `rtos_core.bin`
(offsets 0x40000–0x7C4B2). Decompressed with `uncrom.py`, producing
two segments:

- **Segment 0**: 514,144 bytes — ARM application code at base **0x45430000**
- **Segment 1**: 12,236 bytes — API vector table (35 dispatch thunks)

Decompressed files: `rcx_output/rtos_crom.bin.0.bin`, `rcx_output/rtos_crom.bin.1.bin`

## Loading into Ghidra

```bash
# Extract and decompress (from the epson/ directory)
python3 -c "
with open('rcx_output/rtos_core.bin', 'rb') as f:
    f.seek(0x40000)
    data = f.read(0x10F400 - 0x40000)
with open('rcx_output/rtos_crom.bin', 'wb') as f:
    f.write(data)
"

# Decompress (needs tqdm or a shim — extract_firmware.py handles this)
python3 epson-reversing/uncrom.py rcx_output/rtos_crom.bin
# Output: rtos_crom.bin.0.bin (514 KB), rtos_crom.bin.1.bin (12 KB)
```

Import into Ghidra project `xp-442`:
1. **Segment 0**: Raw Binary, ARM:LE:32:v6, base **0x45430000**
2. **Segment 1**: Raw Binary, ARM:LE:32:v6, base TBD (match kernel thunk targets)
3. Load alongside `rtos_core.bin` (base 0x0) for cross-referencing

## Key Discovery: Kernel RAM Copy at 0x80000000

The MiLL loader copies the kernel code from flash (0x0–0x3FFFF) to RAM at
**0x80000000**. The CROM application calls kernel functions via their RAM
addresses (0x80000xxx), not their flash addresses (0x0xxxx). This means:

- Kernel function at flash 0x634 → RAM 0x80000634
- Kernel function at flash 0x10ad0 → RAM 0x80010ad0

The thunk table at file offset 0x6969C contains 17 kernel callbacks and
36 RTOS RAM service calls, all using 0x80000xxx addresses.

## Segment 0: Subsystem Map

The 514 KB segment is predominantly ARM code (106 4KB blocks with >5 BL
instructions out of 107 total). Ghidra should identify ~2000+ functions.

```
0x45430000 ┌──────────────────────────────────────────┐
           │ 1. Core Application Engine (64 KB)        │
           │    Main loop, state management, dispatch  │
           │    Top fn: 0x45431694 (215 calls)         │
           │    Top fn: 0x45430E18 (109 calls)         │
0x45440000 ├──────────────────────────────────────────┤
           │ 2. Motor/Carriage Control (64 KB)         │
           │    Mechanical actuator control            │
           │    0x4544BE00 (115 calls) — mem alloc     │
0x45450000 ├──────────────────────────────────────────┤
           │ 3. Print/Copy Engine (112 KB)             │
           │    Print job processing, page control     │
           │    0x45450B28 (144 calls)                 │
0x4546C000 ├──────────────────────────────────────────┤
           │ 4. Scanner API (12 KB)                    │
           │    scnr_api2tbl.c — dispatch table        │
           │    scnrapi2.c — API implementation        │
0x4546F000 ├──────────────────────────────────────────┤
           │ 5. Scan Engine Driver (116 KB) — segd_*   │
           │    segd_front.c — front-end               │
           │    segd_subs.c — subroutines              │
           │    segd_top.c — top-level control         │
           │    segdadj_crppg.c — calibration          │
           │    segddiag.c — diagnostics               │
           │    segdendurance.c — endurance testing     │
           │    segdheat.c — thermal management        │
           │    segdmeasure_crppg.c — measurements     │
           │    segdscan.c — scan operations            │
0x4548C000 ├──────────────────────────────────────────┤
           │ 6. Additional Application Code (52 KB)    │
0x45499000 ├──────────────────────────────────────────┤
           │ 7. Thunk Table (~4 KB)                    │
           │    53 LDR PC,[PC,#-4] thunks:             │
           │    - 17 kernel (→ 0x0xxxx via 0x80000xxx) │
           │    - 36 RTOS services (→ 0x80000xxx)      │
0x4549A000 ├──────────────────────────────────────────┤
           │ 8. Data Region (84 KB)                    │
           │    State machine strings (15 states)      │
           │    IP networking templates                │
           │    Diagnostic menus                       │
           │    ASCII lookup table                     │
0x454AD860 └──────────────────────────────────────────┘
```

### Source File References

Source file names embedded in the binary reveal the original codebase:

| Offset | File | Subsystem |
|--------|------|-----------|
| 0x3DABC | `scnr_api2tbl.c` | Scanner API dispatch table |
| 0x3E768 | `scnrapi2.c` | Scanner API implementation |
| 0x472DC | `segd_front.c` | Scan engine front-end |
| 0x4A8DC | `segd_subs.c` | Scan engine subroutines |
| 0x4D12C–0x4E99C | `segd_top.c` (×4) | Scan engine top-level (large) |
| 0x4FE38 | `segdadj_crppg.c` | Carriage/page adjustment |
| 0x53350 | `segddiag.c` | Scanner diagnostics |
| 0x55AA8 | `segdendurance.c` | Endurance testing |
| 0x56180 | `segdheat.c` | Thermal management |
| 0x56884 | `segdmeasure_crppg.c` | Scanner measurements |
| 0x579A0–0x588A4 | `segdscan.c` (×3) | Scan operations (large) |

The naming convention `segd_*` = "scan engine driver".

### Printer State Machine

15 states defined at 0x456C4:

```
STATE_IDLE              → Ready / standby
STATE_BUSY_PCSCAN       → Busy: PC-initiated scan
STATE_BUSY_STANDALONE   → Busy: panel-initiated operation
STATE_WARMUP_PCSCAN     → Warming up for PC scan
STATE_WARMUP_STANDALONE → Warming up for standalone
STATE_ENERGY_SAVE       → Energy saving / sleep
STATE_OPEN_ERROR        → Cover/door open
STATE_PAPER_JAM_ERROR   → Paper jam
STATE_FATAL_ERROR       → Fatal hardware error
STATE_LOCK_ERROR        → Lock error (unrecoverable)
STATE_DCU_FATAL_ERROR   → Document copy unit fatal
STATE_BUSY_ERRRECOVERY  → Busy recovering from error
STATE_PAPER_EMPTY_ERROR → Paper tray empty
STATE_ADF_PAPER_SIZE_ERR → ADF paper size mismatch
STATE_ADF_FATAL_ERROR   → ADF fatal error
```

### Diagnostic / Developer Menu

Strings at 0x7D633+ reveal a built-in service menu:

```
Select Develop Menu
1. SCN FT Mode / SCN+ADF FT Mode
2. SCN Auto Check
3. SCN Endurance / ADF Endurance Mode
4. Color All / Mono All
5. Color 10mm / Mono 10mm
1. Skip Scan
1. Single Face Paper Feed
2. Double Face Paper Feed
3. LD Up/Down
* Show Parameter
* NoMechSetup Mode
```

UI interactions: `Push[OK]Button`, `Start[OK]Button`, `Stop[STOP]Button`,
`Yes:UP/No:DOWN`, `Run:OK [UP/DOWN]`, `Retry: OK SW`, `Finish: Stop SW`.

### Top Internal Functions

| Address | Calls | Size | Description |
|---------|-------|------|-------------|
| 0x45431694 | 215 | ~80B | State/parameter marshalling (4 callees) |
| 0x45450B28 | 144 | ~88B | Printer operation dispatch (2 callees) |
| 0x454705D4 | 132 | ~28B | Byte accessor — read/modify/write a config byte |
| 0x45474F04 | 126 | ~736B | Large routine, no external calls — data processing |
| 0x4544BE00 | 115 | ~68B | Memory pool request (calls 0x45C5xxxx allocator) |
| 0x45430E18 | 109 | ~216B | Complex dispatcher (9 internal callees) |
| 0x4543BB5C | 104 | ~104B | RTOS service wrapper (1 call to 0x45B5xxxx) |
| 0x4547049C | 97 | ~48B | Structure copy — 5 fields from src to dest |

## Segment 1: API Vector Table

35 `LDR PC, [PC, #-4]` thunks dispatching to implementations across
the address space. This is the **RTOS API entry point table** that the
kernel's thunk table at ~0x166B8 (flash) jumps through.

| Vector | Target | Module |
|--------|--------|--------|
| 0x00 | 0x45BF4508 | RTOS Core Services |
| 0x01 | 0x45458360 | Segment 0 (print engine) |
| 0x02–0x04 | 0x45C5E4xx | Resource Management |
| 0x05 | 0x454556D0 | Segment 0 |
| 0x06 | 0x45458A98 | Segment 0 |
| 0x07–0x08 | 0x45C52Exx | Resource Management |
| 0x09 | 0x45DB8990 | Peripheral Drivers |
| 0x0A | 0x45B51FD8 | RTOS Core Services |
| 0x0B–0x0D | 0x4545xxxx | Segment 0 |
| 0x0E–0x0F | 0x45E46xxx | I/O Services |
| 0x10–0x11 | 0x4545xxxx | Segment 0 |
| 0x12–0x14 | 0x45B5xxxx | RTOS Core Services |
| 0x15–0x1A | 0x4545xxxx | Segment 0 |
| 0x1B | 0x45BA77A4 | RTOS Core Services |
| 0x1C | 0x454558AC | Segment 0 |
| 0x1D | 0x45C9421C | Resource Management |
| 0x1E | 0x45457BCC | Segment 0 |
| 0x1F | 0x4545958C | Segment 0 |
| 0x20 | 0x45C94100 | Resource Management |
| 0x21 | 0x45B63224 | RTOS Core Services |
| 0x22 | 0x45C86728 | Resource Management |

After the 35 vectors, segment 1 contains ~12 KB of additional ARM code
(138 BL instructions) — likely a small dispatcher/initialization stub.

## RTOS Identity: T-Kernel OS

The RTOS is confirmed as **T-Kernel** — a TRON-family real-time OS
originally developed by the T-Engine Forum (now TRON Forum). Key evidence:

1. **Explicit strings**: `T-Kernel/OS SVC in execution`, `T-Kernel/DS`,
   `Software Interrupt (SWI) #6 (T-Kernel/OS & Extended SVC)`
2. **SWI dispatch table** at 0x45E43054–0x45E4308C with standard T-Kernel
   SWI numbers:
   - SWI #4 = T-Monitor
   - SWI #6 = T-Kernel/OS & Extended SVC (main system call gate)
   - SWI #7 = `tk_ret_int` (return from interrupt handler)
   - SWI #8 = Dispatcher
   - SWI #9 = T-Kernel/DS (debug support)
   - SWI #12 = Exception Manager
3. **Configuration table** at 0x45F7F068 with standard T-Kernel parameters:
   `TMaxTskId`, `TMaxSemId`, `TMaxFlgId`, `TMaxMbxId`, `TMaxMtxId`,
   `TMaxMbfId`, `TMaxPorId`, `TMaxMpfId`, `TMaxMplId`, `TMaxCycId`,
   `TMaxAlmId`, `TMaxSsyId`, `TKernelStkSz`, `TKernelHeapSz`
4. **Object type names**: `task`, `semaphore`, `eventflag`, `mailbox`,
   `messagebuffer`, `memorypool`, `cyclichandler`, `subsystem`,
   `resource_group`, `timer`
5. **Diagnostic output**: `Task independent part`, `Quasi-task part`,
   `Task part`, `Task dispatching`, `Scheduling Unit ID`, `Task ID`,
   `Task Name`

### T-Kernel SVC API (96 wrappers)

The SVC wrapper table at 0x45E442D4–0x45E4549C contains 93 valid SVC
stubs. Each follows the pattern: `PUSH {r4,lr}` / setup / `LDR r1, [PC, #8]`
(function code) / `MOV r0, sp` / `BL svc6_wrapper` / `POP {r4,pc}`.

**OS Kernel calls** (44 wrappers, function code < 0x80000000):

| Code | T-Kernel Name | Group |
|------|---------------|-------|
| 0x00010228 | `tk_del_tsk` | Task management |
| 0x00050128 | `tk_cre_mtx` | Mutex |
| 0x000D0228 | `tk_ret_int` | Interrupt management |
| 0x000E0128 | `tk_ref_sys` | System management |
| 0x0069–0x0076 | Custom Epson extensions | 14 custom groups |
| 0x01010109 | `tk_cre_tsk` | Task creation |

Epson extends T-Kernel with **custom groups 0x11–0x76** (at least 14
additional object types beyond the standard T-Kernel set). Groups
0x69–0x76 have the most wrappers — these likely include scanner control,
print engine control, and panel I/O primitives.

**Extended SVC / Subsystem calls** (49 wrappers, function code ≥ 0x80000000):

Subsystem IDs range from 0x01 to 0x5B, covering device drivers,
file systems, and Epson-specific services. The extended SVCs handle I/O
operations, device management, and peripheral access.

### Task Names Found

- `ScannerTask` — scanner control task (referenced with status query)
- `FILEW_TASK` — file write task (DSM write operations)
- `ENCODE_TASK` — encoding task (DSM write, event flag operations)

### Note on SWI in rtos_core.bin

The kernel stub (rtos_core.bin 0x0–0x3FFFF) has SWI vector → infinite loop
because the CROM-decompressed T-Kernel overwrites the exception vectors
after being loaded to RAM. The real SWI handler is in the T-Kernel
kernel proper within `block1_streamA_web.bin`.

## External Module Map

Segment 0 makes 1,754 external calls to 275 unique targets across 5
modules. These modules exist somewhere in the full firmware but are not
in the two CROM segments we decompressed from rtos_core.bin's CROM area.

```
┌─────────────────────────────────────────────────────────────────┐
│                    CROM Application Memory Map                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  0x45430000  ┌─────────────────────┐ rtos_crom.bin.0.bin         │
│              │  Segment 0 (514 KB) │ Print engine + scanner drv  │
│  0x454AD860  └─────────────────────┘                            │
│                                                                  │
│  0x45900000  ┌─────────────────────┐ block1_streamA_web.bin      │
│              │ ▒ Code (3.5 MB)     │ RTOS middleware             │
│              │ │ Module A (637 c.) │   RTOS core: sched, sema   │
│              │ │ Module B (471 c.) │   Resource mgmt: alloc     │
│              │ │ Module D (541 c.) │   I/O / HAL: mutex, irq    │
│  0x45C70000  │ ▒ Data (1.9 MB)    │                             │
│              │ │ Module C          │   NOT code — data tables:  │
│              │ │   Paper/media cfg │     IPP media names, JPEG  │
│              │ │   Dispatch table  │     20K cmd→handler entries │
│              │ │   HTML templates  │     WebConfig fragments     │
│              │ │   Mfg/diag strs   │     "Charging Now!", etc.  │
│              │ │   License text    │     GPL/Apache boilerplate  │
│  0x45E65450  └─────────────────────┘                            │
│                                                                  │
│  0x46000000  ┌─────────────────────┐ block1_streamB_panel.bin    │
│              │  Panel UI (4.1 MB)  │ C++ UIWidget framework      │
│  ~0x463F4000 └─────────────────────┘                            │
│                                                                  │
│  0x80000000  ┌─────────────────────┐ kernel RAM copy             │
│              │  Kernel (256 KB)    │ Flash 0x0 copied by MiLL    │
│  0x80040000  └─────────────────────┘                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Module A: RTOS Core Services (0x45B0–0x45BA)

The most-called external module (637 calls, 79 targets). Located in
`block1_streamA_web.bin` at file offset ~0x2D504–0xD1B34 (base 0x45AD8000).

Two addresses dominate call targets:

- **0x45B5C8A0** — 250 calls. **NOT a function entry** — mid-function
  address inside the function at 0x45B5C828. This is a **table lookup**
  routine that indexes into arrays via register-scaled addressing
  (`LDR r5, [r9, r4, LSL#2]`), compares values, and dispatches.

- **0x45B5C7E0** — 197 calls. Also mid-function (inside 0x45B5C728).
  A **multi-array search** function that iterates through 5+ arrays
  with different element sizes (LDRH variants), comparing against
  search keys in r4/r5, calling handlers on match via conditional BL.

These are **dispatcher/lookup functions**, not semaphore primitives as
previously hypothesised. They form the core event routing mechanism
for the print engine — mapping event codes to handler functions.

The actual RTOS synchronisation primitives are in Module D (see below)
which contains the T-Kernel SVC stubs.

### Module B: System Controller + Services (0x45C5–0x45CF)

**676 KB, 2877 functions.** Much larger than the 134-target / 471-call
view from Segment 0 suggests — the rest is called internally or from
other modules. Located in `block1_streamA_web.bin` at file offset
0x253DD0–0x2FCF38 (base 0x45A00000).

Originally classified as "resource management" but is actually the
**system controller layer** (`sysc_*`) that orchestrates jobs, manages
memory pools, and implements the web/print services stack.

#### Source Files

12 `sysc_*` source files are referenced at 0x45C79728–0x45C800F0:

| File | Purpose |
|------|---------|
| `sysc_api.c` | System controller public API |
| `sysc_main.c` | Main scheduler/dispatcher |
| `sysc_mem_ctrl.c` | Memory pool management |
| `sysc_queue.c` | Job/message queuing |
| `sysc_job_exctrl.c` | Job execution control |
| `sysc_job_cancelctrl.c` | Job cancellation |
| `sysc_job_holdctrl.c` | Job hold/resume |
| `sysc_job_stopctrl.c` | Job stop |
| `sysc_job_errctrl.c` | Job error handling |
| `sysc_hrnss_ctrl.c` | Harness control (hardware coordination) |
| `sysc_intr_ctrl.c` | Interrupt control |
| `sysc_receipt_id_ctrl.c` | Receipt/job ID management |

#### Sub-Regions

| Range | Size | Functions | Content |
|-------|------|-----------|---------|
| 0x45C53–0x45C65 | 72 KB | 215 | **Memory pool manager** — alloc (0x45C5DA48, 59 calls), post-alloc register (0x45C5EE18, 60 calls), pool-sized alloc variants. Pool headers at offsets -0x16 (index), -0x18 (free flag), -0x12 (type), -0x1C (size), -0x10 (chain ptr) |
| 0x45C65–0x45C72 | 52 KB | 310 | **USB / PictBridge / DPS** — DCIM, DPS paths, USB bulk transfer. Not called from Segment 0 |
| 0x45C72–0x45C80 | 52 KB | 425 | **System controller core** — all 12 `sysc_*.c` files, command interpreter ("F START !", "Invalid Command"), scanner name/version reporting |
| 0x45C80–0x45C95 | 84 KB | 247 | **Print format handler** — PM-790PT headers, font tables (Courier), receipt IDs, print template parser (`[HEADER]`, `[PAGE]`) |
| 0x45C95–0x45CA5 | 64 KB | 281 | **SOAP/XML web services** — xmlns, myfaultDetails, Server Error, HTTP/HTTPS handlers, URI processing |
| 0x45CA5–0x45CC0 | 108 KB | 240 | **IPP/WSD/PTP protocol handlers** — UpChannelNum, IPPPrintReq, WSDPrintReq, WSDScanReq, EpsonPushScan, PTPIPEvt, AirPrintReq |
| 0x45CC0–0x45CE0 | 128 KB | 252+276 | **Network config** — IP address formatting, date/time, SNMP-like, profile management, %s-%dMbps |
| 0x45CE0–0x45D00 | 128 KB | 285+185 | **IPP print service** — job-token, printer-state, print-x-disc-tray-state, document-format, media selection, error reasons |

#### Key Functions

| Address | Calls | Role |
|---------|-------|------|
| 0x45C5DA48 | 59 | **mem_pool_alloc** — pool allocator, calls Module C (0x45D66550) for low-level ops |
| 0x45C5EE18 | 60 | **mem_pool_register** — post-allocation registration/linkage |
| 0x45C5D9B8 | 37 | **mem_pool_alloc_variant** — sized allocation entry |
| 0x45C5EE1C | 28 | **mem_pool_register_variant** — alternate registration |
| 0x45C94DDC | 18 | Print format entry point |
| 0x45CEF33C | 17 | IPP attribute handler |
| 0x45C831FC | 184 | **return_zero** — `MOV r0, #0; BX LR` (null/success stub) |

#### Memory Allocator Details

The allocator at 0x45C5D990–0x45C5DC00 is a single large function with
multiple entry points for different allocation sizes. It manages fixed-size
**memory pools** indexed by a pool descriptor table:

- Pool descriptor array at `DAT_00010dac` (kernel literal pool), each
  entry 0x20 bytes
- Fields: `+0x04` (type flag), `+0x14` (alloc count), `+0x18` (total bytes),
  `+0x60` (capacity), `+0x78` (free list head), `+0x80` (chain ptr),
  `+0x88` (next free)
- Max 0x29 (41) pools
- Protected by Module D semaphore (0x45E45A34 take / 0x45E45A04 give)
- Kernel function 0x10AD0 is the **mem_free** — checks pool header,
  decrements count/size, returns block to free list

The kernel function at 0x10ad0 (96 calls from seg0 thunks) is a
**memory free** operation — it checks pool headers at negative offsets
(-0x16, -0x18, -0x12) and calls into this module.

### Module C: Data Tables and Dispatch (0x45D0–0x45DB)

**Not code.** Module C is a 768 KB data region (1.5% ARM instruction
density vs 80%+ for code) within `block1_streamA_web.bin` (file offset
0x400000–0x4BFFFF, loaded at base 0x45900000). The 101 "calls" from
segment 0 land in lookup tables, not functions.

Sub-regions:

| Address Range | Content |
|---------------|---------|
| 0x45D00000–0x45D0FFFF | Character/encoding lookup tables, "Panasonic" string |
| 0x45D10000–0x45D17FFF | IPP media size names: PWG self-describing format (`iso_a4_210x297mm`, `na_letter_8.5x11in`, `jpn_hagaki_100x148mm`, `custom_hivision_101.6x180.6mm`), IPP media type keywords (`photographic-glossy`, `stationery-inkjet`, `cardstock`, `envelope`) |
| 0x45D18000–0x45D2FFFF | JPEG Huffman tables (DHT marker data) + image quantization tables |
| 0x45D30000–0x45D47FFF | WebConfig menu/action dispatch tables ("CA_IMPORT", "BUSINESSCARD1", "Panoramic", "default-menu-item"), HTTPS cert URL (`epsonpfu.ebz.epson.net/root_certs2/cert.inf`) |
| **0x45D48000–0x45D6FFFF** | **Master dispatch table**: ~20,000 `[func_ptr, index]` 8-byte entries mapping sequential indices (0–65535) to handler functions. Targets: 38% → 0x459Fxxxx (RTOS middleware in this binary, many to default handler 0x459F61E2), 35% → 0x45FC–0x4602xxxx (WebConfig subsystem), 20% → 0x4600xxxx (panel UI binary at base 0x46000000), 7% → other RTOS code. This is the central **command/event dispatch table** for the entire application. |
| 0x45D70000–0x45D87FFF | Compressed image data (PNG palette fragments, pixel data) |
| 0x45D88000–0x45D9FFFF | HTML template fragments for WebConfig: `<a href=`, `<link rel='stylesheet'`, `<script type='text/javascript'` |
| 0x45DA0000–0x45DA7FFF | Manufacturing/diagnostic strings: "Charging Now!", "Suction Volume OK?", "Shipping JIG", "Touch PAD Screen Mode", "PNL Key Test", "Noise RAW Value"; IPP service strings; localized media names (30+ languages) |
| 0x45DA8000–0x45DBFFFF | MIME types (`application/vnd.pwg-xhtml-print+xml`, `application/octet-stream`) + GPL/Apache license text |

The localized media name strings include paper type names in English,
French, German, Spanish, Portuguese, Italian, Dutch, Swedish, Norwegian,
Danish, Finnish, Polish, Czech, Slovak, Hungarian, Romanian, Turkish,
Japanese, Chinese, and Korean — all for the WebConfig and panel UI.

The dispatch table at 0x45D48000 is the key finding: it bridges segment 0
(print engine) to all other subsystems by mapping numeric command/event IDs
to handler functions across the entire firmware.

### Module D: T-Kernel HAL + I/O Services (0x45E41084–0x45E4F0CC)

Third-largest dependency (541 direct BL calls from segment 0, 32 unique
targets). Code resides in **block1_streamA_web.bin** at file offsets
0x541084–0x54F0CC (~57 KB). **Contains the T-Kernel SVC wrapper table**
(SWI #6 stubs at 0x45E442D4–0x45E4549C) — this is where the actual
T-Kernel system call interface lives. Two distinct sub-layers:

#### Layer 1: T-Kernel SVC Stubs + Synchronization (0x45E40xxx–0x45E46xxx)

Contains 93 SVC wrappers (see "T-Kernel SVC API" above) plus
synchronization primitives called exclusively from the kernel via
14 thunks at kernel 0x103A4–0x1042C. Used by the kernel's memory
management subsystem (0x10000–0x11000). Clear paired functions:

| Acquire | Release | Kernel uses | Likely primitive |
|---------|---------|-------------|------------------|
| 0x45E45A34 | 0x45E45A04 | 6+5 = 11 | Semaphore take/give |
| 0x45E46234 | 0x45E46244 | 8+9 = 17 | Block lock/unlock |
| 0x45E45F10 | 0x45E45DFC | 4+4 = 8 | Alloc sync pair |
| 0x45E45F48 | 0x45E45E40 | 1+1 = 2 | Bulk alloc sync |

Other kernel-accessed functions:
- 0x45E40164 (1 use): Module init / one-time setup
- 0x45E45BC4 (2 uses): Auxiliary lock
- 0x45E45C9C (1 use): Lock variant
- 0x45E45A6C (1 use): Timeout wait
- 0x45E45B94 (1 use): Priority-aware acquire
- 0x45E45BF4 (2 uses): Cleanup/release variant

Two dispatch tables in the kernel reference Module D internal functions:
- **0x28F3C** (7 entries): Channel dispatch — 7 functions spaced ~0x280
  apart (0x45E46A80 through 0x45E4793C). Likely 7 identical DMA or I/O
  channels.
- **0x3ACFC** (5 entries): Service dispatch — 5 functions in 0x45E442xx–
  0x45E447xx range. Peripheral initialization or config variants.

#### Layer 2: I/O Services (0x45E47xxx–0x45E4Fxxx)

Called **directly from CROM segment 0** via BL (not through kernel thunks).
These are the high-level I/O operations the print engine and scanner use.

**Primary I/O cluster** (0x45E47xxx — 303 calls, 6 functions):

| Address | Calls | Description |
|---------|-------|-------------|
| **0x45E474C4** | **212** | Primary I/O service — takes diverse arguments (pointers, registers, constants). Not a simple mutex; likely a **peripheral register access dispatcher** or hardware command submit |
| 0x45E47360 | 68 | Companion to above — possibly read vs write, or synchronous vs async |
| 0x45E479C4 | 11 | I/O configuration |
| 0x45E479E4 | 5 | I/O variant |
| 0x45E479A8 | 4 | I/O variant |
| 0x45E47988 | 3 | I/O variant |

**Secondary I/O cluster** (0x45E4D–0x45E4F — 213 calls, 20 functions):

| Address | Calls | Description |
|---------|-------|-------------|
| 0x45E4EAB0 | 69 | Secondary I/O service |
| 0x45E4EB5C | 44 | Related to 0x45E4EAB0 |
| 0x45E4E858 | 29 | I/O operation |
| 0x45E4EA2C | 25 | I/O operation |
| 0x45E4E024 | 8 | I/O setup |
| 0x45E4DF64 | 7 | I/O operation |
| 0x45E4E708 | 5 | I/O operation |
| 0x45E4D900 | 4 | Low-level I/O |
| Others (12) | 22 | Various I/O utilities |

**Initialization cluster** (0x45E41xxx — 25 calls, 6 functions):

| Address | Calls | Description |
|---------|-------|-------------|
| 0x45E4130C | 14 | Channel/port configuration |
| 0x45E41164 | 4 | Init variant |
| 0x45E41084 | 3 | Module setup |
| 0x45E41430 | 2 | Config variant |
| 0x45E413DC | 1 | One-time init |
| 0x45E4149C | 1 | One-time init |

#### Module D Architecture Summary

```
                    ┌──────────────────────────────────┐
  Segment 0         │         Module D (57 KB)          │
  (print engine)    │                                    │
       │            │  ┌──────────────────────────────┐ │
       │ BL (541)   │  │  I/O Services (0x45E47-E4F)  │ │
       ├───────────►│  │  Primary: 303 calls           │ │
       │            │  │  Secondary: 213 calls          │ │
       │            │  │  Init: 25 calls                │ │
       │            │  └──────────┬───────────────────┘ │
       │            │             │ internal calls       │
       │            │  ┌──────────▼───────────────────┐ │
  Kernel (0x10xxx)  │  │  Sync Primitives (0x45E40-46)│ │
       │ thunk (49) │  │  Semaphores, mutexes, locks   │ │
       ├───────────►│  │  7-channel + 5-entry dispatch  │ │
       │            │  └──────────────────────────────┘ │
                    └──────────────────────────────────┘
```

## Block 1 Binaries (Separate Modules)

The three Block 1 CROM streams decompress to separate modules:

| Binary | Size | Base Address | End Address | Content |
|--------|------|-------------|-------------|---------|
| block1_streamA_web.bin | 5.6 MB | **0x45900000** | 0x45E65450 | RTOS middleware + WebConfig + all data tables (Modules A–D live here) |
| block1_streamB_panel.bin | 4.1 MB | **0x46000000** | ~0x463F4000 | Panel UI (C++ UIWidget framework, loaded above streamA) |
| block1_streamC_gfx.bin | 2.3 MB | TBD | TBD | Graphics (228 PNGs, fonts) |

Base addresses confirmed by self-referencing BL analysis (20,451 hits for
streamA at 0x45900000; 16,133 hits for streamB at 0x46000000 in first
320KB each).

**block1_streamA_web.bin** is the largest binary and contains:
- 0x45900000–0x45C60000: Dense ARM code (~3.5 MB) — RTOS middleware,
  Modules A (RTOS core), B (system controller + services), D (I/O/HAL)
- 0x45C70000–0x45E65450: Data region (~1.9 MB) — Module C dispatch
  tables, media configs, JPEG tables, HTML templates, localized strings

The dispatch table at 0x45D48000 contains ~20,000 entries mapping command
indices to handler functions across all three binaries.

**block1_streamA_web.bin** overlaps the address ranges of Modules A and B
(0x45B0–0x45BA). This strongly suggests that it contains the **RTOS core
services and resource management code** that Segment 0 calls into. The
web binary is not just the WebConfig UI — it likely includes the full
RTOS middleware layer.

## block1_streamA_web.bin Base Address Confirmation

Self-reference analysis of BL instructions gives 92.3% internal ratio at
base **0x45A00000** (72,759 internal / 6,078 external BLs). The file
spans 0x45A00000–0x45F65450 (5,657,680 bytes). Modules A–D all reside
within this binary:

| Module | Address Range | streamA File Offset | Size |
|--------|--------------|--------------------:|------|
| A (RTOS core) | 0x45B05504–0x45BA9B34 | 0x105504–0x1A9B34 | ~664 KB |
| B (system ctrl) | 0x45C53DD0–0x45CFCF38 | 0x253DD0–0x2FCF38 | 676 KB |
| C (data tables) | 0x45D00000–0x45DBFFFF | 0x300000–0x3BFFFF | 768 KB |
| D (I/O/HAL) | 0x45E40000–0x45E4FFFF | 0x440000–0x44FFFF | 64 KB |

The compact C library at 0x453BFxxx (645+ calls from Module B — most-called
external target) is **not** in streamA or in any binary we have. It may be
in a separate small CROM segment or copied from mask ROM.

## What's Still Missing

1. **T-Kernel kernel core** — the actual scheduler, context switch, and
   ready queue implementation. The SVC stubs (SWI #6) trap into kernel
   mode, but the kernel-mode handler that dispatches function codes to
   implementations has not been located. It may be in a different region
   of block1_streamA or loaded separately by the SoC ROM.
2. **Epson custom T-Kernel extensions (groups 0x11–0x76)** — 14+ custom
   SVC groups beyond standard T-Kernel. These likely include Epson-specific
   APIs for scanner control, print engine, and panel I/O. Need to trace
   which custom group codes correspond to which subsystems.
3. **The Segment 1 load address** — needed to properly cross-reference
   the API vector table with the kernel thunks
4. **block1_streamC_gfx.bin base address** — not yet determined by
   self-referencing analysis
5. **Dispatch table semantics** — the 20K-entry table at 0x45D48000 maps
   indices to handlers, but the index numbering scheme (what maps command
   IDs to table indices) is not yet understood
6. **C library module at 0x453BFxxx** — compact runtime library (memcpy,
   string ops) called by all modules but not found in any decompressed binary
7. **block1_streamA_web.bin base address conflict** — self-reference
   analysis gave 0x45A00000, but individual string pointer cross-refs
   give inconsistent bases (0x45920000, 0x45B10000, etc.), suggesting
   the binary may be a scatter-loaded image with multiple load regions

## References

- [CROM_FORMAT.md](CROM_FORMAT.md) — CROM compression algorithm details
- [RTOS_KERNEL.md](RTOS_KERNEL.md) — Kernel analysis, thunk table, IRQ dispatch
- [PANEL_PERIPHERALS.md](PANEL_PERIPHERALS.md) — LCD and button hardware
- [ARCHITECTURE.md](ARCHITECTURE.md) — Dual-domain SoC architecture
