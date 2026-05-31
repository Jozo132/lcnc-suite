// Rolling frame/status-timing probe for the 3D viewer → gateway trace bus.
//
// Purpose: localize the "machine movement hiccup" symptom on large programs.
// The status datastream is fixed-size regardless of g-code file size (the
// payload is pre-encoded once and spliced verbatim per client; the preview
// polylines never touch the WS — see gateway.py), so a stutter has to be one
// of three things, which this probe separates:
//
//   statusGap — wall gap between consecutive status frames as the client sees
//               them. Even ~33 ms = healthy 30 Hz stream; bursty = WS/stream
//               jitter (the "datastream" hypothesis).
//   applyMs   — CPU time inside applyState() (backplot append, highlight
//               drawRange, render-on-demand signature). Spikes here = main-
//               thread work / GC pauses.
//   renderMs  — CPU time submitting the WebGL frame. Spikes here = draw cost.
//
// Plus a jank counter (status frames whose gap exceeds JANK_MS) and a context
// snapshot (toolpath segment counts, backplot ring fill, JS heap) so a summary
// line can be correlated against file size and the backplot-full regime.
//
// Emission rides the existing telemetry batcher (POST /telemetry, off the WS
// status path), tagged `browser.viewer.perf` in trace.ndjson. One summary per
// WINDOW_MS, and only when there was activity — idle machines stay silent.

import { emitTelemetry } from './lcncWs';

const WINDOW_MS = 3000;   // one summary per 3 s of activity
const JANK_MS = 50;       // a status-frame gap over this counts as a hiccup
const SAMPLE_CAP = 1200;  // bound per-window gap-sample memory (~40 s @ 30 Hz)

let _enabled = true;

// Stable per-page-load client identity, so summaries from different browsers/
// tabs can be told apart in the trace (the telemetry POST `peer` is a fresh
// ephemeral port each batch and is useless for this). `host` distinguishes the
// local viewer (localhost/127.0.0.1) from a remote LAN browser (the VM's IP).
const _clientId =
  typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10);
const _host = typeof location !== "undefined" ? location.hostname : "?";

// Per-window accumulators (reset on each emit).
let _frames = 0;                  // applyState invocations this window
const _gaps: number[] = [];       // statusGap samples (ms) for percentiles
let _gapMax = 0;
let _jank = 0;
let _applySum = 0, _applyMax = 0;
let _renderSum = 0, _renderCount = 0, _renderMax = 0;
let _lastApplyTs = 0;             // performance.now() of previous applyState

// Optional context provider, registered by the viewer. Invoked once per emit
// (not per frame) so it can read live geometry/buffer state cheaply.
let _context: (() => Record<string, unknown>) | null = null;

export function setViewerPerfContext(fn: (() => Record<string, unknown>) | null): void {
  _context = fn;
}

export function setViewerPerfEnabled(on: boolean): void {
  _enabled = on;
}

/** Record one applyState() invocation: its CPU duration plus the wall gap
 *  since the previous one (status-arrival cadence as seen by the client). */
export function recordApply(applyMs: number): void {
  if (!_enabled) return;
  const now = performance.now();
  if (_lastApplyTs > 0) {
    const gap = now - _lastApplyTs;
    if (_gaps.length < SAMPLE_CAP) _gaps.push(gap);
    if (gap > _gapMax) _gapMax = gap;
    if (gap > JANK_MS) _jank++;
  }
  _lastApplyTs = now;
  _frames++;
  _applySum += applyMs;
  if (applyMs > _applyMax) _applyMax = applyMs;
}

/** Record CPU time submitting one rendered WebGL frame. */
export function recordRender(renderMs: number): void {
  if (!_enabled) return;
  _renderSum += renderMs;
  _renderCount++;
  if (renderMs > _renderMax) _renderMax = renderMs;
}

function _percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[idx]!;
}

interface ChromiumMemory { usedJSHeapSize: number; jsHeapSizeLimit: number }
function _heapUsedMb(): number | null {
  const mem = (performance as unknown as { memory?: ChromiumMemory }).memory;
  if (!mem) return null;
  return +(mem.usedJSHeapSize / (1024 * 1024)).toFixed(1);
}

function _flush(): void {
  if (_frames === 0) return; // idle window — stay quiet
  const sorted = _gaps.slice().sort((a, b) => a - b);
  emitTelemetry('viewer.perf', {
    cid: _clientId,                // stable per-page-load id (separates tabs)
    host: _host,                   // localhost = local VM browser, IP = remote
    vis: typeof document !== 'undefined' ? document.visibilityState : '?',
    window_ms: WINDOW_MS,
    frames: _frames,
    // status-arrival cadence (the "is the stream bursty?" signal)
    gap_p50_ms: +_percentile(sorted, 50).toFixed(1),
    gap_p95_ms: +_percentile(sorted, 95).toFixed(1),
    gap_max_ms: +_gapMax.toFixed(1),
    jank_frames: _jank,            // gaps > JANK_MS
    jank_ms: JANK_MS,
    // applyState CPU (main-thread work / GC pressure)
    apply_mean_ms: +(_applySum / _frames).toFixed(2),
    apply_max_ms: +_applyMax.toFixed(2),
    // WebGL submit CPU (draw cost)
    render_mean_ms: _renderCount ? +(_renderSum / _renderCount).toFixed(2) : 0,
    render_max_ms: +_renderMax.toFixed(2),
    renders: _renderCount,
    heap_used_mb: _heapUsedMb(),   // null on non-Chromium
    ...(_context?.() ?? {}),
  });
  _frames = 0;
  _gaps.length = 0;
  _gapMax = 0;
  _jank = 0;
  _applySum = _applyMax = 0;
  _renderSum = _renderMax = 0;
  _renderCount = 0;
}

// One self-quieting timer: flushes only when the window saw activity, so an
// idle viewer produces no trace volume. Survives across jobs without toggling.
if (typeof window !== 'undefined') {
  window.setInterval(_flush, WINDOW_MS);
}
