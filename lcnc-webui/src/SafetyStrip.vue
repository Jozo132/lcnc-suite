<script setup lang="ts">
import { computed } from "vue";
import Gate from "./Gate.vue";
import MachineBtn from "./MachineBtn.vue";
import { Lock, LockOpen, TriangleAlert, Power } from "lucide-vue-next";

const props = defineProps<{
  armed: boolean;
  busy: boolean;
  isEstop: boolean;
  isEnabled: boolean;
  isHomed: boolean;
  canEstop: boolean;
  canResetEstop: boolean;
}>();

const emit = defineEmits<{
  (e: "arm", value: boolean): void;
  (e: "estop"): void;
  (e: "estopReset"): void;
  (e: "machineOn"): void;
  (e: "machineOff"): void;
}>();

const estopLabel = computed(() => props.isEstop ? "Reset" : "E-Stop");
</script>

<template>
  <div class="safetyStrip">
    <div class="safetyHeader">
      <Power :size="14" class="headerIcon" />
      <span class="headerLabel">Safety &amp; Power</span>
    </div>

    <div class="safetyBtns">
      <Gate gate="always" class="btnGate">
        <MachineBtn
          type="arm"
          :variant="armed ? 'ok' : 'default'"
          :disabled="busy"
          :title="armed ? 'Disarm' : 'Arm'"
          @click="emit('arm', !armed)"
          class="safetyBtn"
          block
        >
          <component :is="armed ? LockOpen : Lock" :size="18" />
          <span class="btnLabel">{{ armed ? 'Armed' : 'Arm' }}</span>
        </MachineBtn>
      </Gate>

      <Gate gate="always" class="btnGate">
        <MachineBtn
          type="estop"
          :flashing="isEstop"
          :disabled="!(isEstop ? canResetEstop : canEstop)"
          @click="isEstop ? emit('estopReset') : emit('estop')"
          class="safetyBtn"
          block
        >
          <TriangleAlert :size="18" />
          <span class="btnLabel">{{ estopLabel }}</span>
        </MachineBtn>
      </Gate>

      <Gate gate="safety" class="btnGate">
        <MachineBtn
          type="machineOn"
          :variant="isEnabled ? 'ok' : 'default'"
          @click="isEnabled ? emit('machineOff') : emit('machineOn')"
          class="safetyBtn"
          block
        >
          <Power :size="18" />
          <span class="btnLabel">{{ isEnabled ? 'On' : 'Off' }}</span>
        </MachineBtn>
      </Gate>
    </div>

    <div class="statusBar">
      <div class="statusItem">
        <span class="statusDot" :class="{ on: !isEstop }"></span>
        <span class="statusLabel">READY</span>
      </div>
      <div class="statusItem">
        <span class="statusDot" :class="{ on: isEnabled }"></span>
        <span class="statusLabel">ENABLED</span>
      </div>
      <div class="statusItem">
        <span class="statusDot" :class="{ on: isHomed }"></span>
        <span class="statusLabel">HOMED</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.safetyStrip {
  display: flex;
  flex-direction: column;
  gap: var(--gap-controls);
  padding: var(--gap-controls);
  height: 100%;
}

.safetyHeader {
  display: flex;
  align-items: center;
  gap: var(--gap-tight);
  flex-shrink: 0;
}
.headerIcon {
  opacity: var(--opacity-muted);
}
.headerLabel {
  font-size: var(--fs-2xs);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: var(--fw-bold);
  opacity: var(--opacity-muted);
}

.safetyBtns {
  display: flex;
  gap: var(--gap-controls);
  flex: 1;
}
.btnGate {
  flex: 1;
  display: flex;
}
.safetyBtn {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--gap-tight);
  flex: 1;
}
.btnLabel {
  font-size: var(--fs-xs);
  font-weight: var(--fw-bold);
  text-transform: uppercase;
}

.statusBar {
  display: flex;
  align-items: center;
  gap: var(--gap-section);
  padding: var(--gap-tight) var(--gap-controls);
  background: color-mix(in oklab, var(--bg) 80%, transparent);
  border-radius: var(--radius-lg);
  flex-shrink: 0;
}
.statusItem {
  display: flex;
  align-items: center;
  gap: var(--gap-tight);
}
.statusDot {
  width: 10px;
  height: 10px;
  border-radius: var(--radius-pill);
  background: color-mix(in oklab, var(--fg) 20%, transparent);
  border: 1px solid color-mix(in oklab, var(--border) 50%, transparent);
}
.statusDot.on {
  background: var(--ok);
  border-color: transparent;
  box-shadow: 0 0 8px color-mix(in oklab, var(--ok) 50%, transparent);
}
.statusLabel {
  font-size: var(--fs-2xs);
  font-family: var(--font-mono);
  opacity: var(--opacity-muted);
}
</style>
