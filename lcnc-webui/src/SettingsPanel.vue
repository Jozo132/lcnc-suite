<script setup lang="ts">
import { ref, reactive, watch } from "vue";
import TabPanel from "./TabPanel.vue";

type Vec3 = [number, number, number];
type Layer = "backplot" | "toolpath" | "machine" | "workpiece" | "bounds" | "hud";

const STORAGE_KEY = "lcnc-defaults";

interface Defaults {
  workpieceSize: Vec3;
  workpieceOffset: Vec3;
  layers: Record<Layer, boolean>;
}

const fallback: Defaults = {
  workpieceSize: [100, 100, 20],
  workpieceOffset: [0, 0, -20],
  layers: { backplot: true, toolpath: true, machine: true, workpiece: true, bounds: true, hud: true },
};

function load(): Defaults {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        workpieceSize: parsed.workpieceSize ?? [...fallback.workpieceSize],
        workpieceOffset: parsed.workpieceOffset ?? [...fallback.workpieceOffset],
        layers: { ...fallback.layers, ...parsed.layers },
      };
    }
  } catch { /* ignore */ }
  return { ...fallback, workpieceSize: [...fallback.workpieceSize], workpieceOffset: [...fallback.workpieceOffset], layers: { ...fallback.layers } };
}

function save() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    workpieceSize: wpSize,
    workpieceOffset: wpOffset,
    layers: { ...layers },
  }));
}

const saved = load();
const wpSize = reactive<Vec3>([...saved.workpieceSize] as Vec3);
const wpOffset = reactive<Vec3>([...saved.workpieceOffset] as Vec3);
const layers = reactive<Record<Layer, boolean>>({ ...saved.layers });

const subTabs = [
  { id: "viewer", label: "3D Viewer" },
  { id: "dro", label: "DRO" },
  { id: "jog", label: "Jogging" },
];
const activeTab = ref("viewer");

function updateSize(axis: number, value: number) {
  if (isNaN(value) || value < 0) return;
  wpSize[axis] = value;
  if (axis === 2) wpOffset[2] = -value;
  save();
}

function updateOffset(axis: number, value: number) {
  if (isNaN(value)) return;
  wpOffset[axis] = value;
  save();
}

function onLayerChange() {
  save();
}
</script>

<template>
  <div class="settings">
    <div class="hint">Changes here set startup defaults. They take effect on next page load.</div>
    <TabPanel :tabs="subTabs" v-model="activeTab" class="subTabs">
      <template #viewer>
        <div class="section">
          <div class="sectionTitle">Workpiece Defaults</div>
          <div class="fieldGroup">
            <div class="inputRow" v-for="(label, i) in ['Size X', 'Size Y', 'Size Z']" :key="'s'+i">
              <label class="inputLabel">{{ label }}</label>
              <input
                type="number"
                class="numInput"
                :value="wpSize[i]"
                @input="updateSize(i, parseFloat(($event.target as HTMLInputElement).value))"
                step="1" min="0" max="9999"
              />
            </div>
          </div>
          <div class="fieldGroup">
            <div class="inputRow" v-for="(label, i) in ['Offset X', 'Offset Y', 'Offset Z']" :key="'o'+i">
              <label class="inputLabel">{{ label }}</label>
              <input
                type="number"
                class="numInput"
                :value="wpOffset[i]"
                @input="updateOffset(i, parseFloat(($event.target as HTMLInputElement).value))"
                step="1" min="-9999" max="9999"
              />
            </div>
          </div>
        </div>

        <div class="section">
          <div class="sectionTitle">Layer Defaults</div>
          <div class="layerGrid">
            <label v-for="layer in (['backplot', 'toolpath', 'machine', 'workpiece', 'bounds', 'hud'] as Layer[])" :key="layer">
              <input type="checkbox" v-model="layers[layer]" @change="onLayerChange" />
              {{ layer === 'hud' ? 'HUD' : layer.charAt(0).toUpperCase() + layer.slice(1) }}
            </label>
          </div>
        </div>
      </template>

      <template #dro>
        <div class="placeholder">
          <div class="placeholderText">DRO settings coming soon</div>
        </div>
      </template>

      <template #jog>
        <div class="placeholder">
          <div class="placeholderText">Jogging settings coming soon</div>
        </div>
      </template>
    </TabPanel>
  </div>
</template>

<style scoped>
.settings {
  padding: 4px 0;
}

.hint {
  font-size: 11px;
  opacity: 0.45;
  margin-bottom: 12px;
}

.subTabs :deep(.tab-btn) {
  padding: 6px 12px;
  font-size: 12px;
  border-radius: 8px 8px 3px 3px;
}

.section {
  margin-bottom: 24px;
}

.sectionTitle {
  font-size: 11px;
  font-weight: 600;
  opacity: 0.6;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 12px;
}

.fieldGroup {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}

.inputRow {
  display: flex;
  align-items: center;
  gap: 8px;
}

.inputLabel {
  font-size: 12px;
  opacity: 0.8;
  min-width: 60px;
}

.numInput {
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--button-bg);
  color: var(--fg);
  font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
  width: 80px;
}

.numInput:focus {
  outline: none;
  border-color: color-mix(in oklab, var(--fg) 40%, var(--border));
}

.layerGrid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.layerGrid label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
  user-select: none;
}

.layerGrid input[type="checkbox"] {
  cursor: pointer;
}

.placeholder {
  padding: 40px 0;
  text-align: center;
}

.placeholderText {
  font-size: 13px;
  opacity: 0.4;
}
</style>
