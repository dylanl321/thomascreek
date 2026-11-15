"""The hub's XMLTV guide: big files, shared guide ids, and what is on now."""

from __future__ import annotations

import gzip
import time
from datetime import datetime, timezone

from hub.tvcatalog import MAX_PROGRAMMES, _parse_xmltv


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _xml(channel: str, starts: list[float], length: float = 1800) -> bytes:
    rows = "".join(
        f'<programme channel="{channel}" start="{_stamp(s)}" stop="{_stamp(s + length)}"><title>Show {i}</title></programme>'
        for i, s in enumerate(starts))
    return f'<?xml version="1.0"?><tv>{rows}</tv>'.encode()


NOW = 1_800_000_000.0


def test_a_week_of_old_listings_does_not_hide_what_is_on_now():
    starts = [NOW - 6 * 86400 + i * 1800 for i in range(6 * 48 + 10)]
    guide = _parse_xmltv(_xml("espn.us", starts), {"espn.us": ["xc:tc:10"]}, NOW)
    rows = guide["xc:tc:10"]
    assert len(rows) <= MAX_PROGRAMMES
    assert any(row["start_ts"] <= NOW < row["end_ts"] for row in rows)
    assert any(row["start_ts"] > NOW for row in rows)


def test_copies_that_share_a_guide_id_all_get_the_listings():
    guide = _parse_xmltv(_xml("ESPN.us", [NOW - 600]), {"espn.us": ["xc:tc:10", "xc:tc:11"]}, NOW)
    assert set(guide) == {"xc:tc:10", "xc:tc:11"}
    assert guide["xc:tc:10"][0]["title"] == "Show 0"


def test_a_gzipped_guide_file_is_read(tmp_path):
    path = tmp_path / "guide.xml"
    path.write_bytes(gzip.compress(_xml("cnn.us", [NOW - 600, NOW + 1200])))
    guide = _parse_xmltv(path, {"cnn.us": ["xc:tc:20"]}, NOW)
    assert [row["title"] for row in guide["xc:tc:20"]] == ["Show 0", "Show 1"]


def test_a_broken_tail_keeps_what_was_read():
    body = _xml("cnn.us", [NOW - 600])[:-5] + b"<programme"
    assert "xc:tc:20" in _parse_xmltv(body, {"cnn.us": ["xc:tc:20"]}, NOW)


