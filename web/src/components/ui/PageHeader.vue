<script setup lang="ts">
defineProps<{
  eyebrow?: string
  title: string
  description?: string
  meta?: string
}>()
</script>

<template>
  <div class="page-header">
    <div class="min-w-0">
      <p v-if="eyebrow" class="page-eyebrow">{{ eyebrow }}</p>
      <div class="flex min-w-0 flex-wrap items-end gap-x-3 gap-y-1">
        <h1 class="page-title">{{ title }}</h1>
        <span v-if="meta" class="pb-1 text-sm text-text-muted">{{ meta }}</span>
      </div>
      <p v-if="description" class="mt-1 max-w-3xl text-sm text-text-secondary">{{ description }}</p>
    </div>
    <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center justify-end gap-2">
      <slot name="actions" />
    </div>
  </div>
</template>
