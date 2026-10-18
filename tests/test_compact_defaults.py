import io
import os

import zpool_stats as z


def test_default_status_is_compact_one_liner():
    for raw, expect in [
        (
            "zpool a is ONLINE: errors: No known data errors; scan: scrub in progress since Fri; 0B repaired, 58.39% done, 1 days 10:35:15 to go",
            "zpool a is ONLINE: scrub in progress, 58.39% done, 1 days 10:35:15 to go",
        ),
        (
            "zpool b is ONLINE: ADVISORY; status: Some supported features are not enabled",
            "zpool b is ONLINE ADVISORY (Some supported features are not enabled)",
        ),
        ("zpool c is DEGRADED: errors: 2 data errors", None),
    ]:
        line = z.compact_status(raw)
        if expect:
            assert line == expect
        else:
            assert "errors: 2 data errors" in line


def test_compact_status_leaves_room_for_history():
    renderer = z.StickyTableRenderer(
        io.StringIO(),
        [
            "zpool a is ONLINE: errors: No known data errors; scan: scrub in progress since Friday; 0B repaired, 58.39% done, 1 days 10:35:15 to go",
            "zpool b is ONLINE: ADVISORY; status: Some supported features are not enabled; action: Enable features",
        ],
        color=False,
        pools=["a", "b"],
        terminal_size=lambda: os.terminal_size((100, 14)),
    )
    lines, sections, _ = renderer.layout()
    assert len(lines) == 2
    assert "58.39%" in lines[0]
    assert "ADVISORY" in lines[1]
    assert "No known data errors" not in " ".join(lines)
    assert all(count >= 4 for _, count, _ in sections)


def test_default_columns_are_compact():
    columns = z.parse_columns(None)
    assert [c.header for c in columns] == [
        "pool", "free", "used", "total", "cap", "read", "write", "frag", "ratio", "snap"
    ]
