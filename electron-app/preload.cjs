const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("assetsStudio", {
  engineInfo: () => ipcRenderer.invoke("assets:engine-info"),
  retryEngine: () => ipcRenderer.invoke("assets:retry-engine"),
  pickImage: () => ipcRenderer.invoke("assets:pick-image"),
  reveal: (jobId, relative) => ipcRenderer.invoke("assets:reveal", jobId, relative),
  openBlend: (jobId, relative) => ipcRenderer.invoke("assets:open-blend", jobId, relative),
  trashIntermediate: (jobId, relatives) => ipcRenderer.invoke("assets:trash-intermediate", jobId, relatives),
  trashJob: (jobId) => ipcRenderer.invoke("assets:trash-job", jobId),
  focus: () => ipcRenderer.invoke("assets:focus"),
  // 끌어다 놓은 File의 실제 경로는 preload에서만 얻을 수 있다.
  pathForFile: (file) => {
    try { return webUtils.getPathForFile(file) || ""; } catch { return ""; }
  },
});
