"""``GET /api/recipes`` schemas must say what each recipe's ``prepare`` checks.

Every bound, choice, length limit, default and required key in a schema is
probed against the real ``prepare``: the edge values must be accepted and the
values just outside must be rejected. No model runs; ``prepare`` only validates.
"""

import copy
import io
import re

import pytest
from PIL import Image

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes import RECIPES, building_kit, describe_recipes, scan_mesh, terrain_texture
from local_assets_engine.recipes.schemas import SCHEMAS
from local_assets_engine.uploads import save_upload

ITEM = "[]"  # a path step meaning "each element of this array"
MISSING = object()
KEYWORDS = {"type", "properties", "required", "items", "enum", "const", "minimum", "maximum", "maxLength", "pattern",
            "minItems", "maxItems", "default", "description", "anyOf", "allOf", "if", "then", "additionalProperties"}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}

# 다른 입력에 따라 한계가 달라지는 값. 그 한계가 그대로 드러나는 조건에서 본다.
CONTEXT = {
    ("image", ("seed",)): [(("count",), 8)],
    ("text-to-3d", ("seed",)): [(("count",), 8)],
    ("text-to-video", ("width",)): [(("height",), 256)],
    ("text-to-video", ("height",)): [(("width",), 256)],
    ("image-to-video", ("width",)): [(("height",), 256)],
    ("image-to-video", ("height",)): [(("width",), 256)],
    ("selftest", ("failAt",)): [(("steps",), 10)],
    ("edit-asset", ("plan", "frame", "crop", "x")): [(("plan", "frame", "crop", "w"), 0.01)],
    ("edit-asset", ("plan", "frame", "crop", "y")): [(("plan", "frame", "crop", "h"), 0.01)],
}
# 레이어 종류를 바꾸면 그 종류의 필수 값이 따라 바뀐다. 모르는 종류를 거절하는지만 본다.
DISCRIMINATORS = {("edit-asset", ("plan", "layers", ITEM, "type"))}
# 검증이 모르는 값을 거절하지 않고 기본값으로 읽는 선택지. 스키마가 더 좁다.
COERCED_CHOICES = {("previz", ("shots", ITEM, "ease"))}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A store holding one image, one refinable mesh, one legacy mesh and one upload."""
    store = JobStore(tmp_path_factory.mktemp("schemas") / "jobs")

    def job(recipe, files, asset):
        record = store.create(recipe, {"subject": "fixture"}, "fixture")
        for name, content in files.items():
            path = store.job_dir(record["id"]) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                Image.new("RGBA", (8, 8), "red").save(path)
            else:
                path.write_bytes(content)
        store.update(record["id"], lambda value: value.update(state="done", assets=[{"review": "pending", **asset}]))
        return {"jobId": record["id"], "assetId": asset["id"]}

    image = job("image", {"image.png": None},
                {"id": "a01", "kind": "image", "role": "candidate", "file": "image.png", "meta": {}})
    mesh = job("image-to-3d", {"mesh/asset.glb": b"glb", "mesh/source.npz": b"npz", "mesh/input.png": None},
               {"id": "a01", "kind": "mesh", "role": "final", "file": "mesh/asset.glb", "meta": {
                   "seed": 7, "pipelineType": "512", "sizeMeters": 1, "sourceStateFile": "mesh/source.npz"}})
    legacy = job("text-to-3d", {"mesh/raw.glb": b"glb", "mesh/input.png": None,
                                "job.log": b"Baking PBR textures via KDTree (1024x1024)...\n"},
                 {"id": "a01", "kind": "mesh", "role": "final", "file": "mesh/raw.glb", "preview": "mesh/input.png",
                  "meta": {"rawFile": "mesh/raw.glb", "pipelineType": "512", "textureSize": 1024,
                           "targetFaces": 12, "sizeMeters": 2, "seed": 123}})
    picture = io.BytesIO()
    Image.new("RGBA", (8, 8), "blue").save(picture, format="PNG")
    upload = save_upload(store, picture.getvalue())["id"]

    color = {"id": "c1", "type": "color", "to": "#112233"}
    stamp = {"id": "s1", "type": "stamp", "uploadId": upload}
    bases = {
        "image": [{"subject": "red potion"}],
        "text-to-3d": [{"subject": "wooden crate"}],
        "image-to-3d": [{"source": image}],
        "repair-mesh": [{"source": legacy}],
        "refine-mesh": [{"source": mesh}],
        "game-asset": [{"source": mesh}],
        "building-kit": [{"pieces": ["wall", "floor"]}],
        "terrain-texture": [{"preset": "temperate-meadow", "roughness": [0.6, 0.85],
                             "patch": {"scan": "forrest_ground_01", "coverage": 0.3, "noiseMeters": 0.25,
                                       "roughness": [0.5, 0.9]},
                             "grade": {"hueShift": 8, "saturation": 0.9, "value": 1.0, "maxSaturation": 0.62}}],
        "scan-mesh": [{"scan": "boulder_01", "heightMeters": 1.7, "widthMeters": 3.0}],
        "import-image": [{"uploadId": upload}],
        "edit-asset": [
            {"source": image, "plan": {"frame": {"crop": {}}, "layers": [color, stamp]}},
            {"source": mesh, "plan": {"layers": [color, {**stamp, "position": [0, 0, 0], "normal": [0, 0, 1]}]}},
        ],
        "previz": [{
            "assets": [{"standin": "person", "size": [0.5, 0.5, 1.8], "position": [0, 0, 0]}],
            "shots": [{"id": "s1", "framing": {"targetOffset": [0, 0]}}],
        }],
        "text-to-video": [{"subject": "a fox", "camera": {}}],
        "image-to-video": [{"source": image, "camera": {}}],
        "selftest": [{}],
    }
    presets = load_presets()
    schemas = {recipe["name"]: recipe["paramsSchema"] for recipe in describe_recipes(RECIPES, presets)}
    # 건축 키트 재질은 기기에 받아 두는 파일이라, 스키마 검사에서는 받아 둔 것으로 친다.
    patch = pytest.MonkeyPatch()
    patch.setattr(building_kit, "missing", lambda _presets: [])
    patch.setattr(terrain_texture, "missing", lambda _presets, _scans=None: [])
    patch.setattr(scan_mesh, "missing", lambda _presets, _scans=None: [])
    yield store, presets, bases, schemas
    patch.undo()


def accepted(recipe_id, params, world):
    store, presets, _bases, _schemas = world
    try:
        RECIPES[recipe_id].prepare(copy.deepcopy(params), presets, store)
    except PresetError:
        return False
    return True


def edited(node, path, edit):
    """Copies of ``node`` with ``edit`` applied at ``path``, one per array element the path
    crosses. Nothing when the path does not exist in ``node``."""
    head, rest = path[0], path[1:]
    if head == ITEM:
        if isinstance(node, list):
            for index, item in enumerate(node):
                for changed in (edited(item, rest, edit) if rest else [edit(item)]):
                    yield [*node[:index], changed, *node[index + 1:]]
    elif isinstance(node, dict) and not rest:
        changed = {key: value for key, value in node.items() if key != head}
        value = edit(node.get(head, MISSING))
        yield changed if value is MISSING else {**changed, head: value}
    elif isinstance(node, dict) and head in node:
        for changed in edited(node[head], rest, edit):
            yield {**node, head: changed}


def leaves(schema, path=()):
    """(path, node) for each value a schema constrains by choice, range or length."""
    for alternative in schema.get("anyOf", []):
        yield from leaves(alternative, path)
    if "properties" in schema:
        for key, child in schema["properties"].items():
            yield from leaves(child, (*path, key))
    elif isinstance(schema.get("items"), dict):
        yield from leaves(schema["items"], (*path, ITEM))
    elif {"enum", "minimum", "maximum", "maxLength"} & set(schema):
        yield path, schema


def objects(schema, path=()):
    """(path, node) for each object node, to check its ``required`` keys."""
    for alternative in schema.get("anyOf", []):
        yield from objects(alternative, path)
    if "properties" in schema:
        yield path, schema
        for key, child in schema["properties"].items():
            yield from objects(child, (*path, key))
    elif isinstance(schema.get("items"), dict):
        yield from objects(schema["items"], (*path, ITEM))


def probes(node):
    good, bad = [], []
    if "enum" in node:
        good += node["enum"]
        bad.append("__not-a-choice__")
    if "minimum" in node:
        step = 1 if node["type"] == "integer" else max((node["maximum"] - node["minimum"]) * 1e-3, 1e-6)
        good += [node["minimum"], node["maximum"]]
        bad += [node["minimum"] - step, node["maximum"] + step]
    if "maxLength" in node:
        good.append("가" * node["maxLength"])
        bad.append("가" * (node["maxLength"] + 1))
    return good, bad


def with_context(recipe_id, path, base):
    for other, value in CONTEXT.get((recipe_id, path), []):
        base = next(edited(base, other, lambda _old: value), base)
    return base


def check_node(node, where):
    assert isinstance(node, dict) and set(node) <= KEYWORDS, where
    kinds = node.get("type", [])
    assert set([kinds] if isinstance(kinds, str) else kinds) <= TYPES, where
    if "pattern" in node:
        re.compile(node["pattern"])
    if "properties" in node:
        assert set(node.get("required", [])) <= set(node["properties"]), where
    if "default" in node:
        assert node["default"] in node.get("enum", [node["default"]]), where
        if "minimum" in node:
            assert node["minimum"] <= node["default"] <= node["maximum"], where
    children = [*node.get("properties", {}).items(), *enumerate(node.get("anyOf", [])), *enumerate(node.get("allOf", []))]
    children += [(key, node[key]) for key in ("items", "if", "then") if isinstance(node.get(key), dict)]
    for key, child in children:
        check_node(child, f"{where}.{key}")


def test_every_recipe_is_described_with_a_well_formed_schema(world):
    _store, presets, bases, schemas = world
    assert set(SCHEMAS) == set(RECIPES) == set(bases)
    described = {recipe["name"]: recipe for recipe in describe_recipes(RECIPES, presets)}
    for name, recipe in described.items():
        assert recipe["label"] == RECIPES[name].label and isinstance(recipe["needsReview"], bool)
        assert schemas[name]["type"] == "object"
        check_node(schemas[name], name)
    assert not described["selftest"]["needsReview"] and described["image"]["needsReview"]


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_schema_limits_are_the_limits_prepare_enforces(recipe_id, world):
    _store, _presets, bases, schemas = world
    for base in bases[recipe_id]:
        assert accepted(recipe_id, base, world), base
    for path, node in leaves(schemas[recipe_id]):
        good, bad = probes(node)
        if (recipe_id, path) in DISCRIMINATORS:
            good = []
        if (recipe_id, path) in COERCED_CHOICES:
            bad = []
        starts = [with_context(recipe_id, path, base) for base in bases[recipe_id]]
        for value in good:
            tried = [params for start in starts for params in edited(start, path, lambda _old: value)]
            assert tried, f"{recipe_id} {path}: 기준 입력에 이 값이 놓일 자리가 없다"
            refused = [params for params in tried if not accepted(recipe_id, params, world)]
            assert not refused, f"{recipe_id} {path}={value!r:.40}: 스키마는 받는데 검증이 거절한다"
        for value in bad:
            tried = [params for start in starts for params in edited(start, path, lambda _old: value)]
            assert tried, f"{recipe_id} {path}: 기준 입력에 이 값이 놓일 자리가 없다"
            assert not all(accepted(recipe_id, params, world) for params in tried), \
                f"{recipe_id} {path}={value!r:.40}: 스키마 밖인데 검증이 받는다"


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_schema_required_keys_and_defaults_match_prepare(recipe_id, world):
    store, presets, bases, schemas = world
    schema = schemas[recipe_id]
    for path, node in objects(schema):
        for key in node.get("required", []):
            tried = [params for base in bases[recipe_id] for params in edited(base, (*path, key), lambda _old: MISSING)]
            assert not any(accepted(recipe_id, params, world) for params in tried), \
                f"{recipe_id} {(*path, key)}: 필수라고 적었는데 없어도 받는다"
    alternatives = [choice["required"][0] for choice in schema.get("anyOf", []) if "required" in choice]
    if alternatives:
        for base in bases[recipe_id]:
            bare = {key: value for key, value in base.items() if key not in alternatives}
            assert not accepted(recipe_id, bare, world), f"{recipe_id}: {alternatives} 없이도 받는다"
    for path, node in leaves(schema):
        if "default" not in node or ITEM in path:
            continue
        for base in bases[recipe_id]:
            without = next(edited(base, path, lambda _old: MISSING), None)
            if without is None:
                continue
            value, _title = RECIPES[recipe_id].prepare(copy.deepcopy(without), presets, store)
            for key in path:
                value = value.get(key, MISSING) if isinstance(value, dict) else MISSING
                if value is MISSING:
                    break
            assert value is MISSING or value == node["default"], f"{recipe_id} {path}: 기본값이 다르다"


def test_probing_reaches_the_nested_inputs(world):
    """The walk above must actually visit nested plans, layers, placements and shots."""
    _store, _presets, _bases, schemas = world
    reached = {recipe_id: {path for path, _node in leaves(schema)} for recipe_id, schema in schemas.items()}
    assert ("plan", "layers", ITEM, "position", ITEM) in reached["edit-asset"]
    assert ("plan", "frame", "crop", "w") in reached["edit-asset"]
    assert ("assets", ITEM, "size", ITEM) in reached["previz"]
    assert ("shots", ITEM, "framing", "distanceEnd") in reached["previz"]
    assert ("camera", "move") in reached["text-to-video"]
