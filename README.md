# E4916A / E4915A Firmware Loader

A menu-driven Python tool for the **Agilent/HP E4916A** and **E4915A Crystal
Impedance Meters**. It uploads manufacturer firmware to the instrument's resident
`DOWNLOAD-CALGAMO` bootloader over GPIB, and enables or disables licensed
instrument options — for maintaining your own long-discontinued hardware.

The GPIB protocol, bootloader behaviour, download-mode entry, and option record
were reverse-engineered from the boot EPROM and the main firmware image; see
[`E4916A_debug_findings.md`](E4916A_debug_findings.md) for the full write-up.

> [!WARNING]
> **Unofficial tool — use at your own risk on equipment you own.** Reprogramming
> firmware and editing the option record are inherently risky operations.
> Firmware upload is *recoverable by design* (see [Recoverability](#recoverability-why-this-is-safe)),
> but read the safety notes before you start.

---

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Quick start](#quick-start)
- [The menu](#the-menu)
  - [Startup: auto-connect](#startup-auto-connect)
  - [1 — Scan for instrument](#1--scan-for-instrument)
  - [2 — Upload firmware](#2--upload-firmware)
  - [3 — Enable / disable options](#3--enable--disable-options)
  - [4 — Show procedure / help](#4--show-procedure--help)
- [Entering download mode (the "5" key)](#entering-download-mode-the-5-key)
- [GPIB address in download mode](#gpib-address-in-download-mode)
- [Instrument options reference](#instrument-options-reference)
- [Recoverability (why this is safe)](#recoverability-why-this-is-safe)
- [Troubleshooting](#troubleshooting)
- [Files](#files)
- [Disclaimer](#disclaimer)

---

## Features

- **Firmware upload** over GPIB to the `DOWNLOAD-CALGAMO` bootloader, with a live
  progress bar.
- **Automatic CRC-32 verification** of the image before anything is sent.
- **Option enable/disable** for the LCR (`010`) and EM (`S01`) options, reading
  the current record first and preserving the model, serial, and untouched
  fields.
- **Auto-discovery** of the instrument on the GPIB bus at startup.
- **Recoverable by design** — the bootloader lives in a separate EPROM the tool
  never touches, so a failed upload cannot brick the unit.

## How it works

The E4916A/E4915A contains a small **boot EPROM** (separate from the main program
flash). Held-key-at-power-on selects one of two paths:

- **Normal mode** — runs the main firmware; answers SCPI (`*IDN?`, options, etc.).
- **Download mode** — a raw byte-stream loader that receives a firmware image over
  GPIB and programs it to flash.

This tool talks to the instrument in **both** modes: SCPI in normal mode (scanning
and options), and the raw download protocol in download mode (firmware upload).

## Requirements

| Requirement | Notes |
| --- | --- |
| **Python 3.7+** | No third-party packages needed for the menu itself. |
| **PyVISA** | `pip install pyvisa` — used for all GPIB I/O. |
| **A VISA backend** | NI-VISA, Keysight IO Libraries Suite, **or** `pip install pyvisa-py`. |
| **A VISA-compatible GPIB adapter** | e.g. Keysight 82357B, NI GPIB-USB-HS. |
| **The firmware image** | The manufacturer file `fw-0213.m` (2.13), placed next to the script. |

> [!NOTE]
> The script runs and shows its menu without PyVISA installed, but any action that
> talks to the instrument (scan, upload, options) needs PyVISA **and** a working
> VISA backend with your GPIB adapter.

## Setup

1. Install Python 3 and PyVISA plus a backend:

   ```bash
   pip install pyvisa
   # If you don't have NI-VISA / Keysight IO Libraries, add the pure-Python backend:
   pip install pyvisa-py
   ```

2. Confirm your GPIB adapter is installed and visible to VISA.

3. Put the firmware image **`fw-0213.m`** in the **same folder** as
   `E491xA_firmware_updater.py` (this is the default the upload option offers).

4. Run it from a real terminal (Command Prompt / PowerShell / a shell), **not**
   from IDLE — the in-place progress bar relies on a proper terminal:

   ```bash
   python E491xA_firmware_Updater.py
   ```

## Quick start

```text
============================================================
  E4916A / E4915A firmware download tool
============================================================
Scanning the GPIB bus for the instrument...
  Connected: GPIB0::17::INSTR   [HEWLETT-PACKARD,E4916A,JP1KD00123,A.02.13]

Menu:
  1. Scan for instrument on GPIB bus
  2. Upload firmware (fw-0213.m)
  3. Enable/disable instrument options (010 LCR / S01 EM)
  4. Show the update procedure / help
  0. Exit

Select:
```

To update firmware: choose **2**, follow the prompts to put the unit in download
mode, and type `WRITE` to send. To change options: choose **3**, toggle, and type
`WRITE`.

## The menu

### Startup: auto-connect

On launch the tool scans the GPIB bus (normal mode) for an E4916A/E4915A.

- **Found** → it prints the resource and IDN and remembers it for the session.
- **Not found** → it prompts you to connect the instrument and press **Enter** to
  retry, or type **`s`** to skip and continue offline.

The discovered resource is reused for normal-mode actions. For firmware upload it
automatically switches to **GPIB primary address 17** on the same bus (the
bootloader always listens there — see [below](#gpib-address-in-download-mode)).

### 1 — Scan for instrument

Rescans the bus and remembers the first E4916A/E4915A it finds. Useful to confirm
which bus/address your adapter presents.

> [!NOTE]
> Scanning uses `*IDN?`, which only works in **normal** firmware. An instrument
> already in **download** mode will not be found (and must not be probed while a
> transfer is expected).

### 2 — Upload firmware

Guided, safe firmware upload. Typical session:

```text
--- Upload firmware (fw-0213.m) ---

Press Enter to accept the default firmware file, or type a path.
Firmware image file [/path/to/fw-0213.m]:

Verifying image: DOWNLOAD-CALGAMO-REV01.00, 483790 bytes
  stored CRC 0x7C7EDAF9  ->  CRC check: PASSED

Now put the instrument in DOWNLOAD mode:
  power OFF -> hold '5' -> power ON -> release when the display is active.

Is the instrument showing 'BOOTROM REV:xx.xx'? [y/N]: y

GPIB resource (download mode is always address 17): GPIB0::17::INSTR
About to send 483790 bytes to GPIB0::17::INSTR.
This will ERASE and reprogram the main firmware flash with the new firmware.

Type EXACTLY "WRITE" to proceed (anything else cancels): WRITE

Watch the instrument: it should ERASE, then display:
'WRITING TO FLASH... sector N', then:
'COMPLETE' 'Power Off->On to RUN!'.
On error it prints the reason and
stays in the bootloader so you can retry.

Sending 483827 bytes...
  [##############################] 100%   483827/483827 bytes
Image sent. Reboot instrument
```

Step by step:

1. **Choose the image** — press Enter for the default `fw-0213.m`, or type a path.
2. **CRC check** runs automatically; the upload aborts if it fails.
3. **Enter download mode** on the instrument (power OFF → hold **"5"** → power ON).
   Confirm it is showing the `BOOTROM REV:xx.xx` banner.
4. **Confirm** the destructive write by typing `WRITE`.
5. The image streams with a **progress bar**. Watch the instrument for
   `ERASING` → `WRITING TO FLASH... sector N` → `COMPLETE` / `Power Off->On to RUN!`.
6. **Power-cycle** the instrument (without holding "5") to run the new firmware.

> [!IMPORTANT]
> If the upload fails or is interrupted, the instrument **stays in the
> bootloader**. Just re-enter download mode and run the upload again.

### 3 — Enable / disable options

Reads the instrument's identity/option record, lets you toggle options, and writes
it back. The model, serial number, and any groups you don't touch are always
preserved.

```text
--- Enable / disable instrument options ---

Using connection: GPIB0::17::INSTR
IDN: HEWLETT-PACKARD,E4916A,JP1KD00123,A.02.13
Current record: "E4916A","JP1KD00123","000","000","000","000"

Options:
  1. LCR Meter (010)        disabled
  2. EM / Evap Mon (S01)    disabled
  w. write & apply     q. cancel

Action: 1

Options:
  1. LCR Meter (010)        ENABLED
  2. EM / Evap Mon (S01)    disabled
  w. write & apply     q. cancel

Action: w

  before: "E4916A","JP1KD00123","000","000","000","000"
  after : "E4916A","JP1KD00123","010","000","000","000"

About to write (updates the config-flash option record):
  :TEST:INSTR:INFO:DATA "E4916A","JP1KD00123","010","000","000","000"
Type EXACTLY "WRITE" to proceed (anything else cancels): WRITE

Record now : "E4916A","JP1KD00123","010","000","000","000"
*OPT? now  : 010
Error queue: +0,"No error"

Power-cycle the instrument for the change to take effect.
```

- Enter an option **number** (`1`, `2`) to toggle it on/off.
- **`w`** writes the change (after a `WRITE` confirmation and a before/after diff).
- **`q`** cancels without writing.
- After writing, the tool reads the record back and checks `*OPT?` and the error
  queue. **Power-cycle** for the change to take effect.

> [!NOTE]
> `010` (LCR) applies to the **E4916A only**; on an E4915A the tool flags it and
> the instrument may reject it. Full LCR operation also needs the Option 001
> impedance-probe hardware.

### 4 — Show procedure / help

Prints the firmware-update and option procedures for quick reference.

## Entering download mode (the "5" key)

Download mode is a **physical power-on condition** — there is no software command
to enter it.

1. Power the instrument **OFF**.
2. **Hold** the front-panel **"5"** key.
3. Power **ON** while holding "5".
4. Release when the boot banner appears (e.g. `BOOTROM REV:1.01`). It then waits
   for the image over GPIB.

To leave download mode, simply power-cycle **without** holding "5".

> [!TIP]
> This is non-destructive to test — if "5"-at-power-on doesn't show the boot
> banner, nothing happens and the unit boots normally.

## GPIB address in download mode

In download mode the bootloader **hard-codes GPIB primary address 17**, regardless
of the instrument's normal-operation address. The tool handles this automatically:
it keeps the bus number it discovered and targets address 17, e.g.
`GPIB0::17::INSTR`. There is no hardware address switch on this instrument.

## Instrument options reference

The option record is six comma-separated, individually quoted fields:
`"MODEL","SERIAL","g1","g2","g3","g4"`.

| Code | Group | Option | Notes |
| --- | --- | --- | --- |
| `010` | g1 | **LCR Meter Function** | E4916A only; full LCR also needs the Opt 001 impedance-probe hardware. |
| `S01` | g4 | **EM / Evaporation Monitor Mode** | Thin-film deposition monitoring. |
| `P01` | g3 | Power Range | **Not supported by this tool — do not enable.** |

> [!WARNING]
> **Do not enable `P01` (Power Range).** On these units it causes
> `E16: Prev. setting lost` on every reboot because it needs power-range hardware
> / calibration data that isn't present. The tool deliberately does not offer or
> touch this group.

## Recoverability (why this is safe)

The bootloader lives in a **separate EPROM** that the firmware-upload path never
writes to. If a download fails or is interrupted, the loader reports the error and
**stays resident** — re-enter download mode and retry. A firmware upload cannot
brick the unit.

> [!CAUTION]
> This safety net applies to the **firmware download path only**. The option/cal
> sector (written by the options menu) and the boot EPROM are **not** recoverable
> if damaged. The options menu mitigates this with a read-modify-write that
> preserves surrounding calibration data, but only change options deliberately.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `GPIB scan unavailable: No module named 'pyvisa'` | Install PyVISA (`pip install pyvisa`) and a VISA backend. |
| Scan finds nothing in normal mode | Check the GPIB cable/adapter, that the unit is powered and in normal mode, and that your adapter is visible to VISA. |
| Progress bar prints on many lines / doesn't update in place | You're running in IDLE or another console that doesn't honour `\r`. Run from a real terminal (Command Prompt / PowerShell / shell). |
| Upload stalls or times out | Confirm the unit is actually at the `BOOTROM REV` banner before sending. If a slow adapter stalls mid-stream, the transfer can be retried; the loader stays resident on failure. |
| Instrument shows `Download Failed!` / `CHECKSUM ERROR` | The transfer didn't complete cleanly. Re-enter download mode and upload again. |
| Enabling `010` is rejected | `010` is E4916A-only; an E4915A will refuse it (check the `Error queue` line). |
| Option change didn't take effect | **Power-cycle** the instrument after writing options. |

## Files

| File | Purpose |
| --- | --- |
| `E491xA_firmware_Updater.py` | The tool (run this). |
| `E4916A_debug_findings.md` | Reverse-engineering findings: protocol, bootloader, options. |
| `fw-0213.m` | Manufacturer firmware image (2.13). Place it next to the script. |

## Disclaimer

This is an **unofficial**, community-built maintenance tool for hardware you own.
It is not affiliated with or endorsed by Keysight or Agilent. Firmware images are
the property of their respective owners and are **not** included here. Use it at
your own risk; the authors accept no liability for damage to equipment.
