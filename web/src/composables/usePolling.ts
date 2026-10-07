import { ref, onMounted, onUnmounted } from 'vue'

export function usePolling(fetchFn: () => Promise<void>, intervalMs: number = 30000) {
  const loading = ref(false)
  const error = ref<string | null>(null)
  let timer: ReturnType<typeof setInterval> | null = null

  async function refresh() {
    loading.value = true
    error.value = null
    try {
      await fetchFn()
    } catch (err: any) {
      error.value = err.response?.data?.detail || err.message || 'Request failed'
    } finally {
      loading.value = false
    }
  }

  function start() {
    refresh()
    timer = setInterval(refresh, intervalMs)
  }

  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  onMounted(start)
  onUnmounted(stop)

  return { loading, error, refresh, start, stop }
}
