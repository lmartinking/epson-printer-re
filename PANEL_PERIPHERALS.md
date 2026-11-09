# XP-442 Control Panel Peripherals

Reverse-engineering notes for the front panel — the LCD display and physical
buttons on the XP-442. Both are driven entirely by the **RTOS domain**
(ARM1136JF-S); the Linux domain has no panel drivers or framebuffer.

## Display

### Hardware

| Property | Value |
|----------|-------|
| Type | 240×320 QVGA TFT color LCD (~2.4 inch) |
| Controller | ILI9325-compatible (register set matches ILI9325/ILI9328/SPFD5408) |
| Pixel format | RGB565 (16-bit, 5 red / 6 green / 5 blue) |
| Interface | SPI via SoC09S serial controller |
| SPI controller base | **0xF0084500** |
| SPI data port | **0xF0084810** |
| DMA control | **0xF0084800** |
| Timer (frame sync) | **0x4011C000** |

### SPI Controller Registers (base 0xF0084500)

| Offset | Width | Function |
|--------|-------|----------|
| +0x12 | 16-bit | Control/trigger — write 0x08 to commit a transfer |
| +0x14 | 16-bit | Transfer config — write 0xFFF7 to start |
| +0x300 | 16-bit | Status — bit 1 = SPI ready |
| +0x310 | 16-bit | TX data (16-bit write) |

Data port at **0xF0084810**: single-byte TX output (address byte of transfer).

### SPI Protocol

Each register write is a 3-byte SPI transfer, implemented by
`FUN_00005AF8` (hw_reg_write):

```
hw_reg_write(uint32_t param):
  1. Write 0xFFF7 to [base+0x14]             // start config
  2. Write param[23:8] to [base+0x310]        // 16-bit: reg addr + data high
  3. Wait until [base+0x300] & 2              // poll SPI ready
  4. Write param[7:0] to [0xF0084810]         // data low byte
  5. Wait until [base+0x300] & 2              // poll SPI ready
  6. Write 0x08 to [base+0x12]                // commit
```

The 24-bit param encodes: **bits[23:16] = register index**, **bits[15:0] = data**.

### LCD Controller Registers (ILI9325-compatible)

| Register | Init value | Function |
|----------|-----------|----------|
| 0x01 | 0x00EF (239) | Horizontal pixel count (240 columns) |
| 0x02 | 0x03BF (959) | Vertical size / waveform control |
| 0x03 | 0x027F | Entry mode (auto-increment, BGR, etc.) |
| 0x04 | 0x0007 | Resize control |
| 0x05 | 0x04F5 | Display control 2 |
| 0x06 | 0x0014 | Display control 3 |
| 0x07 | 0x0000→on | Display on/off (power sequence) |
| 0x08 | 0x00EF | Frame cycle control |
| 0x0B | 0x0202 | External display interface |
| 0x0C | 0x0001 | RGB interface control |
| 0x0D | 0x0001 | Frame marker position |
| 0x10 | 0x0000 | Power control 1 |
| 0x12 | 0x0031 | Power control 3 |
| 0x20 | 0x0FEF | Horizontal GRAM address (start position) |
| **0x22** | — | **GRAM data write** (pixel data entry) |
| 0x23 | — | GRAM data continuation |
| 0x30–0x36 | various | Gamma correction curve |
| 0x50 | 0x0008 | Horizontal window start |
| 0x58 | 0x0005 | Vertical window start |
| 0x65 | 0x0008 | Horizontal window end |
| 0xC0–0xCE | various | Panel interface control |
| 0xD4 | 0x0008 | Panel timing |

### Key Display Functions in rtos_core.bin

| Address | Name | Purpose |
|---------|------|---------|
| **0x05AF8** | hw_reg_write | Send one 24-bit SPI command to the LCD controller |
| **0x05B44** | lcd_write_pixel | Write one RGB565 pixel: sends reg 0x22 with pixel data, then reg 0x23 (continuation). **Note**: Ghidra merged this into a 6875-instruction mega-function — it's actually multiple small functions sharing a literal pool |
| **0x05B68** | lcd_flush_dma | Trigger DMA transfer from VRAM to LCD. Writes to [0xF0084800+0x06] and polls [0xF0084800+0x00] bit 0 |
| **0x05BAC** | lcd_init_full | Full LCD initialization — 40+ register writes setting up the ILI9325 controller |
| **0x05D98** | lcd_init_partial | Partial re-initialization (sleep exit / mode change) |

### How to Draw to the LCD

To write a pixel at position (x, y):

```
1. Set horizontal address:  hw_reg_write(0x200000 | x)      // reg 0x20 = H addr
2. Set vertical address:    hw_reg_write(0x210000 | y)      // reg 0x21 = V addr
3. Write pixel:             hw_reg_write(0x220000 | rgb565)  // reg 0x22 = GRAM write
```

To write a rectangular region efficiently:

```
1. Set window:  hw_reg_write(0x500000 | x_start)   // reg 0x50 = H window start
                hw_reg_write(0x510000 | x_end)     // reg 0x51 = H window end
                hw_reg_write(0x520000 | y_start)   // reg 0x52 = V window start
                hw_reg_write(0x530000 | y_end)     // reg 0x53 = V window end
2. Set start:   hw_reg_write(0x200000 | x_start)
                hw_reg_write(0x210000 | y_start)
3. Stream:      for each pixel: lcd_write_pixel(rgb565)
```

The controller auto-increments within the window — pixels flow left-to-right,
top-to-bottom (depending on entry mode register 0x03).

### SPI DMA Registers

The SPI controller has DMA-related registers:

- DMA control at **0xF0084800**: write 1 to [+0x06] to trigger, poll [+0x00] bit 0 for completion
- Buffer at 0xF0084820: staging area for DMA data

Note: `lcd_flush_dma` (0x5B68) is misleadingly named — it is part of
the display power-on timing sequence (writes timer value + power control
register), not a bulk pixel transfer. See Drawing Acceleration below.

## Panel UI Framework (block1_streamB_panel.bin)

The panel application is a **4.1 MB decompressed ARM binary** (from CROM
block 1, stream B) containing a full C++ UI framework. It runs in the
CROM address space (0x45xxxxxx).

### UI Class Hierarchy (from RTTI strings)

```
UIWidget (base class)
├── UIPartsButton          — physical button representation
├── UIMenuConfig           — menu configuration
│   ├── UIHomeMenuConfig
│   ├── UICopyMainMenuConfig
│   ├── UICopySettingMenuConfig
│   ├── UIScanTopMenuConfig
│   ├── UIScanSubMenuConfig
│   ├── UISetupItemMenuConfig
│   └── UIMoreFunctionMenuConfig
├── UISettingItemIF         — setting items
│   ├── UISettingItemParent
│   ├── UISettingItemProxy
│   └── UISettingItemEnumeration
├── UIBidListElem<UIWidget> — bidirectional list element
├── UIPartsImageSelectItem  — image selection widget
├── UIInputStringGetter     — text input
├── UIInputNetworkGetter    — network input
├── UIRegionSetting        — region/language
├── UILanguageSetting
├── UIPowerOffSetting
├── UIQuietModeSetting
└── (many more getters/settings)
```

### Panel Graphics (block1_streamC_gfx.bin)

The graphics binary (2.3 MB) contains ARM code plus **228 embedded PNG
images** (RGBA, 8-bit). These are sprite sheets for the UI:

| Dimensions | Count | Typical use |
|-----------|-------|-------------|
| 136×34 | 28 | Small UI buttons/icons |
| 200×44 | 27 | Medium menu items |
| 612×86 | 17 | Large sprite strips |
| 236×12 | 16 | Text/label sprites |
| 304×59 | 14 | Dialog elements |
| 48×12 | 11 | Small icons |
| 160×40 | 10 | Menu item icons |
| 640×120 | 1 | Full-width banner |
| 640×40 | 1 | Full-width header |
| 3132×4074 | 1 | Print check pattern |

The sprites are wider than the 240px display because they contain
multiple states side-by-side (normal/focused/pressed/disabled) that
get blitted individually at rendering time.

## Buttons

### Physical Buttons

The XP-442 front panel has these physical buttons (from diagnostic
strings at 0x45DA0220 in streamA):

| Button | Diagnostic name | Typical function |
|--------|----------------|-----------------|
| Power | (separate) | On/off/standby |
| Home | [Home] | Return to home screen |
| Up | [Up] | Navigate up |
| Down | [Down] | Navigate down |
| Left | [Left] | Navigate left |
| Right | [Right] | Navigate right |
| Back | [Back] | Return to previous screen |
| + | [+] | Increase / scroll up |
| - | [-] | Decrease / scroll down |
| Start | [Start] | Start copy/print |
| Stop | [Stop] | Cancel current operation |

### Button Input Hardware

Buttons use a **resistor ladder + on-chip ADC**, not I2C or shift
registers. Each button group pulls the ADC input to a different
voltage level through a unique resistor value; the ADC converts this
to a digital reading that identifies which button is pressed.

| Component | Address | Notes |
|-----------|---------|-------|
| ADC peripheral | **0xF0080000** | On-chip ADC, configured during boot |
| DMA result buffer | **0xE081A000** | ADC scan results written here by DMA |
| Power button | **0xF0084636** bit 0 | Separate GPIO input, not on ADC |

The ADC buffer at 0xE081A000 contains:

| Offset | Width | Content |
|--------|-------|---------|
| +0x00 | 16-bit | Channel A raw ADC value |
| +0x04 | 16-bit | Channel B raw ADC value |
| +0x20 | 16-bit | Status — bit 15 = scan complete |

Two ADC channels each serve a group of ~5 buttons. The raw 16-bit
values are right-shifted to get an 8-bit comparison value (>>8 in
the streamA debounce functions, >>6 in the kernel boot check).

### ADC Value Thresholds

The debounce functions in streamA (0x45ABB264, 0x45ABB2E0, 0x45ABB360)
compare the 8-bit ADC reading (after >>8) against these thresholds to
determine auto-repeat timing. Each range corresponds to a different
button on the resistor ladder:

**Channel B thresholds** (from 0x45ABB2E0):
```
Value >= 0xEE (238):  no button pressed
Value  < 0x2A  (42):  button 1 (lowest resistance)
0x2A – 0x39  (42–57):  button 2
0x3A – 0x4F  (58–79):  button 3
0x50 – 0x57  (80–87):  button 4
0x58 – 0x60  (88–96):  button 5
0x61 – 0xED  (97–237): (transition / invalid)
```

**Channel B alternate thresholds** (from 0x45ABB360):
```
Value >= 0xEE (238):  no button pressed
Value  < 0x2A  (42):  button 1
0x2A – 0x40  (42–64):  button 2
0x41 – 0x57  (65–87):  button 3
0x58 – 0x60  (88–96):  button 4
0x61 – 0x6A  (97–106): button 5
0x6B – 0xED (107–237): (transition / invalid)
```

The exact mapping of threshold ranges to physical button names (Home,
Up, Down, etc.) has not been determined from static analysis alone.
The "PNL Key Test" diagnostic function (reachable from service menu)
prints button names when pressed, which would allow empirical mapping
via UART console.

### Scan Value Processing

Button values flow through this pipeline:

```
ADC hardware (0xF0080000)
    │  DMA
    ▼
RAM buffer (0xE081A000)  ← raw 16-bit ADC readings
    │  read by streamA functions
    ▼
read_scan_A (0x45ABB6A0) / read_scan_B (0x45ABB6B0)
    │  halfword read + >>8 → 8-bit value
    ▼
Debounce (0x45ABB264 / 0x45ABB2E0 / 0x45ABB360)
    │  sample 20× at 10ms intervals, wait for stable value
    ▼
Value inversion:  255 - raw_value
    │
    ▼
State machine (0x45ABB6D0)
    │  processes two channel values, writes button event table
    ▼
T-Kernel message → Panel UI (block1_streamB_panel.bin)
```

### Power Button

The power button has its own dedicated GPIO input, separate from the
ADC-based panel buttons:

| Address | Bit | Function |
|---------|-----|----------|
| 0xF0084636 | bit 0 | Power button state (1 = pressed) |

Power button handler (FUN_000155C8 in rtos_core.bin):
- Polls 0xF0084636 bit 0 in a loop
- Debounces: requires 30 consecutive "pressed" readings
- On confirmed long-press: triggers system init (FUN_00012130)

### I2C Interface (NOT buttons)

The bit-banged I2C interface at GPIO 0xF000008F/0xF0000020 communicates
with device address 0xA2 (7-bit: 0x51), which is an **RTC (real-time
clock)**, not a key-scan controller. This was initially misidentified
as the button interface. The two kernel routines at `0x144F8`
(`i2c_sample_ack`) and `0x1457C` (`rtc_i2c_write_reg`, sends start,
`0xA2`, register, value, stop) were first named after that wrong theory
and have been renamed.

| Signal | Address | Bit | Function |
|--------|---------|-----|----------|
| SDA (data) | 0xF000008F | bit 0 | I2C data line |
| SCL (clock) | 0xF0000020 | bit 1 | I2C clock line |
| SDA direction | 0xF000008E | — | Data pin direction control |

### Key Scan Functions (streamA)

| Address | Name | Purpose |
|---------|------|---------|
| 0x45ABB6A0 | read_scan_A | Read ADC channel A value (>>8) |
| 0x45ABB6B0 | read_scan_B | Read ADC channel B value (>>8) |
| 0x45ABB264 | debounce_A | Debounce + threshold check for channel A |
| 0x45ABB2E0 | debounce_B1 | Debounce + threshold check for channel B |
| 0x45ABB360 | debounce_B2 | Debounce + alternate thresholds for channel B |
| 0x45ABB6D0 | btn_process | State machine: combine channels → button event |
| 0x45ABB884 | btn_value_A_entry | Invert + store channel A value |
| 0x45ABB894 | btn_value_B_entry | Invert + store channel B value |

Kernel boot button check (0x000113C4 in rtos_core.bin):
- Reads both ADC channels from 0xE081A000
- Shifts values >>6, compares against 0x9B (155)
- Used during boot to detect held buttons (service mode entry)

### GPIO Port Map (from table at 0x168F8)

The SoC09S has 17 GPIO ports at 0xF005xxxx:

| Port | Base address | Notes |
|------|-------------|-------|
| 0 | 0xF0050800 | |
| 1 | 0xF0050A00 | |
| 2 | 0xF0051000 | |
| 3 | 0xF0051200 | |
| 4 | 0xF0051400 | |
| 5 | 0xF0050600 | (double entry) |
| 6 | 0xF0050700 | (double entry) |
| 7 | 0xF0050100 | |
| 8 | 0xF0050200 | |
| 9 | 0xF0050000 | |
| 10 | 0xF0050400 | Used by boot init |
| 11 | 0xF0058000 | |
| 12 | 0xF0059000 | |
| 13 | 0xF0052000 | |
| 14 | 0xF0054000 | |
| 15 | 0xF0058100 | |
| 16 | 0xF0058200 | |

GPIO register offsets (per-port):

| Offset | Width | Function |
|--------|-------|----------|
| +0x10 | 16-bit | GPIO data/direction |
| +0x12 | 16-bit | GPIO data/direction |
| +0x14 | 16-bit | GPIO data/direction |
| +0x16 | 16-bit | GPIO data/direction |
| +0x18 | 16-bit | GPIO data/direction |
| +0x24 | 16-bit | GPIO config |
| +0x26 | 16-bit | GPIO config |
| +0x2C | 16-bit | GPIO control (used to write 0x20 during boot) |
| +0xAE | 16-bit | Serial address high |
| +0xB0 | 16-bit | Serial address low |
| +0xB2 | 16-bit | Serial control (clear bit 1 to start) |
| +0xBA | 16-bit | Serial status (poll != 0 for completion) |

### Service/Diagnostic Menu Buttons

The RTOS contains a diagnostic mode menu (strings at 0x7A6AA):

```
Mode: Skip Scan SFK_1
Yes:UP/No:DOWN
4. ContinuanCW
5Retry:  OK SW
Finish/Power/Next/Start/Run:[]
2Double Fa/PapFeed
1Sg3CN EndurFT
ADF* NoMechSup/Auto Chk/no All
[OK]But/[STOP]/Up/Down/Parameter/Select/DevelMenu
```

This reveals the button naming convention used internally:
- **OK** / **STOP** / **Up** / **Down** are physical buttons
- **Parameter** and **Select** are navigation modes
- **DevelMenu** is the developer/service menu entry point

## Linux Domain

The Linux rootfs has **no panel-related kernel modules or drivers**. The
kernel modules are exclusively networking (WiFi, Ethernet) and EPIDC:

```
bcmdhd.ko         — Broadcom WiFi
epidc.ko           — cross-domain shared memory
lsimodem.ko        — modem/fax
lsiserial.ko       — serial
```

There is no `/dev/fb*`, no display framebuffer, no button input device,
and no GPIO driver. The panel is entirely RTOS-domain hardware.

## SoC09S Peripheral Address Map (Panel-Relevant)

```
0xE000_1000  ┌─────────────────────┐
             │ Interrupt Controller │
0xE080_0000  ├─────────────────────┤
             │ System RAM           │
             │  0xE081A000: ADC DMA │ (button scan result buffer)
0xF000_0000  ├─────────────────────┤
             │ System Control       │ (57 refs in RTOS binary)
             │  +0x02: GPIO input   │ (status signals, bit 4 + bit 2)
             │  +0x04: GPIO input   │ (status signals, bit 3)
             │  +0x20: I2C SCL      │ (bit-banged, bit 1; RTC only)
             │  +0x21: GPIO ctrl    │ (bits 1,3,5,8 — misc I/O)
             │  +0x34: GPIO output  │ (bit 0 — LED/status?)
             │  +0x35: GPIO output  │ (bit 0 — LED/status?)
             │  +0x44: GPIO output  │ (bit 0)
             │  +0x48: GPIO output  │ (bit 0)
             │  +0x8E: I2C SDA dir  │
             │  +0x8F: I2C SDA      │ (bit-banged, bit 0; RTC only)
0xF005_xxxx  ├─────────────────────┤
             │ GPIO Ports (17 ports)│
             │  0x0000–0x9200       │
0xF008_xxxx  ├─────────────────────┤
             │ ADC / SPI / Periph   │
             │  +0x0000: ADC        │ (button resistor ladder)
             │  +0x4500: LCD SPI    │ controller base
             │  +0x4636: Power btn  │ (GPIO input, bit 0)
             │  +0x4744: Control    │
             │  +0x4750: Alt ctrl   │
             │  +0x4800: DMA ctrl   │
             │  +0x4810: LCD data   │ port (byte TX)
             │  +0x4820: DMA buffer │
             │  +0x5000: 2nd SPI ch │
             │  +0x5100: 2nd status │
0xF00A–0xF0F ├─────────────────────┤
             │ Assorted Peripherals │ (print engine, scanner,
             │                      │  motor control, etc.)
             └─────────────────────┘
```

## Concrete Next Steps

### Reading Button Presses

1. **Map button-to-ADC-value empirically** — With the UART root shell,
   read the ADC DMA buffer at 0xE081A000 while pressing each button.
   This gives the exact ADC reading for each button on each channel.
   Example (via devmem or custom tool):
   ```
   # Read channel A (halfword at 0xE081A000)
   devmem2 0xE081A000 h
   # Read channel B (halfword at 0xE081A004)
   devmem2 0xE081A004 h
   # Read scan complete flag (halfword at 0xE081A020)
   devmem2 0xE081A020 h
   ```

2. **Identify which buttons are on which channel** — Press each button
   and note which channel's value changes. Likely split: navigation
   buttons (Up/Down/Left/Right/Home/Back) on one channel, action
   buttons (Start/Stop/+/-) on the other.

3. **Trace the "PNL Key Test" diagnostic** — The service menu has a
   "PNL Key Test" mode (strings at 0x45DA022D) that prints button
   names when pressed. Entering service mode and running this test
   would confirm the complete button mapping.

4. **For programmatic button reading** — Read the ADC buffer directly,
   apply the debounce algorithm (sample 20× at 10ms intervals), then
   compare against the threshold table to identify the button. See
   the scan value processing pipeline above.

### Writing to the LCD

1. **Implement hw_reg_write** — The SPI protocol is fully decoded above.
   To write from the Linux side (if you add a kernel module or a
   `/dev/mem` user-space accessor):
   ```c
   #define SPI_BASE    0xF0084500
   #define SPI_DATA    0xF0084810
   
   void hw_reg_write(uint32_t param) {
       iowrite16(0xFFF7, SPI_BASE + 0x14);
       iowrite16((param >> 8) & 0xFFFF, SPI_BASE + 0x310);
       while (!(ioread16(SPI_BASE + 0x300) & 2));
       iowrite8(param & 0xFF, SPI_DATA);
       while (!(ioread16(SPI_BASE + 0x300) & 2));
       iowrite16(0x08, SPI_BASE + 0x12);
   }
   ```

2. **Set up the display window and stream pixels** — use the ILI9325
   register writes documented above.

3. **Beware of bus contention** — The RTOS owns the SPI bus and writes
   to the LCD continuously (UI refresh). Writing from Linux requires
   either:
   - Halting the RTOS panel refresh (risky — may watchdog)
   - Adding a custom display command via EPIDC that the RTOS services
   - Patching the RTOS firmware to delegate display control

4. **Window-addressed bulk writes** for efficiency — see Drawing
   Acceleration section below.

### Alternative: Patching the Panel App

Since the panel app runs from CROM-decompressed RAM, it can be modified:

1. **Decompress** block1_streamB_panel.bin with `uncrom.py`
2. **Patch** the C++ UI framework to add custom screens
3. **Recompress** and repack the firmware
4. **Flash** via the unsigned update chain

This is the most sustainable approach for custom panel content but
requires full CROM decompression and understanding the UI framework's
rendering pipeline.

## Drawing Acceleration

### ILI9325 Window Addressing (primary acceleration)

The ILI9325 controller's window address registers allow rectangular region
fills without per-pixel address setup. This is the main drawing optimization:

```
1. Set draw window:
     hw_reg_write(0x500000 | x_start)   // reg 0x50 = H window start
     hw_reg_write(0x510000 | x_end)     // reg 0x51 = H window end
     hw_reg_write(0x520000 | y_start)   // reg 0x52 = V window start
     hw_reg_write(0x530000 | y_end)     // reg 0x53 = V window end
2. Set cursor to window origin:
     hw_reg_write(0x200000 | x_start)   // reg 0x20 = H cursor
     hw_reg_write(0x210000 | y_start)   // reg 0x21 = V cursor
3. Stream pixels (auto-increment fills the window):
     for each pixel in region:
         lcd_write_pixel(rgb565)         // reg 0x22 = GRAM write
```

The auto-increment direction is set by register 0x03 (Entry Mode):

| Reg 0x03 value | Init value 0x027F | Meaning |
|----------------|-------------------|---------|
| ID[1:0] (bits 5-4) | 11 | Both H and V auto-increment |
| AM (bit 3) | 1 | Vertical-first scan (column then row) |
| BGR (bit 9) | 1 | BGR color order |

This means after each pixel write to reg 0x22, the address auto-increments
vertically. When it hits the window boundary (reg 0x53), it wraps to the
next column and resets to the top of the window (reg 0x52).

### SPI Controller DMA (0xF0084800) — limited

The SPI controller has a small DMA capability:

| Offset from 0xF0084800 | Width | Function |
|------------------------|-------|----------|
| +0x00 | 16-bit | Status (bit 0 = completion) |
| +0x06 | 16-bit | Trigger (write 1 to start) |
| +0x20 | 32-bit | Staging buffer |

**This is NOT bulk pixel DMA.** Analysis of `lcd_flush_dma` (0x5B68) shows
it writes a single 32-bit word from the timer counter (0x4011C000) to the
staging buffer, sends a power control register write (reg 0x31 = 0x0001),
then triggers the DMA. It has only **1 caller** in the entire kernel stub
(at 0x5D80, during display initialization). This is part of the display
power-on timing sequence, not a pixel transfer mechanism.

### System DMA Controller (0x42100000) — potentially capable

A DMA controller at 0x42100000 is registered as a peripheral during boot
(at 0x118D8, passed to the peripheral registration function 0x159D0 along
with the display controllers at 0x41D00000, 0x41E00000, 0x41F00000). No
channel configuration code exists in the kernel stub — the DMA setup is
in the CROM-decompressed RTOS application (0x45xxxxxx). This is potentially
a multi-channel AMBA-style DMA engine capable of memory-to-SPI transfers,
but confirming this requires decompressing the CROM section.

### SPI Controller Extended Registers

Beyond the basic registers documented above, the SPI controller has
additional registers that may support burst or FIFO modes:

| Address | Refs | Purpose (inferred) |
|---------|------|--------------------|
| 0xF0084618 | 2 | Extended config |
| 0xF0084636 | 3 | **Power button GPIO input** (bit 0 = pressed) |
| 0xF0084744 | 3 | Transfer control |
| 0xF0084750 | 2 | Burst/FIFO control |
| 0xF0084754 | 2 | Additional control |

Note: 0xF0084636 is NOT an SPI status register — it is the power
button's dedicated GPIO input pin. See the Buttons section above.

### What Does Not Exist (in the kernel stub)

- **No 2D hardware blitter** — the ILI9325 has no graphics engine
- **No framebuffer DMA** — no code sets up a memory buffer → SPI bulk
  transfer using the system DMA controller
- **No rectangle fill primitive** — no function that combines window
  setup + solid color fill in a single call
- **No bitmap blit primitive** — no function that windows + streams
  from a source buffer
- **No PNG decoder** — the 228 PNGs in block1_streamC_gfx.bin are
  decoded by the CROM application, not the kernel

### Where the Real Drawing Happens

The drawing pipeline is layered:

```
Panel App (block1_streamB, UIWidget C++ framework)
    │
    ▼  calls through RTOS API thunk table
CROM Application (0x45xxxxxx in RAM)
    │  ┌─ draw_rect: window setup + color fill
    │  ├─ blit_bitmap: window setup + pixel stream
    │  ├─ draw_text: glyph lookup + blit
    │  └─ possibly: DMA bulk transfer via 0x42100000
    ▼
Kernel SPI Primitives (hw_reg_write, lcd_write_pixel)
    │
    ▼
ILI9325 LCD Controller (SPI bus)
```

The panel app (block1_streamB_panel.bin) has only **1 reference** to the
SPI peripheral space (0xF0084400). It operates entirely through the RTOS
API at 0x45xxxxxx — ~70,000 references to that address range.

The kernel stub has a simple character renderer at **0x5990** that maps
ASCII (0-9, A-Z, a-z, punctuation) to font glyph indices and renders
them pixel-by-pixel. This is used for boot/diagnostic screens before
the CROM application is decompressed.

### Performance Estimate

| Method | Time per pixel | Full frame (240×320) |
|--------|---------------|---------------------|
| Per-pixel address + write | ~3 SPI txns (~4 μs) | ~310 ms |
| Window-addressed stream | ~1 SPI txn (~1.5 μs) | ~115 ms |
| DMA burst (if available) | SPI clock rate | ~15-30 ms |

At ~8-9 fps with window-addressed writes, the UI likely uses **dirty
rectangle** updates — only redrawing the changed regions of the screen.
A button highlight or menu scroll only touches a small portion of the
240×320 display, making sub-100ms updates feasible.

## References

- [RTOS_KERNEL.md](RTOS_KERNEL.md) — RTOS kernel internals, IRQ dispatch, boot sequence
- [ARCHITECTURE.md](ARCHITECTURE.md) — Dual-domain SoC architecture
- [TELNET_STATUS.md](notes/TELNET_STATUS.md) — UART root shell for empirical testing
- ILI9325 datasheet — register map for the LCD controller
