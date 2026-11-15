"""Fold provider rows into one catalog.

Matching runs when a refresh finds new or changed rows. A request only reads
the result. A pair is joined above a high score, or when an admin already
said so. Anything less stays two titles.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from .iptvnorm import AUTO_MERGE, UNSURE, Parsed, logo_key, parse, score

log = logging.getLogger("hub.iptvcanon")

KV = "tv.catalog.canonical"
KV_AI = "tv.catalog.ai"
KV_OVERRIDES = "tv.catalog.overrides"
KV_USAGE = "tv.catalog.ai.usage"
KV_ASKED = "tv.catalog.ai.asked"
# Bump when the matching rules change, so the next refresh compares every row again.
FINGERPRINT_VERSION = 2


def sources_from_catalog(doc: dict[str, Any], provider: str = "thomas-creek") -> list[dict[str, Any]]:
    """The rows already stored for one provider. This does not download anything."""
    rows = []
    for kind, bucket, ref_key in (("movie", "movies", "id"), ("series", "series", "id"), ("live", "channels", "key")):
        for row in doc.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            ref = str(row.get(ref_key) or "")
            stream_id = ref.split(":")[-1]
            name = row.get("name")
            if not stream_id or not name:
                continue
            rows.append({
                "provider": provider, "kind": kind, "stream_id": stream_id, "name": name,
                "year": row.get("year"), "ref": ref, "tvg_id": row.get("epg_id"),
                "logo": row.get("logo") or row.get("cover"), "tmdb": row.get("tmdb"),
            })
    return rows


def source_key(provider: str, kind: str, stream_id: str) -> str:
    return f"{provider}|{kind}|{stream_id}"


def observe(db: Any, sources: list[dict[str, Any]], *, decisions: dict[str, bool] | None = None,
            auto: float = AUTO_MERGE) -> dict[str, Any]:
    """Fold `sources` into the saved catalog. Unchanged rows are not compared again."""
    state = _state(db)
    overrides = _overrides(db)
    cache = dict(_ai(db))
    if decisions:
        for key, same in decisions.items():
            cache[key] = bool(same)
        db.kv_set(KV_AI, cache)
    fingerprints = dict(state.get("fingerprints") or {})
    previous = dict(state.get("by_source") or {})
    items = {item["id"]: item for item in (state.get("items") or []) if isinstance(item, dict) and item.get("id")}
    changed = False
    fresh: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sources:
        row = _source(raw)
        if row is None:
            continue
        seen.add(row["key"])
        fp = _fingerprint(row)
        if fingerprints.get(row["key"]) == fp and row["key"] in previous:
            continue
        fingerprints[row["key"]] = fp
        fresh.append(row)
        changed = True
    if not changed and set(previous) == seen:
        return _public(list(items.values()))
    # Drop sources that disappeared.
    for key in list(previous):
        if key not in seen:
            _detach(items, previous, key)
            fingerprints.pop(key, None)
            changed = True
    existing = _rows_from_items(items)
    by_key = {row["key"]: row for row in existing}
    for row in fresh:
        by_key[row["key"]] = row
    forced = set()
    fresh_keys = {row["key"] for row in fresh}
    for bundle in overrides.get("merge") or []:
        if isinstance(bundle, list) and any(key in fresh_keys for key in bundle):
            forced.update(bundle)
    groups, unsure_rows = _group(list(by_key.values()), fresh, previous, cache, overrides, auto, forced)
    groups = _glue(groups, overrides)
    for group in groups:
        for row in group:
            _detach(items, previous, row["key"])
    for group in groups:
        _place(group, items, previous, overrides)
    items = {item_id: item for item_id, item in items.items() if item.get("sources")}
    new_ids = {row["id"] for row in unsure_rows}
    kept = [row for row in state.get("unsure") or [] if isinstance(row, dict) and row.get("id") not in new_ids]
    state = {
        "items": [item for item in items.values() if item.get("id") != "__unsure__"],
        "by_source": previous,
        "fingerprints": fingerprints,
        "unsure": _open_pairs(unsure_rows + kept, items, previous, cache)[:500],
        "by_tmdb": _tmdb_index(list(by_key.values()), previous),
    }
    db.kv_set(KV, state)
    return _public(state["items"])


def search(db: Any, q: str = "", kind: str | None = None) -> dict[str, Any]:
    needle = q.strip().casefold()
    items = []
    for item in _public(_items(db))["items"]:
        if kind and item.get("kind") != kind:
            continue
        if needle and needle not in str(item.get("title") or "").casefold() and not any(
                needle in str(source.get("name") or "").casefold() for source in item.get("sources") or []):
            continue
        items.append(item)
    items.sort(key=lambda item: (str(item.get("title") or "").casefold(), item["id"]))
    return {"items": items}


def unsure(db: Any, limit: int | None = 20) -> dict[str, Any]:
    """Pairs still waiting for an answer. A pair already folded, kept apart or gone is left out."""
    state = _state(db)
    items = {item["id"]: item for item in _items(db)}
    rows = _open_pairs(list(state.get("unsure") or []), items, dict(state.get("by_source") or {}), _ai(db))
    return {"unsure": rows[:limit] if limit else rows, "total": len(rows)}


def keep_apart(db: Any, pair_id: str) -> bool:
    """An admin said the pair are different titles. It is not asked about again."""
    if not pair_id:
        return False
    cache = dict(_ai(db))
    cache[pair_id] = False
    db.kv_set(KV_AI, cache)
    state = _state(db)
    state["unsure"] = [row for row in state.get("unsure") or [] if isinstance(row, dict) and row.get("id") != pair_id]
    db.kv_set(KV, state)
    return True


def _open_pairs(rows: list[Any], items: dict[str, dict[str, Any]], by_source: dict[str, str],
                cache: dict[str, Any]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("id") in seen or row.get("id") in cache:
            continue
        left = by_source.get(str(row.get("left_key") or "")) or row.get("left")
        right = by_source.get(str(row.get("right_key") or "")) or row.get("right")
        if left not in items or right not in items or left == right:
            continue
        seen.add(row["id"])
        out.append({**row, "left": left, "right": right})
    return out


def merge(db: Any, ids: list[str]) -> dict[str, Any]:
    items = {item["id"]: item for item in _items(db)}
    chosen = [items[item_id] for item_id in ids if item_id in items]
    if len(chosen) < 2:
        return {}
    keep = chosen[0]
    keys = []
    for other in chosen:
        for source in other.get("sources") or []:
            keys.append(source_key(source["provider"], keep.get("kind") or "live", source["stream_id"]))
    for other in chosen[1:]:
        keep["sources"] = _dedupe_sources(list(keep.get("sources") or []) + list(other.get("sources") or []))
        items.pop(other["id"], None)
    items[keep["id"]] = keep
    overrides = _overrides(db)
    merges = list(overrides.get("merge") or [])
    bundle = sorted(set(keys))
    if bundle not in merges:
        merges.append(bundle)
    overrides["merge"] = merges
    _save_items(db, list(items.values()), overrides)
    return {"ok": True, "id": keep["id"]}


def split(db: Any, item_id: str, provider: str, stream_id: str) -> dict[str, Any]:
    items = {item["id"]: item for item in _items(db)}
    item = items.get(item_id)
    if item is None:
        return {}
    kind = item.get("kind") or "live"
    key = source_key(provider, str(kind), stream_id)
    kept = [source for source in item.get("sources") or [] if source_key(source["provider"], str(kind), source["stream_id"]) != key]
    moving = [source for source in item.get("sources") or [] if source_key(source["provider"], str(kind), source["stream_id"]) == key]
    if not moving:
        return {}
    item["sources"] = kept
    new_id = "can_" + hashlib.sha256(f"split|{key}".encode()).hexdigest()[:16]
    items[new_id] = {**item, "id": new_id, "sources": moving, "title": moving[0].get("name") or item.get("title")}
    if not kept:
        items.pop(item_id, None)
    overrides = _overrides(db)
    splits = list(overrides.get("split") or [])
    if key not in splits:
        splits.append(key)
    overrides["split"] = splits
    kept_merges = []
    for bundle in overrides.get("merge") or []:
        if not isinstance(bundle, list):
            continue
        rest = [item for item in bundle if item != key]
        if len(rest) >= 2:
            kept_merges.append(rest)
    overrides["merge"] = kept_merges
    _save_items(db, list(items.values()), overrides)
    return {"ok": True, "id": new_id}


def playable(db: Any, *, kind: str, title: str, year: int | None = None, tmdb: str | None = None) -> dict[str, Any] | None:
    """The best source for a title, or nothing when the catalog has not folded it in."""
    want = "series" if kind == "series" else "movie"
    parsed = parse(title, year)
    best = None
    if tmdb:
        item_id = (_state(db).get("by_tmdb") or {}).get(f"{want}:{tmdb}")
        best = next((item for item in _items(db) if item["id"] == item_id), None)
    if best is None:
        for item in _items(db):
            if item.get("kind") != want:
                continue
            if parse(str(item.get("title") or "")).key == parsed.key and (year is None or item.get("year") in (None, year)):
                best = item
                break
    if best is None:
        return None
    sources = list(best.get("sources") or [])
    sources.sort(key=lambda source: _quality_rank(source.get("quality")))
    chosen = sources[0] if sources else None
    if chosen is None:
        return None
    return {"canonical_id": best["id"], "ref": chosen.get("ref"), "provider": chosen.get("provider")}


_INFO_FIELDS = ("name", "year", "category", "genre", "rating", "tmdb", "plot", "duration_s")


def ai_payload(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the model is allowed to see: titles, years and the provider's own description. No logos or links."""
    safe = []
    for pair in pairs:
        row = {
            "id": pair["id"],
            "kind": pair["kind"],
            "left": pair["left"],
            "right": pair["right"],
            "left_year": pair.get("left_year"),
            "right_year": pair.get("right_year"),
        }
        for side in ("left_info", "right_info"):
            copies = []
            for info in pair.get(side) or []:
                if isinstance(info, dict):
                    copies.append({k: info[k] for k in _INFO_FIELDS if info.get(k) not in (None, "")})
            if copies:
                row[side] = copies[:3]
        if "same_provider" in pair:
            row["same_provider"] = bool(pair["same_provider"])
        safe.append(row)
    return safe


SYSTEM = (
    "You decide whether two entries in an IPTV provider's catalog are the same movie, show, or channel. "
    "Each pair has the cleaned titles, years, and left_info/right_info: the provider's raw names, year, category, "
    "genre, rating, TMDB id, plot and running time for each copy, when known. Use all of it. "
    "same is true when they are the same work (the same film in another quality, language category or folder "
    "counts as the same). same is false for remakes, sequels, different years, different TMDB ids, or a "
    "different plot. Use null when the information can't tell them apart. "
    "Reply with JSON only: {\"decisions\":[{\"id\":\"...\",\"same\":true}]}."
)


async def ask_model(hub: Any, pairs: list[dict[str, Any]]) -> dict[str, bool]:
    """Ask the cheap model about pairs the rules would not decide. A miss leaves them apart."""
    if not pairs:
        return {}
    secret = hub.config.assistant.key_secret
    key = hub.vault.get(secret) if hub.vault.has(secret) else None
    if not key:
        return {}
    cap = int(getattr(hub.config, "iptv_match", None).daily_tokens if getattr(hub.config, "iptv_match", None) else 20000)
    used = _used_today(hub.db)
    if used >= cap:
        log.info("catalog match: daily token cap reached")
        return {}
    model = getattr(getattr(hub.config, "iptv_match", None), "model", None) or "claude-haiku-5-5"
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=str(key), max_retries=1, timeout=60)
        response = await client.messages.create(
            model=model, max_tokens=2000, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(ai_payload(pairs))}],
        )
    except Exception as exc:  # noqa: BLE001
        log.info("catalog match: %s", type(exc).__name__)
        return {}
    tokens = int(getattr(getattr(response, "usage", None), "input_tokens", 0) or 0)
    tokens += int(getattr(getattr(response, "usage", None), "output_tokens", 0) or 0)
    _add_usage(hub.db, tokens)
    text = "".join(getattr(block, "text", "") for block in response.content)
    return _read_decisions(text)


ASK_AGAIN_S = 7 * 86400
BATCH = 25


async def settle(hub: Any, *, ask: Any = None, now: float | None = None, force: bool = False) -> dict[str, int]:
    """Ask the model about the waiting pairs, then fold the ones it calls the same and keep apart the rest.
    A pair it can't tell stays on the admin page, and isn't asked about again for a week."""
    db = hub.db
    now = time.time() if now is None else now
    ask = ask or ask_model
    raw = db.kv_get(KV_ASKED)
    asked = {k: v for k, v in raw.items() if now - float(v or 0) < ASK_AGAIN_S} if isinstance(raw, dict) else {}
    waiting = [row for row in unsure(db, limit=None)["unsure"] if force or row["id"] not in asked]
    folded = apart = 0
    catalog = getattr(hub, "tv_catalog", None)
    for start in range(0, len(waiting), BATCH):
        batch = [_with_context(db, catalog, row) for row in waiting[start:start + BATCH]]
        decisions = await ask(hub, batch)
        for row in batch:
            asked[row["id"]] = now
        db.kv_set(KV_ASKED, asked)
        if not decisions:
            if _used_today(db) >= _cap(hub):
                break
            continue
        for row in batch:
            same = decisions.get(row["id"])
            if same is True and merge(db, [row["left_id"], row["right_id"]]):
                folded += 1
            elif same is False and keep_apart(db, row["id"]):
                apart += 1
    if folded or apart:
        log.info("catalog match: model folded %d, kept apart %d", folded, apart)
    return {"folded": folded, "apart": apart, "left": len(unsure(db, limit=None)["unsure"])}


def _cap(hub: Any) -> int:
    cfg = getattr(hub.config, "iptv_match", None)
    return int(cfg.daily_tokens) if cfg else 20000


def _with_context(db: Any, catalog: Any, row: dict[str, Any]) -> dict[str, Any]:
    items = {item["id"]: item for item in _items(db)}
    doc = catalog.load() if catalog is not None else {}
    details = catalog._details() if catalog is not None and hasattr(catalog, "_details") else {}
    bucket = "series" if row.get("kind") == "series" else "movies" if row.get("kind") == "movie" else "channels"
    ref_key = "key" if bucket == "channels" else "id"
    rows = {str(r.get(ref_key)): r for r in doc.get(bucket) or [] if isinstance(r, dict)}
    more = (details.get(bucket) or {}) if isinstance(details, dict) else {}

    def info(item_id: str) -> tuple[list[dict[str, Any]], set[str]]:
        out, providers = [], set()
        for source in (items.get(item_id) or {}).get("sources") or []:
            providers.add(str(source.get("provider")))
            ref = str(source.get("ref") or "")
            base = rows.get(ref) or {}
            extra = more.get(ref) if isinstance(more.get(ref), dict) else {}
            plot = extra.get("plot")
            out.append({
                "name": source.get("name") or base.get("name"), "year": base.get("year") or extra.get("year"),
                "category": base.get("category"), "genre": base.get("genre") or extra.get("genre"),
                "rating": base.get("rating"), "tmdb": base.get("tmdb"),
                "plot": str(plot)[:240] if plot else None, "duration_s": extra.get("duration_s"),
            })
        return out, providers

    left, left_p = info(row["left"])
    right, right_p = info(row["right"])
    return {**row, "left": row.get("title"), "right": row.get("other"), "left_id": row["left"], "right_id": row["right"],
            "left_info": left, "right_info": right, "same_provider": bool(left_p & right_p)}


def _rows_from_items(items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items.values():
        if not isinstance(item, dict) or item.get("id") in (None, "__unsure__"):
            continue
        for source in item.get("sources") or []:
            row = _source({**source, "kind": item.get("kind"), "year": item.get("year")})
            if row is not None:
                rows.append(row)
    return rows


def _group(rows: list[dict[str, Any]], fresh: list[dict[str, Any]], previous: dict[str, str],
           cache: dict[str, Any], overrides: dict[str, Any], auto: float,
           forced: set[str] | None = None) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    fresh_keys = {row["key"] for row in fresh}
    splits = set(overrides.get("split") or [])
    touched = {_bucket(row) for row in fresh}
    held = forced or set()
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bucket = _bucket(row)
        if bucket not in touched and row["key"] not in fresh_keys and row["key"] not in held:
            continue
        buckets.setdefault(bucket, []).append(row)
    groups: list[list[dict[str, Any]]] = []
    unsure_rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        parent = list(range(len(bucket)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, left in enumerate(bucket):
            for j in range(i + 1, len(bucket)):
                right = bucket[j]
                if left["key"] in splits or right["key"] in splits:
                    continue
                if left["key"] not in fresh_keys and right["key"] not in fresh_keys:
                    if previous.get(left["key"]) and previous.get(left["key"]) == previous.get(right["key"]):
                        parent[find(j)] = find(i)
                    continue
                value = _pair_score(left, right)
                pair_id = _pair_id(left, right)
                if value >= auto or cache.get(pair_id) is True:
                    parent[find(j)] = find(i)
                elif UNSURE <= value < auto and pair_id not in cache:
                    unsure_rows.append({
                        "id": pair_id, "left_key": left["key"], "right_key": right["key"],
                        "title": left["parsed"].display, "other": right["parsed"].display,
                        "score": round(value, 2), "kind": left["kind"],
                        "left_year": left["parsed"].year, "right_year": right["parsed"].year,
                    })
        made: dict[int, list[dict[str, Any]]] = {}
        for i, row in enumerate(bucket):
            if row["key"] in splits:
                continue
            made.setdefault(find(i), []).append(row)
        groups.extend(made.values())
    return groups, unsure_rows


def _glue(groups: list[list[dict[str, Any]]], overrides: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Admin merges stay together even when the names would not."""
    bundles = [set(bundle) for bundle in overrides.get("merge") or [] if isinstance(bundle, list)]
    if not bundles:
        return groups
    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    where = {}
    for index, group in enumerate(groups):
        for row in group:
            where[row["key"]] = index
    for bundle in bundles:
        found = [where[key] for key in bundle if key in where]
        for other in found[1:]:
            parent[find(other)] = find(found[0])
    made: dict[int, list[dict[str, Any]]] = {}
    for index, group in enumerate(groups):
        made.setdefault(find(index), []).extend(group)
    return list(made.values())


def _place(group: list[dict[str, Any]], items: dict[str, dict[str, Any]], previous: dict[str, str],
           overrides: dict[str, Any]) -> None:
    if not group:
        return
    existing = [previous[row["key"]] for row in group if previous.get(row["key"]) in items and previous.get(row["key"]) != "__unsure__"]
    forced = _forced(group, overrides, items)
    item_id = forced or (existing[0] if existing else _new_id(group[0]["kind"], group[0]))
    current = items.get(item_id) or {"id": item_id, "kind": group[0]["kind"], "sources": []}
    by_key = {source_key(source["provider"], current.get("kind") or group[0]["kind"], source["stream_id"]): source
              for source in current.get("sources") or []}
    for row in group:
        by_key[row["key"]] = _public_source(row)
        previous[row["key"]] = item_id
    sources = list(by_key.values())
    sources.sort(key=lambda source: (source["provider"], source["stream_id"]))
    parsed = group[0]["parsed"]
    title = min((row["parsed"].display for row in group), key=len) or parsed.display
    year = next((row["parsed"].year for row in group if row["parsed"].year), None)
    items[item_id] = {
        "id": item_id,
        "kind": group[0]["kind"],
        "title": title,
        "year": year,
        "region": parsed.region,
        "variant": parsed.variant,
        "timeshift": parsed.timeshift,
        "season": next((row.get("season") for row in group if row.get("season")), None),
        "episode": next((row.get("episode") for row in group if row.get("episode")), None),
        "sources": sources,
    }


def _pair_score(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left["kind"] != right["kind"]:
        return 0.0
    return score(left["parsed"], right["parsed"], kind=left["kind"], left_ids=left["ids"], right_ids=right["ids"],
                 left_logo=left["logo"], right_logo=right["logo"], left_season=left.get("season"),
                 right_season=right.get("season"), left_episode=left.get("episode"), right_episode=right.get("episode"))


def _source(raw: dict[str, Any]) -> dict[str, Any] | None:
    provider = str(raw.get("provider") or "").strip()
    stream_id = str(raw.get("stream_id") or "").strip()
    kind = raw.get("kind")
    name = str(raw.get("name") or "").strip()
    if not provider or not stream_id or kind not in ("movie", "series", "live") or not name:
        return None
    parsed = parse(name, raw.get("year"), raw.get("language"))
    ids = {}
    for field in ("tmdb", "imdb", "tvg_id"):
        if raw.get(field):
            ids[field] = str(raw[field])
    return {
        "key": source_key(provider, kind, stream_id),
        "provider": provider, "kind": kind, "stream_id": stream_id, "name": name,
        "parsed": parsed, "ids": ids, "logo": logo_key(raw.get("logo")),
        "season": raw.get("season") if isinstance(raw.get("season"), int) else None,
        "episode": raw.get("episode") if isinstance(raw.get("episode"), int) else None,
        "ref": raw.get("ref") or _ref(kind, provider, stream_id),
    }


def _public_source(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": row["provider"], "stream_id": row["stream_id"], "ref": row["ref"],
        "quality": row["parsed"].quality, "language": row["parsed"].language, "name": row["name"],
    }


def _public(items: list[dict[str, Any]]) -> dict[str, Any]:
    shown = []
    for item in items:
        if item.get("id") == "__unsure__":
            continue
        row = {
            "id": item["id"], "kind": item["kind"], "title": item.get("title"), "year": item.get("year"),
            "sources": item.get("sources") or [],
        }
        if item.get("kind") == "live":
            row["region"] = item.get("region")
            row["variant"] = item.get("variant")
            row["timeshift"] = int(item.get("timeshift") or 0)
        if item.get("kind") == "series" and item.get("season") is not None:
            row["season"] = item["season"]
            row["episode"] = item.get("episode")
        shown.append(row)
    shown.sort(key=lambda item: item["id"])
    return {"items": shown}


def _bucket(row: dict[str, Any]) -> str:
    return f"{row['kind']}|{row['parsed'].key}|{row['parsed'].variant or ''}|{row['parsed'].timeshift}"


def _pair_id(left: dict[str, Any], right: dict[str, Any]) -> str:
    ends = tuple(sorted((left["parsed"].key + "|" + str(left["parsed"].year), right["parsed"].key + "|" + str(right["parsed"].year))))
    return hashlib.sha256((left["kind"] + "\n" + "\n".join(ends)).encode()).hexdigest()[:20]


def _new_id(kind: str, row: dict[str, Any]) -> str:
    hard = row["ids"].get("tmdb") or row["ids"].get("imdb") or row["ids"].get("tvg_id")
    identity = f"{kind}|{hard}" if hard else f"{kind}|{row['parsed'].key}|{row['parsed'].year}|{row['parsed'].variant}|{row['parsed'].timeshift}|{row['parsed'].region}"
    return "can_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def _fingerprint(row: dict[str, Any]) -> str:
    payload = json.dumps({
        "v": FINGERPRINT_VERSION, "name": row["name"], "year": row["parsed"].year, "ids": row["ids"], "logo": row["logo"],
        "season": row.get("season"), "episode": row.get("episode"),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _ref(kind: str, provider: str, stream_id: str) -> str:
    prefix = {"movie": "vod", "series": "series", "live": "xc"}[kind]
    return f"{prefix}:{provider}:{stream_id}"


def _tmdb_index(rows: list[dict[str, Any]], previous: dict[str, str]) -> dict[str, str]:
    found = {}
    for row in rows:
        tmdb = row["ids"].get("tmdb")
        item_id = previous.get(row["key"])
        if tmdb and item_id:
            found[f"{row['kind']}:{tmdb}"] = item_id
    return found


def _quality_rank(quality: object) -> int:
    return {"2160p": 0, "1080p": 1, "720p": 2, "sd": 3}.get(str(quality or ""), 4)


def _detach(items: dict[str, dict[str, Any]], previous: dict[str, str], key: str) -> None:
    item_id = previous.pop(key, None)
    item = items.get(item_id or "")
    if not item:
        return
    kind = item.get("kind")
    item["sources"] = [source for source in item.get("sources") or []
                       if source_key(source["provider"], str(kind), source["stream_id"]) != key]
    if not item["sources"]:
        items.pop(item_id, None)


def _forced(group: list[dict[str, Any]], overrides: dict[str, Any], items: dict[str, dict[str, Any]]) -> str | None:
    return None


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = {}
    for source in sources:
        found[(source.get("provider"), source.get("stream_id"))] = source
    return list(found.values())


def _items(db: Any) -> list[dict[str, Any]]:
    return [item for item in (_state(db).get("items") or []) if isinstance(item, dict) and item.get("id") and item["id"] != "__unsure__"]


def _state(db: Any) -> dict[str, Any]:
    raw = db.kv_get(KV) or {}
    return raw if isinstance(raw, dict) else {}


def _ai(db: Any) -> dict[str, Any]:
    raw = db.kv_get(KV_AI) or {}
    return raw if isinstance(raw, dict) else {}


def _overrides(db: Any) -> dict[str, Any]:
    raw = db.kv_get(KV_OVERRIDES) or {}
    return raw if isinstance(raw, dict) else {}


def _save_items(db: Any, items: list[dict[str, Any]], overrides: dict[str, Any]) -> None:
    state = _state(db)
    by_source = {}
    for item in items:
        if item.get("id") == "__unsure__":
            continue
        for source in item.get("sources") or []:
            by_source[source_key(source["provider"], item.get("kind") or "live", source["stream_id"])] = item["id"]
    state["items"] = items
    state["by_source"] = by_source
    db.kv_set(KV, state)
    db.kv_set(KV_OVERRIDES, overrides)


def _load_unsure(items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    raw = items.get("__unsure__") or {}
    rows = raw.get("rows") if isinstance(raw, dict) else None
    return list(rows or [])


def _used_today(db: Any) -> int:
    raw = db.kv_get(KV_USAGE) or {}
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if not isinstance(raw, dict) or raw.get("day") != day:
        return 0
    return int(raw.get("tokens") or 0)


def _add_usage(db: Any, tokens: int) -> None:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    raw = db.kv_get(KV_USAGE) or {}
    current = int(raw.get("tokens") or 0) if isinstance(raw, dict) and raw.get("day") == day else 0
    db.kv_set(KV_USAGE, {"day": day, "tokens": current + tokens})


def _read_decisions(text: str) -> dict[str, bool]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out = {}
    for row in payload.get("decisions") or []:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("same"), bool):
            out[row["id"]] = row["same"]
    return out
