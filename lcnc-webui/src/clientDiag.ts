// Periodic client diagnostics → gateway trace log.
//
// Every CLIENT_DIAG_INTERVAL ms we send a snapshot of {JS heap, Three.js
// renderer counters, connection state} to the gateway, which forwards it to
// /tmp/lcnc-trace.log via _trace.emit("client.diag", ...). Survives the
// renderer dying ("Aw Snap"): the file is owned by the gateway process.
//
// Chromium-only `performance.memory` is reported when available; other
// browsers report null heap fields and the renderer counters still flow.
//
// Cost: ~9 small fields once per minute. Negligible compared to the 30 Hz
// status stream. Always-on — no toggle.

import { send, connected, armed } from './lcncWs';

const CLIENT_DIAG_INTERVAL = 60_000; // 1 min

interface ChromiumMemory {
  usedJSHeapSize: number;
  totalJSHeapSize: number;
  jsHeapSizeLimit: number;
}

function readHeap(): { used_mb: number; total_mb: number; limit_mb: number } | null {
  const mem = (performance as unknown as { memory?: ChromiumMemory }).memory;
  if (!mem) return null;
  const MB = 1024 * 1024;
  return {
    used_mb: +(mem.usedJSHeapSize / MB).toFixed(1),
    total_mb: +(mem.totalJSHeapSize / MB).toFixed(1),
    limit_mb: +(mem.jsHeapSizeLimit / MB).toFixed(1),
  };
}

function snapshot(): Record<string, any> {
  const heap = readHeap();
  const render = window.__viewerDiag?.getRenderInfo?.() ?? null;
  return {
    ts_client: Date.now(),
    heap,                // null on non-Chromium
    render,              // null until viewer initialized
    connected: connected.value,
    armed: armed.value,
  };
}

let timer: number | null = null;

export function startClientDiag(): void {
  if (timer !== null) return;
  // Don't fire on the leading edge — first tick at +interval so the page has
  // time to settle (viewer init, WS connect) before producing a noisy snapshot.
  timer = window.setInterval(() => {
    // send() is a no-op when the WS isn't open, so calling unconditionally
    // is safe. Reconnects pick up at the next tick.
    send({ cmd: 'client_diag', data: snapshot() });
  }, CLIENT_DIAG_INTERVAL);
}
