import {
  JOB_STATE_LABELS, RECIPE_LABELS, REVIEW_LABELS, STAGE_STATE_LABELS,
  buildJobRequest, elapsedSeconds, escapeHtml, fileUrl, formatBytes, formatDuration, isActive,
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
  const tag = asset.kind === "mesh" ? "3D" : asset.role === "concept" ? "컨셉" : "";
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
    await refreshJobs();
    await refreshHealth();
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
