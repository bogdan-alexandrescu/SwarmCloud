import { useEffect, useState, useSyncExternalStore } from 'react'
import { AccountsScreen } from './Accounts'
import { ActivityScreen, TenantsScreen } from './Activity'
import { AdminSettingsScreen } from './AdminSettings'
import { AgentDetailScreen } from './AgentDetail'
import { AgentsScreen } from './Agents'
import { AttemptTimelineScreen } from './AttemptTimeline'
import { CapacityScreen } from './Capacity'
import { DataSourceStrip } from './DataSources'
import { probeSnapshot, subscribeProbes, type ProbeRecord } from './fetch'
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
 * already happened. The QUESTION each section answers is still here, printed
 * under the tabs, where it is a test for what belongs in the section rather
 * than a name anyone has to memorise.
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
   * Printed under the tabs. Not the name — the test for whether a screen
   * belongs in this section, kept in the file the sections are declared in so
   * it is read by whoever adds the next one.
   */
  question: string
  tabs: TabDef[]
}

const SECTIONS: SectionDef[] = [
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
    id: 'agents',
    label: 'Agents',
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
    // `capacity` was the old id and still resolves; see SECTION_ALIASES.
    id: 'pools',
    label: 'Pools',
    question:
      'Is there room to run more, which ceiling is the binding one, and what is holding what there is?',
    tabs: [
      { id: 'pools', label: 'Pools' },
      { id: 'profiles', label: 'Runner profiles' },
      // "Capacity holders" rather than "Holders": the screen answers "what is
      // holding capacity, and do the two records of that agree", and "Holders"
      // alone does not say holders of WHAT -- which, sitting one tab away from
      // "Accounts", is the reading that makes someone open it looking for
      // people. The longer heading was the better name and it won.
      { id: 'holders', label: 'Capacity holders' },
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
 * Every hash the eleven-item nav produced, still resolving.
 *
 * Not a courtesy. These hashes are in runbooks, in incident notes and in the
 * links people paste to each other at 3am, and a redesign that silently
 * redirects them all to Home is a redesign that loses the one link someone
 * needed. Each lands on the pane that answers what the old screen answered,
 * and the address bar is rewritten to the new form so the next copy of the
 * link is the current one.
 *
 * `capacity` and `activity` are NOT here although they were old top-level
 * hashes: they are section ids that were renamed, and SECTION_ALIASES resolves
 * them first, with their tail intact. An entry here as well would be dead and
 * would read as the place those two are handled.
 */
const LEGACY: Record<string, { section: string; tab: string }> = {
  home: { section: 'overview', tab: 'now' },
  // The Trouble board has no route any more: there is no problem section at
  // any level. The hash still resolves, and it resolves to the screen that
  // carries the derived checks it used to hold.
  trouble: { section: 'overview', tab: 'now' },
  holders: { section: 'pools', tab: 'holders' },
  quota: { section: 'pools', tab: 'quota' },
  agents: { section: 'agents', tab: 'running' },
  workflows: { section: 'agents', tab: 'workflows' },
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
  capacity: 'pools',
  activity: 'history',
}

/** `#settings/<tail>` from the two-pane Settings screen. */
const LEGACY_SETTINGS: Record<string, { section: string; tab: string }> = {
  limits: { section: 'admin', tab: 'limits' },
  accounts: { section: 'pools', tab: 'accounts' },
}

/** Which pane of one agent is open. */
type TaskPane = 'detail' | 'attempts'

interface Route {
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

function fromHash(): Route {
  const hash = window.location.hash.replace(/^#/, '')
  const seg = hash.split('/')
  const head = seg[0] ?? ''
  const tail = seg.slice(1)
  const blank: Pick<Route, 'taskId' | 'taskPane'> = { taskId: null, taskPane: 'detail' }

  if (head === REFERENCE) return { sectionId: REFERENCE, tab: '', ...blank }

  // THE AGENT DRAWER, in its current form and its old one. `agents/task/<id>`
  // is explicit so that a task whose id happens to spell a tab name cannot be
  // mistaken for one; `agents/<id>` is what the old nav wrote and is still
  // accepted, after the tab names have had their chance to match.
  if (head === 'agents' && tail[0] === 'task' && tail.length > 1) {
    const rest = tail.slice(1)
    const attempts = rest[rest.length - 1] === 'attempts'
    // Task ids are opaque and may contain characters that were encoded on the
    // way in, so the remaining segments are rejoined rather than assumed to
    // be one.
    const id = (attempts ? rest.slice(0, -1) : rest).join('/')
    if (id) {
      return {
        sectionId: 'agents',
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

  const section = sectionOf(SECTION_ALIASES[head] ?? head)
  if (section) {
    const wanted = tail.join('/')
    const tab = section.tabs.find((t) => t.id === wanted)
    if (tab) return { sectionId: section.id, tab: tab.id, ...blank }
    // An agents tail that matches no tab is a task id from the old nav.
    if (section.id === 'agents' && wanted) {
      return { sectionId: 'agents', tab: 'running', taskId: decodeURIComponent(wanted), taskPane: 'detail' }
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
function canonical(r: Route): string {
  if (r.taskId !== null) {
    const base = `agents/task/${encodeURIComponent(r.taskId)}`
    return r.taskPane === 'attempts' ? `${base}/attempts` : base
  }
  if (r.sectionId === REFERENCE) return REFERENCE
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

  return (
    <div className="app">
      <Nav at={at} go={go} />

      {section === null ? (
        <ReferenceScreen />
      ) : (
        <>
          <SubNav section={section} tab={at.tab} go={go} />
          <p className="ctl-section-q">{section.question}</p>
          <SectionBody sectionId={section.id} tab={at.tab} go={go} />
        </>
      )}

      {at.taskId !== null && <AgentDrawer taskId={at.taskId} pane={at.taskPane} go={go} />}

      <DataSourceStrip />
    </div>
  )
}

/**
 * The section bar. A `<nav>` of links, NOT a tablist: these change what the
 * page is about and push history, which is navigation. The tab roles belong on
 * the strip below, where they are true. Getting this backwards is how a screen
 * reader ends up announcing "tab 3 of 11" for a whole product.
 */
function Nav({ at, go }: { at: Route; go: (to: string) => void }) {
  return (
    <nav className="ctl-nav" aria-label="Sections">
      <div className="ctl-nav-sections">
        {SECTIONS.map((s) => {
          const on = at.sectionId === s.id
          return (
            <button
              key={s.id}
              className={`ctl-nav-link${on ? ' is-on' : ''}`}
              aria-current={on ? 'page' : undefined}
              title={s.question}
              onClick={() => go(`${s.id}/${firstTab(s)}`)}
            >
              {s.label}
            </button>
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
      </div>
    </nav>
  )
}

/**
 * The section's panes. `role="tablist"` / `role="tab"` / `aria-selected` is the
 * convention Agents.tsx already uses; reusing the styling without it would
 * leave the selected pane expressed only as a CSS class.
 */
function SubNav({
  section,
  tab,
  go,
}: {
  section: SectionDef
  tab: string
  go: (to: string) => void
}) {
  if (section.tabs.length < 2) return null
  return (
    <div className="ctl-subnav" role="tablist" aria-label={`${section.label} views`}>
      {section.tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={tab === t.id}
          onClick={() => go(`${section.id}/${t.id}`)}
        >
          {t.label}
          {t.admin && <span className="ctl-subnav-admin">admin</span>}
        </button>
      ))}
    </div>
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
  const openAgent = (id: string) => go(`agents/task/${encodeURIComponent(id)}`)

  switch (`${sectionId}/${tab}`) {
    case 'overview/now':
      return <OverviewScreen />

    case 'agents/running':
      return <AgentsScreen onOpen={openAgent} />
    case 'agents/workflows':
      return <WorkflowsScreen />
    case 'agents/new':
      return <SubmitScreen />
    case 'agents/new-workflow':
      return <SubmitWorkflowScreen />

    case 'runtimes/catalogue':
      return <RuntimesScreen />

    case 'pools/pools':
      return <CapacityScreen />
    case 'pools/profiles':
      return <ProfilesScreen />
    case 'pools/holders':
      return <HoldersScreen />
    case 'pools/accounts':
      return <AccountsScreen />
    case 'pools/quota':
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
          <h3>No such pane</h3>
          <p>
            This address names a pane of <strong>{sectionId}</strong> that does
            not exist. Nothing was read and nothing failed — pick a pane above.
          </p>
          <span className="ctl-empty-foot">requested: {sectionId}/{tab || '(none)'}</span>
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
 * by changing that file, which belongs to another track. If it ever stops
 * drawing its own drawer, those rules become no-ops rather than breakage.
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

  return (
    <div className="drawer ctl-drawer" role="dialog" aria-label={`Agent ${taskId}`}>
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
      <div className="head">
        <h1>{REFERENCE_LABEL}</h1>
        
      </div>
      <p className="sub">What this UI reads, and how those reads are going.</p>

      <p className="ctl-ref-lead">
        This is <strong>not</strong> a list of the endpoints SwarmCloud offers.
        It is every route this browser tab has called since it loaded, taken
        from the same registry that fills the strip at the foot of every
        screen. A route absent from it has not been called yet — which is not
        the same as a route that does not exist.
      </p>

      {probes.length === 0 ? (
        <div className="ctl-empty">
          <h3>Nothing has been read yet in this tab</h3>
          <p>
            This page issues no requests of its own, so it starts empty by
            design. Open any section and come back — each read registers here
            as it happens.
          </p>
          <p>
            Nothing failed, and this says nothing about whether the API is
            reachable.
          </p>
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
                  <th scope="col">Outcome</th>
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
                {probes.length} route{probes.length === 1 ? '' : 's'} called
                since this tab loaded · a 403 on an <code>/v1/admin</code> route
                is the expected answer for a non-admin, not a fault
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
