<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()

interface NavItem {
  path: string
  label: string
  icon: string
}

interface NavSection {
  title: string
  items: NavItem[]
}

const props = defineProps<{
  collapsed: boolean
}>()

const emit = defineEmits<{
  'update:collapsed': [value: boolean]
}>()

const navSections: NavSection[] = [
  {
    title: 'Farm',
    items: [
      { path: '/', label: 'Dashboard', icon: 'D' },
      { path: '/devices', label: 'Devices', icon: 'P' },
      { path: '/accounts', label: 'Accounts', icon: 'A' },
      { path: '/queue', label: 'Queue', icon: 'Q' },
      { path: '/schedule', label: 'Schedule', icon: 'S' },
    ],
  },
  {
    title: 'Content',
    items: [
      { path: '/scenarios', label: 'Scenarios', icon: 'S' },
      { path: '/tracks', label: 'Tracks', icon: 'T' },
      { path: '/models', label: 'Models', icon: 'M' },
      { path: '/pinterest', label: 'Pinterest', icon: 'P' },
      { path: '/reddit', label: 'Reddit', icon: 'R' },
    ],
  },
  {
    title: 'Analytics',
    items: [
      { path: '/insights', label: 'Insights', icon: 'I' },
      { path: '/engagement', label: 'Engagement', icon: 'E' },
      { path: '/monitor', label: 'Monitor', icon: 'M' },
    ],
  },
  {
    title: 'System',
    items: [
      { path: '/logs', label: 'Logs', icon: 'L' },
      { path: '/settings', label: 'Settings', icon: 'C' },
    ],
  },
]

const sidebarWidth = computed(() => props.collapsed ? 'w-[68px]' : 'w-60')

function isActive(path: string): boolean {
  if (path === '/') return route.path === '/'
  return route.path === path || route.path.startsWith(`${path}/`)
}
</script>

<template>
  <aside
    :class="[sidebarWidth, 'fixed left-0 top-0 z-40 flex h-screen flex-col border-r border-border-default bg-bg-secondary transition-all duration-200']"
  >
    <div class="flex h-16 items-center gap-3 px-4">
      <router-link to="/" class="flex min-w-0 flex-1 items-center gap-3">
        <span class="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent text-sm font-bold text-accent-fg">
          R
        </span>
        <span v-if="!collapsed" class="truncate text-lg font-semibold text-text-primary">Reelsomet</span>
      </router-link>
      <button
        class="icon-button h-8 w-8"
        @click="emit('update:collapsed', !collapsed)"
        :title="collapsed ? 'Expand' : 'Collapse'"
      >
        {{ collapsed ? '>>' : '<<' }}
      </button>
    </div>

    <nav class="flex-1 overflow-y-auto px-3 py-3">
      <section v-for="section in navSections" :key="section.title" class="mb-5 last:mb-0">
        <div v-if="!collapsed" class="mb-2 px-2 text-xs font-bold uppercase tracking-wide text-text-muted">
          {{ section.title }}
        </div>
        <router-link
          v-for="item in section.items"
          :key="item.path"
          :to="item.path"
          :class="[
            'group my-0.5 flex h-10 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors',
            isActive(item.path)
              ? 'bg-accent-muted text-accent'
              : 'text-text-secondary hover:bg-bg-hover hover:text-text-primary',
            collapsed ? 'justify-center px-0' : '',
          ]"
          :title="collapsed ? item.label : undefined"
        >
          <span
            :class="[
              'inline-grid h-5 w-5 shrink-0 place-items-center text-[13px] font-semibold',
              isActive(item.path) ? 'text-accent' : 'text-text-muted group-hover:text-text-secondary',
            ]"
          >
            {{ item.icon }}
          </span>
          <span v-if="!collapsed" class="truncate">{{ item.label }}</span>
        </router-link>
      </section>
    </nav>

    <div class="border-t border-border-subtle p-3">
      <button
        @click="auth.logout()"
        :class="[
          'flex h-10 w-full items-center gap-3 rounded-lg px-3 text-sm font-medium text-text-secondary transition-colors hover:bg-bg-hover hover:text-danger',
          collapsed ? 'justify-center px-0' : '',
        ]"
        :title="collapsed ? 'Logout' : undefined"
      >
        <span class="inline-flex w-5 justify-center text-lg">&larr;</span>
        <span v-if="!collapsed">Logout</span>
      </button>
    </div>
  </aside>
</template>
