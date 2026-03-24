# Gate Framework — Fieldset-Based Permission Enforcement

**Date:** 2026-03-24
**Status:** Approved for implementation

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
- ~76 `:disabled="!can.X"` bindings where the permission matches the parent Gate
- 21 mixed-logic bindings simplify (e.g., `:disabled="!activeFile || !can.idle"` becomes `:disabled="!activeFile"`)

## Design

### Gate.vue

```vue
<script setup lang="ts">
const props = defineProps<{ allow: boolean }>();
</script>

<template>
  <fieldset :disabled="!allow" class="gate">
    <legend v-if="$slots.exempt" class="gate-exempt">
      <slot name="exempt" />
    </legend>
    <slot />
  </fieldset>
</template>

<style scoped>
.gate {
  border: none;
  margin: 0;
  padding: 0;
  min-inline-size: 0;
}
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
| Plain `<button>` (not Btn) | No change needed — native buttons are disabled by fieldset natively |

### Gate boundaries — what lives outside Gates

**Tab navigation:** TabPanel.vue tab buttons must NOT be inside a Gate — tabs are pure navigation. When wrapping a panel, the Gate goes around the panel *content*, not the tab bar.

**Status-display popovers:** Machine Status, Program Status, and Overrides popovers in the sidebar are read-only views. Their trigger buttons live outside any Gate so operators can always view machine status, even when disarmed.

**Dialog overlays:** Dialogs (`.dialogOverlay`) render at `position: fixed; z-index: 1000`. If a dialog is inside a Gate that becomes disabled while the dialog is open, the dialog's buttons would be disabled — stranding the user. Strategy: dialogs use `<Teleport to="body">` to render outside any Gate scope. Close/Cancel buttons are always accessible. Action buttons inside dialogs get their own Gate if they perform machine operations.

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

## Migration Strategy

### Phase 1 — Create Gate.vue + styles

- Create `Gate.vue` component
- Add fieldset reset styles to `style.css`
- Keep `.inactive` class alive during migration

### Phase 2 — Migrate components (simplest first)

Each migration: wrap section in Gate, remove redundant `:disabled="!can.X"` and `:class="{ inactive: ... }"`, keep application-state `:disabled`.

1. **DroPanel** — 1 inactive wrapper → `Gate :allow="can.idle"`
2. **JogPanel** — 1 inactive wrapper → `Gate :allow="can.jog"`, preserve JogButton `:disabled` props
3. **ManualPanel** — 4 inactive wrappers → Gates for `can.idle`, `can.ready`
4. **ToolTablePanel** — 1 inactive wrapper → `Gate :allow="can.idle"`, keep `:disabled="loading"` etc.
5. **Toolbar** — 29 `:disabled="!can.idle"` bindings → `Gate :allow="can.idle"`
6. **CameraViewer** — 6 `:disabled` bindings → Gate wrapping
7. **ProbePanel** — 13 inactive wrappers → Gates for `can.idle`, `can.ready`, `can.probe` (distinct class: adds eoffset check)
8. **GcodePanel** — mixed gates (ready + abort + override), needs exempt slot for Abort, nested Gate for M01/BD override buttons
9. **App.vue sidebar** — popover groups, offset table, tool dialog; status popovers stay outside Gates
10. **GcodeHUD / JogHUD / SetupHUD** — small HUD overlays
11. **SettingsPanel** — all inputs gated (option A: gate everything) → `Gate :allow="can.idle"`
12. **GcodeReferenceDialog** — 1 inactive wrapper → `Gate :allow="can.idle"`
13. **Dialogs** — add `<Teleport to="body">` to dialogs that render inside gated sections

### Phase 3 — Cleanup

- Remove `.inactive` class from `style.css`
- Remove orphaned `:disabled="!can.X"` bindings
- Update CLAUDE.md: replace Pre-Flight Checklist permission gate guidance (`:disabled="!can.X"` → Gate wrapping), remove `.inactive` references, document Gate.vue pattern
- Audit: grep for any `<button>` or `<input>` not inside a `<fieldset>` ancestor

### Phase 4 — Dev warning

- Console warning in dev mode when a `<button>` or `<input>` renders without a `<fieldset>` ancestor
- This is mandatory to maintain the fail-closed guarantee as new components are added

## Decisions Made

- **Option A for settings**: all settings inputs gated (can relax individually later with `gate="safe"` or exemptions)
- **Fieldset over provide/inject**: browser enforcement is structurally fail-closed; provide/inject is opt-in per element
- **Legend exception for exempt controls**: E-Stop and Abort use the `#exempt` slot
- **No changes to Btn.vue**: enforcement is at the Gate layer, not the component layer
- **No changes to permissions.ts**: Gate consumes existing permission classes
- **No useGate() composable initially**: fieldset handles all cases; add later if needed
