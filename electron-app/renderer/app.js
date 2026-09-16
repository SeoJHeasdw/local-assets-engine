import {
  JOB_STATE_LABELS, RECIPE_LABELS, REVIEW_LABELS, STAGE_STATE_LABELS,
  buildJobRequest, elapsedSeconds, escapeHtml, fileUrl, formatBytes, formatDuration, isActive,
} from "../shared/format.mjs";
import { createPreviz } from "./previz.js";
import { installResizer } from "./resize.js";
import { installTooltips } from "./tooltip.js";
import { popup, toast, pickFile, uploadBlob, waitJob } from "./ui.js";
import { createEditor } from "./editor.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
// Electron preload가 주는 기능. 일반 브라우저로 열면 없고, 파일 고르기·Finder 열기만 빠진다.
const bridge = window.assetsStudio || null;

const CAPABILITY_LABELS = {
  image2d: "2D 에셋 생성",
  mesh3d: "3D 에셋 생성",
  meshTexture: "원본 PBR 텍스처 굽기",
  gameReady: "게임용 GLB 최적화",
  previz: "프리비즈 샷 렌더",
};
const PIXEL_FILE = /px(@preview)?\.png$/;

const state = {
  presets: null,
  kind: "3d",
  uploadId: null, imageSource: null, imageName: "",
  source: "text",
  presetId: { "2d": "item-icon", "3d": "prop-3d" },
  imagePath: "",
  imagePreviewUrl: "",
  jobs: [],
  signatures: new Map(),
  doctor: null,
  libraryFilter: "pending",
  view: "create",
};

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : `요청이 실패했습니다. (${response.status})`);
  }
  return data;
}

const previz = createPreviz({
  api, bridge, openAsset, refreshJobs,
  getJobs: () => state.jobs,
  getPresets: () => state.presets,
  goCreate: () => showView("create"),
});

const editor = createEditor({api, refreshJobs, openAsset, makeMesh, addToPrevizScene, bridge});

// ---- 화면 전환 -------------------------------------------------------------

function showView(view) {
  state.view = view;
  for (const button of $$(".nav-item")) button.classList.toggle("is-active", button.dataset.view === view);
  for (const section of $$(".view")) section.hidden = section.id !== `view-${view}`;
  if (view === "library") refreshLibrary();
  if (view === "previz") previz.show();
  if (view === "system") refreshSystem();
}

// 프리비즈 배치처럼 넓은 편집 공간이 필요할 때 사이드바를 접는다. 단추는 창 신호등 옆에 늘 같은 자리다.
const SIDEBAR_CLOSED_KEY = "assets-studio.sidebar-closed";

function setSidebar(closed, { save = true } = {}) {
  $(".shell").classList.toggle("is-sidebar-closed", closed);
  const toggle = $("#sidebar-toggle");
  toggle.setAttribute("aria-expanded", String(!closed));
  toggle.setAttribute("data-tip", closed ? "사이드바 펼치기" : "사이드바 접기 — 편집 공간을 넓힙니다");
  if (save) {
    try { localStorage.setItem(SIDEBAR_CLOSED_KEY, closed ? "1" : ""); } catch { /* 기억하지 못해도 접기는 된다 */ }
  }
  // 편집 패널의 최대 너비가 사이드바 너비에 달려 있어 한도를 다시 잰다.
  window.dispatchEvent(new Event("resize"));
}

// ---- 만들기 ----------------------------------------------------------------

function presetsFor(kind) {
  return (state.presets?.presets || []).filter((preset) => preset.kind === kind);
}

function renderComposer() {
  const { kind, source } = state;
  const fromImage = kind === "3d" && source === "image";
  for (const button of $$("#kind-switch button")) { button.classList.toggle("is-active", button.dataset.kind === kind); button.setAttribute("aria-selected", String(button.dataset.kind === kind)); }
  for (const button of $$("#source-switch button")) button.classList.toggle("is-active", button.dataset.source === source);
  $("#source-switch").hidden = kind !== "3d";
  $("#preset-field").hidden = fromImage;
  $("#subject-field").hidden = fromImage;
  $("#style-field").hidden = fromImage;
  $("#count-field").hidden = fromImage || (kind === "3d" && workflow() === "direct");
  $("#workflow-field").hidden = kind !== "3d" || fromImage;
  $("#background-field").hidden = kind === "3d";
  $("#prompt-examples").hidden = fromImage;
  $("#image-field").hidden = !fromImage;
  for (const option of $$(".mesh-option")) option.hidden = kind !== "3d";

  const presets = presetsFor(kind);
  if (!presets.some((preset) => preset.id === state.presetId[kind]) && presets[0]) state.presetId[kind] = presets[0].id;
  $("#preset-chips").innerHTML = presets.map((preset) => `
    <button type="button" class="chip${preset.id === state.presetId[kind] ? " is-active" : ""}" data-preset="${escapeHtml(preset.id)}">
      ${escapeHtml(preset.label)}
    </button>`).join("");
  $("#preset-hint").textContent = presets.find((preset) => preset.id === state.presetId[kind])?.hint || "";
  const preset = presets.find(p => p.id === state.presetId[kind]);
  if (state.lastPreset !== preset?.id) { $("#remove-background").checked = !!preset?.removeBackground; state.lastPreset = preset?.id; }
  $("#preset-field").hidden = fromImage || kind === "3d";
  renderImagePick();
  updateSubmitLabel();
}

function workflow() { return $('input[name="workflow"]:checked')?.value || "concept"; }
function updateSubmitLabel() {
  const count = Number($("#count").value);
  let label = `이미지 ${count}장 만들기`, hint = "완성된 이미지를 열어 색상과 로고를 편집할 수 있습니다.";
  if (state.kind === "3d") {
    if (state.source === "image") [label, hint] = ["이 이미지로 3D 만들기", "선택한 이미지를 고품질 3D 원본으로 만듭니다."];
    else if (workflow() === "direct") [label, hint] = ["3D 만들기", "컨셉 이미지 1장부터 3D 완성까지 진행합니다."];
    else [label, hint] = [`컨셉 ${count}장 만들기`, "이미지를 고르고 확인한 뒤 3D로 만들 수 있습니다."];
  }
  $("#count-field").hidden = state.kind === "3d" && (state.source === "image" || workflow() === "direct");
  $("#submit").textContent = label;
  $("#submit-hint").textContent = hint;
  const preset = state.presets?.presets.find(p => p.id === state.presetId[state.kind]);
  $("#settings-summary").textContent = state.kind === "3d" ? `고품질 원본 · ${Number($("#texture").value) / 1024}K 텍스처`
    : `${preset?.pixelate ? preset.pixelate.size + "px 픽셀 아트" : (preset?.canvas?.width || preset?.width || 1024) + "px PNG"} · ${$("#remove-background").checked ? "투명 배경" : "배경 포함"}`;
}

function renderImagePick() {
  const preview = $("#image-preview");
  if (!state.imagePath && !state.uploadId && !state.imageSource) {
    preview.innerHTML = "";
    return;
  }
  preview.innerHTML = `${state.imagePreviewUrl ? `<img src="${state.imagePreviewUrl}" alt="">` : ""}
    <div class="path">${escapeHtml(state.imageName || state.imagePath)}</div>`;
}

function setImage(upload, name) {
  state.imagePath = ""; state.imageSource = null; state.uploadId = upload.id;
  state.imagePreviewUrl = upload.url; state.imageName = name; renderImagePick();
}
async function chooseImage(file) {
  if (!file) return;
  try { showFormError(""); setImage(await uploadBlob(file), file.name); }
  catch(error) { showFormError(error.message); }
}
async function chooseFromLibrary() {
  const {assets} = await api("/api/assets?kind=image&limit=200");
  const dialog = popup("3D로 만들 이미지 선택", `<div class="library-pick">${assets.map((a,i) => `<button type="button" data-choice="${i}"><img loading="lazy" src="${fileUrl(a.jobId,a.preview||a.file)}" alt=""><small>${escapeHtml(a.jobTitle)}</small></button>`).join("") || '<p class="hint">먼저 이미지를 만들거나 가져와 주세요.</p>'}</div>`, {wide:true});
  dialog.addEventListener("click", e => { const choice=e.target.closest("[data-choice]"); if(!choice)return; const a=assets[Number(choice.dataset.choice)];
    state.uploadId=null;state.imagePath="";state.imageSource={jobId:a.jobId,assetId:a.id};state.imageName=a.jobTitle;state.imagePreviewUrl=fileUrl(a.jobId,a.preview||a.file);dialog.close();renderImagePick();});
}

function showFormError(message) {
  const error = $("#form-error");
  error.textContent = message;
  error.hidden = !message;
}

async function submitComposer(event) {
  event.preventDefault();
  showFormError("");
  let request;
  try {
    request = buildJobRequest({
      kind: state.kind, source: state.source, preset: state.presetId[state.kind],
      subject: $("#subject").value, style: $("#style").value, count: $("#count").value, seed: $("#seed").value,
      imagePath: state.imagePath, imageName:state.imageName, uploadId: state.uploadId, imageSource: state.imageSource, workflow: workflow(),
      removeBackground: $("#remove-background").checked, pipelineType: $("#pipeline").value,
      textureSize: $("#texture").value, targetFaces: $("#faces").value,
      gameFaces: 0,
    });
  } catch (error) {
    showFormError(error.message);
    return;
  }
  const button = $("#submit");
  button.disabled = true;
  try {
    await api("/api/jobs", { method: "POST", body: request });
    toast("새 작업을 시작했습니다. 완료되면 오른쪽에 결과가 나타납니다.");
    await refreshJobs();
  } catch (error) {
    showFormError(error.message);
  } finally {
    button.disabled = false;
  }
}

// ---- 작업 목록 --------------------------------------------------------------

function timeLabel(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("ko-KR", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function stageHtml(stage) {
  const running = stage.state === "running";
  const percent = Math.round((stage.progress || 0) * 100);
  const measured = [
    stage.seconds ? formatDuration(stage.seconds) : "",
    stage.peakMemoryBytes ? `최대 ${formatBytes(stage.peakMemoryBytes)}` : "",
  ].filter(Boolean).join(" · ");
  const info = running ? `${percent}%` : measured || STAGE_STATE_LABELS[stage.state] || "";
  const showDetail = stage.detail && (running || stage.state === "skipped");
  return `<li class="stage stage-${escapeHtml(stage.state)}">
    <span class="dot"></span><span class="stage-label">${escapeHtml(stage.label)}</span>
    <span class="stage-info">${escapeHtml(info)}</span>
    ${running ? `<div class="bar"><i style="width:${percent}%"></i></div>` : ""}
    ${showDetail ? `<div class="stage-detail">${escapeHtml(stage.detail)}</div>` : ""}
  </li>`;
}

function thumbHtml(jobId, asset) {
  const src = asset.preview ? fileUrl(jobId, asset.preview) : "";
  const warn = asset.meta?.error ? "물체 없음" : asset.meta?.checks?.touchesEdge ? "가장자리 닿음" : "";
  const tag = asset.kind === "mesh" ? asset.meta?.label || "3D"
    : asset.kind === "shot" ? asset.meta?.shot || "샷"
    : asset.role === "concept" ? "컨셉" : "";
  return `<button class="thumb${asset.review === "rejected" ? " is-rejected" : ""}" type="button"
      data-action="open" data-job="${escapeHtml(jobId)}" data-asset="${escapeHtml(asset.id)}"
      title="${escapeHtml(REVIEW_LABELS[asset.review] || "")}">
    ${src ? `<img src="${src}" alt="" loading="lazy" class="${PIXEL_FILE.test(asset.file) ? "pixel" : ""}">` : ""}
    ${tag ? `<span class="tag">${tag}</span>` : ""}
    ${warn ? `<span class="tag warn">${warn}</span>` : ""}
    ${asset.review !== "pending" ? `<span class="review-dot ${escapeHtml(asset.review)}"></span>` : ""}
  </button>`;
}

function readablePhase(job) {
  if (job.state === "queued") return "앞선 작업이 끝나면 시작합니다";
  if (job.state === "cancelling") return "작업을 중지하고 있습니다";
  const name = job.stages.findLast(s=>s.state === "running")?.name || "";
  if (name === "generate") return "이미지를 만들고 있습니다";
  if (name === "mesh") return "3D 형태를 만들고 있습니다";
  if (name === "inspect") return "완성된 에셋을 확인하고 있습니다";
  if (name.startsWith("texture") || name === "surface" || name === "lod") return "표면과 재질을 다듬고 있습니다";
  if (name === "edit") return "편집본을 저장하고 있습니다";
  if (name === "cutout") return "배경을 정리하고 있습니다";
  return "에셋을 준비하고 있습니다";
}
function jobCardHtml(job) {
  const elapsed = elapsedSeconds(job.startedAt, job.finishedAt);
  const assets = job.assets.map(asset => thumbHtml(job.id,asset)).join("");
  const title=job.params?.subject || job.title;
  const label=job.recipe === "image" && job.params?.preset === "prop-3d" ? "3D 컨셉" : RECIPE_LABELS[job.recipe] || job.recipe;
  return `<header class="job-head"><div><div class="job-title">${escapeHtml(title)}</div><div class="job-meta">${escapeHtml(label)} · ${escapeHtml(timeLabel(job.createdAt))}</div></div><span class="state state-${escapeHtml(job.state)}">${escapeHtml(JOB_STATE_LABELS[job.state])}</span></header>
    ${isActive(job) ? `<div class="job-summary"><span class="pulse"></span>${readablePhase(job)}</div>` : ""}
    ${job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : ""}
    ${assets ? `<div class="job-assets">${assets}</div>` : ""}
    <footer class="job-foot"><button class="ghost" type="button" data-action="record" data-job="${job.id}">작업 기록 ↗</button>${isActive(job)?`<button class="ghost is-danger" type="button" data-action="cancel" data-job="${job.id}" ${job.state==='cancelling'?'disabled':''}>중지</button>`:`<span class="job-meta">${elapsed!==null?formatDuration(elapsed):''}</span>`}</footer>`;
}
async function openRecord(jobId) {
 const job=await api(`/api/jobs/${jobId}`);
 popup("작업 기록", `<div class="record-summary"><span>${escapeHtml(job.title)}</span><span>${JOB_STATE_LABELS[job.state]}</span></div><ol class="stages">${job.stages.map(stageHtml).join("")}</ol>${job.error?`<p class="notice">${escapeHtml(job.error)}</p>`:''}<details class="advanced"><summary>입력 조건</summary><pre class="record-log">${escapeHtml(JSON.stringify(job.params,null,2))}</pre></details><details class="advanced"><summary>실행 로그</summary><pre class="record-log">${escapeHtml((job.logTail||[]).join('\n'))}</pre></details>`, {wide:true});
}

function renderJobs() {
  const list = $("#job-list");
  // 프리비즈 작업은 프리비즈 탭이 컷 순서로 보여 준다. 여기서는 에셋 생성만 다룬다.
  const jobs = state.jobs.filter((job) => job.recipe !== "previz").slice(0, 10);
  if (!jobs.length) {
    list.innerHTML = `<div class="empty empty-studio"><span>◇</span><h3>첫 에셋을 만들어 보세요.</h3><p>만든 결과가 이곳에 모입니다.<br>에셋을 열면 색과 로고를 자유롭게 바꿀 수 있습니다.</p></div>`;
    state.signatures.clear();
  } else {
    list.querySelector(".empty")?.remove();
    const cards = new Map($$(".job", list).map((card) => [card.dataset.id, card]));
    let previous = null;
    for (const job of jobs) {
      // 바뀐 카드만 다시 그린다. 매초 전체를 갈아 끼우면 썸네일이 깜빡인다.
      const signature = JSON.stringify([job.state, job.stages, job.assets, job.error]);
      let card = cards.get(job.id);
      if (!card) {
        card = document.createElement("article");
        card.className = "job";
        card.dataset.id = job.id;
      }
      if (state.signatures.get(job.id) !== signature) {
        card.innerHTML = jobCardHtml(job);
        card.classList.toggle("is-active", isActive(job));
        state.signatures.set(job.id, signature);
      }
      const expected = previous ? previous.nextElementSibling : list.firstElementChild;
      if (card !== expected) list.insertBefore(card, expected);
      previous = card;
      cards.delete(job.id);
    }
    for (const [id, card] of cards) {
      card.remove();
      state.signatures.delete(id);
    }
  }
  for (const label of $$(".job-elapsed", list)) {
    if (!label.dataset.finished) label.textContent = formatDuration(elapsedSeconds(label.dataset.started, null));
  }
  const active = jobs.filter(isActive).length;
  $("#queue-summary").textContent = active ? `${active}개 진행·대기` : "";
  const pending = state.jobs.reduce((sum, job) => sum + job.assets.filter((asset) => asset.review === "pending").length, 0);
  $("#review-count").textContent = pending ? String(pending) : "";
}

async function refreshJobs() {
  try {
    state.jobs = (await api("/api/jobs?limit=40")).jobs;
    renderJobs();
  } catch {
    showOffline();
  }
}

async function refreshHealth() {
  const foot = $("#engine-state");
  try {
    const health = await api("/api/health");
    const running = state.jobs.find((job) => job.id === health.currentJob);
    foot.classList.remove("offline");
    foot.classList.toggle("busy", Boolean(health.currentJob));
    foot.innerHTML = health.currentJob
      ? `<strong><span class="pulse"></span>작업 중</strong>${escapeHtml(running?.title || health.currentJob)}`
      : `<strong><span class="pulse"></span>엔진 대기 중</strong>v${escapeHtml(health.version)}`;
  } catch {
    showOffline();
  }
}

function showOffline() {
  const foot = $("#engine-state");
  foot.classList.add("offline");
  foot.classList.remove("busy");
  foot.innerHTML = `<strong><span class="pulse"></span>엔진 연결 끊김</strong>다시 연결하는 중`;
}

let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await refreshJobs();
    await refreshHealth();
    // 프리비즈 컷은 작업 기록에서 바로 읽는다. 바뀐 게 없으면 다시 그리지 않는다.
    if (state.view === "previz") previz.onJobs();
    schedulePoll();
  }, state.jobs.some(isActive) ? 1000 : 4000);
}

// ---- 에셋 상세 --------------------------------------------------------------

function detailHtml(job, asset) {
  const meta = asset.meta || {};
  const viewerReady = Boolean(customElements.get("model-viewer"));
  let stage;
  if (asset.kind === "mesh") {
    stage = viewerReady
      ? `<model-viewer src="${fileUrl(job.id, asset.file)}" camera-controls auto-rotate shadow-intensity="0.7"
          ${meta.processingVersion >= 2 && asset.preview ? `poster="${fileUrl(job.id, asset.preview)}"` : ""}
          exposure="1.05" environment-image="neutral" interaction-prompt="none" alt="${escapeHtml(job.title)}"></model-viewer>`
      : `<div><img src="${fileUrl(job.id, asset.preview)}" alt=""><p class="notice">3D 미리보기를 쓰려면 npm install 이 필요합니다.</p></div>`;
  } else if (asset.kind === "shot") {
    const aux = ["depth", "line"].flatMap((name) => (meta.files?.[name] || []).slice(1, 2));
    stage = `<div class="shot-stage">
      ${asset.file.endsWith(".mp4")
        ? `<video src="${fileUrl(job.id, asset.file)}" controls loop autoplay muted playsinline></video>`
        : `<img src="${fileUrl(job.id, asset.preview || asset.file)}" alt="">`}
      ${aux.length ? `<div class="shot-aux">${aux.map((file) =>
        `<img src="${fileUrl(job.id, file)}" alt="" loading="lazy">`).join("")}</div>` : ""}
    </div>`;
  } else {
    stage = `<img src="${fileUrl(job.id, asset.preview || asset.file)}" alt="" class="${PIXEL_FILE.test(asset.file) ? "pixel" : ""}">`;
  }

  const rows = [];
  const add = (label, value, mono = false) => {
    if (value === undefined || value === null || value === "") return;
    rows.push(`<dt>${escapeHtml(label)}</dt><dd class="${mono ? "mono" : ""}">${escapeHtml(value)}</dd>`);
  };
  add("상태", REVIEW_LABELS[asset.review]);
  add("작업", RECIPE_LABELS[job.recipe] || job.recipe);
  if (asset.kind === "image") {
    add("크기", `${meta.width}×${meta.height}px`);
    add("시드", meta.seed, true);
    add("모델", meta.model);
    if (meta.checks?.coverage != null) add("물체 면적", `${Math.round(meta.checks.coverage * 100)}%`);
    if (meta.checks?.touchesEdge) add("자동 검사", "원본에서 물체가 가장자리에 닿음 (잘렸을 수 있음)");
    add("자동 검사", meta.error);
  } else if (asset.kind === "shot") {
    const point = (value) => value?.position?.map((number) => number.toFixed(2)).join(", ");
    add("컷", `${meta.label || ""} · ${meta.purpose || ""}`);
    add("길이", `${meta.seconds}초 · ${meta.frames}프레임 · ${meta.fps}fps`);
    add("움직임", meta.move);
    add("렌즈", meta.lens === meta.lensEnd ? `${meta.lens}mm` : `${meta.lens} → ${meta.lensEnd}mm`);
    add("카메라 시작", point(meta.start), true);
    add("카메라 끝", point(meta.end), true);
    add("바라보는 곳", meta.start?.target?.map((number) => number.toFixed(2)).join(", "), true);
    add("깊이 범위 (m)", meta.depthRange?.map((value) => value.toFixed(2)).join(" ~ "), true);
    add("렌더", `${meta.renderer} · ${meta.resolution?.join("×")}${meta.clay ? " · 점토" : ""}`);
    add("렌더 시간", formatDuration(meta.renderSeconds));
  } else {
    const stats = meta.stats || {};
    add("산출물", meta.label);
    if (stats.facesOut != null) add("면 수", stats.facesOut.toLocaleString());
    if (stats.sourceTriangles != null) add("생성 원본 면 수", stats.sourceTriangles.toLocaleString());
    if (Array.isArray(stats.dimensionsMeters)) add("크기 (m)", stats.dimensionsMeters.map((value) => value.toFixed(2)).join(" × "));
    if (stats.textures != null) add("텍스처", `${stats.textures}장 · ${meta.textureSize}px`);
    add("형상 해상도", meta.pipelineType);
    add("시드", meta.seed, true);
    add("GLB", formatBytes(stats.bytes));
    if (meta.optimizedBytes) add("최적화 GLB", formatBytes(meta.optimizedBytes));
    if (stats.topology?.warnings?.length) add("자동 검사", stats.topology.warnings.join(" "));
  }
  add("파일", asset.file, true);

  const reviewButton = (status, label, className) => `<button class="secondary ${className}${asset.review === status ? " is-on" : ""}"
      type="button" data-action="review" data-job="${escapeHtml(job.id)}" data-asset="${escapeHtml(asset.id)}"
      data-target="${status}">${label}</button>`;
  const actions = [
    reviewButton("approved", "승인", "approve"),
    reviewButton("rejected", "거절", "reject"),
    asset.kind === "image"
      ? `<button class="secondary" type="button" data-action="to3d" data-job="${escapeHtml(job.id)}" data-asset="${escapeHtml(asset.id)}">3D로 만들기</button>`
      : "",
    asset.kind === "mesh"
      ? `<button class="secondary" type="button" data-action="previz" data-job="${escapeHtml(job.id)}" data-asset="${escapeHtml(asset.id)}">프리비즈 장면에 넣기</button>`
      : "",
    asset.kind === "mesh" && (meta.sourceStateFile || job.params?.audit)
      ? `<button class="secondary" type="button" data-action="refine" data-job="${escapeHtml(job.id)}" data-asset="${escapeHtml(asset.id)}">원본으로 품질 다시 만들기</button>`
      : "",
    meta.inspectionFile
      ? `<a class="secondary" href="${fileUrl(job.id, meta.inspectionFile)}" target="_blank" rel="noopener">여섯 방향 검수 보기</a>`
      : "",
    bridge
      ? `<button class="ghost" type="button" data-action="reveal" data-job="${escapeHtml(job.id)}" data-file="${escapeHtml(asset.file)}">Finder에서 보기</button>`
      : "",
    bridge && meta.optimizedFile
      ? `<button class="ghost" type="button" data-action="reveal" data-job="${escapeHtml(job.id)}" data-file="${escapeHtml(meta.optimizedFile)}">최적화 GLB 보기</button>`
      : "",
  ].join("");

  return `<div class="detail">
    <button class="close" type="button" data-action="close" aria-label="닫기">✕</button>
    <div class="detail-stage">${stage}</div>
    <aside class="detail-side">
      <h3>${escapeHtml(job.title)}${meta.label ? ` · ${escapeHtml(meta.label)}` : ""}</h3>
      <div class="actions">${actions}</div>
      <p class="hint" id="detail-message"></p>
      <dl class="meta">${rows.join("")}</dl>
      ${job.params?.prompt ? `<div class="prompt">${escapeHtml(job.params.prompt)}</div>` : ""}
    </aside>
  </div>`;
}

let assetOpenRequest=0;
async function openAsset(jobId, assetId) {
  const request=++assetOpenRequest;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if(request!==assetOpenRequest)return;
    const asset = job.assets.find((item) => item.id === assetId);
    if (!asset) return;
    if (asset.kind === "image" || asset.kind === "mesh") { $("#asset-dialog").close(); return editor.open(job, asset); }
    $("#asset-detail").innerHTML = detailHtml(job, asset);
    const dialog = $("#asset-dialog");
    if (!dialog.open) dialog.showModal();
  } catch (error) {
    showFormError(error.message);
  }
}

function setDetailMessage(message) {
  const line = $("#detail-message");
  if (line) line.textContent = message;
}

async function setReview(button) {
  const { job, asset, target } = button.dataset;
  const status = button.classList.contains("is-on") ? "pending" : target;
  try {
    await api(`/api/jobs/${encodeURIComponent(job)}/assets/${encodeURIComponent(asset)}/review`, {
      method: "POST", body: { status },
    });
    // 3D 뷰어를 다시 불러오지 않도록 단추 상태만 바꾼다.
    for (const other of $$('[data-action="review"]', $("#asset-detail"))) {
      other.classList.toggle("is-on", other.dataset.target === status);
    }
    setDetailMessage(status === "pending" ? "검토 대기로 되돌렸습니다." : `${REVIEW_LABELS[status]}으로 표시했습니다.`);
    await refreshJobs();
    if (state.view === "library") refreshLibrary();
    if (state.view === "previz") previz.onJobs();
  } catch (error) {
    setDetailMessage(error.message);
  }
}

async function makeMesh(jobId, assetId) {
  try {
    const job=await api(`/api/jobs/${jobId}`),asset=job.assets.find(a=>a.id===assetId);
    const dialog=popup("이 이미지로 3D 만들기", `<img class="conversion-preview" src="${fileUrl(jobId,asset.preview||asset.file)}" alt="선택한 이미지"><div class="quality-note"><strong>고품질 3D 원본</strong><p>선택한 이미지로 형태와 재질을 만듭니다. 완성 후 색상과 로고를 편집할 수 있습니다.</p></div><button class="primary full" data-convert>3D 생성 시작</button>`);
    dialog.querySelector('[data-convert]').onclick=async e=>{e.target.disabled=true;try {
      await api('/api/jobs',{method:'POST',body:{recipe:'image-to-3d',params:{source:{jobId,assetId},pipelineType:$('#pipeline').value,textureSize:Number($('#texture').value),targetFaces:Number($('#faces').value),gameFaces:0}}});
      dialog.close();$('#asset-dialog').close();editor.close();showView('create');await refreshJobs();toast('3D 생성을 시작했습니다.');
    } catch(error){toast(error.message);e.target.disabled=false;}};
  }catch(error){toast(error.message);}
}

// ---- 보관함 -----------------------------------------------------------------

async function refreshLibrary() {
  const grid = $("#library-grid");
  const filter = state.libraryFilter;
  for (const button of $$("#library-filter button")) button.classList.toggle("is-active", button.dataset.filter === filter);
  try {
    const response = await api(`/api/assets${filter === "all" ? "" : `?review=${filter}`}`);
    const query = $("#library-search").value.trim().toLowerCase(), kind = $("#library-kind").value;
    const assets = response.assets.filter(a=>(kind === "all" || a.kind === kind) && (!query || a.jobTitle.toLowerCase().includes(query)));
    grid.innerHTML = assets.length
      ? assets.map((asset) => `<div class="asset-card">${thumbHtml(asset.jobId, asset)}
          <div class="caption">${escapeHtml(asset.jobTitle)}${asset.meta?.label ? ` · ${escapeHtml(asset.meta.label)}` : ""}</div></div>`).join("")
      : `<div class="empty">${filter === "pending" ? "검토할 에셋이 없습니다." : "해당하는 에셋이 없습니다."}</div>`;
  } catch (error) {
    grid.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

// ---- 환경 -------------------------------------------------------------------

function doctorHtml(report, info) {
  const capabilities = Object.entries(CAPABILITY_LABELS).map(([key, label]) => `
    <div class="capability${report.capabilities[key] ? " ready" : ""}">
      <span>${report.capabilities[key] ? "준비됨" : "준비 필요"}</span><strong>${label}</strong>
    </div>`).join("");
  const checks = Object.values(report.checks).map((check) => {
    const kind = check.ok ? "ok" : check.required ? "missing" : "optional";
    const note = check.ok ? "" : [check.detail, check.hint].filter(Boolean).join(" → ");
    return `<li class="check ${kind}"><span class="mark">${check.ok ? "✓" : check.required ? "✗" : "!"}</span>
      <div>${escapeHtml(check.label)}${note ? `<small>${escapeHtml(note)}</small>` : ""}</div></li>`;
  }).join("");
  const engine = info?.url ? ` · 엔진 ${info.url}${info.owned ? "" : " (앱 밖에서 실행 중)"}` : "";
  return `<p class="hint">${escapeHtml(`${report.machine} · 메모리 ${Math.round(report.memoryBytes / 2 ** 30)}GB${engine}`)}</p>
    <div class="capabilities" style="margin-top: 12px">${capabilities}</div>
    <ul class="checks">${checks}</ul>`;
}

function benchHtml(rows) {
  if (!rows.length) return `<div class="empty" style="margin-top: 12px">완료된 작업이 생기면 단계별 기록이 쌓입니다.</div>`;
  return `<div class="table-wrap"><table>
    <thead><tr><th>단계</th><th>조건</th><th>횟수</th><th>중앙값</th><th>최대</th><th>최대 메모리</th></tr></thead>
    <tbody>${rows.map((row) => `<tr>
      <td>${escapeHtml(row.label)}</td><td>${escapeHtml(row.variant || "–")}</td><td class="num">${row.runs}</td>
      <td class="num">${formatDuration(row.medianSeconds)}</td><td class="num">${formatDuration(row.maxSeconds)}</td>
      <td class="num">${formatBytes(row.maxPeakMemoryBytes)}</td></tr>`).join("")}</tbody>
  </table></div>`;
}

async function refreshSystem() {
  const container = $("#doctor");
  container.innerHTML = `<div class="empty">검사하는 중…</div>`;
  try {
    const [report, bench, info] = await Promise.all([
      api("/api/doctor?deep=true"), api("/api/bench"), bridge?.engineInfo() ?? null,
    ]);
    state.doctor = report;
    updateSubmitLabel();
    container.innerHTML = doctorHtml(report, info);
    $("#bench").innerHTML = benchHtml(bench.rows);
  } catch (error) {
    container.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

// ---- 연결 -------------------------------------------------------------------

function wireEvents() {
  for (const button of $$(".nav-item")) button.addEventListener("click", () => showView(button.dataset.view));
  $("#kind-switch").addEventListener("click", (event) => {
    const button = event.target.closest("[data-kind]");
    if (!button) return;
    state.kind = button.dataset.kind;
    renderComposer();
  });
  $("#source-switch").addEventListener("click", (event) => {
    const button = event.target.closest("[data-source]");
    if (!button) return;
    state.source = button.dataset.source;
    renderComposer();
  });
  $("#preset-chips").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-preset]");
    if (!chip) return;
    state.presetId[state.kind] = chip.dataset.preset;
    renderComposer();
  });
  $("#count").addEventListener("change", updateSubmitLabel);
  for (const input of $$('input[name="workflow"]')) input.addEventListener('change', updateSubmitLabel);
  $("#open-settings").onclick=()=>$("#settings-dialog").showModal();
  for (const id of ['close-settings','apply-settings']) $('#'+id).onclick=()=>{$('#settings-dialog').close();updateSubmitLabel();};
  $('#remove-background').onchange=updateSubmitLabel;
  $('#prompt-examples').onclick=e=>{const b=e.target.closest('[data-example]');if(b){$('#subject').value=b.dataset.example;$('#subject').focus();}};
  $('#pick-library').onclick=()=>chooseFromLibrary().catch(e=>toast(e.message));
  $('#import-image').onclick=async()=>{try{const file=await pickFile();if(!file)return;const upload=await uploadBlob(file);const created=await api('/api/jobs',{method:'POST',body:{recipe:'import-image',params:{uploadId:upload.id,name:file.name.replace(/\.[^.]+$/,'')}}});await refreshJobs();const job=await waitJob(api,created.id);await refreshJobs();openAsset(job.id,job.assets[0].id);}catch(error){toast(error.message);}};
  $("#composer").addEventListener("submit", submitComposer);
  $("#subject").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) $("#composer").requestSubmit();
  });
  $("#library-filter").addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    state.libraryFilter = button.dataset.filter;
    refreshLibrary();
  });
  $("#doctor-refresh").addEventListener("click", refreshSystem);
  $("#library-search").addEventListener("input", refreshLibrary);
  $("#library-kind").addEventListener("change", refreshLibrary);
  // 닫을 때 비워야 3D 뷰어가 뒤에서 계속 그리지 않는다.
  $("#asset-dialog").addEventListener("close", () => { $("#asset-detail").innerHTML = ""; });

  $("#pick-image").addEventListener("click", async () => chooseImage(await pickFile()));
  const dropzone = $("#image-field");
  dropzone.addEventListener("dragover", (event) => { event.preventDefault(); dropzone.classList.add("is-over"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-over"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-over");
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    chooseImage(file);
  });
  // 창 아무 데나 파일을 놓으면 Electron이 그 파일로 이동하려 한다.
  for (const type of ["dragover", "drop"]) document.addEventListener(type, (event) => event.preventDefault());

  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const { action, job, asset } = target.dataset;
    if (action === "open") openAsset(job, asset);
    else if (action === "record") openRecord(job).catch(e=>toast(e.message));
    else if (action === "library") showView("library");
    else if (action === "review") setReview(target);
    else if (action === "to3d") makeMesh(job, asset);
    else if (action === "refine") {
      target.disabled = true;
      api("/api/jobs", { method: "POST", body: { recipe: "refine-mesh", params: { source: { jobId: job, assetId: asset } } } })
        .then(() => { $("#asset-dialog").close(); showView("create"); return refreshJobs(); })
        .catch((error) => setDetailMessage(error.message))
        .finally(() => { target.disabled = false; });
    }
    else if (action === "previz") addToPrevizScene(job, asset);
    else if (action === "reveal") bridge?.reveal(job, target.dataset.file);
    else if (action === "close") $("#asset-dialog").close();
    else if (action === "cancel") {
      target.disabled = true;
      api(`/api/jobs/${encodeURIComponent(job)}/cancel`, { method: "POST" })
        .catch((error) => showFormError(error.message))
        .finally(refreshJobs);
    }
  });
}

async function addToPrevizScene(jobId, assetId) {
  $("#asset-dialog").close();
  showView("previz");
  await previz.show();
  previz.addAsset(jobId, assetId);
}

async function init() {
  installTooltips($("#tooltip"));
  installResizer($("#sidebar-resizer"), {
    storageKey: "assets-studio.sidebar-width", min: 180, max: 360, fallback: 220,
    apply: (width) => document.documentElement.style.setProperty("--sidebar-w", `${width}px`),
  });
  let sidebarClosed = false;
  try { sidebarClosed = localStorage.getItem(SIDEBAR_CLOSED_KEY) === "1"; } catch { /* 처음 상태로 연다 */ }
  setSidebar(sidebarClosed, { save: false });
  $("#sidebar-toggle").addEventListener("click", () => setSidebar(!$(".shell").classList.contains("is-sidebar-closed")));
  wireEvents();
  try {
    state.presets = await api("/api/presets");
    const defaults = state.presets.mesh.defaults;
    $("#pipeline").value = defaults.pipelineType;
    $("#texture").value = String(defaults.textureSize);
    $("#faces").value = String(defaults.targetFaces);

  } catch (error) {
    showFormError(error.message);
  }
  renderComposer();
  await refreshJobs();
  await refreshHealth();
  api("/api/doctor?deep=false").then((report) => { state.doctor = report; updateSubmitLabel(); }).catch(() => {});
  const initialView = new URLSearchParams(location.search).get("view");
  if (initialView && $(`#view-${initialView}`)) showView(initialView);
  schedulePoll();
}

init();
