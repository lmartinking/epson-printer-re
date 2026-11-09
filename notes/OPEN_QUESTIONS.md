# Epson XP-442 — open questions and unverified speculation

Early-stage speculation from before the firmware was reverse engineered. Most
has since been verified or disproven. This file retains only items that remain
**unverified and not yet covered** by the project's verified documentation.

For verified findings, see:
- [ARCHITECTURE.md](../ARCHITECTURE.md) — dual-domain SoC, EPIDC, D4
- [RTOS_KERNEL.md](../RTOS_KERNEL.md) — T-Kernel RTOS, exception vectors, boot sequence, IRQ dispatch
- [PANEL_PERIPHERALS.md](../PANEL_PERIPHERALS.md) — ILI9325 LCD (SPI), button ADC, drawing acceleration
- [CROM_FORMAT.md](../CROM_FORMAT.md) — CROM compression (Huffman + LZ77)
- [CROM_APPLICATION.md](../CROM_APPLICATION.md) — decompressed application code map, Modules A–D
- [PNRSC_CONFIG_REGISTRY.md](../PNRSC_CONFIG_REGISTRY.md) — PNRSC registry, NVRAM
- [TELNET_STATUS.md](TELNET_STATUS.md) — root shell approaches

---

## Unverified: RAM Size

Estimated 32–128 MB RAM. Exact figure unknown without opening the printer or
finding a boot log that reports it. The MMU page table at 0x10B000 maps
0x40000000–0x47FFFFFF as cached (128 MB range), but that's the virtual
address space, not necessarily physical RAM size.

## Unverified: Clock Speed

ARM1136JF-S rated 200–500 MHz; the XP-442's actual clock speed is unknown.
No PLL configuration or clock divider code has been identified yet. Would
need to find the clock tree setup in BooSt or the SoC mask ROM.

## Unverified: SD Card Access

The XP-442 has an SD card slot for PC-free printing. Which domain reads it
(RTOS or Linux) is unconfirmed. The RTOS has print-from-media strings, so
likely RTOS-side, but the actual SD controller address and driver code haven't
been traced.

## Unverified: Giantplus LCD Brand

The LCD was speculatively identified as Giantplus. The firmware confirms an
ILI9325-compatible controller (240x320 QVGA TFT, RGB565, SPI), but the
panel manufacturer hasn't been confirmed from firmware strings or PCB
markings.

## Unverified: RAM Reallocation Between CPUs

The idea of reallocating SDRAM between the RTOS and Linux domains by
modifying page tables or Linux bootargs remains untested. The MMU page table
is at 0x10B000 and the Linux domain's `mem=` parameter could theoretically
be changed, but:
- We don't know the actual RAM split
- We don't know if the bootloader builds page tables at runtime or uses
  hardcoded ones
- Shrinking either domain's allocation without understanding its actual usage
  could cause silent failures

## Unverified: Cross-Model Firmware Diffing

Comparing XP-352/355/445 firmware to isolate model-specific code hasn't been
done. These models share the SoC09S platform and likely have identical RTOS
kernels with different CROM application configurations.

## Speculation: Doom on XP-442

The hardware is theoretically capable (ARM11 >> 486, plenty of RAM, 240x320
display, physical buttons for input). The practical obstacles are all
software: porting the engine to T-Kernel, writing the LCD driver (now well
understood — ILI9325 window addressing), and mapping buttons (ADC resistor
ladder at 0xF0080000, results at 0xE081A000). The 55x40mm display would
make it nearly unplayable.

## Reference: Reverse Engineering Resources

### Firmware Tools

- [abrasive/epson-reversing](https://github.com/abrasive/epson-reversing) —
  RCX unpacking, CROM decompression (uncrom.py), IEEE 1284.4
- [BlackLotus/epson-stuff](https://github.com/BlackLotus/epson-stuff) —
  Wireshark ENPC dissector, ENPC Python library
- [effeffe/Epson-Firmware-Reverse-Engineering](https://github.com/effeffe/Epson-Firmware-Reverse-Engineering) —
  WF-2830 (Linux-based, different platform)
- [joxon/epson-firmwares](https://github.com/joxon/epson-firmwares) —
  Archive of old Epson firmware versions

### Hardware & Protocols

- [phooky/EpsonCP](https://github.com/phooky/EpsonCP) — Epson control panel
  RE (different model, FFC pin mapping)
- [ReInk](http://lion-simba.github.io/reink/index.html) — IEEE 1284.4,
  EEPROM read/write, ink/waste counter access
- [Ink Cartridge Protocol](https://www.instructables.com/Reverse-Engineering-to-Emulate-Ink-Cartridges-for-/) —
  Logic analyzer findings for cartridge chip serial protocol

### CPU Reference

- [ARM1136JF-S TRM](https://developer.arm.com/documentation/ddi0211/k/) —
  CP15, MMU, page tables, instruction set

## What Was Disproven

For the record, these early hypotheses turned out to be wrong:

| Claim | Reality |
|-------|---------|
| 74VHC165 shift register for buttons | Resistor ladder + on-chip ADC at 0xF0080000 |
| I2C device 0xA2 as key-scan controller | 0xA2 (0x51) is an RTC, not button input |
| 74VHC164/595 shift registers for display | Direct SPI to ILI9325 LCD controller |
| LCD commands 0x2A/0x2B/0x2C | ILI9325 uses regs 0x20/0x21/0x22, window 0x50–0x53 |
| Custom Epson RTOS | T-Kernel (TRON-family) with Epson extensions |
| No GPL obligations | T-License applies; GPL/Apache text found in CROM data |
| DMA bulk pixel transfer at 0xF0084800 | Power-on timing only, not pixel DMA |
