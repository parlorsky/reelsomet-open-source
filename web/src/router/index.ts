import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { setupApi } from '@/api/endpoints'

const routes: RouteRecordRaw[] = [
  {
    path: '/setup',
    name: 'Setup',
    component: () => import('@/pages/SetupPage.vue'),
    meta: { public: true },
  },
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/pages/LoginPage.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('@/pages/DashboardPage.vue'),
  },
  {
    path: '/devices',
    name: 'Devices',
    component: () => import('@/pages/DevicesPage.vue'),
  },
  {
    path: '/accounts',
    name: 'Accounts',
    component: () => import('@/pages/AccountsPage.vue'),
  },
  {
    path: '/queue',
    name: 'Queue',
    component: () => import('@/pages/QueuePage.vue'),
  },
  {
    path: '/schedule',
    name: 'Schedule',
    component: () => import('@/pages/SchedulePage.vue'),
  },
  {
    path: '/scenarios',
    name: 'Scenarios',
    component: () => import('@/pages/ScenariosPage.vue'),
  },
  {
    path: '/tracks',
    name: 'Tracks',
    component: () => import('@/pages/TracksPage.vue'),
  },
  {
    path: '/models',
    name: 'Models',
    component: () => import('@/pages/ModelsPage.vue'),
  },
  {
    path: '/insights',
    name: 'Insights',
    component: () => import('@/pages/InsightsPage.vue'),
  },
  {
    path: '/engagement',
    name: 'Engagement',
    component: () => import('@/pages/EngagementPage.vue'),
  },
  {
    path: '/monitor',
    name: 'Monitor',
    component: () => import('@/pages/MonitorPage.vue'),
  },
  {
    path: '/pinterest',
    name: 'Pinterest',
    component: () => import('@/pages/PinterestPage.vue'),
  },
  {
    path: '/reddit',
    name: 'Reddit',
    component: () => import('@/pages/RedditPage.vue'),
  },
  {
    path: '/logs',
    name: 'Logs',
    component: () => import('@/pages/LogsPage.vue'),
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('@/pages/SettingsPage.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.onError((error, to) => {
  const message = `${error?.message || error}`
  const isStaleChunk = /Failed to fetch dynamically imported module|Importing a module script failed|Unable to preload CSS|Loading chunk|error loading dynamically imported module/i.test(message)
  if (!isStaleChunk || typeof window === 'undefined') {
    return
  }

  const reloadKey = 'reelsomet:chunk-reload-at'
  const lastReloadAt = Number(sessionStorage.getItem(reloadKey) || '0')
  const now = Date.now()
  if (now - lastReloadAt < 5000) {
    console.error(error)
    return
  }
  sessionStorage.setItem(reloadKey, String(now))
  window.location.assign(to.fullPath || window.location.pathname)
})

// Cache the setup status for the duration of a navigation cycle to avoid
// multiple requests within the same guard invocation.
let _setupChecked = false
let _needsSetup = false

router.beforeEach(async (to) => {
  const token = localStorage.getItem('auth_token')

  // When navigating to login, check if setup is needed first
  if (to.path === '/login') {
    if (!_setupChecked) {
      try {
        const resp = await setupApi.getStatus()
        _needsSetup = resp.data.needs_setup
        _setupChecked = true
      } catch {
        // If API unreachable, proceed to login
      }
    }
    if (_needsSetup) {
      return { path: '/setup' }
    }
    if (token) {
      return { path: '/' }
    }
    return
  }

  // Setup page is always accessible (public)
  if (to.path === '/setup') {
    return
  }

  // Protected routes need a token
  if (!to.meta.public && !token) {
    return { path: '/login' }
  }
})

/**
 * Reset the cached setup check. Call this after setup completes so the
 * guard re-checks on next navigation.
 */
export function resetSetupCache() {
  _setupChecked = false
  _needsSetup = false
}

export default router
