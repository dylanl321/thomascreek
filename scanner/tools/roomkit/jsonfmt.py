"""Write JSON the way ios/Rooms/catalog.json is laid out: short number lists and view rows inline,
so a script edit only changes the lines it means to."""
from __future__ import annotations

import json


def _scalar(x) -> bool:
    return not isinstance(x, (dict, list))


def dumps(v, ind: int = 0, inline_depth: int = 4) -> str:
    sp = "  "
    if _scalar(v):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        if all(_scalar(i) for i in v):
            return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in v) + "]"
        return "[\n" + ",\n".join(sp * (ind + 1) + dumps(i, ind + 1, inline_depth) for i in v) + "\n" + sp * ind + "]"
    flat = all(_scalar(x) or (isinstance(x, list) and all(_scalar(j) for j in x)) for x in v.values())
    if flat and ind >= inline_depth:
        return "{ " + ", ".join(f"{json.dumps(k)}: {dumps(x, ind + 1, inline_depth)}" for k, x in v.items()) + " }"
    if not v:
        return "{}"
    return "{\n" + ",\n".join(f"{sp * (ind + 1)}{json.dumps(k)}: {dumps(x, ind + 1, inline_depth)}" for k, x in v.items()) + "\n" + sp * ind + "}"
