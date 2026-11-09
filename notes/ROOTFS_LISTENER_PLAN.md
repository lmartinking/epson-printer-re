# Rootfs listener plan — authenticated network shell via a rootfs mod

Plan for an **authenticated** network shell on the XP-442, delivered entirely
by a block-0 rootfs modification alongside a running (unmodified) `nwsoc`.
Supersedes the earlier `build_remote_access.py` payload (which failed because
its busybox `inetd` never bound) and the shelved `NWSOC_NETSHELL_DESIGN.md`
(no longer needed — see below).

> **⚠️ Execution status (2026-09-29): plan executed, still fails.** The
> `build_remote_access.py` builder was updated with `--auth-hash` (required),
> and `FWF729TL_authshell.rcx` was flashed. Result: TCP 2323 still returns
> kernel-RST ("Connection refused") from the LAN, same symptom as prior
> attempts. Static evidence from
> [INETD_DIAGNOSIS.md](INETD_DIAGNOSIS.md) rules
> out every hypothesis this plan was built on; residual ambiguity is "S50
> silently failed post-parse pre-bind" vs "S50 never ran at all," which cannot
> be distinguished from off-device. **UART is now the required next step.**
> A one-more-cycle fallback image (`FWF729TL_authshell2.rcx`) with an
> exec-form S50 (drops `-f`, adds `setsid`, redirects stdio to
> `/tmp/inetd.out`) exists as an optional pre-UART bet.
>
> The tooling (build pipeline, auth requirement, slot-fit checks, verification
> steps) is correct and re-usable. Only the *runtime outcome* is unresolved.
> See `REMOTE_ACCESS.md`'s STATUS banner for the current authoritative view.

---

## Premise (now confirmed, no UART/HTTP-dump needed)

Static evidence from the `nwsoc` binary
([HTTP_DIAG_INVESTIGATION.md](HTTP_DIAG_INVESTIGATION.md),
[NWSOC_CMD_CONSOLE_RE.md](NWSOC_CMD_CONSOLE_RE.md),
[NWSOC_NETSHELL_DESIGN.md](NWSOC_NETSHELL_DESIGN.md)):

- Serves TCP via the **kernel** socket API: `PNSUTLOpenSocket` @0x79200 =
  `getaddrinfo → socket → bind → listen`, with an `FD_SETSIZE` (fd < 1024)
  guard.
- **No network namespaces:** no `unshare`/`setns`/`CLONE_NEW*` in the binary.
- Reads kernel network state directly: `getifaddrs`, `/proc/net/dev`,
  `/proc/net/route`, `/proc/net/tcp`, `ioctl(SIOCGIFINDEX/SIOCGIFHWADDR)`.
- Runs its own `udhcpc` on a **kernel** interface (`ether_load.sh`,
  `startup_dhcp.sh`).

⇒ The printer's LAN IP (192.168.1.238) is on a **kernel `eth0`/`wlan0`
in the main netns**. Any rootfs process that binds `0.0.0.0:<port>` on that
kernel is reachable from the LAN. The earlier "connection refused" was
kernel-RST-for-no-listener — busybox `inetd` never bound — not an
architectural wall.

## Does this need nwsoc? Yes — and we don't touch it.

nwsoc must keep running:

- It's what associates the Wi-Fi radio (BCM43143 + `wlan.confdef`) and runs
  `udhcpc` to obtain the LAN lease. Without nwsoc there is no interface up
  and no IP for our listener to bind.
- It IS the printer — HTTP/443/LPD/IPP/9100/SNMP are all served by nwsoc.
  Killing it destroys printing.

Coexistence: our listener binds a **non-nwsoc port** on the same kernel
interface. Ports to avoid: 80, 443, 515, 631, 9100, 161, 3702, 5353 (and
whatever else `netstat -ln` in situ shows). Suggested default: **2222**
(SSH-adjacent, unlikely to clash).

---

## Design

### Authentication (hard requirement, no open-shell option)

The failure mode we're avoiding is "unauthenticated LAN root port," which is a
network backdoor. Two options, in order of preference:

**A. Dropbear SSH (recommended).**
- Real SSH — password or key auth, encrypted transport, no hand-rolled crypto.
- **Cost:** need a `dropbear` binary cross-compiled for the printer's ARM
  glibc (uClibc?) toolchain. Not in stock busybox. Also needs host keys
  (generated on first boot into `/var` or shipped in-image).
- **Where it fits:** ~200-500 KB static-linked in the rootfs squashfs. Verify
  fit in the 1,310,720-byte block-0 slot before committing (current build
  used ~1.13 MB of 1.28 MB — headroom is ~124 KB; a dropbear binary likely
  fits only if built minimal and stripped, otherwise something else has to
  go).

**B. inetd + `/bin/login` with a REAL root password (fallback).**
- All pieces already in stock busybox (`inetd`, `login`, `getty`); no extra
  binary needed → fits trivially in the slot.
- **Auth:** replace the empty root password with a set one. Add
  `/etc/shadow` with a `crypt(3)`-hashed password for root (busybox `login`
  reads /etc/shadow if present), OR set the password field directly in
  `/etc/passwd` (`root:$1$…:0:0:root:/root:/bin/ash`).
- **The bind bug from last time:** whatever went wrong with inetd previously
  is the same risk here. We need a reliable listener; two mitigations:
    1. Use `busybox tcpsvd` instead of `inetd` — simpler CLI, one line:
       `tcpsvd -vE 0.0.0.0 2222 /bin/login &` (verify `tcpsvd` applet is
       compiled in — `strings bin/busybox | grep -x tcpsvd`).
    2. Or a small shell/socat wrapper if a suitable listener isn't in busybox
       — least attractive.

Independently useful either way: also drop a tiny **diagnostic file** into
`/tmp` at boot (`ifconfig; netstat -ln; ps > /tmp/coip.log` — we already put
a `coip.log` write into the earlier build's script). It won't be served over
HTTP (no docroot), but it's readable the moment you get any shell.

### Init hook

- Same slot as before: `/etc/init.d/S50remoteaccess` runs from `rcS` before
  `S99NWControl` starts nwsoc. **But** the listener wants nwsoc to have
  brought the interface up before it binds — bind order doesn't strictly
  require it (bind on `0.0.0.0` works with a link-down iface too), but a
  visible "waited for carrier" log helps debugging.
- Structure: `start` case backgrounds a wait-for-carrier poll, then execs
  the listener. Do NOT rely on `case "$1"` matching anything other than
  `start` — that was the bug in the very first `S50remoteaccess`; `rcS` does
  call it with `start`, but keep an unconditional fallback too.
- Log to `/tmp/rmaccess.log` for post-mortem via any shell.

### Failure surface, and why it's low

- No nwsoc modification → no risk to printing, WSD, IPP, etc.
- All state changes are additions to the read-only squashfs; nothing existing
  is replaced. Rollback = reflash stock.
- The one live risk is our listener binding a port nwsoc later expects. Fix:
  pick a port from the "unused" range and cross-check with the port scan we
  already have (80/443/515/631/9100 open; 2222/2323/8022 all closed = safe
  choices).

---

## Deliverables (concrete)

1. **`build_remote_access.py` update** (in-place, keep the same script):
   - Add an auth mode: `--auth=password:<hash>` (writes `/etc/shadow` with
     that crypt hash for root; leaves `/etc/passwd` intact) or
     `--auth=ssh-key:<pubkey path>` (writes `/root/.ssh/authorized_keys`
     when dropbear is included).
   - Replace the inetd payload with a `tcpsvd`-based `S50remoteaccess`:

     ```sh
     #!/bin/sh
     LOG=/tmp/rmaccess.log
     start() {
       # wait briefly for nwsoc to bring an interface up (best-effort;
       # bind works even without it)
       i=0
       while [ $i -lt 60 ]; do
         for IF in eth0 wlan0; do
           [ -e /sys/class/net/$IF ] && \
             [ "$(cat /sys/class/net/$IF/carrier 2>/dev/null)" = "1" ] && \
             break 2
         done
         i=$((i+1)); sleep 1
       done
       # boot-time diagnostic dump (readable from any shell we get)
       { echo "== ifconfig =="; /sbin/ifconfig -a
         echo "== netstat -ln =="; /bin/netstat -ln 2>/dev/null || \
            /bin/busybox netstat -ln
         echo "== ps =="; /bin/ps
       } > $LOG 2>&1
       # authenticated listener
       /bin/busybox tcpsvd -vE 0.0.0.0 2222 /bin/login >>$LOG 2>&1 &
     }
     case "${1:-start}" in
       start) start ;;
       stop)  /bin/busybox killall tcpsvd 2>/dev/null ;;
     esac
     exit 0
     ```

   - Preflight checks in the script:
     - Verify `tcpsvd` is a compiled busybox applet in the extracted rootfs
       (`strings bin/busybox | grep -x tcpsvd`); if not, fall back to
       `inetd`-with-a-diagnostic-poke or fail loudly.
     - Verify the auth mode isn't the empty-password default. Refuse to
       build without `--auth`.
     - Verify the chosen port isn't in the nwsoc-in-use list.

2. **A separate optional builder for the dropbear variant** if you want SSH:
   `build_remote_access_ssh.py` — needs a pre-built `dropbear` ARM binary
   supplied via `--dropbear PATH`, plus host-key generation on first boot.
   (Design only for now; the tcpsvd+login path is enough to prove out the
   theory and gets you an authenticated shell with zero new binaries.)

3. **A one-page test doc** (`ROOTFS_LISTENER_TESTING.md`) covering:
   - Flash sequence (`epsflasher_mac.py FWF729TL_remote.rcx --only-first-block -d`).
   - What to check post-reboot: `nc -vz 192.168.1.238 2222` (bind ok?),
     then `telnet 192.168.1.238 2222` → root password.
   - If it fails: pull `/tmp/rmaccess.log` off the device via UART (fallback
     shell) — since we can't HTTP-dump.
   - Rollback: reflash stock `FWF729TL.rcx`.

---

## Sequence to execute

1. Verify `tcpsvd` applet is compiled into the printer's busybox (30-sec
   static check on the already-unpacked rootfs).
2. Ask you for a **root password** (or accept a pre-hashed value) — this is
   the auth material and it's yours to choose.
3. Update `build_remote_access.py` with the new payload + `--auth` requirement.
4. Build a test image `FWF729TL_authshell.rcx`.
5. Confirm image size fits the 1,310,720-byte slot.
6. Flash → connect → done.

Rollback is one reflash of the stock `.rcx`.

## Busybox applet inventory (verified)

Extracted from the `applet_names` table in `/tmp/rootfs_remote/bin/busybox` (ARM ELF, stripped) via `strings`. The applet table is a contiguous NUL-terminated run from `arping` through `zcat` (see strings offsets ~299–437 in the dump). Usage-string block cross-checked — no additional applet names appear only there.

**BusyBox version:** `BusyBox v1.17.4 (2017-08-02 15:44:55 JST)`

**Checked applets:**

- `tcpsvd`   — **absent**
- `udpsvd`   — **absent**
- `inetd`    — **present** (reconfirmed)
- `nc`       — **absent** (no busybox netcat)
- `telnetd`  — **absent** (only the `telnet` client is compiled in — reconfirms prior finding: no telnet server)
- `dropbear` — **absent** (not a busybox applet anyway; also no separate binary in this rootfs check)
- `sshd`     — **absent**
- `getty`    — **present** (reconfirmed)
- `login`    — **present** (reconfirmed)

**Other network-server-ish applets in the list:** none. The full applet table contains only `inetd` as a socket-listener server. Other network-adjacent applets are all clients or diagnostics: `telnet` (client), `arping`, `ping`, `ping6`, `nslookup`, `ifconfig`, `ifup`, `ifdown`, `ifenslave`, `ifplugd`, `netstat`, `route`, `hostname`, `dnsdomainname`, `hostid`. No `httpd`, `ftpd`, `tftpd`, `nbd-client`, `udhcpd`, `dnsd`, or `fakeidentd`.

**Bottom line — listener choice:**
`tcpsvd` is not available, so the one-line `tcpsvd -vE 0.0.0.0 PORT PROG` pattern is off the table. The only compiled-in listener applet is **`inetd`**, so the shell-service mod must be wired as an inetd service: add a line to an `inetd.conf` (e.g. `PORT stream tcp nowait root /bin/login login`, or a wrapper that enforces auth before exec) and arrange for `busybox inetd /path/to/inetd.conf` to be launched from rc. No other in-tree option without shipping a new static binary (e.g. a statically-linked `dropbear` or `ncat`).
