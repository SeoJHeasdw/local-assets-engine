// [data-tip]이 붙은 요소에 마우스를 올리거나 키보드로 초점을 옮기면 설명을 띄운다.
// title 속성은 늦게 뜨고 모양을 고를 수 없어 조작 설명에 쓰기 어렵다.
const SHOW_DELAY_MS = 320;
const GAP = 8;

export function installTooltips(bubble) {
  let timer = null;
  let current = null;

  function place(target) {
    const box = target.getBoundingClientRect();
    bubble.hidden = false;
    const tip = bubble.getBoundingClientRect();
    const below = box.bottom + GAP + tip.height < window.innerHeight;
    const top = below ? box.bottom + GAP : box.top - GAP - tip.height;
    const left = Math.min(window.innerWidth - tip.width - GAP, Math.max(GAP, box.left + box.width / 2 - tip.width / 2));
    bubble.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }

  function show(target) {
    const text = target.getAttribute("data-tip");
    if (!text) return;
    current = target;
    bubble.textContent = text;
    target.setAttribute("aria-describedby", bubble.id);
    place(target);
  }

  function hide() {
    clearTimeout(timer);
    current?.removeAttribute("aria-describedby");
    current = null;
    bubble.hidden = true;
  }

  function schedule(event) {
    const target = event.target.closest?.("[data-tip]");
    if (!target || target === current) return;
    hide();
    timer = setTimeout(() => show(target), event.type === "focusin" ? 0 : SHOW_DELAY_MS);
  }

  document.addEventListener("mouseover", schedule);
  document.addEventListener("focusin", schedule);
  document.addEventListener("mouseout", (event) => {
    const target = event.target.closest?.("[data-tip]");
    if (target && !target.contains(event.relatedTarget)) hide();
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("scroll", hide, true);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hide(); });
}
