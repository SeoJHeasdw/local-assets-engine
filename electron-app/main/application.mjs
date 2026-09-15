import { createEngineService, DEFAULT_PORT } from "./engine.mjs";
import { createIpcService } from "./ipc.mjs";
import { ENGINE_PYTHON, ROOT } from "./paths.mjs";
import { createWindowService } from "./window.mjs";

// Electron 객체는 여기서만 들어온다. 서비스는 Node 테스트에서도 그대로 돌 수 있다.
export function createStudio({ app, BrowserWindow, dialog, ipcMain, shell, engine = null }) {
  const state = { mainWindow: null, engineUrl: null, engineOwned: false, outputDir: null };
  const engineService = engine || createEngineService({
    python: ENGINE_PYTHON,
    root: ROOT,
    port: Number(process.env.LOCAL_ASSETS_PORT) || DEFAULT_PORT,
    log: (text) => process.stdout.write(`[engine] ${text}`),
  });
  const { createWindow, showStartup, showStudio } = createWindowService({ BrowserWindow, app, state });

  let connecting = null;
  function connect() {
    connecting ||= (async () => {
      await showStartup();
      try {
        const { url, owned, health } = await engineService.start();
        Object.assign(state, { engineUrl: url, engineOwned: owned, outputDir: health.outputDir });
        await showStudio(url);
        return { ok: true };
      } catch (error) {
        await showStartup(error.message);
        return { ok: false, message: error.message };
      } finally {
        connecting = null;
      }
    })();
    return connecting;
  }

  const { registerIpc } = createIpcService({ ipcMain, dialog, shell, state, reconnect: connect });

  function openWindow() {
    createWindow();
    return connect();
  }

  async function start() {
    registerIpc();
    await openWindow();
  }

  function stop() {
    if (state.engineOwned) engineService.stop();
  }

  return { start, stop, openWindow, state };
}

export function startStudio(electron) {
  const studio = createStudio(electron);
  const { app, BrowserWindow } = electron;
  app.whenReady().then(async () => {
    await studio.start();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) studio.openWindow();
    });
  });
  app.on("before-quit", studio.stop);
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  return studio;
}
