<script setup lang="ts">
defineProps<{
  title: string
  description?: string
}>()
</script>

<template>
  <div class="flex min-h-40 flex-col items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-tertiary/45 px-6 py-10 text-center">
    <p class="text-sm font-semibold text-text-primary">{{ title }}</p>
    <p v-if="description" class="mt-1 max-w-md text-sm text-text-secondary">{{ description }}</p>
    <div v-if="$slots.actions" class="mt-4 flex flex-wrap justify-center gap-2">
      <slot name="actions" />
    </div>
  </div>
</template>
