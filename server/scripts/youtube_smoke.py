"""Real-world check of hub/youtube.py against YouTube, without the rest of the hub.

    python scripts/youtube_smoke.py <link> [<link> ...]

Uses a throwaway database and folder (printed at the end). Looks each link up,
adds it, fetches up to two of its videos, and prints what the app would see.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from hub.config import Config
from hub.db import DB
from hub.downloads import options
from hub.youtube import YouTube, parse_link


class Bus:
    def publish(self, e):  # noqa: D401
        print("  event:", e)


async def main(links: list[str]) -> None:
    root = Path(tempfile.mkdtemp(prefix="yt-smoke-"))
    cfg = Config()
    cfg.youtube.channel_latest = 3
    cfg.youtube.playlist_max = 3
    db = DB(root / "hub.db")
    person = db.add_person("Theo", "child")
    hub = SimpleNamespace(config=cfg, db=db, bus=Bus())
    yt = YouTube(hub, root / "youtube")
    await yt.update_ytdlp() if "--update" in sys.argv else None
    for link in links:
        print(f"\n== {link}")
        parsed = parse_link(link)
        print("  parsed:", parsed)
        info = await yt.lookup(parsed)
        print("  lookup:", json.dumps({k: v for k, v in info.items() if k != "videos"}, default=str))
        for v in info["videos"]:
            print("   -", v["id"], v["title"], v["duration_s"])
        src = await yt.add(link, True, person["id"])
        print("  source:", src["title"], src["kind"], "waiting", src["waiting"])
        for _ in range(2):
            if not await yt.fetch_next():
                break
        for v in db.all("SELECT * FROM yt_videos"):
            out = yt.video_out(v)
            print("  video:", out["video_id"], out["status"], out.get("height"), out.get("bytes"), out.get("error"))
            if out["status"] == "ready":
                p = yt.file(v)
                media = {"local_path": str(p), "size": v["bytes"], "duration_ms": int((v["duration_s"] or 0) * 1000)}
                print("    options:", [(o["quality"], o["bytes"]) for o in options(media)])
                print("    picture:", yt.picture(v["id"]))
    print("\nfolder:", root, "\nsummary:", json.dumps(yt.summary(), default=str))


if __name__ == "__main__":
    asyncio.run(main([a for a in sys.argv[1:] if not a.startswith("--")]))
