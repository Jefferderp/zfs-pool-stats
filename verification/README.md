# Independent CLI smoke probe

Run from the project directory:

```bash
python3 verification/cli_smoke.py
python3 verification/discovery_smoke.py
python3 -m pytest -q verification/test_contracts.py
```

This probe creates temporary **synthetic** `zfs` and `zpool` executables under
this directory, prepends their directory to the child process's `PATH`, and
runs the real CLI. It does not query or modify real ZFS pools. Temporary
executables are removed when the probe exits.

The assertions check:

- A snapshot command failure or timeout for one pool does not stop the other
  pool or suppress successful usage/compression measurements.
- Missing fragmentation and failed snapshot measurements serialize as JSON
  `null` or blank CSV/TSV fields, not healthy-looking zeroes.
- Machine-readable stdout remains parseable; warnings go to stderr.
- Table output includes active scrub progress and data errors.
- An ONLINE pool with errors receives attention coloring rather than green.
- Pool discovery recovers from an empty initial set, adds/removes pools, and
  prints the first table heading even when a pool appears after startup.

A passing result is a local integration check, **not live-host verification**.

The separate contract suite requires the development dependency `pytest`. It
checks unavailable-value propagation, malformed snapshot output, completion-based
cache expiry, command-launch errors, status severity, an 80-column scrub viewport,
and exact raw integer/null serialization across JSONL, CSV, and TSV.
