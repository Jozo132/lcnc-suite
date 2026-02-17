# lcnc-webui

Reference Vue 3 + TypeScript web interface for lcnc-gateway.

## Development

```bash
npm install
npm run dev          # Dev server with HMR
npm run build        # Production build
npm run type-check   # TypeScript checking
```

## Architecture

```
App.vue                          Root — state, layout, permission provider
├── Toolbar.vue                  Top bar: connection, arm, estop, enable
├── ThreeViewer.vue              3D viewer (Three.js)
│   ├── JogHUD.vue               Jog overlay pill
│   ├── GcodeHUD.vue             G-code overlay pill
│   ├── SpindleHUD.vue           Spindle overlay pill
│   ├── OverrideHUD.vue          Override overlay pill
│   └── SetupHUD.vue             Setup overlay pill (home, zero)
└── TabPanel.vue                 Side panel tab selector
    ├── DroPanel.vue             Digital readout + G5x selector
    ├── JogPanel.vue             Axis jog wheel + speed/increment
    ├── MdiPanel.vue             Manual data input + history
    ├── GcodePanel.vue           G-code viewer + program controls
    ├── SpindlePanel.vue         Spindle direction + RPM + override
    ├── OverridePanel.vue        Feed/spindle/rapid override sliders
    ├── SettingsPanel.vue        Colors, opacities, layers, workpiece
    └── MessagesPanel.vue        Error/message log
```

### Services

| File | Purpose |
|------|---------|
| `lcncWs.ts` | WebSocket client — status polling, command sending, heartbeat |
| `lcncApi.ts` | REST helpers — file listing, upload |
| `permissions.ts` | Centralized button permission system |

## Permission System

All button enable/disable logic is defined once in `permissions.ts` and distributed via Vue's provide/inject. Components never compute their own disable conditions — they reference a permission class.

See the [permission class table](../README.md#reference-ui-lcnc-webui) in the root README for the full rule and button matrix.

### Usage

```vue
<script setup>
import { usePermissions } from "./permissions";
const can = usePermissions();
</script>

<template>
  <button :disabled="!can.idle">Zero All</button>
  <button :disabled="!can.jog">Jog X+</button>
  <button :disabled="!can.override">Feed 100%</button>
</template>
```

`usePermissions()` returns a `ComputedRef<Permissions>` — auto-unwraps in templates, use `.value` in script.

## Panel Layout

Viewport-locked layout: `html { overflow: auto }`, `body { overflow: hidden; min-width: 760px; min-height: 600px }`. Browser scrollbar appears only when the window is smaller than the minimums. Inside the app, `.panels` scrolls one axis per orientation — horizontal in landscape, vertical in portrait. All `.panel` elements use `box-sizing: border-box` (sizes include padding + border).

### Landscape (side by side, `overflow-x: auto`)

| Panel | Width | Height |
|-------|-------|--------|
| viewer | `flex: 1`, `min-width: 560px` | stretch, `min-height: 400px` |
| dro | `flex: 0 0 320px`, `min-width: 560px` | stretch, `min-height: 400px` |
| gcode | `flex: 0.5`, `min-width: 320px` | stretch, `min-height: 400px` |
| messages | `flex: 0.5`, `min-width: 320px` | stretch, `min-height: 400px` |
| jog | `flex: 0 0 320px` | stretch, `min-height: 400px` |
| overrides | `flex: 0 0 320px` | stretch, `min-height: 400px` |
| spindle | `flex: 0 0 320px` | stretch, `min-height: 400px` |
| settings | `flex: 0 0 320px` | stretch, `min-height: 400px` |
| mdi | `flex: 0 0 320px` | stretch, `min-height: 400px` |

Viewer fills remaining width. Gcode and messages grow at half rate (`flex: 0.5`). All other panels stay fixed at 320px. DRO gets a wider `min-width: 560px` override. Height is uniform — all panels stretch to `.panels` container height.

### Portrait (stacked, `overflow-y: auto`)

| Panel | Width | Height |
|-------|-------|--------|
| viewer | stretch, `min-width: 560px` | `flex: 1`, `min-height: 500px` |
| dro, jog, overrides, spindle, settings | stretch, `min-width: 560px` | `flex: 0 0 auto` (content-sized) |
| gcode, messages, mdi | stretch, `min-width: 560px` | `flex: 0 0 500px` (fixed for internal scroll) |

Viewer fills remaining height. Static panels (dro, jog, overrides, spindle, settings) auto-size to their content — no manual height tracking needed. Scrollable panels (gcode, messages, mdi) use a fixed 500px height to bound their internal scroll areas. Width is uniform — all panels stretch to `.panels` width with a shared `min-width: 560px`.
