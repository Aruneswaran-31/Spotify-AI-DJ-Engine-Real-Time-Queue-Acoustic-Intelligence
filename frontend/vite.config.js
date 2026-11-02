import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.VITE_BACKEND || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/static': { target: backend, changeOrigin: true },
      '/ws': { target: backend, ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
