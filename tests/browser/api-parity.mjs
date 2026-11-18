// The UI/API seam, checked against what the browser ACTUALLY asked for.
//
// tests/unit/control_plane/test_runtimes_screen.py already scans api.ts for
// versioned path literals and compares their shape with the router's
// declarations. That is the static half and it is good, but it can only see
// paths that exist as literals in the source. A path assembled at runtime --
// from a variable, a template, an encoded id, a query string built by
// URLSearchParams -- is invisible to it.
//
// This is the dynamic half. Every request the running page made passed through
// the control plane and was recorded, so what is compared here is the exact
// byte sequence that went on the wire. `service_accounts` versus the
// `service-accounts` the metadata server really serves is precisely this class
// of defect, and it is the class that a literal scan cannot reach.
//
// It reads the Python rather than a list typed out here. A hand-kept table of
// routes would be the restatement CLAUDE.md warns about, and it would agree
// with itself forever.

import { readdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * Every path template swarm-api declares.
 *
 * FastAPI spells a parameter `{task_id}`; this keeps that spelling so a
 * reported template is greppable in the Python.
 */
export async function declaredRoutes(routesDir) {
  const files = (await readdir(routesDir)).filter((f) => f.endsWith('.py'))
  const routes = []
  for (const f of files) {
    const src = await readFile(join(routesDir, f), 'utf8')
    const prefixMatch = /APIRouter\(\s*(?:prefix\s*=\s*"([^"]*)")?/.exec(src)
    const prefix = prefixMatch && prefixMatch[1] ? prefixMatch[1] : ''
    const re = /@router\.(get|post|put|delete|patch)\(\s*"([^"]*)"/g
    let m
    while ((m = re.exec(src)) !== null) {
      const method = m[1].toUpperCase()
      const path = (prefix + m[2]) || '/'
      routes.push({ method, path, file: f })
    }
  }
  return routes
}

function toMatcher(template) {
  const parts = template.split('/')
  return (path) => {
    const got = path.split('/')
    if (got.length !== parts.length) return false
    for (let i = 0; i < parts.length; i++) {
      if (parts[i].startsWith('{') && parts[i].endsWith('}')) {
        if (got[i] === '') return false
        continue
      }
      if (parts[i] !== got[i]) return false
    }
    return true
  }
}

/**
 * Compare what was requested with what is declared.
 *
 * Two findings, and they are different problems:
 *
 *   unrouted-request   The UI asked for a path swarm-api does not serve. In
 *                      production that is a 404 the screen renders as an empty
 *                      state, which is the whole bug wearing a nicer font.
 *   unfixtured-request The suite had no fixture for a path the UI really asked
 *                      for. Not a product defect -- a hole in this suite --
 *                      but it is reported rather than swallowed, because a
 *                      screen quietly fed a 404 is a screen whose audit means
 *                      nothing.
 *
 * Routes declared and never touched are returned as `untouched` for the report
 * and are NOT findings: plenty of routes are writes, or belong to screens this
 * matrix does not open.
 */
export function compare({ requests, routes }) {
  const matchers = routes.map((r) => ({ ...r, test: toMatcher(r.path) }))
  const findings = []
  const touched = new Set()
  const seen = new Set()

  for (const req of requests) {
    const key = `${req.method} ${req.path}`
    if (seen.has(key)) continue
    seen.add(key)

    const hit = matchers.find((m) => m.method === req.method && m.test(req.path))
    if (!hit) {
      findings.push({
        category: 'api',
        kind: 'unrouted-request',
        selector: key,
        detail: `no @router declaration in apps/swarm-api/swarm_api/routes matches ${key}`,
        text: req.path,
      })
      continue
    }
    touched.add(`${hit.method} ${hit.path}`)
  }

  for (const req of requests) {
    if (!req.unfixtured) continue
    const key = `${req.method} ${req.path}`
    if (seen.has('unfix:' + key)) continue
    seen.add('unfix:' + key)
    findings.push({
      category: 'api',
      kind: 'unfixtured-request',
      selector: key,
      detail:
        'the control plane answered 404 because tests/browser/fixtures.py models no body for this route, ' +
        'so whatever the screen rendered was rendered over a failure',
      text: req.path,
    })
  }

  return {
    findings,
    touched: [...touched].sort(),
    untouched: routes
      .map((r) => `${r.method} ${r.path}`)
      .filter((k) => !touched.has(k))
      .sort(),
  }
}
