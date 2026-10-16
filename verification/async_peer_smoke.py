"""Parent-owned integration test: one blocked snapshot must not freeze peers."""

import json
import os
import pty
import re
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
args = sys.argv[1:]
(Path(os.environ['SYNTHETIC_STATE']) / ('pid-' + str(os.getpid()))).write_text(str(os.getpid()))
if Path(sys.argv[0]).name == 'zpool':
    print('a\\nb')
else:
    pool = args[-1]
    if 'usedbysnapshots' in args:
        if pool == 'a':
            counter = Path(os.environ['SYNTHETIC_STATE']) / 'snapshot-counter'
            count = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(count))
            if count > 1:
                time.sleep(30)
        print(pool + '\\t1')
    else:
        path = Path(os.environ['SYNTHETIC_STATE']) / pool
        count = int(path.read_text()) + 1 if path.exists() else 1
        path.write_text(str(count))
        print(pool + '\\tused\\t' + str(count))
"""


def run():
    with tempfile.TemporaryDirectory(dir=ROOT / "verification") as directory:
        folder = Path(directory)
        for name in ("zfs", "zpool"):
            executable = folder / name
            executable.write_text(FAKE)
            executable.chmod(0o755)
        master, slave = pty.openpty()
        env = dict(
            os.environ,
            PATH=str(folder) + os.pathsep + os.environ["PATH"],
            SYNTHETIC_STATE=str(folder),
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "zpool_stats.py"),
                "-i",
                "0.1",
                "--command-timeout",
                "20",
                "--snapshot-refresh",
                "0",
                "--no-status",
                "-c",
                "pool,used,snapshots,snapshot_age,sample_age",
                "--color",
                "never",
            ],
            stdout=slave,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        frames = b""
        try:
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    frames += os.read(master, 65536)
            counts = {
                name: int((folder / name).read_text())
                if (folder / name).exists()
                else 0
                for name in ("a", "b")
            }
            assert counts["a"] >= 3 and counts["b"] >= 3, counts
            assert frames.count(b"\x1b[H") >= 3, frames
            plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", frames)
            ages = [
                float(match.group(1))
                for match in re.finditer(
                    rb"(?:^|[\r\n])a\s+\d+B\s+1B\s+(\d+\.\d+)\s+", plain
                )
            ]
            assert ages and max(ages) >= 1.0, plain[-4000:]
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=2)
            assert process.returncode == 143, (process.returncode, stderr)
            print(
                json.dumps(
                    {
                        "synthetic_blocked_snapshot_peer_updates": "PASS",
                        "property_queries_by_pool": counts,
                        "frames": frames.count(b"\x1b[H"),
                        "max_snapshot_age_seconds": max(ages),
                        "shutdown_seconds": round(time.monotonic() - started, 3),
                    }
                )
            )
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            for record in folder.glob("pid-*"):
                pid = int(record.read_text())
                try:
                    command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
                    if str(folder).encode() in command_line and os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, FileNotFoundError):
                    pass
            os.close(master)


if __name__ == "__main__":
    run()
