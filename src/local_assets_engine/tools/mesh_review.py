"""Review boards that separate shape from surface, and show what survives at game size.

``review.png``: one row per mesh, all from the same view. When the input image has a cutout,
the view is the one whose silhouette best overlaps it (searched over azimuth and elevation),
and the overlap is recorded as ``silhouetteIoU`` in ``review.json``. Otherwise a fixed
three-quarter view is used. Columns are the input image, clay (shape only), unlit base color
(lighting baked into the texture shows up here), and the textured mesh lit from the left and
from the right (a shadow that does not move with the light is painted in). A raw ``.npz`` mesh
has no material and gets the clay view only.

``distance.png`` (``--distance``): the textured meshes rendered so the object itself is a given
number of pixels tall on screen, enlarged without smoothing, so detail lost at play size shows.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from pathlib import Path

from . import progress_line

VIEW = (1.4, -1.8, 1.1)
MODES = (("clay", "점토 · 형상"), ("unlit", "무조명 색"), ("left", "왼쪽 조명"), ("right", "오른쪽 조명"))
# 실루엣 비교는 이 크기로 줄여서 한다. 무거운 메시는 이 면 수의 대역으로 줄여 렌더한다.
MASK_SIZE, MASK_RENDER, PROXY_FACES = 64, 96, 60_000
COARSE_AZIMUTHS = range(0, 360, 20)
COARSE_ELEVATIONS = (-10, 0, 15, 30, 45, 60)
# 물체 높이 위아래에 남기는 여백(비율).
DISTANCE_MARGIN = 0.1
DISPLAY_HEIGHT = 360


def view_direction(azimuth: float, elevation: float):
    """Unit vector from the object toward the camera. Azimuth 0 looks at the -Y side."""
    a, e = math.radians(azimuth), math.radians(elevation)
    return (math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e))


def _font(size):
    from PIL import ImageFont

    path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


def _load(label: str, path: Path):
    """Objects of one mesh in an empty scene. ``.npz`` holds ``vertices`` and ``faces`` only."""
    import bpy
    import numpy as np

    bpy.ops.wm.read_factory_settings(use_empty=True)
    if path.suffix == ".npz":
        data = np.load(path, allow_pickle=False)
        vertices = np.asarray(data["vertices"], dtype=np.float32)
        faces = np.asarray(data["faces"], dtype=np.int32)
        mesh = bpy.data.meshes.new(label)
        mesh.vertices.add(len(vertices))
        mesh.vertices.foreach_set("co", vertices.ravel())
        mesh.loops.add(faces.size)
        mesh.loops.foreach_set("vertex_index", faces.ravel())
        mesh.polygons.add(len(faces))
        mesh.polygons.foreach_set("loop_start", np.arange(0, faces.size, 3, dtype=np.int32))
        mesh.polygons.foreach_set("loop_total", np.full(len(faces), 3, dtype=np.int32))
        mesh.update()
        bpy.context.scene.collection.objects.link(bpy.data.objects.new(label, mesh))
        return False
    bpy.ops.import_scene.gltf(filepath=str(path.resolve()))
    return True


def _world_vertices(objects, limit=200_000):
    import numpy as np

    chunks = []
    for obj in objects:
        coords = np.empty(len(obj.data.vertices) * 3)
        obj.data.vertices.foreach_get("co", coords)
        coords = coords.reshape(-1, 3)
        matrix = np.array(obj.matrix_world)
        chunks.append(coords @ matrix[:3, :3].T + matrix[:3, 3])
    points = np.concatenate(chunks)
    if len(points) > limit:
        points = points[np.random.default_rng(0).choice(len(points), limit, replace=False)]
    return points


def _stage(height: int, direction=VIEW):
    """Camera aimed along ``direction``, render settings and an empty world."""
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    objects = [obj for obj in scene.objects if obj.type == "MESH"]
    corners = [obj.matrix_world @ Vector(c) for obj in objects for c in obj.bound_box]
    low = Vector([min(p[i] for p in corners) for i in range(3)])
    high = Vector([max(p[i] for p in corners) for i in range(3)])
    center, span = (low + high) / 2, max(high - low)
    camera = bpy.data.objects.new("camera", bpy.data.cameras.new("camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.data.type, camera.data.ortho_scale = "ORTHO", span * 1.6
    _aim(camera, center, span, direction)
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = 32
    scene.render.resolution_x = scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.world = bpy.data.worlds.new("review-world")
    scene.world.use_nodes = True
    return scene, objects, center, span


def _aim(camera, center, span, direction):
    from mathutils import Vector

    camera.location = center + Vector(direction).normalized() * span * 3
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()


def _lights(scene, center, span, rig):
    import bpy
    from mathutils import Vector

    for obj in [o for o in scene.objects if o.type == "LIGHT"]:
        bpy.data.objects.remove(obj, do_unlink=True)
    background = scene.world.node_tree.nodes["Background"]
    if rig == "soft":
        background.inputs[0].default_value, background.inputs[1].default_value = (0.45, 0.45, 0.45, 1), 0.8
        lights = [((2, -3, 4), 450), ((-3, -1, 2), 250), ((0, 3, 3), 350)]
    else:
        # One low key light from the side and little fill: baked shadows stand out against it.
        background.inputs[0].default_value, background.inputs[1].default_value = (0.45, 0.45, 0.45, 1), 0.12
        lights = [(((-3 if rig == "left" else 3), -1.2, 1.2), 900)]
    for i, (direction, power) in enumerate(lights):
        light = bpy.data.lights.new(f"light-{i}", "AREA")
        light.energy, light.shape, light.size = power * span * span, "DISK", 2 * span
        obj = bpy.data.objects.new(light.name, light)
        scene.collection.objects.link(obj)
        obj.location = center + Vector(direction) * span
        obj.rotation_euler = (center - obj.location).to_track_quat("-Z", "Y").to_euler()


def _render(scene, path: Path, *, standard=False):
    import bpy

    scene.view_settings.view_transform = "Standard" if standard else "AgX"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def silhouette(image, size: int = MASK_SIZE):
    """The alpha cutout cropped to its box, centered on a square and shrunk: a scale-free mask."""
    import numpy as np
    from PIL import Image

    alpha = np.asarray(image.convert("RGBA"))[:, :, 3] > 127
    ys, xs = np.nonzero(alpha)
    if not len(xs):
        return None
    crop = alpha[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    side = max(crop.shape)
    square = np.zeros((side, side), dtype=np.uint8)
    top, left = (side - crop.shape[0]) // 2, (side - crop.shape[1]) // 2
    square[top:top + crop.shape[0], left:left + crop.shape[1]] = crop * 255
    return np.asarray(Image.fromarray(square).resize((size, size), Image.Resampling.BOX)) > 127


def has_cutout(path: Path | None) -> bool:
    if not path or not Path(path).is_file():
        return False
    from PIL import Image

    from ..imaging import has_cutout as cutout

    with Image.open(path) as image:
        return cutout(image)


def align_to_input(path: Path, input_image: Path) -> dict | None:
    """The azimuth and elevation whose silhouette overlaps the input cutout best."""
    import bpy
    import numpy as np
    from PIL import Image

    with Image.open(input_image) as image:
        target = silhouette(image)
    if target is None:
        return None
    _load("align", path)
    objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    for obj in objects:
        faces = len(obj.data.polygons)
        if faces > PROXY_FACES:
            bpy.context.view_layer.objects.active = obj
            modifier = obj.modifiers.new("proxy", "DECIMATE")
            modifier.ratio = PROXY_FACES / faces
            bpy.ops.object.modifier_apply(modifier=modifier.name)
    scene, objects, center, span = _stage(MASK_RENDER)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "FLAT"
    scene.display.shading.color_type = "SINGLE"
    camera = scene.camera
    scratch = Path(tempfile.mkdtemp(prefix="review-align-"))
    cache = {}

    def score(azimuth, elevation):
        key = (azimuth % 360, max(-80, min(80, elevation)))
        if key not in cache:
            _aim(camera, center, span, view_direction(*key))
            frame = scratch / "mask.png"
            _render(scene, frame, standard=True)
            with Image.open(frame) as image:
                mask = silhouette(image)
            cache[key] = 0.0 if mask is None else float((mask & target).sum() / max(1, (mask | target).sum()))
        return cache[key], key

    best = max(score(a, e) for a in COARSE_AZIMUTHS for e in COARSE_ELEVATIONS)
    for step in (10, 5):
        _, (azimuth, elevation) = best
        best = max([best] + [score(azimuth + da, elevation + de) for da in (-step, 0, step) for de in (-step, 0, step)])
    iou, (azimuth, elevation) = best
    return {"azimuth": azimuth, "elevation": elevation, "silhouetteIoU": round(iou, 4)}


def _clay():
    import bpy

    material = bpy.data.materials.new("review-clay")
    principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    principled.inputs["Base Color"].default_value = (0.62, 0.62, 0.62, 1)
    principled.inputs["Roughness"].default_value = 0.65
    return material


def _unlit(objects):
    """Drive every material with its base color as emission, so lighting cannot change it."""
    for material in {slot.material for obj in objects for slot in obj.material_slots if slot.material}:
        tree = material.node_tree
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
        if principled is None or output is None:
            continue
        emission = tree.nodes.new("ShaderNodeEmission")
        source = principled.inputs["Base Color"]
        if source.is_linked:
            tree.links.new(source.links[0].from_socket, emission.inputs["Color"])
        else:
            emission.inputs["Color"].default_value = source.default_value
        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])


def review(meshes, output: Path, size: int, input_image: Path | None) -> dict:
    import bpy

    view = {"source": "fixed", **dict(zip(("x", "y", "z"), VIEW))}
    direction = VIEW
    if has_cutout(input_image):
        # 같은 좌표계의 메시들이므로 하나(품질본이 있으면 그것)로 시점을 찾는다.
        reference = dict(meshes).get("master") or meshes[0][1]
        found = align_to_input(reference, input_image)
        if found:
            view = {"source": "input", **found}
            direction = view_direction(found["azimuth"], found["elevation"])
    total = len(meshes) * len(MODES)
    done = 0
    for label, path in meshes:
        textured = _load(label, path)
        scene, objects, center, span = _stage(size, direction)
        clay = _clay()
        _lights(scene, center, span, "soft")
        bpy.context.view_layer.material_override = clay
        _render(scene, output / f"{label}-clay.png")
        bpy.context.view_layer.material_override = None
        done += 1
        print(progress_line(done, total, f"{label} · 점토"), flush=True)
        if not textured:
            done += len(MODES) - 1
            continue
        for rig in ("left", "right"):
            _lights(scene, center, span, rig)
            _render(scene, output / f"{label}-{rig}.png")
            done += 1
            print(progress_line(done, total, f"{label} · {rig} 조명"), flush=True)
        _unlit(objects)
        scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.0
        _render(scene, output / f"{label}-unlit.png", standard=True)
        done += 1
        print(progress_line(done, total, f"{label} · 무조명 색"), flush=True)
    report = {"view": view}
    (output / "review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    _board(meshes, output, size, input_image, view)
    return report


def _board(meshes, output: Path, size: int, input_image: Path | None, view: dict) -> None:
    from PIL import Image, ImageDraw

    head, side = 76, 120
    columns = (("input", "입력 이미지"),) + MODES
    board = Image.new("RGB", (side + size * len(columns), head + size * len(meshes)), (38, 40, 44))
    draw = ImageDraw.Draw(board)
    font = _font(21)
    if view["source"] == "input":
        caption = (f"시점: 입력 실루엣에 맞춤 (방위 {view['azimuth']}° · 고도 {view['elevation']}°, "
                   f"실루엣 일치 {view['silhouetteIoU']:.2f})")
    else:
        caption = "시점: 고정 3/4 (입력 이미지에 누끼가 없어 맞추지 않음)"
    draw.text((12, 10), caption, font=font, fill=(200, 200, 200))
    for c, (_key, title) in enumerate(columns):
        draw.text((side + c * size + 12, 44), title, font=font, fill="white")
    for r, (label, _path) in enumerate(meshes):
        top = head + r * size
        draw.text((12, top + 12), label, font=font, fill="white")
        if r == 0 and input_image and Path(input_image).is_file():
            with Image.open(input_image) as picture:
                picture = picture.convert("RGBA")
                picture.thumbnail((size, size))
                board.paste(picture, (side + (size - picture.width) // 2, top + (size - picture.height) // 2), picture)
        for c, (key, _title) in enumerate(MODES, start=1):
            cell = output / f"{label}-{key}.png"
            if cell.is_file():
                with Image.open(cell) as picture:
                    board.paste(picture, (side + c * size, top), picture.convert("RGBA"))
            else:
                draw.text((side + c * size + 12, top + size // 2), "재질 없음", font=font, fill=(150, 150, 150))
    board.save(output / "review.png")


def distance(meshes, output: Path, heights: list[int]) -> None:
    """Each mesh rendered so the object is exactly ``height`` pixels tall, enlarged without smoothing."""
    import bpy
    import numpy as np
    from PIL import Image, ImageDraw

    total = len(meshes) * len(heights)
    for m, (label, path) in enumerate(meshes):
        _load(label, path)
        scene, objects, center, span = _stage(max(heights))
        _lights(scene, center, span, "soft")
        camera = scene.camera
        bpy.context.view_layer.update()  # matrix_world follows the new rotation only after an update
        matrix = np.array(camera.matrix_world)
        points = _world_vertices(objects) - np.array(center)
        width = np.ptp(points @ matrix[:3, 0])
        height = np.ptp(points @ matrix[:3, 1])
        # The camera's vertical extent is the object's projected height plus the margin.
        camera.data.sensor_fit = "VERTICAL"
        camera.data.ortho_scale = height * (1 + DISTANCE_MARGIN)
        for h, pixels in enumerate(heights):
            scene.render.resolution_y = round(pixels * (1 + DISTANCE_MARGIN))
            scene.render.resolution_x = max(1, round(pixels * (1 + DISTANCE_MARGIN) * width / height))
            _render(scene, output / f"{label}-{pixels}px.png")
            print(progress_line(m * len(heights) + h + 1, total, f"{label} · 물체 높이 {pixels}px"), flush=True)
    cells = {}
    for label, _path in meshes:
        for pixels in heights:
            with Image.open(output / f"{label}-{pixels}px.png") as picture:
                factor = DISPLAY_HEIGHT / picture.height
                cells[label, pixels] = picture.convert("RGBA").resize(
                    (max(1, round(picture.width * factor)), DISPLAY_HEIGHT), Image.Resampling.NEAREST)
    column = max(cell.width for cell in cells.values())
    head, side = 44, 120
    board = Image.new("RGB", (side + column * len(heights), head + DISPLAY_HEIGHT * len(meshes)), (38, 40, 44))
    draw = ImageDraw.Draw(board)
    font = _font(21)
    for c, pixels in enumerate(heights):
        draw.text((side + c * column + 12, 12), f"화면 속 물체 높이 {pixels}px", font=font, fill="white")
    for r, (label, _path) in enumerate(meshes):
        draw.text((12, head + r * DISPLAY_HEIGHT + 12), label, font=font, fill="white")
        for c, pixels in enumerate(heights):
            cell = cells[label, pixels]
            board.paste(cell, (side + c * column + (column - cell.width) // 2, head + r * DISPLAY_HEIGHT), cell)
    board.save(output / "distance.png")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mesh", action="append", required=True, help="label=/absolute/mesh.glb or .npz")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="the image the mesh was made from")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--distance", help="comma-separated object heights in pixels; renders distance.png instead")
    args = parser.parse_args(argv)
    meshes = []
    for entry in args.mesh:
        label, path = entry.split("=", 1)
        if not re.fullmatch(r"[a-z0-9-]+", label):
            raise ValueError("검수 이름은 영문 소문자·숫자·-만 가능합니다.")
        meshes.append((label, Path(path)))
    args.output.mkdir(parents=True, exist_ok=True)
    if args.distance:
        distance(meshes, args.output, [int(value) for value in args.distance.split(",")])
    else:
        review(meshes, args.output, args.size, args.input)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
