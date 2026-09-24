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
  /**
   * WHICH ENVIRONMENT THIS BUNDLE WAS BUILT FOR. Read by Brand.tsx, and the
   * only thing allowed to make the product header say `PROD` or `DEV`.
   *
   *   VITE_SWARM_ENV=dev npm run build
   *
   * DECLARED, NEVER INFERRED. Left unset, the header says ENVIRONMENT UNKNOWN
   * and treats the tab as production -- loudly -- rather than guessing from
   * the hostname. A hostname table in the app would be a copy of
   * terraform/environments/<env>/<env>.tfvars kept in a file nothing checks
   * against it, and a stale copy of it is exactly the "this says dev while
   * pointed at prod" failure the badge exists to prevent.
   *
   * THE DEPLOYED BUILD PASSES IT. scripts/build-images.sh (`generate_config`)
   * gives the swarm-ui image `--build-arg VITE_SWARM_ENV=${ENVIRONMENT}`, and
   * images/swarm-ui/Dockerfile turns that ARG into an ENV before `npm run
   * build`; tests/unit/scripts/test_ui_build_declares_its_environment.py holds
   * both ends. A local `npm run build` with nothing set still says ENVIRONMENT
   * UNKNOWN, which is the point: nothing here infers it.
   */
  readonly VITE_SWARM_ENV?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
