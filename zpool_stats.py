#!/usr/bin/env python3
"""A lightweight, dependency-free ZFS pool statistics monitor."""

from __future__ import annotations

import argparse
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

VERSION = "1.3.0"
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
) -> dict[str, int | float | str]:
    requested = (
        set(COLUMN_SPECS) if columns is None else {column.key for column in columns}
    )
    sample: dict[str, int | float | str] = {"pool": pool}

    iostat_keys = {
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
    if requested & iostat_keys:
        iostat = parse_iostat(
            runner(["zpool", "iostat", "-Hplvy", pool, str(interval), "1"]), pool
        )
        aliases = {"read": "read_bandwidth", "write": "write_bandwidth"}
        for key in requested & iostat_keys:
            source = aliases.get(key, key)
            if source in iostat:
                sample[key] = iostat[source]
    else:
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
        "--format",
        choices=("table", "tsv", "jsonl"),
        default="table",
        help="output format; TSV and JSON Lines use raw values (default: table)",
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
    if args.list_pools and args.pool:
        parser.error("POOL cannot be used with --list-pools")
    if not (args.list_columns or args.list_pools) and not args.pool:
        parser.error("a pool name is required")
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

    if args.format == "table" and not args.no_status:
        print(status_line(args.pool, command_runner), flush=True)
    printed = 0
    rows_since_header = 0
    snapshot_cache = SnapshotCache()
    formatter = TableFormatter(args.parsed_columns) if args.format == "table" else None
    if args.format == "tsv":
        print("\t".join(column.key for column in args.parsed_columns), flush=True)
    while args.count == 0 or printed < args.count:
        # The collector uses zpool iostat as the sampling clock when an I/O
        # column needs it, and sleeps directly when no I/O column is selected.
        sample = collect_sample(
            args.pool,
            args.interval,
            runner=command_runner,
            columns=args.parsed_columns,
            snapshot_cache=snapshot_cache,
            snapshot_refresh=args.snapshot_refresh,
        )
        _validate_sample(sample)
        if args.format == "table":
            assert formatter is not None
            header, row, widths_expanded = formatter.format(sample)
            repeat_every = header_interval(args.header_every)
            if (
                printed == 0
                or widths_expanded
                or (repeat_every and rows_since_header >= repeat_every)
            ):
                print(header)
                rows_since_header = 0
            print(row, flush=True)
            rows_since_header += 1
        elif args.format == "tsv":
            print(
                "\t".join(
                    "" if sample.get(column.key) is None else str(sample[column.key])
                    for column in args.parsed_columns
                ),
                flush=True,
            )
        else:
            record = {
                column.key: sample.get(column.key) for column in args.parsed_columns
            }
            print(
                json.dumps(record, separators=(",", ":"), allow_nan=False), flush=True
            )
        printed += 1
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
