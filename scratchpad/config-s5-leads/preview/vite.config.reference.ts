// Copied into apps/swarm-ui as a temporary config to build this preview: `npx vite build -c <copy>` (output /tmp/s5-dist).
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { readFileSync } from 'node:fs'
const UI = __dirname
const pkg = JSON.parse(readFileSync(resolve(UI, 'package.json'), 'utf8'))
const names = [...Object.keys(pkg.dependencies), 'react/jsx-runtime', 'react/jsx-dev-runtime', 'react-dom/client']
export default defineConfig({
  root: resolve(__dirname, '../../scratchpad/config-s5-leads/preview'),
  base: './',
  plugins: [react()],
  define: { 'import.meta.env.VITE_LIVE': JSON.stringify('1') },
  resolve: { alias: names.sort((a, b) => b.length - a.length).map((n) => ({ find: n, replacement: resolve(UI, 'node_modules', n) })) },
  build: { rollupOptions: { input: resolve(__dirname, '../../scratchpad/config-s5-leads/preview/preview.html') }, target: 'es2022', outDir: '/tmp/s5-dist', emptyOutDir: true },
})
