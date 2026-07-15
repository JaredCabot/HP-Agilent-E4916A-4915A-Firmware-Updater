#!/usr/bin/env python3
"""
E4916A_firmware_loader.py -- verify and upload E4916A/E4915A firmware images to
the resident "DOWNLOAD-CALGAMO" bootloader over GPIB (PyVISA), and enable or
disable licensed instrument options.

--------------------------------------------------------------------------------
PROTOCOL (reverse-engineered from the AM27C1024 boot EPROM and VERIFIED against
a real image, fw-0213.m)
--------------------------------------------------------------------------------
The boot EPROM maps at 0x000000 and is SEPARATE from the main program flash.

  * At power-on the EPROM reads a hardware register at 0x1E9801. If it reads
    0x0A the unit enters DOWNLOAD mode; otherwise it jumps to the main firmware
    at 0xE00400. 0x1E9801 is the front-panel key scan register, so download
    mode is entered by a PHYSICAL condition at power-up (holding the "5" key) --
    there is no pure-software "reboot into download".

  * In download mode the loader reads the following over GPIB (the instrument
    is addressed as a listener at primary address 17, which the bootloader
    hard-codes regardless of the unit's normal GPIB address):

        25 bytes : ASCII header  "DOWNLOAD-CALGAMO-REV01.00"  (strncmp, 25)
         4 bytes : payload size  (big-endian, must be <= flash capacity)
         4 bytes : payload CRC   (big-endian)
         4 bytes : reserved      (read and DISCARDED; 0 in official images)
                                  -> the prefix is therefore 37 bytes
        <size> B : raw firmware image, programmed to flash at 0xE00000

  * It erases 0xE00000, writes the image in 0x8000-byte sectors, recomputes the
    CRC over the written flash and compares. On success it prints "COMPLETE" and
    "Power Off->On to RUN!"; on any failure it prints the reason and STAYS in the
    bootloader.

  CHECKSUM = standard reflected CRC-32 (poly 0xEDB88320, init 0x00000000, no
  final XOR) over the payload bytes. Verified: the EPROM table is the standard
  CRC-32 table, and the CRC of fw-0213.m's payload equals its stored checksum
  0x7C7EDAF9.

--------------------------------------------------------------------------------
WHY THIS IS RECOVERABLE
--------------------------------------------------------------------------------
The bootloader lives in a separate EPROM this process never touches. A failed or
corrupted download cannot brick the unit: the loader reports the failure, stays
resident, and you can simply re-enter download mode and retry. This tool
deliberately drives ONLY the program-firmware download path -- it never programs
the boot EPROM or the option/calibration sector at 0xF00000 in a way that could
be unrecoverable.

--------------------------------------------------------------------------------
INSTRUMENT OPTIONS
--------------------------------------------------------------------------------
Options live in the identity record in the 0xF00000 config sector, accessed via
the SCPI command ":TEST:INSTR:INFO:DATA". The record is six comma-separated,
individually quoted fields:

    "MODEL","SERIAL","g1","g2","g3","g4"

Only two groups are exposed here as user-toggleable options:
    g1 = "010" -> LCR Meter Function          (E4916A only)
    g2 = Reserved
    g3 = "P01" -> Power Range option? - Do NOT enable: causes E16: Prev. setting lost on every reboot.
    g4 = "S01" -> EM / Evaporation Monitor Mode
The model, serial number and the remaining groups are always preserved on write.
(The Power Range option "P01" in g3 is intentionally NOT offered: on these units
it triggers "E16: Prev. setting lost" on every reboot.)
"""

from __future__ import annotations

import os
import re
import struct
import time
from typing import Callable, List, NamedTuple, Optional, Tuple


# =============================================================================
# Module-level constants
# =============================================================================

#: ASCII header prefix; the loader appends a revision string (e.g. "01.00")
#: to make the full 25-byte header "DOWNLOAD-CALGAMO-REV01.00".
HEADER_PREFIX = b"DOWNLOAD-CALGAMO-REV"

#: Fixed length of the ASCII header field, in bytes.
HEADER_LEN = 25

#: CPU address the payload is programmed to (documentation only; the loader
#: hard-codes this, the host never sends an address).
FLASH_BASE = 0x00E00000

#: The manufacturer image shipped alongside this script; used as the default
#: firmware file for uploads.
DEFAULT_IMAGE_NAME = "fw-0213.m"

#: The GPIB primary address the bootloader always listens on in download mode,
#: independent of the instrument's normal-operation address.
BOOTLOADER_GPIB_ADDRESS = 17


# =============================================================================
# CRC-32
# =============================================================================

class Crc32:
    """Reflected CRC-32 exactly as implemented by the E4916A bootloader.

    The parameters match the loader's on-EPROM lookup table:

    * polynomial      0xEDB88320 (reflected form of 0x04C11DB7)
    * initial value   0x00000000
    * no final XOR

    This is the checksum stored in a DOWNLOAD-CALGAMO image; the loader
    recomputes it over the received payload and both values must agree.
    """

    #: 256-entry lookup table, built once when the class is defined.
    _TABLE: Tuple[int, ...] = ()

    @staticmethod
    def _build_table() -> Tuple[int, ...]:
        """Compute the standard reflected CRC-32 lookup table."""
        table: List[int] = []
        for index in range(256):
            crc = index
            for _ in range(8):
                # Shift right, XOR-ing the polynomial when the low bit is set.
                crc = (crc >> 1) ^ 0xEDB88320 if (crc & 1) else (crc >> 1)
            table.append(crc)
        return tuple(table)

    @classmethod
    def compute(cls, data: bytes, seed: int = 0) -> int:
        """Return the CRC-32 of ``data``.

        Args:
            data: The bytes to checksum.
            seed: Starting CRC value (0 for a fresh checksum; pass a previous
                result to continue a running CRC over multiple buffers).

        Returns:
            The 32-bit CRC as an ``int``.
        """
        crc = seed & 0xFFFFFFFF
        table = cls._TABLE
        for byte in data:
            crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
        return crc & 0xFFFFFFFF


# Populate the lookup table now that the class body is complete.
Crc32._TABLE = Crc32._build_table()


# =============================================================================
# Firmware image
# =============================================================================

class FirmwareImage:
    """A parsed DOWNLOAD-CALGAMO firmware image.

    On-disk / on-the-wire layout::

        [25 B] ASCII header "DOWNLOAD-CALGAMO-REV<rev>"
        [ 4 B] payload size   (big-endian)
        [ 4 B] payload CRC-32  (big-endian)
        [ 4 B] reserved        (present in the 37-byte prefix; read & discarded)
        [ N B] payload         -> programmed to flash at 0xE00000

    The official manufacturer images already use the 37-byte prefix, so their
    bytes are sent verbatim. ``parse`` also accepts the legacy 33-byte prefix
    and disambiguates the two by CRC.

    Attributes:
        header: The decoded 25-byte header string.
        size: Payload length in bytes, as declared in the size field.
        crc_stored: The CRC-32 stored in the image.
        crc_ok: True if ``crc_stored`` matches the CRC of the extracted payload.
        payload: The raw firmware payload (what gets written to flash).
        prefix_len: The detected prefix length (33 or 37 bytes).
    """

    def __init__(self, raw: bytes, header: str, size: int, crc_stored: int,
                 crc_ok: bool, payload: bytes, prefix_len: int) -> None:
        self._raw = raw
        self.header = header
        self.size = size
        self.crc_stored = crc_stored
        self.crc_ok = crc_ok
        self.payload = payload
        self.prefix_len = prefix_len

    @property
    def raw(self) -> bytes:
        """The complete image bytes (prefix + payload) to stream to the loader."""
        return self._raw

    def __len__(self) -> int:
        """Total number of bytes that will be sent (prefix + payload)."""
        return len(self._raw)

    @classmethod
    def parse(cls, data: bytes) -> "FirmwareImage":
        """Parse and CRC-verify a DOWNLOAD-CALGAMO image from raw bytes.

        The prefix length is auto-detected: the 33- and 37-byte layouts are both
        tried and the one whose payload CRC matches the stored checksum wins. If
        neither matches, a best-effort object is returned with ``crc_ok=False``.

        Args:
            data: The full image bytes.

        Returns:
            A populated :class:`FirmwareImage`.

        Raises:
            ValueError: If the header is not a DOWNLOAD-CALGAMO header.
        """
        if data[:len(HEADER_PREFIX)] != HEADER_PREFIX:
            raise ValueError("not a DOWNLOAD-CALGAMO image (bad header)")

        header = data[:HEADER_LEN].decode("ascii", "replace")
        size = struct.unpack(">I", data[25:29])[0]
        stored = struct.unpack(">I", data[29:33])[0]

        # Try the legacy (33) then current (37) prefix and keep the CRC match.
        for prefix in (33, 37):
            payload = data[prefix:prefix + size]
            if len(payload) == size and Crc32.compute(payload) == stored:
                return cls(data, header, size, stored, True, payload, prefix)

        # No CRC match: report best effort with the legacy prefix, flagged bad.
        payload = data[33:33 + size]
        return cls(data, header, size, stored, False, payload, 33)

    @classmethod
    def from_file(cls, path: str) -> "FirmwareImage":
        """Load and parse a firmware image from ``path``.

        Args:
            path: Path to a DOWNLOAD-CALGAMO image (e.g. fw-0213.m).

        Returns:
            A populated :class:`FirmwareImage`.

        Raises:
            SystemExit: If the file is not a DOWNLOAD-CALGAMO image.
        """
        with open(path, "rb") as handle:
            data = handle.read()
        if data[:len(HEADER_PREFIX)] != HEADER_PREFIX:
            raise SystemExit(f"{path} is not a DOWNLOAD-CALGAMO image "
                             "(expected the manufacturer's fw-0213.m).")
        return cls.parse(data)


def script_dir() -> str:
    """Return the directory containing this script (falls back to the cwd)."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:  # __file__ is undefined (e.g. interactive paste)
        return os.getcwd()


def default_image_path() -> str:
    """Return the path to the default firmware image next to this script."""
    return os.path.join(script_dir(), DEFAULT_IMAGE_NAME)


# =============================================================================
# Instrument options
# =============================================================================

class Option(NamedTuple):
    """Definition of one user-toggleable instrument option.

    Attributes:
        group_index: Index (0-3) of the option group within the record.
        label: Human-readable name shown in the menu.
        code: The value written to enable the option (e.g. "010").
        e4916a_only: True if the option only applies to the E4916A model.
    """
    group_index: int
    label: str
    code: str
    e4916a_only: bool


class OptionRecord:
    """The instrument identity/option record (model, serial, four option groups).

    The record is read and written via ":TEST:INSTR:INFO:DATA" as six
    comma-separated, individually quoted fields::

        "MODEL","SERIAL","g1","g2","g3","g4"

    Only the options in :data:`OPTIONS` are exposed for toggling; every other
    field is preserved verbatim so a write never disturbs the model, serial or
    untouched groups.
    """

    #: The options this tool will enable/disable. Group 3 ("P01" Power Range) is
    #: deliberately omitted -- it causes "E16: Prev. setting lost" on reboot.
    OPTIONS: Tuple[Option, ...] = (
        Option(group_index=0, label="LCR Meter (010)", code="010", e4916a_only=True),
        Option(group_index=3, label="EM / Evap Mon (S01)", code="S01", e4916a_only=False),
    )

    #: The value that represents a disabled option group.
    DISABLED = "000"

    def __init__(self, model: str, serial: str, groups: List[str]) -> None:
        self.model = model
        self.serial = serial
        self.groups = list(groups)  # copy; four group strings [g1, g2, g3, g4]

    # -- classification ------------------------------------------------------

    @property
    def is_e4916a(self) -> bool:
        """True if the record's model is an E4916A (some options are model-gated)."""
        return self.model.upper() == "E4916A"

    # -- per-option state ----------------------------------------------------

    def is_enabled(self, option: Option) -> bool:
        """Return True if ``option`` is currently enabled in the record."""
        return self.groups[option.group_index].upper() == option.code

    def set_enabled(self, option: Option, enabled: bool) -> None:
        """Enable or disable ``option`` in place."""
        self.groups[option.group_index] = option.code if enabled else self.DISABLED

    def toggle(self, option: Option) -> None:
        """Flip the enabled state of ``option`` in place."""
        self.set_enabled(option, not self.is_enabled(option))

    def snapshot(self) -> List[str]:
        """Return a copy of the current group values (for before/after diffs)."""
        return list(self.groups)

    def enabled_e4916a_only_labels(self) -> List[str]:
        """Return labels of currently-enabled options that require an E4916A."""
        return [opt.label for opt in self.OPTIONS
                if opt.e4916a_only and self.is_enabled(opt)]

    # -- serialisation -------------------------------------------------------

    def fields(self) -> List[str]:
        """Return all six record fields in order: model, serial, then groups."""
        return [self.model, self.serial] + self.groups

    @staticmethod
    def quote_fields(fields: List[str]) -> str:
        """Join ``fields`` as a comma-separated list of individually quoted values."""
        return ",".join(f'"{value}"' for value in fields)

    def as_quoted(self) -> str:
        """Return this record as the quoted, comma-separated field string."""
        return self.quote_fields(self.fields())

    def to_command(self) -> str:
        """Return the full ":TEST:INSTR:INFO:DATA ..." write command."""
        return f":TEST:INSTR:INFO:DATA {self.as_quoted()}"

    @classmethod
    def parse(cls, response: str) -> "OptionRecord":
        """Build an :class:`OptionRecord` from a query response.

        Args:
            response: The raw ":TEST:INSTR:INFO:DATA?" reply.

        Returns:
            The parsed record.

        Raises:
            ValueError: If the response does not have exactly six fields.
        """
        fields = [value.strip().strip('"') for value in response.split(",")]
        if len(fields) != 6:
            raise ValueError(f"unexpected option record: {response!r}")
        return cls(fields[0], fields[1], fields[2:6])


# =============================================================================
# GPIB instrument
# =============================================================================

class Instrument:
    """A GPIB connection to an E4916A/E4915A via PyVISA.

    For normal-mode SCPI work use it as a context manager::

        with Instrument(resource) as inst:
            model, serial, idn = inst.identity()

    The firmware upload path (:meth:`upload_firmware`) is deliberately separate
    and manages its own session, because in download mode the unit is not a SCPI
    instrument at all -- it is a raw byte-stream loader that does not answer
    queries and must receive a continuous, untermintated stream.
    """

    #: Model substrings that identify a supported instrument in an *IDN? reply.
    MODELS = ("E4916A", "E4915A")

    def __init__(self, resource: str, timeout_ms: int = 5000) -> None:
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._rm = None    # pyvisa ResourceManager (lazily created)
        self._inst = None  # pyvisa resource handle

    # -- connection lifecycle ------------------------------------------------

    def open(self) -> "Instrument":
        """Open the VISA session configured for SCPI (newline-terminated)."""
        import pyvisa
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self.resource)
        self._inst.timeout = self.timeout_ms
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        return self

    def close(self) -> None:
        """Close the VISA session and resource manager, ignoring errors."""
        for handle in (self._inst, self._rm):
            try:
                if handle is not None:
                    handle.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        self._inst = self._rm = None

    def __enter__(self) -> "Instrument":
        return self.open()

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False  # do not suppress exceptions

    # -- SCPI primitives -----------------------------------------------------

    def query(self, scpi: str) -> str:
        """Send a query and return the stripped response."""
        return self._inst.query(scpi).strip()

    def write(self, scpi: str) -> None:
        """Send a command with no response."""
        self._inst.write(scpi)

    # -- higher-level normal-mode operations ---------------------------------

    def identity(self) -> Tuple[str, str, str]:
        """Return ``(model, serial, idn)`` from *IDN?.

        Raises:
            ValueError: If the reply is not from a supported model.
        """
        idn = self.query("*IDN?")
        parts = idn.split(",")
        if len(parts) < 3 or not any(model in idn for model in self.MODELS):
            raise ValueError(
                f"that doesn't look like an E4915A/E4916A in normal mode: {idn!r}")
        return parts[1].strip(), parts[2].strip(), idn

    def read_option_groups(self) -> Tuple[Optional[List[str]], Optional[str]]:
        """Read the four option-group values from the identity record.

        Returns:
            ``(groups, raw_response)`` where ``groups`` is a list of four strings,
            or ``(None, raw_response)`` if the record could not be parsed, or
            ``(None, None)`` if the query itself failed.
        """
        try:
            response = self.query(":TEST:INSTR:INFO:DATA?")
        except Exception:  # noqa: BLE001 - instrument/query error
            return None, None
        fields = [value.strip().strip('"') for value in response.split(",")]
        if len(fields) == 6:
            return fields[2:6], response
        return None, response

    def write_option_record(self, record: OptionRecord) -> None:
        """Write ``record`` back to the instrument's option store."""
        self.write(record.to_command())

    # -- discovery -----------------------------------------------------------

    @classmethod
    def scan(cls, models: Optional[Tuple[str, ...]] = None,
             stop_on_first: bool = False, verbose: bool = True,
             probe_timeout_ms: int = 2000) -> List[Tuple[str, str]]:
        """Scan the GPIB bus for supported instruments.

        Identification is by *IDN?, which only answers in NORMAL firmware -- an
        instrument already in download mode will not be found (and must not be
        probed, as the query bytes would desync the loader). Run this before
        entering download mode.

        Args:
            models: Model substrings to accept (defaults to :data:`MODELS`).
            stop_on_first: Return as soon as the first match is found.
            verbose: Print per-resource progress.
            probe_timeout_ms: Per-resource *IDN? timeout.

        Returns:
            A list of ``(resource, idn)`` tuples (possibly empty).

        Raises:
            ImportError: If PyVISA (or a backend) is not available.
        """
        import pyvisa
        models = models or cls.MODELS
        rm = pyvisa.ResourceManager()
        found: List[Tuple[str, str]] = []
        try:
            gpib = [res for res in rm.list_resources() if "GPIB" in res.upper()]
            if verbose:
                print(f"Scanning {len(gpib)} GPIB resource(s): {gpib}")
            for res in gpib:
                inst = None
                try:
                    inst = rm.open_resource(res)
                    inst.timeout = probe_timeout_ms
                    inst.read_termination = "\n"
                    inst.write_termination = "\n"
                    idn = inst.query("*IDN?").strip()
                    if any(model in idn for model in models):
                        if verbose:
                            print(f"  FOUND {res}: {idn}")
                        found.append((res, idn))
                        if stop_on_first:
                            return found
                    elif verbose:
                        print(f"  {res}: {idn}  (not a match)")
                except Exception as exc:  # noqa: BLE001 - skip busy/silent devices
                    if verbose:
                        print(f"  {res}: no response ({type(exc).__name__})")
                finally:
                    if inst is not None:
                        try:
                            inst.close()
                        except Exception:  # noqa: BLE001
                            pass
        finally:
            try:
                rm.close()
            except Exception:  # noqa: BLE001
                pass
        return found

    # -- firmware download ---------------------------------------------------

    def upload_firmware(self, image: FirmwareImage,
                        on_progress: Optional[Callable[[int, int], None]] = None,
                        chunk_bytes: int = 16384,
                        timeout_ms: int = 120_000) -> None:
        """Stream a firmware image to a unit that is ALREADY in download mode.

        A dedicated raw session is opened (read/write termination disabled) and
        the image is sent in ``chunk_bytes`` pieces. ``on_progress(sent, total)``
        is invoked after each chunk so the caller can render a progress bar.

        The bootloader counts received bytes against the size field (it does not
        rely on EOI), so chunking is safe: the only gap between chunks is Python
        overhead (<<1 ms), well inside the loader's per-byte receive timeout. EOI
        is asserted only on the final chunk.

        Args:
            image: The verified image to send.
            on_progress: Optional ``callback(bytes_sent, bytes_total)``.
            chunk_bytes: Bytes per write (raise this if a slow adapter stalls).
            timeout_ms: VISA write timeout for the streaming session.

        Raises:
            SystemExit: If the image's CRC is self-inconsistent.
        """
        if not image.crc_ok:
            raise SystemExit("Image CRC does not match its own checksum field; "
                             "refusing to send a self-inconsistent image.")

        import pyvisa
        data = image.raw
        total = len(data)

        rm = pyvisa.ResourceManager()
        inst = rm.open_resource(self.resource)
        inst.timeout = timeout_ms
        # The loader wants a continuous, unterminated stream.
        try:
            inst.write_termination = None
            inst.read_termination = None
        except Exception:  # noqa: BLE001 - not all backends expose these
            pass

        try:
            sent = 0
            for offset in range(0, total, chunk_bytes):
                part = data[offset:offset + chunk_bytes]
                is_last = offset + len(part) >= total
                try:
                    inst.send_end = bool(is_last)  # assert EOI only at the end
                except Exception:  # noqa: BLE001
                    pass
                inst.write_raw(part)
                sent += len(part)
                if on_progress is not None:
                    on_progress(sent, total)
        finally:
            inst.close()
            rm.close()


# =============================================================================
# Console I/O helpers
# =============================================================================

class Console:
    """Small, stateless helpers for terminal prompts and progress output."""

    @staticmethod
    def ask(prompt: str, default: Optional[str] = None) -> str:
        """Prompt for a line of input, showing ``default`` and returning it on Enter."""
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return default or ""
        return value if value else (default or "")

    @staticmethod
    def ask_yes_no(prompt: str, default: bool = False) -> bool:
        """Prompt for a yes/no answer; empty input returns ``default``."""
        hint = "Y/n" if default else "y/N"
        try:
            value = input(f"{prompt} [{hint}]: ").strip().lower()
        except EOFError:
            return default
        if not value:
            return default
        return value in ("y", "yes")

    @staticmethod
    def pause() -> None:
        """Wait for the user to press Enter (used to hold menu output on screen)."""
        try:
            input("\nPress Enter to continue...")
        except EOFError:
            pass

    @staticmethod
    def render_progress(sent: int, total: int, width: int = 30) -> None:
        """Render one in-place progress-bar update (expects a real terminal)."""
        filled = width * sent // total
        bar = "#" * filled + "." * (width - filled)
        print(f"\r  [{bar}] {sent * 100 // total:3d}%  {sent:>7}/{total} bytes",
              end="", flush=True)


# =============================================================================
# Interactive application
# =============================================================================

class FirmwareLoaderApp:
    """The interactive, menu-driven front end.

    Holds the one piece of session state -- the discovered normal-mode VISA
    resource -- and orchestrates :class:`Instrument`, :class:`FirmwareImage` and
    :class:`OptionRecord` in response to the user's menu choices.
    """

    def __init__(self) -> None:
        #: The normal-mode VISA resource discovered at startup / by a rescan.
        self.resource: Optional[str] = None

    # -- session helpers -----------------------------------------------------

    def bootloader_resource(self) -> str:
        """Return the download-mode resource: address 17 on the discovered bus.

        Download mode always listens at primary address 17 regardless of the
        instrument's normal address, so we keep the discovered bus number but
        force the address. Falls back to ``GPIB0::17::INSTR`` if nothing has been
        discovered yet.
        """
        if self.resource:
            match = re.match(r'(GPIB\d+::)\d+(::INSTR)', self.resource, re.I)
            if match:
                return f"{match.group(1)}{BOOTLOADER_GPIB_ADDRESS}{match.group(2)}"
        return f"GPIB0::{BOOTLOADER_GPIB_ADDRESS}::INSTR"

    def startup_connect(self) -> None:
        """Auto-scan for the instrument at startup, remembering the resource.

        If nothing is found, the user is invited to connect the instrument and
        press Enter to retry, or type 's' to skip and continue offline.
        """
        print("Scanning the GPIB bus for the instrument...")
        while True:
            try:
                matches = Instrument.scan(stop_on_first=True, verbose=False)
            except Exception as exc:  # noqa: BLE001 - PyVISA/backend missing
                print(f"  GPIB scan unavailable: {exc}")
                matches = []
            if matches:
                res, idn = matches[0]
                self.resource = res
                print(f"  Connected: {res}   [{idn}]")
                return
            print("\n  No E4916A/E4915A found on the GPIB bus.")
            answer = Console.ask("  Connect the instrument via GPIB and press "
                                 "Enter to retry, or type 's' to skip")
            if answer.strip().lower() in ("s", "skip"):
                print("  Continuing without a confirmed connection; normal-mode "
                      "actions will ask for the address.")
                return

    # -- top-level loop ------------------------------------------------------

    def run(self) -> None:
        """Show the banner, connect, and drive the main menu loop."""
        # Menu entries: key -> (label, bound handler).
        actions = {
            "1": ("Scan for instrument on GPIB bus", self.menu_scan),
            "2": (f"Upload firmware ({DEFAULT_IMAGE_NAME})", self.menu_upload),
            "3": ("Enable/disable instrument options (010 LCR / S01 EM)",
                  self.menu_options),
            "4": ("Show the update procedure / help", self.menu_procedure),
        }

        print("=" * 60)
        print("  E4916A / E4915A firmware download tool")
        print("=" * 60)
        self.startup_connect()

        while True:
            print("\nMenu:")
            for key in sorted(actions):
                print(f"  {key}. {actions[key][0]}")
            print("  0. Exit")
            try:
                choice = input("\nSelect: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if choice == "0":
                break
            entry = actions.get(choice)
            if entry is None:
                print("Invalid selection.")
                continue
            try:
                entry[1]()  # invoke the bound handler
            except KeyboardInterrupt:
                print("\n(cancelled)")
            Console.pause()
        print("Bye.")

    # -- menu handlers -------------------------------------------------------

    def menu_scan(self) -> None:
        """Menu 1: rescan the bus and remember the instrument's resource."""
        print("\n--- Scan for E4916A/E4915A (NORMAL mode only) ---")
        print("Note: the instrument only answers *IDN? in normal firmware, NOT in")
        print("download/bootrom mode. Use this to confirm your adapter's bus number.\n")
        try:
            matches = Instrument.scan(stop_on_first=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Scan failed: {exc}")
            print("(Is PyVISA + a VISA backend installed and the GPIB adapter present?)")
            return
        if matches:
            res, idn = matches[0]
            self.resource = res
            print(f"\nFound: {res}\t{idn}")
            print("(stopped at the first match; remembered for this session)")
        else:
            print("\nNothing matched. If the unit is powered and in normal mode, "
                  "check the GPIB cable/adapter and address.")

    def menu_upload(self) -> None:
        """Menu 2: verify a firmware image and stream it in download mode."""
        print(f"\n--- Upload firmware ({DEFAULT_IMAGE_NAME}) ---")

        # 1) Choose the image (defaulting to the manufacturer file).
        print("\nPress Enter to accept the default firmware file, or type a path.")
        path = Console.ask("Firmware image file", default_image_path())
        if not path or not os.path.exists(path):
            print(f"File not found: {path}")
            return
        try:
            image = FirmwareImage.from_file(path)
        except SystemExit as exc:
            print(exc)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"Could not load image: {exc}")
            return

        # 2) CRC check runs automatically before anything is sent.
        print(f"\nVerifying image: {image.header}, {image.size} bytes")
        print(f"  stored CRC 0x{image.crc_stored:08X}  ->  CRC check: "
              f"{'PASSED' if image.crc_ok else 'FAILED'}")
        if not image.crc_ok:
            print("Image CRC is INVALID -- refusing to upload.")
            return

        # 3) Make sure the instrument is in download mode.
        print("\nNow put the instrument in DOWNLOAD mode:")
        print("  power OFF -> hold '5' -> power ON -> release when the display "
              "is active.")
        if not Console.ask_yes_no("\nIs the instrument showing 'BOOTROM REV:xx.xx'?",
                                  default=False):
            print("Enter download mode first, then re-run this option.")
            return

        # 4) Confirm the destructive write.
        resource = self.bootloader_resource()
        print(f"\nGPIB resource (download mode is always address 17): {resource}")
        print(f"About to send {image.size} bytes to {resource}.")
        print("This will ERASE and reprogram the main firmware flash with the "
              "new firmware.")
        if Console.ask('\nType EXACTLY "WRITE" to proceed (anything else cancels)') \
                != "WRITE":
            print("Cancelled.")
            return

        # 5) Guidance, then stream with a live progress bar.
        print("\nWatch the instrument: it should ERASE, then display:")
        print("'WRITING TO FLASH... sector N', then:")
        print("'COMPLETE' 'Power Off->On to RUN!'.")
        print("On error it prints the reason and ")
        print("stays in the bootloader so you can retry.\n")
        print(f"Sending {len(image)} bytes...")
        try:
            Instrument(resource).upload_firmware(
                image, on_progress=Console.render_progress)
            print()  # finish the progress line
            print("Image sent. Reboot instrument")
        except SystemExit as exc:
            print()
            print(exc)
        except Exception as exc:  # noqa: BLE001
            print()
            print(f"Upload error: {exc}")
            print("The instrument stays in the bootloader on failure; you can retry.")

    def menu_options(self) -> None:
        """Menu 3: read, toggle and write the LCR/EM instrument options."""
        print("\n--- Enable / disable instrument options ---")
        print("\nReads the identity/option record, lets you toggle each option,")
        print("then writes it back via :TEST:INSTR:INFO:DATA. ")
        print("Model & serial number are always preserved.")
        print("\nOptions offered:")
        print("  010 = LCR Meter Function            (E4916A only)")
        print("  S01 = EM / Evaporation Monitor Mode\n")

        # Resolve the (normal-mode) connection.
        if self.resource:
            resource = self.resource
            print(f"Using connection: {resource}")
        else:
            resource = Console.ask("GPIB resource", "GPIB0::17::INSTR")

        try:
            with Instrument(resource) as inst:
                # Model & serial come authoritatively from *IDN?.
                try:
                    model, serial, idn = inst.identity()
                except ValueError as exc:
                    print(f"{exc}\nAborting.")
                    return
                print(f"IDN: {idn}")

                # Current option groups come from the record (default if unreadable).
                groups, raw = inst.read_option_groups()
                if groups is None:
                    print("(couldn't read current options; unknown groups "
                          "default to 000)")
                    groups = ["000", "000", "000", "000"]
                else:
                    print(f"Current record: {raw}")

                record = OptionRecord(model, serial, groups)
                original = record.snapshot()

                # Interactive toggle editor; False means the user cancelled.
                if not self._edit_options(record):
                    return
                if record.groups == original:
                    print("No change.")
                    return

                # Warn if enabling E4916A-only options on an E4915A.
                if not record.is_e4916a:
                    e4916a_only = record.enabled_e4916a_only_labels()
                    if e4916a_only:
                        print(f"\nNote: {', '.join(e4916a_only)} are E4916A "
                              f"features; this is {model}. The instrument may "
                              "reject them -- check the result below.")

                # Show the before/after and confirm the write.
                before = OptionRecord.quote_fields([model, serial] + original)
                after = record.as_quoted()
                print(f"\n  before: {before}")
                print(f"  after : {after}")
                print(f"\nAbout to write (updates the config-flash option record):"
                      f"\n  {record.to_command()}")
                if Console.ask('Type EXACTLY "WRITE" to proceed (anything else '
                               'cancels)') != "WRITE":
                    print("Cancelled.")
                    return

                inst.write_option_record(record)
                time.sleep(1.0)  # let the flash write settle before reading back

                # Read back and surface any instrument-side validation error.
                for scpi, label in ((":TEST:INSTR:INFO:DATA?", "Record now"),
                                    ("*OPT?", "*OPT? now"),
                                    (":SYSTem:ERRor?", "Error queue")):
                    try:
                        print(f"{label:11s}: {inst.query(scpi)}")
                    except Exception:  # noqa: BLE001
                        pass
                print("\nPower-cycle the instrument for the change to take effect.")
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}")

    @staticmethod
    def _edit_options(record: OptionRecord) -> bool:
        """Run the interactive toggle loop against ``record``.

        Args:
            record: The record to edit in place.

        Returns:
            True if the user chose to write ('w'); False if they cancelled ('q').
        """
        while True:
            print("\nOptions:")
            for number, option in enumerate(OptionRecord.OPTIONS, 1):
                state = "ENABLED " if record.is_enabled(option) else "disabled"
                note = ("   (E4916A only)"
                        if option.e4916a_only and not record.is_e4916a else "")
                print(f"  {number}. {option.label:22s} {state}{note}")
            print("  w. write & apply     q. cancel")

            choice = Console.ask("\nAction").lower()
            if choice == "q":
                print("Cancelled.")
                return False
            if choice == "w":
                return True
            if choice.isdigit() and 1 <= int(choice) <= len(OptionRecord.OPTIONS):
                record.toggle(OptionRecord.OPTIONS[int(choice) - 1])
            else:
                print("Unknown action.")

    def menu_procedure(self) -> None:
        """Menu 4: print the firmware-update and option procedures."""
        print("""
--- Firmware update procedure ---

  1. Select 'Upload Firmware' (menu option 2).

  2. Enter DOWNLOAD mode on the instrument:
       - Power the instrument OFF.
       - Hold the front-panel "5" key.
       - Power ON while holding "5".
       - Release when it shows the copyright screen / "BOOTROM REV:1.01".
         It now waits for the image over GPIB.

  3. Upload:
       - The image's CRC is verified automatically before anything is sent;
         the latest fw-0213.m (2.13) is already in the correct format and selected by default.
       - GPIB address in download mode is fixed at 17 -> GPIB0::17::INSTR
         (the bus number depends on your adapter; menu option 1 confirms it).
       - The instrument shows: ERASING FLASH... , WRITING TO FLASH... sector N,
         then COMPLETE and "Power Off->On to RUN !".
       - On any error it prints the reason and stays in the bootloader, so you
         can simply retry.

  4. Power-cycle (without pressing "5") to run the new firmware.

--- Option Enable/Disable procedure ---

  1. Select 'Enable/disable instrument options' (menu option 3).

  2. Select which options to enable or disable.
       - 010 LCR Meter Function (E4916A only; full LCR requires Opt 001 impedance-probe hardware)
       - S01 EM option = Evaporation Monitor Mode (thin-film deposition monitoring)

  3. Apply the selection to enable or disable selected options.

  4. Power-cycle to finalise.

""")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    try:
        FirmwareLoaderApp().run()
    except KeyboardInterrupt:
        print("\nBye.")
