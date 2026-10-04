import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { canonicalJobDir, canonicalJobFile, createIpcService } from "../../electron-app/main/ipc.mjs";

test("IPC uses canonical paths and never opens a blend alias of another file type", async () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "lae-ipc-"));
  const id = "20261004-000000-abcd", dir = path.join(out, "jobs", id);
  fs.mkdirSync(dir, { recursive: true });
  const outside = path.join(out, "outside.txt"); fs.writeFileSync(outside, "synthetic");
  fs.writeFileSync(path.join(dir, "payload.command"), "synthetic");
  fs.symlinkSync("payload.command", path.join(dir, "alias.blend"));
  fs.symlinkSync(outside, path.join(dir, "outside.blend"));
  fs.writeFileSync(path.join(dir, "scene.blend"), "synthetic");
  const handlers = new Map(), opened = [];
  const frame = { url: "http://127.0.0.1:47831/" }, sender = { mainFrame: frame };
  const event = { sender, senderFrame: frame };
  createIpcService({ ipcMain: { handle: (name, fn) => handlers.set(name, fn) }, dialog: {},
    shell: { openPath: async p => { opened.push(p); return ""; } },
    state: { outputDir: out, engineUrl: "http://127.0.0.1:47831", mainWindow: { webContents: sender } }, reconnect() {} }).registerIpc();
  try {
    assert.equal(canonicalJobFile(out, id, "outside.blend"), null);
    const open = handlers.get("assets:open-blend");
    assert.equal(await open(event, id, "alias.blend"), false);
    assert.equal(await open(event, id, "outside.blend"), false);
    assert.equal(await open(event, id, "scene.blend"), true);
    assert.deepEqual(opened, [fs.realpathSync(path.join(dir, "scene.blend"))]);
    assert.throws(() => open({ sender: {}, senderFrame: frame }, id, "scene.blend"), /허용된 창/);
    frame.url = "http://127.0.0.1:47831/files/synthetic.html";
    assert.throws(() => open(event, id, "scene.blend"), /허용된 화면/);
    const alias = "20261004-000001-abcd";
    fs.symlinkSync(dir, path.join(out, "jobs", alias));
    assert.equal(canonicalJobDir(out, alias), null);
  } finally { fs.rmSync(out, { recursive: true, force: true }); }
});

test("Trash acquires a server reservation and releases it on success and failure", async () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "lae-trash-"));
  const id = "20261004-000000-abcd", dir = path.join(out, "jobs", id);
  fs.mkdirSync(dir, { recursive: true });
  const handlers = new Map(), calls = [], frame = { url: "http://127.0.0.1:47831/" };
  const sender = { mainFrame: frame }, event = { sender, senderFrame: frame };
  let fail = false;
  createIpcService({ ipcMain: { handle: (n, f) => handlers.set(n, f) }, dialog: {}, reconnect() {},
    shell: { trashItem: async () => { calls.push("trash"); if (fail) throw Error("mock failure"); } },
    state: { outputDir: out, engineUrl: "http://127.0.0.1:47831", mainWindow: { webContents: sender } },
    fetchImpl: async (url, request) => { const body = JSON.parse(request.body); calls.push({ url, body });
      return { ok: true, json: async () => ({ token: body.token, ok: true }) }; },
  }).registerIpc();
  try {
    const trash = handlers.get("assets:trash-job");
    assert.equal(await trash(event, id), true);
    assert.equal(calls[0].body.ownerPid, process.pid);
    assert.match(calls[0].body.token, /^[0-9a-f]{32}$/);
    assert.equal(calls[2].body.token, calls[0].body.token);
    assert.equal(calls[1], "trash"); assert.equal(calls[2].body.failed, false);
    fail = true; calls.length = 0;
    await assert.rejects(trash(event, id), /mock failure/);
    assert.equal(calls[2].body.failed, true);
  } finally { fs.rmSync(out, { recursive: true, force: true }); }
});

test("a failed reservation release survives until reconnect and then recovers", async () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "lae-trash-retry-"));
  const id = "20261004-000000-abcd", dir = path.join(out, "jobs", id);
  fs.mkdirSync(dir, { recursive: true });
  const handlers = new Map(), frame = { url: "http://127.0.0.1:47831/" }, sender = { mainFrame: frame };
  const event = { sender, senderFrame: frame };
  let offline = true, reserved = false, releases = 0;
  createIpcService({ ipcMain: { handle: (n, f) => handlers.set(n, f) }, dialog: {},
    shell: { trashItem: async () => {} },
    state: { outputDir: out, engineUrl: "http://127.0.0.1:47831", mainWindow: { webContents: sender } },
    reconnect: async () => { offline = false; return "connected"; },
    fetchImpl: async (url, request) => {
      if (url.endsWith("/reserve")) reserved = true;
      else { releases += 1; if (offline) throw Error("injected connection reset"); reserved = false; }
      return { ok: true, json: async () => ({ token: JSON.parse(request.body).token, ok: true }) };
    },
  }).registerIpc();
  try {
    await assert.rejects(handlers.get("assets:trash-job")(event, id), /connection reset/);
    assert.equal(reserved, true); assert.equal(releases, 3);
    assert.equal(await handlers.get("assets:retry-engine")(event), "connected");
    assert.equal(reserved, false); assert.equal(releases, 4);
  } finally { fs.rmSync(out, { recursive: true, force: true }); }
});

test("lost reserve responses reuse one token and always close the unknown request", async () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "lae-trash-reserve-"));
  const id = "20261004-000000-abcd";
  fs.mkdirSync(path.join(out, "jobs", id), { recursive: true });
  try {
    for (const loseEveryResponse of [false, true]) {
      const handlers = new Map(), tokens = [], finishes = [];
      const frame = { url: "http://127.0.0.1:47831/" }, sender = { mainFrame: frame };
      let moves = 0, reserved = false;
      createIpcService({ ipcMain: { handle: (n, f) => handlers.set(n, f) }, dialog: {}, reconnect() {},
        shell: { trashItem: async () => { moves += 1; } },
        state: { outputDir: out, engineUrl: "http://127.0.0.1:47831", mainWindow: { webContents: sender } },
        fetchImpl: async (url, request) => {
          const body = JSON.parse(request.body);
          if (url.endsWith("/reserve")) {
            tokens.push(body.token); reserved = true;
            if (loseEveryResponse || tokens.length === 1) throw Error("lost reserve response");
          } else { finishes.push(body); reserved = false; }
          return { ok: true, json: async () => ({ token: body.token, ok: true }) };
        },
      }).registerIpc();
      const request = handlers.get("assets:trash-job")({ sender, senderFrame: frame }, id);
      if (loseEveryResponse) await assert.rejects(request, /lost reserve response/);
      else assert.equal(await request, true);
      assert.equal(new Set(tokens).size, 1);
      assert.equal(tokens.length, loseEveryResponse ? 3 : 2);
      assert.equal(moves, loseEveryResponse ? 0 : 1);
      assert.equal(reserved, false);
      assert.deepEqual(finishes, [{ token: tokens[0], failed: loseEveryResponse }]);
    }
  } finally { fs.rmSync(out, { recursive: true, force: true }); }
});
