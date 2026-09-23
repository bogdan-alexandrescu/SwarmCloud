import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { createPortal } from 'react-dom'
import { INSPECTOR, clampPane, readPane, summariseProbes, writePane } from './panes'
import { AccountsScreen } from './Accounts'
import { ActivityScreen, TenantsScreen } from './Activity'
import { AdminSettingsScreen } from './AdminSettings'
import { AgentDetailScreen } from './AgentDetail'
import { AgentsScreen } from './Agents'
import { AttemptTimelineScreen } from './AttemptTimeline'
import { ProductHeader } from './Brand'
import { CapacityScreen } from './Capacity'
import { Dock } from './Dock'
import { probeSnapshot, subscribeProbes, type ProbeRecord } from './fetch'
import { useHelpDisclosure, useEdgeSafePlacement } from './HelpCard'
import { HELP_ROUTE } from './help'
import { HelpScreen } from './HelpSection'
import { HoldersScreen } from './Holders'
import { OverviewScreen } from './Overview'
import { PlatformCountsScreen } from './PlatformCounts'
import { ProfilesScreen } from './Profiles'
import { QuotaDetailScreen } from './QuotaDetail'
import { RuntimesScreen } from './Runtimes'
import { timeAgo } from './Shell'
import { SubmitScreen } from './Submit'
import { SubmitWorkflowScreen } from './SubmitWorkflow'
import { WorkflowsScreen } from './Workflows'

/**
 * THE NAVIGATION, AND WHY IT IS AS FEW THINGS AS IT IS.
 *
 * It was eleven: Home, Trouble, Capacity, Holders, Agents, Workflows,
 * Activity, Quota, Counts, Tenants, Settings. Six of those were one table
 * each, and the list was ordered by which route served it rather than by what
 * anyone was trying to find out — so "is there room to run another agent"
 * lived in three separate top-level items and "why has my agent not moved"
 * lived in two others.
 *
 * SECTIONS ARE NAMED AFTER OBJECTS, NOT QUESTIONS. An earlier pass labelled
 * them with the question each one answers ("Capacity", "Activity"), which
 * reads well once and then has to be re-read every time: a label that is a
 * paraphrase cannot be predicted from the thing you are looking for. Temporal
 * (Workflows, Schedules, Namespaces) and Nomad (Jobs, Clients, Servers,
 * Variables) both name the noun and let the tabs be the views, and this now
 * does the same:
 *
 *   Overview · Agents · Runtimes · Pools · History · Admin
 *
 * "Pools" because the section lists pools; "History" because it is what
 * already happened. The QUESTION each section answers is still here, and it is
 * still the test for what belongs in the section rather than a name anyone has
 * to memorise -- but it is no longer PRINTED on every pane. It was rendered
 * under the tab strip on all fifteen routes, which is five copies of the same
 * sentence for Pools alone, costing a line of vertical on every screen to say
 * something nobody re-reads after the first visit. §B2 moves it behind the
 * head's `?` (see `SectionQuestion`): written once, one keystroke from any
 * pane, and unchanged.
 *
 * That test is also what lets the list grow again honestly. It was five when
 * the redesign landed; "Runtimes" is the sixth, and the note on that entry
 * argues it against all five questions rather than against the count. A
 * section added because a screen existed would be the eleven-item nav coming
 * back; a section added because no existing question covers the screen is the
 * test working.
 *
 * THERE IS NO PROBLEM SECTION, AT ANY LEVEL. Neither Temporal nor Nomad has
 * one, and this platform has no alerting engine and no incident model, so a
 * section called "Trouble", "Alerts" or "Incidents" would claim machinery that
 * does not exist. The checks this UI derives live as a pane at the top of
 * Overview, re-derived on every read; a failed agent is a row in the Agents
 * list (its Recent tab) like any other row. The Trouble board consequently has
 * no route — see the note on `LEGACY`.
 *
 * A TAB'S LABEL AND ITS SCREEN'S HEADING ARE ONE NAME, and there is a test.
 * Seven of the sixteen routes here used to disagree: "Pools" opened a page
 * headed "Capacity", "Timeline" opened "Activity", "Holders" opened "Capacity
 * holders", "Pool limits" opened "Admin settings", both submit tabs opened
 * pages with different verbs, and the utility button said "Reference" over a
 * page headed "API surface". A reader cannot tell a rename from a redirect, so
 * each of those is a question about whether the click went where it said. It
 * also breaks every external reference — a runbook step "go to Pools" named
 * nothing on the screen it landed you on.
 *
 * The fix went BOTH WAYS on purpose: where the heading was the better name it
 * became the tab ("Capacity holders", "Submit a task"), where the tab was
 * better the heading gave way ("Pools", "Timeline", "Pool limits"), and where
 * neither was honest both were replaced (see REFERENCE_LABEL). Whichever side
 * moved, the reason is written beside the value it changed.
 *
 * THREE OF THOSE TAB LABELS NOW DIVERGE FROM docs/web-ui/redesign.md, whose
 * pane table (lines 69-74) still lists "New agent", "New workflow", "Holders"
 * and "Reference", and from docs/web-ui/ui-audit-and-build-prompt.md, which
 * proposed the opposite trade on two pairs -- shortening the heading to
 * "Holders" rather than lengthening the tab, and heading this section's
 * Timeline pane "History", which is the SECTION's name and so would have left
 * the tab and the heading still disagreeing. Both files are Track D and are
 * not edited from here; the divergence is deliberate and is reported rather
 * than patched. If those recommendations are reinstated, change both sides of
 * the pair -- the test below does not care which name wins, only that one does.
 *
 * tests/unit/control_plane/test_nav_headings_agree.py reads this array, the
 * SectionBody switch and every screen's `Screen title=` / `<h1>`, and fails on
 * any route where the two differ — including a route added later, which is the
 * case a one-off audit does not cover.
 *
 * Four finished screens that no route had ever pointed at — the runner-profile
 * catalogue, both submit forms and the attempt timeline — are reachable, and
 * so, for the first time, is the Overview screen itself. Every old hash still
 * resolves; see LEGACY and SECTION_ALIASES below. The full reasoning is
 * docs/web-ui/redesign.md — that file is the place to argue with this, not
 * this array.
 */
interface TabDef {
  id: string
  label: string
  /**
   * Marks a tab that reads `/v1/admin/*`. It is a LABEL, not a gate: the
   * screens behind it render their own "admin only" panel, which is
   * information rather than an error. Saying so before the click is cheaper
   * than a blue panel after it, and this UI never hides a route from someone
   * to spare them a 403 — a hidden tab is indistinguishable from a tab that
   * does not exist.
   */
  admin?: boolean
}

interface SectionDef {
  /**
   * The id is the first hash segment, so it is also the address people paste.
   * Renaming one is a breaking change to a link someone saved; the previous
   * spelling therefore lives on in SECTION_ALIASES rather than being dropped.
   */
  id: string
  /** The OBJECT this section is about. A noun, never a question. */
  label: string
  /**
   * What this section answers. NOT the name, and no longer printed on the
   * pane: it is the test for whether a screen belongs in this section, kept in
   * the file the sections are declared in so it is read by whoever adds the
   * next one, and rendered behind the head's `?` (§B2) for the reader who
   * wants it.
   */
  question: string
  tabs: TabDef[]
}

/**
 * The two section ids this file also compares against by hand.
 *
 * Constants rather than literals because each was spelled in four places in
 * `fromHash` and `canonical` alone, and a rename that updated three of them
 * would not fail to compile -- it would route one link wrong. The other four
 * sections are never compared against and stay literals in the array, where
 * there is only one of each to be wrong.
 */
export const WORK = 'work'
export const CAPACITY = 'capacity'

/**
 * EXPORTED FOR THE SWEEPS, which is not the same as exported for reuse.
 *
 * `__tests__/spacing.test.tsx` held its own hand-written copy of every route
 * the rail can reach -- under a comment saying "a screen added without being
 * added here would be a screen nothing measures, which is the hole this
 * repository keeps producing". It was right, and it was itself the hole: when
 * two sections were renamed the copy went on naming the old ids, every route
 * resolved through SECTION_ALIASES, and the sweep quietly examined 798 shapes
 * where it had examined 1295. Zero findings, 500 shapes unmeasured, nothing
 * red. It derives from this array now.
 */
export const SECTIONS: SectionDef[] = [
  {
    id: 'overview',
    label: 'Overview',
    question:
      'Is the platform healthy right now, and if not, what is the first thing to look at?',
    // ONE PANE, so the tab strip does not render at all. The second pane here
    // used to be "Needs attention", a problem board; the checks it drew are
    // now a pane at the top of this screen, which is where someone landing on
    // the platform reads them without a click.
    tabs: [{ id: 'now', label: 'Overview' }],
  },
  {
    // RENAMED WITH THE LABEL, not left behind it. An id that says `agents`
    // under a section called Work is the same two-spellings defect in a
    // quieter place: SECTION_ALIASES would keep every old href working, and
    // because it would, nothing would ever make anyone update one. Every
    // internal link was moved with it, and `nav.links.test.tsx` fails the
    // build if an internal href ever uses an alias again.
    // A LITERAL, not the WORK constant, and the reason is a gate rather than a
    // preference: `tests/unit/control_plane/test_nav_headings_agree.py` reads
    // this array out of the TypeScript with a regex -- it cannot import it,
    // because it is a Python test asserting that every SectionBody case has a
    // tab and every tab has a case. `id: WORK` made it fail to COLLECT, which
    // took the whole unit-test job down rather than one assertion. The
    // constant is still used everywhere a comparison happens; the
    // `nav.links.test.tsx` assertion that the two constants name real sections
    // is what stops these drifting apart.
    id: 'work',
    // WAS 'Agents', which made the rail read `Agents > Agents` and the
    // breadcrumb `Agents ▸ Agents`, because this section's first tab is the
    // agent list and both levels render (Rail draws the tab strip whenever
    // tabs.length > 1, and so does the crumb).
    //
    // The section is not the agent list. It holds the agent list, the workflow
    // list, and the two screens that CREATE one of each -- four screens whose
    // common noun is the work itself, not one of its two shapes. Naming the
    // parent after one of its children is what produced the duplicate, and
    // renaming the child would have been the wrong half: `Work > Agents`,
    // `Work > Workflows`, `Work > Submit a task` each say something the
    // section name does not, which is the test a tab has to pass.
    label: 'Work',
    question:
      'What is running, what is waiting, what did it produce — and why has mine not moved?',
    tabs: [
      { id: 'running', label: 'Agents' },
      { id: 'workflows', label: 'Workflows' },
      // "Submit a task", not "New agent", and the screen's own copy is why.
      // Submit.tsx creates a TASK at READY or PARKED and then says, in the
      // panel it renders on success, "That is not a running agent" -- because
      // invariant 1 is that neither state holds capacity. A tab promising an
      // agent and a page explaining you have not got one is the contradiction,
      // and the tab is the side that was wrong. `task` is also the noun the
      // API uses (`POST /v1/tasks`, `TaskCreate`), the same test that named
      // the Runtimes section after `/v1/runtimes`.
      { id: 'new', label: 'Submit a task' },
      { id: 'new-workflow', label: 'Submit a workflow' },
    ],
  },
  {
    // WHY THIS IS A SECTION AND NOT A TAB, given the redesign above went to
    // some trouble to get the nav down to five.
    //
    // The sections' `question` fields are the membership test, and this screen
    // fails all five of them. It is not about health (Overview), not about what
    // is running (Agents), not about room (Pools), not about the past
    // (History) and changes nothing (Admin). The closest fit was a sixth tab
    // under Pools beside "Runner profiles" — and those two would then be
    // adjacent tabs whose labels are near-synonyms while answering different
    // questions, which is how someone ends up reading per-tenant headroom as a
    // platform figure. They stay apart, and each names the other in prose.
    //
    // It is a noun, like the rest, and it is the noun the API and the caller
    // already use: the route is `/v1/runtimes` and the field a submission
    // carries is `runner_profile`. It sits before Pools because "what is this
    // thing and how big is one" is the question you answer before "how many
    // fit".
    //
    // ONE PANE, so the tab strip does not render.
    id: 'runtimes',
    label: 'Runtimes',
    question:
      'What kinds of agent can this platform run, where does each one run, and how big is one?',
    tabs: [{ id: 'catalogue', label: 'Runtimes' }],
  },
  {
    // BACK TO `capacity`, which this section was called before an earlier
    // pass renamed it to `pools`. That rename is what produced `Pools > Pools`
    // -- it named the parent after its first child -- so this is not churn for
    // its own sake, it is undoing the half of that change that was wrong. Both
    // old spellings resolve; see SECTION_ALIASES.
    id: 'capacity', // a literal for the same reason as `work` above
    // THE SECOND `X > X`. Same defect as Work above and the same fix: this
    // section has five tabs, so both levels render, and the first tab is the
    // pool table -- `Pools > Pools`.
    //
    // `Capacity` is the section's own `question` in one word, and it is the
    // word the five tabs have in common: pools, profiles, holders, accounts
    // and provider quota are five different ceilings on the same thing. The id
    // stays `pools` for the reason given on Work.
    label: 'Capacity',
    question:
      'Is there room to run more, which ceiling is the binding one, and what is holding what there is?',
    tabs: [
      { id: 'pools', label: 'Pools' },
      { id: 'profiles', label: 'Runner profiles' },
      // "Holders", and the earlier argument for "Capacity holders" is what
      // makes it right rather than what it overrules. That argument was:
      // "Holders" alone does not say holders of WHAT, and one tab away from
      // "Accounts" it reads as people. True -- while the section was called
      // Pools. The section is now called Capacity, so the parent supplies the
      // noun the tab was carrying for it, and `Capacity > Capacity holders`
      // says it twice. The requirement never changed; what changed is where it
      // is met.
      { id: 'holders', label: 'Holders' },
      // Accounts moved out of Settings deliberately. The subscription pool's
      // five-hour and seven-day windows are the only used-against-available
      // reading this platform has that is not a pool counter, and a
      // REAUTH_REQUIRED account removes capacity exactly the way a lowered
      // ceiling does. Someone asking "why can nothing start" has to find it,
      // and nobody looks under Settings for that. `#settings/accounts` still
      // works and lands here.
      { id: 'accounts', label: 'Accounts' },
      { id: 'quota', label: 'Provider quota', admin: true },
    ],
  },
  {
    // `activity` was the old id and still resolves; see SECTION_ALIASES.
    id: 'history',
    label: 'History',
    question: 'What has this platform done over time, who used it, and what did it cost?',
    tabs: [
      { id: 'timeline', label: 'Timeline' },
      { id: 'counts', label: 'Platform counts', admin: true },
    ],
  },
  {
    id: 'admin',
    label: 'Admin',
    question: 'Change a ceiling, or see who is registered to use this platform.',
    tabs: [
      { id: 'limits', label: 'Pool limits', admin: true },
      { id: 'tenants', label: 'Tenants', admin: true },
    ],
  },
]

/**
 * The API surface. Reachable, and deliberately NOT one of the sections.
 *
 * It is a thing you look up once, not a thing you work in, and giving it a
 * section would put it beside five questions that people arrive with — which
 * is the mistake the old eleven-item nav made eleven times over.
 */
const REFERENCE = 'reference'

/**
 * The utility button's label AND the heading of the screen it opens. One
 * constant, used in both places, because they are two renderings of one name.
 *
 * NEITHER OLD NAME SURVIVED, and the screen's own lead is the argument. The
 * button said "Reference" and the page said "API surface"; the first paragraph
 * of that page says, in bold, that it is NOT a list of the endpoints
 * SwarmCloud offers -- it is every route this browser tab has called since it
 * loaded. So "Reference" promised documentation the screen does not hold and
 * "API surface" promised completeness the screen disclaims in its own first
 * sentence. Copying either one onto the other would have made the app agree
 * with itself about something untrue.
 *
 * The hash stays `reference`: it is an address people have saved, and
 * SECTION_ALIASES exists precisely because renaming one breaks a saved link.
 */
const REFERENCE_LABEL = 'API reads'

/**
 * Help. Reachable, and deliberately NOT one of the sections either (§B7.3).
 *
 * Exactly the argument above, for exactly the same reason: it is a thing you
 * look up, not a thing you work in. It takes a tail -- `#help/absent-vs-zero`
 * -- because every `?` card in the product links to one topic, and a link that
 * dumped the reader at the top of a page of thirteen would be a link nobody
 * follows twice.
 */
const HELP = HELP_ROUTE

/**
 * Every hash the eleven-item nav produced, still resolving.
 *
 * Not a courtesy. These hashes are in runbooks, in incident notes and in the
 * links people paste to each other at 3am, and a redesign that silently
 * redirects them all to Home is a redesign that loses the one link someone
 * needed. Each lands on the pane that answers what the old screen answered,
 * and the address bar is rewritten to the new form so the next copy of the
 * link is the current one.
 *
 * `agents`, `pools` and `activity` are NOT here although all three were old
 * top-level hashes: they are section ids that were renamed, and SECTION_ALIASES
 * resolves them first, with their tail intact. An entry here as well would be
 * dead and would read as the place they are handled.
 */
const LEGACY: Record<string, { section: string; tab: string }> = {
  home: { section: 'overview', tab: 'now' },
  // The Trouble board has no route any more: there is no problem section at
  // any level. The hash still resolves, and it resolves to the screen that
  // carries the derived checks it used to hold.
  trouble: { section: 'overview', tab: 'now' },
  holders: { section: CAPACITY, tab: 'holders' },
  quota: { section: CAPACITY, tab: 'quota' },
  // `agents` and `pools` are NOT here, for the reason given above: they are
  // section ids that were renamed, SECTION_ALIASES resolves them with their
  // tail intact, and an entry here would be dead code that reads as the place
  // they are handled. `workflows` is different -- it was never a section id,
  // it was a top-level hash for what is now a tab, so it belongs here.
  workflows: { section: WORK, tab: 'workflows' },
  counts: { section: 'history', tab: 'counts' },
  tenants: { section: 'admin', tab: 'tenants' },
  settings: { section: 'admin', tab: 'limits' },
}

/**
 * Section ids that were renamed, old spelling to new.
 *
 * Distinct from LEGACY, and it has to be: LEGACY maps a whole old hash to one
 * destination, which is right for `#holders` (a section that became a tab) and
 * wrong for `#capacity/holders` — that one has a TAIL, and folding it through
 * LEGACY would drop the tail and land on the section's first pane. Here the
 * head is rewritten and the tail keeps its meaning, so every `#capacity/<tab>`
 * and `#activity/<tab>` link written before the rename still opens the pane it
 * named.
 */
const SECTION_ALIASES: Record<string, string> = {
  agents: WORK,
  // `capacity` -> `pools` -> `capacity`. The middle spelling had a life of its
  // own in saved links, so it resolves too; what it must never do again is
  // appear in a link this app writes.
  pools: CAPACITY,
  activity: 'history',
}

/**
 * NO INTERNAL LINK MAY USE ONE OF THESE. An alias is for a hash someone else
 * saved -- a runbook, an incident note, a message from 3am -- and it exists so
 * that link still lands. It is not a second name this app may write.
 *
 * The distinction is not decorative. An alias that internal links also use is
 * a spelling nothing can ever retire: every href keeps working, so nothing
 * fails, so nobody updates one, and the old name outlives the rename by years.
 * That is the defect that put `Agents > Agents` in the rail in the first place.
 *
 * `__tests__/nav.links.test.tsx` reads every `#`-href literal in
 * `apps/swarm-ui/src` and fails if one starts with a key of this map, or names
 * a section or tab that does not exist.
 */
export const INTERNAL_LINKS_MAY_NOT_USE_ALIASES = Object.keys(SECTION_ALIASES)

/** `#settings/<tail>` from the two-pane Settings screen. */
const LEGACY_SETTINGS: Record<string, { section: string; tab: string }> = {
  limits: { section: 'admin', tab: 'limits' },
  accounts: { section: CAPACITY, tab: 'accounts' },
}

/** Which pane of one agent is open. */
type TaskPane = 'detail' | 'attempts'

export interface Route {
  /** A section id, or REFERENCE. */
  sectionId: string
  /** Meaningless when sectionId is REFERENCE; carried anyway so Route is flat. */
  tab: string
  /** Set when an agent drawer is open over the Agents section. */
  taskId: string | null
  taskPane: TaskPane
}

function sectionOf(id: string): SectionDef | null {
  return SECTIONS.find((s) => s.id === id) ?? null
}

function firstTab(s: SectionDef): string {
  // `noUncheckedIndexedAccess` is on, and a section with no tabs would be a
  // programming error rather than a state to render — so it falls back to a
  // string that matches no tab, and SectionBody's default branch says so.
  return s.tabs[0]?.id ?? ''
}

/**
 * The hash, resolved to a route.
 *
 * EXPORTED FOR THE ROUTE TESTS. `#help/<topic>` is a new destination and the
 * links to it are generated, so a typo in this function would produce a `?`
 * card whose "Full explanation" link lands on Home -- silently, and only for
 * the reader who followed it. `tests/route.test.ts` drives it directly.
 */
export function fromHash(): Route {
  const hash = window.location.hash.replace(/^#/, '')
  const seg = hash.split('/')
  const raw = seg[0] ?? ''
  // ALIASED FIRST, ONCE, so that every check below sees one spelling. The
  // drawer branch used to test `head === 'agents'` on the unresolved head; the
  // moment the section was renamed that test would have been the only place
  // still answering to the old name, and `#work/task/<id>` -- the address in
  // every workflow node and every saved deep link -- would have resolved to
  // the section and dropped the task id, opening the list instead of the agent.
  const head = SECTION_ALIASES[raw] ?? raw
  const tail = seg.slice(1)
  const blank: Pick<Route, 'taskId' | 'taskPane'> = { taskId: null, taskPane: 'detail' }

  if (head === REFERENCE) return { sectionId: REFERENCE, tab: '', ...blank }

  // The tail is a topic id and is carried VERBATIM, including one this build
  // does not have: HelpScreen says which topic was asked for and lists what it
  // does carry. Silently rewriting an unknown topic to the top of the page
  // would turn a stale link into a page that looks right and answers nothing.
  if (head === HELP) return { sectionId: HELP, tab: tail.join('/'), ...blank }

  // THE AGENT DRAWER, in its current form and its old one. `work/task/<id>`
  // is explicit so that a task whose id happens to spell a tab name cannot be
  // mistaken for one; `work/<id>` is what the old nav wrote and is still
  // accepted, after the tab names have had their chance to match. `head` is
  // already aliased, so `#work/task/<id>` reaches here too.
  if (head === WORK && tail[0] === 'task' && tail.length > 1) {
    const rest = tail.slice(1)
    const attempts = rest[rest.length - 1] === 'attempts'
    // Task ids are opaque and may contain characters that were encoded on the
    // way in, so the remaining segments are rejoined rather than assumed to
    // be one.
    const id = (attempts ? rest.slice(0, -1) : rest).join('/')
    if (id) {
      return {
        sectionId: WORK,
        tab: 'running',
        taskId: decodeURIComponent(id),
        taskPane: attempts ? 'attempts' : 'detail',
      }
    }
  }

  if (head === 'settings') {
    const to = LEGACY_SETTINGS[tail.join('/')] ?? LEGACY['settings']!
    return { sectionId: to.section, tab: to.tab, ...blank }
  }

  const section = sectionOf(head)
  if (section) {
    const wanted = tail.join('/')
    const tab = section.tabs.find((t) => t.id === wanted)
    if (tab) return { sectionId: section.id, tab: tab.id, ...blank }
    // A Work tail that matches no tab is a task id from the old nav.
    if (section.id === WORK && wanted) {
      return { sectionId: WORK, tab: 'running', taskId: decodeURIComponent(wanted), taskPane: 'detail' }
    }
    // An unrecognised tail falls back to the section's first pane rather than
    // rendering nothing: a mistyped hash must not produce a blank screen.
    return { sectionId: section.id, tab: firstTab(section), ...blank }
  }

  const legacy = LEGACY[head]
  if (legacy) return { sectionId: legacy.section, tab: legacy.tab, ...blank }

  const home = SECTIONS[0]!
  return { sectionId: home.id, tab: firstTab(home), ...blank }
}

/** The one spelling of a route. What the address bar is rewritten to. */
export function canonical(r: Route): string {
  if (r.taskId !== null) {
    const base = `${WORK}/task/${encodeURIComponent(r.taskId)}`
    return r.taskPane === 'attempts' ? `${base}/attempts` : base
  }
  if (r.sectionId === REFERENCE) return REFERENCE
  if (r.sectionId === HELP) return r.tab === '' ? HELP : `${HELP}/${r.tab}`
  return `${r.sectionId}/${r.tab}`
}

export function App() {
  const [at, setAt] = useState<Route>(fromHash)

  // The hash IS the router. A real router earns its place when there are
  // nested layouts or loaders; today it would be a dependency that does
  // nothing forty lines cannot, and back/forward already work — including out
  // of the detail drawer, which is why the drawer is a route rather than
  // component state.
  useEffect(() => {
    const onHash = () => setAt(fromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // NORMALISE, WITHOUT ADDING HISTORY. An old hash resolves to a new route and
  // the address bar is rewritten in place, so copying the link afterwards
  // yields the current spelling. `replaceState` rather than assigning to
  // `location.hash`: the latter pushes an entry, and Back would then walk the
  // user through every alias they never typed.
  useEffect(() => {
    const want = canonical(at)
    if (window.location.hash.replace(/^#/, '') !== want) {
      window.history.replaceState(null, '', `#${want}`)
    }
  }, [at])

  const go = (to: string) => {
    window.location.hash = to
  }

  const section = sectionOf(at.sectionId)
  const inspector = at.taskId !== null

  return (
    // THE FRAME (§3.4, §11.3). Two rows: everything that scrolls, then the
    // dock. The dock USED to be `position: fixed` over a document that
    // scrolled under it, and an opaque bar over a scrolling document has
    // content behind it at some scroll offset -- always, by construction. Two
    // passes tried to buy that back with reservations (`.app`'s bottom
    // padding, then `scroll-padding-bottom`) and neither could work: bottom
    // padding only clears the END of the document and scroll-padding only
    // affects scrolls the browser performs. At rest, at scrollY=0, the bar
    // still painted over whatever happened to land under it -- which at
    // 390x844 was the row the owner screenshotted.
    //
    // Making it a ROW removes the class of bug instead of the instances:
    // there is no offset at which a grid row overlaps its sibling, so there is
    // nothing left to reserve and nothing left to tune.
    <div className="ctl-frame">
      {/* THE SCROLLER. The header travels with the content on purpose -- the
          rail's `position: sticky; top: 0` pins it once the header has
          scrolled away, and that behaviour is unchanged because the header is
          still inside the same scrolling box the rail is. */}
      <div className="ctl-scroll">
        {/* OUTSIDE `.app`, DELIBERATELY. The header's environment bar is 3px
            tall and spans the full viewport width -- that full width is what
            makes it readable in peripheral vision and in a scaled-down
            screenshot, and an element inside `.app` stops at the content
            gutter. The bar's own padding uses the same `--app-pad` token
            `.app` does, so the wordmark still lines up with the nav below it
            at every breakpoint. */}
        <ProductHeader />
        <div className={`app${inspector ? ' has-inspector' : ''}`}>
          <Rail at={at} go={go} />

          {/* THE WORK AREA (§B3). A grid column, `min-width: 0`, no max-width.
              Its own head sits inside it rather than above the rail, because
              the head names the PAGE and the rail names the product. */}
          <main className="work">
            <Head at={at} section={section} />

            {section === null ? (
              at.sectionId === HELP ? (
                <HelpScreen topic={at.tab} />
              ) : (
                <ReferenceScreen />
              )
            ) : (
              <SectionBody sectionId={section.id} tab={at.tab} go={go} />
            )}
          </main>

          {at.taskId !== null && <AgentDrawer taskId={at.taskId} pane={at.taskPane} go={go} />}
        </div>
      </div>

      {/* THE DOCK (§B3), A SIBLING OF THE SCROLLER RATHER THAN A LAYER OVER
          IT. It is still outside `.app`, still spans the viewport, and still
          survives navigation within the session -- it is rendered here, above
          the routed body, exactly as before. What changed is that it now
          OCCUPIES its height instead of borrowing it. */}
      <Dock />
    </div>
  )
}

/**
 * THE RAIL (§B3): 200px fixed, down the left, and it never reorders.
 *
 * WHY A RAIL AND NOT THE TWO HORIZONTAL STRIPS IT REPLACES. The console had a
 * section bar and a tab strip stacked above every screen, and between them
 * they spent about 64px of vertical on every one of fifteen routes -- on the
 * 900px-tall laptops these screens are actually read on, that is 7% of the
 * glass, permanently, to display navigation that does not change. Horizontal
 * is also the axis this product has least of: the work area wants every pixel
 * of width for tables and for the workflow graph, and has vertical to spare.
 *
 * IT NEVER REORDERS, which is the property the whole thing is for. After a
 * week you go to a POSITION rather than reading a label, and that only works
 * if the position is a constant -- so every section's tabs are drawn at all
 * times, indented, rather than appearing when their section is opened. A
 * second-level list that materialises under the cursor is a list whose
 * geometry you have to re-read on every visit.
 *
 * THE UTILITY CORNER stays in the rail, at the bottom, and keeps its
 * `ctl-nav-util` class: the API reads page and Help are things you look up
 * once, not things you work in, and putting them among the six sections is the
 * mistake the eleven-item nav made eleven times over.
 * `tests/unit/control_plane/test_nav_headings_agree.py` reads that class name
 * to find the utility button, so it is load-bearing rather than decorative.
 *
 * BELOW 900px IT IS A TOP BAR AGAIN. Two hundred pixels of a 390pt phone is
 * half the screen. The markup does not change -- the CSS turns the column into
 * a scrolling row and hides every unopened section's tabs, which is exactly
 * the eleven-item horizontal strip this replaced, and is the right shape at
 * that width.
 *
 * WHAT WAS NOT BUILT, AND WHY IT IS NOT A 56px ICON RAIL BELOW 1280px. §B3
 * asks for icon-only at 56px. This product has no icon set, and a single
 * letter is not an icon: Agents and Admin both begin with A, so a
 * letter-per-section rail would put two identical marks in a list whose whole
 * value is that a position means one thing. It narrows to 152px instead, which
 * still fits "Runner profiles" at 12px, and the reason is here rather than in
 * a commit message.
 */
function Rail({ at, go }: { at: Route; go: (to: string) => void }) {
  return (
    <nav className="ctl-rail" aria-label="Sections">
      <div className="ctl-rail-sections">
        {SECTIONS.map((s) => {
          const on = at.sectionId === s.id
          return (
            <div key={s.id} className={`ctl-rail-group${on ? ' is-on' : ''}`}>
              <button
                className={`ctl-nav-link${on ? ' is-on' : ''}`}
                aria-current={on ? 'page' : undefined}
                title={s.question}
                onClick={() => go(`${s.id}/${firstTab(s)}`)}
              >
                {s.label}
              </button>
              {/* A single-pane section draws no second level: one tab under
                  one section is a duplicate of the section. */}
              {s.tabs.length > 1 && (
                <div
                  className="ctl-rail-tabs"
                  role="tablist"
                  aria-label={`${s.label} views`}
                >
                  {s.tabs.map((t) => (
                    <button
                      key={t.id}
                      role="tab"
                      aria-selected={on && at.tab === t.id}
                      onClick={() => go(`${s.id}/${t.id}`)}
                    >
                      {t.label}
                      {t.admin && <span className="ctl-subnav-admin">admin</span>}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="ctl-nav-util">
        <button
          className={at.sectionId === REFERENCE ? 'is-on' : ''}
          aria-current={at.sectionId === REFERENCE ? 'page' : undefined}
          onClick={() => go(REFERENCE)}
        >
          {REFERENCE_LABEL}
        </button>
        {/* THE HEAD `?` (§B7.3). The way into Help from anywhere, for the
            reader who has not got a `?` in front of them. A glyph and an
            accessible name, not the word "Help" -- the rail is the product's
            six questions and a seventh word beside them reads as a seventh. */}
        <button
          className={at.sectionId === HELP ? 'is-on' : ''}
          aria-current={at.sectionId === HELP ? 'page' : undefined}
          aria-label="Help"
          title="Help"
          onClick={() => go(HELP)}
        >
          ?
        </button>
      </div>
    </nav>
  )
}

/**
 * THE HEAD (§B3): 44px, spanning the work area, above everything routed.
 *
 * Three things, and the third is the one that moved.
 *
 * 1. THE BREADCRUMB -- section, pane, and the object if one is open. It is the
 *    only place the open agent's id appears outside the inspector itself, and
 *    it is what makes "back" legible: you can see what you would go back to.
 *
 * 2. THE READ AGE. The age of the newest SUCCESSFUL payload of any route this
 *    tab has called, taken from the probe registry the dock summarises. It is
 *    a measurement, never a promise: "nothing has loaded" is a different
 *    sentence from "0s ago" and this renders the first one when it is true.
 *
 *    THERE IS NO REFRESH BUTTON HERE, deliberately, although §B3 asks for one.
 *    Every screen owns its own reads -- `Screen` in Shell.tsx holds the result
 *    and the retry -- and a control in the frame would have to claim it
 *    refreshed something it does not own. The honest version of that control
 *    needs a refresh seam the screens do not expose yet; a button that reloaded
 *    the page and called it a refresh would be worse than its absence, because
 *    it would look like it worked.
 *
 * 3. THE SECTION'S `?` (§B2). The question each section answers used to be
 *    printed under the tab strip on EVERY pane of that section -- five times
 *    for Pools, in a sentence nobody re-reads after the first visit, costing a
 *    line of vertical on every screen. §B2 moves it: printed once, behind the
 *    section's own `?`, where it is still the membership test for what belongs
 *    in the section and is still one keystroke from any pane. The question
 *    itself is unchanged and still lives in `SECTIONS`.
 */
function Head({ at, section }: { at: Route; section: SectionDef | null }) {
  const probes = useSyncExternalStore(subscribeProbes, probeSnapshot, probeSnapshot)
  const [, tick] = useState(0)

  // The age is the point, so it moves on its own rather than only when a
  // fetch happens to land.
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 5000)
    return () => clearInterval(id)
  }, [])

  const newest = summariseProbes(probes).newestSuccessAt
  const tab = section?.tabs.find((t) => t.id === at.tab) ?? null
  const head = section?.label ?? (at.sectionId === HELP ? 'Help' : REFERENCE_LABEL)

  return (
    <div className="ctl-head">
      <p className="ctl-crumb">
        <span className="ctl-crumb-at">{head}</span>
        {tab !== null && section !== null && section.tabs.length > 1 && (
          <>
            <span className="ctl-crumb-sep" aria-hidden>
              &#9656;
            </span>
            <span className="ctl-crumb-at">{tab.label}</span>
          </>
        )}
        {at.taskId !== null && (
          <>
            <span className="ctl-crumb-sep" aria-hidden>
              &#9656;
            </span>
            <span className="id ctl-crumb-obj">{at.taskId}</span>
          </>
        )}
      </p>

      <span className="ctl-head-age">
        {newest === null ? (
          <span className="ctl-em">nothing has loaded in this tab</span>
        ) : (
          <>newest read {timeAgo(newest)}</>
        )}
      </span>

      {section !== null && <SectionQuestion section={section} />}
    </div>
  )
}

/**
 * The section's question, behind the `?` (§B2, §B7.2).
 *
 * It reuses `useHelpDisclosure` rather than re-implementing the behaviour,
 * because the behaviour is the part that is easy to get wrong and is already
 * tested: hover opens after 120ms, FOCUS opens immediately so the keyboard
 * reaches it, click pins it so it survives the pointer leaving and can be read
 * in a screenshot, and Escape or an outside click dismisses. A hover-only
 * explanation is invisible on a touch device and invisible in the screenshot
 * someone pastes into an incident channel.
 *
 * It is not a `<HelpCard>`: those render a topic out of `help.ts`, and a
 * section's question is data on the section, not a help topic. The card's box
 * is drawn by `.ctl-q-card` rather than inline so the question inherits the
 * same surface as everything else in the frame.
 */
/**
 * THE SECOND HELP CARD IN THIS APP, and it now shares the first one's placement.
 *
 * This rendered `.ctl-q-card` and positioned it with CSS alone, so it missed
 * every fix made to `HelpCard`: measured 2026-09-24 at 390px, all four of these
 * opened 299px past the right edge. The rail becomes a horizontal SCROLLER
 * below 900px, so a glyph scrolled off to the right reports an anchor outside
 * the viewport and a card anchored to it lands outside too.
 *
 * `useEdgeSafePlacement` is imported rather than reimplemented. Two
 * implementations of one widget is exactly how the first one's fixes stopped
 * reaching the second, and a third would do it again.
 */
function SectionQuestion({ section }: { section: SectionDef }) {
  const { state, trigger, hover } = useHelpDisclosure()
  const cardId = `q-${section.id}`
  const [anchorRef, placement] = useEdgeSafePlacement(state.open)

  const card = (
    <span
      id={cardId}
      role={state.pinned ? 'dialog' : 'tooltip'}
      className="ctl-q-card"
      style={placement}
    >
      <strong className="ctl-q-title">{section.label} answers</strong>
      <span className="ctl-q-body">{section.question}</span>
    </span>
  )

  return (
    <span className="ctl-q" {...hover} ref={anchorRef}>
      <button
        type="button"
        className="ctl-q-glyph"
        aria-label={`What the ${section.label} section answers`}
        aria-expanded={state.open}
        aria-controls={state.open ? cardId : undefined}
        {...trigger}
      >
        ?
      </button>
      {state.open &&
        (typeof document === 'undefined' ? card : createPortal(card, document.body))}
    </span>
  )
}

/**
 * Route to screen. Nothing else lives here: every screen below owns its own
 * reads, its own empty state and its own failure state, and this file must not
 * acquire a second opinion about any of them.
 */
function SectionBody({
  sectionId,
  tab,
  go,
}: {
  sectionId: string
  tab: string
  go: (to: string) => void
}) {
  const openAgent = (id: string) => go(`${WORK}/task/${encodeURIComponent(id)}`)

  // EVERY CASE IS A STRING LITERAL, and that is required rather than casual.
  //
  // WHAT HAPPENED. When `agents` became `work` and `pools` became `capacity`,
  // nine literal cases here stopped matching and nine screens rendered "No
  // such pane" -- through a `default` branch written to explain a hand-typed
  // address, so the break looked like a considered answer. Nothing was red in
  // the browser: the routes resolved, the rail drew, the screens were simply
  // gone.
  //
  // THE FIRST FIX WAS WRONG. These were rewritten as `` case `${WORK}/running` ``
  // so a rename could not separate them from the section declaration. That
  // broke the thing that actually catches this:
  // `tests/unit/control_plane/test_nav_headings_agree.py` reads this switch out
  // of the source with a regex -- it is a Python test and cannot import
  // TypeScript -- and asserts in BOTH directions that every tab has a case and
  // every case has a tab. A template literal is opaque to it, so the test
  // stopped COLLECTING, which took the whole unit-test job down and told us
  // nothing about the nine screens.
  //
  // With literals it says exactly the right thing:
  //
  //     tab 'Agents' points at work/running, which SectionBody has no case for:
  //     clicking it renders the 'No such pane' panel
  //
  // That is a better guarantee than matching constants, because it checks the
  // whole mapping rather than one spelling of one half of it. The chain that
  // holds it together: `nav.links.test.tsx` binds WORK/CAPACITY to SECTIONS,
  // SECTIONS declares its ids as literals, and the Python gate binds SECTIONS
  // to these cases. Nothing in it can move alone.
  switch (`${sectionId}/${tab}`) {
    case 'overview/now':
      return <OverviewScreen />

    case 'work/running':
      return <AgentsScreen onOpen={openAgent} />
    case 'work/workflows':
      return <WorkflowsScreen />
    case 'work/new':
      return <SubmitScreen />
    case 'work/new-workflow':
      return <SubmitWorkflowScreen />

    case 'runtimes/catalogue':
      return <RuntimesScreen />

    case 'capacity/pools':
      return <CapacityScreen />
    case 'capacity/profiles':
      return <ProfilesScreen />
    case 'capacity/holders':
      return <HoldersScreen />
    case 'capacity/accounts':
      return <AccountsScreen />
    case 'capacity/quota':
      return <QuotaDetailScreen />

    case 'history/timeline':
      return <ActivityScreen />
    case 'history/counts':
      return <PlatformCountsScreen />

    case 'admin/limits':
      return <AdminSettingsScreen />
    case 'admin/tenants':
      return <TenantsScreen />

    default:
      // Unreachable through the nav, and reachable only by hand-editing a hash
      // into a pane that does not exist. It says which pane rather than
      // rendering an empty page, because a blank screen here is
      // indistinguishable from a screen whose read returned nothing — the one
      // confusion this whole UI is built to avoid.
      return (
        <div className="ctl-empty">
          {/* NO `.ctl-mark` HERE, deliberately. The mark's six words are the
              six kinds of missing MEASUREMENT, and a mistyped address is not
              one of them -- inventing a seventh is how the vocabulary stops
              being a vocabulary. What this needs is the heading and the
              address it was given, which is below. */}
          <h3>No such pane</h3>
          <p>Nothing was read and nothing failed.</p>
          <span className="ctl-empty-foot">
            requested: {sectionId}/{tab || '(none)'}
          </span>
        </div>
      )
  }
}

/**
 * One agent, in two panes.
 *
 * WHY THE TAB STRIP IS HERE. `AttemptTimelineScreen` is a finished screen —
 * every attempt with its generation, backend, exit code, peak RSS, checkpoint
 * count and the events grouped under the attempt that wrote them — that no
 * route has ever pointed at. It is the only place per-attempt history is
 * legible, so it needed a way in, and this is the one that does not edit
 * AgentDetail.tsx.
 *
 * `AgentDetailScreen` draws its own `.drawer` and its own close button. Both
 * are flattened by two rules scoped to `.ctl-drawer` in styles.css rather than
 * by changing that file. If it ever stops drawing its own drawer, those rules
 * become no-ops rather than breakage.
 *
 * IT IS A GRID COLUMN, NOT AN OVERLAY, WHEREVER TWO PANES FIT (§B3). The
 * defect it fixes is "reading one agent takes the others off the screen": the
 * drawer was `position: fixed` at every width, so on a 1600px display it drew
 * a 560px panel over a list that had 1000px of room beside it. At 1600px the
 * work area now reflows to 1600 - 200 - 480 - gutters and the list stays
 * legible. Below 1100px total width two panes genuinely do not fit and it
 * reverts to the overlay, `role="dialog"` and all -- which is why that role is
 * on the element unconditionally rather than switched with the layout.
 *
 * THE WIDTH IS THE VIEWER'S, 400-720px, remembered per viewer through
 * `panes.ts` -- whose reads and writes are inside `try/catch`, because
 * `localStorage` THROWS in a private window and losing the shell to a
 * preference is not a trade anyone would make. It is published as a custom
 * property on the documentElement rather than as an inline width, because the
 * element that has to react to it is `.app`'s grid template, which is this
 * element's parent.
 */
function AgentDrawer({
  taskId,
  pane,
  go,
}: {
  taskId: string
  pane: TaskPane
  go: (to: string) => void
}) {
  const base = `agents/task/${encodeURIComponent(taskId)}`
  const close = () => go('agents/running')
  const [width, setWidth] = useState(() => readPane(INSPECTOR))
  const dragging = useRef(false)

  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--inspector-w', `${width}px`)
    return () => {
      root.style.removeProperty('--inspector-w')
    }
  }, [width])

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return
    // The inspector is anchored to the right edge, so its width is the
    // distance from the pointer to that edge.
    setWidth(clampPane((globalThis.innerWidth || 0) - e.clientX, INSPECTOR.min, INSPECTOR.max))
  }
  const endDrag = () => {
    if (!dragging.current) return
    dragging.current = false
    writePane(INSPECTOR, width)
  }

  return (
    <div className="drawer ctl-drawer" role="dialog" aria-label={`Agent ${taskId}`}>
      <div
        className="ctl-inspector-grip"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the inspector"
        onPointerDown={(e) => {
          dragging.current = true
          e.currentTarget.setPointerCapture(e.pointerId)
        }}
        onPointerMove={onMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      />
      <button className="drawer-close" onClick={close} aria-label="Close">
        ✕
      </button>
      <div className="ctl-subnav" role="tablist" aria-label="Agent panes">
        <button role="tab" aria-selected={pane === 'detail'} onClick={() => go(base)}>
          Detail
        </button>
        <button
          role="tab"
          aria-selected={pane === 'attempts'}
          onClick={() => go(`${base}/attempts`)}
        >
          Attempts
        </button>
      </div>
      {pane === 'detail' ? (
        <AgentDetailScreen taskId={taskId} onClose={close} />
      ) : (
        <AttemptTimelineScreen taskId={taskId} />
      )}
    </div>
  )
}

/**
 * The reads this tab has made — the one page the brief reserved for the API.
 *
 * IT DOES NOT CLAIM TO BE THE API. It renders the probe registry: the routes
 * this UI has called since the page loaded, with what happened to each. That
 * is a fact the app already collects for the footer strip. A hand-written
 * table of "the endpoints SwarmCloud offers" would be a restatement of the
 * server's contract kept in a TypeScript file nothing checks, and every
 * restatement in this repository has since drifted — so the page says what it
 * is measuring, in the lead, rather than implying completeness it lacks.
 *
 * It fetches nothing of its own. Opening it first therefore shows an empty
 * registry, and the empty state says exactly that rather than "no routes".
 */
function ReferenceScreen() {
  const probes = useSyncExternalStore(subscribeProbes, probeSnapshot, probeSnapshot)
  const [, tick] = useState(0)

  // The ages are the point, so they move on their own rather than only when a
  // fetch happens to land.
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 5000)
    return () => clearInterval(id)
  }, [])

  return (
    <>
      {/* THE LEAD PARAGRAPH IS THE COLUMN HEADING NOW. 48 words said one thing:
          this is what THIS TAB called, not what the API offers. That caveat
          belongs to the thing it qualifies -- the page's own title -- so it is
          a `.ctl-card-note` beside it, in the slot §8.4.2 reserves for exactly
          this, and the argument is one click away in `#help/api-reads`. */}
      <div className="ctl-page-head">
        <h1>{REFERENCE_LABEL}</h1>
        <span className="ctl-card-note">this tab only · not the API surface</span>
        <a className="is-end" href={`#${HELP}/api-reads`}>
          What these mean &rarr;
        </a>
      </div>

      {probes.length === 0 ? (
        // A REAL ZERO, and the one screen in the product where that is true by
        // construction: this page issues no reads of its own. `.is-partial`
        // and `.is-failed` would both be claims; the default variant is the
        // one that means "we looked and there is nothing".
        <div className="ctl-empty">
          <h3>
            <i className="ctl-mark is-zero">real zero</i> Nothing has been read
            yet in this tab
          </h3>
          <p>Nothing failed. Open any section and each read registers here.</p>
        </div>
      ) : (
        <section className="section">
          <h2>Routes called in this tab</h2>
          <div className="ctl-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">Route</th>
                  <th scope="col">Last attempt</th>
                  {/* THE PARENTHETICAL CARRIES THE CAVEAT (§8.4.3). The
                      caption used to spend 24 words saying a 403 on an admin
                      route is the expected answer for a non-admin; the column
                      it is about says so instead, and `describeProbe` already
                      draws that row in `--info` rather than in `--bad`. */}
                  <th scope="col">Outcome (403 on /v1/admin is expected)</th>
                  <th scope="col" className="is-num">Took</th>
                  <th scope="col">Newest payload</th>
                </tr>
              </thead>
              <tbody>
                {probes.map((p) => (
                  <RouteRow key={p.path} probe={p} />
                ))}
              </tbody>
              <caption>
                {probes.length} route{probes.length === 1 ? '' : 's'} since this
                tab loaded
              </caption>
            </table>
          </div>
        </section>
      )}
    </>
  )
}

function RouteRow({ probe }: { probe: ProbeRecord }) {
  const outcome = describeProbe(probe)
  return (
    <tr className={outcome.row}>
      <th scope="row" className="ctl-ref-path">
        {probe.path}
      </th>
      <td>{timeAgo(probe.lastAttemptAt)}</td>
      <td>
        <span className={`ctl-chip ${outcome.tone}`}>
          <i aria-hidden />
          {outcome.label}
        </span>
      </td>
      <td className="is-num ctl-ref-ms">{probe.lastLatencyMs}ms</td>
      <td>
        {/* THE COLUMN THAT MATTERS. A panel showing a figure from four minutes
            ago while its route has been failing for three of them looks
            healthy unless this says otherwise. "never" is a fact about this
            tab, and is not written as a time. */}
        {probe.lastSuccessAt === null ? (
          <span className="ctl-em">never in this tab</span>
        ) : (
          timeAgo(probe.lastSuccessAt)
        )}
      </td>
    </tr>
  )
}

/**
 * The outcome of one read, by CAUSE.
 *
 * The raw `kind` is printed for anything this does not name, deliberately:
 * collapsing several causes into one word is a mistake this codebase has made
 * before, and an unfamiliar kind spelled out is more useful than a tidy
 * "failed" that hides which one it was.
 */
function describeProbe(p: ProbeRecord): { tone: string; label: string; row?: string } {
  if (p.lastKind === null) return { tone: 'is-ok', label: `${p.lastStatus ?? 200} ok` }
  switch (p.lastKind) {
    // Not a failure. A non-admin genuinely cannot read /v1/admin/*.
    case 'admin_required':
      return { tone: 'is-info', label: '403 admin only' }
    case 'unauthenticated':
    case 'session_expired':
      return { tone: 'is-bad', label: 'session expired', row: 'is-bad' }
    case 'rate_limited':
      return { tone: 'is-warn', label: '429 paused', row: 'is-warn' }
    // Same status as the next one, a different thing to do about it.
    case 'tenant_unresolved':
      return { tone: 'is-warn', label: '503 tenant unresolved', row: 'is-warn' }
    case 'upstream_degraded':
      return { tone: 'is-warn', label: '503 degraded', row: 'is-warn' }
    case 'unreachable':
      return { tone: 'is-bad', label: 'unreachable', row: 'is-bad' }
    default:
      return { tone: 'is-bad', label: `${p.lastStatus ?? '—'} ${p.lastKind}`, row: 'is-bad' }
  }
}
