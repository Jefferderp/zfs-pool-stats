import io
import json
import os
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime

import zpool_stats


class FormatBytesTests(unittest.TestCase):
    def test_automatic_binary_scale_keeps_one_decimal(self):
        self.assertEqual(zpool_stats.format_bytes(76.5 * 1024**4), "76.5T")

    def test_explicit_scale_and_precision(self):
        self.assertEqual(zpool_stats.format_bytes(1536, "K", 2), "1.50K")

    def test_zero_is_formatted_with_a_unit(self):
        self.assertEqual(zpool_stats.format_bytes(0), "0B")

    def test_iostat_nanoseconds_are_formatted_as_time(self):
        self.assertEqual(zpool_stats.format_time(1_000_000), "1.0ms")
        self.assertEqual(zpool_stats.format_time(1_000_000, "us", 0), "1000us")


class ParseTests(unittest.TestCase):
    def test_iostat_uses_pool_row_from_last_sample(self):
        output = """tank\t100\t900\t1\t2\t3\t4\t5\t6
mirror-0\t100\t900\t1\t2\t3\t4\t5\t6
tank\t110\t890\t7\t8\t9000000\t12000000\t13\t14
mirror-0\t110\t890\t7\t8\t9000000\t12000000\t13\t14"""
        sample = zpool_stats.parse_iostat(output, "tank")
        self.assertEqual(sample["logical_used"], 110)
        self.assertEqual(sample["read_bandwidth"], 9_000_000)
        self.assertEqual(sample["total_wait"], 27)

    def test_pool_properties_are_parsed_by_property_name(self):
        output = """tank\tavailable\t200
tank\tcompressratio\t1.25x
tank\tused\t800
tank\tusedbychildren\t700"""
        props = zpool_stats.parse_properties(output)
        self.assertEqual(props["used"], 800)
        self.assertEqual(props["available"], 200)
        self.assertEqual(props["compressratio"], 1.25)

    def test_snapshot_usage_ignores_snapshot_rows_and_missing_values(self):
        output = "tank\t10\ntank/a\t20\ntank/a@snap\t-\ntank/b\t30\n"
        self.assertEqual(zpool_stats.parse_snapshot_usage(output), 60)

    def test_snapshot_usage_rejects_malformed_values_cleanly(self):
        with self.assertRaisesRegex(zpool_stats.ZfsCommandError, "tank/a"):
            zpool_stats.parse_snapshot_usage("tank\t10\ntank/a\tnot-a-number\n")

    def test_pool_list_parser_preserves_health_fragmentation_tuple(self):
        self.assertEqual(
            zpool_stats.parse_pool_list("tank\tONLINE\t48%\n"),
            ("ONLINE", 0.48),
        )

    def test_status_summary_extracts_health_and_scan(self):
        output = """  pool: tank
 state: ONLINE
  scan: scrub repaired 15.4M in 01:00:15 with 0 errors on Wed Jul 29 23:18:46 2026
config:

        NAME STATE READ WRITE CKSUM
"""
        health, detail = zpool_stats.parse_status(output)
        self.assertEqual(health, "ONLINE")
        self.assertTrue(detail.startswith("scan: scrub repaired"))

    def test_run_command_times_out_cleanly(self):
        with self.assertRaisesRegex(
            zpool_stats.ZfsCommandError, "timed out after 0.01 seconds"
        ):
            zpool_stats.run_command(
                [sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01
            )

    def test_pool_discovery_returns_names_in_zpool_order(self):
        commands = []

        def run(command):
            commands.append(command)
            return "tank\nbackup\n"

        self.assertEqual(zpool_stats.discover_pools(run), ["tank", "backup"])
        self.assertEqual(commands, [["zpool", "list", "-H", "-o", "name"]])


class ColumnTests(unittest.TestCase):
    def test_default_columns_match_monitor_layout(self):
        columns = zpool_stats.parse_columns(None)
        names = [column.header for column in columns]
        self.assertEqual(
            names,
            [
                "pool",
                "used",
                "free",
                "total",
                "cap",
                "read",
                "write",
                "frag",
                "comp",
                "snap",
            ],
        )
        capacity = next(column for column in columns if column.key == "capacity")
        self.assertEqual(capacity.precision, 0)

    def test_modern_names_and_custom_format_are_supported(self):
        columns = zpool_stats.parse_columns("used:T:2:USED,read:M")
        self.assertEqual(columns[0].key, "pool")
        self.assertEqual(columns[1].key, "used")
        self.assertEqual(columns[1].unit, "T")
        self.assertEqual(columns[1].precision, 2)
        self.assertEqual(columns[1].header, "USED")
        self.assertEqual(columns[2].key, "read")

    def test_explicit_pool_column_is_moved_to_the_front_without_duplication(self):
        columns = zpool_stats.parse_columns("used,pool:::ZPOOL,free")

        self.assertEqual([column.key for column in columns], ["pool", "used", "free"])
        self.assertEqual(columns[0].header, "ZPOOL")

    def test_legacy_column_names_are_rejected(self):
        legacy_names = (
            "PoolName",
            "StateHealth",
            "LogicCapUsed",
            "LogicCapFree",
            "VirtCapUsed",
            "VirtCapFree",
            "VirtCapTot",
            "VirtCapUsedPerc",
            "OpsRead",
            "OpsWrite",
            "BwRead",
            "BwWrite",
            "TotalWaitRead",
            "TotalWaitWrite",
            "TotalWaitBoth",
            "StateFragPerc",
            "VirtCompPerc",
            "VirtCompRatio",
            "VirtCapUsedByChilds",
            "VirtCapUsedByChildren",
            "VirtCapUsedBySnaps",
        )
        for name in legacy_names:
            with self.subTest(name=name), self.assertRaises(ValueError):
                zpool_stats.parse_columns(name)

    def test_timestamp_columns_are_supported(self):
        columns = zpool_stats.parse_columns("timestamp,unix_time")
        self.assertEqual(
            [column.key for column in columns], ["pool", "timestamp", "unix_time"]
        )
        self.assertEqual(
            [column.header for column in columns], ["pool", "timestamp", "unix"]
        )

    def test_unknown_column_is_rejected(self):
        with self.assertRaises(ValueError):
            zpool_stats.parse_columns("used,definitely-not-a-column")


class TableFormatterTests(unittest.TestCase):
    def test_widths_only_expand_and_expansion_is_reported(self):
        formatter = zpool_stats.TableFormatter(zpool_stats.parse_columns("pool,read"))

        _, _first, first_expanded = formatter.format({"pool": "a", "read": 1})
        _, wide, wide_expanded = formatter.format(
            {"pool": "extra-long-pool", "read": 1}
        )
        _, narrow_again, narrow_expanded = formatter.format({"pool": "b", "read": 1})

        self.assertFalse(first_expanded)
        self.assertTrue(wide_expanded)
        self.assertFalse(narrow_expanded)
        self.assertEqual(wide.index("1B"), narrow_again.index("1B"))


class InteractiveRendererTests(unittest.TestCase):
    def test_pool_sections_scroll_independently_with_fixed_blank_separator(self):
        output = io.StringIO()
        renderer = zpool_stats.StickyTableRenderer(
            output,
            ["tank status", "backup status"],
            color=False,
            pools=["tank", "backup"],
            terminal_size=lambda: os.terminal_size((80, 11)),
        )
        for number in range(6):
            renderer.draw("pool used", f"tank {number}", pool="tank")
            renderer.draw("pool used", f"backup {number}", pool="backup")
        frame = output.getvalue().split("\x1b[H")[-1]
        lines = frame.replace("\x1b[K", "").replace("\x1b[J", "").split("\r\n")
        self.assertEqual(
            lines,
            [
                "tank status",
                "pool used",
                "tank 3",
                "tank 4",
                "tank 5",
                "",
                "backup status",
                "pool used",
                "backup 3",
                "backup 4",
                "backup 5",
            ],
        )

    def test_pool_sections_resize_preserves_independent_history(self):
        output = io.StringIO()
        height = 9
        renderer = zpool_stats.StickyTableRenderer(
            output,
            [],
            color=False,
            pools=["a", "b"],
            terminal_size=lambda: os.terminal_size((80, height)),
        )
        for number in range(5):
            for pool in ("a", "b"):
                renderer.draw("pool used", f"{pool} {number}", pool=pool)
        height = 3
        renderer.draw("pool used", "a 5", pool="a")
        self.assertEqual(renderer.last_line_count, 3)
        height = 10
        renderer.draw("pool used", "a 6", pool="a")
        frame = output.getvalue().split("\x1b[H")[-1]
        self.assertEqual(renderer.last_line_count, 10)
        for row in ("a 3", "a 4", "a 5", "a 6", "b 2", "b 3", "b 4"):
            self.assertIn(row, frame)

    def test_color_auto_follows_tty_and_never_disables_ansi(self):
        tty = io.StringIO()
        tty.isatty = lambda: True

        self.assertTrue(zpool_stats.colors_enabled("auto", tty))
        self.assertFalse(zpool_stats.colors_enabled("never", tty))
        self.assertTrue(zpool_stats.colors_enabled("always", io.StringIO()))

    def test_status_health_and_header_use_semantic_colors(self):
        self.assertEqual(
            zpool_stats.color_status("zpool tank is ONLINE", True),
            "\x1b[1;32mzpool tank is ONLINE\x1b[0m",
        )
        self.assertEqual(
            zpool_stats.color_status("zpool tank is DEGRADED", True),
            "\x1b[1;33mzpool tank is DEGRADED\x1b[0m",
        )
        self.assertEqual(
            zpool_stats.color_status("zpool tank is FAULTED", True),
            "\x1b[1;31mzpool tank is FAULTED\x1b[0m",
        )
        self.assertEqual(
            zpool_stats.color_header("pool  used", True),
            "\x1b[1;36mpool  used\x1b[0m",
        )

    def test_sticky_renderer_keeps_header_and_latest_rows_in_viewport(self):
        output = io.StringIO()
        output.isatty = lambda: True
        renderer = zpool_stats.StickyTableRenderer(
            output,
            ["zpool tank is ONLINE"],
            color=True,
            terminal_size=lambda: os.terminal_size((80, 5)),
        )

        for number in range(1, 6):
            renderer.draw("pool  used", f"tank  {number}G")
        renderer.close()

        last_frame = output.getvalue().split("\x1b[H")[-1]
        self.assertIn("\x1b[1;32mzpool tank is ONLINE\x1b[0m", last_frame)
        self.assertIn("\x1b[1;36mpool  used\x1b[0m", last_frame)
        self.assertNotIn("tank  2G", last_frame)
        self.assertIn("tank  3G", last_frame)
        self.assertIn("tank  4G", last_frame)
        self.assertIn("tank  5G", last_frame)
        self.assertTrue(output.getvalue().endswith("\x1b[5;1H\x1b[?25h\n"))

    def test_sticky_renderer_uses_explicit_carriage_returns_between_lines(self):
        output = io.StringIO()
        renderer = zpool_stats.StickyTableRenderer(
            output,
            ["status"],
            color=False,
            terminal_size=lambda: os.terminal_size((80, 4)),
        )

        renderer.draw("header", "row")

        frame = output.getvalue()
        self.assertIn("status\x1b[K\r\nheader\x1b[K\r\nrow", frame)

    def test_sticky_renderer_drops_misaligned_rows_when_header_width_changes(self):
        output = io.StringIO()
        renderer = zpool_stats.StickyTableRenderer(
            output,
            [],
            color=False,
            terminal_size=lambda: os.terminal_size((80, 5)),
        )

        renderer.draw("pool  read", "a     1B")
        renderer.draw("pool             read", "extra-long-pool  1B")

        last_frame = output.getvalue().split("\x1b[H")[-1]
        self.assertNotIn("a     1B", last_frame)
        self.assertIn("extra-long-pool  1B", last_frame)

    def test_sticky_renderer_reserves_space_for_data_in_a_short_terminal(self):
        output = io.StringIO()
        renderer = zpool_stats.StickyTableRenderer(
            output,
            ["status one", "status two", "status three"],
            color=False,
            terminal_size=lambda: os.terminal_size((80, 3)),
        )

        renderer.draw("pool  read", "tank  1B")

        last_frame = output.getvalue().split("\x1b[H")[-1]
        self.assertIn("status one", last_frame)
        self.assertNotIn("status two", last_frame)
        self.assertIn("pool  read", last_frame)
        self.assertIn("tank  1B", last_frame)

    def test_sticky_renderer_restores_recent_rows_after_terminal_growth(self):
        output = io.StringIO()
        height = 5
        renderer = zpool_stats.StickyTableRenderer(
            output,
            ["status"],
            color=False,
            terminal_size=lambda: os.terminal_size((80, height)),
        )

        renderer.draw("pool", "one")
        renderer.draw("pool", "two")
        renderer.draw("pool", "three")
        height = 3
        renderer.draw("pool", "four")
        height = 5
        renderer.draw("pool", "five")

        last_frame = output.getvalue().split("\x1b[H")[-1]
        self.assertIn("three", last_frame)
        self.assertIn("four", last_frame)
        self.assertIn("five", last_frame)


class CollectorTests(unittest.TestCase):
    def test_sample_set_uses_one_iostat_interval_for_all_pools(self):
        commands = []
        sleeps = []

        def run(command):
            commands.append(command)
            return "tank\t100\t900\t1\t2\t300\t400\nbackup\t50\t950\t3\t4\t500\t600\n"

        samples = zpool_stats.collect_sample_set(
            ["tank", "backup"],
            2.5,
            run,
            columns=zpool_stats.parse_columns("read,write"),
            sleeper=sleeps.append,
        )

        self.assertEqual(
            samples,
            [
                {"pool": "tank", "read": 300, "write": 400},
                {"pool": "backup", "read": 500, "write": 600},
            ],
        )
        self.assertEqual(
            commands,
            [
                [
                    "zpool",
                    "iostat",
                    "-Hplvy",
                    "tank",
                    "backup",
                    "2.5",
                    "1",
                ]
            ],
        )
        self.assertEqual(sleeps, [])

    def test_sample_set_without_iostat_sleeps_once_for_all_pools(self):
        sleeps = []

        samples = zpool_stats.collect_sample_set(
            ["tank", "backup"],
            2.5,
            lambda command: self.fail(f"unexpected command: {command}"),
            columns=zpool_stats.parse_columns("pool"),
            sleeper=sleeps.append,
        )

        self.assertEqual(samples, [{"pool": "tank"}, {"pool": "backup"}])
        self.assertEqual(sleeps, [2.5])

    def test_non_finite_source_values_are_rejected(self):
        for value in ("nan", "inf", "-inf"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    zpool_stats.ZfsCommandError, "non-finite numeric value"
                ),
            ):
                zpool_stats.parse_properties(
                    f"tank\tcompressratio\t{value}\n", {"compressratio"}
                )

    def test_malformed_source_values_are_rejected_cleanly(self):
        with self.assertRaisesRegex(
            zpool_stats.ZfsCommandError, "invalid numeric value"
        ):
            zpool_stats.parse_properties(
                "tank\tcompressratio\tnot-a-number\n", {"compressratio"}
            )

    def test_byte_properties_reject_floating_point_source_values(self):
        with self.assertRaisesRegex(
            zpool_stats.ZfsCommandError, "invalid integer value"
        ):
            zpool_stats.parse_properties("tank\tused\t1e308\n", {"used"})

    def test_derived_non_finite_values_are_rejected_before_output(self):
        with self.assertRaisesRegex(
            zpool_stats.ZfsCommandError, "non-finite value for column 'total'"
        ):
            zpool_stats._validate_sample({"used": 1e308, "total": 1e308 + 1e308})

    def test_timestamp_columns_share_one_sample_time(self):
        sleeps = []
        sample = zpool_stats.collect_sample(
            "tank",
            2.5,
            columns=zpool_stats.parse_columns("timestamp,unix_time::3"),
            sleeper=sleeps.append,
            wall_clock=lambda: 1_725_000_000.125,
        )

        self.assertEqual(sample["unix_time"], 1_725_000_000.125)
        self.assertEqual(
            datetime.fromisoformat(sample["timestamp"]).timestamp(), 1_725_000_000
        )
        self.assertEqual(sleeps, [2.5])

    def test_snapshot_usage_is_cached_until_refresh_interval_expires(self):
        calls = 0
        now = 100.0

        def run(command):
            nonlocal calls
            calls += 1
            return f"tank\t{calls * 10}\n"

        cache = zpool_stats.SnapshotCache()
        columns = zpool_stats.parse_columns("snapshots")
        kwargs = {
            "columns": columns,
            "snapshot_cache": cache,
            "snapshot_refresh": 60.0,
            "clock": lambda: now,
            "sleeper": lambda interval: None,
        }

        first = zpool_stats.collect_sample("tank", 1.0, run, **kwargs)
        now = 159.9
        cached = zpool_stats.collect_sample("tank", 1.0, run, **kwargs)
        now = 160.0
        refreshed = zpool_stats.collect_sample("tank", 1.0, run, **kwargs)

        self.assertEqual(first["snapshots"], 10)
        self.assertEqual(cached["snapshots"], 10)
        self.assertEqual(refreshed["snapshots"], 20)
        self.assertEqual(calls, 2)

    def test_compression_avoids_binary_float_noise(self):
        sample = zpool_stats.collect_sample(
            "tank",
            1.0,
            lambda command: "tank\tcompressratio\t1.29x\n",
            columns=zpool_stats.parse_columns("compression"),
            sleeper=lambda interval: None,
        )

        self.assertEqual(sample["compression"], 0.29)

    def test_collection_only_runs_collectors_needed_by_selected_columns(self):
        commands = []

        def run(command):
            commands.append(command)
            return "tank\t100\t900\t1\t2\t300\t400\t5\t6\n"

        sample = zpool_stats.collect_sample(
            "tank",
            1.0,
            run,
            columns=zpool_stats.parse_columns("read,write"),
        )

        self.assertEqual(sample, {"pool": "tank", "read": 300, "write": 400})
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:2], ["zpool", "iostat"])

    def test_collection_sleeps_instead_of_running_iostat_when_not_needed(self):
        sleeps = []
        commands = []

        sample = zpool_stats.collect_sample(
            "tank",
            2.5,
            lambda command: commands.append(command) or "tank\t48%\n",
            columns=zpool_stats.parse_columns("fragmentation"),
            sleeper=sleeps.append,
        )

        self.assertEqual(sample["fragmentation"], 0.48)
        self.assertEqual(sleeps, [2.5])
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0],
            ["zpool", "list", "-H", "-o", "name,frag", "tank"],
        )

    def test_health_only_requests_health_from_zpool_list(self):
        commands = []

        sample = zpool_stats.collect_sample(
            "tank",
            1.0,
            lambda command: commands.append(command) or "tank\tONLINE\n",
            columns=zpool_stats.parse_columns("health"),
            sleeper=lambda interval: None,
        )

        self.assertEqual(sample["health"], "ONLINE")
        self.assertEqual(
            commands,
            [["zpool", "list", "-H", "-o", "name,health", "tank"]],
        )

    def test_collection_combines_command_outputs(self):
        outputs = {
            "zpool iostat": "tank\t100\t900\t1\t2\t300\t400\t5\t6\n",
            "zfs get": "tank\tused\t800\ntank\tavailable\t200\ntank\tcompressratio\t1.03x\ntank\tusedbychildren\t700\n",
            "zfs list": "tank\t50\ntank/a\t10\n",
            "zpool list": "tank\tONLINE\t48%\n",
        }

        def run(command):
            if command[:3] == ["zfs", "get", "-Hpr"]:
                return outputs["zfs list"]
            prefix = " ".join(command[:2])
            return outputs[prefix]

        sample = zpool_stats.collect_sample("tank", 1.0, run)
        self.assertEqual(sample["used"], 800)
        self.assertEqual(sample["total"], 1000)
        self.assertEqual(sample["capacity"], 0.8)
        self.assertAlmostEqual(float(sample["compression"]), 0.03)
        self.assertEqual(sample["snapshots"], 60)
        self.assertEqual(sample["fragmentation"], 0.48)


class CliTests(unittest.TestCase):
    def test_pool_argument_is_optional_for_automatic_discovery(self):
        args = zpool_stats.parse_args(["--count", "1"])

        self.assertIsNone(args.pool)

    def test_all_flag_explicitly_selects_automatic_pool_discovery(self):
        args = zpool_stats.parse_args(["--all", "--count", "1"])

        self.assertTrue(args.all_pools)
        self.assertIsNone(args.pool)

    def test_all_flag_rejects_an_explicit_pool(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            zpool_stats.parse_args(["tank", "--all"])

        self.assertIn("POOL cannot be used with --all", stderr.getvalue())

    def test_explicit_pool_does_not_discover_or_collect_other_pools(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "1", "--format", "tsv", "--columns", "used"]
        )
        collected = []

        def collect(pool, interval, **kwargs):
            collected.append(pool)
            return {"pool": pool, "used": 800}

        original_collect = zpool_stats.collect_sample
        original_discover = zpool_stats.discover_pools
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = collect
            zpool_stats.discover_pools = lambda runner: self.fail(
                "explicit pool must not trigger discovery"
            )
            zpool_stats._require_tools = lambda: None
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats.discover_pools = original_discover
            zpool_stats._require_tools = original_require

        self.assertEqual(collected, ["tank"])
        self.assertEqual(output.getvalue(), "pool\tused\ntank\t800\n")

    def test_format_accepts_table_csv_tsv_and_jsonl(self):
        for output_format in ("table", "csv", "tsv", "jsonl"):
            with self.subTest(output_format=output_format):
                args = zpool_stats.parse_args(
                    ["tank", "--count", "1", "--format", output_format]
                )
                self.assertEqual(args.format, output_format)

    def test_list_pools_discovers_pools_without_requiring_zfs(self):
        required = []
        calls = []

        def require(commands=("zpool", "zfs")):
            required.append(tuple(commands))

        def command(command, timeout=None):
            calls.append((command, timeout))
            return "tank\nbackup\n"

        original_command = zpool_stats.run_command
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.run_command = command
            zpool_stats._require_tools = require
            output = io.StringIO()
            with redirect_stdout(output):
                returncode = zpool_stats.main(
                    ["--list-pools", "--command-timeout", "7"]
                )
        finally:
            zpool_stats.run_command = original_command
            zpool_stats._require_tools = original_require

        self.assertEqual(returncode, 0)
        self.assertEqual(output.getvalue(), "tank\nbackup\n")
        self.assertEqual(required, [("zpool",)])
        self.assertEqual(calls, [(["zpool", "list", "-H", "-o", "name"], 7.0)])

    def test_list_pools_rejects_a_pool_argument(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            zpool_stats.parse_args(["tank", "--list-pools"])

    def test_jsonl_rejects_duplicate_column_names(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            zpool_stats.parse_args(
                ["tank", "--format", "jsonl", "--columns", "used,used"]
            )

    def test_tsv_outputs_raw_values_with_one_machine_header(self):
        args = zpool_stats.parse_args(
            [
                "tank",
                "--count",
                "1",
                "--format",
                "tsv",
                "--columns",
                "used,capacity,compression_ratio",
            ]
        )

        self.assertEqual(
            self._run_monitor(args),
            "pool\tused\tcapacity\tcompression_ratio\ntank\t800\t0.8\t1.03\n",
        )

    def test_monitor_discovers_and_reports_every_pool_each_sample(self):
        args = zpool_stats.parse_args(
            ["--count", "2", "--format", "tsv", "--columns", "used"]
        )
        collected = []

        def collect(pool, interval, **kwargs):
            collected.append(pool)
            return {"pool": pool, "used": 800 if pool == "tank" else 400}

        original_collect = zpool_stats.collect_sample
        original_discover = zpool_stats.discover_pools
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = collect
            zpool_stats.discover_pools = lambda runner: ["tank", "backup"]
            zpool_stats._require_tools = lambda: None
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats.discover_pools = original_discover
            zpool_stats._require_tools = original_require

        self.assertEqual(collected, ["tank", "backup", "tank", "backup"])
        self.assertEqual(
            output.getvalue(),
            "pool\tused\ntank\t800\nbackup\t400\ntank\t800\nbackup\t400\n",
        )

    def test_monitor_fails_cleanly_when_no_pools_are_imported(self):
        args = zpool_stats.parse_args(["--count", "1"])
        original_discover = zpool_stats.discover_pools
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.discover_pools = lambda runner: []
            zpool_stats._require_tools = lambda: None
            with self.assertRaisesRegex(
                zpool_stats.ZfsCommandError, "no imported ZFS pools"
            ):
                zpool_stats.monitor(args)
        finally:
            zpool_stats.discover_pools = original_discover
            zpool_stats._require_tools = original_require

    def test_csv_outputs_raw_values_with_one_machine_header(self):
        args = zpool_stats.parse_args(
            [
                "tank",
                "--count",
                "1",
                "--format",
                "csv",
                "--columns",
                "pool,used,capacity,compression_ratio",
            ]
        )

        self.assertEqual(
            self._run_monitor(args),
            "pool,used,capacity,compression_ratio\ntank,800,0.8,1.03\n",
        )

    def test_csv_quotes_fields_according_to_rfc_4180(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "1", "--format", "csv", "--columns", "pool"]
        )
        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = lambda *args, **kwargs: {
                "pool": 'pool,"quoted"'
            }
            zpool_stats._require_tools = lambda: None
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require

        self.assertEqual(output.getvalue(), 'pool\n"pool,""quoted"""\n')

    def test_jsonl_outputs_typed_raw_values_without_status(self):
        args = zpool_stats.parse_args(
            [
                "tank",
                "--count",
                "1",
                "--format",
                "jsonl",
                "--columns",
                "pool,used,capacity,compression_ratio",
            ]
        )

        record = json.loads(self._run_monitor(args))
        self.assertEqual(
            record,
            {
                "pool": "tank",
                "used": 800,
                "capacity": 0.8,
                "compression_ratio": 1.03,
            },
        )

    def test_jsonl_reports_non_finite_source_values_cleanly(self):
        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = lambda *args, **kwargs: {"used": float("inf")}
            zpool_stats._require_tools = lambda: None
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                returncode = zpool_stats.main(
                    ["tank", "--count", "1", "--format", "jsonl", "--columns", "used"]
                )
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require

        self.assertEqual(returncode, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "zpool-stats: error: non-finite value for column 'used'\n",
        )

    def test_closed_downstream_pipe_exits_zero_without_stderr(self):
        code = """
import zpool_stats

zpool_stats._require_tools = lambda: None
zpool_stats.collect_sample = lambda *args, **kwargs: {"pool": "tank"}
raise SystemExit(
    zpool_stats.main(
        ["tank", "--count", "100000", "--no-status", "--columns", "pool"]
    )
)
"""
        producer = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert producer.stdout is not None
        assert producer.stderr is not None
        producer.stdout.readline()
        producer.stdout.close()
        stderr = producer.stderr.read()
        producer.stderr.close()
        returncode = producer.wait(timeout=10)

        self.assertEqual(returncode, 0)
        self.assertEqual(stderr, "")

    def test_signals_use_shell_exit_codes_without_tracebacks(self):
        code = """
import sys
import zpool_stats

zpool_stats._require_tools = lambda: None

def collect(*args, **kwargs):
    print("ready", flush=True)
    kwargs["runner"]([sys.executable, "-c", "import time; time.sleep(60)"])
    return {"pool": "tank"}

zpool_stats.collect_sample = collect
raise SystemExit(
    zpool_stats.main(
        ["tank", "--count", "1", "--no-status", "--columns", "pool"]
    )
)
"""
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signum):
                process = subprocess.Popen(
                    [sys.executable, "-c", code],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert process.stdout is not None
                assert process.stderr is not None
                self.assertEqual(process.stdout.readline(), "ready\n")
                os.kill(process.pid, signum)
                stdout, stderr = process.communicate(timeout=10)

                self.assertEqual(process.returncode, expected)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "")

    def test_snapshot_refresh_defaults_to_sixty_seconds(self):
        args = zpool_stats.parse_args(["tank", "--count", "1"])
        self.assertEqual(args.snapshot_refresh, 60.0)

    def test_command_timeout_defaults_to_thirty_seconds(self):
        args = zpool_stats.parse_args(["tank", "--count", "1"])
        self.assertEqual(args.command_timeout, 30.0)

    def test_monitor_adds_sampling_interval_to_iostat_timeout(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "1", "--no-status", "--command-timeout", "7"]
        )
        observed = []
        sample = {
            "used": 800,
            "free": 200,
            "total": 1000,
            "capacity": 0.8,
            "read": 300,
            "write": 400,
            "fragmentation": 0.48,
            "compression": 0.03,
            "snapshots": 60,
        }

        def command(command, timeout=None):
            observed.append((command, timeout))
            return ""

        def collect(pool, interval, *, runner, **kwargs):
            runner(["zfs", "get"])
            runner(["zpool", "iostat"])
            return sample

        original_collect = zpool_stats.collect_sample
        original_command = zpool_stats.run_command
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = collect
            zpool_stats.run_command = command
            zpool_stats._require_tools = lambda: None
            with redirect_stdout(io.StringIO()):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats.run_command = original_command
            zpool_stats._require_tools = original_require

        self.assertEqual(observed, [(["zfs", "get"], 7.0), (["zpool", "iostat"], 8.0)])

    def test_snapshot_refresh_accepts_zero_to_disable_caching(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "1", "--snapshot-refresh", "0"]
        )
        self.assertEqual(args.snapshot_refresh, 0.0)

    def test_interval_must_be_positive(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            zpool_stats.parse_args(["tank", "--interval", "0"])
        self.assertIn("greater than zero", stderr.getvalue())

    def test_interval_must_be_finite(self):
        for value in ("nan", "inf"):
            with (
                self.subTest(value=value),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                zpool_stats.parse_args(["tank", "--interval", value])

    def test_pool_can_use_legacy_option(self):
        args = zpool_stats.parse_args(["--pool", "tank", "--count", "1"])
        self.assertEqual(args.pool, "tank")
        self.assertEqual(args.count, 1)

    def test_header_repetition_is_not_configured_by_default(self):
        args = zpool_stats.parse_args(["tank", "--count", "1"])
        self.assertIsNone(args.header_every)

    def test_sticky_header_and_automatic_color_are_defaults(self):
        args = zpool_stats.parse_args(["tank", "--count", "1"])

        self.assertTrue(args.sticky_header)
        self.assertEqual(args.color, "auto")

    def test_sticky_header_can_be_disabled(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "1", "--no-sticky-header", "--color", "never"]
        )

        self.assertFalse(args.sticky_header)
        self.assertEqual(args.color, "never")

    def test_monitor_uses_sticky_colored_header_on_a_tty(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "1", "--no-status", "--columns", "pool,read"]
        )
        output = io.StringIO()
        output.isatty = lambda: True
        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = lambda *args, **kwargs: {
                "pool": "tank",
                "read": 1024,
            }
            zpool_stats._require_tools = lambda: None
            with redirect_stdout(output):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require

        rendered = output.getvalue()
        self.assertTrue(rendered.startswith("\x1b[?25l\x1b[H"))
        self.assertIn("\x1b[1;36mpool  read\x1b[0m", rendered)
        self.assertTrue(rendered.endswith("\x1b[?25h\n"))

    def test_monitor_does_not_repeat_adaptive_header_when_redirected(self):
        args = zpool_stats.parse_args(["tank", "--count", "4", "--no-status"])
        output = self._run_monitor(args)
        self.assertEqual(output.count("used"), 1)

    def test_monitor_honors_explicit_header_interval(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "4", "--no-status", "--header-every", "2"]
        )
        output = self._run_monitor(args)
        self.assertEqual(output.count("used"), 2)

    def test_header_every_zero_stays_single_when_column_width_expands(self):
        args = zpool_stats.parse_args(
            [
                "tank",
                "--count",
                "2",
                "--no-status",
                "--header-every",
                "0",
                "--columns",
                "pool,read",
            ]
        )
        samples = iter(
            [
                {"pool": "a", "read": 1},
                {"pool": "extra-long-pool", "read": 1},
            ]
        )
        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = lambda *args, **kwargs: next(samples)
            zpool_stats._require_tools = lambda: None
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require

        header_lines = [
            line for line in output.getvalue().splitlines() if line.startswith("pool ")
        ]
        self.assertEqual(len(header_lines), 1)

    def test_monitor_reprints_header_when_a_column_width_expands(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "2", "--no-status", "--columns", "pool,read"]
        )
        samples = iter(
            [
                {"pool": "a", "read": 1},
                {"pool": "extra-long-pool", "read": 1},
            ]
        )
        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = lambda *args, **kwargs: next(samples)
            zpool_stats._require_tools = lambda: None
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require

        header_lines = [
            line for line in output.getvalue().splitlines() if line.startswith("pool ")
        ]
        self.assertEqual(len(header_lines), 2)

    def _run_monitor(self, args):
        sample = {
            "pool": "tank",
            "used": 800,
            "free": 200,
            "total": 1000,
            "capacity": 0.8,
            "read": 300,
            "write": 400,
            "fragmentation": 0.48,
            "compression": 0.03,
            "compression_ratio": 1.03,
            "snapshots": 60,
        }
        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = lambda pool, interval, **kwargs: sample
            zpool_stats._require_tools = lambda: None
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(zpool_stats.monitor(args), 0)
            return output.getvalue()
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require

    def test_monitor_passes_selected_columns_to_collector(self):
        args = zpool_stats.parse_args(
            ["tank", "--count", "1", "--no-status", "--columns", "read,write"]
        )
        received_columns = None

        def collect(pool, interval, *, columns, **kwargs):
            nonlocal received_columns
            received_columns = columns
            return {"read": 300, "write": 400}

        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = collect
            zpool_stats._require_tools = lambda: None
            with redirect_stdout(io.StringIO()):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require

        self.assertEqual(
            [column.key for column in received_columns], ["pool", "read", "write"]
        )

    def test_monitor_collects_exact_requested_count(self):
        args = zpool_stats.parse_args(["tank", "--count", "2", "--no-status"])
        sample = {
            "used": 800,
            "free": 200,
            "total": 1000,
            "capacity": 0.8,
            "read": 300,
            "write": 400,
            "fragmentation": 0.48,
            "compression": 0.03,
            "snapshots": 60,
        }
        calls = 0

        def collect(pool, interval, **kwargs):
            nonlocal calls
            calls += 1
            return sample

        original_collect = zpool_stats.collect_sample
        original_require = zpool_stats._require_tools
        try:
            zpool_stats.collect_sample = collect
            zpool_stats._require_tools = lambda: None
            with redirect_stdout(io.StringIO()):
                self.assertEqual(zpool_stats.monitor(args), 0)
        finally:
            zpool_stats.collect_sample = original_collect
            zpool_stats._require_tools = original_require
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
