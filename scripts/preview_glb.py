"""Render a GLB to a PNG so a generated mesh can be judged by eye.

앱의 3D 뷰어 없이 결과를 비교할 때 쓴다. 텍스처 굽기 품질처럼 통계로는 드러나지
않는 문제는 이렇게 같은 각도로 렌더해 나란히 놓아야 보인다.

    .venv/bin/python scripts/preview_glb.py mesh/asset.glb preview.png [--size 480]
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("glb", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size", type=int, default=480)
    parser.add_argument("--samples", type=int, default=24)
    args = parser.parse_args(argv)

    import bpy
    from mathutils import Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.glb))
    corners = [obj.matrix_world @ Vector(corner)
               for obj in bpy.context.scene.objects if obj.type == "MESH"
               for corner in obj.bound_box]
    if not corners:
        raise SystemExit("GLB에 메시가 없습니다.")
    low = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    high = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    center, size = (low + high) / 2, max(high - low)

    camera = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    bpy.context.scene.collection.objects.link(camera)
    camera.location = center + Vector((1.2, -1.7, 0.95)).normalized() * size * 2.4
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera

    world = bpy.data.worlds.new("preview")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.1
    sun = bpy.data.lights.new("sun", "SUN")
    sun.energy = 4
    sun_object = bpy.data.objects.new("sun", sun)
    bpy.context.scene.collection.objects.link(sun_object)
    sun_object.rotation_euler = (math.radians(55), 0, math.radians(35))

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = args.samples
    preferences = bpy.context.preferences.addons["cycles"].preferences
    preferences.compute_device_type = "METAL"
    preferences.get_devices()
    for device in preferences.devices:
        device.use = True
    scene.cycles.device = "GPU"
    scene.render.resolution_x = scene.render.resolution_y = args.size
    scene.render.filepath = str(args.output)
    bpy.ops.render.render(write_still=True)
    print(f"저장: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
