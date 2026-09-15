import { spawn as nativeSpawn } from "node:child_process";

export const DEFAULT_PORT = 47831;

// 엔진은 앱보다 먼저 떠 있을 수 있다(javis나 CLI가 띄운 경우). 이미 응답하는 엔진은
// 그대로 쓰고, 이 앱이 직접 띄운 엔진만 앱과 함께 끈다.
export function createEngineService({
  python,
  root,
  port = DEFAULT_PORT,
  spawn = nativeSpawn,
  fetchImpl = globalThis.fetch,
  env = process.env,
  startTimeoutMs = 30_000,
  pollMs = 250,
  log = () => {},
}) {
  const url = `http://127.0.0.1:${port}`;
  let child = null;
  let exit = null;

  async function health() {
    try {
      const response = await fetchImpl(`${url}/api/health`, { signal: AbortSignal.timeout(1500) });
      if (!response.ok) return null;
      const body = await response.json();
      return body?.ok === true ? body : null;
    } catch {
      return null;
    }
  }

  async function start() {
    const existing = await health();
    if (existing) return { url, owned: false, health: existing };
    if (child) stop();
    exit = null;
    child = spawn(python, ["-m", "local_assets_engine", "serve", "--port", String(port)], {
      cwd: root,
      env: { ...env, PYTHONUNBUFFERED: "1" },
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const current = child;
    current.stdout?.on("data", (chunk) => log(chunk.toString()));
    current.stderr?.on("data", (chunk) => log(chunk.toString()));
    current.once("error", (error) => { exit = { detail: error.message }; if (child === current) child = null; });
    current.once("exit", (code, signal) => { exit = { detail: `종료 ${signal || code}` }; if (child === current) child = null; });

    const deadline = Date.now() + startTimeoutMs;
    while (Date.now() < deadline) {
      if (exit) throw new Error(`엔진이 시작하자마자 멈췄습니다. (${exit.detail})`);
      const ready = await health();
      if (ready) return { url, owned: true, health: ready };
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
    stop();
    throw new Error(`엔진이 ${Math.round(startTimeoutMs / 1000)}초 안에 응답하지 않았습니다.`);
  }

  function stop() {
    const current = child;
    if (!current?.pid) return false;
    child = null;
    try {
      process.kill(-current.pid, "SIGTERM");
    } catch {
      try { current.kill("SIGTERM"); } catch { /* already gone */ }
    }
    return true;
  }

  return { url, start, stop, health };
}
