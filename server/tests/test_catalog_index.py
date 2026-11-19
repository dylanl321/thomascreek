"""Films, series and guide search read a SQLite copy of the snapshot (hub rebuild phase 3). It must give the same
pages as the old in-memory path, keep kids to kids' categories, and answer 304 for an unchanged page."""

from __future__ import annotations

import random

from hub import catalogdb, tvcatalog


def _doc(n: int = 300) -> dict:
    rng = random.Random(7)
    cats = ["Kids", "Drama", "Comedy", None]
    words = ["Star", "Office", "Night", "Ocean", "Blue", "Zoo", "Apple"]
    movies, series = [], []
    for i in range(n):
        name = f"{rng.choice(['EN - ', '', 'US| '])}{rng.choice(words)} {rng.choice(words)} {i % 37}"
        if i % 5 == 0:
            name += f" ({1990 + i % 30})"
        row = {"id": f"vod:thomas-creek:{i}", "name": name, "category": rng.choice(cats),
               "year": str(1990 + rng.randrange(30)) if i % 3 else None, "added": rng.choice([None, 1.7e9 + i])}
        movies.append(row)
        series.append({**row, "id": f"series:thomas-creek:{i}"})
    channels = [{"key": f"xc:{i}", "name": f"Channel {i}", "number": i + 1, "category": "Kids" if i % 2 else "News"}
                for i in range(6)]
    guide = {f"xc:{i}": [{"title": f"{words[(i + j) % len(words)]} Hour", "subtitle": "", "start_ts": 1000 + j,
                          "end_ts": 2000 + j} for j in range(10)] for i in range(6)}
    return {"fetched_at": 1.0, "guide_format": tvcatalog.GUIDE_FORMAT, "channels": channels, "movies": movies,
            "series": series, "guide": guide, "categories": {}}


def _both(cat, monkeypatch, **kw):
    fast = cat._list(**kw)
    monkeypatch.setattr(cat, "index_ready", lambda: False)
    slow = cat._list(**kw)
    monkeypatch.undo()
    return fast, slow


def test_index_pages_match_the_snapshot_path(hub, monkeypatch):
    cat = hub.tv_catalog
    cat.kids_categories = lambda: ["Kids"]
    cat._write(_doc())
    assert cat.index_ready()
    for kind in ("movies", "series"):
        for sort in ("name", "year", "added"):
            for q in (None, "star", "office 1"):
                for kids in (False, True):
                    for category in (None, "drama"):
                        for offset in (0, 40):
                            kw = dict(kind=kind, q=q, category=category, kids_only=kids, limit=25, offset=offset,
                                      sort=sort)
                            fast, slow = _both(cat, monkeypatch, **kw)
                            assert fast == slow, kw


def test_kids_never_get_other_categories(hub):
    cat = hub.tv_catalog
    cat.kids_categories = lambda: ["Kids"]
    cat._write(_doc())
    page = cat.movies(q=None, category=None, kids_only=True, limit=100, sort="name")
    assert page["movies"] and all(row["kids"] for row in page["movies"])
    cat.kids_categories = lambda: ["Not A Category Here"]
    cat._kid_memo = None
    doc = _doc()
    for row in doc["movies"]:
        row["category"] = "Drama" if row["category"] == "Kids" else row["category"]
    cat._write(doc)
    # No saved kids' category is in the lists and none is named for children: nothing, never adult titles.
    assert cat.movies(q=None, category=None, kids_only=True, limit=100)["movies"] == []


def test_kids_fall_back_to_child_named_categories_when_the_saved_ones_are_gone(hub):
    """The saved kids' categories no longer match the provider's names (Nov 19 2026: every child's page and
    search came back empty). Children then get the categories named for them, and nothing else."""
    cat = hub.tv_catalog
    doc = _doc()
    names = ["|EN| CHILDREN/FAMILY", "DISNEY+ KIDS", "|EN| CLASSIC CARTOONS", "|EN| ACTION", "Kidnapped Thrillers"]
    for i, row in enumerate(doc["movies"]):
        row["category"] = names[i % len(names)]
    cat._write(doc)
    for saved in ([], ["Kids (old name)"]):
        cat.kids_categories = lambda saved=saved: saved
        cat._kid_memo = None
        page = cat.movies(q=None, category=None, kids_only=True, limit=500, sort="name")
        assert page["movies"]
        assert {row["category"] for row in page["movies"]} == set(names[:3])
    cat.kids_categories = lambda: ["|EN| CLASSIC CARTOONS"]  # a saved name that matches wins
    cat._kid_memo = None
    page = cat.movies(q=None, category=None, kids_only=True, limit=500, sort="name")
    assert {row["category"] for row in page["movies"]} == {"|EN| CLASSIC CARTOONS"}


def test_search_programmes_come_from_the_index_in_guide_order(hub, monkeypatch):
    cat = hub.tv_catalog
    cat.kids_categories = lambda: ["Kids"]
    cat._write(_doc())
    fast = cat.search("ocean", kids_only=False, now=1500)
    monkeypatch.setattr(cat, "index_ready", lambda: False)
    slow = cat.search("ocean", kids_only=False, now=1500)
    assert fast["programmes"] and fast == slow


def test_a_stale_index_is_rebuilt_once_and_the_snapshot_serves_meanwhile(hub, monkeypatch):
    cat = hub.tv_catalog
    cat._write(_doc(20))
    cat.index.path.unlink()
    started = []
    monkeypatch.setattr(cat, "_start_rebuild", lambda stamp: started.append((stamp,)))
    assert not cat.index_ready() and not cat.index_ready()
    assert len(started) == 1
    assert cat.movies(q=None, category=None, kids_only=False, limit=5)["total"] == 20  # served from the snapshot
    cat._rebuild_index(*started[0])
    assert cat.index_ready()


def test_reader_without_a_file_says_so(tmp_path):
    reader = catalogdb.Reader(tmp_path / "none.db")
    assert reader.stamp() is None


def test_unchanged_pages_answer_304(hub, people):
    from tests.test_tv_api import _library  # the same library as the TV API tests

    _library(hub)
    dylan = people["phones"]["Dylan"]
    first = dylan.client.get("/v1/tv/movies", headers=dylan.h, params={"limit": 5})
    tag = first.headers["etag"]
    again = dylan.client.get("/v1/tv/movies", headers={**dylan.h, "If-None-Match": tag}, params={"limit": 5})
    assert again.status_code == 304 and again.content == b""
    other = dylan.client.get("/v1/tv/movies", headers={**dylan.h, "If-None-Match": tag}, params={"limit": 6})
    assert other.status_code == 200 and other.headers["etag"] != tag


def test_channels_carry_a_readable_name_and_keep_the_providers(hub):
    cat = hub.tv_catalog
    doc = _doc(5)
    doc["channels"][0]["name"] = "US| THE OFFICE FHD"
    cat._write(doc)
    row = next(r for r in cat.lineup(kids_only=False)["channels"] if r["key"] == "xc:0")
    assert row["name"] == "US| THE OFFICE FHD"
    assert row["display_name"] and row["display_name"] != row["name"]
    found = cat.search("office", kids_only=False)
    card = next(c for c in found["channels"] if c["key"] == "xc:0")
    assert card["display_name"] == row["display_name"]


def test_grown_up_cartoon_channels_in_a_kids_category_are_not_for_kids(hub):
    """Providers file "Adult Swim" with the kids' channels. The lineup, the guide and search keep it from a child;
    a grown-up's own kids switch on a channel still wins."""
    cat = hub.tv_catalog
    cat.kids_categories = lambda: ["Kids"]
    doc = _doc()
    doc["channels"] = [{"key": "xc:1", "name": "US| CARTOON NETWORK", "number": 1, "category": "Kids"},
                       {"key": "xc:2", "name": "US| ADULT SWIM FHD", "number": 2, "category": "Kids"}]
    doc["guide"] = {}
    cat._write(doc)
    kids = {row["key"] for row in cat.lineup(kids_only=True)["channels"]}
    assert kids == {"xc:1"}
    everyone = {row["key"]: row["kids"] for row in cat.lineup(kids_only=False)["channels"]}
    assert everyone == {"xc:1": True, "xc:2": False}
    assert not cat.channel("xc:2")["kids"]


def test_skipped_titles_leave_the_page_and_the_total(hub, monkeypatch):
    """A child's level leaves titles out before paging, so a page stays full and `total` counts what is left."""
    cat = hub.tv_catalog
    cat.kids_categories = lambda: ["Kids"]
    cat._write(_doc())
    full = cat.movies(q=None, category=None, kids_only=True, limit=500, sort="name")
    ids = [row["id"] for row in full["movies"]]
    skip = set(ids[::3])
    for kw in (dict(kind="movies", q=None, category=None, kids_only=True, limit=10, offset=0, sort="name"),
               dict(kind="movies", q="star", category=None, kids_only=True, limit=10, offset=5, sort="year")):
        fast, slow = _both(cat, monkeypatch, skip=skip, **kw)
        assert fast == slow, kw
        assert not {row["id"] for row in fast["movies"]} & skip
    page = cat.movies(q=None, category=None, kids_only=True, limit=10, sort="name", skip=skip)
    assert page["total"] == len(ids) - len(skip)
    assert len(page["movies"]) == 10


def test_search_can_leave_whole_categories_out_before_the_top_twenty(hub, monkeypatch):
    """A grown-up's "friends" search came back with no films: the top 20 were all from the provider's grown-up
    section, which the route then hid. Leaving the section out before the cut keeps the real matches."""
    import re

    cat = hub.tv_catalog
    cat.kids_categories = lambda: ["Kids"]
    doc = _doc(0)
    doc["movies"] = ([{"id": f"vod:thomas-creek:{i}", "name": f"Friends {i:02d}", "category": "|EN| FOR ADULTS"}
                      for i in range(30)]
                     + [{"id": "vod:thomas-creek:900", "name": "Friends With Benefits", "category": "Comedy"},
                        {"id": "vod:thomas-creek:901", "name": "Friends Forever", "category": None}])
    cat._write(doc)
    hide = cat.categories_matching(re.compile(r"\bfor adults\b"))
    assert hide == {"|en| for adults"}
    for ready in (True, False):
        monkeypatch.setattr(cat, "index_ready", lambda ready=ready: ready)
        films = cat.search("friends", kids_only=False, skip_categories=hide)["movies"]
        assert sorted(row["id"] for row in films) == ["vod:thomas-creek:900", "vod:thomas-creek:901"], ready
        assert len(cat.search("friends", kids_only=False)["movies"]) == 20


def test_a_kids_page_from_the_index_never_parses_the_snapshot(hub, monkeypatch):
    """A child's first films page after a start paid 1.4 s to parse the whole snapshot for its category names
    (Nov 19 2026). With the index ready, the kids' categories come from the index."""
    cat = hub.tv_catalog
    cat.kids_categories = lambda: ["Kids"]
    cat._write(_doc())
    assert cat.index_ready()
    cat.__dict__.pop("_loaded", None)
    cat._kid_memo = None
    monkeypatch.setattr(cat, "load", lambda: (_ for _ in ()).throw(AssertionError("parsed the snapshot")))
    page = cat.movies(q=None, category=None, kids_only=True, limit=40, sort="name")
    assert page["movies"] and all(row["kids"] for row in page["movies"])
