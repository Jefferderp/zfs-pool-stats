import io
import os
from unittest.mock import patch

import zpool_stats as z


def test_status_action_advisory_preserved():
    health, detail = z.parse_status(
        "state: ONLINE\nstatus: Features disabled\n    on this pool.\naction: Upgrade if desired.\n    Consult documentation.\nscan: none requested\nerrors: No known data errors\n"
    )
    assert health == "ONLINE"
    assert "status: Features disabled" in detail
    assert "on this pool." in detail
    assert "action: Upgrade if desired." in detail
    assert "Consult documentation." in detail
    assert "ADVISORY" in detail and "ATTENTION" not in detail
    assert z.color_status("zpool tank is ONLINE: " + detail, True).startswith(
        z.ANSI_BOLD_YELLOW
    )


def test_measurement_completion_and_dynamic_history_age():
    now = [0.0]

    def run(_):
        now[0] += 5
        return "tank\tused\t1\n"

    row = z.collect_sample(
        "tank",
        1,
        run,
        columns=z.parse_columns("used,sample_age,collection_duration"),
        clock=lambda: now[0],
        sleeper=lambda _: None,
    )
    assert row["sample_age"] == 0
    assert row["collection_duration"] == 5
    now[0] = 20
    with patch.object(z.time, "monotonic", side_effect=lambda: now[0]):
        assert (
            z.format_column_value(z.Column("sample_age", "age", "number"), row)
            == "15.0"
        )
    assert row["sample_age"] == 0


def test_finite_failures_explicit_in_machine_formats(capsys):
    import json

    for fmt in ("jsonl", "csv", "tsv"):
        args = z.parse_args(
            [
                "tank",
                "-n",
                "1",
                "-i",
                "0.001",
                "--format",
                fmt,
                "-c",
                "used,read",
                "--no-status",
            ]
        )
        for partial in (False, True):

            def run(cmd, partial=partial, **kw):
                if partial and cmd[0] == "zfs":
                    return "tank\tused\t12\n"
                raise z.ZfsCommandError("failed")

            with (
                patch.object(z, "_require_tools"),
                patch.object(z, "run_command", side_effect=run),
            ):
                assert z.monitor(args) == (0 if partial else 1)
            out = capsys.readouterr().out
            assert "collection_status" in out and "unavailable_columns" in out
            assert ("partial" if partial else "failed") in out
            if fmt == "jsonl":
                assert json.loads(out)["unavailable_columns"] == (
                    ["read"] if partial else ["used", "read"]
                )


def test_only_viewport_history_is_formatted():
    renderer = z.StickyTableRenderer(
        io.StringIO(),
        [],
        color=False,
        pools=["tank"],
        terminal_size=lambda: os.terminal_size((80, 10)),
    )
    renderer.samples["tank"] = [{"pool": "tank", "used": n} for n in range(10000)]
    formatter = z.TableFormatter(z.parse_columns("used"))
    with patch.object(formatter, "format", wraps=formatter.format) as formatted:
        renderer.draw_sample({"pool": "tank", "used": 4}, formatter)
    assert formatted.call_count <= 22
    assert len(renderer.samples["tank"]) == 10000


def test_every_visible_pool_summary_precedes_long_details():
    out = io.StringIO()
    renderer = z.StickyTableRenderer(
        out,
        ["zpool a is ONLINE: " + "long " * 100, "zpool b is FAULTED: broken"],
        detailed_status=True,
        color=False,
        pools=["a", "b"],
        terminal_size=lambda: os.terminal_size((40, 10)),
    )
    renderer.draw_pool("pool used", "a 1", pool="a")
    frame = out.getvalue()
    assert "zpool b is FAULTED" in frame
    assert frame.index("zpool b is FAULTED") < frame.index("long")


def test_blocked_startup_redraw_and_signal_shutdown(tmp_path):
    import pty
    import select
    import signal
    import subprocess
    import sys
    import time

    fake = tmp_path / "zpool"
    fake.write_text('#!/bin/sh\necho $$ > "$PID_FILE"\nsleep 60\n')
    fake.chmod(0o755)
    (tmp_path / "zfs").symlink_to(fake)
    for sig in (signal.SIGINT, signal.SIGTERM):
        master, slave = pty.openpty()
        env = {
            **os.environ,
            "PATH": str(tmp_path) + ":" + os.environ["PATH"],
            "PID_FILE": str(tmp_path / "pid"),
        }
        proc = subprocess.Popen(
            [sys.executable, "zpool_stats.py", "--command-timeout", "30", "-c", "used"],
            stdout=slave,
            stderr=subprocess.PIPE,
            env=env,
        )
        os.close(slave)
        data = b""
        deadline = time.monotonic() + 0.8
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    data += os.read(master, 65536)
            proc.send_signal(sig)
            proc.communicate(timeout=2)
            assert proc.returncode == 128 + sig
            while select.select([master], [], [], 0.05)[0]:
                try:
                    data += os.read(master, 65536)
                except OSError:
                    break
            assert data.count(b"\x1b[H") >= 2
            assert b"\x1b[?25h" in data
        finally:
            proc.kill() if proc.poll() is None else None
            os.close(master)


def test_shared_measurement_age_not_reset_by_later_properties():
    now = [0.0]

    def run(cmd):
        now[0] += 10
        if cmd[1] == "iostat":
            return "a 1 9 1 1 7 8\nb 1 9 1 1 7 8\n"
        return f"{cmd[-1]}\tused\t12\n"

    rows = z.collect_sample_set(
        ["a", "b"],
        1,
        run,
        columns=z.parse_columns("read,used,sample_age"),
        clock=lambda: now[0],
    )
    assert [r["sample_age"] for r in rows] == [20, 20]
    assert [r["_measured_at"] for r in rows] == [10, 10]


def test_unreapable_children_global_cap_and_nonblocking_close():
    import time
    from unittest.mock import MagicMock

    controller = z.CommandController()
    child = MagicMock()
    child.poll.return_value = None
    with (
        patch.object(z.subprocess, "Popen", return_value=child) as spawn,
        patch.object(controller, "kill"),
    ):
        start = time.monotonic()
        for number in range(8):
            try:
                controller.run(["fake", str(number)], timeout=0.001)
            except z.ZfsCommandError:
                pass
        controller.close()
    assert spawn.call_count <= 4
    assert time.monotonic() - start < 1


def test_async_metadata_count_waits_for_status_and_emits_rows():
    output = io.StringIO()
    output.isatty = lambda: True
    args = z.parse_args(
        [
            "tank",
            "-n",
            "2",
            "-i",
            "0.01",
            "-c",
            "pool",
            "--color",
            "never",
            "--detailed-status",
        ]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(
            z, "status_line", return_value="zpool tank is ONLINE: advisory text"
        ),
        patch.object(z.sys, "stdout", output),
    ):
        assert z.monitor(args) == 0
    assert "advisory text" in output.getvalue()
    assert output.getvalue().count("tank") >= 3


def test_summary_retains_advisory_color():
    output = io.StringIO()
    renderer = z.StickyTableRenderer(
        output,
        ["zpool a is ONLINE: ADVISORY; action: upgrade"],
        color=True,
        pools=["a"],
        terminal_size=lambda: os.terminal_size((80, 8)),
    )
    renderer.draw_pool("pool", "a", pool="a")
    assert z.ANSI_BOLD_YELLOW + "zpool a is ONLINE" in output.getvalue()
    assert z.ANSI_BOLD_RED not in output.getvalue()


def test_snapshot_age_failure_and_failed_discovery_exit_nonzero(capsys):
    for argv in (["tank", "-c", "snapshot_age"], ["-c", "timestamp"]):
        args = z.parse_args([*argv, "-n", "1", "-i", "0.001", "--format", "jsonl"])
        with (
            patch.object(z, "_require_tools"),
            patch.object(
                z, "run_command", side_effect=z.ZfsCommandError("unavailable")
            ),
        ):
            assert z.monitor(args) == 1


def test_public_command_path_uses_bounded_controller():
    import sys

    with patch.object(z.CommandController, "run", return_value="bounded") as run:
        assert (
            z.run_command([sys.executable, "-c", "print('legacy')"], timeout=1)
            == "bounded"
        )
    run.assert_called_once()


def test_finite_never_returning_worker_is_bounded():
    import subprocess
    import sys

    code = """
import io, threading
from unittest.mock import patch
import zpool_stats as z
out = io.StringIO()
out.isatty = lambda: True
with patch.object(z, "_require_tools"), patch.object(z.CommandController, "run", side_effect=lambda *a, **k: threading.Event().wait()), patch.object(z.sys, "stdout", out):
    result = z.main(["tank", "-n", "1", "-i", "0.01", "--command-timeout", "0.05", "--no-status", "-c", "used"])
print(result, "restored", "\\x1b[?25h" in out.getvalue())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert result.stdout.strip() == "1 restored True"


def test_removed_reimported_pool_discards_old_generation():
    import threading

    output = io.StringIO()
    output.isatty = lambda: True
    released = threading.Event()
    calls = [0, 0]

    def discover(_):
        calls[0] += 1
        if calls[0] == 2:
            return []
        if calls[0] >= 3:
            released.set()
        return ["tank"]

    def collect(*a, **kw):
        calls[1] += 1
        if calls[1] == 1:
            released.wait(1)
            return {"pool": "tank", "used": 999}
        return {"pool": "tank", "used": 42}

    args = z.parse_args(
        ["-n", "3", "-i", "0.01", "--pool-refresh", "0", "--no-status", "-c", "used"]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(z, "discover_pools", side_effect=discover),
        patch.object(z, "_collect_sample", side_effect=collect),
        patch.object(z.sys, "stdout", output),
    ):
        assert z.monitor(args) == 0
    assert "999B" not in output.getvalue()
    assert "42B" in output.getvalue()


def test_hidden_pool_records_do_not_affect_widths():
    renderer = z.StickyTableRenderer(
        io.StringIO(),
        ["zpool a is ONLINE", "zpool b is ONLINE"],
        color=False,
        pools=["a", "b"],
        terminal_size=lambda: os.terminal_size((40, 3)),
    )
    renderer.samples = {
        "a": [{"pool": "a", "health": "ONLINE"}],
        "b": [{"pool": "b", "health": "X" * 500}],
    }
    formatter = z.TableFormatter(z.parse_columns("health"))
    renderer.redraw(formatter)
    assert max(formatter.widths) < 20


def test_expired_operation_recovers_without_query_storm():
    import time
    from types import SimpleNamespace

    import pytest

    started = time.monotonic()
    calls = []
    latest = []

    def collect(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.02)
        return {"pool": "tank", "used": 42}

    def redraw(renderer, formatter):
        latest[:] = renderer.samples.get("tank", [])
        if time.monotonic() - started > 0.35:
            raise z.SignalExit(2)

    clock = SimpleNamespace(
        monotonic=lambda: (time.monotonic() - started) * 100,
        time=time.time,
        sleep=lambda delay: time.sleep(delay / 100),
    )
    args = z.parse_args(
        ["tank", "-i", "1", "--command-timeout", "0.05", "--no-status", "-c", "used"]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(z, "_collect_sample", side_effect=collect),
        patch.object(z, "time", clock),
        patch.object(z.StickyTableRenderer, "redraw", redraw),
        pytest.raises(z.SignalExit),
    ):
        z.interactive_monitor(args)
    assert latest[-1]["used"] == 42
    assert len(calls) < 40


def test_saturated_workers_do_not_hang_finite_inventory():
    import subprocess
    import sys

    code = """
import io, threading
from unittest.mock import patch
import zpool_stats as z
out = io.StringIO()
out.isatty = lambda: True
with patch.object(z, "_require_tools"), patch.object(z, "discover_pools", return_value=list("abcde")), patch.object(z, "_collect_sample", side_effect=lambda *a, **k: threading.Event().wait()), patch.object(z.sys, "stdout", out):
    result = z.main(["-n", "1", "-i", "0.01", "--command-timeout", "0.05", "--no-status", "-c", "used"])
print(result)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert result.stdout.strip() == "1"


def test_async_short_iostat_clears_old_latency():
    rows = [{"pool": "tank", "read": 8, "read_wait": 5}, {"pool": "tank", "read": 9}]
    shown = []

    def draw(renderer, formatter):
        shown[:] = renderer.samples.get("tank", [])

    args = z.parse_args(
        ["tank", "-n", "2", "-i", "0.01", "--no-status", "-c", "read,read_wait"]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(z, "_collect_sample", side_effect=rows),
        patch.object(z.StickyTableRenderer, "redraw", draw),
    ):
        assert z.interactive_monitor(args) == 0
    assert shown[-1]["read"] == 9
    assert shown[-1]["read_wait"] is None


def test_command_controller_initialization_is_singleton():
    import threading
    import time

    made = []

    class Controller:
        def __init__(self):
            made.append(self)
            self.stop = threading.Event()
            time.sleep(0.03)

        def run(self, *args):
            return "ok"

    with (
        patch.object(z, "_command_controller", None),
        patch.object(z, "CommandController", Controller),
    ):
        threads = [
            threading.Thread(target=lambda: z.run_command(["fake"])) for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
    assert len(made) == 1


def test_old_child_cleanup_does_not_unregister_replacement():
    from unittest.mock import MagicMock

    controller = z.CommandController()
    first, second = MagicMock(), MagicMock()
    first.poll.return_value = 0
    first.returncode = 0
    second.poll.return_value = None
    files = [MagicMock() for _ in range(4)]
    for file in files:
        file.read.return_value = b"ok"

    def replace():
        try:
            controller.run(["fake"], timeout=0.001)
        except z.ZfsCommandError:
            pass

    files[0].close.side_effect = replace
    with (
        patch.object(z.subprocess, "Popen", side_effect=[first, second]),
        patch.object(z.tempfile, "TemporaryFile", side_effect=files),
        patch.object(controller, "kill"),
    ):
        controller.run(["fake"], timeout=1)
        assert controller.children.get(("fake",)) is second
        controller.close()


def test_reimport_during_saturation_finishes_finite_run():
    import subprocess
    import sys

    code = """
import io, threading
from unittest.mock import patch
import zpool_stats as z
out = io.StringIO()
out.isatty = lambda: True
calls = [0]
def discover(_):
    calls[0] += 1
    if calls[0] == 2:
        return []
    if calls[0] > 3:
        threading.Event().wait()
    return ["tank", "a", "b"]
with patch.object(z, "_require_tools"), patch.object(z, "discover_pools", side_effect=discover), patch.object(z, "_collect_sample", side_effect=lambda *a, **k: threading.Event().wait()), patch.object(z.sys, "stdout", out):
    result = z.main(["-n", "3", "-i", "0.01", "--pool-refresh", "0", "--command-timeout", "0.3", "--no-status", "-c", "used"])
print(result)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert result.stdout.strip() == "1"


def test_async_discovery_failure_warns_and_recovers(capsys):
    calls = []

    def discover(_):
        calls.append(1)
        if len(calls) == 1:
            raise z.ZfsCommandError("discovery permission denied")
        return ["tank"]

    args = z.parse_args(
        ["-n", "2", "-i", "0.01", "--pool-refresh", "0", "--no-status", "-c", "pool"]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(z, "discover_pools", side_effect=discover),
    ):
        assert z.interactive_monitor(args) == 0
    captured = capsys.readouterr()
    assert "discovery permission denied" in captured.err
    assert "tank" in captured.out


def test_failed_async_attempt_does_not_reuse_success_duration():
    shown = []

    def draw(renderer, formatter):
        shown[:] = renderer.samples.get("tank", [])

    args = z.parse_args(
        [
            "tank",
            "-n",
            "2",
            "-i",
            "0.01",
            "--no-status",
            "-c",
            "used,collection_duration",
        ]
    )
    with (
        patch.object(z, "_require_tools"),
        patch.object(
            z,
            "_collect_sample",
            side_effect=[{"pool": "tank", "used": 1}, z.ZfsCommandError("timeout")],
        ),
        patch.object(z.StickyTableRenderer, "redraw", draw),
    ):
        assert z.interactive_monitor(args) == 0
    assert shown[-1]["used"] is None
    assert shown[-1]["collection_duration"] is None


def test_zero_and_subnanosecond_units():
    assert z.format_time(0) == "0.0ns"
    assert z.format_time(0.5) == "0.5ns"
    assert z.format_time(-0.5) == "-0.5ns"
