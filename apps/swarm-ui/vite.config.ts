import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the API is proxied so the app talks to a same-origin /v1,
// exactly as it does in production behind the load balancer. Point
// SWARM_API_ORIGIN at https://swarm.saga.xyz once DNS resolves; until then the
// app runs on fixtures (see src/api.ts) and never reaches the network.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: process.env.SWARM_API_ORIGIN
      ? { '/v1': { target: process.env.SWARM_API_ORIGIN, changeOrigin: true, secure: true } }
      : undefined,
  },
  build: { outDir: 'dist', sourcemap: true },
})
