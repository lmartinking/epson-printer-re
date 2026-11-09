# SD Card over SMB — how the memory-card share is exposed

How the printer's SD/memory card is served as a Windows (CIFS/SMB) network share.

## TL;DR

The **network CPU never touches the SD card hardware.** There is no `smb.conf`, no
`smbd`, and no SD/MMC mount anywhere in the network CPU's Linux rootfs. The entire
SMB stack is a **custom CIFS server compiled into the `nwsoc` binary**, and it reaches
the card over the inter-processor (IDC) link to the **main (printer) CPU**, which
physically owns the card slot and runs the FAT filesystem.

Data path:

```
SMB client (PC)
  → nwsoc CIFS server task           (SMB1 "NT LM 0.12" / SMB2 "SMB 2.002")
  → EPMS command                     (Epson Mass Storage protocol)
  → IDC channel  /dev/epidc0         (epidc.ko driver)
  → main CPU                         (owns the slot, runs FAT)
  → SD / memory card
```

## 1. Two CPUs, no shared block device

- [squashfs_rootfs1/etc/fstab](squashfs_rootfs1/etc/fstab) mounts only proc/sys/tmpfs.
  The SD card is **never mounted** on the network CPU's Linux. The commented
  `mtdblock` lines are the only storage the NIC CPU knows about (its own firmware).
- The main (printer) CPU physically owns the card slot. The two chips communicate
  over an IDC (inter-die communication) channel via the `epidc.ko` driver → device
  node `/dev/epidc0`, loaded by `idc_load.sh` from
  [NWControl:56](squashfs_rootfs2/NWControl).

## 2. The SMB server lives inside `nwsoc`

[squashfs_rootfs2/nwsoc](squashfs_rootfs2/nwsoc) (~6.8 MB, ARM ELF, stripped)
contains the whole stack — no external Samba. Evidence (strings):

- `CIFS server task already running.` / `CIFS NetBIOS task already running.` — its
  own SMB + NetBIOS-over-TCP tasks (ports 137/138/139; see
  [squashfs_rootfs1/etc/services](squashfs_rootfs1/etc/services)). This is why
  [NWControl:97](squashfs_rootfs2/NWControl) brings up loopback with the comment
  *"It's required by CIFS"* (`ifconfig lo 127.0.0.1`).
- Dialects advertised: `NT LM 0.12` (SMB1) and `SMB 2.002` (SMB2), with SMB2 gated by
  a runtime flag: `SMB2Enable`, `SMB2_ENABLE`, `PCIFSetSMB2Enable`.
- Default share name: **`MEMORYCARD`**. Also `EPSON Storage Server`, and `*SMBSERV`
  (the NetBIOS wildcard name used for browsing).

## 3. The bridge: EPMS over IDC

The SMB server does not call a POSIX filesystem. It translates SMB file operations
into an **Epson Mass Storage (EPMS)** protocol carried over the IDC channel to the
main CPU:

- Two dedicated IDC channels: `EPSON-STORAGE-CTRL` and `EPSON-STORAGE-DATA`, driven by
  `epidc_open_channel` / `epidc_send_data` / `epidc_receive_data`.
- EPMS is a SCSI / USB-Mass-Storage-flavoured command set that also carries
  **filesystem-level** semantics — the FAT layer lives on the main CPU. The error
  enum shows both levels:
  - Block/SCSI level: `EPMS_ERR_INVALID_CDB`, `EPMS_GetMaxLUN`,
    `EPMS_ERR_MEDIA_CHANGED`, `EPMS_ERR_NO_MEDIA`, `EPMS_ERR_INCOMPATIBLE_MEDIA`.
  - Filesystem level: `EPMS_ERR_FS_FILE_NOT_FOUND`, `EPMS_ERR_FS_DIRECTORY_EXIST`,
    `EPMS_ERR_FS_FILE_SHARE_MODE_ERROR`, `EPMS_ERR_FS_WRITE_PROTECTED`,
    `EPMS_ERR_FS_UNFORMATTED_MEDIA`, `EPMS_ERR_FS_DISK_NOT_FREE`.
- Lifecycle helpers seen: `EPMS_Reset` (via `EPMS_Reset()` / `MassStorageReset`),
  `EPMS_ShutDown`, `EPMS_GetMaxLUN`.

## 4. Configuration & the read-only toggle

Config is pushed from the main CPU to `nwsoc` over the **PCIF** channel
(`PNPCIFStart`, `PNPCIFSendStorageSetting`). Relevant keys read/written by `nwsoc`:

| Key | Meaning |
|---|---|
| `SMB_STORAGE_SHARE_SUPPORT` / `SMB_STORAGE_SHARE_ENABLE_DISABLE` | master File Sharing on/off (panel / Web Config) |
| `SMB_STORAGE_SHARE_CURRENT_ACTIVATION` | current active state of the share |
| `STORAGE_PUBLIC_SHARE_NAME` / `STORAGE_SHARE_NAME` (`_CHANGEABLE`) | share name (default `MEMORYCARD`) |
| `STORAGE_PUBLIC_DEVICE_NUM` / `STORAGE_PUBLIC_DEVICE_TYPE` | which storage device/LUN is published |
| `STORAGE_DEVICE_NUM` / `STORAGE_DEVICE_TYPE` | total devices / type enumeration |
| `SMB_STORAGE_RO_FA_CHANGE` (`_SUPPORT`) | **Read-Only ↔ Read/Write** access mode |
| `WORKGROUP` / `WorkGroupName` (`pnGetMSNWorkGroupNameInfo`) | NetBIOS workgroup |

`SMB_STORAGE_RO_FA_CHANGE` corresponds to the printer's "File Sharing → Read-Only /
Read-Write from network" menu option. When read-only, writes fail with
`EPMS_ERR_FS_WRITE_PROTECTED` (`FS WRITE PROTECTED`).

## 5. Access control

Access is effectively **open / guest** — the classic Epson memory-card share. There
are no per-user CIFS credentials in the storage path; access is governed only by the
enable flag and the RO/RW flag, not a password. (The `anonymous` /
`Anonymous Authentication` strings in `nwsoc` belong to the 802.1X/EAP supplicant, not
the SMB server.) On the LAN, anyone who can reach the NetBIOS/SMB ports gets the card
at whatever access mode `SMB_STORAGE_RO_FA_CHANGE` is set to.

## 6. Next steps (not yet done)

Ghidra trace of `nwsoc` (base `0x8000`) to map SMB `TREE_CONNECT` → EPMS dispatch.
Good entry points are the string xrefs to `MEMORYCARD`, `STORAGE_PUBLIC_SHARE_NAME`,
and `PNPCIFSendStorageSetting`.
