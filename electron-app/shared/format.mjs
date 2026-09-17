// 화면과 테스트가 함께 쓰는 순수 계산. 브라우저에서도 돌아야 하므로 Node API를 쓰지 않는다.

export const JOB_STATE_LABELS = {
  queued: "대기", running: "진행 중", cancelling: "중지 중",
  done: "완료", failed: "실패", cancelled: "중지됨",
};
export const STAGE_STATE_LABELS = {
  running: "진행 중", done: "완료", failed: "실패", cancelled: "중지됨", skipped: "건너뜀",
};
export const REVIEW_LABELS = { pending: "검토 대기", approved: "승인", rejected: "거절" };
export const RECIPE_LABELS = {
  image: "2D 후보", "image-to-3d": "이미지 → 3D", "text-to-3d": "텍스트 → 3D", previz: "프리비즈 샷",
  "repair-mesh": "기존 3D 복구",
  "refine-mesh": "3D 원본 재구성", "edit-asset": "에셋 편집", "import-image": "가져온 이미지",
};

export function isActive(job) {
  return ["queued", "running", "cancelling"].includes(job?.state);
}

export function currentStage(job) {
  return [...(job?.stages || [])].reverse().find((stage) => stage.state === "running") || null;
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 || value >= 100 ? Math.round(value) : value.toFixed(1)}${units[unit]}`;
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "–";
  if (seconds < 10) return `${seconds.toFixed(1)}초`;
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds}초`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}분 ${String(seconds % 60).padStart(2, "0")}초`;
  return `${Math.floor(minutes / 60)}시간 ${String(minutes % 60).padStart(2, "0")}분`;
}

export function elapsedSeconds(startedAt, finishedAt, now = Date.now()) {
  if (!startedAt) return null;
  const end = finishedAt ? Date.parse(finishedAt) : now;
  return Math.max(0, (end - Date.parse(startedAt)) / 1000);
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

export function fileUrl(jobId, relative) {
  const parts = String(relative || "").split("/").map(encodeURIComponent).join("/");
  return `/files/${encodeURIComponent(jobId)}/${parts}`;
}

// 폼 상태를 엔진 요청 하나로 바꾼다. 3D를 설명으로 만들 때 후보가 1장이면 바로 3D까지
// 가고, 여러 장이면 컨셉 후보만 만든 뒤 사용자가 고른 후보를 3D로 바꾼다.
export function buildJobRequest(form) {
  const seed = String(form.seed ?? "").trim();
  if (seed && !/^\d+$/.test(seed)) throw new Error("시드는 0 이상의 정수로 적어 주세요.");
  const mesh = {
    pipelineType: String(form.pipelineType || "512"),
    textureSize: Number(form.textureSize || 4096),
    targetFaces: Number(form.targetFaces ?? 1000000),
    ...(form.gameFaces !== undefined ? { gameFaces: Number(form.gameFaces) } : {}),
  };
  if (form.kind === "3d" && form.source === "image") {
    if (!form.imagePath && !form.uploadId && !form.imageSource) throw new Error("3D로 만들 이미지를 골라 주세요.");
    const source = form.imageSource ? {source:form.imageSource} : form.uploadId ? {uploadId:form.uploadId} : {imagePath:form.imagePath};
    return { recipe: "image-to-3d", params: { ...source, ...(form.imageName ? {name:form.imageName} : {}), ...mesh, ...(seed ? { meshSeed: Number(seed) } : {}) } };
  }
  const subject = String(form.subject ?? "").trim();
  if (!subject) throw new Error("무엇을 만들지 적어 주세요.");
  const base = { preset: form.preset, subject, style: String(form.style ?? "").trim(), ...(seed ? { seed: Number(seed) } : {}) };
  if (form.removeBackground !== undefined && form.kind === "2d") base.removeBackground = Boolean(form.removeBackground);
  const count = Number(form.count || 1);
  if (form.kind === "3d" && (form.workflow === "direct" || (!form.workflow && count === 1))) return { recipe: "text-to-3d", params: { ...base, count: 1, ...mesh } };
  return { recipe: "image", params: { ...base, count } };
}

// 대기열에서 이 작업 앞에 있는 작업 수. 엔진은 만든 순서대로 한 줄로 돌린다.
export function jobsAhead(jobs, jobId) {
  const target = jobs.find((job) => job.id === jobId);
  if (target?.state !== "queued") return 0;
  const created = Date.parse(target.createdAt);
  return jobs.filter((job) => job.id !== jobId && (["running", "cancelling"].includes(job.state)
    || (job.state === "queued" && Date.parse(job.createdAt) <= created))).length;
}

// 기다린 시간(만들어진 뒤 시작까지)과 실제 처리 시간을 나눈다.
export function jobTimes(job, now = Date.now()) {
  const created = Date.parse(job.createdAt), started = job.startedAt ? Date.parse(job.startedAt) : null;
  const finished = job.finishedAt ? Date.parse(job.finishedAt) : null;
  const waitEnd = started ?? finished ?? now;
  return {
    waited: Number.isFinite(created) ? Math.max(0, (waitEnd - created) / 1000) : null,
    worked: started !== null ? Math.max(0, ((finished ?? now) - started) / 1000) : null,
  };
}

// 이전 폴링에서 진행·대기였다가 이번에 끝난 작업. 처음 불러온 목록에서는 알리지 않는다.
export function newlyFinished(previousStates, jobs) {
  if (!previousStates) return [];
  return jobs.filter((job) => isActive({ state: previousStates.get(job.id) }) && !isActive(job));
}
