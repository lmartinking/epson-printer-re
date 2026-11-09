# Epson XP-442 — Safe EEPROM Recon Plan

**Goal:** Discover the NVRAM byte address of `TELNET_MS_CURRENT_ACTIVATION`
(PNRSC cat `0x37`, item `0xc2`) via the `||` (EEPROM byte access) command over
EPSON-CTRL, **without** changing device state in any way that would need front-
panel recovery. That address is the only known remaining opening for enabling
telnet without flashing (see [PNRSC_CONFIG_REGISTRY.md § D4 EPSON-CTRL — prior art](../PNRSC_CONFIG_REGISTRY.md)).

Uses the [`epson_print_conf`](epson_print_conf/) tool (submodule).

**Test target:** `scanner.lan` (mDNS-stable; DHCP address may vary — see
[NETWORK_SERVICES.md](../NETWORK_SERVICES.md)).

**Model in the tool's config:** none for XP-442 specifically. Nearest siblings:
XP-422 (`Muscari.`), XP-432 (`Polyxena`), **XP-435** (aliased under XP-432 —
closest numeric match), XP-452 (`Suramadu`).

---

## Risk taxonomy used below

Every step is labelled with its **side-effect risk**:

- **READ-ONLY** — no bytes leave the host that could change device state. Safe
  to interrupt at any time.
- **SCRATCH-WRITE** — writes one byte to EEPROM as a probe, then restores it.
  The tool's own code retries the restore up to 3 times and logs the exact
  recovery command if it fails. Risk is small but non-zero. **Never run this
  on an EEPROM cell whose value we don't already have from a fresh read**.
- **STATE-CHANGE** — persistent change to device configuration. Requires a
  documented baseline and a rollback path.

Also carrying forward the hard-earned rule from [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md):
**never touch a `/SET` web endpoint without a full baseline of its `/TOP`
form fields**. That rule cost us Wi-Fi credentials once already.

---

## Phase 0 — Environment setup (one-time, on the Mac) ✅ DONE

**Risk:** none (host-side only).
**Outcome (2026-09-26):** Dependencies installed (system Python 3.9, pysnmp, pyyaml, etc.).

1. Confirm Homebrew Python and libusb are present:
   ```bash
   /opt/homebrew/bin/python3 --version    # expect ≥ 3.10
   brew list libusb                        # expect libusb present
   ```

2. Install `epson_print_conf` dependencies into a venv (keeps system Python clean):
   ```bash
   cd epson_print_conf
   /opt/homebrew/bin/python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   # If epson-usb refuses on 3.9-style install, force it (README-approved):
   pip install --ignore-requires-python epson-usb
   ```

3. Sanity-check the tool loads and lists models:
   ```bash
   python epson_print_conf.py -m '?'
   ```
   Expect a list of known Epson models. **XP-442 will not be listed** — that's
   expected; we'll be operating in "detect keys" mode.

4. **Baseline the device state** before touching anything (recovery insurance):
   - Log into WebConfig at `https://scanner.lan/PRESENTATION/PSWD` with
     `session=admin`.
   - Screenshot every `.../TOP` page under `/PRESENTATION/ADVANCED/*` —
     specifically `INFO_PRTINFO`, `INFO_NWINFO`, `INFO_BEHAVIORINFO`,
     `NW_WIFI/TOP`, `NW_BASIC/TOP`, `NW_SERVICE_PRTCL/TOP`,
     `NW_SERVICE_WIFID/TOP`, `NW_SERVICE_MSNW/TOP`, `NW_SERVICE_NWSCAN/TOP`,
     `NWS_SSLTLS/TOP`, `ADMIN_CTINFO/TOP`.
   - This gives us the ground-truth state to compare against and the exact
     form fields to POST back if anything gets clobbered.

---

## Phase 1 — Transport check (READ-ONLY) ✅ DONE

**Risk:** READ-ONLY. Only pulls model metadata via well-known SNMP OIDs.
**Outcome (2026-09-26):** Printer reachable at scanner.lan (192.168.1.238) via SNMP.

The point of this phase: confirm the tool can reach the device and speak
EPSON-CTRL over SNMP. This works even without knowing the model's `read_key`
because model detection uses public OIDs (see `MIB_INFO` at
[epson_print_conf.py:992](epson_print_conf/epson_print_conf.py)).

```bash
# Info-only, unknown model is fine here (--model XP-432 is a reasonable stand-in;
# only its MIB_INFO reads run, not any key-gated operations).
python epson_print_conf.py --model XP-432 -a scanner.lan -i
```

**Expected:** Product Status, Network Status, firmware version, MAC, serial,
IP — matching what NETWORK_SERVICES.md documents. **No EEPROM reads should happen at
this stage** (they'd all fail with the wrong key anyway).

**STOP if:** no response, or SNMP times out. That means the transport is
broken and everything downstream is meaningless. Confirm `ping scanner.lan`
and `snmpget -v1 -c public scanner.lan sysDescr.0` before moving on.

---

## Phase 2 — Read-key detection (READ-ONLY) ✅ DONE

**Risk:** READ-ONLY. This is the safest key operation: it iterates all 65,536
**Outcome (2026-09-26):** read_key = **[1, 5]** (same as XP-342). Found on
attempt 6 of a narrow search (second byte 0–10, covering known XP-4xx patterns).

candidate `(byte, byte)` read_keys and sends an EEPROM read of address `0x00`
with each. A wrong key gets a refusal packet; the correct key returns an
`EE:0000VV;` reply.

```bash
python epson_print_conf.py --model XP-432 -a scanner.lan --detect-key
```

**Expected on the SNMP path:** the tool will iterate; per the README's own
warning, "the printer's agent stops answering after a few hundred closely
spaced requests" over SNMP. If it stalls, the tool waits, backs off, and
resumes. Full scan may take longer than the ~10-second USB path.

**Expected outcome:**
- A key like `[133, 5]` (if it matches XP-432) or something else entirely.
- The tool prints the detected `read_key` on success.
- If it fails with "could not detect read_key", the printer's model may be in
  the "known incompatible" list — check that section of the README.

**Record the read_key** in this file (or in a scratch note) before continuing.

---

## Phase 3 — Baseline EEPROM dump (READ-ONLY) ✅ DONE

**Risk:** READ-ONLY. Reads the entire 64 KB address space and writes it to a
local file. The tool sends 65,536 `||` reads back-to-back.
**Outcome (2026-09-26):** Full 65,536-byte dump completed. Saved as
`eeprom_baseline.txt`. Only 3 ASCII strings found: serial number (`X2CL036077`
at 0x00C0), admin password (`admin` at 0x0191), `%%%%%` at 0x01B5. No
WEBASSIST field names, no service-config data — this EEPROM is purely
main-domain hardware config.

Once we have a valid `read_key`, dump the whole EEPROM:

```bash
# Save the discovered key so future runs skip the scan. Method depends on the
# tool — either -c CONFIG_FILE (loads a saved dump) or the pickle path.
python epson_print_conf.py --model XP-432 -a scanner.lan \
  --eeprom-dump 0 65535 > /tmp/eeprom_baseline_$(date +%s).bin

# Also save a hex/decimal listing for diffing later:
python epson_print_conf.py --model XP-432 -a scanner.lan \
  --eeprom-dump 0 65535 --debug 2>&1 | tee /tmp/eeprom_baseline.log
```

**Notes:**
- Same SNMP-throttling caveat applies; expect this to take several minutes at
  minimum.
- **Save the dump.** This is the ground truth for the diff experiment in
  Phase 5, and is a full backup of the printer's config if we ever have to
  restore it byte-for-byte via `-W ADDRESS: VALUE` writes.

**STOP if:** the dump contains large runs of `None` / read failures beyond a
few percent — the key may be wrong, or the transport is dropping. Retry with
`-r` (retries) and `-t` (timeout) bumped up.

---

## Phase 4 — Write-key detection (SCRATCH-WRITE — the first real risk) ⏭ SKIPPED

**Risk:** SCRATCH-WRITE. This is the point where the tool actually mutates a
byte to validate a candidate key. Per the source
([`validate_write_key`](epson_print_conf/epson_print_conf.py) at line 3014):
it writes `value + 1` to a scratch address, reads it back, then writes the
original `value` back and verifies. It retries the restore 3 times and, on
failure, prints the exact `-W` command to run manually.

**Preconditions before running:**
- Phase 3 dump saved locally.
- Know the exact starting byte value at the scratch address.
- Have `-W` command syntax ready to restore that byte if the tool leaves it
  incremented.

**Recommended safer variant:** rather than let the tool pick a scratch
address, point it explicitly at an EEPROM cell we know is a **counter that
already changes over time** (waste-ink counters, maintenance counters — the
`raw_waste_reset` addresses like 46 or 47 in the XP-432 model config). These
cells:
- Have a known "safe" value at rest (from Phase 3 dump).
- Change frequently as part of normal operation, so a temporary off-by-one
  can't do lasting harm.

Try the write-keys in this order (based on family proximity to XP-442):

| Priority | Try | Because |
|---|---|---|
| 1 | `b"Polyxena"` | XP-432/XP-435 (aliased) — closest sibling |
| 2 | `b"Suramadu"` | XP-452/XP-455 — next-closest |
| 3 | `b"Muscari."` | XP-422 family |
| 4 | Full `write_key_list()` (all keys in `PRINTER_CONFIG`) | Fallback |

The tool automates this if you call the detection function directly from the
GUI or Python API. From the CLI, `--dry-run` shows the write it would send
without committing.

**STOP if:** after any candidate:
- The read-back does NOT equal `value + 1` → this candidate is wrong, tool
  will try the next.
- The restore fails 3× → the tool prints the recovery `-W` command. **Run
  it immediately** before moving on. Do NOT retry other candidates until the
  cell is back to baseline.

**Record the discovered write_key** here or in a scratch note.

---

## Phase 5 — EEPROM diff experiment to locate the WEBASSIST NVRAM region (STATE-CHANGE — via WebConfig UI only) ❌ DISPROVED

**Risk:** STATE-CHANGE, but through the official web admin, not raw EEPROM,
so it's the intended user-facing path (as long as we baseline first).

**Idea:** we know from [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md) that `TELNET_MS_CURRENT_ACTIVATION`
(item `0xc2`) sits immediately after `WEBASSIST_REBOOT_WAIT_TIME` (item `0xc0`)
in the PNRSC ID table. If NVRAM layout follows ID order, changing
`WEBASSIST_REBOOT_WAIT_TIME` through the WebConfig UI will flip a byte at a
NVRAM address near where telnet's flag lives.

**Outcome (2026-09-26): DISPROVED.** Toggled SLP off and changed both WSD
timeouts (300→999) via WebConfig, confirmed changes persisted in the UI after
reboot. Full 64KB EEPROM re-dump showed **zero diff** from baseline — the only
changed byte was 0x003A (0xB4→0xB5), a boot/write counter unrelated to
service config.

**Conclusion:** The `||`-accessible EEPROM is the main-domain (ARM1136 RTOS)
hardware EEPROM only. The NVRAMWEBASSIST blob — containing all WEBASSIST-tier
service settings including telnet — is stored in a **separate backend** in the
Linux domain (likely JFFS2 or a different NVRAM partition), inaccessible via
the `||` command. The diff-based approach cannot work via this channel.

See [TELNET_STATUS.md](TELNET_STATUS.md) for remaining approaches.

**Procedure:**

1. Baseline: keep the Phase 3 dump. Note the current value of
   `WEBASSIST_REBOOT_WAIT_TIME` (the "Reboot wait time" field — hunt in the
   WebConfig admin UI's advanced Web/HTTP settings page; if present, likely
   at `/PRESENTATION/ADVANCED/NW_...` — enumerate CAUTIOUSLY, GET on `/TOP`
   only).

2. Fetch the `/TOP` page of whichever admin form exposes this setting, and
   record every `INPUTC_*` / `INPUTT_*` / `INPUTR_*` field's current value.
   This is our safety belt against the Wi-Fi-wipe scenario.

3. POST back **the full form** with only `WEBASSIST_REBOOT_WAIT_TIME`
   changed to a distinct new value (e.g. increment it by 10 seconds). This is
   the standard user action — WebConfig applies it via `PNCOMIFSetField` and
   PNRSC in the usual way, storing the byte to NVRAM.

4. Second EEPROM dump:
   ```bash
   python epson_print_conf.py --model XP-432 -a scanner.lan \
     --eeprom-dump 0 65535 > /tmp/eeprom_after_$(date +%s).bin
   ```

5. Diff the two dumps:
   ```bash
   cmp -l /tmp/eeprom_baseline_*.bin /tmp/eeprom_after_*.bin | head -40
   ```
   Expect just a handful of changed bytes:
   - The `WEBASSIST_REBOOT_WAIT_TIME` byte(s) we intended to change.
   - Possibly a nearby checksum or update-count.
   - Nothing else stable across the interval.

6. **Restore** `WEBASSIST_REBOOT_WAIT_TIME` back to its original value via
   WebConfig (full form POST again). Third dump, cmp again — should now
   match the baseline (modulo any counters that legitimately drift).

**What we learn:** the physical NVRAM address of a known WEBASSIST-tier item.
Then, since `WEBASSIST_REBOOT_WAIT_TIME` = item `0xc0` and telnet's flag =
item `0xc2`, telnet's byte should be **a small, positive offset** from the
WEBASSIST address in NVRAM — probably at `WEBASSIST_ADDR + N` for some small
N (1, 2, 4, or the WEBASSIST item's struct-size, depending on how PNRSC
lays out its store).

**STOP if:**
- The diff shows more than ~10 changed bytes — some other subsystem is also
  writing NVRAM concurrently, and we can't attribute changes cleanly. Wait
  a minute, retry the diff, or find a quieter moment.
- The restore doesn't return the baseline. Do NOT proceed — investigate
  what else changed.

---

## Phase 6 — Identify the telnet byte candidate (READ-ONLY analysis) ⛔ BLOCKED (Phase 5 disproved)

**Risk:** READ-ONLY. All analysis on locally-saved dumps.

From Phase 5 we know a WEBASSIST NVRAM address. Now:

1. **Read** (via `-R`, not write) the byte at that address ±16, ±32, ±64 to
   see the WEBASSIST region's shape. The current value of the telnet byte
   should be `0` (feature disabled — that's exactly what we've established
   through every other channel).

2. Cross-reference with the two static clues we already have from
   [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md):
   - The PNRSC ID table's byte layout may hint at NVRAM layout — items
     `0xbe` PACFILE, `0xbf` PACFILE_DATA_TYPE, `0xc0` WEBASSIST_REBOOT_WAIT_TIME,
     `0xc1` TELNET_MS_SUPPORT, `0xc2` TELNET_MS_CURRENT_ACTIVATION.
   - `nwsoc` calls `PNRSCBaseSetU32(Subject=0x37, Item=0xc2, …)` — if we
     were to statically RE the PNRSC storage backend, we'd find the exact
     NVRAM address for cat/item pairs. That's the RE-branch alternative to
     this diff experiment.

3. **Do NOT write yet.** Phase 6 concludes with a *candidate* NVRAM address
   for the telnet byte, backed by evidence (position adjacent to a known
   WEBASSIST byte, current value `0` matching the feature-disabled state).

**Only if the candidate is high-confidence** (adjacent, correct default,
consistent with the neighbouring items' shapes) does Phase 7 become
sensible. Otherwise: iterate — flip a different known WEBASSIST-adjacent
item, diff again, refine the layout hypothesis.

---

## Phase 7 — The actual telnet write (STATE-CHANGE — this is the goal) ⛔ BLOCKED (Phase 5 disproved)

**Risk:** STATE-CHANGE, persistent (survives reboot). Only run this after
Phase 6 has produced a high-confidence candidate address.

```bash
# For value=1 at candidate address ADDR:
python epson_print_conf.py --model XP-432 -a scanner.lan \
  --dry-run -W "ADDR: 1"    # preview
python epson_print_conf.py --model XP-432 -a scanner.lan \
  -W "ADDR: 1"              # commit
```

**Verify:**
- Immediate read-back at ADDR shows `1`.
- Second read of nearby bytes: nothing unexpected changed.
- **Power-cycle the printer** — the telnet service task in `nwsoc` most
  likely reads its enable flag from NVRAM at boot only, so the change
  takes effect only after a restart.
- After reboot: `nmap -p 23 scanner.lan` — expect port 23 open if the
  hypothesis is right, closed if not (in which case flip ADDR back and
  either the candidate was wrong, or the flag isn't stored where we think).

**Rollback (always available):**
```bash
python epson_print_conf.py --model XP-432 -a scanner.lan -W "ADDR: 0"
# power-cycle again
```

**STOP if:**
- Write is refused → wrong write_key (unexpected at this point; investigate).
- Read-back doesn't match → serious problem, restore from Phase 3 dump if
  you have to.
- Post-reboot the printer misbehaves → immediately write `0` back and
  power-cycle. If that doesn't recover, the printer's IPL recovery mode
  (documented in [FIRMWARE_LAYOUT.md § Recovery Modes](../FIRMWARE_LAYOUT.md)) is the escape
  hatch.

---

## Success / stop criteria per phase

| Phase | Success | Stop-and-investigate |
|---|---|---|
| 0 | venv + libusb work | any install failure |
| 1 | model info returned via SNMP | timeout or garbled response |
| 2 | `read_key` printed | "could not detect" after full scan |
| 3 | full 64 KB dump saved | >1% failed reads |
| 4 | `write_key` printed | scratch byte cannot be restored → immediate manual `-W` |
| 5 | small clean diff of dumps around a WEBASSIST byte | large or noisy diff, or restore doesn't match baseline |
| 6 | high-confidence candidate address with `value=0` today | ambiguous layout — iterate with more WEBASSIST diffs |
| 7 | port 23 open after reboot | anything unexpected — roll back |

---

## What this plan explicitly does NOT do

- Does not GET or POST any `/SET` web endpoint blindly (the Wi-Fi wipe rule
  from [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md)).
- Does not modify the firmware image (that's the flash route — a separate
  option in [PNRSC_CONFIG_REGISTRY.md § Remaining options](../PNRSC_CONFIG_REGISTRY.md)).
- Does not require physical USB access unless SNMP proves too slow — the
  same operations work over both transports (per the
  [D4 EPSON-CTRL prior-art section in PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md)).
- Does not try to reach telnet via WebConfig / ENPC / WSD SOAP — all three
  have been ruled out (see the runtime-enable matrix in PNRSC_CONFIG_REGISTRY.md).

## What could kill this plan cleanly

- **The telnet byte isn't in the same NVRAM region as WEBASSIST.** Then the
  Phase 5–6 diff strategy doesn't localise it, and we fall back to static
  RE of `nwsoc`'s PNRSC storage layer to find the address directly.
- **The service task doesn't read the NVRAM byte at boot** (e.g. reads it
  only when told to by a control command over D4 that we can't send). Then
  the write succeeds and the byte stays flipped, but no telnet listener
  starts. That'd be diagnosable from `nmap -p 23` staying closed after
  reboot; recovery is `-W "ADDR: 0"` and move on to the flash route or the
  UART route.

## Companion docs

- [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md) — PNRSC registry internals, runtime-enable matrix,
  D4 EPSON-CTRL prior art (this plan's rationale).
- [ARCHITECTURE.md](../ARCHITECTURE.md) — dual-domain SoC, why NVRAM writes
  need a power-cycle.
- [NETWORK_SERVICES.md](../NETWORK_SERVICES.md) — hostname `scanner.lan`, port map.
- [FIRMWARE_LAYOUT.md](../FIRMWARE_LAYOUT.md) — IPL recovery modes (last-resort escape hatch).
