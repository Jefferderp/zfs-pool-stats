import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

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


class ColumnTests(unittest.TestCase):
    def test_default_columns_match_monitor_layout(self):
        columns = zpool_stats.parse_columns(None)
        names = [column.header for column in columns]
        self.assertEqual(
            names,
            ["used", "free", "total", "cap", "read", "write", "frag", "comp", "snap"],
        )
        capacity = next(column for column in columns if column.key == "capacity")
        self.assertEqual(capacity.precision, 0)

    def test_legacy_names_and_custom_format_are_supported(self):
        columns = zpool_stats.parse_columns("VirtCapUsed:T:2:USED,BwRead:M")
        self.assertEqual(columns[0].key, "used")
        self.assertEqual(columns[0].unit, "T")
        self.assertEqual(columns[0].precision, 2)
        self.assertEqual(columns[0].header, "USED")
        self.assertEqual(columns[1].key, "read")

    def test_unknown_column_is_rejected(self):
        with self.assertRaises(ValueError):
            zpool_stats.parse_columns("used,definitely-not-a-column")


class CollectorTests(unittest.TestCase):
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

        def collect(pool, interval):
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
