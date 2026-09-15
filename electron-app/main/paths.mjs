import path from "node:path";
import { fileURLToPath } from "node:url";

export const APP_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
export const ROOT = path.dirname(APP_DIR);
export const ENGINE_PYTHON = path.join(ROOT, ".venv", "bin", "python");
export const STARTUP_PAGE = path.join(APP_DIR, "startup.html");
export const PRELOAD = path.join(APP_DIR, "preload.cjs");
