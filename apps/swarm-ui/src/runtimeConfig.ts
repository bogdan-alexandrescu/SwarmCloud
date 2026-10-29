// WHAT THE DEPLOYMENT SERVING THIS CONSOLE SAYS ABOUT ITSELF, read at run time.
//
// ONE IMAGE, EVERY DEPLOYMENT (DAYONE-UIENV-1; design day-one-install.md
// section 6.2). The environment used to be compiled in: the swarm-ui image was
// built with `--build-arg VITE_SWARM_ENV=<env>`, and Vite inlined it into the
// bundle, so a dev image and a prod image were two different images and the
// release could not promote one bundle from one deployment to the next. Now
// the image compiles no environment. When the container starts, an entrypoint
// script (images/swarm-ui/Dockerfile) writes `/config.json` from the
// container's own ENVIRONMENT variable, and nginx (images/swarm-ui/nginx.conf)
// serves it uncached. This module is the one reader of that file.
//
// FAILS QUIET, NEVER WRONG. A missing file (an image older than this change),
// the dev server's index.html, a network failure, or a value that is not a
// plain environment name all read as "the deployment declared nothing". That
// is not a guess: Brand.tsx then falls through to the host, and from there to
// the loud ENVIRONMENT UNKNOWN, which is the honest answer when nothing said.
//
// WHEN THE READ STARTS. A built bundle starts it the first time anything asks
// for the environment, which is the frame's first render; the file is a small
// static one on the same origin, so it lands well before `/v1/tenants/me`,
// whose own declared answer outranks it anyway (Brand.tsx). Under the Vite dev
// server and in tests (`import.meta.env.DEV`) nothing is fetched behind the
// caller: there is no `/config.json` there, and `VITE_SWARM_ENV` still names
// a developer's environment as it always did.

/** Where the container serves the file. Same origin, so IAP fronts it too. */
export const RUNTIME_CONFIG_PATH = '/config.json'

/** What the file may declare. Every field is optional: absent means unsaid. */
export interface RuntimeConfig {
  /** The deployment's environment name, trimmed and lower-cased. */
  environment?: string
}

/** The fetch this module needs, so a test can hand it one. */
export type ConfigFetcher = (url: string, init?: RequestInit) => Promise<Response>

/**
 * A plain environment name: the shape terraform/environments/<env>/ uses. The
 * entrypoint refuses anything else before it writes the file; this refuses it
 * again, because a badge is no place to draw text from a file nobody checked.
 */
const ENVIRONMENT_NAME = /^[a-z0-9][a-z0-9._-]{0,62}$/

let settled: RuntimeConfig | null = null
let pending: Promise<RuntimeConfig> | null = null

/** The file's body as a RuntimeConfig, keeping only what is well formed. */
function parse(body: unknown): RuntimeConfig {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return {}
  const raw: unknown = (body as { environment?: unknown }).environment
  if (typeof raw !== 'string') return {}
  const name = raw.trim().toLowerCase()
  return ENVIRONMENT_NAME.test(name) ? { environment: name } : {}
}

/**
 * Read `/config.json` once. Concurrent and later calls share the first read;
 * it never rejects.
 */
export function loadRuntimeConfig(fetcher?: ConfigFetcher): Promise<RuntimeConfig> {
  if (pending !== null) return pending
  const get: ConfigFetcher = fetcher ?? ((url, init) => fetch(url, init))
  pending = (async () => {
    let config: RuntimeConfig = {}
    try {
      const res = await get(RUNTIME_CONFIG_PATH, { cache: 'no-store', headers: { Accept: 'application/json' } })
      if (res.ok) config = parse(JSON.parse(await res.text()))
    } catch {
      // Unreachable or not JSON: the deployment declared nothing.
      config = {}
    }
    settled = config
    return config
  })()
  return pending
}

/**
 * The deployment's declared environment, or undefined while the file has not
 * arrived or when it declared none. In a built bundle the first call starts
 * the read.
 */
export function deployedEnvironment(): string | undefined {
  if (settled === null && pending === null && !import.meta.env.DEV) void loadRuntimeConfig()
  return settled?.environment
}

/** Forget the read, so each test starts from a console that has not loaded. */
export function resetRuntimeConfigForTest(): void {
  settled = null
  pending = null
}
