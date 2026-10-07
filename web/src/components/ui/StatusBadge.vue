<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  status: string
}>()

const colorClass = computed(() => {
  const s = props.status.toLowerCase()
  if (['active', 'online', 'posted', 'completed', 'ok', 'running'].includes(s)) {
    return 'bg-success-muted text-success'
  }
  if (['paused', 'pending', 'scheduled', 'queued', 'uploading', 'posting', 'creating', 'retry_waiting', 'warning', 'login_required', 'needs_create', 'needs_attention'].includes(s)) {
    return 'bg-warning-muted text-warning'
  }
  if (['blocked', 'failed', 'error', 'aborted'].includes(s)) {
    return 'bg-danger-muted text-danger'
  }
  return 'bg-bg-tertiary text-text-secondary'
})

const label = computed(() => {
  return props.status.replace(/_/g, ' ')
})
</script>

<template>
  <span
    class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize"
    :class="colorClass"
  >
    {{ label }}
  </span>
</template>
