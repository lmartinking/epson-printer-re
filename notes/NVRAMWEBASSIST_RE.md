# NVRAMWEBASSIST Storage Backend — Deeper Static RE

**Goal:** Resolve where the NVRAMWEBASSIST blob (containing
`TELNET_MS_CURRENT_ACTIVATION` and all WEBASSIST-tier service settings)
is physically stored. The answer determines whether the telnet flag is
reachable without flashing firmware or opening the case.

**Status:** ✅ **RESOLVED (2026-09-26).** Static RE executed on `nwsoc`
(Steps 1–3 below). The contradiction is resolved in favour of **H1**:
the NVRAMWEBASSIST blob is stored in the **main domain's authoritative
NVRAM**, reached only across the domain boundary via COMIF → D4 (MIB
channel) → EPIDC (`ioctl` on `/dev/epidc0`). It is **not** in any
Linux-domain file, **not** in raw MTD, and **not** in the
`||`-accessible 64 KB main-domain *hardware* EEPROM. See
[Execution results](#execution-results-2026-09-26) below.

---

## Execution results (2026-09-26)

Tooling: GNU `objdump` 2.47 (`elf32-littlearm`) + `capstone`/`pyelftools`
helpers over the stripped ARM32 `nwsoc`. Binary is stripped, so functions
were re-segmented from the set of all `bl`/`blx` targets to build a call
graph. VA↔offset: first `PT_LOAD` maps `off = va − 0x8000`.

### Verdict

| Hypothesis | Verdict | Basis |
|---|---|---|
| **H1** — WriteSetup blob → main-domain storage outside `\|\|` window | ✅ **CONFIRMED** | COMIF→D4→EPIDC transport terminates at `ioctl(/dev/epidc0)`; no Linux-local store exists (below) |
| **H2** — persistence is Linux-domain-local (file/MTD/ioctl-shm) | ❌ **REJECTED** | No `rscwebassist` file; JFFS2 flush covers only 4 regions; no write-mode `fopen` to any WEBASSIST path; no raw MTD write anywhere in `nwsoc` |
| **H3** — Phase 0 followed the wrong branch | ❌ **REJECTED** | Setter→vtable[12]→WriteSetup and the `PLeoNVRamDB` vtable are confirmed exactly as Phase 0 described (slot 12 = `0x32b18`) |

### Step 1 — `PNRSCBlockadeAndNvramFlush` traced (flushes only the 4 JFFS2 regions)

Flush string confirmed at VA `0x5cba8c`; its sole xref is a literal pool
at `0x6d984`, loaded by the error path at `0x6d948` when the call at
`0x6d920` returns −1. That call is the flush itself:

```
PNRSCBlockadeAndNvramFlush @ 0x24f04
  ├─ sets blockade flag on global 0x695dbc, takes lock (0x76fa8)
  ├─ bl 0x1942c  (real flush)
  │     └─ PLeoNVRamDB singleton @ 0x695de4 → vtable[41] (+0xa4) = 0x34d28  (flush-all)
  │           └─ vtable[57] (+0xe4) = 0x339e0  (per-region flush dispatcher)
  └─ clears blockade flag, unlock (0x76fa4)
```

The per-region dispatcher `0x339e0` handles **exactly four** regions
(dirty flags at obj `+0x44/+0x48/+0x4c/+0x5c`; buffers allocated in the
ctor at sizes `0x40`, `0xc000`, `0x80`, `0x800`), each written by the
file helper `0x15ee8`, which `fopen`s one of exactly four paths:
`/opt/nwsoc_data/{rscbasic,rsceternal,rscncl,rscncl2}` (loader at
`0x15e40`). **WEBASSIST is not one of them** — it is never touched by the
JFFS2 flush. `PLeoNVRamDB` is *only* the 4-file JFFS2 manager. This also
moots the "flush-on-exit timing caveat" for WEBASSIST: WEBASSIST does not
flow through this flush at all.

Confirmed vtable (`PLeoNVRamDB`, base `0x568b18`): slot 12 (`+0x30`) =
`0x32b18` (matches Phase 0), slot 41 = `0x34d28`, slot 57 = `0x339e0`.

### Step 2 — WriteSetup routes to the cross-domain transport, not local I/O

`vtable[12]` SetU32 (`0x32b18`) looks up a per-item descriptor via
`vtable[43]` and routes on a **storage-type field** (`[desc+0x18]`,
values 3/4/5/6): type 4 → `vtable[20]`, type 6 → `0x32ccc`, else →
`0x32cec`. `PNCOMIFWriteSetup` (`0xe2378`, called by `sub_1d5780`) builds
the self-describing text blob (`bl 0xbd894`, the serializer at `0xbd750`)
into the in-memory COMIF field cache; it makes **no** direct file/socket
syscall. Cross-domain delivery is event-decoupled: `COMIF_LEVT_WRITESETUP`
(callback registered in `sub_1d62d0`, event id 5) drives a D4 MIB-channel
send. The transport primitive is concrete:

```
D4CbtSendIdc wrapper @ 0x40b408
  └─ 0x44c044:  open("/dev/epidc0")  +  ioctl(fd, …)  ×3   → MAIN DOMAIN
```

`/dev/epidc0` = the EPIDC shared-memory inter-domain driver
([ARCHITECTURE.md](../ARCHITECTURE.md)); COMIF is defined as the D4-MIB
config bridge to the main domain. The static call graph cannot bridge the
callback/channel-task boundary between the field-cache write and the D4
send (expected for an event-driven design), but every Linux-local
alternative is eliminated (Step 3), so the blob's only destination is the
main domain.

### Step 3 — full write-path catalog (no Linux-local WEBASSIST store)

All 173 `fopen`/`fopen64`/`open` sites resolved; **30 are write-mode**.
The only persistent (`/opt/nwsoc_data`) literal-path writer is
`ipsec.dat`; the rest are `/tmp/*` (volatile), `/dev/null`, `/dev/tty`,
or dynamic-path (the 4 `rsc*` files via `0x15ee8`, traced in Step 1).
No write-mode open targets a `webassist`/service-config path. `nwsoc`
contains **no** `/dev/mtd*`, `mtdblock`, `flash_eraseall`, or `mkfs`
reference — it never writes raw flash. The commented-out `mtdblock3`
fstab entry is inert.

### Step 4 — Ghidra decompilation trace (2026-09-28): WriteSetup is backup-only

Interactive decompilation in Ghidra (project `xp-442`, `nwsoc` at image base
`0x8000`) traces the whole write path. Functions in the project:

| VA | Name | Role |
|---|---|---|
| `0x3214c` | `PNRSCBaseSetU32` | generic setter; packs `(Subject<<16)\|Item`, dispatches singleton `DAT_00695de4` vtable `+0x30` |
| `0x32b18` | `PLeoNVRamDB_SetU32` | vtable slot 12 (verified: vtable `0x568b18`, slot 12 = `0x32b18`) |
| `0xe2378` | `PNCOMIFWriteSetup` | logs *"Write Setup error. (Network Settings to Main SOC)"* |
| `0xbd894` | `UPPTWriteSetup` | mallocs `0xA800` blob, calls serializer, D4-writes, D4-reads reply |
| `0xbd750` | `UPPTWriteSetupSerializeBlob` | builds the request text |
| `0x40f980` | `D4_Write` | D4 channel write |
| `0x40d3a4` | `D4CbtUPnPDataWrite` | D4/IDC byte write → `ioctl(/dev/epidc0)` → main domain |
| `0x1d5d54` | `prvBackupTask` | **the only caller of `PNCOMIFWriteSetup`** |

**Generic setter path:** `PNRSCBaseSetU32` dispatches through
`PLeoNVRamDB_SetU32` (vtable[12]); that function does a descriptor lookup via
vtable `+0xac` (slot 43) keyed by `(subject<<16)|item`, reads the storage-type
at `descriptor+0x18` (values 2/4/6), and routes to a per-type concrete writer
(`+0x48/50/5c` or `+0x6c/78/84`) with a commit at `+0xc0`. For telnet
(subject `0x37`) the descriptor lookup has nothing to resolve — `0x37` is
absent from the descriptor table — so the call errors out before any store.

**The WriteSetup serializer emits a per-request named text blob.**
`UPPTWriteSetupSerializeBlob` builds exactly:

```
"EpsonLocalReq:WriteSetup\x1b\r\n" <field-name> ":" <payload-bytes> "\x1b\r\n"
```

(`field-name` at struct `+0`, payload length at `+0x28`, payload at `+0x2c`).
So the cross-domain write *is* addressable by field name — the question is
purely which names any caller supplies.

**The only caller supplies `"SetupData"` in a bulk backup loop.**
`PNCOMIFWriteSetup` has exactly one caller, `prvBackupTask` (`0x1d5d54`). It
iterates a config image in `0x30`-byte strides, base64-encodes each `0x40`-byte
chunk (`pnudrBase64Encode`), and sends every chunk under the single field name
**`"SetupData"`**, finishing with a `"ResetAll"`. No caller passes a per-service
field name (telnet or otherwise) to WriteSetup. This is a base64 `SetupData`
backup/restore of an already-populated config image.

**Telnet strings:** `TELNET`, `TELNET_MS_SUPPORT`, `TELNET_MS_CURRENT_ACTIVATION`
(at `0x5c6328/0x5c7098/0x5c70ac`) have **zero code xrefs** — present only as data
in the debug ID name-table.

**Net:** both write mechanisms are closed to telnet. The generic setter has no
`0x37` descriptor; WriteSetup is driven only by an opaque bulk `SetupData`
backup that serializes whatever the config image already holds — it is not a
per-field setter.

### Consequence for enabling telnet

> **The flag gates nothing — there is no telnet server in this build.** Every TCP
> listener in `nwsoc` is accounted for and none binds port 23; the `TELNET_*`
> strings have zero code xrefs; rootfs busybox has no `telnetd`. Writing the flag
> by any means starts no service; enabling telnet means **adding a server**.
> Shell-access routes are in [NETWORK_SERVICES.md § Getting a shell](../NETWORK_SERVICES.md) and
> [TELNET_STATUS.md](TELNET_STATUS.md). The storage analysis in this section is about where the
> flag would live, not about a reachable outcome.

The flag lives in main-domain NVRAM, writable only by the main domain's own
WriteSetup handler. There is no TelnetEnable COMIF field and no telnet entry in
the descriptor tables ([PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md)), so no exposed write interface
(ENPC / WSD `SetData` / COMIF field set) can address it, and the only WEBASSIST
writer is the bulk `SetupData` backup. And because no server consumes the flag,
none of this matters for enabling telnet — a root shell comes from adding a
server or the UART console, not from the flag ([NETWORK_SERVICES.md](../NETWORK_SERVICES.md)).

The blob cannot be dumped from the Linux side: it does not live there.

---

<details>
<summary>Original plan (pre-execution) — retained for reference</summary>

**Physical access constraint:** UART root shell would shortcut this
entire analysis, but physical bench access to the printer is not easy
and no USB-to-serial converter is available. This document therefore
focuses on the remote-only static RE path.

---

## What the static RE proved (RE_PLAN Phases 0–2)

The write path for `TELNET_MS_CURRENT_ACTIVATION` was traced through
`nwsoc`:

```
PNRSCBaseSetU32(Subject=0x37, Item=0xc2, value)
  → PLeoNVRamDB vtable[12] @ VA 0x32b18
    → PNCOMIFWriteSetup @ VA 0xe2378
      → serializer @ VA 0xbd750 (builds a text-format blob)
        → sends blob over COMIF/D4 to main domain
```

- **Singleton:** `PLeoNVRamDB` global instance at VA `0x695de4`, vtable
  at `0x568b18`.
- **Setter:** `PNRSCBaseSetU32` at VA `0x3214c` packs `(Subject<<16|Item)`
  and dispatches through vtable slot 12.

Phase 2 hit a stop condition: TELNET items are **completely absent** from
the 724-entry per-item descriptor table (56-byte entries at VA `0x6575f8`).
Subject `0x37` is skipped — the table jumps directly from `0x36`
(WEBASSIST, 1 entry) to `0x38` (JM, 3 entries). That table covers the
JFFS2-backed NVRAM regions (`rscbasic`, `rsceternal`, `rscncl`, `rscncl2`).

NVRAMWEBASSIST items use the WriteSetup serializer instead, which builds
a **self-describing text-format blob** whose layout is determined at
runtime — not by a static offset table. The item offset within the blob
is not statically recoverable without the blob contents themselves.

> **Note:** NVRAM_ADDRESS_STATIC_RE.md labelled this the "Layout C" stop condition, but
> that label referred to hash-based dictionary storage. The actual
> mechanism here — a self-describing text format with embedded field
> names — is structurally different and likely **easier** to parse once
> the raw blob is obtained. If the blob can be dumped (via any path),
> simple `strings` + field-name grep should locate the telnet flag
> without needing to reverse the serializer's layout logic.

## The contradiction the empirical work exposed

RE_PLAN Phase 0 concluded "backend is EEPROM via COMIF WriteSetup" — the
blob is serialized and sent over COMIF to the main domain. But the EEPROM
diff experiment ([NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) Phase 5) **disproved**
that the `||`-accessible EEPROM stores these settings:

- Toggled SLP off and changed both WSD timeouts (300→999) via WebConfig.
- Confirmed changes persisted in the UI after reboot.
- Full 64KB EEPROM re-dump showed **zero diff** from baseline — the only
  changed byte was `0x003A` (`0xB4`→`0xB5`), a boot/write counter.

The `||` EEPROM contains only main-domain hardware data: serial number
(`X2CL036077` at `0x00C0`), admin password (`admin` at `0x0191`), waste
ink counters, calibration. No WEBASSIST field names, no service-config
data.

### Timing caveat

The diff experiment changed settings and re-dumped EEPROM while `nwsoc`
was still running. If `nwsoc`'s flush mechanism writes to persistent
storage **only at shutdown** (the `NWControl` launcher handles return
codes 1=shutdown, 2=firmware-update reboot), the diff window may have
missed the write entirely. The settings persisted across a reboot,
which is consistent with a flush-on-exit model: `nwsoc` holds dirty
state in memory, flushes to storage on clean shutdown, then the
NWControl loop restarts it. Step 1 below (tracing the flush function)
would confirm or rule this out.

## Hypotheses to resolve the fork

### H1. WriteSetup blob goes to main-domain storage outside the `||` window

The main domain (ARM1136 RTOS) might have additional flash or EEPROM
beyond what `||` can address. The `||` command uses a 16-bit address
space (64KB), but the SoC could have:
- A larger EEPROM with a separate access window
- An SPI flash partition mapped outside the `||` range
- A dedicated NVRAM region in the CROM/IPL flash area
- A reserved sector of the same NOR/NAND that holds the firmware image

If this is the case, the COMIF WriteSetup blob crosses domains and lands
in storage that requires main-domain disassembly to locate.

### H2. Persistence is Linux-domain-local; COMIF is for sync, not storage

The COMIF WriteSetup path may exist so the main domain can **act on**
new settings (e.g. updating panel display state), while the actual
persistent store is on the Linux side. Both paths could be real but
serve different purposes. Candidates for Linux-domain persistence:
- A JFFS2 file not named in the obvious `rscbasic`/`rsceternal`/`rscncl`
  set (e.g. an `rscwebassist` equivalent)
- An MTD partition mounted at runtime (the fstab has a *commented-out*
  `/dev/mtdblock3 /opt/nwsoc_data jffs2` entry — the actual mount
  happens elsewhere)
- In-process memory flushed to a file or MTD device at shutdown
  (consistent with the timing caveat above)
- Storage via `epidc.ko`'s `ioctl` interface (shared memory that the
  main domain persists on its side)

If this hypothesis holds, Steps 1 and 4 (tracing the COMIF receiver)
become unnecessary — the answer is entirely within `nwsoc`.

### H3. Phase 0 followed the wrong branch entirely

The Phase 0 analysis may have misidentified the storage path. The
WriteSetup serializer at `0xbd750` might not be the code path used for
WEBASSIST items at all — there could be a separate vtable method or a
conditional branch earlier in the dispatch that routes WEBASSIST writes
differently.

### Constraint worth noting

The 154-entry COMIF field handler table (VA `0x57af94`, per
[PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md)) has Enable handlers for every other service but
**no TelnetEnable field**. Even if the blob crosses domains via COMIF,
there is no dedicated COMIF field for telnet, which constrains how the
write could propagate to the main domain — it must go as part of a
bulk WriteSetup blob, not as an individual field set.

## Concrete RE steps to resolve

### Step 1 — Trace `PNRSCBlockadeAndNvramFlush` (most targeted)

Debug string: `"PNRSCBlockadeAndNvramFlush() returns error. Some data may be lost."`
at file offset `0x5c3a8c` (VA `0x5cba8c`).

This is the periodic/shutdown flush mechanism. It directly answers
"which NVRAM regions get written where" — the most targeted single
function for resolving the storage question.

**Method:** Find pointer xrefs to `0x5cba8c` in the `.rodata`/`.text`
segment to locate the function that logs this string. Disassemble that
function's call graph: it should enumerate NVRAM regions (by name or
index) and call a write/flush on each. Look for:
- File paths passed to `fopen`/`open` (→ Linux-domain persistence, H2)
- D4/COMIF send calls (→ main-domain persistence, H1)
- Both (→ H2 with COMIF sync)

Also check whether this function is called from NWControl's shutdown
path (signal handler or return-code logic) — that would confirm the
flush-on-exit timing model.

### Step 2 — Trace the WriteSetup serializer's output path

The serializer at VA `0xbd750` builds the text blob. Where does that
blob go after serialization?

- **Sub-question A:** Does `PNCOMIFWriteSetup` (VA `0xe2378`) call into
  the D4/COMIF send path? If so, the blob crosses domains and the
  receiving code is in the ARM1136 RTOS binary (`ipl_0_seg1`), which
  has no ELF headers or symbols.
- **Sub-question B:** Or does it write locally first — to a JFFS2 file,
  MTD partition, or `/tmp` staging area — before (or instead of) sending
  over COMIF?

**Method:** Fully disassemble `0xe2378` and follow all branches down to
the actual I/O operation (`write`, `fwrite`, D4 send via
`D4CbtSendIdc`, or `ioctl` on `/dev/epidc0`). The function's call graph
should terminate at one of these.

### Step 3 — Catalog all file-write paths in `nwsoc`

Grep `nwsoc` for all `open()` / `fopen()` calls with writable flags and
catalog every file path it writes to. Cross-reference with:
- The JFFS2/MTD layout in [ROOTFS.md](../ROOTFS.md)
- The `mount` commands in init scripts (`rcS`, `S99NWControl`,
  `NWControl`)
- Any `mkfs.jffs2` or `flash_eraseall` calls

**Known writable paths (from prior analysis):**
- `/opt/nwsoc_data/rscbasic` — JFFS2, basic NVRAM
- `/opt/nwsoc_data/rsceternal` — JFFS2, eternal NVRAM
- `/opt/nwsoc_data/rscncl` — JFFS2, NCL
- `/opt/nwsoc_data/rscncl2` — JFFS2, NCL2
- `/tmp/wlan_tmp_ssid.dat` — Wi-Fi credentials (volatile)
- `/tmp/wlan_tmp_psk.dat` — Wi-Fi credentials (volatile)
- `/tmp/cert/ssl`, `/tmp/cert/dot1x`, `/tmp/cert/ipsec` — certificates

**Target:** any path not in this list that could be the WEBASSIST
store — e.g. `/opt/nwsoc_data/rscwebassist`, or a raw `/dev/mtd*`
write.

### Step 4 — Disassemble the ARM1136 RTOS COMIF receiver (if blob crosses domains)

Only needed if Steps 1–2 confirm the blob goes over COMIF/D4 and does
**not** persist locally. If H2 is confirmed (Linux-domain storage),
skip this step.

The storage code would be in `ipl_0_seg1` (ARM1136 RTOS, bare-metal,
no ELF, no symbols). Anchors:
- COMIF event strings: `COMIF_LEVT_WRITESETUP` (if present in the RTOS
  image — they may only be in `nwsoc`)
- The D4 MIB channel handler on the receiving side
- The `WriteSetup` dispatch (COMIF dispatch table maps
  `"writesetup"→4` on the `nwsoc` side; the main domain has the
  receiving counterpart)

This is the hardest step — bare-metal ARM1136 code, unknown base
address (likely `0x0` from the vector table), no section headers. The
RTOS binary is at `ipl_0_seg1` offsets `0x0`–`0x110400` (~1.1MB).

---

## Assessment vs. alternative approaches

| Approach | Effort | Risk | Outcome | Feasibility now |
|---|---|---|---|---|
| **This RE work** | High (days of disassembly, possibly ARM1136 bare-metal) | None (read-only analysis) | Exact storage location; reusable for any `_MS_` flag | Yes — all artifacts on disk |
| **Firmware mod** | Medium (toolchain exists, no crypto signing) | Low-medium (flash risk; IPL recovery mode documented) | Persistent telnet enable; full control | Yes — requires USB to printer |
| **WSD SOAP SetData** | Medium (SOAP tooling + WSD callback wiring) | Low | Bounded by same PNRSC subset — won't reach telnet | Yes — network only |
| **UART root shell** | Low (physical access + serial adapter) | None (empty root password, non-destructive) | Immediate shell; `find` the file directly | **No** — no USB serial converter, physical access difficult |

The UART path would shortcut this entire analysis but is currently
blocked on equipment and access. The static RE path is therefore the
primary avenue: it requires no hardware, no device interaction, and all
the necessary artifacts (`nwsoc`, `ipl_0_seg1`, extracted filesystems)
are already on disk.

If H2 is confirmed (Linux-domain storage), the telnet flag could
potentially be set via a **firmware mod** that patches the NVRAM file
in the SquashFS or adds a startup script to write it — leveraging the
existing `rcx_correct` + `epsflasher` toolchain and the absence of
cryptographic firmware signing.

---

## Companion docs

- [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md) — PNRSC internals, RE_PLAN execution results
  (Phases 0–2), runtime-enable matrix, 154-entry COMIF field handler
  table (no TelnetEnable)
- [NVRAM_ADDRESS_STATIC_RE.md](NVRAM_ADDRESS_STATIC_RE.md) — the original static RE plan (Phase 2
  blocked)
- [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) — empirical EEPROM-diff plan (Phase 5
  disproved)
- [TELNET_STATUS.md](TELNET_STATUS.md) — current status, remaining approaches
- [ARCHITECTURE.md](../ARCHITECTURE.md) — dual-domain SoC, EPIDC, D4,
  COMIF
- [ROOTFS.md](../ROOTFS.md) — SquashFS/MTD layout

</details>
