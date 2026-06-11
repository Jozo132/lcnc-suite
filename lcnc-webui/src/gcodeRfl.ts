/**
 * Run-from-line toolchange scan (RFL × M600 guard).
 *
 * LinuxCNC's run-from-line skim re-enters M600/M601 remap bodies in lines
 * 1..N-1: probes execute for REAL while plain moves are discarded and G43
 * doesn't survive into the synthesized state (no TLO at cycle start). The UI
 * therefore scans the program text before confirming a run-from-line start:
 *   - 0 toolchanges before N  → normal RFL.
 *   - 1 toolchange, known T   → redirect: gateway measures via MDI first
 *                               (pre_tool), the #3116 flag skips the skim's
 *                               re-entry (see tool_touch_off.ngc o<450>).
 *   - anything else           → refuse with guidance (unsupported).
 *
 * Pure module (string in, result out) so it's unit-testable.
 */

export interface RflToolchangeScan {
  /** toolchange statements (M6 / M600 / M601) strictly before startLine */
  count: number;
  /** T number in effect at the LAST toolchange before startLine (null = none/unknown) */
  lastTool: number | null;
  /** 1-based source line of that last toolchange */
  lastLine: number | null;
}

// Word matchers on comment-stripped text. M0*6 must not match M60/M66/M600 —
// (?!\d) guards the tail; M600/M601 are matched explicitly first.
const RE_M600 = /(?<![A-Z0-9.])M0*60([01])(?!\d)/gi;
const RE_M6 = /(?<![A-Z0-9.])M0*6(?!\d)/gi;
const RE_T = /(?<![A-Z0-9.])T0*(\d+)(?!\d)/gi;

function stripComments(line: string): string {
  // `;` to end of line, and any `(...)` blocks (LinuxCNC inline comments).
  const semi = line.indexOf(";");
  let s = semi === -1 ? line : line.slice(0, semi);
  s = s.replace(/\([^)]*\)/g, " ");
  return s;
}

export function scanToolchangesBefore(text: string, startLine: number): RflToolchangeScan {
  const out: RflToolchangeScan = { count: 0, lastTool: null, lastLine: null };
  if (!text || startLine <= 1) return out;

  let lineNum = 0;
  let pos = 0;
  let pendingTool: number | null = null;  // last T word seen so far (modal prepare)

  while (pos <= text.length) {
    lineNum++;
    if (lineNum >= startLine) break;  // strictly before the start line
    const nl = text.indexOf("\n", pos);
    const rawLine = nl === -1 ? text.slice(pos) : text.slice(pos, nl);
    pos = nl === -1 ? text.length + 1 : nl + 1;

    // Cheap pre-filter: the heavy regexes only run on lines that can match.
    if (!/[TtMm]/.test(rawLine)) continue;
    const line = stripComments(rawLine);
    if (!line) continue;

    RE_T.lastIndex = 0;
    let tMatch: RegExpExecArray | null = null;
    for (let m = RE_T.exec(line); m; m = RE_T.exec(line)) tMatch = m;
    if (tMatch) pendingTool = parseInt(tMatch[1]!, 10);

    RE_M600.lastIndex = 0;
    RE_M6.lastIndex = 0;
    // Remove M600/M601 occurrences before testing plain M6 so "M600" doesn't
    // need negative-lookahead gymnastics in RE_M6.
    const m600Hits = line.match(RE_M600)?.length ?? 0;
    const m6Hits = line.replace(RE_M600, " ").match(RE_M6)?.length ?? 0;
    const hits = m600Hits + m6Hits;
    if (hits > 0) {
      out.count += hits;
      out.lastTool = pendingTool;
      out.lastLine = lineNum;
    }
    if (nl === -1) break;
  }
  return out;
}
