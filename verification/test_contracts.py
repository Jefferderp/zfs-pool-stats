"""Independent synthetic contract checks; these do not require live OpenZFS."""

import csv
import io
import json
import os
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

import zpool_stats as z


def test_command_permission_error_is_recoverable():
    with (
        patch.object(z.subprocess, "Popen", side_effect=PermissionError("denied")),
        pytest.raises(z.ZfsCommandError, match="denied"),
    ):
        z.run_command(["zfs", "get"], timeout=1)


def test_unknown_operands_preserve_known_measurements():
    def runner(_command):
        return "tank\tused\t5\ntank\tavailable\t-\ntank\tcompressratio\t-\n"

    result = z.collect_sample(
        "tank",
        1,
        runner,
        columns=z.parse_columns(
            "used,free,total,capacity,compression_ratio,compression"
        ),
        sleeper=lambda _: None,
    )
    assert result["used"] == 5
    assert all(
        result.get(key) is None
        for key in ("free", "total", "capacity", "compression_ratio", "compression")
    )


def test_online_errors_and_scrub_continuations_are_visible():
    fixture = """  pool: tank
 state: ONLINE
  scan: scrub in progress since today
        746G / 65.3T scanned at 11.5G/s, 0B / 65.3T issued
        0B repaired, 20.00% done, 01:02:03 to go
config:

        NAME STATE READ WRITE CKSUM
        tank ONLINE 0 0 0
          disk1 ONLINE 0 0 1200

errors: 2 data errors
"""
    line = z.status_line("tank", lambda _command: fixture)
    assert "20.00% done" in line
    assert "11.5G/s" in line
    assert "01:02:03 to go" in line
    assert "2 data errors" in line
    assert "disk1" in line and "1200" in line
    assert not z.color_status(line, True).startswith(z.ANSI_BOLD_GREEN)


def test_faulted_health_is_not_downgraded_by_error_attention():
    line = "zpool tank is FAULTED: ATTENTION; errors: 2 data errors"
    assert z.color_status(line, True).startswith(z.ANSI_BOLD_RED)


def test_scrub_progress_is_visible_in_eighty_column_viewport():
    fixture = """state: ONLINE
  scan: scrub in progress since Fri Oct 16 15:36:02 2026
        746G / 65.3T scanned at 11.5G/s, 0B / 65.3T issued
        0B repaired, 20.00% done, 01:02:03 to go
errors: No known data errors
"""
    output = io.StringIO()
    renderer = z.StickyTableRenderer(
        output,
        [z.status_line("tank", lambda _: fixture)],
        detailed_status=True,
        color=False,
        pools=["tank"],
        terminal_size=lambda: os.terminal_size((80, 24)),
    )
    renderer.draw("pool used", "tank 1K", pool="tank")
    assert "20.00% done" in output.getvalue()
    assert "11.5G/s" in output.getvalue()
    assert "01:02:03 to go" in output.getvalue()
    assert renderer.last_line_count <= 24


def test_malformed_snapshot_row_does_not_become_partial_total():
    with pytest.raises(z.ZfsCommandError):
        z.parse_snapshot_usage("tank\t20\ntank/child\n")


def test_snapshot_partial_unknown_is_not_complete_total():
    assert z.parse_snapshot_usage("tank\t-\ntank/child\t10\n") is None


def test_shared_io_recovers_without_inherited_pool_parse_backoff():
    now = [0.0]
    acquisitions = [0]
    slow = [True]

    def sleep(delay):
        now[0] += delay

    def runner(command):
        if command[1] == "iostat":
            acquisitions[0] += 1
            if acquisitions[0] == 1:
                raise z.ZfsCommandError("synthetic shared failure")
            now[0] += 2
            return "a 1 9 1 1 5 1\nb 1 9 1 1 5 1\n"
        if command[-1] == "a" and slow[0]:
            now[0] += 100
            slow[0] = False
        return f"{command[-1]}\tused\t5\n"

    state = z.RetryState(clock=lambda: now[0])
    kwargs = {
        "columns": z.parse_columns("read,used"),
        "clock": lambda: now[0],
        "sleeper": sleep,
        "retry_state": state,
    }
    first = z.collect_sample_set(["a", "b"], 2, runner, **kwargs)
    assert all(row["read"] is None for row in first)
    recovered = z.collect_sample_set(["a", "b"], 2, runner, **kwargs)
    assert all(row["read"] == 5 for row in recovered)


def test_shared_collection_timing_includes_shared_io_and_other_pools():
    now = [0.0]

    def runner(command):
        if command[1] == "iostat":
            now[0] += 10
            return "a 1 9 1 1 1 1\nb 1 9 1 1 1 1\n"
        now[0] += 2 if command[-1] == "a" else 3
        return f"{command[-1]}\tused\t5\n"

    rows = z.collect_sample_set(
        ["a", "b"],
        10,
        runner,
        columns=z.parse_columns("read,used,sample_age,collection_duration"),
        clock=lambda: now[0],
        sleeper=lambda _: None,
    )
    assert now[0] == 15
    assert all(row["collection_duration"] == 15 for row in rows)
    assert all(row["sample_age"] == 5 for row in rows)


def test_snapshot_cache_refresh_deadline_uses_completion():
    now = [100.0]
    calls = []

    def runner(command):
        calls.append(command)
        now[0] += 30
        return "tank\t0\n"

    cache = z.SnapshotCache()
    assert cache.get("tank", 60, runner, lambda: now[0]) == 0
    assert cache.values["tank"][0] == 130
    now[0] = 189.9
    assert cache.get("tank", 60, runner, lambda: now[0]) == 0
    assert len(calls) == 1
    now[0] = 190
    assert cache.get("tank", 60, runner, lambda: now[0]) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("output_format", ["jsonl", "csv", "tsv"])
def test_raw_output_keeps_integer_precision_and_unknowns(output_format):
    integer = 9007199254740993
    args = z.parse_args(
        [
            "tank",
            "--count",
            "1",
            "--format",
            output_format,
            "--color",
            "always",
            "--columns",
            "used,free",
        ]
    )
    output = io.StringIO()
    with (
        patch.object(z, "_require_tools"),
        patch.object(
            z,
            "collect_sample_set",
            return_value=[
                {
                    "pool": "tank",
                    "used": integer,
                    "free": None,
                }
            ],
        ),
        redirect_stdout(output),
    ):
        assert z.monitor(args) == 0
    text = output.getvalue()
    assert str(integer) in text
    assert "\x1b" not in text
    if output_format == "jsonl":
        assert json.loads(text) == {
            "pool": "tank",
            "used": integer,
            "free": None,
            "collection_status": "partial",
            "unavailable_columns": ["free"],
        }
    else:
        records = list(
            csv.reader(
                io.StringIO(text), delimiter="," if output_format == "csv" else "\t"
            )
        )
        assert records == [
            ["pool", "used", "free", "collection_status", "unavailable_columns"],
            ["tank", str(integer), "", "partial", "free"],
        ]
