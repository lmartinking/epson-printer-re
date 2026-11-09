# Epson XP-442 — PNRSC config registry

How `nwsoc` stores and mutates network configuration, which settings are exposed
via SNMP, and the concrete write path for the TELNET enable flag.

All findings are from static analysis of
[squashfs_rootfs2/nwsoc](squashfs_rootfs2/nwsoc) (ELF32 ARM, EABI5, stripped).
VA↔offset for the text/rodata segment: `vaddr = file_offset + 0x8000`.

## PNRSC / PNWDB registry

`nwsoc` keeps its config in an internal registry addressed by
**Subject (module category) + Item**. It is a C++ object (`PNWDBChecker`,
`PNRSCBaseStart`) reached through generic accessors — subject/item are **runtime
parameters**, never hard-coded at call sites.

Generic API (function names from debug strings):

| Kind | Functions |
|------|-----------|
| Set | `PNRSCBaseSetU32`, `PNRSCBaseSetS32`, `PNRSCSetBool`, `PNRSCSetString`, `PNRSCSetBlock` |
| Get | `PNRSCGetBool`, `PNRSCGetValueU32`, `PNRSCGetString`, `PNRSCGetBlock`, `PNRSCGetBlock2` |

Setter debug strings: `%d PNRSCBaseSetU32(Subject=%d, Item=%d)` @ VA `0x321e4`;
`SetS32` @ VA `0x31cec` / `0x3213c`. Dispatch is via C++ vtable (`blx [rX,#off]`),
which is why `(subject,item)` pairs never appear as call-site immediates — even
for known pairs like `PNRSCGetBool(IP6, IP6_ENABLE_DISABLE)`.

### ID ↔ name tables

Two `{id, name_ptr}` arrays map numeric IDs to debug names:

- **Category table** ~ VA `0x655b30`+
- **Item table** ~ VA `0x656480`+

Discovered category IDs:

| ID | Category | | ID | Category |
|----|----------|-|----|----------|
| `0x1b` | IP | | `0x39` | DR |
| `0x35` | HTTP | | `0x3a` | DR_INFO |
| `0x36` | WEBASSIST | | `0x3b` | FAVORITE |
| `0x37` | **TELNET** | | `0x3c` | MIB_PROXY |
| `0x38` | JM | | `0x62` | IP6 |

Discovered item IDs (excerpt):

| ID | Item |
|----|------|
| `0xbe` | PACFILE |
| `0xbf` | PACFILE_DATA_TYPE |
| `0xc0` | WEBASSIST_REBOOT_WAIT_TIME |
| `0xc1` | **TELNET_MS_SUPPORT** |
| `0xc2` | **TELNET_MS_CURRENT_ACTIVATION** |
| `0xc3` | JMJOBINDEX |
| `0xc4` | JMJOBSTATE |
| `0x1bd` | IP6_ENABLE_DISABLE |

TELNET's items sit **immediately after the WEBASSIST items** and persist to the
`NVRAMWEBASSIST` blob — telnet is a WEBASSIST-tier (web-config) setting.

### Relationship to the ENPC wire schema

ENPC (UDP 3289) exposes a **curated subset** of PNRSC to the network, addressed
by `(category, item)` pairs in the same *shape* as PNRSC but **not the same
numbers** — ENPC's client-visible schema tops out at category `05`, while
PNRSC has subjects up to `0x37`+ (telnet) and beyond. A remap table inside
`PNENPCServiceStart` in `nwsoc` translates each incoming wire ID into the
corresponding PNRSC `(subject, item)` before calling the generic `PNRSCSet*`
family. Practical consequences:

- The full ENPC catalog in [ENPC.md](ENPC.md) is the **network-exposed attack
  surface** for the registry.
- PNRSC entries with **no ENPC counterpart** — telnet (`0x37`,`0xc2`), the
  low-level flash-control items, and the discovery-only tier — are only
  reachable via COMIF (from the main domain / panel) or a local channel (USB
  D4, UART), never from the LAN.
- The remap table itself is the highest-value single reverse target: it fixes
  which PNRSC IDs are legal write destinations for an unauthenticated LAN
  peer, versus which are strictly local.

## Activation tiers

Service enable flags follow a naming convention encoding *which layer* owns them:

| Suffix | Tier | Examples |
|--------|------|----------|
| `_PR_CURRENT_ACTIVATION` | Protocol | `IP_PR_`, `IP6_PR_`, `IPX_PR_`, `NIC_PR_` |
| `_PS_CURRENT_ACTIVATION` | Print-server service | `LPD_PS_`, `IPP_PS_`, `FTPD_PS_`, `NBT_PS_` |
| `_MS_CURRENT_ACTIVATION` | **Management service** | `TELNET_MS_`, `HTTP_MS_`, `SLP_MS_` |
| `<svc>_CURRENT_ACTIVATION` (bare) | Discovery | `MDNS_`, `LLMNR_`, `LLTD_`, `DNSSD_` |
| `_<ENPC\|SNMP>_MS_...` | Mgmt-over-transport | `IP_ENPC_MS_`, `IP_SNMP_MS_`, `DDP_SNMP_MS_` |

**TELNET is `_MS_` (management service)** with no `ENPC`/`SNMP` infix, i.e. a
service master-switch, not a per-transport toggle.

## SNMP availability of config modules

SNMP is a **separate** access layer: MIB groups registered via `LMibRegister` /
`epson_printserver_mib.cpp` in the tables at `0x65b000`–`0x664000`, under Epson
enterprise OID `1.3.6.1.4.1.1248`. Not every PNRSC module is wired to it.
(SNMP column = presence of a registered MIB group, verified against those tables
— not a live per-item walk.)

| PNRSC module | MIB group `…1248.x` | SNMP-configurable |
|--------------|---------------------|-------------------|
| PS (system/host) | System/Host `.1` | Yes |
| SNMP | SNMP `.2` | Yes |
| PRT (model) | Model `.3` | Yes |
| (protocol enable) | Protocols `.4` | Yes |
| IP (DNS/domain) | DNS `.5` | Yes |
| (mDNS) Bonjour | Bonjour `.14` | Yes |
| SMB (storage) | Storage `.15` | Yes |
| (firmware) | Firmware `.21` | Yes |
| IP6 | IPv6 `.28` | Yes |
| WLAN | WiFi `.29` | Yes |
| P2P | WiFi Direct `.30` | Yes |
| WEB | HTTP/Web `.36` | Yes |
| SSL | SSL `.37` | Yes |
| IPSEC | IPSec `.39` | Yes |
| IEEE802 | 802.1X `.40` | Yes |
| **TELNET (`0x37`)** | **— (omitted)** | **No** |
| LPD, IPP, WSD, SCN, SSDP, SNTP, LDAP, NETWARE | not registered | No |

Management/data-plane protocols are SNMP-configurable; service/discovery
protocols and **telnet** are not. Telnet's toggle lives only in the PNRSC
registry, reachable via the web-config/COMIF path below — not via any SNMP OID.

## TELNET activation-vector trace

`TELNET_MS_CURRENT_ACTIVATION` = PNRSC **category `0x37`, item `0xc2`**
(`0xc1` = `_MS_SUPPORT`). The write is a generic `PNRSCSet*(0x37, 0xc2, value)`;
any handler that can call the generic setter can flip it.

```
Web admin (WebConfigEx, HTTP/HTTPS 80/443, in nwsoc)   ← local path
  └─ PNCOMIFWebConfigExCheckPermission (gate)
        │                                              ┌ OR ─ main domain (panel /
        ▼                                              │       EpsonNet) via COMIF
   PNCOMIFSetField(field, value)  ◄────── D4 MIB channel over EPIDC ──────┘
        ▼
   PNRSCBaseSetU32 / SetBool (Subject=0x37, Item=0xc2)   ← C++ vtable, generic
        ▼
   NVRAM (management-service tier → NVRAMWEBASSIST)      ← persisted
        ▼
   ✗ nothing reads the flag — no telnet listener exists in this build
```

> **No telnet listener exists in this build.** Every TCP listener in `nwsoc` is
> accounted for and none is on port 23; the `TELNET_*` strings have zero code
> xrefs; the Linux rootfs busybox has no `telnetd` applet. The write path above
> is real, but its endpoint gates nothing — the flag is vestigial. See
> [NETWORK_SERVICES.md § No telnet server exists](NETWORK_SERVICES.md).

Two external vectors converge on the same registry write:

1. **WebConfigEx** — embedded web admin in `nwsoc`, gated by
   `PNCOMIFWebConfigExCheckPermission`. Most likely toggle (WEBASSIST-tier field).
2. **Main domain** (front panel / EpsonNet setup) — COMIF SET over the D4 MIB
   channel (see [ARCHITECTURE.md](ARCHITECTURE.md)).

Because `PNCOMIFSetField` is generic-by-field (unlike SNMP's per-category MIB
table that omits `0x37`), reachability depends on the permission check and
whether the UI exposes the field — not on a registration table.

### Confidence

Confirmed: registry IDs (name tables), the generic setter, the COMIF broker, the
D4 transport, WebConfigEx + its permission gate. **Not** pinned to a single
instruction — the exact caller resolves through C++ vtable dispatch plus the
cross-domain D4/COMIF boundary, so the channel and gate are identified by name,
not by one call address. No telnet listener exists in this build (see
[NETWORK_SERVICES.md § No telnet server exists](NETWORK_SERVICES.md)).

### WebConfig front-end (Block 1)

The WebConfigEx UI assets live in **Block 1, CROM stream A** (`ipl_1_seg1:0x10`,
decompressed to [rcx_output/block1_streamA_web.bin](rcx_output/block1_streamA_web.bin);
extraction in [FIRMWARE_LAYOUT.md](FIRMWARE_LAYOUT.md)). Asset tree includes
`HTML/TOP/INDEX.HTML`, `HTML/NET/LAN.HTML` (network settings), `HTML/MENT/FIRMUP.HTML`
(firmware update), `SCRIPT/*.JS`, `*.CSS`, and admin-password endpoints
`/ADVANCED/ADMIN_PASSWORDSET_CHANGE|DELETE/`, `/ADVANCED/PASSWORD`.

**No telnet page exists in the web UI** — telnet is not a user-exposed WebConfig
setting, reinforcing that its toggle is an internal/service field (written via the
generic `PNCOMIFSetField`/`PNRSCSet*` path, not a standard UI control).

### Runtime enable — investigation & findings (2026-09-26)

**Question:** Can `TELNET_MS_CURRENT_ACTIVATION` be set to `1` on this firmware
(`20.24.RO05IA.FY16`) without flashing modified firmware?

**Conclusion:** **No known non-destructive network path exists.** Every management
channel exposed by the device was assessed; none accepts a write to PNRSC
`(cat=0x37, item=0xc2)`. Full matrix below with the evidence for each row.

Test target: `scanner.lan` (was `192.168.1.236` before the Wi-Fi incident below).

| Channel | Reachable | Writes telnet flag? | Evidence |
|---|---|---|---|
| **SNMP** (v1, `public` RO / `admin` RW) | ✅ live | ❌ | Full walk of enterprise OID `.1.3.6.1.4.1.1248` returned 280 leaves; grep for TELNET/23/shell = 0 hits. Category `0x37` is not registered in any MIB group (statically verified at `nwsoc` tables `0x65b000`–`0x664000`). |
| **WebConfig admin UI** (HTTPS/443, default `admin` password) | ✅ live | ❌ | Sessioned via `POST /PRESENTATION/PSWD` (`session=admin&from=top&trigger=set`) → `EPSON_COOKIE_SESSION`. Full admin menu enumerated (Status, Wi-Fi, Basic, SSL/TLS, Protocol, MS Network, Network Scan, Wi-Fi Direct, Admin settings, Certificate). The Services→Protocol page (`NW_SERVICE_PRTCL/TOP`) exposes Bonjour, SLP, WSD, LLTD, LLMNR, LPR, RAW9100, IPP — **no telnet toggle anywhere in any form**. |
| **ENPC** (UDP 3289) | Port open; wire schema recovered (see [ENPC.md](ENPC.md)) — packet framing still un-reversed | ❌ (assessed) | Wire schema is a **curated subset** of PNRSC, with 6 top-level categories (`00`–`05`) and no telnet category at all. Full field catalog in [enpc_schema/CATALOG.tsv](enpc_schema/CATALOG.tsv); a grep for `TELNET` across all 278 shipped device templates returns zero hits, confirming telnet has no exposed field-name form. `TELNET_MS_*` strings appear **only** in the debug ID name-tables at `0x65649c`/`0x6564a4` — zero code refs elsewhere in `nwsoc`. |
| **WSD SOAP config** (POST `/Epson_WS_Config`) | ✅ **verified live** on `scanner.lan` — HTTP 202 on well-formed SOAP 1.2 + WS-Addressing GetData; HTTP 400 on malformed XML; HTTP 503 under load | ❌ (same subset as ENPC UDP) | New finding: `nwsoc` serves a `wsd/1.0` daemon that handles a single URL `/Epson_WS_Config` and dispatches by `wsa:Action`. Request actions are all under `http://schemas.epson.net/ws/2008/07/config/` (`GetData`, `SetData`, `GetFile`, `SetFile`). **Same operation set as ENPC — same field-ID space — so same PNRSC subset. No auth challenge on GetData without credentials.** Response delivery is async: `ReplyTo=anonymous` did not produce an inline body; a client-side WSD listener (or the correct request parameter shape) is needed to receive replies. Full details in [ENPC.md §2](ENPC.md). |
| **D4 over USB** (`dirty4.py` / [Ircama epson_print_conf](https://github.com/Ircama/epson_print_conf), EPSON-CTRL) | Prior-art review complete; not yet tested against `scanner.lan` | ❌ for named field writes; ⚠ conditional for raw NVRAM byte writes | EPSON-CTRL over D4 is the **exact same operation set** as SNMP (`epson_print_conf` sends the same OID payload over either transport — the USB bridge is a subclass overriding just `fetch_oid_values`), so the "named-field WriteSetup" path is the same dead-end SNMP is. The one interesting opening is the **`\|\|` command (raw EEPROM byte read/write)**: 16-bit address space, per-model `read_key` (2 bytes) and Caesar-encoded `write_key` (8 bytes). If we can locate the NVRAM byte address of `TELNET_MS_CURRENT_ACTIVATION` in the `NVRAMWEBASSIST` blob, we could flip it directly. See "D4 EPSON-CTRL — prior art" section below for the full protocol details and per-model keys. |
| **Command injection via `popen("ping -c 1 %s")`** | ❌ | n/a | The format string exists in `nwsoc` but the network-diagnosis UI is **not present** in the WebConfig admin menu on this firmware — reachable only from the front panel via COMIF, not over HTTP. |
| **Bulk config-import** (`/ADVANCED/IMPORT/IMPORT_SELECTCATEGORY_WITHIMPORT`) | Backend URL wired into dispatch; **handler traced, sub-dispatch table empty** (see next section) | ❌ (dead code path; safe to hit) | Handler is at stream A file offset `0x2c5608` (POST branch, request type=1). Fast-path `memcmp` at `0x2c566c` matches this exact 49-char URL, then falls into a 3-entry sub-dispatch table at RAM `0x45f61490`. **No writer to that table exists in any binary** (stream A/B/C, `ipl_0_seg1`, `nwsoc`), so the table stays zero-initialised — every request drops to the 404 fallthrough at `0x2c5768` (`mov r0, #0x194`) without touching config. |

### The IMPORT endpoint — static RE result: dead code path

The `/ADVANCED/IMPORT/IMPORT_SELECTCATEGORY_WITHIMPORT` handler was
reverse-engineered in stream A ([rcx_output/block1_streamA_web.bin](rcx_output/block1_streamA_web.bin)).
The finding is unambiguous: the endpoint is compiled in and reachable through
the outer HTTP dispatcher, but its sub-dispatch table is never populated on
this firmware, so any request drops through to an HTTP 404 without side
effects.

**Routing chain** (all offsets are file offsets in stream A):

```
outer HTTP dispatcher (0x2c6c20)
  ├─ reads record[8] = request type
  ├─ record[8] == 1 && record[12] == 1 (POST):
  │     bl 0x2c5608  ← "advanced POST" handler
  │       ├─ 0x2c5648: bl 0x2b3590                — allocate 150 KB response buffer
  │       ├─ 0x2c565c: bl 0x2be380                — likely: url-length helper
  │       ├─ 0x2c5664: add r1, pc, #0x164         — loads URL literal
  │       │                "/ADVANCED/IMPORT/IMPORT_SELECTCATEGORY_WITHIMPORT"
  │       ├─ 0x2c566c: blx 0x367350               — memcmp(url, template, 49)
  │       │            (Thumb; confirmed as memcmp by disassembly)
  │       ├─ match → skip body-read
  │       │  mismatch → malloc 20 KB, read POST body into it
  │       └─ sub-dispatch loop @ 0x2c56cc:
  │            for i in 0..2:
  │              entry = *(0x45f61490 + i*12)      ← 3-row table in RAM
  │              if entry.len == 0: break          ← end-of-table sentinel
  │              if memcmp(url, entry.str, entry.len) == 0:
  │                  entry.handler(...)            ← would call actual writer
  │                  return
  │            → falls through to 0x2c5768:
  │                mov r0, #0x194                  ← 404
  │                bl 0x2a73c4                     ← response builder
  │                free(response buffer); return
  ├─ record[12] == 0 (GET):  bl 0x2c6804  (different handler, not analysed)
  └─ record[12] == 2 (PUT):  bl 0x2c55b4  (different handler, not analysed)
```

**Why the table is empty:**

1. The table pointer `0x45f61490` appears exactly **once** across every
   binary in the firmware (stream A/B/C, `ipl_0_seg1`, `nwsoc`) — as the
   read at `0x2c5804`. No `movw/movt` pair encodes it either. There is
   no writer.
2. The two neighbours `0x45f6148c` and `0x45f61494` also have zero writers
   (checked for offset-based stores).
3. No sibling URL exists for the endpoint anywhere in the firmware: grep for
   `/ADVANCED/(IMPORT|EXPORT)/` across all binaries returns exactly one
   match — this URL. If the table were meant to hold IMPORT sub-endpoints,
   their strings would exist somewhere; they don't.
4. Contrast with the 20 working admin URLs (`/ADVANCED/COMMON/MENU`,
   `/ADVANCED/PASSWORD`, `/ADVANCED/INFO_*`, …) which live in a data
   string-pool at `0x4a9b79`–`0x4a9d16` and route through a *different*
   dispatch pattern (general router, not this special-case handler). The
   IMPORT handler was clearly written for a bulk-import feature that never
   shipped on this SKU / firmware version.

**Consequences:**

- ✅ Safe to POST to this URL: no config write happens; the code path
  allocates and frees a response buffer and returns 404. No `/SET`-style
  wipe risk.
- ❌ Useless for enabling telnet: the writer that the sub-dispatch would
  reach is unreachable because the table has no entries.

**Caveat left open:** it is theoretically possible that the RTOS boot code
in the Block 0 ARM1136 core (or an `nwsoc`-side init message over the D4
MIB channel) populates the table at runtime by mechanisms other than a
straight 32-bit store of `0x45f61490` (e.g. a base pointer plus a large
offset computed from another table). Static analysis of stream A alone
can't fully rule that out. But given the total absence of sibling URL
strings anywhere, the parsimonious reading is that the feature is a
compiled-in stub, not a hidden active writer.

### D4 EPSON-CTRL — prior art & protocol details (2026-09-26)

The [`epson_print_conf`](https://github.com/Ircama/epson_print_conf) tool
(a submodule at [epson_print_conf/](epson_print_conf/)) implements the full
EPSON-CTRL protocol over both transports and is the definitive prior art. Key
findings after reading its source:

**Transport equivalence.** EPSON-CTRL over D4/USB and EPSON-CTRL over SNMP
send the *identical* payload — only the envelope differs. The USB bridge
([epson_print_conf/epson_usb_bridge.py](epson_print_conf/epson_usb_bridge.py))
is a `EpsonPrinter` subclass that overrides just `fetch_oid_values`; the OID
string is built the same way, then sent as raw bytes over the D4 EPSON-CTRL
channel instead of an SNMP GET. This is the "1:1 mapping" statement from the
`reinkpy` community.

**SNMP wrapping (also usable directly).** Base OID:
```
1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1.<c0>.<c1>.<len_lo>.<len_hi>.<payload_bytes>
```
where `c0 c1` is the 2-letter ASCII command (e.g. `st` = `115.116`, `||` = `124.124`)
and the length is little-endian u16.

**Command inventory (~23 commands, none for network services).**
`st`, `di`, `vi`, `ia`, `ii`, `ot`, `ti`, `rw`, `ex`, `\|\|`, `cd`, `cs`, `cx`,
`ei`, `fl`, `ht`, `pe`, `pj`, `pm`, `rj`, `rp`, `rs`, `xi`. All are
printer-hardware oriented (paper/ink/cartridge/timer/firmware). **Zero
references to telnet, service enable, PNRSC, or named-field writes** across
epson_print_conf's ~4,000 lines. The entire community effort is EEPROM
byte-level operations for hardware state (waste counters, ink levels, serial,
timers).

**The `||` (EEPROM byte access) opening — the only generic writer.**

Read: sends `read_key[0], read_key[1], 0x41, 0xBE, 0xA0, addr_lsb, addr_msb`.
Reply parses as `EE:AAAAVV;` (2-byte address, 1-byte value hex).

Write: sends `read_key[0], read_key[1], 0x42, 0xBD, 0x21, addr_lsb, addr_msb,
value` followed by an 8-byte Caesar-encoded write_key (each byte +1, zeros
preserved). Example: `b"Polyxena"` → `0x51 0x70 0x6D 0x7A 0x79 0x66 0x6F 0x62`
on the wire.

Address space is 16-bit (up to 64 KB EEPROM). No per-address auth beyond the
model's two keys.

**XP-442 model keys — not in the config; siblings for reference:**

| Model (family neighbours) | `read_key` | `write_key` |
|---|---|---|
| XP-422 (aliases XP-423, XP-425, XP-225) | `[85, 5]` | `b"Muscari."` |
| XP-432 (aliases XP-235, XP-433, XP-435) | `[133, 5]` | `b"Polyxena"` |
| XP-452 (alias XP-455) | `[49, 6]` | `b"Suramadu"` |

XP-442 sits numerically between XP-432 (whose family includes XP-435) and
XP-452, so it's most likely to share keys with one of those two — try
`Polyxena` first, then `Suramadu`. Otherwise:

- `brute_force_read_key()` at [epson_print_conf.py:2877](epson_print_conf/epson_print_conf.py) —
  256² attempts against address `0x00`, a few minutes wall-clock. Read-only,
  safe.
- `write_key_list()` at [epson_print_conf.py:2991](epson_print_conf/epson_print_conf.py) —
  returns candidates from other models with matching `read_key`, tried in
  turn against a probe-write. Detection is done by incrementing then
  restoring a scratch byte, so it's non-destructive if the code is trusted.

**What this doesn't give us.** A byte-write to EEPROM changes NVRAM, but the
telnet service task in `nwsoc` presumably reads its flag from NVRAM only at
boot (typical PNRSC caching), so a power-cycle after the byte-write would be
required for the change to take effect.

**Open problem — the NVRAM byte address of `TELNET_MS_CURRENT_ACTIVATION`.**

Static RE (NVRAM_ADDRESS_STATIC_RE.md Phases 0–2) confirmed the storage backend is
main-domain EEPROM via COMIF WriteSetup, but **could not determine the byte
offset** — TELNET items are absent from the per-item descriptor table and
the WriteSetup blob uses a self-describing text format whose layout is
embedded in the EEPROM data itself. See "NVRAM_ADDRESS_STATIC_RE.md execution results"
section below for the full analysis.

**Remaining path** — empirical (NVRAM_EEPROM_RECON.md):
1. Dump the full 64 KB EEPROM via `read_eeprom_many(range(65536))`.
2. Search the dump for ASCII COMIF field names to locate the WriteSetup
   blob and any TELNET field within it.
3. Diff experiment: change `WEBASSIST_REBOOT_WAIT_TIME` through the web UI,
   dump again, diff. The changed byte anchors the WEBASSIST region.
4. TELNET items (`0xc1`/`0xc2`) sit adjacent in the ID space and should be
   nearby in the EEPROM if the serialization follows ID ordering.

### ⚠ Testing-safety rules (learned the hard way, 2026-09-26)

During endpoint enumeration, bare **GET** requests to
`/PRESENTATION/ADVANCED/NW_WIFI/SET` and `/NW_SERVICE_WIFID/SET` — with no
`INPUTT_*` fields — **wiped the Wi-Fi configuration** (recovery required
front-panel re-entry). The `/SET` handlers treat missing form fields as empty
values and apply them. Rules for future work on this device:

1. **Never GET or POST a `/SET` endpoint** without first fetching its matching
   `/TOP` and preserving every `INPUTC_*` / `INPUTT_*` / `INPUTR_*` value.
2. **No enumeration by verb-combination.** Only touch endpoints referenced
   explicitly by extracted JS/HTML in `block1_streamA_web.bin`.
3. `/TOP`, `/GET` and plain read paths (`INFO_*`) are safe; anything named
   `SET`, `CHANGE`, `DELETE`, `UPDATE`, `RESET` is state-mutating and must
   be treated with a real baseline. (Note: `IMPORT` sounds equally scary
   but the one IMPORT endpoint we've traced —
   `/ADVANCED/IMPORT/IMPORT_SELECTCATEGORY_WITHIMPORT` — is verified via
   static RE to be a dead code path returning 404 without side effects; see
   the IMPORT-endpoint section above. Don't generalise from that to other
   `IMPORT` names without checking.)
4. Test target hostname: `scanner.lan` (DHCP address may vary).

### Remaining options (decision pending)

The runtime, no-flash question terminates at the IMPORT endpoint. Note also
that all remaining LAN-side avenues still write into the **curated PNRSC
subset** exposed to the network (see the ENPC-relationship section above); if
telnet isn't in that subset, no LAN request writes it regardless of which
transport delivers it. The remaining avenues, in order of expected effort:

- **WSD SOAP config path** (**new — most-promising LAN avenue**). Verified
  reachable on `scanner.lan` (see the matrix row above and [ENPC.md §2](ENPC.md)).
  Removes the biggest unknown of the UDP ENPC path — packet framing is now
  standard SOAP 1.2 + WS-Addressing, no reverse required. Still bounded by the
  same PNRSC subset, so **won't reach telnet** on its own; useful as a general
  read/write handle into every wire-exposed field, and as a testbed for
  whether SetData accepts fields the WebConfig HTML never renders (same
  "backend-with-no-UI" pattern as IMPORT). Remaining work: determine
  response-delivery mode (inline vs. WS-A callback), recover the input-body
  shape for each op from `execGetDetailSOAP` in the EpsonNet Config binary,
  test auth requirements for SetData/SetFile.
- ~~**Static reverse of the IMPORT handler**~~ — **done, closed as negative.**
  Handler traced at stream A `0x2c5608`; sub-dispatch table at RAM
  `0x45f61490` has no writer anywhere in the firmware and stays
  zero-initialised, so the endpoint returns 404 with no config effect.
  Full analysis in the IMPORT-endpoint section above.
- **Static reverse of the ENPC receiver** (`PNENPCServiceStart` code path) to
  enumerate what commands UDP 3289 actually accepts and whether any include
  arbitrary field writes. The client-side field catalog is now known
  ([ENPC.md](ENPC.md)), so the remaining work is the packet framing and the
  wire↔PNRSC remap table — no schema-guessing needed. **Lower priority now
  that WSD SOAP is confirmed reachable** and shares the same operation set.
- **D4 EPSON-CTRL over USB (or SNMP-wrapped)** — prior-art review done; see
  "D4 EPSON-CTRL — prior art & protocol details" above. **Named-field
  WriteSetup path is a dead-end** (same operation set as SNMP, no telnet in
  the whitelist). The only remaining opening is a **raw NVRAM byte write via
  `\|\|`** using [epson_print_conf](epson_print_conf/) — but requires finding
  the NVRAM byte address of `TELNET_MS_CURRENT_ACTIVATION` first (RE of
  `nwsoc` or the ARM1136 main-domain, or an EEPROM-diff experiment using a
  known WEBASSIST-tier setting as anchor). Read-key detection is a solved
  problem (brute-force is safe and takes minutes); write-key detection is
  built into the tool and tries the neighbouring model keys (`Polyxena`,
  `Suramadu`) first. Reachable over the network too via the SNMP wrapper —
  no USB access strictly required.
- **Firmware modification + re-sign + flash** — works with the existing
  toolchain (`rcx_correct` + `epsflasher`), enabled by the lack of update signing. Flash risk exists but recovery mode is documented.
- **Physical UART root shell** — the empty root password + `getty` on `ttyS0`
  in [squashfs_rootfs1/etc/inittab](squashfs_rootfs1/etc/inittab) already give
  root on the Linux domain via the mainboard's UART test points. Doesn't need
  telnet or flashing but requires bench access.

### Prior static-analysis follow-ups (resolved / remaining)

- ~~Determine whether `TELNET_MS_*` is registered as a **COMIF field**~~ →
  **RESOLVED: NO.** The 154-entry COMIF field handler table at VA `0x57af94`
  contains Enable handlers for SLP, SNMP, LPR, Port9100, WSD, IPv6,
  IPSec, LLTD, eSCL, LDAP, IEEE802.1X, etc. — but **no TelnetEnable or
  any telnet-related field**. Telnet activation is not exposed through the
  web config / COMIF interface at all.
- Inspect `PNCOMIFWebConfigExCheckPermission` — **deprioritised**; with no
  COMIF field for telnet, the permission check is moot for this path.

---

## NVRAM_ADDRESS_STATIC_RE.md execution results (2026-09-26)

Static reverse engineering of the TELNET_MS_CURRENT_ACTIVATION storage path,
following [NVRAM_ADDRESS_STATIC_RE.md](notes/NVRAM_ADDRESS_STATIC_RE.md) Phases 0–2.

### Name table vs PNRSC runtime IDs

Two distinct ID systems exist in `nwsoc`:

| System | Purpose | TELNET cat | TELNET_MS_CURRENT_ACTIVATION item |
|--------|---------|-----------|----------------------------------|
| **Name→ID table** (VA `0x655ae4` cats, `0x655dec` items) | Debug/display name lookup | `0x38` | `0xc3` |
| **PNRSC runtime** (descriptor table keys, code arguments) | Actual register dispatch | `0x37` | `0xc2` |

Verified by cross-referencing descriptor table entries with their
embedded name strings: HTTP has descriptor Subject `0x35` but name table
ID `0x36`; WEBASSIST has descriptor Subject `0x36` but name table ID `0x37`.
PNRSC_CONFIG_REGISTRY.md's original values `(0x37, 0xc2)` are the **PNRSC runtime values**
and are correct.

### Phase 0 — Backend determination (HIGH CONFIDENCE: EEPROM)

**Result:** NVRAMWEBASSIST is stored on **main-domain EEPROM** accessed via
COMIF WriteSetup/ReadSetup. Evidence:

1. No JFFS2 file for WEBASSIST — the JFFS2 paths are `/opt/nwsoc_data/rscbasic`,
   `rsceternal`, `rscncl`, `rscncl2` only. No `rscwebassist`.
2. `PNCOMIFWriteSetup` at VA `0xe2378` calls serialiser at `0xbd750` which
   builds a text-format blob (field name + data pairs, using strcpy/memcpy).
3. COMIF dispatch table at VA `0x57b880` maps `"writesetup"→4`, `"readsetup"→3`.
4. The `||` EPSON-CTRL command reaches the same physical EEPROM with 16-bit
   byte addresses, so the RECON approach (raw EEPROM byte write) is viable.

### Phase 1 — Setter identification (HIGH CONFIDENCE)

**Result:** `PNRSCBaseSetU32` at VA `0x3214c` packs `(Subject<<16|Item)` and
dispatches through `PLeoNVRamDB` vtable\[12\] at `0x32b18`. Single global
singleton at VA `0x695de4`, vtable at `0x568b18`.

### Phase 2 — ID→offset mapping (BLOCKED — hit stop condition)

**Result:** TELNET items are **completely absent** from the 724-entry
per-item descriptor table (56-byte entries at VA `0x6575f8`). Subject
`0x37` does not appear anywhere in the table — it jumps directly from
`0x36` (WEBASSIST, 1 entry) to `0x38` (JM, 3 entries).

The descriptor table covers items in the JFFS2-backed NVRAM regions
(BASIC, ETERNAL, NCL, NCL2). NVRAMWEBASSIST items use a **different
storage mechanism**: the WriteSetup serialiser at `0xbd750` builds a
text-format COMIF blob that is sent to the main domain and stored in
EEPROM as-is. The item layout within this blob is determined at
serialization time by the code that iterates over items, not by a
static table.

Additionally, all apparent code references to TELNET_MS_CURRENT_ACTIVATION's
numeric IDs (`0xc2`/`0xc3`) were **false positives**: `mq_open` flags,
`printf`/`ostream<<` debug logging, and OpenSSL constants. The actual PNRSC
accessors use table-driven indirect calls, not hardcoded immediates.

**Stop condition met:** This is NVRAM_ADDRESS_STATIC_RE.md's "Layout C" scenario — the item
offset is determined at runtime by a self-describing format embedded in the
EEPROM data itself. The hash/mapping function is not statically recoverable
without the EEPROM contents.

### Pivot recommendation

Per NVRAM_ADDRESS_STATIC_RE.md §"Interaction with the empirical NVRAM_EEPROM_RECON.md plan", the
recommended next step is the **best-of-both-worlds sequence**:

1. **NVRAM_EEPROM_RECON.md Phases 0–3**: setup `epson_print_conf`, establish transport
   to `scanner.lan`, detect read/write keys, take a full 64 KB EEPROM
   baseline dump.
2. **Search the dump** for ASCII COMIF field names (e.g. `HttpEnable`,
   `SLPEnable`, `Port9100Enable`) to locate the WriteSetup blob region
   and determine if any TELNET-related fields appear in it.
3. **RECON Phase 5 diff**: change `WEBASSIST_REBOOT_WAIT_TIME` via the
   web UI (the one WEBASSIST-tier setting exposed in the admin page),
   dump again, diff. The changed byte localises the WEBASSIST region.
   TELNET items (`0xc1`, `0xc2`) sit adjacent in the ID space and
   should be nearby in the blob.
4. **RECON Phase 7**: once the byte address is known, write it via `||`
   EEPROM command and power-cycle.
