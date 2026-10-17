"""One game tree in Blender: a procedural trunk and branches, leaf cards cut from real scanned leaves, three
LODs, a cross billboard and a stump.

Inputs are ``tree_textures``' output (bark tile, graded leaf maps with their elements, stump rings) and the
species spec. Steps:

1. Twigs. Small 3D twigs are assembled from the scanned leaves (each element placed by its base along thin
   stems) and rendered top-down with an orthographic camera into a 2048² leaf atlas: twig cards for LOD0,
   branch-cluster cards for LOD1, dense leaf-mass cards for LOD2, and the stump's rings in a spare slot. Every
   map is an emission render read back from EXR, so nothing is lit: albedo, normal (card space, OpenGL +Y),
   and R roughness · G translucency · B occlusion. Colors are un-premultiplied by coverage and spread past
   the cut-out so mipmaps do not darken the edges.
2. Skeleton. A trunk with a flared base, 0.4 m into the ground, and branches grown step by step toward the
   crown envelope (broadleaf: limbs from a fork or branches along the trunk; conifer: whorls).
3. Meshes per LOD: tubes with bark UVs (U wraps a whole number of tiles around, V runs along at the bark's
   real size) and leaf cards, both in one mesh with two materials. Card normals are moved to the outside of a
   smooth shell around the crown (the gradient of the blurred card density), so the crown takes light as
   one mass. Wind data is the vertex color (see ``wind``). Stump and billboard have zero vertex color.
4. Billboard: LOD0 rendered front and side into 2048×1024 — color under even light (a uniform white sky,
   diffuse and translucent only), normal in each plane's space — on two crossed quads sized to LOD0.
5. Export to FBX (LODs in one file named ``_LOD0``…, the billboard and the stump in their own files), read
   back to check names, triangles, size and vertex colors; a GLB of LOD0 for the app; review renders.

Blender axes: X right, Y front, Z up, the trunk's ground center at the origin. With the FBX settings below
Unity reads Blender (X, Y, Z) as (x, z, y): Y-up, 1 unit = 1 m, pivot at the base of the trunk on the ground.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import progress_line

ATLAS = 2048
# 잎 아틀라스 칸: (x, y, 폭, 높이) 픽셀, 왼쪽 위 기준. 카드는 칸 아래 가운데에서 위로 자란다.
SLOTS = {
    "twig0": (0, 0, 512, 512), "twig1": (512, 0, 512, 512), "twig2": (1024, 0, 512, 512), "twig3": (1536, 0, 512, 512),
    "twig4": (0, 512, 512, 512), "twig5": (512, 512, 512, 512), "twig6": (1536, 1536, 512, 512),
    "mass0": (1024, 512, 512, 512), "mass1": (1536, 512, 512, 512),
    "cluster0": (0, 1024, 512, 1024), "cluster1": (512, 1024, 512, 1024), "cluster2": (1024, 1024, 512, 1024),
    "ring": (1536, 1024, 512, 512),
}
TWIGS = [name for name in SLOTS if name.startswith("twig")]
CLUSTERS = [name for name in SLOTS if name.startswith("cluster")]
MASSES = [name for name in SLOTS if name.startswith("mass")]
UNDERGROUND = 0.4
BILLBOARD = 1024
BILLBOARD_NORMAL_BLUR = 1.5
STUMP_HEIGHT = 0.5
STUMP_SIDES = 20
# 카드 렌더 품질: 샘플(AO 잡음), 투명 통과 횟수(겹친 잎).
CARD_SAMPLES = 48
AMBIENT_SAMPLES = 128
TRANSPARENT_BOUNCES = 96
LOD_RATIOS = (1.0, 0.4, 0.15)
MASS_INSET = 0.18


def _v(*values):
    from mathutils import Vector

    return Vector(values)


def unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n > 1e-9, n, 1.0)


def perpendicular(d: np.ndarray) -> np.ndarray:
    helper = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    return unit(np.cross(d, helper))


def rotate(v: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
    """Rodrigues rotation of ``v`` about unit ``axis``."""
    axis = unit(axis)
    return v * math.cos(angle) + np.cross(axis, v) * math.sin(angle) + axis * (axis @ v) * (1 - math.cos(angle))


# ---------------------------------------------------------------- materials and pass renders

class Passes:
    """Every material's output can be switched between a lit look (``beauty``) and unlit emission passes.

    ``albedo`` (base color), ``normal`` (world normal × 0.5 + 0.5, after the normal map), ``data``
    (R roughness, G translucency, B occlusion: the AO node × the material's own occlusion map) and
    ``occlusion`` (the AO node alone). The cut-out is the same in every pass, so the render's alpha is the
    card's coverage.
    """

    def __init__(self):
        self.materials: list[dict] = []
        self.ao_nodes: list = []

    def set(self, which: str, ao_distance: float | None = None) -> None:
        for entry in self.materials:
            links = entry["tree"].links
            output = entry["output"]
            for link in list(output.inputs["Surface"].links):
                links.remove(link)
            if which in ("beauty", "ambient"):
                links.new(entry[which], output.inputs["Surface"])
            else:
                for link in list(entry["emission"].inputs["Color"].links):
                    links.remove(link)
                links.new(entry[which], entry["emission"].inputs["Color"])
                links.new(entry["unlit"], output.inputs["Surface"])
        if ao_distance is not None:
            for node in self.ao_nodes:
                node.inputs["Distance"].default_value = ao_distance


PASSES = Passes()


def image(path: Path, data: bool):
    import bpy

    loaded = bpy.data.images.load(str(path), check_existing=True)
    loaded.colorspace_settings.name = "Non-Color" if data else "sRGB"
    return loaded


def make_material(name: str, maps: dict, *, alpha_map=None, translucency: float = 0.0):
    """A pass-switchable material. ``maps``: ``color`` (sRGB), ``normal`` (OpenGL), and either a packed
    ``mask`` (G occlusion, B translucency, A smoothness) or ``rough`` and ``ao`` maps (constants when absent).
    The cut-out comes from ``alpha_map`` or, with ``alphaFromColor``, the color texture's alpha."""
    import bpy

    material = bpy.data.materials.new(name)
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    coords = nodes.new("ShaderNodeTexCoord")

    def texture(img, closest=False):
        node = nodes.new("ShaderNodeTexImage")
        node.image = img
        node.interpolation = "Closest" if closest else "Linear"
        links.new(coords.outputs["UV"], node.inputs["Vector"])
        return node

    color = texture(maps["color"])
    normal_tex = texture(maps["normal"])
    normal_map = nodes.new("ShaderNodeNormalMap")
    links.new(normal_tex.outputs["Color"], normal_map.inputs["Color"])
    if "mask" in maps:
        mask_tex = texture(maps["mask"])
        split = nodes.new("ShaderNodeSeparateColor")
        links.new(mask_tex.outputs["Color"], split.inputs["Color"])
        occlusion_socket = split.outputs["Green"]
        trans_socket = split.outputs["Blue"]
        smooth = nodes.new("ShaderNodeMath")
        smooth.operation = "SUBTRACT"
        smooth.inputs[0].default_value = 1.0
        links.new(mask_tex.outputs["Alpha"], smooth.inputs[1])
        rough_socket = smooth.outputs[0]
    else:
        def constant(value):
            node = nodes.new("ShaderNodeValue")
            node.outputs[0].default_value = value
            return node.outputs[0]

        rough_socket = texture(maps["rough"]).outputs["Color"] if "rough" in maps else constant(0.85)
        occlusion_socket = texture(maps["ao"]).outputs["Color"] if "ao" in maps else constant(1.0)
        trans_socket = constant(translucency)
    if alpha_map is not None:
        alpha_source = texture(alpha_map).outputs["Color"]
    elif "alphaFromColor" in maps:
        alpha_source = color.outputs["Alpha"]
    else:
        alpha_source = None
    if alpha_source is not None:
        cut = nodes.new("ShaderNodeMath")
        cut.operation = "GREATER_THAN"
        cut.inputs[1].default_value = 0.5
        links.new(alpha_source, cut.inputs[0])
        alpha_socket = cut.outputs[0]
    else:
        one = nodes.new("ShaderNodeValue")
        one.outputs[0].default_value = 1.0
        alpha_socket = one.outputs[0]

    # unlit passes
    emission = nodes.new("ShaderNodeEmission")
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    unlit = nodes.new("ShaderNodeMixShader")
    links.new(alpha_socket, unlit.inputs["Fac"])
    links.new(transparent.outputs[0], unlit.inputs[1])
    links.new(emission.outputs[0], unlit.inputs[2])
    encode = nodes.new("ShaderNodeVectorMath")
    encode.operation = "MULTIPLY_ADD"
    encode.inputs[1].default_value = (0.5, 0.5, 0.5)
    encode.inputs[2].default_value = (0.5, 0.5, 0.5)
    links.new(normal_map.outputs["Normal"], encode.inputs[0])
    ao = nodes.new("ShaderNodeAmbientOcclusion")
    ao.samples = 16
    ao.inputs["Distance"].default_value = 0.05
    links.new(normal_map.outputs["Normal"], ao.inputs["Normal"])
    occluded = nodes.new("ShaderNodeMath")
    occluded.operation = "MULTIPLY"
    links.new(ao.outputs["AO"], occluded.inputs[0])
    links.new(occlusion_socket, occluded.inputs[1])
    data = nodes.new("ShaderNodeCombineColor")
    links.new(rough_socket, data.inputs["Red"])
    links.new(trans_socket, data.inputs["Green"])
    links.new(occluded.outputs[0], data.inputs["Blue"])

    # lit look for review renders
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    links.new(color.outputs["Color"], principled.inputs["Base Color"])
    links.new(rough_socket, principled.inputs["Roughness"])
    links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
    translucent = nodes.new("ShaderNodeBsdfTranslucent")
    links.new(color.outputs["Color"], translucent.inputs["Color"])
    links.new(normal_map.outputs["Normal"], translucent.inputs["Normal"])
    thin = nodes.new("ShaderNodeMath")
    thin.operation = "MULTIPLY"
    thin.inputs[1].default_value = 0.3
    links.new(trans_socket, thin.inputs[0])
    lit = nodes.new("ShaderNodeMixShader")
    links.new(thin.outputs[0], lit.inputs["Fac"])
    links.new(principled.outputs[0], lit.inputs[1])
    links.new(translucent.outputs[0], lit.inputs[2])
    beauty = nodes.new("ShaderNodeMixShader")
    links.new(alpha_socket, beauty.inputs["Fac"])
    links.new(transparent.outputs[0], beauty.inputs[1])
    links.new(lit.outputs[0], beauty.inputs[2])
    links.new(beauty.outputs[0], output.inputs["Surface"])
    # 고른 빛(균일한 흰 하늘) 아래의 확산·투과만: 빌보드 색. 반사광 하이라이트가 섞이지 않는다.
    diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    links.new(color.outputs["Color"], diffuse.inputs["Color"])
    links.new(normal_map.outputs["Normal"], diffuse.inputs["Normal"])
    matte = nodes.new("ShaderNodeMixShader")
    links.new(thin.outputs[0], matte.inputs["Fac"])
    links.new(diffuse.outputs[0], matte.inputs[1])
    links.new(translucent.outputs[0], matte.inputs[2])
    ambient = nodes.new("ShaderNodeMixShader")
    links.new(alpha_socket, ambient.inputs["Fac"])
    links.new(transparent.outputs[0], ambient.inputs[1])
    links.new(matte.outputs[0], ambient.inputs[2])
    PASSES.materials.append({"tree": tree, "output": output, "emission": emission, "unlit": unlit.outputs[0],
                             "beauty": beauty.outputs[0], "ambient": ambient.outputs[0], "albedo": color.outputs["Color"],
                             "normal": encode.outputs[0], "data": data.outputs[0], "occlusion": ao.outputs["AO"]})
    PASSES.ao_nodes.append(ao)
    return material


def wind_material(channel: int):
    """Emission of one vertex-color channel, for the wind review."""
    import bpy

    material = bpy.data.materials.new(f"wind_{channel}")
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    attribute = nodes.new("ShaderNodeAttribute")
    attribute.attribute_name = "wind"
    split = nodes.new("ShaderNodeSeparateColor")
    links.new(attribute.outputs["Color"], split.inputs["Color"])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.02, 0.05, 0.25, 1)
    ramp.color_ramp.elements[1].color = (1.0, 0.85, 0.1, 1)
    source = split.outputs[["Red", "Green", "Blue"][channel]] if channel < 3 else attribute.outputs["Alpha"]
    links.new(source, ramp.inputs["Fac"])
    emission = nodes.new("ShaderNodeEmission")
    links.new(ramp.outputs["Color"], emission.inputs["Color"])
    links.new(emission.outputs[0], output.inputs["Surface"])
    return material


def setup_render(scene, samples: int):
    from .game_bake import _use_gpu

    scene.render.engine = "CYCLES"
    device = _use_gpu(scene)
    scene.cycles.samples = samples
    scene.cycles.use_denoising = False
    scene.cycles.transparent_max_bounces = TRANSPARENT_BOUNCES
    scene.cycles.max_bounces = 4
    scene.render.film_transparent = True
    scene.render.resolution_percentage = 100
    return device


def unlit_mode(scene) -> None:
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.render.film_transparent = True
    scene.world = None
    settings = scene.render.image_settings
    settings.file_format = "OPEN_EXR"
    settings.color_depth = "32"
    settings.color_mode = "RGBA"


def render_exr(scene, path: Path, width: int, height: int) -> np.ndarray:
    """Render to EXR and read it back as a top-down (rows from the top) float array, premultiplied."""
    import bpy

    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    loaded = bpy.data.images.load(str(path), check_existing=False)
    pixels = np.empty(width * height * 4, dtype=np.float32)
    loaded.pixels.foreach_get(pixels)
    bpy.data.images.remove(loaded)
    path.unlink(missing_ok=True)
    return np.flipud(pixels.reshape(height, width, 4))


def render_passes(scene, folder: Path, stem: str, width: int, height: int, ao_distance: float,
                  which_passes=("albedo", "normal", "data")) -> dict:
    """Passes of what is visible, un-premultiplied, with the coverage."""
    out = {}
    for which in which_passes:
        PASSES.set(which, ao_distance)
        rgba = render_exr(scene, folder / f"{stem}_{which}.exr", width, height)
        alpha = rgba[..., 3:4]
        out[which] = np.where(alpha > 1e-4, rgba[..., :3] / np.maximum(alpha, 1e-4), 0.0)
        out["alpha"] = rgba[..., 3]
    PASSES.set("beauty")
    return out


def render_ambient(scene, path: Path, size: int) -> np.ndarray:
    """What is visible lit only by a uniform white sky of strength 1, diffuse and translucent, un-premultiplied."""
    import bpy

    world = bpy.data.worlds.new("even")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (1, 1, 1, 1)
    background.inputs["Strength"].default_value = 1.0
    scene.world = world
    samples, denoise = scene.cycles.samples, scene.cycles.use_denoising
    scene.cycles.samples, scene.cycles.use_denoising = AMBIENT_SAMPLES, True
    PASSES.set("ambient")
    rgba = render_exr(scene, path, size, size)
    PASSES.set("beauty")
    scene.cycles.samples, scene.cycles.use_denoising = samples, denoise
    scene.world = None
    bpy.data.worlds.remove(world)
    alpha = rgba[..., 3:4]
    return np.where(alpha > 1e-4, rgba[..., :3] / np.maximum(alpha, 1e-4), 0.0)


def fill_outside(values: np.ndarray, alpha: np.ndarray, threshold: float = 0.5) -> np.ndarray:
    """Every pixel outside the cut-out takes the nearest inside pixel's value (mipmaps keep the edge color)."""
    from scipy.ndimage import distance_transform_edt

    inside = alpha >= threshold
    if not inside.any():
        return values
    _, (iy, ix) = distance_transform_edt(~inside, return_indices=True)
    return values[iy, ix]


def srgb(linear: np.ndarray) -> np.ndarray:
    c = np.clip(linear, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def byte(values: np.ndarray) -> np.ndarray:
    return (np.clip(values, 0, 1) * 255 + 0.5).astype(np.uint8)


# ---------------------------------------------------------------- simple mesh builder

class MeshData:
    """Vertices, faces, UVs, per-vertex normals and wind colors, gathered before one Blender mesh is made."""

    def __init__(self):
        self.verts: list[np.ndarray] = []
        self.normals: list[np.ndarray] = []
        self.uvs: list[np.ndarray] = []
        self.colors: list[np.ndarray] = []
        self.faces: list[np.ndarray] = []
        self.materials: list[np.ndarray] = []
        self.count = 0

    def add(self, verts, normals, uvs, colors, faces, material: int) -> None:
        verts = np.asarray(verts, dtype=np.float64)
        self.verts.append(verts)
        self.normals.append(unit(np.asarray(normals, dtype=np.float64)))
        self.uvs.append(np.asarray(uvs, dtype=np.float64))
        self.colors.append(np.asarray(colors, dtype=np.float64))
        faces = np.asarray(faces, dtype=np.int64) + self.count
        self.faces.append(faces)
        self.materials.append(np.full(len(faces), material, dtype=np.int64))
        self.count += len(verts)

    def extend(self, other: "MeshData") -> None:
        base = self.count
        for i in range(len(other.verts)):
            self.verts.append(other.verts[i])
            self.normals.append(other.normals[i])
            self.uvs.append(other.uvs[i])
            self.colors.append(other.colors[i])
            self.faces.append(other.faces[i] + base)
            self.materials.append(other.materials[i])
        self.count += other.count

    def chunk_faces(self, i: int) -> np.ndarray:
        """Faces of chunk ``i`` numbered from that chunk's first vertex."""
        return self.faces[i] - sum(len(v) for v in self.verts[:i])

    def triangles(self) -> int:
        return int(sum(len(f) for f in self.faces))

    def build(self, name: str, materials: list):
        """A Blender mesh object. Faces are triangles; corner UVs; per-vertex custom normals and colors."""
        import bpy

        verts = np.concatenate(self.verts) if self.verts else np.zeros((0, 3))
        faces = np.concatenate(self.faces) if self.faces else np.zeros((0, 3), dtype=np.int64)
        mesh = bpy.data.meshes.new(name)
        mesh.vertices.add(len(verts))
        mesh.vertices.foreach_set("co", verts.astype(np.float32).ravel())
        mesh.loops.add(len(faces) * 3)
        mesh.loops.foreach_set("vertex_index", faces.astype(np.int32).ravel())
        mesh.polygons.add(len(faces))
        mesh.polygons.foreach_set("loop_start", (np.arange(len(faces)) * 3).astype(np.int32))
        mesh.polygons.foreach_set("loop_total", np.full(len(faces), 3, dtype=np.int32))
        mesh.polygons.foreach_set("material_index", np.concatenate(self.materials).astype(np.int32))
        mesh.polygons.foreach_set("use_smooth", np.ones(len(faces), dtype=bool))
        uv_layer = mesh.uv_layers.new(name="UVMap")
        uvs = np.concatenate(self.uvs)
        uv_layer.data.foreach_set("uv", uvs[faces.ravel()].astype(np.float32).ravel())
        colors = np.concatenate(self.colors)
        attribute = mesh.color_attributes.new(name="wind", type="FLOAT_COLOR", domain="POINT")
        attribute.data.foreach_set("color", colors.astype(np.float32).ravel())
        mesh.color_attributes.active_color = attribute
        mesh.color_attributes.render_color_index = 0
        mesh.update()
        mesh.validate(clean_customdata=False)
        mesh.normals_split_custom_set_from_vertices([tuple(n) for n in np.concatenate(self.normals)])
        for material in materials:
            mesh.materials.append(material)
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        return obj


def tube(data: MeshData, points: np.ndarray, radii: np.ndarray, sides: int, *, tile=(1.0, 1.0), wind=None,
         material: int = 0, cap_tip: bool = True, v_start: float = 0.0) -> None:
    """A tapered tube along ``points`` (parallel-transported rings). U wraps a whole number of bark tiles
    around (``tile`` = real width, height of the bark in meters), V follows the length; the tip closes to a
    point. ``wind(i)`` gives the RGBA of ring ``i`` (the tip takes the last ring's)."""
    points = np.asarray(points, dtype=np.float64)
    count = len(points)
    tangents = np.zeros_like(points)
    tangents[1:-1] = points[2:] - points[:-2]
    tangents[0] = points[1] - points[0]
    tangents[-1] = points[-1] - points[-2]
    tangents = unit(tangents)
    normal = perpendicular(tangents[0])
    frames = []
    for i in range(count):
        if i:
            axis = np.cross(tangents[i - 1], tangents[i])
            if np.linalg.norm(axis) > 1e-9:
                angle = math.atan2(np.linalg.norm(axis), tangents[i - 1] @ tangents[i])
                normal = rotate(normal, axis, angle)
        normal = unit(normal - tangents[i] * (normal @ tangents[i]))
        frames.append((normal, np.cross(tangents[i], normal)))
    lengths = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))])
    mean_radius = float(np.mean(radii[: max(1, count // 2)]))
    repeats = max(1, round(2 * math.pi * mean_radius / tile[0]))
    verts, normals, uvs, colors, faces = [], [], [], [], []
    # 굵기가 줄어드는 만큼 노멀이 길이 방향으로 조금 눕는다.
    slope = np.gradient(radii, lengths) if count > 1 and lengths[-1] > 0 else np.zeros(count)
    for i in range(count):
        n0, b0 = frames[i]
        color = wind(i) if wind else (0, 0, 0, 0)
        for j in range(sides + 1):
            angle = 2 * math.pi * j / sides
            radial = n0 * math.cos(angle) + b0 * math.sin(angle)
            verts.append(points[i] + radial * radii[i])
            normals.append(unit(radial - tangents[i] * slope[i]))
            uvs.append((repeats * j / sides, v_start + lengths[i] / tile[1]))
            colors.append(color)
    for i in range(count - 1):
        for j in range(sides):
            a, b = i * (sides + 1) + j, i * (sides + 1) + j + 1
            c, d = a + sides + 1, b + sides + 1
            faces += [(a, b, d), (a, d, c)]
    if cap_tip:
        tip = len(verts)
        last = (count - 1) * (sides + 1)
        for j in range(sides):
            verts.append(points[-1] + tangents[-1] * radii[-1])
            normals.append(tangents[-1])
            uvs.append((repeats * (j + 0.5) / sides, v_start + (lengths[-1] + radii[-1]) / tile[1]))
            colors.append(wind(count - 1) if wind else (0, 0, 0, 0))
            faces.append((last + j, last + j + 1, tip + j))
    data.add(verts, normals, uvs, colors, faces, material)


# ---------------------------------------------------------------- twigs for the atlas

@dataclass
class Element:
    base: np.ndarray       # source pixel, y down
    axis: np.ndarray       # unit, base → tip, image coords
    length_px: float
    width_px: float
    offset_px: float
    size: int

    def uv(self, along: float, across: float) -> tuple[float, float]:
        """UV of a point ``along`` (0 base … 1 tip) and ``across`` (−0.5 … 0.5 of the width) on the element."""
        perp = np.array([-self.axis[1], self.axis[0]])
        p = self.base + self.axis * (along * self.length_px) + perp * (self.offset_px + across * self.width_px)
        return float(p[0] / self.size), float(1 - p[1] / self.size)


def load_elements(path: Path) -> list[Element]:
    record = json.loads(path.read_text("utf-8"))
    out = []
    for e in record["elements"]:
        base, tip = np.array(e["base"]), np.array(e["tip"])
        axis = unit(tip - base)
        out.append(Element(base, axis, e["lengthPx"], e["widthPx"], e["offsetPx"], record["size"]))
    return out


class TwigMaker:
    """Small 3D twigs from scanned leaves, for rendering into atlas cards."""

    def __init__(self, spec: dict, elements: list[Element], rng: np.random.Generator):
        self.spec, self.elements, self.rng = spec, elements, rng
        median = float(np.median([e.length_px for e in elements]))
        self.meters_per_px = float(spec["leafLength"]) / median

    def leaf(self, data: MeshData, base: np.ndarray, direction: np.ndarray, up: np.ndarray, scale: float,
             roll: float = 0.0, element: Element | None = None) -> None:
        """One scanned leaf as a slightly folded, curled quad grid from ``base`` along ``direction``."""
        e = element or self.elements[self.rng.integers(len(self.elements))]
        length = e.length_px * self.meters_per_px * scale
        width = e.width_px * self.meters_per_px * scale * 1.04
        y = unit(direction)
        x = unit(np.cross(y, up))
        z = np.cross(x, y)
        if roll:
            x, z = rotate(x, y, roll), rotate(z, y, roll)
        fold = self.spec.get("fold", 0.12)
        curl = self.spec.get("curl", 0.1)
        verts, uvs, normals = [], [], []
        rows, cols = (4, 3)
        for r in range(rows):
            t = r / (rows - 1)
            for c in range(cols):
                s = c / (cols - 1) - 0.5
                lift = fold * abs(s) * width * 2 - curl * t * t * length
                verts.append(base + y * (t * length) + x * (s * width) + z * lift)
                uvs.append(e.uv(t, s))
                normals.append(z)
        faces = []
        for r in range(rows - 1):
            for c in range(cols - 1):
                a = r * cols + c
                faces += [(a, a + 1, a + cols + 1), (a, a + cols + 1, a + cols)]
        data.add(verts, normals, uvs, np.zeros((len(verts), 4)), faces, 1)

    def stem(self, data: MeshData, points: np.ndarray, r0: float, r1: float) -> None:
        radii = np.linspace(r0, r1, len(points))
        tube(data, points, radii, 5, tile=(0.08, 0.3), material=0)

    @staticmethod
    def curve(start: np.ndarray, direction: np.ndarray, length: float, bend: np.ndarray, steps: int = 6) -> np.ndarray:
        points = [start]
        d = unit(direction)
        step = length / steps
        for _ in range(steps):
            d = unit(d + bend / steps)
            points.append(points[-1] + d * step)
        return np.array(points)

    def shoot(self, data: MeshData, start, direction, length, radius, *, style: str, up=np.array([0, 0, 1.0])):
        """A stem with leaves along it; returns its points."""
        rng = self.rng
        bend = np.array([rng.normal(0, 0.25), rng.normal(0, 0.25), rng.normal(0.05, 0.08)])
        points = self.curve(np.asarray(start, float), direction, length, bend)
        self.stem(data, points, radius, radius * 0.35)
        spec = self.spec
        leaf_len = float(spec["leafLength"])
        segment = np.linalg.norm(np.diff(points, axis=0), axis=1)
        cumulative = np.concatenate([[0], np.cumsum(segment)])

        def at(distance):
            i = min(np.searchsorted(cumulative, distance) - 1, len(points) - 2)
            i = max(i, 0)
            t = (distance - cumulative[i]) / max(segment[i], 1e-9)
            return points[i] + (points[i + 1] - points[i]) * t, unit(points[i + 1] - points[i])

        spacing = leaf_len * spec.get("leafSpacing", 0.4)
        start_at = length * spec.get("leafStart", 0.25)
        side = 1 if rng.random() < 0.5 else -1
        d = start_at
        while d < length * 0.97:
            p, t = at(d)
            side = -side
            angle = math.radians(rng.uniform(*spec.get("leafAngle", (40, 70)))) * side
            direction_leaf = rotate(t, up, angle)
            direction_leaf = unit(direction_leaf + up * rng.normal(0.05, 0.12))
            self.leaf(data, p, direction_leaf, up, rng.uniform(0.75, 1.1), roll=rng.normal(0, 0.35) * side)
            d += spacing * rng.uniform(0.7, 1.3)
        tip, t = at(length)
        rosette = spec.get("rosette", 0)
        for k in range(rosette):
            angle = math.radians((k - (rosette - 1) / 2) * (110 / max(rosette - 1, 1)) + rng.normal(0, 8))
            self.leaf(data, tip, rotate(t, up, angle), up, rng.uniform(0.85, 1.15), roll=rng.normal(0, 0.3))
        return points

    def twig(self, data: MeshData, card: float, style: str) -> None:
        """A twig whose base is the origin, growing along +Y inside a ``card`` square."""
        rng = self.rng
        up = np.array([0.0, 0.0, 1.0])
        if style == "conifer":
            self.spray(data, card)
            return
        main_len = card * rng.uniform(0.74, 0.84)
        main = self.shoot(data, [0, 0, 0], [rng.normal(0, 0.1), 1, 0], main_len, card * 0.009, style=style)
        shoots = self.spec.get("sideShoots", (2, 3))
        count = int(rng.integers(shoots[0], shoots[1] + 1))
        for k in range(count):
            t = 0.15 + 0.6 * (k + rng.uniform(0.2, 0.8)) / count
            i = min(int(t * (len(main) - 1)), len(main) - 2)
            d = unit(main[i + 1] - main[i])
            side = 1 if k % 2 == 0 else -1
            direction = rotate(d, up, math.radians(rng.uniform(25, 55)) * side)
            length = main_len * rng.uniform(0.4, 0.6) * (1 - 0.35 * t)
            self.shoot(data, main[i], direction, length, card * 0.006, style=style)

    def spray(self, data: MeshData, card: float) -> None:
        """A flat, twice-divided conifer spray: an axis and side axes, scanned sprigs on both sides of each."""
        rng = self.rng
        up = np.array([0.0, 0.0, 1.0])
        length = card * rng.uniform(0.74, 0.84)
        points = self.sprig_axis(data, np.zeros(3), np.array([rng.normal(0, 0.06), 1, 0]), length, card * 0.008, 1.0)
        shoots = self.spec.get("sideShoots", (3, 4))
        count = int(rng.integers(shoots[0], shoots[1] + 1))
        for k in range(count):
            t = 0.12 + 0.62 * (k + rng.uniform(0.2, 0.8)) / count
            i = min(int(t * (len(points) - 1)), len(points) - 2)
            d = unit(points[i + 1] - points[i])
            side = 1 if k % 2 == 0 else -1
            direction = rotate(d, up, math.radians(rng.uniform(40, 62)) * side)
            self.sprig_axis(data, points[i], direction, length * rng.uniform(0.38, 0.52) * (1 - 0.4 * t), card * 0.005,
                            0.85 * (1 - 0.3 * t))

    def sprig_axis(self, data: MeshData, start, direction, length: float, radius: float, scale: float) -> np.ndarray:
        rng = self.rng
        up = np.array([0.0, 0.0, 1.0])
        points = self.curve(np.asarray(start, float), direction, length, np.array([rng.normal(0, 0.12), 0, 0.02]))
        self.stem(data, points, radius, radius * 0.4)
        cumulative = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))])
        spacing = float(self.spec.get("sprigSpacing", 0.06))
        d = spacing * 0.5
        while d < length * 0.9:
            i = min(np.searchsorted(cumulative, d), len(points) - 1)
            p, t = points[i], unit(points[min(i + 1, len(points) - 1)] - points[max(i - 1, 0)])
            fade = scale * (1 - 0.4 * d / length)
            for s in (1, -1):
                angle = math.radians(rng.uniform(40, 65)) * s
                self.leaf(data, p, unit(rotate(t, up, angle) + up * rng.normal(0.02, 0.06)), up,
                          fade * rng.uniform(0.85, 1.1), roll=rng.normal(0, 0.25))
            d += spacing * rng.uniform(0.75, 1.25)
        self.leaf(data, points[-1] - unit(points[-1] - points[-2]) * 0.02, unit(points[-1] - points[-2]), up,
                  scale * rng.uniform(0.9, 1.1))
        return points


def assemble(twig_data: list[MeshData], placements: list[tuple[np.ndarray, np.ndarray, float, float]]) -> MeshData:
    """Copies of twigs placed at (base, direction in XY, scale, tilt): a branch cluster or a leaf mass."""
    out = MeshData()
    for k, (base, direction, scale, tilt) in enumerate(placements):
        source = twig_data[k % len(twig_data)]
        angle = math.atan2(direction[0], direction[1])
        cos, sin = math.cos(-angle), math.sin(-angle)
        rotation = np.array([[cos, -sin, 0], [sin, cos, 0], [0, 0, 1]])
        tilt_matrix = np.array([[1, 0, 0], [0, math.cos(tilt), -math.sin(tilt)], [0, math.sin(tilt), math.cos(tilt)]])
        m = rotation @ tilt_matrix
        for i in range(len(source.verts)):
            out.add(source.verts[i] * scale @ m.T + base, source.normals[i] @ m.T, source.uvs[i], source.colors[i],
                    source.chunk_faces(i), int(source.materials[i][0]))
    return out


def fit(data: MeshData, width: float, height: float, margin: float = 0.03) -> float:
    """Scale content about the origin (the card's base) so it lies inside the card; never enlarges. Content
    below the base (a leaf turned back) may use the bottom margin only."""
    verts = np.concatenate(data.verts)
    span_x = np.abs(verts[:, 0]).max() * 2
    low, high = verts[:, 1].min(), verts[:, 1].max()
    scale = min(1.0, (width * (1 - 2 * margin)) / max(span_x, 1e-6), (height * (1 - margin)) / max(high, 1e-6))
    if low < 0:
        scale = min(scale, height * margin * 0.8 / -low)
    for i in range(len(data.verts)):
        data.verts[i] = data.verts[i] * scale
    return scale


def card_sizes(spec: dict) -> dict:
    s = float(spec["cardMeters"])
    return {"twig": (s, s), "cluster": (s * 1.25, s * 2.5), "mass": (s * 3.2, s * 3.2)}


def build_cards(spec: dict, textures: Path, output: Path, scene, materials) -> dict:
    """Twig, cluster and mass cards rendered into the leaf atlas, plus the rings; returns the atlas maps."""
    import bpy

    rng = np.random.default_rng(spec["seed"])
    elements = load_elements(textures / "leaves" / "elements.json")
    maker = TwigMaker(spec["twig"], elements, rng)
    sizes = card_sizes(spec)
    style = spec["form"] if spec["form"] == "conifer" else spec["twig"].get("style", "broadleaf")
    atlas = {"albedo": np.zeros((ATLAS, ATLAS, 3)), "normal": np.zeros((ATLAS, ATLAS, 3)),
             "data": np.zeros((ATLAS, ATLAS, 3)), "alpha": np.zeros((ATLAS, ATLAS))}
    camera_data = bpy.data.cameras.new("card")
    camera_data.type = "ORTHO"
    camera = bpy.data.objects.new("card", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    twig_data = []
    for name in TWIGS:
        data = MeshData()
        maker.twig(data, sizes["twig"][0], style)
        data.fit_scale = fit(data, *sizes["twig"])
        twig_data.append(data)
    contents = {name: twig_data[i] for i, name in enumerate(TWIGS)}
    for k, name in enumerate(CLUSTERS):
        width, height = sizes["cluster"]
        placements = []
        count = int(spec["twig"].get("clusterTwigs", 7))
        for i in range(count):
            t = (i + 0.5) / count
            side = 1 if i % 2 == 0 else -1
            angle = math.radians(rng.uniform(22, 50)) * side * (1 - 0.5 * t)
            base = np.array([rng.normal(0, 0.03) * width, t * height * 0.6, rng.normal(0, 0.02)])
            placements.append((base, np.array([math.sin(angle), math.cos(angle), 0]), rng.uniform(0.9, 1.1),
                               rng.normal(0, 0.15)))
        placements.append((np.array([0, height * 0.5, 0.02]), np.array([0, 1.0, 0]), 1.0, 0.0))
        data = assemble(twig_data, placements)
        axis = MeshData()
        tube(axis, np.array([[0, 0, -0.01], [0, height * 0.55, -0.01]]), np.array([0.015, 0.006]), 5,
             tile=(0.15, 0.5))
        data.extend(axis)
        data.fit_scale = fit(data, width, height)
        contents[name] = data
    for k, name in enumerate(MASSES):
        width, height = sizes["mass"]
        placements = []
        count = int(spec["twig"].get("massTwigs", 30))
        for i in range(count):
            r = math.sqrt(rng.uniform(0, 1)) * width * 0.32
            a = rng.uniform(0, 2 * math.pi)
            center = np.array([r * math.cos(a), height * 0.5 + r * math.sin(a), rng.normal(0, 0.1)])
            direction = unit(np.array([math.cos(a), math.sin(a), 0]) * 0.8 + rng.normal(0, 0.5, 3) * [1, 1, 0])
            base = center - direction * sizes["twig"][0] * 0.35
            placements.append((base, direction, rng.uniform(0.85, 1.15), rng.normal(0, 0.25)))
        data = assemble(twig_data, placements)
        data.fit_scale = fit(data, width, height)
        contents[name] = data
    folder = output / "renders"
    folder.mkdir(parents=True, exist_ok=True)
    stats = {}
    for index, (name, data) in enumerate(contents.items()):
        print(progress_line(index, len(contents) + 8, f"잎 카드 렌더 {name}"), flush=True)
        obj = data.build(f"card_{name}", materials)
        x, y, w, h = SLOTS[name]
        kind = "twig" if name.startswith("twig") else "cluster" if name.startswith("cluster") else "mass"
        width, height = sizes[kind]
        camera_data.ortho_scale = max(width, height)
        camera.location = (0, height / 2, 5)
        camera.rotation_euler = (0, 0, 0)
        passes = render_passes(scene, folder, name, w, h, ao_distance=0.04 if kind == "twig" else 0.08)
        atlas["albedo"][y:y + h, x:x + w] = passes["albedo"]
        atlas["normal"][y:y + h, x:x + w] = passes["normal"]
        atlas["data"][y:y + h, x:x + w] = passes["data"]
        atlas["alpha"][y:y + h, x:x + w] = passes["alpha"]
        stats[name] = {"meters": [round(width, 3), round(height, 3)], "coverage": round(float(passes["alpha"].mean()), 3),
                       "triangles": data.triangles(), "fitScale": round(float(getattr(data, "fit_scale", 1.0)), 3)}
        bpy.data.objects.remove(obj, do_unlink=True)
    return {"atlas": atlas, "cards": stats, "sizes": sizes}


def write_atlas(atlas: dict, textures: Path, output: Path, name: str) -> tuple[dict, dict]:
    from PIL import Image

    alpha = atlas["alpha"]
    albedo = fill_outside(atlas["albedo"], alpha)
    normal = fill_outside(atlas["normal"], alpha)
    data = fill_outside(atlas["data"], alpha)
    # 나이테: 칸에 그대로 붙인다(알파는 원 안).
    x, y, w, h = SLOTS["ring"]
    ring_color = np.asarray(Image.open(textures / "ring" / "ring_basecolor.png").convert("RGBA").resize((w, h)),
                            dtype=np.float64) / 255
    ring_normal = np.asarray(Image.open(textures / "ring" / "ring_normal.png").convert("RGB").resize((w, h)),
                             dtype=np.float64) / 255
    ring_mask = np.asarray(Image.open(textures / "ring" / "ring_mask.png").convert("RGBA").resize((w, h)),
                           dtype=np.float64) / 255
    base_srgb = byte(srgb(albedo))
    base_srgb[y:y + h, x:x + w] = byte(ring_color[..., :3])
    alpha_out = alpha.copy()
    alpha_out[y:y + h, x:x + w] = ring_color[..., 3]
    normal_out = normal.copy()
    normal_out[y:y + h, x:x + w] = ring_normal
    mask = np.stack([np.zeros_like(alpha), data[..., 2], data[..., 1], 1 - data[..., 0]], axis=-1)
    mask[y:y + h, x:x + w] = np.stack([np.zeros((h, w)), ring_mask[..., 1], np.zeros((h, w)), ring_mask[..., 3]], axis=-1)
    files = {"basecolor": f"T_tree_{name}_leaves_basecolor.png", "normal": f"T_tree_{name}_leaves_normal.png",
             "mask": f"T_tree_{name}_leaves_mask.png"}
    from .terrain_tile import neon_fraction

    leaf = alpha >= 0.5
    leaf[y:y + h, x:x + w] = False
    files_stats = {"neonFraction": round(neon_fraction(albedo[leaf].astype(np.float32)), 5),
                   "meanColorSrgb": [round(float(v), 4) for v in srgb(albedo[leaf]).mean(axis=0)]}
    Image.fromarray(np.concatenate([base_srgb, byte(alpha_out)[..., None]], axis=-1)).save(output / files["basecolor"])
    Image.fromarray(byte(normal_out)).save(output / files["normal"])
    Image.fromarray(byte(mask)).save(output / files["mask"])
    return files, files_stats


# ---------------------------------------------------------------- skeleton

@dataclass
class Branch:
    points: np.ndarray
    radii: np.ndarray
    level: int
    phase: float = 0.0
    g0: float = 0.0
    g1: float = 1.0
    parent: "Branch | None" = None
    children: list = field(default_factory=list)
    virtual: bool = False

    def length(self) -> float:
        return float(np.linalg.norm(np.diff(self.points, axis=0), axis=1).sum())

    def cumulative(self) -> np.ndarray:
        return np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(self.points, axis=0), axis=1))])

    def at(self, t: float) -> tuple[np.ndarray, np.ndarray, float, float]:
        """Point, direction, radius and wind G at fraction ``t`` of the length."""
        c = self.cumulative()
        d = t * c[-1]
        i = int(np.clip(np.searchsorted(c, d) - 1, 0, len(self.points) - 2))
        f = (d - c[i]) / max(c[i + 1] - c[i], 1e-9)
        p = self.points[i] + (self.points[i + 1] - self.points[i]) * f
        r = self.radii[i] + (self.radii[i + 1] - self.radii[i]) * f
        return p, unit(self.points[i + 1] - self.points[i]), float(r), self.g(t)

    def g(self, t) -> float:
        return self.g0 + (self.g1 - self.g0) * t if self.level > 0 else 0.0


class Envelope:
    """The crown's outline: an ellipsoid (broadleaf) or a cone (conifer), around the trunk axis."""

    def __init__(self, spec: dict):
        self.kind = "cone" if spec["form"] == "conifer" else "ellipsoid"
        crown = spec["crown"]
        self.base, self.top = float(crown["base"]), float(spec["heightMeters"])
        self.radius = float(crown["radius"])
        self.center = (self.base + self.top) / 2
        self.half = (self.top - self.base) / 2
        self.widest = float(crown.get("widest", 0.5))

    def radius_at(self, z: float) -> float:
        if z < self.base or z > self.top:
            return 0.0
        t = (z - self.base) / (self.top - self.base)
        if self.kind == "cone":
            return self.radius * (1 - t) ** 0.95
        # 가장 넓은 높이(widest)가 가운데가 아닐 수 있는 타원 단면
        if t < self.widest:
            u = 1 - t / self.widest
        else:
            u = (t - self.widest) / (1 - self.widest)
        return self.radius * math.sqrt(max(0.0, 1 - u * u))

    def inside(self, p: np.ndarray, slack: float = 1.0) -> bool:
        return math.hypot(p[0], p[1]) <= self.radius_at(p[2]) * slack + 1e-6

    def distance(self, p: np.ndarray, d: np.ndarray, limit: float = 30.0) -> float:
        """How far from ``p`` along ``d`` the envelope is crossed (stepping)."""
        step = 0.1
        t = 0.0
        while t < limit:
            q = p + d * t
            if q[2] > self.base and not self.inside(q):
                return t
            t += step
        return limit


def grow(start: np.ndarray, direction: np.ndarray, length: float, steps: int, *, up: float, out: float,
         noise: float, rng: np.random.Generator, droop_tip: float = 0.0) -> np.ndarray:
    """A branch curve: each step turns a little toward up (phototropism), outward, randomly, and the tip
    may droop."""
    points = [np.asarray(start, float)]
    d = unit(np.asarray(direction, float))
    step = length / steps
    for i in range(steps):
        t = (i + 1) / steps
        radial = np.array([points[-1][0], points[-1][1], 0.0])
        radial = unit(radial) if np.linalg.norm(radial) > 1e-6 else np.zeros(3)
        turn = np.array([0, 0, up]) + radial * out + rng.normal(0, noise, 3)
        if droop_tip:
            turn += np.array([0, 0, -droop_tip * t * t])
        d = unit(d + turn / steps * 3)
        points.append(points[-1] + d * step)
    return np.array(points)


def taper(count: int, r0: float, r1: float, power: float = 1.0) -> np.ndarray:
    t = np.linspace(0, 1, count)
    return r1 + (r0 - r1) * (1 - t) ** power


def trunk_radius(spec: dict, z: np.ndarray, top: float) -> np.ndarray:
    """DBH/2 at 1.3 m, a flare toward the ground, a taper to ``tipRadius`` at ``top``."""
    r = float(spec["dbhMeters"]) / 2
    flare = float(spec.get("flare", 0.3))
    tip = float(spec.get("tipRadius", 0.02))
    power = float(spec.get("taperPower", 1.0))
    zc = np.clip(z, 0.0, None)
    above = np.clip((zc - 1.3) / max(top - 1.3, 1e-6), 0, 1)
    body = np.where(zc <= 1.3, r, tip + (r - tip) * (1 - above) ** power)
    body = np.where(zc <= 1.3, r + (r * 0.04) * (1.3 - zc) / 1.3, body)
    return body * (1 + flare * np.exp(-zc / 0.45))


def skeleton(spec: dict, rng: np.random.Generator) -> list[Branch]:
    if spec["form"] == "conifer":
        return conifer(spec, rng)
    return broadleaf(spec, rng)


def trunk_branch(spec: dict, top: float, rng: np.random.Generator, lean_deg: float) -> Branch:
    steps = max(8, int(top / 0.6))
    z = np.concatenate([[-UNDERGROUND], np.linspace(0, top, steps + 1)])
    lean = math.radians(lean_deg)
    azimuth = rng.uniform(0, 2 * math.pi)
    wobble = rng.normal(0, 0.04, (len(z), 2)).cumsum(axis=0) * 0.25
    zc = np.clip(z, 0, None)
    x = math.sin(lean) * zc * math.cos(azimuth) + wobble[:, 0] * (zc / max(top, 1)) ** 1.5
    y = math.sin(lean) * zc * math.sin(azimuth) + wobble[:, 1] * (zc / max(top, 1)) ** 1.5
    points = np.stack([x, y, z], axis=1)
    radii = trunk_radius(spec, z, float(spec["heightMeters"]))
    return Branch(points, radii, 0, phase=0.0, g0=0.0, g1=0.0)


def side_branches(spec: dict, parent: Branch, env: Envelope, rng: np.random.Generator, level: int,
                  start: float, end: float, branches: list[Branch], phase: float) -> None:
    """Branches along ``parent`` between fractions ``start`` and ``end`` by rule ``spec["branches"][level - 1]``.

    Each turns 137.5° around its parent from the last (``planar`` rules alternate left and right in the
    horizontal plane instead: a fir's flat sprays). Its length is a share of the distance to the crown edge.
    The deepest levels may be ``virtual``: never meshed, they only carry leaf cards (the real twigs are too
    thin to model). Wind G continues from the parent's value where the branch starts; the phase is the
    first-order branch's, so a limb and all it carries sway together.
    """
    rule = spec["branches"][level - 1]
    length = parent.length()
    spacing = float(rule["spacing"])
    d = start * length + rng.uniform(0, spacing)
    azimuth = rng.uniform(0, 2 * math.pi)
    up = np.array([0.0, 0.0, 1.0])
    side = 1
    while d < end * length:
        t = d / length
        p, tangent, radius, g = parent.at(t)
        angle = math.radians(rng.uniform(*rule["angle"]))
        if rule.get("planar"):
            side = -side
            across = np.cross(up, tangent)
            across = unit(across) if np.linalg.norm(across) > 1e-6 else perpendicular(tangent)
            around = unit(across * side + up * rng.normal(0, 0.15))
        else:
            azimuth += math.radians(137.5) + rng.normal(0, 0.3)
            around = rotate(perpendicular(tangent), tangent, azimuth)
        direction = unit(tangent * math.cos(angle) + around * math.sin(angle))
        # 수관 안쪽(줄기 쪽)으로 향하는 가지는 바깥으로 돌린다.
        radial = np.array([p[0], p[1], 0.0])
        if level > 1 and np.linalg.norm(radial) > 1e-6 and direction @ unit(radial) < -0.2:
            direction = unit(direction + unit(radial) * 0.9)
        reach = env.distance(p, direction)
        span = min(float(rule.get("maxLength", 9.0)), max(reach, float(rule.get("reachFloor", 0.0))) *
                   float(rule["lengthFactor"]) * rng.uniform(0.85, 1.1))
        span *= float(rule.get("shorten", 1.0)) ** t
        if span >= float(rule.get("minLength", 0.3)):
            steps = max(2, int(span / float(rule.get("step", 0.5))))
            points = grow(p, direction, span, steps, up=float(rule.get("up", 0.1)), out=float(rule.get("out", 0.0)),
                          noise=float(rule.get("noise", 0.08)), rng=rng, droop_tip=float(rule.get("droop", 0.0)))
            r0 = max(float(rule.get("minRadius", 0.008)), min(radius * float(rule["radiusRatio"]), radius * 0.9))
            radii = taper(len(points), r0, float(rule.get("tipRadius", 0.005)), float(rule.get("taperPower", 1.0)))
            own_phase = rng.uniform(0, 1) if level == 1 else phase
            branch = Branch(points, radii, level, phase=own_phase, g0=g if level > 1 else 0.0, g1=1.0, parent=parent)
            branch.virtual = bool(rule.get("virtual"))
            parent.children.append(branch)
            branches.append(branch)
            if level < len(spec["branches"]):
                side_branches(spec, branch, env, rng, level + 1, float(spec["branches"][level].get("start", 0.2)),
                              0.97, branches, own_phase)
        d += spacing * rng.uniform(0.75, 1.25)


def broadleaf(spec: dict, rng: np.random.Generator) -> list[Branch]:
    env = Envelope(spec)
    height = float(spec["heightMeters"])
    trunk_spec = spec["trunk"]
    fork = trunk_spec.get("fork")
    top = float(fork) if fork else height - float(trunk_spec.get("leaderGap", 0.3))
    trunk = trunk_branch(spec, top, rng, float(trunk_spec.get("lean", 1.5)))
    branches = [trunk]
    if fork:
        limbs = spec["limbs"]
        count = int(limbs["count"])
        p, tangent, radius, _ = trunk.at(1.0)
        azimuth0 = rng.uniform(0, 2 * math.pi)
        for k in range(count):
            leader = k == 0 and limbs.get("leader", True)
            spread = math.radians(rng.uniform(*limbs["leaderSpread"])) if leader else \
                math.radians(rng.uniform(*limbs["spread"]))
            azimuth = azimuth0 + 2 * math.pi * k / count + rng.normal(0, 0.25)
            direction = unit(np.array([math.sin(spread) * math.cos(azimuth), math.sin(spread) * math.sin(azimuth),
                                       math.cos(spread)]))
            reach = env.distance(p, direction)
            span = reach * float(limbs["lengthFactor"]) * rng.uniform(0.9, 1.05)
            steps = max(4, int(span / 0.6))
            points = grow(p - tangent * radius * 0.3, direction, span, steps, up=float(limbs.get("up", 0.15)),
                          out=float(limbs.get("out", 0.1)), noise=float(limbs.get("noise", 0.1)), rng=rng)
            ratio = float(limbs["radiusRatio"]) * (1.15 if leader else 1.0)
            radii = taper(len(points), radius * ratio, float(limbs.get("tipRadius", 0.02)), 1.1)
            limb = Branch(points, radii, 1, phase=rng.uniform(0, 1), g0=0.0, g1=1.0, parent=trunk)
            trunk.children.append(limb)
            branches.append(limb)
            side_branches(spec, limb, env, rng, 2, float(spec["branches"][1].get("start", 0.15)), 0.97, branches,
                          limb.phase)
    else:
        side_branches(spec, trunk, env, rng, 1, max(0.0, (env.base - 0.2) / top), 0.985, branches, 0.0)
    return branches


def conifer(spec: dict, rng: np.random.Generator) -> list[Branch]:
    """A straight trunk to the top and whorls of near-level branches whose length follows the cone; each may
    carry flat side shoots (``branches[1]``)."""
    env = Envelope(spec)
    height = float(spec["heightMeters"])
    trunk = trunk_branch(spec, height, rng, float(spec["trunk"].get("lean", 0.8)))
    branches = [trunk]
    rule = spec["whorls"]
    z = env.base
    while z < height - 0.3:
        p, tangent, radius, _ = trunk.at(min(1.0, (z + UNDERGROUND) / (height + UNDERGROUND)))
        count = int(rng.integers(rule["count"][0], rule["count"][1] + 1))
        azimuth0 = rng.uniform(0, 2 * math.pi)
        t_height = (z - env.base) / (height - env.base)
        for k in range(count):
            azimuth = azimuth0 + 2 * math.pi * k / count + rng.normal(0, 0.2)
            pitch = math.radians(rule["pitchLow"] + (rule["pitchHigh"] - rule["pitchLow"]) * t_height + rng.normal(0, 5))
            direction = np.array([math.cos(pitch) * math.cos(azimuth), math.cos(pitch) * math.sin(azimuth),
                                  math.sin(pitch)])
            span = max(0.25, env.radius_at(z) * float(rule["lengthFactor"]) * rng.uniform(0.85, 1.08))
            steps = max(2, int(span / 0.45))
            points = grow(p, direction, span, steps, up=float(rule.get("up", 0.25)), out=0.0, noise=0.05, rng=rng,
                          droop_tip=float(rule.get("droop", 0.0)) * (1 - t_height))
            r0 = max(0.012, min(radius * 0.4, 0.015 + 0.02 * span))
            radii = taper(len(points), r0, 0.005, 1.0)
            branch = Branch(points, radii, 1, phase=rng.uniform(0, 1), g0=0.0, g1=1.0, parent=trunk)
            trunk.children.append(branch)
            branches.append(branch)
            if len(spec.get("branches", [])) > 1:
                side_branches(spec, branch, env, rng, 2, float(spec["branches"][1].get("start", 0.15)), 0.9, branches,
                              branch.phase)
        z += float(rule["spacing"]) * rng.uniform(0.85, 1.15) * (1 - 0.35 * t_height)
    return branches


# ---------------------------------------------------------------- wood and cards per LOD

def wind_color(z: float, height: float, g: float, b: float, phase: float) -> tuple:
    return (float(np.clip(max(z, 0.0) / height, 0, 1) ** 1.5), float(g), float(b), float(phase))


def wood(branches: list[Branch], spec: dict, lod: int, tile: tuple[float, float]) -> MeshData:
    """Tubes for the branches kept at this LOD: ``lods[lod]`` gives sides and the share of rings kept per level
    (0 sides drops the level), and the shortest branch kept per level. Virtual branches are never meshed.
    ``data.by_level`` counts the triangles per level."""
    data = MeshData()
    data.by_level = {}
    height = float(spec["heightMeters"])
    rule = spec["lods"][lod]
    sides_by_level = rule["sides"]
    has_fork = bool(spec.get("trunk", {}).get("fork"))
    for branch in branches:
        if branch.virtual or branch.level >= len(sides_by_level):
            continue
        sides = int(sides_by_level[branch.level])
        if sides < 3 or (branch.level > 0 and branch.length() < float(rule["minLength"][branch.level - 1])):
            continue
        rings = rule["rings"][branch.level] if isinstance(rule["rings"], list) else rule["rings"]
        keep = max(2, int(math.ceil((len(branch.points) - 1) * float(rings))) + 1)
        # 잎 카드 속으로 끝나는 가지(침엽수)는 끝까지 그리지 않는다. 맨가지 끝이 잎 밖으로 나오지 않게.
        last = len(branch.points) - 1
        if branch.level > 0 and float(rule.get("meshFraction", 1.0)) < 1.0:
            last = max(1, int(round(last * float(rule["meshFraction"]))))
        index = np.unique(np.round(np.linspace(0, last, keep)).astype(int))
        if branch.level == 0:
            # 땅 높이와 밑동 퍼짐의 고리는 남긴다(땅에서 굵기가 맞고 비탈에서 뜨지 않게).
            z = branch.points[:, 2]
            index = np.unique(np.concatenate([index, [int(np.argmin(np.abs(z - 0.0))), int(np.argmin(np.abs(z - 0.6)))]]))
        points, radii = branch.points[index], branch.radii[index]
        cumulative = branch.cumulative()[index]
        total = branch.cumulative()[-1]

        def color(i, branch=branch, points=points, cumulative=cumulative, total=total):
            g = branch.g(cumulative[i] / max(total, 1e-9))
            return wind_color(points[i][2], height, g, 0.0, branch.phase if branch.level else 0.0)

        before = data.triangles()
        tube(data, points, radii, sides, tile=tile, wind=color, material=0,
             cap_tip=branch.level > 0 or not has_fork)
        data.by_level[branch.level] = data.by_level.get(branch.level, 0) + data.triangles() - before
    return data


@dataclass
class Card:
    base: np.ndarray
    up: np.ndarray        # along the card, base → free end
    normal: np.ndarray    # facing
    width: float
    height: float
    slot: str
    g: float
    phase: float


def card_frame(direction: np.ndarray, facing: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    y = unit(direction)
    n = facing - y * (facing @ y)
    if np.linalg.norm(n) < 1e-6:
        n = perpendicular(y)
    return y, unit(n)


def lod0_cards(branches: list[Branch], spec: dict, env: Envelope, rng: np.random.Generator, count: int,
               sizes: dict) -> tuple[list[Card], int]:
    """Twig cards along leafy branches (``cards.levels``: level → [from, to] fraction of the branch), ``count``
    of them, outer ones preferred. Returns the cards and how many candidates there were."""
    rule = spec["cards"]
    up = np.array([0.0, 0.0, 1.0])
    candidates = []
    w, h = sizes["twig"]
    mode = rule.get("direction", "outward")
    for branch in branches:
        span = rule["levels"].get(str(branch.level))
        if not span:
            continue
        length = branch.length()
        spacing = float(rule["spacing"])
        d = span[0] * length + rng.uniform(0, spacing * 0.5)
        stops = []
        while d <= span[1] * length:
            stops.append(d / length)
            d += spacing * rng.uniform(0.7, 1.3)
        for t in stops:
            p, tangent, radius, g = branch.at(t)
            radial = np.array([p[0], p[1], 0.0])
            radial = unit(radial) if np.linalg.norm(radial) > 1e-6 else perpendicular(up)
            r_env = env.radius_at(p[2])
            shell = min(1.2, math.hypot(p[0], p[1]) / max(r_env, 0.3)) if r_env > 0 else 1.0
            height_share = (p[2] - env.base) / max(env.top - env.base, 1e-6)
            for _ in range(int(rule.get("perPoint", 1))):
                if mode == "hanging":
                    direction = unit(-up * 0.75 + radial * 0.5 + tangent * 0.3 + rng.normal(0, 0.3, 3))
                    facing = unit(radial + rng.normal(0, 0.6, 3))
                elif mode == "spray":
                    side = 1 if rng.random() < 0.5 else -1
                    flat = np.array([tangent[0], tangent[1], 0.0])
                    flat = unit(flat) if np.linalg.norm(flat) > 1e-6 else radial
                    direction = unit(rotate(flat, up, math.radians(rng.uniform(15, 65)) * side) + up * rng.normal(0.05, 0.15))
                    facing = unit(up * 0.55 + radial * 0.6 + rng.normal(0, 0.5, 3))
                else:
                    direction = unit(tangent * 0.45 + radial * 0.55 + up * (0.2 + 0.35 * height_share) +
                                     rng.normal(0, 0.4, 3))
                    # 위를 보는 카드는 옆에서 모서리로 보인다. 바깥을 주로 보게 한다.
                    facing = unit(radial * 0.75 + up * 0.25 + rng.normal(0, 0.5, 3))
                y, n = card_frame(direction, facing)
                scale = rng.uniform(0.85, 1.15)
                score = 0.4 + shell + rng.uniform(0, 0.5)
                candidates.append((score, Card(p, y, n, w * scale, h * scale, TWIGS[rng.integers(len(TWIGS))], g,
                                              branch.phase)))
    candidates.sort(key=lambda item: -item[0])
    total = len(candidates)
    if total <= count:
        return [c for _, c in candidates], total
    # 점수 순으로 두되 같은 곳만 몰리지 않게 상위 1.4배 안에서 고른다.
    pool = candidates[: min(total, int(count * 1.4))]
    pick = rng.choice(len(pool), size=count, replace=False)
    return [pool[i][1] for i in sorted(pick)], total


def kmeans(points: np.ndarray, k: int, rng: np.random.Generator, iterations: int = 25) -> np.ndarray:
    from scipy.spatial import cKDTree

    k = max(1, min(k, len(points)))
    centers = points[rng.choice(len(points), size=k, replace=False)]
    labels = np.zeros(len(points), dtype=int)
    for _ in range(iterations):
        _, labels = cKDTree(centers).query(points)
        for j in range(k):
            members = points[labels == j]
            if len(members):
                centers[j] = members.mean(axis=0)
    return labels


def merged_cards(cards: list[Card], count: int, kind: str, sizes: dict, shell, rng: np.random.Generator) -> list[Card]:
    """Fewer, bigger cards for LOD1 (branch clusters) or LOD2 (leaf masses) from LOD0's cards, grouped by
    k-means. Their total area is LOD0's leaf card area, so the crown keeps its fullness and outline."""
    centers = np.array([c.base + c.up * c.height / 2 for c in cards])
    labels = kmeans(centers, count, rng)
    groups = [np.nonzero(labels == j)[0] for j in range(labels.max() + 1)]
    groups = [g for g in groups if len(g)]
    names = CLUSTERS if kind == "cluster" else MASSES
    w, h = sizes[kind]
    area0 = float(sum(c.width * c.height for c in cards))
    scale = math.sqrt(area0 / max(len(groups), 1) / (w * h))
    out = []
    for members in groups:
        center = centers[members].mean(axis=0)
        nearest = cards[min(members, key=lambda i: np.linalg.norm(centers[i] - center))]
        outward = shell(center[None])[0]
        jitter = rng.uniform(0.9, 1.1)
        # 수관 꼭대기 카드가 하늘을 보면 옆에서 선으로 보이므로 바깥 방향의 높이 성분을 줄인다.
        sideways = unit(outward * np.array([1.0, 1.0, 0.4]))
        if kind == "cluster":
            direction = unit(np.mean([cards[i].up for i in members], axis=0) + outward * 0.3)
            facing = unit(sideways * 0.75 + unit(np.mean([cards[i].normal * (1 if cards[i].normal @ outward >= 0 else -1)
                                                          for i in members], axis=0)) * 0.25)
            y, n = card_frame(direction, facing)
        else:
            n = sideways
            y = np.array([0.0, 0.0, 1.0]) - n * n[2]
            y = unit(y) if np.linalg.norm(y) > 0.2 else perpendicular(n)
            # 모두 곧게 서면 덩어리 윤곽이 매끈한 달걀이 된다. 조금씩 돌리고 기울인다.
            y = rotate(y, n, rng.normal(0, 0.45))
            n = unit(rotate(n, unit(np.cross(n, y)), rng.normal(0, 0.3)))
            y = unit(y - n * (y @ n))
        base = center - y * h * scale * jitter * 0.5
        if kind == "mass":
            # 덩어리 카드는 군집 중심에 서므로 바깥 끝이 LOD0 윤곽 밖으로 나간다. 크기만큼 조금 안으로 넣는다.
            base = base - n * h * scale * MASS_INSET
        out.append(Card(base, y, n, w * scale * jitter, h * scale * jitter, names[len(out) % len(names)], nearest.g,
                        nearest.phase))
    return out


def slot_uv(slot: str, inset_px: float = 1.5) -> tuple[float, float, float, float]:
    x, y, w, h = SLOTS[slot]
    return ((x + inset_px) / ATLAS, (x + w - inset_px) / ATLAS, 1 - (y + h - inset_px) / ATLAS, 1 - (y + inset_px) / ATLAS)


def card_mesh(cards: list[Card], height: float, shell) -> MeshData:
    """Quads (2 triangles) per card; normals from the crown shell; B runs 0 at the base to 1 at the free end."""
    data = MeshData()
    if not cards:
        return data
    verts, uvs, colors, faces = [], [], [], []
    for k, card in enumerate(cards):
        x = unit(np.cross(card.up, card.normal))
        u0, u1, v0, v1 = slot_uv(card.slot)
        corners = [card.base - x * card.width / 2, card.base + x * card.width / 2,
                   card.base + x * card.width / 2 + card.up * card.height, card.base - x * card.width / 2 + card.up * card.height]
        for corner, uv, b in zip(corners, ((u0, v0), (u1, v0), (u1, v1), (u0, v1)), (0, 0, 1, 1)):
            verts.append(corner)
            uvs.append(uv)
            colors.append(wind_color(corner[2], height, card.g, b, card.phase))
        a = 4 * k
        faces += [(a, a + 1, a + 2), (a, a + 2, a + 3)]
    verts = np.array(verts)
    data.add(verts, shell(verts), uvs, colors, faces, 1)
    return data


class Shell:
    """Outward normals of a smooth shell around the crown: the negative gradient of the blurred density of
    card points. Deep inside, where the gradient fades, the direction away from the trunk axis takes over."""

    def __init__(self, cards: list[Card], env: Envelope, cell: float = 0.25, sigma_m: float | None = None):
        from scipy.ndimage import gaussian_filter

        points = np.concatenate([[c.base + c.up * c.height * f for f in (0.25, 0.6, 0.95)] for c in cards])
        self.low = points.min(axis=0) - 3.0
        high = points.max(axis=0) + 3.0
        self.cell = cell
        shape = np.ceil((high - self.low) / cell).astype(int) + 1
        grid = np.zeros(shape, dtype=np.float32)
        index = np.clip(((points - self.low) / cell).round().astype(int), 0, shape - 1)
        np.add.at(grid, tuple(index.T), 1.0)
        sigma = (sigma_m or max(0.9, 0.16 * (env.top - env.base))) / cell
        density = gaussian_filter(grid, sigma)
        self.gradient = [np.asarray(g, dtype=np.float32) for g in np.gradient(density)]
        self.scale = float(np.abs(np.stack(self.gradient)).max())
        self.center = np.array([0.0, 0.0, env.center])

    def __call__(self, points: np.ndarray) -> np.ndarray:
        from scipy.ndimage import map_coordinates

        coords = ((np.asarray(points) - self.low) / self.cell).T
        grad = np.stack([map_coordinates(g, coords, order=1, mode="nearest") for g in self.gradient], axis=-1)
        outward = -grad
        strength = np.linalg.norm(outward, axis=-1, keepdims=True) / max(self.scale, 1e-9)
        fallback = unit(np.asarray(points) - self.center)
        weight = np.clip(strength * 6, 0, 1)
        return unit(unit(outward) * weight + fallback * (1 - weight))


# ---------------------------------------------------------------- stump and billboard

def stump(spec: dict, tile: tuple[float, float]) -> MeshData:
    """A 0.5 m stump of the trunk's own girth: bark sides (material 0), a slightly inset sawn top whose rings
    sit in the leaf atlas's ring slot (material 1). Vertex colors are zero."""
    data = MeshData()
    z = np.array([-UNDERGROUND, 0.0, 0.08, 0.2, 0.34, STUMP_HEIGHT - 0.02])
    radii = trunk_radius(spec, z, float(spec["heightMeters"]))
    points = np.stack([np.zeros_like(z), np.zeros_like(z), z], axis=1)
    tube(data, points, radii, STUMP_SIDES, tile=tile, material=0, cap_tip=False)
    # 껍질 테두리: 바깥 고리에서 톱질 면으로 조금 들어간다.
    r_top = radii[-1]
    verts, normals, uvs, faces = [], [], [], []
    repeats = max(1, round(2 * math.pi * r_top / tile[0]))
    for ring, (radius, height) in enumerate(((r_top, STUMP_HEIGHT - 0.02), (r_top * 0.95, STUMP_HEIGHT))):
        for j in range(STUMP_SIDES + 1):
            a = 2 * math.pi * j / STUMP_SIDES
            verts.append((radius * math.cos(a), radius * math.sin(a), height))
            normals.append(unit(np.array([math.cos(a), math.sin(a), 1.2 * ring])))
            uvs.append((repeats * j / STUMP_SIDES, (height + 0.02 * ring) / tile[1]))
    for j in range(STUMP_SIDES):
        a, b, c, d = j, j + 1, j + STUMP_SIDES + 1, j + STUMP_SIDES + 2
        faces += [(a, b, d), (a, d, c)]
    data.add(verts, normals, uvs, np.zeros((len(verts), 4)), faces, 0)
    u0, u1, v0, v1 = slot_uv("ring", 3)
    cu, cv, ru, rv = (u0 + u1) / 2, (v0 + v1) / 2, (u1 - u0) / 2, (v1 - v0) / 2
    top = [(0.0, 0.0, STUMP_HEIGHT)]
    top_uv = [(cu, cv)]
    inner = r_top * 0.95
    for j in range(STUMP_SIDES):
        a = 2 * math.pi * j / STUMP_SIDES
        top.append((inner * math.cos(a), inner * math.sin(a), STUMP_HEIGHT))
        top_uv.append((cu + ru * 0.97 * math.cos(a), cv + rv * 0.97 * math.sin(a)))
    faces = [(0, 1 + j, 1 + (j + 1) % STUMP_SIDES) for j in range(STUMP_SIDES)]
    data.add(top, np.tile([0, 0, 1.0], (len(top), 1)), top_uv, np.zeros((len(top), 4)), faces, 1)
    return data


def bounds(data: MeshData) -> tuple[np.ndarray, np.ndarray]:
    verts = np.concatenate(data.verts)
    return verts.min(axis=0), verts.max(axis=0)


def billboard(scene, lod0, low: np.ndarray, high: np.ndarray, output: Path, name: str) -> tuple[MeshData, dict]:
    """Front and side renders of LOD0 into 2048×1024 and two crossed quads mapped onto them."""
    import bpy
    from PIL import Image

    camera_data = bpy.data.cameras.new("billboard")
    camera_data.type = "ORTHO"
    camera = bpy.data.objects.new("billboard", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    center = (low + high) / 2
    views = {"front": (np.array([1.0, 0, 0]), np.array([0, -1.0, 0]), (low[0], high[0]), (math.radians(90), 0, 0),
                       (center[0], low[1] - 30, center[2])),
             "side": (np.array([0, 1.0, 0]), np.array([1.0, 0, 0]), (low[1], high[1]), (math.radians(90), 0, math.radians(90)),
                      (high[0] + 30, center[1], center[2]))}
    color_out = np.zeros((BILLBOARD, BILLBOARD * 2, 4))
    normal_out = np.zeros((BILLBOARD, BILLBOARD * 2, 3))
    quads = MeshData()
    stats = {}
    for index, (view, (right, toward, (a0, a1), rotation, location)) in enumerate(views.items()):
        width, height = a1 - a0, high[2] - low[2]
        scale = max(width, height) * 1.02
        mid = (a0 + a1) / 2
        camera_data.ortho_scale = scale
        camera.rotation_euler = rotation
        loc = list(location)
        if view == "front":
            loc[0] = mid
        else:
            loc[1] = mid
        camera.location = loc
        camera_data.clip_end = 200
        # 색은 고른 빛(세기 1의 균일한 흰 하늘)에서 확산·투과로 렌더한 값이다. 가림 없는 면은 알베도 그대로이고,
        # 수관 안쪽은 하늘이 가려진 만큼(사이 반사 포함) 어두워진다. 방향 있는 그림자·하이라이트는 없다.
        passes = render_passes(scene, output / "renders", f"billboard_{view}", BILLBOARD, BILLBOARD, ao_distance=1.2,
                               which_passes=("normal",))
        ambient = render_ambient(scene, output / "renders" / f"billboard_{view}_ambient.exr", BILLBOARD)
        alpha = passes["alpha"]
        albedo = ambient
        world = passes["normal"] * 2 - 1
        # 바늘잎·잎맥 노멀의 픽셀 단위 잡음은 먼 거리에서 반짝인다. 덮인 비율로 가중해 1.5px만 고른다.
        from scipy.ndimage import gaussian_filter

        weight = gaussian_filter(alpha, BILLBOARD_NORMAL_BLUR)[..., None]
        world = gaussian_filter(world * alpha[..., None], (BILLBOARD_NORMAL_BLUR, BILLBOARD_NORMAL_BLUR, 0)) / np.maximum(weight, 1e-4)
        up = np.array([0, 0, 1.0])
        tangent = np.stack([world @ right, world @ up, world @ toward], axis=-1)
        tangent = unit(np.where(alpha[..., None] > 1e-4, tangent, [0, 0, 1.0]))
        albedo = fill_outside(albedo, alpha)
        tangent = fill_outside(tangent, alpha)
        color_out[:, index * BILLBOARD:(index + 1) * BILLBOARD, :3] = srgb(albedo)
        color_out[:, index * BILLBOARD:(index + 1) * BILLBOARD, 3] = alpha
        normal_out[:, index * BILLBOARD:(index + 1) * BILLBOARD] = tangent * 0.5 + 0.5
        # 쿼드: 화면에서 나무가 차지한 영역(가로 a0..a1, 세로 low..high)을 그 반쪽 텍스처의 같은 자리에.
        def u_of(value):
            return (index + 0.5 + (value - mid) / scale) / 2

        def v_of(value):
            return 0.5 + (value - center[2]) / scale

        corners = []
        for (pa, pz) in ((a0, low[2]), (a1, low[2]), (a1, high[2]), (a0, high[2])):
            corners.append(right * pa + np.array([0, 0, pz]))
        corners = np.array(corners)
        uvs = [(u_of(pa), v_of(pz)) for pa, pz in ((a0, low[2]), (a1, low[2]), (a1, high[2]), (a0, high[2]))]
        quads.add(corners, np.tile(toward, (4, 1)), uvs, np.zeros((4, 4)), [(0, 1, 2), (0, 2, 3)], 0)
        inside = alpha > 0.5
        stats[view] = {"widthMeters": round(float(width), 3), "heightMeters": round(float(height), 3),
                       "coverage": round(float(alpha.mean()), 3),
                       "meanColorSrgb": [round(float(v), 4) for v in srgb(albedo[inside]).mean(axis=0)],
                       "meanNormal": [round(float(v), 3) for v in tangent[inside].mean(axis=0)]}
    files = {"basecolor": f"T_tree_{name}_billboard_basecolor.png", "normal": f"T_tree_{name}_billboard_normal.png"}
    Image.fromarray(byte(color_out)).save(output / files["basecolor"])
    Image.fromarray(byte(normal_out)).save(output / files["normal"])
    bpy.data.objects.remove(camera, do_unlink=True)
    return quads, {"views": stats, "files": files}


# ---------------------------------------------------------------- export and checks

def export_fbx(objects: list, path: Path) -> None:
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True, object_types={"MESH"},
                             apply_scale_options="FBX_SCALE_UNITS", axis_forward="-Z", axis_up="Y",
                             bake_space_transform=True, mesh_smooth_type="FACE", use_tspace=True,
                             colors_type="LINEAR", prioritize_active_color=True, path_mode="STRIP",
                             embed_textures=False)


def triangles(obj) -> int:
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def check_fbx(path: Path, expected: dict[str, dict]) -> tuple[list[str], dict]:
    """Read an FBX back: every expected mesh by name, its triangles, size, and wind colors' ranges."""
    import bpy

    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=str(path), colors_type="LINEAR")
    added = [obj for obj in bpy.data.objects if obj not in before]
    problems, found = [], {}
    by_name = {obj.name.split(".")[0]: obj for obj in added if obj.type == "MESH"}
    for name, want in expected.items():
        obj = by_name.get(name)
        if obj is None:
            problems.append(f"{path.name}: {name} 없음")
            continue
        tris = triangles(obj)
        corners = [obj.matrix_world @ _v(*c) for c in obj.bound_box]
        size = [round(max(c[i] for c in corners) - min(c[i] for c in corners), 3) for i in range(3)]
        record = {"triangles": tris, "size": size}
        attributes = obj.data.color_attributes
        if attributes:
            values = np.empty(len(attributes[0].data) * 4, dtype=np.float32)
            attributes[0].data.foreach_get("color", values)
            values = values.reshape(-1, 4)
            record["colorMin"] = [round(float(v), 3) for v in values.min(axis=0)]
            record["colorMax"] = [round(float(v), 3) for v in values.max(axis=0)]
        else:
            record["colorMin"] = record["colorMax"] = None
        found[name] = record
        if tris != want["triangles"]:
            problems.append(f"{name} 면 수 {tris} ≠ {want['triangles']}")
        if any(abs(a - b) > 0.01 for a, b in zip(sorted(size), sorted(want["size"]))):
            problems.append(f"{name} 크기 {size} ≠ {want['size']}")
        if record["colorMax"] is None:
            problems.append(f"{name} 정점색 없음")
        elif want.get("zeroColor") and max(record["colorMax"]) > 1e-3:
            problems.append(f"{name} 정점색이 0이 아님 {record['colorMax']}")
        elif not want.get("zeroColor") and record["colorMax"][0] < 0.9:
            problems.append(f"{name} 정점색 R 최대 {record['colorMax'][0]} (꼭대기 1이어야 함)")
    for obj in added:
        bpy.data.objects.remove(obj, do_unlink=True)
    return problems, found


def unity_bounds(low, high) -> dict:
    return {"min": [round(float(low[0]), 3), round(float(low[2]), 3), round(float(low[1]), 3)],
            "max": [round(float(high[0]), 3), round(float(high[2]), 3), round(float(high[1]), 3)]}


# ---------------------------------------------------------------- review

def review_scene(scene):
    from .kit_build import ground

    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Punchy"
    scene.view_settings.exposure = -0.6
    import bpy

    world = bpy.data.worlds.new("sky")
    scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    sky = world.node_tree.nodes.new("ShaderNodeTexSky")
    sky.sky_type = "MULTIPLE_SCATTERING"
    sky.sun_disc = False
    sky.sun_elevation = math.radians(40)
    sky.sun_rotation = math.radians(215)
    world.node_tree.links.new(sky.outputs["Color"], background.inputs["Color"])
    background.inputs["Strength"].default_value = 0.35
    sun = bpy.data.lights.new("sun", "SUN")
    sun.energy = 4.5
    sun.angle = math.radians(2.5)
    sun.color = (1.0, 0.95, 0.86)
    sun_obj = bpy.data.objects.new("sun", sun)
    sun_obj.rotation_euler = (math.radians(50), 0, math.radians(-25))
    scene.collection.objects.link(sun_obj)
    scene.render.film_transparent = False
    settings = scene.render.image_settings
    settings.file_format = "PNG"
    settings.color_depth = "8"
    settings.color_mode = "RGB"
    floor = ground(400)
    return [sun_obj, floor]


def render_png(scene, path: Path, width: int, height: int, samples: int) -> None:
    import bpy

    scene.cycles.samples = samples
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    scene.cycles.use_denoising = False


def look_at(camera, location, target):
    camera.location = location
    direction = _v(*target) - _v(*location)
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def board(output: Path, report: dict, files: dict) -> None:
    """``review.png``: the tree, the LOD lineup, wind channels and the atlases side by side."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    font = ImageFont.truetype(str(font_path), 20) if font_path.exists() else ImageFont.load_default()
    hero = Image.open(output / "hero.png").convert("RGB")
    lods = Image.open(output / "lods.png").convert("RGB")
    wind = Image.open(output / "wind.png").convert("RGB")
    atlas = Image.open(output / files["leaves"]["basecolor"]).convert("RGBA")
    checker = Image.new("RGBA", atlas.size, (70, 74, 80, 255))
    checker.alpha_composite(atlas)
    atlas_small = checker.convert("RGB").resize((600, 600))
    bb = Image.open(output / files["billboard"]["basecolor"]).convert("RGBA")
    bb_bg = Image.new("RGBA", bb.size, (70, 74, 80, 255))
    bb_bg.alpha_composite(bb)
    bb_small = bb_bg.convert("RGB").resize((600, 300))
    width = hero.width + 600
    top = Image.new("RGB", (width, max(hero.height, 900)), (34, 36, 40))
    top.paste(hero, (0, 0))
    top.paste(atlas_small, (hero.width, 0))
    top.paste(bb_small, (hero.width, 600))
    lods_scaled = lods.resize((width, int(lods.height * width / lods.width)))
    wind_scaled = wind.resize((width, int(wind.height * width / wind.width)))
    out = Image.new("RGB", (width, top.height + lods_scaled.height + wind_scaled.height + 40), (34, 36, 40))
    out.paste(top, (0, 40))
    out.paste(lods_scaled, (0, 40 + top.height))
    out.paste(wind_scaled, (0, 40 + top.height + lods_scaled.height))
    draw = ImageDraw.Draw(out)
    t = report["triangles"]
    draw.text((10, 8), f"SM_tree_{report['name']} · {report['heightMeters']} m · LOD0 {t['LOD0']:,} / LOD1 {t['LOD1']:,} / "
                       f"LOD2 {t['LOD2']:,} / billboard {t['billboard']} / stump {t['stump']} tris", font=font,
              fill=(235, 235, 235))
    out.save(output / "review.png")


def label(image, text: str):
    from PIL import ImageDraw, ImageFont

    font_path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default()
    ImageDraw.Draw(image).text((12, 10), text, font=font, fill=(255, 255, 255))
    return image


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--textures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    import bpy
    from PIL import Image

    started = time.monotonic()
    spec = json.loads(args.spec.read_text("utf-8"))
    name = spec["name"]
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    (out / "renders").mkdir(exist_ok=True)
    total = 20
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    device = setup_render(scene, CARD_SAMPLES)
    unlit_mode(scene)

    textures = args.textures
    bark_json = json.loads((textures / "bark" / "bark.json").read_text("utf-8"))
    bark_tile = tuple(bark_json["tileMeters"])
    bark_files = {key: textures / "bark" / value for key, value in bark_json["files"].items()}
    bark_material = make_material(f"M_tree_{name}_bark", {"color": image(bark_files["basecolor"], False),
                                                          "normal": image(bark_files["normal"], True),
                                                          "mask": image(bark_files["mask"], True)})
    leaves_dir = textures / "leaves"
    source_leaf = make_material("leaf_source", {"color": image(leaves_dir / "diff.png", False),
                                                "normal": image(leaves_dir / "nor.png", True),
                                                "rough": image(leaves_dir / "rough.png", True),
                                                "ao": image(leaves_dir / "ao.png", True)},
                                alpha_map=image(leaves_dir / "alpha.png", True),
                                translucency=float(spec["twig"].get("translucency", 1.0)))
    # 1. 잎 아틀라스
    cards_result = build_cards(spec, textures, out, scene, [bark_material, source_leaf])
    leaf_files, leaf_stats = write_atlas(cards_result["atlas"], textures, out, name)
    sizes = cards_result["sizes"]
    leaves_material = make_material(f"M_tree_{name}_leaves", {"color": image(out / leaf_files["basecolor"], False),
                                                              "normal": image(out / leaf_files["normal"], True),
                                                              "mask": image(out / leaf_files["mask"], True),
                                                              "alphaFromColor": True})

    # 2. 뼈대
    print(progress_line(13, total, "줄기·가지 만드는 중"), flush=True)
    rng = np.random.default_rng(spec["seed"] + 101)
    branches = skeleton(spec, rng)
    env = Envelope(spec)
    height = float(spec["heightMeters"])

    # 3. LOD
    target = int(spec["triangles"])
    wood_lods = [wood(branches, spec, lod, bark_tile) for lod in range(3)]
    n0 = max(50, (target - wood_lods[0].triangles()) // 2)
    cards0, candidates = lod0_cards(branches, spec, env, rng, n0, sizes)
    shell = Shell(cards0, env, sigma_m=spec["cards"].get("shellSigma"))
    lod_data = []
    for lod in range(3):
        data = MeshData()
        data.extend(wood_lods[lod])
        if lod == 0:
            cards = cards0
        else:
            lod_target = int(round(target * LOD_RATIOS[lod]))
            count = max(6, (lod_target - wood_lods[lod].triangles()) // 2)
            cards = merged_cards(cards0, count, "cluster" if lod == 1 else "mass", sizes, shell, rng)
        data.extend(card_mesh(cards, height, shell))
        lod_data.append((data, len(cards)))
    objects = [data.build(f"SM_tree_{name}_LOD{lod}", [bark_material, leaves_material])
               for lod, (data, _) in enumerate(lod_data)]
    stump_data = stump(spec, bark_tile)
    stump_obj = stump_data.build(f"SM_tree_{name}_stump", [bark_material, leaves_material])
    low, high = bounds(lod_data[0][0])

    # 4. 빌보드
    print(progress_line(15, total, "빌보드 렌더 중"), flush=True)
    for obj in objects[1:] + [stump_obj]:
        obj.hide_render = True
    billboard_data, billboard_stats = billboard(scene, objects[0], low, high, out, name)
    billboard_material = make_material(f"M_tree_{name}_billboard",
                                       {"color": image(out / billboard_stats["files"]["basecolor"], False),
                                        "normal": image(out / billboard_stats["files"]["normal"], True),
                                        "alphaFromColor": True})
    billboard_obj = billboard_data.build(f"SM_tree_{name}_billboard", [billboard_material])

    # 5. 내보내기와 확인
    print(progress_line(16, total, "FBX 내보내고 다시 읽는 중"), flush=True)
    files = {"fbx": f"SM_tree_{name}.fbx", "billboardFbx": f"SM_tree_{name}_billboard.fbx",
             "stumpFbx": f"SM_tree_{name}_stump.fbx", "glb": f"SM_tree_{name}_LOD0.glb"}
    export_fbx(objects, out / files["fbx"])
    export_fbx([billboard_obj], out / files["billboardFbx"])
    export_fbx([stump_obj], out / files["stumpFbx"])
    bpy.ops.object.select_all(action="DESELECT")
    objects[0].select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.export_scene.gltf(filepath=str(out / files["glb"]), export_format="GLB", use_selection=True,
                              export_yup=True, export_image_format="AUTO")
    expected = {obj.name: {"triangles": triangles(obj), "size": list(obj.dimensions)} for obj in objects}
    problems, readback = check_fbx(out / files["fbx"], expected)
    p2, r2 = check_fbx(out / files["billboardFbx"], {billboard_obj.name: {"triangles": triangles(billboard_obj),
                                                                         "size": list(billboard_obj.dimensions),
                                                                         "zeroColor": True}})
    p3, r3 = check_fbx(out / files["stumpFbx"], {stump_obj.name: {"triangles": triangles(stump_obj),
                                                                 "size": list(stump_obj.dimensions), "zeroColor": True}})
    problems += p2 + p3
    readback.update(r2)
    readback.update(r3)

    # 6. 검수 렌더
    print(progress_line(17, total, "검수 렌더 중"), flush=True)
    extras = review_scene(scene)
    camera_data = bpy.data.cameras.new("review")
    camera_data.lens = 50
    camera_data.clip_end = 1000
    camera = bpy.data.objects.new("review", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    for obj in objects[1:] + [stump_obj, billboard_obj]:
        obj.hide_render = True
    objects[0].hide_render = False
    span = max(high[2], float(np.max(high[:2] - low[:2])))
    look_at(camera, (-span * 1.05, -span * 1.35, span * 0.42), (0, 0, high[2] * 0.47))
    render_png(scene, out / "hero.png", 900, 900, 64)
    Image.open(out / "hero.png").convert("RGB").resize((512, 512), Image.LANCZOS).save(out / "preview.png")
    # LOD 줄: LOD0·1·2·빌보드·그루터기를 나란히 놓고 정사영으로(크기를 그대로 비교).
    gap = max(high[0] - low[0], high[1] - low[1]) * 1.08
    lineup = objects + [billboard_obj, stump_obj]
    for k, obj in enumerate(lineup):
        obj.hide_render = False
        obj.location = ((k - 2) * gap, 0, 0)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = gap * 5.05
    camera.location = (0, -200, (high[2] + 0.6) / 2)
    camera.rotation_euler = (math.radians(90), 0, 0)
    width_px = 2000
    render_png(scene, out / "lods.png", width_px, int(width_px * (high[2] + 0.6) / (gap * 5.05)), 48)
    camera_data.type = "PERSP"
    for obj in lineup:
        obj.location = (0, 0, 0)
        obj.hide_render = True
    # 바람 정점색 R·G·B·A
    objects[0].hide_render = False
    for extra in extras:
        extra.hide_render = True
    original = [slot.material for slot in objects[0].material_slots]
    panels = []
    look_at(camera, (0, -span * 1.9, high[2] * 0.5), (0, 0, high[2] * 0.48))
    for channel, text in enumerate(("R 줄기 휨", "G 가지 휨", "B 잎 떨림", "A 위상")):
        material = wind_material(channel)
        for slot in objects[0].material_slots:
            slot.material = material
        # 잎 카드는 알파를 빼면 판으로 보여 위치가 잘 읽힌다.
        render_png(scene, out / "renders" / f"wind_{channel}.png", 400, 520, 8)
        panels.append(label(Image.open(out / "renders" / f"wind_{channel}.png").convert("RGB"), text))
    for slot, material in zip(objects[0].material_slots, original):
        slot.material = material
    sheet = Image.new("RGB", (1600, 520))
    for k, panel in enumerate(panels):
        sheet.paste(panel, (400 * k, 0))
    sheet.save(out / "wind.png")

    t = {f"LOD{lod}": triangles(obj) for lod, obj in enumerate(objects)}
    t.update({"billboard": triangles(billboard_obj), "stump": triangles(stump_obj)})
    dbh = float(trunk_radius(spec, np.array([1.3]), height)[0] * 2)
    report = {
        "name": name, "heightMeters": round(float(high[2]), 3), "dbhMeters": round(dbh, 3),
        "crownWidthMeters": [round(float(high[0] - low[0]), 3), round(float(high[1] - low[1]), 3)],
        "triangles": t, "wood": [w.triangles() for w in wood_lods],
        "woodByLevel": [{str(k): v for k, v in sorted(w.by_level.items())} for w in wood_lods],
        "cards": [n for _, n in lod_data],
        "cardCandidates": candidates, "branches": {str(level): sum(1 for b in branches if b.level == level)
                                                   for level in sorted({b.level for b in branches})},
        "lodRatios": [round(t[f"LOD{lod}"] / t["LOD0"], 3) for lod in range(3)],
        "boundsUnity": unity_bounds(low, high), "barkTileMeters": list(bark_tile), "atlas": leaf_files,
        "neonFraction": leaf_stats["neonFraction"], "leafMeanColorSrgb": leaf_stats["meanColorSrgb"],
        "atlasCards": cards_result["cards"], "cardMeters": {k: [round(v, 3) for v in s] for k, s in sizes.items()},
        "billboard": billboard_stats, "files": files, "readback": readback, "exportProblems": problems,
        "unityAxes": "Unity (x, y, z) = Blender (X, Z, Y); pivot = trunk base on the ground (y = 0)",
        "device": device, "seconds": round(time.monotonic() - started, 2),
    }
    board(out, report, {"leaves": leaf_files, "billboard": billboard_stats["files"]})
    (out / "tree.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(total, total, f"LOD0 {t['LOD0']:,} · LOD1 {t['LOD1']:,} · LOD2 {t['LOD2']:,}"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
