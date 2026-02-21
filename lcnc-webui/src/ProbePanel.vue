<script setup lang="ts">
import { ref, computed, onMounted, watch } from "vue";
import { usePermissions } from "./permissions";

const STORAGE_KEY = "lcnc-probe-params";

const props = defineProps<{
  probing: boolean;
  probeTripped: boolean;
  probedPosition: number[] | null;
  workPos: number[];
  initialVars: Record<string, number> | null;
}>();

const emit = defineEmits<{
  (e: "mdi", text: string): void;
  (e: "abort"): void;
  (e: "listProbeMacros"): void;
  (e: "simulateProbeTrip"): void;
  (e: "setProbeVars", vars: Record<string, number>): void;
  (e: "getProbeVars"): void;
}>();

const can = usePermissions();

// ─── Probe operations ─────────────────────────────────────────────
type ProbeOp = {
  id: string;
  label: string;
  macro: string;
  wcoMacro: string;
  axisIdx: number;      // 0=X, 1=Y, 2=Z
  description: string;
};

const operations: ProbeOp[] = [
  { id: "x+", label: "X+", macro: "probe_x_plus",  wcoMacro: "probe_x_plus_wco",  axisIdx: 0, description: "Place probe left of workpiece face" },
  { id: "x-", label: "X−", macro: "probe_x_minus", wcoMacro: "probe_x_minus_wco", axisIdx: 0, description: "Place probe right of workpiece face" },
  { id: "y+", label: "Y+", macro: "probe_y_plus",  wcoMacro: "probe_y_plus_wco",  axisIdx: 1, description: "Place probe in front of workpiece face" },
  { id: "y-", label: "Y−", macro: "probe_y_minus", wcoMacro: "probe_y_minus_wco", axisIdx: 1, description: "Place probe behind workpiece face" },
  { id: "z-", label: "Z−", macro: "probe_z_minus", wcoMacro: "probe_z_minus_wco", axisIdx: 2, description: "Place probe above workpiece surface" },
];

const selectedOp = ref<string>("x+");
const currentOp = computed(() => operations.find(o => o.id === selectedOp.value)!);

// ─── Parameters ───────────────────────────────────────────────────
// Parameter order matches config v0.2 subroutines:
// #1=probe_tool, #2=slow_fr, #3=fast_fr, #4=traverse_fr, #5=max_distance, #6=clearance, #7=cal_offset
const params = ref({
  probeTool: 99,
  slowFr: 50.0,
  fastFr: 200.0,
  traverseFr: 1000.0,
  maxDistance: 10.0,
  clearance: 2.0,
  calOffset: 0.0,
});

const autoZero = ref(false);

/** Map UI params → LinuxCNC var numbers (writes both XY and Z distance/clearance) */
function buildVarMap(probeMode: number): Record<string, number> {
  const p = params.value;
  return {
    "3014": p.probeTool,
    "3015": p.slowFr,
    "3016": p.fastFr,
    "3017": p.traverseFr,
    "3018": p.maxDistance,   // XY max distance
    "3019": p.clearance,     // XY clearance
    "3020": p.maxDistance,   // Z max distance (same value)
    "3021": p.clearance,     // Z clearance (same value)
    "3030": probeMode,       // 0 = set WCO, 1 = measure only
    "3032": p.calOffset,
  };
}

function loadParams() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const saved = JSON.parse(raw);
      if (saved.autoZero != null) autoZero.value = saved.autoZero;
      delete saved.autoZero;
      Object.assign(params.value, saved);
    }
  } catch { /* ignore */ }
  // Request current values from var file (overlay on next tick via initialVars watcher)
  emit("getProbeVars");
}

function saveParams() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...params.value, autoZero: autoZero.value }));
  // Sync to var file (and best-effort MDI) on every change
  emit("setProbeVars", buildVarMap(autoZero.value ? 0 : 1));
}

/** When var file values arrive, overlay onto current params */
watch(() => props.initialVars, (vars) => {
  if (!vars) return;
  const p = params.value;
  if (vars["3014"] != null) p.probeTool = vars["3014"];
  if (vars["3015"] != null) p.slowFr = vars["3015"];
  if (vars["3016"] != null) p.fastFr = vars["3016"];
  if (vars["3017"] != null) p.traverseFr = vars["3017"];
  // Use XY distance/clearance as canonical (3018/3019)
  if (vars["3018"] != null) p.maxDistance = vars["3018"];
  if (vars["3019"] != null) p.clearance = vars["3019"];
  if (vars["3032"] != null) p.calOffset = vars["3032"];
  // Persist to localStorage
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...params.value, autoZero: autoZero.value }));
});

onMounted(loadParams);

// ─── Status ───────────────────────────────────────────────────────
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

// ─── Run probe ────────────────────────────────────────────────────
function runProbe() {
  const op = currentOp.value;
  // Save to localStorage (don't use saveParams() to avoid double setProbeVars emit)
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...params.value, autoZero: autoZero.value }));
  // Always use _wco macro — #3030 controls whether WCS is set (0) or measure-only (1)
  // setProbeVars writes to var file AND sets in-memory vars via MDI, then we call the macro
  const vars = buildVarMap(autoZero.value ? 0 : 1);
  emit("setProbeVars", vars);
  emit("mdi", `O<${op.wcoMacro}> CALL`);
}

function fmt(n: number | undefined): string {
  if (n == null || !Number.isFinite(n)) return "---";
  return n.toFixed(4);
}
</script>

<template>
  <div class="probePanel scroll-thin">
    <!-- Operation selector + diagram -->
    <div class="section">
      <div class="sub">Probe Operation</div>
      <div class="opRow">
        <div class="opButtons">
          <button
            v-for="op in operations"
            :key="op.id"
            class="opBtn"
            :class="{ active: selectedOp === op.id }"
            @click="selectedOp = op.id"
          >{{ op.label }}</button>
        </div>

        <!-- Probe position diagram -->
        <div class="diagramWrap">
          <!-- X+ : probe left of right face, arrow pointing right -->
          <svg v-if="selectedOp === 'x+'" viewBox="0 0 160 120" class="diagram">
            <rect x="70" y="20" width="70" height="80" rx="3" class="workpiece" />
            <text x="105" y="65" class="wpLabel">W</text>
            <!-- probe stylus -->
            <line x1="20" y1="60" x2="52" y2="60" class="stylus" />
            <circle cx="56" cy="60" r="4" class="probeTip" />
            <!-- arrow -->
            <line x1="30" y1="45" x2="50" y2="45" class="arrow" />
            <polygon points="50,41 58,45 50,49" class="arrowHead" />
            <!-- label -->
            <text x="80" y="112" class="dirLabel">X+</text>
          </svg>

          <!-- X- : probe right of left face, arrow pointing left -->
          <svg v-else-if="selectedOp === 'x-'" viewBox="0 0 160 120" class="diagram">
            <rect x="20" y="20" width="70" height="80" rx="3" class="workpiece" />
            <text x="55" y="65" class="wpLabel">W</text>
            <line x1="140" y1="60" x2="108" y2="60" class="stylus" />
            <circle cx="104" cy="60" r="4" class="probeTip" />
            <line x1="130" y1="45" x2="110" y2="45" class="arrow" />
            <polygon points="110,41 102,45 110,49" class="arrowHead" />
            <text x="80" y="112" class="dirLabel">X−</text>
          </svg>

          <!-- Y+ : probe in front (below in top-down view), arrow up -->
          <svg v-else-if="selectedOp === 'y+'" viewBox="0 0 160 120" class="diagram">
            <rect x="30" y="10" width="100" height="50" rx="3" class="workpiece" />
            <text x="80" y="40" class="wpLabel">W</text>
            <line x1="80" y1="100" x2="80" y2="78" class="stylus" />
            <circle cx="80" cy="74" r="4" class="probeTip" />
            <line x1="65" y1="95" x2="65" y2="78" class="arrow" />
            <polygon points="61,78 65,70 69,78" class="arrowHead" />
            <text x="80" y="115" class="dirLabel">Y+</text>
          </svg>

          <!-- Y- : probe behind (above in top-down view), arrow down -->
          <svg v-else-if="selectedOp === 'y-'" viewBox="0 0 160 120" class="diagram">
            <rect x="30" y="60" width="100" height="50" rx="3" class="workpiece" />
            <text x="80" y="90" class="wpLabel">W</text>
            <line x1="80" y1="20" x2="80" y2="42" class="stylus" />
            <circle cx="80" cy="46" r="4" class="probeTip" />
            <line x1="65" y1="25" x2="65" y2="42" class="arrow" />
            <polygon points="61,42 65,50 69,42" class="arrowHead" />
            <text x="80" y="115" class="dirLabel">Y−</text>
          </svg>

          <!-- Z- : side view, probe above workpiece surface, arrow down -->
          <svg v-else-if="selectedOp === 'z-'" viewBox="0 0 160 120" class="diagram">
            <!-- workpiece (side view, flat block at bottom) -->
            <rect x="20" y="70" width="120" height="30" rx="3" class="workpiece" />
            <text x="80" y="90" class="wpLabel">W</text>
            <!-- probe stylus (vertical) -->
            <line x1="80" y1="10" x2="80" y2="48" class="stylus" />
            <circle cx="80" cy="52" r="4" class="probeTip" />
            <!-- arrow -->
            <line x1="65" y1="18" x2="65" y2="45" class="arrow" />
            <polygon points="61,45 65,53 69,45" class="arrowHead" />
            <!-- side view label -->
            <text x="15" y="115" class="viewLabel">side view</text>
            <text x="80" y="115" class="dirLabel">Z−</text>
          </svg>
        </div>
      </div>
      <div class="opHint">{{ currentOp.description }}</div>
    </div>

    <div class="sep"></div>

    <!-- Parameters -->
    <div class="section">
      <div class="sub">Parameters</div>
      <div class="paramGrid">
        <label>Probe Tool</label>
        <input type="number" v-model.number="params.probeTool" min="1" step="1" @change="saveParams" />

        <label>Max Distance</label>
        <input type="number" v-model.number="params.maxDistance" min="0.1" step="0.5" @change="saveParams" />

        <label>Clearance</label>
        <input type="number" v-model.number="params.clearance" min="0.01" step="0.1" @change="saveParams" />

        <label>Slow Feed</label>
        <input type="number" v-model.number="params.slowFr" min="0" step="1" @change="saveParams" />

        <label>Fast Feed</label>
        <input type="number" v-model.number="params.fastFr" min="1" step="10" @change="saveParams" />

        <label>Traverse Feed</label>
        <input type="number" v-model.number="params.traverseFr" min="1" step="100" @change="saveParams" />

        <label>Cal Offset</label>
        <input type="number" v-model.number="params.calOffset" step="0.001" @change="saveParams" />
      </div>
      <label class="checkRow">
        <input type="checkbox" v-model="autoZero" @change="saveParams" />
        Auto Zero
      </label>
    </div>

    <div class="sep"></div>

    <!-- Run / Abort + status -->
    <div class="section">
      <div class="btnRow">
        <button
          class="runBtn"
          :class="statusClass"
          :disabled="!can.ready || probing"
          @click="runProbe"
        >
          <span v-if="probing">Probing {{ currentOp.label }}…</span>
          <span v-else>Probe {{ currentOp.label }}</span>
        </button>
        <button
          class="abortBtn"
          :disabled="!probing"
          @click="emit('abort')"
        >Abort</button>
        <button
          class="simTripBtn"
          :disabled="!probing"
          @click="emit('simulateProbeTrip')"
          title="Simulate probe contact (sim/debug only)"
        >Sim Trip</button>
      </div>

      <div class="statusRow">
        <span class="statusDot" :class="statusClass"></span>
        <span class="statusText">{{ probeStatus }}</span>
      </div>
    </div>

    <div class="sep"></div>

    <!-- Results -->
    <div class="section">
      <div class="sub">Probed Position</div>
      <div class="resultGrid">
        <div class="resultAxis"><span class="axisLabel">X</span><span class="axisVal">{{ probeTripped ? fmt(probedPosition?.[0]) : '---' }}</span></div>
        <div class="resultAxis"><span class="axisLabel">Y</span><span class="axisVal">{{ probeTripped ? fmt(probedPosition?.[1]) : '---' }}</span></div>
        <div class="resultAxis"><span class="axisLabel">Z</span><span class="axisVal">{{ probeTripped ? fmt(probedPosition?.[2]) : '---' }}</span></div>
      </div>
    </div>

    <div class="sep"></div>

    <!-- Current position reference -->
    <div class="section">
      <div class="sub">Current Work Position</div>
      <div class="resultGrid">
        <div class="resultAxis"><span class="axisLabel">X</span><span class="axisVal dim">{{ fmt(workPos[0]) }}</span></div>
        <div class="resultAxis"><span class="axisLabel">Y</span><span class="axisVal dim">{{ fmt(workPos[1]) }}</span></div>
        <div class="resultAxis"><span class="axisLabel">Z</span><span class="axisVal dim">{{ fmt(workPos[2]) }}</span></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.probePanel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  height: 100%;
}

.section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sub {
  font-size: 11px;
  font-weight: 600;
  opacity: 0.6;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.sep {
  border-top: 1px solid var(--border);
  opacity: 0.3;
}

/* Operation row: buttons left, diagram right */
.opRow {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.opButtons {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}

.opBtn {
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  min-width: 48px;
  text-align: center;
}

.opBtn.active {
  background: color-mix(in oklab, var(--fg) 15%, var(--button-bg));
  border-color: color-mix(in oklab, var(--fg) 30%, var(--border));
}

.opHint {
  font-size: 11px;
  opacity: 0.5;
  font-style: italic;
}

/* Diagram */
.diagramWrap {
  flex: 1;
  display: flex;
  justify-content: center;
  min-height: 100px;
}

.diagram {
  width: 100%;
  max-width: 180px;
  height: auto;
}

.workpiece {
  fill: color-mix(in oklab, var(--fg) 12%, var(--bg));
  stroke: var(--fg);
  stroke-width: 1.5;
  stroke-opacity: 0.5;
}

.wpLabel {
  fill: var(--fg);
  opacity: 0.3;
  font-size: 14px;
  font-weight: 700;
  text-anchor: middle;
  dominant-baseline: central;
}

.stylus {
  stroke: var(--fg);
  stroke-width: 2;
  stroke-opacity: 0.8;
}

.probeTip {
  fill: var(--warn);
  stroke: var(--fg);
  stroke-width: 1;
  stroke-opacity: 0.5;
}

.arrow {
  stroke: var(--ok);
  stroke-width: 1.5;
  stroke-opacity: 0.8;
}

.arrowHead {
  fill: var(--ok);
  opacity: 0.8;
}

.dirLabel {
  fill: var(--fg);
  opacity: 0.5;
  font-size: 11px;
  font-weight: 600;
  text-anchor: middle;
}

.viewLabel {
  fill: var(--fg);
  opacity: 0.3;
  font-size: 9px;
  font-style: italic;
  text-anchor: start;
}

/* Parameters */
.paramGrid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 10px;
  align-items: center;
}

.paramGrid label {
  font-size: 11px;
  opacity: 0.7;
}

.paramGrid input {
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 4px;
  max-width: 100px;
}

.checkRow {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  cursor: pointer;
  user-select: none;
  margin-top: 4px;
}

/* Run / Abort buttons */
.btnRow {
  display: flex;
  gap: 6px;
}

.runBtn {
  flex: 1;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
}

.abortBtn {
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  background: color-mix(in oklab, var(--danger) 20%, var(--button-bg));
  border-color: color-mix(in oklab, var(--danger) 30%, var(--border));
  color: var(--danger);
}

.abortBtn:disabled {
  opacity: 0.3;
  color: var(--fg);
  background: var(--button-bg);
  border-color: var(--border);
}

.simTripBtn {
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  background: color-mix(in oklab, #6c63ff 15%, var(--button-bg));
  border-color: color-mix(in oklab, #6c63ff 30%, var(--border));
  color: #6c63ff;
  font-style: italic;
}

.simTripBtn:disabled {
  opacity: 0.3;
  color: var(--fg);
  background: var(--button-bg);
  border-color: var(--border);
  font-style: normal;
}

.runBtn.probing {
  background: color-mix(in oklab, var(--warn) 25%, var(--button-bg));
  border-color: color-mix(in oklab, var(--warn) 40%, var(--border));
}

.runBtn.tripped {
  background: color-mix(in oklab, var(--ok) 25%, var(--button-bg));
  border-color: color-mix(in oklab, var(--ok) 40%, var(--border));
}

/* Status */
.statusRow {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}

.statusDot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--border);
}

.statusDot.probing {
  background: var(--warn);
  animation: pulse 0.8s ease-in-out infinite alternate;
}

.statusDot.tripped {
  background: var(--ok);
}

@keyframes pulse {
  from { opacity: 0.4; }
  to { opacity: 1; }
}

.statusText {
  font-size: 11px;
  font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
  opacity: 0.7;
}

/* Results */
.resultGrid {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.resultAxis {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.axisLabel {
  font-size: 11px;
  opacity: 0.6;
  width: 14px;
}

.axisVal {
  font-size: 16px;
  font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
  font-weight: 600;
}

.axisVal.dim {
  opacity: 0.5;
  font-size: 13px;
}
</style>
