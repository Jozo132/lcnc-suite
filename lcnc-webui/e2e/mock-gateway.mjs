// Minimal headless gateway for E2E (issue #26).
//
// Serves the built frontend AND a /ws that pushes a fixed "armed + ready"
// machine state, answering every hello/heartbeat with the same frame so the
// connection stays up and armed stays set. This lets Playwright verify the
// connected -> armed -> controls-enabled path in a real browser without a real
// LinuxCNC. (The disconnected path is covered by smoke.spec.ts; the per-class
// armed/busy overlay logic by permissions.test.ts.)
//
// A real server is used rather than page.routeWebSocket because the app owns its
// socket inside a Web Worker, which routeWebSocket intercepts only unreliably.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";

const DIST = fileURLToPath(new URL("../dist/", import.meta.url));
const PORT = Number(process.env.MOCK_PORT) || 4174;

const MIME = {
  ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon",
  ".woff2": "font/woff2", ".png": "image/png", ".wasm": "application/wasm",
};

const server = createServer(async (req, res) => {
  const path = decodeURIComponent((req.url || "/").split("?")[0]);
  const rel = path === "/" ? "index.html" : path.replace(/^\/+/, "");
  const file = normalize(join(DIST, rel));
  const send = async (f, code = 200) => {
    const buf = await readFile(f);
    res.writeHead(code, { "content-type": MIME[extname(f)] || "application/octet-stream" });
    res.end(buf);
  };
  if (!file.startsWith(DIST)) { res.writeHead(403); res.end(); return; }
  try {
    await send(file);
  } catch {
    try { await send(join(DIST, "index.html")); } catch { res.writeHead(404); res.end(); } // SPA fallback
  }
});

const READY = JSON.stringify({
  type: "status",
  armed: true, // lcncWs.ts: any frame with `armed` sets the client's armed state
  data: {
    estop: false, enabled: true, emc_enable_in: true, homed: true,
    is_estop: false, is_enabled: true,  // backend-merged truth (review #5)
    interp_state: 1, paused: false, eoffset_enabled: false,
    permissions: {
      idle: true, jog: true, override: true, ready: true, pause: false,
      resume: false, step: true, abort: true, probe: true, zero: true,
      safety: true, setup: true, armed: true, always: true,
    },
  },
});

const wss = new WebSocketServer({ server, path: "/ws" });
wss.on("connection", (ws) => {
  ws.send(READY);
  ws.on("message", () => ws.send(READY)); // answer hello/heartbeat -> stay connected & armed
});

server.listen(PORT, () => console.log(`mock-gateway: http://localhost:${PORT}`));
