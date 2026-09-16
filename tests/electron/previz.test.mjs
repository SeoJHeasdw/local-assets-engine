import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

import {
  aimFromPoint, buildPrevizRequest, cameraAt, draftFromPreset, fitDistance, freeSpot, moveItem, nextShotId,
  refocusCuts, sceneSubjects, setSeconds, totalSeconds,
} from "../../electron-app/shared/previz.mjs";

const presets = JSON.parse(fs.readFileSync(new URL("../../config/presets.json", import.meta.url), "utf8"));
const standin = (id, extra) => ({ standin: id, dimensions: presets.previz.standins.find((kind) => kind.id === id).size, yaw: 0, ...extra });
const trailerShot = (id) => presets.previz.shotPresets[0].shots.find((shot) => shot.id === id);

const chest = { jobId: "20260916-144943-2668", assetId: "a02", x: 0, y: 0, yaw: 0, scale: 1, dimensions: [0.916, 0.9864, 1.0] };
const orbit = {
  id: "s02", focus: "hero", lens: 50, seconds: 3,
  framing: { distance: 1.7, azimuth: -55, azimuthEnd: 5, height: 0.8, targetHeight: 0.55 },
};
const crane = {
  id: "s05", focus: "hero", lens: 35, seconds: 2.5,
  framing: { distance: 2.2, distanceEnd: 1.9, azimuth: -20, azimuthEnd: -5, height: 2.4, heightEnd: 0.55, targetHeight: 0.55 },
};

const near = (actual, expected, tolerance = 2e-3) =>
  expected.forEach((value, index) => assert.ok(Math.abs(actual[index] - value) < tolerance, `${actual} ≈ ${expected}`));

test("the map puts cameras where the engine rendered them", () => {
  // 기대값은 같은 에셋·프리셋으로 엔진이 실제 기록한 shots.json의 값이다.
  const subjects = sceneSubjects([chest]);
  near(cameraAt(orbit, subjects, 0).position, [-3.4384, -2.4076, 0.8]);
  near(cameraAt(orbit, subjects, 1).position, [0.3658, -4.1816, 0.8]);
  near(cameraAt(crane, subjects, 0).position, [-1.3005, -3.5732, 2.4]);
  near(cameraAt(crane, subjects, 1).position, [-0.2862, -3.2715, 0.55]);
  near(cameraAt(orbit, subjects, 0).target, [0, 0, 0.55]);
});

test("stand-ins put cameras where the engine rendered them", () => {
  // 기대값은 이 배치(사람 30°·차·늘린 벽·상자 GLB·건물 15°)로 엔진이 실제 기록한 shots.json의 값이다.
  // 카탈로그 치수를 설정 파일에서 읽으므로 부품과 size가 어긋나도 여기서 드러난다.
  const subjects = sceneSubjects([
    standin("person", { x: 0, y: 0, yaw: 30 }),
    standin("car", { x: 2.5, y: 1, yaw: -90 }),
    { ...standin("wall", { x: 0, y: 3 }), dimensions: [8, 0.2, 3] },
    { ...chest, x: -1.5, y: 0.5, dimensions: [0.914, 0.9842, 1.0] },
    standin("building", { x: 6, y: 12, yaw: 15 }),
  ]);
  near([subjects.hero.width, subjects.hero.height], [0.6213, 1.75]);
  near([subjects.asset5.width, subjects.scene.width, ...subjects.scene.center], [12.2474, 18.3868, 4.0619, 8.9303]);
  near(cameraAt(trailerShot("s02"), subjects, 0).position, [-6.0172, -4.2133, 1.4]);
  near(cameraAt(trailerShot("s02"), subjects, 1).position, [0.6402, -7.3177, 1.4]);
  near(cameraAt(trailerShot("s05"), subjects, 1).position, [-0.5009, -5.725, 0.9625]);
  near(cameraAt(trailerShot("s06"), subjects, 1).position, [23.519, -44.5277, 10.2]);
});

test("distance 1.0 is the distance at which the subject fills the frame", () => {
  const subject = { width: 1, height: 1 };
  assert.ok(Math.abs(fitDistance(50, subject) - 50 * (16 / 9) / 36) < 1e-9);
  assert.ok(fitDistance(85, subject) > fitDistance(28, subject));
});

test("dragging a camera on the map round-trips to the same spot", () => {
  const subjects = sceneSubjects([chest]);
  const moved = aimFromPoint(orbit, subjects, [2, -3], "start");
  near(cameraAt(moved, subjects, 0).position.slice(0, 2), [2, -3], 0.2);
  const ended = aimFromPoint(orbit, subjects, [-1, -4], "end");
  assert.equal(ended.framing.azimuth, -55);
  near(cameraAt(ended, subjects, 1).position.slice(0, 2), [-1, -4], 0.2);
});

test("dragging past the back does not flip an orbit the long way round", () => {
  const subjects = sceneSubjects([chest]);
  const cut = { ...orbit, framing: { ...orbit.framing, azimuth: 170 } };
  // 뒤쪽(+Y 방향) 바로 옆으로 끌면 -170이 아니라 190에 가까운 값이어야 한다.
  const moved = aimFromPoint(cut, subjects, [-0.2, 3], "start");
  assert.ok(moved.framing.azimuth > 180 && moved.framing.azimuth < 200, String(moved.framing.azimuth));
});

test("the whole scene is a subject when cuts aim at it", () => {
  const subjects = sceneSubjects([chest, { ...chest, x: 3, y: 0 }]);
  assert.ok(subjects.scene.width > 3.5);
  assert.deepEqual(Object.keys(subjects).sort(), ["asset2", "hero", "scene"]);
});

test("cut list edits: reorder, new ids, seconds limits", () => {
  const cuts = [{ id: "s01" }, { id: "s02" }, { id: "s03" }];
  assert.deepEqual(moveItem(cuts, 0, 2).map((cut) => cut.id), ["s02", "s03", "s01"]);
  assert.equal(nextShotId([{ id: "s01" }, { id: "s03" }]), "s02");
  assert.equal(setSeconds({ seconds: 2 }, 0).seconds, 0.2);
  assert.equal(setSeconds({ seconds: 2 }, 2.26).seconds, 2.3);
  assert.equal(totalSeconds([{ seconds: 2.5 }, { seconds: 3 }]), 5.5);
});

test("an untouched preset is not sent as an edit", () => {
  const preset = { id: "game-trailer", shots: [orbit, crane] };
  const settings = { renderer: "eevee", resolution: "960x540", fps: "12", aux: "keys", clay: true, animatic: true };
  const plain = buildPrevizRequest({ placed: [chest], cuts: draftFromPreset(preset), preset, settings });
  assert.equal(plain.params.shots, undefined);
  assert.deepEqual(plain.params.assets[0], {
    id: "hero", source: { jobId: chest.jobId, assetId: "a02" }, position: [0, 0, 0], yaw: 0, scale: 1,
  });
  const cuts = moveItem(draftFromPreset(preset), 1, 0);
  const edited = buildPrevizRequest({ placed: [chest], cuts, preset, settings });
  assert.deepEqual(edited.params.shots.map((cut) => cut.id), ["s05", "s02"]);
  assert.equal(edited.params.width, 960);
});

test("a stand-in is sent as its size in meters, and a scene of stand-ins alone is enough", () => {
  const preset = { id: "game-trailer", shots: [orbit] };
  const wall = { ...standin("wall", { x: 1, y: 3, yaw: 90 }), dimensions: [12, 0.2, 3] };
  const request = buildPrevizRequest({ placed: [standin("person", { x: 0, y: 0 }), wall], cuts: [orbit], preset });
  assert.deepEqual(request.params.assets, [
    { id: "hero", standin: "person", size: [0.55, 0.29, 1.75], position: [0, 0, 0], yaw: 0 },
    { id: "asset2", standin: "wall", size: [12, 0.2, 3], position: [1, 3, 0], yaw: 90 },
  ]);
  assert.throws(() => buildPrevizRequest({ placed: [{ ...wall, dimensions: [12, 0, 3] }], cuts: [orbit], preset }), /1번째 대역의 깊이/);
});

test("cuts keep aiming at the same thing when the scene is reordered or something is taken out", () => {
  const cuts = [{ id: "s01", focus: "hero" }, { id: "s02", focus: "asset2" }, { id: "s03", focus: "asset3" }, { id: "s04", focus: "scene" }];
  // 셋 중 두 번째(asset2)를 뺀다: asset3은 asset2가 되고, 빠진 것을 보던 컷은 장면 전체를 본다.
  assert.deepEqual(refocusCuts(cuts, [0, 2]).map((cut) => cut.focus), ["hero", "scene", "asset2", "scene"]);
  // 세 번째를 주인공으로 올린다: 역할인 hero는 새 주인공을 보고, 물체를 가리키던 컷은 그 물체를 따라간다.
  assert.deepEqual(refocusCuts(cuts, [2, 0, 1]).map((cut) => cut.focus), ["hero", "asset3", "hero", "scene"]);
  assert.equal(refocusCuts(cuts, [0, 1, 2])[0], cuts[0]);
});

test("something placed without a drop point steps aside from what is already there", () => {
  assert.deepEqual(freeSpot([], [1, 1, 1]), [0, 0]);
  const spot = freeSpot([chest], [10, 10, 12]);
  // 상자(가로 0.916m) 오른쪽 끝에서 간격 0.3m를 두고 건물 왼쪽 끝이 시작한다.
  assert.ok(spot[0] - 5 >= 0.458 + 0.3 && spot[0] - 5 < 0.458 + 0.3 + 0.5, String(spot));
  assert.deepEqual(freeSpot([{ ...chest, y: 20 }], [1, 1, 1]), [0, 0]);
});

test("the request is refused before it reaches the engine when the scene is incomplete", () => {
  const preset = { id: "game-trailer", shots: [orbit] };
  assert.throws(() => buildPrevizRequest({ placed: [], cuts: [orbit], preset }), /에셋/);
  assert.throws(() => buildPrevizRequest({ placed: [chest], cuts: [], preset }), /컷/);
  assert.throws(() => buildPrevizRequest({ placed: [{ ...chest, x: "여기" }], cuts: [orbit], preset }), /1번째 에셋의 x/);
  assert.throws(() => buildPrevizRequest({ placed: [{ ...chest, scale: 0 }], cuts: [orbit], preset }), /크기/);
});
