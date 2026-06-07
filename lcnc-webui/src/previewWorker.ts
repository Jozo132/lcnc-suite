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

self.onmessage = async (e: MessageEvent<Req>) => {
  const { version, url } = e.data;
  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      self.postMessage({ version, error: `HTTP ${resp.status}` });
      return;
    }
    const buf = await resp.arrayBuffer();
    const g = msgpackDecode(new Uint8Array(buf)) as Record<string, any>;

    const feedPos = _flatten(g.feed);
    const rapidPos = _flatten(g.rapid);
    const feedLines = _toU32(g.feed_lines);

    // Drop the nested arrays from the passthrough; the flat typed arrays replace
    // them. Everything else (file, stats fields) is small and cloned as-is.
    const { feed: _f, rapid: _r, feed_lines: _fl, ...rest } = g;

    const transfer: Transferable[] = [feedPos.buffer as ArrayBuffer, rapidPos.buffer as ArrayBuffer];
    if (feedLines) transfer.push(feedLines.buffer as ArrayBuffer);

    self.postMessage(
      { version, gcode: { ...rest, feedPos, rapidPos, feed_lines: feedLines } },
      { transfer },
    );
  } catch (err) {
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
