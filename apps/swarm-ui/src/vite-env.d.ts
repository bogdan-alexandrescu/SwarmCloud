/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Vite sets this. True under `npm run dev`. */
  readonly DEV: boolean
  /**
   * Set to any value to make the dev server talk to the REAL API instead of the
   * fixtures in api.ts. Pair it with SWARM_API_ORIGIN so the proxy has a target:
   *
   *   SWARM_API_ORIGIN=https://swarm.example.com VITE_LIVE=1 npm run dev
   */
  readonly VITE_LIVE?: string
  /**
   * WHICH ENVIRONMENT A DEVELOPER'S OWN BUILD IS FOR, and nothing more. Read
   * by Brand.tsx (through Spine.tsx) as the third of four sources, below the
   * API's declared environment (`/v1/tenants/me`) and below the deployment's
   * `/config.json` (runtimeConfig.ts):
   *
   *   VITE_SWARM_ENV=dev npm run dev
   *
   * THE IMAGE NO LONGER COMPILES IT (DAYONE-UIENV-1). Vite inlines
   * `import.meta.env.VITE_*` when the bundle is built, so a value set here is
   * the build machine's, and a release builds one bundle for every
   * deployment. images/swarm-ui/Dockerfile declares no such ARG; the running
   * container writes `/config.json` from its ENVIRONMENT variable instead, and
   * src/__tests__/runtimeConfig.test.ts holds both ends.
   *
   * DECLARED, NEVER INFERRED. With none of the four set, the header says
   * ENVIRONMENT UNKNOWN and treats the tab as production -- loudly -- rather
   * than guessing from the hostname. A hostname table in the app would be a
   * copy of terraform/environments/<env>/<env>.tfvars kept in a file nothing
   * checks against it, and a stale copy of it is exactly the "this says dev
   * while pointed at prod" failure the badge exists to prevent.
   */
  readonly VITE_SWARM_ENV?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
