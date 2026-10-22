#!/usr/bin/env python3
"""Read-only Modbus TCP register dump of an APsystems ECU.

Reads holding registers 40000-40261 from one or more inverter unit IDs and
probes one unit ID that has no inverter, so the answers can be used to design
the integration. It never writes and never reads past 40261 (the ECU-R fills
40262-40269 with 0xDEADBEEF and stops answering at 40270).

Usage:
    python3 dump_registers.py --host 192.0.2.10 --units 1 2 --out .

Before running:
  * Run it only while the ECU is answering normally (no timeouts in the
    integration that polls it), preferably right after a scheduled poll.
  * One TCP connection, one request at a time, a one second pause between
    requests. About 15-20 requests in total.
  * Serial numbers are left out, so the JSON file can be shared as it is.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time

FIRST_REGISTER = 40000
LAST_REGISTER = 40261  # inclusive, never read beyond this
BLOCK_SIZES = (125, 60, 30)  # tried in this order on the first unit
PAUSE = 1.0  # seconds between requests
TIMEOUT = float(os.environ.get("DUMP_TIMEOUT", "15"))  # seconds per request
SERIAL_REGISTERS = range(40052, 40068)  # model 1 serial number, left out

EXCEPTION_NAMES = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "server device failure",
    6: "server device busy",
    10: "gateway path unavailable",
    11: "gateway target device failed to respond",
}


class ModbusReplyError(Exception):
    """The device answered with a Modbus exception code."""

    def __init__(self, code: int) -> None:
        """Keep the exception code for the report."""
        super().__init__(f"exception {code} ({EXCEPTION_NAMES.get(code, 'unknown')})")
        self.code = code


class Client:
    """Minimal Modbus TCP client for function code 3 only."""

    def __init__(self, host: str, port: int) -> None:
        """Remember where to connect; the socket opens on the first read."""
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.transaction = 0

    def connect(self) -> None:
        """Open a fresh TCP connection."""
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=TIMEOUT)

    def close(self) -> None:
        """Close the connection if open."""
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv_exact(self, size: int) -> bytes:
        if self.sock is None:
            raise ConnectionError("not connected")
        buf = b""
        while len(buf) < size:
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("connection closed by device")
            buf += chunk
        return buf

    def read_holding(
        self, unit: int, address: int, count: int
    ) -> tuple[list[int], float]:
        """Read `count` holding registers starting at `address` from `unit`.

        Returns the register values and the response time in seconds.
        """
        if self.sock is None:
            self.connect()
        if self.sock is None:
            raise ConnectionError("not connected")
        self.transaction = (self.transaction + 1) & 0xFFFF
        pdu = struct.pack(">BHH", 3, address, count)
        frame = struct.pack(">HHHB", self.transaction, 0, len(pdu) + 1, unit) + pdu
        started = time.monotonic()
        self.sock.sendall(frame)
        while True:
            header = self._recv_exact(7)
            transaction, _protocol, length, resp_unit = struct.unpack(">HHHB", header)
            body = self._recv_exact(length - 1)
            if transaction == self.transaction:
                break
            print(
                f"      (discarding late reply, transaction {transaction}, unit {resp_unit})"
            )
        elapsed = time.monotonic() - started
        if body[0] & 0x80:
            raise ModbusReplyError(body[1])
        byte_count = body[1]
        values = list(struct.unpack(f">{byte_count // 2}H", body[2 : 2 + byte_count]))
        return values, elapsed


def fmt_block(address: int, values: list[int]) -> str:
    """Format one block as rows of eight registers, hex and decimal."""
    lines = []
    for offset in range(0, len(values), 8):
        chunk = values[offset : offset + 8]
        hexes = " ".join(f"{v:04x}" for v in chunk)
        decs = " ".join(f"{v:5d}" for v in chunk)
        lines.append(f"  {address + offset:5d}  {hexes:<40}  {decs}")
    return "\n".join(lines)


def registers_to_ascii(values: list[int]) -> str:
    """Decode registers as a null-padded ASCII string."""
    raw = b"".join(struct.pack(">H", v) for v in values)
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def summarise(address_of: dict[int, int]) -> list[str]:
    """Walk the SunSpec model chain and describe what was found."""
    notes: list[str] = []

    def reg(addr: int) -> int | None:
        return address_of.get(addr)

    marker = [reg(FIRST_REGISTER), reg(FIRST_REGISTER + 1)]
    if None not in marker:
        notes.append(
            f"40000-40001 as text: {registers_to_ascii(marker)!r} (expect 'SunS')"
        )

    addr = FIRST_REGISTER + 2
    while addr + 1 <= LAST_REGISTER:
        model_id, length = reg(addr), reg(addr + 1)
        if model_id is None or length is None:
            notes.append(f"chain stops at {addr}: register not read")
            break
        if model_id == 0xFFFF:
            notes.append(f"{addr}: end marker 0xFFFF (length {length})")
            break
        notes.append(
            f"{addr}: model {model_id}, length {length} -> data {addr + 2}-{addr + 1 + length}"
        )
        if model_id == 1:
            fields = {
                "Mn": (addr + 2, 16),
                "Md": (addr + 18, 16),
                "Opt": (addr + 34, 8),
                "Vr": (addr + 42, 8),
                "SN": (addr + 50, 16),
            }
            for name, (start, size) in fields.items():
                vals = [reg(a) for a in range(start, start + size)]
                if None not in vals:
                    notes.append(f"    model 1 {name}: {registers_to_ascii(vals)!r}")
            da = reg(addr + 66)
            if da is not None:
                notes.append(f"    model 1 DA (device address): {da}")
        addr = addr + 2 + length
    return notes


def main() -> int:
    """Run the dump."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", required=True, help="ECU IP address or hostname")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument(
        "--units", type=int, nargs="+", default=[1], help="unit IDs to dump, e.g. 1 2"
    )
    parser.add_argument(
        "--probe-unit",
        type=int,
        default=None,
        help="a unit ID without an inverter, read once",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("."), help="directory for the JSON file"
    )
    args = parser.parse_args()

    stamp = datetime.now(UTC).astimezone()
    result: dict = {
        "timestamp": stamp.isoformat(timespec="seconds"),
        "port": args.port,
        "first_register": FIRST_REGISTER,
        "last_register": LAST_REGISTER,
        "block_size_attempts": [],
        "block_size": None,
        "units": {},
        "probe": None,
    }

    client = Client(args.host, args.port)
    try:
        client.connect()
    except OSError as err:
        print(f"cannot connect to {args.host}:{args.port}: {err}")
        return 1
    print(f"{stamp:%H:%M:%S} connected to port {args.port}")

    # 1. Find the largest block size the ECU accepts, on the first unit.
    first_unit = args.units[0]
    block_size = None
    for size in BLOCK_SIZES:
        print(
            f"\n[block size] unit {first_unit}: reading {size} registers from {FIRST_REGISTER}"
        )
        attempt = {"unit": first_unit, "size": size}
        try:
            values, elapsed = client.read_holding(first_unit, FIRST_REGISTER, size)
            attempt.update(ok=True, seconds=round(elapsed, 2))
            print(f"  ok in {elapsed:.2f} s")
            block_size = size
        except ModbusReplyError as err:
            attempt.update(ok=False, exception=err.code, seconds=None)
            print(f"  {err}")
        except (OSError, ConnectionError) as err:
            attempt.update(ok=False, error=str(err), seconds=None)
            print(f"  {type(err).__name__}: {err}; reconnecting")
            try:
                client.connect()
            except OSError as err2:
                print(f"  reconnect failed: {err2}")
                result["block_size_attempts"].append(attempt)
                break
        result["block_size_attempts"].append(attempt)
        time.sleep(PAUSE)
        if block_size:
            break
    if block_size is None:
        print(
            "\nno block size worked; stopping (do not retry while the ECU is struggling)"
        )
        client.close()
        write_json(args.out, stamp, result)
        return 1
    result["block_size"] = block_size

    # 2. Dump every requested unit in blocks.
    for unit in args.units:
        print(
            f"\n[unit {unit}] registers {FIRST_REGISTER}-{LAST_REGISTER} in blocks of {block_size}"
        )
        unit_result: dict = {"blocks": [], "registers": {}, "error": None}
        address = FIRST_REGISTER
        while address <= LAST_REGISTER:
            count = min(block_size, LAST_REGISTER - address + 1)
            try:
                values, elapsed = client.read_holding(unit, address, count)
            except ModbusReplyError as err:
                print(f"  {address}+{count}: {err}")
                unit_result["blocks"].append(
                    {"address": address, "count": count, "exception": err.code}
                )
                unit_result["error"] = str(err)
                break
            except (OSError, ConnectionError) as err:
                print(
                    f"  {address}+{count}: {type(err).__name__}: {err}; stopping this unit"
                )
                unit_result["blocks"].append(
                    {"address": address, "count": count, "error": str(err)}
                )
                unit_result["error"] = str(err)
                client.connect()
                break
            values = [
                0 if address + offset in SERIAL_REGISTERS else value
                for offset, value in enumerate(values)
            ]
            print(f"  {address}+{count} in {elapsed:.2f} s")
            print(fmt_block(address, values))
            unit_result["blocks"].append(
                {"address": address, "count": count, "seconds": round(elapsed, 2)}
            )
            for offset, value in enumerate(values):
                unit_result["registers"][str(address + offset)] = value
            address += count
            time.sleep(PAUSE)
        address_of = {int(k): v for k, v in unit_result["registers"].items()}
        notes = summarise(address_of)
        unit_result["summary"] = notes
        print(f"\n  summary unit {unit}:")
        for note in notes:
            print(f"    {note}")
        result["units"][str(unit)] = unit_result

    # 3. Probe a unit ID without an inverter.
    if args.probe_unit is not None:
        print(
            f"\n[probe] unit {args.probe_unit}: reading 2 registers from {FIRST_REGISTER}"
        )
        probe: dict = {"unit": args.probe_unit}
        try:
            values, elapsed = client.read_holding(args.probe_unit, FIRST_REGISTER, 2)
            probe.update(ok=True, seconds=round(elapsed, 2), values=values)
            print(
                f"  answered in {elapsed:.2f} s: {values} ({registers_to_ascii(values)!r})"
            )
        except ModbusReplyError as err:
            probe.update(ok=False, exception=err.code)
            print(f"  {err}")
        except (OSError, ConnectionError) as err:
            probe.update(ok=False, error=str(err))
            print(f"  {type(err).__name__}: {err}")
        result["probe"] = probe

    client.close()
    write_json(args.out, stamp, result)
    return 0


def write_json(out_dir: Path, stamp: datetime, result: dict) -> None:
    """Store the raw registers and probe result next to the output directory."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ecu_dump_{stamp:%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(result, indent=1))
    print(f"\nJSON written to {path}")


if __name__ == "__main__":
    sys.exit(main())
