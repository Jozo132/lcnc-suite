# Keyboard Settings Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded keyboard shortcuts in App.vue with a user-configurable key binding system, exposed through a new Keyboard settings tab.

**Architecture:** Flat key map (`Record<KeyboardAction, string>`) in `defaults.ts`, server-synced. App.vue builds a reverse `Map<string, KeyboardAction>` for O(1) lookup in `onKeyDown`/`onKeyUp`. SettingsPanel gets a Keyboard tab with toggles and a click-to-capture key binding table.

**Tech Stack:** Vue 3 + TypeScript, existing `defaults.ts` section registry, server-synced settings via WebSocket

**Spec:** `docs/superpowers/specs/2026-03-21-keyboard-settings-tab-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `lcnc-webui/src/defaults.ts` | Modify | Add `KeyboardAction` type, `KeyboardDefaults` interface, `KEYBOARD_ACTION_LABELS`, `DEFAULT_KB_MAPPING`, `KEYBOARD_FALLBACK`, `formatKeyName()`, section registration, load/save. Add `"keyboard"` to `SERVER_SECTIONS`. |
| `lcnc-webui/src/main.ts` | Modify | Add `"keyboard"` to `SERVER_SECTIONS` array |
| `lcnc-webui/src/JogPanel.vue` | Modify | Replace key-based `KEY_SECTOR_MAP` + `rotaryKeyMap` with action-based `ACTION_SECTOR_MAP`. Rename prop `activeJogKeys` → `activeJogActions`. Update all `:active` bindings and `isSectorActive()`. Update hint text. |
| `lcnc-webui/src/ManualPanel.vue` | Modify | Rename prop `activeJogKeys` → `activeJogActions` (pass-through) |
| `lcnc-webui/src/App.vue` | Modify | Replace hardcoded `JOG_KEY_MAP`, `ROTARY_KEY_PAIRS`, `rotaryJogKeys`, inline key checks with config-driven `reverseKeyMap`. Add `keyboardConfig` ref, prop/emit, `settingsVersion` watcher, safety-on-disable. Rename `jogKeys` to store actions. Fix `isInputFocused()`. |
| `lcnc-webui/src/SettingsPanel.vue` | Modify | Add Keyboard tab (toggles + key binding table + capture UI). Remove Keyboard Jogging section from Machine tab. |

---

## Task 1: Data Model in defaults.ts

**Files:**
- Modify: `lcnc-webui/src/defaults.ts` (after gamepad section, ~line 585)
- Modify: `lcnc-webui/src/main.ts:7`

- [ ] **Step 1: Add KeyboardAction type, labels, default mapping, and interface**

Add after the gamepad section (after `saveGamepadDefaults`):

```typescript
// ── Keyboard shortcuts ──────────────────────────────────────────

export type KeyboardAction =
  | "jog_x+" | "jog_x-" | "jog_y+" | "jog_y-" | "jog_z+" | "jog_z-"
  | "jog_a+" | "jog_a-" | "jog_b+" | "jog_b-"
  | "estop" | "cycle" | "abort";

export const KEYBOARD_ACTION_LABELS: Record<KeyboardAction, string> = {
  "jog_x+": "Jog X+", "jog_x-": "Jog X-",
  "jog_y+": "Jog Y+", "jog_y-": "Jog Y-",
  "jog_z+": "Jog Z+", "jog_z-": "Jog Z-",
  "jog_a+": "Jog A+", "jog_a-": "Jog A-",
  "jog_b+": "Jog B+", "jog_b-": "Jog B-",
  estop: "E-Stop",
  cycle: "Cycle Start / Pause / Resume",
  abort: "Abort",
};

const ALL_KB_ACTIONS = Object.keys(KEYBOARD_ACTION_LABELS) as KeyboardAction[];

export const DEFAULT_KB_MAPPING: Record<KeyboardAction, string> = {
  "jog_x+": "ArrowRight", "jog_x-": "ArrowLeft",
  "jog_y+": "ArrowUp",    "jog_y-": "ArrowDown",
  "jog_z+": "PageUp",     "jog_z-": "PageDown",
  "jog_a+": "]",           "jog_a-": "[",
  "jog_b+": "'",           "jog_b-": ";",
  estop: "Escape",
  cycle: " ",
  abort: "Backspace",
};

export interface KeyboardDefaults {
  enabled: boolean;
  jogEnabled: boolean;
  mapping: Record<KeyboardAction, string>;
}

const KEYBOARD_FALLBACK: KeyboardDefaults = {
  enabled: true,
  jogEnabled: false,
  mapping: { ...DEFAULT_KB_MAPPING },
};
```

- [ ] **Step 2: Add formatKeyName() utility**

```typescript
const KEY_DISPLAY: Record<string, string> = {
  ArrowRight: "→", ArrowLeft: "←", ArrowUp: "↑", ArrowDown: "↓",
  " ": "Space", Escape: "Esc", Backspace: "⌫",
  PageUp: "PgUp", PageDown: "PgDn",
  Delete: "Del", Insert: "Ins",
  Enter: "Enter", Home: "Home", End: "End",
};

export function formatKeyName(key: string): string {
  if (!key) return "None";
  if (KEY_DISPLAY[key]) return KEY_DISPLAY[key];
  if (key.length === 1) return key.toUpperCase();
  return key;
}
```

- [ ] **Step 3: Add section registration, load/save, and SERVER_SECTIONS entry**

```typescript
registerSection<KeyboardDefaults>("keyboard", KEYBOARD_FALLBACK, (saved, fb) => {
  if (!saved) return { ...fb, mapping: { ...fb.mapping } };
  const mapping = { ...fb.mapping };
  if (saved.mapping && typeof saved.mapping === "object") {
    for (const action of ALL_KB_ACTIONS) {
      if (typeof saved.mapping[action] === "string") {
        mapping[action] = saved.mapping[action];
      }
    }
  }
  return {
    enabled: saved.enabled ?? fb.enabled,
    jogEnabled: saved.jogEnabled ?? fb.jogEnabled,
    mapping,
  };
});

export function loadKeyboardDefaults(): KeyboardDefaults {
  return loadSection<KeyboardDefaults>("keyboard");
}

export function saveKeyboardDefaults(data: KeyboardDefaults): void {
  saveSection("keyboard", data);
}
```

Add `"keyboard"` to the `SERVER_SECTIONS` Set at line 74 of `defaults.ts`:

```typescript
const SERVER_SECTIONS = new Set(["macros", "machine", "camera", "mdi", "gamepad", "probe", "toolsetter", "keyboard"]);
```

- [ ] **Step 4: Add "keyboard" to SERVER_SECTIONS in main.ts**

At `main.ts:7`, add `"keyboard"`:

```typescript
const SERVER_SECTIONS = ["macros", "machine", "camera", "mdi", "gamepad", "probe", "toolsetter", "keyboard"];
```

- [ ] **Step 5: Build verification**

Run: `cd lcnc-webui && npm run build`
Expected: zero errors, clean build

- [ ] **Step 6: Commit**

```bash
git add lcnc-webui/src/defaults.ts lcnc-webui/src/main.ts
git commit -m "feat: add KeyboardDefaults data model and section registration"
```

---

## Task 2: JogPanel — action-based active highlighting

**Files:**
- Modify: `lcnc-webui/src/JogPanel.vue:24,41-49,148-162,323-324,332-333,338-339,347`
- Modify: `lcnc-webui/src/ManualPanel.vue:33,197`

- [ ] **Step 1: Rename prop and replace KEY_SECTOR_MAP in JogPanel.vue**

In props (line 24), rename:
```typescript
// old
activeJogKeys?: Set<string>;
// new
activeJogActions?: Set<string>;
```

Replace `KEY_SECTOR_MAP` and `rotaryKeyMap` and `ROTARY_KEY_PAIRS` (lines 41-49, 148-153) with `ACTION_SECTOR_MAP`:

```typescript
const ACTION_SECTOR_MAP: Record<string, string> = {
  "jog_x+": "xp",
  "jog_x-": "xn",
  "jog_y+": "yp",
  "jog_y-": "yn",
};
```

Remove `ROTARY_KEY_PAIRS` (line 41) and `rotaryKeyMap` computed (lines 42-49).

- [ ] **Step 2: Update isSectorActive()**

Replace `isSectorActive` (lines 155-162):

```typescript
function isSectorActive(id: string): boolean {
  if (activeSectors.has(id)) return true;
  if (!props.activeJogActions) return false;
  for (const [action, sectorId] of Object.entries(ACTION_SECTOR_MAP)) {
    if (sectorId === id && props.activeJogActions.has(action)) return true;
  }
  return false;
}
```

- [ ] **Step 3: Update Z and rotary :active bindings**

Z buttons (lines 323-324):
```vue
<JogButton :axis="2" :dir="1" label="Z+" :vel="jogVel" :disabled="!can.jog" direction="up" :active="activeJogActions?.has('jog_z+')" :jogIncrement="jogIncrement" />
<JogButton :axis="2" :dir="-1" label="Z-" :vel="jogVel" :disabled="!can.jog" direction="down" :active="activeJogActions?.has('jog_z-')" :jogIncrement="jogIncrement" />
```

ABC rotary buttons (lines 332-333) — derive the action name from the axis letter:
```vue
<JogButton :axis="ra.index" :dir="-1" :label="ra.letter + '-'" :vel="angularJogVel" :disabled="!can.jog" direction="left" :jogIncrement="jogIncrement" :active="activeJogActions?.has('jog_' + ra.letter.toLowerCase() + '-')" />
<JogButton :axis="ra.index" :dir="1" :label="ra.letter + '+'" :vel="angularJogVel" :disabled="!can.jog" direction="right" :jogIncrement="jogIncrement" :active="activeJogActions?.has('jog_' + ra.letter.toLowerCase() + '+')" />
```

UVW buttons (lines 338-339) — same pattern:
```vue
<JogButton :axis="ra.index" :dir="-1" :label="ra.letter + '-'" :vel="jogVel" :disabled="!can.jog" direction="left" :jogIncrement="jogIncrement" :active="activeJogActions?.has('jog_' + ra.letter.toLowerCase() + '-')" />
<JogButton :axis="ra.index" :dir="1" :label="ra.letter + '+'" :vel="jogVel" :disabled="!can.jog" direction="right" :jogIncrement="jogIncrement" :active="activeJogActions?.has('jog_' + ra.letter.toLowerCase() + '+')" />
```

- [ ] **Step 4: Update hint text (line 347)**

Remove the hardcoded key hint. The hint no longer shows specific keys since they're configurable:
```vue
<div class="hint">
  {{ jogIncrement > 0 ? 'Click to jog one step.' : 'Press and hold to jog.' }} {{ isTeleop ? 'World mode: coordinated Cartesian movement.' : 'Joint mode: individual axis control.' }}
</div>
```

- [ ] **Step 5: Rename prop in ManualPanel.vue**

In props (line 33):
```typescript
// old
activeJogKeys?: Set<string>;
// new
activeJogActions?: Set<string>;
```

In template (line 197):
```vue
<!-- old -->
:activeJogKeys="activeJogKeys"
<!-- new -->
:activeJogActions="activeJogActions"
```

- [ ] **Step 6: Do NOT commit yet**

Build will fail because App.vue still passes `activeJogKeys`. Do not commit — continue directly to Task 3. Tasks 2 and 3 will be committed together after Task 3 completes.

---

## Task 3: App.vue — config-driven keyboard handling

**Files:**
- Modify: `lcnc-webui/src/App.vue`

This is the core refactor. Replace all hardcoded key handling with the config-driven reverse map.

- [ ] **Step 1: Add imports and keyboard config ref**

Add imports at top of `<script setup>`:
```typescript
import { loadKeyboardDefaults, saveKeyboardDefaults, type KeyboardDefaults, type KeyboardAction } from "./defaults";
```

Add the ref near `gamepadConfig` (around line 1023):
```typescript
const keyboardConfig = ref<KeyboardDefaults>(loadKeyboardDefaults());
```

- [ ] **Step 2: Add reverseKeyMap computed**

Add near the keyboard config ref:
```typescript
const reverseKeyMap = computed(() => {
  const map = new Map<string, KeyboardAction>();
  for (const [action, key] of Object.entries(keyboardConfig.value.mapping)) {
    if (key) map.set(key, action as KeyboardAction);
  }
  return map;
});
```

- [ ] **Step 3: Change jogKeys to store actions, add axis lookup**

Replace `const jogKeys = reactive(new Set<string>());` with:
```typescript
const jogActions = reactive(new Set<string>());
```

Add a helper to map jog actions to axis/dir (replaces `JOG_KEY_MAP` + `rotaryJogKeys`):
```typescript
const ANGULAR_LETTERS = new Set(["A", "B", "C"]);

function jogActionToAxis(action: string): { axis: number; dir: 1 | -1; isAngular: boolean } | null {
  const match = action.match(/^jog_([a-z])([+-])$/);
  if (!match) return null;
  const letter = match[1]!.toUpperCase();
  const dir = match[2] === "+" ? 1 : -1;
  const idx = axes.value.indexOf(letter);
  if (idx < 0) return null;
  return { axis: idx, dir, isAngular: ANGULAR_LETTERS.has(letter) };
}
```

- [ ] **Step 4: Replace onKeyDown**

Remove the old `onKeyDown` (lines 1057-1103) and replace with:

```typescript
function onKeyDown(e: KeyboardEvent) {
  const action = reverseKeyMap.value.get(e.key);
  if (!action) return;

  // E-Stop always fires — bypasses master toggle and input focus
  if (action === "estop") {
    e.preventDefault();
    if (canEstop.value) send({ cmd: "estop" });
    else if (canResetEstop.value) send({ cmd: "estop_reset" });
    return;
  }

  if (!keyboardConfig.value.enabled) return;
  if (isInputFocused()) return;

  // Jog actions
  if (action.startsWith("jog_")) {
    if (!keyboardConfig.value.jogEnabled) return;
    e.preventDefault();
    if (e.repeat || jogActions.has(action)) return;
    if (!permissions.value.jog) return;
    const jog = jogActionToAxis(action);
    if (!jog) return;
    jogActions.add(action);
    const vel = (jog.isAngular ? angularJogVel.value : jogVel.value) * jog.dir;
    if (jogIncrement.value > 0) {
      send({ cmd: "jog_incr", axis: jog.axis, vel, distance: jogIncrement.value * jog.dir });
    } else {
      send({ cmd: "jog_cont", axis: jog.axis, vel });
    }
    return;
  }

  // Cycle start / pause / resume
  if (action === "cycle") {
    e.preventDefault();
    if (permissions.value.resume) fire({ cmd: "cycle_resume" });
    else if (permissions.value.pause) fire({ cmd: "cycle_pause" });
    else if (permissions.value.ready && !!activeFile.value) fire({ cmd: "cycle_start" });
    return;
  }

  // Abort
  if (action === "abort") {
    e.preventDefault();
    if (permissions.value.abort) fire({ cmd: "abort" });
    return;
  }
}
```

- [ ] **Step 5: Replace onKeyUp**

Remove old `onKeyUp` (lines 1105-1113) and replace:

```typescript
function onKeyUp(e: KeyboardEvent) {
  const action = reverseKeyMap.value.get(e.key);
  if (!action || !action.startsWith("jog_")) return;
  if (jogActions.has(action)) {
    jogActions.delete(action);
    if (jogIncrement.value <= 0) {
      const jog = jogActionToAxis(action);
      if (jog) send({ cmd: "jog_stop", axis: jog.axis });
    }
  }
}
```

- [ ] **Step 6: Update stopAllJog()**

In `stopAllJog()` (line ~1149), change `jogKeys.clear()` to `jogActions.clear()`.

- [ ] **Step 7: Fix isInputFocused()**

Add `isContentEditable` check:
```typescript
function isInputFocused(): boolean {
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!(el as HTMLElement).isContentEditable;
}
```

- [ ] **Step 8: Remove old hardcoded keyboard constants**

Delete (around lines 1024-1050):
- `JOG_KEY_MAP` constant
- `ROTARY_LETTERS` constant
- `ROTARY_KEY_PAIRS` constant
- `rotaryJogKeys` computed

Delete (line 1023):
- `keyboardJogEnabled` ref

- [ ] **Step 9: Update settingsVersion watcher**

In the `watch(settingsVersion, ...)` block (around line 1139), add:
```typescript
keyboardConfig.value = loadKeyboardDefaults();
```

Remove:
```typescript
keyboardJogEnabled.value = mach.keyboardJog;
```

- [ ] **Step 10: Add safety-on-disable watcher**

Add a watcher for keyboard config changes:
```typescript
watch(() => [keyboardConfig.value.enabled, keyboardConfig.value.jogEnabled], (curr, prev) => {
  if (!prev) return; // first call, no previous values
  const [enabled, jogEnabled] = curr;
  const [prevEnabled, prevJogEnabled] = prev;
  if ((!enabled && prevEnabled) || (!jogEnabled && prevJogEnabled)) {
    stopAllJog();
  }
});
```

- [ ] **Step 11: Add setKeyboardConfig handler and props/emits**

Add the handler:
```typescript
function setKeyboardConfig(cfg: KeyboardDefaults) {
  keyboardConfig.value = cfg;
  saveKeyboardDefaults(cfg);
}
```

Update SettingsPanel in the template — add props and emit:
```vue
:keyboardConfig="keyboardConfig"
@setKeyboardConfig="setKeyboardConfig"
```

Remove the old `@setKeyboardJog` emit:
```
@setKeyboardJog="keyboardJogEnabled = $event"  <!-- DELETE THIS LINE -->
```

- [ ] **Step 12: Update activeJogKeys → activeJogActions in template**

Find where `jogKeys` is passed to ManualPanel (line ~1745):
```vue
<!-- old -->
:activeJogKeys="jogKeys"
<!-- new -->
:activeJogActions="jogActions"
```

Also update any direct pass to JogPanel if it exists outside ManualPanel.

- [ ] **Step 13: Build verification**

Run: `cd lcnc-webui && npm run build`
Expected: may still fail due to SettingsPanel expecting `keyboardConfig` prop — proceed to next task

- [ ] **Step 14: Commit (includes Task 2 changes)**

```bash
git add lcnc-webui/src/App.vue lcnc-webui/src/JogPanel.vue lcnc-webui/src/ManualPanel.vue
git commit -m "refactor: config-driven keyboard shortcuts with reverse key map

Replace hardcoded JOG_KEY_MAP, ROTARY_KEY_PAIRS, and inline key checks
with config-driven reverseKeyMap lookup. JogPanel uses action-based
active highlighting instead of key-based."
```

---

## Task 4: SettingsPanel — Keyboard tab

**Files:**
- Modify: `lcnc-webui/src/SettingsPanel.vue`

- [ ] **Step 1: Add Keyboard tab to tab list and add prop/emit**

In the `subTabs` array (line ~387), add `keyboard` before `hal`:
```typescript
{ id: "keyboard", label: "Keyboard" },
```

Add prop:
```typescript
keyboardConfig?: KeyboardDefaults;
```

Add import:
```typescript
import { loadKeyboardDefaults, saveKeyboardDefaults, type KeyboardDefaults, type KeyboardAction, KEYBOARD_ACTION_LABELS, DEFAULT_KB_MAPPING, formatKeyName } from "./defaults";
```

Add emit:
```typescript
(e: "setKeyboardConfig", cfg: KeyboardDefaults): void;
```

- [ ] **Step 2: Add keyboard reactive state**

Add script-level state for the keyboard tab:

```typescript
// ── Keyboard tab state ──
const kbConfig = ref<KeyboardDefaults>(props.keyboardConfig ?? loadKeyboardDefaults());
const listeningAction = ref<KeyboardAction | null>(null);
const captureError = ref("");
let captureErrorTimer: ReturnType<typeof setTimeout> | null = null;

// Sync from prop changes
watch(() => props.keyboardConfig, (cfg) => {
  if (cfg) kbConfig.value = cfg;
});

function saveKb() {
  emit("setKeyboardConfig", { ...kbConfig.value, mapping: { ...kbConfig.value.mapping } });
}
```

- [ ] **Step 3: Add key capture logic**

```typescript
// Jog actions for dimming when jogEnabled is off
const JOG_ACTIONS = new Set<KeyboardAction>(["jog_x+","jog_x-","jog_y+","jog_y-","jog_z+","jog_z-","jog_a+","jog_a-","jog_b+","jog_b-"]);

// Actions to show in the key binding table
// (rotary actions filtered dynamically based on machine axes)
const COMMAND_ACTIONS: KeyboardAction[] = ["estop", "cycle", "abort"];
const LINEAR_JOG_ACTIONS: KeyboardAction[] = ["jog_x+", "jog_x-", "jog_y+", "jog_y-", "jog_z+", "jog_z-"];
const ROTARY_JOG_ACTIONS: KeyboardAction[] = ["jog_a+", "jog_a-", "jog_b+", "jog_b-"];

// Modifier keys to reject
const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta"]);

function startCapture(action: KeyboardAction) {
  listeningAction.value = action;
  captureError.value = "";
  if (captureErrorTimer) clearTimeout(captureErrorTimer);
}

function handleCapture(e: KeyboardEvent) {
  if (!listeningAction.value) return;
  e.preventDefault();
  e.stopPropagation();

  // Reject modifier-only keys
  if (MODIFIER_KEYS.has(e.key)) return;

  // Reject Tab
  if (e.key === "Tab") {
    showCaptureError("Tab cannot be bound");
    return;
  }

  // Duplicate check
  const existing = Object.entries(kbConfig.value.mapping).find(
    ([a, k]) => k === e.key && a !== listeningAction.value
  );
  if (existing) {
    showCaptureError(`Already bound to ${KEYBOARD_ACTION_LABELS[existing[0] as KeyboardAction]}`);
    return;
  }

  // Accept the key
  kbConfig.value.mapping[listeningAction.value] = e.key;
  listeningAction.value = null;
  saveKb();
}

function showCaptureError(msg: string) {
  captureError.value = msg;
  if (captureErrorTimer) clearTimeout(captureErrorTimer);
  captureErrorTimer = setTimeout(() => { captureError.value = ""; }, 2000);
}

function unbindKey(action: KeyboardAction) {
  kbConfig.value.mapping[action] = "";
  saveKb();
}

function cancelCapture() {
  listeningAction.value = null;
  captureError.value = "";
}

function resetKeyboard() {
  kbConfig.value = {
    enabled: true,
    jogEnabled: false,
    mapping: { ...DEFAULT_KB_MAPPING },
  };
  saveKb();
}
```

- [ ] **Step 4: Add capture event listeners**

In the component, add lifecycle hooks for the capture listener:

```typescript
function onCaptureKeydown(e: KeyboardEvent) {
  if (listeningAction.value) handleCapture(e);
}

function onClickOutside(e: MouseEvent) {
  if (!listeningAction.value) return;
  const target = e.target as HTMLElement;
  if (!target.closest(".kbKeyCell")) cancelCapture();
}

onMounted(() => {
  window.addEventListener("keydown", onCaptureKeydown, true); // capturing phase
  window.addEventListener("click", onClickOutside);
});

onUnmounted(() => {
  window.removeEventListener("keydown", onCaptureKeydown, true);
  window.removeEventListener("click", onClickOutside);
});
```

- [ ] **Step 5: Add reset handler to resetConfirm()**

Add `keyboard` to the `resetActions` record and `resetLabels`:

```typescript
// In resetActions record, add:
keyboard: resetKeyboard,

// In resetLabels record, add:
keyboard: "Keyboard",
```

- [ ] **Step 6: Remove Keyboard Jogging section from Machine tab**

Delete the Keyboard Jogging section from the Machine tab template (around line 810):
```vue
<!-- DELETE this entire section -->
<div class="sep"></div>
<div class="section">
  <div class="sub">Keyboard Jogging</div>
  <div class="settingDesc">Allow arrow keys, Page Up/Down, and bracket keys to jog axes.</div>
  <label class="toggleRow">
    <input type="checkbox" class="toggle" v-model="keyboardJog" @change="emit('setKeyboardJog', keyboardJog); saveMachine()" />
    Enable keyboard jogging
  </label>
</div>
```

Remove the `setKeyboardJog` emit declaration, the `keyboardJog` ref, and any related watcher lines that sync `keyboardJog`.

- [ ] **Step 7: Add Keyboard tab template**

Add the template slot after `#gamepad`:

```vue
<template #keyboard>
  <div class="stack-panel scrollContent scroll-thin">
    <fieldset :disabled="!can.idle" class="fs-reset">
      <div class="section">
        <div class="sub">Keyboard Shortcuts</div>
        <div class="settingDesc">Allow keyboard keys to control the machine. When disabled, no keyboard shortcuts are active except E-Stop.</div>
        <label class="toggleRow">
          <input type="checkbox" class="toggle" v-model="kbConfig.enabled" @change="saveKb()" />
          Enable keyboard shortcuts
        </label>
      </div>

      <template v-if="kbConfig.enabled">
        <div class="sep"></div>

        <div class="section">
          <div class="sub">Keyboard Jogging</div>
          <div class="settingDesc">Allow jog keys to move axes.</div>
          <label class="toggleRow">
            <input type="checkbox" class="toggle" v-model="kbConfig.jogEnabled" @change="saveKb()" />
            Enable keyboard jogging
          </label>
        </div>

        <div class="sep"></div>

        <div class="section">
          <div class="sub">Key Bindings</div>
          <table class="kbMapTable">
            <tbody>
              <tr v-for="action in LINEAR_JOG_ACTIONS" :key="action" :class="{ inactive: !kbConfig.jogEnabled }">
                <td class="kbMapAction">{{ KEYBOARD_ACTION_LABELS[action] }}</td>
                <td class="kbKeyCell"
                    :class="{ listening: listeningAction === action }"
                    @click="startCapture(action)">
                  {{ listeningAction === action ? 'Press a key...' : formatKeyName(kbConfig.mapping[action]) }}
                </td>
                <td class="kbUnbind">
                  <Btn icon v-if="kbConfig.mapping[action]" @click.stop="unbindKey(action)" title="Unbind">&times;</Btn>
                </td>
              </tr>
              <tr v-for="action in ROTARY_JOG_ACTIONS" :key="action" :class="{ inactive: !kbConfig.jogEnabled }">
                <td class="kbMapAction">{{ KEYBOARD_ACTION_LABELS[action] }}</td>
                <td class="kbKeyCell"
                    :class="{ listening: listeningAction === action }"
                    @click="startCapture(action)">
                  {{ listeningAction === action ? 'Press a key...' : formatKeyName(kbConfig.mapping[action]) }}
                </td>
                <td class="kbUnbind">
                  <Btn icon v-if="kbConfig.mapping[action]" @click.stop="unbindKey(action)" title="Unbind">&times;</Btn>
                </td>
              </tr>
              <tr class="kbSep"><td colspan="3"></td></tr>
              <tr v-for="action in COMMAND_ACTIONS" :key="action">
                <td class="kbMapAction">{{ KEYBOARD_ACTION_LABELS[action] }}</td>
                <td class="kbKeyCell"
                    :class="{ listening: listeningAction === action }"
                    @click="startCapture(action)">
                  {{ listeningAction === action ? 'Press a key...' : formatKeyName(kbConfig.mapping[action]) }}
                </td>
                <td class="kbUnbind">
                  <Btn icon v-if="kbConfig.mapping[action]" @click.stop="unbindKey(action)" title="Unbind">&times;</Btn>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="captureError" class="kbCaptureError">{{ captureError }}</div>
        </div>
      </template>

      <div class="resetRow">
        <Btn variant="danger" :disabled="!can.idle" @click="resetTarget = 'keyboard'">Reset Keyboard</Btn>
      </div>
    </fieldset>
  </div>
</template>
```

- [ ] **Step 8: Add scoped CSS for Keyboard tab**

Add after the gamepad CSS section:

```css
/* ── Keyboard settings ───────────────────────────────────────── */
.kbMapTable {
  width: 100%;
  border-collapse: collapse;
}

.kbMapTable td {
  padding: 4px 8px;
  font-size: var(--fs-sm);
  border-bottom: 1px solid var(--border);
}

.kbMapAction {
  font-weight: var(--fw-semibold);
  white-space: nowrap;
  width: 1%;
}

.kbKeyCell {
  cursor: pointer;
  font-family: var(--font-mono);
  border-radius: var(--radius-sm);
  transition: background 0.15s;
}

.kbKeyCell:hover {
  background: color-mix(in oklab, var(--fg) var(--hl-hover), var(--bg));
}

.kbKeyCell.listening {
  background: color-mix(in oklab, var(--info) var(--hl-selected), var(--bg));
  outline: 1px solid var(--info);
}

.kbUnbind {
  width: 1%;
}

.kbSep td {
  padding: 0;
  height: var(--gap-section);
  border-bottom: none;
}

.kbCaptureError {
  font-size: var(--fs-sm);
  color: var(--danger);
  margin-top: var(--gap-controls);
}
```

- [ ] **Step 9: Build verification**

Run: `cd lcnc-webui && npm run build`
Expected: zero errors, clean build

- [ ] **Step 10: Commit**

```bash
git add lcnc-webui/src/SettingsPanel.vue lcnc-webui/src/App.vue
git commit -m "feat: add Keyboard settings tab with rebindable key shortcuts"
```

---

## Task 5: Cleanup and final verification

**Files:**
- Modify: `lcnc-webui/src/SettingsPanel.vue` (if needed)
- Modify: `lcnc-webui/src/App.vue` (if needed)
- Modify: `lcnc-webui/src/defaults.ts` (if needed — remove `keyboardJog` from `MachineDefaults`)

- [ ] **Step 1: Remove keyboardJog from MachineDefaults**

In `defaults.ts`, remove `keyboardJog` from the `MachineDefaults` interface, `MACHINE_FALLBACK`, and the machine migration function. The keyboard section now owns this setting.

Update the keyboard section's `migrateFn` to handle cross-section migration: if `saved` is null/empty (first load of keyboard section), check the machine section's saved data for `keyboardJog` and use it to initialize `jogEnabled`:

```typescript
registerSection<KeyboardDefaults>("keyboard", KEYBOARD_FALLBACK, (saved, fb) => {
  if (!saved) {
    // Cross-section migration: read keyboardJog from machine section
    const machRaw = loadSection<any>("machine");
    const jogEnabled = machRaw?.keyboardJog ?? fb.jogEnabled;
    return { ...fb, jogEnabled, mapping: { ...fb.mapping } };
  }
  // ... rest of existing migration
});
```

- [ ] **Step 2: Clean up any remaining references**

Search for any remaining references to `keyboardJog`, `keyboardJogEnabled`, `setKeyboardJog`, `activeJogKeys`, `JOG_KEY_MAP`, `ROTARY_KEY_PAIRS`, `rotaryJogKeys` across all files. Remove any stale references.

- [ ] **Step 3: Final build verification**

Run: `cd lcnc-webui && npm run build`
Expected: zero errors, clean build

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "chore: remove deprecated keyboardJog from MachineDefaults"
```

---

## Implementation Notes

- **E-Stop safety:** The `estop` action in `onKeyDown` fires before _all_ other checks (master toggle, input focus). This matches the current behavior where Escape always works.
- **Safety on disable:** A watcher on `keyboardConfig.enabled` and `jogEnabled` calls `stopAllJog()` when toggled off mid-jog.
- **Key capture phase:** The capture listener uses `addEventListener(..., true)` (capturing phase) + `e.stopPropagation()` to prevent the key from reaching App.vue's `onKeyDown`.
- **Build verification:** Run `npm run build` (not `vue-tsc --noEmit`) — uses `vue-tsc -b` which is stricter (catches TS6133 unused imports).
- **No `:deep()` visual overrides** — all new CSS uses scoped selectors on elements within the keyboard tab, no cross-component visual overrides.
- **Spacing tokens** — all gaps use `--gap-*` variables, no hardcoded values.
- **Permission gates** — the fieldset `:disabled="!can.idle"` wraps the entire tab.
