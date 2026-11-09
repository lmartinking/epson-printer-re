# Status — Enabling Telnet on the Epson XP-442

## Current status

> **🔑 There is no telnet server in this firmware.** The `TELNET_MS_*` PNRSC IDs
> are vestigial — they gate a listener that is not in the build. Every TCP
> listener in `nwsoc` is accounted for and none is on port 23; the telnet strings
> have zero code xrefs; the Linux rootfs `busybox` has no `telnetd`. Enabling
> telnet requires **adding a server**, not setting a flag. Details:
> [NETWORK_SERVICES.md § No telnet server exists](../NETWORK_SERVICES.md).
>
> **The goal is a remote root shell.** Root has an **empty password** and a getty
> runs on `ttyS0` (115200 8N1), so a USB-serial adapter gives root with no
> flashing; or add a network shell via busybox `inetd` (compiled in) with a small
> rootfs edit + reflash. See [NETWORK_SERVICES.md § Getting a shell](../NETWORK_SERVICES.md).

RECON Phases 0–3 complete, **Phase 5 disproved**: the `||` EEPROM does not
store WEBASSIST/network service settings. The telnet flag is not reachable
via `epson_print_conf`'s `||` byte-access command.

**Storage backend RESOLVED (2026-09-26)** via deeper static RE — see
[NVRAMWEBASSIST_RE.md](NVRAMWEBASSIST_RE.md#execution-results-2026-09-26).
The NVRAMWEBASSIST blob (incl. `TELNET_MS_CURRENT_ACTIVATION`) lives in the
**main domain's authoritative NVRAM**, reached only across the domain
boundary via COMIF → D4 (MIB channel) → EPIDC (`ioctl` on `/dev/epidc0`).
It is **not** in any Linux file, **not** in raw MTD, and **not** in the
`||`-accessible 64 KB hardware EEPROM. `PLeoNVRamDB` is only the 4-file
JFFS2 manager (basic/eternal/ncl/ncl2) and never touches WEBASSIST.
Consequence: with no TelnetEnable COMIF field and no telnet descriptor
entry, the flag is unreachable via any remote write interface
(ENPC/WSD/COMIF). Only firmware mod or an on-device path that drives PNRSC
remains.

**Write path (Ghidra):** the cross-domain `WriteSetup` writer's only caller is
`prvBackupTask`, which ships a bulk base64 **`SetupData`** backup of the config
image — not per-field service writes — so no code path names telnet to
WriteSetup. Full trace + function names in
[NVRAMWEBASSIST_RE.md](NVRAMWEBASSIST_RE.md#step-4--ghidra-decompilation-trace-2026-09-28-writesetup-is-backup-only).

## What the static RE proved

| Finding | Confidence | Detail |
|---------|-----------|--------|
| Storage backend is EEPROM | HIGH | NVRAMWEBASSIST has no JFFS2 file; WriteSetup sends a blob via COMIF to main-domain EEPROM |
| `\|\|` command reaches the same chip | HIGH | Same physical EEPROM, 16-bit byte addressing, read/write supported |
| TELNET_MS_CURRENT_ACTIVATION = PNRSC `(0x37, 0xc2)` | HIGH | Verified against both name tables and descriptor table cross-refs |
| Setter path: `PNRSCBaseSetU32` → PLeoNVRamDB vtable[12] | HIGH | VA `0x3214c`, single global instance at `0x695de4` |
| No web UI or COMIF field for telnet | HIGH | 154-entry COMIF field handler table has Enable entries for every other service but not telnet |
| TELNET items absent from the per-item descriptor table | HIGH | 724 entries; Subject `0x37` completely missing (jumps `0x36`→`0x38`) |

## What the EEPROM recon proved (2026-09-26)

| Finding | Confidence | Detail |
|---------|-----------|--------|
| XP-442 read_key is `[1, 5]` | HIGH | Brute-forced via SNMP; matches XP-342 |
| `\|\|` EEPROM is 64 KB (0x0000–0xFFFF) | HIGH | Full dump completed, no all-None truncation |
| `\|\|` EEPROM contains only main-domain hardware data | HIGH | Only 3 ASCII strings found: serial `X2CL036077` at 0x00C0, `admin` at 0x0191, `%%%%%` at 0x01B5 |
| Network/service config is NOT in `\|\|` EEPROM | **HIGH** | Toggling SLP off + changing WSD timeouts (300→999) via WebConfig produced **zero diff** in a full 64KB dump. The only changed byte was 0x003A (0xB4→0xB5) — a boot/write counter |
| NVRAMWEBASSIST blob lives elsewhere | HIGH | Consistent with static RE finding that WriteSetup uses "text-format serialization" with runtime-determined offsets — that blob is in a different storage backend (likely JFFS2 or a separate NVRAM partition in the Linux domain) |

### EEPROM dump artefacts

- `eeprom_baseline.txt` — full 64KB dump, pre-change state (SLP on, WSD
  timeouts 300/300). Format documented in file header.

## Why the `||` EEPROM approach failed

The static RE noted that NVRAMWEBASSIST is the storage backend for telnet's
flag, and that the `||` command accesses the main-domain EEPROM. These were
assumed to be the same chip. The empirical evidence now shows they are
**separate storage**:

- The `||` EEPROM holds hardware config: serial number, admin password,
  waste ink counters, calibration data. This is the main-domain (ARM1136
  RTOS) EEPROM.
- The NVRAMWEBASSIST blob — containing all WEBASSIST-tier settings including
  service toggles (SLP, Bonjour, WSD, telnet) and their parameters — is
  stored in the Linux-domain persistent storage, inaccessible via `||`.

## Remaining approaches

> Reframed 2026-09-28. The goal is a **root shell**, not literally the telnet
> flag (which gates a nonexistent server). Ranked by effort/risk:

1. **UART root console (recommended if hardware available).** getty on `ttyS0`
   @115200 + empty root password = instant root, **zero firmware mod**. Only
   blocker: a USB-serial adapter on the ttyS0 test points. Non-destructive.
2. **Add a network shell via busybox `inetd` (firmware mod).** `inetd` is
   already compiled into the rootfs busybox and reads `/etc/inetd.conf`. Add a
   line (e.g. `2323 stream tcp nowait root /bin/login login`) + start `inetd`
   from `rcS`, repack `squashfs_rootfs1`, `rcx_correct`, flash block 0 (no
   crypto signing). Gives a network root shell.
3. **Add a dropbear/telnetd binary (firmware mod).** More work than (2) since
   `inetd` is present; yields a proper telnet/SSH daemon.
4. **Remote flash via CSRF + unsigned-update chain** — a *delivery vector* for (2)/(3) without physical access; elaborate.

### Not viable — setting the telnet flag

Setting `TELNET_MS_CURRENT_ACTIVATION` cannot enable telnet: no server starts
even when it is set, and no interface can set it in any case.

- ENPC / WSD `SetData` / SNMP / WebConfig / COMIF field write — none exposes a
  telnet field.
- `||` EEPROM byte-write — the WEBASSIST blob is not in that EEPROM.

## Tools

### dump_eeprom.py

Dumps the XP-442's `||`-accessible EEPROM (64KB, main-domain hardware config)
via SNMP. Also does read_key brute-force detection and baseline diffing.
Wraps `epson_print_conf` — requires its dependencies installed.

```bash
# Brute-force the read_key (try narrow XP-4xx range first, then full 256x256)
uv run --script dump_eeprom.py --detect-key

# Full 64KB dump to file (~15 min over Wi-Fi)
uv run --script dump_eeprom.py -o eeprom_dump.txt

# Dump and diff against a baseline
uv run --script dump_eeprom.py --diff eeprom_baseline.txt

# Use a different hostname
uv run --script dump_eeprom.py -a 192.168.1.238 -o dump.txt
```

Output format: `0xADDR HH` (one line per byte, `--` for failed reads).
Progress goes to stderr, data to stdout (or `-o` file).

### Other scripts

- [build_enpc_catalog.py](build_enpc_catalog.py) — build ENPC field catalog from EpsonNet Config templates
- [extract_firmware.py](extract_firmware.py) — extract RCX firmware partitions

## Key references

- [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md) — full PNRSC internals, ID tables, RE results
- [NVRAM_ADDRESS_STATIC_RE.md](NVRAM_ADDRESS_STATIC_RE.md) — the static RE plan (Phases 0–2 done, 3–5 blocked)
- [NVRAM_EEPROM_RECON.md](NVRAM_EEPROM_RECON.md) — the empirical EEPROM-diff plan (Phases 0–3 done, 5 disproved)
- [epson_print_conf/](epson_print_conf/) — the `||` EEPROM read/write tool (upstream)
