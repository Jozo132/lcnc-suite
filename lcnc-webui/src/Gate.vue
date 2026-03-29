<script setup lang="ts">
import { computed } from "vue";
import { usePermissions, type Permissions } from "./permissions";

const props = defineProps<{ gate: keyof Permissions }>();
const permissions = usePermissions();
const allow = computed(() => permissions.value[props.gate]);
</script>

<template>
  <fieldset :disabled="!allow" :data-gate="gate" class="fs-reset">
    <legend v-if="$slots.exempt" class="gate-exempt">
      <slot name="exempt" />
    </legend>
    <slot />
  </fieldset>
</template>

<style scoped>
.gate-exempt {
  float: none;
  padding: 0;
  width: auto;
}
</style>
