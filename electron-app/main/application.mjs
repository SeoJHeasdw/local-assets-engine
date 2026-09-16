import { createEngineService, DEFAULT_PORT, shouldStopEngine } from "./engine.mjs";
import { createIpcService } from "./ipc.mjs";
import { ENGINE_PYTHON, ROOT } from "./paths.mjs";
import { createWindowService } from "./window.mjs";

// Electron 객체는 여기서만 들어온다. 서비스는 Node 테스트에서도 그대로 돌 수 있다.
export function createStudio({ app, BrowserWindow, dialog, ipcMain, shell, session = null, engine = null }) {
  const state = { mainWindow: null, engineUrl: null, engineOwned: false, outputDir: null, busyJob: null };
  let healthTimer = null;
  const engineService = engine || createEngineService({
    python: ENGINE_PYTHON,
    root: ROOT,
    port: Number(process.env.LOCAL_ASSETS_PORT) || DEFAULT_PORT,
    log: (text) => process.stdout.write(`[engine] ${text}`),
  });
  const { createWindow, showStartup, showStudio } = createWindowService({ BrowserWindow, app, state, session });

  let connecting = null;
  function connect() {
    connecting ||= (async () => {
      await showStartup();
      try {
        const { url, owned, health } = await engineService.start();
        Object.assign(state, { engineUrl: url, engineOwned: owned, outputDir: health.outputDir });
        watchEngine();
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

  // 종료 시점에는 비동기 검사를 할 수 없다. 진행 중인 작업을 미리 알아 둔다.
  function watchEngine() {
    clearInterval(healthTimer);
    healthTimer = setInterval(async () => {
      state.busyJob = (await engineService.health())?.currentJob || null;
    }, 5000);
    healthTimer.unref?.();
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
    clearInterval(healthTimer);
    if (shouldStopEngine({ owned: state.engineOwned, busyJob: state.busyJob })) engineService.stop();
    else if (state.busyJob) process.stdout.write(`[engine] 작업 ${state.busyJob} 이(가) 진행 중이라 엔진을 남겨 둡니다.\n`);
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
