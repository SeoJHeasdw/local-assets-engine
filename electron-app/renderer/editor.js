// 에셋 편집 공간. 편집은 레이어(색 바꾸기·로고·문구) 목록이고 저장하면 새 버전이 된다.
// 계산은 edit-preview.js(Web Worker)가 저장과 같은 renderEdit로 하고, 여기서는 조작과 기록만 다룬다.
import {
  CROP_RATIOS, clamp, colorLayer, defaults, frameLayout, layerId, migratePlan, outputToSource, stampLayer,
} from "../shared/editing.mjs";
import { escapeHtml, fileUrl, formatBytes } from "../shared/format.mjs";
import { popup, toast, pickFile, uploadBlob, waitJob } from "./ui.js";
import { readDraft, writeDraft } from "./drafts.js";

const clone = (value) => structuredClone(value);
const HISTORY_LIMIT = 100;
const MERGE_MS = 1500;
const ICONS = {
  eye: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8s-2.4 4.5-6.5 4.5S1.5 8 1.5 8Z"/><circle cx="8" cy="8" r="2"/></svg>',
  eyeOff: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 2l12 12M6.3 3.8A6 6 0 0 1 8 3.5c4.1 0 6.5 4.5 6.5 4.5a11 11 0 0 1-1.9 2.4M4.1 5.2A11 11 0 0 0 1.5 8s2.4 4.5 6.5 4.5a6 6 0 0 0 2.4-.5"/></svg>',
  lock: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5 7V5a3 3 0 0 1 6 0v2"/></svg>',
  unlock: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5 7V5a3 3 0 0 1 5.8-1"/></svg>',
  star: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m8 1.8 1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6Z"/></svg>',
};
const FONTS = {
  bold: "700 160px Pretendard, -apple-system, 'Apple SD Gothic Neo', sans-serif",
  regular: "400 160px Pretendard, -apple-system, 'Apple SD Gothic Neo', sans-serif",
  serif: "700 160px 'AppleMyungjo', 'Nanum Myeongjo', serif",
};
const range = (key, label, min, max, step, tip = "") => `<label class="edit-range"${tip ? ` data-tip="${escapeHtml(tip)}"` : ""}><span>${label}<output data-out="${key}"></output></span><input type="range" data-prop="${key}" aria-label="${label}" min="${min}" max="${max}" step="${step}"></label>`;

async function decodeBlob(blob, max = 1024) {
  const bitmap = await createImageBitmap(blob);
  const scale = Math.min(1, max / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d");
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return { width: canvas.width, height: canvas.height, data: context.getImageData(0, 0, canvas.width, canvas.height).data };
}

async function textStamp(text, color, font = "bold") {
  const canvas = document.createElement("canvas"), context = canvas.getContext("2d");
  context.font = FONTS[font] || FONTS.bold;
  canvas.width = Math.min(4096, Math.ceil(context.measureText(text).width + 60));
  canvas.height = 240;
  context.font = FONTS[font] || FONTS.bold;
  context.fillStyle = color;
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text, canvas.width / 2, 124, canvas.width - 30);
  return new Promise((resolve) => canvas.toBlob(resolve));
}

export function createEditor({ api, refreshJobs, openAsset, makeMesh, addToPrevizScene, bridge, quietJob = () => {} }) {
  let session = null;
  let opening = 0;
  const drafts = new Map();

  function close() {
    opening++;
    session?.dialog.close();
  }

  // 예전 임시 편집(로고 하나)과 새 형식(레이어별 그림)을 모두 읽는다.
  function draftState(old) {
    if (old.version === 2) return { plan: migratePlan(old.plan), stamps: new Map(old.stamps || []), selected: old.selected || null };
    const plan = migratePlan(old.plan);
    const stamps = new Map();
    const stampId = plan.layers.find((layer) => layer.type === "stamp")?.id;
    if (old.stamp && stampId) stamps.set(stampId, { image: old.stamp, blob: old.stampBlob, upload: old.stampUpload, label: old.stampLabel });
    return { plan, stamps, selected: null };
  }

  async function savedStamps(job, asset, plan) {
    const files = { ...(asset.meta?.editStampFiles || {}) };
    const legacyId = plan.layers.find((layer) => layer.type === "stamp")?.id;
    if (asset.meta?.editStampFile && legacyId && !files[legacyId]) files[legacyId] = asset.meta.editStampFile;
    const stamps = new Map();
    await Promise.all(Object.entries(files).map(async ([id, file]) => {
      const response = await fetch(fileUrl(job.id, file));
      if (!response.ok) return;
      const blob = await response.blob();
      const layer = plan.layers.find((item) => item.id === id);
      stamps.set(id, { image: await decodeBlob(blob), blob, upload: null, label: layer?.text || "로고 이미지" });
    }));
    return stamps;
  }

  async function open(job, asset) {
    close();
    const ticket = ++opening;
    const key = `${job.id}/${asset.id}`, isMesh = asset.kind === "mesh";
    const old = drafts.get(key) || await readDraft(key);
    if (ticket !== opening) return;
    const baseFile = asset.meta?.editBaseFile || asset.file;
    let initial;
    if (old) initial = draftState(old);
    else {
      const plan = migratePlan(asset.meta?.editBaseFile ? asset.meta.editPlan : defaults());
      initial = { plan, stamps: await savedStamps(job, asset, plan), selected: null };
    }
    if (ticket !== opening) return;
    const name = old?.name || job.params?.subject || job.title;
    const dialog = document.createElement("dialog");
    dialog.id = "editor-dialog";
    dialog.className = "editor-dialog";
    dialog.setAttribute("aria-label", "에셋 편집");
    dialog.innerHTML = shellHtml({ job, asset, isMesh, name, baseFile });
    document.body.append(dialog);
    dialog.showModal();
    // 예전 편집본의 문구 색은 기록에 없고 그림에만 있다. 다시 입력해도 같은 색으로 만들도록 그림에서 읽는다.
    for (const item of initial.plan.layers) {
      const image = initial.stamps.get(item.id)?.image;
      if (item.type !== "stamp" || !item.text || item.textColor || !image) continue;
      for (let i = 0; i < image.data.length; i += 4) {
        if (image.data[i + 3] > 245) { item.textColor = `#${[0, 1, 2].map((k) => image.data[i + k].toString(16).padStart(2, "0")).join("")}`; break; }
      }
    }
    const s = {
      dialog, job, asset, key, isMesh, name,
      plan: initial.plan, stamps: initial.stamps, selected: initial.selected || initial.plan.layers.at(-1)?.id || null,
      tab: "layers", tool: null, brush: { size: isMesh ? .04 : .05, erase: false }, showRegion: true,
      past: [], future: [], dirty: Boolean(old), ready: false, loaded: !isMesh, busy: false, pending: false,
      revision: 0, compare: false, closed: false, saving: false, source: { width: 1, height: 1 },
      textures: new Map(), originals: [], longest: 1, lastMerge: null,
    };
    session = s;
    wire(s);
  }

  function shellHtml({ job, asset, isMesh, name, baseFile }) {
    const viewer = isMesh
      ? `<model-viewer src="${fileUrl(job.id, baseFile)}" camera-controls camera-orbit="30deg 70deg auto" shadow-intensity=".7" environment-image="neutral" exposure="1.05" interaction-prompt="none" alt="${escapeHtml(name)}"${asset.preview ? ` poster="${fileUrl(job.id, asset.preview)}"` : ""}></model-viewer>`
      : '<canvas aria-label="이미지 편집 미리보기"></canvas>';
    return `<header class="editor-head"><div class="editor-title"><span class="eyebrow">${isMesh ? "3D 에셋" : "2D 이미지"}</span><h2>${escapeHtml(name)}</h2></div>
      <div class="editor-actions"><button class="secondary" type="button" data-e="compare" aria-pressed="false">원본 비교</button><button class="ghost" type="button" data-e="info">정보·정리</button><button class="icon-button" type="button" data-e="close" aria-label="편집 닫기">✕</button></div></header>
    <div class="editor-body">
      <div class="editor-canvas-wrap"><div class="editor-canvas">${viewer}<div class="editor-overlay" aria-hidden="false"></div></div>
        <div class="viewport-bar"><span class="editor-status" role="status">불러오는 중…</span><span class="viewport-hint"></span></div></div>
      <aside class="editor-panel">
        <div class="editor-tabs" role="tablist"><button type="button" role="tab" aria-selected="true" data-tab="layers">레이어</button><button type="button" role="tab" aria-selected="false" data-tab="tone">${isMesh ? "톤·재질" : "톤"}</button>${isMesh ? "" : '<button type="button" role="tab" aria-selected="false" data-tab="frame">구성</button>'}</div>
        <div class="edit-controls" data-panel="layers">
          <div class="layer-add"><button class="secondary" type="button" data-e="add-color" data-tip="특정 색이나 칠한 영역의 색을 바꿉니다">＋ 색 바꾸기</button><button class="secondary" type="button" data-e="add-logo">＋ 로고</button><button class="secondary" type="button" data-e="add-text">＋ 문구</button></div>
          <ol class="layer-list" aria-label="레이어"></ol>
          <div class="layer-props"></div>
        </div>
        <div class="edit-controls" data-panel="tone" hidden><h3>전체 톤</h3><p class="hint">색 레이어를 적용한 뒤 에셋 전체에 겁니다. 로고·문구에는 적용하지 않습니다.</p>
          ${range("brightness", "밝기", .1, 2, .05)}${range("contrast", "대비", .1, 2, .05)}${range("saturation", "채도", 0, 2, .05)}
          ${isMesh ? `<div class="control-divider"></div><h3>재질</h3>${range("metallic", "금속성 유지", 0, 1, .05)}${range("roughness", "거칠기 유지", .05, 1, .05)}` : ""}
        </div>
        ${isMesh ? "" : '<div class="edit-controls" data-panel="frame" hidden></div>'}
        <div class="edit-bottom">
          <div class="control-row history-row"><button class="ghost" type="button" data-e="undo" disabled data-tip="되돌리기 (⌘Z)">↶</button><button class="ghost" type="button" data-e="redo" disabled data-tip="다시 실행 (⇧⌘Z)">↷</button><button class="ghost" type="button" data-e="history">작업 내역</button><button class="ghost" type="button" data-e="reset">처음 상태</button></div>
          <label class="field"><span class="label">새 버전 이름</span><input id="edit-name" value="${escapeHtml(name)}" maxlength="120"></label>
          <button class="primary full" type="button" data-e="save" disabled>새 버전으로 저장</button>
          <div class="save-queue" hidden><span></span><button class="ghost is-danger" type="button" data-e="cancel-save">저장 취소</button></div>
          <p class="hint">원본은 그대로 보관됩니다.</p>
        </div>
      </aside>
    </div>
    <footer class="editor-foot"><div class="control-row"><button class="ghost favorite-toggle" type="button" data-e="favorite" aria-pressed="${asset.favorite ? "true" : "false"}">${ICONS.star}<span>즐겨찾기</span></button><button class="ghost" type="button" data-e="versions">버전 이력</button></div>
      <div class="control-row">${isMesh ? '<button class="secondary" type="button" data-e="previz">프리비즈에 넣기</button>' : '<button class="secondary" type="button" data-e="to3d">이 이미지로 3D 만들기</button>'}<a class="secondary button-link" data-e="download" href="${fileUrl(job.id, asset.file)}" download>파일 내보내기</a></div></footer>`;
  }

  function wire(s) {
    const { dialog, job, asset, isMesh } = s;
    const $ = (query) => dialog.querySelector(query);
    const viewer = $("model-viewer"), canvas = $("canvas"), overlay = $(".editor-overlay"), viewport = $(".editor-canvas");
    const status = (text) => { $(".editor-status").textContent = text; };
    const layer = (id = s.selected) => s.plan.layers.find((item) => item.id === id) || null;
    const selectedStamp = () => (layer()?.type === "stamp" ? layer() : null);

    // ---- 기록 --------------------------------------------------------------------
    const capture = () => ({ plan: clone(s.plan), stamps: new Map(s.stamps), selected: s.selected });
    function restore(state) {
      s.plan = clone(state.plan);
      s.stamps = new Map(state.stamps);
      s.selected = state.selected;
      syncStamps();
    }
    // 같은 조작(슬라이더 끌기, 문구 입력)은 잠깐 사이에 이어지면 한 단계로 묶는다.
    function remember(label, merge = null) {
      const now = Date.now();
      if (merge && s.lastMerge?.key === merge && now - s.lastMerge.at < MERGE_MS) { s.lastMerge.at = now; return; }
      s.past.push({ label, state: capture() });
      if (s.past.length > HISTORY_LIMIT) s.past.shift();
      s.future = [];
      s.lastMerge = merge ? { key: merge, at: now } : null;
      s.dirty = true;
    }
    function undo() {
      const entry = s.past.pop();
      if (!entry) return;
      s.future.push({ label: entry.label, state: capture() });
      restore(entry.state);
      s.lastMerge = null;
      changed();
    }
    function redo() {
      const entry = s.future.pop();
      if (!entry) return;
      s.past.push({ label: entry.label, state: capture() });
      restore(entry.state);
      s.lastMerge = null;
      changed();
    }
    function jumpTo(index) {
      // index: 0 = 처음(내역 전), n = past[n-1] 적용 후.
      while (s.past.length > index) undo();
    }

    // ---- 임시 보관 -----------------------------------------------------------------
    const draftValue = () => ({ version: 2, name: $("#edit-name").value, plan: clone(s.plan), stamps: [...s.stamps], selected: s.selected });
    async function flushDraft() {
      clearTimeout(s.draftTimer);
      if (!s.dirty || s.saved) return;
      const value = draftValue();
      drafts.set(s.key, value);
      if (!await writeDraft(s.key, value) && !s.closed) status("임시 보관을 못 했습니다. 저장 공간을 확인하거나 새 버전으로 저장해 주세요.");
    }
    function scheduleDraft() {
      clearTimeout(s.draftTimer);
      if (s.dirty) s.draftTimer = setTimeout(flushDraft, 400);
    }
    const onHide = () => { if (document.visibilityState === "hidden") flushDraft(); };
    window.addEventListener("pagehide", flushDraft);
    document.addEventListener("visibilitychange", onHide);

    // ---- 미리보기 --------------------------------------------------------------------
    s.worker = new Worker(new URL("./edit-preview.js", import.meta.url), { type: "module" });
    function syncStamps() {
      for (const item of s.plan.layers) if (item.type === "stamp") s.worker.postMessage({ type: "stamp", id: item.id, image: s.stamps.get(item.id)?.image || null });
    }
    function queue() {
      s.pending = true;
      updateSave();
      clearTimeout(s.timer);
      s.timer = setTimeout(send, 60);
    }
    function previewPlan() {
      if (s.compare) return defaults();
      if (s.tab !== "frame" || isMesh) return s.plan;
      // 구성 탭에서는 자르기 전 전체를 보여 주고 그 위에 자르기 틀을 얹는다.
      return { ...s.plan, frame: { turns: s.plan.frame.turns, flipX: s.plan.frame.flipX }, layers: s.plan.layers.filter((item) => item.type === "color") };
    }
    function send() {
      if (s.closed || !s.ready || !s.loaded || s.busy || !s.pending) return;
      s.pending = false;
      s.busy = true;
      s.revision++;
      const current = layer();
      const highlight = !s.compare && s.showRegion && current?.type === "color" && current.region ? current.id : null;
      s.worker.postMessage({ type: "preview", id: s.revision, plan: previewPlan(), highlight });
    }
    async function display(images) {
      if (s.closed) return;
      if (isMesh) {
        for (const image of images) {
          let texture = s.textures.get(image.index);
          if (!texture) { texture = viewer.createCanvasTexture(); s.textures.set(image.index, texture); }
          const target = texture.source.element;
          target.width = image.width;
          target.height = image.height;
          // CanvasTexture는 V가 뒤집혀 올라간다. 화면에 올릴 때만 행을 뒤집는다.
          const flipped = new Uint8ClampedArray(image.data.length);
          for (let y = 0; y < image.height; y++) flipped.set(image.data.subarray(y * image.width * 4, (y + 1) * image.width * 4), (image.height - 1 - y) * image.width * 4);
          target.getContext("2d").putImageData(new ImageData(flipped, image.width, image.height), 0, 0);
          texture.source.update();
          for (const index of image.materials) viewer.model.materials[index].pbrMetallicRoughness.baseColorTexture.setTexture(s.compare ? s.originals[index].texture : texture);
        }
        viewer.model.materials.forEach((material, index) => {
          material.pbrMetallicRoughness.setMetallicFactor(s.originals[index].metallic * (s.compare ? 1 : s.plan.metallic));
          material.pbrMetallicRoughness.setRoughnessFactor(s.originals[index].roughness * (s.compare ? 1 : s.plan.roughness));
        });
      } else {
        const image = images[0];
        canvas.width = image.width;
        canvas.height = image.height;
        canvas.getContext("2d").putImageData(new ImageData(image.data, image.width, image.height), 0, 0);
      }
      if (!s.saving) status(s.compare ? "원본을 보고 있습니다" : idleStatus());
      updateSave();
      renderOverlay();
    }
    s.worker.onmessage = async ({ data }) => {
      if (s.closed) return;
      if (data.type === "ready") {
        s.ready = true;
        s.source = { width: data.width, height: data.height };
        syncStamps();
        renderFrame();
        queue();
      } else if (data.type === "preview") {
        s.busy = false;
        await display(data.images);
        if (s.pending) send();
      } else if (data.type === "sample") {
        const current = layer();
        if (current?.type === "color") {
          remember("원래 색 찍기");
          current.from = data.color;
          current.mode = "match";
        }
        setTool(null);
        changed();
      } else if (data.type === "error") {
        s.busy = false;
        status(data.message);
        toast(data.message);
      }
    };
    s.worker.postMessage({ type: "init", kind: asset.kind, url: fileUrl(job.id, s.asset.meta?.editBaseFile || asset.file) });
    if (viewer) {
      viewer.addEventListener("load", () => {
        s.originals = viewer.model.materials.map((material) => ({
          texture: material.pbrMetallicRoughness.baseColorTexture.texture,
          metallic: material.pbrMetallicRoughness.metallicFactor,
          roughness: material.pbrMetallicRoughness.roughnessFactor,
        }));
        s.loaded = true;
        const size = viewer.getDimensions();
        s.longest = Math.max(size.x, size.y, size.z) || 1;
        changed({ draft: false });
      }, { once: true });
      viewer.addEventListener("error", () => status("3D 파일을 불러오지 못했습니다. 다시 열어 주세요."));
      viewer.addEventListener("camera-change", () => renderOverlay());
    }

    // ---- 화면 갱신 -------------------------------------------------------------------
    function idleStatus() {
      const current = layer();
      if (s.tool === "brush") return s.brush.erase ? "지우개: 칠한 곳을 드래그해 지웁니다" : "브러시: 바꿀 곳을 드래그해 칠합니다";
      if (s.tool === "pick-color") return "바꾸고 싶은 색을 클릭하세요";
      if (s.tool === "place") return "로고를 붙일 곳을 클릭하세요";
      if (current?.type === "stamp" && isMesh && !current.position) return "로고를 붙일 표면을 클릭하세요";
      if (current?.type === "color" && Array.isArray(current.region?.strokes) && !current.region.strokes.length) return "칠한 곳만 바꿉니다. 브러시로 영역을 칠하세요";
      return "미리보기";
    }
    function viewportHint() {
      if (s.tool === "brush") return isMesh ? "표면 위를 드래그해 칠하기 · 빈 곳을 드래그하면 회전" : "드래그해 칠하기 · [ ] 브러시 크기";
      if (s.tab === "frame") return "틀을 끌어 옮기고 모서리로 크기 조절";
      if (selectedStamp()) return isMesh ? "로고를 끌어 표면을 따라 옮기기 · Alt+휠 크기 · Shift+휠 회전" : "로고를 끌어 옮기기 · 모서리 크기 · 위 손잡이 회전";
      return isMesh ? "드래그로 회전 · 휠로 확대" : "레이어를 골라 편집";
    }
    function updateSave() {
      $('[data-e="save"]').disabled = !s.ready || !s.loaded || s.saving || !s.dirty;
      $('[data-e="undo"]').disabled = !s.past.length;
      $('[data-e="redo"]').disabled = !s.future.length;
    }
    function changed({ draft = true } = {}) {
      renderLayers();
      renderProps();
      renderTone();
      renderFrame();
      $(".viewport-hint").textContent = viewportHint();
      if (draft) scheduleDraft();
      queue();
    }
    function setTool(tool) {
      s.tool = tool;
      dialog.classList.toggle("is-picking", tool === "pick-color" || tool === "place");
      dialog.classList.toggle("is-brushing", tool === "brush");
      status(idleStatus());
      $(".viewport-hint").textContent = viewportHint();
      renderProps();
      renderOverlay();
    }

    function layerSummary(item) {
      if (item.type === "color") {
        const area = !item.region ? "전체" : item.region.strokes.length ? "칠한 곳" : "칠할 곳 없음";
        return `${item.mode === "fill" ? "영역 칠하기" : "비슷한 색"} · ${area}`;
      }
      if (isMesh && !item.position) return "붙일 곳 선택 전";
      return item.text ? `문구 · ${item.text}` : "로고 이미지";
    }
    function renderLayers() {
      const list = $(".layer-list");
      const stamps = s.plan.layers.filter((item) => item.type === "stamp").reverse();
      const colors = s.plan.layers.filter((item) => item.type === "color").reverse();
      const row = (item) => `<li class="layer-row${item.id === s.selected ? " is-selected" : ""}${item.visible === false ? " is-hidden" : ""}" data-layer="${escapeHtml(item.id)}">
        <button type="button" class="layer-toggle" data-e="toggle-visible" aria-pressed="${item.visible !== false}" data-tip="${item.visible === false ? "보이기" : "숨기기"}">${item.visible === false ? ICONS.eyeOff : ICONS.eye}</button>
        <button type="button" class="layer-name" data-e="select" aria-current="${item.id === s.selected}"><span class="layer-swatch ${item.type}"${item.type === "color" ? ` style="--from:${escapeHtml(item.mode === "fill" ? item.to : item.from)};--to:${escapeHtml(item.to)}"` : ""}>${item.type === "stamp" ? (item.text ? "T" : "▣") : ""}</span><span><strong>${escapeHtml(item.name || (item.type === "color" ? "색 바꾸기" : "로고"))}</strong><small>${escapeHtml(layerSummary(item))}</small></span></button>
        <button type="button" class="layer-toggle" data-e="toggle-lock" aria-pressed="${Boolean(item.locked)}" data-tip="${item.locked ? "잠금 풀기" : "잠그기"}">${item.locked ? ICONS.lock : ICONS.unlock}</button>
        <span class="layer-order"><button type="button" data-e="layer-up" aria-label="위로" data-tip="위로" ${item.locked ? "disabled" : ""}><svg viewBox="0 0 10 6" aria-hidden="true"><path d="M1 5l4-4 4 4"/></svg></button><button type="button" data-e="layer-down" aria-label="아래로" data-tip="아래로" ${item.locked ? "disabled" : ""}><svg viewBox="0 0 10 6" aria-hidden="true"><path d="M1 1l4 4 4-4"/></svg></button></span>
        <button type="button" class="layer-delete" data-e="layer-delete" aria-label="레이어 삭제" data-tip="삭제" ${item.locked ? "disabled" : ""}>✕</button></li>`;
      list.innerHTML = s.plan.layers.length
        ? `${stamps.length ? `<li class="layer-group">로고·문구</li>${stamps.map(row).join("")}` : ""}${colors.length ? `<li class="layer-group">에셋 색</li>${colors.map(row).join("")}` : ""}`
        : '<li class="layer-empty">레이어가 없습니다. 위에서 색 바꾸기·로고·문구를 추가하세요.<br><small>로고·문구는 에셋 색 위에 쌓입니다.</small></li>';
    }

    function renderProps() {
      const props = $(".layer-props"), item = layer();
      if (!item) { props.innerHTML = ""; return; }
      const locked = item.locked ? " disabled" : "";
      const head = `<div class="props-head"><input class="props-name" data-prop="name" value="${escapeHtml(item.name || "")}" aria-label="레이어 이름" maxlength="60"${locked}>${item.locked ? '<span class="props-lock">잠김</span>' : ""}</div>`;
      if (item.type === "color") {
        const area = !item.region ? "all" : "brush";
        props.innerHTML = `${head}<fieldset${locked}>
          <div class="segmented small" role="radiogroup" aria-label="바꾸는 방식"><button type="button" data-e="mode" data-value="match" aria-pressed="${item.mode !== "fill"}">비슷한 색만</button><button type="button" data-e="mode" data-value="fill" aria-pressed="${item.mode === "fill"}">영역 전체 칠하기</button></div>
          ${item.mode === "fill"
            ? `<div class="color-pair"><label>칠할 색<input type="color" data-prop="to" value="${escapeHtml(item.to)}"></label></div><p class="hint">원래 명암은 살리고 색만 바꿉니다. 영역을 칠해 부위를 정하세요.</p>`
            : `<div class="color-pair"><label>원래 색<input type="color" data-prop="from" value="${escapeHtml(item.from)}"></label><span>→</span><label>바꿀 색<input type="color" data-prop="to" value="${escapeHtml(item.to)}"></label></div>
               <button class="secondary full" type="button" data-e="pick-color" aria-pressed="${s.tool === "pick-color"}">에셋에서 원래 색 찍기</button>${range("tolerance", "비슷한 색 범위", .01, 1, .01)}`}
          <div class="control-divider"></div>
          <span class="label">적용할 곳</span>
          <div class="segmented small"><button type="button" data-e="area" data-value="all" aria-pressed="${area === "all"}">에셋 전체</button><button type="button" data-e="area" data-value="brush" aria-pressed="${area === "brush"}">칠한 곳만</button></div>
          ${area === "brush" ? `<div class="control-row brush-tools"><button class="secondary" type="button" data-e="brush" aria-pressed="${s.tool === "brush" && !s.brush.erase}">브러시</button><button class="secondary" type="button" data-e="eraser" aria-pressed="${s.tool === "brush" && s.brush.erase}">지우개</button><button class="ghost" type="button" data-e="clear-region">영역 비우기</button></div>
            <label class="edit-range"><span>브러시 크기<output>${Math.round(s.brush.size * 100)}%</output></span><input type="range" data-brush-size min="${isMesh ? .005 : .005}" max=".3" step=".005" value="${s.brush.size}" aria-label="브러시 크기"></label>
            <label class="check-label"><input type="checkbox" data-e="show-region" ${s.showRegion ? "checked" : ""}>칠한 곳 주황색으로 표시</label>
            <p class="hint">${isMesh ? "같은 색이 여러 부위에 있어도 칠한 곳만 바뀝니다. 에셋 위를 드래그해 칠하고, 빈 곳을 드래그하면 회전합니다." : "같은 색이 여러 곳에 있어도 칠한 곳만 바뀝니다."}</p>` : ""}
        </fieldset>`;
      } else {
        const stamp = s.stamps.get(item.id);
        props.innerHTML = `${head}<fieldset${locked}>
          ${item.text !== undefined && item.text !== ""
            ? `<label class="field"><span class="label">문구</span><input data-prop="text" value="${escapeHtml(item.text)}" maxlength="80"></label>
               <div class="text-tools"><label>색<input type="color" data-prop="textColor" value="${escapeHtml(item.textColor || "#f5e3b4")}"></label><select data-prop="font" aria-label="글꼴"><option value="bold"${item.font !== "regular" && item.font !== "serif" ? " selected" : ""}>굵은 고딕</option><option value="regular"${item.font === "regular" ? " selected" : ""}>보통 고딕</option><option value="serif"${item.font === "serif" ? " selected" : ""}>명조</option></select></div>`
            : `<div class="logo-preview">${escapeHtml(stamp?.label || "로고 이미지")}</div><button class="secondary full" type="button" data-e="replace-logo">이미지 바꾸기</button>`}
          <div class="control-divider"></div>
          ${isMesh
            ? `<button class="secondary full" type="button" data-e="place" aria-pressed="${s.tool === "place"}">${item.position ? "붙일 곳 다시 찍기" : "붙일 표면 선택"}</button><p class="hint">에셋 위의 로고를 끌면 표면을 따라 움직입니다.</p>`
            : `<div class="control-row align-row"><button class="ghost" type="button" data-e="align" data-value="center">가운데</button><button class="ghost" type="button" data-e="align" data-value="x">가로 가운데</button><button class="ghost" type="button" data-e="align" data-value="y">세로 가운데</button></div>`}
          ${range("size", isMesh ? "크기 (m)" : "크기", isMesh ? .005 : .02, isMesh ? Math.max(.1, s.longest * 1.2).toFixed(3) : 1.2, isMesh ? .001 : .01)}
          ${range("rotation", "회전", -180, 180, 1)}${range("opacity", "불투명도", 0, 1, .05)}
          ${isMesh ? `<details class="edit-section"${item.clip === "projection" ? " open" : ""}><summary>표면 맞춤</summary>
            <div class="segmented small"><button type="button" data-e="clip" data-value="connected" aria-pressed="${item.clip !== "projection"}" data-tip="찍은 곳과 이어진 표면에만 붙입니다. 자물쇠·손잡이처럼 튀어나온 부위에 붙일 때 뒤쪽 면으로 번지지 않습니다.">이어진 표면만</button><button type="button" data-e="clip" data-value="projection" aria-pressed="${item.clip === "projection"}" data-tip="투영 범위 안의 앞쪽 면에 모두 찍습니다. 여러 조각으로 나뉜 면을 한꺼번에 덮을 때 씁니다.">투영 범위 전체</button></div>
            ${range("depth", "투영 깊이 (m)", .001, Math.max(.05, s.longest * .3).toFixed(3), .001, "표면에서 앞뒤로 이만큼 떨어진 곳까지 로고가 닿습니다")}</details>` : ""}
        </fieldset>`;
      }
      syncInputs(props);
    }
    function renderTone() { syncInputs($('[data-panel="tone"]')); }
    function syncInputs(root) {
      const item = layer();
      for (const input of root.querySelectorAll("[data-prop]")) {
        const key = input.dataset.prop;
        const inLayer = root.matches(".layer-props") || root.closest(".layer-props");
        const value = inLayer ? item?.[key] : s.plan[key];
        if (input.type === "range") {
          const fallback = { depth: s.longest * .035 }[key];
          input.value = value ?? fallback ?? input.min;
          const out = root.querySelector(`[data-out="${key}"]`);
          if (out) out.textContent = Number(input.value).toFixed(input.step === "1" ? 0 : Number(input.step) < .01 ? 3 : 2);
        } else if (input.type !== "color" && input.tagName !== "SELECT" && document.activeElement !== input) input.value = value ?? "";
      }
    }

    function renderFrame() {
      if (isMesh) return;
      const panel = $('[data-panel="frame"]'), frame = s.plan.frame;
      const layout = frameLayout(frame, s.source.width, s.source.height);
      const scale = (s.sourceOriginal?.width || s.asset.meta?.width || s.source.width) / s.source.width;
      const outW = frame.width || Math.round(layout.cw * scale), outH = frame.width ? layout.oh : Math.round(layout.ch * scale);
      const ratios = [["original", "원래"], ["free", "자유"], ["square", "1:1"], ["portrait", "3:4"], ["landscape", "4:3"], ["wide", "16:9"]];
      panel.innerHTML = `<h3>구성</h3>
        <div class="control-row"><button class="secondary" type="button" data-e="rotate">90° 회전</button><button class="secondary" type="button" data-e="flip" aria-pressed="${Boolean(frame.flipX)}">좌우 반전</button></div>
        <div class="field"><span class="label">자르기</span><div class="chips">${ratios.map(([value, label]) => `<button type="button" class="chip${(frame.ratio || "original") === value ? " is-active" : ""}" data-e="ratio" data-value="${value}">${label}</button>`).join("")}</div>
        <p class="hint">미리보기의 틀을 끌어 위치를 옮기고 모서리로 크기를 바꿉니다.</p></div>
        ${range("padding", "여백", 0, .5, .01)}
        <div class="field"><span class="label">여백 배경</span><div class="control-row"><div class="segmented small"><button type="button" data-e="background" data-value="" aria-pressed="${!frame.background}">투명</button><button type="button" data-e="background" data-value="color" aria-pressed="${Boolean(frame.background)}">색</button></div>${frame.background ? `<input type="color" data-frame="background" value="${escapeHtml(frame.background)}" aria-label="배경색">` : ""}</div></div>
        <label class="field"><span class="label">출력 가로 크기 <em>px</em></span><input type="number" inputmode="numeric" min="16" max="4096" step="1" data-frame="width" placeholder="원래 크기" value="${frame.width || ""}"></label>
        <p class="hint output-size">저장 크기 ${outW} × ${outH}px</p>`;
      const padding = panel.querySelector('[data-prop="padding"]');
      padding.value = frame.padding || 0;
      panel.querySelector('[data-out="padding"]').textContent = `${Math.round((frame.padding || 0) * 100)}%`;
    }

    // ---- 겹쳐 그리는 손잡이(자르기 틀, 2D 로고 상자) ------------------------------------------
    function canvasBox() {
      const area = viewport.getBoundingClientRect(), rect = canvas.getBoundingClientRect();
      return { left: rect.left - area.left, top: rect.top - area.top, width: rect.width, height: rect.height, rect };
    }
    function renderOverlay() {
      overlay.innerHTML = "";
      if (isMesh || s.compare || !s.ready || !canvas.width) return;
      const box = canvasBox();
      if (s.tab === "frame") {
        const layout = frameLayout({ ...s.plan.frame, padding: 0, width: null }, s.source.width, s.source.height);
        const crop = layout.crop;
        const el = document.createElement("div");
        el.className = "crop-box";
        el.style.cssText = `left:${box.left + crop.x / layout.rw * box.width}px;top:${box.top + crop.y / layout.rh * box.height}px;width:${crop.w / layout.rw * box.width}px;height:${crop.h / layout.rh * box.height}px`;
        el.innerHTML = '<span class="handle nw" data-handle="nw"></span><span class="handle ne" data-handle="ne"></span><span class="handle sw" data-handle="sw"></span><span class="handle se" data-handle="se"></span>';
        el.dataset.handle = "move";
        overlay.append(el);
        return;
      }
      const item = selectedStamp(), stamp = item && s.stamps.get(item.id);
      if (!item || !stamp || item.visible === false || s.tool) return;
      const width = item.size * box.width, height = width * stamp.image.height / stamp.image.width;
      const el = document.createElement("div");
      el.className = `stamp-box${item.locked ? " is-locked" : ""}`;
      el.tabIndex = 0;
      el.setAttribute("aria-label", "로고 위치. 화살표로 옮기기");
      el.style.cssText = `left:${box.left + item.x * box.width - width / 2}px;top:${box.top + item.y * box.height - height / 2}px;width:${width}px;height:${height}px;transform:rotate(${item.rotation}deg)`;
      el.dataset.handle = "move";
      if (!item.locked) el.innerHTML = '<span class="handle rotate" data-handle="rotate"></span><span class="handle se" data-handle="scale"></span>';
      overlay.append(el);
      const guides = document.createElement("div");
      guides.className = "snap-guides";
      guides.innerHTML = `<i class="v" style="left:${box.left + box.width / 2}px;top:${box.top}px;height:${box.height}px"></i><i class="h" style="top:${box.top + box.height / 2}px;left:${box.left}px;width:${box.width}px"></i>`;
      overlay.append(guides);
    }
    new ResizeObserver(() => renderOverlay()).observe(viewport);

    let drag = null;
    overlay.addEventListener("pointerdown", (event) => {
      const handle = event.target.closest("[data-handle]")?.dataset.handle;
      if (!handle || s.saving) return;
      event.preventDefault();
      event.stopPropagation();
      const box = canvasBox();
      if (s.tab === "frame") {
        remember("자르기 틀 조절");
        const layout = frameLayout({ ...s.plan.frame, padding: 0, width: null }, s.source.width, s.source.height);
        drag = { kind: "crop", handle, box, start: [event.clientX, event.clientY], crop: { x: layout.crop.x / layout.rw, y: layout.crop.y / layout.rh, w: layout.crop.w / layout.rw, h: layout.crop.h / layout.rh }, aspect: layout.rw / layout.rh };
        if (s.plan.frame.ratio === "original") s.plan.frame.ratio = "free";
      } else {
        const item = selectedStamp();
        if (!item || item.locked) return;
        remember(handle === "move" ? "로고 옮기기" : handle === "scale" ? "로고 크기" : "로고 회전");
        drag = { kind: "stamp", handle, box, start: [event.clientX, event.clientY], item: { ...item } };
      }
      window.addEventListener("pointermove", dragMove);
      window.addEventListener("pointerup", endDrag, { once: true });
      window.addEventListener("pointercancel", endDrag, { once: true });
    });
    // 끄는 동안 틀·상자를 다시 그리므로 요소가 아니라 창에서 움직임을 받는다.
    function dragMove(event) {
      if (!drag) return;
      const { box } = drag, dx = (event.clientX - drag.start[0]) / box.width, dy = (event.clientY - drag.start[1]) / box.height;
      if (drag.kind === "crop") {
        const crop = { ...drag.crop }, ratio = CROP_RATIOS[s.plan.frame.ratio];
        if (drag.handle === "move") {
          crop.x = clamp(crop.x + dx, 0, 1 - crop.w);
          crop.y = clamp(crop.y + dy, 0, 1 - crop.h);
        } else {
          const left = drag.handle.includes("w"), top = drag.handle.includes("n");
          let x0 = drag.crop.x, y0 = drag.crop.y, x1 = x0 + drag.crop.w, y1 = y0 + drag.crop.h;
          if (left) x0 = clamp(x0 + dx, 0, x1 - .02); else x1 = clamp(x1 + dx, x0 + .02, 1);
          if (top) y0 = clamp(y0 + dy, 0, y1 - .02); else y1 = clamp(y1 + dy, y0 + .02, 1);
          if (ratio) {
            // 비율 고정: 가로를 기준으로 세로를 맞추고, 넘치면 세로 기준으로 되돌린다.
            let w = x1 - x0, h = w * drag.aspect / ratio;
            if (top ? y1 - h < 0 : y0 + h > 1) { h = top ? y1 : 1 - y0; w = h * ratio / drag.aspect; }
            if (left) x0 = x1 - w; else x1 = x0 + w;
            if (top) y0 = y1 - h; else y1 = y0 + h;
          }
          Object.assign(crop, { x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
        }
        s.plan.frame.crop = crop;
      } else {
        const item = layer(drag.item.id);
        if (drag.handle === "move") {
          let x = drag.item.x + dx, y = drag.item.y + dy;
          const snapX = Math.abs(x - .5) < .015, snapY = Math.abs(y - .5) < .015;
          if (snapX) x = .5;
          if (snapY) y = .5;
          item.x = clamp(x, -.5, 1.5);
          item.y = clamp(y, -.5, 1.5);
          overlay.classList.toggle("snap-x", snapX);
          overlay.classList.toggle("snap-y", snapY);
        } else {
          const center = [box.rect.left + drag.item.x * box.width, box.rect.top + drag.item.y * box.height];
          if (drag.handle === "scale") {
            const before = Math.hypot(drag.start[0] - center[0], drag.start[1] - center[1]) || 1;
            item.size = clamp(drag.item.size * Math.hypot(event.clientX - center[0], event.clientY - center[1]) / before, .02, 1.5);
          } else {
            let angle = Math.atan2(event.clientY - center[1], event.clientX - center[0]) * 180 / Math.PI + 90;
            if (event.shiftKey) angle = Math.round(angle / 15) * 15;
            item.rotation = Math.round(((angle + 540) % 360) - 180);
          }
        }
      }
      changed();
    }
    function endDrag() {
      window.removeEventListener("pointermove", dragMove);
      if (!drag) return;
      drag = null;
      overlay.classList.remove("snap-x", "snap-y");
      renderOverlay();
    }
    overlay.addEventListener("keydown", (event) => {
      const item = selectedStamp();
      if (!item || item.locked || !event.target.matches(".stamp-box")) return;
      const step = event.shiftKey ? .05 : .005, moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (!moves[event.key]) return;
      event.preventDefault();
      remember("로고 옮기기", `nudge:${item.id}`);
      item.x = clamp(item.x + moves[event.key][0], -.5, 1.5);
      item.y = clamp(item.y + moves[event.key][1], -.5, 1.5);
      changed();
      overlay.querySelector(".stamp-box")?.focus();
    });

    // ---- 에셋 위 조작: 색 찍기, 로고 붙이기·끌기, 브러시 ----------------------------------------
    let pointer = null;
    function surfaceHit(event) {
      const hit = viewer.positionAndNormalFromPoint(event.clientX, event.clientY);
      return hit ? { position: [hit.position.x, hit.position.y, hit.position.z], normal: [hit.normal.x, hit.normal.y, hit.normal.z], uv: hit.uv, event } : null;
    }
    function imagePoint(event) {
      const rect = canvas.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width, y = (event.clientY - rect.top) / rect.height;
      return { x, y, inside: x >= 0 && x <= 1 && y >= 0 && y <= 1 };
    }
    function addStroke(point) {
      const item = layer();
      if (!item?.region) return false;
      const strokes = item.region.strokes, mode = s.brush.erase ? 0 : 1;
      let stroke;
      if (isMesh) stroke = [...point.position.map((v) => Number(v.toFixed(5))), Number((s.brush.size * s.longest).toFixed(5)), mode];
      else {
        const [u, v] = outputToSource(s.plan.frame, s.source.width, s.source.height, point.x, point.y);
        stroke = [Number(u.toFixed(5)), Number(v.toFixed(5)), s.brush.size, mode];
      }
      const last = strokes.at(-1), radius = stroke.at(-2);
      if (last && last.at(-1) === mode) {
        const distance = isMesh ? Math.hypot(last[0] - stroke[0], last[1] - stroke[1], last[2] - stroke[2]) : Math.hypot(last[0] - stroke[0], (last[1] - stroke[1]) * s.source.height / s.source.width);
        if (distance < radius * .35) return false;
      }
      strokes.push(stroke);
      return true;
    }
    // 캡처 단계에서 가로채야 model-viewer가 같은 드래그로 카메라를 돌리지 않는다.
    viewport.addEventListener("pointerdown", (event) => {
      if (!s.ready || !s.loaded || s.saving || s.compare || event.button !== 0 || event.target.closest(".editor-overlay [data-handle]")) return;
      const item = layer();
      pointer = { start: [event.clientX, event.clientY], moved: false };
      if (s.tool === "brush" && item?.type === "color" && item.region && !item.locked) {
        const point = isMesh ? surfaceHit(event) : imagePoint(event);
        if (!point || (!isMesh && !point.inside)) return;
        event.preventDefault();
        event.stopPropagation();
        remember(s.brush.erase ? "영역 지우기" : "영역 칠하기");
        pointer.brush = true;
        followPointer();
        if (addStroke(point)) changed();
        return;
      }
      if (isMesh && !s.tool && item?.type === "stamp" && item.position && !item.locked && item.visible !== false) {
        const hit = surfaceHit(event);
        const reach = Math.max(item.size * .6, s.longest * .02);
        if (hit && Math.hypot(...hit.position.map((v, i) => v - item.position[i])) < reach) {
          event.preventDefault();
          event.stopPropagation();
          remember("로고 옮기기");
          pointer.stamp = true;
          followPointer();
          viewport.classList.add("is-grabbing");
        }
      }
    }, true);
    let frameRequest = 0;
    function followPointer() {
      window.addEventListener("pointermove", pointerMove, true);
      window.addEventListener("pointerup", pointerEnd, { capture: true, once: true });
    }
    function pointerMove(event) {
      if (!pointer || (!pointer.brush && !pointer.stamp)) return;
      event.stopPropagation();
      if (frameRequest) return;
      const { clientX, clientY } = event;
      frameRequest = requestAnimationFrame(() => {
        frameRequest = 0;
        const fake = { clientX, clientY };
        const item = layer();
        if (pointer?.brush) {
          const point = isMesh ? surfaceHit(fake) : imagePoint(fake);
          if (point && (isMesh || point.inside) && addStroke(point)) { queue(); scheduleDraft(); }
        } else if (pointer?.stamp && item) {
          const hit = surfaceHit(fake);
          if (hit) { item.position = hit.position; item.normal = hit.normal; queue(); scheduleDraft(); }
        }
      });
    }
    function pointerEnd() {
      window.removeEventListener("pointermove", pointerMove, true);
      pointer = null;
      viewport.classList.remove("is-grabbing");
      changed();
    }
    viewport.addEventListener("pointerup", (event) => {
      const state = pointer;
      if (!state || state.brush || state.stamp) return;
      pointer = null;
      if (Math.hypot(event.clientX - state.start[0], event.clientY - state.start[1]) > 6 || !s.ready || !s.loaded) return;
      const item = layer();
      if (s.tool === "pick-color") {
        if (isMesh) {
          const hit = surfaceHit(event), material = viewer.materialFromPoint(event.clientX, event.clientY);
          if (!hit?.uv) return toast("에셋 표면을 클릭해 주세요.");
          s.worker.postMessage({ type: "sample", material: material?.index ?? 0, uv: [hit.uv.u ?? hit.uv.x, hit.uv.v ?? hit.uv.y] });
        } else {
          const point = imagePoint(event);
          if (point.inside) s.worker.postMessage({ type: "sample", uv: outputToSource(s.plan.frame, s.source.width, s.source.height, point.x, point.y) });
        }
      } else if (isMesh && item?.type === "stamp" && (s.tool === "place" || !item.position) && !item.locked) {
        const hit = surfaceHit(event);
        if (!hit) return toast("에셋 표면을 클릭해 주세요.");
        remember("로고 붙이기");
        item.position = hit.position;
        item.normal = hit.normal;
        item.depth ??= Number((s.longest * .035).toFixed(4));
        setTool(null);
        changed();
      }
    });
    viewport.addEventListener("wheel", (event) => {
      const item = selectedStamp();
      if (!isMesh || !item?.position || item.locked || (!event.altKey && !event.shiftKey)) return;
      event.preventDefault();
      event.stopPropagation();
      const delta = (event.deltaY || event.deltaX) > 0 ? -1 : 1;
      if (event.altKey) {
        remember("로고 크기", `wheel-size:${item.id}`);
        item.size = clamp(item.size * (1 + delta * .05), .005, s.longest * 1.2);
      } else {
        remember("로고 회전", `wheel-rotate:${item.id}`);
        item.rotation = ((item.rotation + delta * 3 + 540) % 360) - 180;
      }
      changed();
    }, { capture: true, passive: false });

    // ---- 레이어 조작 -----------------------------------------------------------------
    function addLayer(item, label) {
      remember(label);
      s.plan.layers.push(item);
      // 로고·문구는 에셋 색 위에 쌓인다. 목록 순서도 색 → 로고로 맞춘다.
      s.plan.layers.sort((a, b) => (a.type === b.type ? 0 : a.type === "color" ? -1 : 1));
      s.selected = item.id;
      s.tab = "layers";
      showTab();
    }
    async function setStampImage(item, blob, label) {
      s.stamps.set(item.id, { image: await decodeBlob(blob), blob, upload: null, label });
      s.worker.postMessage({ type: "stamp", id: item.id, image: s.stamps.get(item.id).image });
    }
    function newStampLayer(fields) {
      const base = stampLayer({ id: layerId("stamp", s.plan.layers), ...fields });
      if (isMesh) Object.assign(base, { size: Number((s.longest * .25).toFixed(3)), depth: Number((s.longest * .035).toFixed(4)), clip: "connected" });
      return base;
    }
    function moveLayer(id, direction) {
      const index = s.plan.layers.findIndex((item) => item.id === id);
      // 화면 목록은 위가 나중(위에 쌓임)이다. '위로'는 배열에서 뒤로 보낸다.
      const target = index + (direction === "up" ? 1 : -1);
      const other = s.plan.layers[target];
      if (!other || other.type !== s.plan.layers[index].type || other.locked) return;
      remember("레이어 순서");
      [s.plan.layers[index], s.plan.layers[target]] = [s.plan.layers[target], s.plan.layers[index]];
    }
    function showTab() {
      for (const button of dialog.querySelectorAll("[data-tab]")) button.setAttribute("aria-selected", String(button.dataset.tab === s.tab));
      for (const panel of dialog.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== s.tab;
      if (s.tool) setTool(null);
    }
    let textTimer = null;
    function refreshText(item) {
      clearTimeout(textTimer);
      textTimer = setTimeout(async () => {
        if (!item.text) return;
        await setStampImage(item, await textStamp(item.text, item.textColor || "#f5e3b4", item.font), item.text);
        queue();
        scheduleDraft();
      }, 200);
    }

    // 입력 요소: 레이어 속성, 전체 톤, 구성.
    dialog.addEventListener("input", (event) => {
      const input = event.target;
      if (input.matches("[data-brush-size]")) {
        s.brush.size = Number(input.value);
        input.previousElementSibling.querySelector("output").textContent = `${Math.round(s.brush.size * 100)}%`;
        return;
      }
      if (input.id === "edit-name") { s.dirty = true; scheduleDraft(); updateSave(); return; }
      if (input.matches("[data-frame]")) {
        const key = input.dataset.frame;
        remember(key === "width" ? "출력 크기" : "배경색", `frame:${key}`);
        s.plan.frame[key] = key === "width" ? (Number(input.value) >= 16 ? Math.min(4096, Math.round(Number(input.value))) : null) : input.value;
        queue();
        scheduleDraft();
        renderFrameSize();
        return;
      }
      const key = input.dataset.prop;
      if (!key) return;
      const inLayer = Boolean(input.closest(".layer-props"));
      const target = inLayer ? layer() : key === "padding" ? s.plan.frame : s.plan;
      if (!target || (inLayer && target.locked)) return;
      const value = input.type === "range" || input.type === "number" ? Number(input.value) : input.value;
      remember(labelFor(key), `${inLayer ? target.id : "plan"}:${key}`);
      target[key] = value;
      if (inLayer && ["text", "textColor", "font"].includes(key)) refreshText(target);
      if (input.type === "range") {
        const out = input.closest(".edit-range")?.querySelector("output");
        if (out) out.textContent = key === "padding" ? `${Math.round(value * 100)}%` : value.toFixed(input.step === "1" ? 0 : Number(input.step) < .01 ? 3 : 2);
      }
      if (key === "name") { renderLayers(); scheduleDraft(); updateSave(); return; }
      if (input.type === "range" || input.type === "color" || key === "text") {
        renderLayers();
        if (key === "padding") renderFrameSize();
        renderOverlay();
        scheduleDraft();
        queue();
      } else changed();
    });
    function renderFrameSize() {
      const layout = frameLayout(s.plan.frame, s.source.width, s.source.height);
      const scale = (s.asset.meta?.width || s.source.width) / s.source.width;
      const line = $(".output-size");
      if (line) line.textContent = `저장 크기 ${s.plan.frame.width || Math.round(layout.cw * scale)} × ${s.plan.frame.width ? layout.oh : Math.round(layout.ch * scale)}px`;
    }
    const labelFor = (key) => ({
      name: "이름 바꾸기", from: "원래 색", to: "바꿀 색", tolerance: "색 범위", size: "로고 크기", rotation: "로고 회전", opacity: "불투명도",
      depth: "투영 깊이", text: "문구 고치기", textColor: "문구 색", font: "글꼴", brightness: "밝기", contrast: "대비", saturation: "채도",
      metallic: "금속성", roughness: "거칠기", padding: "여백",
    }[key] || "편집");

    dialog.addEventListener("click", async (event) => {
      const tab = event.target.closest("[data-tab]");
      if (tab) {
        s.tab = tab.dataset.tab;
        showTab();
        renderFrame();
        $(".viewport-hint").textContent = viewportHint();
        queue();
        return;
      }
      const button = event.target.closest("[data-e]");
      if (!button || button.disabled) return;
      const row = button.closest("[data-layer]"), rowId = row?.dataset.layer, item = layer();
      try {
        switch (button.dataset.e) {
          case "close": close(); break;
          case "select": s.selected = rowId; if (s.tool) s.tool = null; dialog.classList.remove("is-picking", "is-brushing"); changed({ draft: false }); break;
          case "toggle-visible": { const target = layer(rowId); remember(target.visible === false ? "레이어 보이기" : "레이어 숨기기"); target.visible = target.visible === false; changed(); break; }
          case "toggle-lock": { const target = layer(rowId); remember(target.locked ? "잠금 풀기" : "레이어 잠그기"); target.locked = !target.locked; if (target.locked && s.selected === rowId) s.tool = null; changed(); break; }
          case "layer-up": case "layer-down": moveLayer(rowId, button.dataset.e === "layer-up" ? "up" : "down"); changed(); break;
          case "layer-delete": {
            const target = layer(rowId);
            if (target.locked) break;
            remember("레이어 삭제");
            s.plan.layers = s.plan.layers.filter((entry) => entry.id !== rowId);
            if (s.selected === rowId) s.selected = s.plan.layers.at(-1)?.id || null;
            setTool(null);
            changed();
            toast("레이어를 삭제했습니다. ⌘Z로 되돌릴 수 있습니다.");
            break;
          }
          case "add-color": addLayer(colorLayer({ id: layerId("color", s.plan.layers), name: `색 바꾸기 ${s.plan.layers.filter((l) => l.type === "color").length + 1}` }), "색 바꾸기 추가"); changed(); break;
          case "add-logo": {
            const file = await pickFile();
            if (!file) break;
            const created = newStampLayer({ name: file.name.replace(/\.[^.]+$/, "").slice(0, 60) || "로고" });
            await setStampImage(created, file, file.name);
            addLayer(created, "로고 추가");
            if (isMesh) setTool("place");
            changed();
            break;
          }
          case "add-text": {
            const text = "TEXT";
            const created = newStampLayer({ name: "문구", text, textColor: "#f5e3b4", font: "bold" });
            await setStampImage(created, await textStamp(text, created.textColor), text);
            addLayer(created, "문구 추가");
            if (isMesh) setTool("place");
            changed();
            requestAnimationFrame(() => { const input = $('.layer-props [data-prop="text"]'); input?.focus(); input?.select(); });
            break;
          }
          case "replace-logo": {
            const file = await pickFile();
            if (!file || !item) break;
            remember("로고 이미지 바꾸기");
            await setStampImage(item, file, file.name);
            changed();
            break;
          }
          case "mode":
            remember("바꾸는 방식");
            item.mode = button.dataset.value;
            // 영역 칠하기는 보통 한 부위를 칠한다. 에셋 전체를 한 색으로 칠하려면 '에셋 전체'를 고른다.
            if (item.mode === "fill" && !item.region) { item.region = { strokes: [] }; s.brush.erase = false; s.tool = "brush"; dialog.classList.add("is-brushing"); }
            changed();
            break;
          case "area":
            remember("적용할 곳");
            item.region = button.dataset.value === "all" ? null : item.region || { strokes: [] };
            setTool(button.dataset.value === "all" ? null : "brush");
            s.brush.erase = false;
            changed();
            break;
          case "brush": case "eraser": {
            const erase = button.dataset.e === "eraser", active = s.tool === "brush" && s.brush.erase === erase;
            s.brush.erase = erase;
            setTool(active ? null : "brush");
            break;
          }
          case "clear-region": if (item?.region?.strokes.length) { remember("영역 비우기"); item.region.strokes = []; changed(); } break;
          case "show-region": s.showRegion = button.checked; queue(); break;
          case "pick-color": setTool(s.tool === "pick-color" ? null : "pick-color"); break;
          case "place": setTool(s.tool === "place" ? null : "place"); break;
          case "clip": remember("표면 맞춤"); item.clip = button.dataset.value; changed(); break;
          case "align": {
            remember("로고 정렬");
            if (button.dataset.value !== "y") item.x = .5;
            if (button.dataset.value !== "x") item.y = .5;
            changed();
            break;
          }
          case "rotate": {
            remember("90° 회전");
            const frame = s.plan.frame;
            frame.turns = (frame.turns + 1) % 4;
            // 돌리면 자르기 좌표계가 바뀐다. 비율을 고른 경우 같은 비율로 가운데를 다시 잡는다.
            if (CROP_RATIOS[frame.ratio]) {
              const layout = frameLayout({ turns: frame.turns, flipX: frame.flipX, crop: frame.ratio }, s.source.width, s.source.height);
              frame.crop = { x: layout.crop.x / layout.rw, y: layout.crop.y / layout.rh, w: layout.crop.w / layout.rw, h: layout.crop.h / layout.rh };
            } else { frame.crop = null; frame.ratio = "original"; }
            changed();
            break;
          }
          case "flip": remember("좌우 반전"); s.plan.frame.flipX = !s.plan.frame.flipX; if (s.plan.frame.crop && typeof s.plan.frame.crop === "object") s.plan.frame.crop.x = 1 - s.plan.frame.crop.x - s.plan.frame.crop.w; changed(); break;
          case "ratio": {
            remember("자르기 비율");
            const value = button.dataset.value, frame = s.plan.frame;
            frame.ratio = value;
            if (value === "original") frame.crop = null;
            else if (value === "free") frame.crop = frame.crop && typeof frame.crop === "object" ? frame.crop : { x: .05, y: .05, w: .9, h: .9 };
            else {
              const layout = frameLayout({ turns: frame.turns, flipX: frame.flipX, crop: value }, s.source.width, s.source.height);
              frame.crop = { x: layout.crop.x / layout.rw, y: layout.crop.y / layout.rh, w: layout.crop.w / layout.rw, h: layout.crop.h / layout.rh };
            }
            changed();
            break;
          }
          case "background": remember("여백 배경"); s.plan.frame.background = button.dataset.value ? s.plan.frame.background || "#ffffff" : null; changed(); break;
          case "undo": undo(); break;
          case "redo": redo(); break;
          case "history": openHistory(); break;
          case "reset": if (s.plan.layers.length || JSON.stringify(s.plan) !== JSON.stringify(migratePlan(defaults()))) { remember("처음 상태로"); s.plan = migratePlan(defaults()); s.selected = null; setTool(null); changed(); } break;
          case "compare": s.compare = !s.compare; button.setAttribute("aria-pressed", String(s.compare)); button.textContent = s.compare ? "편집본 보기" : "원본 비교"; setTool(null); queue(); break;
          case "save": await saveVersion(); break;
          case "cancel-save": if (s.savingJob) await api(`/api/jobs/${s.savingJob}/cancel`, { method: "POST" }); break;
          case "favorite": {
            const next = button.getAttribute("aria-pressed") !== "true";
            await api(`/api/jobs/${job.id}/assets/${asset.id}/library`, { method: "POST", body: { favorite: next } });
            button.setAttribute("aria-pressed", String(next));
            asset.favorite = next;
            refreshJobs();
            break;
          }
          case "versions": openVersions(); break;
          case "to3d": if (s.dirty) await saveVersion((saved) => makeMesh(saved.id, saved.assets[0].id)); else { close(); makeMesh(job.id, asset.id); } break;
          case "previz": if (s.dirty) await saveVersion((saved) => addToPrevizScene(saved.id, saved.assets[0].id)); else { close(); addToPrevizScene(job.id, asset.id); } break;
          case "download":
            if (s.dirty) {
              event.preventDefault();
              await saveVersion(async (saved) => {
                await openAsset(saved.id, saved.assets[0].id);
                const link = document.createElement("a");
                link.href = fileUrl(saved.id, saved.assets[0].file);
                link.download = "";
                link.click();
              });
            }
            break;
          case "info": openInfo(); break;
        }
      } catch (error) {
        finishSaving();
        status(error.message);
        toast(error.message);
      }
    });

    // ---- 저장 -------------------------------------------------------------------------
    function finishSaving() {
      s.saving = false;
      s.savingJob = null;
      $(".editor-panel").inert = false;
      $(".editor-foot").inert = false;
      $('[data-e="compare"]').disabled = false;
      $(".save-queue").hidden = true;
      updateSave();
    }
    async function saveVersion(next = null) {
      if (s.saving) return;
      const unplaced = s.plan.layers.find((item) => item.type === "stamp" && item.visible !== false && isMesh && !item.position);
      if (unplaced) { s.selected = unplaced.id; setTool("place"); changed({ draft: false }); return toast("로고를 붙일 표면을 먼저 선택해 주세요."); }
      s.saving = true;
      $(".editor-panel").inert = true;
      $(".editor-foot").inert = true;
      $('[data-e="compare"]').disabled = true;
      setTool(null);
      updateSave();
      status("새 버전을 준비하고 있습니다…");
      const plan = clone(s.plan);
      for (const item of plan.layers) {
        if (item.type !== "stamp") continue;
        const entry = s.stamps.get(item.id);
        if (!entry?.blob) throw new Error(`'${item.name || "로고"}' 레이어의 그림을 찾지 못했습니다. 이미지를 다시 넣어 주세요.`);
        if (!entry.upload) { entry.upload = await uploadBlob(entry.blob); scheduleDraft(); }
        item.uploadId = entry.upload.id;
      }
      const created = await api("/api/jobs", { method: "POST", body: { recipe: "edit-asset", params: { source: { jobId: job.id, assetId: asset.id }, name: $("#edit-name").value, replaceEdits: Boolean(asset.meta?.editBaseFile), plan } } });
      s.savingJob = created.id;
      quietJob(created.id);
      await refreshJobs();
      const started = Date.now();
      const completed = await waitJob(api, created.id, {
        onUpdate: (record) => {
          if (s.closed) return;
          const queued = record.state === "queued";
          $(".save-queue").hidden = false;
          $(".save-queue span").textContent = queued ? `앞선 작업이 끝나면 저장합니다 · ${Math.round((Date.now() - started) / 1000)}초째 대기` : "새 버전을 저장하고 있습니다…";
          status(queued ? "대기 중 — 창을 닫아도 순서가 되면 저장됩니다" : "새 버전을 저장하고 있습니다…");
        },
      });
      s.saved = true;
      clearTimeout(s.draftTimer);
      drafts.delete(s.key);
      await writeDraft(s.key, null);
      const wasOpen = !s.closed;
      if (wasOpen) close();
      await refreshJobs();
      if (wasOpen) {
        if (next) await next(completed);
        else await openAsset(completed.id, completed.assets[0].id);
      }
      toast(wasOpen ? "편집본을 새 버전으로 저장했습니다." : `'${completed.title}'을 새 버전으로 저장했습니다.`);
    }

    // ---- 팝업: 작업 내역, 버전 이력, 정보·정리 --------------------------------------------------
    function openHistory() {
      const entries = [{ label: "열었을 때" }, ...s.past.map((entry) => ({ label: entry.label }))];
      const future = [...s.future].reverse();
      const dialogBox = popup("작업 내역", `<p class="hint">항목을 누르면 그 단계로 돌아갑니다. 저장하기 전까지의 편집만 기록됩니다.</p>
        <ol class="history-list">${entries.map((entry, index) => `<li><button type="button" data-step="${index}" class="${index === entries.length - 1 ? "is-current" : ""}">${escapeHtml(entry.label)}${index === entries.length - 1 ? "<small>현재</small>" : ""}</button></li>`).join("")}
        ${future.map((entry, index) => `<li><button type="button" data-redo="${index + 1}" class="is-future">${escapeHtml(entry.label)}<small>되돌린 단계</small></button></li>`).join("")}</ol>`);
      dialogBox.addEventListener("click", (event) => {
        const step = event.target.closest("[data-step]"), forward = event.target.closest("[data-redo]");
        if (step) jumpTo(Number(step.dataset.step));
        else if (forward) for (let i = 0; i < Number(forward.dataset.redo); i++) redo();
        else return;
        dialogBox.close();
      });
    }

    async function openVersions() {
      const data = await api(`/api/jobs/${job.id}/assets/${asset.id}/versions`);
      const date = (iso) => { const value = new Date(iso); return Number.isNaN(value.getTime()) ? "" : value.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
      const box = popup("버전 이력", `<p class="hint">원본에서 어떤 편집을 거쳐 왔는지 보여 줍니다. 이전 버전을 열어 편집하면 그 버전에서 새 갈래로 저장됩니다.</p>
        <ol class="version-tree">${data.versions.map((node) => `<li style="--depth:${node.depth}" class="${node.key === data.current ? "is-current" : ""}">
          <button type="button" data-open="${escapeHtml(node.jobId)}|${escapeHtml(node.assetId)}">
            ${node.preview ? `<img src="${fileUrl(node.jobId, node.preview)}" alt="" loading="lazy">` : '<span class="version-blank"></span>'}
            <span class="version-text"><strong>${escapeHtml(node.relation || (node.missingParent ? "원본(이전 기록 없음)" : "원본"))}${node.favorite ? " ★" : ""}</strong>
            <small>${escapeHtml([node.kind === "mesh" ? "3D" : "2D", node.summary.join(" · "), date(node.createdAt)].filter(Boolean).join(" · "))}</small>
            <small class="version-title">${escapeHtml(node.title)}</small></span>
            ${node.key === data.current ? '<span class="version-badge">지금 편집 중</span>' : ""}
          </button></li>`).join("")}</ol>`, { wide: true });
      box.addEventListener("click", (event) => {
        const target = event.target.closest("[data-open]");
        if (!target) return;
        const [jobId, assetId] = target.dataset.open.split("|");
        if (`${jobId}/${assetId}` === data.current) return box.close();
        box.close();
        close();
        openAsset(jobId, assetId);
      });
    }

    async function openInfo() {
      const meta = asset.meta || {}, stats = meta.stats || {};
      let collections = [];
      try { collections = [...new Set((await api("/api/assets?limit=2000")).assets.map((item) => item.collection).filter(Boolean))].sort(); } catch { /* 목록 없이도 적을 수 있다 */ }
      const info = popup("정보·정리", `<dl class="meta"><dt>파일</dt><dd>${escapeHtml(asset.file)}</dd><dt>크기</dt><dd>${isMesh ? formatBytes(stats.bytes) : `${meta.width} × ${meta.height}px`}</dd>${isMesh ? `<dt>면 수</dt><dd>${(stats.facesOut || 0).toLocaleString()}</dd><dt>텍스처</dt><dd>${meta.textureSize || "–"}px</dd>` : ""}<dt>시드</dt><dd>${meta.seed ?? "–"}</dd>${meta.model ? `<dt>생성 모델</dt><dd>${escapeHtml(job.params?.imageModelConfig?.label || meta.model)}</dd>` : ""}</dl>
        ${meta.inspectionFile ? `<a class="secondary button-link" href="${fileUrl(job.id, meta.inspectionFile)}" target="_blank" rel="noopener">여섯 방향 보기</a>` : ""}
        ${stats.topology?.warnings?.length ? `<p class="notice">${escapeHtml(stats.topology.warnings.join(" "))}</p>` : ""}
        <div class="control-divider"></div><h3>정리</h3>
        <label class="field"><span class="label">컬렉션 <em>프로젝트·용도</em></span><input data-library="collection" list="collection-options" maxlength="60" value="${escapeHtml(asset.collection || "")}" placeholder="예: 던전 1장"><datalist id="collection-options">${collections.map((name) => `<option value="${escapeHtml(name)}">`).join("")}</datalist></label>
        <label class="field"><span class="label">태그 <em>쉼표로 구분</em></span><input data-library="tags" maxlength="400" value="${escapeHtml((asset.tags || []).join(", "))}" placeholder="예: 상자, 보상, 파란색"></label>
        <label class="field"><span class="label">메모</span><textarea data-library="note" rows="2" maxlength="500" placeholder="고른 이유나 쓸 곳">${escapeHtml(asset.note || "")}</textarea></label>
        <p class="hint library-saved" role="status"></p>
        ${meta.sourceStateFile ? '<div class="control-divider"></div><h3>원본에서 다시 구성</h3><p class="hint">생성한 원본으로 표면과 재질을 다시 만듭니다.</p><button class="secondary" type="button" data-refine>고품질 원본 다시 구성</button>' : ""}`);
      info.addEventListener("change", async (event) => {
        const field = event.target.dataset.library;
        if (!field) return;
        const value = field === "tags" ? event.target.value.split(",").map((tag) => tag.trim()).filter(Boolean) : event.target.value;
        try {
          const updated = await api(`/api/jobs/${job.id}/assets/${asset.id}/library`, { method: "POST", body: { [field]: value } });
          Object.assign(asset, { collection: undefined, tags: undefined, note: undefined }, updated.assets.find((item) => item.id === asset.id));
          info.querySelector(".library-saved").textContent = "저장했습니다.";
          refreshJobs();
        } catch (error) {
          info.querySelector(".library-saved").textContent = error.message;
        }
      });
      info.querySelector("[data-refine]")?.addEventListener("click", async (event) => {
        event.target.disabled = true;
        try {
          await api("/api/jobs", { method: "POST", body: { recipe: "refine-mesh", params: { source: { jobId: job.id, assetId: asset.id }, gameFaces: 0 } } });
          info.close();
          toast("원본 재구성을 시작했습니다.");
          refreshJobs();
        } catch (error) {
          toast(error.message);
          event.target.disabled = false;
        }
      });
    }

    // ---- 키보드와 닫기 ----------------------------------------------------------------
    dialog.addEventListener("keydown", (event) => {
      const typing = event.target.matches("input:not([type=range]):not([type=checkbox]),textarea,select");
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z" && !typing) {
        event.preventDefault();
        if (event.shiftKey) redo(); else undo();
      } else if (!typing && s.tool === "brush" && (event.key === "[" || event.key === "]")) {
        s.brush.size = clamp(s.brush.size * (event.key === "]" ? 1.2 : 1 / 1.2), .005, .3);
        renderProps();
      } else if (event.key === "Escape" && s.tool) {
        event.preventDefault();
        setTool(null);
      }
    });
    dialog.addEventListener("close", () => {
      if (!s.saved && s.dirty) flushDraft();
      s.closed = true;
      clearTimeout(s.timer);
      window.removeEventListener("pagehide", flushDraft);
      document.removeEventListener("visibilitychange", onHide);
      s.worker.terminate();
      dialog.remove();
      if (session === s) session = null;
    });

    showTab();
    changed({ draft: false });
    status("불러오는 중…");
  }

  return { open, close };
}
