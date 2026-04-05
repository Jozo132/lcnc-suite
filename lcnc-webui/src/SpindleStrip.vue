<script setup lang="ts">
import Gate from "./Gate.vue";
import MachineBtn from "./MachineBtn.vue";
import MachineInput from "./MachineInput.vue";
import MachineToggle from "./MachineToggle.vue";
import { RotateCw, RotateCcw, Square } from "lucide-vue-next";
import { STEP_RPM } from "./defaults";
import { fmtRpm } from "./format";

const props = defineProps<{
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
}>();

const emit = defineEmits<{
  (e: "spindleFwd", speed: number): void;
  (e: "spindleRev", speed: number): void;
  (e: "spindleStop"): void;
  (e: "update:rpmInput", v: number): void;
  (e: "toggleFlood"): void;
  (e: "toggleMist"): void;
}>();
</script>

<template>
  <div class="stripSection">
    <div class="sub">Spindle</div>
    <Gate gate="ready" class="spnBlock stack-controls">
      <div class="spDirRow row-tight">
        <MachineBtn type="spindleRev" :active="isReverse" @click="emit('spindleRev', rpmInput)">
          <span class="btn-label"><RotateCcw :size="14" /> Rev</span>
        </MachineBtn>
        <MachineBtn type="spindleStop" :active="isSpinning" :disabled="!isSpinning" @click="emit('spindleStop')">
          <span class="btn-label"><Square :size="14" /> Stop</span>
        </MachineBtn>
        <MachineBtn type="spindleFwd" :active="isForward" @click="emit('spindleFwd', rpmInput)">
          <span class="btn-label"><RotateCw :size="14" /> Fwd</span>
        </MachineBtn>
      </div>

      <div class="spRpmRow">
        <span class="label-muted md">Speed</span>
        <MachineInput gate="stripInput" type="number" class="spRpmInput" :value="rpmInput" @input="emit('update:rpmInput', +($event.target as HTMLInputElement).value)" :min="minSpindleSpeed" :max="maxSpindleSpeed" :step="STEP_RPM" />
      </div>

      <div class="spActualGroup inset-panel stack-tight">
        <div class="spActualRow">
          <span class="label-muted md">Actual</span>
          <span class="val-status md mono">{{ fmtRpm(spindleActual) }}</span>
        </div>
        <div class="spActualRow">
          <span class="label-muted md">Commanded</span>
          <span class="val-status md mono muted">{{ fmtRpm(spindleSpeed) }}</span>
        </div>
        <div class="spActualRow">
          <span class="label-muted md">Direction</span>
          <span class="val-status md" :class="{ ok: isSpinning }"><span class="stable-width"><span :class="{ alt: !isForward }">FWD</span><span :class="{ alt: !isReverse }">REV</span><span :class="{ alt: isForward || isReverse }">OFF</span></span></span>
        </div>
        <div v-if="spindleLoad != null" class="spActualRow">
          <span class="label-muted md">Load</span>
          <span class="val-status md mono">{{ Math.round(spindleLoad) }}%</span>
        </div>
      </div>
    </Gate>

    <div class="coolBlock">
      <div class="sub">Coolant</div>
      <div class="coolToggles">
        <MachineToggle gate="coolant" :modelValue="floodOn" @update:modelValue="emit('toggleFlood')" label="Flood" />
        <MachineToggle gate="coolant" :modelValue="mistOn" @update:modelValue="emit('toggleMist')" label="Mist" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.spnBlock {
  flex: 1;
}
.spRpmRow {
  display: flex;
  align-items: center;
  gap: var(--gap-controls);
}
.spRpmInput { flex: 1; }
.spActualRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.coolBlock {
  display: flex;
  flex-direction: column;
  gap: var(--gap-tight);
  flex-shrink: 0;
}
.coolToggles {
  display: flex;
  gap: var(--gap-section);
}
</style>
