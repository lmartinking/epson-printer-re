#!/usr/bin/env python3
"""Build an XP-442 firmware image that opens an AUTHENTICATED root shell on
the network.

End-to-end, reproducible, and idempotent. Given a stock firmware blob (.efu or
.rcx) *or* an already-unpacked tree, it:

  1. obtains the unpacked RCX parts (unpacks the blob if needed),
  2. carves the Linux rootfs squashfs out of Block 0 (`ipl_0_seg1` @ 0x230000),
  3. adds a busybox `inetd` service that binds `/bin/login` to a TCP port,
     with a REAL root password (crypt-hash injected into /etc/shadow),
  4. repacks the squashfs (restoring the 134 device nodes + setuid busybox that
     a non-root/macOS unpack drops), splices it back at the same offset and
     size,
  5. reassembles the .rcx and fixes its IPL checksums (rcx_correct).

Authentication is mandatory. --auth-hash is required; the tool refuses to
build an unauthenticated image. Generate the hash on your Mac with:

    openssl passwd -1        # $1$... MD5-crypt (universally supported)
    openssl passwd -6        # $6$... SHA-512    (if your openssl has it)

then pass the resulting $1$.../$6$... string as --auth-hash.

Rootfs changes (all confined to block 0's squashfs):
  - /etc/inetd.conf                       — one service line, PORT stream tcp ...
  - /etc/init.d/S50remoteaccess           — starts inetd -fe (foreground,
                                            backgrounded) so bb_daemonize's
                                            /dev/null dependency isn't hit
  - /etc/shadow                           — root's hash (mode 0600, root:root)
  - /etc/passwd                           — root's password field flipped from
                                            "" (empty) to "x" (see /etc/shadow)
  - /etc/securetty                        — REMOVED (busybox 1.17.4 login
                                            rejects socket-invoked root logins
                                            when this file exists and lacks
                                            pts/*; removing it defaults login
                                            to "allow all ttys")

nwsoc is not modified and keeps running (it's what associates Wi-Fi + runs
udhcpc, and serves HTTP/443/LPD/IPP/9100/SNMP). The listener rides the same
kernel eth0/wlan0 nwsoc brings up. Bind is on 0.0.0.0 so the listener is
robust to Wi-Fi/DHCP timing (bind succeeds with no interface up; socket
answers the moment the interface comes online).

It never touches the device. Flashing is a separate, documented manual step
(see REMOTE_ACCESS.md; use epsflasher_mac.py --only-first-block).

The rootfs is read-only squashfs at a *fixed* flash offset, so the new image
must stay within the original slot (0x230000..0x370000 = 1,310,720 bytes). The
script enforces this and leaves Block 0's kernel and the nwsoc squashfs (Block
0 @ 0x370000) byte-identical.

Usage:
    ./build_remote_access.py --firmware FWF729TL.efu \\
        --auth-hash '$1$...$...' --port 2323 --out FWF729TL_authshell.rcx
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# --- Fixed layout of ipl_0_seg1 (Block 0), from FIRMWARE_LAYOUT.md / ROOTFS.md -------
KERNEL_OFF = 0x110400      # uImage
SQ1_OFF = 0x230000         # Linux rootfs squashfs (what we modify)
SQ2_OFF = 0x370000         # nwsoc squashfs (must stay byte-identical, at this offset)
SLOT = SQ2_OFF - SQ1_OFF   # 0x140000 = 1,310,720 bytes: hard size ceiling for rootfs

# Parts produced by rcx_unpack, in .rcx order. Concatenating them reproduces the
# original .rcx byte-for-byte (verified against FWF729TL.rcx).
RCX_PARTS = [
    "rcx_header",
    "ipl_0_hdr", "ipl_0_seg0", "ipl_0_seg1",
    "ipl_1_hdr", "ipl_1_seg0", "ipl_1_seg1",
]


def die(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


def need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        die(f"required tool not found on PATH: {tool}")
    return p


# --- squashfs metadata reconstruction ----------------------------------------
# A non-root (or macOS) `unsquashfs` cannot recreate device nodes, the setuid
# bit, or non-root ownership. We rebuild the fs from the extracted tree plus a
# mksquashfs pseudo-file that re-adds exactly those, read from the ORIGINAL image
# so the result is faithful on any host.

def _mode_to_octal(perm: str) -> int:
    """'rwsr-xr-x' (9 chars) -> octal int including suid/sgid/sticky."""
    bits = 0
    for i, ch in enumerate(perm):
        if ch in "rwx":
            bits |= 1 << (8 - i)
    if perm[2] in "sS":
        bits |= 0o4000
    if perm[5] in "sS":
        bits |= 0o2000
    if perm[8] in "tT":
        bits |= 0o1000
    return bits


def _idnum(tok: str) -> int:
    # unsquashfs prints names; gid 0 shows as 'wheel' on macOS, 'root' on Linux.
    return 0 if tok in ("root", "wheel") else int(tok)


_LLS = re.compile(
    r"^(?P<mode>\S+)\s+(?P<owner>\S+)\s+(?P<mid>.*?)\s+"
    r"\d{4}-\d\d-\d\d\s+\d\d:\d\d\s+(?P<path>.*)$"
)


def build_pseudo(orig_sq: Path, pseudo_path: Path, unsquashfs: str) -> int:
    """Write a mksquashfs pseudo-file from the original image. Returns node count."""
    out = subprocess.run([unsquashfs, "-lls", str(orig_sq)],
                         check=True, capture_output=True, text=True).stdout
    lines = []
    nodes = 0
    for ln in out.splitlines():
        if not ln or ln[0] not in "cbd-l":
            continue
        m = _LLS.match(ln)
        if not m:
            continue
        typ = ln[0]
        rel = m["path"].split("/", 1)
        if len(rel) == 1:            # the root entry itself ("squashfs-root")
            continue
        name = rel[1]
        uid, gid = (_idnum(t) for t in m["owner"].split("/"))
        mode = _mode_to_octal(m["mode"][1:10])
        if typ in "cb":             # device node: recreate it
            maj, minr = (int(x) for x in m["mid"].replace(",", " ").split())
            lines.append(f"{name} {typ} {mode:o} {uid} {gid} {maj} {minr}")
            nodes += 1
        elif mode & 0o7000 or uid or gid:
            # only override entries with special bits or non-root ownership;
            # everything else keeps the tree's own (root, via -all-root) metadata
            lines.append(f"{name} m {mode:o} {uid} {gid}")
    pseudo_path.write_text("\n".join(lines) + "\n")
    return nodes


# --- rootfs mutation ----------------------------------------------------------

_HASH_RE = re.compile(r"^\$[1356][ay]?\$[^$]{1,64}\$[A-Za-z0-9./]+$")


def mutate_rootfs(root: Path, port: int, auth_hash: str) -> None:
    # --- Validate the auth hash ------------------------------------------
    if not _HASH_RE.match(auth_hash):
        die(f"--auth-hash doesn't look like a crypt(3) hash "
            f"($1$/$5$/$6$/$y$...): {auth_hash!r}. Generate one with "
            f"`openssl passwd -1` (recommended for busybox 1.17.4) or -6.")

    etc = root / "etc"
    etc.mkdir(exist_ok=True)

    # --- 1. inetd.conf: authenticated login on <port> --------------------
    svc = f"{port} stream tcp nowait root /bin/login login"
    (etc / "inetd.conf").write_text(
        "# remote access — added by build_remote_access.py\n"
        f"# authenticated login prompt on TCP {port} (root pw from /etc/shadow)\n"
        f"{svc}\n"
    )

    # --- 2. /etc/shadow: root's real password hash -----------------------
    # Format: user:hash:last_change:min:max:warn:inactive:expire:reserved
    # Values 0:0:99999:7::: = min-age 0, max-age 99999 (~273y), 7-day warn.
    # Only root needs a real entry; other accounts stay whatever they were.
    shadow_lines = [f"root:{auth_hash}:0:0:99999:7:::"]
    (etc / "shadow").write_text("\n".join(shadow_lines) + "\n")
    (etc / "shadow").chmod(0o600)

    # --- 3. /etc/passwd: flip root's password field from "" to "x" -------
    # Stock is `root::0:0:root:/root:/bin/ash` (empty pw). "x" means "see
    # /etc/shadow"; belt-and-braces so an accidental /etc/shadow absence
    # doesn't silently fall back to empty password.
    passwd_path = etc / "passwd"
    if passwd_path.exists():
        lines = passwd_path.read_text().splitlines()
        for i, line in enumerate(lines):
            parts = line.split(":")
            if parts and parts[0] == "root" and len(parts) >= 2:
                parts[1] = "x"
                lines[i] = ":".join(parts)
                break
        else:
            die("/etc/passwd has no 'root' line — refusing to build a rootfs "
                "that won't let root log in.")
        passwd_path.write_text("\n".join(lines) + "\n")
    else:
        die("/etc/passwd missing from rootfs — the unpack is malformed.")

    # --- 4. /etc/securetty: REMOVE ---------------------------------------
    # Busybox 1.17.4 login rejects root when securetty exists and doesn't
    # list the current tty; a socket-invoked login has no tty and matches
    # "UNKNOWN", which isn't in the stock securetty (tty1..8, ttyS0-1).
    # Deleting the file makes busybox login default to "allow all ttys".
    sec = etc / "securetty"
    securetty_note = "(was not present)"
    if sec.exists():
        sec.unlink()
        securetty_note = "removed"

    # --- 5. S50remoteaccess: start inetd in foreground, backgrounded -----
    # -f skips bb_daemonize (which needs /dev/null and was the suspected
    #   silent-fail cause of the previous build's non-binding inetd).
    # -e sends inetd's own errors to stderr (would go to console/UART if
    #   we had one wired; here it's a no-op but harmless).
    # `&` in the script backgrounds inetd so rcS proceeds to S99NWControl.
    # No sleep, no wait-for-carrier: bind(0.0.0.0) works with no interface
    # up; the socket answers the moment DHCP finishes — robust to any
    # Wi-Fi/DHCP timing.
    initd = etc / "init.d"
    initd.mkdir(parents=True, exist_ok=True)
    s50 = initd / "S50remoteaccess"
    # Exec-form S50 (per INETD_DIAGNOSIS.md):
    #  - drops -f (let inetd daemonize normally; -f path was unexplained)
    #  - `setsid` cleanly detaches from any session/controlling tty init
    #    hands down
    #  - stdio redirected to /tmp/inetd.out (tmpfs); any bind/config error
    #    inetd prints is preserved and readable from a shell (UART)
    #  - subshell + `&` so the setsid'd process is a clean child of init
    s50.write_text(
        "#!/bin/sh\n"
        "# Authenticated inetd listener on TCP {port}.\n"
        "# Exec-form (post-3rd-attempt diagnosis): no -f, setsid, stdio->tmp.\n"
        "case \"${{1:-start}}\" in\n"
        "  start)\n"
        "    ( /bin/busybox setsid /bin/busybox inetd /etc/inetd.conf \\\n"
        "        </dev/null >/tmp/inetd.out 2>&1 ) &\n"
        "    ;;\n"
        "  stop)\n"
        "    /bin/busybox killall inetd 2>/dev/null\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n".format(port=port)
    )
    s50.chmod(0o755)

    print(f"  + /etc/inetd.conf         ({svc})")
    print(f"  + /etc/shadow             (root:{auth_hash[:12]}…, 0600)")
    print(f"  + /etc/passwd             (root password field: '' -> 'x')")
    print(f"  - /etc/securetty          {securetty_note}")
    print(f"  + /etc/init.d/S50remoteaccess "
          f"(inetd -fe, backgrounded, runs before nwsoc)")


# --- squashfs carve / repack --------------------------------------------------

def squashfs_size(seg1: bytes, off: int) -> int:
    if seg1[off:off + 4] != b"hsqs":
        die(f"no squashfs magic at 0x{off:x} in ipl_0_seg1")
    (bytes_used,) = struct.unpack_from("<Q", seg1, off + 0x28)
    return bytes_used


# --- part acquisition ---------------------------------------------------------

def ensure_parts(args, work: Path) -> Path:
    """Return a directory containing all RCX_PARTS, unpacking the blob if needed."""
    parts = Path(args.parts)
    if all((parts / p).exists() for p in RCX_PARTS):
        print(f"[parts] using existing unpacked state: {parts}")
        return parts

    if not args.firmware:
        die(f"no unpacked parts in {parts} and no --firmware given. "
            f"Pass --firmware PATH (.efu/.rcx) or point --parts at an rcx_output/.")

    fw = Path(args.firmware)
    if not fw.exists():
        die(f"firmware not found: {fw}")

    rcx = fw
    if fw.suffix.lower() == ".efu":
        print(f"[unpack] .efu is a zip; extracting the .rcx")
        with zipfile.ZipFile(fw) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".rcx")]
            if not names:
                die(".efu contains no .rcx")
            z.extract(names[0], work)
            rcx = work / names[0]

    outdir = work / "rcx_output"
    print(f"[unpack] rcx_unpack {rcx.name} -> {outdir}")
    run([sys.executable, str(Path(args.toolkit) / "rcx_unpack"), str(rcx), str(outdir)])
    missing = [p for p in RCX_PARTS if not (outdir / p).exists()]
    if missing:
        die(f"rcx_unpack did not produce: {', '.join(missing)}")
    return outdir


# --- main ---------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", help=".efu or .rcx to unpack (optional if --parts is already unpacked)")
    ap.add_argument("--parts", default="rcx_output",
                    help="dir with rcx_unpack output (default: rcx_output)")
    ap.add_argument("--rootfs", default=None,
                    help="existing unpacked rootfs tree to modify (default: fresh unsquashfs from the blob)")
    ap.add_argument("--toolkit", default="epson-reversing",
                    help="abrasive/epson-reversing checkout (default: epson-reversing)")
    ap.add_argument("--out", default=None, help="output .rcx (default: <input>_remote.rcx)")
    ap.add_argument("--port", type=int, default=2323, help="TCP port for the shell (default: 2323)")
    ap.add_argument("--auth-hash", required=True, metavar="HASH",
                    help="REQUIRED. crypt(3) hash of root's password "
                         "($1$.../$5$.../$6$...). Generate with `openssl passwd -1` "
                         "(recommended for busybox 1.17.4) or -6. This is written "
                         "verbatim to /etc/shadow. The tool refuses to build "
                         "without this — unauthenticated shell images are not "
                         "produced by this builder.")
    ap.add_argument("--workdir", default=None, help="keep intermediates here (default: temp dir)")
    ap.add_argument("--keep", action="store_true", help="don't delete the work dir")
    args = ap.parse_args()

    unsquashfs = need("unsquashfs")
    mksquashfs = need("mksquashfs")
    toolkit = Path(args.toolkit)
    rcx_correct = toolkit / "rcx_correct"
    if not rcx_correct.exists():
        die(f"rcx_correct not found in toolkit: {rcx_correct}")

    tmp = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="xp442_ra_"))
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"[work] {tmp}")

    try:
        parts = ensure_parts(args, tmp)
        seg1_path = parts / "ipl_0_seg1"
        seg1 = bytearray(seg1_path.read_bytes())

        # carve original rootfs squashfs
        sq_len = squashfs_size(seg1, SQ1_OFF)
        orig_sq = tmp / "sq1.orig"
        orig_sq.write_bytes(seg1[SQ1_OFF:SQ1_OFF + sq_len])
        print(f"[carve] rootfs squashfs: {sq_len} bytes @ 0x{SQ1_OFF:x} (slot = {SLOT})")

        # rootfs tree to modify
        rootdir = tmp / "rootfs"
        if args.rootfs:
            print(f"[rootfs] copying existing tree: {args.rootfs}")
            shutil.copytree(args.rootfs, rootdir, symlinks=True)
        else:
            print(f"[rootfs] fresh unsquashfs -> {rootdir}")
            # Non-root/macOS unsquashfs exits non-zero solely because it can't
            # mknod the device files; it still extracts every file/dir/symlink,
            # and the pseudo-file re-adds the nodes into the image. So don't
            # treat that exit code as fatal — verify by sentinel instead.
            print("  $ " + " ".join([unsquashfs, "-f", "-d", str(rootdir), str(orig_sq)]))
            subprocess.run([unsquashfs, "-f", "-d", str(rootdir), str(orig_sq)])
            if not (rootdir / "bin" / "busybox").exists():
                die("unsquashfs did not produce a usable rootfs (bin/busybox missing)")

        # faithful metadata (device nodes, setuid, non-root owners) from the original
        pseudo = tmp / "pseudo.txt"
        nodes = build_pseudo(orig_sq, pseudo, unsquashfs)
        print(f"[meta] pseudo-file: {nodes} device nodes + special modes -> {pseudo}")

        mutate_rootfs(rootdir, args.port, args.auth_hash)

        # repack: match original (xz, 128 KiB blocks, no xattrs), root-owned base,
        # pseudo-file restores nodes/suid/owners.
        new_sq = tmp / "sq1.new"
        if new_sq.exists():
            new_sq.unlink()
        run([mksquashfs, str(rootdir), str(new_sq),
             "-comp", "xz", "-b", "131072", "-noappend", "-no-xattrs",
             "-all-root", "-pf", str(pseudo)])
        new_len = new_sq.stat().st_size
        print(f"[repack] new rootfs squashfs: {new_len} bytes "
              f"({new_len - sq_len:+d} vs original; {SLOT - new_len} free in slot)")
        if new_len > SLOT:
            die(f"repacked rootfs {new_len} > slot {SLOT}. Remove files or use "
                f"--mode shell; growing it would overrun the nwsoc partition.")

        # splice back at same offset; length of ipl_0_seg1 is unchanged
        seg1[SQ1_OFF:SQ1_OFF + new_len] = new_sq.read_bytes()
        assert len(seg1) == len(seg1_path.read_bytes()), "seg1 length changed"
        assert seg1[SQ2_OFF:SQ2_OFF + 4] == b"hsqs", "nwsoc squashfs @0x370000 disturbed"
        seg1_new = tmp / "ipl_0_seg1.new"
        seg1_new.write_bytes(seg1)

        # reassemble the .rcx from parts (concatenation is byte-faithful)
        raw = tmp / "assembled.rcx"
        with open(raw, "wb") as out:
            for p in RCX_PARTS:
                out.write((seg1_new if p == "ipl_0_seg1" else (parts / p)).read_bytes())
        print(f"[assemble] {raw} ({raw.stat().st_size} bytes)")

        # fix IPL checksums + SHA-1
        out_rcx = Path(args.out) if args.out else Path(
            (Path(args.firmware).stem if args.firmware else "firmware") + "_authshell.rcx")
        run([sys.executable, str(rcx_correct), str(raw), str(out_rcx)])

        print("\n=== done ===")
        print(f"flashable image: {out_rcx.resolve()}")
        print(f"shell exposure : authenticated login on TCP {args.port}")
        print(f"connect after flash+reboot: telnet <printer-ip> {args.port}")
        print("next steps      : see REMOTE_ACCESS.md § Flashing "
              "(epsflasher_mac.py --only-first-block). This does NOT flash "
              "anything.")
    finally:
        if args.keep or args.workdir:
            print(f"[work] kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
