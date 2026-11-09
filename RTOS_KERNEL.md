# Epson XP-442 RTOS Kernel — Reverse Engineering Notes

Analysis of `rcx_output/rtos_core.bin` (1,115,136 bytes = 0x110400), the
ARM1136JF-S main-domain firmware. Ghidra project `xp-442`, language
ARM:LE:32:v6, base address 0x0.

## Identity

The RTOS is **T-Kernel** — a TRON-family real-time operating system.
The kernel stub in `rtos_core.bin` contains only the bootstrap and HAL;
the full T-Kernel OS (scheduler, SVC dispatch, IPC primitives) is in the
CROM-decompressed `block1_streamA_web.bin`. Confirmed by explicit strings
(`T-Kernel/OS SVC in execution`, SWI #6/#7/#8/#9 descriptions) and the
standard T-Kernel configuration table (`TMaxTskId`, `TMaxSemId`, etc.).
Epson extends it with 14+ custom SVC groups. See
[CROM_APPLICATION.md](CROM_APPLICATION.md) for the full T-Kernel API map.

Identifiers in the kernel stub:

| Offset | String | Purpose |
|--------|--------|---------|
| 0x0062 | `BooSt for SoC09S 1.0.3 (2008/08/01)` | Primary bootstrap |
| 0x0088 | `Start ARM1136JF-S ... RTOS` | RTOS boot banner |
| 0xF26F | `EPSON PRINTER ROM` | ROM identity |
| 0xF9E4 | `MFG:EPSON;CMD:ESCPL2J,BDC,D4;MDL:MFP-IPL;CLS:PRINTER` | IEEE-1284 device ID |
| 0x10F400 | `RO20J9` | Firmware version tag |
| 0x10FFF0 | `TXW00K` | CROM version tag |
| 0x110018 | `MiLL for Soc09S 2.0.0 (2011/09/15)` | Secondary loader |

"Seiko Epson Corp" at 0x17BA4. Firmware build `RO20J9`.

## Image Layout

```
0x000000 ┌──────────────────────────────────┐
         │ ARM exception vectors (0x20)      │
0x000020 │ Vector targets (0x24)             │
0x000044 │ Fault handlers (infinite loops)   │
0x000054 │ FIQ stub                          │
0x000062 │ BooSt banner + RTOS banner        │
0x000114 │ Reset entry (BooSt bootstrap)     │
         │                                   │
0x000424 │ IRQ dispatch handler              │
0x000478 │ RTOS initialization               │
         │   ...                             │
0x005924 │ Hardware register I/O functions    │
0x006098 │ memcpy                            │
         │   ...                             │
0x010000 │ More kernel code                  │
         │   ...                             │
0x017000 ├──────────────────────────────────┤
         │ Data tables, init data, sparse    │
0x018000 │ Printer data tables               │
         │ JPEG/PDF templates (0x20E3A)      │
         │ Cloud Print strings (0x2320C)     │
         │ D4/EPSON-DATA protocol (0xF9CD)   │
         │ State machine strings (0x75862)   │
         │   ...                             │
0x03C000 ├──────────────────────────────────┤
         │ Zero padding                      │
0x040000 ├──────────────────────────────────┤
         │ CROM compressed application code  │
         │ (Huffman + LZ77, see CROM_FORMAT) │
         │ Seg 0: 239 KB → 514 KB app code  │
         │ Seg 1: 7 KB → 12 KB API vectors  │
         │ Decompresses to ~0x45430000 RAM   │
0x07C4B2 │ End of CROM data                  │
         │ 0xFF padding (~584 KB)            │
0x10B000 ├──────────────────────────────────┤
         │ ARM MMU L1 page table (16 KB)     │
         │ 4096 section descriptors          │
         │ All identity-mapped (VA = PA)     │
0x10F000 │ 0xFF padding                      │
0x10F400 ├──────────────────────────────────┤
         │ RO20J9 version tag                │
         │ Zero padding                      │
0x10FFF0 │ TXW00K / RO20J9 trailer           │
0x110000 │ MiLL loader code                  │
0x110018 │ MiLL banner                       │
0x110040 │ "Copy Kernel from ROM to RAM"     │
0x110400 └──────────────────────────────────┘
```

**Kernel code**: 0x0–0x3BFFF (~240 KB uncompressed ARM code + data).
Ghidra identifies **524 functions** in this region.

**CROM application**: 0x40000–0x7C4B2 (~247 KB compressed, 2 segments).
Decompresses to 526 KB: segment 0 (514 KB ARM application code) loads at
~0x45430000, segment 1 (12 KB API vector table with 35 `LDR PC` entries).
Uses Huffman + LZ77 compression — see [CROM_FORMAT.md](CROM_FORMAT.md).

**MMU page table**: 0x10B000–0x10EFFF (16 KB) — ARM L1 page table, all
identity-mapped. The 0x40000000–0x47FFFFFF region (128 MB) is cached.

**MiLL loader**: 0x110000–0x110400 — a small secondary loader that copies
the RTOS kernel from ROM to RAM. Prints "Success/Error: Copy Kernel from
ROM to RAM" to a UART.

## Exception Vector Table

All at base address 0x0. Each vector is `LDR PC, [PC, #0x1C]` loading a
target from the literal pool at 0x24–0x40.

| Vector | Offset | Target | Handler |
|--------|--------|--------|---------|
| Reset | 0x00 | 0x000114 | BooSt bootstrap — CP15 setup, MMU, cache, domain access, TLB flush, then jumps to RTOS init |
| Undef | 0x04 | 0x000044 | `B .` (infinite loop — hang on undefined instruction) |
| SWI | 0x08 | 0x000048 | `B .` (infinite loop — **SWI not used**) |
| Prefetch Abort | 0x0C | 0x00004C | `B .` (infinite loop) |
| Data Abort | 0x10 | 0x000050 | `B .` (infinite loop) |
| Reserved | 0x14 | — | NOP (`MOV r0, r0`) |
| IRQ | 0x18 | 0x000424 | Vectored interrupt dispatch (see below) |
| FIQ | 0x1C | 0x000054 | Thunk → 0x100E4 → indirect call via function pointer |

**SWI is unused.** The RTOS makes no trap-based system calls; all kernel
services are invoked via direct function calls. This is typical for
single-address-space embedded RTOS designs where the kernel and application
share the same privilege level.

**Fault handlers are fatal.** Undefined instruction, prefetch abort, and data
abort all hang in infinite loops — no recovery, no diagnostic output.

## Boot Sequence

```
Power-on → SoC mask ROM
  └→ Decompress CROM (0x40000–0x7C4B2) to RAM at 0x45430000+
       └→ MiLL (0x110000) — copies kernel from ROM to RAM
            └→ ARM reset vector (0x0)
                 └→ BooSt (0x114)
                      ├─ Disable MMU, caches, branch prediction
                      ├─ Invalidate TLB
                      ├─ Set domain access = all (0xFFFFFFFF)
                      ├─ Check Main ID register (ARM1136 variant detection)
                      ├─ Configure Auxiliary Control register
                      ├─ Initialize memory regions (memcpy/memset)
                      ├─ Load MMU page table from 0x10B000
                      └→ Jump to RTOS init
                           ├─ RTOS init (0x478)
                           │    ├─ FUN_00000a84() — early hardware init
                           │    ├─ memcpy() — copy data structures
                           │    ├─ memset() — zero BSS
                           │    ├─ Configure task/memory descriptors
                           │    └─ Set initial interrupt masks
                           └→ Jump to 0x40007BC0 (RTOS main in RAM)
```

The BooSt bootstrap configures CP15 coprocessor registers:
- **Control register**: Disables MMU (bit 0), I-cache (bit 12), alignment
  checking (bits 1-2), sets high vectors if needed
- **Domain Access Control**: Set to 0xFFFFFFFF (manager mode for all domains
  — no permission checking)
- **TLB**: Invalidated (unified TLB flush)
- **Auxiliary Control**: Enables return stack, dynamic branch prediction,
  static branch prediction (bits 0-2)

## IRQ Dispatch

The IRQ handler at 0x424 implements a **vectored interrupt controller**
dispatch:

```c
// Decompiled IRQ handler (0x424)
void IRQ_Handler(void) {
    uint *ic_base = (uint *)0xE0001000;   // Interrupt controller
    uint status = *ic_base;                // Read pending interrupt
    uint irq_num = (status >> 16) & 0x7F; // Extract 7-bit IRQ number
    
    code *handler = irq_table[irq_num];   // irq_table at RAM 0x4012CB2C
    if (handler != NULL) {
        handler();                         // Call registered handler
    }
    
    ic_base[2] = status;                  // Write EOI (offset +8)
    return;                               // SUBS PC, LR, #4
}
```

**Key details:**
- Interrupt controller base: **0xE0001000** (memory-mapped)
- IRQ number: 7-bit field from status register bits [22:16]
- Up to 128 IRQ sources
- Handler table: RAM address **0x4012CB2C** (registered at runtime)
- EOI: Write original status word to controller base + 8
- The handler saves/restores {r0-r5, r12, lr} — a lightweight context save

## Task Scheduling

### No Preemptive Scheduler in the Kernel Stub

The uncompressed kernel region (0x0–0x3FFFF) contains **no visible task
scheduler**, no ready-queue data structures, and no SWI-based context switch.
The RTOS uses **direct function-call scheduling** — the scheduler, task
creation, and context switch logic are almost certainly in the CROM
compressed region (0x40000+), which decompresses to addresses around
0x45xxxxxx in RAM at runtime.

Evidence for this:
1. The kernel's thunk table at ~0x166B8 contains 56+ entries pointing to
   0x45xxxxxx addresses — these are the RTOS API entry points
2. The context save/restore descriptors at 0x175E8–0x177A0 contain STM/LDM
   patterns for all ARM processor modes (USR/SYS, FIQ, IRQ, ABT, UND, SVC)
   — this is the register save area layout, but the code that uses it is
   in CROM
3. The kernel RAM area at **0x4012C000** (referenced 119 times) is the
   primary runtime data region — likely contains TCBs, scheduler queues,
   and kernel state

### Context Save Area (0x175E8–0x177A0)

This region contains ARM STM/LDM instruction encodings that describe
register save/restore patterns for each processor mode. The pattern of
conditional STM (save) followed by conditional LDM (restore) for all 16
registers (including PC, SP, LR) across multiple modes is a classic RTOS
context switch descriptor table:

- 9 conditional STM all-register saves (mode save)
- 14 conditional LDM all-register restores (mode restore)

This table is indexed by the context switch code (in CROM) to save/restore
the correct set of banked registers when switching between ARM modes during
a task switch.

### RTOS API Thunk Table (~0x166B8)

The kernel exposes its services to the decompressed application through a
jump table of thunk functions:

```
0x166B8: thunk → EXT_FUN_45b5df28
0x166C0: thunk → EXT_FUN_45b5df30
0x166C8: thunk → EXT_FUN_45b5df48
  ... (56+ entries)
0x16808: thunk → EXT_FUN_45e3f0f0
```

These addresses in the 0x45xxxxxx range are in the CROM-decompressed
application. The thunk table acts as a **service vector** — the kernel's
equivalent of a syscall table, but using direct branches instead of SWI
traps.

Notable external function groups (by address cluster):
- **0x45b5dfxx**: Core RTOS services (8 entries)
- **0x45c5xxxx**: Memory/resource management
- **0x45c6ebxx**: Event/notification services (6 entries)
- **0x45e45xxx**: I/O services
- **0x45baxxxx**: Communication services
- **0x47effxxx**: Inter-domain (EPIDC) interface

## Memory Map

### SoC Peripheral Addresses

| Address | Refs | Function |
|---------|------|----------|
| 0x40000000 | 7 | System control base |
| 0x40008000 | 1 | Secondary controller |
| 0x40010000 | 1 | Peripheral |
| 0x40014000 | 1 | Peripheral |
| 0x40100000 | 1 | External peripheral (thunk target) |
| 0x4011C000 | 4 | Timer / counter |
| **0x4012C000** | **119** | **RTOS kernel RAM** (task data, IRQ tables, scheduler state) |
| 0x40400000 | 1 | Peripheral block |
| 0x40404000 | 1 | Peripheral block |
| 0x41D00000 | 1 | Display / panel controller |
| 0x41E00000 | 1 | Display / panel controller |
| 0x41F00000 | 1 | Front panel |
| 0x42100000 | 1 | DMA controller |
| 0x42600000 | 2 | Print engine |
| 0x42F07000 | 2 | Scanner |
| 0x42F90000 | 2 | Scanner |
| 0x45000000 | 1 | CROM decompressed code base |
| 0x45400000 | 5 | Application code region |
| 0x46000000 | 1 | Peripheral |
| 0x47EFF000 | 1 | EPIDC shared memory |
| 0x47F00000 | 2 | EPIDC control |
| **0xE0001000** | — | **Interrupt controller** (from IRQ handler) |

The kernel RAM at **0x4012C000** is by far the most-referenced address,
confirming it as the central runtime data area. The IRQ handler table
(confirmed at 0x4012CB2C) lives within this region.

### RAM Layout (inferred)

```
0x00000000  Kernel flash (0x0-0x3FFFF) — read-only at this address
   ...
0x40000000  SoC control registers (memory-mapped I/O)
   ...
0x4012C000  RTOS kernel data (TCBs, IRQ table, scheduler)
   ...       (IRQ handler table at 0x4012CB2C)
   ...
0x42000000  EPIDC shared memory (0x42003000, size 0x15B000)
   ...
0x45430000  CROM Segment 0: print engine + scanner (514 KB)
   ...
0x45B00000  RTOS core services module (scheduler, semaphores)
0x45C50000  Resource/memory management module
0x45D00000  Peripheral driver module
0x45E40000  I/O / hardware abstraction module
   ...
0x47EFF000  EPIDC region
   ...
0x80000000  Kernel RAM copy (MiLL copies flash 0x0 → 0x80000000)
   ...       CROM application calls kernel at 0x80000xxx, NOT 0x0xxxx
0xE0001000  Interrupt controller (ARM peripheral space)
0xE0C00000  EPIDC comm control (from epidc.ko params)
```

**Kernel RAM copy**: MiLL copies the kernel code from flash (0x0–0x3FFFF)
to RAM at 0x80000000. The CROM application always calls kernel functions
via their RAM addresses (e.g., kernel 0x634 → 0x80000634). The flash copy
at 0x0 is used only during early boot before MiLL runs.

## Key Kernel Functions

### Identified by Ghidra (524 total)

| Address | Calls | Name | Purpose |
|---------|-------|------|---------|
| 0x00114 | 1 | FUN_00000114 | **BooSt reset entry** — CP15 config, MMU setup |
| 0x00424 | — | FUN_00000424 | **IRQ dispatch** — vectored interrupt handler |
| 0x00478 | 1 | FUN_00000478 | **RTOS init** — hardware init, data structure setup |
| 0x004B0 | — | FUN_000004B0 | Block search (scans for 0xFF boundary markers) |
| 0x006E8 | — | FUN_000006E8 | Command parser (processes 0x33FF/0x37FF/0x7FFF opcodes) |
| 0x01808 | — | FUN_00001808 | **memcpy** (byte-by-byte, 36 bytes) |
| 0x0182C | — | FUN_0000182C | **memset** (byte-by-byte, 28 bytes) |
| 0x05AF8 | 77 | FUN_00005AF8 | **Hardware register write** — writes to peripheral, busy-waits for status |
| 0x05B44 | 34 | FUN_00005B44 | **Hardware register sequence** — two writes + tail-call |
| 0x06098 | 25 | FUN_00006098 | **memcpy** (another variant, byte-by-byte) |
| 0x0EA28 | 20 | FUN_0000EA28 | Frequently called utility |
| 0x10EA4 | 94 | FUN_00010EA4 | **Busy-wait delay** (`while(n--) {}`) |
| 0x13080 | 49 | FUN_00013080 | **Interrupt enable/disable** — toggles bit 0 at 0xF0000020 |
| 0x13130 | 30 | FUN_00013130 | Register manipulation |
| 0x144F8 | — | `i2c_sample_ack` | **I2C ack clock** — raises SCL (bit 1 at +0x20), waits, samples SDA, returns 1/0 (tail of a bit-banged I2C routine) |
| 0x1457C | 24 | `rtc_i2c_write_reg` | **RTC register write** — I2C start, device byte 0xA2, register, value, stop (see PANEL_PERIPHERALS.md, I2C section) |
| 0x15548 | 26 | FUN_00015548 | **Timer delay** — reads timer counter, busy-waits |
| 0x100E4 | — | FUN_000100E4 | **FIQ handler** — indirect call via function pointer |

### Hardware Register I/O (0x5AF8)

```c
// Decompiled — hardware register write with busy-wait
void hw_reg_write(uint param) {
    volatile uint16_t *base = (volatile uint16_t *)PERIPH_BASE;
    base[0x0A] = 0xFFF7;                        // Clear/config
    base[0x188] = (param >> 8) | (param >> 16);  // Address/data
    while (!(base[0x180] & 2)) {}                // Wait for ready
    *BYTE_PORT = (char)param;                    // Write data byte
    while (!(base[0x180] & 2)) {}                // Wait for done
    base[0x09] = 8;                              // Trigger/commit
}
```

This pattern appears 77 times — it's the primary way the RTOS kernel talks
to the SoC09S hardware. The busy-wait on bit 1 of a status register
suggests a synchronous serial interface (SPI or I2C-like).

### Interrupt Enable/Disable (0x13080)

```c
// Decompiled — interrupt master switch
void irq_control(int enable) {
    if (enable == 0) {
        GLOBAL_FLAGS &= ~1;  // Disable interrupts
    } else {
        GLOBAL_FLAGS |= 1;   // Enable interrupts
    }
}
```

This function (49 calls) toggles a single bit in a global control register.
The flag at address 0xF0000020 is likely an ARM coprocessor-mapped
interrupt controller enable.

## IPC Mechanisms

### Within the RTOS Domain

1. **Direct function calls** — no SWI barrier; kernel and application share
   address space and privilege level
2. **Shared memory** — the kernel RAM at 0x4012C000 is directly accessible
   by all code
3. **IRQ-driven notifications** — the vectored interrupt controller delivers
   up to 128 interrupt sources
4. **Function pointer callbacks** — the FIQ handler and several kernel
   services use indirect calls through function pointers in RAM

### Cross-Domain (RTOS ↔ Linux)

Communication with the network domain (ARM926 / Linux) uses EPIDC:

- **Shared physical memory**: 0x42003000 (size 0x15B000)
- **Control memory**: 0xE0C00000
- **Kernel-side thunks**: 0x47EFF000, 0x47F00000 (2-3 references each)
- **Protocol**: D4 (IEEE-1284.4) multiplexed over EPIDC shared memory
- **Channels**: MIB (config), IPP-over-USB, PTP/IP, reverse events

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full EPIDC/D4/COMIF stack.

## Printer State Machine

15 states defined in the CROM application at 0x456C4 (relative to CROM
segment 0 base 0x45430000 = file offset 0x26C4 in the decompressed binary):

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

The state machine is driven by the application code in the CROM section,
not the kernel stub. The kernel's uncompressed region at 0x75862 has a
truncated/concatenated copy of these names.

## Flash Update Support

The kernel includes flash update logic (strings at 0xF2A9–0xF4DD):

- **Program Update Mode** (PUM) — full firmware flash
- **IPL Update Mode** — bootloader update
- **Recovery Mode** — fallback update path
- **Progress**: "Now Reading File", "Updating...", "Now Erasing Rom Data",
  "Now Writing Rom Data", "Now Verifying Rom", "Finished!"
- **Errors**: "FILE READ ERR", "FILE IS NOT FOUND", "FATAL ERROR",
  "PREVIOUS UPDATE ERR", "ROM IS NOT WRITABLE", "CONTACT OUR SUPPORT"

The D4 protocol channels for updates:
- `EPSON-DATA` (0xF9CD) — firmware data stream
- `EPSON-CTRL` (0xF9D8) — control commands

## Embedded Applications

Strings reveal the kernel includes support for:

1. **Google Cloud Print** (0x2320C–0x23D9C):
   - `EpsonCloudAwarePrinter/1.0`
   - OAuth2 via `accounts.google.com`
   - Client ID: `676756576767.apps.googleusercontent.com`
   - Client secret: `FojGVM5woJJzgMEsNBV9G78C`
   - Token/register/submit/delete/control/ticket APIs

2. **PDF Generation** (0x20E3A–0x213A8):
   - Full PDF-1.4 structure templates (Catalog, Pages, XObject)
   - RGB and grayscale image support
   - Rotation and media box control

3. **XML Parsing** (0x31790–0x317E7):
   - UTF-16, EUC-JP, SHIFT_JIS encoding support
   - CDATA, DOCTYPE, entity handling

4. **Scanner** (0x74C37):
   - `scnr_api2tbl.c` — scanner API table source file reference
   - `egd_front` (0x75BCA) — scanner front-end

5. **IPP Printing** (0x38500):
   - `http://[%s]:631/%s` — IPP URL templates (IPv6 aware)
   - `https://[%s]:631/%s` — IPP over TLS

## What's Not in the Kernel Stub

The uncompressed kernel (0x0–0x3FFFF) is a **minimal bootstrap and HAL**
layer. The following are all in the CROM compressed application:

- Task creation / deletion / scheduling
- Semaphore / mutex / event flag primitives
- Timer callbacks and periodic scheduling
- Memory pool management
- The actual print engine control loop
- COMIF peer (cross-domain config bridge)
- PNRSC configuration registry
- All network protocol handling

To analyze these, decompress the CROM section using `uncrom.py` and load
into Ghidra at base 0x45430000. See [CROM_FORMAT.md](CROM_FORMAT.md) for
the format and [CROM_APPLICATION.md](CROM_APPLICATION.md) for the
high-level subsystem map of the decompressed code.

## Next Steps

1. **Import decompressed CROM into Ghidra**: Load `rtos_crom.bin.0.bin`
   at base 0x45430000 alongside the kernel for cross-referencing. See
   [CROM_APPLICATION.md](CROM_APPLICATION.md) for the full procedure.
2. **Map the RTOS service modules**: The CROM application calls into 4
   external modules (0x45B0–0x45EA) with 1,754 total calls. These likely
   come from block1_streamA_web.bin, which overlaps their address range.
3. **Map the scheduler**: Module A at 0x45B5xxxx has the most-called
   pair (0x45B5C8A0 / 0x45B5C7E0 — 250+197 calls) — likely
   semaphore acquire/release or event flag wait/set.
4. **Map the COMIF peer**: Find the main-domain side of the cross-domain
   config bridge (counterpart to nwsoc's PNCOMIFSetField)
5. **Identify PNRSC**: Find the authoritative NVRAM config registry (the
   main domain holds the ground truth for all device settings)

## References

- [CROM_FORMAT.md](CROM_FORMAT.md) — CROM compression format, Ghidra import procedure
- [CROM_APPLICATION.md](CROM_APPLICATION.md) — Decompressed CROM subsystem map
- [PANEL_PERIPHERALS.md](PANEL_PERIPHERALS.md) — LCD and button hardware
- [ARCHITECTURE.md](ARCHITECTURE.md) — Dual-domain SoC architecture, EPIDC, D4, COMIF
- [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md) — PNRSC registry internals, ID tables
- [FIRMWARE_LAYOUT.md](FIRMWARE_LAYOUT.md) — Firmware layout, extraction details
- [TELNET_STATUS.md](notes/TELNET_STATUS.md) — Telnet investigation status, root shell approaches
