import path from "node:path";

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

// 화면이 보낸 경로를 그대로 믿지 않는다. 엔진이 방금 분류한 목록에서 '중간 파일'인 것만 고른다.
export function pickIntermediate(storage, requested) {
  if (!storage || storage.active) throw new Error("진행 중인 작업의 파일은 정리할 수 없습니다.");
  const allowed = new Map((storage.files || []).filter((file) => file.category === "intermediate").map((file) => [file.path, file.bytes]));
  const picked = [...new Set(Array.isArray(requested) ? requested : [])].filter((relative) => allowed.has(relative));
  return { paths: picked, bytes: picked.reduce((sum, relative) => sum + allowed.get(relative), 0) };
}

export function createIpcService({ ipcMain, dialog, shell, state, reconnect, app = null, fetchImpl = globalThis.fetch }) {
  async function jobStorage(jobId) {
    if (!state.engineUrl) throw new Error("엔진에 연결되지 않았습니다.");
    const response = await fetchImpl(`${state.engineUrl}/api/jobs/${encodeURIComponent(jobId)}/storage`);
    if (!response.ok) throw new Error("작업을 찾을 수 없습니다.");
    return response.json();
  }

  function registerIpc() {
    ipcMain.handle("assets:engine-info", () => ({
      url: state.engineUrl, owned: state.engineOwned, outputDir: state.outputDir,
    }));
    ipcMain.handle("assets:retry-engine", () => reconnect());
    ipcMain.handle("assets:pick-image", async () => {
      const result = await dialog.showOpenDialog(state.mainWindow, {
        title: "3D로 만들 이미지",
        properties: ["openFile"],
        filters: [{ name: "이미지", extensions: ["png", "jpg", "jpeg", "webp"] }],
      });
      return result.canceled ? null : result.filePaths[0] ?? null;
    });
    ipcMain.handle("assets:reveal", (_event, jobId, relative) => {
      const target = resolveJobFile(state.outputDir, jobId, relative);
      if (!target) return false;
      shell.showItemInFolder(target);
      return true;
    });
    ipcMain.handle("assets:open-blend", async (_event, jobId, relative) => {
      const target = resolveBlendFile(state.outputDir, jobId, relative);
      if (!target) return false;
      return (await shell.openPath(target)) === "";
    });
    // 지우지 않고 macOS 휴지통으로 보낸다. 사람이 Finder에서 되살릴 수 있다.
    ipcMain.handle("assets:trash-intermediate", async (_event, jobId, relatives) => {
      const { paths, bytes } = pickIntermediate(await jobStorage(jobId), relatives);
      let trashed = 0;
      for (const relative of paths) {
        const target = resolveJobFile(state.outputDir, jobId, relative);
        if (!target) continue;
        await shell.trashItem(target);
        trashed += 1;
      }
      return { trashed, bytes };
    });
    ipcMain.handle("assets:trash-job", async (_event, jobId) => {
      const storage = await jobStorage(jobId);
      if (storage.active) throw new Error("진행 중인 작업은 휴지통으로 보낼 수 없습니다.");
      const target = resolveJobDir(state.outputDir, jobId);
      if (!target) return false;
      await shell.trashItem(target);
      return true;
    });
    // 알림을 눌렀을 때 다른 앱 뒤에 있던 창을 앞으로 가져온다.
    ipcMain.handle("assets:focus", () => {
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
