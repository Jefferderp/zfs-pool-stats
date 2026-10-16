"""Independent real-process checks; synthetic ZFS tools, no real pools touched."""

import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = """#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
name = Path(sys.argv[0]).name
a = sys.argv[1:]
if os.environ.get("COMMAND_LOG"):
    with open(os.environ["COMMAND_LOG"], "a") as log:
        log.write(name + " " + " ".join(a) + "\\n")
if os.environ.get("STALL_SNAPSHOT") and name == "zfs" and "usedbysnapshots" in a:
    time.sleep(60)
if os.environ.get("STALL_STARTUP") and name == "zpool":
    time.sleep(60)
if os.environ.get("FAIL_ALL") or (os.environ.get("FAIL_IO") and name == "zpool" and a[0] == "iostat"):
    sys.stderr.write("synthetic collection failure\\n")
    sys.exit(1)
if name == "zpool":
    if a[0] == "status":
        print("  pool: tank\\n state: ONLINE\\nstatus: Features available.\\naction: Read documentation.\\n  scan: none requested\\nerrors: No known data errors")
    elif a[0] == "iostat":
        time.sleep(float(a[-2]))
        print("tank 100 900 1 2 300 400")
    else:
        fields = a[a.index("-o") + 1].split(",")
        print("\\t".join({"name":"tank", "health":"ONLINE", "frag":"10"}[k] for k in fields))
else:
    if "usedbysnapshots" in a:
        print("tank\\t20")
    else:
        props = a[-2].split(",")
        for prop in props:
            print("tank\\t" + prop + "\\t" + {"used":"100", "available":"900", "compressratio":"1.03", "usedbychildren":"0"}[prop])
"""


def run_checks():
    with tempfile.TemporaryDirectory(dir=ROOT / "verification") as directory:
        path = Path(directory)
        for name in ("zpool", "zfs"):
            exe = path / name
            exe.write_text(FAKE)
            exe.chmod(0o755)
        env = dict(os.environ, PATH=str(path) + os.pathsep + os.environ["PATH"])
        command = [sys.executable, str(ROOT / "zpool_stats.py")]
        common = [
            "tank",
            "-n",
            "1",
            "-i",
            "0.01",
            "--no-status",
            "--format",
            "jsonl",
            "-c",
            "used,read",
        ]
        good = subprocess.run(
            check=False,
            args=command + common,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert good.returncode == 0, good.stderr
        record = json.loads(good.stdout)
        assert record["used"] == 100 and record["read"] == 300, record
        print("PASS real fake-command successful JSON collection")
        bad = subprocess.run(
            check=False,
            args=command + common,
            env=dict(env, FAIL_ALL="1"),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert bad.returncode != 0, (bad.returncode, bad.stdout)
        record = json.loads(bad.stdout)
        assert record["used"] is None and record["read"] is None, record
        print("PASS real fake-command all-failed finite exit", bad.returncode, record)
        for fmt in ("jsonl", "csv", "tsv"):
            import csv
            import io

            partial_command = [fmt if arg == "jsonl" else arg for arg in common]
            partial = subprocess.run(
                check=False,
                args=command + partial_command,
                env=dict(env, FAIL_IO="1"),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert partial.returncode == 0, partial.stderr
            if fmt == "jsonl":
                record = json.loads(partial.stdout)
                assert record["unavailable_columns"] == ["read"], record
            else:
                record = next(
                    csv.DictReader(
                        io.StringIO(partial.stdout),
                        delimiter="," if fmt == "csv" else "\t",
                    )
                )
                assert "read" in record["unavailable_columns"], record
            assert record["collection_status"] == "partial", record
            print("PASS real partial failure schema", fmt, record)
        master, slave = pty.openpty()
        process = subprocess.Popen(
            command + ["--command-timeout", "30", "-i", "0.01"],
            env=dict(env, STALL_STARTUP="1", TERM="xterm"),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        received = b""
        try:
            end = time.monotonic() + 2.5
            while time.monotonic() < end:
                if select.select([master], [], [], 0.1)[0]:
                    received += os.read(master, 65536)
            frames = received.count(b"\x1b[H")
            assert frames >= 2, (frames, received)
            start = time.monotonic()
            process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=2)
            assert code == 143, (code, received)
            print(
                "PASS PTY startup stall responsive frames",
                frames,
                "SIGTERM seconds",
                round(time.monotonic() - start, 3),
            )
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            os.close(master)
        log = path / "commands.log"
        master, slave = pty.openpty()
        process = subprocess.Popen(
            command
            + [
                "tank",
                "--no-status",
                "--command-timeout",
                "30",
                "-i",
                "0.05",
                "-c",
                "used,snapshots,sample_age",
            ],
            env=dict(env, STALL_SNAPSHOT="1", COMMAND_LOG=str(log), TERM="xterm"),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        received = b""
        try:
            end = time.monotonic() + 2
            while time.monotonic() < end:
                if select.select([master], [], [], 0.1)[0]:
                    received += os.read(master, 65536)
            lines = log.read_text().splitlines()
            snapshot_calls = sum("usedbysnapshots" in line for line in lines)
            property_calls = sum("name,property,value" in line for line in lines)
            assert snapshot_calls == 1, lines
            assert property_calls >= 2, lines
            assert b"100B" in received, received
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=2) == 143
            print(
                "PASS stalled snapshot does not block healthy properties; property calls",
                property_calls,
                "snapshot calls",
                snapshot_calls,
            )
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            os.close(master)


if __name__ == "__main__":
    run_checks()
