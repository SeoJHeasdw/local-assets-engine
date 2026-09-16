import {
  JOB_STATE_LABELS, RECIPE_LABELS, REVIEW_LABELS, STAGE_STATE_LABELS,
  buildJobRequest, buildPrevizRequest, elapsedSeconds, escapeHtml, fileUrl, formatBytes,
  formatDuration, isActive,
} from "../shared/format.mjs";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
// Electron preload가 주는 기능. 일반 브라우저로 열면 없고, 파일 고르기·Finder 열기만 빠진다.
const bridge = window.assetsStudio || null;

const CAPABILITY_LABELS = {
  image2d: "2D 에셋 생성",
  mesh3d: "3D 에셋 생성",
  meshTexture: "3D 텍스처 Metal 가속 (선택)",
  gameReady: "게임용 GLB 최적화",
  previz: "프리비즈 샷 렌더",
};
const PIXEL_FILE = /px(@preview)?\.png$/;

const state = {
  presets: null,
  kind: "2d",
  source: "text",
  presetId: { "2d": "item-icon", "3d": "prop-3d" },
  imagePath: "",
  imagePreviewUrl: "",
  jobs: [],
  signatures: new Map(),
  doctor: null,
  libraryFilter: "pending",
  view: "create",
  previz: { meshes: [], placed: [], presetId: null, jobId: null, cuts: new Map() },
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

// ---- 화면 전환 -------------------------------------------------------------

function showView(view) {
  state.view = view;
  for (const button of $$(".nav-item")) button.classList.toggle("is-active", button.dataset.view === view);
  for (const section of $$(".view")) section.hidden = section.id !== `view-${view}`;
  if (view === "library") refreshLibrary();
  if (view === "previz") refreshPreviz();
  if (view === "system") refreshSystem();
}

// ---- 만들기 ----------------------------------------------------------------

function presetsFor(kind) {
  return (state.presets?.presets || []).filter((preset) => preset.kind === kind);
}

function renderComposer() {
  const { kind, source } = state;
  const fromImage = kind === "3d" && source === "image";
  for (const button of $$("#kind-switch button")) button.classList.toggle("is-active", button.dataset.kind === kind);
  for (const button of $$("#source-switch button")) button.classList.toggle("is-active", button.dataset.source === source);
  $("#source-switch").hidden = kind !== "3d";
  $("#preset-field").hidden = fromImage;
  $("#subject-field").hidden = fromImage;
  $("#style-field").hidden = fromImage;
  $("#count-field").hidden = fromImage;
  $("#image-field").hidden = !fromImage;
  for (const option of $$(".mesh-option")) option.hidden = kind !== "3d";

  const presets = presetsFor(kind);
  if (!presets.some((preset) => preset.id === state.presetId[kind]) && presets[0]) state.presetId[kind] = presets[0].id;
  $("#preset-chips").innerHTML = presets.map((preset) => `
    <button type="button" class="chip${preset.id === state.presetId[kind] ? " is-active" : ""}" data-preset="${escapeHtml(preset.id)}">
      ${escapeHtml(preset.label)}
    </button>`).join("");
  $("#preset-hint").textContent = presets.find((preset) => preset.id === state.presetId[kind])?.hint || "";
  renderImagePick();
  updateSubmitLabel();
}

function updateSubmitLabel() {
  const count = Number($("#count").value);
  let label = "후보 만들기";
  let hint = "";
  if (state.kind === "3d") {
    if (state.source === "image") label = "이미지로 3D 만들기";
    else if (count === 1) [label, hint] = ["3D 바로 만들기", "컨셉 이미지 1장을 만든 뒤 곧바로 3D로 바꿉니다."];
    else [label, hint] = ["컨셉 후보 만들기", "마음에 드는 후보를 열어 3D로 만들기를 누르세요."];
    if (state.doctor && !state.doctor.capabilities.mesh3d) {
      hint = `${hint} 3D 준비가 덜 됐습니다. 환경 화면을 확인하세요.`.trim();
    }
  }
  $("#submit").textContent = label;
  $("#submit-hint").textContent = hint;
}

function renderImagePick() {
  const preview = $("#image-preview");
  if (!state.imagePath) {
    preview.innerHTML = "";
    return;
  }
  preview.innerHTML = `${state.imagePreviewUrl ? `<img src="${state.imagePreviewUrl}" alt="">` : ""}
    <div class="path">${escapeHtml(state.imagePath)}</div>`;
}

function setImage(path, previewUrl = "") {
  if (state.imagePreviewUrl) URL.revokeObjectURL(state.imagePreviewUrl);
  state.imagePath = path;
  state.imagePreviewUrl = previewUrl;
  renderImagePick();
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
      imagePath: state.imagePath, pipelineType: $("#pipeline").value,
      textureSize: $("#texture").value, targetFaces: $("#faces").value,
    });
  } catch (error) {
    showFormError(error.message);
    return;
  }
  const button = $("#submit");
  button.disabled = true;
  try {
    await api("/api/jobs", { method: "POST", body: request });
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
  const tag = asset.kind === "mesh" ? "3D"
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

function jobCardHtml(job) {
  const elapsed = elapsedSeconds(job.startedAt, job.finishedAt);
  const stages = job.stages.map(stageHtml).join("");
  const assets = job.assets.map((asset) => thumbHtml(job.id, asset)).join("");
  const cancelling = job.state === "cancelling";
  return `
    <header class="job-head">
      <div>
        <div class="job-title">${escapeHtml(job.title)}</div>
        <div class="job-meta">${escapeHtml(RECIPE_LABELS[job.recipe] || job.recipe)} · ${escapeHtml(timeLabel(job.createdAt))}${
          elapsed === null ? "" : ` · <span class="job-elapsed" data-started="${escapeHtml(job.startedAt)}"
            data-finished="${escapeHtml(job.finishedAt || "")}">${formatDuration(elapsed)}</span>`}</div>
      </div>
      <span class="state state-${escapeHtml(job.state)}">${escapeHtml(JOB_STATE_LABELS[job.state] || job.state)}</span>
    </header>
    ${stages ? `<ol class="stages">${stages}</ol>` : ""}
    ${job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : ""}
    ${assets ? `<div class="job-assets">${assets}</div>` : ""}
    ${isActive(job) ? `<footer class="job-foot">
      <button class="ghost is-danger" type="button" data-action="cancel" data-job="${escapeHtml(job.id)}" ${cancelling ? "disabled" : ""}>
        ${cancelling ? "중지하는 중" : "중지"}
      </button></footer>` : ""}`;
}

function renderJobs() {
  const list = $("#job-list");
  if (!state.jobs.length) {
    list.innerHTML = `<div class="empty">아직 작업이 없습니다. 왼쪽에서 첫 에셋을 만들어 보세요.</div>`;
    state.signatures.clear();
  } else {
    list.querySelector(".empty")?.remove();
    const cards = new Map($$(".job", list).map((card) => [card.dataset.id, card]));
    let previous = null;
    for (const job of state.jobs) {
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
  const active = state.jobs.filter(isActive).length;
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
    const working = state.jobs.some((job) => job.recipe === "previz" && isActive(job));
    await refreshJobs();
    await refreshHealth();
    // 프리비즈 작업이 끝나면 컷 목록이 그 자리에서 채워져야 한다.
    if (state.view === "previz" && (working || state.jobs.some((job) => job.recipe === "previz" && isActive(job)))) {
      await refreshPreviz();
    }
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
    if (stats.facesOut != null) add("면 수", `${stats.facesOut.toLocaleString()} (원본 ${stats.facesIn.toLocaleString()})`);
    if (Array.isArray(stats.dimensionsMeters)) add("크기 (m)", stats.dimensionsMeters.map((value) => value.toFixed(2)).join(" × "));
    if (stats.textures != null) add("텍스처", `${stats.textures}장 · ${meta.textureSize}px`);
    add("3D 품질", meta.pipelineType);
    add("시드", meta.seed, true);
    add("GLB", formatBytes(stats.bytes));
    if (meta.optimizedBytes) add("게임용 GLB", formatBytes(meta.optimizedBytes));
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
      ? `<button class="secondary" type="button" data-action="previz" data-job="${escapeHtml(job.id)}" data-asset="${escapeHtml(asset.id)}">프리비즈 만들기</button>`
      : "",
    bridge
      ? `<button class="ghost" type="button" data-action="reveal" data-job="${escapeHtml(job.id)}" data-file="${escapeHtml(asset.file)}">Finder에서 보기</button>`
      : "",
    bridge && meta.optimizedFile
      ? `<button class="ghost" type="button" data-action="reveal" data-job="${escapeHtml(job.id)}" data-file="${escapeHtml(meta.optimizedFile)}">게임용 GLB 보기</button>`
      : "",
  ].join("");

  return `<div class="detail">
    <button class="close" type="button" data-action="close" aria-label="닫기">✕</button>
    <div class="detail-stage">${stage}</div>
    <aside class="detail-side">
      <h3>${escapeHtml(job.title)}</h3>
      <div class="actions">${actions}</div>
      <p class="hint" id="detail-message"></p>
      <dl class="meta">${rows.join("")}</dl>
      ${job.params?.prompt ? `<div class="prompt">${escapeHtml(job.params.prompt)}</div>` : ""}
    </aside>
  </div>`;
}

async function openAsset(jobId, assetId) {
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    const asset = job.assets.find((item) => item.id === assetId);
    if (!asset) return;
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
    refreshJobs();
    if (state.view === "library") refreshLibrary();
    if (state.view === "previz") refreshPreviz();
  } catch (error) {
    setDetailMessage(error.message);
  }
}

async function makeMesh(jobId, assetId) {
  try {
    await api("/api/jobs", {
      method: "POST",
      body: {
        recipe: "image-to-3d",
        params: {
          source: { jobId, assetId }, pipelineType: $("#pipeline").value,
          textureSize: Number($("#texture").value), targetFaces: Number($("#faces").value),
        },
      },
    });
    $("#asset-dialog").close();
    showView("create");
    refreshJobs();
  } catch (error) {
    setDetailMessage(error.message);
  }
}

// ---- 프리비즈 ---------------------------------------------------------------

function shotPresets() {
  return state.presets?.previz?.shotPresets || [];
}

function currentShotPreset() {
  return shotPresets().find((preset) => preset.id === state.previz.presetId) || shotPresets()[0] || null;
}

// 위에서 본 배치. 좌표를 값으로만 적으면 무엇이 어디 놓였는지 알 수 없다.
function sceneMapHtml(placed) {
  const size = 100;
  // 놓인 것들이 지도를 꽉 채우면 간격을 읽을 수 없다. 둘레에 1.5m 여유를 둔다.
  const reach = Math.max(3, ...placed.map((item) => Math.abs(Number(item.x) || 0) + 1.5),
    ...placed.map((item) => Math.abs(Number(item.y) || 0) + 1.5));
  const scale = size / 2 / reach;
  const grid = [];
  for (let meter = -Math.ceil(reach); meter <= Math.ceil(reach); meter += 1) {
    const at = size / 2 + meter * scale;
    const axis = meter === 0 ? ' class="axis"' : "";
    grid.push(`<line x1="${at}" y1="0" x2="${at}" y2="${size}"${axis}></line>`,
      `<line x1="0" y1="${at}" x2="${size}" y2="${at}"${axis}></line>`);
  }
  const dots = placed.map((item, index) => {
    const x = size / 2 + (Number(item.x) || 0) * scale;
    // 화면 위쪽이 +Y다. 샷 프리셋의 기본 시선이 −Y에서 들어오므로 카메라는 아래쪽이다.
    const y = size / 2 - (Number(item.y) || 0) * scale;
    const radius = Math.min(size / 6, Math.max(2, 0.5 * (Number(item.scale) || 1) * scale));
    const yaw = ((Number(item.yaw) || 0) - 90) * (Math.PI / 180);
    return `<g class="dot${index === 0 ? " hero" : ""}">
      <circle cx="${x}" cy="${y}" r="${radius}"></circle>
      <line x1="${x}" y1="${y}" x2="${x + Math.cos(yaw) * radius * 1.8}"
        y2="${y + Math.sin(yaw) * radius * 1.8}"></line>
      <text x="${x}" y="${y - radius - 2}">${index === 0 ? "주인공" : `#${index + 1}`}</text></g>`;
  }).join("");
  return `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="장면 배치">
    <g class="grid">${grid.join("")}</g>${dots}
    <text class="camera-note" x="${size / 2}" y="${size - 2}">▲ 카메라가 들어오는 쪽</text></svg>`;
}

function placedRowHtml(item, index) {
  const cell = (field, label) => `<label class="cell"><span>${label}</span>
    <input inputmode="decimal" data-previz-field="${field}" data-index="${index}" value="${escapeHtml(item[field])}"></label>`;
  return `<div class="placed-row">
    ${item.preview ? `<img src="${fileUrl(item.jobId, item.preview)}" alt="">` : `<span class="placed-blank"></span>`}
    <div class="placed-main">
      <strong>${escapeHtml(item.label)}</strong>
      <span class="placed-tag">${index === 0 ? "주인공" : `#${index + 1}`}</span>
    </div>
    <button class="ghost is-danger" type="button" data-action="previz-remove" data-index="${index}">제거</button>
    <div class="placed-cells">${cell("x", "x (m)")}${cell("y", "y (m)")}${cell("yaw", "회전 (°)")}${cell("scale", "크기")}</div>
  </div>`;
}

function renderPrevizForm() {
  const { meshes, placed } = state.previz;
  const picker = $("#previz-mesh");
  picker.innerHTML = meshes.length
    ? meshes.map((asset) => `<option value="${escapeHtml(asset.jobId)}:${escapeHtml(asset.id)}">
        ${escapeHtml(asset.jobTitle)}${asset.review === "approved" ? " · 승인됨" : ""}</option>`).join("")
    : `<option value="">아직 3D 에셋이 없습니다</option>`;
  picker.disabled = !meshes.length;
  $("#previz-add").disabled = !meshes.length;

  $("#previz-placed").innerHTML = placed.length ? placed.map(placedRowHtml).join("")
    : meshes.length
      ? `<p class="hint">3D 에셋을 골라 추가하세요. 첫 에셋이 주인공이 되고, 샷 프리셋의 <em>hero</em> 컷이 그것을 겨냥합니다.</p>`
      : `<p class="hint">먼저 <strong>만들기</strong>에서 3D 에셋을 만들어 주세요. 완성된 메시가 프리비즈 장면의 재료입니다.</p>`;
  $("#previz-map-field").hidden = !placed.length;
  if (placed.length) $("#previz-map").innerHTML = sceneMapHtml(placed);

  const preset = currentShotPreset();
  state.previz.presetId = preset?.id || null;
  $("#shot-preset-chips").innerHTML = shotPresets().map((item) => `<button type="button"
      class="chip${item.id === state.previz.presetId ? " is-active" : ""}" data-shot-preset="${escapeHtml(item.id)}">
    ${escapeHtml(item.label)}</button>`).join("");
  $("#shot-preset-hint").textContent = preset?.hint || "";

  const cuts = preset?.shots || [];
  const seconds = cuts.reduce((sum, shot) => sum + Number(shot.seconds || 0), 0);
  const frames = Math.round(seconds * Number($("#previz-fps").value || 12));
  $("#previz-cost").textContent = cuts.length
    ? `${cuts.length}컷 · ${seconds.toFixed(1)}초 · 렌더 약 ${frames}프레임`
    : "";
  $("#previz-submit").disabled = !placed.length || !cuts.length;
}

function cutHtml(asset) {
  const meta = asset.meta || {};
  const values = [meta.move, meta.lens ? `${Math.round(meta.lens)}mm` : "",
    meta.seconds ? `${meta.seconds}초` : "", meta.frames ? `${meta.frames}프레임` : ""].filter(Boolean).join(" · ");
  const review = (status, label, className) => `<button class="ghost ${className}${asset.review === status ? " is-on" : ""}"
      type="button" data-action="review" data-job="${escapeHtml(asset.jobId)}" data-asset="${escapeHtml(asset.id)}"
      data-target="${status}">${label}</button>`;
  return `<article class="cut${asset.review === "rejected" ? " is-rejected" : ""}">
    <button class="cut-thumb" type="button" data-action="open"
        data-job="${escapeHtml(asset.jobId)}" data-asset="${escapeHtml(asset.id)}">
      <img src="${fileUrl(asset.jobId, asset.preview || asset.file)}" alt="" loading="lazy">
      <span class="tag">${escapeHtml(meta.shot || "")}</span>
    </button>
    <div class="cut-body">
      <div class="cut-title">
        <strong>${escapeHtml(meta.label || meta.shot || asset.id)}</strong>
        <span class="cut-order">${meta.order ? `${meta.order}번째 컷` : ""}</span>
      </div>
      <p class="cut-purpose">${escapeHtml(meta.purpose || "")}</p>
      <div class="cut-values">${escapeHtml(values)}</div>
      <div class="cut-review">${review("approved", "승인", "approve")}${review("rejected", "거절", "reject")}</div>
    </div>
  </article>`;
}

function renderPrevizCuts() {
  const { cuts, jobId } = state.previz;
  const picker = $("#previz-job");
  picker.hidden = cuts.size < 2;
  picker.innerHTML = [...cuts.entries()].map(([id, group]) =>
    `<option value="${escapeHtml(id)}"${id === jobId ? " selected" : ""}>${escapeHtml(group.title)}</option>`).join("");
  const working = state.jobs.filter((job) => job.recipe === "previz" && isActive(job))
    .map((job) => `<article class="job is-active">${jobCardHtml(job)}</article>`).join("");
  const group = cuts.get(jobId);
  const shots = group ? group.shots.map(cutHtml).join("") : "";
  $("#previz-cuts").innerHTML = working + shots || `<div class="empty">
    아직 만든 샷이 없습니다. 장면을 꾸리고 "샷 만들기"를 누르세요.</div>`;
}

async function refreshPreviz() {
  try {
    const [meshes, shots] = await Promise.all([api("/api/assets?kind=mesh"), api("/api/assets?kind=shot")]);
    state.previz.meshes = meshes.assets;
    const cuts = new Map();
    for (const shot of shots.assets) {
      if (!cuts.has(shot.jobId)) cuts.set(shot.jobId, { title: shot.jobTitle, shots: [] });
      cuts.get(shot.jobId).shots.push(shot);
    }
    for (const group of cuts.values()) group.shots.sort((a, b) => (a.meta?.order || 0) - (b.meta?.order || 0));
    state.previz.cuts = cuts;
    if (!cuts.has(state.previz.jobId)) state.previz.jobId = cuts.keys().next().value || null;
    renderPrevizForm();
    renderPrevizCuts();
  } catch (error) {
    $("#previz-cuts").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

function addPlaced(jobId, assetId) {
  const { meshes, placed } = state.previz;
  if (placed.some((item) => item.jobId === jobId && item.assetId === assetId)) return;
  const asset = meshes.find((item) => item.jobId === jobId && item.id === assetId);
  // 두 번째부터는 겹치지 않게 옆으로 놓는다. 그 뒤 위치는 사람이 고친다.
  placed.push({
    jobId, assetId, label: asset?.jobTitle || assetId, preview: asset?.preview,
    x: placed.length ? (placed.length * 1.2).toFixed(1) : "0", y: "0", yaw: "0", scale: "1",
  });
  renderPrevizForm();
}

async function addToPrevizScene(jobId, assetId) {
  $("#asset-dialog").close();
  showView("previz");
  await refreshPreviz();
  addPlaced(jobId, assetId);
}

async function submitPreviz(event) {
  event.preventDefault();
  const button = $("#previz-submit");
  const error = $("#previz-error");
  error.hidden = true;
  try {
    const request = buildPrevizRequest({
      assets: state.previz.placed, preset: state.previz.presetId,
      renderer: $("#previz-renderer").value, resolution: $("#previz-resolution").value,
      fps: $("#previz-fps").value, aux: $("#previz-aux").value,
      clay: $("#previz-clay").checked, animatic: $("#previz-animatic").checked,
    });
    button.disabled = true;
    await api("/api/jobs", { method: "POST", body: request });
    await refreshJobs();
    await refreshPreviz();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

// ---- 보관함 -----------------------------------------------------------------

async function refreshLibrary() {
  const grid = $("#library-grid");
  const filter = state.libraryFilter;
  for (const button of $$("#library-filter button")) button.classList.toggle("is-active", button.dataset.filter === filter);
  try {
    const { assets } = await api(`/api/assets${filter === "all" ? "" : `?review=${filter}`}`);
    grid.innerHTML = assets.length
      ? assets.map((asset) => `<div class="asset-card">${thumbHtml(asset.jobId, asset)}
          <div class="caption">${escapeHtml(asset.jobTitle)}</div></div>`).join("")
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
  $("#previz-form").addEventListener("submit", submitPreviz);
  $("#previz-add").addEventListener("click", () => {
    const [jobId, assetId] = String($("#previz-mesh").value || "").split(":");
    if (jobId && assetId) addPlaced(jobId, assetId);
  });
  $("#previz-placed").addEventListener("input", (event) => {
    const input = event.target.closest("[data-previz-field]");
    if (!input) return;
    // 입력 중에 폼을 다시 그리면 글자를 치던 칸이 초점을 잃는다. 지도만 고친다.
    state.previz.placed[Number(input.dataset.index)][input.dataset.previzField] = input.value;
    $("#previz-map").innerHTML = sceneMapHtml(state.previz.placed);
  });
  $("#shot-preset-chips").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-shot-preset]");
    if (!chip) return;
    state.previz.presetId = chip.dataset.shotPreset;
    renderPrevizForm();
  });
  $("#previz-fps").addEventListener("change", renderPrevizForm);
  $("#previz-job").addEventListener("change", (event) => {
    state.previz.jobId = event.target.value;
    renderPrevizCuts();
  });
  $("#doctor-refresh").addEventListener("click", refreshSystem);
  // 닫을 때 비워야 3D 뷰어가 뒤에서 계속 그리지 않는다.
  $("#asset-dialog").addEventListener("close", () => { $("#asset-detail").innerHTML = ""; });

  $("#pick-image").addEventListener("click", async () => {
    if (!bridge) return showFormError("파일 고르기는 앱에서만 쓸 수 있습니다.");
    const path = await bridge.pickImage();
    if (path) setImage(path);
  });
  const dropzone = $("#image-field");
  dropzone.addEventListener("dragover", (event) => { event.preventDefault(); dropzone.classList.add("is-over"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-over"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-over");
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    const path = bridge?.pathForFile(file) || "";
    if (!path) return showFormError("끌어다 놓은 파일의 경로를 읽지 못했습니다. 이미지 고르기를 써 주세요.");
    showFormError("");
    setImage(path, URL.createObjectURL(file));
  });
  // 창 아무 데나 파일을 놓으면 Electron이 그 파일로 이동하려 한다.
  for (const type of ["dragover", "drop"]) document.addEventListener(type, (event) => event.preventDefault());

  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const { action, job, asset } = target.dataset;
    if (action === "open") openAsset(job, asset);
    else if (action === "review") setReview(target);
    else if (action === "to3d") makeMesh(job, asset);
    else if (action === "previz") addToPrevizScene(job, asset);
    else if (action === "previz-remove") {
      state.previz.placed.splice(Number(target.dataset.index), 1);
      renderPrevizForm();
    }
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

async function init() {
  wireEvents();
  try {
    state.presets = await api("/api/presets");
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
