# INETD on TCP 2323 — diagnosis

**Status: unresolved.** On the authshell build, TCP 2323 is refused from the LAN
(kernel RST, so no listener is bound). Static analysis of the flashed rootfs
rules out the config, parsing and netfilter causes. The remaining causes can
only be told apart with a UART session, or with a reflash that changes what
the boot does.

## What is flashed

- `/etc/inetd.conf` (0644, 165 B on the authshell build): one service line for port 2323.
- `/etc/init.d/S50remoteaccess` (0755, 338 B): runs `/bin/busybox inetd -fe /etc/inetd.conf`
  in the background and exits 0.
- `/etc/shadow` (0600) and `/etc/passwd` as set for the authshell build. `/etc/securetty` is absent.
- `S99NWControl` is unchanged. It is a symlink to the proprietary `/opt/nwsoc/NWControl`.

## Verified (static)

- The flashed `FWF729TL_authshell.rcx` contains the authshell rootfs byte-for-byte
  (at offset `0x233123`), so the image on disk is the intended one.
- BusyBox 1.17.4 inetd: the getopt string is `R:feq:`, so `-fe /etc/inetd.conf`
  parses as `-f`, `-e` and the config path.
- The port is numeric, so `atoi()` parses `2323` and no `/etc/services` entry is needed.
- The rootfs is squashfs and nothing remounts `/` read-write, so `/etc` cannot change at runtime.
- The kernel (2.6.35.8) has `nf_conntrack` sysctls but no netfilter filter table or
  modules, and the rootfs has no `iptables` binary.

## Ruled out for the refused-connection symptom

- **Config format or port resolution.** Numeric ports are accepted, and the line is well-formed.
- **Applet not built.** The inetd applet is present and complete.
- **Config overwritten at boot.** The rootfs is read-only.
- **Flash not applied.** The bytes on flash match the file.
- **Netfilter REJECT or DROP.** No filter table exists in the kernel.
- **Daemonize path.** Foreground and background both reach `bind()`. `/dev/null` is
  never touched, because init gives inetd `/dev/console` on fds 0–2.
- **Inherited stderr or SIGPIPE.** The inherited descriptors are `/dev/console`, a character device.
- **`&` in the sysinit subshell.** `ash` handles it correctly.
- **File-descriptor limit.** inetd has its own descriptor table.
- **securetty, passwd and shadow.** These only run after a connection is accepted, so
  they cannot cause a refused connection. They do matter for login, below.

## Open

1. **S50 never ran.** Cannot be shown statically. The rcS loop
   `for i in /etc/init.d/S??*` sorts S20urandom, S40network, S50remoteaccess, S99NWControl,
   and rcS does not use `set -e`, so nothing visible stops it. Unproven either way.
2. **inetd starts but fails silently between reading the config and binding.** Possible
   causes are a privileged-port or uid check that misbehaves under init, or a
   setup call that fails in this specific build. Not provable without the source.
3. **The port is shadowed after S50 runs.** NWControl (S99) is the only userland that
   configures `eth0`. With no netfilter in the kernel, any filtering would have to
   live in NWControl's own code. Unverified. Testing it needs Ghidra on
   `/opt/nwsoc/NWControl`, or a listener on a different port.

**Login will need work once the listener exists.** `/bin/login` runs on a socket with
no controlling tty, so `ttyname` fails and login falls back to `UNKNOWN`. The stock
`/etc/securetty` does not list `UNKNOWN`. The authshell build removes `securetty`,
which lets that check pass, so this is expected to work there. Not yet tested.

## Next steps

**With UART:** log in on the console, then check what is running and what is listening.

```bash
ps w | grep -E 'inetd|login'
netstat -lntp | grep 2323
dmesg | tail -50
/bin/busybox inetd -fe /etc/inetd.conf    # run by hand; any error it prints is the answer
```

**Without UART:** one reflash changing only `S50remoteaccess` to the exec form. It drops
`-f` and `-e`, detaches with `setsid`, and sends output to `/tmp/inetd.out`:

```sh
#!/bin/sh
case "${1:-start}" in
  start)
    ( /bin/busybox setsid /bin/busybox inetd /etc/inetd.conf \
        </dev/null >/tmp/inetd.out 2>&1 ) &
    ;;
  stop) /bin/busybox killall inetd 2>/dev/null ;;
esac
exit 0
```

If that still refuses the connection, the remaining hypotheses cannot be separated
from the network side. Get UART before another reflash.

See also [TELNET_STATUS.md](TELNET_STATUS.md) and [REMOTE_ACCESS.md](REMOTE_ACCESS.md).
