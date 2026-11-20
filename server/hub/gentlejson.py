"""Reading a big JSON document without stopping the rest of the hub.

`json.loads` on the tens of MB of the provider snapshot or the several MB of the title fold state is one C call:
it holds the interpreter lock from start to end, and on the NAS that was 3 to 4 s with every request waiting
(Nov 20 2026). `loads` here reads the same text one element at a time below the top level, so other threads (the
event loop) get the lock between elements, and `settle` moves what a big read built out of the collector's reach.
"""

from __future__ import annotations

import gc
import json
import re
import time
from typing import Any

_DEC = json.JSONDecoder()
_WS = re.compile(r"[ \t\n\r]*")
_YIELD_EVERY = 256  # elements between explicit yields; each element is a short C call


def loads(text: str) -> Any:
    """The same value as `json.loads(text)`, read a top-level member and a nested element at a time. Text it can't
    read this way is handed to `json.loads`, which raises the usual `JSONDecodeError`."""
    try:
        return _loads(text)
    except (IndexError, ValueError):
        return json.loads(text)


def _loads(text: str) -> Any:
    i = _skip(text, 0)
    if i >= len(text) or text[i] != "{":
        return json.loads(text)
    out: dict[str, Any] = {}
    i = _skip(text, i + 1)
    if text[i] == "}":
        _end(text, i + 1)
        return out
    while True:
        key, i = _DEC.raw_decode(text, i)
        i = _expect(text, _skip(text, i), ":")
        ch = text[i]
        value: Any
        if ch == "[":
            value, i = _array(text, i)
        elif ch == "{":
            value, i = _object(text, i)
        else:
            value, i = _DEC.raw_decode(text, i)
        out[key] = value
        i = _skip(text, i)
        if text[i] == ",":
            i = _skip(text, i + 1)
            continue
        _close(text, i, "}")
        _end(text, i + 1)
        return out


BIG = 1_000_000  # characters: a document this size or more is worth freezing after it is read


def settle(size: int = BIG) -> None:
    """After a big read: move every object alive now into the collector's permanent generation. A full collection
    walks every tracked object; with the snapshot and the fold index alive that walk took seconds and held the lock.
    The documents are trees, so reference counting still frees an old copy once it is replaced."""
    if size >= BIG:
        gc.freeze()


def _array(text: str, i: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    i = _skip(text, i + 1)
    if text[i] == "]":
        return out, i + 1
    n = 0
    while True:
        value, i = _DEC.raw_decode(text, i)
        out.append(value)
        n += 1
        if not n % _YIELD_EVERY:
            time.sleep(0)
        i = _skip(text, i)
        if text[i] == ",":
            i = _skip(text, i + 1)
            continue
        _close(text, i, "]")
        return out, i + 1


def _object(text: str, i: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    i = _skip(text, i + 1)
    if text[i] == "}":
        return out, i + 1
    n = 0
    while True:
        key, i = _DEC.raw_decode(text, i)
        i = _expect(text, _skip(text, i), ":")
        value, i = _DEC.raw_decode(text, i)
        out[key] = value
        n += 1
        if not n % _YIELD_EVERY:
            time.sleep(0)
        i = _skip(text, i)
        if text[i] == ",":
            i = _skip(text, i + 1)
            continue
        _close(text, i, "}")
        return out, i + 1


def _skip(text: str, i: int) -> int:
    match = _WS.match(text, i)
    return match.end() if match else i


def _expect(text: str, i: int, ch: str) -> int:
    if i >= len(text) or text[i] != ch:
        raise json.JSONDecodeError(f"Expecting '{ch}'", text, i)
    return _skip(text, i + 1)


def _close(text: str, i: int, ch: str) -> None:
    if text[i] != ch:
        raise json.JSONDecodeError(f"Expecting '{ch}'", text, i)


def _end(text: str, i: int) -> None:
    if _skip(text, i) != len(text):
        raise json.JSONDecodeError("Extra data", text, i)
