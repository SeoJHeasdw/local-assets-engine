import { escapeHtml, fileUrl } from "../shared/format.mjs";
import { popup, toast } from "./ui.js";

// The editor and video details share one lineage view, including cross-kind children.
export async function showVersionHistory(api, jobId, assetId, onOpen, { currentLabel = "현재 버전" } = {}) {
  const data = await api(`/api/jobs/${encodeURIComponent(jobId)}/assets/${encodeURIComponent(assetId)}/versions`);
  const date = iso => { const value = new Date(iso); return Number.isNaN(value.getTime()) ? "" : value.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
  const kinds = { image: "2D", mesh: "3D", video: "영상" };
  const box = popup("버전 이력", `<p class="hint">원본과 파생 버전을 보여 줍니다. 이전 이미지를 열어 편집하면 그 버전에서 새 갈래로 저장됩니다.</p>
    <ol class="version-tree">${data.versions.map((node, index) => `<li style="--depth:${node.depth}" class="${node.key === data.current ? "is-current" : ""}">
      <button type="button" data-version-index="${index}">
        ${node.preview ? `<img src="${fileUrl(node.jobId, node.preview)}" alt="" loading="lazy">` : '<span class="version-blank"></span>'}
        <span class="version-text"><strong>${escapeHtml([node.relation || "원본", node.missingParent ? "이전 기록 없음" : ""].filter(Boolean).join(" · "))}${node.favorite ? " ★" : ""}</strong>
        <small>${escapeHtml([kinds[node.kind] || node.kind, node.summary.join(" · "), date(node.createdAt)].filter(Boolean).join(" · "))}</small>
        <small class="version-title">${escapeHtml(node.title)}</small></span>
        ${node.key === data.current ? `<span class="version-badge">${escapeHtml(currentLabel)}</span>` : ""}
      </button></li>`).join("")}</ol>`, { wide: true });
  box.addEventListener("click", event => {
    const target = event.target.closest("[data-version-index]");
    if (!target) return;
    const node = data.versions[Number(target.dataset.versionIndex)];
    if (!node) return;
    box.close();
    if (node.key !== data.current) Promise.resolve().then(() => onOpen(node.jobId, node.assetId)).catch(error => toast(error.message));
  });
  return box;
}
