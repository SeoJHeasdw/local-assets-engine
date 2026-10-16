"""Review boards that separate shape from surface, and show what survives at game distance.

``review.png``: one row per mesh, all from the input image's three-quarter view. Columns are
the input image, clay (shape only), unlit base color (lighting baked into the texture shows up
here), and the textured mesh lit from the left and from the right (a shadow that does not move
with the light is painted in). A raw ``.npz`` mesh has no material and gets the clay view only.

``distance.png`` (``--distance``): the textured meshes at several screen heights, enlarged
without smoothing, so detail lost at play distance is visible.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from . import progress_line

VIEW = (1.4, -1.8, 1.1)
MODES = (("clay", "점토 · 형상"), ("unlit", "무조명 색"), ("left", "왼쪽 조명"), ("right", "오른쪽 조명"))


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


def _stage(height: int):
    """Camera framed on the scene's meshes, render settings and the default soft lights."""
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
    camera.location = center + Vector(VIEW).normalized() * span * 3
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
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


def review(meshes, output: Path, size: int, input_image: Path | None) -> None:
    import bpy

    total = len(meshes) * len(MODES)
    done = 0
    for label, path in meshes:
        textured = _load(label, path)
        scene, objects, center, span = _stage(size)
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
    _board(meshes, output, size, input_image)


def _board(meshes, output: Path, size: int, input_image: Path | None) -> None:
    from PIL import Image, ImageDraw

    head, side = 44, 120
    columns = (("input", "입력 이미지"),) + MODES
    board = Image.new("RGB", (side + size * len(columns), head + size * len(meshes)), (38, 40, 44))
    draw = ImageDraw.Draw(board)
    font = _font(21)
    for c, (_key, title) in enumerate(columns):
        draw.text((side + c * size + 12, 12), title, font=font, fill="white")
    for r, (label, _path) in enumerate(meshes):
        top = head + r * size
        draw.text((12, top + 12), label, font=font, fill="white")
        if r == 0 and input_image and input_image.is_file():
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
    """Textured meshes at several screen heights, each enlarged with nearest-neighbor to the largest."""
    from PIL import Image, ImageDraw

    largest = max(heights)
    total = len(meshes) * len(heights)
    for m, (label, path) in enumerate(meshes):
        _load(label, path)
        scene, _objects, center, span = _stage(largest)
        _lights(scene, center, span, "soft")
        for h, height in enumerate(heights):
            scene.render.resolution_x = scene.render.resolution_y = height
            _render(scene, output / f"{label}-{height}px.png")
            print(progress_line(m * len(heights) + h + 1, total, f"{label} · {height}px"), flush=True)
    head, side = 44, 120
    board = Image.new("RGB", (side + largest * len(heights), head + largest * len(meshes)), (38, 40, 44))
    draw = ImageDraw.Draw(board)
    font = _font(21)
    for c, height in enumerate(heights):
        draw.text((side + c * largest + 12, 12), f"화면 높이 {height}px", font=font, fill="white")
    for r, (label, _path) in enumerate(meshes):
        draw.text((12, head + r * largest + 12), label, font=font, fill="white")
        for c, height in enumerate(heights):
            with Image.open(output / f"{label}-{height}px.png") as picture:
                enlarged = picture.convert("RGBA").resize((largest, largest), Image.Resampling.NEAREST)
                board.paste(enlarged, (side + c * largest, head + r * largest), enlarged)
    board.save(output / "distance.png")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mesh", action="append", required=True, help="label=/absolute/mesh.glb or .npz")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="the image the mesh was made from")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--distance", help="comma-separated screen heights; renders distance.png instead")
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
