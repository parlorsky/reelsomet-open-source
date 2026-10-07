<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
}>()
</script>

<template>
  <aside class="inspector-panel">
    <div class="border-b border-border-subtle px-4 py-3">
      <h2 class="truncate text-sm font-semibold">{{ title }}</h2>
      <p v-if="subtitle" class="mt-0.5 truncate text-xs text-text-secondary">{{ subtitle }}</p>
    </div>
    <div class="space-y-4 p-4">
      <slot />
    </div>
  </aside>
</template>
