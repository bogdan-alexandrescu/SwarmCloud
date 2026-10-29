// ONE IMAGE, EVERY DEPLOYMENT (DAYONE-UIENV-1, design day-one-install.md
// section 6.2): the console learns which environment it is pointed at from
// `/config.json`, which the swarm-ui container writes from its own
// environment when it starts, and no longer from a `VITE_SWARM_ENV` that the
// image build compiled into the bundle.
//
// The first test is the lane's red-first test. The rest pin the edges a
// runtime read has that a compiled constant did not: the file can be missing,
// can be the dev server's index.html, can hold anything at all, and must not
// outrank the API's own declared answer.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { classifyEnvironment, envTreatment } from '../Brand'
import {
  RUNTIME_CONFIG_PATH,
  deployedEnvironment,
  loadRuntimeConfig,
  resetRuntimeConfigForTest,
} from '../runtimeConfig'

/** A fetch that answers `/config.json` with `body` and records the calls. */
function serving(body: unknown, init: ResponseInit = { status: 200 }) {
  const calls: Array<{ url: string; init: RequestInit | undefined }> = []
  const fetcher = async (url: string, req?: RequestInit) => {
    calls.push({ url, init: req })
    const text = typeof body === 'string' ? body : JSON.stringify(body)
    return new Response(text, init)
  }
  return { fetcher, calls }
}

afterEach(() => {
  resetRuntimeConfigForTest()
  vi.unstubAllEnvs()
})

describe('the console reads its environment at run time', () => {
  it('Brand reads the environment from /config.json, not import.meta.env.VITE_SWARM_ENV', async () => {
    // A bundle that still carries a compiled `dev` must not be believed over
    // the deployment that is actually serving it.
    vi.stubEnv('VITE_SWARM_ENV', 'dev')
    await loadRuntimeConfig(serving({ environment: 'prod' }).fetcher)

    const env = classifyEnvironment(import.meta.env.VITE_SWARM_ENV, 'console.example.test')
    expect(env).toMatchObject({ kind: 'production', name: 'prod', source: 'deployment' })
    const t = envTreatment(env)
    expect(t.label).toBe('PROD')
    expect(t.bar).toBe(true)
    expect(t.explain).toMatch(/declared by this deployment/)
  })

  it('a nonprod deployment reads quietly, in sentence case', async () => {
    await loadRuntimeConfig(serving({ environment: 'staging' }).fetcher)
    const env = classifyEnvironment(undefined, 'console.example.test')
    expect(env).toMatchObject({ kind: 'nonprod', name: 'staging', source: 'deployment' })
    expect(envTreatment(env).label).toBe('Staging')
  })

  it('fetches /config.json once, uncached, however often it is asked', async () => {
    const { fetcher, calls } = serving({ environment: 'dev' })
    await Promise.all([loadRuntimeConfig(fetcher), loadRuntimeConfig(fetcher)])
    await loadRuntimeConfig(fetcher)
    expect(calls).toHaveLength(1)
    expect(calls[0]?.url).toBe(RUNTIME_CONFIG_PATH)
    expect(RUNTIME_CONFIG_PATH).toBe('/config.json')
    expect(calls[0]?.init?.cache).toBe('no-store')
    expect(deployedEnvironment()).toBe('dev')
  })

  it("the API's declared answer still outranks the deployment's file", async () => {
    await loadRuntimeConfig(serving({ environment: 'dev' }).fetcher)
    const env = classifyEnvironment(undefined, 'console.example.test', { name: 'prod', declared: true })
    expect(env).toMatchObject({ kind: 'production', name: 'prod', source: 'api', deployment: 'dev' })
    expect(envTreatment(env).explain).toMatch(/reported by the API.*This deployment declared dev\./)
  })

  it('a deployment that declares nothing falls through to the loud unknown', async () => {
    await loadRuntimeConfig(serving({}).fetcher)
    const env = classifyEnvironment(undefined, 'console.example.test')
    expect(env).toEqual({ kind: 'unknown', host: 'console.example.test' })
    expect(envTreatment(env).explain).toMatch(/\/config\.json/)
  })
})

describe('what /config.json may say', () => {
  const unreadable: Array<[string, () => Promise<Response>]> = [
    ['a 404 (an image older than the file)', async () => new Response('not found', { status: 404 })],
    // The Vite dev server, and any SPA fallback, answer with index.html.
    ['the SPA fallback page', async () => new Response('<!doctype html><html></html>', { status: 200 })],
    ['a network failure', async () => Promise.reject(new TypeError('offline'))],
    ['a JSON array', async () => new Response('["prod"]', { status: 200 })],
    ['a non-string environment', async () => new Response('{"environment": 7}', { status: 200 })],
    ['an empty environment', async () => new Response('{"environment": "  "}', { status: 200 })],
    ['markup in the environment', async () => new Response('{"environment": "<b>prod</b>"}', { status: 200 })],
    ['an environment past 63 characters', async () => new Response(JSON.stringify({ environment: 'p'.repeat(64) }), { status: 200 })],
  ]

  it.each(unreadable)('%s declares no environment and never throws', async (_name, fetcher) => {
    await expect(loadRuntimeConfig(fetcher)).resolves.toEqual({})
    expect(deployedEnvironment()).toBeUndefined()
    expect(classifyEnvironment(undefined, 'console.example.test').kind).toBe('unknown')
  })

  it('a well-formed name is trimmed and lower-cased', async () => {
    await loadRuntimeConfig(serving({ environment: ' Prod-EU ' }).fetcher)
    expect(deployedEnvironment()).toBe('prod-eu')
  })
})

describe('when the read starts', () => {
  it('under the dev server and in tests nothing is fetched behind the caller', () => {
    const spy = vi.spyOn(globalThis, 'fetch')
    expect(deployedEnvironment()).toBeUndefined()
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })

  it('a built bundle starts the read the first time the environment is asked for', async () => {
    vi.stubEnv('DEV', false)
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"environment":"dev"}', { status: 200 }))
    expect(deployedEnvironment()).toBeUndefined()
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0]?.[0]).toBe('/config.json')
    await loadRuntimeConfig()
    expect(deployedEnvironment()).toBe('dev')
    spy.mockRestore()
  })
})

// THE OTHER END OF THE FILE: the image that serves it. Read as text, because
// CI does not run the container; what it proves is that nothing in the image
// recipe compiles an environment into the bundle any more.
describe('the swarm-ui image', () => {
  const images = join(__dirname, '..', '..', '..', '..', 'images', 'swarm-ui')
  const dockerfile = readFileSync(join(images, 'Dockerfile'), 'utf8')
  const nginx = readFileSync(join(images, 'nginx.conf'), 'utf8')
  const code = dockerfile
    .split('\n')
    .filter((l) => !l.trimStart().startsWith('#'))
    .join('\n')

  it('compiles no environment into the bundle', () => {
    expect(code).not.toMatch(/VITE_SWARM_ENV/)
    expect(code).not.toMatch(/^\s*(ARG|ENV)\s+VITE_/m)
  })

  it('writes /config.json from the container environment when it starts', () => {
    // nginx's own entrypoint runs every executable script in this directory
    // before it starts nginx.
    expect(dockerfile).toMatch(
      /RUN cat > (\/docker-entrypoint\.d\/\d+-swarm-ui-config\.sh) <<"EOF" && chmod 0755 \1\n#!\/bin\/sh\n/,
    )
    expect(code).toMatch(/\$\{ENVIRONMENT:-\}/)
    // Written whole, then moved into place, where nginx.conf's alias reads it.
    expect(code).toMatch(/^dir=\/tmp\/swarm-ui$/m)
    expect(code).toMatch(/mv "\$dir\/config\.json\.tmp" "\$dir\/config\.json"/)
  })

  it('nginx serves that file, uncached, at /config.json', () => {
    const block = /location\s*=\s*\/config\.json\s*\{([^}]*)\}/.exec(nginx)
    expect(block).not.toBeNull()
    expect(block![1]).toMatch(/alias\s+\/tmp\/swarm-ui\/config\.json;/)
    expect(block![1]).toMatch(/no-store/)
    expect(block![1]).toMatch(/application\/json/)
  })
})
