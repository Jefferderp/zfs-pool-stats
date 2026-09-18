#!/usr/bin/env python3
"""A lightweight, dependency-free ZFS pool statistics monitor."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime

VERSION = "1.7.2"
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
ANSI_RESET = "\x1b[0m"
ANSI_BOLD_GREEN = "\x1b[1;32m"
ANSI_BOLD_RED = "\x1b[1;31m"
ANSI_BOLD_CYAN = "\x1b[1;36m"


class ZfsCommandError(RuntimeError):
    """A ZFS command failed or returned output the monitor cannot use."""


class SignalExit(Exception):
    """Request a conventional shell exit status for a received signal."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@dataclasses.dataclass(frozen=True)
class Column:
    key: str
    header: str
    kind: str
    unit: str | None = None
    precision: int = 1


@dataclasses.dataclass
class SnapshotCache:
    """Cache recursive snapshot totals independently for each pool."""

    values: dict[str, tuple[float, int]] = dataclasses.field(default_factory=dict)

    def get(
        self,
        pool: str,
        refresh_interval: float,
        runner: Callable[[Sequence[str]], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> int:
        now = clock()
        cached = self.values.get(pool)
        if (
            cached is None
            or refresh_interval == 0
            or now < cached[0]
            or now - cached[0] >= refresh_interval
        ):
            value = parse_snapshot_usage(
                runner(
                    [
                        "zfs",
                        "get",
                        "-Hpr",
                        "-o",
                        "name,value",
                        "usedbysnapshots",
                        pool,
                    ]
                )
            )
            self.values[pool] = (now, value)
            return value
        return cached[1]


COLUMN_SPECS = {
    "timestamp": ("timestamp", "label"),
    "unix_time": ("unix", "number"),
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

DEFAULT_COLUMNS = "pool,used,free,total,capacity::0,read,write,fragmentation::0,compression::0,snapshots"
IOSTAT_KEYS = {
    "logical_used",
    "logical_free",
    "read_ops",
    "write_ops",
    "read",
    "write",
    "read_wait",
    "write_wait",
    "total_wait",
}


def _number(value: str) -> int | float:
    value = value.strip().rstrip("x%")
    if value in {"", "-"}:
        return 0
    try:
        return int(value)
    except ValueError:
        try:
            number = float(value)
        except ValueError as exc:
            raise ZfsCommandError(f"invalid numeric value {value!r}") from exc
        if not math.isfinite(number):
            raise ZfsCommandError(f"non-finite numeric value {value!r}")
        return number


def _integer(value: str) -> int:
    value = value.strip()
    if value in {"", "-"}:
        return 0
    try:
        return int(value)
    except ValueError as exc:
        raise ZfsCommandError(f"invalid integer value {value!r}") from exc


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
    pool_columns = [column for column in columns if column.key == "pool"]
    if not pool_columns:
        default_header, kind = COLUMN_SPECS["pool"]
        pool_columns = [Column("pool", default_header, kind)]
    return pool_columns + [column for column in columns if column.key != "pool"]


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
    values = [_integer(value) for value in row[1:]]
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


def parse_properties(
    output: str, required: set[str] | None = None
) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            _, prop, value = fields
            result[prop] = (
                _number(value) if prop == "compressratio" else _integer(value)
            )
    required = required or {"used", "available", "compressratio", "usedbychildren"}
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


def parse_pool_properties(
    output: str, properties: Sequence[str]
) -> dict[str, str | float]:
    fields = output.split()
    if len(fields) < len(properties) + 1:
        raise ZfsCommandError("zpool list returned incomplete output")
    result: dict[str, str | float] = {}
    for prop, value in zip(properties, fields[1:]):
        result[prop] = float(_number(value)) / 100 if prop == "frag" else value
    return result


def parse_pool_list(output: str) -> tuple[str, float]:
    values = parse_pool_properties(output, ("health", "frag"))
    return str(values["health"]), float(values["frag"])


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


def run_command(command: Sequence[str], timeout: float | None = None) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ZfsCommandError(f"required command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ZfsCommandError(
            f"{' '.join(command)}: timed out after {timeout:g} seconds"
        ) from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or "command failed").strip()
        raise ZfsCommandError(f"{' '.join(command)}: {message}") from exc
    return result.stdout


def discover_pools(
    runner: Callable[[Sequence[str]], str] = run_command,
) -> list[str]:
    """Return imported pool names in the order reported by OpenZFS."""
    output = runner(["zpool", "list", "-H", "-o", "name"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def collect_sample(
    pool: str,
    interval: float,
    runner: Callable[[Sequence[str]], str] = run_command,
    *,
    columns: Sequence[Column] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    snapshot_cache: SnapshotCache | None = None,
    snapshot_refresh: float = 0,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    iostat_output: str | None = None,
    wait_for_interval: bool = True,
) -> dict[str, int | float | str]:
    requested = (
        set(COLUMN_SPECS) if columns is None else {column.key for column in columns}
    )
    sample: dict[str, int | float | str] = {"pool": pool}

    if requested & IOSTAT_KEYS:
        if iostat_output is None:
            iostat_output = runner(
                ["zpool", "iostat", "-Hplvy", pool, str(interval), "1"]
            )
        iostat = parse_iostat(
            iostat_output,
            pool,
        )
        aliases = {"read": "read_bandwidth", "write": "write_bandwidth"}
        for key in requested & IOSTAT_KEYS:
            source = aliases.get(key, key)
            if source in iostat:
                sample[key] = iostat[source]
    elif wait_for_interval:
        sleeper(interval)

    property_dependencies = {
        "used": {"used"},
        "free": {"available"},
        "total": {"used", "available"},
        "capacity": {"used", "available"},
        "compression": {"compressratio"},
        "compression_ratio": {"compressratio"},
        "children": {"usedbychildren"},
    }
    needed_properties = set().union(
        *(
            property_dependencies[key]
            for key in requested & property_dependencies.keys()
        )
    )
    if needed_properties:
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
                    ",".join(sorted(needed_properties)),
                    pool,
                ]
            ),
            needed_properties,
        )
        if "used" in requested:
            sample["used"] = props["used"]
        if "free" in requested:
            sample["free"] = props["available"]
        if requested & {"total", "capacity"}:
            total = props["used"] + props["available"]
            if "total" in requested:
                sample["total"] = total
            if "capacity" in requested:
                sample["capacity"] = props["used"] / total if total else 0
        if "compression_ratio" in requested:
            sample["compression_ratio"] = props["compressratio"]
        if "compression" in requested:
            sample["compression"] = round(float(props["compressratio"]) - 1, 12)
        if "children" in requested:
            sample["children"] = props["usedbychildren"]

    if "snapshots" in requested:
        cache = snapshot_cache or SnapshotCache()
        sample["snapshots"] = cache.get(pool, snapshot_refresh, runner, clock)

    if requested & {"health", "fragmentation"}:
        pool_properties = [
            prop
            for key, prop in (("health", "health"), ("fragmentation", "frag"))
            if key in requested
        ]
        pool_values = parse_pool_properties(
            runner(
                [
                    "zpool",
                    "list",
                    "-H",
                    "-o",
                    ",".join(("name", *pool_properties)),
                    pool,
                ]
            ),
            pool_properties,
        )
        if "health" in requested:
            sample["health"] = pool_values["health"]
        if "fragmentation" in requested:
            sample["fragmentation"] = pool_values["frag"]

    if requested & {"timestamp", "unix_time"}:
        sample_time = wall_clock()
        if "timestamp" in requested:
            sample["timestamp"] = (
                datetime.fromtimestamp(sample_time)
                .astimezone()
                .isoformat(timespec="seconds")
            )
        if "unix_time" in requested:
            sample["unix_time"] = sample_time

    return sample


def collect_sample_set(
    pools: Sequence[str],
    interval: float,
    runner: Callable[[Sequence[str]], str] = run_command,
    *,
    columns: Sequence[Column] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    snapshot_cache: SnapshotCache | None = None,
    snapshot_refresh: float = 0,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> list[dict[str, int | float | str]]:
    """Collect one interval-aligned sample for every selected pool."""
    if len(pools) == 1:
        return [
            collect_sample(
                pools[0],
                interval,
                runner=runner,
                columns=columns,
                sleeper=sleeper,
                snapshot_cache=snapshot_cache,
                snapshot_refresh=snapshot_refresh,
                clock=clock,
                wall_clock=wall_clock,
            )
        ]

    requested = (
        set(COLUMN_SPECS) if columns is None else {column.key for column in columns}
    )
    iostat_output = None
    if requested & IOSTAT_KEYS:
        iostat_output = runner(
            ["zpool", "iostat", "-Hplvy", *pools, str(interval), "1"]
        )
    else:
        sleeper(interval)

    return [
        collect_sample(
            pool,
            interval,
            runner=runner,
            columns=columns,
            sleeper=sleeper,
            snapshot_cache=snapshot_cache,
            snapshot_refresh=snapshot_refresh,
            clock=clock,
            wall_clock=wall_clock,
            iostat_output=iostat_output,
            wait_for_interval=False,
        )
        for pool in pools
    ]


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


class TableFormatter:
    """Render aligned rows while preserving maximum observed column widths."""

    def __init__(self, columns: Sequence[Column]) -> None:
        self.columns = tuple(columns)
        self.widths = [len(column.header) + 2 for column in self.columns]

    def format(self, sample: dict[str, int | float | str]) -> tuple[str, str, bool]:
        rendered = [format_column_value(column, sample) for column in self.columns]
        new_widths = [
            max(width, len(value) + 2) for width, value in zip(self.widths, rendered)
        ]
        expanded = new_widths != self.widths
        self.widths = new_widths
        header = "".join(
            f"{column.header:<{width}}"
            for column, width in zip(self.columns, self.widths)
        ).rstrip()
        row = "".join(
            f"{value:<{width}}" for value, width in zip(rendered, self.widths)
        ).rstrip()
        return header, row, expanded


def colors_enabled(mode: str, output=None) -> bool:
    """Resolve an explicit color mode or detect terminal color support."""
    output = sys.stdout if output is None else output
    if mode == "always":
        return True
    if mode == "never":
        return False
    return output.isatty() and "NO_COLOR" not in os.environ


def color_status(line: str, enabled: bool) -> str:
    """Color a pool status according to its health state."""
    if not enabled:
        return line
    color = ANSI_BOLD_GREEN if " is ONLINE" in line else ANSI_BOLD_RED
    return f"{color}{line}{ANSI_RESET}"


def color_header(line: str, enabled: bool) -> str:
    """Render table headings in bold cyan when color is enabled."""
    return f"{ANSI_BOLD_CYAN}{line}{ANSI_RESET}" if enabled else line


class StickyTableRenderer:
    """Redraw a terminal viewport with fixed status and heading rows."""

    def __init__(
        self,
        output,
        status_lines: Sequence[str],
        *,
        color: bool,
        pools: Sequence[str] = (),
        terminal_size: Callable[[], os.terminal_size] = shutil.get_terminal_size,
    ) -> None:
        self.output = output
        self.status_lines = tuple(status_lines)
        self.pools = tuple(pools)
        self.pool_rows: dict[str, list[str]] = {pool: [] for pool in pools}
        self.color = color
        self.terminal_size = terminal_size
        self.rows: list[str] = []
        self.header: str | None = None
        self.last_line_count = 0
        self.started = False
        self.closed = False

    def draw(self, header: str, row: str, *, pool: str | None = None) -> None:
        if self.pools:
            self.draw_pool(header, row, pool=pool)
            return
        size = self.terminal_size()
        width = max(1, size.columns)
        height = max(1, size.lines)
        displayed_status_lines = self.status_lines[: max(0, height - 2)]
        available_rows = max(0, height - len(displayed_status_lines) - 1)
        if self.header is not None and header != self.header:
            self.rows.clear()
        self.header = header
        self.rows.append(row)
        if len(self.rows) > 10_000:
            del self.rows[: len(self.rows) - 10_000]

        visible_status = [
            color_status(line[:width], self.color) for line in displayed_status_lines
        ]
        visible_header = color_header(header[:width], self.color)
        visible_rows = (
            [line[:width] for line in self.rows[-available_rows:]]
            if available_rows
            else []
        )
        lines = [*visible_status, visible_header, *visible_rows]
        self.last_line_count = len(lines)
        prefix = "\x1b[?25l\x1b[H" if not self.started else "\x1b[H"
        frame = "\r\n".join(f"{line}\x1b[K" for line in lines)
        self.output.write(f"{prefix}{frame}\x1b[J")
        self.output.flush()
        self.started = True

    def draw_pool(self, header: str, row: str, *, pool: str | None) -> None:
        if pool not in self.pool_rows:
            raise ValueError(f"unknown display pool: {pool}")
        if self.header is not None and self.header != header:
            for history in self.pool_rows.values():
                history.clear()
        self.header = header
        rows = self.pool_rows[pool]
        rows.append(row)
        del rows[:-10_000]
        size = self.terminal_size()
        height, width = max(1, size.lines), max(1, size.columns)
        # Reserve a shared heading, one row per pool, and blank separators.
        # Drop statuses first on short terminals, then show a stable pool subset.
        status_budget = max(0, height - 2 * len(self.pools))
        lines = [
            color_status(line[:width], self.color)
            for line in self.status_lines[:status_budget]
        ]
        lines.append(color_header(header[:width], self.color))
        remaining = height - len(lines)
        visible = self.pools[: (remaining + 1) // 2]
        if visible:
            budget, remainder = divmod(remaining - len(visible) + 1, len(visible))
            for index, name in enumerate(visible):
                if index:
                    lines.append("")
                section_height = budget + (index < remainder)
                section = [
                    line[:width] for line in self.pool_rows[name][-section_height:]
                ]
                section.extend([""] * (section_height - len(section)))
                lines.extend(section)
        self.last_line_count = len(lines)
        prefix = "\x1b[?25l\x1b[H" if not self.started else "\x1b[H"
        frame = "\r\n".join(f"{line}\x1b[K" for line in lines)
        self.output.write(f"{prefix}{frame}\x1b[J")
        self.output.flush()
        self.started = True

    def close(self) -> None:
        if self.closed:
            return
        if self.started:
            self.output.write(f"\x1b[{self.last_line_count};1H\x1b[?25h\n")
            self.output.flush()
        self.closed = True


def format_row(
    columns: Sequence[Column], sample: dict[str, int | float | str]
) -> tuple[str, str]:
    header, row, _ = TableFormatter(columns).format(sample)
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


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and zero or greater")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zpool-stats",
        description="Continuously print useful statistics for local ZFS pools.",
    )
    parser.add_argument(
        "pool", nargs="?", help="pool to monitor; omit to discover all imported pools"
    )
    parser.add_argument("-p", "--pool", dest="pool_option", help=argparse.SUPPRESS)
    parser.add_argument(
        "-a",
        "--all",
        dest="all_pools",
        action="store_true",
        help="monitor all imported pools (default when POOL is omitted)",
    )
    parser.add_argument(
        "-i",
        "-t",
        "--interval",
        type=positive_float,
        default=10.0,
        help="seconds between samples (default: 10)",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=nonnegative_int,
        default=0,
        help="sample sets to print for all selected pools; 0 means forever (default: 0)",
    )
    parser.add_argument(
        "-c",
        "--columns",
        help="comma-separated columns, each optionally NAME:UNIT:PRECISION:HEADER",
    )
    parser.add_argument(
        "--format",
        choices=("table", "csv", "tsv", "jsonl"),
        default="table",
        help="output format; CSV, TSV, and JSON Lines use raw values (default: table)",
    )
    parser.add_argument(
        "--list-columns",
        action="store_true",
        help="list available column names and exit",
    )
    parser.add_argument(
        "--list-pools",
        action="store_true",
        help="list imported ZFS pool names and exit",
    )
    parser.add_argument(
        "--no-status", action="store_true", help="do not print the pool status line"
    )
    parser.add_argument(
        "--snapshot-refresh",
        type=nonnegative_float,
        default=60.0,
        metavar="SECONDS",
        help="seconds to cache recursive snapshot usage; 0 disables caching (default: 60)",
    )
    parser.add_argument(
        "--command-timeout",
        type=positive_float,
        default=30.0,
        metavar="SECONDS",
        help=(
            "maximum runtime for each ZFS command, excluding the requested "
            "iostat sampling delay (default: 30)"
        ),
    )
    parser.add_argument(
        "--header-every",
        type=nonnegative_int,
        default=None,
        metavar="N",
        help=("disable the sticky header and repeat it every N rows; 0 prints it once"),
    )
    parser.add_argument(
        "--sticky-header",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "keep status and column headings at the top of an interactive "
            "terminal (default)"
        ),
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize table status and headings (default: auto)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pool and args.pool_option and args.pool != args.pool_option:
        parser.error("POOL and --pool specify different pools")
    args.pool = args.pool or args.pool_option
    if args.all_pools and args.pool:
        parser.error("POOL cannot be used with --all")
    if args.list_pools and args.pool:
        parser.error("POOL cannot be used with --list-pools")
    try:
        args.parsed_columns = parse_columns(args.columns)
    except ValueError as exc:
        parser.error(str(exc))
    if args.format == "jsonl":
        keys = [column.key for column in args.parsed_columns]
        if len(keys) != len(set(keys)):
            parser.error("JSON Lines output requires unique column names")
    return args


def _require_tools(commands: Sequence[str] = ("zpool", "zfs")) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise ZfsCommandError(f"required command(s) not found: {', '.join(missing)}")


def _validate_sample(sample: dict[str, int | float | str]) -> None:
    for key, value in sample.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ZfsCommandError(f"non-finite value for column {key!r}")


def monitor(args: argparse.Namespace) -> int:
    _require_tools()

    def command_runner(command: Sequence[str]) -> str:
        timeout = args.command_timeout
        if list(command[:2]) == ["zpool", "iostat"]:
            timeout += args.interval
        return run_command(command, timeout=timeout)

    pools = [args.pool] if args.pool else discover_pools(command_runner)
    if not pools:
        raise ZfsCommandError("no imported ZFS pools found")

    status_lines = []
    if args.format == "table" and not args.no_status:
        status_lines = [status_line(pool, command_runner) for pool in pools]
    use_sticky_header = (
        args.format == "table"
        and args.sticky_header
        and args.header_every is None
        and sys.stdout.isatty()
    )
    use_color = args.format == "table" and colors_enabled(args.color)
    sticky_renderer = (
        StickyTableRenderer(sys.stdout, status_lines, color=use_color, pools=pools)
        if use_sticky_header
        else None
    )
    if sticky_renderer is None:
        for line in status_lines:
            print(color_status(line, use_color), flush=True)

    samples_printed = 0
    rows_since_header = 0
    snapshot_cache = SnapshotCache()
    formatter = TableFormatter(args.parsed_columns) if args.format == "table" else None
    delimited_writer = None
    if args.format in {"csv", "tsv"}:
        delimiter = "," if args.format == "csv" else "\t"
        delimited_writer = csv.writer(
            sys.stdout, delimiter=delimiter, lineterminator="\n"
        )
        delimited_writer.writerow(column.key for column in args.parsed_columns)
        sys.stdout.flush()

    try:
        while args.count == 0 or samples_printed < args.count:
            samples = collect_sample_set(
                pools,
                args.interval,
                runner=command_runner,
                columns=args.parsed_columns,
                snapshot_cache=snapshot_cache,
                snapshot_refresh=args.snapshot_refresh,
            )
            for sample_index, sample in enumerate(samples):
                _validate_sample(sample)
                if args.format == "table":
                    assert formatter is not None
                    header, row, widths_expanded = formatter.format(sample)
                    if sticky_renderer is not None:
                        sticky_renderer.draw(header, row, pool=str(sample["pool"]))
                    else:
                        repeat_every = args.header_every or 0
                        if (
                            samples_printed == 0
                            and sample_index == 0
                            or (widths_expanded and args.header_every is None)
                            or (repeat_every and rows_since_header >= repeat_every)
                        ):
                            print(color_header(header, use_color))
                            rows_since_header = 0
                        print(row, flush=True)
                        rows_since_header += 1
                elif args.format in {"csv", "tsv"}:
                    assert delimited_writer is not None
                    delimited_writer.writerow(
                        "" if sample.get(column.key) is None else sample[column.key]
                        for column in args.parsed_columns
                    )
                    sys.stdout.flush()
                else:
                    record = {
                        column.key: sample.get(column.key)
                        for column in args.parsed_columns
                    }
                    print(
                        json.dumps(record, separators=(",", ":"), allow_nan=False),
                        flush=True,
                    )
            samples_printed += 1
    finally:
        if sticky_renderer is not None:
            sticky_renderer.close()
    return 0


def _discard_stdout() -> None:
    """Redirect stdout so interpreter shutdown cannot flush a broken pipe."""
    try:
        with open(os.devnull, "w") as devnull:
            os.dup2(devnull.fileno(), sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    previous_handlers = {}

    def handle_signal(signum, _frame) -> None:
        raise SignalExit(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, handle_signal)
    except ValueError:
        # Python only permits signal registration from the main thread.
        previous_handlers.clear()
    try:
        args = parse_args(argv)
        if args.list_columns:
            for name, (header, kind) in COLUMN_SPECS.items():
                print(f"{name:<20} {kind:<8} default header: {header}", flush=True)
            return 0
        if args.list_pools:
            _require_tools(("zpool",))
            pools = discover_pools(
                lambda command: run_command(command, timeout=args.command_timeout)
            )
            for pool in pools:
                print(pool, flush=True)
            return 0
        return monitor(args)
    except BrokenPipeError:
        _discard_stdout()
        return 0
    except ZfsCommandError as exc:
        print(f"zpool-stats: error: {exc}", file=sys.stderr)
        return 1
    except SignalExit as exc:
        return 128 + exc.signum
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
