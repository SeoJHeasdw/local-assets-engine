import path from "node:path";
import fs from "node:fs";
import { randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";
import { STARTUP_PAGE } from "./paths.mjs";

const JOB_ID = /^\d{8}-\d{6}-[0-9a-f]{4}$/;

export function resolveJobFile(outputDir, jobId, relative) {
  if (!outputDir || !JOB_ID.test(String(jobId))) return null;
  const base = path.resolve(outputDir, "jobs", String(jobId));
  const target = path.resolve(base, String(relative || ""));
  return target.startsWith(base + path.sep) ? target : null;
}

// 기본 앱으로 여는 통로는 작업 폴더 안의 Blender 장면으로만 좁힌다.
// 임의 파일을 열 수 있으면 화면이 실행 파일을 여는 통로가 된다.
export function resolveBlendFile(outputDir, jobId, relative) {
  const target = resolveJobFile(outputDir, jobId, relative);
  return target && path.extname(target) === ".blend" ? target : null;
}

export function resolveJobDir(outputDir, jobId) {
  if (!outputDir || !JOB_ID.test(String(jobId))) return null;
  return path.resolve(outputDir, "jobs", String(jobId));
}

export function canonicalJobDir(outputDir, jobId) {
  const directory = resolveJobDir(outputDir, jobId);
  if (!directory) return null;
  try {
    const root = fs.realpathSync(path.join(outputDir, "jobs"));
    if (fs.lstatSync(directory).isSymbolicLink()) return null;
    const resolved = fs.realpathSync(directory);
    return path.dirname(resolved) === root && path.basename(resolved) === String(jobId) ? resolved : null;
  } catch { return null; }
}

export function canonicalJobFile(outputDir, jobId, relative) {
  const lexical = resolveJobFile(outputDir, jobId, relative), directory = canonicalJobDir(outputDir, jobId);
  if (!lexical || !directory) return null;
  try {
    const target = fs.realpathSync(lexical);
    return target.startsWith(directory + path.sep) && fs.statSync(target).isFile() ? target : null;
  } catch { return null; }
}

// 화면이 보낸 경로를 그대로 믿지 않는다. 엔진이 방금 분류한 목록에서 '중간 파일'인 것만 고른다.
export function pickIntermediate(storage, requested) {
  if (!storage || storage.active) throw new Error("진행 중인 작업의 파일은 정리할 수 없습니다.");
  const allowed = new Map((storage.files || []).filter((file) => file.category === "intermediate").map((file) => [file.path, file.bytes]));
  const picked = [...new Set(Array.isArray(requested) ? requested : [])].filter((relative) => allowed.has(relative));
  return { paths: picked, bytes: picked.reduce((sum, relative) => sum + allowed.get(relative), 0) };
}

export function createIpcService({ ipcMain, dialog, shell, state, reconnect, app = null, fetchImpl = globalThis.fetch }) {
  const pendingFinishes = new Map();
  let finishTimer = null;
  function handle(channel, handler, startup = false) {
    ipcMain.handle(channel, (event, ...args) => {
      const sender = state.mainWindow?.webContents;
      if (!sender || event.sender !== sender || event.senderFrame !== sender.mainFrame) {
        throw new Error("허용된 창에서만 요청할 수 있습니다.");
      }
      const url = new URL(event.senderFrame.url);
      const studio = state.engineUrl && url.origin === new URL(state.engineUrl).origin
        && (url.pathname === "/" || url.pathname === "/index.html");
      const boot = startup && url.protocol === "file:"
        && url.pathname === new URL(pathToFileURL(STARTUP_PAGE)).pathname;
      if (!studio && !boot) throw new Error("허용된 화면에서만 요청할 수 있습니다.");
      return handler(event, ...args);
    });
  }

  async function trashApi(jobId, action, body, engineUrl = state.engineUrl) {
    const response = await fetchImpl(`${engineUrl}/api/jobs/${encodeURIComponent(jobId)}/trash/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      const error = new Error(detail?.detail || "엔진을 다시 시작한 뒤 정리해 주세요.");
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function retryLater() {
    if (finishTimer || !pendingFinishes.size) return;
    finishTimer = setTimeout(() => {
      finishTimer = null;
      drainFinishes().catch(() => {}).finally(retryLater);
    }, 2000);
    finishTimer.unref?.();
  }

  async function finishOne(entry, attempts = 1) {
    const key = `${entry.engineUrl}/${entry.jobId}/${entry.token}`;
    let failure;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        await trashApi(entry.jobId, "finish", { token: entry.token, failed: entry.failed }, entry.engineUrl);
        pendingFinishes.delete(key);
        return;
      } catch (error) {
        // These receipts mean this old reservation no longer exists. Never
        // release a replacement reservation using a stale token.
        if (error.status === 404 || error.status === 409) { pendingFinishes.delete(key); return; }
        failure = error;
        if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 100 * (attempt + 1)));
      }
    }
    retryLater();
    throw failure;
  }

  async function drainFinishes() {
    for (const entry of pendingFinishes.values()) await finishOne(entry);
  }

  async function reservedTrash(jobId, paths, action) {
    const engineUrl = state.engineUrl;
    const token = randomBytes(16).toString("hex");
    const body = { token, ownerPid: process.pid, ...(paths === null ? {} : { paths }) };
    const entry = { engineUrl, jobId, token, failed: true };
    let result, failure;
    try {
      for (let attempt = 0; ; attempt += 1) {
        try {
          const reserved = await trashApi(jobId, "reserve", body, engineUrl);
          if (reserved.token !== token) throw new Error("정리 예약 응답을 확인해 주세요.");
          break;
        } catch (error) {
          if (attempt === 2 || (error.status && error.status < 500)) throw error;
          await new Promise(resolve => setTimeout(resolve, 100 * (attempt + 1)));
        }
      }
      result = await action();
      entry.failed = false;
    } catch (error) { failure = error; }
    // Keep the token even when a reserve response was lost. Finishing this token
    // also closes any late reserve request before its result becomes visible.
    pendingFinishes.set(`${engineUrl}/${jobId}/${token}`, entry);
    try { await finishOne(entry, 3); }
    catch (error) { failure ||= error; }
    if (failure) throw failure;
    return result;
  }
  async function jobStorage(jobId) {
    if (!state.engineUrl) throw new Error("엔진에 연결되지 않았습니다.");
    const response = await fetchImpl(`${state.engineUrl}/api/jobs/${encodeURIComponent(jobId)}/storage`);
    if (!response.ok) throw new Error("작업을 찾을 수 없습니다.");
    return response.json();
  }

  function registerIpc() {
    handle("assets:engine-info", () => ({
      url: state.engineUrl, owned: state.engineOwned, outputDir: state.outputDir,
    }), true);
    handle("assets:retry-engine", async () => {
      const result = await reconnect();
      await drainFinishes().catch(() => {});
      return result;
    }, true);
    handle("assets:pick-image", async () => {
      const result = await dialog.showOpenDialog(state.mainWindow, {
        title: "3D로 만들 이미지",
        properties: ["openFile"],
        filters: [{ name: "이미지", extensions: ["png", "jpg", "jpeg", "webp"] }],
      });
      return result.canceled ? null : result.filePaths[0] ?? null;
    });
    handle("assets:reveal", (_event, jobId, relative) => {
      const target = canonicalJobFile(state.outputDir, jobId, relative);
      if (!target) return false;
      shell.showItemInFolder(target);
      return true;
    });
    handle("assets:open-blend", async (_event, jobId, relative) => {
      if (!resolveBlendFile(state.outputDir, jobId, relative)) return false;
      const target = canonicalJobFile(state.outputDir, jobId, relative);
      if (!target || path.extname(target) !== ".blend") return false;
      return (await shell.openPath(target)) === "";
    });
    // 지우지 않고 macOS 휴지통으로 보낸다. 사람이 Finder에서 되살릴 수 있다.
    handle("assets:trash-intermediate", async (_event, jobId, relatives) => {
      const { paths, bytes } = pickIntermediate(await jobStorage(jobId), relatives);
      if (!paths.length) return { trashed: 0, bytes: 0 };
      return reservedTrash(jobId, paths, async () => {
        let trashed = 0;
        for (const relative of paths) {
          const target = canonicalJobFile(state.outputDir, jobId, relative);
          if (!target) throw new Error("정리할 파일 경로를 확인해 주세요.");
          await shell.trashItem(target);
          trashed += 1;
        }
        return { trashed, bytes };
      });
    });
    handle("assets:trash-job", async (_event, jobId) => {
      return reservedTrash(jobId, null, async () => {
        const target = canonicalJobDir(state.outputDir, jobId);
        if (!target) throw new Error("정리할 작업 경로를 확인해 주세요.");
        await shell.trashItem(target);
        return true;
      });
    });
    // 알림을 눌렀을 때 다른 앱 뒤에 있던 창을 앞으로 가져온다.
    handle("assets:focus", () => {
      const window = state.mainWindow;
      if (!window) return false;
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
      app?.focus?.({ steal: true });
      return true;
    });
  }

  return { registerIpc };
}
