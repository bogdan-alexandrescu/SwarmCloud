// THE KEYBOARD SWEEP, walked in slices: one `keyboard.<group>.test.tsx` per
// slice, the way `spacingsweep.tsx` slices the spacing sweep.
//
// WHY IT IS SLICED (owner decision 2026-11-06, part of #2688). Vitest runs a
// file inside one worker, so the single file that walked every route was a
// floor under whichever component shard drew it: 383 s alone in an 8-vCPU
// container with three workers (PR #3513), against a shard's few minutes.
// Each slice file is now its own unit of work for the scheduler.
//
// WHAT HOLDS THE SLICES TOGETHER. `keyboard.test.tsx` asserts that every route
// in ROUTES is in exactly one of KEYBOARD_GROUPS, that every group has its own
// file, that the slices' stop floors add up to the single sweep's STOP_FLOOR,
// and that their help shares add up to the whole. Each slice's help floor is
// its share of half the source's `?` call sites, so the slices together still
// open at least half of them, counted from the source as before. The walk and
// its assertions live here, once; the reasons for each are kept beside it.

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import STYLES from '../styles.css?raw'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, createEvent, fireEvent, render } from '@testing-library/react'

import { App, SECTIONS } from '../App'
import { tabStops } from '../focus'
import { pointerSelectors, probeKeyboard, ringSelectors, signature, type Finding } from './keyboard'
import { resolveSheet } from './spaceprobe'
import { PANEL_PAGES } from '../Spine'

/**
 * Every route the rail can reach, DERIVED rather than restated.
 *
 * The same expression `spacing.test.tsx` uses, and for the same reason it now
 * uses it: that file held a hand-written copy of these fifteen routes, two
 * sections were renamed, nine of its routes resolved through SECTION_ALIASES
 * instead of failing, and the sweep quietly examined 500 fewer shapes while
 * reporting zero findings. A list kept in step by hand is a list that will
 * not be.
 */
export const ROUTES = SECTIONS.flatMap((s) => s.tabs.map((t) => `${s.id}/${t.id}`))

/**
 * 700ms, on a clock this file controls -- see `spacing.test.tsx` for the long
 * version. Past every fixture delay in `api.ts` (30-350ms) and short of the 1s
 * and 5s refresh intervals in `Agents.tsx`, `Overview.tsx`, `App.tsx` and
 * `Dock.tsx`, so each screen sees a fixed number of ticks rather than a
 * function of how busy the machine is.
 */
export const CLOCK_MS = 700

export async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CLOCK_MS)
  })
}

/**
 * Press Tab on an element and answer whether a handler swallowed it.
 *
 * THE EVENT IS BUILT AND READ BACK RATHER THAN TRUSTED TO `fireEvent`'s RETURN
 * VALUE. `fireEvent` answers with `dispatchEvent`'s boolean, which is the same
 * fact -- but through two wrappers (`@testing-library/react` re-wraps every
 * event in `act` and hands the result back), and "the return value survives
 * both wrappers" is an assumption about a library rather than an observation
 * about this app. `defaultPrevented` on the event object is the observation.
 *
 * jsdom implements no sequential focus navigation, so this moves nothing by
 * itself. That is exactly why the question is phrased as "was it swallowed":
 * in this app the only way to change where Tab goes is `preventDefault`, so an
 * unprevented Tab is one the browser will handle normally.
 */
export function tabSwallowed(el: Element, shiftKey = false): boolean {
  const ev = createEvent.keyDown(el, { key: 'Tab', shiftKey })
  fireEvent(el, ev)
  return ev.defaultPrevented
}

/** Selectors read off the sheet once. Neither depends on what is rendered. */
export const RINGS = ringSelectors(STYLES)
export const POINTERS = pointerSelectors(STYLES)

/**
 * THE FLOOR, AND WHY IT IS THIS NUMBER.
 *
 * MEASURED: 576, on the run that established this file, and 518 on CI run
 * 35948635897 before the nav collapsed to three sections. The per-route
 * numbers are in the log of every run and ranged from 24 (`admin/tenants`) to
 * 62 (`capacity/accounts`).
 *
 * THE SHELL ALONE IS 345, AND THAT IS WHY THE FLOOR IS NOT A PERCENTAGE. The
 * rail draws one button per section, one per tab of a section that has more
 * than one (Overview's single pane draws no second level) and two utility
 * buttons, on EVERY route: 4 + 14 + 2 = 20 tab stops that are there whatever
 * the screen behind them does. The header's home link, the dock's line and the
 * head's `?` add three more. 23 x 15 routes = 345 before a single screen has
 * rendered anything at all, so `spacing.test.tsx`'s habit of setting the floor
 * at about 60% of the measurement would put this one BELOW the number a
 * completely blank app still reports.
 *
 * THE SHELL GOT ONE STOP CHEAPER PER ROUTE, not fifteen. Six section buttons
 * and thirteen tab buttons became four and fourteen: the three-section
 * collapse moved panes between sections, it did not remove any, so the tab
 * count went UP by one while the section count went down by two. 480 is
 * unchanged and still has room -- 345 of shell plus half of the ~200 controls
 * that come from real screens -- and moving it because the shell moved by 15
 * would be tuning a floor to the number it is meant to be insensitive to.
 *
 * It fails when the sweep reaches nothing -- the shape this repository keeps
 * producing: a loop that did not word-split, a probe that returned `[]`, a
 * route list that went stale and examined 500 fewer shapes with nothing red --
 * and it also fails if roughly three screens stop rendering their contents.
 * Which screens is a question for the per-route table, not for this number.
 */
export const STOP_FLOOR = 480

/**
 * HOW MANY `?` TRIGGERS THE SOURCE RENDERS, COUNTED FROM THE SOURCE.
 *
 * THIS WAS AN ABSOLUTE 60 AND IT ROTTED THE WEEK IT WAS WRITTEN. It was
 * calibrated against 84 measured triggers with a margin sized to survive the
 * two biggest screens going to their failure state, which was sound reasoning
 * about the wrong kind of number. What it could not survive was the count
 * being deliberately REDUCED: a parallel lane cut the help widgets from 82 to
 * 24 -- the entire point of that work, because a console needing 82
 * explanatory popovers is a console whose labels are not carrying their weight
 * -- and this guard went red on a change that improved the product. Both lanes
 * were right; the hardcoded number was the mistake.
 *
 * AND THE OBVIOUS REPLACEMENT IS VACUOUS. "Every trigger found was exercised"
 * looks like the derived version of this and is not a check at all: the loop
 * below has no `continue`, so a trigger either increments the counter or fails
 * an assertion inside the loop. Found-equals-exercised is therefore true by
 * construction, and it is true at zero -- which is precisely the failure this
 * guard exists for, a selector that stopped matching anything.
 *
 * So the floor is counted from the SOURCE, the way `nav.links.test.tsx` reads
 * every href out of it. `<HelpCard` and `<SectionQuestion` are the two things
 * that render a `?`, and counting their call sites gives a number that moves
 * with the product instead of against it.
 *
 * THE RATIO, AND WHY IT IS NOT 1. Several screens here read `/v1/admin/*`,
 * answer 403 against the fixtures and render an admin panel instead of their
 * content, so their cards never mount. A few more are inside collapsed
 * sections. Half the source count is comfortably above what those can remove
 * and still fails hard if the sweep stops finding cards, which is the whole
 * job. The message names both numbers so a real drop is diagnosable rather
 * than just red.
 *
 * AND THE RATIO IS ALSO NOT 1 BECAUSE ONE CALL SITE IS NOT ONE TRIGGER. The
 * head's `?` is a SINGLE `<SectionQuestion` in App.tsx and it renders on every
 * route, so it contributes 1 to the source count and 15 to the sweep. That is
 * the reason the sweep opened 24 against 19 call sites on run 35948635897, and
 * it is why the derived floor survived the nav collapsing from six sections to
 * three: the collapse moved panes between sections without adding or removing a
 * `<HelpCard>` or a `<SectionQuestion>`, so neither side of the ratio moved.
 * A floor counted from call sites is insensitive to the nav's shape, which a
 * floor counted from routes would not have been.
 */
export function helpTriggersInSource(): number {
  // `join(__dirname, '..')`, the way nav.links.test.tsx in this directory does
  // it. `new URL('..', import.meta.url).pathname` was tried first and resolved
  // to `/src` under vitest -- not `<repo>/apps/swarm-ui/src` -- so the scandir
  // threw ENOENT and the whole sweep failed on its own bookkeeping rather than
  // on anything it was measuring.
  const dir = join(__dirname, '..')
  let n = 0
  for (const name of readdirSync(dir)) {
    if (!name.endsWith('.tsx')) continue
    const text = readFileSync(join(dir, name), 'utf8')
    n += (text.match(/<HelpCard\b/g) ?? []).length
    n += (text.match(/<SectionQuestion\b/g) ?? []).length
  }
  return n
}

/**
 * The shell's controls on one rendered route, read off the shell itself.
 *
 * `all` is what the shell drew; `reachable` is the part of it that should be
 * a Tab stop. The two differ for the panel's locked Admin rows, which a
 * non-admin is shown DISABLED on purpose (Spine.tsx: a hidden page is
 * indistinguishable from one that does not exist) and which are therefore not
 * stops, and for the theme switch's unchosen radios: a radio group is one
 * stop, its chosen radio (ThemeToggle.tsx, V074).
 */
function shellControls(root: Element): Record<string, { all: Element[]; reachable: Element[] }> {
  const spine = root.querySelector('.sk-spine')
  const panel = root.querySelector('.sk-panel')
  const of = (els: Element[]) => ({ all: els, reachable: els })
  // LINKS SINCE #503 -- a section, a page and Help open in a new tab and copy
  // as a link -- so the stops are `a` as well as `button`. A locked Admin row
  // is a span with `aria-disabled`, shown and deliberately not a stop.
  const pages = panel === null ? [] : [...panel.querySelectorAll('[class$="pscroll"] a, [class$="pscroll"] button, [class$="pscroll"] [aria-disabled="true"]')]
  return {
    'spine section': of(spine === null ? [] : [...spine.querySelectorAll(':scope > a.sk-ri:not(.sk-cta)')]),
    'Submit button': of(spine === null ? [] : [...spine.querySelectorAll(':scope > a.sk-cta')]),
    'panel page': {
      all: pages,
      reachable: pages.filter((el) => !(el as HTMLButtonElement).disabled && el.getAttribute('aria-disabled') !== 'true'),
    },
    // A RADIO GROUP IS ONE STOP (visual QA V074, 2026-10-11): the theme
    // switch's chosen radio is in the Tab order and the arrow keys move the
    // choice, as a native group does. Its other radios are drawn and are
    // deliberately not stops; the group still is, so it cannot drop out.
    'footer control': ((els: Element[]) => ({
      all: els,
      reachable: els.filter((el) => el.getAttribute('role') !== 'radio' || el.getAttribute('aria-checked') === 'true'),
    }))([
      ...(spine === null ? [] : [...spine.querySelectorAll('.sk-foot a, .sk-foot button')]),
      ...(panel === null ? [] : [...panel.querySelectorAll('.sk-ph button, .sk-pfoot button')]),
    ]),
  }
}

/**
 * The panel pages its model names that the rendered panel does not draw. The
 * panel renders `PANEL_PAGES[section]` for Work, Capacity and Admin; the
 * section is read off the panel's own title, so this holds the rendered rows
 * to the model rather than to a count.
 */
function panelModelMissing(root: Element): string[] {
  const title = (root.querySelector('.sk-panel .sk-pt')?.textContent ?? '').trim()
  const sec = title === 'Work' ? 'work' : title === 'Capacity' ? 'capacity' : title === 'Admin' ? 'admin' : null
  if (sec === null) return []
  const drawn = new Set([...root.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((el) => (el.textContent ?? '').trim()))
  return PANEL_PAGES[sec].map((p) => p.label).filter((label) => !drawn.has(label))
}

export interface KeyboardGroup {
  /** The slice's name; its file is `keyboard.<name>.test.tsx`. */
  name: string
  /** The routes it walks, in the rail's order. */
  routes: readonly string[]
  /** Its part of STOP_FLOOR: the slice must reach more stops than this. */
  stopFloor: number
  /** The help cards it opened on the run that set these slices: its share of the source's floor. */
  helps: number
}

/**
 * THE SLICES, in the rail's order, sized by the elements their screens draw
 * (the sweep's own per-route table, 2026-11-07: 37 routes, 1,351 tab stops,
 * 34 help cards, about 14,400 elements; the walk's cost follows the elements).
 *
 * STOP FLOORS. 36% of each slice's measured stops, rounded up to ten, so they
 * add up to 490 against the single sweep's 480: work 385, capacity 242,
 * accounts 232, config 267, admin 225 stops measured. The admin slice's are
 * the shell's own -- the fixture person administers nothing, so each Admin
 * route draws the rail and no screen -- which is why it is above zero at all.
 *
 * HELP SHARES are the cards each slice opened on that run (11, 8, 6, 6, 3):
 * each slice's floor is its share of half the source's call sites, rounded
 * up, so the five floors add up to at least the single sweep's half.
 */
export const KEYBOARD_GROUPS: readonly KeyboardGroup[] = [
  {
    name: 'work',
    routes: ['overview/now', 'work/running', 'work/workflows', 'work/runs', 'work/timeline', 'work/new', 'work/new-workflow', 'work/new-issue'],
    stopFloor: 140,
    helps: 11,
  },
  {
    name: 'capacity',
    routes: ['automate/schedules', 'automate/approvals', 'capacity/pools', 'capacity/catalogue', 'capacity/profiles', 'capacity/holders'],
    stopFloor: 90,
    helps: 8,
  },
  {
    name: 'accounts',
    routes: ['capacity/llm-providers', 'capacity/account-health', 'capacity/quota', 'capacity/jev-usage', 'capacity/api-reads'],
    stopFloor: 90,
    helps: 6,
  },
  {
    name: 'config',
    routes: ['config/github', 'config/repositories', 'config/secrets', 'config/mcp-servers', 'config/plugins', 'config/workspace'],
    stopFloor: 100,
    helps: 6,
  },
  {
    name: 'admin',
    routes: [
      'admin/limits', 'admin/runner-sizes', 'admin/tenants', 'admin/counts', 'admin/platform-secrets', 'admin/plugins',
      'admin/setup', 'admin/health', 'admin/people', 'admin/workspaces',
      'voice/talk', 'voice/projects', 'voice/preferences', 'voice/settings', 'voice/telemetry',
    ],
    stopFloor: 80,
    helps: 3,
  },
]

/** The cards the single sweep opened, which the shares divide. */
export const KEYBOARD_HELPS_TOTAL = KEYBOARD_GROUPS.reduce((n, g) => n + g.helps, 0)

/** A slice's help floor: its share of half the source's `?` call sites, rounded up. */
export function helpFloor(group: KeyboardGroup, inSource: number): number {
  return Math.ceil((Math.ceil(inSource / 2) * group.helps) / KEYBOARD_HELPS_TOTAL)
}

/** Register one slice's sweep. Each `keyboard.<group>.test.tsx` is this call. */
export function keyboardGroup(name: string): void {
  const found = KEYBOARD_GROUPS.find((g) => g.name === name)
  if (found === undefined) throw new Error(`no keyboard group named ${name}`)
  const slice: KeyboardGroup = found
  let sheet: HTMLStyleElement

  describe(`keyboard traversal: ${slice.name}`, () => {
    beforeAll(() => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
      // THE SHIPPED SHEET, RESOLVED. Two things here need it and both are
      // silent without it: `display: none` is how `styles.css` removes the
      // inner drawer's close button from the focus order, and `position: fixed`
      // vs `sticky` is how `isOverlay` tells the modal drawer from the side
      // column. With `css: false` in vitest.config.ts this import would be the
      // empty string and every one of those questions would answer "no rule",
      // which reads as a pass.
      sheet = document.createElement('style')
      sheet.textContent = resolveSheet(STYLES, 'dark')
      document.head.appendChild(sheet)
    })

    afterAll(() => {
      sheet.remove()
      window.location.hash = ''
      vi.useRealTimers()
    })

    /**
     * A SWEEP THAT OUTLIVES ITS TEST IS STOPPED BEFORE THE NEXT ONE STARTS.
     *
     * A test that times out is abandoned, not cancelled: its body keeps
     * rendering routes and advancing the shared fake clock. On 2026-10-19 (16:42Z
     * and 23:34Z, #1822) the sweep crossed its limit on a busy runner and the
     * drawer test after it found "no rows" in a DOM the sweep was still
     * rendering into. So the sweep checks `sweepStopped` between routes, and
     * this hook sets it and waits until the sweep has really returned: the next
     * test starts on an empty page whatever the sweep's own outcome, and a sweep
     * timeout is reported as itself.
     */
    let sweepStopped = false
    let sweepRun: Promise<void> = Promise.resolve()
    afterEach(async () => {
      sweepStopped = true
      // The sweep's own failure is reported by its own test, not again here.
      await sweepRun.catch(() => undefined)
    }, 120_000)

    async function sweepGroup(): Promise<void> {
      const per: { route: string; stops: number; helps: number; elements: number }[] = []
      const findings: Finding[] = []
      const unsupported = new Set<string>()
      const swallowers: string[] = []
      const shellRendered: { route: string; what: string; count: number }[] = []
      const shellMissing: string[] = []
      let stops = 0
      let ringed = 0
      let helps = 0

      for (const route of slice.routes) {
        if (sweepStopped) return
        window.location.hash = `#${route}`
        const { container, unmount } = render(<App />)
        await settle()
        if (sweepStopped) {
          unmount()
          return
        }

        // A screen's own `<style>` element and the cursors in it. Overview
        // shipped one (`OVERVIEW_CSS`) until U8 folded it into styles.css, where
        // POINTERS already reads its cursors; no screen ships one now. Kept so a
        // sheet that comes back is read rather than reported as controls the
        // sheet never gave a cursor to.
        const local = [...container.querySelectorAll('style')].flatMap((el) =>
          pointerSelectors(el.textContent ?? ''),
        )

        const r = probeKeyboard(document, {
          appRoot: container,
          ringSelectors: RINGS,
          pointerSelectors: [...POINTERS, ...local],
        })
        findings.push(...r.findings.map((f) => ({ ...f, where: `${route} ${f.where}` })))
        for (const u of r.unsupported) unsupported.add(u)

        // EVERY SHELL CONTROL IS A TAB STOP: each spine section, the Submit
        // button, each panel page and the footer controls (the spine's Help and
        // API reads, the panel's collapse and the theme switch).
        const stopSet = new Set<Element>(r.stopElements)
        // NO LOCKED ADMIN PANEL (NAV-ADMIN-ROLE; part of #1633): the fixture
        // person administers nothing, so an Admin route draws no Admin item and
        // no Admin page in the panel. Since CONFIG-NAV (part of #2157) the route
        // also sends them to Overview once both reads say so, whose panel is its
        // jumps: what must never be drawn is a link to an Admin page.
        // Config › Workspace Settings's address opens the workspace's own page,
        // an Admin page (admin routes lane, part of #2688), so where the route
        // landed counts as well as the route it was typed as.
        const landed = `${window.location.pathname}${window.location.hash}`
        const onAdmin = route.startsWith('admin/') || /(^\/|#\/?)admin\//.test(landed)
        const noAdmin = onAdmin && container.querySelector('.sk-spine a[data-sec="admin"]') === null
        for (const [what, group] of Object.entries(shellControls(container))) {
          if (noAdmin && what === 'panel page') {
            const adminRows = group.all.filter((el) => (el.getAttribute('href') ?? '').startsWith('/admin'))
            expect(adminRows, `${route}: a locked Admin panel was drawn for someone who administers nothing`).toEqual([])
            continue
          }
          shellRendered.push({ route, what, count: group.all.length })
          for (const el of group.reachable) {
            if (!stopSet.has(el)) shellMissing.push(`${route} ${what}: ${signature(el)} "${(el.textContent ?? '').trim()}"`)
          }
        }
        for (const label of panelModelMissing(container)) shellMissing.push(`${route} panel page not rendered: ${label}`)
        stops += r.stops
        ringed += r.ringed

        // EVERY `?` ON THIS SCREEN, OPENED FROM THE KEYBOARD AND DISMISSED WITH
        // ESCAPE. `button[aria-label^="Help: "]` is both kinds -- `HelpCard`'s
        // "Help: Absent is not zero" and `SectionQuestion`'s "Help: Work
        // answers" -- and no other control in the app is named that way (the
        // rail's own `?` is plain "Help", with no colon).
        const triggers = [...container.querySelectorAll<HTMLElement>('button[aria-label^="Help: "]')]
        for (const trigger of triggers) {
          const name = trigger.getAttribute('aria-label') ?? ''
          await act(async () => {
            trigger.focus()
          })
          expect(trigger.getAttribute('aria-expanded'), `${route}: "${name}" did not open on focus`).toBe(
            'true',
          )
          const cardId = trigger.getAttribute('aria-controls')
          expect(cardId, `${route}: "${name}" is open and controls nothing`).not.toBeNull()
          const card = document.getElementById(cardId ?? '')
          expect(card, `${route}: "${name}" points aria-controls at no element`).not.toBeNull()

          // A PORTALLED CARD WITH A TAB STOP IN IT HAS TO SAY WHERE FOCUS GOES
          // BACK TO. This is the assertion the portal defect fails: strip
          // `data-focus-return` or the bridge that justifies it and this names
          // the card. A card with nothing focusable in it needs no bridge and
          // is not asked for one.
          if (card !== null && tabStops(card).length > 0) {
            expect(
              card.closest('[data-focus-return]')?.getAttribute('data-focus-return'),
              `${route}: "${name}" is portalled with a tab stop in it and no way back`,
            ).toBe(trigger.id)
          }

          await act(async () => {
            fireEvent.keyDown(trigger, { key: 'Escape' })
          })
          expect(trigger.getAttribute('aria-expanded'), `${route}: "${name}" survived Escape`).toBe(
            'false',
          )
          expect(document.getElementById(cardId ?? ''), `${route}: "${name}" left its card behind`).toBeNull()
          expect(document.activeElement, `${route}: "${name}" lost focus on Escape`).toBe(trigger)
          helps += 1
        }

        // NOTHING ON A PLAIN ROUTE MAY SWALLOW TAB. The only way to change where
        // Tab goes in this app is `preventDefault` on the keydown, and
        // `fireEvent` answers false when that happened. None of these fifteen
        // routes opens a drawer, and every help card above was dismissed, so the
        // expected set here is empty -- the drawer's trap is asserted on its own
        // route, below, where it is meant to exist.
        // `r.stopElements`, not a second `tabStops(document.body)`: the probe has
        // already paid for the computed-style pass that produces this list, and
        // paying for it twice per route is most of this suite's running time.
        for (const el of r.stopElements) {
          for (const shiftKey of [false, true]) {
            if (tabSwallowed(el, shiftKey)) {
              swallowers.push(`${route} ${signature(el)}${shiftKey ? ' [shift]' : ''}`)
            }
          }
        }

        per.push({ route, stops: r.stops, helps: triggers.length, elements: r.elements })
        unmount()
      }

      // PRINTED BEFORE ANYTHING IS ASSERTED, for the reason `spacing.test.tsx`
      // gives: six assertions follow and the first to fail hides the rest, but
      // the useful artefact is the whole picture and "the numbers before and
      // after" cannot be read off a run that stopped at the first difference.
      console.log(
        [
          `${per.length} routes · ${stops} tab stops · ${helps} help cards opened` +
            ` · ${ringed} stops covered by a :focus-visible rule` +
            ` · ${findings.length} findings`,
          ...per.map(
            (p) => `  ${p.route.padEnd(22)} ${String(p.stops).padStart(4)} stops` +
              ` ${String(p.helps).padStart(3)} cards  of ${p.elements} elements`,
          ),
          ...findings.map((f) => `    ${f.kind} ${f.where} — ${f.detail}`),
          ...swallowers.map((s) => `    swallowed Tab: ${s}`),
          ...shellMissing.map((s) => `    shell control not reached: ${s}`),
          ...[...unsupported].map((s) => `    unsupported selector: ${s}`),
        ].join('\n'),
      )

      // THE SWEEP HAS TO HAVE LOOKED AT SOMETHING. Four guards, in the order
      // they would catch a break: a route list that stopped matching the rail, a
      // screen that rendered nothing, a body that produced no controls, and a
      // pass that opened no cards.
      expect(per.length, `the ${slice.name} slice rendered a different number of routes than it names`).toBe(
        slice.routes.length,
      )
      // THE SHELL'S OWN STOPS, EACH ONE, NOT A COUNT (owner decision 2026-10-01).
      // This was `p.stops > 10` per route: a floor any ten controls cleared, so
      // a spine section, a panel page or the theme switch could drop out of the
      // Tab order and the floor still held. The property is that every one of
      // them is a stop, and the list is read off the shell each route rendered
      // (and the panel's against `PANEL_PAGES`, the model it renders from), so
      // no count is written here to go stale.
      for (const s of shellRendered) {
        expect(s.count, `${s.route}: the shell rendered no ${s.what}`).toBeGreaterThan(0)
      }
      expect(shellMissing, 'shell controls the Tab order does not reach').toEqual([])
      expect(stops, 'the sweep reached almost nothing').toBeGreaterThan(slice.stopFloor)

      // EVERY CARD FOUND WAS EXERCISED. Not a threshold on how many exist -- see
      // the comment on HELP_MUST_EXIST for why that number could not survive the
      // help count being deliberately cut. A shortfall here names the routes it
      // happened on rather than reporting one total against one constant.
      const inSource = helpTriggersInSource()
      const floor = helpFloor(slice, inSource)
      expect(
        helps,
        `the ${slice.name} slice opened ${helps} help cards; the source renders ${inSource} ` +
          `<HelpCard>/<SectionQuestion> call sites, and this slice's share of half of them is ${floor}, so anything under it means ` +
          `the sweep stopped finding them rather than that the product has fewer. ` +
          `Per route: ${per.filter((x) => x.helps > 0).map((x) => `${x.route}=${x.helps}`).join(' ')}`,
      ).toBeGreaterThanOrEqual(floor)

      // Selectors jsdom would not evaluate. Each one is a rule that silently
      // stopped being checked, which is worse than a finding.
      expect([...unsupported]).toEqual([])
      expect(swallowers).toEqual([])
      expect(findings.map((f) => `${f.kind} ${f.where} — ${f.detail}`)).toEqual([])
      // 780s, NOT 480s. MEASURED: 183.7s for this test alone on an idle
      // 6-core agent container (2026-10-20), and 241.8s -- a timeout -- in shard
      // 4/4 of CI run 39342685547, whose sweep rendered fifteen routes; 240s was
      // 76% of the idle cost, so any busy runner crossed it, and the abandoned
      // sweep kept rendering into the next test, which then found no agent rows
      // (the hook above now stops it). 480s was 2.6x that idle cost. The rail
      // has since grown to 31 routes: 290.6s alone on the same idle 6-core
      // container (2026-10-27), and 486.8s -- a timeout, after the sweep had
      // finished with 0 findings -- in shard 4/4 of CI run 40436247349. 780s
      // keeps the 2.6x margin over today's idle cost. A genuine hang still
      // fails, at thirteen minutes; re-measure when the rail grows again.
      // SLICED (part of #2688, 2026-11-07): the single sweep of 37 routes took
      // 395 s alone with three workers; each slice walks a fifth of it under the
      // same limit.
    }

    it('reaches every control on every route, and nothing swallows Tab', async () => {
      sweepStopped = false
      sweepRun = sweepGroup()
      await sweepRun
    }, 780_000)
  })
}
