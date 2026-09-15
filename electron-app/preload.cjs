const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("assetsStudio", {
  engineInfo: () => ipcRenderer.invoke("assets:engine-info"),
  retryEngine: () => ipcRenderer.invoke("assets:retry-engine"),
  pickImage: () => ipcRenderer.invoke("assets:pick-image"),
  reveal: (jobId, relative) => ipcRenderer.invoke("assets:reveal", jobId, relative),
  // 끌어다 놓은 File의 실제 경로는 preload에서만 얻을 수 있다.
  pathForFile: (file) => {
    try { return webUtils.getPathForFile(file) || ""; } catch { return ""; }
  },
});
