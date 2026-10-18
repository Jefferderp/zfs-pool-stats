#!/usr/bin/env python3
"""A lightweight, dependency-free ZFS pool statistics monitor."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TypeVar

Value = int | float | str | None
T = TypeVar("T")

VERSION = "1.9.3"
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
ANSI_BOLD_YELLOW = "\x1b[1;33m"
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

    values: dict[str, tuple[float, int | None]] = dataclasses.field(
        default_factory=dict
    )

    def get(
        self,
        pool: str,
        refresh_interval: float,
        runner: Callable[[Sequence[str]], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> int | None:
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
                        "-t",
                        "filesystem,volume",
                        "-o",
                        "name,value",
                        "usedbysnapshots",
                        pool,
                    ]
                )
            )
            self.values[pool] = (clock(), value)
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
    "compression_ratio": ("ratio(x)", "ratio"),
    "children": ("child", "bytes"),
    "snapshots": ("snap", "bytes"),
    "snapshot_age": ("snap_age_s", "number"),
    "sample_age": ("age_s", "number"),
    "collection_duration": ("collect_s", "number"),
}

DEFAULT_COLUMNS = "pool,free,used,total,capacity::0,read,write,fragmentation::0,compression_ratio::2:ratio,snapshots"
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


def _number(value: str) -> int | float | None:
    value = value.strip().rstrip("x%")
    if value in {"", "-"}:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            number = float(value)
        except ValueError as exc:
            raise ZfsCommandError(f"invalid numeric value {value!r}") from exc
        if not math.isfinite(number):
            raise ZfsCommandError(f"non-finite numeric value {value!r}") from None
        return number


def _integer(value: str) -> int | None:
    value = value.strip()
    if value in {"", "-"}:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ZfsCommandError(f"invalid integer value {value!r}") from exc


def format_bytes(
    value: float, unit: str | None = None, precision: int = 1, *, base: int = 1024
) -> str:
    value = float(value)
    if unit is None:
        index = (
            0
            if value == 0
            else max(0, min(int(math.log(abs(value), base)), len(BYTE_UNITS) - 1))
        )
        unit = BYTE_UNITS[index]
    else:
        unit = unit.upper()
        if unit not in BYTE_UNITS:
            raise ValueError(
                f"invalid byte unit {unit!r}; choose from {', '.join(BYTE_UNITS)}"
            )
        index = BYTE_UNITS.index(unit)
    scaled = value / (base**index)
    if precision == 1 and abs(scaled) >= 100:
        rounded = int(math.copysign(math.floor(abs(scaled) + 0.5), scaled))
        return f"{rounded}{unit}"
    if unit == "B" and precision == 1:
        precision = 0
    return f"{scaled:.{precision}f}{unit}"


def format_time(value: float, unit: str | None = None, precision: int = 1) -> str:
    value = float(value)
    units = dict(TIME_UNITS)
    if unit is None:
        unit, divisor = next(
            ((name, size) for name, size in TIME_UNITS if abs(value) >= size), ("ns", 1)
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
            precision = (
                int(parts[2])
                if len(parts) > 2 and parts[2]
                else (0 if kind == "percent" else 1)
            )
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


def parse_iostat(output: str, pool: str) -> dict[str, Value]:
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
        result["total_wait"] = (
            values[6] + values[7]
            if values[6] is not None and values[7] is not None
            else None
        )
    return result


def parse_properties(
    output: str, required: set[str] | None = None
) -> dict[str, int | float | None]:
    result: dict[str, int | float | None] = {}
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


def parse_snapshot_usage(output: str) -> int | None:
    total = 0
    found = False
    for line in output.splitlines():
        if not line.strip() or "@" in line.split("\t")[0]:
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ZfsCommandError(f"incomplete snapshot usage row: {line!r}")
        if len(fields) == 2 and "@" not in fields[0] and fields[1] in {"", "-"}:
            return None
        if len(fields) == 2 and "@" not in fields[0] and fields[1] != "-":
            try:
                total += int(fields[1])
                found = True
            except ValueError as exc:
                raise ZfsCommandError(
                    f"invalid usedbysnapshots value for {fields[0]!r}: {fields[1]!r}"
                ) from exc
    return total if found else None


def parse_pool_properties(
    output: str, properties: Sequence[str]
) -> dict[str, str | float | None]:
    fields = output.split()
    if len(fields) < len(properties) + 1:
        raise ZfsCommandError("zpool list returned incomplete output")
    result: dict[str, str | float | None] = {}
    for prop, value in zip(properties, fields[1:]):
        numeric = _number(value) if prop == "frag" else None
        result[prop] = (
            (numeric / 100 if numeric is not None else None)
            if prop == "frag"
            else value
        )
    return result


def parse_pool_list(output: str) -> tuple[str, float | None]:
    values = parse_pool_properties(output, ("health", "frag"))
    return str(values["health"]), float(values["frag"]) if values[
        "frag"
    ] is not None else None


def parse_status(output: str) -> tuple[str, str]:
    """Keep scan continuations and report errors independently of pool health."""
    health = "UNKNOWN"
    scan = []
    errors = []
    devices = []
    diagnostics = []
    section = ""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("state:"):
            health = stripped.partition(":")[2].strip()
            section = ""
        elif stripped.startswith(("status:", "action:")):
            diagnostics.append(stripped)
            section = "diagnostic"
        elif stripped.startswith("scan:"):
            scan.append(stripped)
            section = "scan"
        elif stripped.startswith("errors:"):
            errors.append(stripped)
            section = "errors"
        elif stripped.startswith("config:"):
            section = "config"
        elif section == "config":
            fields = stripped.split()
            if len(fields) >= 5 and fields[1] in {
                "ONLINE",
                "DEGRADED",
                "FAULTED",
                "OFFLINE",
                "UNAVAIL",
                "REMOVED",
                "AVAIL",
            }:
                counters = fields[2:5]
                if any(value not in {"0", "0.0", "-"} for value in counters):
                    devices.append(f"{fields[0]} READ/WRITE/CKSUM={'/'.join(counters)}")
        elif section == "diagnostic" and line[:1].isspace():
            diagnostics.append(stripped)
        elif section == "scan" and line[:1].isspace():
            scan.append(stripped)
        elif section == "errors" and line[:1].isspace():
            errors.append(stripped)
        else:
            section = ""
    attention = bool(devices) or any("No known data errors" not in e for e in errors)
    detail = [*diagnostics, *errors, *devices, *scan]
    if attention:
        detail.insert(0, "ATTENTION")
    elif diagnostics:
        detail.insert(0, "ADVISORY")
    return health, "; ".join(detail)


_command_controller = None
_command_controller_lock = threading.Lock()


def run_command(command: Sequence[str], timeout: float | None = None) -> str:
    global _command_controller
    with _command_controller_lock:
        if _command_controller is None or _command_controller.stop.is_set():
            _command_controller = CommandController()
        controller = _command_controller
    return controller.run(command, timeout)


def discover_pools(
    runner: Callable[[Sequence[str]], str] = run_command,
) -> list[str]:
    """Return imported pool names in the order reported by OpenZFS."""
    output = runner(["zpool", "list", "-H", "-o", "name"])
    return [line.strip() for line in output.splitlines() if line.strip()]


@dataclasses.dataclass
class RetryState:
    """Per-operation exponential retry backoff; failed values are unavailable."""

    clock: Callable[[], float] = time.monotonic
    failures: dict[tuple, tuple[float, float]] = dataclasses.field(default_factory=dict)

    def call(self, key: tuple, operation: Callable[[], T]) -> T | None:
        deadline, delay = self.failures.get(key, (0, 0))
        if self.clock() < deadline:
            return None
        try:
            value = operation()
        except ZfsCommandError as exc:
            delay = min(300, max(10, delay * 2))
            self.failures[key] = (self.clock() + delay, delay)
            print(
                f"zpool-stats: warning: {key}: {exc}; unavailable, retry in {delay}s",
                file=sys.stderr,
            )
            return None
        self.failures.pop(key, None)
        return value


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
    retry_state: RetryState | None = None,
    skip_iostat: bool = False,
    iostat_completed: float | None = None,
) -> dict[str, Value]:
    started = clock()
    measurements = []
    requested = set(COLUMN_SPECS) if columns is None else {c.key for c in columns}
    state = retry_state or RetryState(clock=clock)
    sample: dict[str, Value] = dict.fromkeys(requested)
    sample["pool"] = pool
    groups = [
        IOSTAT_KEYS,
        {
            "used",
            "free",
            "total",
            "capacity",
            "compression",
            "compression_ratio",
            "children",
        },
        {"snapshots", "snapshot_age"},
        {"health", "fragmentation"},
        {"timestamp", "unix_time"},
    ]
    if not requested & IOSTAT_KEYS and wait_for_interval:
        sleeper(interval)
    for group in groups:
        if group == IOSTAT_KEYS and skip_iostat:
            continue
        keys = requested & group
        if not keys:
            continue
        result = state.call(
            (pool, tuple(sorted(group))),
            lambda keys=keys: _collect_sample(
                pool,
                interval,
                runner,
                columns=[Column(k, *COLUMN_SPECS[k]) for k in keys],
                sleeper=sleeper,
                snapshot_cache=snapshot_cache,
                snapshot_refresh=snapshot_refresh,
                clock=clock,
                wall_clock=wall_clock,
                iostat_output=iostat_output,
                wait_for_interval=False,
            ),
        )
        if result is not None:
            sample.update(result)
            if group not in (
                {"snapshots", "snapshot_age"},
                {"timestamp", "unix_time"},
            ) and any(result.get(k) is not None for k in keys):
                measurements.append(
                    iostat_completed
                    if group == IOSTAT_KEYS and iostat_completed is not None
                    else clock()
                )
        elif group == IOSTAT_KEYS and wait_for_interval:
            sleeper(max(0, interval - (clock() - started)))
    completed = clock()
    if "sample_age" in requested:
        sample["_measured_at"] = min(measurements) if measurements else None
    if "collection_duration" in requested:
        sample["collection_duration"] = max(0, completed - started)
    if "sample_age" in requested:
        sample["sample_age"] = (
            max(0, completed - sample["_measured_at"]) if measurements else None
        )
    return sample


def _collect_sample(
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
) -> dict[str, Value]:
    requested = (
        set(COLUMN_SPECS) if columns is None else {column.key for column in columns}
    )
    sample: dict[str, Value] = {"pool": pool}

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
            total = (
                props["used"] + props["available"]
                if props["used"] is not None and props["available"] is not None
                else None
            )
            if "total" in requested:
                sample["total"] = total
            if "capacity" in requested:
                sample["capacity"] = (
                    props["used"] / total
                    if total and props["used"] is not None
                    else None
                )
        if "compression_ratio" in requested:
            sample["compression_ratio"] = props["compressratio"]
        if "compression" in requested:
            sample["compression"] = (
                round(float(props["compressratio"]) - 1, 12)
                if props["compressratio"] is not None
                else None
            )
        if "children" in requested:
            sample["children"] = props["usedbychildren"]

    if requested & {"snapshots", "snapshot_age"}:
        cache = snapshot_cache or SnapshotCache()
        sample["snapshots"] = cache.get(pool, snapshot_refresh, runner, clock)
        sample["_snapshot_at"] = cache.values[pool][0]
        sample["snapshot_age"] = (
            max(0, clock() - cache.values[pool][0])
            if sample["snapshots"] is not None
            else None
        )

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
    retry_state: RetryState | None = None,
) -> list[dict[str, Value]]:
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
                retry_state=retry_state,
            )
        ]

    requested = (
        set(COLUMN_SPECS) if columns is None else {column.key for column in columns}
    )
    iostat_output = None
    iostat_completed = None
    skip_iostat = False
    state = retry_state or RetryState(clock=clock)
    started = clock()
    if requested & IOSTAT_KEYS:
        iostat_output = state.call(
            (tuple(pools), "iostat"),
            lambda: runner(["zpool", "iostat", "-Hplvy", *pools, str(interval), "1"]),
        )
        iostat_completed = clock()
        if iostat_output is None:
            skip_iostat = True
            sleeper(max(0, interval - (clock() - started)))
    else:
        sleeper(interval)

    samples = [
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
            retry_state=state,
            skip_iostat=skip_iostat,
            iostat_completed=iostat_completed,
        )
        for pool in pools
    ]
    duration = max(0, clock() - started)
    for sample in samples:
        if "collection_duration" in requested:
            sample["collection_duration"] = duration
        if "sample_age" in requested and sample.get("_measured_at") is not None:
            sample["sample_age"] = max(0, clock() - sample["_measured_at"])
    return samples


def format_column(column: Column, value: float | str, *, base: int = 1024) -> str:
    if column.kind == "bytes":
        return format_bytes(float(value), column.unit, column.precision, base=base)
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

    def __init__(self, columns: Sequence[Column], *, base: int = 1024) -> None:
        self.base = base
        self.columns = tuple(columns)
        self.widths = [len(column.header) + 2 for column in self.columns]

    def format(self, sample: dict[str, Value]) -> tuple[str, str, bool]:
        rendered = [
            format_column_value(column, sample, base=self.base)
            for column in self.columns
        ]
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
    if ("ATTENTION" in line or "ADVISORY" in line) and " is ONLINE" in line:
        color = ANSI_BOLD_YELLOW
    elif " is ONLINE" in line:
        color = ANSI_BOLD_GREEN
    elif " is DEGRADED" in line:
        color = ANSI_BOLD_YELLOW
    else:
        color = ANSI_BOLD_RED
    return f"{color}{line}{ANSI_RESET}"


def color_header(line: str, enabled: bool) -> str:
    """Render table headings in bold cyan when color is enabled."""
    return f"{ANSI_BOLD_CYAN}{line}{ANSI_RESET}" if enabled else line


def wrapped_status(lines: Sequence[str], width: int, color: bool) -> list[str]:
    result = []
    for line in lines:
        colored = color_status(line, color)
        prefix = colored[: len(colored) - len(line) - len(ANSI_RESET)] if color else ""
        for part in textwrap.wrap(line, width=width):
            result.append(f"{prefix}{part}{ANSI_RESET}" if color else part)
    return result


def compact_status(status: str) -> str:
    """Keep health, scan progress, and notable problems on one line."""
    summary, _, detail = status.partition(":")
    health = summary.rsplit(" is ", 1)[-1]
    serious = health not in {"ONLINE"} or "UNAVAILABLE" in summary
    for marker in ("ATTENTION", "ADVISORY"):
        if marker in detail and marker not in summary:
            summary += f" with {marker}" if summary.endswith("ONLINE") else f" {marker}"
            reason = re.search(r"status:\s*([^;]+)", detail)
            if reason and marker == "ATTENTION":
                text = reason.group(1).strip()
                summary += f" ({text[:40]}…)" if len(text) > 40 else f" ({text})"
    errors = re.search(r"errors?:\s*([^;]+)", detail)
    if errors and errors.group(1).strip() != "No known data errors":
        serious = True
    if serious and detail.strip():
        return f"{summary}: {detail.strip()}" if ":" not in summary else status
    scan = re.search(r"scan:\s*([^;]+)", detail)
    if scan:
        text = scan.group(1).strip()
        if "in progress" in text:
            text = text.split(" in progress", 1)[0] + " in progress"
            progress = re.search(r"[\d.]+% done[^;]*", detail)
            if progress:
                text += ", " + progress.group(0)
        summary += ": " + text
    return summary


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
        detailed_status: bool = False,
    ) -> None:
        self.detailed_status = detailed_status
        self.output = output
        self.status_lines = tuple(status_lines)
        self.pools = tuple(pools)
        self.pool_rows: dict[str, list[str]] = {pool: [] for pool in pools}
        self.samples: dict[str, list[dict]] = {pool: [] for pool in pools}
        self.color = color
        self.terminal_size = terminal_size
        self.rows: list[str] = []
        self.header: str | None = None
        self.last_line_count = 0
        self.started = False
        self.closed = False

    def draw_sample(self, sample: dict, formatter: TableFormatter) -> None:
        """Retain structured history and reformat every pool at current widths."""
        pool = str(sample["pool"])
        history = self.samples.setdefault(pool, [])
        history.append(dict(sample))
        del history[:-10_000]
        self.redraw(formatter)

    def redraw(self, formatter: TableFormatter) -> None:
        """Reformat only viewport candidates, twice to stabilize column widths."""
        _, sections, _ = self.layout()
        records = {
            name: self.samples.get(name, [])[-count:] if count else []
            for name, count, _ in sections
        }
        for history in records.values():
            for record in history:
                formatter.format(record)
        self.pool_rows = {name: [] for name in self.pools}
        for name, history in records.items():
            self.pool_rows[name] = [formatter.format(record)[1] for record in history]
        header = formatter.format({})[0]
        self.header = header
        if records:
            name = next(iter(records))
            row = self.pool_rows[name].pop() if self.pool_rows[name] else ""
            self.draw_pool(header, row, pool=name)
        else:
            self.draw("Waiting for imported ZFS pools / collecting", "")

    def draw(self, header: str, row: str, *, pool: str | None = None) -> None:
        if self.pools:
            self.draw_pool(header, row, pool=pool)
            return
        size = self.terminal_size()
        width = max(1, size.columns)
        height = max(1, size.lines)
        displayed_status_lines = wrapped_status(self.status_lines, width, self.color)[
            : max(0, height - 2)
        ]
        available_rows = max(0, height - len(displayed_status_lines) - 1)
        if self.header is not None and header != self.header:
            self.rows.clear()
        self.header = header
        self.rows.append(row)
        if len(self.rows) > 10_000:
            del self.rows[: len(self.rows) - 10_000]

        visible_status = displayed_status_lines
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

    def layout(self):
        """One layout calculation shared by viewport selection and painting."""
        size = self.terminal_size()
        height, width = max(1, size.lines), max(1, size.columns)
        visible_count = min(
            len(self.pools), max(1, height // (3 if self.status_lines else 2))
        )
        visible = self.pools[:visible_count]
        summaries, details = [], []
        for index, name in enumerate(visible):
            if not self.status_lines:
                continue
            fallback = (
                self.status_lines[index]
                if index < len(self.status_lines)
                else f"zpool {name} is UNAVAILABLE"
            )
            status = next(
                (
                    line
                    for line in self.status_lines
                    if line.startswith(f"zpool {name} is ")
                ),
                fallback,
            )
            summary, _, detail = status.partition(":")
            for marker in ("ATTENTION", "ADVISORY"):
                if marker in detail and marker not in summary:
                    summary += (
                        f" with {marker}"
                        if summary.endswith("ONLINE")
                        else f" {marker}"
                    )
            colored = color_status(status, self.color)
            prefix = (
                colored[: len(colored) - len(status) - len(ANSI_RESET)]
                if self.color
                else ""
            )

            def tint(text, prefix=prefix):
                return f"{prefix}{text}{ANSI_RESET}" if self.color else text

            if not self.detailed_status:
                summary = compact_status(status)
            summaries.append(tint(summary[:width]))
            if self.detailed_status:
                for paragraph in detail.split(";"):
                    details.extend(
                        tint(part)
                        for part in textwrap.wrap(f"{name}: {paragraph.strip()}", width)
                    )
        detail_budget = max(0, height - len(summaries) - 2 * len(visible))
        lines = summaries[:height] + details[:detail_budget]
        remaining = max(0, height - len(lines))
        sections = []
        if visible:
            budget, remainder = divmod(remaining, len(visible))
            for index, name in enumerate(visible):
                section_height = budget + (index < remainder)
                sections.append((name, max(0, section_height - 1), section_height > 0))
        return lines, sections, width

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
        lines, sections, width = self.layout()
        for name, row_count, heading in sections:
            if heading:
                lines.append(color_header(header[:width], self.color))
            section = (
                [line[:width] for line in self.pool_rows[name][-row_count:]]
                if row_count
                else []
            )
            section.extend([""] * (row_count - len(section)))
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


def format_row(columns: Sequence[Column], sample: dict[str, Value]) -> tuple[str, str]:
    header, row, _ = TableFormatter(columns).format(sample)
    return header, row


def format_column_value(
    column: Column, sample: dict[str, Value], *, base: int = 1024
) -> str:
    value = sample.get(column.key)
    origin = {"sample_age": "_measured_at", "snapshot_age": "_snapshot_at"}.get(
        column.key
    )
    if value is not None and sample.get(origin) is not None:
        value = max(0, time.monotonic() - sample[origin])
    if value is None:
        return "-"
    return format_column(column, value, base=base)


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
        help="iostat sampling window (or minimum delay without I/O), plus collection overhead; not a refresh guarantee (default: 10 seconds)",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=nonnegative_int,
        default=0,
        help="streaming cycles / interactive attempts per query family (including status); 0 means forever",
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
        "--detailed-status",
        action="store_true",
        help="show full pool diagnostics instead of compact status summaries",
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
        "--pool-refresh",
        type=nonnegative_float,
        default=60.0,
        help="rediscover pools between cycles after this many seconds; 0 means every cycle (default: 60)",
    )
    parser.add_argument(
        "--command-timeout",
        type=positive_float,
        default=300.0,
        metavar="SECONDS",
        help=(
            "maximum runtime for each ZFS command, excluding the requested "
            "iostat sampling delay (default: 300)"
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
    units = parser.add_mutually_exclusive_group()
    units.add_argument(
        "--decimal",
        dest="byte_base",
        action="store_const",
        const=1000,
        help="display byte quantities in base-1000 units (default)",
    )
    units.add_argument(
        "--binary",
        dest="byte_base",
        action="store_const",
        const=1024,
        help="display byte quantities in base-1024 units",
    )
    parser.set_defaults(byte_base=1000)
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


def _validate_sample(sample: dict[str, Value]) -> None:
    for key, value in sample.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ZfsCommandError(f"non-finite value for column {key!r}")


class CommandController:
    """Bounded polling, process-group cancellation, no blocking wait on teardown."""

    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.children = {}

    def run(self, command, timeout=None):
        key = tuple(command)
        with self.lock:
            if self.stop.is_set():
                raise ZfsCommandError("collection stopped")
            self.children = {
                key: child
                for key, child in self.children.items()
                if child.poll() is None
            }
            if len(self.children) >= 4:
                raise ZfsCommandError(
                    "four commands still outstanding; collection unavailable"
                )
            previous = self.children.get(key)
            if previous and previous.poll() is None:
                raise ZfsCommandError("previous command still outstanding; unavailable")
            # Ownership spans the spawn lock and polling; closed in both failure paths.
            output = tempfile.TemporaryFile()  # noqa: SIM115
            errors = tempfile.TemporaryFile()  # noqa: SIM115
            try:
                child = subprocess.Popen(
                    command,
                    stdout=output,
                    stderr=errors,
                    start_new_session=True,
                    env={**os.environ, "LC_ALL": "C"},
                )
            except OSError as exc:
                output.close()
                errors.close()
                raise ZfsCommandError(f"{' '.join(command)}: {exc}") from exc
            self.children[key] = child
        started = time.monotonic()
        try:
            while child.poll() is None:
                if self.stop.wait(0.02) or (
                    timeout is not None and time.monotonic() - started >= timeout
                ):
                    self.kill(child)
                    raise ZfsCommandError(
                        f"{' '.join(command)}: timed out after {timeout:g} seconds"
                        if timeout is not None
                        else "collection stopped"
                    )
            output.seek(0)
            errors.seek(0)
            stdout = output.read().decode(errors="replace")
            stderr = errors.read().decode(errors="replace")
            if child.returncode:
                raise ZfsCommandError(
                    f"{' '.join(command)}: {(stderr or stdout or 'command failed').strip()}"
                )
            return stdout
        finally:
            output.close()
            errors.close()
            with self.lock:
                if child.poll() is not None and self.children.get(key) is child:
                    self.children.pop(key, None)

    @staticmethod
    def kill(child):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        self.stop.set()
        # A worker stuck inside Popen must never hold shutdown hostage.
        if self.lock.acquire(timeout=0.05):
            try:
                for child in self.children.values():
                    self.kill(child)
            finally:
                self.lock.release()


class BoundedWorkers:
    """Fixed daemon workers; no executor atexit joins or replacement threads."""

    def __init__(self, limit=4):
        self.tasks = queue.Queue(maxsize=limit)
        self.results = queue.Queue(maxsize=limit)
        self.stop = threading.Event()
        self.threads = []
        for _ in range(limit):
            thread = threading.Thread(target=self.work, daemon=True)
            thread.start()
            self.threads.append(thread)

    def work(self):
        while not self.stop.is_set():
            try:
                key, operation = self.tasks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                result = operation()
            except Exception as exc:  # noqa: BLE001 - transport worker errors to UI
                result = exc
            while not self.stop.is_set():
                try:
                    self.results.put((key, result), timeout=0.1)
                    break
                except queue.Full:
                    pass

    def close(self):
        self.stop.set()


def interactive_monitor(args):
    """Render on the main thread; independently schedule bounded operations."""
    _require_tools()
    controller = CommandController()
    workers = BoundedWorkers()
    renderer = StickyTableRenderer(
        sys.stdout,
        [],
        color=colors_enabled(args.color),
        detailed_status=args.detailed_status,
    )
    formatter = TableFormatter(args.parsed_columns, base=args.byte_base)
    requested = {c.key for c in args.parsed_columns}
    groups = [
        IOSTAT_KEYS,
        {
            "used",
            "free",
            "total",
            "capacity",
            "compression",
            "compression_ratio",
            "children",
        },
        {"snapshots", "snapshot_age"},
        {"health", "fragmentation"},
        {"pool", "timestamp", "unix_time"},
    ]
    operations = {}
    outstanding = {}
    measured = {}
    last_tick = {}
    latest = {}
    statuses = {}
    pools = []
    success = False
    discovery_attempts = 0

    def runner(command):
        timeout = args.command_timeout + (
            args.interval if command[1] == "iostat" else 0
        )
        return controller.run(command, timeout)

    def add(key, operation):
        operations[key] = {
            "call": operation,
            "due": 0.0,
            "attempts": 0,
            "failures": 0,
            "generation": object(),
        }

    def reconcile(names):
        nonlocal pools
        pools = list(names)
        for key in list(operations):
            if key[0] is not None and key[0] not in pools:
                del operations[key]
        for name in list(latest):
            if name not in pools:
                latest.pop(name, None)
                statuses.pop(name, None)
                last_tick.pop(name, None)
                for key in list(measured):
                    if key[0] == name:
                        measured.pop(key)
        for name in pools:
            latest.setdefault(name, {"pool": name})
            for index, group in enumerate(groups):
                keys = requested & group
                if index == 4 and requested & set().union(*groups[:4]):
                    continue
                key = (name, index)
                if keys and key not in operations:
                    cache = SnapshotCache()

                    def collect(name=name, keys=keys, cache=cache):
                        started = time.monotonic()
                        row = _collect_sample(
                            name,
                            args.interval,
                            runner,
                            columns=[Column(k, *COLUMN_SPECS[k]) for k in keys],
                            snapshot_cache=cache,
                            snapshot_refresh=args.snapshot_refresh,
                            wait_for_interval=False,
                        )
                        return (row, time.monotonic(), time.monotonic() - started)

                    add(key, collect)
            if not args.no_status and (name, "status") not in operations:
                add((name, "status"), lambda name=name: status_line(name, runner))
        renderer.pools = tuple(pools)
        renderer.samples = {p: renderer.samples.get(p, []) for p in pools}
        renderer.pool_rows = {p: renderer.pool_rows.get(p, []) for p in pools}

    if args.pool:
        reconcile([args.pool])
    else:
        add((None, "discovery"), lambda: discover_pools(runner))
    try:
        while True:
            now = time.monotonic()
            ready = []
            while True:
                try:
                    (key, generation), result = workers.results.get_nowait()
                except queue.Empty:
                    break
                previous = outstanding.pop(key, None)
                if previous is not None and not previous.get("expired"):
                    ready.append((key, generation, result))
            for key, op in outstanding.items():
                deadline = (
                    op["started"]
                    + args.command_timeout
                    + (args.interval if key[1] == 0 else 0)
                    + 0.1
                )
                if now >= deadline and not op.get("expired"):
                    op["expired"] = True
                    if args.count:
                        op["attempts"] = args.count - 1
                    ready.append(
                        (
                            key,
                            op["generation"],
                            ZfsCommandError(
                                "operation deadline exceeded; worker still outstanding"
                            ),
                        )
                    )
            if len(outstanding) == len(workers.threads) and all(
                op.get("expired") for op in outstanding.values()
            ):
                for key, op in operations.items():
                    current_outstanding = outstanding.get(key)
                    if (
                        current_outstanding is None
                        or current_outstanding["generation"] is not op["generation"]
                    ) and (not args.count or op["attempts"] < args.count):
                        if args.count:
                            op["attempts"] = args.count - 1
                        ready.append(
                            (
                                key,
                                op["generation"],
                                ZfsCommandError("worker capacity unavailable"),
                            )
                        )
            for key, generation, result in ready:
                op = operations.get(key)
                if op is None or op["generation"] is not generation:
                    continue
                op["attempts"] += 1
                failed = isinstance(result, Exception)
                op["failures"] = op["failures"] + 1 if failed else 0
                delay = (
                    min(300, 10 * 2 ** min(op["failures"] - 1, 5))
                    if failed and not args.count
                    else args.interval
                )
                op["due"] = (
                    now + delay if failed else max(now, op["started"] + args.interval)
                )
                name, group = key
                if name is None:
                    discovery_attempts = op["attempts"]
                    op["due"] = now + (delay if failed else args.pool_refresh)
                    if not failed:
                        reconcile(result)
                    else:
                        print(
                            f"zpool-stats: warning: discovery: {result}; retaining last inventory, retry in {delay:g}s",
                            file=sys.stderr,
                        )
                elif group == "status":
                    statuses[name] = (
                        f"zpool {name} is UNAVAILABLE: {result}" if failed else result
                    )
                else:
                    keys = requested & groups[group]
                    row = latest[name]
                    if failed:
                        row.update(dict.fromkeys(keys))
                        row["collection_duration"] = None
                        measured.pop(key, None)
                    else:
                        values, completed, duration = result
                        row.update(dict.fromkeys(keys))
                        row.update(values)
                        if any(values.get(k) is not None for k in keys):
                            success = True
                            if group not in (2, 4):
                                measured[key] = completed
                            else:
                                measured.pop(key, None)
                        else:
                            measured.pop(key, None)
                        row["collection_duration"] = duration
                    origins = [v for k, v in measured.items() if k[0] == name]
                    row["_measured_at"] = min(origins) if origins else None
                    row["sample_age"] = (
                        max(0, now - row["_measured_at"]) if origins else None
                    )
                    stamp = time.time()
                    row["unix_time"] = stamp
                    row["timestamp"] = (
                        datetime.fromtimestamp(stamp)
                        .astimezone()
                        .isoformat(timespec="seconds")
                    )
                    if now - last_tick.get(name, -float("inf")) >= args.interval:
                        renderer.samples.setdefault(name, []).append(dict(row))
                        last_tick[name] = now
                        del renderer.samples[name][:-10_000]
                    elif renderer.samples.get(name):
                        renderer.samples[name][-1] = dict(row)
            renderer.status_lines = (
                tuple(
                    statuses.get(p, f"zpool {p} is UNAVAILABLE: collecting")
                    for p in pools
                )
                if not args.no_status
                else ()
            )
            renderer.redraw(formatter)
            metric_ops = [op for key, op in operations.items() if key[0] is not None]
            finite_done = args.count and (
                (pools and all(op["attempts"] >= args.count for op in metric_ops))
                or (not pools and discovery_attempts >= args.count)
            )
            if finite_done:
                return int(not success and bool(metric_ops or not pools))
            # Sort by due time for fair scheduling; no more than four outstanding
            # operations, including removed pools and uninterruptible callbacks.
            for key, op in sorted(operations.items(), key=lambda item: item[1]["due"]):
                if len(outstanding) >= len(workers.threads):
                    break
                if key in outstanding or op["due"] > now:
                    continue
                if args.count and key[0] is not None and op["attempts"] >= args.count:
                    continue
                op["started"] = now
                op["expired"] = False
                outstanding[key] = op
                workers.tasks.put_nowait(((key, op["generation"]), op["call"]))
            time.sleep(0.1)
    finally:
        workers.close()
        controller.close()
        renderer.close()


def monitor(args: argparse.Namespace) -> int:
    if (
        args.format == "table"
        and args.sticky_header
        and args.header_every is None
        and sys.stdout.isatty()
    ):
        return interactive_monitor(args)
    return _monitor(args)


def _monitor(args: argparse.Namespace, publish=None) -> int:
    _require_tools()
    retry_state = RetryState()

    def command_runner(command: Sequence[str]) -> str:
        timeout = args.command_timeout
        if list(command[:2]) == ["zpool", "iostat"]:
            timeout += args.interval
        controller = getattr(args, "_controller", None)
        return (controller.run if controller else run_command)(command, timeout=timeout)

    def safe_status(pool):
        return (
            retry_state.call(
                (pool, "status"), lambda: status_line(pool, command_runner)
            )
            or f"zpool {pool} is UNAVAILABLE: status query failed/backing off"
        )

    pools = (
        [args.pool]
        if args.pool
        else (
            retry_state.call(("discovery",), lambda: discover_pools(command_runner))
            or []
        )
    )
    discovered_at = time.monotonic()

    if publish:
        publish(
            pools, [f"zpool {p} is UNAVAILABLE: collecting status" for p in pools], []
        )
    status_lines = []
    if args.format == "table" and not args.no_status:
        status_lines = [safe_status(pool) for pool in pools]
    use_sticky_header = (
        args.format == "table"
        and args.sticky_header
        and args.header_every is None
        and sys.stdout.isatty()
    )
    use_color = args.format == "table" and colors_enabled(args.color)
    sticky_renderer = (
        StickyTableRenderer(
            sys.stdout,
            status_lines,
            color=use_color,
            pools=pools,
            detailed_status=args.detailed_status,
        )
        if use_sticky_header
        else None
    )
    if sticky_renderer is None and not publish:
        for line in status_lines:
            shown = line if args.detailed_status else compact_status(line)
            print(color_status(shown, use_color), flush=True)

    samples_printed = 0
    any_metric_success = False
    metric_keys = [
        c.key
        for c in args.parsed_columns
        if c.key
        not in {"pool", "timestamp", "unix_time", "sample_age", "collection_duration"}
    ]
    any_rows = False
    rows_since_header = 0
    header_printed = False
    snapshot_cache = SnapshotCache()
    formatter = (
        TableFormatter(args.parsed_columns, base=args.byte_base)
        if args.format == "table"
        else None
    )
    delimited_writer = None
    if args.format in {"csv", "tsv"}:
        delimiter = "," if args.format == "csv" else "\t"
        delimited_writer = csv.writer(
            sys.stdout, delimiter=delimiter, lineterminator="\n"
        )
        delimited_writer.writerow(
            [column.key for column in args.parsed_columns]
            + ["collection_status", "unavailable_columns"]
        )
        sys.stdout.flush()

    try:
        while args.count == 0 or samples_printed < args.count:
            if (
                not args.pool
                and samples_printed
                and time.monotonic() - discovered_at >= args.pool_refresh
            ):
                discovered = retry_state.call(
                    ("discovery",), lambda: discover_pools(command_runner)
                )
                discovered_at = time.monotonic()
                if discovered is not None:
                    removed = set(pools) - set(discovered)
                    for name in removed:
                        snapshot_cache.values.pop(name, None)
                    if pools != discovered:
                        retry_state.failures = {
                            key: value
                            for key, value in retry_state.failures.items()
                            if key[0] not in removed and not isinstance(key[0], tuple)
                        }
                    pools = discovered
                    if sticky_renderer is not None:
                        sticky_renderer.pools = tuple(pools)
                        sticky_renderer.samples = {
                            name: sticky_renderer.samples.get(name, [])
                            for name in pools
                        }
                        sticky_renderer.pool_rows = {
                            name: sticky_renderer.pool_rows.get(name, [])
                            for name in pools
                        }
            if not pools:
                if publish:
                    publish([], [], [])
                if sticky_renderer is not None:
                    sticky_renderer.status_lines = ()
                    sticky_renderer.rows.clear()
                    sticky_renderer.draw("Waiting for imported ZFS pools", "")
                elif args.format == "table" and not publish:
                    print("Waiting for imported ZFS pools", flush=True)
                time.sleep(args.interval)
                samples_printed += 1
                continue
            cycle_started = time.monotonic()
            samples = collect_sample_set(
                pools,
                args.interval,
                runner=command_runner,
                columns=args.parsed_columns,
                snapshot_cache=snapshot_cache,
                snapshot_refresh=args.snapshot_refresh,
                retry_state=retry_state,
            )
            if args.format == "table" and not args.no_status:
                refreshed_status = [safe_status(pool) for pool in pools]
                if sticky_renderer is not None:
                    sticky_renderer.status_lines = tuple(refreshed_status)
                elif not publish:
                    for current in refreshed_status:
                        if current not in status_lines:
                            print(color_status(current, use_color), flush=True)
                status_lines = refreshed_status
            collection_duration = max(0, time.monotonic() - cycle_started)
            if publish:
                publish(pools, status_lines, samples)
            for sample in samples:
                any_rows = True
                unavailable = [key for key in metric_keys if sample.get(key) is None]
                successful = len(unavailable) < len(metric_keys)
                any_metric_success |= successful
                collection_status = (
                    "ok" if not unavailable else ("partial" if successful else "failed")
                )
                requested = {column.key for column in args.parsed_columns}
                if "collection_duration" in requested:
                    sample["collection_duration"] = collection_duration
                if "sample_age" in requested and sample.get("_measured_at") is not None:
                    sample["sample_age"] = max(
                        0, time.monotonic() - sample["_measured_at"]
                    )
                if (
                    "snapshot_age" in requested
                    and sample.get("snapshot_age") is not None
                ):
                    cached = snapshot_cache.values.get(str(sample["pool"]))
                    if cached is not None:
                        sample["snapshot_age"] = max(0, time.monotonic() - cached[0])
                _validate_sample(sample)
                if publish:
                    continue
                if args.format == "table":
                    assert formatter is not None
                    header, row, widths_expanded = formatter.format(sample)
                    if sticky_renderer is not None:
                        sticky_renderer.draw_sample(sample, formatter)
                    else:
                        repeat_every = args.header_every or 0
                        if (
                            not header_printed
                            or (widths_expanded and args.header_every is None)
                            or (repeat_every and rows_since_header >= repeat_every)
                        ):
                            print(color_header(header, use_color))
                            header_printed = True
                            rows_since_header = 0
                        print(row, flush=True)
                        rows_since_header += 1
                elif args.format in {"csv", "tsv"}:
                    assert delimited_writer is not None
                    delimited_writer.writerow(
                        [
                            "" if sample.get(column.key) is None else sample[column.key]
                            for column in args.parsed_columns
                        ]
                        + [collection_status, ";".join(unavailable)]
                    )
                    sys.stdout.flush()
                else:
                    record = {
                        column.key: sample.get(column.key)
                        for column in args.parsed_columns
                    }
                    record.update(
                        collection_status=collection_status,
                        unavailable_columns=unavailable,
                    )
                    print(
                        json.dumps(record, separators=(",", ":"), allow_nan=False),
                        flush=True,
                    )
            samples_printed += 1
    finally:
        if sticky_renderer is not None:
            sticky_renderer.close()
    return int(not any_rows or (bool(metric_keys) and not any_metric_success))


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
        if _command_controller is not None:
            _command_controller.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
