"""Recipe sections, holiday/feast plans, and the merged shopping list."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hub import feasts as F


# -- pure logic --------------------------------------------------------------------------
def test_reading_ingredient_lines():
    p = F.parse_line("1 1/2 cups flour, sifted")
    assert (p["qty"], p["unit"], p["name"]) == (1.5, "cups", "flour")
    assert F.parse_line("½ cup butter, melted")["qty"] == 0.5
    assert F.parse_line("1½ cups milk")["qty"] == 1.5
    p = F.parse_line("1 (15 oz) can pumpkin puree")
    assert (p["qty"], p["unit"], p["name"]) == (1, "can", "pumpkin puree")
    assert F.parse_line("2-3 lb turkey breast")["qty"] == 3
    assert F.parse_line("1 T sugar")["unit"] == "T" and F.parse_line("1 t vanilla")["unit"] == "t"
    p = F.parse_line("Salt and pepper to taste")
    assert p["qty"] is None and p["unit"] is None
    assert F.parse_line("2 cups of broth")["name"] == "broth"
    assert F.food_key("Eggs") == F.food_key("egg") == "egg"
    assert F.food_key("tomatoes, diced") == "tomato"
    assert F.food_key("fresh cranberries") == "cranberries"


def test_mealie_parsed_ingredients_win():
    ing = {"quantity": 2, "unit": {"name": "cup"}, "food": {"name": "sugar", "label": {"name": "Baking"}},
           "display": "2 cups sugar", "note": ""}
    out = F.from_mealie(ing)
    assert (out["qty"], out["unit"], out["name"], out["label"]) == (2.0, "cup", "sugar", "Baking")
    raw = F.from_mealie({"quantity": 1, "food": None, "unit": None, "note": "3 cloves garlic", "display": "1 3 cloves garlic"})
    assert (raw["qty"], raw["unit"], raw["name"]) == (3, "cloves", "garlic")


def test_merge_scales_and_combines_units():
    pie = {"title": "Pumpkin pie", "factor": 2, "ingredients": [F.parse_line(x) for x in (
        "1 1/2 cups flour", "1/2 cup butter, melted", "1 (15 oz) can pumpkin puree", "2 large eggs", "1 tsp salt")]}
    rolls = {"title": "Rolls", "factor": 1, "ingredients": [F.parse_line(x) for x in (
        "3 cups flour", "2 tbsp butter", "Salt to taste", "1 egg", "1 cup warm water")]}
    out = F.build_list([pie, rolls])
    by = {i["food"]: i for i in out["items"]}
    assert by["flour"]["text"] == "6 cups flour" and by["flour"]["recipes"] == ["Pumpkin pie", "Rolls"]
    # 2 x ½ cup + 2 tbsp = 1⅛ cups
    assert by["butter"]["qty"] == pytest.approx(1.125) and by["butter"]["unit"] == "cup"
    assert by["egg"]["qty"] == 5
    assert by["pumpkin puree"]["text"] == "2 cans pumpkin puree"
    # "Salt to taste" folds into the measured salt line instead of its own line.
    assert by["salt"]["text"] == "2 tsp salt" and set(by["salt"]["recipes"]) == {"Pumpkin pie", "Rolls"}
    assert by["salt"]["staple"] and by["salt"]["aisle"] == "Spices"
    assert by["flour"]["aisle"] == "Baking" and by["egg"]["aisle"] == "Dairy & Eggs"
    assert by["pumpkin puree"]["aisle"] == "Canned & Jarred"
    # Aisles come in store order.
    names = [a["name"] for a in out["aisles"]]
    assert names == sorted(names, key=F.AISLES.index)
    lean = F.build_list([pie, rolls], skip_staples=True)
    assert "salt" not in {i["food"] for i in lean["items"]} and "water" not in {i["food"] for i in lean["items"]}
    assert lean["staples_skipped"] == 2
    assert F.push_text(by["flour"]) == "6 cups flour (Pumpkin pie, Rolls)"


def test_units_dont_mix_across_kinds_and_counts_round_up():
    a = {"title": "A", "factor": 1.2, "ingredients": [F.parse_line("2 onions"), F.parse_line("8 oz cream cheese")]}
    b = {"title": "B", "factor": 1, "ingredients": [F.parse_line("1 lb cream cheese"), F.parse_line("1 cup onion")]}
    out = F.build_list([a, b])
    texts = sorted(i["text"] for i in out["items"])
    assert "3 onions" in texts             # 2.4 onions -> 3
    assert "1 cup onion" in texts          # a cup of onion is not an onion count
    cheese = next(i for i in out["items"] if i["food"] == "cream cheese")
    assert cheese["unit"] == "lb" and cheese["qty"] == pytest.approx(1.6)  # 9.6 oz + 16 oz


def test_aisles():
    assert F.aisle_for("black pepper") == "Spices" and F.aisle_for("pepper") == "Spices"
    assert F.aisle_for("red bell pepper") == "Produce"
    assert F.aisle_for("garlic powder") == "Spices" and F.aisle_for("garlic") == "Produce"
    assert F.aisle_for("chicken broth") == "Canned & Jarred" and F.aisle_for("whole turkey") == "Meat & Seafood"
    assert F.aisle_for("frozen peas") == "Frozen" and F.aisle_for("dinner rolls") == "Bakery"
    assert F.aisle_for("white wine vinegar") == "Pantry" and F.aisle_for("dry white wine") == "Drinks"
    assert F.aisle_for("anything", "Produce") == "Produce"
    assert F.aisle_for("unobtainium") == "Other"


def test_sections_and_holiday_dates():
    assert F.sections_of(["Dessert", "Holiday"], ["Side", "Favorite"]) == ["Desserts", "Holiday", "Sides"]
    assert F.is_dinner([]) and F.is_dinner(["Mains", "Sides"]) and not F.is_dinner(["Desserts"])
    g = F.group_by_section([{"slug": "pie", "sections": ["Desserts"]}, {"slug": "x", "sections": []}])
    assert g["unsorted"][0]["slug"] == "x"
    assert next(s for s in g["sections"] if s["name"] == "Desserts")["count"] == 1
    assert F.thanksgiving(2026) == date(2026, 11, 26) and F.thanksgiving(2027) == date(2027, 11, 25)
    assert F.easter(2026) == date(2026, 4, 5) and F.easter(2027) == date(2027, 3, 28)
    assert F.template_date("easter", date(2026, 11, 14)) == date(2027, 3, 28)
    p = F.new_plan("thanksgiving", date(2026, 11, 14))
    assert p["name"] == "Thanksgiving 2026" and p["start_date"] == "2026-11-26"
    assert [c["key"] for c in p["courses"]] == ["appetizers", "mains", "sides", "breads", "desserts", "drinks"]
    assert all(c["items"] == [] for c in p["courses"])


# -- routes, with a fake Mealie ------------------------------------------------------------------
RECIPES = {
    "turkey": {"id": "a" * 32, "slug": "turkey", "title": "Roast turkey", "servings": "8",
               "sections": ["Mains"], "tags": [], "favorite": False, "last_made": None,
               "ingredients": ["12 lb turkey", "1/2 cup butter", "1 tbsp salt"]},
    "pie": {"id": "b" * 32, "slug": "pie", "title": "Pumpkin pie", "servings": "8 slices",
            "sections": ["Desserts"], "tags": ["Dessert"], "favorite": False, "last_made": None,
            "ingredients": ["1 (15 oz) can pumpkin puree", "2 eggs", "2 tbsp butter"]},
    "tacos": {"id": "c" * 32, "slug": "tacos", "title": "Tacos", "servings": None,
              "sections": [], "tags": [], "favorite": False, "last_made": None,
              "ingredients": ["1 lb ground beef", "8 tortillas", "1 tsp salt"]},
}


class FakeMealie:
    enabled = True
    last_error = None

    def __init__(self):
        self.recipes_db = {k: dict(v) for k, v in RECIPES.items()}
        self.planned: list[dict] = []
        self.updates: list = []
        self.lists_db = [{"id": "11111111-2222-3333-4444-555555555555", "name": "Groceries"}]
        self.pushed: dict[str, list[str]] = {}
        self.plan_adds: list = []

    async def close(self):
        pass

    def health(self):
        return {"id": "mealie", "ok": True}

    async def recipes(self, search="", tag=None, page=1, per_page=60):
        return {"items": list(self.recipes_db.values()), "total": len(self.recipes_db), "page": 1, "pages": 1}

    async def recipe(self, slug):
        return {**self.recipes_db[slug], "oven": None}

    async def recipe_parts(self, slug):
        r = self.recipes_db[slug]
        return {"id": r["id"], "slug": slug, "title": r["title"], "servings": r["servings"], "image_url": None,
                "ingredients": [F.parse_line(t) | {"label": None} for t in r["ingredients"]]}

    async def update(self, slug, fields):
        self.updates.append((slug, fields))
        if "sections" in fields:
            self.recipes_db[slug]["sections"] = fields["sections"]
        return slug

    async def plan(self, start, days=7):
        end = start + timedelta(days=days - 1)
        return [e for e in self.planned if start.isoformat() <= e["date"] <= end.isoformat()]

    async def plan_add(self, day, recipe_id, meal="side"):
        self.plan_adds.append((day.isoformat(), recipe_id, meal))

    async def lists(self):
        return list(self.lists_db)

    async def create_list(self, name):
        lid = "99999999-2222-3333-4444-555555555555"
        self.lists_db.append({"id": lid, "name": name})
        return lid

    async def add_items(self, list_id, texts):
        self.pushed.setdefault(list_id, []).extend(texts)
        return len(texts)


@pytest.fixture
def meals(hub):
    hub.config.hub.dry_run = False
    hub.mealie = FakeMealie()
    return hub


def test_sections_browse_and_assign(meals, people, home_client):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    r = home_client.get("/v1/meals/sections", headers=dylan.h)
    assert r.status_code == 200, r.text
    body = r.json()
    desserts = next(s for s in body["sections"] if s["name"] == "Desserts")
    assert [i["slug"] for i in desserts["items"]] == ["pie"]
    assert [i["slug"] for i in body["unsorted"]] == ["tacos"]
    assert home_client.put("/v1/meals/recipes/tacos/sections", json={"sections": ["main"]}, headers=theo.h).status_code == 403
    r = home_client.put("/v1/meals/recipes/tacos/sections", json={"sections": ["main", "Holiday"]}, headers=dylan.h)
    assert r.status_code == 200 and r.json()["sections"] == ["Mains", "Holiday"]
    assert meals.mealie.updates[-1] == ("tacos", {"sections": ["main", "Holiday"]})


def test_feast_plan_crud_timeline_and_mealplan(meals, people, home_client):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    r = home_client.get("/v1/meals/feasts", headers=dylan.h)
    assert r.status_code == 200 and {t["id"] for t in r.json()["templates"]} == {"thanksgiving", "christmas", "easter", "feast"}
    assert home_client.post("/v1/meals/feasts", json={"template": "thanksgiving"}, headers=theo.h).status_code == 403
    r = home_client.post("/v1/meals/feasts", json={"template": "thanksgiving", "guests": 16}, headers=dylan.h)
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    pid = plan["id"]
    assert plan["guests"] == 16 and plan["recipe_count"] == 0
    r = home_client.post(f"/v1/meals/feasts/{pid}/items", json={"course": "mains", "slug": "turkey"}, headers=dylan.h)
    assert r.status_code == 200, r.text
    r = home_client.post(f"/v1/meals/feasts/{pid}/items", json={"course": "desserts", "slug": "pie", "servings": 8},
                         headers=dylan.h)
    plan = r.json()["plan"]
    mains = next(c for c in plan["courses"] if c["key"] == "mains")
    assert mains["items"][0]["title"] == "Roast turkey" and mains["items"][0]["base_servings"] == 8
    assert mains["items"][0]["when"] == "morning"
    # The pie is made the day before, so it comes first in the timeline.
    assert [t["when"] for t in plan["timeline"]] == ["day_before", "morning"]
    # Edit: rename, move the pie to the day of.
    courses = plan["courses"]
    next(c for c in courses if c["key"] == "desserts")["items"][0]["when"] = "day_of"
    r = home_client.put(f"/v1/meals/feasts/{pid}", json={"name": "Friendsgiving", "courses": courses}, headers=dylan.h)
    assert r.status_code == 200 and r.json()["plan"]["name"] == "Friendsgiving"
    assert [t["when"] for t in r.json()["plan"]["timeline"]] == ["morning", "day_of"]
    # Onto Mealie's meal plan: turkey as dinner, pie as a side; twice adds nothing new.
    r = home_client.post(f"/v1/meals/feasts/{pid}/mealplan", headers=dylan.h)
    assert r.json()["added"] == 2
    assert {(m[1], m[2]) for m in meals.mealie.plan_adds} == {("a" * 32, "dinner"), ("b" * 32, "side")}
    meals.mealie.planned = [{"date": plan["start_date"], "recipe_id": rid, "slug": None}
                            for _, rid, _m in meals.mealie.plan_adds]
    assert home_client.post(f"/v1/meals/feasts/{pid}/mealplan", headers=dylan.h).json()["added"] == 0
    item_id = mains["items"][0]["id"]
    r = home_client.delete(f"/v1/meals/feasts/{pid}/items/{item_id}", headers=dylan.h)
    assert r.json()["plan"]["recipe_count"] == 1
    assert home_client.get("/v1/meals/feasts", headers=theo.h).status_code == 200
    assert home_client.delete(f"/v1/meals/feasts/{pid}", headers=dylan.h).status_code == 200
    assert home_client.get(f"/v1/meals/feasts/{pid}", headers=dylan.h).status_code == 404


def test_shopping_list_for_a_feast_plus_weeks(meals, people, home_client):
    dylan = people["phones"]["Dylan"]
    pid = home_client.post("/v1/meals/feasts", json={"template": "feast", "guests": 16}, headers=dylan.h).json()["plan"]["id"]
    home_client.post(f"/v1/meals/feasts/{pid}/items", json={"course": "mains", "slug": "turkey"}, headers=dylan.h)
    home_client.post(f"/v1/meals/feasts/{pid}/items", json={"course": "desserts", "slug": "pie", "servings": 8}, headers=dylan.h)
    today = meals.policy.now().date()
    meals.mealie.planned = [{"date": (today + timedelta(days=d)).isoformat(), "slug": "tacos", "recipe_id": "c" * 32}
                            for d in (1, 9)]
    # The feast alone: the turkey doubles (16 guests / serves 8), the pie stays at 8.
    r = home_client.post("/v1/meals/shopping/build", json={"feasts": [pid]}, headers=dylan.h)
    assert r.status_code == 200, r.text
    body = r.json()
    by = {i["food"]: i for i in body["items"]}
    assert by["turkey"]["text"] == "24 lb turkey"
    # 2 x ½ cup + 2 tbsp butter
    assert by["butter"]["qty"] == pytest.approx(1.125) and by["butter"]["recipes"] == ["Roast turkey", "Pumpkin pie"]
    assert {s["title"]: s["factor"] for s in body["sources"]} == {"Roast turkey": 2.0, "Pumpkin pie": 1.0}
    assert body["lists"][0]["name"] == "Groceries"
    # Feast + two weeks of the meal plan: tacos twice (no servings known: not scaled).
    end = (today + timedelta(days=13)).isoformat()
    r = home_client.post("/v1/meals/shopping/build", json={"feasts": [pid], "start": today.isoformat(), "end": end,
                                                          "skip_staples": True}, headers=dylan.h)
    body = r.json()
    by = {i["food"]: i for i in body["items"]}
    assert by["ground beef"]["text"] == "2 lb ground beef" and by["tortilla"]["qty"] == 16
    assert "salt" not in by and body["staples_skipped"] == 1
    assert "+" in body["label"]
    # Push to a new Mealie list, leaving out what we already have.
    r = home_client.post("/v1/meals/shopping/push", json={"feasts": [pid], "skip": [i["key"] for i in body["items"] if i["food"] == "egg"],
                                                         "list_name": "Feast shop"}, headers=dylan.h)
    assert r.status_code == 200, r.text
    pushed = meals.mealie.pushed[r.json()["list_id"]]
    assert meals.mealie.lists_db[-1]["name"] == "Feast shop"
    assert not any("egg" in t for t in pushed) and any(t.startswith("24 lb turkey") for t in pushed)
    # Into an existing list; an unknown list is refused.
    r = home_client.post("/v1/meals/shopping/push", json={"feasts": [pid], "list_id": "11111111-2222-3333-4444-555555555555"},
                         headers=dylan.h)
    assert r.status_code == 200 and meals.mealie.pushed["11111111-2222-3333-4444-555555555555"]
    r = home_client.post("/v1/meals/shopping/push", json={"feasts": [pid], "list_id": "nope"}, headers=dylan.h)
    assert r.status_code == 400
    assert home_client.post("/v1/meals/shopping/build", json={}, headers=dylan.h).status_code == 400
    r = home_client.post("/v1/meals/shopping/build", json={"start": today.isoformat(),
                                                          "end": (today + timedelta(days=80)).isoformat()}, headers=dylan.h)
    assert r.status_code == 400


# -- the Mealie adapter, against a fake Mealie API -------------------------------------------------
def test_mealie_adapter_sections_parts_and_bulk_items():
    import asyncio
    import json as _json

    import httpx

    from hub.adapters.family import Mealie
    from hub.config import ServiceConfig

    recipe = {"id": "r1", "slug": "pie", "name": "Pie", "recipeServings": 8, "extras": {},
              "tags": [{"id": "t1", "name": "Dessert", "slug": "dessert"}, {"id": "t2", "name": "Favorite", "slug": "favorite"}],
              "recipeCategory": [], "recipeInstructions": [],
              "recipeIngredient": [
                  {"quantity": 2, "unit": {"name": "cup"}, "food": {"name": "sugar", "label": {"name": "Baking"}},
                   "display": "2 cups sugar", "note": ""},
                  {"quantity": 1, "unit": None, "food": None, "note": "3 eggs", "display": "3 eggs", "originalText": None},
              ]}
    seen: list[tuple[str, str, object]] = []
    bulk_ok = {"v": False}

    def handler(req: httpx.Request) -> httpx.Response:
        body = _json.loads(req.content) if req.content else None
        seen.append((req.method, req.url.path, body))
        p = req.url.path
        if p == "/api/recipes/pie" and req.method == "GET":
            return httpx.Response(200, json=recipe)
        if p == "/api/recipes/pie" and req.method == "PUT":
            return httpx.Response(200, json={"slug": "pie"})
        if p == "/api/organizers/categories" and req.method == "GET":
            return httpx.Response(200, json={"items": [{"id": "c1", "name": "Desserts", "slug": "desserts"}]})
        if p == "/api/organizers/categories" and req.method == "POST":
            return httpx.Response(201, json={"id": "c2", "name": body["name"], "slug": "holiday"})
        if p == "/api/organizers/tags":
            return httpx.Response(200, json={"items": []})
        if p.endswith("/create-bulk"):
            return httpx.Response(200, json={}) if bulk_ok["v"] else httpx.Response(404, json={"detail": "nope"})
        if p == "/api/households/shopping/items":
            return httpx.Response(201, json={})
        return httpx.Response(404)

    m = Mealie(ServiceConfig(url="http://mealie.test"), vault=type("V", (), {"get": lambda self, k: "tok"})())
    m._client = httpx.AsyncClient(base_url="http://mealie.test", transport=httpx.MockTransport(handler))

    async def go():
        parts = await m.recipe_parts("pie")
        assert parts["servings"] == "8" and [i["name"] for i in parts["ingredients"]] == ["sugar", "eggs"]
        assert parts["ingredients"][0]["label"] == "Baking" and parts["ingredients"][1]["qty"] == 3
        await m.update("pie", {"sections": ["dessert", "Holiday"]})
        put = next(b for meth, path, b in seen if meth == "PUT")
        assert [c["name"] for c in put["recipeCategory"]] == ["Desserts", "Holiday"]
        assert [t["name"] for t in put["tags"]] == ["Favorite"]  # the old course tag goes; categories hold it now
        # Older Mealie without create-bulk: one at a time, in order.
        assert await m.add_items("L1", ["a", "b"]) == 2
        singles = [b for meth, path, b in seen if path == "/api/households/shopping/items"]
        assert [s["note"] for s in singles] == ["a", "b"] and singles[0]["shoppingListId"] == "L1"
        bulk_ok["v"] = True
        seen.clear()
        await m.add_items("L1", ["c"])
        assert [path for _, path, _ in seen] == ["/api/households/shopping/items/create-bulk"]

    asyncio.run(go())
