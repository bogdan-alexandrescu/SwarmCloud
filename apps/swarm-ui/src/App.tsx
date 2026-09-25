import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { createPortal } from 'react-dom'
import { agentListPath, parseAgentList, type AgentList } from './agentlist'
import { INSPECTOR, clampPane, readPane, writePane } from './panes'
import { AccountsScreen } from './Accounts'
import { ActivityScreen, TenantsScreen } from './Activity'
import { AdminSettingsScreen } from './AdminSettings'
import { AgentDetailScreen } from './AgentDetail'
import { AgentsScreen } from './Agents'
import { AttemptTimelineScreen } from './AttemptTimeline'
import { ProductHeader } from './Brand'
import { CapacityScreen } from './Capacity'
import { Dock } from './Dock'
import {
  beginScreenReads,
  probeSnapshot,
  screenReadsSnapshot,
  subscribeProbes,
  subscribeScreenReads,
  type ProbeRecord,
  type ScreenReads,
} from './fetch'
import { isOverlay, nudgePane, trapTab } from './focus'
import { useCardBridge, useHelpDisclosure, useEdgeSafePlacement } from './HelpCard'
import { HELP_ROUTE } from './help'
import { HelpScreen } from './HelpSection'
import { HoldersScreen } from './Holders'
import { OverviewScreen } from './Overview'
import { PlatformCountsScreen } from './PlatformCounts'
import { ProfilesScreen } from './Profiles'
import { QuotaDetailScreen } from './QuotaDetail'
import { RuntimesScreen } from './Runtimes'
import { RoutedPage, timeAgo } from './Shell'
import { SubmitScreen } from './Submit'
import { SubmitWorkflowScreen } from './SubmitWorkflow'
import { AGE_TICK_MS, useNow } from './useNow'
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
 * Variables) both name the noun and let the tabs be the views, and this still
 * does the same -- with three of them rather than six:
 *
 *   Overview · Work · Capacity · Admin
 *
 * THREE SECTIONS, AND OVERVIEW IS THE LANDING SCREEN RATHER THAN A FOURTH
 * QUESTION. It was six -- Overview, Work, Runtimes, Capacity, History, Admin
 * -- over the same fifteen screens. The measurement that ended that
 * arrangement is docs/web-ui/ux-plan.md §1.4: every screen carries 24-67
 * interactive elements and the rail alone was 21 of them on every single one,
 * and the fifteen screens were grouped by WHICH SUBSYSTEM OWNS THE DATA rather
 * than by what anyone wants to know. A reader arrives with five questions --
 * is anything broken right now, what is running, why is this one stuck, what
 * has it cost, can I start something -- and Overview answers the first while
 * the other four were spread across ten destinations.
 *
 * WHAT COLLAPSED IS THE SECTIONS, NOT THE SCREENS, and that distinction is the
 * whole of the answer to the standing objection in docs/web-ui/redesign.md
 * ("What was considered and rejected"), which refused a three-section nav
 * because it read the proposal as MERGING Activity into Work and Overview into
 * Capacity: "what is happening now" and "what happened over the last 500
 * tasks" are different reads, at different costs, with different failure
 * modes, and a merged screen would have to explain in prose which numbers were
 * which. That objection still stands, and nothing here contradicts it. All
 * fifteen screens keep their own route, their own read, their own empty state
 * and their own failure state. Timeline is a PANE of Work beside Agents, not a
 * panel inside the agent list; Overview is still its own screen on its own
 * route. The two sections that went away -- Runtimes and History -- held one
 * pane and two panes, and each was spending a permanent rail entry on that.
 *
 * The QUESTION each section answers is still here, and it is still the test
 * for what belongs in the section rather than a name anyone has to memorise --
 * but it is no longer PRINTED on every pane. It was rendered under the tab
 * strip on all fifteen routes, which is six copies of the same sentence for
 * Capacity alone, costing a line of vertical on every screen to say something
 * nobody re-reads after the first visit. §B2 moves it behind the head's `?`
 * (see `SectionQuestion`): written once, one keystroke from any pane, and
 * unchanged.
 *
 * THE TEST NOW HAS TO CARRY MORE, WHICH IS THE PRICE OF THREE. A membership
 * question that six panes pass is a weaker instrument than one that two panes
 * pass, so the failure to watch for here is a question quietly WIDENED to
 * admit a screen someone wanted to place -- not a seventh section. Runtimes
 * was made a section in the first place precisely because it failed all five
 * of the questions in the file at the time; it is a pane of Capacity now
 * because Capacity's question was rewritten, deliberately and once, to ask
 * what can run BEFORE it asks whether there is room for another -- which is
 * the order those two facts are actually read in. A widening needs that kind
 * of argument, and the argument belongs in docs/web-ui/redesign.md, not in a
 * commit message.
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
 * TWO OF THOSE TAB LABELS STILL DIVERGE FROM
 * docs/web-ui/ui-audit-and-build-prompt.md, which proposed the opposite trade
 * on two pairs -- shortening the heading to "Holders" rather than lengthening
 * the tab, and heading the Timeline pane "History", which was the name of a
 * SECTION and so would have left the tab and the heading still disagreeing.
 * The first has since landed for a different reason (Capacity now supplies the
 * noun the tab was carrying); the second has not and should not, and "History"
 * is not a section at all any more. That file is Track D and is not edited
 * from here; the divergence is deliberate and is reported rather than patched.
 * If the recommendation is reinstated, change both sides of the pair -- the
 * test below does not care which name wins, only that one does.
 *
 * (The same paragraph used to say redesign.md's pane table still listed "New
 * agent", "New workflow", "Holders" and "Reference". It does not: that table
 * was brought up to date on 2026-09-24 and the note outlived the divergence it
 * described. A citation that names a line number in another file is a citation
 * that goes stale silently, which is why this one now names the argument
 * rather than the lines.)
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
    // list, the timeline of what already ran, and the two screens that CREATE
    // one of each -- five screens whose common noun is the work itself, not
    // one of its shapes. Naming the parent after one of its children is what
    // produced the duplicate, and renaming the child would have been the wrong
    // half: `Work > Agents`, `Work > Workflows`, `Work > Timeline`,
    // `Work > Submit a task` each say something the section name does not,
    // which is the test a tab has to pass.
    label: 'Work',
    // WIDENED ONCE, TO TAKE THE TIMELINE, and the widening is the argument
    // rather than a side effect. "What did it produce" and "what did it
    // already do" are the same reader's next two questions and they were two
    // rail entries apart: Timeline was the first pane of a section called
    // History whose only other pane was an admin count. The clause "what has
    // already run" is what this section now has to answer, and the pane is
    // still its own route with its own read -- see the note at the top of this
    // file on collapsing SECTIONS rather than SCREENS.
    question:
      'What is running, what has already run, what did it produce — and why has mine not moved?',
    tabs: [
      { id: 'running', label: 'Agents' },
      { id: 'workflows', label: 'Workflows' },
      // TIMELINE, FROM THE SECTION THAT NO LONGER EXISTS. The id is
      // `timeline`, unchanged, which is what lets `#history/timeline` --
      // the address in every runbook that ever named this screen -- resolve
      // with its TAIL INTACT through SECTION_ALIASES rather than landing on
      // this section's first pane. See SECTION_ALIASES and MOVED_PANES below.
      { id: 'timeline', label: 'Timeline' },
      // "Submit a task", not "New agent", and the screen's own copy is why.
      // Submit.tsx creates a TASK at READY or PARKED and then says, in the
      // panel it renders on success, "That is not a running agent" -- because
      // invariant 1 is that neither state holds capacity. A tab promising an
      // agent and a page explaining you have not got one is the contradiction,
      // and the tab is the side that was wrong. `task` is also the noun the
      // API uses (`POST /v1/tasks`, `TaskCreate`), the same test that named
      // the Runtimes TAB after `/v1/runtimes` (it was a section of its own
      // until the collapse to three; the name came from the route either way).
      { id: 'new', label: 'Submit a task' },
      { id: 'new-workflow', label: 'Submit a workflow' },
    ],
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
    // word the tabs have in common: pools, runtimes, profile headroom,
    // holders, accounts and provider quota are six readings of the same thing
    // -- what can run, and how much of it. The id stays `capacity` for the
    // reason given on Work.
    label: 'Capacity',
    // WIDENED ONCE, TO TAKE THE RUNTIME CATALOGUE. The old question began at
    // "is there room to run more", which the Runtimes screen does not answer
    // -- and that is exactly why Runtimes was a section of its own: it failed
    // all five questions then in the file. The widening is the clause in
    // front: what CAN run is the question you answer before how many fit, and
    // a reader who does not know what a runner profile weighs cannot read a
    // headroom figure at all. This is the one widening the three-section
    // collapse is allowed; the note at the top of this file says why a second
    // one is the failure to watch for.
    question:
      'What kinds of agent can run here, is there room for another, which ceiling is the binding one, and what is holding what there is?',
    tabs: [
      { id: 'pools', label: 'Pools' },
      // RUNTIMES, WHICH WAS A SECTION UNTIL THE COLLAPSE. The id stays
      // `catalogue` so that `#runtimes/catalogue` -- the only address this
      // screen ever had -- resolves through SECTION_ALIASES with its tail
      // intact rather than landing on Pools.
      { id: 'catalogue', label: 'Runtimes' },
      // "Profile headroom", NOT "Runner profiles", AND THIS IS THE RENAME THAT
      // PAYS FOR PUTTING THE TWO IN ONE SECTION.
      //
      // The argument against ever doing this was written on the Runtimes
      // section entry that used to sit above: "Runtimes" and "Runner profiles"
      // are near-synonyms answering different questions, and two adjacent tabs
      // with near-synonymous labels is how someone reads PER-TENANT headroom as
      // a PLATFORM figure. That hazard is real and it is not answered by
      // putting a tab between them -- a reader scanning six labels does not
      // measure distance, they read words.
      //
      // So the words changed. `Runtimes.tsx` renders the runtime topology from
      // `GET /v1/runtimes`: what kinds of agent exist, which backend each one
      // resolves to, how big one is. Every figure on it is platform-wide and
      // cannot be otherwise -- the catalogue route reads no tenant document.
      // `Profiles.tsx` renders `capacity.runner_profiles`, whose pool lists are
      // built with `pool_names_for(tenant_id=ctx.tenant_id)` UNCONDITIONALLY,
      // including for an admin (service.py:313-329): every count on it answers
      // "how many more could I submit". One is a catalogue, the other is a
      // measurement of one tenant against it, and "headroom" is the word this
      // product already uses for that measurement everywhere else
      // (`headroomFor`, `headroomFigure`, the Pools table's own column).
      //
      // The id stays `profiles` -- `runner_profile` is the field name in the
      // contract and invariant 10 is the reason this screen exists, so the
      // ADDRESS keeps the contract's noun while the LABEL says which of the
      // two questions it answers. The screen's `<h1>` moved with the tab;
      // test_nav_headings_agree.py fails the build if it had not.
      { id: 'profiles', label: 'Profile headroom' },
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
    // HISTORY IS GONE AND ITS TWO PANES WENT TO DIFFERENT PLACES, which is the
    // fact the routing below has to survive. Timeline is a pane of Work;
    // Platform counts is here. `#history/timeline` and `#history/counts` both
    // still resolve, and they cannot do it by one head rewrite -- see
    // MOVED_PANES.
    //
    // WHY COUNTS IS ADMIN AND NOT WORK, given Timeline went to Work and both
    // count tasks. Timeline counts the rows it loaded and says so in its
    // header; Platform counts runs one Firestore `count()` per state, twelve
    // of them, twenty-four for an admin, behind a button that prints the cost
    // before you press it. It is admin-gated, it is the only screen in the
    // product that charges for a read, and the person who presses it is the
    // person changing ceilings -- not the person asking what their agent did.
    // It is the platform's own ledger, which is what this section is for.
    id: 'admin',
    label: 'Admin',
    // The count is named here because Admin is no longer only the things an
    // operator CHANGES: one of its three panes changes nothing and is a
    // platform-wide read. That is a widening, said out loud rather than
    // smuggled in by leaving the old sentence in place.
    question:
      'Change a ceiling, see who is registered to use this platform, and count what it has done.',
    tabs: [
      { id: 'limits', label: 'Pool limits', admin: true },
      { id: 'tenants', label: 'Tenants', admin: true },
      // The id stays `counts`, so `#counts` (LEGACY) and `#history/counts`
      // (MOVED_PANES) both land here.
      { id: 'counts', label: 'Platform counts', admin: true },
    ],
  },
]

/**
 * The API surface. Reachable, and deliberately NOT one of the sections.
 *
 * It is a thing you look up once, not a thing you work in, and giving it a
 * section would put it beside the three questions that people arrive with —
 * which is the mistake the old eleven-item nav made eleven times over.
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
 * `agents`, `pools`, `activity`, `history` and `runtimes` are NOT here although
 * all five were old top-level hashes: they are section ids that were renamed or
 * retired, and SECTION_ALIASES resolves them first, with their tail intact. An
 * entry here as well would be dead and would read as the place they are
 * handled. The one tail that an alias cannot carry -- `history/counts`, whose
 * pane went to a different section from its siblings -- is in MOVED_PANES,
 * which runs before both.
 */
const LEGACY: Record<string, { section: string; tab: string }> = {
  home: { section: 'overview', tab: 'now' },
  // The Trouble board has no route any more: there is no problem section at
  // any level. The hash still resolves, and it resolves to the screen that
  // carries the derived checks it used to hold.
  trouble: { section: 'overview', tab: 'now' },
  holders: { section: CAPACITY, tab: 'holders' },
  quota: { section: CAPACITY, tab: 'quota' },
  // `agents`, `pools`, `history` and `runtimes` are NOT here, for the reason
  // given above: they are section ids that were renamed or retired,
  // SECTION_ALIASES resolves them with their tail intact, and an entry here
  // would be dead code that reads as the place they are handled. `workflows`
  // is different -- it was never a section id, it was a top-level hash for
  // what is now a tab, so it belongs here.
  workflows: { section: WORK, tab: 'workflows' },
  // `counts` followed its pane out of History and into Admin. It is the bare
  // top-level hash from the eleven-item nav; `#history/counts`, which has a
  // tail, is MOVED_PANES' job.
  counts: { section: 'admin', tab: 'counts' },
  tenants: { section: 'admin', tab: 'tenants' },
  settings: { section: 'admin', tab: 'limits' },
}

/**
 * A RETIRED SECTION WHOSE PANES DID NOT ALL GO TO ONE PLACE.
 *
 * Keyed on the WHOLE old hash, head and tail together, and consulted before
 * anything else in `fromHash`. This is the case neither of the two mechanisms
 * above can express, and the three-section collapse produced two of them:
 *
 *   - SECTION_ALIASES rewrites a HEAD and keeps the tail, which is right when
 *     every pane of the old section landed in one new section. `history` is
 *     aliased to `work` because Timeline went there -- but Platform counts went
 *     to Admin, so `#history/counts` would arrive at `work/counts`, match no
 *     tab, and then be read as a TASK ID by the drawer fallback below. A saved
 *     link would open an agent inspector for an agent called "counts".
 *   - LEGACY maps a whole old hash to one destination and DROPS the tail,
 *     which is right for `#counts` and wrong for `#history/counts`: it would
 *     land on Work's first pane, which looks like a working link to the wrong
 *     screen -- worse than a dead one.
 *
 * `settings` is here for the same reason and is not new: the two-pane Settings
 * screen split between Admin and Capacity in the redesign, and this replaces
 * the hand-rolled `LEGACY_SETTINGS` branch that used to do it. One mechanism,
 * because two mechanisms for one rule is the defect this repository keeps
 * paying for -- see ux-plan.md §1.5.
 *
 * `#settings/<anything else>` still lands on Pool limits without an entry
 * here: `settings` is neither a section nor an alias, so it falls through to
 * `LEGACY['settings']`.
 */
const MOVED_PANES: Record<string, { section: string; tab: string }> = {
  'history/counts': { section: 'admin', tab: 'counts' },
  // `#activity/counts` is the same pane one rename further back: `activity`
  // was History's id before it was renamed. It aliases to `work` now, so it
  // needs the same interception.
  'activity/counts': { section: 'admin', tab: 'counts' },
  'settings/limits': { section: 'admin', tab: 'limits' },
  'settings/accounts': { section: CAPACITY, tab: 'accounts' },
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
 *
 * A HEAD ALIAS IS ONLY HONEST WHEN THE WHOLE OLD SECTION WENT ONE WAY. Where
 * it did not -- History's two panes went to Work and to Admin -- the tail that
 * disagrees is intercepted by MOVED_PANES above, which runs first. An alias
 * without that interception does not fail: it lands the reader somewhere
 * plausible and wrong, which is the failure mode this whole file is arranged
 * against.
 */
const SECTION_ALIASES: Record<string, string> = {
  agents: WORK,
  // `capacity` -> `pools` -> `capacity`. The middle spelling had a life of its
  // own in saved links, so it resolves too; what it must never do again is
  // appear in a link this app writes.
  pools: CAPACITY,
  // `activity` -> `history` -> `work`. Two renames deep: Activity became the
  // History section, and History's Timeline pane is now a pane of Work. Both
  // old heads point at the section Timeline actually lives in, so
  // `#activity/timeline` and `#history/timeline` each open the Timeline pane
  // with their tail intact. Their `counts` tails are MOVED_PANES' job.
  activity: WORK,
  history: WORK,
  // `runtimes` was a one-pane section and the pane kept its id, so the head
  // rewrite is the whole of it: `#runtimes/catalogue` -> `capacity/catalogue`,
  // which is the Runtimes tab. A bare `#runtimes` lands on Capacity's first
  // pane, like any unrecognised tail -- `canonical` has always written
  // `<section>/<tab>`, so a bare section hash is something typed by hand
  // rather than something this app ever put in an address bar.
  runtimes: CAPACITY,
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
  /**
   * The agent list's tab and Recent state, when the address names them
   * (OV-10): `#work/running/recent/failed`. OPTIONAL, so every route built
   * without one is still a Route; absent and null both mean "the address names
   * no tab", which leaves the list where it is.
   */
  list?: AgentList | null
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

  // MOVED PANES FIRST, AND ON THE UNALIASED HASH, because this is the only
  // lookup in this function that sees both halves of the address at once.
  // Every branch after it has already collapsed the head to one spelling,
  // which is what makes them simple and is exactly why none of them can tell
  // `#history/timeline` (whose pane went to Work with the head) from
  // `#history/counts` (whose pane went to Admin instead). Left to the head
  // alias, the second one resolves to `work/counts`, matches no tab, and is
  // then read as a TASK ID by the drawer fallback below -- a saved link
  // opening an agent inspector for an agent called "counts".
  const moved = MOVED_PANES[seg.join('/')]
  if (moved) return { sectionId: moved.section, tab: moved.tab, ...blank }

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

  // THE AGENT LIST'S OWN ADDRESSES (OV-10), and BEFORE the rule below that
  // reads an unmatched Work tail as a task id. Without this, `running/recent`
  // matched no tab and opened a drawer for an agent called "running/recent".
  // NOTHING UNDER `running/` IS A TASK ID: a tail that is not one of the list
  // addresses (`running/bogus`, `running/live/failed`, `running/recent/faild`)
  // is the plain list, never a drawer and never a guessed tab.
  if (head === WORK && tail[0] === 'running' && tail.length > 1) {
    const list = parseAgentList(tail.slice(1))
    return list === null
      ? { sectionId: WORK, tab: 'running', ...blank }
      : { sectionId: WORK, tab: 'running', ...blank, list }
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
  // A list address is written only while no drawer is open: the drawer's own
  // address wins above, and the list it was opened from is kept by App.
  if (r.sectionId === WORK && r.tab === 'running' && r.list) {
    return `${WORK}/running/${agentListPath(r.list)}`
  }
  return `${r.sectionId}/${r.tab}`
}

/**
 * The key a screen's reads are scoped by (CH-2): the canonical route WITHOUT
 * the agent list's tab and Recent state (OV-10).
 *
 * A list address names a VIEW of one mounted screen, not a screen. The Agents
 * list filters the rows it already holds when its tab or segment changes and
 * reads nothing, so keying the scope by the full address began an empty scope
 * on every tab or segment click -- and on closing an inspector back to
 * `#work/running/recent/failed` after it had opened from there, since the
 * drawer's own page key carries no list. Both are the defect `beginScreenReads`
 * exists to prevent: the head saying "reading…" beside a list fully drawn,
 * with nothing in flight until the next poll. The address bar still carries
 * the list (`canonical`); only the reads scope ignores it.
 */
function readsKey(r: Route): string {
  return canonical(r)
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

  // A NEW SCREEN'S READS START FROM NOTHING (CH-2). A LAYOUT effect, so it has
  // run before any screen's own `useEffect` issues a read: React runs every
  // layout effect of a commit before the first passive one, parent or child.
  // Keyed by the canonical route, so opening an agent over the list, or
  // switching its pane, is a new screen too -- what the head then reports is
  // what that view has read, not what the list read before it.
  //
  // AND THE LIST UNDER IT IS NAMED (CH-2): `pageKey` is the route with its task
  // removed, the page the inspector is drawn over. That page never unmounts
  // while the inspector opens, switches pane and closes, so its reads carry on
  // across all three -- closing the inspector used to begin an empty scope
  // beside a fully drawn list, and the head said "reading…" with nothing being
  // read.
  //
  // THE LIST'S TAB AND STATE ARE NOT A NEW SCREEN (OV-10): both keys are
  // `readsKey`, the route with the list address removed -- see there.
  const screenKey = readsKey(at)
  const pageKey = at.taskId === null ? null : readsKey({ ...at, taskId: null })
  useLayoutEffect(() => {
    beginScreenReads(screenKey, pageKey)
  }, [screenKey, pageKey])

  const go = (to: string) => {
    window.location.hash = to
  }

  // THE LIST ADDRESS THE DRAWER WAS OPENED FROM (OV-10). Opening an agent
  // replaces the list address with the drawer's, so without this the drawer
  // closed to bare `work/running` while the list behind it still showed, say,
  // Recent · failed -- the address bar and the visible list disagreeing the
  // moment it shut. Updated from every route that is not a drawer, and from
  // the list's own clicks (which can happen with the drawer open). Written
  // during render rather than in an effect, so the close address below is
  // never one route behind; the write is idempotent, so a double render is
  // harmless.
  const lastList = useRef<AgentList | null>(at.list ?? null)
  if (at.taskId === null) lastList.current = at.list ?? null

  // A TAB OR SEGMENT CLICK IS A ROUTE CHANGE, and the normalise effect above
  // writes it with `replaceState` -- so clicks add no history entries, and
  // Agents.tsx never writes the hash itself.
  const onList = useCallback((list: AgentList) => {
    lastList.current = list
    setAt((r) => ({ ...r, list }))
  }, [])

  const section = sectionOf(at.sectionId)
  const inspector = at.taskId !== null
  const listAddress = canonical({
    sectionId: WORK,
    tab: 'running',
    taskId: null,
    taskPane: 'detail',
    list: lastList.current,
  })

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
              // THE PAGE (CH-2): its `Screen`s' reads stay its own while the
              // inspector is open over it (`RoutedPage` in Shell.tsx).
              <RoutedPage.Provider value={true}>
                <SectionBody
                  sectionId={section.id}
                  tab={at.tab}
                  taskId={at.taskId}
                  list={at.list ?? null}
                  onList={onList}
                  go={go}
                />
              </RoutedPage.Provider>
            )}
          </main>

          {at.taskId !== null && (
            <AgentDrawer taskId={at.taskId} pane={at.taskPane} closeTo={listAddress} go={go} />
          )}
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
 * once, not things you work in, and putting them among the sections is the
 * mistake the eleven-item nav made eleven times over.
 * `tests/unit/control_plane/test_nav_headings_agree.py` reads that class name
 * to find the utility button, so it is load-bearing rather than decorative.
 *
 * BELOW 900px IT IS TWO ROWS (CH-21). Two hundred pixels of a 390pt phone is
 * half the screen, so the column becomes a strip -- and it used to be ONE
 * scrolling row with the open section's tabs inserted inline, which moved the
 * position of every section after the open one. That broke the property this
 * whole component exists for (§6.14: a position means one thing). So:
 *
 *   row 1  `.ctl-rail-main`  the sections and the utility corner, identical on
 *                            every route
 *   row 2  `.ctl-rail-sub`   the open section's tabs, only when it has more
 *                            than one, each row scrolling on its own
 *
 * The open section's tabs are therefore drawn TWICE, by one component
 * (`RailTabs`): inline for the desktop column and as row 2 for the phone.
 * The sheet displays exactly one copy at any width (`.ctl-rail-main` is
 * `display: contents` above 900px, so the desktop column is unchanged). And
 * the two levels stop looking alike: a section's selection is a 2px `--text`
 * RULE and a tab's is a `--surface-2` FILL -- the phone case of §1.3's
 * "surface step plus a 2px rule", split between the two levels, so they
 * differ in greyscale and need no hue.
 *
 * WHAT WAS NOT BUILT, AND WHY IT IS NOT A 56px ICON RAIL BELOW 1280px. §B3
 * asks for icon-only at 56px. This product has no icon set, and a single
 * letter is not an icon -- it is a label with everything but the first
 * character deleted, which is a thing you decode rather than recognise.
 *
 * THAT ARGUMENT USED TO REST ON A COINCIDENCE, and the collapse to three
 * sections removed the coincidence: the rail said Agents and Admin, two
 * identical marks in a list whose whole value is that a position means one
 * thing. It now says Overview, Work, Capacity, Admin -- four distinct letters
 * -- so the collision is gone and the argument is NOT. A four-letter rail is
 * still four things to decode, and the tabs underneath (which is most of the
 * rail's height and all of its usefulness) have no initials at all. It narrows
 * to 152px instead.
 *
 * THE LONGEST TAB LABEL IS NOW "Profile headroom", one character longer than
 * the "Runner profiles" the 152px measurement was taken against. Nothing in
 * this repository measures rendered text at a width -- jsdom implements no
 * layout -- so that is a reason for someone to LOOK at 1279px, not a claim
 * that it fits, and it is written here rather than left for the reader who
 * finds it wrapped.
 */
function Rail({ at, go }: { at: Route; go: (to: string) => void }) {
  const rail = useRef<HTMLElement>(null)

  /*
   * THE CURRENT ITEM IS BROUGHT INTO VIEW ON EVERY ROUTE CHANGE (CH-14).
   *
   * Below 900px the rail is a strip that scrolls sideways, and nothing ever
   * scrolled it: measured at 390px, `#work/new`, `#admin/tenants` and `#help`
   * all opened with the current tab -- or the Help button -- past the right
   * edge, so the one item that says where the reader is was the one they
   * could not see.
   *
   * WHICH ITEM, IN THIS ORDER, and the order is why these are three queries
   * rather than one selector list: a list returns the first match in DOCUMENT
   * order, and a section button always precedes its own tabs, so `#admin/
   * counts` would have scrolled to "Admin" and left "Platform counts" off
   * screen. The selected tab if there is one; the on-state utility button for
   * API reads and Help; the section itself for a one-pane section.
   *
   * `nearest` ON BOTH AXES, so an item already in view does not move. On the
   * desktop column the rail is sticky and always on screen, so this only ever
   * scrolls the rail's own overflow, on a viewport too short to hold it.
   * BELOW 900px IT CAN SCROLL THE PAGE. The strip is `position: static`
   * there, inside `.ctl-scroll` (styles.css), so after a reader scrolls down
   * and follows an in-content link to another section, `block: nearest`
   * scrolls `.ctl-scroll` up just far enough to show the strip. That puts
   * them at the top of the screen they just opened rather than part-way down
   * it; nothing else resets the scroll position on a route change.
   * `scroll-margin-inline-end` (styles.css) keeps the item clear of the fade
   * at the strip's end. jsdom implements no `scrollIntoView`, hence the guard.
   */
  useEffect(() => {
    const root = rail.current
    if (root === null) return
    // THE VISIBLE COPY OF THE SELECTED TAB (CH-21). The open section's tabs
    // are drawn twice -- inline for the desktop column, and as row 2 below
    // 900px -- and the sheet displays one. Scrolling the hidden copy moves
    // nothing, so the one with a box is chosen; where nothing has a box
    // (jsdom has no layout) it is row 2's, the copy that scrolls.
    const tabs = [
      ...root.querySelectorAll<HTMLElement>('.ctl-rail-sub [role="tab"][aria-selected="true"]'),
      ...root.querySelectorAll<HTMLElement>('.ctl-rail-group [role="tab"][aria-selected="true"]'),
    ]
    const shown = tabs.find((el) => typeof el.getClientRects === 'function' && el.getClientRects().length > 0)
    const current =
      shown ??
      tabs[0] ??
      root.querySelector<HTMLElement>('.ctl-nav-util .is-on') ??
      root.querySelector<HTMLElement>('.ctl-nav-link.is-on')
    if (current !== null && current !== undefined && typeof current.scrollIntoView === 'function') {
      current.scrollIntoView({ inline: 'nearest', block: 'nearest' })
    }
  }, [at.sectionId, at.tab])

  const open = SECTIONS.find((s) => s.id === at.sectionId) ?? null

  return (
    <nav className="ctl-rail" aria-label="Sections" ref={rail}>
      {/* ROW 1 BELOW 900px, and `display: contents` above it, so the desktop
          column is exactly what it was: the sections, then the utility corner
          at the foot. */}
      <div className="ctl-rail-main">
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
                    one section is a duplicate of the section. Drawn for every
                    section at all times on the desktop column; hidden below
                    900px, where row 2 carries the open section's. */}
                {s.tabs.length > 1 && <RailTabs section={s} at={at} go={go} className="ctl-rail-tabs" />}
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
          {/* THE RAIL'S `?`, which this comment used to call "the head `?`" --
              it is not (AH-5). The head `?` is `SectionQuestion`, in `Head`
              below, and it is the way into Help that ui-audit §B7.2/§B7.3
              describe: its card now ends in a link here. This button is the
              other way in, always in the same place, for a reader with no
              screen-level `?` in front of them. A glyph and an accessible name,
              not the word "Help" -- the rail is the product's three questions
              over a landing screen, and a fifth word beside them reads as a
              fifth section. */}
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
      </div>

      {/* ROW 2 BELOW 900px: the open section's tabs, on a row of their own so
          nothing in row 1 moves when a section opens. Not drawn for a
          one-pane section (the rule the inline tabs already follow), and not
          displayed at all above 900px, where the inline copy is. */}
      {open !== null && open.tabs.length > 1 && (
        <RailTabs section={open} at={at} go={go} className="ctl-rail-tabs ctl-rail-sub" />
      )}
    </nav>
  )
}

/**
 * One section's tabs, the one way they are drawn -- used for the inline copy
 * on the desktop column and for row 2 of the phone strip (CH-21), so the two
 * cannot drift. `.ctl-rail-tabs` on both is what gives row 2 every tab rule the
 * inline tabs have, the 44px phone target included; `.ctl-rail-sub` is only
 * what places it.
 */
function RailTabs({
  section,
  at,
  go,
  className,
}: {
  section: SectionDef
  at: Route
  go: (to: string) => void
  className: string
}) {
  const on = at.sectionId === section.id
  return (
    <div className={className} role="tablist" aria-label={`${section.label} views`}>
      {section.tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={on && at.tab === t.id}
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
 * THE HEAD (§B3): 44px, spanning the work area, above everything routed.
 *
 * Three things, and the third is the one that moved.
 *
 * 1. THE BREADCRUMB -- section, pane, and the object if one is open. It is the
 *    only place the open agent's id appears outside the inspector itself, and
 *    it is what makes "back" legible: you can see what you would go back to.
 *
 * 2. THE READ AGE -- OF THIS SCREEN'S OWN READS (CH-2). It was the newest
 *    SUCCESSFUL payload of any route the whole tab had called, so it sat
 *    beside the page title saying "just now" while that page was still
 *    loading: the frame's identity read had landed, the page's had not, and
 *    the dock already said the same tab-wide thing a few hundred pixels
 *    below. Now it is the newest success among the reads the CURRENT screen
 *    started (`beginScreenReads` in fetch.ts, keyed by `readsKey(at)`), and
 *    the dock keeps the tab-wide view. Four sentences, each a measurement or
 *    the plain absence of one:
 *
 *      reading…            nothing this screen asked for has settled yet
 *      newest read 4s ago  the newest payload this screen received
 *      not read            every read it made failed
 *      admin only          every read it made met the admin gate
 *
 *    Help and API reads issue no reads of their own and say so.
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
  const reads = useSyncExternalStore(subscribeScreenReads, screenReadsSnapshot, screenReadsSnapshot)
  // The age is the point, so it moves on its own rather than only when a
  // fetch happens to land -- on the SHARED clock, the one every screen's
  // sub-line and the dock read, so the head and the provenance line under a
  // screen title can no longer disagree by up to a tick (CH-1).
  const now = useNow(AGE_TICK_MS)

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
        <ScreenAge at={at} reads={reads} now={now} />
      </span>

      {section !== null && <SectionQuestion section={section} />}
    </div>
  )
}

/**
 * The screen's own read age, in the head (CH-2). See `Head` for the four
 * sentences; the rule under all of them is that the head never shows an age
 * for data this screen has not received -- not the frame's identity read, not
 * the screen you just left, not a success from another route of the tab.
 */
function ScreenAge({ at, reads, now }: { at: Route; reads: ScreenReads; now: number }) {
  // Help and API reads draw from nothing they fetch, so there is no age.
  if (at.sectionId === HELP || at.sectionId === REFERENCE) {
    return <span className="ctl-em">reads nothing</span>
  }
  // `reads` is about ANOTHER screen until this one's scope has begun (the
  // first render of a route comes before its layout effect), and a screen
  // whose reads have not settled is still reading.
  //
  // "NOTHING SETTLED" MEANS A SCREEN THAT HAS JUST MOUNTED, and only that: a
  // scope starts from nothing only for a screen that mounts on this route
  // change, and every screen reads on mount -- its read starts in the effect
  // right after this render (the fixture path registers it only when it
  // lands). A screen that stayed mounted -- the list under a closing
  // inspector -- keeps its own reads (`beginScreenReads`), so it can never
  // land here saying "reading…" with nothing being read.
  const own = reads.key === readsKey(at) ? reads : null
  if (own !== null && own.newestSuccessAt !== null) {
    return <>newest read {timeAgo(own.newestSuccessAt, now)}</>
  }
  if (own === null || own.inFlight > 0 || own.settled === 0) {
    return <span className="ctl-em">reading…</span>
  }
  // Settled, nothing landed. The admin gate is information, not a failure, and
  // is said as the kit says it; anything else is a read that did not land.
  return <span className="ctl-em">{own.failed === 0 ? 'admin only' : 'not read'}</span>
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
 * reaching the second, and a third would do it again. The same goes for the
 * outside press (AH-6, in `useHelpDisclosure`) and the tab bridge
 * (`useCardBridge`), which this card needs now that it has a stop in it.
 *
 * IT ENDS IN A WAY INTO HELP (AH-5). The code and ui-audit §B7.2/§B7.3 call the
 * head `?` the way into Help, and its card was a dead end: the section's
 * question and nothing to follow. `Help →` goes to the top of the Help page,
 * because the question is about a section and no single topic answers it.
 */
function SectionQuestion({ section }: { section: SectionDef }) {
  // THE CARD IS MEASURED THROUGH `cardRef`, and before it existed this card
  // was not measured at all. `useEdgeSafePlacement` used to look for the card
  // under the ANCHOR, and this one is portalled to `document.body` below, so
  // the vertical placement fell back to its 220px estimate every time. A
  // section's question is the longest string in `SECTIONS` -- Capacity's runs
  // to four clauses -- and at 390px it wraps well past 220px, which is exactly
  // the case the estimate gets wrong and the clamp then cannot correct. The
  // ref belongs to the disclosure, whose outside-press test needs it too.
  const { state, trigger, hover, cardRef, triggerRef } = useHelpDisclosure()
  const cardId = `q-${section.id}`
  const triggerId = `${cardId}-t`
  const [anchorRef, placement] = useEdgeSafePlacement(state.open, cardRef)
  const bridge = useCardBridge(state, trigger, cardRef, triggerRef)

  const card = (
    <span
      id={cardId}
      role={state.pinned ? 'dialog' : 'tooltip'}
      className="ctl-q-card"
      data-focus-return={triggerId}
      style={placement}
      ref={cardRef}
    >
      <strong className="ctl-q-title">{section.label} answers</strong>
      <span className="ctl-q-body">{section.question}</span>
      {/* Inline, from tokens, like the topic cards' own link: styles.css is
          another lane's this pass, and an inline style reaches no other
          element. */}
      <a
        className="ctl-link"
        href={`#${HELP}`}
        style={{
          display: 'inline-block',
          marginTop: 'var(--ctl-s2)',
          fontSize: 'var(--t-micro)',
          lineHeight: 'var(--lh-micro)',
        }}
        {...bridge.stop}
      >
        Help &rarr;
      </a>
    </span>
  )

  return (
    <span className="ctl-q" {...hover} ref={anchorRef}>
      <button
        type="button"
        id={triggerId}
        ref={triggerRef}
        className="ctl-q-glyph"
        // `Help: <card title>`, the name every `?` in the app now carries
        // (AH-4), so one query finds both kinds and a screen reader hears the
        // same shape of name wherever it meets one.
        aria-label={`Help: ${section.label} answers`}
        aria-expanded={state.open}
        aria-controls={state.open ? cardId : undefined}
        {...trigger}
        {...bridge.trigger}
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
  taskId,
  list,
  onList,
  go,
}: {
  sectionId: string
  tab: string
  /** The agent the inspector has open, or null. */
  taskId: string | null
  /** The list address's tab and Recent state (OV-10), or null when it names none. */
  list: AgentList | null
  /** Where the list reports a tab or state click; App turns it into the route. */
  onList: (list: AgentList) => void
  go: (to: string) => void
}) {
  const openAgent = (id: string) => go(`${WORK}/task/${encodeURIComponent(id)}`)
  /*
   * THE LIST IS TOLD WHICH AGENT IS OPEN (AG-17). `taskId` stopped at the
   * drawer, so with the inspector open no row said which agent it was showing
   * -- the list and the panel beside it could not be read as one view.
   *
   * SPREAD FROM AN OBJECT, NOT WRITTEN AS AN ATTRIBUTE, so this compiles
   * whichever of two lanes lands first: `Agents.tsx` declares the optional
   * `taskId` prop in its own lane, and a JSX attribute naming a prop the
   * component does not declare is a type error, where an object spread is
   * not checked for extra keys. Once the prop is declared the spread is
   * checked like any attribute, so nothing is left unchecked for long.
   */
  const agentsProps = { onOpen: openAgent, taskId, list, onList }

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
      return <AgentsScreen {...agentsProps} />
    case 'work/workflows':
      return <WorkflowsScreen />
    case 'work/timeline':
      return <ActivityScreen />
    case 'work/new':
      return <SubmitScreen />
    case 'work/new-workflow':
      return <SubmitWorkflowScreen />

    case 'capacity/pools':
      return <CapacityScreen />
    case 'capacity/catalogue':
      return <RuntimesScreen />
    case 'capacity/profiles':
      return <ProfilesScreen />
    case 'capacity/holders':
      return <HoldersScreen />
    case 'capacity/accounts':
      return <AccountsScreen />
    case 'capacity/quota':
      return <QuotaDetailScreen />

    case 'admin/limits':
      return <AdminSettingsScreen />
    case 'admin/tenants':
      return <TenantsScreen />
    case 'admin/counts':
      return <PlatformCountsScreen />

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
  closeTo,
  go,
}: {
  taskId: string
  pane: TaskPane
  /**
   * The list address this drawer was opened from (OV-10), so closing it
   * restores the address the list behind it is showing -- not bare
   * `work/running`, which would name a different tab than the one on screen.
   */
  closeTo: string
  go: (to: string) => void
}) {
  // `WORK`, NOT THE LITERAL `agents`. These two were the last places in the app
  // still MINTING the old spelling: `openAgent` twenty lines up already builds
  // `${WORK}/task/...`, and `nav.links.test.tsx` fails the build on an internal
  // href that uses an alias -- but these are `go()` calls, so it could not see
  // them. Every pane switch and every close wrote `#agents/...`, which resolved
  // through SECTION_ALIASES and was then rewritten in place by the canonicalise
  // effect, so nothing was visibly broken and nothing was going to make anyone
  // update it either.
  const base = `${WORK}/task/${encodeURIComponent(taskId)}`
  const close = () => go(closeTo)
  const [width, setWidth] = useState(() => readPane(INSPECTOR))
  const dragging = useRef(false)
  const panel = useRef<HTMLDivElement>(null)
  /** The element that was focused when this opened. Where Escape puts you back. */
  const opener = useRef<Element | null>(null)

  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--inspector-w', `${width}px`)
    return () => {
      root.style.removeProperty('--inspector-w')
    }
  }, [width])

  /*
   * FOCUS, ON THE WAY IN AND ON THE WAY OUT.
   *
   * WHAT WAS WRONG. This panel carried `role="dialog"` and no focus handling of
   * any kind. Opening it from the keyboard left focus on the row behind it, so
   * the next Tab walked the list the panel was covering; clicking the ✕
   * unmounted the element that held focus, which puts `document.activeElement`
   * back on `<body>` -- a reader who had tabbed forty rows down was returned to
   * the top of the document with nothing said. That is the single worst
   * keyboard defect on these screens, because the agent list is the screen
   * people arrive on and the drawer is how they read one.
   *
   * THE ROW IS STILL THERE TO GO BACK TO, which is what makes the restore
   * honest rather than a guess. The drawer is a SIBLING of `<main>` (see the
   * shell above) and the route that opens it keeps `tab: 'running'`, so the
   * list is never unmounted and the row node that was clicked is the same node
   * after the drawer closes. Nothing has to be found again by id.
   *
   * WHY THE OPENER IS CAPTURED HERE AND NOT PASSED IN. The drawer is a ROUTE --
   * a deep link, the breadcrumb, a workflow node and a row click all open it --
   * so there is no single caller who could hand over "the thing you came from".
   * `document.activeElement` at mount is the only answer that is right for all
   * of them, and when it is `<body>` (a pasted link, a page load) the restore
   * correctly does nothing.
   *
   * StrictMode double-invokes this in development: mount, cleanup, mount. The
   * cleanup restores focus to the row, so the second mount captures the row
   * again and the net effect is the same as a single pass.
   */
  useEffect(() => {
    const el = panel.current
    const came = document.activeElement
    opener.current = came
    // FOCUS MOVES IN ONLY WHEN THE PANEL COVERS THE LIST. Above 1100px this is
    // a grid column beside the rows, and pulling focus off the row into a panel
    // that did not obscure anything would be the mirror of the bug above.
    if (el !== null && isOverlay(el)) el.focus()
    return () => {
      const back = opener.current
      opener.current = null
      if (back instanceof HTMLElement && back.isConnected && back !== document.body) {
        back.focus()
      }
    }
    // Once per open. `taskId` changing swaps the CONTENTS of an open drawer and
    // must not re-capture an opener that is now inside the drawer itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
    <div
      className="drawer ctl-drawer"
      role="dialog"
      aria-label={`Agent ${taskId}`}
      ref={panel}
      // -1, so the panel is a legal destination for `.focus()` when it opens
      // over the list and is NOT a stop Tab lands on afterwards.
      tabIndex={-1}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          // An open help card inside the drawer stops Escape before it reaches
          // here (see `HelpCard.tsx`), so the innermost open thing closes.
          e.stopPropagation()
          close()
          return
        }
        // Only while it is an overlay. `trapTab` reads the computed position
        // rather than a breakpoint repeated here; see `isOverlay`.
        if (e.key === 'Tab' && panel.current !== null && isOverlay(panel.current)) {
          trapTab(e, panel.current)
        }
      }}
    >
      <div
        className="ctl-inspector-grip"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the inspector"
        /*
         * A CONTROL THE MOUSE COULD REACH AND THE KEYBOARD COULD NOT. This was
         * four pointer handlers on a `<div>`: no `tabindex`, no key handling,
         * so the inspector's width was adjustable only by dragging. The
         * WAI-ARIA window-splitter pattern is a focusable separator that moves
         * on the arrow keys and reports its position, which is what these
         * three `aria-value*` attributes are for -- a separator that is a tab
         * stop and does NOT report its value announces as an unlabelled
         * landmark.
         *
         * LEFT WIDENS, because the inspector is anchored to the right edge:
         * the handle moving left is the panel getting bigger, which is what
         * the pointer drag already does.
         */
        tabIndex={0}
        aria-valuenow={width}
        aria-valuemin={INSPECTOR.min}
        aria-valuemax={INSPECTOR.max}
        onKeyDown={(e) => {
          const next = nudgePane(width, e.key, INSPECTOR, 'ArrowLeft')
          if (next === null) return
          e.preventDefault()
          setWidth(next)
          writePane(INSPECTOR, next)
        }}
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
      {/* THE ONE SEGMENTED CONTROL, NOT TWO PILLS. `.ctl-subnav` drew Detail /
          Attempts as 999px pills with a filled, bordered selection -- the
          shape design-system.md §6.11 retired in favour of `.ctl-seg` (one
          bordered group, selection by a surface step and weight). `ctl-subnav`
          stays only for where the strip sits in the drawer. */}
      <div className="ctl-seg ctl-subnav" role="tablist" aria-label="Agent panes">
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
  // The ages are the point, so they move on their own rather than only when a
  // fetch happens to land -- on the shared clock the head and the dock read.
  const now = useNow(AGE_TICK_MS)

  return (
    <>
      {/* THE LEAD PARAGRAPH IS THE COLUMN HEADING NOW. 48 words said one thing:
          this is what THIS TAB called, not what the API offers. That caveat
          belongs to the thing it qualifies -- the page's own title -- so it is
          a `.ctl-card-note` beside it, in the slot §8.4.2 reserves for exactly
          this, and the argument is one click away in `#help/api-reads`.

          `ctl-link` (CH-5): this anchor carried no class, so it fell back to
          the browser's own blue -- visited purple once followed -- in a
          product whose links are ink plus an underline. */}
      <div className="ctl-page-head">
        <h1>{REFERENCE_LABEL}</h1>
        <span className="ctl-card-note">this tab only · not the API surface</span>
        <a className="ctl-link is-end" href={`#${HELP}/api-reads`}>
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
          {/* `is-scroll` (CH-13, design-system.md §7.3). This was `is-stacked`,
              for F6 of `docs/audits/2026-09-23/overflow-inventory.md`: five
              columns behind an `overflow-x: auto` that paints no scrollbar.
              The owner's rule for tables below 900px is that a DATA table --
              five or more columns, compared across rows -- scrolls with its
              first column held in view, and only a record of four columns or
              fewer stacks. This is a data table: the route stays pinned at the
              left edge while the outcome, latency and age scroll beside it. */}
          <div className="ctl-table is-scroll">
            <table role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Route</th>
                  <th role="columnheader" scope="col">Last attempt</th>
                  {/* THE PARENTHETICAL CARRIES THE CAVEAT (§8.4.3). The
                      caption used to spend 24 words saying a 403 on an admin
                      route is the expected answer for a non-admin; the column
                      it is about says so instead, and `describeProbe` already
                      draws that row with the neutral flat bar (`is-info`,
                      grey since CH-17) rather than in `--bad`. */}
                  <th role="columnheader" scope="col">Outcome (403 on /v1/admin is expected)</th>
                  <th role="columnheader" scope="col" className="is-num">Took</th>
                  <th role="columnheader" scope="col">Newest payload</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {probes.map((p) => (
                  <RouteRow key={p.path} probe={p} now={now} />
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

function RouteRow({ probe, now }: { probe: ProbeRecord; now: number }) {
  const outcome = describeProbe(probe)
  return (
    <tr role="row" className={outcome.row}>
      <th role="rowheader" scope="row" className="ctl-ref-path">
        {probe.path}
        {/* THE CONCRETE CALL, in the raw-id slot under the readable name
            (§6.7). A route is a template now (CH-18) -- one row for every
            task's attempts read -- so this is how a reader still finds WHICH
            task's read the outcome beside it belongs to. Always drawn, as the
            slot always is: the name and the id, never one.
            CUT BELOW 900px, WHOLE IN ITS TITLE (CH-13): the route cell is the
            held column, which has a ceiling, and a checkpoint file's URL is
            110 characters -- one line, ellipsized, rather than six. */}
        <span className="ctl-sub" title={probe.lastUrl}>
          {probe.lastUrl}
        </span>
      </th>
      <td role="cell" data-label="Last attempt">{timeAgo(probe.lastAttemptAt, now)}</td>
      <td role="cell" data-label="Outcome">
        <span className={`ctl-chip ${outcome.tone}`}>
          <i aria-hidden />
          {outcome.label}
        </span>
      </td>
      <td role="cell" data-label="Took" className="is-num ctl-ref-ms">{probe.lastLatencyMs}ms</td>
      <td role="cell" data-label="Newest payload">
        {/* THE COLUMN THAT MATTERS. A panel showing a figure from four minutes
            ago while its route has been failing for three of them looks
            healthy unless this says otherwise. "never" is a fact about this
            tab, and is not written as a time. */}
        {probe.lastSuccessAt === null ? (
          <span className="ctl-em">never in this tab</span>
        ) : (
          timeAgo(probe.lastSuccessAt, now)
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
