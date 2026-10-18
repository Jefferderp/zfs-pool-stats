# zpool-stats 1.9.0 — final remediation verification

## Result and scope

All seven requested remediation areas are implemented in `/opt/hermes/workspaces/projects/zfs-pool-stats`. Existing uncommitted work was preserved. Nothing was committed, pushed, deployed, or run against a real ZFS pool.

Final standalone source SHA-256: `df2e7214e44d1099eee54341d6763b3584e38a0d298d7c84ea051c8439275f8d`.

## Changes

1. **Freshness:** successful query-family completion timestamps anchor measurement age. Shared streaming I/O retains its actual completion time. `sample_age` represents the oldest successful requested noncached family, or is unavailable when none succeeded. Snapshot freshness has its own origin. Interactive historical ages advance on redraw without changing measurement timestamps. Failed asynchronous attempts clear the previous successful collection duration rather than reusing it.
2. **Failure reporting:** finite runs with no usable requested measurements, or no emitted rows, return 1. Metadata cannot hide requested metric failures; snapshot-age-only queries count as measurements. JSONL/CSV/TSV always append `collection_status` and `unavailable_columns`. Partial success returns 0 and remains explicit in each record.
3. **Diagnostics:** multiline `status:` and `action:` survive parsing, alongside scrub, data-error, and device-error details. Advisory notices and error attention remain distinct. Asynchronous discovery failures warn and can recover while retaining the last inventory.
4. **Bounded asynchronous collection:** the sticky UI redraws independently of startup discovery, initial status, and subsequent collection. Four fixed daemon workers and a four-child cap prevent replacement storms. Timed-out unreaped children retain their slots. Polling/process-group cancellation replaces blocking subprocess timeout cleanup. Tests cover worker expiry/recovery, finite saturation, pool-generation churn, controller initialization/replacement races, and signal shutdown.
5. **Visible-history rendering:** one layout selects exactly the visible records. Two formatting passes stabilize widths without scanning hidden histories. Structured history remains bounded at 10,000 rows per pool and survives resizing.
6. **Health summaries:** every visible pool gets its compact health summary before optional wrapped details. Advisory/attention markers and colors survive truncation. Tiny terminal frames remain height-bounded.
7. **Latency units:** zero and fractional nanosecond values use a consistent `ns` fallback.

README documents the schema change, independent versus streaming collection semantics, count behavior, timing fields, and limits. CI runs all four integration scripts.

## Parent-executed final verification

These results were obtained after the final source changes, independently of the implementation worker's earlier report:

- Full suite, first run: **135 passed, 41 subtests passed in 45.86 s**.
- Full suite, second run: **135 passed, 41 subtests passed in 45.83 s**.
- `python -m ruff check zpool_stats.py tests verification`: **All checks passed**.
- `python -m ruff format --check zpool_stats.py tests verification`: **11 files already formatted**.
- `git diff --check`: passed.
- `python -m compileall -q zpool_stats.py tests verification`: passed.
- `python -m build`: passed; output is in `verification/build-1.9.0.log`.
- Wheel metadata reports **1.9.0**. Both the wheel's and source distribution's `zpool_stats.py` bytes exactly match the final standalone source.
- The wheel was also imported directly and its CLI returned `zpool-stats 1.9.0`.
- HEAD remains `ae77ac0` (`Display percentage columns without decimals by default`).

### Real-process synthetic integration tests

All four scripts passed in the final parent-owned run:

- `verification/cli_smoke.py`: four JSON records; unknown measurements remain null; snapshot failures are isolated; scrub progress and error details remain visible.
- `verification/discovery_smoke.py`: five records; empty inventory recovers through discovery churn.
- `verification/independent_acceptance.py`: success, total failure, and partial-failure contracts pass for JSONL/CSV/TSV. A blocked startup still produced **25 PTY frames** and SIGTERM completed in **0.032 s**. A stalled snapshot allowed **20 property queries** while only **one snapshot query** remained outstanding.
- `verification/async_peer_smoke.py`: both pools completed **24 property queries** while one snapshot refresh stalled; **25 frames** rendered; snapshot age advanced to **2.3 s**; SIGTERM completed in **0.012 s**.

An additional parent-owned 12-second real-process probe verified normal timeout/backoff recovery: a first snapshot request exceeded a 0.2-second timeout, subsequent requests recovered after backoff, and 119 property queries/119 frames continued during the observation.

A viewport probe retained 30,000 records across three pools but needed only seven formatter calls for the three visible data rows and header.

## Review and cleanup

An independent source reviewer returned `passed: true`, with no security or logic findings. A broad lint run had reformatted the temporary review snapshot, changing its initially recorded digest. The reviewer consistently inspected digest `63551069649909334fccbdb2435d6cfc2f0c0c07c05c1ebb554675a46cecedad`. The parent reconciled that snapshot against final source: the only semantic deltas were the subsequently tested discovery warning and clearing stale collection duration on failure. The temporary review directory was removed.

The implementation worker was interrupted after its final gate command, leaving its earlier report stale and one synthetic fixture directory behind. The parent checked that no fixture processes remained, removed that owned directory, reran the final gates and build, and replaced the stale report with this verified result. Historical logs describe intermediate states, not the final result.

## Artifacts

- Standalone: `zpool_stats.py`
- Wheel: `dist/zpool_stats-1.9.0-py3-none-any.whl`
- Source distribution: `dist/zpool_stats-1.9.0.tar.gz`
- Regression tests: `tests/test_release_190.py`, plus retained earlier tests/contracts.

## Limits and intentional behavior

- No live OpenZFS host or real kernel D-state fault injection was available. Tests use explicitly synthetic executables, real PTYs/signals, controlled clocks, and simulated unreapable children/never-returning callbacks.
- Python cannot force uninterruptible kernel I/O to finish. Shutdown stops waiting and sends SIGKILL; it does not guarantee that a D-state process disappears. Four permanently stuck workers/children exhaust collection capacity without creating replacements. Restart the CLI to reset stuck workers.
- Bounds apply to one running CLI monitor, not repeated embedded library invocations. A block inside process creation can consume collection capacity; rendering and bounded shutdown remain separate.
- Sticky rows combine independently completed measurements, not atomic pool snapshots. Streaming collection remains sequential.
- Sticky `collection_duration` describes the latest successful metric-family collection and is cleared on failure; streaming duration covers the sample set and status refresh. Neither duration is data age.
- Sticky finite counts are attempts per selected family, including status. Finite interactive retries use the interval; continuous failures use exponential backoff. Fatal saturation can finish finite runs early with unavailable data.
- Machine availability fields are appended even with explicit `--columns`. Strict consumers must accept the schema change. Partial success remains exit 0; valid empty inventories emit no rows and finite exit 1.
- Terminal size can hide pools/details or clip text; visible summaries have priority.
