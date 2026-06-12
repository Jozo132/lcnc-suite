import type { MacroParam } from "./defaults";

// Pure macro-param helpers. Kept in their own side-effect-free module (only a
// type-only import) so they're unit-testable in a plain node environment —
// importing defaults.ts directly pulls in its page-lifecycle listeners which
// need `document`. Re-exported from defaults.ts for back-compat.

/** Extract unique {placeholder} names from a macro command string, in order. */
export function extractParams(command: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const match of command.matchAll(/\{(\w+)\}/g)) {
    const name = match[1]!;
    if (!seen.has(name)) { seen.add(name); result.push(name); }
  }
  return result;
}

/** Reconcile a macro's params with the {placeholders} in its command — in
 *  command order: keep (preserving edits to) params still referenced, add new
 *  ones, drop removed ones. Existing param objects are reused by reference, so
 *  in-progress edits to a kept param survive a command change. */
export function syncMacroParams(command: string, existing: MacroParam[]): MacroParam[] {
  const byName = new Map(existing.map(p => [p.name, p]));
  return extractParams(command).map(
    name => byName.get(name) ?? { name, label: name, default: "" },
  );
}
