# Gate Audit & Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Gate.vue to accept a permission name string, render `data-gate` on its fieldset, and upgrade the existing dev-mode audit to validate all form elements are inside a Gate with a valid permission class — with red outlines + console warnings.

**Architecture:** Gate.vue changes from `<Gate :allow="can.idle">` to `<Gate gate="idle">` — it resolves the boolean internally via `usePermissions()`. The audit in main.ts is upgraded to check for `data-gate` with a valid permission name (not just any `<fieldset>`). `permissions.ts` exports `VALID_GATES` as the single source of truth.

**Tech Stack:** Vue 3, TypeScript, Vite (dev mode detection)

---

### Task 1: Export VALID_GATES from permissions.ts

**Files:**
- Modify: `lcnc-webui/src/permissions.ts`

- [ ] **Step 1: Add VALID_GATES set**

Add after the `Permissions` type definition (before `evaluatePermissions`):

```ts
/** Valid gate names — single source of truth for Gate.vue and dev-mode audit */
export const VALID_GATES: ReadonlySet<string> = new Set<keyof Permissions>([
  "always", "safety", "idle", "jog", "override",
  "ready", "pause", "resume", "abort", "probe", "zero",
]);
```

- [ ] **Step 2: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: clean build, no TS errors

- [ ] **Step 3: Commit**

```bash
git add lcnc-webui/src/permissions.ts
git commit -m "feat: export VALID_GATES set from permissions.ts"
```

---

### Task 2: Refactor Gate.vue to accept gate name string

**Files:**
- Modify: `lcnc-webui/src/Gate.vue`

Gate changes from `defineProps<{ allow: boolean }>()` to accepting a `gate` string prop. It uses `usePermissions()` to resolve the boolean internally and renders `data-gate` on the fieldset.

- [ ] **Step 1: Rewrite Gate.vue**

```vue
<script setup lang="ts">
import { computed } from "vue";
import { usePermissions, type Permissions } from "./permissions";

const props = defineProps<{ gate: keyof Permissions }>();
const permissions = usePermissions();
const allow = computed(() => permissions.value[props.gate]);
</script>

<template>
  <fieldset :disabled="!allow" :data-gate="gate" class="fs-reset">
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

- [ ] **Step 2: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: TS errors in files using the old `allow` prop — this is expected, we fix them in Task 3.

---

### Task 3: Update all Gate usages

**Files:**
- Modify: `lcnc-webui/src/App.vue`
- Modify: `lcnc-webui/src/GcodePanel.vue`
- Modify: `lcnc-webui/src/GcodeReferenceDialog.vue`
- Modify: `lcnc-webui/src/SettingsPanel.vue`

Every `<Gate :allow="...">` becomes `<Gate gate="...">`. App.vue uses `permissions.X`, child components use `can.X` — both become just the string name.

- [ ] **Step 1: Update App.vue (8 usages)**

| Old | New |
|-----|-----|
| `<Gate :allow="permissions.always"` | `<Gate gate="always"` |
| `<Gate :allow="permissions.safety"` | `<Gate gate="safety"` |
| `<Gate :allow="permissions.abort"` | `<Gate gate="abort"` |
| `<Gate :allow="permissions.ready"` | `<Gate gate="ready"` |

Apply to all 8 Gate instances in App.vue. Keep all other attributes (class, etc.) unchanged.

- [ ] **Step 2: Update GcodePanel.vue (1 usage)**

| Old | New |
|-----|-----|
| `<Gate :allow="can.ready"` | `<Gate gate="ready"` |

- [ ] **Step 3: Update GcodeReferenceDialog.vue (1 usage)**

| Old | New |
|-----|-----|
| `<Gate :allow="can.idle"` | `<Gate gate="idle"` |

- [ ] **Step 4: Update SettingsPanel.vue (1 usage)**

| Old | New |
|-----|-----|
| `<Gate :allow="can.idle"` | `<Gate gate="idle"` |

- [ ] **Step 5: Clean up unused imports**

In files that no longer reference `can` or `permissions` for Gate props, check if the import is still needed for other uses (e.g. `:disabled`, `:class` bindings). Only remove if truly unused.

- App.vue: `permissions` is still used for non-Gate bindings (e.g. `:jogDisabled="!permissions.jog"`, `:class` conditions) — keep it.
- GcodePanel.vue: check if `can` is used elsewhere — if only for Gate, remove `usePermissions` import.
- GcodeReferenceDialog.vue: check if `can` is used elsewhere.
- SettingsPanel.vue: check if `can` is used elsewhere.

- [ ] **Step 6: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: clean build, zero TS errors

- [ ] **Step 7: Commit**

```bash
git add lcnc-webui/src/Gate.vue lcnc-webui/src/App.vue lcnc-webui/src/GcodePanel.vue lcnc-webui/src/GcodeReferenceDialog.vue lcnc-webui/src/SettingsPanel.vue
git commit -m "refactor: Gate accepts gate name string, resolves permission internally"
```

---

### Task 4: Upgrade dev-mode audit in main.ts

**Files:**
- Modify: `lcnc-webui/src/main.ts`

Replace the existing audit code (lines 70–102) with the upgraded version that checks for `data-gate` with a valid permission name, adds red outlines, and uses MutationObserver for dynamic elements.

- [ ] **Step 1: Rewrite the dev-mode audit block**

Replace the existing `if (import.meta.env.DEV) { ... }` block with:

```ts
if (import.meta.env.DEV) {
  const { VALID_GATES } = await import('./permissions');

  function auditElement(el: HTMLElement) {
    const gate = el.closest('[data-gate]');
    if (gate && VALID_GATES.has(gate.getAttribute('data-gate')!)) return;

    el.style.outline = '3px solid red';
    el.title = 'UNGATED: not inside a <Gate> with a valid permission';
    console.warn(
      `[Gate audit] Ungated element:`,
      el,
      gate ? `(nearest data-gate="${gate.getAttribute('data-gate')}" is not a valid permission)` : '(no Gate ancestor)',
    );
  }

  function auditAll(root: Element | Document = document) {
    for (const el of root.querySelectorAll('button, input, select, textarea')) {
      auditElement(el as HTMLElement);
    }
  }

  // Audit initial DOM after mount settles
  setTimeout(() => {
    auditAll();

    // Watch for dynamically added elements (dialogs, popovers)
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (!(node instanceof HTMLElement)) continue;
          if (node.matches('button, input, select, textarea')) auditElement(node);
          auditAll(node);
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }, 0);
}
```

- [ ] **Step 2: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: clean build, zero TS errors

- [ ] **Step 3: Commit**

```bash
git add lcnc-webui/src/main.ts
git commit -m "feat: upgrade gate audit — check data-gate validity, red outlines + console.warn"
```

---

### Task 5: Fix ungated dialogs in ToolTablePanel.vue

**Files:**
- Modify: `lcnc-webui/src/ToolTablePanel.vue`

Three dialogs have ungated action sections. Add Gate import, usePermissions, and wrap each dialog's actions.

- [ ] **Step 1: Add imports**

Add to the imports at the top of the script:

```ts
import Gate from "./Gate.vue";
```

Note: `usePermissions` is NOT needed — Gate resolves permissions internally now.

- [ ] **Step 2: Wrap delete dialog actions**

Change (around line 434):
```vue
<div class="dialogActions">
  <MachineBtn type="dialogCancel" @click="cancelDelete">Cancel</MachineBtn>
```
To:
```vue
<Gate gate="idle" class="dialogActions">
  <MachineBtn type="dialogCancel" @click="cancelDelete">Cancel</MachineBtn>
```
And the closing `</div>` → `</Gate>`.

- [ ] **Step 3: Wrap edit/add dialog actions**

Change (around line 520):
```vue
<div class="editFooter">
```
To:
```vue
<Gate gate="idle" class="editFooter">
```
And the closing `</div>` → `</Gate>`.

- [ ] **Step 4: Wrap import preview dialog actions**

Change (around line 553):
```vue
<div class="dialogActions">
  <MachineBtn type="dialogCancel" @click="cancelImport">Cancel</MachineBtn>
```
To:
```vue
<Gate gate="idle" class="dialogActions">
  <MachineBtn type="dialogCancel" @click="cancelImport">Cancel</MachineBtn>
```
And the closing `</div>` → `</Gate>`.

- [ ] **Step 5: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: clean build, zero TS errors

- [ ] **Step 6: Commit**

```bash
git add lcnc-webui/src/ToolTablePanel.vue
git commit -m "fix: wrap ToolTablePanel dialog actions in Gate(idle)"
```

---

### Task 6: Fix ungated macro edit dialog in SettingsPanel.vue

**Files:**
- Modify: `lcnc-webui/src/SettingsPanel.vue`

- [ ] **Step 1: Wrap macro edit dialog actions**

Change (around line 1094):
```vue
<div class="macroEditActions">
```
To:
```vue
<Gate gate="idle" class="macroEditActions">
```
And the closing `</div>` → `</Gate>`.

Gate is already imported in SettingsPanel.vue (used at line 1397).

- [ ] **Step 2: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: clean build, zero TS errors

- [ ] **Step 3: Commit**

```bash
git add lcnc-webui/src/SettingsPanel.vue
git commit -m "fix: wrap SettingsPanel macro edit actions in Gate(idle)"
```

---

### Task 7: Fix hardcoded padding in importRow

**Files:**
- Modify: `lcnc-webui/src/ToolTablePanel.vue`

- [ ] **Step 1: Replace hardcoded padding**

Change:
```css
padding: 4px 8px;
```
To:
```css
padding: var(--gap-tight) var(--gap-controls);
```

- [ ] **Step 2: Verify build**

Run: `cd lcnc-webui && npm run build`
Expected: clean build

- [ ] **Step 3: Commit**

```bash
git add lcnc-webui/src/ToolTablePanel.vue
git commit -m "fix: replace hardcoded padding with spacing tokens in importRow"
```

---

### Task 8: Verify audit works in dev mode

- [ ] **Step 1: Start dev server**

Run: `cd lcnc-webui && npm run dev`

- [ ] **Step 2: Open browser and check console**

Open the dev URL. Open DevTools console. Expect to see `[Gate audit]` warnings for sidebar buttons (Arm, E-Stop, Machine On/Off) — these are intentionally outside Gate and will have red outlines. This confirms the audit is working.

- [ ] **Step 3: Verify no unexpected violations**

All buttons inside panels should NOT have red outlines (they're inside Gates). If any do, investigate and fix.
