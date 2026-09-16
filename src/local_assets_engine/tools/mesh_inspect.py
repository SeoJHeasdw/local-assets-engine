"""Render the actual GLB from six directions, including top and underside."""

from __future__ import annotations

import argparse
from pathlib import Path
import re

from . import progress_line


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mesh", action="append", required=True, help="label=/absolute/mesh.glb")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, default=640)
    args = parser.parse_args(argv)
    import bpy
    from mathutils import Vector
    from PIL import Image, ImageDraw, ImageFont

    args.output.mkdir(parents=True, exist_ok=True)
    views = [(1.4, -1.8, 1.1), (-1.4, -1.8, 1.1), (-1.4, 1.8, 1.1),
             (1.4, 1.8, 1.1), (0, -0.05, 3), (0, -0.05, -3)]
    total = len(views) * len(args.mesh)
    for number, entry in enumerate(args.mesh):
        label, path = entry.split("=", 1)
        if not re.fullmatch(r"[a-z0-9-]+", label):
            raise ValueError("검수 이름은 영문·숫자·-만 가능합니다.")
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(Path(path).resolve()))
        objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        corners = [obj.matrix_world @ Vector(c) for obj in objects for c in obj.bound_box]
        if not corners:
            raise ValueError("검수할 메시가 없습니다.")
        low = Vector([min(p[i] for p in corners) for i in range(3)])
        high = Vector([max(p[i] for p in corners) for i in range(3)])
        center, span = (low + high) / 2, max(high - low)
        scene = bpy.context.scene
        scene.world = bpy.data.worlds.new("inspection-world")
        scene.world.use_nodes = True
        scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.45, 0.45, 0.45, 1)
        scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.8
        for i, (direction, power) in enumerate([((2, -3, 4), 450), ((-3, -1, 2), 250), ((0, 3, 3), 350)]):
            light = bpy.data.lights.new(f"softbox-{i}", "AREA")
            light.energy, light.shape, light.size = power * span * span, "DISK", 3 * span
            obj = bpy.data.objects.new(light.name, light)
            scene.collection.objects.link(obj)
            obj.location = center + Vector(direction) * span
            obj.rotation_euler = (center - obj.location).to_track_quat("-Z", "Y").to_euler()
        camera = bpy.data.objects.new("inspection-camera", bpy.data.cameras.new("inspection-camera"))
        scene.collection.objects.link(camera)
        scene.camera = camera
        camera.data.type, camera.data.ortho_scale = "ORTHO", span * 1.6
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = 32
        scene.render.resolution_x = scene.render.resolution_y = args.size
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = True
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGBA"
        scene.view_settings.view_transform = "AgX"
        for i, direction in enumerate(views):
            camera.location = center + Vector(direction).normalized() * span * 3
            camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
            scene.render.filepath = str(args.output / f"{label}-{i}.png")
            bpy.ops.render.render(write_still=True)
            print(progress_line(number * len(views) + i + 1, total, f"{label} · 각도 {i + 1}/6"), flush=True)
        board = Image.new("RGB", (args.size * 3, args.size * 2 + 48), (38, 40, 44))
        draw = ImageDraw.Draw(board)
        font_path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
        font = ImageFont.truetype(str(font_path), 23) if font_path.exists() else ImageFont.load_default()
        draw.text((16, 12), f"{label} · 앞/뒤/위/아래 실제 GLB 검수", font=font, fill="white")
        for i in range(len(views)):
            with Image.open(args.output / f"{label}-{i}.png") as image:
                board.paste(image, ((i % 3) * args.size, 48 + (i // 3) * args.size), image)
        board.save(args.output / f"{label}-contact.png")


if __name__ == "__main__":
    main()
