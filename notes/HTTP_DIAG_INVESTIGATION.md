# HTTP diagnostic-leak feasibility (XP-442, remote / static)

> Static analysis only. No probes to the printer. No `nwsoc` mods. Sources:
> unpacked rootfs at `/tmp/rootfs_remote`, nwsoc fs at `/tmp/nwsoc`, and
> `strings` on `/tmp/nwsoc/nwsoc` (ELF ARM LE, base 0x8000).

## Bottom line

**The hypothesis is already confirmed by static evidence — you do NOT need
an HTTP diagnostic dump to prove the LAN IP is on the kernel stack.** And
the specific "drop a text file in a webroot, GET it back" plan is **not
feasible from the rootfs side** on this firmware, because nwsoc's HTTP
server does not serve files from a filesystem docroot at all — every
response is either compiled-in strings or programmatically generated.

Ranked options below.

## 1. Zero-mod: what the stock HTTP/SNMP surface already leaks

### 1a. Static evidence already answers the kernel-stack question (no LAN probe)

The whole point of wanting `ifconfig`/`netstat` output was to prove nwsoc's
listeners sit on the kernel stack in the main netns. `strings` on the nwsoc
binary shows nwsoc itself pulls this exact info from the standard Linux
kernel interfaces:

```
getifaddrs
%s: ioctl[SIOCGIFADDR]: %s
/proc/net/dev
/proc/net/route
/proc/net/tcp
/proc/net/udp
/proc/net/arp
ioctl(SIOCGIFINDEX)
ioctl(SIOCGIFHWADDR)
```

Combined with the earlier findings (no `unshare`/`setns`/`CLONE_NEW*`;
`PNSUTLOpenSocket` @0x79200 = ordinary `getaddrinfo`→`socket`→`bind`→`listen`
with the `FD_SETSIZE` fd<1024 check; nwsoc's own `udhcpc` configures a
kernel interface via `udhcpc.script`), this is dispositive: **nwsoc is a
plain Linux process reading and using the kernel's netns.** There is no
private userspace stack. Interface `.238` sits on a kernel `eth0`/`wlan0`,
and a rootfs listener that actually binds `0.0.0.0:2323` on that same
kernel *will* be reachable. The `busybox inetd` failure is a bind-time
issue in the listener itself, not an architectural wall.

If you still want to see this from the network, the two zero-mod leaks are:

### 1b. ENPC (UDP 3289 broadcast)

Epson NetConfig Protocol runs on the LAN with no auth and returns the
MAC address, model, hostname, and current IP/netmask/gateway. That gives
you the L3 addresses on the printer's kernel interface — the same info
`ifconfig` would. It does not distinguish wired vs Wi-Fi at the OS name
level (`eth0` vs `wlan0`), and it does not enumerate listening TCP ports
or processes.

### 1c. SNMP (UDP 161, community `public`)

Nothing useful for this question. Per `NETWORK_SERVICES.md`'s earlier walk, the
only SNMP tree served is the Epson-private `1.3.6.1.4.1.1248` MIB, plus
minimal system OIDs. Standard **MIB-II `ifTable` (`1.3.6.1.2.1.2`),
`tcpConnTable`/`tcpListenerTable` (`1.3.6.1.2.1.6`), `udpTable`
(`1.3.6.1.2.1.7`)** are **not registered** — grep for MIB-II names
(`ifDescr`, `ifIndex`, `ifPhysAddress`, `IF-MIB`, `tcpListenerTable`)
returns nothing. So SNMP will not give you kernel interface names, a
listening-ports list, or a process list.

### 1d. WebConfig "network status" pages

`nwsoc`'s HTTP surface advertises `WebConfigEx/1.0 (EPSON_Linux)`. The
only `/PRESENTATION/*` URLs that appear as strings are certificate-import
result pages (`/PRESENTATION/ADVANCED/NWS_CERT_*/…`) and a couple of
Bonjour/AirPrint icons. The actual WebConfig menu URLs are almost
certainly built at runtime from a dispatch table rather than stored as
whole strings, so absence-of-string is not proof-of-absence — the live
site really does render "Network Status" / "Wi-Fi Status" / "IPv4 Address"
pages. Those pages will show you the LAN IPv4/IPv6, MAC, gateway, DNS,
and Wi-Fi SSID/BSSID — enough to confirm 1a a second time. They will
**not** show you a kernel interface list, listening-TCP list, or process
list; nothing in the binary suggests such a page exists.

### 1e. The `NetworkStatusSheet` in the binary is a *printed* page

`NetworkStatusSheet` / `PNPSStatusSheetStart` / `XSSWriteStatusSheet…` in
strings is the physical status sheet the printer prints on paper when you
press the button (or trigger it via `STATUSSHEET_INFOMATION`). It's the
richest report the device has (interface, IP config, all-service status),
but it comes out on paper, not over HTTP.

## 2. Does nwsoc's HTTP server serve static files from a filesystem docroot?

**No.** Evidence:

- No `htdocs`/`webroot`/`docroot`/`www`/`/html/` strings in the binary.
- No `.html`/`.htm`/`.js`/`.css`/`.gif`/`.svg` file-name strings that
  would suggest a served-file table (search returned nothing under those
  extensions).
- No `.cgi` strings at all, no `/cgi-bin`. The earlier trace of
  `/data_check.cgi` does not appear in this build's binary (no hits in
  `nwsoc` or under `/tmp/rootfs_remote/etc`).
- Every writable path referenced by the binary under `/opt/nwsoc*` is a
  config or certificate blob (`/opt/nwsoc_data/{rscbasic,rscncl,rscncl2,
  rsceternal,wlan.conf,mac_address,ipsec.dat,cert/…,nwsoc_RootCert.*}`
  and `/opt/nwsoc/{version/*,wlan.confdef,bcm43143/…}`), never a web page.
- The only HTML fragments compiled in are small XHTML result-page
  scaffolds (`<html xmlns=…><body><div class="docframe">…`) — those are
  the WebConfig response wrappers, generated in-place, not read from a
  file per request.
- The URL handler set is a fixed dispatch: `/PRESENTATION/`,
  `/PRESENTATIONEX/`, `/CONTROL/`, `/DEVICE/`, `/DOWN/`, `/EVENT/`,
  `/ICON/`, `/SCPD/`, `/UP/`, `/PRIVET/PRINTER/SUBMITDOC`,
  `/WSD/DEVICE`, `/WDP/PRINT`, `/WDP/SCAN`. All handled by internal
  service code (WebConfigEx, WSD/UPnP, IPP, Privet, etc.), not by a
  generic "read file at request path" routine.

Server dispatch entry points visible in strings: `init_http_task`,
`http_ctrl_task`, `HTTPDSCallbackFuncTCP/UDP`, `HTTPDSCBClientFunc`,
`HTTPCServerConnect`, `PARSE_HTTP_LINE1`, and the C++
`httpParserAndMaker::CHttpData(Base)` classes. `fopen`/`fopen64` are
present as symbols but no evidence they're called from the HTTP GET path
against a client-controllable filename — the file I/O in this binary
targets the fixed config paths above.

Because of this, **the plan "boot script writes
`ifconfig; netstat -ln; ps` to a file under the docroot, we GET it"
cannot work on this firmware**. There is no docroot for a rootfs-side
process to write into that nwsoc would then hand out over HTTP.

## 3. Is `/opt/nwsoc_data` (or similar) rootfs-writable?

Partially, but it doesn't help — see §2 above; nwsoc doesn't turn any
persistent path into web content. For completeness:

- `/etc/fstab` mounts `tmpfs` on `/tmp` and `/var/run`. Rootfs itself is
  remounted `rw` at boot (`inittab`: `mount -o remount,rw /`). Both of
  the `/opt/nwsoc*` entries in `fstab` are commented out (`#/dev/mtdblock2
  /opt/nwsoc cramfs`, `#/dev/mtdblock3 /opt/nwsoc_data jffs2`), so the
  live layout for those two paths is set elsewhere — likely mounted from
  the second squashfs (block-0 `ipl_0_seg1` `0x370000` region) by the
  IPL/kernel init, not fstab.
- `/opt/nwsoc/NWControl` is a symlink from `/etc/init.d/S99NWControl`
  and lives on the (probably read-only) nwsoc squashfs — you would not
  modify it from a block-0 rootfs mod anyway. `NWControl` itself only
  starts `nwsoc` from `/opt/nwsoc/nwsoc`; it does not touch web content.
- The rootfs (`squashfs_rootfs1`) is what our existing block-0 flasher
  already rewrites, and it stays writable at runtime. But nothing in
  nwsoc reads from rootfs at request time to build HTTP responses, so
  writing files there produces no HTTP effect.

## 4. Existing dynamic hooks worth knowing about

There **is** an existing "Network Diagnosis" service (already documented
in `NETWORK_SERVICES.md`) — `PNCOMIFEndNetworkDiagnosis`, `diagRecvTask`,
`DiagnoseNetwork`, `pingExecute`, `diagSendArp`, `diagSendLLTD*`,
`msbrowserExecutor`, `communicationDiagnose`, `ipDiagnose`, `wlanDiagnose`.
It calls `popen`/`execl` to run ping/arp/nslookup/route/ip against
**caller-supplied targets** and reports success/failure back through
COMIF. Two important caveats for using it as an HTTP diagnostic leak:

- The reachable request path is the D4/COMIF `COMIF_LEVT_DIAGNOSENETWORK`
  channel, driven from the main-domain panel and from the EpsonNet Setup
  client, not from a raw HTTP endpoint. WebConfig's "Connection Check"
  button in the UI is one entry point, but it returns a canned
  pass/fail/status text, not raw command output.
- Even where it does return text, the values are structured result codes
  (e.g. `WLANRSC_PINGDIAG_AVERAGE_TIME`, `WLANRSC_JOIN_DIAG_CODE`), not
  the stdout of `ifconfig`/`netstat`/`ps`. It's a network *test* facility,
  not a shell-output facility.

So this hook is not a covert `ifconfig | netstat -ln | ps` sink either —
its output shape is fixed and doesn't include any of the three items we
were after.

## 5. Smallest-mod design (if you really did want HTTP-side dump)

Because of §2/§3, it cannot be done as a rootfs-only mod. Any HTTP-side
diagnostic dump needs a **listener that actually binds** on the rootfs
side — which is the exact problem `S50remoteaccess`'s inetd already tried
to solve and is the wall we're trying to clear. Two rootfs-only options
that would produce HTTP-visible output *without* touching nwsoc:

1. **Ship a tiny standalone HTTP responder on a spare port** (e.g. a
   busybox `httpd -f -p 8080 -h /tmp/www` invoked from `S60` after
   `S50remoteaccess`, with `/tmp/www/diag` re-generated every N seconds
   by a shell loop running `ifconfig; netstat -ln; ps`). This has the
   *same* bind-time risk as the inetd attempt — if binding on the
   kernel iface actually is the fault, this fails the same way. If it
   works, the docroot lives under `/tmp` (tmpfs, writable) and the URL
   is `http://<printer>:8080/diag`.
2. **Put the dump in a file the UART/serial user can `cat`** — but you
   already have UART = root, and that's the "no HTTP needed" path
   `NETWORK_SERVICES.md` §1 endorses.

If nwsoc were fair game, the smallest hook would be to hijack one of the
existing WebConfig result-page handlers (any of the `NWS_CERT_*_RESULT`
pages) to also emit `popen("ifconfig; netstat -ln; ps", "r")` output
into its response body. That's a binary-patch on nwsoc, not a rootfs mod,
and is a bigger change than any of the network-shell approaches already
in the plan. **Not recommended over UART or the fix-inetd path.**

## Ranked recommendation

1. **Trust the static evidence you already have** (§1a). The kernel-stack
   claim is proven. No new probe or mod needed.
2. **If you want one more confirmation from the wire, use WebConfig's
   built-in Network Status / Wi-Fi Status pages** in a browser at
   `http://192.168.1.238/` — they show the LAN IP on the kernel iface
   (the same one `.238`), plus MAC and Wi-Fi SSID. Zero mod, zero code.
3. **Fixing the inetd bind quirk on the rootfs side is the right next
   step for a network shell** — not adding a diagnostic HTTP endpoint.
   The static evidence says `bind 0.0.0.0:2323` on the kernel iface
   will work; the fault is in the listener, not the stack.
4. **Nwsoc-side HTTP injection is possible but oversized** for the
   question. Skip it.

## Concrete file/address citations

- `/tmp/nwsoc/nwsoc` strings for kernel-netns evidence (§1a):
  `getifaddrs`, `/proc/net/{dev,route,tcp,udp,arp}`, `ioctl(SIOCGIF*)`.
- `/tmp/nwsoc/nwsoc` HTTP dispatch symbols (§2): `init_http_task`,
  `http_ctrl_task`, `HTTPDSCallbackFuncTCP`, `HTTPDSCallbackFuncUDP`,
  `HTTPDSCBClientFunc`, `HTTPCServerConnect`, `PARSE_HTTP_LINE1`,
  `httpParserAndMaker::CHttpData(Base)`, `WebConfigEx/1.0 (EPSON_Linux)`.
- URL-prefix table (§2): `/PRESENTATION/`, `/PRESENTATIONEX/`,
  `/CONTROL/`, `/DEVICE/`, `/DOWN/`, `/EVENT/`, `/ICON/`, `/SCPD/`,
  `/UP/`, `/PRIVET/PRINTER/SUBMITDOC`, `/WSD/DEVICE`, `/WDP/PRINT`,
  `/WDP/SCAN`.
- Diagnosis service (§4): `PNCOMIFEndNetworkDiagnosis`, `diagRecvTask`,
  `DiagnoseNetwork`, `pingExecute`, `diagSendArp`, `diagSendLLTD*`,
  `msbrowserExecutor`, `communicationDiagnose`, `ipDiagnose`,
  `wlanDiagnose`, `PNDIAGExecute`.
- Rootfs layout (§3): `/tmp/rootfs_remote/etc/fstab` (both
  `/opt/nwsoc*` entries commented; `tmpfs` on `/tmp` and `/var/run`;
  root remounted `rw` in `inittab`),
  `/tmp/rootfs_remote/etc/init.d/{S40network,S50remoteaccess,rcS}`,
  `/tmp/rootfs_remote/etc/init.d/S99NWControl` →
  `/opt/nwsoc/NWControl`.
- `PNSUTLOpenSocket` @ `0x79200` and no netns primitives (from prior
  analysis, restated in §1a).
