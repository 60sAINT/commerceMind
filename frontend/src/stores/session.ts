import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

type Theme = 'light' | 'dark'

interface SessionState {
  userId: string
  sessionId: string
  baseUrl: string
  theme: Theme
  setSessionId: (sessionId: string) => void
  setBaseUrl: (baseUrl: string) => void
  setTheme: (theme: Theme) => void
  setUserId: (userId: string) => void
}

function initialTheme(): Theme {
  if (typeof localStorage !== 'undefined') {
    const saved = localStorage.getItem('commercemind.theme')
    if (saved === 'light' || saved === 'dark') return saved
  }
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
    ? 'dark'
    : 'light'
}

export const useSessionStore = create<SessionState>()(
  persist(
    (set) => ({
      userId: 'customer-001',
      sessionId: '',
      baseUrl: import.meta.env.VITE_API_BASE_URL ?? '/api',
      theme: initialTheme(),
      setUserId: (userId) => set({ userId }),
      setSessionId: (sessionId) => set({ sessionId }),
      setBaseUrl: (baseUrl) => set({ baseUrl: baseUrl.trim() || '/api' }),
      setTheme: (theme) => set({ theme }),
    }),
    { name: 'commercemind.session', storage: createJSONStorage(() => localStorage) },
  ),
)
