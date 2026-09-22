import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { planDescription, meshPlacement, SCENE_STARTERS } from "../../electron-app/shared/previz-start.mjs";
import { buildPrevizRequest, cameraAt, sceneSubjects, totalSeconds } from "../../electron-app/shared/previz.mjs";

const config = JSON.parse(fs.readFileSync(new URL("../../config/presets.json", import.meta.url))).previz;
const preset = config.shotPresets[0];
const plan = (text) => planDescription(text, config.standins, preset);

test("a scene description produces two facing people, a wall behind and an eight-second camera move", () => {
  const text = "두 사람이 마주 보고 서 있다. 뒤에는 벽이 있고, 카메라가 천천히 다가간다. 전체 8초.";
  const draft = plan(text);
  assert.deepEqual(draft.placed.map((item) => item.standin), ["person", "person", "wall"]);
  const [a, b, wall] = draft.placed;
  assert.ok(wall.y > a.y && wall.y > b.y);
  assert.equal(a.yaw, 90);
  assert.equal(b.yaw, -90);
  assert.equal(totalSeconds(draft.cuts), 8);
  const subjects = sceneSubjects(draft.placed);
  const start = cameraAt(draft.cuts[0], subjects, 0);
  const end = cameraAt(draft.cuts[0], subjects, 1);
  assert.ok(Math.hypot(...end.position.slice(0, 2)) < Math.hypot(...start.position.slice(0, 2)));
  const request = buildPrevizRequest({ ...draft, preset, description: text, settings: config.defaults });
  assert.equal(request.params.description, text);
  assert.equal(request.params.assets.length, 3);
  assert.equal(request.params.shots[0].seconds, 8);
  assert.equal(request.params.renderer, config.defaults.renderer);
});

test("every example is usable without extra assets and leaves the shared catalogue unchanged", () => {
  const before = JSON.stringify(config);
  for (const example of SCENE_STARTERS) {
    const draft = plan(example.text);
    assert.ok(draft.placed.length > 0 && draft.cuts.length > 0);
    buildPrevizRequest({ ...draft, preset });
    draft.placed[0].dimensions[0] = 999;
  }
  assert.equal(JSON.stringify(config), before);
});

test("additional explanation changes placement and quantity; unsupported acting is called out", () => {
  const draft = plan("사람 한 명. 사람은 세 명이 있고 오른쪽에 자동차가 있다. 사람들은 걸어간다. 카메라는 고정.");
  assert.equal(draft.placed.filter((item) => item.standin === "person").length, 3);
  assert.ok(draft.placed.find((item) => item.standin === "car").x > draft.placed[0].x);
  assert.equal(draft.cuts[0].move, "static");
  assert.ok(draft.notes.some((note) => note.includes("제자리에")));
  assert.equal(plan("두 명의 사람이 서 있다.").placed.length, 2);
});

test("unsupported or ambiguous requests do not silently become a sample scene", () => {
  for (const text of ["", "용이 하늘을 난다", "사람 0명", "사람 아홉 명", "사람 없이 자동차", "사람. 3초 후 5초", "사람. 1000초"]) {
    assert.throws(() => plan(text), Error, text);
  }
});

test("a requested total duration is preserved when spread across the preset cuts", () => {
  const draft = plan("사람 한 명. 전체 8초.");
  assert.equal(draft.cuts.length, preset.shots.length);
  assert.ok(Math.abs(totalSeconds(draft.cuts) - 8) < 1e-9);
  assert.ok(draft.cuts.every((cut) => cut.seconds >= 0.2 && cut.seconds <= 60));
});

test("a saved mesh can replace a stand-in without carrying a stand-in identity or losing its location", () => {
  const previous = plan("사람 오른쪽에 자동차").placed[1];
  const mesh = { jobId: "job", id: "a01", jobTitle: "3D 소품 · 상자", meta: { stats: { dimensionsMeters: [1, 2, 3] } } };
  const replaced = meshPlacement(mesh, previous);
  assert.equal(replaced.standin, undefined);
  assert.equal(replaced.x, previous.x);
  assert.deepEqual(buildPrevizRequest({ placed: [replaced], cuts: preset.shots, preset }).params.assets[0].source,
    { jobId: "job", assetId: "a01" });
});
