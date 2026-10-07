<script setup lang="ts">
defineProps<{
  title?: string
  description?: string
  count?: string | number
  flush?: boolean
}>()
</script>

<template>
  <section :class="flush ? 'surface-panel' : 'surface-panel p-4'">
    <div
      v-if="title || description || count !== undefined || $slots.actions"
      :class="flush ? 'section-heading mb-0 px-4 py-3' : 'section-heading'"
    >
      <div class="min-w-0">
        <div class="flex min-w-0 items-center gap-2">
          <h2 v-if="title" class="truncate text-base font-semibold">{{ title }}</h2>
          <span v-if="count !== undefined" class="rounded-full bg-bg-tertiary px-2 py-0.5 text-xs text-text-secondary">
            {{ count }}
          </span>
        </div>
        <p v-if="description" class="mt-1 text-sm text-text-secondary">{{ description }}</p>
      </div>
      <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center justify-end gap-2">
        <slot name="actions" />
      </div>
    </div>
    <slot />
  </section>
</template>
