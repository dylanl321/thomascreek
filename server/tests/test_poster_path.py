"""Title posters come from the hub's cache when the hub can fetch them and the caller may use the TV art route."""

from __future__ import annotations

from hub.tvexplore import poster_path

TMDB = "https://image.tmdb.org/t/p/w342/p.jpg"


def _fake(hub, allowed: bool):
    class Policy:
        @staticmethod
        def has(_subject, cap):
            return allowed and cap == "media.all"

    class H:
        policy = Policy()
        config = hub.config
        vault = hub.vault

    return H


def test_a_poster_is_the_hubs_cached_copy_once_the_hub_has_a_tmdb_key(hub):
    h = _fake(hub, allowed=True)
    secret = hub.config.artwork.tmdb_key_secret
    if not hub.vault.has(secret):
        assert poster_path(h, object(), "movie", 603, "/p.jpg") == TMDB
    hub.vault.set(secret, "k")
    assert poster_path(h, object(), "movie", 603, "/p.jpg") == "/v1/tv/explore/art/poster/movie/603"
    assert poster_path(h, object(), "tv", "1399", "/p.jpg") == "/v1/tv/explore/art/poster/tv/1399"
    # No usable TMDB id: TMDB's own address.
    assert poster_path(h, object(), "tv", "abc", "/p.jpg") == TMDB
    assert poster_path(h, object(), "movie", None, None) is None


def test_a_caller_without_tv_media_keeps_tmdbs_address(hub):
    hub.vault.set(hub.config.artwork.tmdb_key_secret, "k")
    assert poster_path(_fake(hub, allowed=False), object(), "movie", 603, "/p.jpg") == TMDB
