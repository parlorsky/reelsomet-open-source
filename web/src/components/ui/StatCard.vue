<script setup lang="ts">
defineProps<{
  title: string
  value: string | number
  icon: string
  color?: 'accent' | 'success' | 'warning' | 'danger'
  subtitle?: string
}>()
</script>

<template>
  <div class="metric-card flex items-start gap-3">
    <div
      class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-lg"
      :class="{
        'bg-accent/20 text-accent': color === 'accent' || !color,
        'bg-success/20 text-success': color === 'success',
        'bg-warning/20 text-warning': color === 'warning',
        'bg-danger/20 text-danger': color === 'danger',
      }"
    >
      {{ icon }}
    </div>
    <div class="min-w-0 flex-1">
      <p class="text-xs font-semibold uppercase text-text-muted">{{ title }}</p>
      <p class="mt-1 text-2xl font-semibold tabular-nums">{{ value }}</p>
      <p v-if="subtitle" class="mt-0.5 text-xs text-text-secondary">{{ subtitle }}</p>
    </div>
  </div>
</template>
