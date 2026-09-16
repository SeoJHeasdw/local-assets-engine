// 패널 가장자리를 끌어 너비를 바꾼다. 두 번 누르면 기본 너비, 초점을 두고 ←→로도 조절한다.
// 사람이 고른 너비는 기억해 두고, 창이 좁아지면 그때그때 한도 안으로만 줄여 보여 준다.
const KEY_STEP = 16;

function remembered(key) {
  try { return Number(localStorage.getItem(key)) || null; } catch { return null; }
}

function remember(key, value) {
  try { localStorage.setItem(key, String(value)); } catch { /* 저장이 막혀도 조절은 된다 */ }
}

export function installResizer(handle, { storageKey, min, max, fallback, apply }) {
  let preferred = remembered(storageKey) || fallback;
  let drag = null;
  const limit = (value) => Math.round(Math.min(Math.max(min, typeof max === "function" ? max() : max), Math.max(min, value)));

  function show() {
    const width = limit(preferred);
    handle.setAttribute("aria-valuenow", String(width));
    apply(width);
    return width;
  }

  function choose(value) {
    preferred = limit(value);
    remember(storageKey, show());
  }

  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute("aria-valuemin", String(min));
  handle.tabIndex = 0;
  show();

  // 손잡이는 몇 px뿐이라 포인터 캡처가 풀리면 곧 놓친다. 끄는 동안은 창 전체에서 받는다.
  const move = (event) => {
    if (!drag) return;
    // 창 밖에서 버튼을 놓으면 pointerup이 오지 않을 수 있다. 버튼이 떨어졌으면 끝낸다.
    if (event.buttons === 0) {
      end();
      return;
    }
    preferred = limit(drag.width + event.clientX - drag.x);
    show();
  };
  const end = () => {
    if (!drag) return;
    drag = null;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    handle.classList.remove("is-dragging");
    document.body.classList.remove("is-resizing");
    remember(storageKey, preferred);
  };
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    drag = { x: event.clientX, width: limit(preferred) };
    try { handle.setPointerCapture(event.pointerId); } catch { /* 창 전체 수신으로 충분하다 */ }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    handle.classList.add("is-dragging");
    document.body.classList.add("is-resizing");
  });
  handle.addEventListener("dblclick", () => choose(fallback));
  handle.addEventListener("keydown", (event) => {
    const step = { ArrowLeft: -KEY_STEP, ArrowRight: KEY_STEP }[event.key];
    if (!step) return;
    event.preventDefault();
    choose(limit(preferred) + step);
  });
  window.addEventListener("resize", show);
}
