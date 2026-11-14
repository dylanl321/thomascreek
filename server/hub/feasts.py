"""Recipe sections, holiday and feast plans, and the merged shopping list.

Where things live:
- Sections (Mains, Sides, Desserts...) are Mealie *categories* on each recipe, so
  they show in Mealie too. Older recipes tagged "Dessert" or "Side" count as well.
- Feast plans (Thanksgiving, Christmas...) are kept on the hub (`feast_plans`):
  Mealie has meal plans per day but nothing for a named event with courses,
  guests and a prep timeline. A plan can be copied onto Mealie's meal plan.
- The shopping list is worked out here from Mealie's recipe ingredients (scaled,
  merged, sorted by aisle) and pushed to a Mealie shopping list.

Everything below the storage section is plain functions, so it is easy to test.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import date, timedelta
from typing import Any, Iterable

# -- sections -----------------------------------------------------------------------
SECTIONS = ["Appetizers", "Mains", "Sides", "Salads", "Soups", "Breads", "Desserts", "Drinks",
            "Breakfast", "Snacks", "Sauces"]
SECTION_SYMBOLS = {
    "Appetizers": "takeoutbag.and.cup.and.straw", "Mains": "fork.knife", "Sides": "carrot",
    "Salads": "leaf", "Soups": "mug", "Breads": "birthday.cake", "Desserts": "birthday.cake.fill",
    "Drinks": "wineglass", "Breakfast": "sunrise", "Snacks": "popcorn", "Sauces": "drop",
}
# Tags (and loose category names) that mean a section: "Dessert" -> "Desserts".
_SECTION_ALIASES = {
    "appetizer": "Appetizers", "appetizers": "Appetizers", "starter": "Appetizers", "starters": "Appetizers",
    "hors d'oeuvres": "Appetizers", "main": "Mains", "mains": "Mains", "main course": "Mains", "main dish": "Mains",
    "entree": "Mains", "entrees": "Mains", "dinner": "Mains", "side": "Sides", "sides": "Sides", "side dish": "Sides",
    "side dishes": "Sides", "salad": "Salads", "salads": "Salads", "soup": "Soups", "soups": "Soups",
    "bread": "Breads", "breads": "Breads", "rolls": "Breads", "dessert": "Desserts", "desserts": "Desserts",
    "pie": "Desserts", "pies": "Desserts", "cookies": "Desserts", "baking": "Desserts", "sweets": "Desserts",
    "drink": "Drinks", "drinks": "Drinks", "beverage": "Drinks", "beverages": "Drinks", "cocktail": "Drinks",
    "cocktails": "Drinks", "breakfast": "Breakfast", "brunch": "Breakfast", "snack": "Snacks", "snacks": "Snacks",
    "sauce": "Sauces", "sauces": "Sauces", "gravy": "Sauces",
}
# Sections that are not a dinner on their own (the "Fill the week" picker skips them).
NOT_DINNER = {"Appetizers", "Sides", "Salads", "Breads", "Desserts", "Drinks", "Breakfast", "Snacks", "Sauces"}


def section_name(name: str) -> str | None:
    """'dessert' -> 'Desserts'; None when it isn't a section word."""
    n = str(name or "").strip()
    if not n:
        return None
    for s in SECTIONS:
        if s.lower() == n.lower():
            return s
    return _SECTION_ALIASES.get(n.lower())


def sections_of(categories: Iterable[str], tags: Iterable[str]) -> list[str]:
    """A recipe's sections: its Mealie categories, plus course-like tags. Unknown
    categories are kept as they are (someone made a "Holiday" section in Mealie)."""
    out: list[str] = []
    for c in categories:
        s = section_name(c) or str(c).strip()
        if s and s not in out:
            out.append(s)
    for t in tags:
        s = section_name(t)
        if s and s not in out:
            out.append(s)
    return out


def is_dinner(sections: list[str]) -> bool:
    return not sections or "Mains" in sections or not (set(sections) & NOT_DINNER)


def group_by_section(recipes: list[dict[str, Any]], extra: Iterable[str] = ()) -> dict[str, Any]:
    """Recipes grouped by section, in menu order, plus the ones with no section."""
    order = list(SECTIONS) + [e for e in extra if e not in SECTIONS]
    groups: dict[str, list[dict[str, Any]]] = {}
    unsorted = []
    for r in recipes:
        secs = r.get("sections") or []
        if not secs:
            unsorted.append(r)
        for s in secs:
            groups.setdefault(s, []).append(r)
            if s not in order:
                order.append(s)
    return {
        "sections": [{"name": s, "symbol": SECTION_SYMBOLS.get(s, "square.grid.2x2"), "count": len(groups.get(s, [])),
                      "items": groups.get(s, [])} for s in order],
        "unsorted": unsorted,
        "names": order,
    }


# -- holiday dates and templates ----------------------------------------------------------
def thanksgiving(year: int) -> date:
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(weeks=3)


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lt = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lt) // 451
    n = h + lt - 7 * m + 114
    return date(year, n // 31, n % 31 + 1)


COURSES = {
    "appetizers": "Appetizers", "mains": "Mains", "sides": "Sides", "salads": "Salads", "soups": "Soups",
    "breads": "Breads", "desserts": "Desserts", "drinks": "Drinks", "breakfast": "Breakfast", "other": "Other",
}
# When each course is usually made, for the prep timeline.
WHEN = ["days_before", "day_before", "morning", "day_of", "last_minute"]
WHEN_LABELS = {"days_before": "A few days before", "day_before": "The day before", "morning": "Morning of",
               "day_of": "Day of", "last_minute": "Right before serving"}
COURSE_WHEN = {"appetizers": "day_of", "mains": "morning", "sides": "day_of", "salads": "last_minute",
               "soups": "day_before", "breads": "morning", "desserts": "day_before", "drinks": "day_of",
               "breakfast": "morning", "other": "day_of"}

TEMPLATES: dict[str, dict[str, Any]] = {
    "thanksgiving": {"title": "Thanksgiving", "symbol": "leaf.fill", "guests": 10,
                     "courses": ["appetizers", "mains", "sides", "breads", "desserts", "drinks"]},
    "christmas": {"title": "Christmas Feast", "symbol": "gift.fill", "guests": 10,
                  "courses": ["breakfast", "appetizers", "mains", "sides", "breads", "desserts", "drinks"]},
    "easter": {"title": "Easter", "symbol": "sun.max.fill", "guests": 8,
               "courses": ["breakfast", "mains", "sides", "salads", "desserts", "drinks"]},
    "feast": {"title": "Feast", "symbol": "fork.knife", "guests": 8,
              "courses": ["appetizers", "mains", "sides", "desserts", "drinks"]},
}


def template_date(kind: str, today: date) -> date:
    def on(year: int) -> date:
        if kind == "thanksgiving":
            return thanksgiving(year)
        if kind == "christmas":
            return date(year, 12, 25)
        if kind == "easter":
            return easter(year)
        return today
    d = on(today.year)
    return d if d >= today else on(today.year + 1)


def templates(today: date) -> list[dict[str, Any]]:
    out = []
    for k, t in TEMPLATES.items():
        d = template_date(k, today)
        out.append({"id": k, "title": t["title"], "symbol": t["symbol"], "guests": t["guests"],
                    "date": d.isoformat(), "courses": [COURSES[c] for c in t["courses"]]})
    return out


def new_plan(kind: str, today: date, name: str | None = None, start: date | None = None, end: date | None = None,
             guests: int | None = None) -> dict[str, Any]:
    t = TEMPLATES.get(kind) or TEMPLATES["feast"]
    s = start or template_date(kind if kind in TEMPLATES else "feast", today)
    title = name or (f"{t['title']} {s.year}" if kind in ("thanksgiving", "easter", "christmas") else t["title"])
    return {
        "name": title[:80], "kind": kind if kind in TEMPLATES else "feast",
        "start_date": s.isoformat(), "end_date": (end or s).isoformat(),
        "guests": guests or t["guests"], "notes": "",
        "courses": [{"key": c, "title": COURSES[c], "items": []} for c in t["courses"]],
    }


def clean_courses(courses: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    """Validates courses from the app: known keys or a custom title, items with a slug or a title."""
    out = []
    seen = set()
    for c in courses[:12]:
        key = str(c.get("key") or "other")[:30]
        title = str(c.get("title") or COURSES.get(key) or key.title())[:40]
        if key in seen:
            key = f"{key}-{len(seen)}"
        seen.add(key)
        items = []
        for it in (c.get("items") or [])[:40]:
            slug, title_i = it.get("slug"), it.get("title")
            if not slug and not title_i:
                continue
            when = it.get("when") if it.get("when") in WHEN else COURSE_WHEN.get(key.split("-")[0], "day_of")
            day = it.get("day")
            try:
                day = date.fromisoformat(day).isoformat() if day and start <= date.fromisoformat(day) <= end else None
            except ValueError:
                day = None
            servings = it.get("servings")
            try:
                servings = max(1, min(int(servings), 200)) if servings not in (None, "") else None
            except (TypeError, ValueError):
                servings = None
            items.append({
                "id": str(it.get("id") or _item_id())[:40], "slug": slug and str(slug)[:200],
                "recipe_id": it.get("recipe_id"), "title": str(title_i or slug)[:120],
                "image_url": it.get("image_url"), "servings": servings,
                "base_servings": it.get("base_servings"), "when": when, "day": day,
                "note": (str(it.get("note"))[:200] if it.get("note") else None),
            })
        out.append({"key": key, "title": title, "items": items})
    return out


def timeline(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The prep timeline: what to make when, in order."""
    rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in plan.get("courses") or []:
        for it in c.get("items") or []:
            day = it.get("day") or plan["start_date"]
            rows.setdefault((day, it.get("when") or "day_of"), []).append(
                {"title": it.get("title"), "slug": it.get("slug"), "course": c.get("title")})
    out = []
    for (day, when) in sorted(rows, key=lambda k: (k[0], WHEN.index(k[1]) if k[1] in WHEN else 9)):
        out.append({"day": day, "when": when, "label": WHEN_LABELS.get(when, when), "items": rows[(day, when)]})
    return out


def _item_id() -> str:
    import secrets
    return "fi_" + secrets.token_hex(5)


# -- storage ------------------------------------------------------------------------
class FeastStore:
    def __init__(self, db: Any):
        self.db = db

    def _out(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        out = dict(row)
        try:
            out["courses"] = json.loads(out.get("courses") or "[]")
        except (TypeError, json.JSONDecodeError):
            out["courses"] = []
        out["recipe_count"] = sum(len(c.get("items") or []) for c in out["courses"])
        return out

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM feast_plans ORDER BY start_date DESC, created_at DESC")
        return [self._out(r) for r in rows]  # type: ignore[misc]

    def get(self, plan_id: str) -> dict[str, Any] | None:
        return self._out(self.db.one("SELECT * FROM feast_plans WHERE id=?", [plan_id]))

    def create(self, plan: dict[str, Any], person_id: str | None) -> dict[str, Any]:
        from .db import new_id
        now = time.time()
        pid = new_id("feast")
        self.db.execute(
            "INSERT INTO feast_plans (id, name, kind, start_date, end_date, guests, notes, courses, created_by, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [pid, plan["name"], plan["kind"], plan["start_date"], plan["end_date"], int(plan["guests"]),
             plan.get("notes") or "", json.dumps(plan["courses"]), person_id, now, now])
        return self.get(pid)  # type: ignore[return-value]

    def save(self, plan_id: str, plan: dict[str, Any]) -> dict[str, Any] | None:
        self.db.execute(
            "UPDATE feast_plans SET name=?, kind=?, start_date=?, end_date=?, guests=?, notes=?, courses=?, updated_at=? "
            "WHERE id=?",
            [plan["name"], plan["kind"], plan["start_date"], plan["end_date"], int(plan["guests"]),
             plan.get("notes") or "", json.dumps(plan["courses"]), time.time(), plan_id])
        return self.get(plan_id)

    def delete(self, plan_id: str) -> None:
        self.db.execute("DELETE FROM feast_plans WHERE id=?", [plan_id])


# -- ingredients: read, scale, merge, sort by aisle --------------------------------------
_FRACS = {"½": .5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": .25, "¾": .75, "⅛": .125, "⅜": .375, "⅝": .625, "⅞": .875,
          "⅕": .2, "⅖": .4, "⅗": .6, "⅘": .8, "⅙": 1 / 6, "⅚": 5 / 6}
_FRAC_CH = "".join(_FRACS)
_NUM = rf"(?:\d+\s+\d+/\d+|\d+/\d+|\d+\s*[{_FRAC_CH}]|\d*\.\d+|\d+|[{_FRAC_CH}])"
_AMOUNT = re.compile(rf"^\s*({_NUM})(?:\s*(?:-|–|to)\s*({_NUM}))?\s*")

# unit -> (canonical, family, size in the family's base unit)
_UNITS: dict[str, tuple[str, str, float]] = {}
for names, canon, fam, size in [
    (("teaspoon", "teaspoons", "tsp", "tsps", "tsp.", "t"), "tsp", "vol", 1),
    (("tablespoon", "tablespoons", "tbsp", "tbsps", "tbsp.", "tbs", "tbl", "T"), "tbsp", "vol", 3),
    (("cup", "cups", "c", "c."), "cup", "vol", 48),
    (("fluid ounce", "fluid ounces", "fl oz", "fl. oz."), "fl oz", "vol", 6),
    (("pint", "pints", "pt"), "pint", "vol", 96),
    (("quart", "quarts", "qt"), "quart", "vol", 192),
    (("gallon", "gallons", "gal"), "gallon", "vol", 768),
    (("ounce", "ounces", "oz", "oz."), "oz", "wt", 1),
    (("pound", "pounds", "lb", "lbs", "lb.", "lbs."), "lb", "wt", 16),
    (("gram", "grams", "g", "gr"), "g", "g", 1),
    (("kilogram", "kilograms", "kg"), "kg", "g", 1000),
    (("milliliter", "milliliters", "millilitre", "ml"), "ml", "ml", 1),
    (("liter", "liters", "litre", "litres", "l"), "l", "ml", 1000),
]:
    for n in names:
        _UNITS[n] = (canon, fam, size)
# Counted things: they merge only with the same word.
_COUNT_UNITS = {
    "clove": "clove", "cloves": "clove", "can": "can", "cans": "can", "package": "package", "packages": "package",
    "pkg": "package", "packet": "package", "packets": "package", "stick": "stick", "sticks": "stick",
    "bunch": "bunch", "bunches": "bunch", "head": "head", "heads": "head", "sprig": "sprig", "sprigs": "sprig",
    "slice": "slice", "slices": "slice", "jar": "jar", "jars": "jar", "bag": "bag", "bags": "bag", "box": "box",
    "boxes": "box", "bottle": "bottle", "bottles": "bottle", "pinch": "pinch", "pinches": "pinch", "dash": "dash",
    "dashes": "dash", "container": "container", "containers": "container", "loaf": "loaf", "loaves": "loaf",
    "envelope": "envelope", "envelopes": "envelope", "sheet": "sheet", "sheets": "sheet",
}
_PREP = {"large", "small", "medium", "fresh", "freshly", "chopped", "minced", "diced", "sliced", "finely",
         "roughly", "coarsely", "thinly", "grated", "shredded", "softened", "melted", "packed", "lightly", "peeled",
         "cubed", "crushed", "beaten", "divided", "room", "temperature", "cold", "warm", "whole", "about",
         "plus", "more", "optional", "to", "taste", "halved", "quartered", "trimmed", "rinsed", "drained",
         "unsalted", "salted", "boneless", "skinless", "extra", "virgin", "heaping", "level", "ripe", "of"}
_KEEP_PLURAL = {"asparagus", "molasses", "hummus", "couscous", "swiss", "brussels", "grits", "oats", "greens",
                "chives", "lentils", "peas", "noodles", "berries", "cranberries", "pecans", "walnuts", "almonds",
                "raisins", "chips", "sprinkles", "crumbs", "breadcrumbs"}


def _num(s: str) -> float:
    s = s.strip()
    total = 0.0
    for ch, v in _FRACS.items():
        if ch in s:
            total += v
            s = s.replace(ch, "").strip()
    for part in s.split():
        if "/" in part:
            a, b = part.split("/", 1)
            total += float(a) / float(b) if float(b) else 0
        elif part:
            total += float(part)
    return total


def _singular(word: str) -> str:
    if word in _KEEP_PLURAL or len(word) < 4:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def food_key(name: str) -> str:
    n = re.sub(r"\([^)]*\)", " ", name.lower())
    n = n.split(",")[0]
    words = [w for w in re.findall(r"[a-zà-ÿ'\-]+", n) if w not in _PREP]
    if not words:
        return n.strip()
    words[-1] = _singular(words[-1])
    return " ".join(words)


def _display_name(name: str) -> str:
    n = re.sub(r"\([^)]*\)", " ", name)
    n = n.split(",")[0]
    n = re.sub(r"\s+", " ", n).strip(" -–.;:")
    return n


def parse_line(text: str) -> dict[str, Any]:
    """'1 1/2 cups flour, sifted' -> qty 1.5, unit cup, name flour."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lstrip("-•* ").strip()
    qty = None
    m = _AMOUNT.match(t)
    if m:
        try:
            qty = _num(m.group(2) or m.group(1))
        except (ValueError, ZeroDivisionError):
            qty = None
        t = t[m.end():]
    unit = None
    m2 = re.match(r"^\(([^)]*)\)\s*", t)  # "1 (15 oz) can pumpkin": keep the can
    if m2:
        t = t[m2.end():]
    for n in sorted(list(_UNITS) + list(_COUNT_UNITS), key=len, reverse=True):
        if re.match(rf"^{re.escape(n)}(?=\s|$)", t if n in ("T", "t") else t.lower()) and (qty is not None or n not in ("t", "T", "c", "l", "g")):
            unit = n
            t = t[len(n):].strip()
            break
    t = re.sub(r"^of\s+", "", t, flags=re.I)
    return {"qty": qty, "unit": unit, "name": _display_name(t) or str(text).strip(), "text": str(text).strip()}


def from_mealie(ing: dict[str, Any]) -> dict[str, Any]:
    """One Mealie recipe ingredient -> qty, unit, name, label (aisle) when Mealie knows them."""
    food = ing.get("food") or {}
    raw = ing.get("originalText") or ing.get("note") or ing.get("display") or ""
    if food.get("name"):
        unit = ing.get("unit") or {}
        q = ing.get("quantity")
        label = (food.get("label") or {}).get("name")
        return {"qty": float(q) if q else None, "unit": unit.get("name") or unit.get("abbreviation") or None,
                "name": food["name"], "text": ing.get("display") or raw, "label": label}
    out = parse_line(raw)
    out["label"] = None
    return out


def unit_info(unit: str | None) -> tuple[str | None, str, float]:
    """(canonical unit, family, size). Family 'count:<word>' or 'each' merges only with itself."""
    if not unit:
        return None, "each", 1
    u = unit.strip()
    hit = _UNITS.get(u) or _UNITS.get(u.lower())
    if hit:
        return hit
    c = _COUNT_UNITS.get(u.lower())
    if c:
        return c, f"count:{c}", 1
    return u.lower(), f"count:{u.lower()}", 1


def _frac(x: float) -> str:
    whole = int(x)
    rest = x - whole
    best = min([(0, ""), (.125, "⅛"), (.25, "¼"), (1 / 3, "⅓"), (.375, "⅜"), (.5, "½"), (.625, "⅝"),
                (2 / 3, "⅔"), (.75, "¾"), (.875, "⅞"), (1, "")], key=lambda p: abs(p[0] - rest))
    if best[0] == 1:
        whole, best = whole + 1, (0, "")
    if whole and best[1]:
        return f"{whole}{best[1]}"
    return best[1] or str(whole)


def fmt_qty(x: float) -> str:
    if x >= 10:
        return str(int(round(x)))
    return _frac(x) if abs(x - round(x)) > 0.04 else str(int(round(x)))


def _best_unit(fam: str, base: float) -> tuple[float, str]:
    if fam == "vol":
        if base >= 12:
            return base / 48, "cup"
        if base >= 3:
            return base / 3, "tbsp"
        return base, "tsp"
    if fam == "wt":
        return (base / 16, "lb") if base >= 16 else (base, "oz")
    if fam == "g":
        return (base / 1000, "kg") if base >= 1000 else (base, "g")
    if fam == "ml":
        return (base / 1000, "l") if base >= 1000 else (base, "ml")
    return base, ""


_PLURAL_UNITS = {"cup": "cups", "lb": "lb", "clove": "cloves", "can": "cans", "package": "packages", "stick": "sticks",
                 "bunch": "bunches", "head": "heads", "sprig": "sprigs", "slice": "slices", "jar": "jars", "bag": "bags",
                 "box": "boxes", "bottle": "bottles", "pinch": "pinches", "dash": "dashes", "container": "containers",
                 "loaf": "loaves", "envelope": "envelopes", "sheet": "sheets", "quart": "quarts", "pint": "pints",
                 "gallon": "gallons"}


def _ceil_count(x: float) -> float:
    """2.4 eggs -> 3; ½ onion stays ½."""
    if x < 1:
        return x
    return float(math.ceil(x - 0.05))


AISLES = ["Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Baking", "Spices", "Canned & Jarred", "Pantry",
          "Frozen", "Drinks", "Other"]
# First match wins, so the specific words come first.
_AISLE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Frozen", ("frozen", "ice cream", "puff pastry", "whipped topping", "cool whip", "phyllo")),
    ("Canned & Jarred", ("canned", "broth", "stock", "pumpkin puree", "pumpkin purée", "tomato paste", "tomato sauce",
                         "crushed tomato", "diced tomato", "cranberry sauce", "evaporated milk", "condensed milk",
                         "cream of", "beans", "chickpea", "olives", "pickle", "salsa", "jam", "jelly", "preserves")),
    ("Pantry", ("vinegar", "oil", "cooking spray", "soy sauce", "worcestershire", "honey", "maple syrup", "syrup",
                "peanut butter", "mustard", "ketchup", "mayonnaise", "mayo", "hot sauce")),
    ("Meat & Seafood", ("turkey", "chicken", "beef", "pork", "ham", "bacon", "sausage", "lamb", "salmon", "shrimp",
                        "fish", "steak", "prosciutto", "pancetta", "brisket", "roast", "tenderloin", "crab", "lobster",
                        "scallop", "duck", "veal", "meatball", "chorizo")),
    ("Dairy & Eggs", ("milk", "butter", "cream", "cheese", "egg", "yogurt", "buttermilk", "half-and-half",
                      "half and half", "parmesan", "mozzarella", "cheddar", "ricotta", "mascarpone", "ghee")),
    ("Baking", ("flour", "sugar", "baking soda", "baking powder", "yeast", "vanilla", "chocolate chip", "chocolate",
                "cocoa", "cornstarch", "corn starch", "molasses", "corn syrup", "sprinkles", "extract", "gelatin",
                "pie crust", "graham cracker", "marshmallow", "shortening", "food coloring", "pudding mix", "cake mix")),
    ("Spices", ("salt", "black pepper", "peppercorn", "pepper flakes", "cayenne", "cinnamon", "nutmeg", "paprika",
                "cumin", "allspice", "ground clove", "ground ginger", "dried", "garlic powder", "onion powder",
                "chili powder", "curry", "turmeric", "seasoning", "bay lea", "cardamom", "pumpkin pie spice",
                "italian seasoning", "poultry seasoning", "spice")),
    ("Produce", ("onion", "garlic", "shallot", "potato", "carrot", "celery", "lettuce", "tomato", "apple", "lemon",
                 "lime", "orange", "berr", "cranberr", "parsley", "thyme", "rosemary", "sage", "cilantro", "basil",
                 "mint", "dill", "chive", "ginger", "bell pepper", "jalape", "squash", "pumpkin", "sweet potato", "yam",
                 "green bean", "brussels", "broccoli", "cauliflower", "spinach", "kale", "mushroom", "cucumber",
                 "zucchini", "avocado", "banana", "pear", "grape", "corn", "pea", "asparagus", "leek", "scallion",
                 "green onion", "herb", "fruit", "vegetable", "cabbage", "beet", "radish", "fennel", "peach",
                 "cherr", "pomegranate", "fig", "date", "arugula", "salad greens")),
    ("Bakery", ("bread", "roll", "bun", "baguette", "tortilla", "croissant", "brioche", "pita", "biscuit")),
    ("Pantry", ("rice", "pasta", "noodle", "pecan", "walnut", "almond", "nut", "breadcrumb", "bread crumb",
                "stuffing", "oat", "cereal", "cracker", "raisin", "quinoa", "lentil", "coconut", "sauce", "water")),
    ("Drinks", ("wine", "beer", "juice", "soda", "sparkling", "coffee", "tea", "cider", "champagne", "bourbon",
                "rum", "vodka", "whiskey", "brandy", "gin", "tequila", "liqueur", "seltzer", "tonic", "eggnog")),
]
_LABEL_AISLE = {"produce": "Produce", "fruit": "Produce", "vegetables": "Produce", "meat": "Meat & Seafood",
                "seafood": "Meat & Seafood", "dairy": "Dairy & Eggs", "eggs": "Dairy & Eggs", "bakery": "Bakery",
                "bread": "Bakery", "baking": "Baking", "spices": "Spices", "canned": "Canned & Jarred",
                "pantry": "Pantry", "frozen": "Frozen", "beverages": "Drinks", "drinks": "Drinks"}

STAPLES = {"salt", "kosher salt", "sea salt", "table salt", "black pepper", "pepper", "ground black pepper",
           "salt and pepper", "water", "ice", "olive oil", "vegetable oil", "canola oil", "cooking spray",
           "nonstick cooking spray", "salt and black pepper", "warm water", "cold water", "ice water"}


def aisle_for(name: str, label: str | None = None) -> str:
    if label:
        for k, v in _LABEL_AISLE.items():
            if k in label.lower():
                return v
    n = " " + name.lower() + " "
    if food_key(name) == "pepper":
        return "Spices"
    for aisle, words in _AISLE_RULES:
        for w in words:
            if re.search(rf"\b{re.escape(w)}", n):
                return aisle
    return "Other"


def is_staple(name: str) -> bool:
    k = food_key(name)
    return k in STAPLES or name.lower().strip() in STAPLES


def servings_number(v: Any) -> float | None:
    if v in (None, ""):
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m and float(m.group()) > 0 else None


def build_list(sources: list[dict[str, Any]], skip_staples: bool = False) -> dict[str, Any]:
    """sources: [{title, factor, ingredients: [{qty, unit, name, text, label}]}] -> one merged list.

    Same food in compatible units adds up (2 tbsp + ¼ cup butter = 6 tbsp); amounts
    are scaled by each recipe's factor; each line says which recipes it is for."""
    lines: dict[tuple[str, str], dict[str, Any]] = {}
    for src in sources:
        f = float(src.get("factor") or 1)
        for ing in src.get("ingredients") or []:
            name = (ing.get("name") or "").strip()
            if not name:
                continue
            key = food_key(name)
            if not key:
                continue
            canon, fam, size = unit_info(ing.get("unit"))
            qty = ing.get("qty")
            fam_key = fam if qty else "none"
            line = lines.setdefault((key, fam_key), {
                "key": f"{key}|{fam_key}", "food": key, "name": name, "family": fam_key, "unit": canon,
                "base": 0.0, "aisle": aisle_for(name, ing.get("label")), "recipes": [], "lines": [],
                "staple": is_staple(name),
            })
            if qty:
                line["base"] += float(qty) * f * size
            title = src.get("title")
            if title and title not in line["recipes"]:
                line["recipes"].append(title)
            if ing.get("text") and len(line["lines"]) < 8:
                line["lines"].append(ing["text"])
    # "salt to taste" next to "1 tsp salt": fold the no-amount line into the measured one.
    for (key, fam), line in list(lines.items()):
        if fam != "none":
            continue
        other = next((v for (k, fm), v in lines.items() if k == key and fm != "none"), None)
        if other:
            for r in line["recipes"]:
                if r not in other["recipes"]:
                    other["recipes"].append(r)
            other["lines"] += line["lines"][: max(0, 8 - len(other["lines"]))]
            del lines[(key, fam)]
    items = []
    for line in lines.values():
        fam = line["family"]
        qty: float | None
        unit = line["unit"]
        if fam == "none":
            qty, unit = None, None
        elif fam in ("vol", "wt", "g", "ml"):
            qty, unit = _best_unit(fam, line["base"])
        else:
            qty = _ceil_count(line["base"])
        text = line["name"]
        if qty:
            u = unit or ""
            if u and qty > 1 and u in _PLURAL_UNITS:
                u = _PLURAL_UNITS[u]
            text = " ".join(p for p in (fmt_qty(qty), u, line["name"]) if p)
        items.append({"key": line["key"], "name": line["name"], "food": line["food"], "qty": qty and round(qty, 3),
                      "unit": unit, "text": text, "aisle": line["aisle"], "recipes": line["recipes"],
                      "lines": line["lines"], "staple": line["staple"]})
    staples = [i for i in items if i["staple"]]
    if skip_staples:
        items = [i for i in items if not i["staple"]]
    items.sort(key=lambda i: (AISLES.index(i["aisle"]) if i["aisle"] in AISLES else 99, i["food"]))
    aisles = []
    for a in AISLES:
        group = [i for i in items if i["aisle"] == a]
        if group:
            aisles.append({"name": a, "items": group})
    return {"items": items, "aisles": aisles, "count": len(items), "staples_skipped": len(staples) if skip_staples else 0}


def push_text(item: dict[str, Any]) -> str:
    """What goes on the Mealie list: '3 cups flour (Pie, Rolls)'."""
    rec = item.get("recipes") or []
    if not rec:
        return item["text"]
    names = ", ".join(rec[:3]) + (f" +{len(rec) - 3}" if len(rec) > 3 else "")
    return f"{item['text']} ({names})"[:300]
