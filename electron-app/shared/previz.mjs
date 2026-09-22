// 프리비즈 장면·카메라 계산. 엔진(tools/previz_render.py)과 같은 식이어야
// 화면 지도에서 끈 카메라가 렌더 결과와 같은 자리에 선다. 브라우저에서도 돈다.

export const SENSOR_MM = 36;
export const ASPECT = 16 / 9;
const DEFAULTS = { distance: 1.8, azimuth: -35, height: 0.6, targetHeight: 0.5 };
const LIMITS = { distance: [0.2, 20], lens: [8, 300], seconds: [0.2, 60], size: [0.05, 500] };
export const SIZE_LABELS = ["가로", "깊이", "높이"];

const clamp = (value, [low, high]) => Math.min(high, Math.max(low, value));
const clone = (value) => JSON.parse(JSON.stringify(value));

export function smooth(start, end, fraction, ease = "inout") {
  const t = ease === "inout" ? fraction * fraction * (3 - 2 * fraction) : fraction;
  return start + (end - start) * t;
}

function pair(source, key, fallback) {
  const start = Number(source?.[key] ?? fallback);
  const end = source?.[`${key}End`];
  return [start, end === undefined || end === null || end === "" ? start : Number(end)];
}

// 엔진은 에셋의 바닥 중심을 배치 좌표에 두고 회전한 뒤 월드 경계 상자를 잰다.
export function footprint(item) {
  const scale = Number(item.scale) || 1;
  const [dx, dy, dz] = (item.dimensions || [1, 1, 1]).map((value) => Number(value) * scale);
  const yaw = ((Number(item.yaw) || 0) * Math.PI) / 180;
  const ex = Math.abs(dx * Math.cos(yaw)) + Math.abs(dy * Math.sin(yaw));
  const ey = Math.abs(dx * Math.sin(yaw)) + Math.abs(dy * Math.cos(yaw));
  const x = Number(item.x) || 0;
  const y = Number(item.y) || 0;
  return { low: [x - ex / 2, y - ey / 2, 0], high: [x + ex / 2, y + ey / 2, dz] };
}

function subjectOf(low, high) {
  return {
    center: [(low[0] + high[0]) / 2, (low[1] + high[1]) / 2],
    baseZ: low[2],
    width: Math.max(high[0] - low[0], high[1] - low[1], 0.05),
    height: Math.max(high[2] - low[2], 0.05),
    radius: Math.max(Math.hypot(high[0] - low[0], high[1] - low[1], high[2] - low[2]) / 2, 0.05),
  };
}

export function assetId(index) {
  return index === 0 ? "hero" : `asset${index + 1}`;
}

// 배치 순서가 바뀌면 에셋 id(asset2…)가 가리키는 물체도 바뀐다. order[새 자리] = 옛 자리로 받아
// 컷이 같은 물체를 계속 겨냥하게 옮긴다. hero·scene은 역할이라 그대로 두고, 빠진 물체를 보던 컷은 장면 전체를 본다.
export function refocusCuts(cuts, order) {
  const now = new Map(order.map((old, index) => [old, index]));
  return cuts.map((cut) => {
    const match = /^asset(\d+)$/.exec(cut.focus || "");
    if (!match) return cut;
    const index = now.get(Number(match[1]) - 1);
    return { ...cut, focus: index === undefined ? "scene" : assetId(index) };
  });
}

// 새로 놓는 것이 이미 놓인 것과 겹치지 않는 가장 가까운 오른쪽 자리. 건물처럼 큰 대역도 비켜 선다.
export function freeSpot(placed, dimensions, gap = 0.3) {
  const [width, depth] = (dimensions || [1, 1]).map(Number);
  const hits = (x) => placed.some((item) => {
    const { low, high } = footprint(item);
    return x - width / 2 < high[0] + gap && x + width / 2 > low[0] - gap && depth / 2 > low[1] - gap && -depth / 2 < high[1] + gap;
  });
  let x = 0;
  while (hits(x)) x += 0.5;
  return [x, 0];
}

export function sceneSubjects(placed) {
  const boxes = placed.map((item, index) => ({ id: assetId(index), ...footprint(item) }));
  const subjects = Object.fromEntries(boxes.map((box) => [box.id, subjectOf(box.low, box.high)]));
  if (boxes.length) {
    const low = [0, 1, 2].map((axis) => Math.min(...boxes.map((box) => box.low[axis])));
    const high = [0, 1, 2].map((axis) => Math.max(...boxes.map((box) => box.high[axis])));
    subjects.scene = subjectOf(low, high);
  }
  return subjects;
}

// 피사체가 화면을 꽉 채우는 거리. 프레이밍 거리 1.0이 이 거리다.
export function fitDistance(lens, subject, aspect = ASPECT) {
  return Math.max((subject.width * lens) / SENSOR_MM, (subject.height * lens * aspect) / SENSOR_MM);
}

export function focusOf(cut, subjects) {
  return subjects[cut.focus || "scene"] || subjects.scene || null;
}

export function isMoving(cut) {
  const framing = cut.framing || {};
  return Object.keys(framing).some((key) => key.endsWith("End")) || (cut.lensEnd != null && cut.lensEnd !== cut.lens);
}

export function cameraAt(cut, subjects, fraction = 0, aspect = ASPECT) {
  const subject = focusOf(cut, subjects);
  if (!subject) return null;
  const framing = cut.framing || {};
  const ease = cut.ease || "inout";
  const lens = smooth(...pair(cut, "lens", 35), fraction, ease);
  const distance = smooth(...pair(framing, "distance", DEFAULTS.distance), fraction, ease) * fitDistance(lens, subject, aspect);
  const azimuth = (smooth(...pair(framing, "azimuth", DEFAULTS.azimuth), fraction, ease) * Math.PI) / 180;
  const [cx, cy] = subject.center;
  const offset = framing.targetOffset || [0, 0];
  return {
    lens,
    position: [
      cx + distance * Math.sin(azimuth),
      cy - distance * Math.cos(azimuth),
      subject.baseZ + smooth(...pair(framing, "height", DEFAULTS.height), fraction, ease) * subject.height,
    ],
    target: [
      cx + offset[0] * subject.radius,
      cy + offset[1] * subject.radius,
      subject.baseZ + smooth(...pair(framing, "targetHeight", DEFAULTS.targetHeight), fraction, ease) * subject.height,
    ],
  };
}

// 지도에서 끈 점을 방향·거리 값으로 되돌린다. 방향은 원래 값과 가장 가까운 각을 골라
// 궤도 촬영이 반대쪽으로 한 바퀴 돌지 않게 한다.
export function aimFromPoint(cut, subjects, point, handle = "start", aspect = ASPECT) {
  const subject = focusOf(cut, subjects);
  if (!subject) return cut;
  const framing = { ...(cut.framing || {}) };
  const end = handle === "end";
  const lens = end ? pair(cut, "lens", 35)[1] : pair(cut, "lens", 35)[0];
  const dx = point[0] - subject.center[0];
  const dy = point[1] - subject.center[1];
  const previous = end ? pair(framing, "azimuth", DEFAULTS.azimuth)[1] : pair(framing, "azimuth", DEFAULTS.azimuth)[0];
  const raw = (Math.atan2(dx, -dy) * 180) / Math.PI;
  const turn = ((((raw - previous) % 360) + 540) % 360) - 180;
  const azimuth = Math.round(previous + turn);
  const distance = Math.round(clamp(Math.hypot(dx, dy) / fitDistance(lens, subject, aspect), LIMITS.distance) * 20) / 20;
  if (end) {
    framing.azimuthEnd = azimuth;
    framing.distanceEnd = distance;
  } else {
    framing.azimuth = azimuth;
    framing.distance = distance;
  }
  return { ...cut, framing };
}

export function draftFromPreset(preset) {
  return clone(preset?.shots || []).map((shot) => ({ ...shot, framing: shot.framing || {} }));
}

export function moveItem(list, from, to) {
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(Math.max(0, Math.min(next.length, to)), 0, item);
  return next;
}

export function nextShotId(cuts) {
  const used = new Set(cuts.map((cut) => cut.id));
  for (let index = 1; ; index += 1) {
    const id = `s${String(index).padStart(2, "0")}`;
    if (!used.has(id)) return id;
  }
}

export function totalSeconds(cuts) {
  return cuts.reduce((sum, cut) => sum + Number(cut.seconds || 0), 0);
}

export function setSeconds(cut, seconds) {
  return { ...cut, seconds: Math.round(clamp(Number(seconds) || 0, LIMITS.seconds) * 10) / 10 };
}

// 사람이 고친 장면과 컷을 엔진 요청 하나로 바꾼다. 프리셋 그대로면 컷을 보내지 않아
// 기록에 "편집"이 붙지 않는다.
export function buildPrevizRequest({ placed = [], cuts = [], preset = null, settings = {}, description = "" }) {
  if (!placed.length) throw new Error("장면에 3D 에셋이나 대역을 하나 이상 놓아 주세요.");
  if (!cuts.length) throw new Error("컷이 하나 이상 있어야 합니다.");
  const assets = placed.map((item, index) => {
    const number = (value, name, [low, high]) => {
      const parsed = Number(String(value ?? "").trim() || 0);
      if (!Number.isFinite(parsed) || parsed < low || parsed > high) {
        throw new Error(`${index + 1}번째 ${item.standin ? "대역" : "에셋"}의 ${name} 값을 확인해 주세요.`);
      }
      return parsed;
    };
    const placement = {
      id: assetId(index),
      position: [number(item.x, "x", [-1000, 1000]), number(item.y, "y", [-1000, 1000]), 0],
      yaw: number(item.yaw, "회전", [-360, 360]),
    };
    if (item.standin) {
      // 대역은 크기 배율 대신 미터 치수를 보낸다. 엔진이 부품을 그 치수로 늘인다.
      const size = SIZE_LABELS.map((name, axis) => number(item.dimensions?.[axis], name, LIMITS.size));
      return { ...placement, standin: item.standin, size };
    }
    return {
      ...placement,
      source: { jobId: item.jobId, assetId: item.assetId },
      scale: number(item.scale ?? 1, "크기", [0.01, 100]),
    };
  });
  const edited = JSON.stringify(cuts) !== JSON.stringify(draftFromPreset(preset));
  const [width, height] = String(settings.resolution || "960x540").split("x").map(Number);
  return {
    recipe: "previz",
    params: {
      ...(description.trim() ? { description: description.trim() } : {}),
      preset: preset?.id, assets, ...(edited ? { shots: clone(cuts) } : {}),
      renderer: settings.renderer || "eevee", width, height, fps: Number(settings.fps || 12),
      aux: settings.aux || "keys", clay: settings.clay !== false, animatic: settings.animatic !== false,
    },
  };
}
