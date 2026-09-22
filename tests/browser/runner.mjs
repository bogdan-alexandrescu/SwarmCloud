// The browser UX/UI suite. One run: matrix audit, keyboard walk, honesty
// scenarios, runtime seam check, report.
//
// It is driven by scripts/ui-test.sh, which is the one command a person types.
// Running this file directly works too and takes the same flags; the shell
// wrapper exists to resolve tools, build the bundle and generate the fixtures
// first, and to keep the house rules about shellcheck and common.sh in one
// place.
//
// WHAT THIS IS NOT. It is not wired into `make test`, and must not be: that
// gate is offline, needs no credentials, creates nothing and finishes in
// seconds, and a browser is none of those things. It has its own target.

import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createControlPlane, SCENARIOS } from './control-plane.mjs'
import { Browse } from './browse.mjs'
import { compare, declaredRoutes } from './api-parity.mjs'
import {
  HONESTY_SNAPSHOT,
  MARK_INTERACTIVE,
  PAGE_AUDIT,
  READ_FOCUS_TRACE,
  RECORD_FOCUS,
} from './audits.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..', '..')
const DIST = join(REPO, 'apps', 'swarm-ui', 'dist')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------------------
// The matrix
// ---------------------------------------------------------------------------

/**
 * Viewports.
 *
 * 1024x768 is in the list because it is the width at which a fixed sidebar and
 * a three-column metric strip stop fitting, and it is a real laptop on a
 * projector in a meeting where somebody is asking why an agent has not moved.
 * Anything narrower is a phone and this product does not claim one.
 */
const VIEWPORTS = [
  [1600, 1000],
  [1440, 900],
  [1280, 800],
  [1024, 768],
]

/** Every view a nav click or a documented hash can reach. */
function views(ids) {
  return [
    { id: 'overview-now', hash: 'overview/now' },
    { id: 'agents-running', hash: 'agents/running' },
    { id: 'agents-workflows', hash: 'agents/workflows' },
    { id: 'agents-new', hash: 'agents/new' },
    { id: 'agents-new-workflow', hash: 'agents/new-workflow' },
    { id: 'runtimes-catalogue', hash: 'runtimes/catalogue' },
    { id: 'pools-pools', hash: 'pools/pools' },
    { id: 'pools-profiles', hash: 'pools/profiles' },
    { id: 'pools-holders', hash: 'pools/holders' },
    { id: 'pools-accounts', hash: 'pools/accounts' },
    { id: 'pools-quota', hash: 'pools/quota' },
    { id: 'history-timeline', hash: 'history/timeline' },
    { id: 'history-counts', hash: 'history/counts' },
    { id: 'admin-limits', hash: 'admin/limits' },
    { id: 'admin-tenants', hash: 'admin/tenants' },
    { id: 'reference', hash: 'reference' },
    // The drawer, both panes. It is a route, not a modal state, so it audits
    // like any other view -- and it is the one place a task id is rendered
    // large, which is what the identifier check is for.
    { id: 'agent-detail', hash: `agents/task/${ids.running_task}` },
    { id: 'agent-attempts', hash: `agents/task/${ids.running_task}/attempts` },
    { id: 'agent-attempts-unmeasured', hash: `agents/task/${ids.unmeasured_task}/attempts` },
    { id: 'agent-attempts-measured-zero', hash: `agents/task/${ids.measured_zero_task}/attempts` },
  ]
}

/**
 * Views the keyboard walk covers.
 *
 * Not all of them: a Tab walk is two CLI round trips per stop and the matrix is
 * already eighty audits. These four are the ones with the most controls, the
 * only form, and the drawer -- which is the only thing in the product that
 * owes a focus trap.
 */
const KEYBOARD_VIEWS = ['overview-now', 'pools-pools', 'agents-new', 'agent-detail']

// ---------------------------------------------------------------------------
// Honesty: the assertions that need the platform to fail
// ---------------------------------------------------------------------------

const REASSURANCE = 'this is a failure to read the platform'
const UNREACHABLE_COPY = 'the request never reached the platform'

/**
 * Each case names the scenario, the view, and what the screen owes the reader.
 *
 * These are written as prose predicates rather than as a regex table because
 * every one of them is a rule somebody argued for and can argue against. The
 * rule is next to the assertion.
 */
function honestyCases(ids) {
  return [
    {
      id: 'failed-read-renders-no-zero',
      scenario: 'capacity-down',
      view: { id: 'pools-pools', hash: 'pools/pools' },
      check(s) {
        const bad = []
        // 1. A read that failed must not resolve to a number anywhere on the
        //    screen it fed. Not 0, not 0%, not "0 of 0".
        for (const n of s.bareNumbers) {
          bad.push({
            kind: 'zero-for-failed-read',
            selector: n.selector,
            detail: `the capacity read returned HTTP 500 and this cell still renders "${n.text}"`,
            text: n.path,
          })
        }
        // 2. The failure has to be named, and named as a failure to READ --
        //    an error screen taken as evidence about the platform is the bug
        //    one layer up.
        if (!s.text.toLowerCase().includes(REASSURANCE)) {
          bad.push({
            kind: 'failure-not-disclaimed',
            selector: 'body',
            detail:
              'no text on the screen says this is a failure to read the platform rather than a statement about it',
            text: s.text.slice(0, 120),
          })
        }
        // 3. And the probe strip must show the route that failed, with its
        //    status, so the reader can tell which read died.
        const failedProbe = s.probes.find((p) => /\/v1\/capacity/.test(p.path))
        if (!failedProbe || /^200/.test(failedProbe.status.trim())) {
          bad.push({
            kind: 'probe-strip-hides-failure',
            selector: '.source',
            detail: `the data-source strip does not report /v1/capacity as failed (saw ${
              failedProbe ? failedProbe.status : 'no row at all'
            })`,
            text: '',
          })
        }
        return bad
      },
    },

    {
      id: 'total-read-failure-renders-no-figures',
      scenario: 'unreachable',
      view: { id: 'overview-now', hash: 'overview/now' },
      check(s) {
        const bad = []
        for (const m of s.metrics) {
          // A metric tile whose every read failed must say so in the value
          // slot. A number there is a figure invented out of a dead socket.
          if (/^[\s$£€]*-?\d/.test(m.value)) {
            bad.push({
              kind: 'figure-over-total-failure',
              selector: m.selector,
              detail: `every read failed and the tile "${m.label.trim()}" still shows "${m.value}"`,
              text: m.label,
            })
          }
        }
        if (!s.text.toLowerCase().includes(UNREACHABLE_COPY)) {
          bad.push({
            kind: 'unreachable-not-distinguished',
            selector: 'body',
            detail:
              'fetch itself rejected, which is never the caller\'s fault and needs its own copy; the screen does not carry it',
            text: s.sub,
          })
        }
        return bad
      },
    },

    {
      id: 'expired-session-is-not-an-empty-platform',
      scenario: 'session-expired',
      view: { id: 'overview-now', hash: 'overview/now' },
      check(s) {
        const bad = []
        // THE ROW THAT MATTERS MOST, from fetch.ts. An expired IAP session is
        // a 200 carrying HTML. A handler that parses the body and shrugs
        // paints a calm, healthy, EMPTY platform, and that is the status.sh
        // incident reproduced in a browser.
        const said = /sign|session|expired|reload/i.test(s.text)
        if (!said) {
          bad.push({
            kind: 'expired-session-rendered-as-data',
            selector: 'body',
            detail:
              'every /v1 read answered 200 with text/html (the sign-in redirect) and nothing on screen mentions the session',
            text: s.text.slice(0, 160),
          })
        }
        for (const m of s.metrics) {
          if (/^[\s$£€]*-?\d/.test(m.value)) {
            bad.push({
              kind: 'figure-over-expired-session',
              selector: m.selector,
              detail: `no read returned data and the tile "${m.label.trim()}" still shows "${m.value}"`,
              text: m.label,
            })
          }
        }
        return bad
      },
    },

    {
      id: 'partial-response-says-what-it-could-not-see',
      scenario: 'leases-down',
      view: { id: 'overview-now', hash: 'overview/now' },
      check(s) {
        const bad = []
        // A partial answer owes two things: that it is partial, and WHICH part
        // is missing. A screen that shows five of six checks and says "nothing
        // needs attention" is the failure this product was written against.
        if (!/\b\d+ of \d+ reads failed\b/i.test(s.sub)) {
          bad.push({
            kind: 'partial-not-declared',
            selector: '.sub',
            detail: `one of seven reads returned 503 and the header does not say so (it reads "${s.sub}")`,
            text: s.sub,
          })
        }
        if (!/could not|blind|did not run/i.test(s.text)) {
          bad.push({
            kind: 'partial-does-not-name-the-gap',
            selector: 'body',
            detail: 'the screen does not name the check that could not run over the failed read',
            text: s.text.slice(0, 160),
          })
        }
        const probe = s.probes.find((p) => /\/v1\/admin\/leases/.test(p.path))
        if (!probe || /^200/.test(probe.status.trim())) {
          bad.push({
            kind: 'probe-strip-hides-failure',
            selector: '.source',
            detail: `the data-source strip does not report /v1/admin/leases as failed (saw ${
              probe ? probe.status : 'no row at all'
            })`,
            text: '',
          })
        }
        return bad
      },
    },

    {
      id: 'no-total-over-a-partial-read',
      scenario: 'spend-partial',
      view: { id: 'overview-now', hash: 'overview/now' },
      check(s) {
        const bad = []
        const spend = s.metrics.find((m) => /spend/i.test(m.label))
        if (!spend) {
          bad.push({
            kind: 'spend-tile-missing',
            selector: '.ctl-metric',
            detail: 'no token spend tile was rendered, so this case asserted nothing',
            text: '',
          })
          return bad
        }
        // One of the per-task attempt reads failed. A currency figure printed
        // as though it were the total is a number that is wrong by an unknown
        // amount, and nothing on screen would reveal it.
        const looksLikeATotal = /^[\s$£€]*-?\d/.test(spend.value)
        const admitsThePartial = /could not|failed|unread|of \d+ attempts/i.test(
          spend.sub + ' ' + spend.foot,
        )
        if (looksLikeATotal && !admitsThePartial) {
          bad.push({
            kind: 'total-over-partial-read',
            selector: spend.selector,
            detail: `an attempts read returned 503 and the tile still totals "${spend.value}" with no note that a read was missing (sub="${spend.sub.trim()}")`,
            text: spend.label,
          })
        }
        return bad
      },
    },

    {
      id: 'unmeasured-is-an-em-dash-and-a-measured-zero-is-zero',
      scenario: 'healthy',
      // Both panes, read together: the same screen, two tasks, one of which
      // genuinely cost nothing and one of which was never measured. If they
      // render identically one of them is a lie, and which one is the lie
      // cannot be told from a single screen.
      view: (ids) => ({
        id: 'agent-attempts-unmeasured',
        hash: `agents/task/${ids.unmeasured_task}/attempts`,
      }),
      pairedView: (ids) => ({
        id: 'agent-attempts-measured-zero',
        hash: `agents/task/${ids.measured_zero_task}/attempts`,
      }),
      checkPair(unmeasured, measured) {
        const bad = []
        if (unmeasured.emDashes === 0) {
          bad.push({
            kind: 'unmeasured-not-an-em-dash',
            selector: 'body',
            detail:
              'this attempt reports no tokens and no cost (the five spend fields are null) and the screen renders no em dash anywhere',
            text: unmeasured.text.slice(0, 160),
          })
        }
        // The measured-zero attempt really did cost $0.00 and 0 tokens. If the
        // screen shows the same em dash for both, a mock run and an unparsed
        // run are the same pixel -- which is the conflation the decoder
        // comment in codec.py spends a paragraph warning about.
        const zeroRendered = /\b0\b/.test(measured.text)
        if (!zeroRendered) {
          bad.push({
            kind: 'measured-zero-not-rendered-as-zero',
            selector: 'body',
            detail:
              'this attempt measured 0 tokens and $0.00 and the screen renders no 0 at all, so a real measurement is being shown as an absence',
            text: measured.text.slice(0, 160),
          })
        }
        return bad
      },
    },
  ]
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const a = {
    fixtures: null,
    outDir: join(REPO, 'build', 'ui-test'),
    baseline: join(HERE, 'baseline.json'),
    headed: true,
    updateBaseline: false,
    only: null,
    // Which phases to run. All of them by default. Named phases exist for two
    // reasons and only two: proving a check catches a deliberate regression
    // (which needs one phase, repeatedly, not twenty minutes of browser), and
    // working on one category without waiting for the rest. A run that named a
    // subset says so at the top of its report and counts as incomplete
    // coverage, so it can never be mistaken for a full pass.
    sections: ['matrix', 'keyboard', 'honesty', 'api'],
    sectionsGiven: false,
    settleMs: 1400,
  }
  for (let i = 0; i < argv.length; i++) {
    const v = argv[i]
    if (v === '--fixtures') a.fixtures = argv[++i]
    else if (v === '--out') a.outDir = argv[++i]
    else if (v === '--baseline') a.baseline = argv[++i]
    else if (v === '--headless') a.headed = false
    else if (v === '--update-baseline') a.updateBaseline = true
    else if (v === '--only') a.only = argv[++i]
    else if (v === '--sections') {
      a.sections = argv[++i].split(',').map((x) => x.trim()).filter(Boolean)
      a.sectionsGiven = true
      const known = ['matrix', 'keyboard', 'honesty', 'api']
      for (const s of a.sections) {
        if (!known.includes(s)) throw new Error(`unknown section ${s}; known: ${known.join(', ')}`)
      }
    }
    else if (v === '--settle') a.settleMs = Number(argv[++i])
    else throw new Error(`unknown flag ${v}`)
  }
  return a
}

function keyOf(f) {
  return [f.view ?? '-', f.viewport ?? '-', f.kind, f.selector].join('|')
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (!args.fixtures) throw new Error('--fixtures <path> is required')
  if (!existsSync(DIST)) {
    throw new Error(
      `${DIST} does not exist. Build the UI first: (cd apps/swarm-ui && npm ci && npm run build)`,
    )
  }

  const fixtures = JSON.parse(await readFile(args.fixtures, 'utf8'))
  const ids = fixtures.ids
  const cp = createControlPlane({ fixtures, distDir: DIST })
  const port = await cp.listen(0)
  const origin = `http://127.0.0.1:${port}`

  await mkdir(join(args.outDir, 'screenshots'), { recursive: true })

  const b = new Browse({ origin, headed: args.headed })
  process.stderr.write(`     waiting for the browser (${args.headed ? 'headed' : 'headless'})\n`)
  const status = await b.warmUp({ log: (m) => process.stderr.write(`     ${m}\n`) })
  if (!status.ok) {
    process.stderr.write(
      `ui-test: the browse daemon did not become healthy.\n${status.raw}\n` +
        `ui-test: continuing anyway -- the first navigation retries, and the report records the mode.\n`,
    )
  }

  const findings = []
  const notes = []
  const add = (f) => findings.push(f)

  // Progress goes to stderr as it happens. A suite that prints nothing for
  // four minutes is indistinguishable from a suite that has hung, and the
  // browser here really can hang -- which is how three runs of this were
  // killed by a reader who had no way to tell.
  const say = (m) => process.stderr.write(`     ${m}\n`)
  say(`browser ${status.mode ?? 'unknown'}, control plane on ${origin}`)

  /**
   * Run one unit of work; if the browser lets go, record it and carry on.
   *
   * A shared daemon restarts on its own schedule and takes the pinned tab with
   * it. An earlier draft let that abort the run, so one hiccup on the fifth of
   * twenty views threw away the other fifteen AND the report -- and a suite
   * that produces no report cannot be read at all, which is strictly worse
   * than one that says which part it could not reach.
   *
   * What is NOT done here: swallowing it. Every skipped unit is a line in the
   * report's harness notes, and `coverage` at the top of the report says how
   * many units ran out of how many were planned. A green run over half the
   * matrix would be this repository's favourite bug in a new costume.
   */
  const coverage = { planned: 0, ran: 0 }
  // How long each unit took. A suite that slows down over a long run is a
  // suite somebody stops running, and "it felt slow" is not something anybody
  // can act on -- so the report carries the numbers.
  const timings = []
  const guard = async (label, fn) => {
    coverage.planned++
    try {
      await fn()
      coverage.ran++
    } catch (err) {
      notes.push(`SKIPPED ${label}: ${err.message}`)
      say(`skipped ${label} (${err.message.slice(0, 90)})`)
      b.tabId = null
      await b.warmUp({ attempts: 3, log: (m) => notes.push(m) })
    }
  }

  const setScenario = async (name) => {
    const r = await fetch(`${origin}/__uitest/scenario`, { method: 'POST', body: name })
    if (!r.ok) throw new Error(`could not select scenario ${name}: ${await r.text()}`)
  }

  /**
   * Load a view from scratch.
   *
   * A cache-busting query, NOT a bare hash change. The app is hash-routed, so
   * `goto` to a new hash on the same document does not reload and therefore
   * does not re-read the API -- which meant an earlier draft of this runner
   * measured every failure scenario against data fetched while the platform
   * was still healthy, and reported the UI as flawless. Each view is mounted
   * cold, exactly once, against exactly the scenario named.
   */
  const loadView = async (hash) => {
    let last = 'no attempt was made'
    for (let attempt = 0; attempt < 3; attempt++) {
      const url = `${origin}/?r=${Date.now()}#${hash}`
      try {
        await b.open(url)
        await sleep(args.settleMs)
        const here = await b.eval('return location.hash')
        if (typeof here === 'string') return
        last = `the page reported its hash as ${JSON.stringify(here)}`
      } catch (err) {
        // The daemon restarts on its own, and when it does it takes the pinned
        // tab with it. The origin guard catches that rather than letting a
        // script run on whatever tab drifted into focus; recovery is to take a
        // fresh tab and load again, not to press on.
        last = err.message
        notes.push(`recovered from a browse failure on ${hash}: ${err.message}`)
        b.tabId = null
        // The daemon may have wedged rather than merely dropped our tab, and
        // that state does not clear on its own -- so the same recovery the
        // start-up path uses is run again here. Without it a mid-run wedge
        // turns every remaining view into three slow failures.
        await b.warmUp({ attempts: 3, log: (m) => notes.push(m) })
        await sleep(1500)
      }
    }
    throw new Error(`could not load ${hash} after three attempts. Last failure: ${last}`)
  }

  try {
    // ---------------------------------------------------------- 1. matrix
    await setScenario('healthy')
    await fetch(`${origin}/__uitest/requests/clear`)

    const viewList = args.only
      ? views(ids).filter((v) => v.id === args.only)
      : views(ids)
    if (viewList.length === 0) throw new Error(`--only ${args.only} matched no view`)

    for (const view of args.sections.includes('matrix') ? viewList : []) {
      const recent = timings.slice(-4)
      const pace = recent.length
        ? ` (last ${recent.length} units ${Math.round(recent.reduce((a, t) => a + t.ms, 0) / recent.length)}ms each)`
        : ''
      say(`view ${view.id}${pace}`)
      for (const [w, h] of VIEWPORTS) {
        await guard(`${view.id} @ ${w}x${h}`, async () => {
          const started = Date.now()
          await loadView(view.hash)
          // Resize, audit, screenshot and read back in ONE invocation. See
          // Browse#auditViewport: the per-process cost of the driver dominates
          // everything else in this loop.
          const got = await b.auditViewport({
            width: w,
            height: h,
            expression: PAGE_AUDIT,
            screenshotPath: join(args.outDir, 'screenshots', `${view.id}-${w}x${h}.png`),
          })
          for (const f of got) add({ ...f, view: view.id, viewport: `${w}x${h}` })
          timings.push({ unit: `${view.id}@${w}x${h}`, ms: Date.now() - started })
        })
      }
    }

    // -------------------------------------------------- 2. keyboard walk
    for (const id of args.sections.includes('keyboard') ? KEYBOARD_VIEWS : []) {
      const view = viewList.find((v) => v.id === id)
      if (!view) continue
      say(`keyboard walk on ${view.id}`)
      await guard(`keyboard walk on ${view.id}`, async () => {
      await loadView(view.hash)
      await b.viewport(1440, 900)
      await sleep(400)

      const controls = await b.eval(MARK_INTERACTIVE)
      // One extra lap plus a margin, so a control that only becomes reachable
      // after the focus has wrapped is still counted as reachable.
      const budget = controls.length + 12
      // ONE INVOCATION, not two per step. `browse` is a 61MB binary and pays
      // its start-up on every call; a control-heavy screen is seventy steps,
      // and one process per step spent longer tabbing through a single view
      // than auditing all twenty. It also halves the window in which the
      // shared daemon can restart and take the pinned tab with it.
      const steps = [['tab', String(b.tabId)]]
      for (let i = 0; i < budget; i++) {
        steps.push(['press', 'Tab'])
        steps.push(['js', `(() => { ${RECORD_FOCUS} })()`])
      }
      const walked = await b.chain(steps)
      if (walked.code !== 0) {
        notes.push(`the keyboard walk on ${view.id} did not complete: ${walked.err || walked.out}`)
      }
      const trace = await b.eval(READ_FOCUS_TRACE)

      const reached = new Set(trace.map((t) => t.idx).filter((i) => i !== null))
      for (const c of controls) {
        if (!reached.has(c.idx)) {
          add({
            category: 'keyboard',
            kind: 'unreachable-by-keyboard',
            view: view.id,
            viewport: '1440x900',
            selector: c.selector,
            detail: `${budget} Tab presses never landed on this control`,
            text: c.label,
          })
        }
      }
      const ringless = new Map()
      for (const t of trace) {
        if (t.idx === null || t.ring) continue
        if (!ringless.has(t.selector)) ringless.set(t.selector, t)
      }
      for (const [selector, t] of ringless) {
        add({
          category: 'keyboard',
          kind: 'no-visible-focus-ring',
          view: view.id,
          viewport: '1440x900',
          selector,
          detail: `focused by keyboard with no outline and no box-shadow (${t.ringDetail})`,
          text: t.label,
        })
      }

      // The drawer is the only thing here that owes a focus trap, and it is
      // the thing most likely not to have one: it is a route rendering a
      // <div role="dialog">, not a component that took responsibility for
      // focus. Asserted only where a dialog is actually on screen.
      if (id === 'agent-detail') {
        const hasDialog = await b.eval('return !!document.querySelector(\'[role="dialog"]\')')
        if (hasDialog) {
          const escaped = trace.filter((t) => t.idx !== null && !t.inDialog)
          if (escaped.length > 0) {
            add({
              category: 'keyboard',
              kind: 'drawer-does-not-trap-focus',
              view: view.id,
              viewport: '1440x900',
              selector: '[role="dialog"]',
              detail: `Tab left the open drawer and reached ${escaped.length} control(s) behind it, the first being ${escaped[0].selector}`,
              text: escaped[0].label,
            })
          }
          // And on close, focus must come back to something. Left on <body>,
          // the next Tab restarts at the top of the document and a keyboard
          // reader loses their place entirely.
          const before = await b.eval(
            'return (document.querySelector(\'[role="dialog"]\') ? "open" : "shut")',
          )
          if (before === 'open') {
            await b.run('const c = document.querySelector(".drawer-close"); if (c) c.click(); return "clicked";')
            await sleep(600)
            const after = await b.eval(
              'return { tag: document.activeElement ? document.activeElement.tagName.toLowerCase() : null, dialog: !!document.querySelector(\'[role="dialog"]\') }',
            )
            if (!after.dialog && (after.tag === 'body' || after.tag === null)) {
              add({
                category: 'keyboard',
                kind: 'focus-not-restored-on-close',
                view: view.id,
                viewport: '1440x900',
                selector: '.drawer-close',
                detail: 'the drawer closed and focus was left on <body>, so the next Tab restarts at the top of the page',
                text: '',
              })
            }
          }
        }
      }
      })
    }

    // ------------------------------------------------- 3. honesty rules
    for (const c of args.sections.includes('honesty') ? honestyCases(ids) : []) {
      say(`honesty: ${c.id} (scenario ${c.scenario})`)
      await guard(`honesty case ${c.id}`, async () => {
      if (c.checkPair) {
        await setScenario(c.scenario)
        const va = c.view(ids)
        const vb = c.pairedView(ids)
        await loadView(va.hash)
        await b.viewport(1440, 900)
        await sleep(500)
        const a = await b.eval(HONESTY_SNAPSHOT)
        await loadView(vb.hash)
        await b.viewport(1440, 900)
        await sleep(500)
        const bb = await b.eval(HONESTY_SNAPSHOT)
        for (const f of c.checkPair(a, bb)) {
          add({ ...f, category: 'honesty', view: `${va.id}+${vb.id}`, viewport: '1440x900', case: c.id })
        }
        await b.screenshot(join(args.outDir, 'screenshots', `honesty-${c.id}.png`))
        return
      }
      await setScenario(c.scenario)
      await loadView(c.view.hash)
      await b.viewport(1440, 900)
      await sleep(600)
      const snap = await b.eval(HONESTY_SNAPSHOT)
      for (const f of c.check(snap)) {
        add({ ...f, category: 'honesty', view: c.view.id, viewport: '1440x900', case: c.id })
      }
      await b.screenshot(join(args.outDir, 'screenshots', `honesty-${c.id}.png`))
      })
    }
    await setScenario('healthy')

    // -------------------------------------------------- 4. the API seam
    let parity = { findings: [], touched: [], untouched: [] }
    if (args.sections.includes('api')) {
      say('checking the API seam against the router declarations')
      const routes = await declaredRoutes(join(REPO, 'apps', 'swarm-api', 'swarm_api', 'routes'))
      parity = compare({ requests: cp.requests, routes })
      for (const f of parity.findings) add({ ...f, view: 'all', viewport: 'all' })
    }

    // ------------------------------------------------------- 5. report
    const baseline = existsSync(args.baseline)
      ? new Set(JSON.parse(await readFile(args.baseline, 'utf8')).keys)
      : new Set()

    for (const f of findings) f.key = keyOf(f)
    const fresh = findings.filter((f) => !baseline.has(f.key))
    const known = findings.filter((f) => baseline.has(f.key))

    if (args.updateBaseline) {
      await writeFile(
        args.baseline,
        JSON.stringify(
          {
            recordedAt: new Date().toISOString(),
            note:
              'Findings this suite reports today. Presence here is NOT approval -- it is a record ' +
              'so a run can separate "still broken" from "newly broken". Removing a line is how a ' +
              'fix is locked in.',
            keys: [...new Set(findings.map((f) => f.key))].sort(),
          },
          null,
          1,
        ) + '\n',
      )
    }

    const report = renderReport({
      findings,
      fresh,
      known,
      parity,
      status,
      notes,
      coverage,
      timings,
      origin,
      views: viewList,
      sections: args.sections,
      partial: args.sectionsGiven || args.only !== null,
      scenarios: Object.keys(SCENARIOS),
      outDir: args.outDir,
    })
    await writeFile(join(args.outDir, 'report.txt'), report)
    await writeFile(
      join(args.outDir, 'findings.json'),
      JSON.stringify({ generatedAt: new Date().toISOString(), findings, parity }, null, 1) + '\n',
    )
    await writeFile(
      join(args.outDir, 'requests.json'),
      JSON.stringify(cp.requests, null, 1) + '\n',
    )
    process.stdout.write(report)

    // EXIT CODES, and why there are two failing ones.
    //
    // "Exit non-zero when anything fails" is the requirement, and both of these
    // are non-zero. They are kept apart because this UI has real defects today
    // -- the clipped API paths and the nav/heading disagreements are in
    // docs/web-ui/evidence/ -- and a suite that returns the same 1 forever
    // cannot tell anyone that something got WORSE. 2 is a new finding; 1 is the
    // recorded ones still standing.
    if (fresh.length > 0) return 2
    if (findings.length > 0) return 1
    // NO FINDINGS OVER A PARTIAL RUN IS NOT A PASS. It is the exact shape this
    // repository keeps producing: a gate that passes hardest when it is most
    // broken. If any unit was skipped the run is a failure, and the report
    // names each one.
    if (coverage.ran < coverage.planned) return 1
    // A SUBSET IS NEVER A PASS EITHER. `--only` and `--sections` exist to make
    // a tight loop possible, and a tight loop that can print "PASS" is a
    // button somebody will press instead of running the suite.
    if (args.sectionsGiven || args.only !== null) return 1
    return 0
  } finally {
    await b.close()
    await cp.close()
  }
}

function renderReport(ctx) {
  const L = []
  const bar = '='.repeat(78)
  L.push(bar)
  L.push('SwarmCloud UI -- browser suite')
  L.push(bar)
  L.push(`origin        ${ctx.origin}   (production bundle, control plane on the same origin)`)
  L.push(`browser       ${ctx.status.mode ?? 'unknown'}${ctx.status.mode === 'headed' ? '' : '   <- NOT headed; focus and scrollbar behaviour may differ'}`)
  L.push(`views         ${ctx.views.length}`)
  L.push(`viewports     ${VIEWPORTS.map((v) => v.join('x')).join(', ')}`)
  L.push(`sections      ${ctx.sections.join(', ')}${ctx.partial ? '   <- A SUBSET WAS SELECTED; this run is not a full pass' : ''}`)
  L.push(`scenarios     ${ctx.scenarios.join(', ')}`)
  L.push(`screenshots   ${join(ctx.outDir, 'screenshots')}`)
  L.push('')
  if (ctx.timings.length) {
    const total = ctx.timings.reduce((a, t) => a + t.ms, 0)
    const slowest = [...ctx.timings].sort((a, b) => b.ms - a.ms)[0]
    L.push(
      `pace          ${Math.round(total / ctx.timings.length)}ms per audited viewport, ` +
        `slowest ${slowest.unit} at ${slowest.ms}ms`,
    )
  }
  L.push(`coverage      ${ctx.coverage.ran}/${ctx.coverage.planned} units ran${ctx.coverage.ran < ctx.coverage.planned ? '   <- SOME UNITS DID NOT RUN; see harness notes' : ''}`)
  L.push(`findings      ${ctx.findings.length}   (${ctx.fresh.length} NEW, ${ctx.known.length} already recorded in baseline.json)`)
  L.push('')

  if (ctx.notes.length) {
    L.push('-- harness notes ' + '-'.repeat(60))
    for (const n of ctx.notes) L.push('   ' + n)
    L.push('')
  }

  const byCategory = new Map()
  for (const f of ctx.findings) {
    if (!byCategory.has(f.category)) byCategory.set(f.category, [])
    byCategory.get(f.category).push(f)
  }

  const ORDER = ['honesty', 'api', 'headings', 'identifiers', 'keyboard', 'layout', 'contrast']
  const cats = [...byCategory.keys()].sort((a, b) => {
    const ia = ORDER.indexOf(a), ib = ORDER.indexOf(b)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
  })

  for (const cat of cats) {
    const rows = byCategory.get(cat)
    L.push('-'.repeat(78))
    L.push(`${cat.toUpperCase()}  --  ${rows.length} finding(s)`)
    L.push('-'.repeat(78))

    // Grouped by kind + selector, then the views it happened on. Eighty
    // near-identical lines is a report nobody reads to the end.
    const groups = new Map()
    for (const f of rows) {
      const k = `${f.kind} ${f.selector}`
      if (!groups.has(k)) groups.set(k, [])
      groups.get(k).push(f)
    }
    for (const [k, list] of groups) {
      const [kind, selector] = k.split(' ')
      const isNew = list.some((f) => ctx.fresh.includes(f))
      const where = [...new Set(list.map((f) => f.view))]
      const sizes = [...new Set(list.map((f) => f.viewport))]
      L.push('')
      L.push(`  [${isNew ? 'NEW' : 'known'}] ${kind}   ${selector}`)
      L.push(`        views     ${where.slice(0, 6).join(', ')}${where.length > 6 ? ` (+${where.length - 6} more)` : ''}`)
      L.push(`        viewports ${sizes.join(', ')}`)
      L.push(`        count     ${list.length}`)
      const sample = list.slice(0, 3)
      for (const s of sample) {
        L.push(`        - ${s.view} @ ${s.viewport}: ${s.detail}`)
        if (s.text) L.push(`          text: ${s.text}`)
        if (s.path && s.path !== s.selector) L.push(`          at:   ${s.path}`)
      }
      if (list.length > sample.length) L.push(`        ... and ${list.length - sample.length} more`)
    }
    L.push('')
  }

  L.push('-'.repeat(78))
  L.push('API SEAM (runtime)')
  L.push('-'.repeat(78))
  L.push(`  paths requested and matched to a declared route: ${ctx.parity.touched.length}`)
  for (const t of ctx.parity.touched) L.push(`    ok  ${t}`)
  if (ctx.parity.untouched.length) {
    L.push(`  declared routes this run never exercised: ${ctx.parity.untouched.length}`)
    L.push('    (writes, and screens this matrix does not open -- informational, not a failure)')
  }
  L.push('')
  L.push(bar)
  if (ctx.fresh.length > 0) {
    L.push(`RESULT: FAIL -- ${ctx.fresh.length} finding(s) are NEW since baseline.json (exit 2)`)
  } else if (ctx.findings.length > 0) {
    L.push(`RESULT: FAIL -- ${ctx.findings.length} recorded finding(s) still stand, none new (exit 1)`)
  } else if (ctx.partial) {
    L.push('RESULT: no findings in the selected subset (exit 1 -- a subset is never a pass)')
  } else if (ctx.coverage.ran < ctx.coverage.planned) {
    L.push(
      `RESULT: FAIL -- no findings, but only ${ctx.coverage.ran} of ${ctx.coverage.planned} units ran (exit 1)`,
    )
    L.push('A clean report over a partial run is not a pass. See the harness notes above.')
  } else {
    L.push('RESULT: PASS -- no findings (exit 0)')
  }
  L.push(bar)
  L.push('')
  return L.join('\n')
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    process.stderr.write(`\nui-test: ${err && err.stack ? err.stack : err}\n`)
    process.exit(3)
  })
