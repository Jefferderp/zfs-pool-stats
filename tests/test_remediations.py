import io
from unittest.mock import patch

import zpool_stats as z


def test_command_status_locale_is_deterministic():
    import sys

    assert (
        z.run_command(
            [sys.executable, "-c", "import os; print(os.environ['LC_ALL'])"]
        ).strip()
        == "C"
    )


def test_recovered_shared_iostat_not_suppressed_by_pool_backoff():
    now = [0.0]
    attempts = [0]

    def run(command):
        if command[1] == "iostat":
            attempts[0] += 1
            now[0] += 2
            if attempts[0] == 1:
                raise z.ZfsCommandError("shared failure")
            return "a 1 9 1 1 7 8\nb 1 9 1 1 7 8\n"
        if command[-1] == "a" and attempts[0] == 1:
            now[0] += 100
        return f"{command[-1]}\tused\t1\n"

    state = z.RetryState(clock=lambda: now[0])
    kwargs = {
        "columns": z.parse_columns("read,used"),
        "retry_state": state,
        "clock": lambda: now[0],
        "sleeper": lambda _: None,
    }
    z.collect_sample_set(["a", "b"], 2, run, **kwargs)
    rows = z.collect_sample_set(["a", "b"], 2, run, **kwargs)
    assert [r["read"] for r in rows] == [7, 7]


def test_first_header_after_empty_inventory(capsys):
    args = z.parse_args(
        ["--count", "2", "--pool-refresh", "0", "--no-status", "--columns", "pool"]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(z, "discover_pools", side_effect=[[], ["a"]]),
        patch.object(z, "collect_sample_set", return_value=[{"pool": "a"}]),
        patch.object(z.time, "sleep"),
    ):
        assert z.monitor(args) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Waiting for imported ZFS pools",
        "pool",
        "a",
    ]


def test_release_defaults_use_honest_ratio_and_visible_freshness():
    assert z.VERSION == "1.9.3"
    columns = z.parse_columns(None)
    keys = {c.key for c in columns}
    assert "compression_ratio" in keys
    assert not {"snapshot_age", "sample_age", "collection_duration"} & keys
    assert "compression" not in keys
    ratio = next(c for c in columns if c.key == "compression_ratio")
    assert z.format_column(ratio, 1.03) == "1.03x"


def test_display_timing_includes_status_delay_and_snapshot_age(capsys):
    now = [0.0]
    args = z.parse_args(
        [
            "tank",
            "--count",
            "1",
            "--columns",
            "pool,collection_duration,sample_age,snapshot_age",
            "--color",
            "never",
        ]
    )

    def collect(*a, **kw):
        now[0] += 10
        kw["snapshot_cache"].values["tank"] = (now[0], 1)
        return [
            {
                "pool": "tank",
                "snapshot_age": 0,
                "collection_duration": 10,
                "sample_age": 0,
                "_measured_at": now[0],
            }
        ]

    def status(*a):
        now[0] += 50
        return "zpool tank is ONLINE"

    with (
        patch.object(z, "_require_tools"),
        patch.object(z.time, "monotonic", side_effect=lambda: now[0]),
        patch.object(z, "collect_sample_set", side_effect=collect),
        patch.object(z, "status_line", side_effect=status),
    ):
        assert z.monitor(args) == 0
    assert capsys.readouterr().out.splitlines()[-1].split() == [
        "tank",
        "60.0",
        "50.0",
        "50.0",
    ]


def test_discovery_reconciles_empty_add_remove_and_explicit_selection(capsys):
    args = z.parse_args(
        [
            "--count",
            "4",
            "--pool-refresh",
            "0",
            "--no-status",
            "--columns",
            "pool",
            "--format",
            "jsonl",
        ]
    )
    selections = []

    def collect(pools, *a, **kw):
        selections.append(list(pools))
        return [{"pool": pool} for pool in pools]

    with (
        patch.object(z, "_require_tools"),
        patch.object(z, "discover_pools", side_effect=[[], ["a"], ["a", "b"], ["b"]]),
        patch.object(z, "collect_sample_set", side_effect=collect),
        patch.object(z.time, "sleep") as sleep,
    ):
        assert z.monitor(args) == 0
    assert selections == [["a"], ["a", "b"], ["b"]]
    sleep.assert_called()
    args.pool = "chosen"
    args.count = 1
    with (
        patch.object(z, "_require_tools"),
        patch.object(z, "discover_pools") as discover,
        patch.object(z, "collect_sample_set", side_effect=collect),
    ):
        assert z.monitor(args) == 0
        discover.assert_not_called()
    assert selections[-1] == ["chosen"]


def test_structured_history_survives_width_expansion_for_other_pools():
    output = io.StringIO()
    renderer = z.StickyTableRenderer(output, [], color=False, pools=["a", "b"])
    formatter = z.TableFormatter(z.parse_columns("pool,used"))
    renderer.draw_sample({"pool": "a", "used": 1}, formatter)
    renderer.draw_sample({"pool": "b", "used": 2}, formatter)
    formatter.widths[0] = 30
    renderer.draw_sample({"pool": "a", "used": 3}, formatter)
    assert renderer.pool_rows["b"] == [formatter.format({"pool": "b", "used": 2})[1]]
    assert len(renderer.pool_rows["a"]) == 2


def test_status_failure_after_collection_does_not_drop_rows(capsys):
    args = z.parse_args(
        ["tank", "--count", "2", "--columns", "used", "--color", "never"]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(
            z,
            "status_line",
            side_effect=["zpool tank is ONLINE", z.ZfsCommandError("status failed")],
        ),
        patch.object(
            z, "collect_sample_set", return_value=[{"pool": "tank", "used": 23}]
        ),
    ):
        assert z.monitor(args) == 0
    output = capsys.readouterr()
    assert output.out.count("23B") == 2
    assert "UNAVAILABLE" in output.out
    assert "status failed" in output.err


def test_status_continuations_and_scaled_device_errors():
    output = """state: ONLINE
scan: scrub in progress since Friday
    1T scanned at 2G/s, 20G issued at 100M/s
    10% done, 01:00:00 to go
config:
    NAME STATE READ WRITE CKSUM
    tank ONLINE 0 0 0
    disk ONLINE 0 0 1.2K
errors: 2 data errors
"""
    line = z.status_line("tank", lambda _: output)
    for text in (
        "10% done",
        "100M/s",
        "01:00:00",
        "1.2K",
        "2 data errors",
        "ATTENTION",
    ):
        assert text in line
    assert line.index("ATTENTION") < line.index("scan:")
    assert z.color_status(line, True).startswith(z.ANSI_BOLD_YELLOW)


def test_single_pool_failed_iostat_is_paced_and_timed():
    now = [0.0]

    def sleep(delay):
        now[0] += delay

    def fail(_):
        raise z.ZfsCommandError("gone")

    state = z.RetryState(clock=lambda: now[0])
    for _ in range(2):
        row = z.collect_sample(
            "a",
            2,
            fail,
            columns=z.parse_columns("read,collection_duration,sample_age"),
            sleeper=sleep,
            clock=lambda: now[0],
            retry_state=state,
        )
        assert row["collection_duration"] == 2
        assert row["sample_age"] is None
    assert now[0] == 4


def test_unknown_operands_preserve_known_properties():
    sample = z.collect_sample(
        "tank",
        1,
        lambda _: "tank\tused\t12\ntank\tavailable\t-\ntank\tcompressratio\t-\n",
        columns=z.parse_columns("used,free,total,capacity,compression"),
        sleeper=lambda _: None,
    )
    assert sample["used"] == 12
    assert all(sample[k] is None for k in ("free", "total", "capacity", "compression"))
    assert z.parse_snapshot_usage("tank\t-\ntank/child\t10\n") is None


def test_failed_shared_iostat_keeps_each_pool_properties_and_paces():
    now = [0.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    def run(command):
        if command[1] == "iostat":
            raise z.ZfsCommandError("iostat gone")
        return f"{command[-1]}\tused\t42\n"

    state = z.RetryState(clock=lambda: now[0])
    for _ in range(2):
        rows = z.collect_sample_set(
            ["a", "b"],
            10,
            run,
            columns=z.parse_columns("read,used"),
            sleeper=sleep,
            clock=lambda: now[0],
            retry_state=state,
        )
        assert [(r["read"], r["used"]) for r in rows] == [(None, 42), (None, 42)]
    assert sum(sleeps) >= 20


def test_snapshot_failure_isolated_with_backoff(capsys):
    now = [0.0]
    calls = []

    def run(command):
        calls.append(command)
        if "usedbysnapshots" in command:
            raise z.ZfsCommandError("snapshot failed")
        return "tank\tused\t23\n"

    state = z.RetryState(clock=lambda: now[0])
    columns = z.parse_columns("used,snapshots")

    def sample():
        return z.collect_sample(
            "tank", 10, run, columns=columns, retry_state=state, sleeper=lambda _: None
        )

    assert sample()["used"] == 23
    assert sample()["snapshots"] is None
    assert sum("usedbysnapshots" in c for c in calls) == 1
    assert "snapshot failed" in capsys.readouterr().err
    now[0] = 61
    sample()
    assert sum("usedbysnapshots" in c for c in calls) == 2


def test_snapshot_age_starts_at_completion():
    now = [10.0]

    def run(command):
        now[0] = 40.0
        return "tank\t12\n"

    cache = z.SnapshotCache()
    assert cache.get("tank", 60, run, lambda: now[0]) == 12
    assert cache.values["tank"][0] == 40
    sample = z.collect_sample(
        "tank",
        10,
        run,
        columns=z.parse_columns("snapshots,snapshot_age"),
        snapshot_cache=cache,
        snapshot_refresh=60,
        clock=lambda: now[0],
        sleeper=lambda _: None,
    )
    assert sample["snapshot_age"] == 0


def test_unknown_numbers_are_not_zero():
    assert z._number("-") is None
    assert z._integer("") is None
    assert z.parse_snapshot_usage("") is None
    assert z.parse_snapshot_usage("tank\t-\n") is None
    assert z.parse_snapshot_usage("tank\t0\n") == 0
    assert (
        z.format_column_value(z.Column("used", "used", "bytes"), {"used": None}) == "-"
    )
    assert z.parse_pool_properties("tank -", ["frag"])["frag"] is None
    assert z.parse_pool_list("tank ONLINE -") == ("ONLINE", None)
    assert z.parse_iostat("tank - - - - - - - -", "tank")["total_wait"] is None
