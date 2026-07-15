# E4916A / E4915A — Reverse-Engineering Findings

Verified findings from disassembling the boot EPROM and the main firmware image
`fw-0213.m` with a custom M68000 disassembler. Unless noted, all addresses are on
the firmware load map (`file_off + 0xDFFFDB`; file `0x25` → `0xE00000`).

## Table of contents

- [Memory map](#memory-map)
- [`:DEBUG` subsystem](#debug-subsystem)
- [Bootloader — DOWNLOAD-CALGAMO protocol](#bootloader--download-calgamo-protocol)
- [Download-mode entry key = "5"](#download-mode-entry-key--5)
- [Download-mode GPIB address = 17](#download-mode-gpib-address--17-fixed-by-the-bootloader)
- [Instrument options](#instrument-options-identityoption-record)
- [Status of goals](#status-of-goals)

## Memory map

| Region | Address | Notes |
| --- | --- | --- |
| Program firmware | `0xE00000`–`~0xE76000` | executing code; cannot self-reprogram |
| Config / cal / identity flash | `0x00F00000` | model, serial, options, cal data |
| Measurement SRAM buffer | `0x00FD0000`–`0x00FDF000` | 8 bytes (1 double) per entry |
| Flash sector scratch (RAM) | `0x00FE0000` | used by the flash RMW core |
| Flash descriptor (RAM) | `0x00FFC2BE` | state byte, geometry, erase/pgm fn ptrs |

## `:DEBUG` subsystem

### Command handlers

| Command | Handler | Behaviour |
| --- | --- | --- |
| `:DEBUG:WRITESTR <dest>,<data>` | `0xE3BF34` | **arbitrary-address flash write** → `flash_memcpy(dest, data, len)` |
| `:DEBUG:INFO <model>,<serial>,<opts…>` | `0xE3B6E6` | validates + writes identity/option/cal record to `0xF00000` |
| `:DEBUG:MEMREAD <index>` | `0xE407F2` | reads an 8-byte double from SRAM buffer `0xFD0000 + index*8` (buffer only) |
| `:DEBUG:MEMWRITE` | `0xE40672` | fills SRAM buffer with computed doubles (not a general poke) |
| `:DEBUG:EXEC <0\|1>` | `0xE3BF04` | **not** execute-at-address — a bounded 0/1 toggle dispatch |
| `:DEBUG:SRAM <0\|1>` | `0xE3BF04`-style | bounded 0/1 toggle |

### Flash programming primitive

- `flash_memcpy(dest, src, len)` @ `0xE0819C` → core @ `0xE08062`.
- The core does a **sector-granular read-modify-write**: it copies the target
  sector to RAM `0xFE0000`, overlays the new bytes, then erases and programs via
  the function pointers in the descriptor @ `0xFFC2BE`.
  - No manual pre-erase is needed; the rest of the sector is preserved.
  - It cannot target the sector currently executing (that would erase running code).
- Chip = AMD/Fujitsu command set (Am29F0x0 / MBM29F0x0 class, x16):
  - unlock/reset @ `0xE0827E` (`AAAA→+AAAA`, `5555→+5554`, `F0F0→+AAAA`)
  - `DQ7`/`DQ5` data-polling @ `0xE081FE` (masks `0x8080` / `0x2020`)

### Consequences for the read/write goals

- **Write to the config/cal/option sector (`0xF00000`)** — achievable now via
  `:DEBUG:WRITESTR` (raw) or `:DEBUG:INFO` (validated).
- **Program-firmware (`0xE00000`) read *and* write** — the runtime `:DEBUG`
  commands cannot do it: there is no arbitrary-address reader, and the running
  image cannot reprogram its own sector. This must go through the resident
  **DOWNLOAD-CALGAMO bootloader** (below).

> [!WARNING]
> Writing the `0xF00000` option/cal sector risks corrupting calibration data.
> The tool mitigates this with a sector read-modify-write that preserves
> surrounding data, but treat any write here with care.

## Bootloader — DOWNLOAD-CALGAMO protocol

The boot EPROM is a valid 68000 boot ROM mapped at `0x000000` (reset
`SSP = 0x00FD0000`, `PC = 0x2FC4`), **BOOTROM REV 01.00, © 1996 HP**. All of the
following is verified by disassembly and cross-checked against `fw-0213.m`.

### Entry (download mode)

At power-on the boot ROM reads hardware register **`0x1E9801`**; if it equals
**`0x0A`** it enters download mode, otherwise it jumps to the main firmware at
`0xE00400`. `0x1E9801` is read-only to software, so download mode is a **physical
power-on condition** (a front-panel key held during power-up — the standard HP
service method). There is no software "reboot into download".

### Wire format (GPIB, instrument = listener)

| Bytes | Field |
| --- | --- |
| 25 | ASCII header `DOWNLOAD-CALGAMO-REV01.00` (`strncmp`, 25 chars) |
| 4 | payload size, big-endian (≤ flash capacity) |
| 4 | payload CRC, big-endian |
| 4 | reserved — read and **discarded** by the loader; `0` in official images |
| N | raw image, programmed to flash `0xE00000` |

The prefix is **37 bytes** (four HPIB reads before erase: 25 + 4 + 4 + 4, the
last discarded). The official `ci-212.m` (2.12) and `fw-0213.m` (2.13) are both
exactly this format, so they are sent **as-is** — no reformatting needed.

### Flow

```text
header check → size check
  → ERASING FLASH...                (erase 0xE00000)
  → WRITING TO FLASH... sector %d   (0x8000-byte sectors staged via RAM 0xFE0000)
  → recompute CRC over written flash, compare
  → COMPLETE + "Power Off->On to RUN!"
```

Any failure prints an error message and `Download Failed!`, and the loader stays
resident.

### Checksum (verified)

Standard reflected **CRC-32**, polynomial **`0xEDB88320`**, init **`0`**, **no
final XOR**, computed over the payload. The EPROM's 256-entry table at `0x6002`
is the exact standard CRC-32 table.

| Image | Payload CRC | Matches stored field |
| --- | --- | --- |
| `fw-0213.m` (2.13) | `0x7C7EDAF9` | ✓ |
| `ci-212.m` (2.12) | `0x230AB8D8` | ✓ |

### Recoverability

> [!IMPORTANT]
> The bootloader lives in a **separate EPROM** that a download never touches. A
> bad or interrupted download **cannot brick the unit** — it reports the error,
> stays in the bootloader, and you re-enter download mode and retry.

> [!WARNING]
> The `0xF00000` option/cal sector and the boot EPROM itself are **not**
> recoverable — do not target those.

### BOOTROM REV 1.01 (E4915-85011)

The 1.01 boot EPROM (from the bench unit) was compared against 01.00:

- Reset vectors identical. The download routine is the same code relocated
  ~`0x400` higher and behaves identically.
- Same four HPIB reads before erase (25 + 4 + 4 + 4) = the same 37-byte prefix;
  header still `DOWNLOAD-CALGAMO-REV01.00`.
- Standard reflected CRC-32 table present; recomputing with the 1.01 ROM's own
  table gives `0x7C7EDAF9` for 2.13 and `0x230AB8D8` for 2.12 — both matching
  their stored checksums.
- Same entry check: `move.b (0x1E9801),D1 ; cmpi.b #0x0A,D1` (hold "5").

→ `fw-0213.m` (2.13) is accepted by 1.01 units **as-is**.

### How the transfer starts

After printing `BOOTROM REV:1.01` the loader polls a wait function that reads the
GPIB controller (base pointer at `0x63DE` = **`0x1E9C01`**; status reg `base+0`,
data reg `base+0x0E` = `0x1E9C0F`) and tests **status bit `0x20` (byte
received)**. It loops until the host sends the first byte, then runs the download
routine (which prints `DOWNLOAD START... (HPIB)` and reads the 37-byte prefix +
payload).

> [!NOTE]
> Address the unit as a GPIB listener and stream the whole image continuously.
> The read primitive has a per-byte timeout, so avoid per-byte delays. The tool
> streams in 16 KB chunks — the gap between chunks is ≪ 1 ms, well inside the
> timeout, and EOI is asserted only on the final chunk.

## Download-mode entry key = "5"

The boot ROM enters download mode when register **`0x1E9801`** reads **`0x0A`** at
power-on (`move.b (0x1E9801),D1 ; cmpi.b #0x0A,D1`), read as the very first thing
after reset — before peripheral init — so it reflects a physically-held key.

`0x1E9801` is the **keyboard scan-code register**. The main-firmware keyboard ISR
(`0xE00AA2`) reads the scan code from `0x1E9801` and enqueues it into the keyboard
buffer (handle `0xFFC242`) that the normal key-read path (`0xE08C04`) and the
"Key Test" diagnostic dequeue. So the boot ROM's `0x0A` is a keyboard scan code in
the firmware's own encoding.

The firmware's key-code table (`0xE6F878`, used by the Key Test) maps:

```text
scan 0x09 = Num6   0x0A = Num5   0x0B = Num4
     0x11 = Num3   0x12 = Num2   0x13 = Num1
     0x01 = Num9   0x02 = Num8   0x03 = Num7
     0x1B = Num0   0x18 = Enter  ...
```

→ **`0x0A` = the "5" numeric key.**

> [!IMPORTANT]
> Hold the front-panel **"5"** key while switching the instrument **ON** to enter
> download mode. The display then shows the BOOTROM banner and
> `DOWNLOAD START... (HPIB)`, after which it waits for the image over GPIB.

Confidence: high (derived from the instrument's own key table + confirmed shared
register). It is non-destructive to test — if "5"-at-power-on does not show the
download banner, no harm is done. You can verify the scan code with the Key Test
first (it prints `Key Code:` + the value; pressing "5" should report `10` / `0x0A`).

## Download-mode GPIB address = 17 (fixed by the bootloader)

There is **no hardware GPIB address switch** on this instrument; the normal-mode
address is software-set (`GPIBADDRess` / "GPIB" key, 1–31) and stored in flash.
The boot ROM does **not** read that stored value.

Instead, on entering download mode the boot decision path calls the GPIB chip-init
routine (`boot101 @ 0x5958`) **before** the download wait loop:

```asm
move.b #$80,(0x6,A0)   ; AUXCMD (base+6): software reset ON
...clear status/interrupt regs...
move.b #$11,(0x8,A0)   ; ADDR register (base+8 = 0x1E9C09) <- 0x11
clr.b  (0x6,A0)        ; AUXCMD: reset OFF (release)
```

`0x11` = decimal **17** (low 5 address bits = `10001b` = 17; mode bits =
listener + talker enabled). There is also a set-address helper at `0x599E` (writes
an arbitrary value to the ADDR reg), but the download path uses the fixed `0x11`
init.

> [!IMPORTANT]
> In download mode the instrument **always listens at GPIB address 17**,
> regardless of its normal-operation address. Use a VISA resource such as
> `GPIB0::17::INSTR` (the bus number depends on your adapter; use the Scan menu
> option in normal mode to confirm it).

## Instrument options (identity/option record)

Options live in the identity record in the config flash sector (`0x00F00000`),
written/validated by the function at `0xE3B6E6` and read back by `0xE3B956`.

- **Write (normal mode):** `:TEST:INSTR:INFO:DATA "MODEL","SERIAL","g1","g2","g3","g4"` (each field individually quoted, comma-separated)
- **Query:** `:TEST:INSTR:INFO:DATA?` returns the same six quoted, comma-separated fields
- `*OPT?` lists the enabled option codes

The record is six fields: model, serial (≤ 10 chars), then four option groups.

| Group | Field offset | Valid code | Option (per firmware + manual) |
| --- | --- | --- | --- |
| g1 | `0x0E` | `010` | LCR Meter Function (E4916A only; full LCR also needs the Opt 001 impedance-probe hardware) |
| g2 | `0x14` | `000` | reserved / model-specific (writer emits `E4916A not support Opt`) |
| g3 | `0x1A` | `P01` | Power Range option — **do not enable** (see warning below) |
| g4 | `0x20` | `S01` | **EM option = Evaporation Monitor Mode** (thin-film deposition monitoring) |

Unused groups are `000`. The writer validates model, serial length, and each
code, printing `Error: Bad <...> Option No` / `E491xA not support Opt.<code>` on
rejection; the error queue (`:SYSTem:ERRor?`) surfaces these.

> [!WARNING]
> **Do not enable `P01` (Power Range, g3).** On these units it causes
> `E16: Prev. setting lost` on every reboot (it needs power-range hardware /
> `WAttCAL` data that isn't present). The tool does not offer or touch this group.

> [!NOTE]
> Because the command rewrites the whole record, the tool reads the current record
> first and preserves the model, serial, and untouched groups (applied via
> `flash_memcpy`'s sector read-modify-write, so calibration data in the same
> sector is preserved). Power-cycle for option changes to take effect.

## Status of goals

- [x] **Upload / write firmware** — verify the image (header + size + CRC-32) and
  stream it over GPIB while the unit is in download mode. Implemented in
  `E4916A_firmware_loader.py`, validated end-to-end.
- [x] **Enable / disable options (`010` LCR, `S01` EM)** — via
  `:TEST:INSTR:INFO:DATA`, with read-modify-write preservation of the rest of the
  record.
- [ ] **Download / read firmware out over GPIB** — not available. The bootloader
  is write-only (no read-back) and the runtime `:DEBUG` commands cannot read
  program flash. A live-unit flash read would require reading the flash chip
  directly (e.g. on a programmer) or a custom RAM stub.
