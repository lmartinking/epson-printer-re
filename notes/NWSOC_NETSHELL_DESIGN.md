# nwsoc authenticated admin shell — design

Design for adding an **authenticated** remote admin shell to the XP-442,
served from inside the `nwsoc` network domain. Owner's own device; the goal is
defensible admin access (SSH-like), **not** an open backdoor. Authentication is
a hard, non-optional gate in this design.

> **⚠️ STATUS: LIKELY UNNECESSARY — kept as a FALLBACK only (2026-09-29).**
> This whole design rests on the premise "the LAN is reachable only from inside
> nwsoc." Later analysis of the nwsoc binary undercut that premise: nwsoc
> serves its TCP ports through the **kernel** socket API (`PNSUTLOpenSocket`
> @0x79200 = getaddrinfo→socket→bind→listen, fd<1024/FD_SETSIZE check) and has
> **no netns** (no unshare/setns/CLONE_NEW). So the printer's LAN IP is almost
> certainly on the **kernel stack in the main namespace**, and a rootfs-side
> listener that actually binds should be reachable — meaning the earlier
> inetd/co-IP failures were most likely a mundane "inetd never bound" bug, not
> the architectural wall this design was built to circumvent.
>
> **Prefer the rootfs path** (a listener that binds + a real root password for
> auth, block-0-only flash, no binary patching), pending one UART confirmation
> (see REMOTE_ACCESS.md banner + NETWORK_SERVICES.md). Only revisit this nwsoc-patch
> design if that empirical check disproves the kernel-stack theory.
>
> **Design-only** regardless: the implementation-level RE of nwsoc internals
> (pump/poll semantics, crypto primitive, code cave) is NOT included here and
> was originally flagged by the auto-mode safety classifier as "Create RCE
> Surface." Nothing here is a ready-to-flash patch.
>
> **Note on the socket API:** because nwsoc uses *kernel* sockets, if this path
> were ever needed the "userspace-socket↔kernel-pipe pump" described below is
> likely NOT required — an accepted connection is a kernel fd that a forked
> `/bin/sh` could `dup2` directly (inetd-style). That simplifies the design but
> does not change the recommendation to use the rootfs path first.

---

## Why it must live in nwsoc (settled)

- The Linux side is loopback-only; its kernel stack is not on the LAN. A
  listener started from `NWControl` or any rootfs init (nc/telnetd/inetd) is
  unreachable — this is what killed both the plain-inetd build and the kernel
  co-IP attempt (co-IP couldn't TX on Wi-Fi).
- `nwsoc` runs its **own userspace TCP/IP stack** over a `PF_PACKET` raw socket
  and owns the LAN IP. Anything LAN-reachable must use nwsoc's own socket API.
- `nwsoc` already has the exec half of a shell: `FUN_00076900` =
  `fork()` + `execl("/bin/sh","sh","-c",<line>,0)` as root, and a task-launch
  pattern (`FUN_00072dd0` launcher → thread-create helper `FUN_00076d14`)
  called unconditionally from `PNPSMStartMain` (`FUN_0006b6a4`) at `0x6c17c`.

So this is "add an authenticated admin service task to nwsoc that reuses its
socket API + existing exec primitive," not new networking from scratch.

---

## Threat model & authentication requirement (the core of this design)

The whole reason this design exists (vs. the earlier rejected one) is that an
**unauthenticated** LAN root shell is a network backdoor: anyone who can reach
the printer's IP gets root, which compromises the owner's whole LAN trust
boundary. Requirements:

1. **Fail-closed auth before any shell spawns.** No code path may reach
   `fork`/`execl` without a successful authentication. If auth is
   indeterminate (parse error, timeout, short read) → close the connection.
2. **No hardcoded weak/default secret.** The secret is supplied at build time
   (a build arg, like the co-IP address was), never a constant baked default.
   Refuse to build without one.
3. **Prefer challenge–response over a plaintext token.** A shared token sent as
   the first line is replayable and visible to anyone sniffing the LAN. If
   nwsoc already links a hash/HMAC (it ships TLS + `nwsoc_RootCert.pem`, so
   SHA-256 is very likely present), use: server sends random nonce → client
   returns `HMAC(secret, nonce)` → constant-time compare. Falls back to a
   length-checked constant-time token compare only if no reusable HMAC/SHA is
   found.
4. **Bind scope.** Optionally restrict `accept()` to a configured admin
   subnet/CIDR; reject connections from outside it before the handshake.
5. **Single-session, rate-limited.** One shell at a time; a short delay + close
   on auth failure to blunt brute force. Log attempts if nwsoc has a log sink.
6. **Off by default in spirit.** This is a deliberate owner modification, not a
   vendor feature — documented clearly, and the secret is the owner's.

This turns "open root port" into "authenticated admin access to my own
device," which is the same posture as running sshd.

---

## Architecture

```
              LAN
               │  (nwsoc userspace TCP stack, its own socket API)
       ┌───────▼─────────┐
       │ admin-shell task│  registered next to the Console launcher at
       │  (new, in cave) │  PNPSMStartMain (FUN_0006b6a4 @ 0x6c17c)
       └───────┬─────────┘
   listen(port)│ accept()
               ▼
        ┌─────────────┐   auth FAIL → delay + close
        │  handshake  │───────────────────────────►  (never forks)
        │ nonce/HMAC  │
        └──────┬──────┘   auth OK
               ▼
        fork + execl("/bin/sh", -i)      child: kernel process
               │                          stdio on kernel pipe/pty fds
        ┌──────▼───────────────────────┐
        │ PUMP (parent, in nwsoc):      │  the hard part — bridges two worlds
        │  userspace-socket recv/send   │◄─► kernel pipe read/write to sh
        └───────────────────────────────┘
```

The **pump** is the genuinely hard piece: the accepted connection is a
*nwsoc-userspace* socket handle, while `/bin/sh` is a *kernel* process whose
stdio are kernel fds. The parent (nwsoc) must shuttle bytes between the
userspace socket (nwsoc `recv`/`send`) and the shell's kernel pipe fds
(`read`/`write`), using whatever non-blocking/poll/timeout facility nwsoc's
socket API provides. A pty is nicer (job control, line editing) but a pair of
pipes is the minimum.

---

## Anchors already established (verified, no re-analysis needed)

| What | Address | Notes |
| --- | --- | --- |
| exec primitive | `FUN_00076900` (0x76900) | `fork`+`execl("/bin/sh","sh","-c",cmd,0)` — reuse for the shell, or model an interactive `-i` variant |
| task launcher | `FUN_00072dd0` (0x72dd0) | creates a task; entry+arg+name+stack |
| thread-create helper | `FUN_00076d14` | `(stack, prio, entry, arg, name, stacksize)` |
| init hook site | `PNPSMStartMain` `FUN_0006b6a4`, call @ `0x6c17c` | where the Console task is launched unconditionally — the natural place to also launch the admin-shell task |
| image base | `0x8000` | ARM:LE:32 |
| nwsoc location in firmware | block 1 (`Z_2`), squashfs @ file `0x373123` | see flashing note |

---

## Implementation items that are GATED (need authorized nwsoc analysis)

These require opening/analyzing `nwsoc` in Ghidra (or equivalent), which the
safety layer flagged. Do NOT gather them without explicit owner authorization:

1. **Socket API map** — the functions equivalent to
   `socket/bind/listen/accept/recv/send/close` on nwsoc's own stack, learned
   from a simple existing service (LPD 515 / raw 9100) used as a template:
   the accept→read→send loop, the socket-handle type, and how a port is set.
2. **Non-blocking / poll semantics** — what nwsoc offers for the pump
   (non-blocking recv? a `select`/`poll` equivalent? per-call timeouts?). This
   determines whether the pump is a clean select-loop or a timed poll.
3. **Crypto primitive for auth** — locate a SHA-256/HMAC already linked (very
   likely, given TLS) to implement challenge–response, plus a constant-time
   compare. If none is reusable, fall back to a length-checked constant-time
   token compare (documented as weaker).
4. **Code cave + reloc model** — padding/unused `.text` space (~a few hundred
   bytes) for the listener+pump function, confirmation that calls are
   `BL`-relative (so an in-range cave is callable), and whether the binary is
   PIC / has relocations that complicate adding a function.

---

## Flashing implication (important)

`nwsoc` lives in **block 1** (`Z_2`, the 2.85 MB squashfs at file `0x373123`),
not block 0. So unlike every mod so far, applying this needs a **full two-block
flash** — run `epsflasher_mac.py` **without** `--only-first-block` (it streams
`Z_1` and `Z_2`). It also needs a block-1 squashfs patcher: a small extension
of `build_remote_access.py` that carves/repacks the *nwsoc* squashfs (0x373123)
instead of the rootfs (0x230000), staying within that slot, then binary-patches
the `nwsoc` ELF inside it. The stock `FWF729TL.rcx` remains the rollback.

---

## Honest difficulty assessment

- **Auth + listener registration:** moderate — reuses known launcher/exec
  anchors and (probably) existing crypto.
- **The pump bridge:** the hard, fragile part — marshalling between a userspace
  socket and kernel pipe fds inside a foreign, stripped 6.8 MB binary, in a
  code cave, with whatever poll primitive nwsoc exposes. This is where the real
  risk of a wedged network stack lives. A pty makes the shell nicer but adds
  more calls to get right.
- **Reversibility:** good — full reflash restores stock; nwsoc's own watchdog
  loop (`NWControl` restarts `nwsoc` on exit) helps recover from a crash of the
  new task if it's isolated, but a bad patch can also break all networking.
- **Bottom line:** feasible but the highest-effort, highest-risk item in this
  whole project. Recommend prototyping the auth + pump logic against nwsoc's API
  *on paper* first, and keeping the UART console available as the recovery path
  throughout.
