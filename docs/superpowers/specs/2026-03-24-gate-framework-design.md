# Gate Framework — Fieldset-Based Permission Enforcement

**Date:** 2026-03-24
**Status:** Approved for implementation (revised 2026-03-25)

## Problem

The current permission system is fail-open. 214 Btn usages exist across the UI — 117 have no `:disabled` attribute, 53 native form inputs lack gates entirely. Every new button added without a gate defaults to enabled. The `.inactive` CSS class provides visual dimming but doesn't block interaction. Buttons slip through: Abort gets muted when it should be active, M01/Block Delete work when disarmed.

Safety-critical standards (IEC 62443, IEC 62366, DO-178C) require default-deny — an unconfigured control must be inert.

## Solution

Add a `Gate.vue` component that renders an HTML `<fieldset>` element. When the fieldset's `disabled` attribute is set, the browser natively disables all descendant form elements (buttons, inputs, selects, textareas) — no JavaScript gating needed.

This is an additive layer on top of the existing system. Nothing below changes:

- `permissions.ts` — unchanged (evaluatePermissions, usePermissions, permission classes)
- `Btn.vue` — unchanged (variants, sizes, active/muted states)
- `require_armed()` — unchanged (backend defense-in-depth)

What gets removed:

- `.inactive` class and all 26 `:class="{ inactive: !can.X }"` bindings
- `data-gate-exempt` attribute — eliminated entirely
- ~76 `:disabled="!can.X"` bindings where the permission matches the parent Gate
- 21 mixed-logic bindings simplify (e.g., `:disabled="!activeFile || !can.idle"` becomes `:disabled="!activeFile"`)

## Design

### Three mechanisms only

**1. `<Gate :allow="...">` — the zone.** Every interactive element lives inside one. No exceptions. Navigation sections use `<Gate :allow="true">` — always enabled, but the element is inside a fieldset and the audit is complete.

**2. `#exempt` slot — the escape hatch.** Only for controls that must work when the parent Gate is disabled. E-Stop, Abort. Uses the HTML `<legend>` spec. Visible, explicit, auditable.

**3. Individual `:disabled` — the refinement.** Only for conditions the parent Gate doesn't cover: app state (loading, no file, form validation) or tighter permissions than the parent.

No `data-gate-exempt`. No audit suppression. The dev audit is binary: inside a fieldset = pass, not inside a fieldset = bug.

### Gate.vue

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

### How it works

**Browser-enforced default-deny:** Every `<button>`, `<input>`, `<select>` inside a disabled fieldset is disabled by the browser. Click events are blocked before JavaScript sees them. Existing CSS (`button:disabled { opacity: var(--opacity-disabled) }`) fires automatically.

**Exempt slot (legend exception):** The HTML spec exempts form elements inside a fieldset's **first** `<legend>` element from being disabled. Only the first `<legend>` gets this exemption — do not add multiple `<legend>` elements. The `#exempt` slot exposes this for controls that must always work (Abort, E-Stop):

```vue
<Gate :allow="can.ready">
  <template #exempt>
    <Btn variant="danger" @click="abort">Abort</Btn>
  </template>
  <Btn @click="sendMdi">Send</Btn>
  <input type="number" v-model="rpm">
</Gate>
```

When `can.ready` is false: Send and the input are disabled by the browser. Abort stays enabled.

**Always-enabled Gate for navigation:** Pure UI navigation (tab buttons, view switchers, close buttons) uses `<Gate :allow="true">`. The fieldset is never disabled, but the element is inside a fieldset — the audit is complete and the intent is documented.

```vue
<Gate :allow="true">
  <Btn @click="switchTab('dro')">DRO</Btn>
  <Btn @click="switchTab('jog')">Jog</Btn>
</Gate>
```

**Tighten-only nesting (ARINC 661 pattern):** Fieldsets can nest. An inner fieldset cannot re-enable children when its parent is disabled — this is HTML spec behavior. A child Gate can only add more restrictions:

```vue
<Gate :allow="can.idle">           <!-- outer: idle required -->
  <Gate :allow="can.ready">        <!-- inner: ready required (tighter) -->
    <Btn @click="sendMdi">Send</Btn>
  </Gate>
  <Btn @click="home">Home</Btn>   <!-- only needs idle -->
</Gate>
```

**Application-state disabled:** Buttons that need extra conditions beyond the permission gate keep their `:disabled` prop. Both layers stack — fieldset disabled OR explicit disabled = button disabled:

```vue
<Gate :allow="can.idle">
  <Btn :disabled="!activeFile" @click="reload">Reload</Btn>
  <Btn :disabled="loading" @click="refresh">Refresh</Btn>
</Gate>
```

### What stays, what goes

| Current pattern | After migration |
|---|---|
| `<div :class="{ inactive: !can.ready }">` | `<Gate :allow="can.ready">` |
| `<Btn :disabled="!can.ready" @click="...">` (inside Gate) | `<Btn @click="...">` |
| `<Btn :disabled="!can.abort" @click="abort">` (mixed section) | `<template #exempt><Btn @click="abort">` |
| `<Btn :disabled="!activeFile \|\| !can.idle" @click="...">` | `<Btn :disabled="!activeFile" @click="...">` (inside `<Gate :allow="can.idle">`) |
| `<input type="number" v-model="x">` (ungated) | Lives inside a Gate — browser disables it |
| `.inactive { opacity: var(--opacity-disabled) }` | Removed — `:disabled` CSS handles it |
| `data-gate-exempt` on any element | Removed — replaced with `<Gate :allow="true">` or proper Gate |
| Plain `<button>` (not Btn) | No change needed — native buttons are disabled by fieldset natively |

### Section-by-section Gate assignments

Every section in the UI gets a Gate. This table is the single source of truth for what gate each section uses:

**Navigation sections (`<Gate :allow="true">`):**
- TabPanel.vue tab bar
- ManualPanel.vue view tabs (DRO/Jog/MDI)
- ProbePanel.vue view tabs (Outside/Inside/Boss/etc.)
- App.vue add panel button
- App.vue status banner (Refresh is page reload, not machine)
- GcodePanel.vue stats popover (display only)
- GcodePanel.vue error banner (dismiss is UI only)
- DebugTab.vue diagnostic buttons (dev-only, no machine actions)

**Machine control sections (permission-gated):**
- App.vue header right: `<Gate :allow="connected">`, Fullscreen in `#exempt`
- App.vue safety section: `<Gate :allow="connected">`, E-Stop in `#exempt`
- App.vue control openers: `<Gate :allow="permissions.abort">`
- App.vue spindle popover content: `<Gate :allow="permissions.ready">`
- App.vue coolant popover content: `<Gate :allow="permissions.ready">`
- App.vue overrides popover content: `<Gate :allow="permissions.override">`
- App.vue work offsets: `<Gate :allow="permissions.ready">`
- App.vue tool dialog actions: `<Gate :allow="permissions.ready">`, Abort in `#exempt`
- GcodePanel.vue file ops: `<Gate :allow="can.idle">`
- GcodePanel.vue control row: `<Gate :allow="can.abort">`, Abort in `#exempt`
- GcodePanel.vue file browser: `<Gate :allow="can.idle">`
- GcodePanel.vue edit area: `<Gate :allow="can.idle">`
- GcodeHUD.vue ctrl row: `<Gate :allow="can.abort">`, Abort in `#exempt`
- All other component sections: per existing migration (DroPanel, JogPanel, ManualPanel, etc.)

**Dialogs (inner Gates for action buttons):**
- Dialogs render at root template level or via `<Teleport to="body">` — already outside all fieldsets
- Close/Cancel buttons are always safe (pure UI)
- Action buttons get their own `:disabled` binding or inner Gate:
  - Tool change confirm: `:disabled="!armed"`
  - Macro execute: `:disabled="!permissions.ready"`
  - Shutdown confirm: `:disabled="!connected"`
  - Compensation toggle: `:disabled="!permissions.ready"`
  - Settings reset: `:disabled="!permissions.idle"`
  - Run-from-line: `:disabled="!can.ready"` (already present)

### JogButton.vue

JogButton.vue has its own `isDisabled` computed that checks `props.disabled` (Vue prop) plus velocity validity. The HTML fieldset `disabled` attribute blocks browser events but does NOT set the Vue prop. JogButton's `:disabled` prop must be preserved during migration — it feeds the JS-level pointer capture guard. The fieldset provides the outer enforcement layer; the prop provides the inner JS guard for edge cases (captured pointer events during state transitions).

### Properties that match safety standards

| Requirement | How fieldset meets it |
|---|---|
| Default-deny (IEC 62443, IEC 62366, DO-178C) | Browser disables all descendants — no opt-in needed |
| Cascade (parent disables children) | Native fieldset behavior |
| Tighten-only (ARINC 661) | Nested fieldset cannot re-enable — HTML spec |
| Visual follows interaction | `:disabled` CSS fires automatically |
| Event blocking | Browser blocks before JS — not bypassable |
| Defense in depth (DO-178C) | Layer 1: fieldset. Layer 2: require_armed() backend |
| Fail-closed for new elements | New button in fieldset = disabled by default |
| No escape hatch abuse | `data-gate-exempt` eliminated — audit is binary |

## Migration Strategy

### Phase 1 — Create Gate.vue + styles

- Create `Gate.vue` component
- Add fieldset reset styles to `style.css`
- Keep `.inactive` class alive during migration

### Phase 2 — Migrate components (simplest first)

Each migration: wrap section in Gate, remove redundant `:disabled="!can.X"` and `:class="{ inactive: ... }"`, keep application-state `:disabled`.

1. **DroPanel** — 1 inactive wrapper → `Gate :allow="can.idle"`
2. **JogPanel** — 1 inactive wrapper → `Gate :allow="can.jog"`, preserve JogButton `:disabled` props
3. **ManualPanel** — 4 inactive wrappers → Gates for `can.idle`, `can.ready`; view tabs → `Gate :allow="true"`
4. **ToolTablePanel** — 1 inactive wrapper → `Gate :allow="can.idle"`, keep `:disabled="loading"` etc.
5. **Toolbar** — 29 `:disabled="!can.idle"` bindings → `Gate :allow="can.idle"`
6. **CameraViewer** — 6 `:disabled` bindings → Gate wrapping
7. **ProbePanel** — 13 inactive wrappers → Gates for `can.idle`, `can.ready`, `can.probe`; view tabs → `Gate :allow="true"`
8. **GcodePanel** — control row → `Gate :allow="can.abort"` with Abort in `#exempt`; file browser/edit → `Gate :allow="can.idle"`; stats/error → `Gate :allow="true"`
9. **GcodeHUD** — ctrl row → `Gate :allow="can.abort"` with Abort in `#exempt`
10. **JogHUD / SetupHUD** — Gate wrapping with appropriate permissions
11. **App.vue sidebar** — safety → `Gate :allow="connected"` with E-Stop in `#exempt`; control openers → `Gate :allow="permissions.abort"`; popover content already gated; header → `Gate :allow="connected"`
12. **App.vue dialogs** — remove `data-gate-exempt`, add `:disabled` to unprotected action buttons
13. **SettingsPanel** — existing fieldsets → Gates; dialog → remove `data-gate-exempt`
14. **GcodeReferenceDialog** — 1 inactive wrapper → Gate; remove `data-gate-exempt`
15. **TabPanel** — tab bar → `Gate :allow="true"`
16. **DebugTab** — buttons → `Gate :allow="true"`

### Phase 3 — Cleanup

- Remove `.inactive` class from `style.css`
- Remove `data-gate-exempt` check from dev audit (simplify to just `!el.closest('fieldset')`)
- Remove orphaned `:disabled="!can.X"` bindings
- Remove `.fs-reset:disabled { opacity }` rule (prevents double-dimming)
- Update CLAUDE.md
- Final audit: grep for any `data-gate-exempt` remaining (should be zero)
- Final audit: grep for any interactive element outside a fieldset

### Phase 4 — Dev warning (simplified)

Dev-mode MutationObserver checks: is this `<button>`/`<input>`/`<select>` inside a `<fieldset>`? No → console warning. That's it. No `data-gate-exempt` check. Binary pass/fail.

## Decisions Made

- **Option A for settings**: all settings inputs gated (can relax individually later)
- **Fieldset over provide/inject**: browser enforcement is structurally fail-closed; provide/inject is opt-in per element
- **Legend exception for exempt controls**: E-Stop and Abort use the `#exempt` slot
- **No `data-gate-exempt`**: eliminated entirely — replaced with `<Gate :allow="true">` for navigation or proper Gates for machine actions
- **No changes to Btn.vue**: enforcement is at the Gate layer, not the component layer
- **No changes to permissions.ts**: Gate consumes existing permission classes
- **No useGate() composable initially**: fieldset handles all cases; add later if needed
- **Dialogs use inner Gates**: not `data-gate-exempt` — action buttons get `:disabled` bindings or inner Gates
- **Three mechanisms only**: Gate (zone), `#exempt` (escape hatch), `:disabled` (refinement) — nothing else
