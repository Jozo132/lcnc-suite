# Gate Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fail-open per-element permission system with a fail-closed `<Gate>` component backed by HTML `<fieldset disabled>`.

**Architecture:** Gate.vue renders a `<fieldset>` that browser-enforces disabled state on all descendant form elements. Wraps existing `.inactive` divs and absorbs per-element `:disabled="!can.X"` bindings. Existing `permissions.ts`, `Btn.vue`, and backend `require_armed()` are unchanged.

**Tech Stack:** Vue 3, TypeScript, HTML fieldset semantics

**Spec:** `docs/superpowers/specs/2026-03-24-gate-framework-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `lcnc-webui/src/Gate.vue` | Fieldset-based permission gate wrapper |
| Modify | `lcnc-webui/src/style.css` | Merge Gate styles into `fs-reset`, remove `.inactive` |
| Modify | `lcnc-webui/src/DroPanel.vue` | Wrap in Gate, remove inactive + redundant disabled |
| Modify | `lcnc-webui/src/JogPanel.vue` | Wrap in Gate, preserve JogButton disabled props |
| Modify | `lcnc-webui/src/ManualPanel.vue` | 4 inactive wrappers → Gates |
| Modify | `lcnc-webui/src/ToolTablePanel.vue` | 1 inactive wrapper → Gate |
| Modify | `lcnc-webui/src/Toolbar.vue` | 29 disabled bindings → Gate |
| Modify | `lcnc-webui/src/CameraViewer.vue` | 6 disabled bindings → Gate |
| Modify | `lcnc-webui/src/ProbePanel.vue` | 13 inactive wrappers → Gates, dialog Teleport |
| Modify | `lcnc-webui/src/GcodePanel.vue` | Mixed gates with exempt slot, dialog Teleport |
| Modify | `lcnc-webui/src/GcodeHUD.vue` | Mixed gates with exempt slot |
| Modify | `lcnc-webui/src/JogHUD.vue` | Wrap in Gate, preserve JogButton disabled props |
| Modify | `lcnc-webui/src/SetupHUD.vue` | Wrap in Gate (idle + zero sections) |
| Modify | `lcnc-webui/src/DebugTab.vue` | Add data-gate-exempt to diagnostic buttons |
| Modify | `lcnc-webui/src/App.vue` | Sidebar fieldsets → Gates, dialog Teleports |
| Modify | `lcnc-webui/src/SettingsPanel.vue` | Existing fieldsets → Gates, dialog Teleport |
| Modify | `lcnc-webui/src/GcodeReferenceDialog.vue` | 1 inactive wrapper → Gate |
| Modify | `CLAUDE.md` | Update permission gate guidance |

---

## Important Context for All Tasks

- **Read the spec first:** `docs/superpowers/specs/2026-03-24-gate-framework-design.md`
- **Gate.vue** renders `<fieldset :disabled="!allow">`. The browser natively disables all `<button>`, `<input>`, `<select>` descendants. No JS gating needed.
- **`#exempt` slot** renders inside `<legend>` — HTML spec exempts the first legend's children from fieldset disabled. Use for Abort/E-Stop buttons that must always work.
- **Existing `fs-reset` class** in `style.css:389-401` already resets fieldset styling. Gate.vue reuses it.
- **Remove `:disabled="!can.X"`** from elements whose permission matches the parent Gate. Keep `:disabled` for application-state logic (loading, no file, form validation, probing).
- **Remove `:class="{ inactive: !can.X }"`** — Gate's fieldset `:disabled` CSS handles opacity.
- **Mixed logic** like `:disabled="!can.idle || loading"` becomes `:disabled="loading"` inside `<Gate :allow="can.idle">`.
- **Existing `<fieldset :disabled="!permissions.X" class="fs-reset">`** in App.vue and SettingsPanel.vue are already doing what Gate does. Replace them with `<Gate>` for consistency.
- **JogButton.vue**: preserve its `:disabled` prop — it has an internal JS guard for pointer capture edge cases.
- **Dialogs inside Gates**: use `<Teleport to="body">` so they escape the Gate scope. ToolTablePanel already does this.
- **Tab buttons** (TabPanel.vue): must NOT be inside a Gate — tabs are pure navigation. Gate wraps panel content, not the tab bar.
- **Build verification**: run `npm run build` in `lcnc-webui/` after every task. Zero TS errors required.
- **Line numbers are approximate** — based on pre-migration state. Earlier tasks shift line numbers in later files. Always re-read the file to locate actual elements.
- **fs-reset flex conflict**: `fs-reset` sets `display: flex; flex-direction: column`. When Gate replaces a row-layout div (e.g., `row-tight`), wrap the existing div *inside* the Gate rather than adding the row class to the Gate: `<Gate :allow="can.X"><div class="row-tight">...</div></Gate>`. Only add layout classes directly to Gate when the existing wrapper was already column-layout.
- **fs-reset:disabled opacity**: Remove `.fs-reset:disabled { opacity: var(--opacity-disabled) }` from style.css — it causes double-dimming. The fieldset at 0.4 × child at 0.4 = 0.16 effective opacity. Child elements handle their own disabled opacity via their `:disabled` CSS rules.
- **Permission class definitions** for reference: `idle = base && isIdle && !busy`, `jog = base && isIdle && isHomed`, `ready = idle + isHomed`, `override = base && !busy`, `probe = ready + !eoffsetEnabled`, `zero = idle + !eoffsetEnabled`, `abort = base`, `pause = base && isRunning && !isPaused`, `resume = base && isPaused`.
- **Dialogs after Teleport**: once a dialog is Teleported to body, it's outside all Gates. Action buttons inside Teleported dialogs need their own `:disabled` binding or inner Gate for machine operations.
- **DebugTab.vue**: has 3 diagnostic buttons (not machine control). Mark with `data-gate-exempt` attribute so the dev audit (Task 15) doesn't warn about them.

---

### Task 1: Create Gate.vue and update style.css

**Files:**
- Create: `lcnc-webui/src/Gate.vue`
- Modify: `lcnc-webui/src/style.css:389-401`

- [ ] **Step 1: Create Gate.vue**

```vue
<script setup lang="ts">
defineProps<{ allow: boolean }>();
</script>

<template>
  <fieldset :disabled="!allow" class="fs-reset">
    <legend v-if="$slots.exempt" class="gate-exempt">
      <slot name="exempt" />
    </legend>
    <slot />
  </fieldset>
</template>

<style scoped>
.gate-exempt {
  float: none;
  padding: 0;
  width: auto;
}
</style>
```

Note: Gate reuses the existing `fs-reset` class from style.css rather than creating a new `.gate` class (diverges from spec which shows `.gate` — this is intentional simplification). This means existing fieldsets with `fs-reset` already have the right styling.

Note: Leave `.fs-reset:disabled { opacity }` in place during this task — it will be removed in Task 14 Step 5 after all Gates are deployed. Removing it now would break the `.inactive`-based system still in use during intermediate tasks.

- [ ] **Step 2: Add `min-inline-size: 0` to fs-reset if missing**

Check `style.css:389-401`. The existing `fs-reset` may not have `min-inline-size: 0` (fieldset default min-width quirk). Add it if missing.

- [ ] **Step 3: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: PASS (Gate.vue is created but not imported anywhere yet)

- [ ] **Step 4: Commit**

```bash
git add lcnc-webui/src/Gate.vue lcnc-webui/src/style.css
git commit -m "feat: add Gate.vue fieldset-based permission gate component"
```

---

### Task 2: Migrate DroPanel

**Files:**
- Modify: `lcnc-webui/src/DroPanel.vue:60-83`

DroPanel has:
- Line 60: `<div class="stack-sections container" :class="{ inactive: !can.idle }">` — replace with Gate
- Lines 66-69: `:disabled="!can.zero"` on touchoff inputs and Set buttons — these use `can.zero`, NOT `can.idle`. Since `can.zero` is tighter than `can.idle`, use nested Gate or keep as-is.
- Lines 81-83: `:disabled="!can.idle"` on Home/Unhome buttons — redundant inside Gate

- [ ] **Step 1: Add Gate import**

Add `import Gate from './Gate.vue'` to DroPanel.vue's script.

- [ ] **Step 2: Replace inactive wrapper with Gate**

Replace the outer `<div class="stack-sections container" :class="{ inactive: !can.idle }">` with `<Gate :allow="can.idle" class="stack-sections container">`.

- [ ] **Step 3: Remove redundant `:disabled="!can.idle"` bindings**

Remove `:disabled="!can.idle"` from Home/Unhome buttons (lines ~81-83). The Gate handles this.

- [ ] **Step 4: Handle touchoff elements**

The touchoff inputs and Set buttons use `can.zero` (tighter: adds eoffset check). Two options:
- Nest a `<Gate :allow="can.zero">` around the touchoff section
- Keep `:disabled="!can.zero"` on those elements since `can.zero` differs from the parent `can.idle`

Choose nested Gate if there are 3+ elements with `can.zero` in a group. Keep individual `:disabled` if scattered.

Read the actual template to decide, then apply.

- [ ] **Step 5: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: PASS, zero errors

- [ ] **Step 6: Commit**

```bash
git add lcnc-webui/src/DroPanel.vue
git commit -m "refactor: migrate DroPanel to Gate framework"
```

---

### Task 3: Migrate JogPanel

**Files:**
- Modify: `lcnc-webui/src/JogPanel.vue:215-258`

JogPanel has:
- Line 215: `<div :class="{ inactive: !can.jog }">` — replace with Gate
- Line 224: `<Btn :disabled="!can.jog">` (mode toggle) — redundant, remove
- Lines 243, 258: `<input :disabled="!can.jog">` (speed sliders) — redundant, remove
- JogButton components receive `:disabled` via parent — **preserve these** per spec (JogButton has internal JS guard)

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Replace inactive wrapper with Gate**

Replace `<div :class="{ inactive: !can.jog }">` with `<Gate :allow="can.jog">`.

- [ ] **Step 3: Remove redundant `:disabled="!can.jog"` from Btn and input elements**

Remove from mode toggle button and speed slider inputs. The fieldset handles them.

- [ ] **Step 4: Preserve JogButton `:disabled` props**

JogButton components pass `:disabled` through a prop that feeds an internal `isDisabled` computed. These MUST stay — the fieldset blocks browser events but doesn't set the Vue prop. Verify JogButton instances keep their `:disabled` binding.

- [ ] **Step 5: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 6: Commit**

```bash
git add lcnc-webui/src/JogPanel.vue
git commit -m "refactor: migrate JogPanel to Gate framework"
```

---

### Task 4: Migrate ManualPanel

**Files:**
- Modify: `lcnc-webui/src/ManualPanel.vue:145-232`

ManualPanel has 4 inactive wrappers:
- Line 145: `{ inactive: !can.idle }` — WCS selector row
- Line 178: `{ inactive: !can.ready }` — goto row
- Line 209: `{ inactive: !can.ready }` — MDI section
- Line 232: `{ inactive: !can.ready }` on history items (individual divs, not a wrapper)

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Replace WCS row wrapper**

Replace `<div class="row-tight g5xRow" :class="{ inactive: !can.idle }">` with `<Gate :allow="can.idle" class="row-tight g5xRow">`. Remove `:disabled="!can.idle"` from WCS buttons inside.

- [ ] **Step 3: Replace goto row wrapper**

Replace `<div class="gotoRow" :class="{ inactive: !can.ready }">` with `<Gate :allow="can.ready" class="gotoRow">`. Remove `:disabled="!can.ready"` from Go to buttons inside.

- [ ] **Step 4: Replace MDI section wrapper**

Replace `<div ... class="mdiSection" :class="{ inactive: !can.ready }">` with `<Gate :allow="can.ready" class="mdiSection">`. Remove `:disabled="!can.ready"` from MDI input and Send button. Keep `:disabled="history.length === 0"` on Clear History (app-state logic).

- [ ] **Step 5: Handle history items**

The history items have `{ inactive: !can.ready }` on each `<div>` inside a `v-for`. These are clickable divs, not buttons — fieldset won't disable them. Wrap the history list container in a Gate instead, and convert history item click handlers to be inside a button or keep the inactive class on divs. Read the template to decide the cleanest approach.

- [ ] **Step 6: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 7: Commit**

```bash
git add lcnc-webui/src/ManualPanel.vue
git commit -m "refactor: migrate ManualPanel to Gate framework"
```

---

### Task 5: Migrate ToolTablePanel

**Files:**
- Modify: `lcnc-webui/src/ToolTablePanel.vue:396-610`

ToolTablePanel has:
- Line 396: `<div class="container" :class="{ inactive: !can.idle }">` — replace with Gate
- Multiple `:disabled="!can.idle"` on buttons, inputs, sort headers — remove (redundant in Gate)
- Line 406: `:disabled="loading || !can.idle"` — simplify to `:disabled="loading"`
- Line 595: `:disabled="!can.ready"` on Load Tool button — tighter than `can.idle`, keep or nest
- Dialogs already use `<Teleport to="body">` — no change needed

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Replace container wrapper with Gate**

Replace `<div class="container" :class="{ inactive: !can.idle }">` with `<Gate :allow="can.idle" class="container">`.

- [ ] **Step 3: Remove redundant `:disabled="!can.idle"` bindings**

Remove from: Add button, Import button, Search input, STL upload, STL remove, sort headers, Edit buttons, Delete buttons.

- [ ] **Step 4: Simplify mixed logic**

`:disabled="loading || !can.idle"` on Refresh → `:disabled="loading"`.

- [ ] **Step 5: Handle Load Tool button**

Load Tool uses `can.ready` (tighter than `can.idle`). Keep `:disabled="!can.ready"` on this button — the Gate provides `can.idle`, but the button needs the extra `homed` check from `can.ready`.

- [ ] **Step 6: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 7: Commit**

```bash
git add lcnc-webui/src/ToolTablePanel.vue
git commit -m "refactor: migrate ToolTablePanel to Gate framework"
```

---

### Task 6: Migrate Toolbar

**Files:**
- Modify: `lcnc-webui/src/Toolbar.vue:16-104`

Toolbar has 29 `:disabled="!can.idle"` bindings — all the same permission class. Every element is `can.idle`. This is the cleanest case.

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Wrap the toolbar content in a Gate**

Read the template to find the outermost content wrapper. Wrap it with `<Gate :allow="can.idle">`. The toolbar likely has pill groups — the Gate goes around all of them.

- [ ] **Step 3: Remove all 29 `:disabled="!can.idle"` bindings**

Remove from all Btn, input (checkbox, radio, number), and toggle elements. The fieldset handles them all.

- [ ] **Step 4: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 5: Commit**

```bash
git add lcnc-webui/src/Toolbar.vue
git commit -m "refactor: migrate Toolbar to Gate framework"
```

---

### Task 7: Migrate CameraViewer

**Files:**
- Modify: `lcnc-webui/src/CameraViewer.vue:161-176`

CameraViewer has 6 `:disabled="!can.idle"` bindings — 3 Btn toggles, 2 range sliders, 1 color picker. All same permission class.

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Wrap the floating toolbar/overlay controls in a Gate**

Read the template to find the container for the overlay controls. Wrap with `<Gate :allow="can.idle">`.

- [ ] **Step 3: Remove `:disabled="!can.idle"` from all 6 elements**

- [ ] **Step 4: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 5: Commit**

```bash
git add lcnc-webui/src/CameraViewer.vue
git commit -m "refactor: migrate CameraViewer to Gate framework"
```

---

### Task 8: Migrate ProbePanel

**Files:**
- Modify: `lcnc-webui/src/ProbePanel.vue:545-1238`

ProbePanel is the largest migration — 13 inactive wrappers with 3 different permission classes:
- `can.idle` (1): WCS selector row (line 545)
- `can.ready` (6): controlBar, results, surfaceActions, compStatus, G92 offset, rotation (lines 558, 1126, 1148, 1156, 1173, 1214)
- `can.probe` (6): all grid sections — Outside, Inside, Boss, Cal, Edge, Circle (lines 582, 670, 758, 850, 952, 1045)

Mixed logic on probe buttons: `:disabled="!can.probe || probing"` → `:disabled="probing"` inside `<Gate :allow="can.probe">`.

Dialog at line 1238: surface map dialog — needs `<Teleport to="body">`.

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Replace WCS row (can.idle)**

Replace inactive div at line 545 with `<Gate :allow="can.idle" class="row-tight g5xRow">`. Remove `:disabled="!can.idle"` from WCS buttons.

- [ ] **Step 3: Replace controlBar and ready sections (can.ready)**

Replace 5 inactive divs (lines 558, 1126, 1148, 1156, 1173, 1214) with Gates. Remove redundant `:disabled="!can.ready"` from children. Keep app-state disabled where present.

- [ ] **Step 4: Replace probe grid sections (can.probe)**

Replace 6 inactive divs (lines 582, 670, 758, 850, 952, 1045) with `<Gate :allow="can.probe">`. Simplify probe button disabled: `:disabled="!can.probe || probing"` → `:disabled="probing"`.

- [ ] **Step 5: Add Teleport to surface map dialog**

Wrap the dialog at line 1238 with `<Teleport to="body">`.

- [ ] **Step 6: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 7: Commit**

```bash
git add lcnc-webui/src/ProbePanel.vue
git commit -m "refactor: migrate ProbePanel to Gate framework"
```

---

### Task 9: Migrate GcodePanel

**Files:**
- Modify: `lcnc-webui/src/GcodePanel.vue:525-541, 654`

GcodePanel's control row (lines 524-541) is the most complex case — 6 buttons with 4 permission classes in one row:
- Start: `can.ready` (+ app state: activeFile, editing)
- Step: `can.ready || can.resume` (+ app state)
- Pause/Resume: `can.pause || can.resume`
- Abort: `can.abort`
- M01: `can.override`
- /BD: `can.override`

Strategy: there is no single Gate that covers this row since every button has a different class. Options:
1. Wrap the row in `<Gate :allow="can.abort">` (broadest: just `base`) with exempt slot, then keep individual `:disabled` for tighter permissions
2. Keep individual `:disabled` on each button (no Gate for this row)
3. Group by permission class with nested Gates

Read the template and choose the cleanest approach. The file browser section and edit mode likely have simpler gating needs.

Also: dialog at line 654 (run-from-line) — add `<Teleport to="body">` only if the dialog ends up inside a Gate after migration. If no Gate wraps its parent section, Teleport is unnecessary.

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Handle the file operations area**

Read the file browser and toolbar section (lines ~428-440). These buttons use `can.idle` with app-state conditions. Wrap in a Gate if they share a common permission class.

- [ ] **Step 3: Handle the control row**

Read lines 524-541 carefully. If no single Gate covers all buttons cleanly, keep individual `:disabled` bindings here. Do NOT force a Gate that doesn't simplify the code.

- [ ] **Step 4: Add Teleport to run-from-line dialog**

Wrap the dialog at line 654 with `<Teleport to="body">`.

- [ ] **Step 5: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 6: Commit**

```bash
git add lcnc-webui/src/GcodePanel.vue
git commit -m "refactor: migrate GcodePanel to Gate framework"
```

---

### Task 10: Migrate GcodeHUD, JogHUD, and SetupHUD

**Files:**
- Modify: `lcnc-webui/src/GcodeHUD.vue:102-113`
- Modify: `lcnc-webui/src/JogHUD.vue`
- Modify: `lcnc-webui/src/SetupHUD.vue:62-74`

GcodeHUD has same mixed-gate pattern as GcodePanel control row:
- Cycle Start: `can.ready` (+ gcodeContent)
- Pause/Resume: `can.pause || can.resume`
- Abort: `can.abort`
- M01: `can.override`
- /BD: `can.override`

Same strategy as GcodePanel — if no single Gate simplifies, keep individual `:disabled`.

SetupHUD has:
- Lines 72-74: 3 buttons with `:disabled="!can.ready"` — wrap in Gate

- [ ] **Step 1: Add Gate import to all three files**

- [ ] **Step 2: Assess GcodeHUD**

If all 5 buttons have different permission classes, keep individual `:disabled`. If M01+/BD can be grouped under a Gate, do that.

- [ ] **Step 3: Migrate JogHUD**

JogHUD receives a `disabled` prop from its parent (ThreeViewer). It contains ~12 JogButton instances, Btn components, and range sliders. Wrap the content in `<Gate :allow="!disabled">` (using the prop, not a permission class — JogHUD's parent controls the permission). Preserve all JogButton `:disabled` props (same reasoning as JogPanel). The SVG sector elements with pointer events are not form elements — fieldset does not affect them, they remain prop-guarded.

- [ ] **Step 4: Migrate SetupHUD**

SetupHUD has three permission tiers:
- `can.idle`: Home/Unhome buttons (via `homeDisabled`/`unhomeDisabled` computeds)
- `can.zero`: Touchoff inputs and Set buttons (via `zeroDisabled` computed)
- `can.ready`: Go to G30/Home/Zero buttons

Strategy: wrap the entire component content in `<Gate :allow="can.idle">` (broadest needed). Keep `zeroDisabled` computed on touchoff elements (since `can.zero` adds eoffset check beyond idle). Keep `can.ready` check on Go-to buttons (since ready adds homed check). Remove any pure `!can.idle` disabled bindings — the outer Gate handles them. Preserve JogButton `:disabled` props if any JogButtons exist in SetupHUD.

- [ ] **Step 5: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 6: Commit**

```bash
git add lcnc-webui/src/GcodeHUD.vue lcnc-webui/src/JogHUD.vue lcnc-webui/src/SetupHUD.vue
git commit -m "refactor: migrate GcodeHUD, JogHUD, and SetupHUD to Gate framework"
```

---

### Task 11: Migrate App.vue sidebar

**Files:**
- Modify: `lcnc-webui/src/App.vue:1406-1911`

App.vue already uses `<fieldset :disabled="!permissions.X" class="fs-reset">` in 3 places:
- Line 1406: Spindle popover (`permissions.ready`)
- Line 1494: Coolant popover (`permissions.ready`)
- Line 1568: Overrides popover (`permissions.override`)

These are already Gate-equivalent. Replace with `<Gate>` for consistency.

Additionally:
- Lines 1611, 1650: Work Offsets table/actions with `{ inactive: !permissions.ready }` — replace with Gate
- Line 1889: Tool dialog actions with `{ inactive: !permissions.ready }` — replace with Gate
- Lines 1898-1911: Tool dialog buttons with various `:disabled` — simplify inside Gate
- Dialogs (tool table main, settings, macro params): add `<Teleport to="body">`
- Status popovers (Machine, Program, Overrides display): keep trigger buttons OUTSIDE Gates per spec

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Replace 3 existing fieldset wrappers with Gate**

Replace `<fieldset :disabled="!permissions.ready" class="fs-reset">` at lines 1406, 1494 with `<Gate :allow="permissions.ready">`. Replace line 1568 with `<Gate :allow="permissions.override">`. Remove redundant `:disabled="!permissions.X"` from children inside each. Keep app-state disabled (`:disabled="!isSpinning"`, `:disabled="!feedOvrEnabled"`, etc.).

Note on Coolant popover (line 1494): the existing fieldset uses `permissions.ready`, but the coolant toggle buttons inside use `:disabled="!permissions.override"`. Since `ready` (idle + homed + !busy) is tighter than `override` (!busy), the `permissions.override` bindings are fully redundant when the fieldset/Gate is enabled — remove them.

- [ ] **Step 3: Replace Work Offsets inactive wrappers**

Replace `<table :class="{ inactive: !permissions.ready }">` and `<div class="offsetActions" :class="{ inactive: !permissions.ready }">` with Gate wrappers. Remove redundant `:disabled="!permissions.ready"` from children. Keep `:disabled="!selectedWcs"` (app state).

- [ ] **Step 4: Replace Tool dialog actions inactive wrapper**

Replace `<div class="toolDialogActions" :class="{ inactive: !permissions.ready }">` with Gate. Simplify children: `:disabled="!permissions.ready || !!st.probing"` → `:disabled="!!st.probing"`. Handle Abort button (uses `permissions.abort`) — needs exempt slot or separate Gate.

- [ ] **Step 5: Add Teleport to dialogs**

Wrap dialogs that are inside gated sections with `<Teleport to="body">`. Check which App.vue dialogs are at risk: tool table, settings, macro params. Safety dialogs (tool change, shutdown, compensation) are outside Gates — no change.

Note on macro params dialog (~line 1974): after Teleporting, the Execute button's `:disabled="!permissions.ready"` must be preserved — it's no longer protected by any parent Gate. Same for any other action buttons in Teleported dialogs.

- [ ] **Step 6: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 7: Commit**

```bash
git add lcnc-webui/src/App.vue
git commit -m "refactor: migrate App.vue sidebar to Gate framework"
```

---

### Task 12: Migrate SettingsPanel

**Files:**
- Modify: `lcnc-webui/src/SettingsPanel.vue`

SettingsPanel already uses 7 `<fieldset :disabled="!can.X" class="fs-reset">` wrappers:
- Lines 692, 761, 971, 1000, 1113, 1196: `:disabled="!can.idle"`
- Line 836: `:disabled="!can.ready"` (toolsetter)

These are already Gate-equivalent. Replace with `<Gate>` for consistency.

Also has:
- Lines 1224, 1236: `{ inactive: !kbConfig.jogEnabled }` — NOT permission-based, this is app-state config. Keep as-is (inactive class stays for non-permission cases) OR convert to Gate with `:allow="kbConfig.jogEnabled"`.
- Line 1267: `:disabled="!can.idle"` on Reset Keyboard button — will be inside a Gate
- Line 1410: Reset confirm dialog — needs Teleport

Per Option A decision: all settings inputs get gated. The existing fieldsets already accomplish this. Just convert to Gate syntax.

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Replace 7 fieldset wrappers with Gate**

Replace each `<fieldset :disabled="!can.X" class="fs-reset">` with `<Gate :allow="can.X">`. Preserve any classes that were on the fieldset (pass as class on Gate).

- [ ] **Step 3: Handle keyboard jog inactive rows**

The `{ inactive: !kbConfig.jogEnabled }` on lines 1224, 1236 is not a permission gate — it's UI config. Decision: convert to `<Gate :allow="kbConfig.jogEnabled">` (consistent pattern) or keep as inactive class (since it's not a safety gate). Choose the approach that's cleaner.

- [ ] **Step 4: Add Teleport to reset confirm dialog**

Wrap dialog at line 1410 with `<Teleport to="body">`.

- [ ] **Step 5: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 6: Commit**

```bash
git add lcnc-webui/src/SettingsPanel.vue
git commit -m "refactor: migrate SettingsPanel to Gate framework"
```

---

### Task 13: Migrate GcodeReferenceDialog

**Files:**
- Modify: `lcnc-webui/src/GcodeReferenceDialog.vue:48-75`

Has:
- Line 54: `<div class="stack-controls refContent" :class="{ inactive: !can.idle }">` — replace with Gate
- Lines 60, 67, 70, 75: `:disabled="!can.idle"` on search input, sort headers, filter select — remove (redundant in Gate)

The dialog itself (line 48) renders inside App.vue — check if it's already in a Teleport or needs one.

- [ ] **Step 1: Add Gate import**

- [ ] **Step 2: Replace inactive wrapper with Gate**

Replace the inactive div with `<Gate :allow="can.idle" class="stack-controls refContent">`. Remove all 4 `:disabled="!can.idle"` bindings from children.

- [ ] **Step 3: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 4: Commit**

```bash
git add lcnc-webui/src/GcodeReferenceDialog.vue
git commit -m "refactor: migrate GcodeReferenceDialog to Gate framework"
```

---

### Task 14: Cleanup — remove .inactive, audit, update CLAUDE.md

**Files:**
- Modify: `lcnc-webui/src/style.css:664-665`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Verify no remaining .inactive usage**

Run: `grep -r "inactive" lcnc-webui/src/ --include="*.vue" --include="*.css"` to find any remaining uses. If the SettingsPanel keyboard config rows still use it, either convert or keep the class for non-permission use.

- [ ] **Step 2: Remove or keep .inactive class**

If `.inactive` is no longer used anywhere, remove it from `style.css:664-665`. If it's still used for non-permission cases (keyboard config), keep it but add a comment: `/* Non-permission UI state only — permission gating uses Gate.vue */`.

- [ ] **Step 3: Verify no orphaned `:disabled="!can.X"` bindings**

Run: `grep -rn ':disabled="!can\.' lcnc-webui/src/ --include="*.vue"`. Every remaining binding should be:
- JogButton `:disabled` props (intentionally preserved)
- App-state + permission combos where the permission is tighter than the parent Gate
- Elements in sections without a Gate (like GcodePanel's control row if kept as-is)

Verify each is intentional.

- [ ] **Step 4: Update CLAUDE.md**

In the Pre-Flight Checklist section, replace:
- `Every button/input/select gets :disabled="!can.<class>"` → `Every section with interactive controls is wrapped in <Gate :allow="can.X">. Individual :disabled only for app-state conditions.`
- Remove `.inactive` references from the checklist
- Add Gate.vue to the Key Patterns section with: `Gate.vue renders <fieldset :disabled="!allow"> — browser-enforced default-deny. Use #exempt slot for controls that must always work (Abort, E-Stop). See spec: docs/superpowers/specs/2026-03-24-gate-framework-design.md`

- [ ] **Step 5: Remove `fs-reset:disabled` opacity rule**

Remove `.fs-reset:disabled { opacity: var(--opacity-disabled); }` from `style.css:399-401`. This causes double-dimming: fieldset at 0.4 opacity × child button at 0.4 opacity = 0.16 effective opacity. Child elements handle their own disabled opacity via their individual `:disabled` CSS rules. The fieldset should be transparent — only its children dim.

- [ ] **Step 6: Verify build**

Run: `cd lcnc-webui && npm run build`

- [ ] **Step 7: Commit**

```bash
git add lcnc-webui/src/style.css CLAUDE.md
git commit -m "refactor: cleanup after Gate migration — remove .inactive, update docs"
```

---

### Task 15: Dev warning for ungated elements

**Files:**
- Modify: `lcnc-webui/src/main.ts` or create `lcnc-webui/src/gateAudit.ts`

Add a dev-mode check that warns when interactive elements render outside a `<fieldset>` ancestor.

- [ ] **Step 1: Implement the audit**

In development mode only (`import.meta.env.DEV`), use a MutationObserver on document.body that checks newly added `<button>`, `<input>`, `<select>` elements for a `<fieldset>` ancestor. If missing, log a console warning with the element and its component.

```ts
if (import.meta.env.DEV) {
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (!(node instanceof HTMLElement)) continue;
        const els = node.querySelectorAll('button, input, select, textarea');
        for (const el of els) {
          if (!el.closest('fieldset')) {
            console.warn('[Gate audit] Element outside fieldset:', el, el.closest('[data-v-app]'));
          }
        }
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}
```

Note: this will fire for intentionally ungated elements (tab buttons, popover triggers). Add a `data-gate-exempt` attribute to those elements to suppress the warning:

```ts
if (!el.closest('fieldset') && !el.hasAttribute('data-gate-exempt')) {
```

- [ ] **Step 2: Add `data-gate-exempt` to intentionally ungated elements**

Add the attribute to: TabPanel tab buttons, status popover triggers, DebugTab.vue diagnostic buttons (Start/Stop Log, Reset, Download CSV), and any other intentionally ungated buttons. This is documentation in code — it declares "I know this is outside a Gate and that's intentional."

- [ ] **Step 3: Verify warnings fire in dev mode, no warnings in production**

Start dev server, open console. Verify warnings appear for any remaining ungated elements. Verify `npm run build` produces no warnings.

- [ ] **Step 4: Commit**

```bash
git add lcnc-webui/src/main.ts
git commit -m "feat: add dev-mode audit warning for elements outside Gate"
```
