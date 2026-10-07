import axios from 'axios'
import { broadcastAuthExpired, clearStoredAuthToken } from '@/authSession'
import router from '@/router'

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  timeout: 15000,
  headers: {
    'Content-Type': 'application/json',
  },
})

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      clearStoredAuthToken()
      broadcastAuthExpired()
      router.replace('/login')
    }
    return Promise.reject(error)
  }
)

export default apiClient
