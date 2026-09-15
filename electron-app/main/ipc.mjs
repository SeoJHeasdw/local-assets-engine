import path from "node:path";

const JOB_ID = /^\d{8}-\d{6}-[0-9a-f]{4}$/;

export function resolveJobFile(outputDir, jobId, relative) {
  if (!outputDir || !JOB_ID.test(String(jobId))) return null;
  const base = path.resolve(outputDir, "jobs", String(jobId));
  const target = path.resolve(base, String(relative || ""));
  return target.startsWith(base + path.sep) ? target : null;
}

export function createIpcService({ ipcMain, dialog, shell, state, reconnect }) {
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
  }

  return { registerIpc };
}
