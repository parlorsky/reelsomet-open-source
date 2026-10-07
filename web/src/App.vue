<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useWebSocketStore } from '@/stores/websocket'
import AppSidebar from '@/components/layout/AppSidebar.vue'
import AppHeader from '@/components/layout/AppHeader.vue'
import AppStatusBar from '@/components/layout/AppStatusBar.vue'
import Toast from '@/components/ui/Toast.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ws = useWebSocketStore()

const showAppChrome = computed(() => route.meta.public !== true)
const authReady = ref(false)
const sidebarCollapsed = ref(false)
const themeMode = ref<'light' | 'dark' | 'system'>('system')
const systemDark = ref(false)

const refreshKey = ref(0)
provide('refreshKey', refreshKey)

const isDark = computed(() => {
  return themeMode.value === 'system' ? systemDark.value : themeMode.value === 'dark'
})

function handleRefresh() {
  refreshKey.value++
}

function applyTheme() {
  document.documentElement.classList.toggle('dark', isDark.value)
}

function setTheme(mode: 'light' | 'dark' | 'system') {
  themeMode.value = mode
  localStorage.setItem('reelsomet:theme', mode)
  applyTheme()
}

function toggleTheme() {
  setTheme(isDark.value ? 'light' : 'dark')
}

function initializeTheme() {
  const stored = localStorage.getItem('reelsomet:theme')
  themeMode.value = stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system'
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  systemDark.value = media.matches
  const onChange = (event: MediaQueryListEvent) => {
    systemDark.value = event.matches
    if (themeMode.value === 'system') applyTheme()
  }
  media.addEventListener('change', onChange)
  applyTheme()
  return () => media.removeEventListener('change', onChange)
}

async function initializeAuth() {
  if (!auth.isAuthenticated) {
    authReady.value = true
    return
  }

  const isValid = await auth.verifyToken()
  if (isValid) {
    ws.connect()
  } else if (route.meta.public !== true) {
    await router.replace('/login')
  }

  authReady.value = true
}

let cleanupTheme: (() => void) | null = null

onMounted(() => {
  cleanupTheme = initializeTheme()
  void initializeAuth()
})

watch(
  () => auth.isAuthenticated,
  (isAuth) => {
    if (!authReady.value) return
    if (isAuth) ws.connect()
    else ws.disconnect()
  }
)

onUnmounted(() => {
  cleanupTheme?.()
  ws.disconnect()
})
</script>

<template>
  <Toast />

  <div v-if="!authReady" class="min-h-screen bg-bg-primary"></div>

  <template v-else-if="!showAppChrome">
    <router-view />
  </template>

  <template v-else>
    <AppSidebar v-model:collapsed="sidebarCollapsed" />
    <div
      :class="[
        sidebarCollapsed ? 'ml-[68px]' : 'ml-60',
        'flex min-h-screen flex-col pb-8 transition-[margin] duration-200',
      ]"
    >
      <AppHeader :is-dark="isDark" @refresh="handleRefresh" @toggle-theme="toggleTheme" />
      <main class="mx-auto w-full max-w-[1760px] flex-1 px-4 py-5 sm:px-5 lg:px-6">
        <router-view :key="refreshKey" />
      </main>
    </div>
    <AppStatusBar />
  </template>
</template>
