import { draftFromPreset, footprint, freeSpot, setSeconds } from "./previz.mjs";

// 지원하는 표현을 기존 대역과 샷으로 풀어 초안만 만든다. 범용 언어 모델이나 새 생성 경로가 아니다.
export const SCENE_STARTERS = [
  { id: "person", label: "인물 소개", text: "사람 한 명이 서 있다. 카메라가 인물을 천천히 돌며 보여 준다." },
  { id: "pair", label: "두 사람", text: "두 사람이 마주 보고 서 있다. 카메라가 두 사람을 향해 천천히 다가간다." },
  { id: "car", label: "사람과 자동차", text: "사람 오른쪽에 자동차 한 대가 있다. 카메라는 장면 전체를 고정해서 보여 준다." },
];

const KINDS = [
  ["person", "사람|인물|남자|여자|남성|여성|캐릭터|마네킹"],
  ["car", "자동차|승용차|차량|차(?=가|는|를|\\s|$)"],
  ["wall", "벽"], ["building", "건물|빌딩"],
];
const NUMBERS = { 한: 1, 하나: 1, 두: 2, 둘: 2, 세: 3, 셋: 3, 네: 4, 넷: 4, 다섯: 5, 여섯: 6, 일곱: 7, 여덟: 8, 아홉: 9, 열: 10 };
const NUMBER = "(?:\\d+|여덟|일곱|여섯|다섯|아홉|하나|한|두|둘|세|셋|네|넷|열)";
const readNumber = (value) => NUMBERS[value] || Number(value);

// 매칭되지 않은 의미까지 이해했다고 표시하지 않는다. 반환한 요약만 실제 반영 내용이다.
export function planDescription(description, standins, preset) {
  const text = String(description || "").trim();
  if (!text) throw new Error("만들 장면을 한 문장으로 적어 주세요.");
  if (text.length > 2000) throw new Error("설명은 2,000자 안으로 적어 주세요.");
  const mentions = [];
  const counts = new Map();
  for (const [id, names] of KINDS) {
    const matches = [...text.matchAll(new RegExp(names, "g"))];
    if (!matches.length) continue;
    let count = 1;
    for (const match of matches) {
      mentions.push({ id, index: match.index, end: match.index + match[0].length });
      const before = text.slice(Math.max(0, match.index - 12), match.index).match(new RegExp(`(${NUMBER})\\s*(?:(?:명|대|개|채)의?\\s*)?$`));
      const after = text.slice(match.index + match[0].length).match(new RegExp(`^(?:은|는|이|가)?\\s*(${NUMBER})\\s*(?:명|대|개|채)?`));
      if (before || after) {
        count = readNumber((after || before)[1]);
        if (count < 1) throw new Error("대상 수는 1개 이상으로 적어 주세요. 제외할 대상은 설명에서 빼 주세요.");
      }
    }
    if (id === "person" && /남자|남성/.test(text) && /여자|여성/.test(text)) count = Math.max(count, 2);
    counts.set(id, count);
  }
  mentions.sort((a, b) => a.index - b.index);
  if (!mentions.length) throw new Error("배치할 대상을 찾지 못했습니다. 사람·자동차·벽·건물 중 하나를 설명에 넣어 주세요. 만든 3D 에셋은 장면 준비 후 주인공으로 고를 수 있습니다.");
  if ([...counts.values()].reduce((sum, count) => sum + count, 0) > 8) throw new Error("한 장면에는 최대 8개까지 놓을 수 있습니다. 대상 수를 줄여 주세요.");
  const placed = [];
  for (const id of new Set(mentions.map((item) => item.id))) {
    const kind = standins.find((item) => item.id === id);
    if (!kind) throw new Error(`${id} 대역이 준비되지 않았습니다.`);
    for (let index = 0; index < counts.get(id); index++) {
      const [x, y] = freeSpot(placed, kind.size);
      placed.push({ standin: id, label: kind.label, dimensions: [...kind.size], x, y, yaw: 0 });
    }
  }
  const summary = [];
  const notes = ["이 내용으로 초안을 만들었습니다. 장소·외모·의상·조명 등 나머지 설명은 메모로 남습니다. 준비 후 배치와 컷은 직접 고칠 수 있습니다."];
  if (/(없|빼|제외|말고|않|아니)/.test(text)) {
    throw new Error("빼거나 제외하는 표현은 아직 해석하지 못합니다. 장면에 놓을 대상과 원하는 카메라만 적어 주세요.");
  }
  // 문장 안의 ‘사람 오른쪽에 차’, ‘차는 사람 오른쪽’, ‘뒤에 벽’처럼 단순한 상대 배치.
  let offset = 0;
  for (const clause of text.split(/[.,\n。]/)) {
    const local = mentions.filter((item) => item.index >= offset && item.index < offset + clause.length);
    const direction = /(오른쪽|왼쪽|뒤쪽|앞쪽|뒤|앞|옆)/.exec(clause);
    if (direction && local.length && !/카메라/.test(clause)) {
      const at = offset + direction.index;
      const before = local.filter((item) => item.index < at);
      const after = local.find((item) => item.index > at);
      const anchorId = before.at(-1)?.id || placed[0].standin;
      const targetId = after?.id || before.find((item) => item.id !== anchorId)?.id;
      const anchor = placed.find((item) => item.standin === anchorId);
      const target = placed.find((item) => item.standin === targetId);
      if (anchor && target && anchor !== target) {
        const horizontal = /오른쪽|왼쪽|옆/.test(direction[0]);
        const axis = horizontal ? 0 : 1;
        const sign = /왼쪽|앞/.test(direction[0]) ? -1 : 1;
        const gap = (anchor.dimensions[axis] + target.dimensions[axis]) / 2 + 0.8;
        target.x = anchor.x + (horizontal ? sign * gap : 0);
        target.y = anchor.y + (horizontal ? 0 : sign * gap);
        summary.push(`${anchor.label} ${direction[0]}에 ${target.label} 배치`);
      } else notes.push(`‘${clause.trim()}’의 상대 위치는 적용하지 못했습니다. 배치 직접 바꾸기에서 확인해 주세요.`);
    }
    offset += clause.length + 1;
  }
  const people = placed.filter((item) => item.standin === "person");
  if (/마주/.test(text) && people.length === 2) {
    // -Y가 정면이므로 서로 향하는 회전을 위치에서 계산한다.
    const [a, b] = people;
    a.yaw = Math.atan2(b.x - a.x, -(b.y - a.y)) * 180 / Math.PI;
    b.yaw = Math.atan2(a.x - b.x, -(a.y - b.y)) * 180 / Math.PI;
    summary.push("두 사람이 서로 마주 봄");
  }
  const moves = [];
  const cameraClauses = text.split(/[.\n。]/).filter((clause) => /카메라|촬영|샷|컷/.test(clause));
  const movement = /돌며|돌아|돌면서|둘러|회전|다가|가까이|접근|멀어|물러|후퇴|고정|정지/g;
  for (const clause of cameraClauses) {
    for (const match of clause.matchAll(movement)) {
      const type = /돌|둘러|회전/.test(match[0]) ? "orbit" : /다가|가까이|접근/.test(match[0]) ? "dolly-in"
        : /멀어|물러|후퇴/.test(match[0]) ? "dolly-out" : "static";
      if (moves.at(-1)?.type !== type) moves.push({ type, slow: /천천히|느리게/.test(clause) });
    }
  }
  let cuts = draftFromPreset(preset);
  if (moves.length) {
    if (moves.length > 12) throw new Error("카메라 움직임을 12개 이내로 적어 주세요.");
    cuts = moves.map(({ type, slow }, index) => {
      const framing = { distance: 1.8, azimuth: -25, height: 0.7, targetHeight: 0.5 };
      if (type === "orbit") { framing.azimuth = -45; framing.azimuthEnd = 35; }
      if (type === "dolly-in") framing.distanceEnd = 1.2;
      if (type === "dolly-out") { framing.distance = 1.2; framing.distanceEnd = 2.4; }
      const label = { orbit: "둘러보기", "dolly-in": "다가가기", "dolly-out": "멀어지기", static: "고정해서 보기" }[type];
      return { id: `s${String(index + 1).padStart(2, "0")}`, label, purpose: `장면 전체 ${label}`, move: type,
        focus: "scene", lens: 35, seconds: slow ? 5 : 3, ease: "inout", framing };
    });
    summary.push(`카메라: ${cuts.map((cut) => cut.label).join(" → ")}`);
  } else {
    summary.push(`카메라 지정 없음: ${preset?.label || "기본"} ${cuts.length}컷 사용`);
    if (cameraClauses.length) notes.push("카메라 표현을 찾지 못해 기본 컷을 사용합니다. ‘천천히 다가간다’, ‘돌며 보여 준다’, ‘고정’으로 적어 주세요.");
  }
  const durations = [...text.matchAll(/(\d+(?:\.\d+)?)\s*초/g)];
  if (durations.length > 1) throw new Error("지금은 영상 전체 길이만 정할 수 있습니다. ‘전체 8초’처럼 길이를 한 번만 적고, 컷별 길이는 영상 순서에서 조절해 주세요.");
  const duration = durations[0];
  if (duration) {
    const seconds = Number(duration[1]);
    if (seconds < cuts.length * 0.2 || seconds > cuts.length * 60) throw new Error(`영상 길이는 ${cuts.length * 0.2}~${cuts.length * 60}초 안으로 적어 주세요.`);
    const total = cuts.reduce((sum, cut) => sum + cut.seconds, 0);
    const ticks = Math.round(seconds * 10);
    const lengths = cuts.map((cut) => Math.max(2, Math.min(600, Math.round(cut.seconds / total * ticks))));
    let remainder = ticks - lengths.reduce((sum, value) => sum + value, 0);
    while (remainder !== 0) {
      const index = lengths.findIndex((value) => remainder > 0 ? value < 600 : value > 2);
      if (index < 0) break;
      const delta = Math.sign(remainder);
      lengths[index] += delta;
      remainder -= delta;
    }
    cuts = cuts.map((cut, index) => setSeconds(cut, lengths[index] / 10));
    summary.push(`전체 길이 ${cuts.reduce((sum, cut) => sum + cut.seconds, 0).toFixed(1)}초`);
  }
  if (/걷|걸어|뛰|달리|달려|움직|표정|웃|춤|대화|말하|운전|주행/.test(text.replace(/카메라[^.\n。]*/g, ""))) {
    notes.push("걷기·말하기·표정·차량 주행은 아직 지원하지 않아 대상은 제자리에 서 있습니다. 카메라만 움직입니다.");
  }
  for (let a = 0; a < placed.length; a++) for (let b = a + 1; b < placed.length; b++) {
    const first = footprint(placed[a]); const second = footprint(placed[b]);
    if ([0, 1].every((axis) => first.low[axis] < second.high[axis] && first.high[axis] > second.low[axis])) {
      notes.push("겹치는 대역이 있습니다. 배치 직접 바꾸기에서 위치를 조절해 주세요.");
      a = placed.length; break;
    }
  }
  summary.unshift([...counts].map(([id, count]) => `${standins.find((kind) => kind.id === id).label} ${count}${id === "person" ? "명" : id === "car" ? "대" : "개"}`).join(" · "));
  return { placed, cuts, summary, notes };
}

export function meshPlacement(mesh, previous = {}) {
  return {
    jobId: mesh.jobId, assetId: mesh.id, label: String(mesh.jobTitle || "3D 에셋").replace(/^3D( 소품)? · /, ""),
    preview: mesh.preview, dimensions: [...(mesh.meta?.stats?.dimensionsMeters || [1, 1, 1])],
    x: previous.x || 0, y: previous.y || 0, yaw: previous.yaw || 0, scale: 1,
  };
}
