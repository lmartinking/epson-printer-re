# nwsoc `@ run shell command` Console — RE Findings

Static analysis only. Target: `/tmp/nwsoc/nwsoc` (ARM LE32 ELF, image base `0x8000`).
Ghidra project `xp-442`, program `/nwsoc`. All addresses below are **VAs** (file_off + 0x8000).

## Bottom line (TL;DR)

- The `@` verb belongs to an interactive **"Console" menu task** that reads from the process's
  **standard input** (`fgetc(stdin)` / `fgets(stdin)`), i.e. the **serial/UART debug console** — it is
  **not** a network listener, **not** USB/D4/EJL, **not** epidc/`/dev/epidc0`, and **not** a file/pipe.
- The `@` handler runs the typed line via `fork()` + `execl("/bin/sh","sh","-c",<line>,0)` — full arbitrary
  shell as whatever user nwsoc runs as (root on this device).
- **No NVRAM/config/env/build gate** protects the console task: it is started **unconditionally** at the end
  of `PNPSMStartMain()`. The only "gate" is physical: whatever fd 0 is wired to. nwsoc is launched with **no
  stdio redirection**, so fd 0/1/2 are inherited from init → the serial console (`ttyS0`).
- **Reachability verdict: local/physical only.** Reaching the `@` console requires UART access — the exact
  same access that already yields a root login shell (empty password, per prior notes / `setsid cttyhack
  /bin/sh` in PreBoot). So the `@` console grants **no new capability** over stock firmware and is **not
  reachable over LAN/USB** without patching.

## 1. The console command menu (full verb set)

Help strings live at file `0x5c4dd0`–`0x5c5024` (VA `0x5ccdd0`+). Printed by the dispatcher every loop:

```
Launching Console Menu.                 (VA 0x5ccdd0 — launcher banner)
<<Current: %s mode>>
Input     'r' : Reboot.
          'R' : Reset settings and reboot.
          'a' : display current IP address.
          'm' : display current MAC address.
          'f' : show /proc/meminfo.
          '@' : run shell command          <-- VA 0x5ccf54
          'l' : List current module status
          's' : statussheet output
          '1' : wcif debug mode
          'y' : management service start trigger
enter shell command:                     (VA 0x5cd2f8 — '@' sub-prompt)
**** Exiting console task...             (VA 0x5cd310)
```

Verb dispatch (inside `FUN_00072ecc`), by ASCII code read from stdin:
| key | code | action |
|-----|------|--------|
| `r` | 0x72 | `FUN_000783a4("ResetAll",...)` |
| `R` | 0x52 | `FUN_000783a4("FactoryDefaultAndReset",...)` |
| `a` | 0x61 | print IPv4/IPv6 from RSC config store |
| `m` | 0x6d | print MAC from `PS_CURRENT/PS_MAC_ADDRESS` |
| `f` | 0x66 | dump `/proc/meminfo` to stderr |
| `l` | 0x6c | print module status table (XENON/POSE/…/PNBROWSER) |
| `s` | 0x73 | `FUN_00072e2c()` (statussheet output) |
| `1` | 0x31 | `FUN_001f7640()` (wcif debug mode) |
| `y` | 0x79 | mgmt-service trigger; prints Wlan/Fax*(OK/NG) from `DAT_007227b8` bitfield |
| `c` | 0x63 | (undocumented) change N/W interface if `PS_DEVICE_CATEGORY & 0x40` |
| `@` | 0x40 | **run shell command** (see §3) |

This verb set (reboot / factory-reset / meminfo / shell) is characteristic of a **developer/serial debug
console**, not a production network service.

## 2. Handler function addresses

- `FUN_00072ecc` (0x72ecc–0x73b57) — **console task body / menu loop + dispatcher**. Literal pool with all the
  help/verb string pointers sits at 0x73b68–0x73c64 (that pool is why byte-searching the string VAs landed here).
- `FUN_00072dd0` (0x72dd0) — **launcher**: prints "Launching Console Menu." then
  `FUN_00076d14(stack,100,FUN_00072ecc,0,"Console",0x4000)` = create thread named **"Console"**, prio 100,
  16 KiB stack, entry = the dispatcher.
- `FUN_0006b6a4` = `PNPSMStartMain()` — network-domain main startup; calls `FUN_00072dd0()` at 0x6c17c
  (near function end, address-tag `0x348`), **unconditionally**, right after the optional BrowserMgmt task.
- `FUN_000775d4` (0x775d4) — thin wrapper the `@` handler calls: `FUN_00076900(line)` + optional delay.
- `FUN_00076900` (0x76900) — **the exec**:
  ```c
  __pid = fork();
  if (__pid == 0) { execl("/bin/sh","sh","-c",param_1,0); exit(-1); }
  waitpid(...);   // returns child exit status
  ```
  `DAT_005cdcb8` verified = bytes `2d 63 00` = **"-c"**.

## 3. Input transport — how the `@` line is read

From `FUN_00072ecc`:
```c
FUN_00076f08(0x32);            // ~sleep/yield
DAT_0069bd04 = 1;              // loop-run flag (NOT an external gate)
do {
    ... printf(menu) ...       // to stdout
    uVar3 = fgetc(stdin);      // <-- command char from STDIN
    uVar3 &= 0xff;
    fgetc(stdin);              // eat the newline
    ...
    else if (uVar3 == 0x40) {  // '@'
        __s = <128-byte stack buf>;
        fprintf(stderr,"enter shell command: ");
        pcVar4 = fgets(__s, 0x80, stdin);   // <-- shell line from STDIN
        if (pcVar4) { __s[strlen(__s)] = 0; FUN_000775d4(__s, 0); }  // -> /bin/sh -c
    }
    ...
    FUN_00076f08(0x32);
} while (DAT_0069bd04 != 0);
printf("**** Exiting console task...\n");
```
All I/O is via the C stdio globals **`stdin` / `stdout` / `stderr` (fd 0/1/2)**. There is **no** `recv`/
`accept`/`read(sockfd)`, no `/dev/epidc0`, no named pipe, and no `dup2` redirect anywhere in this path.
Therefore the transport is simply **the standard input the process was launched with**.

## 4. Gating — is the console conditional?

**Console task creation: NOT gated.** `PNPSMStartMain()` → `FUN_00072dd0()` runs on every normal boot of the
network domain; there is no `getenv`, NVRAM/config key, `/tmp` flag-file, or build `#ifdef` around it.
(`DAT_0069bd04` is only the in-loop run flag; it is set to 1 on entry and cleared by reboot verbs to exit.)

**The real "gate" is what fd 0 is connected to**, decided entirely outside nwsoc by the launch chain:

- `/opt/nwsoc/NWControl` runs the binary as bare `./nwsoc` inside a `while true` loop — **no `<`, `>`, `2>`,
  or pipe redirection**. So nwsoc inherits NWControl's stdio unchanged.
- NWControl is started by `/etc/init.d/S99NWControl`, reached from the boot flow
  (`/root/PreBoot` → `S99NWControl`; `rcS` also iterates `S??*`). None of these redirect stdio either.
- The chain's fd 0/1/2 trace back to the kernel console = **serial UART `ttyS0`** (`inittab`:
  `ttyS0::respawn:/sbin/getty -L ttyS0 115200 vt100`; PreBoot: `echo 1 > /proc/epson_console_enable`,
  and finally `setsid cttyhack /bin/sh` = a root shell on that same tty).

So: the console appears on the **serial debug port**, gated only by physical access to the UART.

## 5. Reachability ranking (honest)

1. **Serial/UART (ttyS0) — the actual transport. Local physical only.** This is where the `@` menu lives.
   But the same UART already gives a root shell (empty password / `cttyhack /bin/sh`), so the `@` console is
   redundant — it adds no capability an attacker at the UART doesn't already have.
2. **TCP/UDP network listener — NOT the transport.** The `@` console never touches a socket. nwsoc's network
   services (HTTP 80/443, LPD 515, IPP 631, raw 9100, SNMP 161, mDNS 5353, WSD 3702, ENPC/D4, etc.) do not
   feed the stdin-based console. No "extra"/debug port routes into it. **Not reachable over LAN.**
3. **USB D4 / EJL / `@BDC` maintenance stream — NOT the transport.** Those are handled by the D4/protocol
   service tasks, entirely separate from `fgetc(stdin)`. **Not reachable via USB print channel.**
4. **epidc / `/dev/epidc0` — NOT the transport.** IDC is the inter-domain channel to the main SoC; the
   console does not read it.
5. **File / named pipe — NOT the transport.** No such source in the handler.

### What it would take to reach it without patching firmware
- **Physical UART access** (already yields root a simpler way). Practically, use the UART root shell instead.
- Config-only / env-only enablement over the network: **none exists** — there is no flag that redirects the
  console to a socket. Any LAN/USB path would require **modifying firmware** (e.g. redirecting nwsoc's fd 0 to
  a socket, or adding a listener), which is out of scope and explicitly disallowed.

**Conclusion:** the `'@' : run shell command` console is a **UART/local-only developer console with no remote
gate to flip**. It is genuinely a root-shell primitive, but only for someone already at the serial port —
where a root shell is already available. It is **not** a remote-access vector on stock or config-only firmware.

## Verification anchors
- String `'@' : run shell command`: file off `0x5c4f54` → VA `0x5ccf54`.
- Dispatcher `FUN_00072ecc` 0x72ecc; launcher `FUN_00072dd0` 0x72dd0; caller `PNPSMStartMain FUN_0006b6a4`
  BL @ 0x6c17c; exec `FUN_00076900` 0x76900 (`execl "/bin/sh","sh","-c"`, "-c" @ 0x5cdcb8).
- Launch chain: `/tmp/nwsoc/NWControl` (`./nwsoc`, no redirection), `/tmp/rootfs_remote/etc/inittab`,
  `/tmp/rootfs_remote/root/PreBoot`, `/tmp/rootfs_remote/etc/init.d/{rcS,S99NWControl}`.
