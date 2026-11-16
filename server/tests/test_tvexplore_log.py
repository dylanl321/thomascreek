"""TMDB errors reach the log without the key."""

from __future__ import annotations

import httpx

from hub.tvexplore import _safe


def test_a_tmdb_status_error_is_logged_without_its_address():
    request = httpx.Request("GET", "https://api.themoviedb.org/3/trending/all/week?api_key=SECRET123")
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("Client error '401' for url " + str(request.url), request=request, response=response)
    assert _safe(error) == "HTTP 401"
    assert "SECRET123" not in _safe(ValueError("bad url ...?api_key=SECRET123"))
    assert _safe(ValueError("timeout")) == "timeout"
