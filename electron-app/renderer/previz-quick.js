import { escapeHtml, fileUrl } from "../shared/format.mjs";
import { totalSeconds } from "../shared/previz.mjs";

export function quickPanelHtml({ placed, cuts, meshes, standins, plan }) {
  const hero = placed[0];
  const heroValue = hero ? hero.standin ? `standin:${hero.standin}` : `${hero.jobId}:${hero.assetId}` : "";
  const option = (value, label) => `<option value="${escapeHtml(value)}" ${heroValue === value ? "selected" : ""}>${escapeHtml(label)}</option>`;
  return `${plan ? `<section class="pv-plan-result" aria-label="설명에서 읽은 내용"><h3>설명에서 읽은 내용</h3>
      <ul>${plan.summary.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      ${plan.notes.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}</section>` : ""}
    ${placed.length ? `<section class="pv-quick-section" aria-label="주인공 선택">
      <h3>주인공 바꾸기 <small>필요할 때만</small></h3>
      <label class="field"><span class="label">사람 대역 또는 내 3D 에셋</span>
        <select id="pv-quick-hero" ${hero ? "" : "disabled"}>
          <optgroup label="회색 대역">${standins.map((kind) => option(`standin:${kind.id}`, `${kind.label} 대역`)).join("")}</optgroup>
          ${meshes.length ? `<optgroup label="내 3D 에셋">${meshes.map((mesh) => option(`${mesh.jobId}:${mesh.id}`, mesh.jobTitle)).join("")}</optgroup>` : ""}
        </select></label>
      <p class="hint">사람은 구도를 잡는 회색 마네킹입니다. 걷기·표정 애니메이션은 아직 지원하지 않습니다.</p>
      <p class="pv-quick-cast">현재 장면에 ${placed.length}개 · ${placed.map((item) => escapeHtml(item.label)).join(" · ")}</p>
        <button class="ghost" type="button" data-pv="advanced" data-section="scene">배치 직접 바꾸기 ↗</button>
    </section>
    <section class="pv-quick-section" aria-label="영상 순서">
      <h3>영상 순서</h3>
      <p class="hint">${cuts.length}컷 · ${totalSeconds(cuts).toFixed(1)}초. 준비된 카메라를 그대로 써도 됩니다.</p>
      <ol class="pv-quick-cuts">${cuts.map((cut, index) => `<li><span>${index + 1}</span><div><strong>${escapeHtml(cut.label || cut.id)}</strong>
        <small>${escapeHtml(cut.purpose || "")}</small></div><span class="pv-seconds">
          <button type="button" data-pv="seconds" data-index="${index}" data-delta="-0.5" aria-label="${index + 1}번 컷 0.5초 줄이기">−</button>
          <output>${Number(cut.seconds).toFixed(1)}초</output>
          <button type="button" data-pv="seconds" data-index="${index}" data-delta="0.5" aria-label="${index + 1}번 컷 0.5초 늘리기">+</button></span></li>`).join("")}</ol>
      <button class="ghost" type="button" data-pv="advanced" data-section="cuts">컷·카메라 직접 바꾸기 ↗</button>
    </section>` : ""}`;
}

export function quickStageHtml({ placed, cuts, standins, icon }) {
  const ready = placed.length > 0;
  return `<div class="pv-quick-empty">
    <div class="pv-scene-illustration" aria-label="${ready ? "장면 구성 안내" : "사람 장면 예시"}">${(ready ? placed : [{ standin: "person", label: "사람" }]).map((item) =>
      `<div>${item.standin ? icon(standins.find((kind) => kind.id === item.standin)) : item.preview
        ? `<img src="${fileUrl(item.jobId, item.preview)}" alt="">` : '<span class="pv-object-symbol">◇</span>'}<small>${escapeHtml(item.label)}</small></div>`).join("")}</div>
    <span class="eyebrow">${ready ? "장면 준비 완료" : "첫 프리비즈"}</span>
    <h2>${ready ? "이제 영상으로 확인해 보세요" : "만들고 싶은 장면부터 적어 주세요"}</h2>
    <p>${ready ? `${cuts.length}컷 · ${totalSeconds(cuts).toFixed(1)}초의 카메라가 준비됐습니다.<br>위의 ‘영상 만들기’를 눌러 구도와 움직임을 확인하세요.` : "설명을 적거나 예시를 골라 내용을 덧붙이세요.<br>‘설명으로 장면 준비’를 누르면 반영할 배치와 컷이 나옵니다."}</p>
    <small>배치 안내 그림입니다. 실제 카메라 화면은 만든 영상에서 확인합니다.</small>
  </div>`;
}
