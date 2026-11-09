# Epson-Waste-Reset (EWR) — Relevance to XP-442 PUM Firmware Flash

Source: `Epson-Waste-Reset/` (upstream: waste-ink counter reset tool, C++/CMake).

## TL;DR

EWR **does not upload firmware** and does not touch USB PID `0x0007` (Program Update Mode). It talks to normal printing-mode Epson devices over IEEE 1284.4 to send factory `||:42:...;` EEPROM writes. However, it contains three artefacts that are directly useful to us:

1. **`fwu:ctrl` service is real.** For the XP-241-family (which the DB explicitly maps XP-442 into) EWR opens a **separate, short-lived D4 session** to the D4 socket named `"fwu:ctrl"` and sends the 5-byte command `67 6D 01 00 01` (`"gm"+01 00 01`) to enter firmware-recovery mode ("RCMODE"), waits for `"OK"` (`4F 4B`), then does its EEPROM writes on the normal EPSON-CTRL socket, then sends `67 6D 01 00 03` on `fwu:ctrl` at the end to leave.
2. **Their D4 stack is a very clean reimplementation of the same shape as dirty4** — same opcodes, same OpenChannel proposal (256/256 MTU, zero initial credit, `0x0000` unlimited outstanding), same header layout — with one detail worth copying: **their CreditRequest is 7 bytes (`04 SID SID FF FF 00 01`)**, matching what our PUM RE told us to switch to. See "CreditRequest shape" below.
3. **They have two non-1284.4 fallbacks (`END4` and `ESC/P Remote`)** for machines whose D4 handshake goes silent. Both are irrelevant to firmware update but confirm the on-the-wire framings Epson uses on the "data line".

No search hit for `04b8:0007`, "Program Update Mode", "IPL", "MFP-IPL", "ROIPLJ" or "firmware update mode" anywhere in the tree (`grep -rE '0007|program.update|MFP.IPL|firmware.update|ROIPLJ|ipl' ...` matched only unrelated words like "multiple GUID classes" and comments about credit-request triplets).

## The one big lead: `fwu:ctrl` service + `gm` command

### Database entry — XP-442 lives in the `XP-241-family` spec

`Epson-Waste-Reset/database.json:40753`:
```json
"XP-442": { "spec": "XP-241-family" },
"XP-443": { "spec": "XP-241-family" },
"XP-445": { ... "spec": "XP-241-family" ... }
```

Family spec at `database.json:50270`–`50346`:
```json
"XP-241-family": {
    "mem_high": 511,
    "pad_groups": [ ... EEPROM addresses ... ],
    "recovery": {
        "enter":   [103, 109, 1, 0, 1],  // 67 6D 01 00 01  = "gm" 01 00 01
        "close":   [103, 109, 1, 0, 3],  // 67 6D 01 00 03  = "gm" 01 00 03
        "reply":   [79, 75],             // 4F 4B          = "OK"
        "service": "fwu:ctrl"
    },
    "rkey": 1281, "rlen": 2,
    "wkey": "Tvsbnbev", "wkey1": "Suramadu", "wlen": 2
}
```

This is Epson's `fwu:ctrl` D4 service — "fwu" is exactly the term (firmware update) we've been looking for. Across the whole 42k-line database, `"service"` only ever holds one value: `"fwu:ctrl"`. It's the only firmware-related D4 service EWR knows about.

### How the recovery session is opened on the wire

`Epson-Waste-Reset/src/d4session.cpp:701`–`751` (`RunRecoveryCommand`):

- Takes the **already-open** USB bulk transport (same libusb handle used for EPSON-CTRL — a plain Epson bulk pair).
- Constructs a **new** `D4Session` with `serviceName = "fwu:ctrl"` (line 717).
- Calls `Start()` (line 727), which runs the standard sequence:
  1. EJL enter (`00 00 00 1B 01 @EJL 1284.4\n@EJL\n@EJL\n`) — sent via raw bulk (`d4session.cpp:355`–`373`).
  2. D4 Init transaction (`d4session.cpp:375`).
  3. `GetSocketID("fwu:ctrl")` (`d4session.cpp:389`–`412`) — a plain D4 transaction packet with opcode `0x09` and the ASCII service name concatenated. The reply carries the assigned socket id at payload[2]; if the printer never answers, it falls back to well-known socket `SOCKET_EPSON_CTRL = 0x02` (which for `fwu:ctrl` is almost certainly wrong).
  4. `OpenChannel` on `(socket, socket)` with 256/256 MTU (`d4session.cpp:417`–`457`).
- Calls `Exchange(command, reply)` (`d4session.cpp:737`), which:
  - Grants the printer one credit via `EnsurePrinterCredit()` — a D4 `CMD_CREDIT` (0x03) transaction: `{03, socket, socket, 00, 01}` with header credit=1 (`d4session.cpp:517`–`536`).
  - Calls `SendData()`, which chunks to MTU-6 and gates on `EnsureSendCredit()` — `{04, socket, socket, FF, FF, 00, 01}` header credit=1 (`d4session.cpp:538`–`558`, `560`–`585`).
  - Sends the 5-byte payload `67 6D 01 00 01` **as-is, not wrapped in `||`** (`d4session.cpp:577`). It goes straight into the D4 data packet payload.
  - Waits for a fragment on the `fwu:ctrl` socket, containing `4F 4B` ("OK") somewhere in it.
- Closes the recovery session (`d4session.cpp:738`).
- Later, on scope exit, opens a fresh recovery session again to send `67 6D 01 00 03` and close.

### What this means for XP-442 in PUM

- `fwu:ctrl` is a **real, named D4 service** on the XP-4xx family. It's not `EPSON-DATA`. On a normally-booted printer it's used only to toggle a firmware-recovery flag; the actual EEPROM writes still ride EPSON-CTRL.
- In PUM (`04b8:0007`), the printer is already the loader, and `EPSON-DATA` is refusing credit. It is worth **trying `GetSocketID("fwu:ctrl")` on the PUM D4 stack**: if the loader exposes a firmware-download service, it is almost certainly named this. Even a definitive "socket not found" reply is progress — it rules out one hypothesis.
- If `fwu:ctrl` is present in PUM, the same handshake shape (GetSocketID → OpenChannel → EnsurePrinterCredit → SendData) is what to speak on it — not raw bulk after the D4 handshake.

Related speculative service names worth probing on the PUM stack once we've confirmed `fwu:ctrl`: `fwu:data`, `fwu:update`, `MFP-IPL`, `EPSON-FWU`. None of these appear in EWR — they'd be guesses.

## CreditRequest shape (matches our current RE)

EWR ships two forms of the same packet, and both use the **7-byte payload** the PUM RE told us to switch to (vs. dirty4's 5-byte). Compare to the D4 stall notes.

Canned static packet at `Epson-Waste-Reset/src/generator.cpp:415`–`419`:
```
{ 0x00, 0x00, 0x00, 0x0D, 0x01, 0x00,       // D4 header: psid=0 ssid=0 len=13 credit=1 ctrl=0
  0x04,                                       // opcode = CREDIT_REQUEST
  SOCKET_EPSON_CTRL, SOCKET_EPSON_CTRL,       // 02 02
  0xFF, 0xFF,                                 // maximum credit-request = 0xFFFF
  0x00, 0x01 }                                // requested amount = 1 (2-byte BE)
```

Session-layer version at `Epson-Waste-Reset/src/d4session.cpp:544`:
```
SendPacket(0x00, 0x00,
    { D4::CMD_CREDIT_REQUEST, m_socket, m_socket, 0xFF, 0xFF, 0x00, 0x01 },
    0x01);
```

Header carries `credit=0x01` (a one-credit grant to the printer piggybacked on the CreditRequest transaction), payload is `04 SID SID FF FF 00 01`. Confirms the shape from `FIRMWARE_UPDATE.md`.

CreditGrant, for reference, is the 5-byte form (`03 SID SID 00 01`) at `generator.cpp:409`–`413` and `d4session.cpp:522`.

`EnsureSendCredit()` (`d4session.cpp:538`–`558`) says the printer's zero-credit reply — `Absorb` seeing `m_sendCredit <= 0` — is what triggers `"Printer answered the credit request but granted zero credit"`. This matches the PUM symptom of the DATA socket refusing to grant credit.

Comment at `generator.h:16`:
```cpp
constexpr uint8_t CREDIT             = 0x00; // must stay 0, nonzero locks up the hardware buffer
```
That's talking about the D4 header credit byte on **data** packets (byte 4 of the header). EWR sends data packets with header `credit=0x00` and only sets header `credit=0x01` on transaction packets (Init / OpenChannel / CloseChannel / CreditRequest / CreditGrant). If our flasher is putting a nonzero header credit on outgoing data packets, that could be the "silent lockup" they call out.

## D4 stack shape vs. dirty4

Same opcodes, same layout, same negotiation. Notable differences EWR does and dirty4 might not:

- **Length-framed reads.** `D4Framer::ReadPacket` (`d4session.cpp:96`–`183`) reads the 2-byte BE length from header bytes 2–3 and waits until *that many* bytes arrive; timeouts are only a safety net. dirty4 tends to sleep-and-drain, which shifts replies onto the wrong query when the printer splits.
- **Resync heuristic.** `FindPacketStart` (`d4session.cpp:49`–`62`) skips leading bytes unless `buffer[i] == buffer[i+1]` (both socket ids match — Epson always opens `sock→sock`) and the declared total is ≥ 6. Useful for shrugging off usbprint.sys prefix junk. On raw libusb this rarely matters, but it makes the code robust to a `00 00 00 …` NULL-flushed prefix.
- **Piggyback credit accounting.** `Absorb` (`d4session.cpp:238`–`276`) — every incoming non-transaction packet with `psid == m_socket` consumes one printer-credit and adds `pkt.credit` (header byte 4) to send-credit. Transaction replies to `CMD_CREDIT_REQUEST` carry the granted count in payload bytes 4–5 (BE).
- **`m_pendingData`** (`d4session.cpp:155`–`157`, 305–309, 320–326). If a data-channel fragment arrives while we're still waiting for a transaction reply, it's stashed and handed to the next `WaitForData` call. Prevents the classic "reply arrives with the credit ack, gets dropped, next query gets the wrong answer" bug.
- **`DiscardHeldReplies`** (`d4session.cpp:475`–`496`). After OpenChannel succeeds, EWR proactively grants credit and reads up to 32 stale replies "held over from an earlier session". Comment says an R220 hangs on to replies through CloseChannel/Exit/Init and hands them over the moment any later session grants credit. This is the same trap `FIRMWARE_UPDATE.md` hints at when it says early Data packets wedge the state machine. Not a fix for our arming issue, but worth stealing.
- **OpenChannel proposal:** MTU `01 00 / 01 00` (=256/256 BE), initial credit `00 00`, max outstanding `00 00` (unlimited). Exactly what our current flasher uses. See `d4session.cpp:417`–`424`.

## Non-D4 fallbacks (END4 / ESC/P Remote) — irrelevant but worth cataloguing

Purpose: on some ET-2xxx/L-series where usbprint.sys eats the 1284.4 handshake, EWR gives up on D4 and drops to a raw ASCII framing.

- **END4 framing** — `src/end4.cpp:21`–`35`, `include/ewr/end4.h:14`–`17`:
  ```
  'E','N','D','4', 02 01 00 00 00, [total_len_byte], 00 00 02 00, [command]
  ```
  Total length is a single byte — commands must be ≤ 241. Used only after the D4 channel goes silent.

- **Preamble to escape packet mode** — `src/end4.cpp:9`–`13`:
  ```
  00 00 00 1B 01 @EJL 1284.4\n @EJL \t\t\t\t\t\n
  ```
  Different from the D4 EJL enter (which uses `@EJL\n@EJL\n@EJL\n`) — the tab run tells the printer to LEAVE packet mode, not enter it.

- **DDS flush** — after the preamble, send `DDS` bytes (from the `DDS:` field of the 1284 device ID, hex) of `0x11` filler to flush the packet-mode parser. `src/end4.cpp:97`–`125`.

- **ESC/P Remote framing** — `src/end4.cpp:37`–`63`. `1B @` (reset) ×2, `1B ( R 08 00 00 REMOTE1`, then the raw factory command, then `1B 00 00 00 1B @`. Used as third-rung fallback.

None of these are hints about firmware upload; they're all EEPROM-only.

## USB layer notes

`Epson-Waste-Reset/src/usb_libusb.cpp:180`–`241` (`CollectCandidates`):
- Filters by Epson VID (`0x04B8`) only — no PID filter, so `04b8:0007` would be included if it enumerates.
- Accepts either `LIBUSB_CLASS_PRINTER` or `LIBUSB_CLASS_VENDOR_SPEC` interfaces with any bulk IN+OUT pair.
- Sorts printer-class first, then by `bInterfaceNumber`.
- No kernel-driver detach quirks specific to PUM.

No special setup (no `SET_INTERFACE`, no vendor control transfer) is done before the first bulk send. The 1284.4 GET_DEVICE_ID printer-class control request (`usb_libusb.cpp:307`–`324`) is used for discovery only.

## Answers to your seven questions

1. **How do they open a session?** Standard 1284.4 over Epson bulk pair. EJL enter (`@EJL 1284.4\n@EJL\n@EJL\n`) → D4 Init (`0x00 0x10`) → `GetSocketID("EPSON-CTRL")` → OpenChannel(sock, sock, 256/256, credit 0). All on the same bulk pair, no PID-specific setup. See `d4session.cpp:353`–`467`.

2. **`04b8:0007` / IPL / MFP-IPL / firmware-update-mode references?** None. Zero hits across code, docs, DB.

3. **How do they format data-channel writes?** After OpenChannel:
   - Wait for send-credit (`EnsureSendCredit` — CreditRequest transaction on the session socket).
   - Split payload into chunks of `mtuToPrinter - 6` bytes.
   - Each chunk is a D4 data packet: `[psid=sock, ssid=sock, len BE (chunk+6), credit=0, ctrl=0, ...chunk...]`.
   - No `@EJL FUS` / `@EJL EN` before it — for EEPROM writes the payload is a `||`-prefixed factory command; for the firmware-recovery `gm` command it's just the raw 5 bytes. Firmware bytes: EWR doesn't do it.

4. **CreditRequest shape?** 7-byte payload `04 SID SID FF FF 00 01`, header credit=1. Amount requested = 1. Same as the shape our PUM RE recommends. See `generator.cpp:415`, `d4session.cpp:544`.

5. **Vendor-specific setup command before writes?** Only for the recovery-mode families (which includes XP-442). It's the `gm 01 00 01` payload on the `fwu:ctrl` D4 socket, expecting `"OK"`. This runs **on the same USB bulk pair** as everything else, but in **its own D4 session** — Start / Exchange / Close, then re-Start for the actual writes. See `d4session.cpp:701`–`751` and `820`–`839`.

6. **XP-442 model config?** Yes, in `database.json:40753` mapped to spec `XP-241-family` at `database.json:50270`–`50346`. The spec explicitly lists the recovery block with the `fwu:ctrl` service and the `gm` enter/close commands.

7. **Vendor libraries?** Only `nlohmann/json.hpp` (JSON parser for the database). D4 stack is written from scratch, matches dirty4's shape.

## Recommendations

**Priority 1 — Do this next.** Once we've re-opened the D4 handshake on the PUM device and confirmed EPSON-CTRL works, immediately try `GetSocketID("fwu:ctrl")` on the PUM D4 stack. If the loader answers with a socket id ≠ 0:
- OpenChannel on that socket (same 256/256, credit 0 proposal).
- Try `EnsurePrinterCredit` + `EnsureSendCredit` on the `fwu:ctrl` socket. If it grants credit here where `EPSON-DATA` refused, the loader is telling us its firmware pipe is `fwu:ctrl`, not `EPSON-DATA`. The `state[+0x1574]` gate mystery would resolve as "wrong socket, not wrong flag".
- If credit is granted, first try the recovery-mode enter `67 6D 01 00 01`. If the loader answers `4F 4B`, we're inside its firmware acceptance state and can try streaming firmware bytes. If not, try streaming firmware bytes without the `gm` prelude — a loader that is already the loader may not need the "enter recovery" toggle.
- **The 5-byte payload is sent raw into the D4 data packet** — no `||` prefix, no EJL, no ESC/P Remote wrapper. Matches what our EEPROM path would look like once we're past the handshake.

**Priority 2 — Fix the CreditRequest already if not done.** Switch to the 7-byte form `04 SID SID FF FF 00 01` (with D4 header credit=1). Our RE says 5-byte is dirty4's bug; EWR confirms 7-byte is what Epson's own firmware answers. `epsflasher_mac.py`'s CreditRequest monkey-patch should send exactly `{04, sid, sid, 0xFF, 0xFF, 0x00, 0x01}` as the transaction payload with header credit `0x01`. Anything else EWR does (data-packet header credit stays `0x00`, transaction header credit `0x01`) is worth matching too.

**Priority 3 — Copy `DiscardHeldReplies`.** After OpenChannel, grant printer-credit + read-with-timeout in a loop up to 32 times, discarding whatever arrives. If a previous session (or a previous run of our own flasher) left the printer holding an unsent reply, that reply is what wedges the state machine when we send the first real Data packet. `d4session.cpp:475`–`496`. This is one plausible explanation for the "silently drops subsequent packets" symptom in `FIRMWARE_UPDATE.md`.

**Priority 4 — On any Data-packet send, header credit byte must be `0x00`.** EWR's comment at `generator.h:16` says explicitly: nonzero header credit on data packets "locks up the hardware buffer". Worth auditing our flasher.

**What EWR does NOT tell us.** Whether the loader in PUM even exposes `fwu:ctrl` (or any GetSocketID service), what the firmware payload framing is (checksummed blocks? plain bytes? EJL-wrapped?), whether the loader needs `SET_INTERFACE` or a vendor control transfer, or where the actual `state[+0x1574]` gate is armed from. Those are still open questions for the Ghidra pass on the loader — but knowing the "fwu" naming and the `gm` verb narrows the search space in the loader binary considerably: `grep`/search for `"fwu"`, `"gm"`, `0x67 0x6D` strings, and any GetSocketID handler.

## Handy file:line map

- `d4session.cpp:353`–`467` — full D4 Start() choreography.
- `d4session.cpp:517`–`558` — EnsurePrinterCredit / EnsureSendCredit (the 5-byte CreditGrant and 7-byte CreditRequest).
- `d4session.cpp:560`–`585` — SendData chunking + credit gating.
- `d4session.cpp:587`–`643` — Exchange (SendData + fragment reassembly).
- `d4session.cpp:645`–`673` — Close (CloseChannel + Exit).
- `d4session.cpp:675`–`686` — ExtractDataPayload (strips the 6-byte D4 header off a canned packet).
- `d4session.cpp:701`–`751` — **RunRecoveryCommand: the `fwu:ctrl` / `gm` flow.**
- `d4session.cpp:820`–`839` — where the recovery guard is armed for a write session.
- `d4session.cpp:475`–`496` — DiscardHeldReplies.
- `generator.cpp:409`–`419` — canned CreditGrant/CreditRequest packets.
- `generator.cpp:458`–`482` — canned handshake (EJL enter / D4 Init / OpenChannel).
- `generator.h:127`–`139` — `RecoveryChannel` struct definition.
- `executor.h:80`–`87` — ExecutorOptions recovery fields with the `67 6D 01 00 01` example in the comment.
- `end4.cpp:9`–`63` — END4 and ESC/P Remote framings (not firmware-relevant but complete non-D4 catalogue).
- `session.cpp:295`–`302` — where the DB's recovery data is copied into ExecutorOptions.
- `database.json:40753` — XP-442 → `XP-241-family`.
- `database.json:50270`–`50346` — `XP-241-family` spec including the recovery block.
