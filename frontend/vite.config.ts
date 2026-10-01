import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // In dev, the UI runs on Vite's own port while the Python backend (`scoutqa ui`) runs on its own.
    // `changeOrigin` rewrites the Host header to match the backend, satisfying its Host-pinning check;
    // in production the built frontend is served *by* the backend, so this proxy doesn't apply at all.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8766', changeOrigin: true },
    },
  },
  build: {
    outDir: '../src/scoutqa/webui/static',
    emptyOutDir: true,
  },
})
