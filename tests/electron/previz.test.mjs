import assert from "node:assert/strict";
import test from "node:test";

import {
  aimFromPoint, buildPrevizRequest, cameraAt, draftFromPreset, fitDistance, moveItem, nextShotId,
  sceneSubjects, setSeconds, totalSeconds,
} from "../../electron-app/shared/previz.mjs";

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

test("the request is refused before it reaches the engine when the scene is incomplete", () => {
  const preset = { id: "game-trailer", shots: [orbit] };
  assert.throws(() => buildPrevizRequest({ placed: [], cuts: [orbit], preset }), /에셋/);
  assert.throws(() => buildPrevizRequest({ placed: [chest], cuts: [], preset }), /컷/);
  assert.throws(() => buildPrevizRequest({ placed: [{ ...chest, x: "여기" }], cuts: [orbit], preset }), /1번째 에셋의 x/);
  assert.throws(() => buildPrevizRequest({ placed: [{ ...chest, scale: 0 }], cuts: [orbit], preset }), /크기/);
});
