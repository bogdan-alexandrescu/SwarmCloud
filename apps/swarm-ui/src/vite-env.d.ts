/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Vite sets this. True under `npm run dev`. */
  readonly DEV: boolean
  /**
   * Set to any value to make the dev server talk to the REAL API instead of the
   * fixtures in api.ts. Pair it with SWARM_API_ORIGIN so the proxy has a target:
   *
   *   SWARM_API_ORIGIN=https://swarm.saga.xyz VITE_LIVE=1 npm run dev
   */
  readonly VITE_LIVE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
