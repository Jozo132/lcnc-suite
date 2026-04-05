<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import MachineBtn from "./MachineBtn.vue";
import MachineInput from "./MachineInput.vue";
import ToolPreview from "./ToolPreview.vue";
import { send, lastReply, connected } from "./lcncWs";
import { toolTypeLabel } from "./toolTypes";

const props = defineProps<{
  toolNumber: number;
  currentTool: number;
  toolDiameter: number | null;
  toolLength: number | null;
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

// ─── Tool table for preview ──────────────────────────────────
const tools = ref<Record<string, any>[]>([]);

function fetchTools() { send({ cmd: "get_tool_table" }); }

watch(lastReply, (reply) => {
  if (reply?.ok && Array.isArray(reply.tools)) {
    tools.value = reply.tools;
  }
}, { flush: "sync" });

onMounted(fetchTools);
watch(connected, (val) => { if (val) setTimeout(fetchTools, 300); });
watch(() => props.currentTool, fetchTools);

const currentToolData = computed(() =>
  tools.value.find(t => t.T === props.currentTool) ?? null
);

const toolTypeLbl = computed(() => toolTypeLabel(currentToolData.value?.type));

// ─── Preview frame sizing ────────────────────────────────────
const previewFrameRef = ref<HTMLElement | null>(null);
const previewSize = reactive({ w: 0, h: 0 });
let _previewRo: ResizeObserver | null = null;

onMounted(() => {
  _previewRo = new ResizeObserver(entries => {
    for (const e of entries) {
      previewSize.h = Math.floor(e.contentRect.height);
      previewSize.w = Math.floor(e.contentRect.width);
    }
  });
  if (previewFrameRef.value) _previewRo.observe(previewFrameRef.value);
});

watch(previewFrameRef, (el, old) => {
  if (old) _previewRo?.unobserve(old);
  if (el) _previewRo?.observe(el);
});

onBeforeUnmount(() => _previewRo?.disconnect());
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

      <div class="toolStats inset-panel scroll-thin">
        <div class="spActualRow">
          <span class="label-muted md">Current Tool</span>
          <span class="val-status md mono">T{{ currentTool }}</span>
        </div>
        <div class="spActualRow">
          <span class="label-muted md">Pocket</span>
          <span class="val-status md mono">{{ currentToolData?.P ?? '---' }}</span>
        </div>
        <div class="spActualRow">
          <span class="label-muted md">Diameter</span>
          <span class="val-status md mono">{{ toolDiameter != null ? toolDiameter.toFixed(3) : '---' }}</span>
        </div>
        <div class="spActualRow">
          <span class="label-muted md">Z Offset</span>
          <span class="val-status md mono">{{ toolLength != null ? toolLength.toFixed(3) : '---' }}</span>
        </div>
        <div class="spActualRow">
          <span class="label-muted md">Type</span>
          <span class="val-status md">{{ toolTypeLbl }}</span>
        </div>
        <div class="spActualRow">
          <span class="label-muted md">Description</span>
          <span class="val-status md toolDesc">{{ currentToolData?.description || '---' }}</span>
        </div>
      </div>
    </div>

    <div v-if="currentToolData && currentTool > 0" ref="previewFrameRef" class="toolPreviewFrame inset-panel">
      <ToolPreview v-if="previewSize.w > 0"
        :diameter="currentToolData.D || 0"
        :length="Math.abs(currentToolData.Z) || 0"
        :fluteLength="currentToolData.flute_length || Math.abs(currentToolData.Z || 0) * 0.6"
        :shaftDiameter="currentToolData.shoulder_diameter || undefined"
        :toolType="currentToolData.type || undefined"
        :cornerRadius="currentToolData.corner_radius || undefined"
        :taperAngle="currentToolData.taper_angle || undefined"
        :pointAngle="currentToolData.point_angle || undefined"
        :tipDiameter="currentToolData.tip_diameter || undefined"
        :bodyLength="currentToolData.body_length || undefined"
        :width="previewSize.w" :height="previewSize.h"
      />
    </div>
  </div>
</template>

<style scoped>
.toolStrip {
  display: flex;
  gap: var(--gap-controls);
  align-items: stretch;
}
.toolStrip > * {
  flex-shrink: 0;
}

.toolPreviewFrame {
  width: 120px;
  flex-shrink: 0;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: var(--gap-controls);
}

.toolInputRow {
  align-items: stretch;
}

.toolLabel {
  align-self: center;
}

.toolNumInput {
  width: 60px;
}

.toolActionRow {
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

.toolStats {
  display: flex;
  flex-direction: column;
  gap: var(--gap-tight);
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.toolDesc {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
</style>
