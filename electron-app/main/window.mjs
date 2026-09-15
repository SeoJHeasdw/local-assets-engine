import nativeFs from "node:fs/promises";
import { PRELOAD, STARTUP_PAGE } from "./paths.mjs";

export function createWindowService({ BrowserWindow, app, state, fs = nativeFs, env = process.env }) {
  function createWindow() {
    const window = new BrowserWindow({
      width: 1440,
      height: 900,
      minWidth: 1040,
      minHeight: 700,
      titleBarStyle: "hiddenInset",
      backgroundColor: "#0f1110",
      show: false,
      webPreferences: { preload: PRELOAD, contextIsolation: true, nodeIntegration: false, sandbox: true },
    });
    state.mainWindow = window;
    window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    window.webContents.on("will-navigate", (event, url) => {
      if (!state.engineUrl || !url.startsWith(`${state.engineUrl}/`)) event.preventDefault();
    });
    window.once("ready-to-show", () => window.show());
    window.on("closed", () => { if (state.mainWindow === window) state.mainWindow = null; });
    return window;
  }

  function showStartup(message = "") {
    return state.mainWindow?.loadFile(STARTUP_PAGE, { query: { message } });
  }

  function showStudio(url) {
    const window = state.mainWindow;
    if (!window) return undefined;
    const view = env.ASSETS_STUDIO_SCREENSHOT_VIEW;
    const screenshot = env.ASSETS_STUDIO_SCREENSHOT;
    if (screenshot) {
      window.webContents.once("did-finish-load", async () => {
        await new Promise((resolve) => setTimeout(resolve, 1800));
        const image = await window.webContents.capturePage();
        await fs.writeFile(screenshot, image.toPNG());
        app.quit();
      });
    }
    return window.loadURL(view ? `${url}/?view=${encodeURIComponent(view)}` : `${url}/`);
  }

  return { createWindow, showStartup, showStudio };
}
