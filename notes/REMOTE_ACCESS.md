# Remote access — getting a shell on the printer

> **⚠️ STATUS (2026-09-29, after 3 flash attempts): the network-shell payload
> below still does NOT work as flashed. UART is now the required next step to
> resolve it — blind reflashes have exhausted the useful hypothesis space.**
>
> Timeline of attempts (all block-0 rootfs mods, all flashed successfully via
> the LO protocol, all boot normally with printing intact):
> 1. **Original inetd** (`inetd /etc/inetd.conf`, empty root pw): TCP 2323
>    Connection-refused (kernel RST = no listener).
> 2. **Kernel co-IP variant** (kernel alias IP 192.168.1.240:2323): also
>    Connection-refused. (Original suspicion "nwsoc owns LAN in userspace" was
>    disproven — see below.)
> 3. **Authenticated inetd -fe + real root pw + /etc/shadow + no securetty**
>    (`FWF729TL_authshell.rcx`): still Connection-refused. This ruled out the
>    "bb_daemonize needed /dev/null" theory that motivated attempt 3.
>
> **What static analysis proved** (see `HTTP_DIAG_INVESTIGATION.md`,
> `INETD_DIAGNOSIS.md`):
>
> - nwsoc uses the standard **kernel** socket API (`PNSUTLOpenSocket` @0x79200
>   = getaddrinfo→socket→bind→listen); has **no network namespace**
>   (no unshare/setns/CLONE_NEW); reads kernel netns state via `/proc/net/*`
>   and `getifaddrs`; its `udhcpc` configures a **kernel** interface. So the
>   LAN IP is on the kernel stack in the main netns.
> - **Netfilter is dead as a cause.** Kernel has only `nf_conntrack_*` sysctls;
>   no `iptable_filter`, no `xt_target`, no netfilter modules in `/lib/modules`,
>   no `iptables` binary in the rootfs. (nwsoc's binary does contain 21
>   `iptables …` command strings referenced by code — dead code: the tool
>   isn't there and the kernel can't service the rules.)
> - Every content/parsing hypothesis (inetd getopt, `S??*` glob match, passwd/
>   shadow syntax, squashfs remount, flash-didn't-apply, FD_SETSIZE, securetty
>   changing the RST symptom, `-fe` misparse) is **individually disproven**.
>
> **What's left** is a residual ambiguity that CAN'T be distinguished from the
> LAN side: either **"S50 ran, inetd started, then silently failed between
> config-parse and bind"** (some quirk of this busybox 1.17.4 build), or
> **"S50 never ran at all"** (some quirk of how init calls the sysinit
> script). No static evidence distinguishes them.
>
> **The path forward is UART.** `NETWORK_SERVICES.md` § "Getting a shell" describes
> the stock UART root console (getty on ttyS0, empty root password on stock,
> or the MD5-crypt password from `--auth-hash` on the authshell build). Three
> commands from that shell settle everything: `ps w | grep inetd`,
> `netstat -lntp | grep 2323`, then `/bin/busybox inetd -fe /etc/inetd.conf`
> by hand and watch the stderr message.
>
> **Optional one-more-cycle bet before UART**: `FWF729TL_authshell2.rcx` uses
> an exec-form S50 (drops `-f`, adds `setsid`, redirects stdio to
> `/tmp/inetd.out`) — eliminates the remaining boot-context theories. If it
> works, we've narrowed to `-f`-specific behavior. If it still refuses, UART
> becomes strictly required (further blind guessing isn't worth the cycles).
> Either way, `/tmp/inetd.out` preserves any inetd startup error for later
> shell reading.
>
> **The flashing half is fully solved** — see `FIRMWARE_UPDATE.md` and
> `epsflasher_mac.py` (the LO handshake + EPSON-DATA stream; reports "Finished"
> with matching checksums).

Adds a network root shell to an XP-442 by patching one squashfs inside the
stock firmware and reflashing block 0 over USB. Non-destructive to the nwsoc
partition; produces a `.rcx` that `epsflasher` accepts in IPL recovery mode.

Everything below is what [`build_remote_access.py`](build_remote_access.py)
actually does (its docstring + `--help` are the source of truth) plus how to
flash the resulting image. The build script never touches the printer.

## What the build script does

`build_remote_access.py` takes a stock firmware blob (`.efu` or `.rcx`) — or an
already-unpacked `rcx_output/` — and produces a modified `.rcx`. End-to-end,
reproducible, idempotent:

1. **Obtains the RCX parts.** If given `.efu`, extracts the inner `.rcx`
   (`.efu` is just a zip). Runs `epson-reversing/rcx_unpack` to split the
   `.rcx` into its 7 parts (`rcx_header`, `ipl_{0,1}_{hdr,seg0,seg1}`).
   Concatenating these reproduces the original `.rcx` byte-for-byte.
2. **Carves the Linux rootfs squashfs** out of `ipl_0_seg1` at fixed offset
   `0x230000`. The 8-byte little-endian `bytes_used` field at offset `+0x28`
   from the `hsqs` magic gives the exact length.
3. **Reconstructs faithful metadata** by running `unsquashfs -lls` on the
   original image and writing a `mksquashfs` pseudo-file that re-adds the
   134 device nodes, the setuid bit on busybox, and any non-root
   uid/gid — none of which a non-root or macOS `unsquashfs` can materialize
   on disk. Everything else keeps its tree-side metadata (root-owned via
   `-all-root`).
4. **Adds the network shell** to the extracted rootfs:
   - `/etc/inetd.conf` — either
     `PORT stream tcp nowait root /bin/login login` (default; auth prompt,
     empty root password) or
     `PORT stream tcp nowait root /bin/sh sh -i` (no auth, `--mode shell`).
   - `/etc/init.d/S50remoteaccess` — starts `busybox inetd /etc/inetd.conf`
     at boot. `S50` runs before `S99NWControl` (nwsoc bring-up) so the
     shell is up as soon as the network stack is.
5. **Repacks the squashfs** with the same settings as the original
   (`-comp xz -b 131072 -no-xattrs`, `-all-root`, pseudo-file for
   nodes/suid/owners). Verifies the new image fits the fixed slot
   `0x230000..0x370000` = 1,310,720 bytes. Aborts if it would overrun the
   nwsoc squashfs at `0x370000`.
6. **Splices** the new squashfs back at `0x230000` in `ipl_0_seg1` (length of
   `ipl_0_seg1` is unchanged; block 0's kernel at `0x110400` and the nwsoc
   squashfs at `0x370000` stay byte-identical).
7. **Reassembles the `.rcx`** by concatenating the 7 parts, then runs
   `epson-reversing/rcx_correct` to fix the IPL checksums and SHA-1.

Output: `<input>_remote.rcx` (or `--out PATH`).

### Usage

```bash
# from a stock blob
./build_remote_access.py --firmware FWF729TL.efu

# from an already-unpacked repo state (rcx_output/ present)
./build_remote_access.py

# options
./build_remote_access.py --port 2323 --mode login --out my_remote.rcx
```

Flags of note:

- `--port N` — TCP port for the shell (default `2323`).
- `--mode {login,shell}` — `login` is the safer default (empty-root-pw auth
  prompt); `shell` binds `/bin/sh -i` directly, no authentication.
- `--rootfs DIR` — modify an existing unpacked rootfs tree instead of a fresh
  `unsquashfs`.
- `--toolkit PATH` — where `rcx_unpack` / `rcx_correct` live (default
  `epson-reversing/`).
- `--workdir DIR` / `--keep` — keep intermediates for inspection.

Requires `unsquashfs` and `mksquashfs` on `PATH` (`brew install squashfs` on
macOS; `apt install squashfs-tools` on Linux).

### Why this stays inside the original slot

The rootfs is read-only squashfs at a **fixed** flash offset. Its slot is
`0x230000..0x370000` — the next 1,310,720 bytes belong to the nwsoc squashfs.
Growing past that would corrupt nwsoc, which is what runs the network stack.
The script enforces the ceiling and asserts the `hsqs` magic at `0x370000` is
undisturbed before it writes the output.

The xz-compressed added files are small (a few hundred bytes each), so a
default build has plenty of headroom in the slot.

## Flashing

The printer must be in **IPL Mode** (Epson's LCD calls this "Program Update
Mode") to accept a partial-block flash. The full-`.rcx` path in normal
operating mode crashes the printer in practice — see
`epson-reversing/README.md` — so this doc is only about IPL-mode flashing of
the first block, which is the only block the build script modifies.

### What the flash actually sends (the "LO" protocol)

[`epsflasher_mac.py`](epsflasher_mac.py) implements the flash sequence
reverse-engineered from Epson's own Windows updater `EPFWUPD.exe` — see
[`FIRMWARE_UPDATE.md`](../FIRMWARE_UPDATE.md) for the full RE. In short,
Program Update Mode is flashed over the two D4 sockets the PUM ROM exposes
(`EPSON-CTRL` and `EPSON-DATA` — there is **no** `fwu:ctrl` socket in this
ROM, contrary to an earlier hypothesis):

1. **Handshake on `EPSON-CTRL`** — five binary commands, each framed as
   `<opcode:2 ASCII><arglen:u16 LE><arg>`:
   - `st 01 00 01` (`73 74 01 00 01`) — status
   - `vi 01 00 04 / 00 / FF / 07` — version/identity queries (`vi…FF` → `ROIPLJ`)
   - `fl 01 00 01` (`66 6c 01 00 01`) — flash-prepare (reply-gated)
   - `gm 01 00 01` (`67 6d 01 00 01`) — go-mode / arm the loader
   This is the **binary** form the updater uses — not the colon form
   (`vi:FF`) earlier attempts sent. (dirty4's `cmd2()` already produces it.)
2. **Stream on `EPSON-DATA`** — the raw firmware block(s) `Z_1` (and `Z_2`
   unless `--only-first-block`), chunked at MTU−6, EOM on each block's last
   packet. No per-chunk wrapper.
3. **End command on `EPSON-CTRL`** — a "firmware update end" frame
   (`--end-cmd`, exact bytes still unconfirmed), then close.

The reason this sequence is worth trying: every prior EPSON-DATA attempt
stalled on the credit gate (`state[+0x1574]`, see
[`FIRMWARE_UPDATE.md`](../FIRMWARE_UPDATE.md)). The `fl`/`gm` handshake on
EPSON-CTRL — which we had never sent in the correct binary form before the
data stream — is the leading candidate for what arms that gate. Run with `-d`
and watch whether `CreditRequest` starts granting after `gm`.

### macOS vs Linux transport

Upstream `epson-reversing/epsflasher` opens `/dev/usb/lp0` (Linux `usblp`
kernel character device) with a raw file-descriptor. macOS has no equivalent,
so on macOS use [`epsflasher_mac.py`](epsflasher_mac.py), which:

- Talks to the printer over `libusb1` bulk endpoints instead of `/dev/usb/lp0`.
- Reuses the upstream `dirty4.py` IEEE 1284.4 framing, by shimming its
  `os.read` / `os.write` calls to dispatch to the USB transport when the port
  is a `UsbPort` instead of an int fd.
- Calls libusb's `setAutoDetachKernelDriver(True)` and, as a fallback,
  explicit `detachKernelDriver`, so macOS's `usbprint` class driver is
  released from userspace. **No `kextunload` is needed** and this works
  under SIP.
- Forces D4-header `credit=0` on every data packet (both the CTRL commands
  and the EPSON-DATA payload) — nonzero credit on data packets locks up the
  printer's hardware buffer.

The upstream Linux `epsflasher` predates this RE and streams the whole image
on a single channel; it does **not** run the `EPSON-CTRL` handshake above, so
prefer `epsflasher_mac.py` (it runs anywhere libusb1 does, including Linux)
for Program Update Mode.

### Prereqs (macOS)

```bash
brew install libusb           # C library
pip3 install --user libusb1   # Python bindings (`import usb1`)
pip3 install --user tqdm      # optional but strongly recommended: progress bar
```

`tqdm` is optional but **without it the flasher is completely silent during
the ~2–5 minute bulk write**, which is easy to mistake for a hang. Install it.

Upstream `epsflasher` uses `#!/usr/bin/env python` in its shebang, which
doesn't exist on modern macOS (only `python3`). Invoke either script with
`python3 ...`; `epsflasher_mac.py` does this correctly already.

### Enter IPL Mode

Power off the printer, then hold **Stop + Color Copy + Info + Power** together
for ~2 seconds until the LCD reads **"Program Update Mode"** and the printer
re-enumerates on USB with:

- **Vendor ID `0x04b8`, Product ID `0x0007`** (distinct from the normal-mode
  PID)
- Manufacturer `EPSON`, product string typically `USB2.0 Printer (Hi-speed)`

Confirm from System Information's USB section, or:

```bash
python3 -c "import usb1; [print(hex(d.getVendorID()), hex(d.getProductID())) for d in usb1.USBContext().getDeviceList()]"
```

You should see `0x4b8 0x7` in the list. If not, power-cycle and retry the key
combo. Note: the key combo is documented for the XP-240 in
`epson-reversing/README.md` and appears to match on the XP-442. The reliable
confirmation is the `0x0007` PID, whatever buttons produced it.

### Flash

**macOS / Linux (via libusb1) — recommended, runs the EPSON-CTRL handshake:**

```bash
uv run --script ./epsflasher_mac.py FWF729TL_remote.rcx --only-first-block
```

Add `-d` for verbose D4 protocol logging (recommended on first run — it shows
each CTRL reply and whether EPSON-DATA starts granting credit).

The upstream `epson-reversing/epsflasher` (`/dev/usb/lp0`, Linux only) predates
the updater RE and does not run the `st/vi/fl/gm` handshake, so it is not
expected to work in Program Update Mode; use `epsflasher_mac.py`.

What to expect during the write:

- The script prints, roughly:
  ```
  Found Epson 04b8:0007 on bus <N> addr <N>
  Bulk endpoints: IN=0x85 OUT=0x04 (iface 0)
  EPSON-CTRL handshake...
  CTRL st 01 00 01 -> '...'
  CTRL vi 01 00 FF -> 'vi:FF:ROIPLJ;\f'
  CTRL fl 01 00 01 -> '...'
  CTRL gm 01 00 01 -> '...'
  EPSON-DATA open (MTU capped to 512; 506 payload bytes/packet).
  EPSON-DATA <- Z_1 (7483392 bytes)
  ```
- Then `tqdm` should show a bar crawling forward. Wall-clock throughput
  is ~50–100 KB/s, so **the ~7.5 MB block-0 write takes 2–5 minutes**. This
  is normal, not a hang — D4 is credit-flow-controlled per packet, and the
  printer is doing real NOR flash writes as bytes arrive. To confirm the
  process is alive rather than stuck, in another shell:
  ```bash
  ps -o pid,etime,pcpu,stat,command -p <PID>     # STAT S+, low CPU is expected
  sample <PID> 1 | head -80                      # stack should sit in __select
  ```
- When the write finishes, the script prints `Firmware write complete.` and
  the printer reboots into normal mode on its own. **Do not power-cycle mid-
  flash**: the LCD may go blank briefly during finalization; wait for it.

### Confirm the shell is up

After the printer reboots and gets a DHCP lease (check the panel, your DHCP
server, or `arp -a`):

```bash
telnet <printer-ip> 2323
```

- `--mode login` (build script default): a login prompt. User `root`, empty
  password.
- `--mode shell`: dumped straight into `/bin/sh` as root, no prompt.

The shell is a busybox `ash`. `getty`, `login`, `inetd`, and busybox itself
(setuid) are all present in the stock image — the modification only added the
two config files.

### Rollback

Same procedure with the stock `.rcx`:

```bash
uv run --script ./epsflasher_mac.py FWF729TL.rcx --only-first-block
```

Flash offsets are identical, so this cleanly reverts block 0. Block 1 (nwsoc
+ scanner IPL) is never touched by either build.

### Troubleshooting

- **`No such file or directory: '/dev/usb/lp0'`** — you ran the upstream
  Linux-only `epsflasher` on macOS. Use `epsflasher_mac.py` instead.
- **`env: python: No such file or directory`** — upstream shebang assumes
  `python`. Prefix with `python3`.
- **`ModuleNotFoundError: No module named 'usb1'`** — install with
  `pip3 install --user libusb1`. If pip complains about "externally-managed
  environment", add `--break-system-packages` or use a venv.
- **Flasher looks frozen after `Writing firmware…`** — you almost certainly
  don't have `tqdm` installed and it's writing silently. Confirm liveness
  with `ps` / `sample` as above; install `tqdm` next time.
- **`Unexpected response 'vi:NA;\f...' does not start with 'vi:04:'`** —
  you're using upstream `epsflasher` in IPL mode; it doesn't accept the IPL
  version reply. Use `epsflasher_mac.py`, or patch the upstream to relax the
  prefix check.
- **`Cannot claim USB interface 0`** — CUPS is holding the printer. Remove
  it from System Settings → Printers & Scanners and retry. `kextunload` of
  `AppleUSBPrint` is generally not needed (and SIP-blocked on modern macOS);
  `epsflasher_mac.py` uses libusb's userspace detach instead.
- **Printer never reappears after flash** — IPL mode is still there as a
  safety net. Re-enter it and reflash the stock `.rcx`.

## Alternative: UART

Skip everything above if a USB-serial adapter is available.
`inittab` runs a getty on `ttyS0` at 115200 8N1, and root has an empty
password. Locate the ttyS0 test points on the mainboard, connect a 3.3V TTL
adapter (TX↔RX, RX↔TX, GND↔GND — do **not** connect Vcc), and:

```bash
screen /dev/tty.usbserial-* 115200
```

Press enter, log in as `root` with no password. Zero firmware modification;
covered in [NETWORK_SERVICES.md § Getting a shell](../NETWORK_SERVICES.md#getting-a-shell-what-actually-works).

## Files

- [build_remote_access.py](build_remote_access.py) — the build script (source
  of truth for the image mutation; `--help` for flag list).
- [epsflasher_mac.py](epsflasher_mac.py) — macOS libusb1 wrapper around
  upstream `dirty4.py` / `rcx.py`. Used in place of upstream `epsflasher`
  when flashing from a Mac. Handles the `vi:NA` IPL-mode reply and the
  usbprint kernel-driver claim from userspace.
- [epson-reversing/epsflasher](epson-reversing/epsflasher) — upstream USB
  flasher (Linux only; requires `/dev/usb/lp0`). `--only-first-block` is
  required in IPL mode.
- [epson-reversing/dirty4.py](epson-reversing/dirty4.py) — IEEE 1284.4 / D4
  protocol implementation. Untouched by `epsflasher_mac.py`; only its `os`
  module reference is monkey-patched to route reads/writes through
  libusb1 when the port object is a `UsbPort` instead of an int fd.
- [epson-reversing/rcx_correct](epson-reversing/rcx_correct) — checksum fixer,
  invoked internally by the build script.
- [ROOTFS.md](../ROOTFS.md) — block 0 layout, squashfs offsets.
- [NETWORK_SERVICES.md](../NETWORK_SERVICES.md) — what's in the rootfs, init sequence,
  UART route.
