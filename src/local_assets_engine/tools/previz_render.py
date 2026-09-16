"""Render previz sketch shots from a blockout scene with Blender (bpy).

Places GLB assets on a ground plane, resolves each shot's relative framing
(distance in "fits the subject" units, azimuth, height) into camera values,
renders a cheap sketch pass per frame plus depth and outline, writes an
animatic, and reports the camera values it used. Those values are the previz
product; the images only exist so a person can approve them.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from . import progress_line

SENSOR_MM = 36.0


def _interpolate(start: float, end: float, fraction: float, ease: str) -> float:
    if ease == "inout":
        fraction = fraction * fraction * (3.0 - 2.0 * fraction)
    return start + (end - start) * fraction


def _pair(framing: dict[str, Any], key: str, default: float) -> tuple[float, float]:
    start = float(framing.get(key, default))
    end = framing.get(f"{key}End")
    return start, start if end is None else float(end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text("utf-8"))

    import bpy
    from mathutils import Matrix, Quaternion, Vector

    width, height = int(plan["width"]), int(plan["height"])
    fps = int(plan["fps"])
    aux = plan.get("aux", "keys")
    look = plan.get("look", {})
    shots = plan["shots"]
    animatic = bool(plan.get("animatic", True))

    def shot_frames(shot: dict[str, Any]) -> int:
        return max(1, round(float(shot["seconds"]) * fps))

    def shot_renders(shot: dict[str, Any]) -> int:
        """Render calls one shot costs: the animatic, colour keys, then aux passes."""
        frames = shot_frames(shot)
        keys = len({1, (frames + 1) // 2, frames})
        aux_frames = frames if aux == "all" else keys
        return (frames if animatic and frames > 1 else 0) + keys + (0 if aux == "none" else aux_frames * 2)

    total = sum(shot_renders(shot) for shot in shots)
    progress = {"done": 0}

    def step(count: int, detail: str) -> None:
        progress["done"] += count
        print(progress_line(progress["done"], total, detail), flush=True)

    print(progress_line(0, total, "장면 배치 중"), flush=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    def world_bounds(objects: list[Any]) -> tuple[Vector, Vector]:
        corners = [obj.matrix_world @ Vector(corner)
                   for obj in objects if obj.type == "MESH" for corner in obj.bound_box]
        if not corners:
            raise SystemExit("메시가 없는 장면은 렌더할 수 없습니다.")
        return (Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners))),
                Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners))))

    placed: dict[str, list[Any]] = {}
    for entry in plan["assets"]:
        before = set(scene.objects)
        bpy.ops.import_scene.gltf(filepath=str(entry["file"]))
        imported = [obj for obj in scene.objects if obj not in before]
        low, high = world_bounds(imported)
        pivot = bpy.data.objects.new(f"place-{entry['id']}", None)
        scene.collection.objects.link(pivot)
        pivot.location = Vector(entry.get("position", (0.0, 0.0, 0.0)))
        pivot.rotation_euler = (0.0, 0.0, math.radians(float(entry.get("yaw", 0.0))))
        scale = float(entry.get("scale", 1.0))
        pivot.scale = (scale, scale, scale)
        # GLB의 원점은 제작 도구마다 다르다. 바닥 중심을 배치 좌표에 맞춘다.
        recenter = Matrix.Translation(-Vector(((low.x + high.x) / 2, (low.y + high.y) / 2, low.z)))
        for obj in imported:
            if obj.parent is None:
                obj.parent = pivot
                obj.matrix_parent_inverse = recenter
        placed[str(entry["id"])] = imported
    bpy.context.view_layer.update()

    clay = bool(look.get("clay", True))
    for material in bpy.data.materials:
        # glTF는 단면 재질에 뒷면 컬링을 켠다. 생성된 메시는 면 방향이 고르지 않아
        # 그대로 두면 표면 대부분이 잘려 EEVEE에서 속이 보인다.
        material.use_backface_culling = False
        if not material.node_tree:
            continue
        for node in material.node_tree.nodes:
            if node.type != "BSDF_PRINCIPLED":
                continue
            # 생성된 GLB는 ORM 맵으로 금속성을 구동한다. 반사를 계산하지 않는 스케치
            # 렌더에서 금속면은 하늘만 비춰 유리처럼 보이므로 금속성을 끈다.
            for link in list(node.inputs["Metallic"].links):
                material.node_tree.links.remove(link)
            node.inputs["Metallic"].default_value = 0.0
    if clay:
        clay_material = bpy.data.materials.new("clay")
        clay_material.diffuse_color = (0.62, 0.60, 0.57, 1.0)
        clay_material.use_nodes = True
        clay_material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.62, 0.60, 0.57, 1.0)
        clay_material.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.6
        for objects in placed.values():
            for obj in objects:
                if obj.type == "MESH":
                    obj.data.materials.clear()
                    obj.data.materials.append(clay_material)

    scene_low, scene_high = world_bounds(list(scene.objects))
    ground_z = float(scene_low.z)
    span = max((scene_high - scene_low).length, 0.2)

    if plan.get("ground", True):
        extent = span * 12
        ground_mesh = bpy.data.meshes.new("ground")
        ground_mesh.from_pydata(
            [(-extent, -extent, 0.0), (extent, -extent, 0.0), (extent, extent, 0.0), (-extent, extent, 0.0)],
            [], [(0, 1, 2, 3)],
        )
        ground_material = bpy.data.materials.new("ground")
        tone = float(look.get("groundTone", 0.20))
        ground_material.diffuse_color = (tone, tone, tone, 1.0)
        ground_material.use_nodes = True
        surface = ground_material.node_tree.nodes["Principled BSDF"]
        surface.inputs["Roughness"].default_value = 0.9
        # 평평한 바닥에서는 카메라가 움직여도 화면이 거의 바뀌지 않는다.
        # 1m 격자가 있으면 이동·속도·크기가 애니매틱에서 읽힌다.
        checker = ground_material.node_tree.nodes.new("ShaderNodeTexChecker")
        checker.inputs["Color1"].default_value = (tone, tone, tone, 1.0)
        checker.inputs["Color2"].default_value = (tone * 1.5, tone * 1.5, tone * 1.55, 1.0)
        checker.inputs["Scale"].default_value = 1.0
        coordinates = ground_material.node_tree.nodes.new("ShaderNodeTexCoord")
        ground_material.node_tree.links.new(coordinates.outputs["Object"], checker.inputs["Vector"])
        ground_material.node_tree.links.new(checker.outputs["Color"], surface.inputs["Base Color"])
        ground_mesh.materials.append(ground_material)
        ground = bpy.data.objects.new("ground", ground_mesh)
        ground.location.z = ground_z
        scene.collection.objects.link(ground)

    key_azimuth = float(look.get("keyAzimuth", 35.0))
    key = bpy.data.lights.new("key", type="SUN")
    key.energy = float(look.get("keyEnergy", 4.0))
    key.angle = math.radians(float(look.get("keySoftness", 8.0)))
    key_object = bpy.data.objects.new("key", key)
    key_object.rotation_euler = (math.radians(float(look.get("keyElevation", 52.0))), 0.0, math.radians(key_azimuth))
    scene.collection.objects.link(key_object)
    fill = bpy.data.lights.new("fill", type="SUN")
    fill.energy = float(look.get("fillEnergy", 1.2))
    fill.angle = math.radians(45.0)
    fill_object = bpy.data.objects.new("fill", fill)
    fill_object.rotation_euler = (math.radians(65.0), 0.0, math.radians(key_azimuth + 150.0))
    scene.collection.objects.link(fill_object)

    world = bpy.data.worlds.new("previz")
    sky = look.get("sky", [0.40, 0.45, 0.53])
    background = world.node_tree.nodes["Background"]
    background.inputs[0].default_value = (*sky, 1.0)
    background.inputs[1].default_value = float(look.get("ambient", 0.5))
    scene.world = world

    camera_data = bpy.data.cameras.new("camera")
    camera_data.sensor_width = SENSOR_MM
    camera_data.sensor_fit = "HORIZONTAL"
    camera = bpy.data.objects.new("camera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera

    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.fps = fps
    scene.render.film_transparent = False
    renderer = plan["renderer"]
    samples = int(plan.get("samples", 16))
    if renderer == "workbench":
        scene.render.engine = "BLENDER_WORKBENCH"
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "SINGLE" if clay else "TEXTURE"
        shading.single_color = (0.62, 0.60, 0.57)
        shading.show_shadows = True
        shading.shadow_intensity = 0.45
        shading.show_cavity = True
        shading.studiolight_rotate_z = math.radians(key_azimuth)
        scene.display.render_aa = "8"
    elif renderer == "eevee":
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = samples
        scene.eevee.use_shadows = True
        scene.eevee.use_raytracing = False
    elif renderer == "cycles":
        scene.render.engine = "CYCLES"
        preferences = bpy.context.preferences.addons["cycles"].preferences
        preferences.compute_device_type = "METAL"
        preferences.get_devices()
        for device in preferences.devices:
            device.use = True
        scene.cycles.device = "GPU"
        scene.cycles.samples = samples
        scene.cycles.use_denoising = False
        scene.cycles.max_bounces = 2
    else:
        raise SystemExit(f"알 수 없는 렌더러입니다: {renderer}")

    bpy.context.view_layer.use_pass_z = True
    group = bpy.data.node_groups.new("previz", "CompositorNodeTree")
    group.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    scene.compositing_node_group = group
    layers = group.nodes.new("CompositorNodeRLayers")
    group_output = group.nodes.new("NodeGroupOutput")
    depth_range = group.nodes.new("ShaderNodeMapRange")
    depth_range.inputs["To Min"].default_value = 1.0
    depth_range.inputs["To Max"].default_value = 0.0
    depth_range.clamp = True
    group.links.new(layers.outputs["Depth"], depth_range.inputs["Value"])
    edges = group.nodes.new("CompositorNodeFilter")
    edges.inputs["Type"].default_value = "Sobel"
    group.links.new(depth_range.outputs["Result"], edges.inputs["Image"])
    # 깊이 기울기가 완만해 소벨 결과가 옅다. 임계를 세워 선으로 읽히게 만든다.
    contrast = group.nodes.new("ShaderNodeMapRange")
    contrast.inputs["From Min"].default_value = 0.0
    contrast.inputs["From Max"].default_value = float(look.get("lineThreshold", 0.04))
    contrast.clamp = True
    group.links.new(edges.outputs[0], contrast.inputs["Value"])
    lines = group.nodes.new("CompositorNodeInvert")
    group.links.new(contrast.outputs["Result"], lines.inputs["Color"])
    sockets = {
        "color": layers.outputs["Image"],
        "depth": depth_range.outputs["Result"],
        "line": lines.outputs[0],
    }

    def show(name: str) -> None:
        for link in list(group.links):
            if link.to_node == group_output:
                group.links.remove(link)
        group.links.new(sockets[name], group_output.inputs["Image"])

    def still(path: Path) -> None:
        scene.render.image_settings.media_type = "IMAGE"
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)

    def measure(low: Vector, high: Vector, base_z: float) -> dict[str, Any]:
        return {
            "center": [(low.x + high.x) / 2, (low.y + high.y) / 2, (low.z + high.z) / 2],
            "baseZ": base_z,
            "height": max(high.z - low.z, 0.05),
            "width": max(high.x - low.x, high.y - low.y, 0.05),
            "radius": max((high - low).length / 2, 0.05),
        }

    subjects: dict[str, dict[str, Any]] = {}
    for name, objects in placed.items():
        low, high = world_bounds(objects)
        subjects[name] = measure(low, high, float(low.z))
    subjects["scene"] = measure(scene_low, scene_high, ground_z)

    aspect = width / height
    records: list[dict[str, Any]] = []
    takes: list[tuple[str, list[tuple[Any, Any, float]]]] = []
    for shot in shots:
        started = time.monotonic()
        subject = subjects.get(str(shot.get("focus", "scene")), subjects["scene"])
        center = Vector(subject["center"])
        base_z, radius = float(subject["baseZ"]), float(subject["radius"])
        subject_height, subject_width = float(subject["height"]), float(subject["width"])
        framing = shot.get("framing", {})
        lens_start, lens_end = _pair(shot, "lens", 35.0)
        ease = str(shot.get("ease", "inout"))
        frames = shot_frames(shot)
        distance_start, distance_end = _pair(framing, "distance", 1.8)
        azimuth_start, azimuth_end = _pair(framing, "azimuth", -35.0)
        height_start, height_end = _pair(framing, "height", 0.6)
        target_start, target_end = _pair(framing, "targetHeight", 0.5)
        roll_start, roll_end = _pair(framing, "roll", 0.0)
        target_offset = framing.get("targetOffset", [0.0, 0.0])

        def fit_distance(lens: float) -> float:
            """Distance at which the subject just fills the frame, so 1.0 means tight.

            Framing is written in subject units, not meters, which keeps a preset
            usable for a teacup and a tower.
            """
            horizontal = math.atan(SENSOR_MM / (2 * lens))
            vertical = math.atan(SENSOR_MM / (2 * lens * aspect))
            return max(subject_width / 2 / math.tan(horizontal), subject_height / 2 / math.tan(vertical))

        camera.animation_data_clear()
        path: list[dict[str, Any]] = []
        keys: list[tuple[Any, Any, float]] = []
        for index in range(frames):
            fraction = 0.0 if frames == 1 else index / (frames - 1)
            lens = _interpolate(lens_start, lens_end, fraction, ease)
            distance = _interpolate(distance_start, distance_end, fraction, ease) * fit_distance(lens)
            azimuth = math.radians(_interpolate(azimuth_start, azimuth_end, fraction, ease))
            position = Vector((
                center.x + distance * math.sin(azimuth),
                center.y - distance * math.cos(azimuth),
                base_z + _interpolate(height_start, height_end, fraction, ease) * subject_height,
            ))
            target = Vector((
                center.x + float(target_offset[0]) * radius,
                center.y + float(target_offset[1]) * radius,
                base_z + _interpolate(target_start, target_end, fraction, ease) * subject_height,
            ))
            aim = (target - position).to_track_quat("-Z", "Y")
            roll = math.radians(_interpolate(roll_start, roll_end, fraction, ease))
            camera.location = position
            # 롤은 화면 축 기준이므로 시선 회전 뒤에 카메라 지역 Z로 돌린다. 앞 프레임과 이어지는
            # 오일러 값을 골라야 Blender에서 키를 고칠 때 카메라가 한 바퀴 돌지 않는다.
            turn = aim @ Quaternion((0.0, 0.0, 1.0), roll)
            rotation = turn.to_euler("XYZ", keys[-1][1]) if keys else turn.to_euler("XYZ")
            camera.rotation_euler = rotation
            camera_data.lens = lens
            keys.append((position.copy(), rotation.copy(), lens))
            frame = index + 1
            camera.keyframe_insert("location", frame=frame)
            camera.keyframe_insert("rotation_euler", frame=frame)
            camera_data.keyframe_insert("lens", frame=frame)
            path.append({
                "frame": frame,
                "seconds": round(index / fps, 3),
                "position": [round(v, 4) for v in position],
                "target": [round(v, 4) for v in target],
                "lens": round(lens, 2),
                "distance": round(distance, 4),
            })

        takes.append((str(shot["id"]), keys))

        # 한 샷 안에서 깊이 축척이 바뀌면 프레임끼리 비교할 수 없다. 샷 전체로 고정한다.
        reach = [(Vector(point["position"]) - center).length for point in path]
        near, far = max(0.05, min(reach) - radius), max(reach) + radius * 3
        depth_range.inputs["From Min"].default_value = near
        depth_range.inputs["From Max"].default_value = far

        shot_dir = args.out_dir / "shots" / str(shot["id"])
        shot_dir.mkdir(parents=True, exist_ok=True)
        scene.frame_start, scene.frame_end = 1, frames
        files: dict[str, Any] = {}

        show("color")
        if animatic and frames > 1:
            scene.render.image_settings.media_type = "VIDEO"
            scene.render.ffmpeg.format = "MPEG4"
            scene.render.ffmpeg.codec = "H264"
            scene.render.ffmpeg.constant_rate_factor = "HIGH"
            scene.render.filepath = str(shot_dir / "animatic")
            bpy.ops.render.render(animation=True)
            # Blender는 동영상 파일 이름에 프레임 범위를 붙인다.
            if written := sorted(shot_dir.glob("animatic*.mp4")):
                clip = shot_dir / "animatic.mp4"
                if written[0] != clip:
                    written[0].replace(clip)
                files["animatic"] = clip
            step(frames, f"{shot['id']} 애니매틱 {frames}프레임")

        key_frames = sorted({1, (frames + 1) // 2, frames})
        for name in ("color", "depth", "line"):
            if name != "color" and aux == "none":
                continue
            wanted = key_frames if name == "color" or aux == "keys" else list(range(1, frames + 1))
            show(name)
            files[name] = []
            for frame in wanted:
                scene.frame_set(frame)
                output = shot_dir / f"{name}-{frame:03d}.png"
                still(output)
                files[name].append(output)
            step(len(wanted), f"{shot['id']} {name} {len(wanted)}장")
        show("color")

        records.append({
            "id": shot["id"],
            "label": shot.get("label"),
            "purpose": shot.get("purpose"),
            "move": shot.get("move", "static"),
            "focus": str(shot.get("focus", "scene")),
            "order": shot.get("order", len(records) + 1),
            "seconds": round(float(shot["seconds"]), 3),
            "fps": fps,
            "frames": frames,
            "ease": ease,
            "lens": round(lens_start, 2),
            "lensEnd": round(lens_end, 2),
            "sensorWidth": SENSOR_MM,
            "start": {"position": path[0]["position"], "target": path[0]["target"], "lens": path[0]["lens"]},
            "end": {"position": path[-1]["position"], "target": path[-1]["target"], "lens": path[-1]["lens"]},
            "path": path,
            "framing": framing,
            "depthRange": [round(near, 4), round(far, 4)],
            "files": {
                "animatic": files["animatic"].relative_to(args.out_dir).as_posix() if files.get("animatic") else None,
                "key": (files["color"][len(files["color"]) // 2]).relative_to(args.out_dir).as_posix(),
                "color": [p.relative_to(args.out_dir).as_posix() for p in files.get("color", [])],
                "depth": [p.relative_to(args.out_dir).as_posix() for p in files.get("depth", [])],
                "line": [p.relative_to(args.out_dir).as_posix() for p in files.get("line", [])],
            },
            "renderSeconds": round(time.monotonic() - started, 2),
        })

    # 값으로 잡은 샷을 사람이 Blender에서 그대로 열어 손으로 고칠 수 있게 남긴다.
    # 컷마다 CAM_<샷> 카메라를 만들고 타임라인 마커에 묶어 한 줄의 컷 편집으로 둔다.
    print(progress_line(progress["done"], total, "Blender 장면 저장 중"), flush=True)
    bpy.data.objects.remove(camera, do_unlink=True)
    cursor = 1
    for shot_id, keys in takes:
        cut_data = bpy.data.cameras.new(f"CAM_{shot_id}")
        cut_data.sensor_width = SENSOR_MM
        cut_data.sensor_fit = "HORIZONTAL"
        # 기본 카메라 아이콘은 1m라 소품 크기 장면에서는 다른 카메라가 화면을 가린다.
        cut_data.display_size = max(0.05, span * 0.06)
        cut = bpy.data.objects.new(f"CAM_{shot_id}", cut_data)
        scene.collection.objects.link(cut)
        for index, (location, rotation, lens) in enumerate(keys):
            cut.location, cut.rotation_euler, cut_data.lens = location, rotation, lens
            cut.keyframe_insert("location", frame=cursor + index)
            cut.keyframe_insert("rotation_euler", frame=cursor + index)
            cut_data.keyframe_insert("lens", frame=cursor + index)
        scene.timeline_markers.new(f"CAM_{shot_id}", frame=cursor).camera = cut
        if cursor == 1:
            scene.camera = cut
        cursor += len(keys)
    scene.frame_start, scene.frame_end = 1, cursor - 1
    scene.frame_set(1)
    # 열자마자 카메라가 보는 화면과 컷 마커가 보이게 둔다. 재생하면 컷이 순서대로 바뀐다.
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.shading.type = "SOLID"
    show("color")
    scene.render.image_settings.media_type = "VIDEO"
    scene.render.filepath = "//render/previz"
    blend_path = args.out_dir / "scene.blend"
    # 텍스처를 파일 안에 넣어야 작업 폴더 밖으로 옮겨도 열린다.
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), copy=True, compress=True)

    # 컷을 따로 보면 연결과 리듬을 판단할 수 없다. 애니매틱을 컷 순서대로 이어 한 편으로 만든다.
    sequence = None
    clips = [record for record in records if record["files"]["animatic"]]
    if clips:
        print(progress_line(progress["done"], total, "컷을 한 편으로 잇는 중"), flush=True)
        edit = bpy.data.scenes.new("sequence")
        edit.render.resolution_x, edit.render.resolution_y = width, height
        edit.render.resolution_percentage = 100
        edit.render.fps = fps
        # 컷 영상은 이미 화면용 색으로 구워졌다. 한 번 더 톤 매핑하면 색이 바랜다.
        edit.view_settings.view_transform = "Standard"
        editor = edit.sequence_editor_create()
        cursor, cuts = 1, []
        for record in clips:
            strip = editor.strips.new_movie(record["id"], str(args.out_dir / record["files"]["animatic"]), 1, cursor)
            length = int(strip.frame_final_duration)
            cuts.append({"shot": record["id"], "start": cursor, "end": cursor + length - 1,
                         "at": round((cursor - 1) / fps, 3)})
            cursor += length
        edit.frame_start, edit.frame_end = 1, cursor - 1
        edit.render.image_settings.media_type = "VIDEO"
        edit.render.ffmpeg.format = "MPEG4"
        edit.render.ffmpeg.codec = "H264"
        edit.render.ffmpeg.constant_rate_factor = "HIGH"
        edit.render.filepath = str(args.out_dir / "sequence")
        bpy.ops.render.render(animation=True, scene=edit.name)
        if written := sorted(args.out_dir.glob("sequence*.mp4")):
            film = args.out_dir / "sequence.mp4"
            if written[0] != film:
                written[0].replace(film)
            sequence = {"file": film.name, "fps": fps, "frames": cursor - 1,
                        "seconds": round((cursor - 1) / fps, 3), "cuts": cuts}

    result = {
        "renderer": renderer,
        "resolution": [width, height],
        "fps": fps,
        "samples": samples,
        "aux": aux,
        "clay": clay,
        "sequence": sequence,
        "blend": blend_path.name if blend_path.exists() else None,
        "scene": {
            "boundsLow": [round(v, 4) for v in scene_low],
            "boundsHigh": [round(v, 4) for v in scene_high],
            "groundZ": round(ground_z, 4),
            "subjects": {name: {
                "center": [round(v, 4) for v in value["center"]],
                "baseZ": round(float(value["baseZ"]), 4),
                "height": round(float(value["height"]), 4),
                "width": round(float(value["width"]), 4),
                "radius": round(float(value["radius"]), 4),
            } for name, value in subjects.items()},
        },
        "shots": records,
    }
    args.result.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(total, total, f"샷 {len(records)}개"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
