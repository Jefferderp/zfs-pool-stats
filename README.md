# zpool-stats

`zpool-stats` is a small, dependency-free terminal monitor for one local OpenZFS
pool. It combines pool I/O, dataset usage, snapshot usage, fragmentation,
compression, health, and scrub status into one readable stream.

```text
zpool tank is ONLINE: scan: scrub repaired 0B in 00:10:05 with 0 errors on Sun Sep 13 00:34:06 2026
used    free  total   cap    read  write  frag   comp  snap
407.1G  1.4T  1.8T  21.9%  0B    1.1M   9.0%  28.0%  16.5G
```

## Requirements

- Python 3.10 or newer
- OpenZFS commands `zpool` and `zfs`
- Permission to query the selected pool and its datasets

No third-party Python packages are required.

## Install

Run directly from a clone:

```bash
git clone https://github.com/Jefferderp/zfs-pool-stats.git
cd zfs-pool-stats
./zfs-pool-stats.py tank
```

Or install the command with `pipx`:

```bash
pipx install git+https://github.com/Jefferderp/zfs-pool-stats.git
zpool-stats tank
```

## Usage

```text
zpool-stats POOL [--interval SECONDS] [--count N] [--columns SPEC]
```

Examples:

```bash
# Monitor forever at one-second intervals
zpool-stats tank

# Print five samples
zpool-stats tank --count 5

# Select and customize columns
zpool-stats tank --columns used:T:2:USED,free:T:2:FREE,read:M:1,write:M:1

# Machine-friendly finite output without the status line
zpool-stats tank --count 1 --no-status --header-every 0

# Discover every supported column
zpool-stats --list-columns
```

A column specification has the form `NAME[:UNIT[:PRECISION[:HEADER]]]`.
Units apply to byte and time columns only. Byte units are `B`, `K`, `M`, `G`,
`T`, `P`, `E`, `Z`, and `Y`; time units are `ns`, `us`, `ms`, `s`, `m`, `h`,
and `d`. Values use powers of 1024, matching raw OpenZFS byte counters.

The default columns are:

```text
used,free,total,capacity,read,write,fragmentation,compression,snapshots
```

Legacy column names from the unfinished prototype, such as `VirtCapUsed`,
`BwRead`, and `StateFragPerc`, remain accepted. The old `--pool/-p` option and
`--interval/-t` spelling also remain compatible.

## Behavior and caveats

- Collection is local. Use the command on the ZFS host, or invoke it through
  SSH: `ssh host zpool-stats tank`.
- `zpool iostat` provides each sampling delay. The program does not add another
  sleep, so `--interval 1` remains approximately one second.
- Snapshot usage is the sum of `usedbysnapshots` for the pool and all descendant
  filesystems/volumes. On pools with very large dataset trees, this query can
  add overhead.
- Output is plain text and works when redirected. The header repeats every 20
  samples by default; use `--header-every 0` to disable repetition.
- `Ctrl-C` exits with status 130. Command and parsing failures print a concise
  message and exit with status 1 or 2.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q zpool_stats.py tests
```

Live tests require an OpenZFS host. Unit tests use captured command-shaped data
and do not require ZFS.

## License

MIT
