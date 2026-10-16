"""Modular building pieces on a game grid, all mapped onto one trim-sheet atlas (``kit_atlas``).

Blender axes: X right, Y the piece's front, Z up. With the FBX export below Unity reads Blender
(X, Y, Z) as (x, z, y), so Blender +Y becomes the prefab's +Z (front) without a rotation. The origin
is the cell center (an edge center for walls) at the level's floor height, where the game places
pieces. Sizes follow the game's grid: ``--cell`` meters, ``--story`` meters per level, walls
``--wall-thickness`` thick and one thickness longer than a cell so corners close.

Every board maps onto one plank row of the atlas with U along its length (U wraps), so planks run
on across pieces. Corner posts are square and map the same on all four sides, so posts that two
pieces share at a corner draw identically instead of flickering. Rails, plates and roof rows end at
the cell boundary so neighbors meet without overlapping faces.

Outputs per piece: ``SM_<name>_<piece>.fbx`` (game) and ``.glb`` (preview textures), a 3/4 preview,
and ``kit.json`` with triangles, Unity bounds and the export check; plus ``house.png`` (the pieces
assembled the way the game places them), ``pieces.png`` and ``review.png``.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import zlib
from pathlib import Path

import numpy as np

from . import progress_line

PIECES = ("floor", "wall", "wall_door", "wall_window", "roof", "roof_slope", "stairs", "railing", "fence", "fence_gate")
DOOR = (1.6, 2.4)              # width, height (game: the player passes through)
WINDOW = (1.6, 1.0, 2.2)       # width, sill, head
FLOOR_THICKNESS = 0.1
RAIL_HEIGHT = 1.0
STAIR_STEPS, STAIR_LANDING, STAIR_HALF_WIDTH = 12, 0.4, 1.0
FRAME = 0.12                   # door and window frame width
BEVEL = 0.008


def _v(*values):
    from mathutils import Vector

    return Vector(values)


class Atlas:
    """UV placement in the trim sheet: U along a board's length (meters / period), V inside a region."""

    def __init__(self, layout: dict):
        self.period = layout["periodMeters"]
        regions = layout["regions"]
        self.rows = regions["planks"]["rows"]
        self.rows_meters = regions["planks"]["rowMeters"]
        self.timber = regions["timber"]
        self.shingle_bands = regions["shingles"]["bands"]
        self.margin = 6 / layout["height"]
        planks = regions["planks"]
        per_m = (planks["v"][1] - planks["v"][0]) / planks["metersTall"]
        edges = sorted({v for row in self.rows for v in row})
        # The rows sit edge to edge in the atlas, so one face can run across several boards.
        self.planks = {"lo": edges[0], "hi": edges[-1], "perMeter": per_m, "edges": edges}
        shingles = regions["shingles"]
        band_edges = sorted({v for band in self.shingle_bands for v in band})
        self.shingles = {"lo": band_edges[0], "hi": band_edges[-1],
                         "perMeter": (shingles["v"][1] - shingles["v"][0]) / shingles["metersTall"], "edges": band_edges}

    def span_meters(self, span: dict) -> float:
        return (span["hi"] - span["lo"]) / span["perMeter"]

    def row(self, index: int, width: float):
        v0, v1 = self.rows[index % len(self.rows)]
        return v0, (v1 - v0) / width

    def region(self, region: dict, width: float, offset: float):
        """V base and scale for a face ``width`` meters wide at ``offset`` (0..1) of the free range."""
        v0, v1 = region["v"]
        scale = (v1 - v0) / region["metersTall"]
        free = max(0.0, (v1 - v0) - width * scale - 2 * self.margin)
        return v0 + self.margin + free * offset, scale

    def band(self, index: int, width: float):
        """A shingle band read from the row's top edge down, so the darker butt lands at the lower edge."""
        v0, v1 = self.shingle_bands[index % len(self.shingle_bands)]
        return v1, -(v1 - v0) / width


class Builder:
    """One piece: oriented boxes with trim-sheet UVs in a bmesh."""

    def __init__(self, atlas: Atlas, rng: np.random.Generator):
        import bmesh

        self.atlas, self.rng = atlas, rng
        self.bm = bmesh.new()
        self.uv = self.bm.loops.layers.uv.new("UVMap")

    def box(self, center, axes, size, v_map, *, u0: float = 0.0, faces=("+a", "-a", "+b", "-b", "+n", "-n"),
            material: int = 0, bevel: bool = False, u_period: float | None = None, edge_v_map=None):
        """Box at ``center`` with unit ``axes`` (a length, b width, n thickness) and ``size`` (L, W, T).

        Long faces map U along ``a`` from ``u0`` meters and V = base + across * scale, ``v_map`` giving
        (base, scale); ``across`` runs from the face's left edge as seen from outside, so a square post
        reads the same from every side. End faces take a small patch of the same mapping. ``u_period``
        is the meters one U spans (the atlas period by default); ``edge_v_map`` maps the thin ``±b``
        faces elsewhere (a shingle's butt into its band's shadow).
        """
        period = u_period or self.atlas.period
        a, b, n = (_v(*axis).normalized() for axis in axes)
        c = _v(*center)
        half = {"a": size[0] / 2, "b": size[1] / 2, "n": size[2] / 2}
        vec = {"a": a, "b": b, "n": n}
        base, scale = v_map
        made = []
        for key in faces:
            sign, axis = (1 if key[0] == "+" else -1), key[1]
            d = vec[axis] * sign
            p_axis, q_axis = [k for k in "abn" if k != axis]
            p, q = vec[p_axis], vec[q_axis]
            if p.cross(q).dot(d) < 0:
                p_axis, q_axis, p, q = q_axis, p_axis, q, p
            corners = [c + d * half[axis] + p * (sp * half[p_axis]) + q * (sq * half[q_axis])
                       for sp, sq in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            verts = [self.bm.verts.new(corner) for corner in corners]
            face = self.bm.faces.new(verts)
            face.material_index = material
            if axis == "a":
                right = n.cross(d).normalized() if abs(n.cross(d).length) > 1e-6 else b
                up, width = n, size[2]
                origin = c + d * half["a"] - right * half["b"] - up * half["n"]
                for loop in face.loops:
                    rel = loop.vert.co - origin
                    loop[self.uv].uv = ((rel.dot(right) + u0) / period, base + rel.dot(up) * scale)
            else:
                right = a.cross(d).normalized()
                width = size[1] if axis == "n" else size[2]
                start = c - a * half["a"] - right * (width / 2)
                face_base, face_scale = edge_v_map if (edge_v_map and axis == "b") else (base, scale)
                for loop in face.loops:
                    rel = loop.vert.co - start
                    loop[self.uv].uv = ((rel.dot(a) + u0) / period, face_base + rel.dot(right) * face_scale)
            made.append(face)
        if bevel:
            import bmesh

            edges = list({edge for face in made for edge in face.edges})
            bmesh.ops.remove_doubles(self.bm, verts=list({v for f in made for v in f.verts}), dist=1e-6)
            edges = [edge for edge in {e for f in made if f.is_valid for e in f.edges} if len(edge.link_faces) == 2]
            bmesh.ops.bevel(self.bm, geom=edges, offset=BEVEL, segments=1, affect="EDGES", profile=0.5)
        return made

    # Typed helpers ---------------------------------------------------------------------------------

    def timber(self, center, axes, size, *, offset=None, u0=None, faces=("+a", "-a", "+b", "-b", "+n", "-n"), bevel=False):
        offset = self.rng.uniform() if offset is None else offset
        u0 = self.rng.uniform(0, self.atlas.period) if u0 is None else u0
        widest = max(size[1], size[2])
        return self.box(center, axes, size, self.atlas.region(self.atlas.timber, widest, offset), u0=u0, faces=faces,
                        bevel=bevel)

    def post(self, x, y, z0, z1, width):
        """Square vertical post; the same on all sides so coincident posts of two pieces agree."""
        self.timber(((x, y, (z0 + z1) / 2)), ((0, 0, 1), (1, 0, 0), (0, 1, 0)), (z1 - z0, width, width),
                    offset=0.37, u0=0.0, bevel=True)

    def panel(self, origin, u_axis, v_axis, u_len, v_len, span: dict, *, v_scale: float = 1.0, u_period=None,
              u0: float = 0.0, start=None, top=None, material: int = 0):
        """A flat face of boards: U along ``u_axis`` (their length), V across through ``span``'s rows.

        The face lies in the plane of ``u_axis`` and ``v_axis`` from ``origin``, facing their cross
        product. It is cut only where the rows run out and start again, which is a board edge, so
        each part maps without a wrap. ``v_scale`` widens the boards; ``top(v)`` gives the length
        along ``u_axis`` at ``v`` for a sloped far edge (gables).
        """
        o, u, w = _v(*origin), _v(*u_axis).normalized(), _v(*v_axis).normalized()
        period = u_period or self.atlas.period
        per_m = span["perMeter"] / v_scale
        v_at = span["edges"][int(self.rng.integers(len(span["edges"]) - 1))] if start is None else start
        reach = (lambda v: u_len) if top is None else top
        made, v = [], 0.0
        while v < v_len - 1e-6:
            room = (span["hi"] - v_at) / per_m
            step = min(room, v_len - v)
            if step > 1e-6:
                ua, ub = reach(v), reach(v + step)
                points = [(0.0, v), (ua, v), (ub, v + step), (0.0, v + step)]
                if ua < 1e-4:
                    points = [(0.0, v), (ub, v + step), (0.0, v + step)]
                elif ub < 1e-4:
                    points = [(0.0, v), (ua, v), (0.0, v + step)]
                verts = [self.bm.verts.new(o + u * pu + w * pv) for pu, pv in points]
                face = self.bm.faces.new(verts)
                face.material_index = material
                for loop, (pu, pv) in zip(face.loops, points):
                    loop[self.uv].uv = ((pu + u0) / period, v_at + (pv - v) * per_m)
                made.append(face)
            v += step
            v_at = span["lo"] if step >= room - 1e-9 else v_at + step * per_m
        return made

    def board(self, center, axes, size, *, row=None, u0=None, faces=("+n", "-b")):
        row = int(self.rng.integers(len(self.atlas.rows))) if row is None else row
        u0 = self.rng.uniform(0, self.atlas.period) if u0 is None else u0
        return self.box(center, axes, size, self.atlas.row(row, size[1]), u0=u0, faces=faces)

    def finish(self, name: str, materials):
        import bpy

        mesh = bpy.data.meshes.new(name)
        self.bm.normal_update()
        self.bm.to_mesh(mesh)
        self.bm.free()
        for material in materials:
            mesh.materials.append(material)
        for polygon in mesh.polygons:
            polygon.use_smooth = False
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        return obj


# Pieces --------------------------------------------------------------------------------------------

X, Y, Z = (1, 0, 0), (0, 1, 0), (0, 0, 1)


# A wall's boards are a little wider than the scanned planks.
WALL_BOARD_SCALE = 1.5


def wall_faces(k: Builder, x0, x1, z0, z1, holes, face):
    """Vertical boards on both wall faces as panels, broken at the openings' sides.

    Above and below an opening the panels of one column share their rows, so a board runs on
    past the window.
    """
    edges = sorted({x0, x1, *[x for hole in holes for x in hole[:2] if x0 < x < x1]})
    for a, b in zip(edges[:-1], edges[1:]):
        middle = (a + b) / 2
        spans = [(z0, z1)]
        for hx0, hx1, hz0, hz1 in holes:
            if hx0 < middle < hx1:
                spans = [piece for s0, s1 in spans for piece in ((s0, min(s1, hz0)), (max(s0, hz1), s1))
                         if piece[1] - piece[0] > 0.03]
        for side in (1, -1):
            start = k.atlas.planks["edges"][int(k.rng.integers(len(k.atlas.planks["edges"]) - 1))]
            u0 = k.rng.uniform(0, k.atlas.period)
            for s0, s1 in spans:
                # Front: boards up along Z, across +X (facing +Y); back: across -X (facing -Y).
                origin = (a, side * face, s0) if side > 0 else (b, side * face, s0)
                k.panel(origin, Z, (side, 0, 0), s1 - s0, b - a, k.atlas.planks, v_scale=WALL_BOARD_SCALE,
                        u0=u0 + s0, start=start)


def wall(k: Builder, cell, story, thickness, opening=None):
    half = thickness / 2
    frame = thickness + 0.05          # posts, sill and frames stand out of the boards
    inner = cell / 2 - frame / 2
    face = half - 0.015
    for x in (-cell / 2, cell / 2):
        k.post(x, 0, 0, story, frame)
    holes, sill_gaps = [], []
    if opening == "door":
        w, h = DOOR
        holes.append((-w / 2 - FRAME, w / 2 + FRAME, 0.0, h + FRAME))
        sill_gaps.append((-w / 2 - FRAME, w / 2 + FRAME))
    elif opening == "window":
        w, sill, head = WINDOW
        holes.append((-w / 2 - FRAME, w / 2 + FRAME, sill - FRAME, head + FRAME))
    # Sill beam, cut at a doorway; top plate; the girt across both faces at door-head height.
    spans = [(-inner, inner)]
    for g0, g1 in sill_gaps:
        spans = [piece for s0, s1 in spans for piece in ((s0, min(s1, g0)), (max(s0, g1), s1)) if piece[1] - piece[0] > 0.05]
    for s0, s1 in spans:
        k.timber(((s0 + s1) / 2, 0, 0.125), (X, Z, Y), (s1 - s0, 0.25, frame), faces=("+n", "-n", "+b"), bevel=True)
    k.timber((0, 0, story - 0.1), (X, Z, Y), (2 * inner, 0.2, thickness + 0.03), faces=("+n", "-n", "+b", "-b"), bevel=True)
    for side in (1, -1):
        k.timber((0, side * (face + 0.025), 2.475), (X, Z, (0, side, 0)), (2 * inner, 0.15, 0.05),
                 faces=("+n", "+b", "-b"), bevel=True)
    wall_faces(k, -inner, inner, 0.25, story - 0.2, holes, face)
    depth = thickness + 0.06
    if opening == "door":
        w, h = DOOR
        for x in (-w / 2 - FRAME / 2, w / 2 + FRAME / 2):
            k.timber((x, 0, h / 2), (Z, X, Y), (h, FRAME, depth), faces=("+n", "-n", "+b", "-b"), bevel=True)
        k.timber((0, 0, h + FRAME / 2), (X, Z, Y), (w + 2 * FRAME, FRAME, depth), faces=("+n", "-n", "-b"), bevel=True)
    elif opening == "window":
        w, sill, head = WINDOW
        for x in (-w / 2 - FRAME / 2, w / 2 + FRAME / 2):
            k.timber((x, 0, (sill + head) / 2), (Z, X, Y), (head - sill + 2 * FRAME, FRAME, depth),
                     faces=("+n", "-n", "+b", "-b"), bevel=True)
        k.timber((0, 0, head + FRAME / 2), (X, Z, Y), (w + 2 * FRAME, FRAME, depth), faces=("+n", "-n", "-b"), bevel=True)
        k.timber((0, 0, sill - FRAME / 2), (X, Z, Y), (w + 2 * FRAME + 0.16, FRAME, depth + 0.08), bevel=True)
        k.timber((0, 0, (sill + head) / 2), (Z, X, Y), (head - sill, 0.05, 0.06), faces=("+n", "-n", "+b", "-b"))
        k.timber((0, 0, (sill + head) / 2), (X, Z, Y), (w, 0.05, 0.06), faces=("+n", "-n", "+b", "-b"))
        # Glass: its own material slot, both sides.
        k.box((0, 0, (sill + head) / 2), (X, Z, Y), (w, head - sill, 0.01), (0.0, 0.0), faces=("+n", "-n"), material=1)


def tiling(k: Builder, cell: float, span: dict) -> tuple[float, float]:
    """U period and V widening that repeat a whole number of times per cell, so neighbors continue."""
    u_period = cell / max(1, round(cell / k.atlas.period))
    rounds = max(1, round(cell / k.atlas.span_meters(span)))
    return u_period, cell / (rounds * k.atlas.span_meters(span))


def deck(k: Builder, cell, z_top, thickness, *, roofing=False):
    """A floor or flat roof: one top face over rim beams and joists, boards across the underside.

    The top repeats a whole number of times per cell and starts at a row edge, so a neighboring
    deck carries the same boards (or shingles) on across the seam, and no rim shows on top.
    """
    half = cell / 2
    skin = 0.03
    z_under = z_top - skin
    rim = 0.1
    for y in (-half + rim / 2, half - rim / 2):
        k.timber((0, y, z_under / 2 + (z_top - thickness) / 2), (X, Z, Y), (cell, z_under - (z_top - thickness), rim),
                 faces=("+n", "-n", "-b"))
    for x in (-half + rim / 2, half - rim / 2):
        k.timber((x, 0, z_under / 2 + (z_top - thickness) / 2), (Y, Z, X), (cell - 2 * rim, z_under - (z_top - thickness), rim),
                 faces=("+n", "-n", "-b"))
    for x in np.linspace(-half + rim + 0.5, half - rim - 0.5, 3):
        k.timber((x, 0, z_under - 0.03), (Y, Z, X), (cell - 2 * rim, 0.06, 0.08), faces=("+n", "-n", "-b"))
    span = k.atlas.shingles if roofing else k.atlas.planks
    u_period, v_scale = tiling(k, cell, span)
    k.panel((-half, -half, z_top), X, Y, cell, cell, span, v_scale=v_scale, u_period=u_period, start=span["lo"])
    planks_period, planks_scale = tiling(k, cell, k.atlas.planks)
    k.panel((-half, half, z_under), X, (0, -1, 0), cell, cell, k.atlas.planks, v_scale=planks_scale,
            u_period=planks_period, start=k.atlas.planks["lo"])
    # The deck's edge between the top and the rim beams.
    for y, sign in ((-half, -1), (half, 1)):
        k.box((0, y, z_top - skin / 2), (X, Z, (0, sign, 0)), (cell, skin, 0.0),
              k.atlas.region(k.atlas.timber, skin, 0.2), faces=("+n",), u_period=u_period)
    for x, sign in ((-half, -1), (half, 1)):
        k.box((x, 0, z_top - skin / 2), (Y, Z, (sign, 0, 0)), (cell, skin, 0.0),
              k.atlas.region(k.atlas.timber, skin, 0.2), faces=("+n",), u_period=u_period)


def roof_slope(k: Builder, cell, story, thickness):
    half = cell / 2
    rise = cell / 2
    pitch = math.atan2(rise, cell)
    along = np.array([0.0, math.cos(pitch), math.sin(pitch)])    # up the slope
    normal = np.array([0.0, -math.sin(pitch), math.cos(pitch)])  # out of the roof
    rafter = 0.14
    eave = 0.25
    sheathing = 0.02

    def at(y, lift):
        """Point on the rafter top line above ``y`` (wall-top based), lifted ``lift`` along the normal."""
        base = np.array([0.0, y, story + rafter + (y + half) * rise / cell])
        return base + normal * lift

    # Rafters from the eave to the ridge, seen from below. A rafter is cut square, so it stops
    # short of the ridge by the depth its lower edge would reach past it.
    start, stop = at(-half - eave, 0.0), at(half, 0.0)
    length = float(np.linalg.norm(stop - start))
    short = rafter * math.tan(pitch)
    for x in np.linspace(-half + 0.06, half - 0.06, 5):
        center = (start + stop - along * short) / 2 - normal * (rafter / 2)
        k.timber((x, center[1], center[2]), (tuple(along), X, tuple(normal)), (length - short, 0.08, rafter),
                 faces=("+b", "-b", "-n"))
    fascia = at(-half - eave, -rafter / 2) - along * 0.015
    k.timber((0, fascia[1], fascia[2]), (X, tuple(normal), tuple(-along)), (cell, rafter + 0.05, 0.03),
             faces=("+n", "-b"), bevel=True)
    # Boards across the underside of the sheathing, repeating with the cell like the floor.
    planks_period, planks_scale = tiling(k, cell, k.atlas.planks)
    k.panel((-half, stop[1], stop[2]), X, tuple(-along), cell, length, k.atlas.planks, v_scale=planks_scale,
            u_period=planks_period, start=k.atlas.planks["lo"])
    # Shingle rows, each tilted a little more than the roof so its butt stands proud. A row's U
    # repeats twice per cell and the same in every piece, so neighbors' rows run on unbroken.
    exposure, lap, shingle = 0.2, 0.1, 0.02
    rows = int(math.ceil((length + 0.05) / exposure))
    tilt = math.atan2(shingle, exposure + lap)
    row_along = along * math.cos(tilt) - normal * math.sin(tilt)
    row_normal = np.cross(np.array(X, dtype=float), row_along)
    u_period = cell / max(1, round(cell / k.atlas.period))
    for i in range(rows):
        s0 = i * exposure - 0.05
        span = min(exposure + lap, length - s0)
        if span < 0.03:
            break
        butt = start + along * s0 + normal * (sheathing + 0.004)
        center = butt + row_along * (span / 2) + row_normal * (shingle / 2)
        band = k.atlas.shingle_bands[i % len(k.atlas.shingle_bands)]
        v_map = k.atlas.band(i, exposure + lap)
        k.box(tuple(center), (X, tuple(row_along), tuple(row_normal)), (cell, span, shingle), v_map,
              u0=k.rng.uniform(0, u_period), u_period=u_period, faces=("+n", "-b", "+a", "-a"),
              edge_v_map=(band[0] + 0.0015, 0.0))
    # Half of the ridge cap, lying on this side of the ridge line.
    top = at(half, sheathing + shingle * 2 + 0.04)
    top[1] = half
    cap = top - along * 0.08 - normal * 0.02
    k.timber((0, cap[1], cap[2]), (X, tuple(along), tuple(normal)), (cell, 0.16, 0.04), faces=("+n", "-b", "+a", "-a"),
             u0=0.0)
    for x_side in (-1, 1):
        x = x_side * half
        face = thickness / 2 - 0.015
        # Gable: boards up from the wall top to the rafters on both faces, the top edge on the slope.
        for side in (1, -1):
            # The panel at x + side * face faces side * X: across -Y from the ridge side faces +X.
            origin = (x + side * face, half if side > 0 else -half, story)
            v_dir = (0, -1, 0) if side > 0 else (0, 1, 0)
            if side > 0:
                reach = lambda v: max(0.0, (cell - v) * rise / cell)  # noqa: E731
            else:
                reach = lambda v: max(0.0, v * rise / cell)  # noqa: E731
            k.panel(origin, Z, v_dir, rise, cell, k.atlas.planks, v_scale=WALL_BOARD_SCALE, top=reach)
        # Barge board along the gable edge. Its top stops just under the shingle rows, so a neighbor's
        # roof covers it, while it still closes the slot between the boards underneath and the shingles.
        height = rafter + 0.1
        b0, b1 = at(-half - eave, -height / 2 + sheathing - 0.002), at(half, -height / 2 + sheathing - 0.002)
        mid = (b0 + b1) / 2
        k.timber((x + x_side * (thickness / 2 + 0.015), mid[1], mid[2]), (tuple(along), tuple(normal), (x_side, 0, 0)),
                 (float(np.linalg.norm(b1 - b0)), height, 0.03), faces=("+n", "-n", "+b", "-b", "-a"), bevel=True)
        # A short upright at the apex closes the notch where two barge boards meet.
        apex_top, apex_low = at(half, 0.08)[2], at(half, -height)[2]
        k.timber((x + x_side * (thickness / 2 + 0.015), half - 0.05, (apex_top + apex_low) / 2), (Z, Y, (x_side, 0, 0)),
                 (apex_top - apex_low, 0.1, 0.034), faces=("+n", "-n", "-b", "+a"))


def stairs(k: Builder, cell, story):
    top = story + FLOOR_THICKNESS
    run = cell - STAIR_LANDING
    depth = run / (STAIR_STEPS - 1)
    rise = top / STAIR_STEPS
    half = STAIR_HALF_WIDTH
    tread = 0.05
    per_m = k.atlas.planks["perMeter"]
    edges = k.atlas.planks["edges"]

    def plank_map(width):
        # A tread is a few scanned planks side by side: start at a row edge with room for its depth.
        fits = [edge for edge in edges if edge + width * per_m <= k.atlas.planks["hi"]]
        return fits[int(k.rng.integers(len(fits)))], per_m

    for i in range(STAIR_STEPS - 1):
        y0 = -cell / 2 + depth * i
        k.box((0, y0 + depth / 2 + 0.01, (i + 1) * rise - tread / 2), (X, Y, Z), (2 * half - 0.12, depth + 0.02, tread),
              plank_map(depth + 0.02), u0=k.rng.uniform(0, k.atlas.period), faces=("+n", "-n", "+b", "-b"))
    k.box((0, cell / 2 - STAIR_LANDING / 2, top - tread / 2), (X, Y, Z), (2 * half, STAIR_LANDING, tread),
          plank_map(STAIR_LANDING), u0=k.rng.uniform(0, k.atlas.period), faces=("+n", "-n", "+b", "-b", "+a", "-a"))
    pitch = math.atan2(top, run)
    along = np.array([0.0, math.cos(pitch), math.sin(pitch)])
    normal = np.array([0.0, -math.sin(pitch), math.cos(pitch)])
    start = np.array([0.0, -cell / 2, 0.0])
    length = math.hypot(run, top)
    for x in (-half + 0.03, half - 0.03):
        center = start + along * (length / 2) - normal * 0.06 + np.array([0.0, 0.0, 0.16])
        k.timber((x, center[1], center[2]), (tuple(along), tuple(normal), X), (length, 0.24, 0.06),
                 faces=("+n", "-n", "+b", "-b", "-a"), bevel=True)
    for x in (-half + 0.06, half - 0.06):
        k.post(x, cell / 2 - 0.06, 0, top - tread, 0.1)


def rail_line(k: Builder, cell, post_height, post_width, rails, *, front=0.0, ends=True):
    for i, x in enumerate(np.linspace(-cell / 2, cell / 2, int(round(cell)) + 1)):
        if ends or 0 < i < int(round(cell)):
            k.post(x, 0, 0, post_height, post_width)
    for z, height, depth in rails:
        k.timber((0, front, z), (X, Z, Y), (cell, height, depth), faces=("+n", "-n", "+b", "-b"))


def fence_gate(k: Builder, cell):
    for x in (-cell / 2, cell / 2):
        k.post(x, 0, 0, 1.3, 0.16)
    leaf = cell / 2 - 0.15
    for side in (-1, 1):
        hinge = np.array([side * (cell / 2 - 0.1), 0.0, 0.0])
        angle = math.radians(-side * 70)
        out = np.array([-side * math.cos(angle), -side * math.sin(angle), 0.0])  # from the hinge toward the gap
        face = np.array([-out[1], out[0], 0.0])
        for z in (0.4, 0.85):
            c = hinge + out * (leaf / 2) + np.array([0, 0, z])
            k.timber(tuple(c), (tuple(out), Z, tuple(face)), (leaf, 0.16, 0.05))
        for t in (0.05, leaf - 0.05):
            c = hinge + out * t + np.array([0, 0, 0.62]) + face * 0.03
            k.timber(tuple(c), (Z, tuple(out), tuple(face)), (0.7, 0.08, 0.04))
        diagonal = np.array([0, 0, 0.85 - 0.4])
        brace_along = out * (leaf - 0.1) + diagonal
        brace_len = float(np.linalg.norm(brace_along))
        c = hinge + out * (leaf / 2) + np.array([0, 0, 0.625]) + face * 0.03
        b_axis = brace_along / brace_len
        b_side = np.cross(face, b_axis)
        k.timber(tuple(c), (tuple(b_axis), tuple(b_side), tuple(face)), (brace_len, 0.08, 0.04))


def build_piece(piece: str, k: Builder, cell: float, story: float, thickness: float):
    if piece == "floor":
        deck(k, cell, FLOOR_THICKNESS, FLOOR_THICKNESS)
    elif piece == "wall":
        wall(k, cell, story, thickness)
    elif piece == "wall_door":
        wall(k, cell, story, thickness, "door")
    elif piece == "wall_window":
        wall(k, cell, story, thickness, "window")
    elif piece == "roof":
        deck(k, cell, story + 0.2, 0.2, roofing=True)
    elif piece == "roof_slope":
        roof_slope(k, cell, story, thickness)
    elif piece == "stairs":
        stairs(k, cell, story)
    elif piece == "railing":
        rail_line(k, cell, RAIL_HEIGHT, 0.12, ((RAIL_HEIGHT - 0.05, 0.1, 0.14), (RAIL_HEIGHT * 0.5, 0.07, 0.08)))
    elif piece == "fence":
        rail_line(k, cell, 1.2, 0.14, ((0.4, 0.16, 0.05), (0.85, 0.16, 0.05)), front=0.095)
    elif piece == "fence_gate":
        fence_gate(k, cell)
    else:
        raise SystemExit(f"모르는 부품: {piece}")


# Materials, export and renders ---------------------------------------------------------------------

def kit_material(name: str, folder: Path, files: dict):
    import bpy

    material = bpy.data.materials.new(f"M_{name}_kit")
    tree = material.node_tree
    principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")

    def image(key, colorspace, x):
        node = tree.nodes.new("ShaderNodeTexImage")
        path = folder / files[key]
        node.image = bpy.data.images.load(str(path), check_existing=True)
        node.image.colorspace_settings.name = colorspace
        node.location = (x, 0)
        return node

    tree.links.new(image("basecolor", "sRGB", -700).outputs["Color"], principled.inputs["Base Color"])
    tree.links.new(image("roughness", "Non-Color", -700).outputs["Color"], principled.inputs["Roughness"])
    principled.inputs["Metallic"].default_value = 0.0
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(image("normal", "Non-Color", -900).outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
    from .game_bake import _occlusion_group

    group = tree.nodes.new("ShaderNodeGroup")
    group.node_tree = _occlusion_group()
    tree.links.new(image("occlusion", "Non-Color", -700).outputs["Color"], group.inputs["Occlusion"])
    return material


def glass_material():
    import bpy

    material = bpy.data.materials.new("M_glass")
    principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    principled.inputs["Base Color"].default_value = (0.08, 0.1, 0.11, 1.0)
    principled.inputs["Roughness"].default_value = 0.05
    principled.inputs["Transmission Weight"].default_value = 0.6
    material.diffuse_color = (0.08, 0.1, 0.11, 0.6)
    return material


def unity_bounds(obj) -> dict:
    corners = [obj.matrix_world @ _v(*corner) for corner in obj.bound_box]
    low = [min(c[i] for c in corners) for i in range(3)]
    high = [max(c[i] for c in corners) for i in range(3)]
    # Unity (x, y, z) = Blender (X, Z, Y) with this export.
    return {"min": [round(low[0], 4), round(low[2], 4), round(low[1], 4)],
            "max": [round(high[0], 4), round(high[2], 4), round(high[1], 4)]}


def triangles(obj) -> int:
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def export(obj, folder: Path, stem: str) -> dict:
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    fbx, glb = folder / f"{stem}.fbx", folder / f"{stem}.glb"
    bpy.ops.export_scene.fbx(filepath=str(fbx), use_selection=True, object_types={"MESH"},
                             apply_scale_options="FBX_SCALE_UNITS", axis_forward="-Z", axis_up="Y",
                             bake_space_transform=True, mesh_smooth_type="FACE", use_tspace=True,
                             path_mode="STRIP", embed_textures=False)
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True, export_yup=True,
                              export_image_format="JPEG", export_jpeg_quality=88)
    return {"fbx": fbx.name, "glb": glb.name}


def check_export(path: Path, triangles_expected: int, dimensions) -> list[str]:
    """Read a written file back: the FBX with Blender's importer, the GLB with trimesh (Blender's glTF
    importer would take this scene's occlusion node group for its own settings node)."""
    import bpy

    problems = []
    if path.suffix == ".fbx":
        before = set(bpy.data.objects)
        bpy.ops.import_scene.fbx(filepath=str(path))
        added = [obj for obj in bpy.data.objects if obj not in before]
        meshes = [obj for obj in added if obj.type == "MESH"]
        found = sum(triangles(obj) for obj in meshes)
        corners = [obj.matrix_world @ _v(*c) for obj in meshes for c in obj.bound_box]
        size = sorted(round(max(c[i] for c in corners) - min(c[i] for c in corners), 3) for i in range(3)) if corners else []
        for obj in added:
            bpy.data.objects.remove(obj, do_unlink=True)
    else:
        import trimesh

        scene = trimesh.load(path, force="scene")
        found = sum(len(geometry.faces) for geometry in scene.dump())
        size = sorted(round(float(extent), 3) for extent in scene.extents)
    expected = sorted(round(float(d), 3) for d in dimensions)
    if found != triangles_expected:
        problems.append(f"{path.name} 면 수 {found} ≠ {triangles_expected}")
    if len(size) != 3 or any(abs(a - b) > 0.003 for a, b in zip(size, expected)):
        problems.append(f"{path.name} 크기 {size} ≠ {expected}")
    return problems


def stage_scene(samples: int):
    import bpy

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    from .game_bake import _use_gpu

    _use_gpu(scene)
    scene.cycles.samples = samples
    scene.cycles.use_denoising = True
    scene.view_settings.view_transform = "AgX"
    world = bpy.data.worlds.new("sky") if scene.world is None else scene.world
    scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    sky = world.node_tree.nodes.new("ShaderNodeTexSky")
    sky.sky_type = "MULTIPLE_SCATTERING"
    sky.sun_disc = False
    sky.sun_elevation = math.radians(40)
    sky.sun_rotation = math.radians(215)
    world.node_tree.links.new(sky.outputs["Color"], background.inputs["Color"])
    background.inputs["Strength"].default_value = 0.3
    scene.view_settings.look = "AgX - Punchy"
    scene.view_settings.exposure = -0.9
    sun = bpy.data.lights.new("sun", "SUN")
    sun.energy = 5.0
    sun.angle = math.radians(2.5)
    sun.color = (1.0, 0.95, 0.86)
    sun_obj = bpy.data.objects.new("sun", sun)
    sun_obj.rotation_euler = (math.radians(52), 0, math.radians(-18))
    scene.collection.objects.link(sun_obj)
    return scene


def ground(size=1200.0):
    import bpy

    mesh = bpy.data.meshes.new("ground")
    half = size / 2
    mesh.from_pydata([(-half, -half, -0.002), (half, -half, -0.002), (half, half, -0.002), (-half, half, -0.002)], [], [(0, 1, 2, 3)])
    material = bpy.data.materials.new("grass")
    principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    principled.inputs["Base Color"].default_value = (0.16, 0.3, 0.08, 1.0)
    principled.inputs["Roughness"].default_value = 0.95
    mesh.materials.append(material)
    obj = bpy.data.objects.new("ground", mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def camera_at(scene, location, target, lens=35.0):
    import bpy

    camera = bpy.data.cameras.new("camera")
    camera.lens = lens
    camera.clip_end = 500
    obj = bpy.data.objects.new("camera", camera)
    scene.collection.objects.link(obj)
    obj.location = location
    direction = _v(*target) - _v(*location)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = obj
    return obj


def place(obj, x, y, z=0.0, turns=0):
    """A copy of ``obj`` posed the way the game places a piece: Unity yaw ``turns`` × 90° clockwise."""
    import bpy

    copy = bpy.data.objects.new(f"{obj.name}.placed", obj.data)
    bpy.context.scene.collection.objects.link(copy)
    copy.location = (x, y, z)
    copy.rotation_euler = (0, 0, -math.radians(90 * turns))
    return copy


def render(scene, path: Path, width: int, height: int):
    import bpy

    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.resolution_percentage = 100
    scene.render.filepath = str(path)
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)


def house(objects: dict, cell: float):
    """Two cells deep, one wide: a doorway in front, windows, a gable roof, a fence and gate."""
    placed = []
    c = cell
    for y in (0, c):
        placed.append(place(objects["floor"], 0, y))
    placed.append(place(objects["wall_door"], 0, -c / 2))
    placed.append(place(objects["wall_window"], 0, c * 1.5))
    placed.append(place(objects["wall"], -c / 2, 0, turns=1))
    placed.append(place(objects["wall_window"], -c / 2, c, turns=1))
    placed.append(place(objects["wall_window"], c / 2, 0, turns=1))
    placed.append(place(objects["wall"], c / 2, c, turns=1))
    placed.append(place(objects["roof_slope"], 0, 0))
    placed.append(place(objects["roof_slope"], 0, c, turns=2))
    placed.append(place(objects["fence"], -c, -c * 1.5))
    placed.append(place(objects["fence_gate"], 0, -c * 1.5))
    placed.append(place(objects["fence"], c, -c * 1.5))
    placed.append(place(objects["stairs"], c * 2, 0))
    placed.append(place(objects["roof"], c * 2, c, z=0.0))
    placed.append(place(objects["railing"], c * 2, c * 1.5, z=3.0))
    return placed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", required=True, type=Path, help="folder with layout.json and the maps")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--pieces", default=",".join(PIECES))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--cell", type=float, default=4.0)
    parser.add_argument("--story", type=float, default=3.0)
    parser.add_argument("--wall-thickness", type=float, default=0.25)
    parser.add_argument("--reference", type=Path, help="concept image shown next to the assembled house")
    parser.add_argument("--samples", type=int, default=64)
    args = parser.parse_args(argv)

    import bpy

    started = time.monotonic()
    pieces = [piece for piece in args.pieces.split(",") if piece]
    total = len(pieces) + 3
    layout = json.loads((args.atlas / "layout.json").read_text("utf-8"))
    atlas = Atlas(layout)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    args.output.mkdir(parents=True, exist_ok=True)
    kit = kit_material(args.name, args.atlas, layout["files"])
    glass = glass_material()
    objects, stats = {}, {}
    for i, piece in enumerate(pieces):
        print(progress_line(i, total, f"{piece} 만드는 중"), flush=True)
        rng = np.random.default_rng(args.seed + zlib.crc32(piece.encode()))
        k = Builder(atlas, rng)
        build_piece(piece, k, args.cell, args.story, args.wall_thickness)
        obj = k.finish(f"SM_{args.name}_{piece}", [kit, glass] if piece == "wall_window" else [kit])
        objects[piece] = obj
        stem = f"SM_{args.name}_{piece}"
        files = export(obj, args.output, stem)
        dims = tuple(obj.dimensions)
        problems = check_export(args.output / files["fbx"], triangles(obj), dims) + \
            check_export(args.output / files["glb"], triangles(obj), dims)
        stats[piece] = {"triangles": triangles(obj), "boundsUnity": unity_bounds(obj), "files": files,
                        "materials": [slot.material.name for slot in obj.material_slots], "exportProblems": problems}
    print(progress_line(len(pieces), total, "부품 미리보기 렌더 중"), flush=True)
    scene = stage_scene(max(16, args.samples // 2))
    floor_plane = ground()
    for obj in objects.values():
        obj.hide_render = True
    previews = {}
    for piece, obj in objects.items():
        obj.hide_render = False
        b = unity_bounds(obj)
        center = [(b["min"][0] + b["max"][0]) / 2, (b["min"][2] + b["max"][2]) / 2, (b["min"][1] + b["max"][1]) / 2]
        span = max(b["max"][i] - b["min"][i] for i in range(3))
        cam = camera_at(scene, (center[0] - span * 1.1, center[1] - span * 1.45, center[2] + span * 0.75), center, lens=40)
        path = args.output / f"preview_{piece}.png"
        render(scene, path, 512, 512)
        previews[piece] = path.name
        bpy.data.objects.remove(cam, do_unlink=True)
        obj.hide_render = True
    print(progress_line(len(pieces) + 1, total, "조립한 집 렌더 중"), flush=True)
    scene.cycles.samples = args.samples
    placed = house(objects, args.cell) if set(PIECES) <= set(objects) else []
    if placed:
        camera_at(scene, (-11.5, -15.5, 5.2), (0.4, 1.0, 2.0), lens=32)
        render(scene, args.output / "house.png", 1600, 1000)
    for obj in placed:
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.objects.remove(floor_plane, do_unlink=True)

    print(progress_line(len(pieces) + 2, total, "검수판 만드는 중"), flush=True)
    board(args.output, previews, args.reference, layout)
    report = {"pieces": stats, "previews": previews, "house": "house.png" if placed else None, "pieceSheet": "pieces.png",
              "review": "review.png", "grid": {"cell": args.cell, "story": args.story, "wallThickness": args.wall_thickness},
              "unityAxes": "Unity (x, y, z) = Blender (X, Z, Y); prefab front +Z = Blender +Y",
              "seconds": round(time.monotonic() - started, 2)}
    (args.output / "kit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(total, total, f"부품 {len(pieces)}개 · 삼각형 {sum(s['triangles'] for s in stats.values()):,}"), flush=True)
    return 0


def board(folder: Path, previews: dict, reference: Path | None, layout: dict):
    from PIL import Image, ImageDraw, ImageFont

    font_path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default()
    tiles = []
    for piece, name in previews.items():
        image = Image.open(folder / name).convert("RGB").resize((320, 320))
        ImageDraw.Draw(image).text((10, 8), piece, font=font, fill="white")
        tiles.append(image)
    columns = 5
    sheet = Image.new("RGB", (320 * columns, 320 * ((len(tiles) + columns - 1) // columns)), (34, 36, 40))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % columns) * 320, (i // columns) * 320))
    sheet.save(folder / "pieces.png")
    if not (folder / "house.png").is_file():
        sheet.save(folder / "review.png")
        return
    house_image = Image.open(folder / "house.png").convert("RGB")
    panels = [house_image]
    if reference and reference.is_file():
        ref = Image.open(reference).convert("RGB")
        ref.thumbnail((house_image.height, house_image.height))
        panels.insert(0, ref)
    width = sum(panel.width for panel in panels)
    top = Image.new("RGB", (width, house_image.height), (34, 36, 40))
    x = 0
    for panel in panels:
        top.paste(panel, (x, (house_image.height - panel.height) // 2))
        x += panel.width
    sheet_scaled = sheet.resize((width, int(sheet.height * width / sheet.width)))
    review = Image.new("RGB", (width, top.height + sheet_scaled.height), (34, 36, 40))
    review.paste(top, (0, 0))
    review.paste(sheet_scaled, (0, top.height))
    review.save(folder / "review.png")


if __name__ == "__main__":
    raise SystemExit(main())
