"""Independent CLI integration probe using explicitly synthetic ZFS commands."""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = """#!/usr/bin/env python3
import os, sys
name = os.path.basename(sys.argv[0])
a = sys.argv[1:]
pool = a[-1]
if name == 'zpool':
    if a[0] == 'status':
        print('  pool: ' + pool + '\\n state: ONLINE')
        print('  scan: scrub in progress since Fri Oct 16 15:36:02 2026')
        print('        746G / 65.3T scanned at 11.5G/s, 0B / 65.3T issued')
        print('        0B repaired, 0.00% done, no estimated completion time')
        print('config:\\n\\n        NAME STATE READ WRITE CKSUM')
        print('        ' + pool + ' ONLINE 0 0 0')
        print('          disk1 ONLINE 0 0 ' + ('2' if pool == 'tank' else '0'))
        print('\\nerrors: ' + ('2 data errors' if pool == 'tank' else 'No known data errors'))
    elif a[0] == 'iostat':
        for p in a[2:-2]:
            print(p + '\\t1000\\t9000\\t1\\t2\\t30\\t40\\t5\\t6')
    elif a[0] == 'list':
        props = a[a.index('-o') + 1].split(',')
        if props == ['name']:
            if os.environ.get('FAKE_DISCOVERY'):
                from pathlib import Path
                state = Path(os.environ['FAKE_DISCOVERY'])
                index = int(state.read_text()) if state.exists() else 0
                states = ['', 'a', 'b', 'a\\nb']
                print(states[min(index, len(states)-1)])
                state.write_text(str(index+1))
            else:
                print('tank\\nbackup')
        else:
            vals = {'name': pool, 'health': 'ONLINE', 'frag': '-', 'size': '10000', 'alloc': '1000', 'free': '9000', 'cap': '10', 'capacity': '10'}
            print('\\t'.join(vals[p] for p in props))
    else:
        sys.exit(9)
else:
    if 'usedbysnapshots' in a:
        if pool == 'tank':
            if os.environ.get('FAKE_TIMEOUT') == '1':
                import time
                time.sleep(3)
            print('synthetic snapshot failure', file=sys.stderr)
            sys.exit(1)
        print(pool + '\\t20')
    else:
        vals = {'used': '1000', 'available': '9000', 'compressratio': '2.00x', 'usedbychildren': '500'}
        for prop in a[-2].split(','):
            print(pool + '\\t' + prop + '\\t' + vals[prop])
"""


def run():
    with tempfile.TemporaryDirectory(
        prefix="cli-smoke-", dir=ROOT / "verification"
    ) as tmp:
        for name in ("zfs", "zpool"):
            path = Path(tmp) / name
            path.write_text(FAKE)
            path.chmod(0o755)
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"])
        common = [
            sys.executable,
            str(ROOT / "zpool_stats.py"),
            "--count",
            "2",
            "--interval",
            "0.001",
            "--command-timeout",
            "5",
        ]
        proc = subprocess.run(
            common
            + [
                "--format",
                "jsonl",
                "--columns",
                "pool,used,fragmentation,compression_ratio,snapshots",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        assert len(rows) == 4, rows
        assert [r["pool"] for r in rows] == ["tank", "backup", "tank", "backup"], rows
        assert all(r["used"] == 1000 and r["compression_ratio"] == 2 for r in rows), (
            rows
        )
        assert all(r["fragmentation"] is None for r in rows), rows
        assert [r["snapshots"] for r in rows] == [None, 20, None, 20], rows
        assert "synthetic snapshot failure" in proc.stderr, proc.stderr
        for output_format, delimiter in (("csv", ","), ("tsv", "\t")):
            machine = subprocess.run(
                common
                + [
                    "--format",
                    output_format,
                    "--columns",
                    "pool,used,fragmentation,snapshots",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            assert machine.returncode == 0, machine.stderr
            records = list(
                csv.DictReader(io.StringIO(machine.stdout), delimiter=delimiter)
            )
            assert len(records) == 4, records
            assert [r["snapshots"] for r in records] == ["", "20", "", "20"], records
            assert all(
                r["fragmentation"] == "" and r["used"] == "1000" for r in records
            ), records
        table = subprocess.run(
            common + ["--no-sticky-header", "--color", "always"],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert table.returncode == 0, table.stderr
        assert "0.00% done" in table.stdout, table.stdout
        assert "2 data errors" in table.stdout, table.stdout
        tank_status = next(
            line for line in table.stdout.splitlines() if "zpool tank is ONLINE" in line
        )
        assert tank_status.startswith(("\x1b[1;31m", "\x1b[1;33m")), repr(tank_status)
        assert "tank" in table.stdout and "backup" in table.stdout
        timeout_run = subprocess.run(
            common
            + [
                "--command-timeout",
                "1",
                "--format",
                "jsonl",
                "--columns",
                "pool,used,snapshots",
            ],
            env=dict(env, FAKE_TIMEOUT="1"),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert timeout_run.returncode == 0, timeout_run.stderr
        timeout_rows = [json.loads(line) for line in timeout_run.stdout.splitlines()]
        assert len(timeout_rows) == 4, timeout_rows
        assert [r["snapshots"] for r in timeout_rows] == [None, 20, None, 20], (
            timeout_rows
        )
        assert "timed out" in timeout_run.stderr, timeout_run.stderr
        for output_format in ("jsonl", "table"):
            discovery = subprocess.run(
                common
                + [
                    "--count",
                    "4",
                    "--pool-refresh",
                    "0",
                    "--format",
                    output_format,
                    "--columns",
                    "pool",
                    "--no-status",
                    "--no-sticky-header",
                    "--header-every",
                    "0",
                ],
                env=dict(
                    env, FAKE_DISCOVERY=str(Path(tmp) / (output_format + ".state"))
                ),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            assert discovery.returncode == 0, discovery.stderr
            if output_format == "jsonl":
                assert [
                    json.loads(line)["pool"] for line in discovery.stdout.splitlines()
                ] == ["a", "b", "a", "b"], discovery.stdout
            else:
                assert "pool" in discovery.stdout.splitlines(), discovery.stdout
        print(
            json.dumps(
                {
                    "synthetic_cli_smoke": "PASS",
                    "json_records": len(rows),
                    "json_null_missing_metrics": True,
                    "snapshot_failure_isolated": True,
                    "scrub_progress_and_errors_visible": True,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    run()
