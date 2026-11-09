# Epson XP-442 — static RE of the telnet NVRAM address

**Goal:** Determine, purely from firmware disassembly, the exact NVRAM
storage location and mechanism for PNRSC `(cat=0x37, item=0xc2)`
= `TELNET_MS_CURRENT_ACTIVATION`. Complements the empirical EEPROM-diff
approach in [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md).

**Why this exists:** the RECON plan relies on the WEBASSIST region being in
the EEPROM byte-addressed store that `||` can reach. If PNRSC's WEBASSIST tier
persists to a JFFS2 file or some other backing store instead, the empirical
approach dead-ends and we need this one. Even if RECON succeeds, this plan
gives us **why** it worked and lets us predict where any other `_MS_` flag
lives.

**Everything in this plan is READ-ONLY** — it's static analysis of files
already on disk (`nwsoc`, `rcx_output/*`, `block1_streamA_web.bin`). The
device is never touched.

---

## Risk taxonomy (confidence, not side effects)

Since nothing touches the device, "risk" here is analytical:

- **HIGH-CONFIDENCE** — evidence chain is direct (concrete instructions,
  matched strings, unambiguous ARM decoding).
- **PLAUSIBLE** — inference from structure, could be wrong; needs
  triangulation before acting.
- **SPECULATIVE** — pattern-matching on limited evidence; document as a
  hypothesis, don't act on it alone.

Any action against the live device (Phase 5+ of [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md))
should only proceed on HIGH-CONFIDENCE findings from this plan.

---

## Tooling

Already available in this repo/environment (no new installs required):

- `objdump` (brew binutils) at `/opt/homebrew/opt/binutils/bin/objdump` —
  ARM/Thumb disassembly.
- `readelf` (same) — for `nwsoc`'s ELF layout (segments, sections).
- Python 3 with `struct` — for pointer scans, xref finding.
- Existing disassembly cache: `nwsoc.txt` in the scratchpad from earlier
  sessions.
- Existing docs: [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md), [ARCHITECTURE.md](../ARCHITECTURE.md),
  [ROOTFS.md](../ROOTFS.md) — reference material for what's already known.

Fallback if the analysis stalls on `nwsoc` alone: disassemble
[rcx_output/block1_streamA_web.bin](rcx_output/block1_streamA_web.bin) (the
ARM1136 main-domain code), and possibly [rcx_output/ipl_0_seg1](rcx_output/ipl_0_seg1)
(the RTOS boot core).

---

## Phase 0 — Determine which storage backend PNRSC uses

**Confidence needed to proceed: HIGH.** If this phase is wrong, every
downstream phase is wrong.

Two candidate backends, both plausible from what we already know:

1. **A dedicated EEPROM chip** accessed via memory-mapped I/O or a driver.
   This is what the `||` EPSON-CTRL command reaches, and is the assumption
   [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) makes.
2. **A JFFS2 flash file** under `/opt/nwsoc_data` or similar. The fstab
   ([squashfs_rootfs1/etc/fstab](squashfs_rootfs1/etc/fstab)) has a
   *commented-out* `/dev/mtdblock3 /opt/nwsoc_data jffs2` entry — the
   actual mount happens elsewhere at runtime.
3. **Data structures held in `nwsoc` process memory, flushed over D4 to
   main-domain NVRAM.** In this case the "NVRAM" the setter writes is on
   the ARM1136 side, reachable only via COMIF.

**Method:**

1. Grep `nwsoc` for storage-related strings and syscall targets:
   ```bash
   strings squashfs_rootfs2/nwsoc | \
     grep -iE '/dev/mtd|/dev/nvram|/opt/nwsoc_data|jffs2|\.dat$|NVRAM|nvram_|Flash' | sort -u
   ```
   Expect to see either a `/dev/mtd*` node (→ MTD flash), a filesystem path
   (→ JFFS2 file), or nothing external (→ D4-tunneled to main-domain, or
   memory-mapped).

2. Find xrefs to open/write/mmap for the discovered paths:
   ```bash
   # Example — adjust for actual strings found
   for s in "/dev/mtd" "/opt/nwsoc_data" "NVRAMWEBASSIST"; do
     python3 - <<PY
   import struct
   d = open("squashfs_rootfs2/nwsoc","rb").read()
   pat = b"$s"
   i = 0
   while True:
     j = d.find(pat, i)
     if j < 0: break
     va = j + 0x8000                    # nwsoc LOAD segment offset
     ptr = struct.pack('<I', va)
     refs = []
     k = 0
     while True:
       m = d.find(ptr, k)
       if m < 0: break
       refs.append(m + 0x8000)
       k = m + 1
     print(f"'{$s}' @ {va:#x}  xrefs: {[hex(r) for r in refs]}")
     i = j + 1
   PY
   done
   ```

3. Follow the xrefs into the surrounding functions and note the syscalls
   made near each:
   - `open` + `write` + `close` on a `/dev/mtd*` path → EEPROM/MTD backend
   - `fopen` + `fwrite` on `/opt/nwsoc_data/*` → JFFS2 file backend
   - `bl PNCOMIFSetField` / D4 channel writes → main-domain backend

**Success criteria:** unambiguous evidence of which backend PNRSC's
setters use. Record the finding here or in a scratch note.

**STOP if:** multiple backends look plausible and evidence is thin. Widen
the search: look at `PNRSCBaseStart` (init function), and its data
allocations. `NVRAMWEBASSIST` in particular — the name suggests a discrete
region; if it's an in-memory buffer periodically flushed, find the flush
site (`PNRSCBlockadeAndNvramFlush` is the log-string spelling).

**Downstream branching:**

- **EEPROM backend confirmed:** proceed with Phases 1–4 below, then hand
  off to [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) Phase 7 with the exact address.
- **JFFS2 file backend confirmed:** [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) is a dead-end.
  Pivot to the UART or flash routes — you can edit the file directly with
  root over UART, or bake the change into a modified rootfs.
- **Main-domain backend confirmed:** we need to disassemble stream A (the
  ARM1136 side) instead of `nwsoc`. Restart Phase 1 there.

---

## Phase 1 — Locate the generic PNRSC setter's entry point

**Confidence: HIGH — landmarks are already known.**

From [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md) we already have concrete anchor points inside
`nwsoc`:

- Debug string `"%d PNRSCBaseSetU32(Subject=%d, Item=%d)"` @ VA `0x5c5ff8`
  — logged from within the setter at VA `0x321e4`.
- Debug string `"%d PNRSCBaseSetS32(Subject=%d, Item=%d)"` @ VA `0x5c5f74`
  — logged at VAs `0x31cec` / `0x3213c`.
- Category ID→name table @ VA `0x655c10`+ (`0x37` → `TELNET`).
- Item ID→name table @ VA `0x656480`+ (`0xc0`→`WEBASSIST_REBOOT_WAIT_TIME`,
  `0xc1`→`TELNET_MS_SUPPORT`, `0xc2`→`TELNET_MS_CURRENT_ACTIVATION`).

**Method:**

1. Walk backward from `0x321e4` to find the enclosing function's `push`
   prologue (same technique used for `0x2c5608` in the IMPORT-endpoint
   analysis).
2. Identify how `(Subject, Item, value)` become the arguments to the
   underlying storage write. Given C++ vtable dispatch, expect:
   ```
   push ...                          # entry
   ...                                # arg validation, logging
   ldr rN, [this, #vtable_off]       # load vtable
   ldr r12, [rN, #method_off]        # load setter method ptr
   blx r12                           # dispatch to concrete storage class
   ```
   The `this` pointer likely comes from a global (a singleton PNRSC store).

3. Follow the `blx` target statically: for each candidate concrete storage
   class, find its class name via RTTI / vtable-adjacent strings if
   present, otherwise by grepping xrefs from the vtable.

**Deliverable:** address and shape of the concrete storage function called
by `PNRSCBaseSetU32`. Ideally with a comment `bl <addr> // PNRSCStoreImpl::set(subject, item, value)`.

**STOP if:** vtable dispatch resolves to more than one target (multiple
storage implementations picked by config). Enumerate them; the one
relevant to WEBASSIST is likely the one that references `NVRAMWEBASSIST`.

---

## Phase 2 — Find the (subject, item) → NVRAM offset mapping

**Confidence: PLAUSIBLE — pattern varies per PNRSC implementation style.**

Three canonical layouts to check for, in order of likelihood:

**Layout A: per-subject block with items at fixed offsets.**
Each category (subject) has a contiguous NVRAM region; items within the
category are stored at deterministic offsets. Look for:
- A table indexed by subject ID that gives `{nvram_base, size}` per category.
- Inside the setter: `store_addr = per_subject_base[subject] + item_offset[item]`
  or `per_subject_base[subject] + item_index * item_size`.

**Layout B: flat (subject, item) → offset table.**
A single global array of records like `{subject, item, offset, type, size}`.
The setter binary-searches or linear-scans for the matching tuple, then
writes to `base + offset`. Look for tables containing the small integers
`0x37`, `0xc0`, `0xc1`, `0xc2` in close proximity, especially adjacent to
larger 16-bit or 32-bit fields (offsets/sizes).

**Layout C: hash-based / dictionary storage.**
The setter hashes `(subject << 8) | item` and looks up an entry in a
hashmap. Harder to reverse — you'd need to identify the hash function and
walk the hashmap contents.

**Method:**

1. In the disassembly of the concrete setter (from Phase 1), identify what
   is done with the `subject` and `item` argument registers between entry
   and the actual store instruction.

2. If Layout A: dump the "per-subject base" table. The row for subject
   `0x37` gives WEBASSIST-tier base. Then find the item-offset table (or
   item-size constant) to compute address for item `0xc2`.

3. If Layout B: search the file for a byte sequence containing
   `0x37 <padding> 0xc0` or `0x37 <padding> 0xc2` (accounting for endianness
   and struct alignment). A hit inside `.data`/`.rodata` with adjacent
   16- or 32-bit numeric-looking values is the table.

4. If Layout C: identify the hash by observing the arithmetic done on
   `subject`/`item`. This is the hardest branch and may take longer than
   the empirical approach in NVRAM_EEPROM_RECON.md.

**Anchor cross-check:** whichever layout, use the known live item
`WEBASSIST_REBOOT_WAIT_TIME` (`0xc0`) as your ground truth. This item
is exposed by the WebConfig admin UI (the "Reboot wait time" field), so
we can independently confirm the current value from the web UI to sanity-
check the layout.

**STOP if:** the layout is Layout C and the hash function isn't obvious
after 30 minutes. Fall back to the empirical diff experiment in
[NVRAM_EEPROM_RECON.md § Phase 5](NVRAM_EEPROM_RECON.md).

---

## Phase 3 — Compute the concrete NVRAM address for `(0x37, 0xc2)`

**Confidence: HIGH once Phase 2 gives us the layout.**

Given the layout from Phase 2:

- **Layout A:** `addr = base_of_subject[0x37] + item_offset[0xc2]`.
  Also compute `addr` for items `0xc0` (WEBASSIST_REBOOT_WAIT_TIME) and
  `0xc1` (TELNET_MS_SUPPORT) as sanity references.

- **Layout B:** the row for `(0x37, 0xc2)` in the table directly contains
  the offset. Extract it.

- **Layout C:** simulate the hash for `(0x37, 0xc2)` and walk the
  hashmap statically.

**Cross-verification:**

1. Locate any code that **reads** `TELNET_MS_CURRENT_ACTIVATION` (search
   for `PNRSCGetBool` callers that pass `subject=0x37`, `item=0xc2`).
   Should resolve to the same NVRAM address.

2. Compute where item `0xc0` (`WEBASSIST_REBOOT_WAIT_TIME`) lives.
   Optionally read that address via [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) Phase 3's dump
   → cross-check the value matches what WebConfig reports for "Reboot
   wait time". If it does, the layout is confirmed and we're good.

**Deliverable:** a single hex address (0–65535, since `||` addresses are
16-bit) with high confidence.

---

## Phase 4 — Determine the value semantics

**Confidence: PLAUSIBLE — needs one more code trace.**

Not every "flag" is a single 0/1 byte. Before writing, confirm:

- **Width:** 1 byte, 2 bytes, or 4 bytes? Look at the setter's store
  instruction (`strb`, `strh`, or `str`).
- **Encoding:** `1` = enabled, or some other magic value? Look at the
  consumer — the code path that starts the telnet listener when the flag
  is set. Confirm it does `if (val == 1)` or `if (val != 0)`.
- **Checksum/parity:** does the WEBASSIST region have an integrity byte?
  If yes, we'd need to update it too. Look for a checksum function called
  after every setter.

**Method:**

1. Find `PNRSCGetBool` callers with `(0x37, 0xc2)` args — find the
   telnet-listener startup code and read its check.
2. Look for `PNRSCBlockadeAndNvramFlush` — see what regions it flushes
   and whether it computes any checksum in the process.

**STOP if:** the flag is not a single byte, or there's a checksum we
can't reproduce. Either update the plan (write multiple bytes atomically),
or fall back to another route.

---

## Phase 5 — Hand off to NVRAM_EEPROM_RECON.md Phase 7 with a specific target

Once Phases 0–4 give us `(address, width, value_to_write, any_checksum_updates)`,
the write itself is a single command:

```bash
python epson_print_conf.py --model XP-432 -a scanner.lan -W "<ADDR>: <VALUE>"
```

Then power-cycle and verify per [NVRAM_EEPROM_RECON.md § Phase 7](NVRAM_EEPROM_RECON.md).

**The static-RE path skips the empirical diff experiment (RECON Phase 5)**,
because we already know the address analytically. It still needs
[NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) Phases 0–4 (environment, transport, read-key,
baseline, write-key) — those are prerequisites for any write regardless of
how the address was derived.

---

## Success / stop criteria per phase

| Phase | Success | Stop-and-investigate | Status |
|---|---|---|---|
| 0 | Storage backend identified unambiguously | Multiple plausible backends; evidence thin | **DONE** — EEPROM via COMIF WriteSetup |
| 1 | Concrete storage-class setter located | vtable dispatch too dynamic to resolve statically | **DONE** — PLeoNVRamDB vtable[12] |
| 2 | Layout A / B / C identified with the ID table | Layout C with non-obvious hash — fall back to RECON | **STOPPED** — TELNET items absent from descriptor table; blob format is self-describing |
| 3 | Single hex address computed with cross-verification | Reads and writes resolve to different addresses | Not started (blocked by Phase 2) |
| 4 | `(width, encoding, checksum-behaviour)` known | Multi-byte flag or unrecoverable checksum | Not started |
| 5 | Handoff to NVRAM_EEPROM_RECON.md Phase 7 | — | Not started |

---

## Two things that would kill this plan cleanly

- **The storage backend is on the ARM1136 main-domain.** Then `nwsoc`'s
  setter is just a marshalling stub that sends `(subject, item, value)`
  over D4/COMIF, and the real storage code is in
  [rcx_output/block1_streamA_web.bin](rcx_output/block1_streamA_web.bin)
  (which is already partially reversed for the IMPORT endpoint — see
  [PNRSC_CONFIG_REGISTRY.md § The IMPORT endpoint](../PNRSC_CONFIG_REGISTRY.md)). Restart Phase 1 there.
- **The storage backend isn't byte-addressable via `||`.** JFFS2 files
  don't respond to EEPROM commands. In this case the static-RE branch
  still yields useful understanding (we know where the flag lives), but
  the enable-write itself has to go through UART root shell or a firmware
  mod. Both are documented options in [PNRSC_CONFIG_REGISTRY.md § Remaining options](../PNRSC_CONFIG_REGISTRY.md).

---

## Interaction with the empirical NVRAM_EEPROM_RECON.md plan

The two plans are **complementary**, not competitive. Pick based on which
resource you have more of:

- **Time to reverse ARM code:** go RE-first. Deterministic, doesn't touch
  the device, and if it succeeds you skip RECON's diff experiment.
- **Time to run experiments against the printer:** go RECON-first. The
  EEPROM-diff approach can localise the WEBASSIST region in one round-trip
  without any disassembly.

Best-of-both-worlds sequence:
1. Do [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) Phases 0–3 (setup, transport, keys, baseline dump).
   These give you a full 64 KB snapshot of NVRAM to hunt inside statically.
2. Do this plan's Phase 0 in parallel — cheap and decisive.
3. If Phase 0 says "EEPROM backend": this plan Phases 1–4 pin the exact
   address. RECON's Phase 5 diff experiment then becomes a *verification*
   (change WEBASSIST_REBOOT_WAIT_TIME through the UI, confirm the byte at
   the address we RE'd changes). Once verified, RECON Phase 7 does the
   write.
4. If Phase 0 says "JFFS2 / main-domain / other": abandon NVRAM_EEPROM_RECON.md and
   pick a different route.

---

## Companion docs

- [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) — the empirical EEPROM-diff plan; the natural
  next step after this static-RE work identifies the address.
- [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md) — everything already known about PNRSC internals,
  the ID tables, the C++ vtable dispatch, the IMPORT-endpoint RE (methodology
  reference).
- [ARCHITECTURE.md](../ARCHITECTURE.md) — dual-domain SoC, D4/COMIF (context
  for the "main-domain backend" branch).
- [ROOTFS.md](../ROOTFS.md) — squashfs / mtd layout (context for the
  "JFFS2 backend" branch).
