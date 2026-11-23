"""The cloud worker's ffmpeg settings and playlists (cloud/worker/ladder.py, no outside packages)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "cloud" / "worker" / "ladder.py"
_spec = importlib.util.spec_from_file_location("cloud_ladder", _PATH)
assert _spec and _spec.loader
ladder = importlib.util.module_from_spec(_spec)
sys.modules["cloud_ladder"] = ladder  # dataclasses look their module up while the file loads
_spec.loader.exec_module(ladder)

SRC = "http://iptv.example:8080/movie/household/secret-password/101.mp4"


def test_sizes_come_biggest_first_and_unknown_ones_are_dropped():
    assert [r.height for r in ladder.parse("360, 720p, 999, 720")] == [720, 360]
    assert [r.height for r in ladder.parse("")] == [720, 360]


def test_one_run_writes_every_size_with_matching_cuts():
    rungs = ladder.parse("720,360")
    args = ladder.ffmpeg_args("ffmpeg", SRC, "/w/k", rungs, live=False, start_s=124, start_number=31, threads=2,
                              readrate=4)
    joined = " ".join(args)
    assert args[args.index("-ss") + 1] == "124" and args.index("-ss") < args.index("-i")
    assert args[args.index("-start_number") + 1] == "31" and args[args.index("-readrate") + 1] == "4"
    assert "split=2[v0][v1]" in joined and "min(720,ih)" in joined and "min(360,ih)" in joined
    assert args[args.index("-var_stream_map") + 1] == "v:0,a:0 v:1,a:1"
    assert "-b:v:1" in args and args[args.index("-b:v:1") + 1] == "700k"
    assert "delete_segments" not in joined and "temp_file" in joined
    assert args[-1] == "/w/k/%v/ff.m3u8"  # ffmpeg's own film list is unused; the worker hands out the whole one
    assert "expr:gte(t,n_forced*4)" in args


def test_a_film_list_names_every_segment_up_front():
    text = ladder.film_playlist(9.5)
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text.rstrip().endswith("#EXT-X-ENDLIST")
    assert [line for line in text.splitlines() if not line.startswith("#")] == \
        ["seg00000.ts", "seg00001.ts", "seg00002.ts"]
    assert "#EXTINF:1.500," in text
    assert ladder.segment_number("seg00031.ts") == 31 and ladder.segment_number("ff.m3u8") is None


def test_live_keeps_a_short_window_and_reconnects():
    args = ladder.ffmpeg_args("ffmpeg", SRC, "/w/k", ladder.parse("480"), live=True, start_s=60)
    assert "-ss" not in args and "-reconnect_streamed" in args
    assert args[args.index("-hls_list_size") + 1] == "8" and "delete_segments+independent_segments+temp_file" in args
    assert args[-1] == "/w/k/%v/index.m3u8" and "-readrate" not in args


def test_a_source_without_sound_maps_video_only():
    args = ladder.ffmpeg_args("ffmpeg", SRC, "/w/k", ladder.parse("720,360"), live=True, audio=False)
    assert "0:a:0" not in args and "-c:a" not in args
    assert args[args.index("-var_stream_map") + 1] == "v:0 v:1"


def test_the_master_list_starts_small_and_never_carries_the_pass():
    token = "eyJhbGciOiJIUzI1NiJ9.pass.sig"
    key = ladder.session_key(token, 0)
    text = ladder.master_playlist(key, ladder.parse("720,360"))
    assert token not in text and key in text
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == [f"/s/{key}/1/index.m3u8", f"/s/{key}/0/index.m3u8"]
    assert ladder.session_key(token, 0) != ladder.session_key(token, 60)


def test_ffmpeg_messages_lose_the_provider_address():
    line = f"[http @ 0x1] HTTP error 403 Forbidden for {SRC}"
    clean = ladder.redact(line)
    assert "secret-password" not in clean and "iptv.example" not in clean and "<address>" in clean


def test_only_playlist_and_segment_names_are_served():
    ok = ladder.SAFE_NAME.match
    assert ok("index.m3u8") and ok("seg00012.ts")
    assert not ok("../worker.json") and not ok("seg1.ts") and not ok("index.m3u8.bak")
