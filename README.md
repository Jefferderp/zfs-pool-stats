# zpool-stats

> [!WARNING]
> This project is vibe-coded, not production-ready, and largely untested. Use it at your own risk, review the code before running it, and do not rely on it for critical monitoring or operational decisions.

`zpool-stats` is a small, dependency-free terminal monitor for local OpenZFS
pools. It combines pool I/O, dataset usage, snapshot usage, fragmentation,
compression, health, and scrub status into one readable stream.

```text
zpool tank is ONLINE: scan: scrub repaired 0B in 00:10:05 with 0 errors on Sun Sep 13 00:34:06 2026
pool  free  used  total  cap  read  write  frag  ratio(x)  snap   snap_age_s  age_s  collect_s
tank  1.4T  407G  1.8T   22%  0B    1.1M   9%    1.28x     16.5G  0.0         0.2    10.2
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
zpool-stats [POOL | --all] [--interval SECONDS] [--count N] [--columns SPEC]
                   [--format table|csv|tsv|jsonl] [--snapshot-refresh SECONDS]
                   [--pool-refresh SECONDS]
                   [--command-timeout SECONDS] [--no-sticky-header]
                   [--color auto|always|never] [--header-every N]
zpool-stats --list-pools
```

Examples:

```bash
# Auto-detect every imported pool; request a 10-second I/O sampling interval
zpool-stats

# Explicitly monitor every imported pool
zpool-stats --all

# Monitor only one pool
zpool-stats tank

# Use streaming output instead of the interactive sticky viewport
zpool-stats tank --no-sticky-header

# Disable color, or force ANSI color through a terminal-aware pager
zpool-stats tank --color never
zpool-stats tank --color always | less -R

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
and `d`. Byte quantities use decimal (base-1000) units by default. Use
`--decimal` to select this explicitly or `--binary` for base-1024 units.
The flags are mutually exclusive and apply to all table byte columns, including
bandwidth, snapshots, and explicitly selected column units. Both modes retain
the compact `K`, `M`, `G`, `T`, etc. suffixes. For example, 10,000,000,000,000
bytes displays as `10.0T` in decimal mode or `9.1T` in binary mode.
Raw CSV, TSV, and JSON Lines values and percentage calculations are unchanged.
With the default precision, byte values at 100 units or more are rounded to the
nearest whole unit (`688.1G` becomes `688G`; `1000.5G` becomes `1001G`). An
explicit precision other than one preserves the requested number of decimals.
Percentage columns display whole numbers by default (`71%`, `103%`); use
`fragmentation::1` or `compression::1` to request one decimal place.

The pool name is always the first column, including when `--columns` omits it.
An explicitly configured `pool` column is moved to the front while preserving
its custom header. The default columns are:

```text
pool,free,used,total,capacity,read,write,fragmentation,compression_ratio::2,snapshots,snapshot_age,sample_age,collection_duration
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

Version 1.9.0 always appends `collection_status` (`ok`, `partial`, or `failed`)
and `unavailable_columns`, even with an explicit `--columns`. The latter is a
JSON array in JSONL and a semicolon-separated list in CSV/TSV. Availability is
assessed against requested ZFS measurements, including `snapshot_age`, not
local metadata (`pool`, timestamps, sample age, collection duration). This is an
intentional machine-schema change. `ok` means measurements were available, not
that the pool is healthy. Status/action text remains table-only.

Column names use the modern names shown by `--list-columns`. The old
`--pool/-p` option and `--interval/-t` spelling remain compatible.

## Behavior and caveats

- Collection is local. With no pool argument, imported pools are discovered at
  startup using `zpool list` and reconciled every 60 seconds. `--all` requests
  the same behavior explicitly. `--pool-refresh SECONDS` changes this period;
  zero reconciles every polling cycle. Imported pools appear, exported pools
  disappear, and removed pools lose cached values, retry state, and viewport
  history. Discovery failure retains the last inventory with a warning; a
  successful empty inventory shows a waiting screen and keeps polling. An
  explicit pool argument never triggers automatic discovery. Streaming `--count`
  counts polling cycles (including empty inventories). In the sticky UI it counts
  completed attempts per selected query family and pool, including status; it
  does not count redraws or rows. Metadata-only runs have their own paced attempts.
  Empty inventories count discovery attempts. Terminal worker failures may end a
  finite run early with unavailable results; they never require replacement workers.
- Use the command on the ZFS host, or invoke it through SSH:
  `ssh host zpool-stats`.
- Only selected query families run. Streaming output collects sequentially and
  uses one shared multi-pool `zpool iostat` window; without I/O it waits once per
  set. Sticky output schedules each pool's I/O, properties, snapshots, status,
  and discovery independently on **four fixed daemon workers**, with no more than
  four outstanding callbacks. No replacement worker is created for a blocked one.
  Startup discovery and initial status are also off the rendering thread. The UI
  redraws about every 100 ms independently of collection, including while commands
  time out. Slow queries can consume capacity but do not block healthy peers while
  a worker is available. If all four workers stall, collection becomes unavailable;
  rendering and signal shutdown still work. This is not a hard real-time guarantee.
- `--interval` is the I/O sampling window and the target minimum start-to-start
  cadence for other interactive operations, not a guarantee of fresh data. I/O
  sampling does not incur another full interval after the command completes.
  Interactive rows combine independently completed measurements; they are not
  atomic pool snapshots. Each pool retains at most one history row per interval,
  updating the latest row as other query families complete within that interval.
  Streaming remains a sequence of assembled sample sets.
- Timing fields use seconds. `sample_age` is elapsed monotonic time since the
  **oldest successful requested noncached query family's completion** in the row.
  Shared streaming I/O uses its actual completion time, not later per-pool parsing
  time. Missing families do not contribute an origin; when none succeeded it is
  unavailable (`-`/null). Cached snapshots are deliberately separate:
  `snapshot_age` is time since that row's snapshot query completed. Both ages tick
  on every sticky redraw, including historical rows, without changing their
  measurement timestamps. Streaming ages are evaluated at emission. `timestamp`
  and `unix_time` describe record assembly, not every metric's measurement time.
  `collection_duration` is the full sample-set plus status-refresh duration in
  streaming mode; in the asynchronous UI it is the most recently completed query
  family's elapsed collection time, unavailable after a failed attempt. It is
  **not** data age.
- Snapshot usage is the sum of `usedbysnapshots` for the pool and all descendant
  filesystems/volumes. The query uses `-t filesystem,volume` to avoid enumerating
  individual snapshots; their space is already counted by each owning dataset.
  The recursive query is cached for 60 seconds by default
  to limit overhead on large dataset trees. Use `--snapshot-refresh SECONDS` to
  change the cache lifetime, or `--snapshot-refresh 0` to query every sample.
  Cache lifetime starts when the query finishes, not when it starts. Snapshot
  age measures time since the last successful cached query completed. A failed
  refresh emits an unavailable value rather than silently presenting the old
  total as current. An empty, partial-unknown, or malformed response never
  becomes a healthy-looking zero or a partial total.
- Every `zpool` and `zfs` subprocess has a 300-second timeout by default. The
  requested `zpool iostat` sampling interval is added to that limit, so a long
  sampling interval does not consume the command's execution allowance. Change
  the allowance with `--command-timeout SECONDS`.
- Collection failures are isolated by pool and query family. A failed shared
  streaming `iostat` affects all selected pools' I/O, not their property queries.
  Streaming failures warn on stderr; interactive failures display unavailable
  values/status. Continuous retries back off from 10 to 300 seconds. Finite
  interactive attempts retry at the requested interval instead of waiting for
  continuous-mode backoff; streaming cycles may include skipped retries.
  Failed/unsupported values are `-`, empty CSV/TSV fields, or JSON `null`. During
  an in-flight refresh, prior values remain visible with increasing ages; after
  failure the affected current fields are cleared. Historical rows remain history.
- All CLI command paths use bounded polling rather than `subprocess.run`'s
  blocking timeout cleanup. Child process groups receive SIGKILL on timeout or
  shutdown. A timed-out but unreaped child continues to count against the global
  **four-child cap**, and cannot be replaced by another command with the same
  arguments. Ordinary children are killed, but Python cannot force Linux D-state
  I/O to finish. Shutdown stops waiting rather than promising to kill an
  uninterruptible process. Worker saturation and removed/reimported pools do not
  grow the worker/child limit. Stale results from removed pool generations are
  discarded. The CLI must be restarted to reset permanently stuck workers.
  These bounds are per running monitor; this is a CLI, not a reusable worker service.
- Column widths grow when a longer value appears and never shrink during a run.
  Sticky monitoring retains up to 10,000 structured rows per pool, but formats
  **only the visible history** (two passes to stabilize widths), not all retained
  rows. Hidden pools cannot expand visible widths. Resize and width growth preserve
  the retained history.
- Pool health, scan continuation lines (rates, progress, repaired bytes, and
  ETA), the data-error summary, and nonzero device READ/WRITE/CKSUM counters are
  refreshed independently in sticky mode and after each streaming cycle. Multiline
  `status:` and `action:` diagnostics are preserved. They are marked `ADVISORY`
  when no data/device errors were reported, rather than being labeled corruption.
  Actual data/device errors retain `ATTENTION`. `ONLINE` does not imply error-free.
  Interactive status lines update in place; streaming table output prints only
  changed status lines. `--no-status` skips status queries entirely.
- Interactive table output uses a terminal-sized sticky viewport by default.
  All pool status lines stay at the top. Each pool section has its own colored
  column heading, followed by an independent rolling history; leftover rows go
  to the first sections. Sections stay in place even while their histories are
  filling. Resizing redistributes the rows and preserves recent history. Lines
  containing status information wrap within the available status budget;
  table headings and data rows are clipped to terminal width. Each visible pool's
  compact health summary (including advisory/attention markers) is reserved before
  any wrapped details. Short terminals show a stable subset of pools; extremely
  short terminals may show only summaries. Collection still covers every pool.
  Use `--no-sticky-header` for ordinary streaming output. `--header-every N`
  also selects streaming mode and repeats the heading every N rows; use
  `--header-every 0` to print it once.
- Interactive status lines are bold green for error-free `ONLINE`, yellow for
  `ONLINE` with an advisory or error attention, or `DEGRADED`, and red for other states;
  headings are bold cyan. Error attention never downgrades a faulted pool to
  yellow. `--color auto` is the
  default, honors the `NO_COLOR` environment variable, and emits no ANSI color
  when redirected. Use `--color always` or `--color never` to override it.
  CSV, TSV, and JSON Lines never contain terminal styling.
- A closed downstream pipe exits silently with status 0. `SIGINT`/`Ctrl-C` and
  `SIGTERM` stop collection without a traceback and return the conventional
  shell statuses 130 and 143, restoring the cursor without joining blocked workers.
  Fatal setup errors exit 1 and invalid CLI arguments exit 2. A finite run exits 1
  if no requested measurement succeeded anywhere in the run, or no rows could be
  produced (including an empty inventory or failed discovery). Local metadata
  cannot hide failed requested measurements; a pool-only run with rows can succeed.
  Partial success exits 0: inspect `collection_status`, `unavailable_columns`, and
  nulls for each machine record. Exit 0 does not certify health or completeness.
- `compression_ratio` is OpenZFS's `compressratio` value (`1.03x`, for example)
  and is now the default, with two decimal places and the heading `ratio(x)`.
  The shorter `compression` column reports the amount above parity as a
  percentage: `(compressratio - 1) × 100`, so `1.03x` displays as `3%`. It is
  not the percentage of physical space saved; that would be
  `(1 - 1 / compressratio) × 100`.
- `used`, `free`, `total`, and `capacity` use the **root dataset's** `used` and
  `available` properties. `total = used + available`; `capacity = used / total`.
  These are not raw pool size/allocation figures and can differ from
  `zpool list` because dataset accounting, quotas, reservations, and pool
  overhead differ. The existing `logical_used` and `logical_free` columns come
  from the pool allocation/free fields of `zpool iostat`; despite their legacy
  names, they are not a logical, uncompressed dataset-size measurement.

### Migrating to 1.9.0

The default table and default machine-output schema now use `compression_ratio`
instead of the misleading abbreviated percentage and add freshness/timing
fields. Version 1.9.0 additionally appends the availability fields described above
even to pinned `--columns`; update strict CSV/JSON consumers. The legacy `compression`
column retains `(compressratio - 1)` as its raw ratio and the excess-over-parity
percentage in table output; it does **not** mean percentage space saved.
Use an explicit column selection to keep a narrow table, for example:

```bash
zpool-stats --columns pool,used,free,read,write,compression_ratio::2,snapshots,snapshot_age
```

Missing measurements now remain unavailable instead of becoming zero, and
recoverable query failures no longer terminate the whole monitor. Scripts that
previously treated exit status 0 as a complete/healthy sample must inspect fields.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install pytest ruff build
python3 -m pytest -q
python3 -m compileall -q zpool_stats.py tests
python3 verification/cli_smoke.py
python3 verification/discovery_smoke.py
python3 verification/independent_acceptance.py
python3 verification/async_peer_smoke.py
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m build
```

Run development commands inside a virtual environment. `pytest` is required to
discover the function-based remediation and independent contract tests;
`unittest discover` alone does not run the full suite. The smoke scripts launch
the real CLI against temporary, explicitly synthetic `zfs`/`zpool` executables.
These tests do not require ZFS and do not establish live-host correctness.
Live verification still requires an OpenZFS host.

## License

Released under the [MIT License](LICENSE). You may use, copy, modify, merge,
publish, distribute, sublicense, and sell copies of this software, provided the
copyright and license notices are preserved. The software is provided without
warranty; see `LICENSE` for the full terms.
