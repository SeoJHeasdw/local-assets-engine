import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { createEngineService } from "../../electron-app/main/engine.mjs";
import { resolveJobFile } from "../../electron-app/main/ipc.mjs";

// pid_max보다 큰 값이라 실제 프로세스 그룹에 신호가 가지 않는다.
const IMPOSSIBLE_PID = 99_999_999;

function fetchSequence(sequence) {
  let calls = 0;
  return async () => {
    const next = sequence[Math.min(calls++, sequence.length - 1)];
    if (next instanceof Error) throw next;
    return { ok: true, json: async () => next };
  };
}

function fakeChild() {
  const child = new EventEmitter();
  child.pid = IMPOSSIBLE_PID;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killed = false;
  child.kill = () => { child.killed = true; return true; };
  return child;
}

test("reuses an engine that already answers without spawning", async () => {
  let spawned = 0;
  const engine = createEngineService({
    python: "py", root: "/r", spawn: () => { spawned += 1; },
    fetchImpl: fetchSequence([{ ok: true, outputDir: "/o" }]),
  });
  const result = await engine.start();
  assert.equal(result.owned, false);
  assert.equal(result.health.outputDir, "/o");
  assert.equal(spawned, 0);
  assert.equal(engine.stop(), false);
});

test("spawns the engine and waits until health answers", async () => {
  const calls = [];
  const engine = createEngineService({
    python: "/venv/python", root: "/root", port: 5000, pollMs: 1,
    spawn: (command, args, options) => { calls.push({ command, args, options }); return fakeChild(); },
    fetchImpl: fetchSequence([new Error("down"), new Error("down"), { ok: true, outputDir: "/out" }]),
  });
  const result = await engine.start();
  assert.equal(result.owned, true);
  assert.equal(result.url, "http://127.0.0.1:5000");
  assert.equal(calls[0].command, "/venv/python");
  assert.deepEqual(calls[0].args, ["-m", "local_assets_engine", "serve", "--port", "5000"]);
  assert.equal(calls[0].options.cwd, "/root");
  assert.equal(calls[0].options.detached, true);
});

test("reports an engine that exits during startup", async () => {
  const child = fakeChild();
  const engine = createEngineService({
    python: "py", root: "/r", pollMs: 1,
    spawn: () => { setTimeout(() => child.emit("exit", 1, null), 5); return child; },
    fetchImpl: fetchSequence([new Error("down")]),
  });
  await assert.rejects(engine.start(), /멈췄습니다/);
});

test("gives up after the start timeout and stops the child it spawned", async () => {
  const child = fakeChild();
  const engine = createEngineService({
    python: "py", root: "/r", pollMs: 1, startTimeoutMs: 30,
    spawn: () => child, fetchImpl: fetchSequence([new Error("down")]),
  });
  await assert.rejects(engine.start(), /응답하지 않았습니다/);
  assert.equal(child.killed, true);
});

test("revealed files stay inside the job folder", () => {
  assert.equal(
    resolveJobFile("/out", "20260916-010203-abcd", "mesh/asset.glb"),
    "/out/jobs/20260916-010203-abcd/mesh/asset.glb",
  );
  assert.equal(resolveJobFile("/out", "20260916-010203-abcd", "../../secret"), null);
  assert.equal(resolveJobFile("/out", "../x", "a.png"), null);
  assert.equal(resolveJobFile(null, "20260916-010203-abcd", "a.png"), null);
});
