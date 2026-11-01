import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: `npm run dev` serves on 5173 and proxies /api to the FastAPI backend on 8000.
// Build: `npm run build` emits web/frontend/dist, which FastAPI serves at / in prod.
export default defineConfig({
  plugins: [react()],
  server: {
    // Pin to IPv4 so it matches the backend + the printed URL (Vite's default `localhost`
    // resolves to ::1 on Windows, which 127.0.0.1 requests miss).
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/login': 'http://127.0.0.1:8000',
      '/logout': 'http://127.0.0.1:8000',
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
