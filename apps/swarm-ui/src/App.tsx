import {
  Fragment,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { agentListPath, parseAgentList, type AgentList } from './agentlist'
import { AccountsScreen } from './Accounts'
import { ActivityScreen, TenantsScreen } from './Activity'
import { AdminSettingsScreen } from './AdminSettings'
import { AgentsScreen } from './Agents'
import { TimelineLanesScreen } from './TimelineLanes'
import { AgentSplit } from './AgentSplit'
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
import { useCardBridge, useHelpDisclosure, useEdgeSafePlacement } from './HelpCard'
import { HELP_ROUTE } from './help'
import { SUBMIT_ADDRESS, addressToPath, isLegacyHash, pathToAddress } from './paths'
import { Icon, SkyShell, type SpineSection } from './Spine'
import { routedClick } from './components'
import { HelpScreen, helpPageOf } from './HelpSection'
import { HoldersScreen } from './Holders'
import { OverviewScreen } from './Overview'
import { PlatformCountsScreen } from './PlatformCounts'
import { ProfilesScreen } from './Profiles'
import { QuotaDetailScreen } from './QuotaDetail'
import { RuntimesScreen } from './Runtimes'
import { FrameAge, HeadAge, PageHead, RoutedPage, SectionHelp, timeAgo, useHeadRowClaimed, usePageAgeClaimed } from './Shell'
import { SubmitScreen } from './Submit'
import { SubmitChooser } from './SubmitChooser'
import { SubmitWorkflowScreen } from './SubmitWorkflow'
import { IssueSubmitScreen } from './IssueSubmit'
import { RunsScreen } from './Runs'
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
      // ISSUE RUNS (intake-tenants.html 1A): /runs and /runs/<id>.
      { id: 'runs', label: 'Runs' },
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
      { id: 'new-issue', label: 'Submit from a GitHub issue' },
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
      // (`headroomFor`, and the Profiles matrix it feeds).
      //
      // The id stays `profiles` -- `runner_profile` is the field name in the
      // contract and invariant 10 is the reason this screen exists, so the
      // ADDRESS keeps the contract's noun while the LABEL says which of the
      // two questions it answers. The screen's `<h1>` moved with the tab;
      // test_nav_headings_agree.py fails the build if it had not.
      // "By runner profile" since the rebrand (navigation.html, 2026-10-01):
      // Profile headroom folds into Pools as its profile-by-pool view, and the
      // panel lists it under Pools by this name.
      { id: 'profiles', label: 'By runner profile' },
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
    //
    // A QUESTION, AS THE OTHER THREE ARE (AH-23). It was an instruction --
    // "Change a ceiling, see who is registered..., and count what it has
    // done" -- which is a list of things to do rather than what a reader
    // arrives wanting to know. It asks what each tab answers, in tab order.
    // docs/web-ui/redesign.md §2 carries the same words, and
    // tests/sections.test.ts holds the two together.
    question:
      'What is each ceiling set to, who is registered to use this platform, and how many tasks are in each state?',
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

/** The Submit chooser's address (`/submit`). Not a section, like Help. */
const SUBMIT = SUBMIT_ADDRESS

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

/**
 * Every head an OLD `#...` address can start with: the sections, their
 * aliases, the eleven-item nav's LEGACY hashes, Help and API reads. A fragment
 * whose head is one of these is a route from the hash router and is redirected
 * to its path once (`fromLocation`, `App`); any other fragment is an anchor.
 */
export const LEGACY_HEADS: readonly string[] = [
  ...SECTIONS.map((s) => s.id),
  ...Object.keys(SECTION_ALIASES),
  ...Object.keys(LEGACY),
  ...Object.keys(MOVED_PANES).map((k) => k.split('/')[0]!),
  HELP_ROUTE,
  REFERENCE,
  SUBMIT_ADDRESS,
]

/**
 * Which pane of one agent is open. `artifacts` (#184) is what the agent took
 * in and what it produced -- inputs, the answer and every file, and its logs
 * and transcript, live while it runs. Its address is
 * `#work/task/<id>/artifacts`, as `attempts` is `#work/task/<id>/attempts`.
 */
export type TaskPane = 'detail' | 'attempts' | 'artifacts' | 'checkpoints'

/** The address segment each non-default pane is written with. `detail` has none. */
const PANE_SEGMENTS: readonly TaskPane[] = ['attempts', 'artifacts', 'checkpoints']

export interface Route {
  /** A section id, or REFERENCE. */
  sectionId: string
  /** Meaningless when sectionId is REFERENCE; carried anyway so Route is flat. */
  tab: string
  /** Set when an agent drawer is open over the Agents section. */
  taskId: string | null
  taskPane: TaskPane
  /**
   * The output open in the Artifacts pane, when the address names one:
   * `#work/task/<id>/artifacts/<name>`, `/agents/<tab>/<id>/artifacts/<name>`
   * (owner decision 2026-10-01). OPTIONAL like `list`. The name only -- which
   * file of a patch is open is never in the address.
   */
  artifact?: string | null
  /**
   * The agent list's tab and Recent state, when the address names them
   * (OV-10): `#work/running/recent/failed`. OPTIONAL, so every route built
   * without one is still a Route; absent and null both mean "the address names
   * no tab", which leaves the list where it is.
   */
  list?: AgentList | null
  /**
   * The Timeline's view -- every filter it has -- as the hash's query
   * (`#work/timeline?span=30d&table=1`, #185). OPTIONAL like `list`: absent
   * and null both mean "the address names no view". The Timeline carries
   * one, and so does Pool limits, as the row a link named
   * (`#admin/limits?pool=tenant%3Aeng`, #134).
   */
  view?: string | null
  /** The Timeline's second page, Outcomes (`/timeline/outcomes`); absent is Lanes. */
  page?: 'outcomes' | null
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
  return fromAddress(window.location.hash.replace(/^#/, ''))
}

/**
 * The route the window's location names: a legacy `#...` fragment if it
 * carries one (resolved once, then rewritten to its path by `App`), otherwise
 * the real path (paths.ts).
 */
export function fromLocation(): Route {
  const { pathname, search, hash } = window.location
  if (isLegacyHash(hash, LEGACY_HEADS, pathname)) return fromHash()
  const p = pathToAddress(pathname, search, hash)
  if (p === null) return fromAddress('')
  const r = fromAddress(p.address)
  return p.agentTab === null || r.taskId === null ? r : { ...r, list: { tab: p.agentTab, state: null } }
}

/**
 * An address (paths.ts), resolved to a route: the old hash router's grammar,
 * aliases and moved panes included, so every saved hash still lands.
 */
export function fromAddress(full: string): Route {
  // THE QUERY IS NOT PART OF THE PATH (#185). The Timeline writes its view as
  // `#work/timeline?span=30d`; left on the path, `timeline?span=30d` matched
  // no tab and the Work fallback below read it as a TASK ID, opening an
  // inspector for an agent called "timeline?span=30d".
  const queryAt = full.indexOf('?')
  const hash = queryAt === -1 ? full : full.slice(0, queryAt)
  const query = queryAt === -1 ? '' : full.slice(queryAt + 1)
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
  // The Submit chooser (/submit): two large choices, opened by the spine's
  // Submit and by N.
  if (head === SUBMIT && tail.length === 0) return { sectionId: SUBMIT, tab: '', ...blank }

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
    let rest = tail.slice(1)
    // ONE OUTPUT OF THE ARTIFACTS PANE: `<id>/artifacts/<name>`, the name one
    // encoded segment. Read before the pane, so `artifacts` is still the pane.
    let artifact: string | null = null
    if (rest.length >= 3 && rest[rest.length - 2] === 'artifacts') {
      artifact = decodeSegment(rest[rest.length - 1]!)
      rest = rest.slice(0, -1)
    }
    const last = rest[rest.length - 1]
    const pane = PANE_SEGMENTS.find((p) => p === last) ?? null
    // Task ids are opaque and may contain characters that were encoded on the
    // way in, so the remaining segments are rejoined rather than assumed to
    // be one.
    const id = (pane !== null ? rest.slice(0, -1) : rest).join('/')
    if (id) {
      return {
        sectionId: WORK,
        tab: 'running',
        taskId: decodeURIComponent(id),
        taskPane: pane ?? 'detail',
        ...(artifact !== null && artifact !== '' ? { artifact } : {}),
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
    if (section.id === WORK && wanted === 'timeline/outcomes') {
      return { sectionId: WORK, tab: 'timeline', ...blank, page: 'outcomes', ...(query !== '' ? { view: query } : {}) }
    }
    if (tab && section.id === WORK && tab.id === 'timeline' && query !== '') {
      return { sectionId: section.id, tab: tab.id, ...blank, view: query }
    }
    // The Workflows list's filters and its open workflow (`wf=<id>`) ride on
    // its address the same way: `/workflows/<id>?owner=me` (paths.ts).
    if (tab && section.id === WORK && (tab.id === 'workflows' || tab.id === 'runs') && query !== '') {
      return { sectionId: section.id, tab: tab.id, ...blank, view: query }
    }
    // THE ROW A LINK NAMED rides on Pool limits' address (#134): the Tenants
    // roster links each Enforced figure to `#admin/limits?pool=tenant:<id>`.
    // Dropped here, the normalise effect rewrote the address to
    // `#admin/limits` before the screen's async read had drawn a row, and the
    // link opened the page at no row at all. Only `pool` is kept.
    // Pools and Holders keep it too (#125, #128): a Pools row links to
    // Holders filtered by its pool, and Provider quota to the pool's row.
    if (tab && keepsPool(section.id, tab.id)) {
      const pool = new URLSearchParams(query).get('pool')
      if (pool) return { sectionId: section.id, tab: tab.id, ...blank, view: poolQuery(pool) }
    }
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

const ADMIN_SECTION = 'admin'
const LIMITS_TAB = 'limits'

/** The panes whose address carries the `?pool=` a link named, and no other. */
function keepsPool(sectionId: string, tab: string): boolean {
  return (
    (sectionId === ADMIN_SECTION && tab === LIMITS_TAB) ||
    (sectionId === CAPACITY && (tab === 'pools' || tab === 'holders'))
  )
}

/** Pool limits' query for one pool: `pool=tenant%3Aeng`. */
function poolQuery(pool: string): string {
  return new URLSearchParams({ pool }).toString()
}

/** The one spelling of a route. What the address bar is rewritten to. */
export function canonical(r: Route): string {
  if (r.taskId !== null) {
    const base = `${WORK}/task/${encodeURIComponent(r.taskId)}`
    if (r.taskPane === 'artifacts' && r.artifact) return `${base}/artifacts/${encodeURIComponent(r.artifact)}`
    return r.taskPane === 'detail' ? base : `${base}/${r.taskPane}`
  }
  if (r.sectionId === REFERENCE) return REFERENCE
  if (r.sectionId === SUBMIT) return SUBMIT
  if (r.sectionId === HELP) return r.tab === '' ? HELP : `${HELP}/${r.tab}`
  // A list address is written only while no drawer is open: the drawer's own
  // address wins above, and the list it was opened from is kept by App.
  if (r.sectionId === WORK && r.tab === 'running' && r.list) {
    return `${WORK}/running/${agentListPath(r.list)}`
  }
  // The Timeline's view rides on its address, so a copied link reproduces the
  // page (#185). Written only for that route: no other screen reads a query.
  if (r.sectionId === WORK && r.tab === 'timeline' && (r.view || r.page === 'outcomes')) {
    const at = r.page === 'outcomes' ? `${WORK}/timeline/outcomes` : `${WORK}/timeline`
    return r.view ? `${at}?${r.view}` : at
  }
  if (r.sectionId === WORK && (r.tab === 'workflows' || r.tab === 'runs') && r.view) {
    return `${WORK}/${r.tab}?${r.view}`
  }
  // And Pool limits' linked row, for the same reason (#134) -- and Pools' and
  // Holders' (#125, #128).
  if (keepsPool(r.sectionId, r.tab) && r.view) {
    return `${r.sectionId}/${r.tab}?${r.view}`
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
  // The Timeline's view is a view of one screen too: a filter change re-reads
  // inside the same scope, with the last drawing dimmed, rather than
  // beginning an empty one (#185).
  // Opening an output in the Artifacts pane is not a new screen either: the
  // pane's reads carry on under the viewer.
  return canonical({ ...r, list: null, view: null, artifact: null })
}

/** One address segment, decoded; a malformed escape is kept as written rather than thrown. */
function decodeSegment(seg: string): string {
  try {
    return decodeURIComponent(seg)
  } catch {
    return seg
  }
}

export function App() {
  const [at, setAt] = useState<Route>(fromLocation)

  // THE LIST ADDRESS THE AGENT WAS OPENED FROM (OV-10). Opening an agent
  // replaces the list address with the agent's, so without this the agent
  // closed to bare `/agents` while the list behind it still showed, say,
  // Recent · failed. Written during render, so the close address is never one
  // route behind; the write is idempotent, so a double render is harmless.
  const lastList = useRef<AgentList | null>(at.list ?? null)
  if (at.taskId === null) lastList.current = at.list ?? null

  /** The path a route is written as: an open agent sits under its list's tab. */
  const pathFor = useCallback(
    (r: Route) => addressToPath(canonical(r), r.list?.tab ?? lastList.current?.tab ?? 'live'),
    [],
  )

  // REAL ROUTES (rebrand 2026-10-01). Back and forward are `popstate`; an
  // old `#...` address -- typed, pasted, or an in-app `href="#..."` -- is
  // resolved by the hash router's grammar and then rewritten to its path.
  const go = useCallback(
    (to: string) => {
      let r: Route
      if (to.startsWith('/')) {
        const u = new URL(to, window.location.origin)
        const p = pathToAddress(u.pathname, u.search, u.hash)
        r = p === null ? fromAddress('') : fromAddress(p.address)
        if (p !== null && p.agentTab !== null) r = { ...r, list: { tab: p.agentTab, state: null } }
      } else {
        r = fromAddress(to.replace(/^#/, ''))
      }
      const path = pathFor(r)
      if (here() !== path) window.history.pushState(null, '', path)
      setAt(r)
    },
    [pathFor],
  )

  useEffect(() => {
    const onPop = () => setAt(fromLocation())
    const onHash = () => {
      if (isLegacyHash(window.location.hash, LEGACY_HEADS, window.location.pathname)) setAt(fromHash())
    }
    // A click on an old-style `href="#work/..."` link, or on a same-origin
    // path, is a navigation: pushed, so Back returns to where it was made.
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      const target = e.target as Element | null
      const a = target?.closest?.('a[href]') ?? null
      if (a === null || a.getAttribute('target') === '_blank' || a.hasAttribute('download')) return
      const href = a.getAttribute('href') ?? ''
      if (href.startsWith('#') && isLegacyHash(href, LEGACY_HEADS, window.location.pathname)) {
        e.preventDefault()
        go(href.slice(1))
      } else if (/^\/(?!\/|v1\/)/.test(href) && pathToAddress(href.split(/[?#]/)[0] ?? '') !== null) {
        e.preventDefault()
        go(href)
      }
    }
    window.addEventListener('popstate', onPop)
    window.addEventListener('hashchange', onHash)
    document.addEventListener('click', onClick)
    return () => {
      window.removeEventListener('popstate', onPop)
      window.removeEventListener('hashchange', onHash)
      document.removeEventListener('click', onClick)
    }
  }, [go])

  // NORMALISE, WITHOUT ADDING HISTORY. An old hash, an alias or a bare path
  // resolves to a route and the address bar is rewritten in place to the one
  // path that route has -- the ONE-TIME REDIRECT of every legacy hash. Copying
  // the link afterwards yields the current spelling, and Back does not walk
  // the reader through every alias they never typed.
  useEffect(() => {
    const want = pathFor(at)
    if (here() !== want) window.history.replaceState(null, '', want)
  }, [at, pathFor])

  // A NEW SCREEN'S READS START FROM NOTHING (CH-2). A LAYOUT effect, so it has
  // run before any screen's own `useEffect` issues a read. `pageKey` is the
  // route with its agent removed -- the list the agent is drawn beside, whose
  // reads carry on while the agent opens, switches tab and closes. The list's
  // own tab and state are not a new screen (OV-10); see `readsKey`.
  const screenKey = readsKey(at)
  const pageKey = at.taskId === null ? null : readsKey({ ...at, taskId: null })
  useLayoutEffect(() => {
    beginScreenReads(screenKey, pageKey)
  }, [screenKey, pageKey])

  // A TAB OR SEGMENT CLICK IS A ROUTE CHANGE, written by the normalise effect
  // with `replaceState`, so list clicks add no history entries.
  const onList = useCallback((list: AgentList) => {
    lastList.current = list
    setAt((r) => ({ ...r, list }))
  }, [])

  // THE TIMELINE'S AND THE WORKFLOWS LIST'S FILTERS ARE A ROUTE CHANGE TOO
  // (#185), written the same way.
  const onView = useCallback((view: string) => {
    setAt((r) => ({ ...r, view: view === '' ? null : view }))
  }, [])

  const [apiFailuresOnly, setApiFailuresOnly] = useState(false)

  const section = sectionOf(at.sectionId)
  // The `?` by the title asks the section's question; the Submit chooser is
  // Work's (its forms are Work tabs), so it asks Work's.
  const helpSection = section ?? (at.sectionId === SUBMIT ? sectionOf(WORK) : null)
  const inspector = at.taskId !== null
  const listAddress = canonical({
    sectionId: WORK,
    tab: 'running',
    taskId: null,
    taskPane: 'detail',
    list: lastList.current,
  })
  const tabDef = section?.tabs.find((t) => t.id === at.tab) ?? null
  const title =
    at.sectionId === SUBMIT
      ? 'Submit'
      : at.sectionId === HELP
        ? 'Help'
        : at.sectionId === REFERENCE
          ? REFERENCE_LABEL
          : (tabDef?.label ?? section?.label ?? 'SwarmCloud')

  return (
    // THE FRAME: the spine and the panel, and beside them the content column
    // -- the page that scrolls, then the dock as a ROW of its own under it
    // (§3.4, §11.3). A bar that occupies its height rather than painting over
    // whatever lands under it, and that runs under the content only: under
    // the spine it cut the avatar off at 1440x900 (#503).
    <div className="ctl-frame">
        <SkyShell
          section={spineOf(at.sectionId)}
          tab={at.tab}
          agentTab={at.list?.tab ?? lastList.current?.tab ?? 'live'}
          title={title}
          go={go}
          helpGroup={at.sectionId === HELP ? helpPageOf(at.tab) : null}
          apiFailuresOnly={apiFailuresOnly}
          onApiFilter={setApiFailuresOnly}
          dock={<Dock />}
          // AN UNSENT FORM IS KEPT ON A TENANT SWITCH (intake-tenants.html
          // 2A): the two submit forms re-read under the new tenant in place.
          keepOnSwitch={at.sectionId === WORK && (at.tab === 'new' || at.tab === 'new-workflow')}
          foot={
            // THE UTILITY CORNER: Help and API reads at the spine's foot,
            // links like every spine item (#503). `ctl-nav-util` is
            // load-bearing: test_nav_headings_agree.py reads this element to
            // find the API reads link and its label.
            <div className="ctl-nav-util sk-foot">
              <a
                className={`sk-ri${at.sectionId === HELP ? ' is-on' : ''}`}
                aria-current={at.sectionId === HELP ? 'page' : undefined}
                href={addressToPath(HELP)}
                onClick={(e) => {
                  if (!routedClick(e)) return
                  e.preventDefault()
                  go(HELP)
                }}
              >
                <Icon name="help" />
                <small>Help</small>
              </a>
              <a
                className={`sk-ri${at.sectionId === REFERENCE ? ' is-on' : ''}`}
                aria-current={at.sectionId === REFERENCE ? 'page' : undefined}
                href={addressToPath(REFERENCE)}
                onClick={(e) => {
                  if (!routedClick(e)) return
                  e.preventDefault()
                  go(REFERENCE)
                }}
              >
                <Icon name="api" />{REFERENCE_LABEL}</a>
            </div>
          }
        >
          {/* THE FRAME'S HEAD CARRIES THE SCREEN'S AGE (#98), so a `Screen`
              inside it prints none of its own while its read is fresh. */}
          <FrameAge.Provider value={true}>
          <SectionHelp.Provider value={helpSection === null ? null : <SectionQuestion section={helpSection} />}>
          <HeadAgeProvider at={at}>
          <div className={`app${inspector ? ' has-inspector' : ''}`}>
            <main className="work">
              <Head at={at} section={section} title={title} closeTo={listAddress} go={go} />

              {section === null ? (
                at.sectionId === HELP ? (
                  <HelpScreen topic={at.tab} />
                ) : at.sectionId === SUBMIT ? (
                  <SubmitChooser go={go} />
                ) : (
                  <ReferenceScreen failuresOnly={apiFailuresOnly} />
                )
              ) : (
                // THE PAGE (CH-2): its `Screen`s' reads stay its own while the
                // agent is open beside it (`RoutedPage` in Shell.tsx). And so
                // does its AGE (#98): with an agent open the head times the
                // inspector (`shownBy` in fetch.ts), so the list under it
                // keeps its own `read Ns ago` -- otherwise it would be shown
                // nowhere but the dock's tab-wide age.
                <RoutedPage.Provider value={true}>
                  <FrameAge.Provider value={at.taskId === null}>
                  <SectionBody
                    sectionId={section.id}
                    tab={at.tab}
                    taskId={at.taskId}
                    list={at.list ?? null}
                    onList={onList}
                    view={at.view ?? null}
                    page={at.page ?? null}
                    onView={onView}
                    go={go}
                  />
                  </FrameAge.Provider>
                </RoutedPage.Provider>
              )}
            </main>

            {at.taskId !== null && (
              <AgentSplit
                taskId={at.taskId}
                pane={at.taskPane}
                artifact={at.artifact ?? null}
                closeTo={listAddress}
                go={go}
                base={`${WORK}/task/${encodeURIComponent(at.taskId)}`}
              />
            )}
          </div>
          </HeadAgeProvider>
          </SectionHelp.Provider>
          </FrameAge.Provider>
        </SkyShell>
    </div>
  )
}

/** Where the address bar is now, in the form `pathFor` writes. */
function here(): string {
  return window.location.pathname + window.location.search + window.location.hash
}

/** The spine section a route belongs to. Submit lights Work, as its two
 *  forms do (submit.html M2: every frame lights Work). */
function spineOf(sectionId: string): SpineSection {
  switch (sectionId) {
    case 'overview':
      return 'overview'
    case WORK:
    case SUBMIT:
      return 'work'
    case CAPACITY:
      return 'capacity'
    case ADMIN_SECTION:
      return 'admin'
    case HELP:
      return 'help'
    case REFERENCE:
      return 'api'
    default:
      return null
  }
}

/**
 * THE HEAD (§B3): 44px, spanning the work area, above everything routed.
 *
 * Three things, and the third is the one that moved.
 *
 * 1. THE BREADCRUMB -- the trail TO the page, and the object if one is open
 *    (`crumbsOf`, #138). It is the only place the open agent's id appears
 *    outside the inspector itself, and it is what makes "back" legible: you
 *    can see what you would go back to, and every segment but the object is
 *    a link that goes there. It stops before the page the `<h1>` names.
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
 *    Help and API reads issue no reads of their own and say so. A screen
 *    that prints its own data's age claims it (`useClaimPageAge`, #98) and
 *    the head prints none; a `Screen` defers its fresh age to this one
 *    (`FrameAge` in Shell.tsx). One age per screen.
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
function Head({
  at,
  section,
  title,
  closeTo,
  go,
}: {
  at: Route
  section: SectionDef | null
  /** The page's own title -- the `<h1>` the screen draws under this head. */
  title: string
  /** Where closing the open agent goes: the list it was opened from. */
  closeTo: string
  go: (to: string) => void
}) {
  // THE AGE, computed once (`HeadAgeProvider`), and drawn on the screen's
  // own title row when a screen's page head takes it (`useHeadRowClaimed`):
  // the picked frames put the title, the meta chip and "read · poll ·
  // refresh" on ONE row (#503, "Page head"). A screen with no page head --
  // Overview, Help -- still has it here, beside the breadcrumb.
  const age = useContext(HeadAge)
  const onTitleRow = useHeadRowClaimed()

  const tab = section?.tabs.find((t) => t.id === at.tab) ?? null
  // THE SUBMIT PAGES' TRAIL IS SUBMIT (submit.html): the chooser is a page
  // of its own, not API reads (#503), and the two forms lead back to it.
  const submitForm = at.sectionId === WORK && (at.tab === 'new' || at.tab === 'new-workflow' || at.tab === 'new-issue')
  const head = at.sectionId === SUBMIT || submitForm ? 'Submit'
    : section?.label ?? (at.sectionId === HELP ? 'Help' : REFERENCE_LABEL)
  const home = submitForm ? SUBMIT : section === null ? at.sectionId : `${section.id}/${firstTab(section)}`
  const crumbs = crumbsOf({ at, head, home, tab, tabs: section?.tabs.length ?? 0, title, closeTo })

  // A SECTION PAGE HAS NO ROW HERE (visual QA Q2/Q10, 2026-10-02): its title,
  // meta, freshness and `?` are one row (`PageHead`), and a breadcrumb above
  // a page the panel already lights was a second, underlined copy of the nav.
  // The trail is drawn only inside an open object, where it is the way back.
  if (at.taskId === null) return null

  return (
    <div className="ctl-head">
      {crumbs.length > 0 && (
        <nav className="ctl-crumb" aria-label="Breadcrumb">
          {crumbs.map((c, i) => (
            <Fragment key={c.key}>
              {i > 0 && (
                <span className="ctl-crumb-sep" aria-hidden>
                  &#9656;
                </span>
              )}
              {c.to === null ? (
                <span className="id ctl-crumb-obj" aria-current="page">
                  {c.label}
                </span>
              ) : (
                <a
                  className="ctl-crumb-at"
                  href={`#${c.to}`}
                  onClick={(e) => {
                    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
                    e.preventDefault()
                    go(c.to!)
                  }}
                >
                  {c.label}
                </a>
              )}
            </Fragment>
          ))}
        </nav>
      )}

      {age !== null && !onTitleRow && <span className="ctl-head-age">{age}</span>}
    </div>
  )
}

/**
 * THE SCREEN'S READ AGE, FOR THE HEAD AND THE TITLE ROW (CH-2, #98, #503).
 *
 * Computed here once -- the screen's own reads, on the shared clock (CH-1) --
 * and handed down as `HeadAge`. The page head of the screen the frame times
 * (`PageHead` under `FrameAge`) draws it on its title row; when no screen
 * draws a page head, `Head` draws it beside the breadcrumb. A screen that
 * prints its own data's age (Platform counts, `useClaimPageAge`) gets none.
 */
function HeadAgeProvider({ at, children }: { at: Route; children: ReactNode }) {
  const reads = useSyncExternalStore(subscribeScreenReads, screenReadsSnapshot, screenReadsSnapshot)
  // The age is the point, so it moves on its own rather than only when a
  // fetch happens to land -- on the SHARED clock, the one every screen's
  // sub-line and the dock read, so the head and the provenance line under a
  // screen title can no longer disagree by up to a tick (CH-1).
  const now = useNow(AGE_TICK_MS)
  // A SCREEN THAT PRINTS ITS OWN DATA'S AGE (#98) -- Platform counts -- has
  // claimed it, and the head prints none: one age per screen.
  const claimed = usePageAgeClaimed()
  return <HeadAge.Provider value={claimed ? null : <ScreenAge at={at} reads={reads} now={now} />}>{children}</HeadAge.Provider>
}

/** One breadcrumb segment: a link to a place, or (`to: null`) the open object. */
interface Crumb {
  key: string
  label: string
  to: string | null
}

/**
 * THE TRAIL TO THE PAGE, NOT THE PAGE (#138). Every segment is a way back --
 * the section to its first page, the list to itself with the open agent
 * closed -- and the trail stops before the page the screen's own `<h1>`
 * names, because a crumb that repeats the title one line above it is the
 * same word twice. So Overview, Help and API reads (one page each) draw no
 * crumb at all; Capacity's Pools draws `Capacity`; an open agent draws
 * `Work ▸ Agents ▸ <id>`, where `Agents` closes it, and the id -- the one
 * thing on screen no heading names -- is the last segment and is not a link.
 */
export function crumbsOf({
  at,
  head,
  home,
  tab,
  tabs,
  title,
  closeTo,
}: {
  at: Route
  head: string
  home: string
  tab: TabDef | null
  tabs: number
  title: string
  closeTo: string
}): Crumb[] {
  const out: Crumb[] = []
  const open = at.taskId !== null
  if (open || head !== title) out.push({ key: 'section', label: head, to: home })
  if (tab !== null && tabs > 1 && (open || tab.label !== title)) {
    out.push({ key: 'tab', label: tab.label, to: open ? closeTo : `${at.sectionId}/${tab.id}` })
  }
  if (open) out.push({ key: 'object', label: at.taskId!, to: null })
  return out
}

/**
 * The screen's own read age, in the head (CH-2). See `Head` for the four
 * sentences; the rule under all of them is that the head never shows an age
 * for data this screen has not received -- not the frame's identity read, not
 * the screen you just left, not a success from another route of the tab.
 */
function ScreenAge({ at, reads, now }: { at: Route; reads: ScreenReads; now: number }) {
  // Help, API reads and the Submit chooser draw from nothing they fetch, so
  // there is no age -- and no "reading…" that never resolves (#503).
  if (at.sectionId === HELP || at.sectionId === REFERENCE || at.sectionId === SUBMIT) {
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
  view,
  page,
  onView,
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
  /** The Timeline's view, from the hash's query (#185), or null. */
  view: string | null
  /** The Timeline's page: Outcomes, or null for Lanes. */
  page: 'outcomes' | null
  /** Where the Timeline writes a new view; App turns it into the route. */
  onView: (view: string) => void
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
  // Timeline's second page, Outcomes (`/timeline/outcomes`): today's ledger, unchanged.
  if (`${sectionId}/${tab}` === 'work/timeline' && page === 'outcomes') return <ActivityScreen view={view} onView={onView} />
  switch (`${sectionId}/${tab}`) {
    case 'overview/now':
      return <OverviewScreen />

    case 'work/running':
      return <AgentsScreen {...agentsProps} />
    case 'work/workflows':
      return <WorkflowsScreen view={view} onView={onView} />
    case 'work/timeline':
      return <TimelineLanesScreen view={view} onView={onView} />
    case 'work/new':
      return <SubmitScreen />
    case 'work/new-workflow':
      return <SubmitWorkflowScreen />
    case 'work/new-issue':
      return <IssueSubmitScreen go={go} />
    case 'work/runs':
      return <RunsScreen view={view} go={go} />

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
function ReferenceScreen({ failuresOnly: asked = false }: { failuresOnly?: boolean }) {
  const probes = useSyncExternalStore(subscribeProbes, probeSnapshot, probeSnapshot)
  // The ages are the point, so they move on their own rather than only when a
  // fetch happens to land -- on the shared clock the head and the dock read.
  const now = useNow(AGE_TICK_MS)
  // The panel's "Failures only" row sets this too (Sky spine, API reads).
  const [failuresOnly, setFailuresOnly] = useState(asked)
  useEffect(() => setFailuresOnly(asked), [asked])
  const ordered = referenceOrder(probes)
  const shown = failuresOnly ? ordered.filter(failing) : ordered

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
      <PageHead title={REFERENCE_LABEL} meta="this tab only · not the API surface">
        <a className="ctl-link" href={`#${HELP}/api-reads`}>
          What these mean &rarr;
        </a>
      </PageHead>

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
          {/* THE SCOPE IS SAID ONCE (#140), in the head's `this tab only`. An
              h2 ("Routes called in this tab") and a caption ("N routes since
              this tab loaded") said it twice more; the count the caption
              carried is the `N of M` beside the toggle. */}
          <div className="ctl-toolbar">
            {/* `ctl-seg`, the product's pressed-state control: it draws the
                on state and takes the 44px phone target already. */}
            <div className="ctl-seg" role="group" aria-label="Rows">
              <button type="button" aria-pressed={failuresOnly} onClick={() => setFailuresOnly(!failuresOnly)}>
                failures only
              </button>
            </div>
            <span className="ctl-card-note ref-count">
              {shown.length} of {probes.length}
            </span>
          </div>
          {/* `is-scroll` (CH-13, design-system.md §7.3). This was `is-stacked`,
              for F6 of `docs/audits/2026-09-23/overflow-inventory.md`: five
              columns behind an `overflow-x: auto` that paints no scrollbar.
              The owner's rule for tables below 900px is that a DATA table --
              five or more columns, compared across rows -- scrolls with its
              first column held in view, and only a record of four columns or
              fewer stacks. This is a data table: the route stays pinned at the
              left edge while the outcome, latency and age scroll beside it. */}
          {shown.length === 0 ? (
            <p className="ctl-panel-note">no route is failing</p>
          ) : (
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
                  {shown.map((p) => (
                    <RouteRow key={p.path} probe={p} now={now} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
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

/**
 * How bad each tone is, worst first: the order API reads draws its rows in
 * (#140). The admin 403 ranks with `is-info` -- the expected answer for a
 * non-admin, below anything paused and above a clean read.
 */
const RANK: Readonly<Record<string, number>> = { 'is-bad': 0, 'is-warn': 1, 'is-info': 2, 'is-ok': 3 }

/** A read that failed. The admin 403 is not one: it is the answer expected. */
function failing(p: ProbeRecord): boolean {
  return describeProbe(p).row !== undefined
}

/**
 * THE SCREEN'S ORDER, NOT THE REGISTRY'S (#140). `probeSnapshot` is sorted by
 * route, which is right for a registry and wrong for a reader who opened this
 * page because something did not load: with twenty routes read, the failing
 * one sat wherever its name sorted. So the worst outcome is first, and within
 * one outcome the oldest payload is -- `never` before any age, because a
 * panel that has never had a payload is the one most likely to be showing
 * nothing. The route name breaks what is left, so the order is stable.
 */
function referenceOrder(probes: readonly ProbeRecord[]): ProbeRecord[] {
  const rank = (p: ProbeRecord) => RANK[describeProbe(p).tone] ?? 0
  // `never` as zero: every real success is an epoch millisecond after it.
  const success = (p: ProbeRecord) => p.lastSuccessAt ?? 0
  return [...probes].sort(
    (a, b) => rank(a) - rank(b) || success(a) - success(b) || a.path.localeCompare(b.path),
  )
}
