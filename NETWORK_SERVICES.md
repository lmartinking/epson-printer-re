# Epson XP-442 — network services

> **Architecture note:** `nwsoc` is the Linux (ARM926) network-domain daemon that
> hosts **all** network protocol servers (HTTP/WebConfigEx, SNMP, WSD, IPP, LPD,
> ENPC). The print engine / panel / authoritative config run in a separate
> ARM1136 RTOS domain, reached over the D4 MIB channel via COMIF. See
> [ARCHITECTURE.md](ARCHITECTURE.md).

## TELNET Presence

**There is no telnet server in this firmware.** The `TELNET_MS_*` PNRSC IDs
(`0x37`/`0xc1`/`0xc2`) are vestigial config definitions gating a listener that is
not present in the build. The only telnet artifacts in `nwsoc` are the three
registry-name strings below, and they have zero code xrefs — nothing reads
`TELNET_MS_CURRENT_ACTIVATION` to start anything. Proof:
[§ No telnet server exists](#no-telnet-server-exists).

Two independent facts block "enabling telnet" as a toggle:
- **No interface can set the flag** — SNMP, the WebConfig UI, ENPC, WSD
  `SetData`, and COMIF all lack a telnet field (matrix in
  [PNRSC_CONFIG_REGISTRY.md § Runtime enable](PNRSC_CONFIG_REGISTRY.md)).
- **Setting it would start nothing** — there is no server to start.

Enabling telnet means **adding a server**:
[§ Getting a shell](#getting-a-shell-what-actually-works).

**Evidence in `squashfs_rootfs2/nwsoc`:**
```
TELNET
TELNET_MS_SUPPORT
TELNET_MS_CURRENT_ACTIVATION
```

### PNRSC Internal Database Structure
`nwsoc` uses an internal configuration registry (PNRSC / PNWDB) based on module categories and item IDs:
- **Category:** `0x37` (`TELNET`)
- **Item `0xc1`:** `TELNET_MS_SUPPORT` (feature availability)
- **Item `0xc2`:** `TELNET_MS_CURRENT_ACTIVATION` (service state / toggle) — `_MS_` = management-service tier, grouped with WEBASSIST, persisted to `NVRAMWEBASSIST`

Full registry internals (category/item ID tables, generic setter/getter API,
activation tiers, SNMP-availability mapping) are in **[PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md)**.

### Write path (how the flag gets set)
Writes go through the **generic** `PNRSCBaseSetU32/SetBool(Subject=0x37, Item=0xc2, …)`
(C++ vtable dispatch — subject/item are never call-site immediates). Two external
vectors converge on that write:
1. **WebConfigEx** — embedded web admin in `nwsoc` (HTTP/HTTPS 80/443), gated by `PNCOMIFWebConfigExCheckPermission`.
2. **Main domain** (panel / EpsonNet setup) — `PNCOMIFSetField` / `COMIF_LEVT_SET` over the D4 MIB channel.

Full trace + evidence + confidence notes: [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md).

*(Note: File offset `0x71e`, previously suspected as a reference to port 23 (`0x0017`), was verified via `readelf` to reside within the ELF `.hash` section (`0x148`–`0xbc4`) and was a hash table entry rather than an instruction or port binding).*

## Activation Vector Analysis

### 1. SNMP MIB Inspection (Not Supported via SNMP)
- Tested against live device (`scanner.lan`) using community strings `public` (read-only) and `admin` (read-write).
- Performed a full walk of the enterprise MIB tree (`1.3.6.1.4.1.1248`).
- Cross-referenced the master MIB registration tables in `nwsoc` (`0x65b000`–`0x664000`):
  - Handled MIB groups include: System/Host (`.1`), SNMP (`.2`), Model (`.3`), Protocols (`.4`), DNS (`.5`), Bonjour (`.14`), Storage (`.15`), Firmware (`.21`), IPv6 (`.28`), WiFi (`.29`), WiFi Direct (`.30`), HTTP/Web (`.36`), SSL (`.37`), IPSec (`.39`), and 802.1X (`.40`).
  - **Category `0x37` (TELNET) is omitted from all SNMP MIB registration tables.**
  - **Conclusion:** Telnet is **not** exposed or configurable through SNMP OIDs.

### 2. EpsonNet Config Client Analysis (`EpsonNet Config.app`)
- Analyzed macOS client binary (`EpsonNet Config.app/Contents/MacOS/EpsonNet Config (macOS client)`).
- Found only two occurrences of `telnet` in the application bundle (offsets `0x37464d` in `i386` and `0x86fab1` in `x86_64`), both belonging to the generic URI scheme list of the bundled **POCO C++ Library** (`Poco::URI`).
- **Conclusion:** The client utility does not implement any Telnet configuration menus, handlers, or hidden developer flags.

### 3. Confirmed / Likely Vectors
Since telnet is a WEBASSIST-tier field written via the generic `PNCOMIFSetField` →
`PNRSCSet*` path (not SNMP), the reachable vectors are:
- **WebConfigEx web admin (HTTP/HTTPS 80/443):** most likely toggle. Server string `WebConfigEx/1.0 (EPSON_Linux)`; permission gate `PNCOMIFWebConfigExCheckPermission`. Whether a low-privilege/unauth request can reach the telnet field is an open follow-up (see [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md)).
- **Main domain (front panel / EpsonNet setup):** COMIF SET over the D4 MIB channel to `nwsoc`.
- **Epson NetConfig Protocol (UDP 3289, ENPC):** its own management channel; carries a `*_ENPC_MS_*` activation family but no telnet-specific ENPC item observed.

## Remote Command Execution

`nwsoc` has full command execution via `popen()` and `execl()`.

**Network diagnosis service** (always active) executes:
- `ping`, `arp`, `nslookup`, `route`, `ip`, `LLTD` discovery
- Windows network browsing (`msbrowserExecutor`)

**Chain:** Network request → `diagRecvTask` → `popen()`/`execl()` → command

## No telnet server exists

Ghidra listener enumeration + on-disk checks, high confidence:

- **Every TCP listener in `nwsoc` enumerated** via the six callers of the
  `listen` thunk (`0xd008`). All are non-telnet: the generic helper
  `PNSUTLOpenSocket` (`0x79200`, used by SNMP `snmpIpTask`, LLMNR, `NWT_Task`,
  LPD `PSLM_lpdMain`, P9100 `PS9Main`, ESW), `PNBrowser` (AF_UNIX
  `/tmp/pnbrowser_socket`), and generic bind/listen helpers
  (`FUN_002b9b34/002ccc74/005663ac/00566608`). **None binds port 23.**
- **No port-23 reference** anywhere in `nwsoc` (the "23" hits are all `SSLv23`
  / `s23_srvr.c` / C++ mangled name lengths).
- **The three `TELNET_*` strings have zero code xrefs** — referenced only as
  data in the debug ID name-table.
- **Linux rootfs `busybox`** applet set includes `telnet` (the *client*),
  `inetd`, `getty`, `login` — but **no `telnetd`, no dropbear, no netcat**.
  There is no telnet *server* binary in the image.

The network stack lives only in `nwsoc` (the main domain has no TCP/IP), and
`nwsoc` has no telnet listener, so telnet is **absent from this build**. The
activation flag gates nothing.

## Getting a shell (what actually works)

Because there's no telnet server to enable, the real routes to a root shell are:

1. **UART root console (no firmware mod).** `inittab` runs a getty on the
   serial port — `ttyS0::respawn:/sbin/getty -L ttyS0 115200 vt100` — and
   **root has an empty password** (`root::0:0:root:/root:/bin/ash`, no
   `/etc/shadow`). A USB-serial adapter on the ttyS0 test points = instant root.
   Only blocker is the adapter.
2. **Add a network shell via `inetd` (firmware mod).** busybox `inetd` is
   compiled in and reads `/etc/inetd.conf`. Add a line (e.g.
   `2323 stream tcp nowait root /bin/login login`) and start `inetd` from
   `rcS`, repack `squashfs_rootfs1`, `rcx_correct`, flash block 0. No firmware
   signing to defeat.
   **Status 2026-09-29 (three attempts, all Connection-refused).**
   Attempts: (a) plain inetd with empty root pw; (b) inetd on a kernel co-IP
   alias; (c) `inetd -fe` + real `--auth-hash` in `/etc/shadow` + `/etc/passwd`
   flipped to `x` + `/etc/securetty` removed. Each boots normally (nwsoc/HTTP/
   IPP/LPD/9100/mDNS all fine) but TCP 2323 returns kernel-RST immediately.
   Static analysis rules out every content/parsing/architectural hypothesis
   (see [INETD_DIAGNOSIS.md](notes/INETD_DIAGNOSIS.md)):
   the kernel-socket + no-netns finding is real, no netfilter is loaded, the
   inetd.conf/getopt/glob/passwd/shadow are all correct, and the flashed image
   matches what was intended byte-for-byte. Residual ambiguity: either S50
   silently failed between config-parse and bind, or S50 never ran at all —
   indistinguishable from off-device. **UART is the required next step** to
   settle it (see option 1); with a shell, `ps`/`netstat -lntp` + running
   inetd by hand resolves it in one boot. A `FWF729TL_authshell2.rcx` with an
   exec-form S50 (drops `-f`, adds `setsid`, redirects stdio to
   `/tmp/inetd.out`) is prepared as an optional one-more-cycle bet before
   committing to UART — if it works, it narrows the failure to `-f`
   specifically; if it doesn't, further blind cycles are not worth it.
3. **Add a dropbear/telnetd binary (firmware mod).** More work than (2) since
   `inetd` is already present; gives a proper telnet/SSH daemon with real auth
   (password or key). Worth considering only after UART diagnosis shows what
   was actually wrong with (2) — the same "listener never became reachable"
   root cause might apply.
4. **Patch `nwsoc` to serve the shell** — deprecated design in
   [NWSOC_NETSHELL_DESIGN.md](notes/NWSOC_NETSHELL_DESIGN.md). The kernel-socket
   finding says this shouldn't be needed; keep only as a very-last-resort
   fallback if (2) turns out to be structurally impossible for a reason we
   haven't seen.

## BusyBox Shell

Rootfs `busybox` applets (symlinks in `/bin`, `/sbin`): `ash`, `bash`, `login`,
`getty`, `inetd`, `telnet` (client), plus the usual `vi`, `tar`, `grep`, `ping`,
`arp`, `nslookup`, `route`, `ifconfig`, `cat`, `insmod`, `modprobe`, `init`,
`reboot`. **No `telnetd`/dropbear/netcat** — a network *server* must be added
(see [§ Getting a shell](#getting-a-shell-what-actually-works)).

## Init Sequence

```
rcS → S20urandom → S40network → S99NWControl
  → date -s "2014.01.01-12:00:00" (fallback time)
  → idc_load.sh (loads epidc.ko — the EPIDC inter-domain link, /dev/epidc0)
  → nwsoc (infinite loop)
```

`idc_load.sh` sets up the shared-memory link to the ARM1136 domain
(`insmod epidc.ko major=100 shmemaddr=0x42003000 shmemsize=0x15B000 commctrlmemaddr=0xe0c00000`).
D4 rides over this link (`D4CbtSendIdc`/`ReceiveIdc`). See [ARCHITECTURE.md](ARCHITECTURE.md).

## Key Binary

- **Path:** `/opt/nwsoc/nwsoc`
- **Type:** ELF 32-bit ARM, EABI5, stripped
- **Libraries:** uClibc 0.9.32, libstdc++ 6.0.14, libusb 1.0

## Live Device Profile (`scanner.lan`)

- **Hostname:** `scanner.lan` (mDNS/Bonjour; use this rather than the DHCP address,
  which can change — e.g. after the Wi-Fi re-provisioning caused during endpoint
  enumeration; see [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md) safety notes).
- **Historical DHCP address (pre-2026-09-26):** `192.168.1.236` (fallback: `192.168.192.168`).

Discovered via SNMPv1 (`public` RO / `admin` RW):

- **Hardware / Model:** EPSON XP-442 445 Series
- **Serial Number:** `X2CL036077` (`5832434C3033363077`)
- **MAC Address:** `38:9D:92:7C:2D:06`
- **Firmware Running:** `20.24.RO05IA.FY16` (`RO05IA`)
  - Wireless Card: `A1.0720.0000`
  - NW Kernel: `20` / NW App: `24`
- **Network Environment:**
  - Wi-Fi SSID: `AppleTown` (BSSID: `8C:86:DD:3D:43:E1`, Ch 11)
- **Open TCP Ports:**
  - `80` / `443` — HTTP/HTTPS Web Admin (`WebConfigEx/1.0 (EPSON_Linux)`)
  - `139` / `445` — NetBIOS-SSN / SMB (`\\scanner.lan\MEMORYCARD`)
  - `515` — LPD (`lpr://scanner.lan:515/PASSTHRU`)
  - `631` — IPP (`http://scanner.lan:631/ipp/print`)
  - `1865` — Epson Network Scan service
  - `9100` — RAW JetDirect
- **Open UDP Ports:**
  - `137` / `138` — NetBIOS Name/Datagram
  - `161` — SNMP
  - `427` — SLP
  - `3289` — Epson NetConfig (ENPC)
  - `3702` — WS-Discovery (WSD)
  - `5353` / `5355` — mDNS (Bonjour) / LLMNR
