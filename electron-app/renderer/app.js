import {
  JOB_STATE_LABELS, RECIPE_LABELS, REVIEW_LABELS, STAGE_STATE_LABELS,
  buildJobRequest, escapeHtml, fileUrl, formatBytes, formatDuration, isActive, jobTimes, jobsAhead, newlyFinished, estimateLabel,
} from "../shared/format.mjs";
import { createPreviz } from "./previz.js";
import { installResizer } from "./resize.js";
import { installTooltips } from "./tooltip.js";
import { popup, toast, pickFile, uploadBlob, waitJob } from "./ui.js";
import { createEditor } from "./editor.js";
import { createLibraryBatch } from "./library-batch.js";

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
  imageModels: {},
  imagePath: "",
  imagePreviewUrl: "",
  jobs: [],
  estimates: {}, estimatesAt: 0,
  signatures: new Map(),
  doctor: null,
  libraryFilter: "all",
  libraryTag: "",
  view: "create",
  // 직전 폴링의 작업 상태. 끝난 작업을 알리는 데 쓴다.
  lastStates: null,
  // 화면이 직접 결과를 보여 주는 작업(편집 저장·가져오기). 완료 알림을 따로 띄우지 않는다.
  quietJobs: new Set(),
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

const editor = createEditor({ api, refreshJobs, openAsset, makeMesh, addToPrevizScene, bridge, quietJob: (id) => state.quietJobs.add(id) });
const libraryBatch = createLibraryBatch({ api, bridge, refresh: async () => { await refreshJobs(); await refreshLibrary(); } });

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

// 작성 중인 생성 요청은 앱을 다시 열어도 이어서 쓴다. 기억하지 못해도 만들기는 된다.
const COMPOSER_KEY = "assets-studio.composer-draft";
let composerTimer = null;
function saveComposerDraft() {
  clearTimeout(composerTimer);
  composerTimer = setTimeout(() => {
    const draft = {
      kind: state.kind, source: state.source, presetId: state.presetId,
      imageModels: state.imageModels,
      subject: $("#subject").value, style: $("#style").value, count: $("#count").value, seed: $("#seed").value,
      workflow: workflow(), removeBackground: $("#remove-background").checked,
      pipeline: $("#pipeline").value, texture: $("#texture").value, faces: $("#faces").value,
      image: { uploadId: state.uploadId, imageSource: state.imageSource, imageName: state.imageName, imagePreviewUrl: state.imagePreviewUrl },
    };
    try { localStorage.setItem(COMPOSER_KEY, JSON.stringify(draft)); } catch { /* 저장 공간이 없으면 이번 입력만 유지한다 */ }
  }, 250);
}
function restoreComposerDraft() {
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(COMPOSER_KEY) || "null"); } catch { return; }
  if (!draft || typeof draft !== "object") return;
  if (["2d", "3d"].includes(draft.kind)) state.kind = draft.kind;
  if (["text", "image"].includes(draft.source)) state.source = draft.source;
  if (draft.presetId && typeof draft.presetId === "object") state.presetId = { ...state.presetId, ...draft.presetId };
  if (draft.imageModels && typeof draft.imageModels === "object") state.imageModels = draft.imageModels;
  for (const [id, value] of [["subject", draft.subject], ["style", draft.style], ["seed", draft.seed]]) if (typeof value === "string") $(`#${id}`).value = value;
  for (const [id, value] of [["count", draft.count], ["pipeline", draft.pipeline], ["texture", draft.texture], ["faces", draft.faces]]) {
    if (value !== undefined && [...$(`#${id}`).options].some((option) => option.value === String(value))) $(`#${id}`).value = String(value);
  }
  const radio = $(`input[name="workflow"][value="${draft.workflow}"]`);
  if (radio) radio.checked = true;
  if (typeof draft.removeBackground === "boolean") {
    $("#remove-background").checked = draft.removeBackground;
    state.lastPreset = state.presetId[state.kind];
  }
  const image = draft.image || {};
  if (image.uploadId || image.imageSource) Object.assign(state, { uploadId: image.uploadId || null, imageSource: image.imageSource || null, imageName: image.imageName || "", imagePreviewUrl: image.imagePreviewUrl || "" });
}

function presetsFor(kind) {
  return (state.presets?.presets || []).filter((preset) => preset.kind === kind);
}

function imageCategory() {
  return presetsFor("2d").find(p => p.id === state.presetId["2d"])?.category || "game";
}

function renderImageModel() {
  const models = [state.presets?.imageModel, ...(state.presets?.imageModels || [])].filter(Boolean);
  const category = imageCategory();
  const preset = presetsFor("2d").find(p => p.id === state.presetId["2d"]);
  const defaultId = preset?.imageModel || state.presets?.imageModel?.id;
  let selected = state.imageModels[category] || "";
  if (selected && !models.some(m => m.id === selected)) selected = state.imageModels[category] = "";
  const model = models.find(m => m.id === (selected || defaultId));
  $("#image-model").innerHTML = `<option value="">기본 · ${escapeHtml(models.find(m => m.id === defaultId)?.label || "")}</option>`
    + models.map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join("");
  $("#image-model").value = selected;
  $("#image-model-hint").textContent = model?.hint || "";
}

function renderComposer() {
  const { kind, source } = state;
  const fromImage = kind === "3d" && source === "image";
  for (const button of $$("#kind-switch button")) { button.classList.toggle("is-active", button.dataset.kind === kind); button.setAttribute("aria-selected", String(button.dataset.kind === kind)); }
  for (const button of $$("#source-switch button")) button.classList.toggle("is-active", button.dataset.source === source);
  $("#source-switch").hidden = kind !== "3d";
  $("#category-field").hidden = kind !== "2d";
  $("#image-model-field").hidden = kind !== "2d";
  $("#preset-field").hidden = fromImage;
  $("#subject-field").hidden = fromImage;
  $("#style-field").hidden = fromImage;
  $("#count-field").hidden = fromImage || (kind === "3d" && workflow() === "direct");
  $("#workflow-field").hidden = kind !== "3d" || fromImage;
  $("#background-field").hidden = kind === "3d";
  $("#prompt-examples").hidden = fromImage;
  $("#image-field").hidden = !fromImage;
  for (const option of $$(".mesh-option")) option.hidden = kind !== "3d";

  const category = imageCategory();
  $("#category-chips").innerHTML = (state.presets?.imageCategories || []).map(c => `<button type="button" class="chip${c.id === category ? " is-active" : ""}" data-category="${escapeHtml(c.id)}" aria-pressed="${c.id === category}">${escapeHtml(c.label)}</button>`).join("");
  const presets = presetsFor(kind).filter(p => kind !== "2d" || (p.category || "game") === category);
  if (!presets.some((preset) => preset.id === state.presetId[kind]) && presets[0]) state.presetId[kind] = presets[0].id;
  $("#preset-chips").innerHTML = presets.map((preset) => `
    <button type="button" class="chip${preset.id === state.presetId[kind] ? " is-active" : ""}" data-preset="${escapeHtml(preset.id)}">
      ${escapeHtml(preset.label)}
    </button>`).join("");
  $("#preset-hint").textContent = presets.find((preset) => preset.id === state.presetId[kind])?.hint || "";
  const preset = presets.find(p => p.id === state.presetId[kind]);
  const examples = preset?.example ? [["예시 넣기", preset.example]] : [["보물상자", "wooden treasure chest with dark iron bands"], ["고양이 주전자", "ceramic teapot shaped like a sleepy cat"], ["이끼 낀 석등", "old stone lantern covered in moss"]];
  $("#prompt-examples").innerHTML = examples.map(([label, text]) => `<button type="button" data-example="${escapeHtml(text)}">${escapeHtml(label)}</button>`).join("");
  $("#subject").placeholder = preset?.example ? `예: ${preset.example}` : "예: 둥근 뚜껑과 검은 철띠가 있는 나무 보물상자";
  if (state.lastPreset !== preset?.id) { $("#remove-background").checked = !!preset?.removeBackground; state.lastPreset = preset?.id; }
  $("#preset-field").hidden = fromImage || kind === "3d";
  renderImageModel();
  renderImagePick();
  updateSubmitLabel();
  saveComposerDraft();
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
  const cutout = $("#remove-background").checked;
  const size = cutout && preset?.canvas ? preset.canvas : preset;
  $("#settings-summary").textContent = state.kind === "3d" ? `고품질 원본 · ${Number($("#texture").value) / 1024}K 텍스처`
    : `${cutout && preset?.pixelate ? preset.pixelate.size + "px 픽셀 아트" : `${size?.width || 1024}×${size?.height || 1024} PNG`} · ${cutout ? "투명 배경" : "배경 포함"}`;
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
  state.imagePreviewUrl = upload.url; state.imageName = name; renderImagePick(); saveComposerDraft();
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
    state.uploadId=null;state.imagePath="";state.imageSource={jobId:a.jobId,assetId:a.id};state.imageName=a.jobTitle;state.imagePreviewUrl=fileUrl(a.jobId,a.preview||a.file);dialog.close();renderImagePick();saveComposerDraft();});
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
      imageModel: state.imageModels[imageCategory()] || "",
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
  // 승인·거절은 프리비즈 컷에만 쓴다. 이미지·3D는 즐겨찾기로 고른다.
  const shot = asset.kind === "shot";
  return `<button class="thumb${shot && asset.review === "rejected" ? " is-rejected" : ""}" type="button"
      data-action="open" data-job="${escapeHtml(jobId)}" data-asset="${escapeHtml(asset.id)}"
      title="${escapeHtml(shot ? REVIEW_LABELS[asset.review] || "" : asset.note || "")}">
    ${src ? `<img src="${src}" alt="" loading="lazy" class="${PIXEL_FILE.test(asset.file) ? "pixel" : ""}">` : ""}
    ${tag ? `<span class="tag">${tag}</span>` : ""}
    ${warn ? `<span class="tag warn">${warn}</span>` : ""}
    ${shot && asset.review !== "pending" ? `<span class="review-dot ${escapeHtml(asset.review)}"></span>` : ""}
    ${!shot && asset.favorite ? '<span class="favorite-mark" aria-label="즐겨찾기">★</span>' : ""}
  </button>`;
}

function readablePhase(job) {
  if (job.state === "queued") {
    const ahead = jobsAhead(state.jobs, job.id);
    return ahead ? `대기 중 · 앞에 ${ahead}개` : "곧 시작합니다";
  }
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
// 대기와 실제 처리 시간을 나눠 적는다. 진행 중이면 renderJobs가 매 폴링마다 숫자만 고친다.
function timesText(job) {
  const { waited, worked } = jobTimes(job);
  const estimate = estimateLabel(state.estimates[job.id], job.state);
  if (job.state === "queued") return [`${formatDuration(waited)}째 대기`, estimate].filter(Boolean).join(" · ");
  const parts = [];
  if (worked !== null) parts.push(`처리 ${formatDuration(worked)}`);
  if (waited >= 5) parts.push(`대기 ${formatDuration(waited)}`);
  if (isActive(job) && estimate) parts.push(estimate);
  return parts.join(" · ");
}
function jobCardHtml(job) {
  const assets = job.assets.map(asset => thumbHtml(job.id,asset)).join("");
  const title=job.params?.subject || job.title;
  const label=job.recipe === "image" && job.params?.preset === "prop-3d" ? "3D 컨셉" : RECIPE_LABELS[job.recipe] || job.recipe;
  const images = job.assets.filter((asset) => asset.kind === "image").length;
  const compare = !isActive(job) && images >= 2 ? `<button class="ghost" type="button" data-action="compare" data-job="${escapeHtml(job.id)}">나란히 비교</button>` : "";
  return `<header class="job-head"><div><div class="job-title">${escapeHtml(title)}</div><div class="job-meta">${escapeHtml(label)} · ${escapeHtml(timeLabel(job.createdAt))}</div></div><span class="state state-${escapeHtml(job.state)}">${escapeHtml(JOB_STATE_LABELS[job.state])}</span></header>
    ${isActive(job) ? `<div class="job-summary"><span class="pulse"></span>${readablePhase(job)}</div>` : ""}
    ${job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : ""}
    ${assets ? `<div class="job-assets">${assets}</div>` : ""}
    <footer class="job-foot"><div class="control-row"><button class="ghost" type="button" data-action="record" data-job="${job.id}">작업 기록 ↗</button>${compare}</div><span class="job-meta job-times" data-job="${escapeHtml(job.id)}">${escapeHtml(timesText(job))}</span>${isActive(job)?`<button class="ghost is-danger" type="button" data-action="cancel" data-job="${job.id}" ${job.state==='cancelling'?'disabled':''}>중지</button>`:""}</footer>`;
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
      const signature = JSON.stringify([job.state, job.stages, job.assets, job.error, jobsAhead(state.jobs, job.id)]);
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
  for (const label of $$(".job-times", list)) {
    const job = jobs.find((item) => item.id === label.dataset.job);
    if (job && isActive(job)) label.textContent = timesText(job);
  }
  const running = jobs.filter((job) => job.state === "running" || job.state === "cancelling").length;
  const queued = jobs.filter((job) => job.state === "queued").length;
  $("#queue-summary").textContent = running || queued ? [running ? `${running}개 진행` : "", queued ? `${queued}개 대기` : ""].filter(Boolean).join(" · ") : "";
  $("#active-count").textContent = running + queued ? String(running + queued) : "";
}

// 다른 일을 하는 동안 끝난 작업을 알린다. 창을 보고 있으면 화면 알림, 아니면 macOS 알림.
function announceFinished(jobs) {
  for (const job of newlyFinished(state.lastStates, jobs)) {
    if (job.recipe === "previz" && state.view === "previz") continue;
    const quiet = state.quietJobs.delete(job.id);
    const title = job.params?.subject || job.title;
    const { worked } = jobTimes(job);
    const headline = job.state === "done" ? `완료: ${title}` : job.state === "failed" ? `실패: ${title}` : `중지됨: ${title}`;
    const body = job.state === "done"
      ? [worked !== null ? `처리 ${formatDuration(worked)}` : "", job.assets.length ? `결과 ${job.assets.length}개` : ""].filter(Boolean).join(" · ")
      : (job.error || "").split("\n")[0];
    const focused = document.hasFocus();
    if (!focused && "Notification" in window && Notification.permission !== "denied") {
      try {
        const notice = new Notification(headline, { body, silent: job.state !== "done" });
        notice.onclick = () => {
          bridge?.focus?.();
          window.focus();
          if (job.assets[0]) openAsset(job.id, job.assets[0].id);
          else openRecord(job.id).catch(() => {});
        };
      } catch { /* 알림을 못 띄워도 목록에는 결과가 보인다 */ }
    }
    if (focused && !quiet) toast(`${headline}${body ? ` · ${body}` : ""}`);
  }
  state.lastStates = new Map(jobs.map((job) => [job.id, job.state]));
}

async function refreshJobs() {
  try {
    state.jobs = (await api("/api/jobs?limit=40")).jobs;
    if (state.jobs.some(isActive) && Date.now() - state.estimatesAt > 15000) {
      state.estimatesAt = Date.now();
      state.estimates = (await api("/api/estimates").catch(() => ({jobs: {}}))).jobs;
    }
    announceFinished(state.jobs);
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

let libraryRequest = 0;
async function refreshLibrary() {
  const grid = $("#library-grid");
  const request = ++libraryRequest;
  for (const button of $$("#library-filter button")) button.classList.toggle("is-active", button.dataset.filter === state.libraryFilter);
  try {
    const { assets: all } = await api("/api/assets?limit=2000");
    if (request !== libraryRequest) return;
    // 컬렉션·태그 목록은 전체 에셋에서 만든다. 걸러도 고를 수 있는 값이 사라지지 않게.
    const collections = [...new Set(all.map((asset) => asset.collection).filter(Boolean))].sort((a, b) => a.localeCompare(b, "ko"));
    const select = $("#library-collection"), chosen = select.value;
    select.innerHTML = `<option value="">모든 컬렉션</option>${collections.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")}<option value="__none__">컬렉션 없음</option>`;
    select.value = [...select.options].some((option) => option.value === chosen) ? chosen : "";
    const counts = new Map();
    for (const asset of all) for (const tag of asset.tags || []) counts.set(tag, (counts.get(tag) || 0) + 1);
    const tags = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ko")).slice(0, 24);
    if (state.libraryTag && !counts.has(state.libraryTag)) state.libraryTag = "";
    $("#library-tags").innerHTML = tags.map(([tag, count]) => `<button type="button" class="chip${tag === state.libraryTag ? " is-active" : ""}" data-library-tag="${escapeHtml(tag)}">#${escapeHtml(tag)} <small>${count}</small></button>`).join("");
    const query = $("#library-search").value.trim().toLowerCase(), kind = $("#library-kind").value, collection = select.value;
    const assets = all.filter((asset) => (kind === "all" || asset.kind === kind)
      && (state.libraryFilter !== "favorite" || asset.favorite)
      && (!collection || (collection === "__none__" ? !asset.collection : asset.collection === collection))
      && (!state.libraryTag || (asset.tags || []).includes(state.libraryTag))
      && (!query || [asset.jobTitle, asset.collection, asset.note, ...(asset.tags || [])].some((text) => String(text || "").toLowerCase().includes(query))));
    grid.innerHTML = assets.length
      ? assets.map((asset) => `<div class="asset-card">${thumbHtml(asset.jobId, asset)}
          ${asset.kind !== "shot" ? `<button type="button" class="card-favorite" data-action="favorite" data-job="${escapeHtml(asset.jobId)}" data-asset="${escapeHtml(asset.id)}" aria-pressed="${Boolean(asset.favorite)}" aria-label="즐겨찾기">★</button>` : ""}
          <div class="caption">${escapeHtml(asset.jobTitle)}${asset.meta?.label && !asset.jobTitle.endsWith(asset.meta.label) ? ` · ${escapeHtml(asset.meta.label)}` : ""}</div>
          ${asset.collection || asset.tags?.length ? `<div class="card-tags">${asset.collection ? `<span class="card-collection">${escapeHtml(asset.collection)}</span>` : ""}${(asset.tags || []).slice(0, 3).map((tag) => `<span>#${escapeHtml(tag)}</span>`).join("")}</div>` : ""}</div>`).join("")
      : `<div class="empty">${all.length ? "조건에 맞는 에셋이 없습니다." : "아직 만든 에셋이 없습니다."}</div>`;
    libraryBatch.render(assets);
  } catch (error) {
    grid.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
    libraryBatch.render([]);
  }
}

async function toggleFavorite(button) {
  const next = button.getAttribute("aria-pressed") !== "true";
  button.setAttribute("aria-pressed", String(next));
  try {
    await api(`/api/jobs/${encodeURIComponent(button.dataset.job)}/assets/${encodeURIComponent(button.dataset.asset)}/library`, { method: "POST", body: { favorite: next } });
    await refreshJobs();
    if (state.view === "library") refreshLibrary();
  } catch (error) {
    button.setAttribute("aria-pressed", String(!next));
    toast(error.message);
  }
}

// ---- 후보 비교 ---------------------------------------------------------------

// 컨셉·2D 후보를 크게 나란히 보고 하나를 고른다. 고른 후보는 즐겨찾기와 메모(고른 이유)로 남는다.
async function openCompare(jobId) {
  const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  const candidates = job.assets.filter((asset) => asset.kind === "image");
  let picked = Math.max(0, candidates.findIndex((asset) => asset.favorite));
  let focus = null;
  const concept = job.params?.preset === "prop-3d" || job.recipe === "text-to-3d";
  const dialog = popup("후보 비교", '<div class="compare-body"></div>', { className: "compare-dialog" });
  const body = dialog.querySelector(".compare-body");
  const warn = (asset) => asset.meta?.error ? "물체 없음" : asset.meta?.checks?.touchesEdge ? "가장자리 닿음" : "";
  function render() {
    const asset = candidates[picked];
    const card = (item, index) => `<figure class="compare-card${index === picked ? " is-picked" : ""}">
        <button type="button" class="compare-image" data-pick="${index}" aria-pressed="${index === picked}"><img src="${fileUrl(job.id, item.preview || item.file)}" alt="후보 ${index + 1}" class="${PIXEL_FILE.test(item.file) ? "pixel" : ""}"></button>
        <figcaption><span>후보 ${index + 1}${item.meta?.seed !== undefined ? ` · 시드 ${escapeHtml(item.meta.seed)}` : ""}${warn(item) ? ` · <em>${warn(item)}</em>` : ""}${item.favorite ? " · ★" : ""}</span><button type="button" class="ghost" data-zoom="${index}">크게 ⤢</button></figcaption></figure>`;
    body.innerHTML = `${focus === null
      ? `<div class="compare-grid count-${Math.min(candidates.length, 4)}">${candidates.map(card).join("")}</div>`
      : `<div class="compare-focus"><button type="button" class="compare-nav" data-step="-1" aria-label="이전 후보">‹</button>${card(candidates[focus], focus)}<button type="button" class="compare-nav" data-step="1" aria-label="다음 후보">›</button></div>`}
      <div class="compare-foot">
        <label class="field"><span class="label">고른 이유 <em>선택 · 후보 ${picked + 1}에 메모로 남습니다</em></span><input data-note maxlength="500" value="${escapeHtml(asset.note || "")}" placeholder="예: 뚜껑 모양이 가장 또렷하고 철띠가 대칭"></label>
        <div class="control-row">${focus === null ? "" : '<button class="ghost" type="button" data-grid>모두 보기</button>'}
          <button class="secondary" type="button" data-choose="edit">고르고 편집</button>
          <button class="${concept ? "secondary" : "primary"}" type="button" data-choose="keep">고르기</button>
          ${concept ? '<button class="primary" type="button" data-choose="mesh">이 후보로 3D 만들기</button>' : ""}</div>
      </div>`;
  }
  async function choose(action) {
    const asset = candidates[picked], note = body.querySelector("[data-note]").value;
    await api(`/api/jobs/${encodeURIComponent(job.id)}/assets/${encodeURIComponent(asset.id)}/library`, { method: "POST", body: { favorite: true, note } });
    dialog.close();
    await refreshJobs();
    if (action === "edit") openAsset(job.id, asset.id);
    else if (action === "mesh") makeMesh(job.id, asset.id);
    else toast(`후보 ${picked + 1}을 골랐습니다. 보관함 즐겨찾기에서 찾을 수 있습니다.`);
  }
  body.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-pick]"), zoom = event.target.closest("[data-zoom]"), step = event.target.closest("[data-step]"), action = event.target.closest("[data-choose]");
    if (pick) { picked = Number(pick.dataset.pick); render(); }
    else if (zoom) { focus = Number(zoom.dataset.zoom); picked = focus; render(); }
    else if (step) { focus = (focus + Number(step.dataset.step) + candidates.length) % candidates.length; picked = focus; render(); }
    else if (event.target.closest("[data-grid]")) { focus = null; render(); }
    else if (action) choose(action.dataset.choose).catch((error) => toast(error.message));
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.target.matches("input")) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      const delta = event.key === "ArrowRight" ? 1 : -1;
      picked = (picked + delta + candidates.length) % candidates.length;
      if (focus !== null) focus = picked;
      render();
    } else if (/^[1-8]$/.test(event.key) && Number(event.key) <= candidates.length) {
      picked = Number(event.key) - 1;
      if (focus !== null) focus = picked;
      render();
    }
  });
  // 크게 보기에서 Escape는 창을 닫지 않고 격자로 돌아간다.
  dialog.addEventListener("cancel", (event) => { if (focus !== null) { event.preventDefault(); focus = null; render(); } });
  render();
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

const STORAGE_ORDER = ["results", "bases", "copies", "previews", "intermediate", "other", "records"];
function storageHtml(report) {
  if (!report.jobs.length) return `<div class="empty" style="margin-top: 12px">아직 보관한 작업이 없습니다.</div>`;
  const total = report.bytes || 1;
  const categories = STORAGE_ORDER.filter((key) => report.categories[key]);
  const bar = categories.map((key) => `<i class="storage-${key}" style="width:${Math.max(.4, report.categories[key] / total * 100)}%" data-tip="${escapeHtml(`${report.labels[key]} ${formatBytes(report.categories[key])}`)}"></i>`).join("");
  const legend = categories.map((key) => `<span><i class="storage-${key}"></i>${escapeHtml(report.labels[key])} <strong>${formatBytes(report.categories[key])}</strong></span>`).join("");
  const rows = report.jobs.slice(0, state.storageAll ? report.jobs.length : 12).map((job) => {
    // 1MB보다 작은 중간 파일은 정리할 이유가 없어 보여 주지 않는다.
    const intermediate = (job.categories.intermediate || 0) >= 1048576 ? job.categories.intermediate : 0;
    return `<tr><td class="storage-title"><span>${escapeHtml(job.title)}</span><small>${escapeHtml(RECIPE_LABELS[job.recipe] || job.recipe)} · ${escapeHtml(timeLabel(job.createdAt))}${job.usedBy.length ? ` · 다른 버전 ${job.usedBy.length}개의 원본` : ""}</small></td>
      <td class="num">${formatBytes(job.bytes)}</td><td class="num">${intermediate ? formatBytes(intermediate) : "–"}</td>
      <td class="storage-actions">${bridge ? `<button class="ghost" type="button" data-storage="reveal" data-job="${escapeHtml(job.jobId)}">Finder</button>` : ""}
        ${bridge && intermediate && !isActive(job) ? `<button class="ghost" type="button" data-storage="intermediate" data-job="${escapeHtml(job.jobId)}">중간 파일 정리</button>` : ""}
        ${bridge && !isActive(job) ? `<button class="ghost is-danger" type="button" data-storage="job" data-job="${escapeHtml(job.jobId)}">휴지통으로</button>` : ""}</td></tr>`;
  }).join("");
  return `<div class="storage-summary"><strong>${formatBytes(report.bytes)}</strong><span>작업 ${report.jobs.length}개</span>${report.categories.intermediate ? `<span>중간 파일 ${formatBytes(report.categories.intermediate)}</span>` : ""}</div>
    <div class="storage-bar">${bar}</div><div class="storage-legend">${legend}</div>
    <div class="table-wrap"><table class="storage-table"><thead><tr><th>작업</th><th>전체</th><th>중간 파일</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    ${report.jobs.length > 12 ? `<button class="ghost" type="button" data-storage="all">${state.storageAll ? "큰 작업만 보기" : `작업 ${report.jobs.length}개 모두 보기`}</button>` : ""}
    ${bridge ? "" : '<p class="hint">휴지통으로 보내기는 앱에서만 할 수 있습니다.</p>'}`;
}

async function refreshStorage() {
  const container = $("#storage");
  try {
    state.storage = await api("/api/storage");
    container.innerHTML = storageHtml(state.storage);
  } catch (error) {
    container.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

async function storageAction(button) {
  const { storage: action, job: jobId } = button.dataset;
  if (action === "all") { state.storageAll = !state.storageAll; $("#storage").innerHTML = storageHtml(state.storage); return; }
  const job = state.storage?.jobs.find((item) => item.jobId === jobId);
  if (!job) return;
  if (action === "reveal") { bridge?.reveal(jobId, "job.json"); return; }
  const intermediate = action === "intermediate";
  const warning = intermediate
    ? `<p>생성 과정에서만 쓰인 표면 재구성·감축 작업 파일 ${job.intermediate.length}개(${formatBytes(job.categories.intermediate)})를 휴지통으로 보냅니다.</p><p class="hint">결과물·원본·다시 만들기에 필요한 파일은 그대로 둡니다. 휴지통에서 되살릴 수 있습니다.</p>`
    : `<p>작업 폴더 전체(${formatBytes(job.bytes)})를 휴지통으로 보냅니다. 이 작업의 결과물도 목록에서 사라집니다.</p>${job.usedBy.length ? `<p class="notice">다른 버전 ${job.usedBy.length}개가 이 작업을 원본으로 씁니다. 그 버전들의 '버전 이력'에서 이 단계가 빠집니다.</p>` : ""}<p class="hint">휴지통에서 되살리면 다시 나타납니다.</p>`;
  const dialog = popup(intermediate ? "중간 파일 정리" : "작업을 휴지통으로", `<strong>${escapeHtml(job.title)}</strong>${warning}<div class="control-row"><button class="ghost" type="button" data-cancel>취소</button><button class="primary" type="button" data-confirm>휴지통으로 보내기</button></div>`);
  dialog.querySelector("[data-cancel]").onclick = () => dialog.close();
  dialog.querySelector("[data-confirm]").onclick = async (event) => {
    event.target.disabled = true;
    try {
      if (intermediate) {
        const result = await bridge.trashIntermediate(jobId, job.intermediate);
        toast(`중간 파일 ${result.trashed}개(${formatBytes(result.bytes)})를 휴지통으로 보냈습니다.`);
      } else {
        await bridge.trashJob(jobId);
        toast("작업을 휴지통으로 보냈습니다.");
        await refreshJobs();
      }
      dialog.close();
      refreshStorage();
    } catch (error) {
      toast(error.message.replace(/^Error invoking remote method '[^']+': (Error: )?/, ""));
      event.target.disabled = false;
    }
  };
}

async function refreshSystem() {
  const container = $("#doctor");
  container.innerHTML = `<div class="empty">검사하는 중…</div>`;
  refreshStorage();
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
  $("#category-chips").addEventListener("click", event => {
    const chip = event.target.closest("[data-category]");
    if (!chip) return;
    const preset = presetsFor("2d").find(p => (p.category || "game") === chip.dataset.category);
    if (preset) { state.presetId["2d"] = preset.id; renderComposer(); }
  });
  $("#image-model").addEventListener("change", () => {
    state.imageModels[imageCategory()] = $("#image-model").value;
    renderImageModel(); saveComposerDraft();
  });
  $("#count").addEventListener("change", updateSubmitLabel);
  for (const input of $$('input[name="workflow"]')) input.addEventListener('change', updateSubmitLabel);
  for (const id of ["subject", "style", "seed"]) $(`#${id}`).addEventListener("input", saveComposerDraft);
  for (const id of ["count", "pipeline", "texture", "faces", "remove-background"]) $(`#${id}`).addEventListener("change", saveComposerDraft);
  for (const input of $$('input[name="workflow"]')) input.addEventListener("change", saveComposerDraft);
  $("#open-settings").onclick=()=>$("#settings-dialog").showModal();
  for (const id of ['close-settings','apply-settings']) $('#'+id).onclick=()=>{$('#settings-dialog').close();updateSubmitLabel();};
  $('#remove-background').onchange=updateSubmitLabel;
  $('#prompt-examples').onclick=e=>{const b=e.target.closest('[data-example]');if(b){$('#subject').value=b.dataset.example;$('#subject').focus();saveComposerDraft();}};
  $('#pick-library').onclick=()=>chooseFromLibrary().catch(e=>toast(e.message));
  $('#import-image').onclick=async()=>{try{const file=await pickFile();if(!file)return;const upload=await uploadBlob(file);const created=await api('/api/jobs',{method:'POST',body:{recipe:'import-image',params:{uploadId:upload.id,name:file.name.replace(/\.[^.]+$/,'')}}});state.quietJobs.add(created.id);await refreshJobs();
    let told=false;const job=await waitJob(api,created.id,{onUpdate:(record)=>{if(record.state==='queued'&&!told){told=true;toast('앞선 작업이 끝나면 가져옵니다. 완료되면 편집 화면이 열립니다.');}}});await refreshJobs();openAsset(job.id,job.assets[0].id);}catch(error){toast(error.message);}};
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
  $("#library-collection").addEventListener("change", refreshLibrary);
  $("#library-tags").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-library-tag]");
    if (!chip) return;
    state.libraryTag = state.libraryTag === chip.dataset.libraryTag ? "" : chip.dataset.libraryTag;
    refreshLibrary();
  });
  $("#storage").addEventListener("click", (event) => {
    const button = event.target.closest("[data-storage]");
    if (button) storageAction(button);
  });
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
    else if (action === "compare") openCompare(job).catch((error) => toast(error.message));
    else if (action === "favorite") toggleFavorite(target);
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
  restoreComposerDraft();
  renderComposer();
  await refreshJobs();
  await refreshHealth();
  api("/api/doctor?deep=false").then((report) => { state.doctor = report; updateSubmitLabel(); }).catch(() => {});
  const initialView = new URLSearchParams(location.search).get("view");
  if (initialView && $(`#view-${initialView}`)) showView(initialView);
  schedulePoll();
}

init();
