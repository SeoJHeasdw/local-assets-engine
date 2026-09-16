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
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}초`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}분 ${String(Math.round(seconds % 60)).padStart(2, "0")}초`;
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
    textureSize: Number(form.textureSize || 1024),
    targetFaces: Number(form.targetFaces ?? 30000),
  };
  if (form.kind === "3d" && form.source === "image") {
    if (!form.imagePath) throw new Error("3D로 만들 이미지를 골라 주세요.");
    return { recipe: "image-to-3d", params: { imagePath: form.imagePath, ...mesh, ...(seed ? { meshSeed: Number(seed) } : {}) } };
  }
  const subject = String(form.subject ?? "").trim();
  if (!subject) throw new Error("무엇을 만들지 적어 주세요.");
  const base = { preset: form.preset, subject, style: String(form.style ?? "").trim(), ...(seed ? { seed: Number(seed) } : {}) };
  const count = Number(form.count || 1);
  if (form.kind === "3d" && count === 1) return { recipe: "text-to-3d", params: { ...base, count: 1, ...mesh } };
  return { recipe: "image", params: { ...base, count } };
}
