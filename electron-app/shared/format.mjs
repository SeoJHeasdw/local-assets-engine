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
  "refine-mesh": "3D 원본 재구성", "game-asset": "게임용 에셋 굽기", "edit-asset": "에셋 편집", "import-image": "가져온 이미지",
  selftest: "엔진 자체 점검",
};

// 엔진 계약 검사(selftest)가 남긴 작업은 에셋 생성이 아니다. 작업 목록과 완료 알림에서 뺀다.
export function isDiagnostic(job) {
  return job?.recipe === "selftest";
}

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

// 자동 검사가 남긴 finding의 문장. 판정이 아니라 사람이 볼 자리를 알린다.
const mm = (meters) => `${((meters || 0) * 1000).toFixed(1)}mm`;
const count = (value) => Number(value || 0).toLocaleString();
const FINDING_TEXT = {
  "image.subject_missing": () => "배경을 지우고 나니 남은 물체가 없습니다.",
  "image.final_empty": () => "정리(캔버스·픽셀화)를 거친 최종 파일에서 물체가 사라졌습니다.",
  "image.finish_failed": (p) => `정리에 실패해 정리 전 그림이 남았습니다. ${p.error || ""}`,
  "image.touches_edge": () => "물체가 그림 가장자리에 닿았습니다. 원본에서 잘렸을 수 있습니다.",
  "image.subject_tiny": (p) => `남은 물체가 매우 작습니다(${count(p.pixels)}픽셀). 우연히 남은 점일 수 있습니다.`,
  "image.overscaled": (p) => `캔버스에 맞추며 물체를 ${p.scale}배로 키웠습니다.`,
  "image.alpha_not_cutout": () => "입력 이미지의 투명 부분이 누끼로 보기에 너무 적어 알파를 버리고 배경을 다시 지웠습니다.",
  "mesh.open_boundary": (p) => `열린 경계 모서리 ${count(p.count)}개`,
  "mesh.non_manifold_edges": (p) => `한 모서리에 세 면 이상이 붙은 곳 ${count(p.count)}개`,
  "mesh.inconsistent_winding": (p) => `이웃 면과 방향이 엇갈린 모서리 ${count(p.count)}개`,
  "mesh.duplicate_faces": (p) => `중복 면 ${count(p.count)}개`,
  "mesh.faces_over_target": (p) => `목표 ${count(p.targetFaces)}면보다 많은 ${count(p.triangles)}면이 남았습니다.`,
  "mesh.game_shape_error": (p) => `품질본과의 표면 거리가 95% 지점에서 ${mm(p.p95Meters)}입니다.`,
  "mesh.game_geometry_lost": (p) => `품질본 표본의 ${((p.lostFraction || 0) * 100).toFixed(1)}%가 게임용에서 사라졌습니다(최대 ${mm(p.lostMaxMeters)}).`,
  "mesh.game_bake_misses": (p) => `UV 영역의 ${((p.missFraction || 0) * 100).toFixed(1)}%에서 굽기 광선이 품질본에 닿지 않았습니다.`,
  "mesh.game_export_mismatch": (p) => `내보낸 파일을 다시 불러오니 굽은 결과와 다릅니다: ${(p.problems || []).join(", ")}`,
  "mesh.parts_merged": (p) => `떨어져 있던 부품 ${count(p.source)}개가 재구성 후 ${count(p.reconstructed)}개로 붙었습니다. 좁은 틈이 메워졌을 수 있습니다.`,
  "mesh.floating_parts": (p) => `본체와 떨어져 떠 있는 조각 ${count(p.count)}개`,
  "mesh.inward_parts": (p) => `면이 안쪽을 향하는 닫힌 부품 ${count(p.count)}개`,
  "mesh.intersecting_parts": (p) => `서로 파고든 닫힌 부품 ${count(p.count)}쌍`,
};
export const FINDING_SEVERITY_LABELS = { fail: "실패", warn: "경고" };

export function findingText(finding) {
  const text = FINDING_TEXT[finding?.code];
  return text ? text(finding.params || {}) : finding?.code || "";
}

// 사람이 아직 확인하지 않은 항목. assetId를 주면 그 에셋과 작업 전체에 걸린 항목만.
export function pendingFindings(job, assetId) {
  return (job?.findings || []).filter((finding) => finding.review !== "cleared"
    && (assetId === undefined || finding.assetId === assetId || finding.assetId == null));
}

export function findingsHtml(job, findings) {
  if (!findings.length) return "";
  return `<ul class="findings">${findings.map((finding) => `<li class="finding finding-${escapeHtml(finding.severity)}">
    <span class="finding-head"><strong>${escapeHtml(FINDING_SEVERITY_LABELS[finding.severity] || finding.severity)}</strong> · ${escapeHtml(finding.where)}</span>
    <span>${escapeHtml(findingText(finding))}</span>
    <span class="finding-actions">${(finding.evidence || []).map((file) => `<a href="${fileUrl(job.id, file)}" target="_blank" rel="noopener">${escapeHtml(file.split("/").pop())}</a>`).join("")}
    ${finding.review === "cleared" ? '<em>확인함</em>' : `<button class="ghost" type="button" data-action="clear-finding" data-job="${escapeHtml(job.id)}" data-finding="${escapeHtml(finding.id)}">확인함</button>`}</span>
  </li>`).join("")}</ul>`;
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
  if (form.kind === "2d" && form.imageModel) base.imageModel = form.imageModel;
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

export function estimateLabel(estimate, state) {
  if (!estimate || state === "cancelling") return "";
  if (estimate.overdue) return "예상보다 오래 걸리는 중";
  const seconds = state === "queued" ? estimate.completionSeconds : estimate.remainingSeconds;
  if (!Number.isFinite(seconds)) return "예상 시간 기록 부족";
  return `완료까지 약 ${formatDuration(Math.max(60, Math.ceil(seconds / 60) * 60))} · 기록 ${estimate.samples}개 기준`;
}
