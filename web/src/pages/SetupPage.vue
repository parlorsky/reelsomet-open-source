<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { setupApi } from '@/api/endpoints'
import { resetSetupCache } from '@/router'

const router = useRouter()
const setupToken = ref('')
const password = ref('')
const confirmPassword = ref('')
const telegramToken = ref('')
const telegramChatId = ref('')
const deviceToken = ref('')
const busy = ref(false)
const error = ref('')
const canSubmit = computed(() => setupToken.value.trim() && password.value.length >= 6 && password.value === confirmPassword.value)
const serverAddress = window.location.host

onMounted(async () => {
  try {
    if (!(await setupApi.getStatus()).data.needs_setup) router.replace('/login')
  } catch { error.value = 'Cannot reach the server. Check that the backend is running.' }
})

async function completeSetup() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    const payload: { admin_password: string; telegram_token?: string; telegram_chat_id?: number } = { admin_password: password.value }
    if (telegramToken.value.trim()) payload.telegram_token = telegramToken.value.trim()
    if (telegramChatId.value.trim()) {
      const id = Number(telegramChatId.value)
      if (!Number.isSafeInteger(id)) throw new Error('Telegram chat ID must be an integer.')
      payload.telegram_chat_id = id
    }
    const result = await setupApi.complete(payload, setupToken.value.trim())
    deviceToken.value = result.data.device_token
    localStorage.setItem('auth_token', result.data.access_token)
    setupToken.value = ''
    password.value = ''
    confirmPassword.value = ''
    resetSetupCache()
  } catch (err: any) {
    error.value = err.response?.data?.detail || err.message || 'Setup failed.'
  } finally { busy.value = false }
}
</script>

<template>
  <main class="min-h-screen bg-bg-primary px-5 py-12">
    <div class="mx-auto max-w-xl">
      <p class="mb-3 text-xs font-semibold uppercase tracking-widest text-accent">Reelsomet · Open source</p>
      <h1 class="mb-3 text-3xl font-semibold text-text-primary">Your devices. Your workspace.</h1>
      <p class="mb-8 text-text-secondary">Set up your administrator account, then connect an Android device. Reelsomet is MIT licensed and needs no activation key.</p>
      <form v-if="!deviceToken" class="card space-y-5" @submit.prevent="completeSetup">
        <div>
          <label for="setup-token" class="mb-2 block text-sm font-medium">One-time setup token</label>
          <input id="setup-token" v-model="setupToken" type="password" autocomplete="off" class="input-field w-full" required />
          <p class="mt-2 text-xs text-text-secondary">Copy the token printed when you start the server. It expires after this setup.</p>
        </div>
        <div>
          <label for="password" class="mb-2 block text-sm font-medium">Administrator password</label>
          <input id="password" v-model="password" type="password" autocomplete="new-password" minlength="6" class="input-field w-full" required />
        </div>
        <div>
          <label for="confirm-password" class="mb-2 block text-sm font-medium">Confirm password</label>
          <input id="confirm-password" v-model="confirmPassword" type="password" autocomplete="new-password" class="input-field w-full" required />
          <p v-if="confirmPassword && password !== confirmPassword" class="mt-2 text-sm text-danger">Passwords do not match.</p>
        </div>
        <details class="rounded-lg border border-border p-4">
          <summary class="cursor-pointer text-sm font-medium">Telegram notifications (optional)</summary>
          <label for="telegram-token" class="mb-2 mt-4 block text-sm">Bot token</label>
          <input id="telegram-token" v-model="telegramToken" type="password" autocomplete="off" class="input-field w-full" />
          <label for="telegram-chat" class="mb-2 mt-3 block text-sm">Administrator chat ID</label>
          <input id="telegram-chat" v-model="telegramChatId" inputmode="numeric" class="input-field w-full" />
          <p class="mt-2 text-xs text-text-secondary">Restart the server after setup to start the optional bot.</p>
        </details>
        <p v-if="error" role="alert" class="rounded-lg bg-danger/10 p-3 text-sm text-danger">{{ error }}</p>
        <button type="submit" :disabled="!canSubmit || busy" class="btn-primary w-full disabled:opacity-50">{{ busy ? 'Creating workspace…' : 'Create workspace' }}</button>
      </form>
      <section v-else class="card space-y-5">
        <h2 class="text-xl font-semibold">Workspace ready</h2>
        <p class="text-text-secondary">Install the Android app you build from this repository. In VPS Connection, enter these details and enable its accessibility service.</p>
        <dl class="space-y-3">
          <div><dt class="text-xs text-text-secondary">Server address</dt><dd class="font-mono">{{ serverAddress }}</dd></div>
          <div><dt class="text-xs text-text-secondary">Device ID</dt><dd class="font-mono">1</dd></div>
          <div><dt class="text-xs text-text-secondary">Device token — keep private</dt><dd class="mt-1 break-all rounded bg-bg-tertiary p-3 font-mono text-sm select-all">{{ deviceToken }}</dd></div>
        </dl>
        <p class="text-xs text-text-secondary">For a physical phone, use a reachable LAN address or your HTTPS domain instead of localhost.</p>
        <button class="btn-primary w-full" @click="router.push('/')">Open dashboard</button>
      </section>
    </div>
  </main>
</template>
