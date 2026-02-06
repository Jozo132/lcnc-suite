<script setup lang="ts">
defineProps<{
  workPos: number[];
  machinePos: number[];
  armed: boolean;
  busy: boolean;
  homed: boolean;
}>();

const emit = defineEmits<{
  (e: "zeroAxis", axis: number): void;
  (e: "homeAll"): void;
  (e: "unhomeAll"): void;
}>();

function fmt(n: any) {
  const x = Number(n);
  return Number.isFinite(x) ? x.toFixed(3) : "-";
}
</script>

<template>
  <div class="container">
    <div class="section">
      <div class="sub">Work Position</div>
      <div class="dro">
        <div class="axisRow">
          <div class="axis"><span>X</span><b>{{ fmt(workPos[0]) }}</b></div>
          <button class="zeroBtn" @click="emit('zeroAxis', 0)" :disabled="!armed || busy">Zero X</button>
        </div>
        <div class="axisRow">
          <div class="axis"><span>Y</span><b>{{ fmt(workPos[1]) }}</b></div>
          <button class="zeroBtn" @click="emit('zeroAxis', 1)" :disabled="!armed || busy">Zero Y</button>
        </div>
        <div class="axisRow">
          <div class="axis"><span>Z</span><b>{{ fmt(workPos[2]) }}</b></div>
          <button class="zeroBtn" @click="emit('zeroAxis', 2)" :disabled="!armed || busy">Zero Z</button>
        </div>
      </div>
    </div>

    <div class="separator"></div>

    <div class="section">
      <div class="sub">Machine Position</div>
      <div class="machineRow">
        <div class="dro">
          <div class="axis"><span>X</span><b>{{ fmt(machinePos[0]) }}</b></div>
          <div class="axis"><span>Y</span><b>{{ fmt(machinePos[1]) }}</b></div>
          <div class="axis"><span>Z</span><b>{{ fmt(machinePos[2]) }}</b></div>
        </div>
        <div class="homingButtons">
          <button class="homeBtn" @click="emit('homeAll')" :disabled="!armed || busy || homed">Home All Axes</button>
          <button class="homeBtn" @click="emit('unhomeAll')" :disabled="!armed || busy || !homed">Unhome</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.container {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.sub {
  font-size: 12px;
  opacity: 0.65;
  margin-bottom: 8px;
}

.dro {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.axisRow {
  display: flex;
  align-items: center;
  gap: 16px;
}

.machineRow {
  display: flex;
  align-items: stretch;
  gap: 16px;
}

.homingButtons {
  display: flex;
  flex-direction: row;
  gap: 12px;
}

.axis {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 24px;
  min-width: 180px;
}

.axis span {
  font-size: 12px;
  opacity: 0.7;
  width: 14px;
}

.zeroBtn {
  padding: 6px 12px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--button-bg);
  color: var(--fg);
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
  min-width: 120px;
}

.zeroBtn:hover {
  background: color-mix(in oklab, var(--button-bg) 85%, var(--fg));
}

.zeroBtn:active:not(:disabled) {
  transform: scale(0.98);
}

.zeroBtn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.homeBtn {
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--button-bg);
  color: var(--fg);
  cursor: pointer;
  transition: all 0.15s ease;
  min-width: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.homeBtn:hover {
  background: color-mix(in oklab, var(--button-bg) 85%, var(--fg));
}

.homeBtn:active:not(:disabled) {
  transform: scale(0.98);
}

.homeBtn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.separator {
  height: 1px;
  background: var(--border);
  opacity: 0.3;
}
</style>
