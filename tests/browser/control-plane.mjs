// The control plane the browser suite drives the UI against.
//
// WHY A SERVER AND NOT A BROWSER ROUTE INTERCEPT
// ----------------------------------------------
// Both were on the table. A route intercept fulfils the request inside the page
// context, which means the thing under test -- fetch.ts's classification of a
// real HTTP response -- is partly supplied by the test harness. The check that
// matters most in this suite is a content-type check on a 200 (an expired IAP
// session arrives as a 200 carrying HTML, and the naive handler paints a calm,
// healthy, EMPTY platform). Proving that assertion needs a real response with a
// real header from a real socket, not a fulfilment object.
//
// So this is an ordinary HTTP server. It serves the PRODUCTION bundle from
// apps/swarm-ui/dist and answers /v1 on the same origin, which is exactly the
// topology the load balancer will present: static at /, swarm-api at /v1. The
// UI cannot tell it apart, and nothing in the page is aware a test is running.
//
// It also records every request, so the run can assert two things no screenshot
// can: that every path the UI actually issued at runtime resolves to a route
// swarm-api declares, and that a screen which showed numbers really did read
// the routes it claims to be reading.
//
// Response BODIES are never built here -- they are looked up from what
// tests/browser/fixtures.py produced by calling the real codec. Constructing an
// envelope in JavaScript would be the API's response shape restated one more
// language away from the code that owns it, which is the exact defect shape
// this suite was written to catch.

import { createServer } from 'node:http'
import { readFile, stat } from 'node:fs/promises'
import { extname, join, normalize, resolve, sep } from 'node:path'

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.ico': 'image/x-icon',
}

/**
 * The failure modes, as scenarios.
 *
 * Each names the routes it breaks and HOW. `match` is tested against the path
 * with the query string already removed, so a scenario cannot accidentally
 * depend on the order of query parameters.
 *
 * `kind` values and what each one is for:
 *   status    -- a real HTTP error with the API's own envelope (errors.py:24).
 *   html      -- 200, content-type text/html. THE IAP CASE. fetch.ts has to
 *                reject this before parsing, or an expired session renders as
 *                an empty, healthy platform.
 *   destroy   -- the socket is closed with no response at all, so `fetch`
 *                itself rejects. Distinct from every 4xx: never the user's
 *                fault, and the UI owes different copy.
 */
export const SCENARIOS = {
  healthy: { breaks: [] },

  // ONE read fails out of many. This is the partial-response case: the screens
  // that join leases against capacity must say what they could not see, and
  // must not print a total computed from the half that answered.
  'leases-down': {
    breaks: [
      {
        match: (p) => p === '/v1/admin/leases',
        kind: 'status',
        status: 503,
        body: {
          code: 'upstream_degraded',
          message: 'The lease index did not answer within the deadline.',
        },
      },
    ],
  },

  // ONE of the reads a TOTAL is summed from fails. Distinct from
  // `leases-down`: there the failed read feeds its own tile, here it feeds an
  // aggregate, and an aggregate missing one of its inputs is wrong by an
  // unknown amount while looking exactly like a correct one.
  'spend-partial': {
    breaks: [
      {
        match: (p) => /^\/v1\/tasks\/[^/]+\/attempts$/.test(p) && p.includes('task_796d05f116c147e9854d'),
        kind: 'status',
        status: 503,
        body: {
          code: 'upstream_degraded',
          message: 'The attempts index did not answer within the deadline.',
        },
      },
    ],
  },

  // The read a whole screen is built on fails. Nothing may render a zero.
  'capacity-down': {
    breaks: [
      {
        match: (p) => p === '/v1/capacity',
        kind: 'status',
        status: 500,
        body: { code: 'internal_error', message: 'The API failed on this request.' },
      },
    ],
  },

  // Every /v1 read answers 200 with a sign-in page. The status.sh incident,
  // reproduced in a browser.
  'session-expired': {
    breaks: [{ match: (p) => p.startsWith('/v1/'), kind: 'html' }],
  },

  // Nothing answers at all.
  unreachable: {
    breaks: [{ match: (p) => p.startsWith('/v1/'), kind: 'destroy' }],
  },
}

const SIGNIN_PAGE =
  '<!doctype html><html><head><title>Sign in - Google Accounts</title></head>' +
  '<body><form action="/signin"><input name="identifier"></form></body></html>'

export function createControlPlane({ fixtures, distDir }) {
  const dist = resolve(distDir)
  const routes = fixtures.routes

  /** Every request the browser made, in order. Read by the parity assertions. */
  const requests = []
  let scenario = 'healthy'

  /**
   * Resolve a request path to a fixture body.
   *
   * Returns `{ found, body, template }`. `template` is the route key, which is
   * what the runtime parity check compares against swarm-api's declarations --
   * so a path the UI assembles at runtime and this server cannot place is a
   * finding, not a 404 that quietly renders as an empty screen.
   */
  function lookup(pathname) {
    const direct = `GET ${pathname}`
    if (Object.prototype.hasOwnProperty.call(routes, direct)) {
      return { found: true, body: routes[direct], template: pathname }
    }
    const parts = pathname.split('/').filter(Boolean) // ['v1','tasks','task_x','events']
    if (parts[0] !== 'v1') return { found: false }

    // /v1/tasks/{task_id}[/sub] and /v1/workflows/{workflow_id}
    if ((parts[1] === 'tasks' || parts[1] === 'workflows') && parts.length >= 3) {
      const id = decodeURIComponent(parts[2])
      const param = parts[1] === 'tasks' ? '{task_id}' : '{workflow_id}'
      const tail = parts.slice(3).join('/')
      const key = `GET /v1/${parts[1]}/${param}${tail ? '/' + tail : ''}`
      const table = routes[key]
      if (!table) return { found: false, template: key }
      if (!Object.prototype.hasOwnProperty.call(table, id)) {
        return { found: false, template: key, unknownId: id }
      }
      return { found: true, body: table[id], template: key }
    }
    return { found: false }
  }

  function breakFor(pathname) {
    const spec = SCENARIOS[scenario]
    if (!spec) return null
    for (const b of spec.breaks) if (b.match(pathname)) return b
    return null
  }

  async function serveStatic(pathname, res) {
    // SPA: anything that is not a file under dist/ is the app's own route.
    let rel = pathname === '/' ? '/index.html' : pathname
    let file = join(dist, normalize(rel))
    if (!file.startsWith(dist + sep) && file !== join(dist, 'index.html')) {
      file = join(dist, 'index.html')
    }
    try {
      const s = await stat(file)
      if (!s.isFile()) throw new Error('not a file')
    } catch {
      file = join(dist, 'index.html')
    }
    const body = await readFile(file)
    res.writeHead(200, {
      'content-type': MIME[extname(file)] ?? 'application/octet-stream',
      'cache-control': 'no-store',
    })
    res.end(body)
  }

  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1')
    const pathname = url.pathname

    // The harness's own channel. Deliberately under a path no route of this
    // product uses, and never recorded as a UI request.
    if (pathname === '/__uitest/scenario') {
      let raw = ''
      req.on('data', (c) => (raw += c))
      req.on('end', () => {
        const next = raw.trim()
        if (!Object.prototype.hasOwnProperty.call(SCENARIOS, next)) {
          res.writeHead(400, { 'content-type': 'application/json' })
          res.end(JSON.stringify({ error: `unknown scenario ${next}` }))
          return
        }
        scenario = next
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ scenario }))
      })
      return
    }
    if (pathname === '/__uitest/requests') {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify(requests))
      return
    }
    if (pathname === '/__uitest/requests/clear') {
      requests.length = 0
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end('{}')
      return
    }

    if (!pathname.startsWith('/v1')) {
      serveStatic(pathname, res).catch(() => {
        res.writeHead(500, { 'content-type': 'text/plain' })
        res.end('static read failed')
      })
      return
    }

    const record = {
      method: req.method,
      path: pathname,
      query: url.search,
      scenario,
      at: Date.now(),
    }
    requests.push(record)

    const brk = breakFor(pathname)
    if (brk) {
      record.broken = brk.kind
      if (brk.kind === 'destroy') {
        record.status = null
        req.socket.destroy()
        return
      }
      if (brk.kind === 'html') {
        record.status = 200
        record.contentType = 'text/html'
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
        res.end(SIGNIN_PAGE)
        return
      }
      record.status = brk.status
      res.writeHead(brk.status, { 'content-type': 'application/json' })
      res.end(JSON.stringify(brk.body))
      return
    }

    finish()

    function finish() {
      const hit = lookup(pathname)
      record.template = hit.template ?? null
      if (!hit.found) {
        // A 404 from THIS server means the suite has no fixture for a path the
        // UI really asked for. That is a finding about coverage, recorded on
        // the request so the run can report it by name rather than letting the
        // screen render an empty state nobody questions.
        record.status = 404
        record.unfixtured = true
        res.writeHead(404, { 'content-type': 'application/json' })
        res.end(
          JSON.stringify({
            code: 'not_found',
            message: `no fixture for ${pathname}; the browser suite did not model this route`,
          }),
        )
        return
      }
      record.status = 200
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify(hit.body))
    }
  })

  return {
    server,
    requests,
    listen: (port) =>
      new Promise((ok) => server.listen(port, '127.0.0.1', () => ok(server.address().port))),
    close: () => new Promise((ok) => server.close(ok)),
    get scenario() {
      return scenario
    },
    setScenario: (s) => {
      scenario = s
    },
  }
}
