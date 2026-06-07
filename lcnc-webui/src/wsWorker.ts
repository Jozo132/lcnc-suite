// Dedicated Worker that OWNS the WebSocket connection.
//
// Why: the client heartbeat (1 Hz) keeps the gateway from disarming this client
// after 3 s of silence. Previously the heartbeat *timer* lived in a worker but
// the actual `ws.send` ran on the main thread, so any main-thread jank (fast
// typing in a large editor, heavy 30 Hz reactive updates) could starve the send
// past 3 s and trigger a spurious disarm. Moving the socket itself into the
// worker means the heartbeat is generated AND sent off the main thread, immune
// to main-thread stalls.
//
// The worker is a transparent transport proxy: it never decodes msgpack and
// only originates the three frames it must (`hello`, `tab_visibility`,
// `heartbeat`). Every other frame is relayed verbatim to the main thread, which
// keeps all message interpretation and reactive state. The gateway is unaware
// anything changed — same single socket, same client_id, byte-identical frames.

type Cfg = { url: string; session: string; resumeArmed: boolean; hidden: boolean };

type MainMsg =
  | { type: "connect"; url: string; session: string; resumeArmed: boolean; hidden: boolean }
  | { type: "send"; payload: string; cmd?: string; dropIfClosed?: boolean }
  | { type: "updateConfig"; resumeArmed?: boolean; hidden?: boolean; fireHeartbeat?: boolean }
  | { type: "close" };

const post = (m: unknown, transfer?: Transferable[]) =>
  (self as unknown as Worker).postMessage(m, transfer ?? []);

let ws: WebSocket | null = null;
let cfg: Cfg | null = null;
let wantConnected = false;

let hbTimer: ReturnType<typeof setInterval> | null = null;
let bufferTimer: ReturnType<typeof setInterval> | null = null;
let connectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

// Commands that arrive before the socket is OPEN are queued and flushed after
// the handshake (hello/tab_visibility) so hello is always the first frame.
const preOpenQueue: string[] = [];
const QUEUE_MAX = 64;

let attempt = 0;
let lastAttemptAt = 0;
let lastCloseAt = 0;
let lastCloseCode = 0;

function startHeartbeat() {
  stopHeartbeat();
  hbTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send('{"cmd":"heartbeat"}'); } catch { /* socket closing */ }
      post({ type: "hbsent" });
    }
  }, 1000);
}
function stopHeartbeat() {
  if (hbTimer !== null) { clearInterval(hbTimer); hbTimer = null; }
}

// Send-buffer pressure is reported as a STATE TRANSITION, not a per-second
// sample (P0). The old code posted every second whenever bufferedAmount > 0 —
// even a normal 19-byte in-flight write — producing ~1 telemetry POST/sec/tab
// (tens of thousands of events + gateway POSTs that themselves loaded the event
// loop). Now: emit once when buffered crosses the 64 KiB threshold (real
// backpressure), once when it drains, plus a periodic summary while sustained.
const BUFFER_PRESSURE_THRESHOLD = 64 * 1024;
const BUFFER_PRESSURE_SUMMARY_MS = 10_000;
let bufferPressureActive = false;
let bufferPressurePeak = 0;
let bufferPressureSince = 0;
let bufferPressureLastSummary = 0;

function startBufferSampler() {
  stopBufferSampler();
  bufferPressureActive = false;
  bufferPressurePeak = 0;
  bufferTimer = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const buffered = ws.bufferedAmount;
    const now = performance.now();
    if (!bufferPressureActive) {
      if (buffered >= BUFFER_PRESSURE_THRESHOLD) {
        bufferPressureActive = true;
        bufferPressurePeak = buffered;
        bufferPressureSince = now;
        bufferPressureLastSummary = now;
        post({ type: "bufferpressure", phase: "start", buffered });
      }
    } else {
      if (buffered > bufferPressurePeak) bufferPressurePeak = buffered;
      if (buffered === 0) {
        bufferPressureActive = false;
        post({ type: "bufferpressure", phase: "recover", buffered: 0,
               peak: bufferPressurePeak,
               durationMs: Math.round(now - bufferPressureSince) });
      } else if (now - bufferPressureLastSummary >= BUFFER_PRESSURE_SUMMARY_MS) {
        bufferPressureLastSummary = now;
        post({ type: "bufferpressure", phase: "sustained", buffered,
               peak: bufferPressurePeak,
               durationMs: Math.round(now - bufferPressureSince) });
      }
    }
  }, 1000);
}
function stopBufferSampler() {
  if (bufferTimer !== null) { clearInterval(bufferTimer); bufferTimer = null; }
}

function clearConnectTimer() {
  if (connectTimer !== null) { clearTimeout(connectTimer); connectTimer = null; }
}

function openSocket() {
  if (!cfg) return;
  attempt++;
  lastAttemptAt = performance.now();
  post({
    type: "attempt",
    attempt,
    lastCloseCode,
    gapMs: lastCloseAt ? Math.round(performance.now() - lastCloseAt) : 0,
  });

  ws = new WebSocket(cfg.url);
  ws.binaryType = "arraybuffer";

  // Connect-attempt timeout: a proxy (e.g. Vite dev) can hold an upstream
  // upgrade open indefinitely while the gateway is down — the socket sits in
  // CONNECTING with no error. Force-close after 3 s so onclose fires and the
  // 2 s reconnect cadence takes over.
  connectTimer = setTimeout(() => {
    if (ws && ws.readyState === WebSocket.CONNECTING) {
      try { ws.close(); } catch { /* ignore */ }
    }
  }, 3000);

  ws.onopen = () => {
    clearConnectTimer();
    post({ type: "open", attempt, dtMs: Math.round(performance.now() - lastAttemptAt) });
    attempt = 0;
    // hello MUST be the first frame so the gateway can associate this
    // connection with the prior tab's armed-resume hold before anything else.
    try {
      ws!.send(JSON.stringify({ cmd: "hello", session: cfg!.session, resume_armed: cfg!.resumeArmed }));
    } catch (e) {
      post({ type: "error", kind: "hello_send_failed", msg: String((e as Error)?.message ?? e) });
    }
    cfg!.resumeArmed = false; // consumed
    try {
      ws!.send(JSON.stringify({ cmd: "tab_visibility", hidden: cfg!.hidden }));
    } catch { /* ignore */ }
    // Flush queued user commands AFTER the handshake.
    while (preOpenQueue.length) {
      const p = preOpenQueue.shift()!;
      try { ws!.send(p); } catch { /* ignore */ }
    }
    startHeartbeat();
    startBufferSampler();
  };

  ws.onmessage = (ev: MessageEvent) => {
    if (typeof ev.data === "string") {
      post({ type: "message", data: ev.data });
    } else {
      // Transfer the ArrayBuffer (zero-copy); the worker doesn't touch it again.
      post({ type: "message", data: ev.data }, [ev.data as ArrayBuffer]);
    }
  };

  ws.onerror = (e: Event) => {
    post({ type: "error", kind: "ws_error", msg: (e as Event)?.type ?? "?" });
  };

  ws.onclose = (ev: CloseEvent) => {
    clearConnectTimer();
    stopHeartbeat();
    stopBufferSampler();
    lastCloseAt = performance.now();
    lastCloseCode = ev.code;
    ws = null;
    post({
      type: "close",
      code: ev.code,
      reason: String(ev.reason ?? ""),
      wasClean: ev.wasClean,
      sinceAttemptMs: Math.round(performance.now() - lastAttemptAt),
    });
    if (wantConnected) {
      post({ type: "reconnecting", attempt: attempt + 1, gapMs: 2000 });
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => { if (wantConnected) openSocket(); }, 2000);
    }
  };
}

function teardown() {
  wantConnected = false;
  stopHeartbeat();
  stopBufferSampler();
  clearConnectTimer();
  if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  preOpenQueue.length = 0;
  if (ws) {
    ws.onclose = null; // prevent the reconnect path from firing on our own close
    try { ws.close(); } catch { /* ignore */ }
    ws = null;
  }
}

self.onmessage = (ev: MessageEvent<MainMsg>) => {
  const msg = ev.data;
  switch (msg.type) {
    case "connect":
      teardown();
      cfg = { url: msg.url, session: msg.session, resumeArmed: msg.resumeArmed, hidden: msg.hidden };
      wantConnected = true;
      openSocket();
      break;

    case "send":
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(msg.payload); } catch { /* ignore */ }
      } else if (msg.dropIfClosed) {
        // Mutating/motion command issued while the socket is closed — DROP it
        // rather than replay a stale operator action into a fresh connection
        // after reconnect (issue #18). Surface it so the operator knows.
        post({ type: "error", kind: "dropped_command", msg: String(msg.cmd ?? "?") });
      } else {
        // Read-only/telemetry — safe to queue across a brief blip.
        if (preOpenQueue.length >= QUEUE_MAX) {
          preOpenQueue.shift();
          post({ type: "error", kind: "send_queue_overflow", msg: String(QUEUE_MAX) });
        }
        preOpenQueue.push(msg.payload);
      }
      break;

    case "updateConfig":
      if (cfg) {
        if (msg.resumeArmed !== undefined) cfg.resumeArmed = msg.resumeArmed;
        if (msg.hidden !== undefined) {
          cfg.hidden = msg.hidden;
          if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify({ cmd: "tab_visibility", hidden: cfg.hidden })); } catch { /* ignore */ }
          }
        }
      }
      // Fire one immediate heartbeat (e.g. tab just became visible) so the
      // gateway's last_hb is fresh without waiting up to 1 s for the timer.
      if (msg.fireHeartbeat && ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send('{"cmd":"heartbeat"}'); } catch { /* ignore */ }
        post({ type: "hbsent" });
      }
      break;

    case "close":
      teardown();
      break;
  }
};
