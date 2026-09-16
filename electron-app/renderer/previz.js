import { REVIEW_LABELS, currentStage, escapeHtml, fileUrl, formatDuration, isActive } from "../shared/format.mjs";
import {
  ASPECT, aimFromPoint, assetId, buildPrevizRequest, cameraAt, draftFromPreset, footprint, isMoving,
  moveItem, nextShotId, sceneSubjects, setSeconds, totalSeconds,
} from "../shared/previz.mjs";

const DRAFT_KEY = "assets-studio.previz.draft";
const LAYOUT_KEY = "assets-studio.previz.layout";
const MOVES = ["static", "dolly-in", "dolly-out", "push-in", "orbit", "pan", "crane-up", "crane-down"];
const FIELDS = [
  { key: "lens", label: "렌즈", unit: "mm", min: 14, max: 135, step: 1, tip: "숫자가 작을수록 넓게, 클수록 좁고 가깝게 보입니다." },
  { key: "distance", label: "거리", unit: "×", min: 0.3, max: 6, step: 0.05, framing: true, tip: "1.0이면 피사체가 화면을 꽉 채웁니다. 2.0은 그 두 배 거리입니다." },
  { key: "azimuth", label: "방향", unit: "°", min: -180, max: 180, step: 1, framing: true, tip: "0°는 정면(지도 아래쪽), 90°는 오른쪽입니다." },
  { key: "height", label: "카메라 높이", unit: "×", min: 0, max: 3, step: 0.05, framing: true, tip: "피사체 높이의 배수입니다. 0이면 바닥, 1이면 피사체 꼭대기 높이입니다." },
  { key: "targetHeight", label: "바라보는 높이", unit: "×", min: 0, max: 1.5, step: 0.05, framing: true, tip: "카메라가 겨냥하는 높이입니다. 피사체 높이의 배수입니다." },
];

const clone = (value) => JSON.parse(JSON.stringify(value));
const hue = (index) => (index * 53 + 28) % 360;
const shortTitle = (title) => String(title || "").replace(/^3D( 소품)? · /, "");
const storage = {
  read(key) {
    try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; }
  },
  write(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 저장이 막혀도 화면은 동작한다 */ }
  },
};

function timecode(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${(value % 60).toFixed(1).padStart(4, "0")}`;
}

export function createPreviz({ api, bridge, getJobs, getPresets, refreshJobs, openAsset, goCreate }) {
  const $ = (selector, root = document) => root.querySelector(selector);
  const layout = { scene: true, cuts: true, render: false, panel: true, ...storage.read(LAYOUT_KEY) };
  const state = {
    meshes: [],
    placed: [],
    presetId: null,
    cuts: [],
    settings: null,
    selectedAsset: -1,
    selectedCut: 0,
    expandedCut: -1,
    renderId: null,
    inspectShot: null,
    view: { center: [0, 0], reach: 3, auto: true },
    drag: null,
    error: "",
    submitting: false,
    keys: { screen: "", timeline: "", inspector: "", bar: "" },
  };

  // ---- 데이터 ---------------------------------------------------------------

  const presets = () => getPresets()?.previz?.shotPresets || [];
  const preset = () => presets().find((item) => item.id === state.presetId) || presets()[0] || null;
  const renders = () => getJobs().filter((job) => job.recipe === "previz");
  const rendering = () => renders().find(isActive) || null;
  const selectedRender = () => renders().find((job) => job.id === state.renderId) || renders()[0] || null;
  const renderCuts = (job) => (job?.assets || [])
    .filter((asset) => asset.kind === "shot")
    .sort((a, b) => (a.meta?.order || 0) - (b.meta?.order || 0));
  const subjects = () => sceneSubjects(state.placed);
  const edited = () => JSON.stringify(state.cuts) !== JSON.stringify(draftFromPreset(preset()));

  function defaults() {
    const base = getPresets()?.previz?.defaults || {};
    return {
      renderer: base.renderer || "eevee",
      resolution: `${base.width || 960}x${base.height || 540}`,
      fps: String(base.fps || 12),
      aux: base.aux || "keys",
      clay: base.clay !== false,
      animatic: base.animatic !== false,
    };
  }

  function saveDraft() {
    storage.write(DRAFT_KEY, {
      placed: state.placed, presetId: state.presetId, cuts: state.cuts, settings: state.settings,
    });
  }

  function restoreDraft() {
    const saved = storage.read(DRAFT_KEY);
    state.settings = { ...defaults(), ...(saved?.settings || {}) };
    state.presetId = presets().some((item) => item.id === saved?.presetId) ? saved.presetId : presets()[0]?.id || null;
    state.cuts = Array.isArray(saved?.cuts) && saved.cuts.length ? saved.cuts : draftFromPreset(preset());
    state.placed = Array.isArray(saved?.placed) ? saved.placed : [];
  }

  function syncPlacedWithMeshes() {
    const known = new Map(state.meshes.map((mesh) => [`${mesh.jobId}:${mesh.id}`, mesh]));
    state.placed = state.placed.filter((item) => known.has(`${item.jobId}:${item.assetId}`));
    for (const item of state.placed) {
      const mesh = known.get(`${item.jobId}:${item.assetId}`);
      item.dimensions = mesh.meta?.stats?.dimensionsMeters || item.dimensions || [1, 1, 1];
      item.preview = mesh.preview;
      item.label = shortTitle(mesh.jobTitle);
    }
    if (state.selectedAsset >= state.placed.length) state.selectedAsset = state.placed.length - 1;
  }

  function changed({ map = true, cuts = false, summary = true } = {}) {
    saveDraft();
    if (map) renderMap();
    if (cuts) renderCutList();
    if (summary) renderSummaries();
    renderBar();
  }

  // ---- 장면 -----------------------------------------------------------------

  function placeAsset(jobId, id, point = null) {
    const mesh = state.meshes.find((item) => item.jobId === jobId && item.id === id);
    if (!mesh) return;
    let [x, y] = point || [0, 0];
    if (!point) {
      // 가운데가 차 있으면 겹치지 않게 오른쪽으로 비켜 놓는다.
      while (state.placed.some((item) => Math.hypot(item.x - x, item.y - y) < 1)) x += 1.2;
    }
    state.placed.push({
      jobId, assetId: id, label: shortTitle(mesh.jobTitle), preview: mesh.preview,
      dimensions: mesh.meta?.stats?.dimensionsMeters || [1, 1, 1],
      x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10, yaw: 0, scale: 1,
    });
    state.selectedAsset = state.placed.length - 1;
    if (state.view.auto) fitView();
    changed({ cuts: true });
    renderTray();
    renderAssetInspector();
  }

  function removeAsset(index) {
    state.placed.splice(index, 1);
    state.selectedAsset = Math.min(state.selectedAsset, state.placed.length - 1);
    changed({ cuts: true });
    renderTray();
    renderAssetInspector();
  }

  function renderTray() {
    const tray = $("#pv-tray");
    if (!state.meshes.length) {
      tray.innerHTML = `<div class="pv-tray-empty">
        <p>아직 3D 에셋이 없습니다.</p>
        <button class="secondary" type="button" data-pv="go-create">만들기에서 3D 에셋 만들기</button>
      </div>`;
      return;
    }
    const counts = new Map();
    for (const item of state.placed) counts.set(`${item.jobId}:${item.assetId}`, (counts.get(`${item.jobId}:${item.assetId}`) || 0) + 1);
    tray.innerHTML = state.meshes.map((mesh) => {
      const key = `${mesh.jobId}:${mesh.id}`;
      const count = counts.get(key) || 0;
      const dims = mesh.meta?.stats?.dimensionsMeters;
      const size = dims ? ` · ${dims.map((value) => Number(value).toFixed(1)).join("×")}m` : "";
      return `<button class="pv-tray-item${count ? " is-placed" : ""}" type="button" draggable="true"
          data-pv="place" data-asset="${escapeHtml(key)}"
          data-tip="${escapeHtml(`${shortTitle(mesh.jobTitle)}${size}\n끌어서 지도에 놓거나 눌러서 가운데에 놓기`)}">
        ${mesh.preview ? `<img src="${fileUrl(mesh.jobId, mesh.preview)}" alt="" draggable="false">` : ""}
        <span class="pv-tray-name">${escapeHtml(shortTitle(mesh.jobTitle))}</span>
        ${count ? `<span class="pv-tray-count">${count > 1 ? `×${count}` : "✓"}</span>` : ""}
      </button>`;
    }).join("");
  }

  function fitView() {
    const points = [];
    for (const item of state.placed) {
      const box = footprint(item);
      points.push(box.low, box.high);
    }
    const cut = state.cuts[state.selectedCut];
    const scene = subjects();
    if (cut && state.placed.length) {
      for (const fraction of [0, 0.5, 1]) points.push(cameraAt(cut, scene, fraction)?.position || [0, 0]);
    }
    if (!points.length) {
      state.view = { center: [0, 0], reach: 3, auto: true };
      return;
    }
    const xs = points.map((point) => point[0]);
    const ys = points.map((point) => point[1]);
    state.view = {
      center: [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2],
      reach: Math.max(2.5, (Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) / 2) * 1.2),
      auto: true,
    };
  }

  function mapSvg() {
    const { center: [cx, cy], reach } = state.view;
    const scene = subjects();
    const unit = reach / 50;
    const grid = [];
    for (let x = Math.floor(cx - reach); x <= Math.ceil(cx + reach); x += 1) {
      grid.push(`<line class="${x === 0 ? "axis" : x % 5 === 0 ? "major" : ""}" x1="${x}" y1="${-(cy + reach)}" x2="${x}" y2="${-(cy - reach)}"></line>`);
    }
    for (let y = Math.floor(cy - reach); y <= Math.ceil(cy + reach); y += 1) {
      grid.push(`<line class="${y === 0 ? "axis" : y % 5 === 0 ? "major" : ""}" x1="${cx - reach}" y1="${-y}" x2="${cx + reach}" y2="${-y}"></line>`);
    }

    const cut = state.cuts[state.selectedCut];
    let cameras = "";
    if (cut && state.placed.length) {
      const start = cameraAt(cut, scene, 0);
      const end = cameraAt(cut, scene, 1);
      const path = Array.from({ length: 17 }, (_, index) => cameraAt(cut, scene, index / 16).position)
        .map(([x, y]) => `${x},${-y}`).join(" ");
      const cone = (camera) => {
        const [px, py] = camera.position;
        const [tx, ty] = camera.target;
        const heading = Math.atan2(ty - py, tx - px);
        const half = Math.atan(18 / camera.lens);
        const reachLength = Math.max(Math.hypot(tx - px, ty - py) * 1.15, unit * 20);
        const edge = (angle) => `${px + Math.cos(angle) * reachLength},${-(py + Math.sin(angle) * reachLength)}`;
        return `<polygon class="pv-cone" points="${px},${-py} ${edge(heading - half)} ${edge(heading + half)}"></polygon>`;
      };
      const handle = (camera, which) => {
        const [px, py] = camera.position;
        const [tx, ty] = camera.target;
        const degrees = (Math.atan2(-(ty - py), tx - px) * 180) / Math.PI;
        const size = unit * 4.2;
        return `<g class="pv-cam ${which}" data-drag="camera" data-handle="${which}"
            transform="translate(${px} ${-py}) rotate(${-degrees + 0})">
          <circle r="${size * 1.9}" class="pv-hit"></circle>
          <polygon points="${size * 1.3},0 ${-size},${-size} ${-size},${size}"></polygon>
        </g>`;
      };
      cameras = `${cone(start)}
        ${isMoving(cut) ? `<polyline class="pv-path" points="${path}"></polyline>${handle(end, "end")}` : ""}
        ${handle(start, "start")}`;
    }

    const assets = state.placed.map((item, index) => {
      const scale = Number(item.scale) || 1;
      const [dx, dy] = (item.dimensions || [1, 1, 1]).map((value) => Number(value) * scale);
      const x = Number(item.x) || 0;
      const y = Number(item.y) || 0;
      const selected = index === state.selectedAsset;
      const radius = Math.max(dx, dy) * 0.5 + unit * 7;
      const yaw = ((Number(item.yaw) || 0) * Math.PI) / 180;
      const hx = x - Math.sin(yaw) * radius;
      const hy = y + Math.cos(yaw) * radius;
      return `<g class="pv-asset${index === 0 ? " hero" : ""}${selected ? " is-selected" : ""}">
        <g data-drag="asset" data-index="${index}" transform="translate(${x} ${-y}) rotate(${-(Number(item.yaw) || 0)})">
          <rect x="${-dx / 2}" y="${-dy / 2}" width="${dx}" height="${dy}" rx="${Math.min(dx, dy) * 0.08}"></rect>
          <line class="pv-front" x1="0" y1="0" x2="0" y2="${-dy / 2}"></line>
        </g>
        <text x="${x}" y="${-y + dy / 2 + unit * 8}">${escapeHtml(index === 0 ? "주인공" : `#${index + 1}`)}</text>
        ${selected ? `<line class="pv-rotate-arm" x1="${x}" y1="${-y}" x2="${hx}" y2="${-hy}"></line>
          <circle class="pv-rotate" data-drag="rotate" data-index="${index}" cx="${hx}" cy="${-hy}" r="${unit * 3.4}"></circle>` : ""}
      </g>`;
    }).join("");

    const empty = state.placed.length ? "" : `<text class="pv-map-empty" x="${cx}" y="${-cy}">위의 에셋을 여기로 끌어다 놓으세요</text>`;
    return `<svg viewBox="${cx - reach} ${-(cy + reach)} ${reach * 2} ${reach * 2}" tabindex="0"
        role="application" aria-label="장면 배치 지도" style="--unit:${unit}">
      <g class="pv-grid">${grid.join("")}</g>
      ${cameras}${assets}${empty}
      <text class="pv-front-note" x="${cx - reach + unit * 3}" y="${-(cy - reach) - unit * 3}">▼ 정면 (카메라 기본 방향)</text>
    </svg>`;
  }

  function renderMap() {
    if (state.view.auto && !state.drag) fitView();
    $("#pv-map").innerHTML = mapSvg();
  }

  function worldPoint(event) {
    const svg = $("#pv-map svg");
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const local = point.matrixTransform(svg.getScreenCTM().inverse());
    return [local.x, -local.y];
  }

  function renderAssetInspector() {
    const item = state.placed[state.selectedAsset];
    const panel = $("#pv-asset");
    if (!item) {
      panel.innerHTML = state.placed.length
        ? `<p class="hint pv-asset-hint">지도에서 에셋을 누르면 위치·회전·크기를 숫자로 고칠 수 있습니다.</p>`
        : "";
      return;
    }
    const index = state.selectedAsset;
    const field = (key, label, step) => `<label class="pv-num"><span>${label}</span>
      <input type="number" step="${step}" data-asset-field="${key}" value="${escapeHtml(item[key])}"></label>`;
    panel.innerHTML = `<div class="pv-asset-card">
      <div class="pv-asset-head">
        ${item.preview ? `<img src="${fileUrl(item.jobId, item.preview)}" alt="">` : ""}
        <div><strong>${escapeHtml(item.label)}</strong><span>${index === 0 ? "주인공 · hero" : `에셋 #${index + 1}`}</span></div>
        ${index === 0 ? "" : `<button class="ghost" type="button" data-pv="make-hero" data-tip="이 에셋을 주인공으로 바꿉니다. hero 컷이 이 에셋을 겨냥합니다.">주인공으로</button>`}
        <button class="ghost is-danger" type="button" data-pv="remove-asset" data-tip="장면에서 뺍니다 (Delete)">제거</button>
      </div>
      <div class="pv-nums">${field("x", "x (m)", 0.1)}${field("y", "y (m)", 0.1)}${field("yaw", "회전 (°)", 5)}${field("scale", "크기", 0.1)}</div>
    </div>`;
  }

  // ---- 컷 -------------------------------------------------------------------

  function focusOptions(current) {
    const options = [["hero", "주인공"], ["scene", "장면 전체"],
      ...state.placed.slice(1).map((_, index) => [assetId(index + 1), `에셋 #${index + 2}`])];
    return options.map(([value, label]) =>
      `<option value="${value}"${value === current ? " selected" : ""}>${label}</option>`).join("");
  }

  function cutEditorHtml(cut, index) {
    const framing = cut.framing || {};
    const moving = isMoving(cut);
    const read = (field, end) => {
      const key = end ? `${field.key}End` : field.key;
      const source = field.framing ? framing : cut;
      const fallback = { lens: 35, distance: 1.8, azimuth: -35, height: 0.6, targetHeight: 0.5 }[field.key];
      return source[key] ?? (end ? source[field.key] : undefined) ?? fallback;
    };
    const slider = (field, end = false) => {
      const value = read(field, end);
      return `<label class="pv-slider" data-tip="${escapeHtml(field.tip)}">
        <span class="pv-slider-label">${field.label}${end ? " 끝" : ""}</span>
        <input type="range" min="${field.min}" max="${field.max}" step="${field.step}" value="${value}"
          data-cut-field="${field.key}" data-end="${end}" data-index="${index}">
        <output>${Number(value).toFixed(field.step < 1 ? 2 : 0)}${field.unit}</output>
      </label>`;
    };
    return `<div class="pv-cut-editor">
      <div class="pv-cut-row">
        <label class="pv-mini"><span>이름</span><input value="${escapeHtml(cut.label || "")}" data-cut-text="label" data-index="${index}"></label>
        <label class="pv-mini"><span>대상</span><select data-cut-text="focus" data-index="${index}">${focusOptions(cut.focus)}</select></label>
        <label class="pv-mini"><span>움직임</span><select data-cut-text="move" data-index="${index}">
          ${MOVES.map((move) => `<option${move === cut.move ? " selected" : ""}>${move}</option>`).join("")}
          ${MOVES.includes(cut.move) ? "" : `<option selected>${escapeHtml(cut.move || "static")}</option>`}
        </select></label>
      </div>
      <div class="pv-sliders">${FIELDS.map((field) => slider(field)).join("")}</div>
      <label class="pv-switch" data-tip="켜면 시작과 끝 값을 따로 정해 카메라가 컷 동안 움직입니다. 지도에 끝 카메라(빈 삼각형)가 생깁니다.">
        <input type="checkbox" data-cut-moving data-index="${index}"${moving ? " checked" : ""}><span>컷 동안 움직이기</span>
      </label>
      ${moving ? `<div class="pv-sliders is-end">${FIELDS.map((field) => slider(field, true)).join("")}</div>
        <label class="pv-switch" data-tip="처음과 끝을 부드럽게 가감속합니다. 끄면 일정한 속도로 움직입니다.">
          <input type="checkbox" data-cut-ease data-index="${index}"${cut.ease !== "linear" ? " checked" : ""}><span>부드럽게 가감속</span>
        </label>` : ""}
      <div class="pv-cut-foot">
        <button class="ghost is-danger" type="button" data-pv="delete-cut" data-index="${index}">이 컷 삭제</button>
      </div>
    </div>`;
  }

  function renderCutList() {
    const list = $("#pv-cutlist");
    list.innerHTML = state.cuts.map((cut, index) => {
      const selected = index === state.selectedCut;
      const expanded = index === state.expandedCut;
      return `<li class="pv-cut${selected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}" style="--cut-hue:${hue(index)}"
          data-index="${index}">
        <div class="pv-cut-line" draggable="true" data-pv="select-cut" data-index="${index}">
          <span class="pv-grip" data-tip="끌어서 순서 바꾸기">⋮⋮</span>
          <span class="pv-cut-id">${escapeHtml(cut.id)}</span>
          <span class="pv-cut-name">
            <strong>${escapeHtml(cut.label || cut.id)}</strong>
            <small>${escapeHtml(cut.move || "static")} · ${Math.round(Number(cut.lens) || 35)}mm</small>
          </span>
          <span class="pv-seconds">
            <button type="button" data-pv="seconds" data-delta="-0.5" data-index="${index}" data-tip="0.5초 줄이기" aria-label="0.5초 줄이기">−</button>
            <output>${Number(cut.seconds).toFixed(1)}초</output>
            <button type="button" data-pv="seconds" data-delta="0.5" data-index="${index}" data-tip="0.5초 늘리기" aria-label="0.5초 늘리기">+</button>
          </span>
          <button class="pv-expand" type="button" data-pv="expand-cut" data-index="${index}"
            aria-expanded="${expanded}" data-tip="${expanded ? "접기" : "카메라 값 펼치기"}">▾</button>
        </div>
        ${expanded ? cutEditorHtml(cut, index) : ""}
      </li>`;
    }).join("") || `<li class="pv-cut-empty">컷이 없습니다. 프리셋을 고르거나 되돌리기를 누르세요.</li>`;

    $("#pv-presets").innerHTML = presets().map((item) => `<button type="button"
        class="chip${item.id === state.presetId ? " is-active" : ""}" data-pv="preset" data-preset="${escapeHtml(item.id)}"
        data-tip="${escapeHtml(item.hint || item.label)}">${escapeHtml(item.label)}</button>`).join("");
    $("#pv-preset-hint").textContent = edited()
      ? "편집한 컷입니다. 프리셋을 다시 누르면 편집이 사라집니다."
      : preset()?.hint || "";
    $("#pv-cut-reset").hidden = !edited();
  }

  function updateCut(index, next, { list = false } = {}) {
    state.cuts[index] = next;
    changed({ cuts: list });
  }

  // ---- 요약·상단 ------------------------------------------------------------

  function renderSummaries() {
    const cuts = state.cuts.length;
    $("#pv-scene-summary").textContent = state.placed.length ? `에셋 ${state.placed.length}개` : "비어 있음";
    $("#pv-cuts-summary").textContent = cuts
      ? `${preset()?.label || ""}${edited() ? " 편집" : ""} · ${cuts}컷 ${totalSeconds(state.cuts).toFixed(1)}초`
      : "컷 없음";
    const { renderer, resolution, fps, clay } = state.settings;
    $("#pv-render-summary").textContent =
      `${{ eevee: "EEVEE", workbench: "Workbench", cycles: "Cycles" }[renderer]} · ${resolution.replace("x", "×")} · ${fps}fps${clay ? " · 점토" : ""}`;
  }

  function blocker() {
    if (!state.placed.length) return "장면에 3D 에셋을 먼저 놓아 주세요.";
    if (!state.cuts.length) return "컷이 하나 이상 있어야 합니다.";
    return "";
  }

  function renderBar() {
    const running = rendering();
    const stage = running ? currentStage(running) : null;
    const percent = Math.round((stage?.progress || 0) * 100);
    const reason = blocker();
    const button = $("#pv-render");
    button.disabled = Boolean(reason || running || state.submitting);
    button.classList.toggle("is-busy", Boolean(running || state.submitting));
    button.style.setProperty("--progress", `${percent}%`);
    button.textContent = state.submitting ? "요청 보내는 중" : running ? `렌더 중 ${percent}%` : "샷 만들기";
    button.setAttribute("data-tip", reason || (running
      ? `${stage?.detail || "준비 중"} · 한 번에 한 작업만 돌아갑니다`
      : `${state.cuts.length}컷 ${totalSeconds(state.cuts).toFixed(1)}초를 렌더합니다. 끝나면 가운데에서 한 편으로 재생됩니다.`));
    const note = $("#pv-cta-note");
    note.classList.toggle("is-error", Boolean(state.error));
    note.textContent = state.error || reason || `${state.cuts.length}컷 · ${totalSeconds(state.cuts).toFixed(1)}초`;

    const list = renders().slice(0, 8);
    const key = JSON.stringify([state.renderId, list.map((job) => [job.id, job.state])]);
    if (key === state.keys.bar) return;
    state.keys.bar = key;
    const active = selectedRender()?.id;
    $("#pv-renders").innerHTML = list.map((job, index) => {
      const time = new Date(job.createdAt).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
      const cuts = renderCuts(job).length;
      return `<button type="button" class="pv-pill${job.id === active ? " is-active" : ""} state-${escapeHtml(job.state)}"
          data-pv="render" data-job="${escapeHtml(job.id)}"
          data-tip="${escapeHtml(`${job.title}\n${job.params?.renderer || ""} · ${cuts}컷`)}">
        <i></i>${index === 0 ? "최근 " : ""}${escapeHtml(time)}${job.params?.edited ? " · 편집" : ""}
      </button>`;
    }).join("");
  }

  // ---- 무대 -----------------------------------------------------------------

  function stepsHtml() {
    const steps = [
      [state.placed.length > 0, "장면에 3D 에셋 놓기", "왼쪽 1번에서 에셋을 지도로 끌어다 놓습니다."],
      [state.cuts.length > 0, "컷 확인·조정", "2번에서 순서를 끌어 바꾸고, 지도에서 카메라를 끌어 옮깁니다."],
      [false, "샷 만들기", "오른쪽 위 단추를 누르면 여기에서 한 편으로 재생됩니다."],
    ];
    return `<div class="pv-empty">
      <h2>뼈대 장면으로 카메라를 먼저 잡습니다</h2>
      <ol>${steps.map(([done, title, text], index) => `<li class="${done ? "is-done" : ""}">
        <span>${done ? "✓" : index + 1}</span><div><strong>${title}</strong><p>${text}</p></div></li>`).join("")}</ol>
    </div>`;
  }

  function renderScreen() {
    const job = selectedRender();
    const cuts = renderCuts(job);
    const sequence = cuts.find((asset) => asset.meta?.sequence)?.meta.sequence;
    const stage = job ? currentStage(job) : null;
    const key = JSON.stringify([job?.id, job?.state, sequence?.file, isActive(job) ? Math.round((stage?.progress || 0) * 20) : 0, job?.error]);
    if (key === state.keys.screen) return;
    state.keys.screen = key;
    const screen = $("#pv-screen");
    const transport = $("#pv-transport");

    if (!job) {
      screen.innerHTML = stepsHtml();
      transport.innerHTML = "";
      return;
    }
    if (isActive(job)) {
      const percent = Math.round((stage?.progress || 0) * 100);
      screen.innerHTML = `<div class="pv-progress">
        <div class="pv-progress-ring" style="--progress:${percent}"><span>${percent}%</span></div>
        <strong>${escapeHtml(stage?.detail || "대기 중")}</strong>
        <p>${escapeHtml(job.title)}</p>
        <button class="ghost is-danger" type="button" data-pv="cancel" data-job="${escapeHtml(job.id)}">중지</button>
      </div>`;
      transport.innerHTML = "";
      return;
    }
    if (job.state !== "done") {
      screen.innerHTML = `<div class="pv-progress is-failed"><strong>렌더가 ${job.state === "cancelled" ? "중지됐습니다" : "실패했습니다"}</strong>
        <p>${escapeHtml(job.error || "")}</p></div>`;
      transport.innerHTML = "";
      return;
    }
    const poster = cuts[0] ? fileUrl(job.id, cuts[0].preview) : "";
    screen.innerHTML = sequence
      ? `<video id="pv-video" src="${fileUrl(job.id, sequence.file)}" poster="${poster}" preload="auto" playsinline muted></video>
         <button class="pv-play-overlay" type="button" data-pv="play" aria-label="재생" data-tip="재생 (Space)">▶</button>`
      : `<img class="pv-still" src="${poster}" alt="">
         <p class="pv-still-note">이 렌더에는 이어 붙인 영상이 없습니다. 애니매틱을 켜고 다시 만들면 한 편으로 재생됩니다.</p>`;

    const blend = cuts.find((asset) => asset.meta?.blendFile)?.meta.blendFile;
    transport.innerHTML = `
      <div class="pv-transport-main">
        <button class="pv-icon" type="button" data-pv="prev" data-tip="이전 컷 (←)" aria-label="이전 컷">⏮</button>
        <button class="pv-icon pv-play" type="button" data-pv="play" data-tip="재생·일시정지 (Space)" aria-label="재생">▶</button>
        <button class="pv-icon" type="button" data-pv="next" data-tip="다음 컷 (→)" aria-label="다음 컷">⏭</button>
        <span class="pv-time"><output id="pv-now">00:00.0</output> / ${timecode(sequence?.seconds || totalSeconds(cuts.map((asset) => asset.meta)))}</span>
        <span class="pv-now-cut" id="pv-now-cut"></span>
      </div>
      <div class="pv-transport-side">
        <button class="ghost" type="button" data-pv="load-render" data-tip="이 렌더의 배치와 컷을 왼쪽 편집으로 불러옵니다. 고친 뒤 다시 만들 수 있습니다.">편집으로 불러오기</button>
        ${bridge && blend ? `<button class="secondary" type="button" data-pv="open-blend" data-job="${escapeHtml(job.id)}" data-file="${escapeHtml(blend)}"
          data-tip="컷마다 CAM 카메라가 타임라인 마커에 묶인 장면을 Blender에서 엽니다">Blender에서 열기</button>` : ""}
      </div>`;
    state.keys.timeline = "";
    state.keys.inspector = "";
  }

  function renderTimeline() {
    const job = selectedRender();
    const cuts = job?.state === "done" ? renderCuts(job) : [];
    const key = JSON.stringify([job?.id, state.inspectShot, cuts.map((asset) => [asset.id, asset.review])]);
    if (key === state.keys.timeline) return;
    state.keys.timeline = key;
    const timeline = $("#pv-timeline");
    if (!cuts.length) {
      timeline.innerHTML = "";
      return;
    }
    const total = cuts.reduce((sum, asset) => sum + Number(asset.meta?.seconds || 0), 0) || 1;
    timeline.innerHTML = `<div class="pv-track" data-pv="scrub">
      ${cuts.map((asset, index) => {
        const meta = asset.meta || {};
        const at = meta.sequence?.at ?? cuts.slice(0, index).reduce((sum, item) => sum + Number(item.meta?.seconds || 0), 0);
        return `<button type="button" class="pv-seg is-${escapeHtml(asset.review)}${meta.shot === state.inspectShot ? " is-current" : ""}"
            style="flex:${Number(meta.seconds) / total};--cut-hue:${hue(index)}"
            data-pv="seek" data-at="${at}" data-shot="${escapeHtml(meta.shot)}"
            data-tip="${escapeHtml(`${meta.shot} ${meta.label || ""}\n${meta.move} · ${Math.round(meta.lens)}mm · ${meta.seconds}초\n${REVIEW_LABELS[asset.review]}`)}">
          <img src="${fileUrl(job.id, asset.preview)}" alt="" draggable="false">
          <span>${escapeHtml(meta.shot)}</span>
        </button>`;
      }).join("")}
      <i class="pv-playhead" id="pv-playhead"></i>
    </div>`;
    updatePlayhead();
  }

  function renderInspector() {
    const job = selectedRender();
    const cuts = job?.state === "done" ? renderCuts(job) : [];
    const asset = cuts.find((item) => item.meta?.shot === state.inspectShot) || cuts[0];
    const key = JSON.stringify([job?.id, asset?.id, asset?.review]);
    if (key === state.keys.inspector) return;
    state.keys.inspector = key;
    const panel = $("#pv-inspector");
    if (!asset) {
      panel.innerHTML = "";
      return;
    }
    const meta = asset.meta || {};
    const coords = (point) => point?.position?.map((value) => value.toFixed(2)).join(", ");
    const thumb = (file, label) => (file ? `<button class="pv-aux" type="button" data-pv="open-asset" data-job="${escapeHtml(job.id)}"
        data-asset="${escapeHtml(asset.id)}" data-tip="크게 보기">
      <img src="${fileUrl(job.id, file)}" alt=""><span>${label}</span></button>` : "");
    const review = (status, label) => `<button type="button" class="pv-review ${status}${asset.review === status ? " is-on" : ""}"
        data-pv="review" data-job="${escapeHtml(job.id)}" data-asset="${escapeHtml(asset.id)}" data-status="${status}"
        data-tip="${asset.review === status ? "다시 누르면 검토 대기로 돌립니다" : `이 컷을 ${label}합니다`}">${label}</button>`;
    panel.innerHTML = `
      <div class="pv-aux-row">
        ${thumb(meta.files?.key, "대표")}${thumb(meta.files?.depth?.[1] || meta.files?.depth?.[0], "깊이")}${thumb(meta.files?.line?.[1] || meta.files?.line?.[0], "윤곽")}
      </div>
      <div class="pv-inspect-body">
        <div class="pv-inspect-title"><span class="pv-cut-id" style="--cut-hue:${hue(cuts.indexOf(asset))}">${escapeHtml(meta.shot)}</span>
          <strong>${escapeHtml(meta.label || meta.shot)}</strong><small>${escapeHtml(meta.purpose || "")}</small></div>
        <div class="pv-chips">
          <span data-tip="카메라 움직임">${escapeHtml(meta.move)}</span>
          <span data-tip="렌즈">${Math.round(meta.lens)}${meta.lensEnd !== meta.lens ? `→${Math.round(meta.lensEnd)}` : ""}mm</span>
          <span data-tip="컷 길이">${meta.seconds}초 · ${meta.frames}f</span>
          <span data-tip="깊이 그림의 가까운·먼 거리 (m)">깊이 ${meta.depthRange?.map((value) => value.toFixed(1)).join("~")}m</span>
        </div>
        <dl class="pv-coords">
          <dt>카메라 시작</dt><dd>${coords(meta.start)}</dd>
          <dt>카메라 끝</dt><dd>${coords(meta.end)}</dd>
        </dl>
      </div>
      <div class="pv-inspect-actions">
        ${review("approved", "승인")}${review("rejected", "거절")}
        <button class="ghost" type="button" data-pv="cut-to-draft" data-job="${escapeHtml(job.id)}" data-asset="${escapeHtml(asset.id)}"
          data-tip="이 컷의 렌즈·길이·구도를 왼쪽 편집의 같은 컷에 덮어씁니다">편집에 반영</button>
      </div>`;
  }

  function video() {
    return $("#pv-video");
  }

  function updatePlayhead() {
    const player = video();
    const job = selectedRender();
    const cuts = renderCuts(job);
    const playhead = $("#pv-playhead");
    if (!player || !playhead || !cuts.length) return;
    const total = player.duration || cuts.reduce((sum, asset) => sum + Number(asset.meta?.seconds || 0), 0) || 1;
    playhead.style.left = `${Math.min(100, (player.currentTime / total) * 100)}%`;
    const now = $("#pv-now");
    if (now) now.textContent = timecode(player.currentTime);
    const current = [...cuts].reverse().find((asset) => (asset.meta?.sequence?.at ?? 0) <= player.currentTime + 0.001);
    if (current && current.meta.shot !== state.inspectShot) {
      state.inspectShot = current.meta.shot;
      renderTimeline();
      renderInspector();
    }
    const label = $("#pv-now-cut");
    if (label && current) label.textContent = `${current.meta.shot} ${current.meta.label || ""}`;
    for (const button of document.querySelectorAll('[data-pv="play"]')) {
      button.textContent = player.paused ? "▶" : "❚❚";
    }
    $(".pv-play-overlay")?.classList.toggle("is-hidden", !player.paused || player.currentTime > 0);
  }

  function seekShot(offset) {
    const cuts = renderCuts(selectedRender());
    const player = video();
    if (!player || !cuts.length) return;
    const index = Math.max(0, cuts.findIndex((asset) => asset.meta?.shot === state.inspectShot));
    const target = cuts[Math.min(cuts.length - 1, Math.max(0, index + offset))];
    player.currentTime = (target.meta?.sequence?.at ?? 0) + 0.01;
    state.inspectShot = target.meta.shot;
    renderTimeline();
    renderInspector();
    updatePlayhead();
  }

  function renderStage() {
    renderScreen();
    renderTimeline();
    renderInspector();
  }

  // ---- 동작 -----------------------------------------------------------------

  async function submit() {
    state.error = "";
    let request;
    try {
      request = buildPrevizRequest({ placed: state.placed, cuts: state.cuts, preset: preset(), settings: state.settings });
    } catch (error) {
      state.error = error.message;
      renderBar();
      return;
    }
    state.submitting = true;
    renderBar();
    try {
      const job = await api("/api/jobs", { method: "POST", body: request });
      state.renderId = job.id;
      state.inspectShot = null;
      await refreshJobs();
    } catch (error) {
      state.error = error.message;
    } finally {
      state.submitting = false;
      renderBar();
      renderStage();
    }
  }

  function loadRender(job) {
    const params = job.params || {};
    const byId = new Map((params.assets || []).map((asset) => [asset.id, asset]));
    state.placed = (params.assets || []).filter((asset) => asset.source).map((asset) => {
      const mesh = state.meshes.find((item) => item.jobId === asset.source.jobId && item.id === asset.source.assetId);
      return {
        jobId: asset.source.jobId, assetId: asset.source.assetId, label: shortTitle(mesh?.jobTitle || asset.label),
        preview: mesh?.preview, dimensions: mesh?.meta?.stats?.dimensionsMeters || [1, 1, 1],
        x: asset.position?.[0] || 0, y: asset.position?.[1] || 0, yaw: asset.yaw || 0, scale: asset.scale || 1,
      };
    });
    const heroId = params.hero || params.assets?.[0]?.id;
    state.presetId = params.preset || state.presetId;
    state.cuts = (params.shots || []).map((shot) => {
      const { order: _order, ...rest } = shot;
      const focus = shot.focus === heroId ? "hero" : byId.has(shot.focus)
        ? assetId((params.assets || []).findIndex((asset) => asset.id === shot.focus)) : shot.focus;
      return { ...rest, focus };
    });
    state.settings = {
      ...state.settings,
      renderer: params.renderer || state.settings.renderer,
      resolution: params.width ? `${params.width}x${params.height}` : state.settings.resolution,
      fps: String(params.fps || state.settings.fps),
      aux: params.aux || state.settings.aux,
      clay: params.clay ?? state.settings.clay,
      animatic: params.animatic ?? state.settings.animatic,
    };
    state.selectedAsset = -1;
    state.selectedCut = 0;
    state.expandedCut = -1;
    state.view.auto = true;
    saveDraft();
    renderEditor();
  }

  function cutToDraft(asset) {
    const meta = asset.meta || {};
    const job = selectedRender();
    const shot = (job?.params?.shots || []).find((item) => item.id === meta.shot);
    if (!shot) return;
    const { order: _order, ...rest } = shot;
    const next = { ...rest, focus: shot.focus === job.params.hero ? "hero" : shot.focus };
    const index = state.cuts.findIndex((cut) => cut.id === shot.id);
    if (index >= 0) state.cuts[index] = next;
    else state.cuts.push(next);
    state.selectedCut = index >= 0 ? index : state.cuts.length - 1;
    changed({ cuts: true });
  }

  async function review(button) {
    const job = getJobs().find((item) => item.id === button.dataset.job);
    const asset = job?.assets.find((item) => item.id === button.dataset.asset);
    const status = asset?.review === button.dataset.status ? "pending" : button.dataset.status;
    await api(`/api/jobs/${encodeURIComponent(button.dataset.job)}/assets/${encodeURIComponent(button.dataset.asset)}/review`, {
      method: "POST", body: { status },
    });
    await refreshJobs();
    renderStage();
  }

  function renderSettings() {
    for (const key of ["renderer", "resolution", "fps", "aux"]) $(`#pv-${key}`).value = state.settings[key];
    $("#pv-clay").checked = state.settings.clay;
    $("#pv-animatic").checked = state.settings.animatic;
  }

  function renderLayout() {
    for (const section of document.querySelectorAll(".pv-section")) {
      const open = layout[section.dataset.section];
      section.classList.toggle("is-collapsed", !open);
      $(".pv-head", section).setAttribute("aria-expanded", String(open));
    }
    $("#pv-body").classList.toggle("is-panel-closed", !layout.panel);
    const toggle = $("#pv-panel-toggle");
    toggle.textContent = layout.panel ? "‹" : "›";
    toggle.setAttribute("aria-expanded", String(layout.panel));
    toggle.setAttribute("data-tip", layout.panel ? "편집 패널 접기 — 영상을 크게 봅니다" : "편집 패널 펼치기");
  }

  function renderEditor() {
    renderTray();
    renderMap();
    renderAssetInspector();
    renderCutList();
    renderSettings();
    renderSummaries();
    renderBar();
  }

  function wire() {
    const view = $("#view-previz");

    view.addEventListener("click", (event) => {
      const head = event.target.closest(".pv-head");
      if (head) {
        const name = head.closest(".pv-section").dataset.section;
        layout[name] = !layout[name];
        storage.write(LAYOUT_KEY, layout);
        renderLayout();
        return;
      }
      const target = event.target.closest("[data-pv]");
      if (!target) return;
      const index = Number(target.dataset.index);
      const action = target.dataset.pv;
      if (action === "go-create") goCreate();
      else if (action === "place") {
        const [jobId, id] = target.dataset.asset.split(":");
        placeAsset(jobId, id);
      } else if (action === "remove-asset") removeAsset(state.selectedAsset);
      else if (action === "make-hero") {
        const [item] = state.placed.splice(state.selectedAsset, 1);
        state.placed.unshift(item);
        state.selectedAsset = 0;
        changed({ cuts: true });
        renderTray();
        renderAssetInspector();
      } else if (action === "preset") {
        if (target.dataset.preset === state.presetId && !edited()) return;
        if (edited() && !window.confirm("편집한 컷을 버리고 이 프리셋으로 바꿀까요?")) return;
        state.presetId = target.dataset.preset;
        state.cuts = draftFromPreset(preset());
        state.selectedCut = 0;
        state.expandedCut = -1;
        changed({ cuts: true });
      } else if (action === "select-cut" && !event.target.closest("button")) {
        state.selectedCut = index;
        // 컷을 고르면 그 컷의 카메라가 보여야 한다. 손으로 옮긴 화면이라도 다시 맞춘다.
        state.view.auto = true;
        changed({ cuts: true, summary: false });
      } else if (action === "expand-cut") {
        state.expandedCut = state.expandedCut === index ? -1 : index;
        state.selectedCut = index;
        state.view.auto = true;
        changed({ cuts: true, summary: false });
      } else if (action === "seconds") {
        state.cuts[index] = setSeconds(state.cuts[index], Number(state.cuts[index].seconds) + Number(target.dataset.delta));
        changed({ cuts: true, map: false });
      } else if (action === "delete-cut") {
        state.cuts.splice(index, 1);
        state.selectedCut = Math.min(state.selectedCut, state.cuts.length - 1);
        state.expandedCut = -1;
        changed({ cuts: true });
      } else if (action === "render") {
        state.renderId = target.dataset.job;
        state.inspectShot = null;
        renderBar();
        renderStage();
      } else if (action === "play") {
        const player = video();
        if (player) (player.paused ? player.play() : Promise.resolve(player.pause())).catch(() => {});
      } else if (action === "prev") seekShot(-1);
      else if (action === "next") seekShot(1);
      else if (action === "seek") {
        const player = video();
        state.inspectShot = target.dataset.shot;
        if (player) player.currentTime = Number(target.dataset.at) + 0.01;
        renderTimeline();
        renderInspector();
        updatePlayhead();
      } else if (action === "review") review(target);
      else if (action === "open-asset") openAsset(target.dataset.job, target.dataset.asset);
      else if (action === "open-blend") bridge?.openBlend(target.dataset.job, target.dataset.file);
      else if (action === "load-render") {
        const job = selectedRender();
        if (job && (!edited() || window.confirm("지금 편집 중인 장면과 컷을 이 렌더의 값으로 바꿀까요?"))) loadRender(job);
      } else if (action === "cut-to-draft") {
        const asset = selectedRender()?.assets.find((item) => item.id === target.dataset.asset);
        if (asset) cutToDraft(asset);
      } else if (action === "cancel") {
        api(`/api/jobs/${encodeURIComponent(target.dataset.job)}/cancel`, { method: "POST" }).finally(refreshJobs);
      }
    });

    $("#pv-render").addEventListener("click", submit);
    $("#pv-cut-add").addEventListener("click", () => {
      const source = state.cuts[state.selectedCut];
      if (!source) {
        state.cuts = draftFromPreset(preset());
      } else {
        const copy = { ...clone(source), id: nextShotId(state.cuts), label: `${source.label || source.id} 복제` };
        state.cuts.splice(state.selectedCut + 1, 0, copy);
        state.selectedCut += 1;
        state.expandedCut = state.selectedCut;
      }
      changed({ cuts: true });
    });
    $("#pv-cut-reset").addEventListener("click", () => {
      if (!window.confirm("편집한 컷을 버리고 프리셋으로 되돌릴까요?")) return;
      state.cuts = draftFromPreset(preset());
      state.selectedCut = 0;
      state.expandedCut = -1;
      changed({ cuts: true });
    });
    $("#pv-fit").addEventListener("click", () => {
      state.view.auto = true;
      renderMap();
    });
    $("#pv-panel-toggle").addEventListener("click", () => {
      layout.panel = !layout.panel;
      storage.write(LAYOUT_KEY, layout);
      renderLayout();
    });

    // 설정
    for (const key of ["renderer", "resolution", "fps", "aux"]) {
      $(`#pv-${key}`).addEventListener("change", (event) => {
        state.settings[key] = event.target.value;
        changed({ map: false });
      });
    }
    for (const key of ["clay", "animatic"]) {
      $(`#pv-${key}`).addEventListener("change", (event) => {
        state.settings[key] = event.target.checked;
        changed({ map: false });
      });
    }

    // 에셋 숫자 입력
    $("#pv-asset").addEventListener("input", (event) => {
      const input = event.target.closest("[data-asset-field]");
      const item = state.placed[state.selectedAsset];
      if (!input || !item || input.value === "" || !Number.isFinite(Number(input.value))) return;
      item[input.dataset.assetField] = Number(input.value);
      changed({});
    });

    // 컷 편집값
    $("#pv-cutlist").addEventListener("input", (event) => {
      const slider = event.target.closest("[data-cut-field]");
      if (slider) {
        const index = Number(slider.dataset.index);
        const field = FIELDS.find((item) => item.key === slider.dataset.cutField);
        const key = slider.dataset.end === "true" ? `${field.key}End` : field.key;
        const cut = clone(state.cuts[index]);
        if (field.framing) cut.framing = { ...cut.framing, [key]: Number(slider.value) };
        else cut[key] = Number(slider.value);
        slider.nextElementSibling.textContent = `${Number(slider.value).toFixed(field.step < 1 ? 2 : 0)}${field.unit}`;
        updateCut(index, cut);
        return;
      }
      const text = event.target.closest("[data-cut-text]");
      if (text && text.tagName === "INPUT") {
        const index = Number(text.dataset.index);
        updateCut(index, { ...state.cuts[index], [text.dataset.cutText]: text.value });
      }
    });
    $("#pv-cutlist").addEventListener("change", (event) => {
      const index = Number(event.target.dataset.index);
      const cut = state.cuts[index];
      if (!cut) return;
      if (event.target.matches("select[data-cut-text]")) {
        updateCut(index, { ...cut, [event.target.dataset.cutText]: event.target.value }, { list: true });
      } else if (event.target.matches("[data-cut-moving]")) {
        const next = clone(cut);
        if (event.target.checked) {
          next.framing = { ...next.framing, azimuthEnd: Number(next.framing?.azimuth ?? -35) + 40 };
        } else {
          next.framing = Object.fromEntries(Object.entries(next.framing || {}).filter(([key]) => !key.endsWith("End")));
          delete next.lensEnd;
        }
        updateCut(index, next, { list: true });
      } else if (event.target.matches("[data-cut-ease]")) {
        updateCut(index, { ...cut, ease: event.target.checked ? "inout" : "linear" }, { list: true });
      } else if (event.target.matches("[data-cut-text]")) {
        renderCutList();
      }
    });

    // 컷 순서 끌기
    const cutList = $("#pv-cutlist");
    cutList.addEventListener("dragstart", (event) => {
      const line = event.target.closest(".pv-cut-line");
      if (!line) return;
      event.dataTransfer.setData("application/x-pv-cut", line.dataset.index);
      event.dataTransfer.effectAllowed = "move";
      line.closest(".pv-cut").classList.add("is-dragging");
    });
    cutList.addEventListener("dragover", (event) => {
      if (!event.dataTransfer.types.includes("application/x-pv-cut")) return;
      event.preventDefault();
      const row = event.target.closest(".pv-cut");
      for (const item of cutList.querySelectorAll(".drop-before, .drop-after")) item.classList.remove("drop-before", "drop-after");
      if (!row) return;
      const box = row.getBoundingClientRect();
      row.classList.add(event.clientY < box.top + box.height / 2 ? "drop-before" : "drop-after");
    });
    cutList.addEventListener("drop", (event) => {
      if (!event.dataTransfer.types.includes("application/x-pv-cut")) return;
      event.preventDefault();
      const from = Number(event.dataTransfer.getData("application/x-pv-cut"));
      const row = event.target.closest(".pv-cut");
      if (!row) return;
      let to = Number(row.dataset.index) + (row.classList.contains("drop-after") ? 1 : 0);
      if (from < to) to -= 1;
      const moving = state.cuts[from];
      state.cuts = moveItem(state.cuts, from, to);
      state.selectedCut = state.cuts.indexOf(moving);
      state.expandedCut = -1;
      changed({ cuts: true });
    });
    cutList.addEventListener("dragend", () => {
      for (const item of cutList.querySelectorAll(".is-dragging, .drop-before, .drop-after")) {
        item.classList.remove("is-dragging", "drop-before", "drop-after");
      }
    });

    // 에셋 트레이 → 지도
    const map = $("#pv-map");
    $("#pv-tray").addEventListener("dragstart", (event) => {
      const item = event.target.closest("[data-asset]");
      if (!item) return;
      event.dataTransfer.setData("application/x-pv-asset", item.dataset.asset);
      event.dataTransfer.effectAllowed = "copy";
    });
    map.addEventListener("dragover", (event) => {
      if (!event.dataTransfer.types.includes("application/x-pv-asset")) return;
      event.preventDefault();
      map.classList.add("is-drop");
    });
    map.addEventListener("dragleave", () => map.classList.remove("is-drop"));
    map.addEventListener("drop", (event) => {
      map.classList.remove("is-drop");
      const key = event.dataTransfer.getData("application/x-pv-asset");
      if (!key) return;
      event.preventDefault();
      const [x, y] = worldPoint(event);
      const [jobId, id] = key.split(":");
      state.view.auto = false;
      placeAsset(jobId, id, [x, y]);
    });

    // 지도 안에서 끌기: 에셋 이동·회전, 카메라 조준, 빈 곳은 화면 이동
    map.addEventListener("pointerdown", (event) => {
      const svg = $("#pv-map svg");
      if (!svg || event.button !== 0) return;
      const handle = event.target.closest("[data-drag]");
      const point = worldPoint(event);
      if (handle?.dataset.drag === "asset") {
        const index = Number(handle.dataset.index);
        state.selectedAsset = index;
        const item = state.placed[index];
        state.drag = { kind: "asset", index, offset: [item.x - point[0], item.y - point[1]] };
        renderAssetInspector();
      } else if (handle?.dataset.drag === "rotate") {
        state.drag = { kind: "rotate", index: Number(handle.dataset.index) };
      } else if (handle?.dataset.drag === "camera") {
        state.drag = { kind: "camera", handle: handle.dataset.handle };
      } else {
        state.selectedAsset = -1;
        state.drag = { kind: "pan", from: [event.clientX, event.clientY], center: [...state.view.center] };
        renderAssetInspector();
      }
      state.view.auto = false;
      map.setPointerCapture(event.pointerId);
      map.classList.add(`is-${state.drag.kind}`);
      renderMap();
    });
    map.addEventListener("pointermove", (event) => {
      const drag = state.drag;
      if (!drag) return;
      const snap = (value, step) => (event.shiftKey ? value : Math.round(value / step) * step);
      if (drag.kind === "pan") {
        const box = map.getBoundingClientRect();
        const perPixel = (state.view.reach * 2) / Math.max(box.width, 1);
        state.view.center = [drag.center[0] - (event.clientX - drag.from[0]) * perPixel, drag.center[1] + (event.clientY - drag.from[1]) * perPixel];
      } else {
        const [x, y] = worldPoint(event);
        if (drag.kind === "asset") {
          const item = state.placed[drag.index];
          item.x = Math.round(snap(x + drag.offset[0], 0.1) * 100) / 100;
          item.y = Math.round(snap(y + drag.offset[1], 0.1) * 100) / 100;
        } else if (drag.kind === "rotate") {
          const item = state.placed[drag.index];
          const degrees = (Math.atan2(-(x - item.x), y - item.y) * 180) / Math.PI;
          item.yaw = Math.round(snap(degrees, 5));
        } else if (drag.kind === "camera") {
          const index = state.selectedCut;
          state.cuts[index] = aimFromPoint(state.cuts[index], subjects(), [x, y], drag.handle, ASPECT);
        }
      }
      renderMap();
      if (drag.kind === "asset" || drag.kind === "rotate") {
        for (const input of document.querySelectorAll("[data-asset-field]")) {
          if (document.activeElement !== input) input.value = state.placed[drag.index][input.dataset.assetField];
        }
      }
    });
    const endDrag = () => {
      if (!state.drag) return;
      const kind = state.drag.kind;
      map.classList.remove(`is-${kind}`);
      state.drag = null;
      if (kind === "camera") changed({ cuts: true });
      else if (kind !== "pan") changed({});
    };
    map.addEventListener("pointerup", endDrag);
    map.addEventListener("pointercancel", endDrag);
    map.addEventListener("wheel", (event) => {
      event.preventDefault();
      state.view.auto = false;
      state.view.reach = Math.min(60, Math.max(1.2, state.view.reach * (event.deltaY > 0 ? 1.12 : 1 / 1.12)));
      renderMap();
    }, { passive: false });
    map.addEventListener("keydown", (event) => {
      const item = state.placed[state.selectedAsset];
      if (!item) return;
      const step = event.shiftKey ? 1 : 0.1;
      const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
      if (moves[event.key]) {
        event.preventDefault();
        item.x = Math.round((item.x + moves[event.key][0]) * 100) / 100;
        item.y = Math.round((item.y + moves[event.key][1]) * 100) / 100;
        changed({});
        renderAssetInspector();
      } else if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        removeAsset(state.selectedAsset);
      }
    });

    // 영상: 재생 위치와 타임라인
    $("#pv-screen").addEventListener("timeupdate", updatePlayhead, true);
    $("#pv-screen").addEventListener("play", updatePlayhead, true);
    $("#pv-screen").addEventListener("pause", updatePlayhead, true);
    $("#pv-screen").addEventListener("click", (event) => {
      if (event.target.matches("video")) {
        const player = event.target;
        (player.paused ? player.play() : Promise.resolve(player.pause())).catch(() => {});
      }
    });
    $("#pv-timeline").addEventListener("pointerdown", (event) => {
      const track = event.target.closest(".pv-track");
      const player = video();
      if (!track || !player || event.target.closest(".pv-seg")) return;
      const box = track.getBoundingClientRect();
      player.currentTime = ((event.clientX - box.left) / box.width) * (player.duration || 0);
    });
    document.addEventListener("keydown", (event) => {
      if (view.hidden || event.target.closest("input, select, textarea, [contenteditable]")) return;
      if (event.target.closest("#pv-map")) return;
      const player = video();
      if (event.code === "Space" && player) {
        event.preventDefault();
        (player.paused ? player.play() : Promise.resolve(player.pause())).catch(() => {});
      } else if (event.key === "ArrowLeft") seekShot(-1);
      else if (event.key === "ArrowRight") seekShot(1);
    });
  }

  let wired = false;

  return {
    async show() {
      if (!wired) {
        wired = true;
        wire();
        restoreDraft();
        renderLayout();
      }
      try {
        state.meshes = (await api("/api/assets?kind=mesh")).assets;
      } catch (error) {
        state.error = error.message;
      }
      syncPlacedWithMeshes();
      renderEditor();
      renderStage();
    },
    onJobs() {
      if (!wired) return;
      renderBar();
      renderStage();
    },
    addAsset(jobId, id) {
      placeAsset(jobId, id);
    },
  };
}
