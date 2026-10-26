// REVIEW HARNESS ONLY (ACCOUNTS-TIMELINE): the real app, with `./api`'s
// accounts and parked-task reads answered from proposal B's scenarios, so
// the Timeline can be rendered against B2, B3 and B6. Never shipped.
import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const ui = resolve(__dirname, '../../../apps/swarm-ui')
export default defineConfig({
  root: ui,
  plugins: [react()],
  resolve: { alias: [{ find: /^\.\/api$/, replacement: resolve(__dirname, 'api.ts') }] },
  build: { outDir: '/tmp/tl-harness', emptyOutDir: true },
})
