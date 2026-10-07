import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { AUTH_EXPIRED_EVENT, clearStoredAuthToken } from '@/authSession'
import { authApi } from '@/api/endpoints'
import router from '@/router'

type AuthLoginError = Error & {
  needsSetup?: boolean
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('auth_token'))
  const loginError = ref<string | null>(null)
  const loading = ref(false)

  const isAuthenticated = computed(() => !!token.value)

  function clearAuthState() {
    token.value = null
    loginError.value = null
    clearStoredAuthToken()
  }

  if (typeof window !== 'undefined') {
    window.addEventListener(AUTH_EXPIRED_EVENT, clearAuthState)
  }

  async function login(password: string) {
    loading.value = true
    loginError.value = null
    try {
      const res = await authApi.login({ password })
      const t = res.data.access_token || res.data.token || ''
      token.value = t
      localStorage.setItem('auth_token', t)
      await router.push('/')
    } catch (err: any) {
      const needsSetup = err.response?.data?.needs_setup === true
      const msg = needsSetup
        ? 'Setup is required before signing in'
        : err.response?.data?.detail || err.response?.data?.error || err.message || 'Login failed'
      loginError.value = msg
      const authError = new Error(msg) as AuthLoginError
      authError.needsSetup = needsSetup
      throw authError
    } finally {
      loading.value = false
    }
  }

  function logout() {
    clearAuthState()
    router.push('/login')
  }

  async function verifyToken(): Promise<boolean> {
    if (!token.value) return false
    try {
      const res = await authApi.verify()
      return res.data.valid
    } catch {
      clearAuthState()
      return false
    }
  }

  return { token, loginError, loading, isAuthenticated, login, logout, verifyToken }
})
