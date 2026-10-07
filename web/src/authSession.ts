export const AUTH_EXPIRED_EVENT = 'reelsomet:auth-expired'

export function clearStoredAuthToken() {
  localStorage.removeItem('auth_token')
}

export function broadcastAuthExpired() {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
  }
}
