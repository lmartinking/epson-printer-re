#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "libusb1",
#     "tqdm",
# ]
# ///
"""macOS-compatible Epson Program-Update-Mode flasher.

Run with: uv run --script epsflasher_mac.py --help
Needs the epson-reversing toolkit in ./epson-reversing/ and libusb
(brew install libusb).

The upstream `epsflasher` speaks IEEE 1284.4 (Dot4) over Linux's `usblp`
character device at `/dev/usb/lp0`. macOS has no equivalent, so this script
does the same thing but talks to the printer through libusb1 bulk endpoints.
The IEEE 1284.4 framing is unchanged — we reuse `epson-reversing/dirty4.py`
by shimming the two file-descriptor calls (`os.read`, `os.write`) it makes on
the port, so a duck-typed USB port object works in place of an int fd.

WHAT THIS SENDS — the "LO" protocol reversed from Epson's own Windows updater
`EPFWUPD.exe` (see FIRMWARE_UPDATE.md). For an XP-442 in Program Update
Mode the updater does NOT use a `fwu:ctrl` socket (that socket isn't even
exposed by the PUM ROM); it uses the two sockets the ROM does expose:

  1. Open D4 socket "EPSON-CTRL" and run a binary command handshake:
         st 01 00 01                 (73 74 01 00 01)  status
         vi 01 00 04 / 00 / FF / 07  (76 69 01 00 NN)  version/identity queries
         fl 01 00 01                 (66 6c 01 00 01)  flash-prepare  (reply-gated)
         gm 01 00 01                 (67 6d 01 00 01)  go-mode / arm the loader
     Each command is <opcode:2 ASCII><arglen:u16-LE><arg>. dirty4's cmd2()
     already emits exactly this framing — the colon form ("vi:FF") we used in
     earlier attempts was never what the updater sends.
  2. Open D4 socket "EPSON-DATA" and stream the raw firmware block(s) Z_1
     (and Z_2 unless --only-first-block), chunked at MTU-6, EOM on the last
     packet. No per-chunk wrapper — raw RCX payload bytes.
  3. Send the "firmware update end" command on EPSON-CTRL, then close.

The open question this tool exists to test: whether the `fl`/`gm` handshake on
EPSON-CTRL is what arms the EPSON-DATA credit gate (state[+0x1574]) that every
prior EPSON-DATA attempt stalled on — see FIRMWARE_UPDATE.md. Run with -d
and watch whether CreditRequest starts granting after `gm`.

Usage:
    uv run --script ./epsflasher_mac.py FWF729TL_remote.rcx --only-first-block

The printer must be in Program Update Mode (VID:PID 04b8:0007). Enter that
mode with the front-panel key combo before running this.
"""
from __future__ import annotations

import argparse
import logging
import os as _real_os
import struct
import sys
import time
import types
from pathlib import Path

try:
    import usb1
except ImportError:
    sys.exit("usb1 module missing. Install with: pip3 install --user libusb1")

# --- import the toolkit's dirty4 + rcx (unmodified) ---------------------------
TOOLKIT = Path(__file__).resolve().parent / "epson-reversing"
sys.path.insert(0, str(TOOLKIT))
import dirty4  # noqa: E402
import rcx     # noqa: E402

USB_VID = 0x04B8
# Recovery-mode PIDs on Epson XP-4xx-series printers:
#   0x0007  — Program Update Mode on the XP-442 (LCD reads "Program Update
#             Mode" in white text; entered with STOP + LEFT ARROW + DOWN ARROW
#             + POWER). This is the mode Epson's own EPFWUPD.exe flashes via
#             the LO/EPSON-CTRL+EPSON-DATA path — and the mode this tool
#             targets. The PUM ROM (rcx_output/ipl_0_seg1) exposes only the
#             EPSON-CTRL and EPSON-DATA D4 sockets.
#   0x1114  — an alternate firmware-update PID seen on some XP variants. Same
#             D4 stack; accepted here as a fallback.
USB_PID_PUM = (0x0007,)          # primary target
USB_PID_ALT = (0x1114,)          # accepted fallback
USB_PID_FLASHABLE = USB_PID_PUM + USB_PID_ALT


# --- USB transport ------------------------------------------------------------
class UsbPort:
    """Duck-types the int fd that dirty4 hands to its shimmed os.read/write."""

    def __init__(self, ctx: "usb1.USBContext", handle: "usb1.USBDeviceHandle",
                 ep_in: int, ep_out: int, iface: int):
        self.ctx = ctx
        self.handle = handle
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.iface = iface
        # Program Update Mode can take >1s between chunks of a reply (LCD
        # updates, flash-op status changes); short timeouts here truncate
        # replies. dirty4._read tolerates b'' returns so this only affects
        # how quickly we detect a genuinely-hung printer.
        self._read_timeout_ms = 3000
        # Longer write timeout — PUM's 512-byte bulk endpoint occasionally
        # NAKs for several seconds while the printer drains its FIFO into
        # the staging buffer. Retry a few times on timeout before giving up.
        self._write_timeout_ms = 30000
        self._write_retries = 5

    def read(self, n: int) -> bytes:
        try:
            data = self.handle.bulkRead(self.ep_in, n, timeout=self._read_timeout_ms)
            return bytes(data)
        except usb1.USBErrorTimeout:
            return b""

    def write(self, data) -> int:
        buf = bytes(data)
        for attempt in range(self._write_retries):
            try:
                return self.handle.bulkWrite(self.ep_out, buf,
                                             timeout=self._write_timeout_ms)
            except usb1.USBErrorTimeout:
                logging.warning("bulkWrite timeout (attempt %d/%d, %d bytes)",
                                attempt + 1, self._write_retries, len(buf))
                # Fall through and retry.
        # Last-chance attempt; if it raises, let it propagate.
        return self.handle.bulkWrite(self.ep_out, buf,
                                     timeout=self._write_timeout_ms)

    def close(self):
        try:
            self.handle.releaseInterface(self.iface)
        except Exception:
            pass
        try:
            self.handle.close()
        except Exception:
            pass
        try:
            self.ctx.close()
        except Exception:
            pass


def find_printer(ctx: "usb1.USBContext"):
    """Return (device, pid) for the first matching Epson recovery-mode device.

    Prefers the Program Update Mode PID (0x0007); falls back to the alternate
    firmware-update PID (0x1114) if that's all that's present.
    """
    alt_dev = None
    for d in ctx.getDeviceList(skip_on_error=True):
        if d.getVendorID() != USB_VID:
            continue
        pid = d.getProductID()
        if pid in USB_PID_PUM:
            return d, pid
        if pid in USB_PID_ALT and alt_dev is None:
            alt_dev = (d, pid)
    return alt_dev if alt_dev else (None, None)


def open_usb_port(debug: bool) -> UsbPort:
    ctx = usb1.USBContext()
    dev, pid = find_printer(ctx)
    if dev is None:
        raise SystemExit(
            f"No Epson printer found at VID {USB_VID:04x} with a known "
            f"recovery PID ({', '.join(f'{p:04x}' for p in USB_PID_FLASHABLE)}).\n"
            "For XP-442: power off, then hold "
            "STOP + LEFT ARROW + DOWN ARROW + POWER for ~3–5s until the LCD "
            "shows 'Program Update Mode' in WHITE text. The reliable "
            "confirmation is the 04b8:0007 PID, whatever buttons produced it."
        )
    if pid in USB_PID_ALT:
        logging.warning(
            "Found Epson %04x:%04x (alternate firmware-update PID, not the "
            "0x0007 Program Update Mode PID). Trying anyway; the LO handshake "
            "will report clearly if the printer isn't in a flashable mode.",
            USB_VID, pid)
    logging.info("Found Epson %04x:%04x on bus %d addr %d",
                 USB_VID, pid, dev.getBusNumber(), dev.getDeviceAddress())

    # Locate bulk endpoints on the first setting of the first interface.
    ep_in = ep_out = None
    iface_num = 0
    for cfg in dev.iterConfigurations():
        for intf in cfg:
            for setting in intf:
                iface_num = setting.getNumber()
                for ep in setting:
                    if (ep.getAttributes() & 0x03) != 0x02:  # not bulk
                        continue
                    addr = ep.getAddress()
                    if addr & 0x80:
                        ep_in = ep_in or addr
                    else:
                        ep_out = ep_out or addr
                if ep_in and ep_out:
                    break
            if ep_in and ep_out:
                break
        if ep_in and ep_out:
            break
    if ep_in is None or ep_out is None:
        raise SystemExit(
            f"Could not find bulk endpoints (in=0x{ep_in or 0:02x}, "
            f"out=0x{ep_out or 0:02x}). Device may not expose a printer-style "
            "bulk interface in this mode."
        )
    logging.info("Bulk endpoints: IN=0x%02x OUT=0x%02x (iface %d)",
                 ep_in, ep_out, iface_num)

    handle = dev.open()
    # Enable libusb's userspace kernel-driver detach. On macOS this uses
    # IOKit to release the interface from any class driver (e.g. usbprint)
    # without unloading a kext, so it works under SIP.
    try:
        handle.setAutoDetachKernelDriver(True)
    except (usb1.USBError, AttributeError):
        pass

    def _try_claim() -> "None | Exception":
        try:
            handle.claimInterface(iface_num)
            return None
        except usb1.USBError as e:
            return e

    err = _try_claim()
    if err is not None:
        # Auto-detach didn't fire (or isn't supported on this libusb build);
        # explicitly detach and retry once.
        logging.info("Initial claim failed (%s); trying explicit "
                     "detachKernelDriver…", err)
        try:
            handle.detachKernelDriver(iface_num)
        except (usb1.USBError, AttributeError) as e2:
            logging.info("detachKernelDriver returned %s (may be harmless)", e2)
        err = _try_claim()

    if err is not None:
        raise SystemExit(
            f"Cannot claim USB interface {iface_num}: {err}. "
            "If this persists, remove the printer from macOS Printers & "
            "Scanners (CUPS may be holding it) and rerun. Last-resort: flash "
            "from a Linux host."
        )

    return UsbPort(ctx, handle, ep_in, ep_out, iface_num)


# --- shim dirty4's `os` so its os.read/os.write dispatch to UsbPort -----------
_shim_os = types.ModuleType("dirty4_os_shim")
# Everything dirty4 might touch on os besides read/write we forward untouched.
for _name in dir(_real_os):
    if not _name.startswith("_"):
        setattr(_shim_os, _name, getattr(_real_os, _name))


def _shim_read(port, n):
    if isinstance(port, UsbPort):
        return port.read(n)
    return _real_os.read(port, n)


def _shim_write(port, data):
    if isinstance(port, UsbPort):
        return port.write(data)
    return _real_os.write(port, data)


_shim_os.read = _shim_read
_shim_os.write = _shim_write
dirty4.os = _shim_os


# --- fix dirty4.Dirty4.CreditRequest ------------------------------------------
# Upstream sends only one 16-bit amount (`>BBH`) as the CreditRequest payload:
#     04 <sid> <sid> <amount_be>            = 5 bytes
# The printer's ROM handler at FUN_0000ad84 reads TWO 16-bit amounts from
# state[+0x67..+0x6a]. The second one lands on stale state-buffer bytes; if
# they're anything other than 0x0000 or 0xffff, the classifier FUN_0000a968
# returns -1 and PUM sends back a 7f 00 00 80 00 00 "Malformed packet" error.
# CTRL's CreditRequest "works" by luck (its OpenChannel leaves ff ff in the
# right slot); DATA's fails deterministically after a fresh OpenChannel. Fix
# by sending the second amount explicitly as 0xffff (streaming path). See
# FIRMWARE_UPDATE.md for the full trace.
#
# 7-byte payload `04 SID SID FF FF 00 <amount>` matches EWR exactly
# (Epson-Waste-Reset/src/d4session.cpp:544 and generator.cpp:415). EWR
# requests amount=1 each time — we mirror that default (dirty4's upstream
# default of 0x100 asks the printer to grant up to 256 credits in one shot,
# which is fine but noisier than EWR's per-packet grant flow).
_dirty4_orig_credit_request = dirty4.Dirty4.CreditRequest


def _fixed_credit_request(self, sid, amount=1):
    req = struct.pack('>BBHH', sid, sid, 0xFFFF, amount)
    resp = self.command("CreditRequest", req)
    _, _, granted = struct.unpack('>BBH', resp)
    self.channels[sid].credits += granted
    return granted


dirty4.Dirty4.CreditRequest = _fixed_credit_request


# --- force credit=0 on data packets -------------------------------------------
# EWR (Epson-Waste-Reset/include/ewr/generator.h:16) says the D4-header credit
# byte on data packets must stay 0x00 — nonzero credit on data packets is
# documented to "lock up the hardware buffer". EWR SendData uses `SendPacket
# (m_socket, m_socket, chunk, 0x00)` at d4session.cpp:577.
#
# dirty4's D4Packet namedtuple defaults `credit=1`, which is correct for
# transaction packets (Init/Open/Close/Credit*) that Dirty4.command() sends
# but WRONG for the data-channel packets sent from DirtyChannelContext.write.
# We call d4.write_packet directly with `credit=0` on the EPSON-DATA stream,
# but the EPSON-CTRL commands (st/vi/fl/gm) go through cmd2 ->
# DirtyChannelContext.write, so we monkey-patch that method to force credit=0
# on every data packet it emits (both CTRL commands and any block payload).
def _fixed_channel_write(self, data, progress=None):
    while len(data):
        if len(data) > self.chan.mtu - 6:
            payload = data[:self.chan.mtu - 6]
            eom = False
        else:
            payload = data
            eom = True
        data = data[len(payload):]
        self.ensure_credit()
        self.d4.write_packet(self.sid, credit=0, eom=eom, payload=payload)
        if progress:
            progress(len(payload))


dirty4.DirtyChannelContext.write = _fixed_channel_write


# --- best-effort held-reply drain ---------------------------------------------
def drain_held_replies(port: "UsbPort", max_attempts: int = 4,
                       window_ms: int = 300) -> int:
    """Consume anything the printer is holding on bulk-in with a short timeout.

    Lightweight analog of EWR's DiscardHeldReplies (d4session.cpp:475-496):
    on some models an unsent reply persists through CloseChannel/Exit/Init
    and is handed over the moment a later session grants credit — passing
    for the answer to that session's first query and shifting every reply
    after it by one.

    EWR grants proactive credit and reads with a bounded timeout up to 32
    times. We don't replicate that here because dirty4's `Credit()` sends
    a transaction on sid 0 whose reply-parser asserts on psid==0 — a held
    data reply arriving first would trip that assert and wedge us. Instead
    we probe raw bulk-in with a short timeout: anything the printer sent
    unsolicited during the handshake (stale D4 fragments, EJL echoes) gets
    absorbed here rather than showing up on the wire during the first real
    fwu:ctrl exchange.
    """
    orig_timeout = port._read_timeout_ms
    port._read_timeout_ms = window_ms
    total = 0
    try:
        for _ in range(max_attempts):
            raw = port.read(4096)
            if not raw:
                break
            total += len(raw)
            logging.info("drained %d bytes of held-over data before first "
                         "fwu:ctrl command: %s", len(raw),
                         raw[:32].hex(' '))
    finally:
        port._read_timeout_ms = orig_timeout
    return total


# --- CTRL-channel command helper ----------------------------------------------
def ctrl_cmd(ctrl, opcode: str, arg: int) -> bytes:
    """Send one binary CTRL command and return the raw reply payload.

    Wire form (matches EPFWUPD.exe exactly): <op:2 ASCII><arglen:u16 LE><arg>.
    dirty4's cmd2() builds precisely this, so `ctrl_cmd(ctrl, "vi", 0xFF)`
    puts `76 69 01 00 FF` on EPSON-CTRL. Reply is returned as raw bytes
    (replies mix ASCII like "vi:FF:ROIPLJ;\\f" with binary status frames).
    """
    reply = ctrl.cmd2(opcode, arg, binary=True)
    try:
        shown = reply.decode("ascii")
    except UnicodeDecodeError:
        shown = reply.hex(" ")
    logging.info("CTRL %s 01 00 %02X -> %r", opcode, arg, shown)
    return bytes(reply)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Flash a firmware to an Epson printer via libusb1 (macOS)."
    )
    ap.add_argument("filename", metavar="firmware.rcx",
                    help="file to flash (RCX format)")
    ap.add_argument("-d", "--debug", action="store_true",
                    help="verbose D4 protocol logging")
    ap.add_argument("--only-first-block", action="store_true",
                    help="only stream the first firmware block Z_1 (the only "
                         "block build_remote_access.py modifies; safest in "
                         "Program Update Mode).")
    ap.add_argument("--skip-vi", action="store_true",
                    help="skip the vi:04/00/FF/07 version queries in the "
                         "EPSON-CTRL preamble (still sends st, fl, gm).")
    ap.add_argument("--skip-handshake", action="store_true",
                    help="skip the whole EPSON-CTRL st/vi/fl/gm handshake and "
                         "stream straight to EPSON-DATA (for A/B testing "
                         "whether the handshake is what arms the data gate).")
    ap.add_argument("--end-cmd", default="gm:03",
                    help="the 'firmware update end' command sent on "
                         "EPSON-CTRL after streaming, as 'opcode:arg[:arg...]' "
                         "where the args are the payload hex bytes AFTER the "
                         "u16 length (which the tool prepends). Default "
                         "'gm:03' => frame 67 6d 01 00 03. The exact end frame "
                         "is UNCONFIRMED from RE (gm-family best guess); use "
                         "--skip-end to send nothing.")
    ap.add_argument("--skip-end", action="store_true",
                    help="don't send any firmware-update-end command after "
                         "streaming; let the LCD/reboot finalize.")
    ap.add_argument("--credit-timeout-secs", type=float, default=30.0,
                    help="if EPSON-DATA keeps granting 0 credits for this long "
                         "during streaming, bail loudly instead of spinning "
                         "(default 30s). A persistent 0-grant here is the "
                         "known credit-gate wall (FIRMWARE_UPDATE.md); the "
                         "point of the fl/gm handshake is to lift it.")
    ap.add_argument("--data-mtu", type=lambda s: int(s, 0), default=512,
                    help="cap the D4 packet size on EPSON-DATA at N bytes "
                         "(default 512). PUM's OpenChannel-reply advertises "
                         "MTU=0x8000 but has rejected packets that large with "
                         "error 0x83 'Packet too big'; 512 has worked "
                         "historically. Payload per packet is N-6 (D4 header).")
    ap.add_argument("--held-reply-drain-ms", type=int, default=300,
                    help="short-timeout window used to drain stale bulk-in "
                         "bytes left from a prior session before the first "
                         "CTRL command (default 300ms). 0 to skip.")
    args = ap.parse_args()

    # Parse --end-cmd "gm:01:00:03" into (opcode, arg-bytes) for ctrl_cmd().
    end_op = None
    end_arg = b""
    if not args.skip_end:
        try:
            parts = args.end_cmd.split(":")
            end_op = parts[0]
            end_arg = bytes(int(p, 16) for p in parts[1:])
            if len(end_op) != 2:
                raise ValueError("opcode must be 2 chars")
        except (ValueError, IndexError) as e:
            raise SystemExit(f"Bad --end-cmd {args.end_cmd!r}: {e}")

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)-8s %(message)s")

    rcx_content = Path(args.filename).read_bytes()
    header_len, cfg = rcx.parse_rcx(rcx_content)

    # Collect the Z_ blocks in header order as (name, bytes). The updater
    # streams each block as its own D4 message ("LO : send Data(Z_%d)").
    blocks = []
    offset = header_len
    for name, section in cfg.items():
        if not name.startswith("Z_"):
            continue
        size = int(section["5"])
        blocks.append((name, rcx_content[offset:offset + size]))
        offset += size

    total_declared = sum(len(b) for _, b in blocks)
    if header_len + total_declared != len(rcx_content):
        raise SystemExit(
            f"RCX payload is {len(rcx_content) - header_len} bytes but the "
            f"Z_ block headers sum to {total_declared}"
        )
    if not blocks:
        raise SystemExit("No Z_ firmware blocks found in the RCX header.")

    if args.only_first_block:
        blocks = blocks[:1]
        logging.info("Will stream only the first block %s: %d bytes",
                     blocks[0][0], len(blocks[0][1]))
    else:
        logging.warning("Streaming all %d blocks (%d bytes total). The updater "
                        "does send every block, but --only-first-block is the "
                        "safer default when only block 0 was modified.",
                        len(blocks), total_declared)

    port = open_usb_port(args.debug)
    try:
        d4 = dirty4.Dirty4(port)

        # Program Update Mode flash, "LO" path from EPFWUPD.exe
        # (FIRMWARE_UPDATE.md): a binary command handshake on EPSON-CTRL,
        # then raw block streaming on EPSON-DATA, then a CTRL end command.
        #
        # All outgoing data packets carry D4-header credit=0x00 (enforced by
        # the DirtyChannelContext.write monkeypatch above + explicit credit=0
        # on the raw streaming writes) — nonzero credit on data packets is
        # documented to "lock up the hardware buffer" (Epson-Waste-Reset/
        # include/ewr/generator.h:16). Transaction packets (Init/Open/Close/
        # Credit*) keep credit=0x01 via dirty4's D4Packet default.
        with d4.channel("EPSON-CTRL") as ctrl:

            # Drain any stale bulk-in left over from a prior session before
            # the first CTRL exchange (EWR d4session.cpp:475-496).
            if args.held_reply_drain_ms > 0:
                dropped = drain_held_replies(port,
                                             window_ms=args.held_reply_drain_ms)
                if dropped:
                    logging.warning("drained %d bytes of stale bulk-in before "
                                    "first CTRL command", dropped)

            # --- 1. EPSON-CTRL handshake (st -> vi... -> fl -> gm) ---------
            if not args.skip_handshake:
                logging.info("EPSON-CTRL handshake...")
                ctrl_cmd(ctrl, "st", 0x01)          # 73 74 01 00 01
                if not args.skip_vi:
                    for arg in (0x04, 0x00, 0xFF, 0x07):
                        ctrl_cmd(ctrl, "vi", arg)   # 76 69 01 00 NN
                ctrl_cmd(ctrl, "fl", 0x01)          # 66 6c 01 00 01  flash-prep
                gm_reply = ctrl_cmd(ctrl, "gm", 0x01)  # 67 6d 01 00 01  arm
                # The updater gates on gm's reply matching an internal token
                # to mark the loader "armed" (state=2). We don't know the
                # token text, so we don't hard-fail — but surface it clearly.
                if b"NG" in gm_reply or b"NA" in gm_reply or \
                        b"NOT" in gm_reply.upper():
                    logging.warning("gm reply looks negative (%r). The loader "
                                    "may not be armed; streaming may stall on "
                                    "the credit gate.", gm_reply)
            else:
                logging.warning("--skip-handshake: streaming to EPSON-DATA "
                                "without the st/vi/fl/gm preamble.")

            # --- 2. EPSON-DATA raw block streaming ------------------------
            with d4.channel("EPSON-DATA") as data:
                data.chan.mtu = args.data_mtu
                logging.info("EPSON-DATA open (MTU capped to %d; %d payload "
                             "bytes/packet).", args.data_mtu, args.data_mtu - 6)

                # Bounded ensure_credit — bail after credit_timeout_secs if
                # grants stay at 0. A persistent 0-grant is the credit-gate
                # wall (FIRMWARE_UPDATE.md) the handshake is meant to lift.
                credit_deadline = [None]

                def data_ensure_credit():
                    if data.credits >= 1:
                        credit_deadline[0] = None
                        return
                    while True:
                        granted = d4.CreditRequest(data.sid)
                        if granted >= 1:
                            credit_deadline[0] = None
                            return
                        now = time.time()
                        if credit_deadline[0] is None:
                            credit_deadline[0] = now + args.credit_timeout_secs
                        elif now > credit_deadline[0]:
                            raise SystemExit(
                                f"EPSON-DATA granted 0 credits for "
                                f"{args.credit_timeout_secs:.0f}s — the credit "
                                "gate is not armed. The fl/gm handshake did "
                                "not lift it; see FIRMWARE_UPDATE.md "
                                "(UART poke of state[+0x1574] may be needed).")
                        time.sleep(0.1)

                bar = None
                progress_cb = None
                try:
                    import tqdm
                    bar = tqdm.tqdm(total=sum(len(b) for _, b in blocks),
                                    unit="byte", unit_scale=True,
                                    unit_divisor=1024)

                    def progress_cb(inc: int) -> None:
                        bar.update(inc)
                except ImportError:
                    logging.warning("tqdm not installed — the bulk write will "
                                    "be silent for minutes. `pip3 install "
                                    "--user tqdm` to fix.")

                mtu = data.chan.mtu
                for name, blob in blocks:
                    logging.info("EPSON-DATA <- %s (%d bytes)", name, len(blob))
                    off = 0
                    n = len(blob)
                    while off < n:
                        chunk_size = min(mtu - 6, n - off)
                        chunk = blob[off:off + chunk_size]
                        # EOM marks the end of each block's D4 message, matching
                        # the updater's per-block send loop (FUN_004216de).
                        eom = (off + chunk_size == n)
                        data_ensure_credit()
                        d4.write_packet(data.sid, credit=0, eom=eom,
                                        payload=chunk)
                        off += chunk_size
                        if progress_cb:
                            progress_cb(chunk_size)

                if bar is not None:
                    bar.close()
                logging.info("EPSON-DATA stream complete.")

            # --- 3. Firmware-update-end on EPSON-CTRL ---------------------
            if end_op is not None:
                # NOTE: exact end frame is UNCONFIRMED (see --end-cmd). The
                # loader only accepts it once fl/gm succeeded; sending it when
                # the handshake was skipped is pointless, so guard on that.
                if args.skip_handshake:
                    logging.warning("skipping end command (%s) because the "
                                    "handshake was skipped.", args.end_cmd)
                else:
                    logging.info("EPSON-CTRL end command: %s (frame %s)",
                                 args.end_cmd,
                                 (end_op.encode("ascii") +
                                  struct.pack("<H", len(end_arg)) +
                                  end_arg).hex(" "))
                    # Build the frame directly (ctrl_cmd only takes one arg
                    # byte; the end arg may differ): <op><u16 len><arg...>.
                    ctrl.write(end_op.encode("ascii") +
                               struct.pack("<H", len(end_arg)) + end_arg)
                    try:
                        end_reply = bytes(ctrl.read().payload)
                        logging.info("EPSON-CTRL end -> %r", end_reply)
                    except Exception as e:  # noqa: BLE001
                        logging.warning("no/failed reply to end command: %s", e)

        logging.info("Upload complete. Watch the LCD: Now Erasing -> "
                     "Now Writing -> Now Verifying -> Finished! Once "
                     "'Finished!' shows, power-cycle the printer and "
                     "connect: telnet <printer-ip> 2323")
    finally:
        port.close()


if __name__ == "__main__":
    main()
