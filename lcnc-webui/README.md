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

### Classes

| Class | Rule | Buttons / Actions |
|-------|------|-------------------|
| `idle` | base, idle, not busy | Home All, Unhome, Zero X/Y/Z, Zero All, G5x select, file Reload/Unload/Browse/Upload |
| `jog` | base, idle, homed | Jog X+/X-/Y+/Y-/Z+/Z-, speed slider, increment select, teleop toggle, keyboard jog |
| `override` | base, not busy | Feed/Spindle/Rapid override sliders + presets, Reset All |
| `ready` | base, idle, not busy, homed | MDI input + Send, Cycle Start, Spindle FWD/REV/STOP, RPM input |
| `pause` | base, running, not paused | Pause |
| `resume` | base, paused | Resume |
| `abort` | armed | Abort |

`base` = armed, not estopped, enabled

Safety buttons (E-Stop, Machine On/Off) use direct conditions in App.vue — they have unique toggle logic and appear in one place.

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
