<script setup lang="ts">
import { computed } from "vue";
import MachineBtn from "./MachineBtn.vue";
import MachineInput from "./MachineInput.vue";

const props = defineProps<{
  toolNumber: number;
  currentTool: number;
  probing: boolean;
  probeInput: boolean;
  probeTripped: boolean;
}>();

const emit = defineEmits<{
  (e: "update:toolNumber", v: number): void;
  (e: "saveToolNumber"): void;
  (e: "measureAuto"): void;
  (e: "loadTool"): void;
  (e: "unloadTool"): void;
  (e: "openToolTable"): void;
  (e: "abort"): void;
  (e: "simTrip"): void;
}>();

const isDev = import.meta.env.DEV;

const probeStatus = computed(() => {
  if (props.probing) return "PROBING";
  if (props.probeTripped) return "TRIPPED";
  return "IDLE";
});

const statusClass = computed(() => {
  if (props.probing) return "probing";
  if (props.probeTripped) return "tripped";
  return "";
});

const probeIndicatorClass = computed(() => {
  if (props.probeInput) return "tripped";
  return "";
});
</script>

<template>
  <div class="toolStrip">
    <div class="stripSection toolControls">
      <div class="sub">Tool</div>
      <div class="toolInputRow row-tight">
        <span class="label-muted md toolLabel">Tool #</span>
        <MachineInput gate="stripInput" type="number" class="toolNumInput"
          :value="toolNumber"
          @input="emit('update:toolNumber', +($event.target as HTMLInputElement).value)"
          @change="emit('saveToolNumber')"
          :min="1" />
        <MachineBtn type="mdi" :disabled="probing" @click="emit('loadTool')">Load</MachineBtn>
        <MachineBtn type="toolUnload" :disabled="probing" @click="emit('unloadTool')">Unload</MachineBtn>
      </div>

      <div class="toolActionRow row-tight">
        <MachineBtn type="mdi" :disabled="probing" @click="emit('measureAuto')">Measure</MachineBtn>
        <MachineBtn type="manage" @click="emit('openToolTable')">Table</MachineBtn>
      </div>

      <MachineBtn type="abort" @click="emit('abort')" block />

      <div class="toolStatusRow">
        <div class="row-tight">
          <span class="statusDot" :class="probeIndicatorClass"></span>
          <span class="label-muted md">Probe</span>
        </div>
        <div class="row-tight">
          <span class="statusDot" :class="statusClass"></span>
          <span class="label-muted md mono">{{ probeStatus }}</span>
        </div>
        <MachineBtn v-if="isDev" type="simTrip" @click="emit('simTrip')">Sim Trip</MachineBtn>
      </div>

    </div>
  </div>
</template>

<style scoped>
.toolInputRow {
  align-items: stretch;
}

.toolLabel {
  align-self: center;
}

.toolNumInput {
  width: 60px;
}

.toolActionRow :deep(.b) {
  flex: 1;
}

.toolStatusRow {
  display: flex;
  align-items: center;
  gap: var(--gap-controls);
  flex-shrink: 0;
}
</style>
