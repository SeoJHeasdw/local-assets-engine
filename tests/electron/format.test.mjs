import assert from "node:assert/strict";
import test from "node:test";

import {
  buildJobRequest, buildPrevizRequest, currentStage, escapeHtml, fileUrl, formatBytes, formatDuration,
} from "../../electron-app/shared/format.mjs";

test("2D request carries preset, trimmed subject and count", () => {
  assert.deepEqual(
    buildJobRequest({ kind: "2d", preset: "item-icon", subject: "  검 ", style: "", count: "4", seed: "" }),
    { recipe: "image", params: { preset: "item-icon", subject: "검", style: "", count: 4 } },
  );
});

test("3D from text goes straight to a mesh only with a single candidate", () => {
  const form = {
    kind: "3d", source: "text", preset: "prop-3d", subject: "상자", count: "1", seed: "7",
    pipelineType: "1024", textureSize: "2048", targetFaces: "0",
  };
  assert.deepEqual(buildJobRequest(form), {
    recipe: "text-to-3d",
    params: {
      preset: "prop-3d", subject: "상자", style: "", seed: 7, count: 1,
      pipelineType: "1024", textureSize: 2048, targetFaces: 0,
    },
  });
  assert.equal(buildJobRequest({ ...form, count: "4" }).recipe, "image");
});

test("3D from an image needs a path and uses the seed for the mesh", () => {
  assert.throws(() => buildJobRequest({ kind: "3d", source: "image" }), /이미지/);
  assert.deepEqual(
    buildJobRequest({
      kind: "3d", source: "image", imagePath: "/tmp/a.png", seed: "3",
      pipelineType: "512", textureSize: "1024", targetFaces: "30000",
    }),
    { recipe: "image-to-3d", params: { imagePath: "/tmp/a.png", pipelineType: "512", textureSize: 1024, targetFaces: 30000, meshSeed: 3 } },
  );
});

test("previz request carries the scene placement in meters and degrees", () => {
  const request = buildPrevizRequest({
    assets: [
      { jobId: "20260916-1", assetId: "a02", x: "0", y: "0", yaw: "0", scale: "1" },
      { jobId: "20260916-2", assetId: "a01", x: "1.6", y: " 0.5 ", yaw: "35", scale: "0.8" },
    ],
    preset: "game-trailer", renderer: "eevee", resolution: "960x540", fps: "12",
    aux: "keys", clay: true, animatic: true,
  });
  assert.equal(request.recipe, "previz");
  assert.deepEqual(request.params.assets[1], {
    source: { jobId: "20260916-2", assetId: "a01" }, position: [1.6, 0.5, 0], yaw: 35, scale: 0.8,
  });
  assert.equal(request.params.width, 960);
  assert.equal(request.params.height, 540);
  assert.equal(request.params.fps, 12);
});

test("previz needs a scene and rejects values the engine would refuse", () => {
  const scene = { assets: [{ jobId: "j", assetId: "a01", x: "0", y: "0", yaw: "0", scale: "1" }] };
  assert.throws(() => buildPrevizRequest({ assets: [] }), /에셋/);
  assert.throws(() => buildPrevizRequest({ assets: [{ ...scene.assets[0], x: "여기" }] }), /1번째 에셋의 x/);
  assert.throws(() => buildPrevizRequest({ assets: [{ ...scene.assets[0], yaw: "400" }] }), /회전/);
  assert.throws(() => buildPrevizRequest({ assets: [{ ...scene.assets[0], scale: "0" }] }), /크기/);
});

test("invalid input is rejected before it reaches the engine", () => {
  assert.throws(() => buildJobRequest({ kind: "2d", subject: " " }), /무엇을/);
  assert.throws(() => buildJobRequest({ kind: "2d", subject: "a", seed: "-1" }), /시드/);
});

test("formatting helpers", () => {
  assert.equal(formatBytes(0), "–");
  assert.equal(formatBytes(1536), "1.5KB");
  assert.equal(formatBytes(20 * 2 ** 30), "20.0GB");
  assert.equal(formatDuration(4.26), "4.3초");
  assert.equal(formatDuration(75), "1분 15초");
  assert.equal(formatDuration(3720), "1시간 02분");
  assert.equal(escapeHtml(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
  assert.equal(fileUrl("20260916-010203-abcd", "final/seed 1.png"), "/files/20260916-010203-abcd/final/seed%201.png");
  assert.equal(currentStage({ stages: [{ name: "a", state: "done" }, { name: "b", state: "running" }] }).name, "b");
});
