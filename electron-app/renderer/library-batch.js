import { escapeHtml, formatBytes } from "../shared/format.mjs";
import { popup, toast } from "./ui.js";

const key = asset => `${asset.jobId}/${asset.id}`;

export function createLibraryBatch({ api, bridge, refresh }) {
  const bar = document.querySelector("#library-batch");
  const grid = document.querySelector("#library-grid");
  const selected = new Set();
  let visible = [], busy = false;

  function updateBar() {
    bar.innerHTML = `<span role="status">${selected.size}개 선택</span>
      <button type="button" class="ghost" data-batch="all">현재 목록 모두 선택</button>
      <button type="button" class="ghost" data-batch="clear">선택 해제</button>
      <button type="button" class="secondary" data-batch="organize">컬렉션·태그 편집</button>
      ${bridge ? '<button type="button" class="ghost is-danger" data-batch="trash">선택 에셋의 작업 정리</button>' : ""}`;
    for (const button of bar.querySelectorAll("button")) button.disabled = busy || (button.dataset.batch === "all" ? !visible.length : !selected.size);
    for (const checkbox of grid.querySelectorAll("[data-select-asset]")) {
      checkbox.checked = selected.has(checkbox.dataset.selectAsset);
      checkbox.disabled = busy;
      checkbox.closest(".asset-card").classList.toggle("is-selected", checkbox.checked);
    }
  }

  grid.addEventListener("change", event => {
    const input = event.target.closest("[data-select-asset]");
    if (!input) return;
    if (input.checked) selected.add(input.dataset.selectAsset); else selected.delete(input.dataset.selectAsset);
    updateBar();
  });

  async function applyEach(items, action, dialog) {
    busy = true; updateBar();
    const errors = [];
    let completed = 0;
    for (const item of items) {
      try {
        await action(item); completed++;
        for (const asset of visible) if (typeof item === "string" ? asset.jobId === item : key(asset) === key(item)) selected.delete(key(asset));
      } catch (error) { errors.push(error.message); }
    }
    busy = false; dialog.close();
    await refresh();
    toast(`${completed}개 처리${errors.length ? ` · ${errors.length}개 실패: ${errors[0]}` : " 완료"}`);
  }

  function organize() {
    const items = visible.filter(asset => selected.has(key(asset)));
    const dialog = popup(`${items.length}개 에셋 정리`, `<p class="hint">체크한 항목만 바꿉니다. 태그 추가는 기존 태그를 유지합니다.</p>
      <label class="check-label"><input type="checkbox" data-change-collection>컬렉션 변경</label>
      <label class="field"><span class="label">컬렉션 이름</span><input data-collection maxlength="60" placeholder="비우면 컬렉션에서 제외"></label>
      <label class="check-label"><input type="checkbox" data-change-tags>태그 변경</label>
      <label class="field"><span class="label">태그 처리</span><select data-tag-mode><option value="addTags">추가</option><option value="removeTags">제거</option><option value="tags">모두 교체</option></select></label>
      <label class="field"><span class="label">태그 (쉼표로 구분)</span><input data-tags placeholder="캐릭터, 초안"></label>
      <p class="form-error" data-error role="alert"></p><button type="button" class="primary full" data-apply>선택 에셋에 적용</button>`);
    dialog.querySelector("[data-apply]").onclick = async event => {
      const payload = {};
      if (dialog.querySelector("[data-change-collection]").checked) payload.collection = dialog.querySelector("[data-collection]").value;
      if (dialog.querySelector("[data-change-tags]").checked) payload[dialog.querySelector("[data-tag-mode]").value] = dialog.querySelector("[data-tags]").value.split(",").map(s => s.trim()).filter(Boolean);
      if (!Object.keys(payload).length) { dialog.querySelector("[data-error]").textContent = "바꿀 항목을 체크해 주세요."; return; }
      event.target.disabled = true;
      await applyEach(items, asset => api(`/api/jobs/${encodeURIComponent(asset.jobId)}/assets/${encodeURIComponent(asset.id)}/library`, {method: "POST", body: payload}), dialog);
    };
  }

  async function trash() {
    const ids = [...new Set(visible.filter(asset => selected.has(key(asset))).map(a => a.jobId))];
    const jobs = [];
    for (const id of ids) jobs.push(await api(`/api/jobs/${encodeURIComponent(id)}/storage`));
    const active = jobs.filter(job => job.active);
    if (active.length) throw new Error("진행 중인 작업이 포함되어 있습니다. 완료 후 정리해 주세요.");
    const report = await api("/api/storage");
    const used = report.jobs.filter(job => ids.includes(job.jobId) && job.usedBy.length);
    const dialog = popup(`작업 ${ids.length}개를 휴지통으로`, `<p>선택한 에셋이 속한 <strong>작업 폴더 전체</strong>를 옮깁니다. 같은 작업의 선택하지 않은 에셋·원본·기록도 함께 목록에서 사라집니다.</p>
      <ul>${jobs.map(job => `<li>${escapeHtml(job.title)} · ${formatBytes(job.bytes)}</li>`).join("")}</ul>
      ${used.length ? `<p class="notice">${used.length}개 작업을 다른 버전이 원본으로 참조합니다. 해당 버전 이력에서 부모가 빠집니다.</p>` : ""}
      <p class="hint">Finder 휴지통에서 되살릴 수 있습니다.</p><div class="control-row"><button type="button" class="ghost" data-cancel>취소</button><button type="button" class="primary" data-confirm>작업 폴더 전체를 휴지통으로</button></div>`);
    dialog.querySelector("[data-cancel]").onclick = () => dialog.close();
    dialog.querySelector("[data-confirm]").onclick = async event => {
      event.target.disabled = true;
      await applyEach(ids, async id => { if (!await bridge.trashJob(id)) throw new Error("작업을 옮기지 못했습니다."); }, dialog);
    };
  }

  bar.addEventListener("click", event => {
    const action = event.target.closest("[data-batch]")?.dataset.batch;
    if (busy) return;
    if (action === "all") { for (const asset of visible) selected.add(key(asset)); updateBar(); }
    if (action === "clear") { selected.clear(); updateBar(); }
    if (action === "organize") organize();
    if (action === "trash") trash().catch(error => toast(error.message));
  });

  return {
    render(assets) {
      visible = assets;
      const keys = new Set(assets.map(key));
      // 필터 밖으로 숨겨진 항목은 다음 일괄 작업에 섞지 않는다.
      for (const id of selected) if (!keys.has(id)) selected.delete(id);
      for (const [index, card] of [...grid.querySelectorAll(".asset-card")].entries()) {
        const asset = assets[index];
        const label = document.createElement("label"); label.className = "card-select";
        const input = document.createElement("input"); input.type = "checkbox"; input.dataset.selectAsset = key(asset);
        input.setAttribute("aria-label", `${asset.jobTitle} · ${asset.id} 선택`);
        label.append(input); card.append(label);
      }
      updateBar();
    },
  };
}
