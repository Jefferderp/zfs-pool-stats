#!/usr/bin/env python3
"""A lightweight, dependency-free ZFS pool statistics monitor."""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence

VERSION = "1.0.0"
BYTE_UNITS = ("B", "K", "M", "G", "T", "P", "E", "Z", "Y")
TIME_UNITS = (
    ("d", 86_400_000_000_000),
    ("h", 3_600_000_000_000),
    ("m", 60_000_000_000),
    ("s", 1_000_000_000),
    ("ms", 1_000_000),
    ("us", 1_000),
    ("ns", 1),
)


class ZfsCommandError(RuntimeError):
    """A ZFS command failed or returned output the monitor cannot use."""


@dataclasses.dataclass(frozen=True)
class Column:
    key: str
    header: str
    kind: str
    unit: str | None = None
    precision: int = 1


COLUMN_SPECS = {
    "pool": ("pool", "label"),
    "health": ("health", "label"),
    "logical_used": ("alloc", "bytes"),
    "logical_free": ("lfree", "bytes"),
    "used": ("used", "bytes"),
    "free": ("free", "bytes"),
    "total": ("total", "bytes"),
    "capacity": ("cap", "percent"),
    "read_ops": ("rops", "number"),
    "write_ops": ("wops", "number"),
    "read": ("read", "bytes"),
    "write": ("write", "bytes"),
    "read_wait": ("rwait", "time"),
    "write_wait": ("wwait", "time"),
    "total_wait": ("wait", "time"),
    "fragmentation": ("frag", "percent"),
    "compression": ("comp", "percent"),
    "compression_ratio": ("ratio", "ratio"),
    "children": ("child", "bytes"),
    "snapshots": ("snap", "bytes"),
}

DEFAULT_COLUMNS = (
    "used,free,total,capacity::0,read,write,fragmentation::0,compression::0,snapshots"
)


def _number(value: str) -> int | float:
    value = value.strip().rstrip("x%")
    if value in {"", "-"}:
        return 0
    try:
        return int(value)
    except ValueError:
        return float(value)


def format_bytes(value: float, unit: str | None = None, precision: int = 1) -> str:
    value = float(value)
    if unit is None:
        index = (
            0
            if value == 0
            else min(int(math.log(abs(value), 1024)), len(BYTE_UNITS) - 1)
        )
        unit = BYTE_UNITS[index]
    else:
        unit = unit.upper()
        if unit not in BYTE_UNITS:
            raise ValueError(
                f"invalid byte unit {unit!r}; choose from {', '.join(BYTE_UNITS)}"
            )
        index = BYTE_UNITS.index(unit)
    scaled = value / (1024**index)
    if unit == "B" and precision == 1:
        precision = 0
    return f"{scaled:.{precision}f}{unit}"


def format_time(value: float, unit: str | None = None, precision: int = 1) -> str:
    value = float(value)
    units = dict(TIME_UNITS)
    if unit is None:
        unit, divisor = next(
            ((name, size) for name, size in TIME_UNITS if abs(value) >= size), ("us", 1)
        )
    else:
        unit = unit.lower()
        if unit not in units:
            raise ValueError(
                f"invalid time unit {unit!r}; choose from {', '.join(units)}"
            )
        divisor = units[unit]
    return f"{value / divisor:.{precision}f}{unit}"


def _column_key(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def header_interval(
    configured: int | None,
    output=None,
    terminal_size: Callable[[], os.terminal_size] = shutil.get_terminal_size,
) -> int:
    """Return the current number of data rows between printed headers."""
    if configured is not None:
        return configured
    output = sys.stdout if output is None else output
    if not output.isatty():
        return 0
    return max(1, terminal_size().lines - 1)


def parse_columns(value: str | None) -> list[Column]:
    raw_columns = (value or DEFAULT_COLUMNS).split(",")
    columns: list[Column] = []
    for raw in raw_columns:
        parts = [part.strip() for part in raw.split(":")]
        key = _column_key(parts[0])
        if key not in COLUMN_SPECS:
            choices = ", ".join(COLUMN_SPECS)
            raise ValueError(
                f"unknown column {parts[0]!r}; available columns: {choices}"
            )
        default_header, kind = COLUMN_SPECS[key]
        unit = parts[1] or None if len(parts) > 1 else None
        try:
            precision = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        except ValueError as exc:
            raise ValueError(f"invalid precision in column {raw!r}") from exc
        if not 0 <= precision <= 9:
            raise ValueError(f"precision in column {raw!r} must be between 0 and 9")
        header = parts[3] if len(parts) > 3 and parts[3] else default_header
        if len(parts) > 4:
            raise ValueError(f"too many fields in column {raw!r}")
        if unit and kind == "bytes" and unit.upper() not in BYTE_UNITS:
            raise ValueError(f"invalid byte unit {unit!r} in column {raw!r}")
        if unit and kind == "time" and unit.lower() not in dict(TIME_UNITS):
            raise ValueError(f"invalid time unit {unit!r} in column {raw!r}")
        if unit and kind not in {"bytes", "time"}:
            raise ValueError(f"column {parts[0]!r} does not accept a unit")
        columns.append(Column(key, header, kind, unit, precision))
    if not columns:
        raise ValueError("at least one column is required")
    return columns


def parse_iostat(output: str, pool: str) -> dict[str, int | float]:
    rows = [
        line.split()
        for line in output.splitlines()
        if line.split() and line.split()[0] == pool
    ]
    if not rows:
        raise ZfsCommandError(f"zpool iostat returned no row for pool {pool!r}")
    row = rows[-1]
    if len(row) < 7:
        raise ZfsCommandError(
            f"zpool iostat returned an incomplete row for pool {pool!r}"
        )
    values = [_number(value) for value in row[1:]]
    result = {
        "pool": pool,
        "logical_used": values[0],
        "logical_free": values[1],
        "read_ops": values[2],
        "write_ops": values[3],
        "read_bandwidth": values[4],
        "write_bandwidth": values[5],
    }
    if len(values) >= 8:
        result["read_wait"] = values[6]
        result["write_wait"] = values[7]
        result["total_wait"] = values[6] + values[7]
    return result


def parse_properties(output: str) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            _, prop, value = fields
            result[prop] = _number(value)
    required = {"used", "available", "compressratio", "usedbychildren"}
    missing = sorted(required - result.keys())
    if missing:
        raise ZfsCommandError(f"zfs get omitted properties: {', '.join(missing)}")
    return result


def parse_snapshot_usage(output: str) -> int:
    total = 0
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and "@" not in fields[0] and fields[1] != "-":
            try:
                total += int(fields[1])
            except ValueError as exc:
                raise ZfsCommandError(
                    f"invalid usedbysnapshots value for {fields[0]!r}: {fields[1]!r}"
                ) from exc
    return total


def parse_pool_list(output: str) -> tuple[str, float]:
    fields = output.split()
    if len(fields) < 3:
        raise ZfsCommandError("zpool list returned incomplete output")
    return fields[1], float(_number(fields[2])) / 100


def parse_status(output: str) -> tuple[str, str]:
    health = "UNKNOWN"
    detail = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("state:"):
            health = stripped.partition(":")[2].strip()
        elif stripped.startswith("scan:"):
            detail = stripped
    return health, detail


def run_command(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise ZfsCommandError(f"required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or "command failed").strip()
        raise ZfsCommandError(f"{' '.join(command)}: {message}") from exc
    return result.stdout


def collect_sample(
    pool: str,
    interval: float,
    runner: Callable[[Sequence[str]], str] = run_command,
) -> dict[str, int | float | str]:
    iostat = parse_iostat(
        runner(["zpool", "iostat", "-Hplvy", pool, str(interval), "1"]), pool
    )
    props = parse_properties(
        runner(
            [
                "zfs",
                "get",
                "-Hp",
                "-d",
                "0",
                "-o",
                "name,property,value",
                "used,available,compressratio,usedbychildren",
                pool,
            ]
        )
    )
    snapshots = parse_snapshot_usage(
        runner(["zfs", "get", "-Hpr", "-o", "name,value", "usedbysnapshots", pool])
    )
    health, fragmentation = parse_pool_list(
        runner(["zpool", "list", "-H", "-o", "name,health,frag", pool])
    )
    used = props["used"]
    free = props["available"]
    total = used + free
    return {
        **iostat,
        "health": health,
        "used": used,
        "free": free,
        "total": total,
        "capacity": used / total if total else 0,
        "compression_ratio": props["compressratio"],
        "compression": props["compressratio"] - 1,
        "children": props["usedbychildren"],
        "snapshots": snapshots,
        "fragmentation": fragmentation,
        "read": iostat["read_bandwidth"],
        "write": iostat["write_bandwidth"],
    }


def format_column(column: Column, value: float | str) -> str:
    if column.kind == "bytes":
        return format_bytes(float(value), column.unit, column.precision)
    if column.kind == "time":
        return format_time(float(value), column.unit, column.precision)
    if column.kind == "percent":
        return f"{float(value):.{column.precision}%}"
    if column.kind == "ratio":
        return f"{float(value):.{column.precision}f}x"
    if column.kind == "number":
        return f"{float(value):.{column.precision}f}"
    return str(value)


def format_row(
    columns: Sequence[Column], sample: dict[str, int | float | str]
) -> tuple[str, str]:
    rendered = [format_column_value(column, sample) for column in columns]
    widths = [
        max(len(column.header), len(value)) + 2
        for column, value in zip(columns, rendered)
    ]
    header = "".join(
        f"{column.header:<{width}}" for column, width in zip(columns, widths)
    ).rstrip()
    row = "".join(
        f"{value:<{width}}" for value, width in zip(rendered, widths)
    ).rstrip()
    return header, row


def format_column_value(column: Column, sample: dict[str, int | float | str]) -> str:
    if column.key not in sample:
        return "-"
    return format_column(column, sample[column.key])


def status_line(pool: str, runner: Callable[[Sequence[str]], str] = run_command) -> str:
    health, detail = parse_status(runner(["zpool", "status", pool]))
    suffix = f": {detail}" if detail else ""
    return f"zpool {pool} is {health}{suffix}"


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zpool-stats",
        description="Continuously print useful statistics for one local ZFS pool.",
    )
    parser.add_argument("pool", nargs="?", help="pool to monitor")
    parser.add_argument("-p", "--pool", dest="pool_option", help=argparse.SUPPRESS)
    parser.add_argument(
        "-i",
        "-t",
        "--interval",
        type=positive_float,
        default=1.0,
        help="seconds between samples (default: 1)",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=nonnegative_int,
        default=0,
        help="samples to print; 0 means forever (default: 0)",
    )
    parser.add_argument(
        "-c",
        "--columns",
        help="comma-separated columns, each optionally NAME:UNIT:PRECISION:HEADER",
    )
    parser.add_argument(
        "--list-columns",
        action="store_true",
        help="list available column names and exit",
    )
    parser.add_argument(
        "--no-status", action="store_true", help="do not print the pool status line"
    )
    parser.add_argument(
        "--header-every",
        type=nonnegative_int,
        default=None,
        metavar="N",
        help=(
            "repeat the header every N rows; by default use the current terminal "
            "height, and 0 disables repetition"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pool and args.pool_option and args.pool != args.pool_option:
        parser.error("POOL and --pool specify different pools")
    args.pool = args.pool or args.pool_option
    if not args.list_columns and not args.pool:
        parser.error("a pool name is required")
    try:
        args.parsed_columns = parse_columns(args.columns)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _require_tools() -> None:
    missing = [command for command in ("zpool", "zfs") if shutil.which(command) is None]
    if missing:
        raise ZfsCommandError(f"required command(s) not found: {', '.join(missing)}")


def monitor(args: argparse.Namespace) -> int:
    _require_tools()
    if not args.no_status:
        print(status_line(args.pool), flush=True)
    printed = 0
    rows_since_header = 0
    while args.count == 0 or printed < args.count:
        # `zpool iostat interval 1` blocks for the requested sampling window,
        # so adding a Python sleep here would double the configured interval.
        sample = collect_sample(args.pool, args.interval)
        header, row = format_row(args.parsed_columns, sample)
        repeat_every = header_interval(args.header_every)
        if printed == 0 or (repeat_every and rows_since_header >= repeat_every):
            print(header)
            rows_since_header = 0
        print(row, flush=True)
        printed += 1
        rows_since_header += 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_columns:
        for name, (header, kind) in COLUMN_SPECS.items():
            print(f"{name:<20} {kind:<8} default header: {header}")
        return 0
    try:
        return monitor(args)
    except ZfsCommandError as exc:
        print(f"zpool-stats: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
