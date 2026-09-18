# zpool-stats

`zpool-stats` is a small, dependency-free terminal monitor for local OpenZFS
pools. It combines pool I/O, dataset usage, snapshot usage, fragmentation,
compression, health, and scrub status into one readable stream.

```text
zpool tank is ONLINE: scan: scrub repaired 0B in 00:10:05 with 0 errors on Sun Sep 13 00:34:06 2026
pool  used    free  total  cap  read  write  frag  comp  snap
tank  407.1G  1.4T  1.8T   22%  0B    1.1M    9%   28%   16.5G
```

## Requirements

- Python 3.10 or newer
- OpenZFS commands `zpool` and `zfs`
- Permission to query the imported pools and their datasets

No third-party Python packages are required.

## Install

Run directly from a clone:

```bash
git clone https://github.com/Jefferderp/zfs-pool-stats.git
cd zfs-pool-stats
./zfs-pool-stats.py
```

Or install the command with `pipx`:

```bash
pipx install git+https://github.com/Jefferderp/zfs-pool-stats.git
zpool-stats
```

## Usage

```text
zpool-stats [POOL] [--interval SECONDS] [--count N] [--columns SPEC]
                   [--format table|csv|tsv|jsonl] [--snapshot-refresh SECONDS]
                   [--command-timeout SECONDS]
zpool-stats --list-pools
```

Examples:

```bash
# Auto-detect and monitor every imported pool at one-second intervals
zpool-stats

# Monitor only one pool
zpool-stats tank

# Print five sample sets for every imported pool
zpool-stats --count 5

# Select and customize columns
zpool-stats tank --columns used:T:2:USED,free:T:2:FREE,read:M:1,write:M:1

# Add a local ISO 8601 timestamp, or Unix time with millisecond precision
zpool-stats tank --columns timestamp,used,free,read,write
zpool-stats tank --columns unix_time::3,used,free,read,write

# Refresh the recursive snapshot total every five minutes
zpool-stats tank --snapshot-refresh 300

# Emit raw records for scripts, metrics collectors, or log ingestion
zpool-stats tank --count 5 --format csv --columns timestamp,used,free,read,write
zpool-stats tank --count 5 --format tsv --columns timestamp,used,free,read,write
zpool-stats tank --format jsonl --columns unix_time,used,free,read,write

# Discover every supported column
zpool-stats --list-columns

# Discover imported pools available on this host
zpool-stats --list-pools
```

A column specification has the form `NAME[:UNIT[:PRECISION[:HEADER]]]`.
Units apply to byte and time columns only. Byte units are `B`, `K`, `M`, `G`,
`T`, `P`, `E`, `Z`, and `Y`; time units are `ns`, `us`, `ms`, `s`, `m`, `h`,
and `d`. Values use powers of 1024, matching raw OpenZFS byte counters.

The pool name is always the first column, including when `--columns` omits it.
An explicitly configured `pool` column is moved to the front while preserving
its custom header. The default columns are:

```text
pool,used,free,total,capacity,read,write,fragmentation,compression,snapshots
```

`timestamp` is local ISO 8601 time with a numeric UTC offset, such as
`2026-09-14T13:45:02-04:00`. `unix_time` is seconds since the Unix epoch;
increase its table precision with a specification such as `unix_time::3`.

### Machine-readable output

`--format csv` and `--format tsv` write one header followed by comma- or
tab-separated records. CSV fields follow standard CSV quoting rules, making the
stream suitable for spreadsheets and tools such as `csvkit`. `--format jsonl`
writes one JSON object per record with no header. All machine-readable formats
automatically omit the human-readable pool status line and use modern column
names as field names. The first CSV/TSV field or JSON object member is always
`pool`. JSON Lines therefore requires each selected column name to be unique.

CSV, TSV, and JSON Lines return unformatted source values so consumers do not
need to strip display units: bytes and nanoseconds are numbers,
percentage-like fields are ratios (`0.48` means 48%), and unavailable values
are an empty CSV/TSV field or JSON `null`. Column units, precision, and custom
headers affect table output only.

Column names use the modern names shown by `--list-columns`. The old
`--pool/-p` option and `--interval/-t` spelling remain compatible.

## Behavior and caveats

- Collection is local. With no pool argument, imported pools are discovered at
  startup using `zpool list` and each sample set emits one row per pool. Pass a
  pool argument to monitor only that pool. `--count` counts complete sample sets,
  not individual pool rows.
- Use the command on the ZFS host, or invoke it through SSH:
  `ssh host zpool-stats`.
- Only the commands and ZFS properties needed by the selected columns are
  queried. A multi-pool sample set uses one shared `zpool iostat` interval, so
  adding pools does not multiply the requested sampling delay. When no I/O
  columns are selected, the program waits once before collecting the set.
- Snapshot usage is the sum of `usedbysnapshots` for the pool and all descendant
  filesystems/volumes. The recursive query is cached for 60 seconds by default
  to limit overhead on large dataset trees. Use `--snapshot-refresh SECONDS` to
  change the cache lifetime, or `--snapshot-refresh 0` to query every sample.
- Every `zpool` and `zfs` subprocess has a 30-second timeout by default. The
  requested `zpool iostat` sampling interval is added to that limit, so a long
  sampling interval does not consume the command's execution allowance. Change
  the allowance with `--command-timeout SECONDS`.
- Column widths grow when a longer value appears and never shrink during a run.
  The header is reprinted whenever widths expand so its alignment remains clear.
- Output is plain text and works when redirected. In an interactive terminal,
  the header repeats before the previous header scrolls out of the current
  terminal height and adapts when the terminal is resized. Automatic repetition
  is disabled when output is redirected. Use `--header-every N` to set a fixed
  interval or `--header-every 0` to disable repetition.
- A closed downstream pipe exits silently with status 0. `SIGINT`/`Ctrl-C` and
  `SIGTERM` stop collection without a traceback and return the conventional
  shell statuses 130 and 143. Command and parsing failures print a concise
  message and exit with status 1 or 2.
- `compression_ratio` is OpenZFS's `compressratio` value (`1.03x`, for example).
  The shorter `compression` column reports the amount above parity as a
  percentage: `(compressratio - 1) × 100`, so `1.03x` displays as `3%`. It is
  not the percentage of physical space saved; that would be
  `(1 - 1 / compressratio) × 100`.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q zpool_stats.py tests
```

Live tests require an OpenZFS host. Unit tests use captured command-shaped data
and do not require ZFS.

## License

Released under the [MIT License](LICENSE). You may use, copy, modify, merge,
publish, distribute, sublicense, and sell copies of this software, provided the
copyright and license notices are preserved. The software is provided without
warranty; see `LICENSE` for the full terms.
