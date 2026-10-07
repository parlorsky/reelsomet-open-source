<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

defineProps<{
  isDark: boolean
}>()

const emit = defineEmits<{
  refresh: []
  toggleTheme: []
}>()

const pageTitle = computed(() => {
  const title = route.meta.title || route.name || 'Page'
  return String(title)
})
</script>

<template>
  <header class="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-border-default bg-bg-secondary/95 px-5 backdrop-blur-md">
    <div class="flex min-w-0 items-center gap-3">
      <div class="hidden h-8 w-px bg-border-subtle sm:block"></div>
      <div class="min-w-0">
        <p class="text-[11px] font-bold uppercase tracking-wide text-text-muted">Workspace</p>
        <h1 class="truncate text-base font-semibold">{{ pageTitle }}</h1>
      </div>
    </div>

    <div class="flex items-center gap-3">
      <button
        type="button"
        class="hidden w-80 items-center gap-2 rounded-lg border border-border-default bg-bg-primary px-3 py-2 text-left text-sm text-text-muted transition-colors hover:bg-bg-hover lg:flex"
      >
        <span>Search, account, file, device...</span>
        <span class="ml-auto rounded border border-border-default px-1.5 text-xs">Cmd</span>
        <span class="rounded border border-border-default px-1.5 text-xs">K</span>
      </button>
      <button class="icon-button" :title="isDark ? 'Switch to light theme' : 'Switch to dark theme'" @click="emit('toggleTheme')">
        {{ isDark ? 'L' : 'D' }}
      </button>
      <button class="btn-secondary btn-sm" title="Refresh" @click="emit('refresh')">
        <span aria-hidden="true">R</span>
        <span class="hidden sm:inline">Refresh</span>
      </button>
    </div>
  </header>
</template>
