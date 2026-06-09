// Off-main-thread G-code preview loader (P4.1 / handoff step 6).
//
// Loading a heavy program used to fetch + msgpack-decode the multi-MB preview AND
// flatten its nested point arrays on the MAIN thread, blocking it for seconds —
// which starved the heartbeat worker and tripped a client-heartbeat disarm on
// load. This worker does the fetch, the decode, and the nested→flat conversion
// off-thread, then transfers the resulting typed arrays back zero-copy. The main
// thread (ThreeViewer) builds BufferAttributes directly from them — no decode, no
// points.flat(), no per-point allocation on the UI thread.
import { decode as msgpackDecode } from "@msgpack/msgpack";

interface Req { version: number; url: string }

// Newest-version-wins: abort any in-flight fetch when a newer preview arrives, so a
// superseded version no longer burns network + decode CPU (review #2). The main
// thread already discards stale *results* via a version guard; this stops the *work*.
let _currentAbort: AbortController | null = null;

self.onmessage = async (e: MessageEvent<Req>) => {
  const { version, url } = e.data;
  if (_currentAbort) _currentAbort.abort();
  const ac = new AbortController();
  _currentAbort = ac;
  try {
    const resp = await fetch(url, { signal: ac.signal });
    if (!resp.ok) {
      self.postMessage({ version, error: `HTTP ${resp.status}` });
      return;
    }
    const buf = await resp.arrayBuffer();
    if (ac.signal.aborted) return;  // superseded during the read — skip the decode
    const g = msgpackDecode(new Uint8Array(buf)) as Record<string, any>;

    const feedPos = _flatten(g.feed);
    const rapidPos = _flatten(g.rapid);
    const feedLines = _toU32(g.feed_lines);
    const feedLineMap = _buildFeedLineMap(g.feed_lines);

    // Drop the nested arrays from the passthrough; the flat typed arrays replace
    // them. Everything else (file, stats fields) is small and cloned as-is.
    const { feed: _f, rapid: _r, feed_lines: _fl, ...rest } = g;

    const transfer: Transferable[] = [feedPos.buffer as ArrayBuffer, rapidPos.buffer as ArrayBuffer];
    if (feedLines) transfer.push(feedLines.buffer as ArrayBuffer);

    self.postMessage(
      { version, gcode: { ...rest, feedPos, rapidPos, feed_lines: feedLines, feedLineMap } },
      { transfer },
    );
  } catch (err) {
    if ((err as Error)?.name === "AbortError") return;  // expected on supersede — silent
    self.postMessage({ version, error: String((err as Error)?.message ?? err) });
  }
};

// nested [[x,y,z],...] → flat Float32Array [x,y,z,x,y,z,...]. Point index i maps
// to offset i*3, so feed_lines (one entry per point) stays index-aligned.
function _flatten(pts: unknown): Float32Array {
  if (!Array.isArray(pts) || pts.length === 0) return new Float32Array(0);
  const out = new Float32Array(pts.length * 3);
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i];
    out[i * 3] = p[0]; out[i * 3 + 1] = p[1]; out[i * 3 + 2] = p[2];
  }
  return out;
}

function _toU32(a: unknown): Uint32Array | undefined {
  if (!Array.isArray(a)) return undefined;
  const out = new Uint32Array(a.length);
  for (let i = 0; i < a.length; i++) out[i] = a[i];
  return out;
}

// Build the source-line → point-index range map off the main thread (P4.1). A Map
// survives structured clone, and it has one entry per source line (far fewer than
// points), so cloning it is cheap while the O(points) build moves off the UI thread.
function _buildFeedLineMap(feed_lines: unknown): Map<number, { start: number; end: number }> {
  const m = new Map<number, { start: number; end: number }>();
  if (!Array.isArray(feed_lines)) return m;
  for (let i = 0; i < feed_lines.length; i++) {
    const ln = feed_lines[i];
    const entry = m.get(ln);
    if (entry) entry.end = i;
    else m.set(ln, { start: i, end: i });
  }
  return m;
}
