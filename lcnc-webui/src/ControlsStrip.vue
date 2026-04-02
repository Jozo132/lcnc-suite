<script setup lang="ts">
import Gate from "./Gate.vue";
import MachineBtn from "./MachineBtn.vue";
import MachineSlider from "./MachineSlider.vue";
import MachineInput from "./MachineInput.vue";
import { RotateCw, RotateCcw, CircleStop } from "lucide-vue-next";
import { STEP_RPM, STEP_OVERRIDE, STEP_RAPID_OVERRIDE, STEP_DEFAULT, type MacroDef } from "./defaults";

const props = defineProps<{
  feedSlider: number;
  spindleSlider: number;
  rapidSlider: number;
  feedOvrEnabled: boolean;
  spindleOvrEnabled: boolean;
  maxFeedOverride: number;
  minSpindleOverride: number;
  maxSpindleOverride: number;
  isForward: boolean;
  isReverse: boolean;
  isSpinning: boolean;
  rpmInput: number;
  spindleActual: number | null;
  spindleSpeed: number | null;
  spindleLoad: number | null;
  minSpindleSpeed: number;
  maxSpindleSpeed: number;
  floodOn: boolean;
  mistOn: boolean;
  toolNumber: number;
  currentTool: number;
  probing: boolean;
  userMacros: MacroDef[];
}>();

const emit = defineEmits<{
  (e: "update:feedSlider", v: number): void;
  (e: "update:spindleSlider", v: number): void;
  (e: "update:rapidSlider", v: number): void;
  (e: "feedChange"): void;
  (e: "spindleSliderChange"): void;
  (e: "rapidChange"): void;
  (e: "overridePreset", type: "feed" | "spindle" | "rapid", percent: number): void;
  (e: "resetAllOverrides"): void;
  (e: "spindleFwd", speed: number): void;
  (e: "spindleRev", speed: number): void;
  (e: "spindleStop"): void;
  (e: "update:rpmInput", v: number): void;
  (e: "toggleFlood"): void;
  (e: "toggleMist"): void;
  (e: "update:toolNumber", v: number): void;
  (e: "saveToolNumber"): void;
  (e: "measureAuto"): void;
  (e: "loadTool"): void;
  (e: "unloadTool"): void;
  (e: "openToolTable"): void;
  (e: "executeMacro", m: MacroDef): void;
}>();

function formatRpm(val: number | null): string {
  if (val == null) return "---";
  return Math.round(val).toLocaleString();
}

function onFeedSlider(v: number) { emit('update:feedSlider', v); }
function onSpindleSlider(v: number) { emit('update:spindleSlider', v); }
function onRapidSlider(v: number) { emit('update:rapidSlider', v); }
</script>

<template>
  <div class="controlsStrip">
    <!-- LEFT: Overrides -->
    <Gate gate="override" class="ovrSection">
      <div class="ovrCol">
        <span class="ovrVal" :class="{ warn: feedSlider !== 100 }">{{ feedSlider }}%</span>
        <MachineSlider gate="feedOverride" :modelValue="feedSlider" @update:model-value="onFeedSlider(Number($event))" @change="emit('feedChange')" :min="0" :max="maxFeedOverride" :step="STEP_OVERRIDE" :disabled="!feedOvrEnabled" class="vSlider" />
        <span class="ovrLabel">Feed</span>
      </div>
      <div class="ovrCol">
        <span class="ovrVal" :class="{ warn: spindleSlider !== 100 }">{{ spindleSlider }}%</span>
        <MachineSlider gate="spindleOverride" :modelValue="spindleSlider" @update:model-value="onSpindleSlider(Number($event))" @change="emit('spindleSliderChange')" :min="minSpindleOverride" :max="maxSpindleOverride" :step="STEP_OVERRIDE" :disabled="!spindleOvrEnabled" class="vSlider" />
        <span class="ovrLabel">Spindle</span>
      </div>
      <div class="ovrCol">
        <span class="ovrVal" :class="{ warn: rapidSlider !== 100 }">{{ rapidSlider }}%</span>
        <MachineSlider gate="rapidOverride" :modelValue="rapidSlider" @update:model-value="onRapidSlider(Number($event))" @change="emit('rapidChange')" :min="25" :max="100" :step="STEP_RAPID_OVERRIDE" class="vSlider" />
        <span class="ovrLabel">Rapid</span>
      </div>
    </Gate>

    <!-- RIGHT: Tool + Spindle + Coolant -->
    <div class="rightSection">
      <!-- Tool display -->
      <Gate gate="ready" class="toolBlock">
        <div class="toolHead">
          <span class="toolLabel">TOOL</span>
          <span class="toolCurrent">T{{ currentTool }}</span>
        </div>
      </Gate>

      <!-- Spindle controls -->
      <Gate gate="ready" class="spnBlock">
        <div class="spnDir">
          <MachineBtn type="spindleFwd" :active="isForward" @click="emit('spindleFwd', rpmInput)" title="Forward (CW)" class="spnBtn">
            <RotateCw :size="18" />
          </MachineBtn>
          <MachineBtn type="spindleStop" :active="isSpinning" :disabled="!isSpinning" @click="emit('spindleStop')" title="Stop" class="spnBtn spnStopBtn">
            <CircleStop :size="18" />
          </MachineBtn>
          <MachineBtn type="spindleRev" :active="isReverse" @click="emit('spindleRev', rpmInput)" title="Reverse (CCW)" class="spnBtn">
            <RotateCcw :size="18" />
          </MachineBtn>
        </div>

        <div class="rpmBlock">
          <div class="rpmRow">
            <span class="rpmLabel">RPM</span>
            <span class="rpmValue">{{ formatRpm(spindleActual) }}</span>
          </div>
          <div class="rpmRow rpmCmd">
            <span class="rpmLabel">CMD</span>
            <span class="rpmValueSm">{{ formatRpm(spindleSpeed) }}</span>
          </div>
        </div>
      </Gate>

      <!-- Coolant -->
      <Gate gate="ready" class="coolBlock">
        <div class="coolBtns">
          <MachineBtn type="flood" :active="floodOn" @click="emit('toggleFlood')" class="coolBtn">FLOOD</MachineBtn>
          <MachineBtn type="mist" :active="mistOn" @click="emit('toggleMist')" class="coolBtn">MIST</MachineBtn>
        </div>
      </Gate>
    </div>
  </div>
</template>

<style scoped>
.controlsStrip {
  display: grid;
  grid-template-columns: 1fr 1fr;
  height: 100%;
  overflow: hidden;
}

/* ── Overrides ── */
.ovrSection {
  display: flex;
  gap: var(--gap-section);
}
.ovrCol {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--gap-tight);
  height: 100%;
  justify-content: center;
}
.ovrVal {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}
.ovrVal.warn {
  color: var(--warn);
}
.ovrLabel {
  font-size: var(--fs-2xs);
  opacity: var(--opacity-muted);
  white-space: nowrap;
}
.vSlider {
  writing-mode: vertical-lr;
  direction: rtl;
  flex: 1;
  min-height: 0;
}

/* ── Right column: Tool + Spindle + Coolant stacked ── */
.rightSection {
  display: flex;
  flex-direction: column;
  gap: var(--gap-tight);
  border-left: 1px solid var(--border-subtle);
  padding-left: var(--gap-controls);
}

/* Tool */
.toolBlock {
  flex-shrink: 0;
}
.toolHead {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--gap-tight) var(--gap-controls);
  background: color-mix(in oklab, var(--bg) 80%, transparent);
  border-radius: var(--radius-lg);
}
.toolLabel {
  font-size: var(--fs-2xs);
  font-family: var(--font-mono);
  opacity: var(--opacity-muted);
}
.toolCurrent {
  font-family: var(--font-mono);
  font-size: var(--fs-2xl);
  font-weight: var(--fw-bold);
  color: var(--active-tool);
}

/* Spindle */
.spnBlock {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--gap-tight);
}
.spnDir {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: var(--gap-tight);
}
.spnBtn {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--gap-controls);
}
.rpmBlock {
  padding: var(--gap-tight) var(--gap-controls);
  background: color-mix(in oklab, var(--bg) 80%, transparent);
  border-radius: var(--radius-lg);
}
.rpmRow {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.rpmLabel {
  font-size: var(--fs-2xs);
  font-family: var(--font-mono);
  opacity: var(--opacity-muted);
}
.rpmValue {
  font-family: var(--font-mono);
  font-size: var(--fs-xl);
  font-weight: var(--fw-bold);
}
.rpmCmd {
  border-top: 1px solid color-mix(in oklab, var(--border) 30%, transparent);
  padding-top: var(--gap-micro);
  margin-top: var(--gap-micro);
}
.rpmValueSm {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  opacity: var(--opacity-muted);
}

/* Coolant */
.coolBlock {
  flex-shrink: 0;
}
.coolBtns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--gap-tight);
}
.coolBtn {
  font-size: var(--fs-2xs);
  font-family: var(--font-mono);
  text-align: center;
}
</style>
