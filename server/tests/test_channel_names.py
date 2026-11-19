"""Live channel names as people read them (`tvnames.channel_name`), for search and the lineup's `display_name`."""

from __future__ import annotations

import pytest

from hub.tvnames import channel_name


@pytest.mark.parametrize("raw, shown", [
    ("US| THE OFFICE FHD", "The Office"),
    ("US| THE OFFICE AU ᴿᴬᵂ", "The Office AU"),
    ("UK: BBC One HD", "BBC One"),
    ("|EN| ESPN 2 4K", "ESPN 2"),
    ("DE: ZDF ◉", "ZDF"),
    ("FOX NEWS", "Fox News"),
    ("US: DISNEY CHANNEL HD", "Disney Channel"),
    ("|UK| SKY SPORTS F1 UHD", "Sky Sports F1"),
    ("US - HBO 2 EAST", "HBO 2 East"),
    ("US| ABC 7 NEW YORK (WABC) HD", "ABC 7 New York (WABC)"),
    ("HISTORY 50FPS", "History"),
    ("UK: ITV 1 +1", "ITV 1 +1"),
    ("Nick Jr.", "Nick Jr."),
    ("E!", "E!"),
    ("5USA", "5USA"),
    ("CNN", "CNN"),
    ("ESPN+", "ESPN+"),
    ("Fox News", "Fox News"),
    ("FBI Channel", "FBI Channel"),  # not a tag the cleaner knows, so left alone
])
def test_channel_names_lose_provider_tags_and_shouting(raw, shown):
    assert channel_name(raw) == shown


def test_what_it_cannot_clean_comes_back_as_given():
    assert channel_name("####### USA #######") == "####### USA #######"
    assert channel_name("") == ""
    assert channel_name(None) == ""
    assert channel_name("HD") == "HD"
