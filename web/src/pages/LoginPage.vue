<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useToast } from '@/composables/useToast'

const auth = useAuthStore()
const toast = useToast()
const router = useRouter()
const password = ref('')
const errorMsg = ref('')

type LoginError = Error & {
  needsSetup?: boolean
}

async function handleLogin() {
  errorMsg.value = ''
  if (!password.value.trim()) {
    errorMsg.value = 'Password is required'
    return
  }
  try {
    await auth.login(password.value)
    toast.success('Logged in successfully')
  } catch (err) {
    // Check if the server says setup is needed
    if ((err as LoginError).needsSetup) {
      router.replace('/setup')
      return
    }
    errorMsg.value = (err as Error).message || 'Login failed'
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-bg-primary">
    <div class="card w-full max-w-sm shadow-2xl">
      <div class="mb-6 text-center">
        <h1 class="text-2xl font-bold text-accent">Reelsomet</h1>
        <p class="mt-1 text-sm text-text-secondary">Instagram Automation Farm</p>
      </div>

      <form @submit.prevent="handleLogin" class="space-y-4">
        <div>
          <label class="mb-1 block text-sm text-text-secondary">Password</label>
          <input
            v-model="password"
            type="password"
            class="input-field"
            placeholder="Enter password"
            autofocus
          />
        </div>

        <div v-if="errorMsg" class="rounded bg-danger/20 px-3 py-2 text-sm text-danger">
          {{ errorMsg }}
        </div>

        <button
          type="submit"
          class="btn-primary w-full"
          :disabled="auth.loading"
        >
          {{ auth.loading ? 'Signing in...' : 'Sign In' }}
        </button>
      </form>
    </div>
  </div>
</template>
