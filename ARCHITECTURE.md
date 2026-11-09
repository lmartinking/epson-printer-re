# Epson XP-442 — System Architecture (Dual-Domain SoC)

The printer runs on a **single Epson SoC ("SoC09S")** split into **two firmware
domains** that execute concurrently and communicate over a shared-memory link.
This is the key to understanding where every service and setting lives.

## Execution domains

| Domain | CPU | OS | Runs | Firmware location |
|--------|-----|----|------|-------------------|
| **Main / print domain** | ARM1136JF-S | T-Kernel RTOS (Epson-extended) | Print engine, front panel, authoritative device config/NVRAM | `ipl_0_seg1` `0x0`–`0x110400` (uncompressed) + CROM blob in `ipl_1_seg1` |
| **Network domain** | ARM926EJ-S | Linux 2.6.35.8 (uClibc) | TCP/IP stack, Wi-Fi, all network protocol servers (`nwsoc`) | `ipl_0_seg1` `0x110400`+ (uImage + 2× SquashFS) |

Both domains' code ships inside **Block 0** (`ipl_0_seg1`). Block 1
(`ipl_1_seg1`) is a separate **CROM-compressed** image (magic `CROM` @ `0x10`,
version tags `RO20J9` / `TXW01K`) — not yet decompressed here; see
[FIRMWARE_LAYOUT.md](FIRMWARE_LAYOUT.md).

### Confirmed evidence (main domain / ARM1136)

All in [rcx_output/ipl_0_seg1](rcx_output/ipl_0_seg1):

- `0x00` — ARM 32-bit exception vector table (`ldr pc,[pc,#0x1c]` ×8), bare-metal reset entry
- `0x62` — `BooSt for SoC09S 1.0.3 (2008/08/01)` (bootstrap loader)
- `0x88` — `Start ARM1136JF-S ... RTOS` (RTOS banner)
- `0xf26f` — `EPSON PRINTER ROM`
- `0xf9e4` — IEEE-1284 device ID: `MFG:EPSON;CMD:ESCPL2J,BDC,D4;MDL:MFP-IPL;CLS:PRINTER`

Region entropy: 4.7–5.6 bits/byte = uncompressed ARM code (one compressed
sub-section ~`0x40000`, plus zero-padding gaps). RTOS base address likely `0x0`
(inferred from vector table; not yet confirmed via load record).

### Confirmed evidence (network domain / ARM926 Linux)

- `nwsoc` interpreter: `/lib/ld-uClibc.so.0` (`readelf -p .interp`)
- uImage: ARM Linux 2.6.35.8-arm1 at `ipl_0_seg1` `0x110400`
- All network protocol servers live in `nwsoc`: WebConfigEx (HTTP admin),
  WSD (WSDL for print/scan), IPP, SNMP, LPD, ENPC, etc.

## Inter-domain link: EPIDC

The two domains talk over **EPIDC** = "EPSON Inter-domain communication driver".

- Kernel module: [squashfs_rootfs2/epidc.ko](squashfs_rootfs2/epidc.ko)
  - Banner: `EPSON Inter-domain communication driver (SOC09S) ver.2.16`
  - Copyright `SEIKO EPSON CORPORATION 2007-2009`
- Loaded by [squashfs_rootfs2/idc_load.sh](squashfs_rootfs2/idc_load.sh):
  ```sh
  insmod /lib/modules/epidc.ko major=100 shmemaddr=0x42003000 \
    shmemsize=0x15B000 commctrlmemaddr=0xe0c00000
  ```
- Transport = **shared physical memory** (`shmemaddr` / `commctrlmemaddr`) + an IRQ
  (`request_irq`). Exposed to Linux userland as **`/dev/epidc0`**.
- Command set includes panic signalling and flash control:
  `EPIDC_CMD_NW_PANIC`, `EPIDC_CMD_MAIN_PANIC_ACK`, `EPIDC_WAIT_PANIC_MODE`,
  `epidck_submit_orb`, flash control reply.

## Transport protocol: D4 (IEEE-1284.4 / DOT4)

**D4** is Epson's IEEE-1284.4 (a.k.a. DOT4 / MLC) packet protocol that
**multiplexes many logical channels over one physical link**. Originally the
USB/parallel printer transport; here it is reused *internally*, carried over
EPIDC shared memory.

- Binding to IDC is explicit: `D4CbtSendIdc`, `D4CbtReceiveIdc`, `D4CbtCloseIdcChannel`
- `nwsoc` opens these D4 logical channels:

| D4 channel | Purpose |
|------------|---------|
| `D4_OpenMibChannel` / `D4_OpenMibReverseChannel` | Status + config (MIB) — **config bridge to main domain** |
| `D4_OpenIppUsbCtrlChannel` / `…DataChannel` | IPP-over-USB |
| `D4_OpenPtpIpCmdChannel` / `…EvtChannel` | PTP/IP (photo / scan) |
| `D4_OpenReverseChannel` | Async events back from the engine |

## COMIF: the config bridge

`COMIF` (Communication InterFace) is the `nwsoc`-side API that talks to the main
domain over the D4 MIB channel:

- `PNCOMIFGetField` / `PNCOMIFSetField` — read/write authoritative device fields
- Events: `COMIF_LEVT_GET`, `COMIF_LEVT_SET`, `COMIF_LEVT_READSETUP`,
  `COMIF_LEVT_WRITESETUP`, `COMIF_LEVT_LOCKFLASH` / `UNLOCKFLASH`,
  `COMIF_LEVT_RESET`, `COMIF_LEVT_RequestReboot`
- `PNCOMIFWebConfigExCheckPermission` — permission gate for the web-admin path

## How it fits together

```
   NETWORK DOMAIN (ARM926 / Linux, nwsoc)          MAIN DOMAIN (ARM1136 / RTOS)
   ┌─────────────────────────────────┐             ┌──────────────────────────┐
   │ HTTP/WebConfigEx, SNMP, WSD,     │             │ print engine, panel,     │
   │ IPP, LPD, ENPC  (all in nwsoc)   │             │ authoritative NVRAM/config│
   │        │                         │   D4 over   │                          │
   │  PNRSC (local net-settings) ◄────┼── EPIDC ───►│  COMIF peer              │
   │        ▲   COMIF / PNCOMIFSetField│  shared mem │                          │
   └────────┼─────────────────────────┘  /dev/epidc0└──────────────────────────┘
            │
   Config writes converge on PNRSC (see PNRSC_CONFIG_REGISTRY.md)
```

See [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md) for the PNRSC registry internals and the telnet
activation-vector trace, and [NETWORK_SERVICES.md](NETWORK_SERVICES.md) for the network-services
inventory.
