import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served same-origin by the studio Python pod at /studio, so the app is based at
// /studio/ and every /v1/* fetch rides the same-origin hanzo_token cookie (no CORS).
export default defineConfig({
  base: '/studio/',
  plugins: [react()],
  build: { outDir: 'dist', sourcemap: false },
})
