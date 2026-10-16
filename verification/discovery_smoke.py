"""Real CLI against a synthetic changing pool inventory, never live ZFS."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if Path(sys.argv[0]).name == 'zpool':
    assert sys.argv[1:] == ['list', '-H', '-o', 'name'], sys.argv
    path = Path(os.environ['SYNTHETIC_INVENTORY'])
    data = json.loads(path.read_text())
    index = data['index']
    data['index'] += 1
    path.write_text(json.dumps(data))
    inventory = data['inventories'][min(index, len(data['inventories']) - 1)]
    if inventory is None:
        print('synthetic inventory failure', file=sys.stderr)
        sys.exit(1)
    for pool in inventory:
        print(pool)
else:
    assert sys.argv[1] == 'get', sys.argv
    print(sys.argv[-1] + '\\tused\\t42')
"""


def run():
    with tempfile.TemporaryDirectory(
        prefix="inventory-smoke-", dir=ROOT / "verification"
    ) as tmp:
        path = Path(tmp)
        for name in ("zpool", "zfs"):
            executable = path / name
            executable.write_text(FAKE)
            executable.chmod(0o755)
        state = path / "inventory.json"
        state.write_text(
            json.dumps(
                {
                    "index": 0,
                    "inventories": [
                        ["tank", "backup"],
                        ["backup", "newpool"],
                        [],
                        ["tank"],
                    ],
                }
            )
        )
        env = dict(
            os.environ,
            PATH=tmp + os.pathsep + os.environ["PATH"],
            SYNTHETIC_INVENTORY=str(state),
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "zpool_stats.py"),
                "--count",
                "4",
                "--interval",
                "0.001",
                "--pool-refresh",
                "0",
                "--format",
                "jsonl",
                "--columns",
                "used",
                "--color",
                "always",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        records = [json.loads(line) for line in result.stdout.splitlines()]
        assert [record["pool"] for record in records] == [
            "tank",
            "backup",
            "backup",
            "newpool",
            "tank",
        ], records
        assert all(record["used"] == 42 for record in records), records
        assert json.loads(state.read_text())["index"] == 4
        assert "\x1b" not in result.stdout
        state.write_text(json.dumps({"index": 0, "inventories": [[], ["a"]]}))
        table = subprocess.run(
            [
                sys.executable,
                str(ROOT / "zpool_stats.py"),
                "--count",
                "2",
                "--interval",
                "0.001",
                "--pool-refresh",
                "0",
                "--columns",
                "pool",
                "--no-status",
                "--no-sticky-header",
                "--color",
                "never",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert table.returncode == 0, table.stderr
        lines = table.stdout.splitlines()
        assert lines[-2:] == ["pool", "a"], lines
        print(
            json.dumps(
                {
                    "synthetic_discovery_churn": "PASS",
                    "records": len(records),
                    "empty_inventory_recovered": True,
                }
            )
        )


if __name__ == "__main__":
    run()
