# Block 0 Firmware Filesystem Analysis

## Overview

Block 0 (main printer firmware) contains 3 components:

| Component | Offset | Size | Description |
|-----------|--------|------|-------------|
| uImage kernel | `0x110400` | ~1 MB | ARM Linux 2.6.35.8 |
| SquashFS #1 | `0x230000` | ~1.1 MB | Main root filesystem (426 inodes) |
| SquashFS #2 | `0x370000` | ~2.8 MB | Network/WiFi module (35 inodes) |

## SquashFS #1 — Main Root Filesystem

**Location:** `rcx_output/ipl_0_seg1` at offset `0x230000`
**Extracted to:** `squashfs_rootfs1/`
**Stats:** 83 files, 52 directories, 157 symlinks
**Compression:** xz
**Created:** 2017-08-02

### Directory Structure

```
bin/          — BusyBox applets (46 binaries)
etc/          — System config (init scripts, network, passwd, hosts)
etc/init.d/   — Startup scripts (rcS, S20urandom, S40network, S99NWControl)
etc/network/  — Network interfaces (loopback only)
lib/          — uClibc 0.9.32 shared libraries
opt/          — Epson-specific apps (nwsoc, version info)
opt/nwsoc/    — NWControl script (symlink target)
opt/version/  — Firmware version (symlinks to ../nwsoc/version)
sbin/         — System binaries (init, ifconfig, insmod, modprobe, etc.)
usr/lib/      — libusb, libstdc++
linuxrc       — Kernel init script (compressed binary)
```

### Rootfs #1 — Key Findings

- **BusyBox-based** — All standard utilities are BusyBox symlinks (ash, bash, vi, tar, grep, etc.)
- **uClibc 0.9.32** — Lightweight C library for embedded ARM
- **No Epson executables** in standard paths (`/bin`, `/sbin`, `/opt`)
- **NWControl** at `/opt/nwsoc/NWControl` is a shell script that calls the ARM binary `nwsoc`
- **Network config minimal** — only loopback interface configured in `/etc/network/interfaces`
- **Init sequence:** `rcS` → S20urandom → S40network → S99NWControl

### Init Scripts

| Script | Action |
|--------|--------|
| `rcS` | Runs all S* scripts in `/etc/init.d/` |
| `S20urandom` | Initializes `/dev/urandom` |
| `S40network` | Configures network interfaces |
| `S99NWControl` | Symlink → `../../opt/nwsoc/NWControl` (WiFi manager) |

### Libraries

| Library | Version |
|---------|---------|
| `libuClibc` | 0.9.32 |
| `libstdc++` | 6.0.14 |
| `libusb` | 1.0.0.0.0 |
| `libgcc_s` | 1 |

## SquashFS #2 — Network/WiFi Module

**Location:** `rcx_output/ipl_0_seg1` at offset `0x370000`
**Extracted to:** `squashfs_rootfs2/`
**Stats:** 22 files
**Compression:** xz
**Created:** 2018-10-03

### Contents

| File/Dir | Type | Description |
|----------|------|-------------|
| `nwsoc` | ELF 32-bit ARM | Network socket daemon (main binary) |
| `NWControl` | Shell script | Launcher for nwsoc (infinite loop) |
| `epidc.ko` | Kernel module | Epson device driver |
| `bcm43143/` | Directory | Broadcom BCM43143 WiFi firmware |
| `dibbler` | Binary | DHCPv6 client |
| `dibbler.conf` | Config | Dibbler configuration |
| `udhcpc` | Binary | DHCP client |
| `udhcpc.script` | Shell script | DHCP client hooks |
| `nwsoc_RootCert.pem` | Certificate | Root CA certificate |
| `nwsoc_RootCert.inf` | Info | Certificate metadata |
| `wlan.confdef` | Config | WiFi defaults |
| `zeroconf.sh` | Shell script | mDNS/Bonjour support |
| `startup_dhcp.sh` | Shell script | DHCP startup |
| `ether_load.sh` | Shell script | Ethernet module loader |
| `ether_dummy_load.sh` | Shell script | Dummy ethernet module |
| `idc_load.sh` | Shell script | IDC driver loader |
| `version` | Text | Version string |
| `dibbler-notify.sh` | Shell script | Dibbler notifications |
| `dummyread_dibbler.sh` | Shell script | Dummy dibbler reader |

### NWControl — Network Manager

**Path:** `squashfs_rootfs2/NWControl`

Shell script that:
1. Loads IDC driver via `idc_load.sh`
2. Pre-caches `nwsoc` and BCM43143 firmware into memory
3. Sets system time to `2014.01.01-12:00:00` (fallback)
4. Configures loopback interface
5. Enters infinite loop calling `nwsoc` binary
6. Handles return codes: 2 = firmware update reboot, 1 = shutdown

### Key Binary: nwsoc

- **Type:** ELF 32-bit LSB executable, ARM, EABI5
- **Linker:** `/lib/ld-uClibc.so.0`
- **Stripped:** Yes
- **Purpose:** Network socket daemon for Epson printer network services (Epson Scan, remote control, firmware updates)

## Firmware Version

From `squashfs_rootfs2/version` and `squashfs_rootfs1/opt/version/a`:

| Source | Content |
|--------|---------|
| `squashfs_rootfs2/version` | Version string |
| `squashfs_rootfs1/opt/version/a` | Symlink → `../nwsoc/version/a` |

## Extraction Commands

```bash
# Extract SquashFS #1 (rootfs)
dd if=rcx_output/ipl_0_seg1 of=/tmp/squashfs1.bin bs=1 skip=0x230000 count=1180420
unsquashfs -f -dest squashfs_rootfs1 /tmp/squashfs1.bin

# Extract SquashFS #2 (network module)
dd if=rcx_output/ipl_0_seg1 of=/tmp/squashfs2.bin bs=1 skip=0x370000 count=2852622
unsquashfs -f -dest squashfs_rootfs2 /tmp/squashfs2.bin
```
