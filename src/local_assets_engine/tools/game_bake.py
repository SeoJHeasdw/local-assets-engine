"""Game-ready asset from a quality master: reduce to a triangle budget, then bake the master onto it.

Two measured stage entry points (Blender ``bpy``):

``reduce``  master GLB → low-poly GLB with fresh UVs, and the surface error against the master.
``bake``    master + low GLB → base color, normal (tangent, OpenGL +Y), roughness, metallic and
            ambient occlusion maps → GLB, FBX and PNGs, including a Unity URP metallic-smoothness map.
``compare`` puts two renders side by side for review.

The master keeps its pivot and scale; the low mesh shares them, so both sit in the same space.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from . import progress_line

# 복셀 표면 찍기에서 한 번에 만드는 점 수. 큰 삼각형도 나눠서 끝까지 찍는다.
POINTS_PER_BATCH = 4_000_000
EMIT_CHANNELS = (("basecolor", "Base Color", "sRGB"), ("roughness", "Roughness", "Non-Color"),
                 ("metallic", "Metallic", "Non-Color"))


def _load_joined(path: Path, name: str):
    import bpy

    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    added = [obj for obj in bpy.data.objects if obj not in before]
    meshes = [obj for obj in added if obj.type == "MESH"]
    if not meshes:
        raise SystemExit(f"{path.name}에 메시가 없습니다.")
    for obj in meshes:
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world
    for obj in [o for o in added if o.type != "MESH"]:
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    obj.name = name
    return obj


def _triangles(obj) -> int:
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def mesh_arrays(obj):
    import numpy as np

    mesh = obj.data
    mesh.calc_loop_triangles()
    coords = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", coords)
    triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    return coords.reshape(-1, 3), triangles.reshape(-1, 3)


def occupancy(coords, triangles, voxel: float, ground: float | None):
    """Solid voxels of everything the surface encloses, as a smoothed field.

    Generated masters are thin double shells with hidden inner walls. Filling what
    the outside cannot reach keeps one outer surface. With ``ground`` set, the plane
    under the object counts as a wall, so an opening that faces the ground is filled.
    """
    import numpy as np
    from scipy import ndimage

    low, high = coords.min(axis=0), coords.max(axis=0)
    a, b, c = coords[triangles[:, 0]], coords[triangles[:, 1]], coords[triangles[:, 2]]
    longest = np.maximum.reduce([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1),
                                 np.linalg.norm(a - c, axis=1)])
    steps = np.maximum(np.ceil(longest / (voxel * 0.5)).astype(int), 1)
    pad = 3
    origin = low - pad * voxel
    shape = np.ceil((high - low) / voxel).astype(int) + 2 * pad + 1
    shell = np.zeros(shape, dtype=bool)
    for n in np.unique(steps):
        # A barycentric lattice per triangle, finer than half a voxel however large the triangle.
        lattice = np.array([(i / n, j / n) for i in range(n + 1) for j in range(n + 1 - i)])
        chosen = np.flatnonzero(steps == n)
        batch = max(1, POINTS_PER_BATCH // len(lattice))
        for start in range(0, len(chosen), batch):
            part = chosen[start:start + batch]
            points = (a[part, None] * (1 - lattice.sum(axis=1))[None, :, None]
                      + b[part, None] * lattice[None, :, 0, None] + c[part, None] * lattice[None, :, 1, None])
            index = np.floor((points.reshape(-1, 3) - origin) / voxel).astype(int)
            shell[index[:, 0], index[:, 1], index[:, 2]] = True
    shell = ndimage.binary_dilation(shell, iterations=1)
    slab = np.zeros_like(shell)
    if ground is not None:
        slab[:, :, :int(np.floor((ground - origin[2]) / voxel))] = True
    solid = ndimage.binary_fill_holes(shell | slab) & ~slab
    # Undo the dilation on the bulk, but keep parts at most three voxels thick (fins, blades,
    # sheets) as they are: eroded to one voxel they would vanish in the smoothing. Thickness is
    # the deepest distance-to-outside nearby, so the edges and corners of thick parts stay eroded.
    depth = ndimage.maximum_filter(ndimage.distance_transform_edt(solid), size=5)
    solid = ndimage.binary_erosion(solid, iterations=1) | (solid & (depth <= 2))
    return ndimage.gaussian_filter(solid.astype(np.float32), sigma=0.8), origin


def outer_surface(field, origin, voxel: float, iso: float = 0.5):
    """Closed triangle surface of the smoothed hull field, facing outward.

    Lewiner marching cubes resolves the ambiguous cells that a naive dual method turns into
    non-manifold edges where two parts touch only along a voxel edge or corner.
    """
    import numpy as np
    from skimage.measure import marching_cubes

    if field.max() <= iso:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    vertices, faces, _, _ = marching_cubes(field, level=iso, allow_degenerate=False)
    # Field samples sit at voxel centers.
    vertices = vertices * voxel + origin + voxel * 0.5
    a, b, c = vertices[faces[:, 0]], vertices[faces[:, 1]], vertices[faces[:, 2]]
    if np.einsum("ij,ij->i", a, np.cross(b, c)).sum() < 0:
        faces = faces[:, ::-1]
    return vertices, faces.astype(np.int64)


def solid_lookup(field, origin, voxel: float):
    """Whether points lie inside the filled hull (the smoothed field above one half)."""
    import numpy as np

    def inside(points):
        index = np.floor((np.asarray(points) - origin) / voxel).astype(int)
        valid = np.all((index >= 0) & (index < field.shape), axis=1)
        result = np.zeros(len(index), dtype=bool)
        result[valid] = field[tuple(index[valid].T)] > 0.5
        return result

    return inside


def surface_error(high, low, span: float, visible_within: float, inside, samples: int = 60000) -> dict:
    """Nearest-surface distance between master and low, relative to the longest side.

    A master point far from the hull surface is *hidden* only when the hull's solid
    encloses it (an inner wall that was filled). Far points outside the solid are master
    shape the hull *lost*; they stay in the error and are counted on their own. The
    unfiltered master-to-low distances are kept as well.
    """
    import bpy
    import numpy as np
    from mathutils.bvhtree import BVHTree

    depsgraph = bpy.context.evaluated_depsgraph_get()
    high_tree, low_tree = BVHTree.FromObject(high, depsgraph), BVHTree.FromObject(low, depsgraph)
    hull_tree = BVHTree.FromObject(bpy.data.objects["hull"], depsgraph)
    rng = np.random.default_rng(0)

    def sample(obj):
        coords, _ = mesh_arrays(obj)
        return coords[rng.choice(len(coords), samples, replace=False)] if len(coords) > samples else coords

    def nearest(tree, points):
        return np.array([tree.find_nearest(tuple(p))[3] for p in points], dtype=np.float64)

    master_points = sample(high)
    far = nearest(hull_tree, master_points) > visible_within
    enclosed = inside(master_points)
    hidden, lost = far & enclosed, far & ~enclosed
    master_to_low = nearest(low_tree, master_points)
    both = np.concatenate((master_to_low[~hidden], nearest(high_tree, sample(low)))) / span
    every = master_to_low / span

    def summary(values):
        return {"meanRelative": round(float(values.mean()), 6),
                "p95Relative": round(float(np.percentile(values, 95)), 6),
                "maxRelative": round(float(values.max()), 6),
                "p95Meters": round(float(np.percentile(values, 95) * span), 5),
                "maxMeters": round(float(values.max() * span), 5)}

    return {**summary(both), "hiddenFraction": round(float(hidden.mean()), 4),
            "lostFraction": round(float(lost.mean()), 4),
            "lostMaxMeters": round(float(master_to_low[lost].max()), 5) if lost.any() else 0.0,
            "unfiltered": summary(every)}


def reduce(args) -> int:
    import bpy
    from .blender_post import topology_checks

    print(progress_line(0, 6, "원본 불러오는 중"), flush=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    high = _load_joined(args.master, "high")
    coords, triangles = mesh_arrays(high)
    span = float((coords.max(axis=0) - coords.min(axis=0)).max())
    voxel = span / args.voxels

    print(progress_line(1, 6, f"바깥 껍데기 만드는 중 (복셀 {args.voxels})"), flush=True)
    ground = float(coords[:, 2].min()) if args.seal_ground else None
    field, origin = occupancy(coords, triangles, voxel, ground)
    vertices, faces = outer_surface(field, origin, voxel)
    if not len(faces):
        raise SystemExit("바깥 껍데기가 비었습니다. 원본 메시가 닫혀 있는지 확인하세요.")
    hull_mesh = bpy.data.meshes.new("hull")
    hull_mesh.from_pydata(vertices.tolist(), [], faces.tolist())
    hull_mesh.validate(verbose=False)
    hull = bpy.data.objects.new("hull", hull_mesh)
    low = bpy.data.objects.new("low", hull_mesh.copy())
    for obj in (hull, low):
        bpy.context.scene.collection.objects.link(obj)
    hull_triangles = _triangles(low)

    print(progress_line(2, 6, f"면 {hull_triangles:,} → {args.target_faces:,}개로 줄이는 중"), flush=True)
    bpy.ops.object.select_all(action="DESELECT")
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    if hull_triangles > args.target_faces:
        modifier = low.modifiers.new("decimate", "DECIMATE")
        modifier.decimate_type = "COLLAPSE"
        modifier.use_collapse_triangulate = True
        modifier.ratio = args.target_faces / hull_triangles
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    triangulate = low.modifiers.new("triangulate", "TRIANGULATE")
    triangulate.quad_method = "BEAUTY"
    bpy.ops.object.modifier_apply(modifier=triangulate.name)
    low.data.shade_smooth()
    low.data.set_sharp_from_angle(angle=math.radians(args.sharp_angle))

    print(progress_line(3, 6, "UV 펴는 중"), flush=True)
    margin = args.margin_px / args.texture_size
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), margin_method="FRACTION", island_margin=margin,
                             correct_aspect=True, scale_to_bounds=False)
    bpy.ops.uv.pack_islands(rotate=True, margin_method="FRACTION", margin=margin, shape_method="CONCAVE")
    bpy.ops.object.mode_set(mode="OBJECT")

    print(progress_line(4, 6, "원본과의 형상 오차 재는 중"), flush=True)
    error = surface_error(high, low, span, visible_within=voxel * 2,
                          inside=solid_lookup(field, origin, voxel))
    low_coords, low_triangles = mesh_arrays(low)
    topology = topology_checks(low_coords, low_triangles)

    print(progress_line(5, 6, "저면 메시 저장 중"), flush=True)
    for obj in (high, hull):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    low.select_set(True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=str(args.output), export_format="GLB", use_selection=True,
                              export_yup=True, export_materials="NONE")
    stats = {"sourceTriangles": len(triangles), "targetFaces": args.target_faces, "triangles": len(low_triangles),
             "vertices": len(low_coords), "spanMeters": round(span, 5), "voxels": args.voxels,
             "voxelMeters": round(voxel, 6), "sealGround": args.seal_ground, "hullTriangles": hull_triangles,
             "surfaceError": error, "uvMarginPx": args.margin_px, "textureSize": args.texture_size,
             "topology": topology}
    args.stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(6, 6, f"면 {stats['triangles']:,}개 · 95% 오차 {error['p95Meters'] * 1000:.1f}mm"), flush=True)
    return 0


def _use_gpu(scene) -> str:
    import bpy

    scene.cycles.device = "CPU"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "METAL"
        prefs.get_devices()
        devices = [d for d in prefs.devices if d.type == "METAL"]
        for device in devices:
            device.use = True
        if devices:
            scene.cycles.device = "GPU"
            return "METAL"
    except (KeyError, TypeError, AttributeError):
        pass
    return "CPU"


def _principled(material):
    for node in material.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _surface_output(material):
    for node in material.node_tree.nodes:
        if node.type == "OUTPUT_MATERIAL" and node.is_active_output:
            return node
    return None


def _route_to_emission(materials, socket_name, constant=None):
    """Temporarily drive each material's surface with one Principled input (or a constant) as emission."""
    restore = []
    for material in materials:
        tree = material.node_tree
        principled, output = _principled(material), _surface_output(material)
        if output is None or (principled is None and constant is None):
            continue
        previous = output.inputs["Surface"].links[0].from_socket if output.inputs["Surface"].is_linked else None
        emission = tree.nodes.new("ShaderNodeEmission")
        emission.inputs["Strength"].default_value = 1.0
        source = principled.inputs[socket_name] if socket_name else None
        if constant is not None:
            emission.inputs["Color"].default_value = (constant, constant, constant, 1.0)
        elif source.is_linked:
            tree.links.new(source.links[0].from_socket, emission.inputs["Color"])
        else:
            value = source.default_value
            emission.inputs["Color"].default_value = (tuple(value)[:3] + (1.0,)) if hasattr(value, "__len__") \
                else (value, value, value, 1.0)
        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
        restore.append((tree, emission, output, previous))
    return restore


def _restore(restore):
    for tree, emission, output, previous in restore:
        tree.nodes.remove(emission)
        if previous is not None:
            tree.links.new(previous, output.inputs["Surface"])


def _pixels(image):
    import numpy as np

    data = np.empty(image.size[0] * image.size[1] * 4, dtype=np.float32)
    image.pixels.foreach_get(data)
    return data.reshape(image.size[1], image.size[0], 4)


def _save(image, path: Path) -> None:
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()


def check_exports(glb: Path, fbx: Path, triangles: int, dimensions) -> dict:
    """Load what was written back in a clean scene and compare it with the baked mesh."""
    import bpy
    from mathutils import Vector

    expected = sorted(round(float(value), 4) for value in dimensions)
    report = {"problems": []}
    for label, path, importer in (("glb", glb, bpy.ops.import_scene.gltf), ("fbx", fbx, bpy.ops.import_scene.fbx)):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        importer(filepath=str(path))
        meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        found = sum(_triangles(obj) for obj in meshes)
        corners = [obj.matrix_world @ Vector(c) for obj in meshes for c in obj.bound_box]
        size = sorted(round(max(c[i] for c in corners) - min(c[i] for c in corners), 4) for i in range(3)) if corners else []
        images = sorted({node.image.name for obj in meshes for slot in obj.material_slots if slot.material
                         and slot.material.node_tree for node in slot.material.node_tree.nodes
                         if node.type == "TEX_IMAGE" and node.image and node.image.size[0] > 0})
        report[label] = {"triangles": found, "dimensionsMeters": size, "textures": len(images)}
        if found != triangles:
            report["problems"].append(f"{label} 면 수 {found} ≠ {triangles}")
        if len(size) != 3 or any(abs(a - b) > max(1e-3, 0.002 * b) for a, b in zip(size, expected)):
            report["problems"].append(f"{label} 크기 {size} ≠ {expected}")
        if len(images) < 3:
            report["problems"].append(f"{label} 텍스처 {len(images)}장만 읽힘")
    return report


def _occlusion_group():
    import bpy

    group = bpy.data.node_groups.get("glTF Material Output")
    if group is None:
        group = bpy.data.node_groups.new("glTF Material Output", "ShaderNodeTree")
        group.interface.new_socket(name="Occlusion", in_out="INPUT", socket_type="NodeSocketFloat")
    return group


def bake(args) -> int:
    import bpy
    import numpy as np

    started = time.monotonic()
    print(progress_line(0, 8, "원본·저면 메시 불러오는 중"), flush=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    high = _load_joined(args.master, "high")
    low = _load_joined(args.low, f"SM_{args.name}")
    reduce_stats = json.loads(args.reduce_stats.read_text("utf-8"))
    span = reduce_stats["spanMeters"]
    error = reduce_stats["surfaceError"]["p95Meters"]
    # Rays start this far outside the low surface; most of the visible master lies within it.
    extrusion = min(max(error * 1.5, span * 0.002), span * 0.03)
    # They may travel further inward to reach recesses the hull filled over.
    reach = extrusion + span * 0.05

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    device = _use_gpu(scene)
    scene.cycles.samples = args.samples
    scene.render.bake.margin = max(4, args.texture_size // 128)
    scene.render.bake.margin_type = "EXTEND"

    material = bpy.data.materials.new(f"M_{args.name}")
    low.data.materials.clear()
    low.data.materials.append(material)
    tree = material.node_tree
    target = tree.nodes.new("ShaderNodeTexImage")
    tree.nodes.active = target

    bpy.ops.object.select_all(action="DESELECT")
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    high_materials = [slot.material for slot in high.material_slots if slot.material and slot.material.use_nodes]

    images, timings = {}, {}

    def run_bake(key, colorspace, bake_type, **options):
        image = bpy.data.images.new(f"T_{args.name}_{key}", args.texture_size, args.texture_size, alpha=False)
        image.colorspace_settings.name = colorspace
        target.image = image
        begin = time.monotonic()
        bpy.ops.object.bake(type=bake_type, use_selected_to_active=True, cage_extrusion=extrusion,
                            max_ray_distance=reach, target="IMAGE_TEXTURES", **options)
        timings[key] = round(time.monotonic() - begin, 2)
        images[key] = image

    print(progress_line(1, 9, f"노멀맵 굽는 중 ({device})"), flush=True)
    run_bake("normal", "Non-Color", "NORMAL", normal_space="TANGENT",
             normal_r="POS_X", normal_g="POS_Y", normal_b="POS_Z")

    def mask(selected_to_active):
        image = bpy.data.images.new("mask", args.texture_size, args.texture_size, alpha=False)
        image.colorspace_settings.name = "Non-Color"
        target.image = image
        margin, scene.render.bake.margin = scene.render.bake.margin, 0
        try:
            bpy.ops.object.bake(type="EMIT", use_selected_to_active=selected_to_active, cage_extrusion=extrusion,
                                max_ray_distance=reach, target="IMAGE_TEXTURES")
        finally:
            scene.render.bake.margin = margin
        values = _pixels(image)[..., 0] > 0.5
        bpy.data.images.remove(image)
        return values

    print(progress_line(2, 9, "굽기 광선이 품질본에 닿는지 확인 중"), flush=True)
    # UV islands of the low mesh alone, then the texels whose rays reach the master.
    high.select_set(False)
    restore = _route_to_emission([material], None, constant=1.0)
    try:
        covered = mask(False)
    finally:
        _restore(restore)
    high.select_set(True)
    restore = _route_to_emission(high_materials, None, constant=1.0)
    try:
        hit = mask(True)
    finally:
        _restore(restore)
    missed = covered & ~hit
    miss_fraction = float(missed.sum() / max(1, covered.sum()))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    from PIL import Image as PILImage
    picture = np.zeros(covered.shape + (3,), dtype=np.uint8)
    picture[covered] = (90, 90, 90)
    picture[missed] = (230, 40, 40)
    PILImage.fromarray(picture[::-1]).save(args.output_dir / "bake-misses.png")
    for step, (key, socket, colorspace) in enumerate(EMIT_CHANNELS, start=3):
        print(progress_line(step, 9, f"{key} 굽는 중"), flush=True)
        restore = _route_to_emission(high_materials, socket)
        try:
            run_bake(key, colorspace, "EMIT")
        finally:
            _restore(restore)
    if args.ao:
        print(progress_line(6, 9, "AO 굽는 중"), flush=True)
        # The low mesh must not shadow the master it samples.
        for flag in ("visible_diffuse", "visible_glossy", "visible_shadow", "visible_transmission"):
            setattr(low, flag, False)
        scene.cycles.samples = max(args.samples, 64)
        run_bake("occlusion", "Non-Color", "AO")
        scene.cycles.samples = args.samples

    print(progress_line(7, 9, "텍스처 저장 중"), flush=True)
    metallic = _pixels(images["metallic"])[..., 0]
    metallic_constant = float(metallic.max()) < 0.02
    roughness = _pixels(images["roughness"])[..., 0]
    files = {}
    for key, image in images.items():
        if key == "metallic" and metallic_constant:
            continue
        path = args.output_dir / f"T_{args.name}_{key}.png"
        _save(image, path)
        files[key] = path.name
    # Unity URP Lit: metallic in R, smoothness (1 - roughness) in A.
    unity = bpy.data.images.new(f"T_{args.name}_metallicsmoothness", args.texture_size, args.texture_size, alpha=True)
    unity.colorspace_settings.name = "Non-Color"
    packed = np.zeros((args.texture_size, args.texture_size, 4), dtype=np.float32)
    packed[..., 0] = 0.0 if metallic_constant else metallic
    packed[..., 3] = 1.0 - roughness
    unity.pixels.foreach_set(packed.ravel())
    unity_path = args.output_dir / f"T_{args.name}_metallicsmoothness.png"
    _save(unity, unity_path)
    files["unityMetallicSmoothness"] = unity_path.name

    print(progress_line(8, 9, "게임용 머티리얼과 파일 만들고 다시 불러와 확인하는 중"), flush=True)
    tree.nodes.remove(target)
    principled = _principled(material)
    output = _surface_output(material)

    def texture(key, x):
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = images[key]
        node.location = (x, 0)
        return node

    tree.links.new(texture("basecolor", -600).outputs["Color"], principled.inputs["Base Color"])
    rough = texture("roughness", -600)
    tree.links.new(rough.outputs["Color"], principled.inputs["Roughness"])
    if metallic_constant:
        principled.inputs["Metallic"].default_value = 0.0
    else:
        tree.links.new(texture("metallic", -600).outputs["Color"], principled.inputs["Metallic"])
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(texture("normal", -800).outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
    if "occlusion" in images:
        group = tree.nodes.new("ShaderNodeGroup")
        group.node_tree = _occlusion_group()
        tree.links.new(texture("occlusion", -600).outputs["Color"], group.inputs["Occlusion"])
    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    bpy.data.objects.remove(high, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    glb = args.output_dir / f"SM_{args.name}.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True, export_yup=True)
    fbx = args.output_dir / f"SM_{args.name}.fbx"
    # Unity: meters, Y up; textures stay next to the FBX as PNGs.
    bpy.ops.export_scene.fbx(filepath=str(fbx), use_selection=True, object_types={"MESH"},
                             apply_scale_options="FBX_SCALE_UNITS", axis_forward="-Z", axis_up="Y",
                             bake_space_transform=True, mesh_smooth_type="FACE", use_tspace=True,
                             path_mode="STRIP", embed_textures=False)
    exported = check_exports(glb, fbx, _triangles(low), low.dimensions)
    stats = {"device": device, "samples": args.samples, "textureSize": args.texture_size,
             "cageExtrusionMeters": round(extrusion, 5), "rayReachMeters": round(reach, 5),
             "metallicConstant": metallic_constant, "missFraction": round(miss_fraction, 5),
             "exportCheck": exported, "bakeSeconds": timings,
             "files": {**files, "glb": glb.name, "fbx": fbx.name, "misses": "bake-misses.png"},
             "bytes": {"glb": glb.stat().st_size, "fbx": fbx.stat().st_size},
             "seconds": round(time.monotonic() - started, 2)}
    args.stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(9, 9, f"완료 · {args.texture_size}px 맵 {len(files)}장 · 광선 누락 {miss_fraction * 100:.2f}%"), flush=True)
    return 0


def compare(args) -> int:
    from PIL import Image, ImageDraw, ImageFont

    panels = [(label, Image.open(path).convert("RGBA")) for label, path in
              (entry.split("=", 1) for entry in args.panel)]
    width, height = panels[0][1].size
    board = Image.new("RGB", (width * len(panels), height + 48), (38, 40, 44))
    draw = ImageDraw.Draw(board)
    font_path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    font = ImageFont.truetype(str(font_path), 23) if font_path.exists() else ImageFont.load_default()
    for i, (label, image) in enumerate(panels):
        board.paste(image, (i * width, 48), image)
        draw.text((i * width + 16, 12), label, font=font, fill="white")
    board.save(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("reduce")
    r.add_argument("--master", required=True, type=Path)
    r.add_argument("--output", required=True, type=Path)
    r.add_argument("--stats", required=True, type=Path)
    r.add_argument("--target-faces", required=True, type=int)
    r.add_argument("--texture-size", type=int, default=1024)
    r.add_argument("--margin-px", type=int, default=8)
    r.add_argument("--sharp-angle", type=float, default=60.0)
    r.add_argument("--voxels", type=int, default=192, help="voxels along the longest side for the outer hull")
    r.add_argument("--seal-ground", action=argparse.BooleanOptionalAction, default=True)
    b = sub.add_parser("bake")
    b.add_argument("--master", required=True, type=Path)
    b.add_argument("--low", required=True, type=Path)
    b.add_argument("--reduce-stats", required=True, type=Path)
    b.add_argument("--output-dir", required=True, type=Path)
    b.add_argument("--stats", required=True, type=Path)
    b.add_argument("--name", required=True)
    b.add_argument("--texture-size", type=int, default=1024)
    b.add_argument("--samples", type=int, default=16)
    b.add_argument("--ao", action=argparse.BooleanOptionalAction, default=True)
    c = sub.add_parser("compare")
    c.add_argument("--panel", action="append", required=True, help="label=/path.png")
    c.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    return {"reduce": reduce, "bake": bake, "compare": compare}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
