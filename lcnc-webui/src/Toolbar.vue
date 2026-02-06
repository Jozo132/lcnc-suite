<template>
  <div class="viewerContainer">
    <!-- Left side: All controls (vertical) -->
    <div class="leftSidebar">
      <div class="group">
        <div class="groupLabel">Camera View</div>
        <button class="viewBtn" @click="$emit('setView', 'top')">Top</button>
        <button class="viewBtn" @click="$emit('setView', 'front')">Front</button>
        <button class="viewBtn" @click="$emit('setView', 'back')">Back</button>
        <button class="viewBtn" @click="$emit('setView', 'left')">Left</button>
        <button class="viewBtn" @click="$emit('setView', 'right')">Right</button>
        <button class="viewBtn" @click="$emit('setView', 'dimetric')">Dimetric</button>
        <button class="viewBtn" @click="$emit('setView', 'reset')">Reset View</button>
      </div>

      <div class="group">
        <div class="groupLabel">Backplot</div>
        <button class="viewBtn" @click="$emit('resetBackplot')">Reset Backplot</button>
      </div>

      <div class="group">
        <div class="groupLabel">Layers</div>
        <label><input type="checkbox" v-model="local.backplot" @change="emitToggle('backplot')" /> Backplot</label>
        <label><input type="checkbox" v-model="local.toolpath" @change="emitToggle('toolpath')" /> Toolpath</label>
        <label><input type="checkbox" v-model="local.machine"  @change="emitToggle('machine')"  /> Machine</label>
        <label><input type="checkbox" v-model="local.workpiece" @change="emitToggle('workpiece')" /> Workpiece</label>
        <label><input type="checkbox" v-model="local.bounds" @change="emitToggle('bounds')" /> Bounds</label>
      </div>
    </div>

    <!-- Right side: 3D Viewer slot -->
    <div class="viewerSlot">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive } from "vue";

type ViewPreset = "top" | "left" | "right" | "front" | "back" | "iso" | "dimetric" | "reset";
type Layer = "backplot" | "toolpath" | "machine" | "workpiece" | "bounds";

const emit = defineEmits<{
  (e: "resetBackplot"): void;
  (e: "setView", preset: ViewPreset): void;
  (e: "toggleLayer", layer: Layer, on: boolean): void;
}>();

const local = reactive<Record<Layer, boolean>>({
  backplot: true,
  toolpath: true,
  machine: true,
  workpiece: true,
  bounds: true,
});

function emitToggle(layer: Layer) {
  emit("toggleLayer", layer, local[layer]);
}
</script>

<style scoped>
.viewerContainer {
  display: flex;
  gap: 12px;
  align-items: stretch;
  width: 100%;
}

.leftSidebar {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 120px;
}

.viewerSlot {
  flex: 1;
  min-width: 0;
}

.group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.groupLabel {
  font-size: 11px;
  font-weight: 600;
  opacity: 0.6;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 2px;
}

.viewBtn {
  padding: 8px 12px;
  font-size: 13px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--button-bg);
  color: var(--fg);
  cursor: pointer;
  transition: all 0.15s ease;
  text-align: left;
  white-space: nowrap;
}

.viewBtn:hover {
  background: color-mix(in oklab, var(--button-bg) 85%, var(--fg));
}

.viewBtn:active {
  transform: scale(0.98);
}

.leftSidebar label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
  user-select: none;
  padding: 4px 0;
}

.leftSidebar input[type="checkbox"] {
  cursor: pointer;
}
</style>
