/**
 * THE ONE PLACE AN EXPLANATION IS WRITTEN.
 *
 * The owner directive (docs/web-ui/redesign-v2.md §9, restated 2026-09-22) is
 * that the app carries no prose: help lives in a dedicated Help section, and
 * everywhere a reader needs a sentence there is a `?` that houses it.
 *
 * THE LINE THIS MODULE IS ON THE WRONG SIDE OF, AND WHY THAT IS SAFE
 * (docs/web-ui/ui-audit-and-build-prompt.md §B7.1). This UI's best property is
 * implemented as prose, so a naive removal deletes it. The split is:
 *
 *   THE FACT stays on the surface, always, with a marker nobody has to hover
 *   to see -- that a read failed, that a response was partial, that a figure
 *   is unmeasured, that a total is withheld.
 *
 *   THE EXPLANATION -- why the em dash, what would have written the number,
 *   what may not be concluded -- lives HERE.
 *
 * A long form says what the reader sees, what it means and what to do. WHY
 * the console draws a state the way it does -- the screen that would have
 * lied -- is design rationale, and it lives in docs/web-ui/help-rationale.md
 * (#131), held there by `src/__tests__/help.rationale.test.tsx`.
 *
 * So nothing in this file may be load-bearing. If a panel needs this module to
 * be read before it can be told apart from a zero, the panel is wrong, not
 * this module. `tests/help.test.ts` asserts that for the exemplar screen.
 *
 * ONE RECORD, TWO RENDERERS. `<HelpCard topic="x">` renders `short`; the Help
 * section renders `title` + `long` at `anchor`. Both read this record, so a
 * topic cannot exist in the card and be missing from the section -- the shape
 * the brief asked for, and the shape a dangling `#help/...` link cannot
 * survive. `anchor` is DERIVED from the id below rather than typed out, so the
 * two cannot drift apart by a keystroke.
 *
 * ------------------------------------------------------------------------
 * IT RESTATES NO FROZEN VALUE, AND THAT IS ENFORCED, NOT PROMISED.
 * ------------------------------------------------------------------------
 * `scripts/lib/check-contract-parity.sh` exists because every restatement in
 * this repository has since drifted. It covers shell and jq. It does NOT read
 * TypeScript, so a state name, a park reason or a limit spelled out in the
 * prose below would drift in total silence -- help text is the last place
 * anyone thinks to check when the contract moves.
 *
 * Therefore: no topic's prose contains a task state, a park reason, a pool
 * name or a figure. Where a topic needs one it declares `values()`, which
 * READS the list from the module that owns it (`types.ts`, this UI's single
 * mirror of `apps/common/swarm_common`) at render time. Rename a state there
 * and the Help section renames with it. `tests/help.test.ts` greps this file's
 * source for every state and reason literal and fails if one appears, so the
 * rule survives the next person who adds a topic in a hurry.
 */

import {
  CARRIER_DETAIL,
  CONCURRENCY_STATES,
  DISPATCH_CARRIERS,
  DISPATCH_STRATEGIES,
  QUOTA_STALE_AFTER_MS,
  QUOTA_SWEEP_INTERVAL_SECONDS,
  REAL_STATES,
  STRATEGY_LABEL,
  TERMINAL_STATES,
  type TaskState,
} from './types'

/**
 * Every topic. Adding a member here without adding an entry to `TOPICS` is a
 * type error, which is the point: `Record<TopicId, ...>` is exhaustive.
 */
export type TopicId =
  | 'absent-vs-zero'
  | 'account-label-rules'
  | 'account-owned-by-one-tenant'
  | 'account-removal-is-reversible'
  | 'account-states'
  | 'accounts-table-shape'
  | 'admin-gate-not-failure'
  | 'advisory-vs-lease'
  | 'all-clear-basis'
  | 'ambiguous-write'
  | 'api-reads'
  | 'attempt-documents'
  | 'binding-window'
  | 'blockers-at-an-instant'
  | 'capacity'
  | 'ceiling-change-evicts-nothing'
  | 'catalogue-from-route'
  | 'checkpoints'
  | 'clipboard-secure-context'
  | 'cpu-figures'
  | 'credential-names-not-values'
  | 'credential-refresh-sweep'
  | 'credential-split'
  | 'declared-vs-resolved-backend'
  | 'dispatch-absent-is-old-api'
  | 'dispatch-carrier'
  | 'dispatch-strategies'
  | 'event-paging'
  | 'failure-classes'
  | 'input-as-submitted'
  | 'input-is-opaque'
  | 'integrate-needs-final-step'
  | 'lease-and-pool-are-two-records'
  | 'lending'
  | 'lending-narrows-isolation'
  | 'lent-account'
  | 'masking-is-serve-time'
  | 'never-assigned-pool'
  | 'no-amber-band'
  | 'not-a-machine-inventory'
  | 'oom-near-miss'
  | 'outcome-buckets'
  | 'outcome-explained'
  | 'park-on-missing-credential'
  | 'partial-read'
  | 'paused-vs-full'
  | 'peak-memory'
  | 'platform-counts'
  | 'poll-cadence'
  | 'pool-freshness'
  | 'pools-all-at-once'
  | 'projected-not-measured'
  | 'provider-defines-windows'
  | 'provider-quota-states'
  | 'quota-document-absent'
  | 'quota-row-fields'
  | 'read-failed'
  | 'reauth-does-not-unpause'
  | 'reauth-required'
  | 'refresh-now-probe'
  | 'refresh-token-required'
  | 'repository-url'
  | 'requests-are-ceilings'
  | 'room-unknown-not-zero'
  | 'runner-unavailable'
  | 'recent-submissions'
  | 'runner-profile-by-name'
  | 'runtime-needs-no-provider'
  | 'second-browser-application'
  | 'sign-in-not-paste'
  | 'strategy-on-a-task'
  | 'signin-201-no-name'
  | 'signin-deadlines'
  | 'signin-holds-label-and-lending'
  | 'signin-is-anthropics-page'
  | 'signin-is-over'
  | 'signin-keeps-readings'
  | 'signin-paste-the-code'
  | 'signin-route-missing'
  | 'signin-still-open'
  | 'signin-verify-by-reload'
  | 'skipped-for-this-tenant'
  | 'state-change-reason'
  | 'states'
  | 'subscription-only-no-api-key'
  | 'success-rate'
  | 'tenant-fields'
  | 'tenant-scope'
  | 'token-cost'
  | 'tokens-reported'
  | 'units-not-agents'
  | 'unreadable-documents'
  | 'what-a-pool-is'
  | 'what-sets-it-apart-is-arithmetic'
  | 'withheld-total'
  | 'workflow-stages'
  | 'workspace-memory'

/** The Help section's headings, in the order it renders them. */
export type HelpGroupId =
  | 'reading-a-figure'
  | 'the-platform'
  | 'an-attempt'
  | 'the-catalogue'
  | 'submitting-work'
  | 'an-account'

export const HELP_GROUPS: readonly { id: HelpGroupId; title: string }[] = [
  { id: 'reading-a-figure', title: 'Reading a figure' },
  { id: 'an-attempt', title: 'Reading an attempt' },
  { id: 'the-platform', title: 'How the platform behaves' },
  { id: 'the-catalogue', title: 'The runtime catalogue' },
  { id: 'submitting-work', title: 'Submitting work' },
  { id: 'an-account', title: 'Running the account pool' },
]

/**
 * A value a topic needs, read from whoever owns it.
 *
 * `term` is the name as its owner spells it today. Nothing here may be a
 * string literal typed into this file.
 */
export interface HelpValue {
  term: string
  note?: string
}

export interface HelpTopic {
  /** The heading, in the card and in the Help section. Identical in both. */
  title: string
  /**
   * What the card shows. At most 60 words (§B7.2), asserted by the tests --
   * a card long enough to need scrolling is a paragraph that moved house.
   */
  short: string
  /** The Help-section route, WITHOUT the leading `#`. Derived, never typed. */
  anchor: string
  /** The long form, one string per paragraph. */
  long: readonly string[]
  /** Values read from their owning module at render time. See the header. */
  values?: () => readonly HelpValue[]
  /**
   * Topics this one sends the reader on to, drawn as links under the long
   * form on the Help page (AH-21: the budget paragraph cross-links
   * `token-cost`). Typed as `TopicId`, so a renamed topic is a compile error
   * here rather than a dead `#help/...` link on the page.
   */
  see?: readonly TopicId[]
  group: HelpGroupId
  /**
   * The topic's name, two to six words: what the Help index and every footer
   * index print (#130, #131). Not the claim -- that is `title`.
   */
  subject: string
  /** What to do, or where to look: the last thing the topic draws (#131). */
  act: HelpAct
}

/** The route the Help section lives at. One spelling, used by the router. */
export const HELP_ROUTE = 'help'

/** `#help/absent-vs-zero`. The only place this string is built. */
export function helpAnchor(topic: TopicId): string {
  return `${HELP_ROUTE}/${topic}`
}

type TopicSpec = Omit<HelpTopic, 'anchor' | 'subject' | 'act'>

const SPECS: Record<TopicId, TopicSpec> = {
  // REWRITTEN FROM design-system.md §8.6 (AH-2). The old text taught an
  // encoding the screens do not draw: that a never-measured figure is "a
  // phrase, on a dashed tile", and that the two absences differ by colour.
  // What tells the kinds apart is the mark and its words; the dashed edge is
  // a second channel under them, and colour is never the only one.
  'absent-vs-zero': {
    group: 'reading-a-figure',
    title: 'Absent is not zero',
    short:
      'A digit is a measurement, and a measured zero is a digit too. A dash, or the hatched mark “not measured”, means nothing ever recorded the figure. The dashed mark “not read” means the read failed and the platform may still hold it. A tilde marks a real reading too old to trust.',
    long: [
      'Every number on these screens is one of four things: measured, never measured, not read, or measured too long ago. They are different facts, and each is drawn with a mark of its own.',
      'A measured figure is a digit — including a measured zero, which is a real result: on a bar it is a tick at the origin, and where a whole panel is empty it is the solid mark “real zero”.',
      'A figure nothing ever recorded is a dimmed dash (—), or the hatched mark “not measured”. It is never drawn as a zero, because a zero is a claim about a measurement nobody has.',
      'A figure a failed read left behind is the dashed mark “not read”, and no number appears beside it: the platform may well hold the figure, and the read that should have brought it did not.',
      'A tilde (~) in front of a figure means the reading is real but older than it can be trusted to describe now.',
      'The dashed edge on a tile that holds no figure — the metric tiles and the Timeline’s reported cost alike — is a second channel for the same fact, not the fact itself. The mark and its words are what carry the distinction, so it survives greyscale and a screenshot; colour never carries it alone.',
      'The same rule runs through the bars: a track whose ceiling could not be read is hatched with no fill, because an empty plain track reads as “0% used” — a claim about a measurement nobody has.',
    ],
  },

  'api-reads': {
    group: 'the-platform',
    title: 'The reads behind a screen',
    short:
      'The bar at the foot of every screen summarises the routes this browser tab has called. The age it shows is of the newest SUCCESSFUL payload, not of the newest attempt \u2014 which is the part that tells a stale panel from a healthy one.',
    long: [
      'Every read this tab makes registers in one place: which route, what happened to the last attempt, how long that attempt took, and when the route last actually produced a payload. The dock at the foot of the window is one line off that registry, and it opens into a cell per route.',
      // CH-18. The registry was keyed by URL, so every opened task added a
      // "route" of its own; it is keyed by the path template now, and this is
      // what makes "one record per route" true. The decision's wording ended in
      // "(… the head shows the screen's own read, CH-2)"; the box id is left
      // out of product copy and the fact kept.
      'A route is a path template with ids and query removed. Its status is the last attempt of any call to it, and its age is the newest successful payload of any call to it; each panel still carries its own age and its own failure, and the head beside the page title shows the screen’s own newest read.',
      'The age is the load-bearing number, and it is the age of the last SUCCESS. A panel drawn from a figure four minutes old, whose route has been failing for three of them, is indistinguishable from a healthy panel \u2014 the figure is still on screen, still formatted as a measurement, and nothing on the panel itself has changed. The age is the only thing that says otherwise.',
      'The p95 on the collapsed line is taken over the last attempt of each route: one sample per route, not one per request. This tab keeps no request history, so a percentile over every request made is not something it could compute.',
      'A 403 on an admin-only route is counted apart from failures. Someone who is not an admin genuinely cannot read those routes, so a 403 there says who is reading, not that anything is broken.',
    ],
  },

  'attempt-documents': {
    group: 'an-attempt',
    title: 'When an attempt document exists',
    short:
      'An attempt document is written when capacity is reserved, not when a task is submitted. A task waiting for capacity has none, and that is a real zero rather than a gap in the record.',
    long: [
      'The unit of the agent screen is the attempt, not the task. Each attempt carries its own runtime, its own requested-against-used figures, its own tokens and cost, its own checkpoints and its own error.',
      'The document is created at dispatch, and the task’s own attempt counter is incremented inside the same admission transaction. So the counter and the documents should agree, and when they do not the difference is the interesting part: a task that counts attempts whose query returned fewer documents has a hole in its record, and that is true of a running task as much as a finished one.',
      'A task that counts no attempts and returned no documents is consistent, and its empty attempt list is a measurement rather than a failure.',
    ],
  },

  // G2-10 (QA 2026-10-07): THE INSPECTOR'S CARDS GET TOPICS OF THEIR OWN.
  // The Outcome card's `?` opened `attempt-documents` (when an attempt
  // document is written), the Input card's opened `input-is-opaque` ("whatever
  // you type"), and the strategy chips' opened `dispatch-strategies` ("the
  // number on each option is computed from the steps on the form"): each a
  // Submit-side or unrelated text on a screen where nothing is typed and no
  // form is drawn. These three say what the reader is looking at.
  'outcome-explained': {
    group: 'an-attempt',
    title: 'What the outcome describes',
    short:
      'The outcome is what the task left when it finished: the pull request or branch it published, the files it uploaded and the answer it wrote. It is written once, when the task ends, so on a task that ran more than once it describes the last attempt only.',
    long: [
      'The outcome is read from the task’s result summary, which the worker writes once, at the end. It names what was published — a pull request, a branch, or nothing when the strategy publishes nothing — and counts the files uploaded as artifacts.',
      'On a task that ran more than once, every part of it belongs to the last attempt. The earlier attempts’ output was never summarised, which is why a retried outcome is marked as the last attempt of several.',
      'The files themselves are on the agent’s Artifacts tab, where each can be viewed or downloaded.',
    ],
    see: ['attempt-documents'],
  },

  'input-as-submitted': {
    group: 'an-attempt',
    title: 'The input, as it was submitted',
    short:
      'This is the input the task was submitted with, as the API serves it: masked where a value matched a credential pattern, otherwise unchanged. The platform handed it to the agent without reading it and checked nothing about it but its size.',
    long: [
      'The input is shown as the platform stored it at submission. The agent decided what it meant; the platform only carried it.',
      'Values that look like credentials are masked when the input is served, and the number masked is shown beside the prompt. Masking changes the copy that is served, not the stored input.',
      'The repository, model, priority and timeout beside it are the submission’s own fields. The image, the command and the resources come from the runner profile, never from the input.',
    ],
    see: ['masking-is-serve-time', 'runner-profile-by-name'],
  },

  'strategy-on-a-task': {
    group: 'an-attempt',
    title: 'What the task was asked to publish',
    short:
      'The chips name the dispatch strategy the task was submitted with — publish nothing, one pull request per step, or one for the whole workflow — and, in a workflow that integrates, this step’s role. What was actually published is the outcome, once the task ends.',
    long: [
      'The strategy is fixed when the task is submitted and does not change while it runs.',
      'Under the integrating strategy each step has a role: a contributor pushes its branch and opens nothing, and the integrator opens the one pull request for the whole workflow.',
      'The strategy is what was asked for. Whether a pull request was opened, and which one, is read from the task’s result summary once it ends.',
    ],
    // READ, not restated, as `dispatch-strategies` reads them.
    values: () =>
      DISPATCH_STRATEGIES.map((s) => ({ term: s, note: STRATEGY_LABEL[s] })),
    see: ['outcome-explained'],
  },

  // AG-19. The Attempts toolbar's `?` opened "One message belongs to one
  // failure" -- a topic about rollups keeping one error -- beside marks that are
  // about how much of the event history this screen holds.
  //
  // THE FIRST VERSION BLAMED THE ROUTE, AND THE ROUTE IS NOT THE LIMIT. It was
  // built from AttemptTimeline.tsx's `say` strings, which said the events route
  // "returns no page token" and that newer events "cannot be fetched at all".
  // Both went stale with #19: `GET /v1/tasks/{id}/events` returns
  // `next_page_token` whenever more events exist and takes `order=desc`
  // (swarm_api/routes/tasks.py, `list_events`). What is true is narrower and is
  // about the UI: api.ts asks for one page, oldest-first, and never sends the
  // token back. The toolbar's strings now say that too, so the card and the
  // marks still agree, and tests/help.test.ts reads both the route and api.ts
  // and holds this topic to whichever of them changes.
  'event-paging': {
    group: 'an-attempt',
    // ONE SCREEN LINKS HERE AGAIN (#185). AG-19 had the Timeline's task
    // window link here too, and AH-13 rule 2 made the title and the opening
    // name both reads. The Timeline no longer reads a window of tasks -- it
    // reads `GET /v1/outcomes` over a real span, and the Rows control is
    // retired -- so the topic is written for the attempt timeline alone, and a
    // paragraph describing a window nothing draws is not left behind.
    title: 'One page of events, not the whole history',
    short:
      'This screen reads one page of events, oldest-first, and does not follow the page token the events route returns, so newer events may exist that it has not fetched. An attempt with none on this page is blind, not quiet. Zero events is a failed query: a task is written with its first event.',
    long: [
      'The attempt timeline in an agent’s inspector reads one page of events, oldest first, and stops there.',
      'The events route pages: each response carries a token for the next page whenever more events exist, and it can also be asked for the newest events first. The attempt timeline in an agent’s inspector uses neither. It asks for one page, oldest-first, and stops there, so what it holds is the beginning of a history, never a guaranteed whole of it.',
      'So \u201cthis is everything\u201d is a claim the attempt timeline is never entitled to make. An attempt with no events on the page is counted as blind rather than drawn as quiet: past one page, the newest events \u2014 everything belonging to the latest attempts \u2014 are on the platform and not in the timeline.',
      'Zero events is a different fact again. A task is written together with its first event, in the same batch, so an empty history is a failed query and is marked as one \u2014 never as an empty record.',
    ],
  },

  // AG-19. The `?` beside `masked N` opened "Credential names, never values",
  // which is about the runtime catalogue publishing variable NAMES. What the
  // artifact viewer's count needs explained is that masking happens on the
  // way out and leaves the stored object untouched -- the `say` on its mark.
  //
  // AND THAT THE COUNT IS OVER THE WINDOW SERVED, NOT THE OBJECT. inspect.py
  // runs `redact()` over the bytes of this read and reports that call's count
  // as `redaction_count`; a large artifact is served a window at a time and the
  // viewer marks it `partial`. The first version called the count "the number
  // of values hidden in this copy", which a zero on a partial read is not.
  'masking-is-serve-time': {
    group: 'an-attempt',
    title: 'Masking happens when an artifact is served',
    short:
      'Credential-shaped values are masked on the way out, when an artifact is served. The object in the bucket is unchanged and still holds them, so a count above zero means rotate what was found. The count covers only the bytes this read served, and zero means nothing in them matched the rules, not that nothing secret is there.',
    long: [
      'Masking is a property of the serving path, not of the artifact. The viewer reads the object, replaces every value that matches one of its rule families, and sends the result; the object in storage is never rewritten.',
      'So the count beside an artifact is the number of values hidden in the part of it this read served. Every one of them is still in the bucket, readable by anything with access to it, and anything recognisable should be rotated.',
      'It covers that part and nothing past it. A large artifact is served a window at a time, and the viewer marks such a read partial beside its byte count: the masked count is over those bytes, and the rest of the object has not been checked by this read at all.',
      'A count of zero is measured against the rules, not against the content: it says no value in the bytes served matched a known family of credential, which is not the same as the artifact holding nothing sensitive.',
    ],
  },

  capacity: {
    group: 'the-platform',
    title: 'What reserves capacity',
    short:
      'Only these states create infrastructure demand. Capacity for a task is reserved all-or-nothing across every pool it needs, in one transaction, and is counted from the moment it is held rather than from the moment an agent starts.',
    long: [
      'Capacity is reserved for a whole task across every pool it needs, in a single transaction, or not at all. A partial reservation would let a task hold a slot in one pool while queueing for another, which is how a platform deadlocks against itself.',
      'Concurrency is counted from the moment capacity is held, not from the moment an agent starts running — the gap between the two is real, and counting from the later one lets the platform admit work it has already promised away.',
      'The states below are the ones that create demand. The rest are waiting states, and a task sitting in one costs nothing.',
    ],
    // READ, not restated. Rename a state in types.ts and this list follows.
    values: () =>
      orderedStates(CONCURRENCY_STATES).map((s) => ({
        term: s,
        note: 'holds a slot in every pool the task needs',
      })),
  },

  checkpoints: {
    group: 'an-attempt',
    title: 'What a checkpoint is, and is not',
    short:
      'Checkpointing is mandatory and periodic: it is what makes a lost attempt cost minutes instead of everything. What is inside one is recorded nowhere — only its id, its size and its uri.',
    long: [
      'A worker can lose its attempt to a quota park-and-exit, a cancellation, a reclaim of a stale generation, or an ordinary crash. A checkpoint is what makes any of those cost minutes rather than the whole attempt, which is why it is mandatory and periodic rather than a nicety.',
      'Nothing writes a manifest of an archive’s contents, so no screen can list the files in a checkpoint. The id, the size and the uri are the whole record, and the uri is what to fetch.',
      'A checkpoint count of zero is a measured zero — it means no attempt document lists one. It is not a failed read, and it is shown as a digit for that reason.',
    ],
  },

  // WAS `cpu-not-sampled`, "CPU is never sampled", and it stayed in the card
  // foot's index directly under the two CPU rows it contradicted after #187
  // started drawing them (#222, post-deploy QA of 2026-09-26). The worker
  // measures CPU; this says what the two rows are.
  'cpu-figures': {
    group: 'an-attempt',
    title: 'CPU is peak and mean cores of the limit',
    short:
      'The worker samples the container’s CPU while the agent runs and writes the attempt’s figures on its own record: the busiest sampling interval (peak) and the CPU-seconds over the runner’s wall time (mean), both in cores, against the limit it read from the kernel or took from the class.',
    long: [
      'The figures are the attempt’s, every runner it started combined. Peak is the busiest sampling interval; mean is the CPU-seconds divided by the time the runners ran, so the setup, the clone and retry waits are not counted as idle.',
      'The limit is the container’s cgroup `cpu.max` when the kernel says one (`cgroup limit`), else the catalogue cpu of the class the container was sized with. An attempt from before the limit’s source was recorded says `reported limit` when the figure is not its class’s.',
      'While the attempt runs the figures are a live reading, rewritten with each periodic reading, and its age is the API’s own measure of when the worker last wrote it. An attempt from before that time was recorded says `age not recorded`. An em dash is a figure nobody measured, never a zero.',
    ],
  },

  'oom-near-miss': {
    group: 'an-attempt',
    title: 'The near-miss flag is the claim; the colour is not',
    short:
      'The warning colours on a memory bar are presentation, so a tall bar is visible before you reach the flag. The claim that an attempt came close to its ceiling is the near-miss flag, set by the worker against the counters the kernel’s OOM killer reads.',
    long: [
      'The worker sets the near-miss flag from its own threshold against the cgroup counters the kernel’s OOM killer itself reads. That is the measurement.',
      'The warn and bad colouring on the utilisation bars is a presentation threshold chosen so a tall bar catches the eye before the reader reaches the flag. It is not a second opinion about whether the attempt was in danger, and it never contradicts the flag.',
    ],
  },

  // PENDING, NOT ABSENT, WHILE THE ATTEMPT IS OPEN (AG-4). The old card said a
  // running attempt "has none, so the figure is absent" -- the encoding for
  // "nothing will ever record this", on a figure the worker is about to write.
  'peak-memory': {
    group: 'an-attempt',
    title: 'When peak memory is written',
    short:
      'Peak memory is written when an attempt ends. While an attempt is still running the figure is pending: not written yet, which is not the same as never recorded. An attempt that ended without writing one is absent rather than zero, and may have used any amount at all.',
    long: [
      'The worker writes peak resident memory at the end of an attempt, from the sampler it ran throughout. An attempt that is still going has not written it yet, so its figure is pending — a reading still to come, beside a heartbeat that says the attempt is alive — and not an absence.',
      'A run-level peak is the worst single attempt, not a total across attempts, and the tile says so. A total would be meaningless: three attempts of 600 MiB each did not use 1.8 GiB at any moment.',
      'When the attempts have ended and none of them wrote one, the tile shows a phrase rather than a zero. A zero would assert the run used no memory, which is the one thing that cannot be true.',
    ],
  },

  'read-failed': {
    group: 'reading-a-figure',
    title: 'A failed read is not an empty result',
    short:
      'When a query fails, no number derived from it appears anywhere — not a zero, not a blank. A failed read says nothing about the platform, and the panel says which of the two happened.',
    long: [
      'A failed read is not an absence, and it is never drawn as one. A sweep of the platform’s operational scripts found 56 places where a read failure was printed as “nothing to report”, including a status tool that said “no services deployed” when a session had simply expired.',
      'So every read here returns a value that forces the question. There is no path to the rows that does not decide, separately, what an empty answer means and what a missing answer means. A component cannot render an empty list for a permission error, because a permission error never produces a list.',
      'On screen the two are told apart without colour: a failed panel carries its own marker and its own heading, and no figure under that heading may be treated as a measurement.',
    ],
  },

  'requests-are-ceilings': {
    group: 'the-platform',
    title: 'A request is a ceiling, not a target',
    short:
      'Requests equal limits platform-wide. There is no bursting, so nothing absorbs an overshoot. A bar at 90% is not “well utilised”; it is one long prompt away from being killed.',
    long: [
      'Every workload on this platform is deployed with its request equal to its limit. That is deliberate and it is platform-wide.',
      'The consequence is that headroom shown on a utilisation bar is the only headroom that exists. There is no burst budget, no shared pool to borrow from, and no grace above the line — a workload that crosses it is killed rather than throttled.',
      'So a bar approaching its ceiling should be read as a risk, not as efficiency.',
    ],
  },

  states: {
    group: 'the-platform',
    title: 'The states a task can be in',
    short:
      'These are the states a task document can actually hold. Several members of the underlying enum are never written to one, so they are decoded when they arrive but never offered here as a filter or a bucket.',
    long: [
      'Several members of the state enum are never written to a task document: some are creation states the real state machine walks through without storing, one belongs to workflows rather than tasks, and one is reachable only through a branch nothing calls.',
      'They are still decoded when they arrive, because decoding a string is not the same as offering it as a choice — but they are never offered as a filter, a legend entry or a histogram bucket. A bucket that cannot fill reads as “nothing is broken” rather than “this cannot happen”, which is the wrong lesson to teach at 3am.',
      'The states a task document can actually hold are listed below, each marked as reserving capacity, waiting, or finished.',
    ],
    // READ from types.ts. Nothing below is typed into this file.
    //
    // THREE NOTES, NOT TWO (AH-3). Everything outside the concurrency set used
    // to read "waiting -- costs nothing", which labelled a finished task as
    // waiting: right about the cost, wrong about the state. The third note is
    // derived from TERMINAL_STATES, the same set every screen tests against.
    values: () =>
      REAL_STATES.map((s) => ({
        term: s,
        note: CONCURRENCY_STATES.has(s)
          ? 'reserves capacity'
          : TERMINAL_STATES.has(s)
            ? 'finished \u2014 holds nothing'
            : 'waiting \u2014 costs nothing',
      })),
  },

  'token-cost': {
    group: 'reading-a-figure',
    title: 'Token cost is the only cost there is',
    short:
      'There is no billing integration, so the infrastructure cost of a run is recorded nowhere this app can read. It is missing, not zero. A run whose cost is absent may have been expensive.',
    long: [
      'Only the CLI runners report token spend at all, and every attempt that ran before the worker’s capture shipped carries nothing for all of its spend fields. So an absent cost has two innocent causes and one alarming one, and none of them is “this run was free”.',
      'A mock task costs nothing on purpose. A result nobody could parse cost an unknown amount. Rendering both as a zero would lie about one of them, so an unreported cost is a phrase and never a figure.',
      'Each half of a token count is summed separately, because a runner can report input tokens and no output. Treating the missing half as a zero would quietly understate the total and caption it as if both halves were in it.',
      'No cost figure in this console includes compute, storage or database cost. No billing integration exists to read them from.',
    ],
  },

  'tokens-reported': {
    group: 'reading-a-figure',
    title: 'Who reports a token count',
    short:
      'Only some runners report tokens, and attempts that ran before the worker’s capture shipped report none. A run with no token count is not a run that used no tokens.',
    long: [
      'Token counts come from the runner, through the worker, and only the CLI runners produce them. An attempt from any other runner carries none.',
      'Attempts that ran before the worker’s token capture shipped also carry none, so the absence is common on older runs and says nothing about them.',
      'Where a total exists, the caption names how many attempts of the run contributed to it, and which halves — input, output, or both — are in it.',
      'The Timeline’s Reported cost sums the cost each attempt reported, every attempt of every task, and places it in the bucket where the task ended, so the cost of work still running appears once that work ends. A sum over fewer attempts than ran carries the partial mark and says how many reported. It is the runners’ own report and not a bill: no compute, storage or database cost is in it.',
    ],
  },

  'workspace-memory': {
    group: 'the-platform',
    title: 'The workspace is a slice of memory',
    short:
      'The workspace is a memory-backed filesystem, not capacity on top of the memory ceiling. Workspace bytes come out of the same allowance the agent runs in.',
    long: [
      'The Terraform provider cannot express the disk-backed workspace this platform originally wanted — the field it would need accepts only the memory-backed form — so the workspace is a tmpfs.',
      'That has one consequence worth stating plainly: files the agent writes consume the runtime’s memory ceiling, the same ceiling an attempt’s memory bar is drawn against. A workspace is not extra capacity, and a large checkout is indistinguishable, to the OOM killer, from a large process.',
      'The path this leaves the deployment on is the fully-supported one, which does support live migration. That covers infrastructure moves. It does not cover a cancellation, a reclaim or a crash, which is why checkpointing is still mandatory.',
    ],
  },

  // -------------------------------------------------------------------------
  // Reading a figure
  // -------------------------------------------------------------------------

  'projected-not-measured': {
    group: 'reading-a-figure',
    title: 'A tilde means projected, not measured',
    short:
      'A figure marked with a tilde is real but not current: either the reading behind it is older than the platform trusts, or the window it describes has already refilled. A figure carrying no mark is a claim that it is current.',
    long: [
      'A reading is a measurement taken at an instant and reported by a worker. It does not keep itself up to date, so at some age it stops describing now and starts describing then.',
      'These accounts are also used by a person at a laptop, so utilisation moves without the platform seeing any of it. That is why an old reading is marked rather than quietly shown: the number is real, it is simply about an earlier moment.',
      'The other case is a window that has passed its reset. The figure describes the window before it and says nothing about the one running now, so it carries the same mark for the same reason.',
    ],
  },

  'partial-read': {
    group: 'reading-a-figure',
    title: 'One message belongs to one failure',
    short:
      'When several reads fail, only the first error is kept. Attaching that sentence to all of them would present unrelated failures as one cause and send you after the wrong one. So the count is exact and the message names the single read it came from.',
    long: [
      'A rollup that fans out across many reads keeps one error, because keeping every error would mean keeping every response it could not use.',
      'The count of failures is exact; the explanation is not, for any failure but the one it came from. Presenting a missing document, a rate limit and a server fault as three instances of whichever resolved first is how an operator spends an afternoon on the wrong cause.',
      'So the panel states how many reads failed, attributes the one message it holds, and says plainly that the others are unexplained rather than explained wrongly.',
    ],
  },

  'binding-window': {
    group: 'reading-a-figure',
    title: 'Utilisation is of the binding window',
    short:
      'Never an average of the two. An account nearly empty on its short window and nearly full on its long one is stopped by the long one, and refilling the short one does nothing for it. The countdown is to whichever window will refuse first.',
    long: [
      'A provider reports more than one window, and an account is refused as soon as any one of them is spent. Averaging them would produce a number that no refusal corresponds to.',
      'So every utilisation figure on an account row is of the window that binds, and the countdown beside it counts down to that window rather than to the nearest one.',
      'Which window binds is itself derived from readings, so when those readings are old the choice of window is old too, and the countdown carries the projected mark for that reason.',
    ],
  },

  'all-clear-basis': {
    group: 'reading-a-figure',
    title: 'An all-clear names its basis',
    short:
      'A short problem list over checks that all ran and a short list over checks that could not run are the same picture, and only one of them is good news. So the panel always says how many ran, and a check that could not run is counted rather than dropped.',
    long: [
      'Silence has two causes and they are opposites: nothing is wrong, or nothing looked. Only the count of checks that ran tells them apart.',
      'So an all-clear here is always phrased over a count of the checks that actually completed, and a check that could not run appears as its own line with its own reason.',
      'A check blocked by an administrative gate is separated from a check that failed, because a reader who is not an administrator genuinely cannot run it and nothing is broken.',
    ],
  },

  'ambiguous-write': {
    group: 'reading-a-figure',
    title: 'A write that failed after it left',
    short:
      'A write whose answer never arrived does not prove nothing happened. The answer was lost, not necessarily the request. So look at what exists before repeating it: looking is measurable, and repeating a write that already landed is not free.',
    long: [
      'Reading and writing fail differently. A failed read leaves the platform as it was; a failed write may have been applied in full with only its acknowledgement lost.',
      'Nothing in a browser can tell those apart after the fact, so this app never claims to. It says which of the two it is looking at and points at the thing that can be checked.',
      'Where repeating the write has a cost — a credential replaced twice, a second task submitted — the panel says so beside the control rather than in a note somewhere else.',
    ],
  },

  // -------------------------------------------------------------------------
  // How the platform behaves
  // -------------------------------------------------------------------------

  'pools-all-at-once': {
    group: 'the-platform',
    title: 'Every pool at once, or none of them',
    short:
      'A task clears every pool in its list at the same moment or it clears none of them, so its ceiling is the minimum across them and never a sum. Raising a pool that is not the binding one changes nothing at all.',
    long: [
      'Capacity is reserved for a whole task across every pool it needs, in a single transaction. A partial reservation would let a task hold a slot in one pool while waiting for another, which is how a platform deadlocks against itself.',
      'The arithmetic consequence is the one most often got wrong in practice: how many more agents a profile can start is the smallest headroom across its pools, divided by the profile’s weight. It is never the pool an operator happens to be looking at.',
      'That is why these panels name the pool that binds. Raising any other one is a change that looks like an action and produces no effect.',
    ],
  },

  'units-not-agents': {
    group: 'the-platform',
    title: 'Capacity is counted in units, not agents',
    short:
      'Each size has a weight, and admission adds that weight to every pool the task has to clear. A pool showing eight in use may be four agents of a size that weighs two, so a ceiling is a ceiling on units and headroom converts back through the weight.',
    long: [
      'A pool counter is a sum of weights, not a count of workloads. The weight is the size’s own, published by the catalogue, and it is the same number in every pool the task touches.',
      'So "units in use" and "how many more agents could start" are different questions with different answers, and this app never prints one under the other’s label.',
      'Where a screen shows how many more could start, it has already divided by the weight, and it says which size it divided by.',
    ],
  },

  // AH-14. redesign-v2 §9.1 named "what a pool is" and "why a paused pool is
  // not a full one" as the first things Help had to carry, and neither had a
  // topic. The facts are the frozen contract's (SlotPool, evaluate_capacity,
  // pool_names_for in apps/common/swarm_common), said without its names.
  'what-a-pool-is': {
    group: 'the-platform',
    title: 'What a pool is',
    short:
      'A pool is a named ceiling on how many units of work may hold capacity at once, with a count of how many do. Every task needs several at the same moment, and it starts only when all of them have room.',
    long: [
      'A pool has three parts: a ceiling, a count of the units in use against it, and a switch that pauses it. The count is changed only by the transaction that admits a task and the one that releases it, so it cannot drift under contention.',
      'The ceiling a pool enforces is the lowest of up to three numbers: the limit an operator set, a target the platform lowers on its own when a provider pushes back, and a cap derived from a provider’s quota. None of them can raise it above the limit the operator set.',
      'Every task needs several pools at once: one for the whole platform, one for its tenant, one for its size, one for its runtime and one for its backend, and, when the runtime uses a provider, one for that provider and one for that provider within the tenant. It is admitted into all of them in one transaction, or into none.',
      'Pools count units, not agents: a task adds its size’s weight to every pool it holds. How many more agents could start is therefore the smallest room across a task’s pools, divided by that weight.',
    ],
    see: ['pools-all-at-once', 'units-not-agents', 'paused-vs-full'],
  },

  'paused-vs-full': {
    group: 'the-platform',
    title: 'A paused pool is not a full one',
    short:
      'A full pool refuses new work because what is in use has reached its ceiling, and it admits again as running work finishes. A paused pool refuses everything, whatever its headroom, until an operator resumes it. Raising a paused pool’s ceiling changes nothing.',
    long: [
      'Both refuse admission, and on a board of figures they can look alike: nothing new starts. The difference is who has to act, and what they have to do.',
      'A full pool has as many units in use as its ceiling allows. Nothing is wrong with it: admission is doing its job, and the pool admits again as soon as running work finishes and releases its slots. Waiting, or raising the ceiling, are the two ways past it.',
      'A paused pool has been switched off by an operator. It refuses every task that needs it even with every slot free, so its headroom says nothing about whether anything can start. Work that needs it waits, costs nothing, and goes on waiting until a person resumes the pool.',
      'So raising the ceiling of a paused pool changes nothing; resuming it is the only thing that does. The Pools screen marks a paused pool with a “paused” chip, not as a pool at its limit, so look for that chip before reaching for the ceiling.',
    ],
    see: ['what-a-pool-is', 'ceiling-change-evicts-nothing'],
  },

  'admin-gate-not-failure': {
    group: 'the-platform',
    title: 'An administrative gate is not a fault',
    short:
      'Some routes answer only to administrators, and someone who is not one genuinely cannot read them. A console reporting that as a fault would report itself broken every time a non-administrator opened it, so it is counted apart from failures and named as what it is.',
    long: [
      'A refusal on grounds of permission is a complete and correct answer. It says nothing about whether the platform is healthy and nothing about the figures the route would have carried.',
      'So these screens keep three outcomes apart: a read that landed, a read that failed, and a read that was refused. The third is drawn in an informational tone and counted in its own column.',
      'What it does not do is stand in for a measurement. A control that needs the refused route is withdrawn and says why, rather than being drawn over data nobody has.',
    ],
  },

  // AH-14, with AH-12's Enforced column and AH-21's budget. Tenants links here
  // twice: the help link under the table (AH-12's, for the columns) and the
  // budget note's `Why →` (AH-21's). The short form answers the column first.
  //
  // Enforced is the tenant pool's `effective_limit` as /v1/capacity serves it
  // (G5-13, QA 2026-10-07; it was min(max_active, capacity_units) computed in
  // the browser). The pool's hard limit is min(max_active, capacity_units)
  // because that is what every writer of the tenant pool writes: swarm_api/store.py
  // `set_tenant_limits` whenever either changes, `ensure_tenant` on a first
  // sign-in, scripts/register-tenant.sh, and terraform/infra/locals.tf
  // `pool_tenants` when Terraform creates the pool. The last two wrote a
  // different value until the review of #161 found it; the paragraph names
  // all four so the claim can be checked rather than taken.
  //
  // A DISABLED TENANT IS NOT LOCKED OUT, and the first paragraph says so.
  // `tenant_for` refuses it and is called only by the paths that create work,
  // the account routes and the tenant credential routes; every task and
  // workflow read, and cancel, goes through `scope_for`, which deliberately
  // does not look at `enabled` (swarm_api/service.py). The paragraph once said
  // "refused on every call" -- an access-control boundary misstated to the
  // admin deciding whether to disable a group.
  //
  // NO KEY IS NOT NO CREDENTIAL, since #171 (#169). The Credentials paragraph
  // said a keyless tenant's provider runtime simply waits. Admission now asks
  // scheduler/credentials.py `credential_for`, which also admits a keyless
  // tenant on a subscription account it owns or is lent, for a profile that
  // takes a subscription token, on a deployment with a broker, and parks the
  // task on a missing credential only when neither exists. The paragraph
  // says both. (The park reason's own spelling is a frozen value, which
  // "help.ts restates no frozen value" keeps out of this file.)
  //
  // The budget paragraph is the account the table note used to print as
  // `no monthly_budget_usd column · no cost attribution source`, from the 422
  // in swarm_api/routes/admin.py `set_tenant_limits`.
  'tenant-fields': {
    group: 'the-platform',
    title: 'What each Tenants column means',
    short:
      'Enforced is the ceiling admission applies to a tenant: its pool’s effective limit, which starts from the smaller of its two configured limits, Max active and Units, because both cap the same count of units and the smaller one binds. Configured shows the two as the tenant record holds them. No budget can be set, so none is shown.',
    long: [
      'Tenant is the id every read and write is scoped by. Status says whether the tenant is enabled. A disabled tenant cannot submit tasks or workflows, reach its subscription accounts or register a provider key; its members can still list, read and cancel the tasks and workflows it already has, with their events, attempts, artifacts, checkpoints and logs, because a stopped tenant still has to see and stop what is running.',
      'Kind and Principal say who belongs to it. A group tenant takes the members of the group named in Principal; a user tenant is the one address named there.',
      'Enforced is the ceiling admission applies to the tenant’s own pool: that pool’s effective limit, read from the pool as Pools and Pool limits show it, not worked out from the two configured values. Its hard limit is the smaller of the two. Both limit the same thing — the units the tenant’s running work holds, where every task costs at least one — so the smaller is the one that binds. Every path that writes the tenant’s pool writes this figure as its hard limit: the admin routes whenever either value changes, a first sign-in that creates the tenant, scripts/register-tenant.sh, and Terraform when it creates the pool. An adaptive target lowered by rate limiting can hold the effective limit below that hard limit; a dash means the pool could not be read, does not exist, or has no limit set.',
      'Configured groups the two values as the tenant record holds them: Max active and Units. Neither is enforced on its own; a larger one beside a smaller one is headroom nobody can use until the smaller is raised.',
      'Credentials names the providers the tenant has registered a key for: names, never keys. None registered does not by itself mean the tenant cannot run work. A runtime that takes a subscription token can still run on a subscription account the tenant owns or is lent, on a deployment with an account pool; where the tenant has neither a key nor such an account, a runtime that needs a provider waits for this tenant rather than failing. Identity is the tenant’s own service account, the one its workloads run as; no service account is said in words, because a blank cell would read as fine.',
      'The tenant record carries a monthly budget field, and it is empty for every tenant. PUT /v1/admin/tenants/{id}/limits refuses it with a 422: the control plane has no cost attribution source — no billing export, no compute cost per attempt — so a budget could be stored but never enforced. Spend is bounded by the two limits the scheduler does enforce on every admission, which is what the Enforced column shows. The table leaves the field out because an empty column would read as “no budget set”. What the console’s spend figures do and do not include is a topic of its own, linked under this one.',
    ],
    see: ['token-cost', 'paused-vs-full'],
  },

  // AH-14. The facts are swarm_api/service.py `stats` and store.py
  // `count_tasks_by_state`; the arithmetic of the cost is PlatformCounts.tsx's
  // STATE_COUNT, which is why no number is written here.
  'platform-counts': {
    group: 'the-platform',
    title: 'What Platform counts returns',
    short:
      'One count of task documents per state for your own tenant, and a second set across every tenant when you are an administrator. Each state is its own count() query, so a run costs one query per state per scope, and the price grows with the platform’s history rather than its load.',
    long: [
      'Each run asks the stats route for the number of task documents in each state, as they stand at that moment. A task that has moved on since is counted where it is now, and nothing about how long any task spent anywhere is in the answer.',
      'The first set is scoped to the caller’s own tenant. An administrator also gets a second set across every tenant; anyone else sees that card as not entitled, which is a permission and not a fault.',
      'Every state the contract defines is counted, including the ones never written to a task document, so a run costs one count() per state per scope. That query bills by the index entries it scans, and those grow with every task ever written — so the price is printed before the control that spends it, and nothing re-runs it on a timer.',
      'A total is drawn only when every state came back. When one did not, the total is withheld and the missing states are named, because a sum over a partial response is not a count of anything.',
    ],
    see: ['withheld-total', 'admin-gate-not-failure'],
  },

  'poll-cadence': {
    group: 'the-platform',
    title: 'What re-polls, and what does not',
    short:
      'Most panels re-read on a timer. The spend rollup does not: it fans out across attempts and moves only when you press refresh. A figure that does not re-poll sitting beside ones that do is indistinguishable from them unless it says how old it is.',
    long: [
      'The cheap reads run on their own cadence, and the interval is shown beside them rather than written down here, so the two can never disagree.',
      'The spend figure is a fan-out over the attempts of many tasks. Running it on the same timer would multiply every tick by the size of the sample, so it runs when asked.',
      'The consequence is that it ages while the panels around it do not, which is why it prints its own age instead of borrowing the screen’s.',
    ],
  },

  'tenant-scope': {
    group: 'the-platform',
    title: 'Whose figures these are',
    short:
      'The capacity route answers for the calling tenant, an administrator included. So a ceiling here is how many more you could start, never how much the platform has. A platform-wide figure is not faked by substituting somebody else’s pools.',
    // WRITTEN FOR EVERY SCREEN THAT LINKS IT (AH-13 rule 2): the Timeline's
    // footer index (its People table went with #185), the Pools footer and the
    // Profile headroom footer.
    // tests/help.test.ts finds the linking screens in the sources, so a fourth
    // fails there until this is written for it too.
    long: [
      'Every task and workflow read the console makes is answered for the calling tenant, an administrator’s included: the API takes the scope from the verified caller, never from a parameter. A figure drawn from those reads is the caller’s own and never a total across tenants. The one exception is the platform scope on the Timeline screen, which an administrator asks for by name, which the API refuses to anyone else, and whose figures then name the tenants they cover.',
      'The Timeline screen reads its ledger for the calling tenant unless an administrator switches it to platform scope, and the two scopes are never drawn side by side. Grouped by person, its reliability table is computed by the API over the whole span, so someone with no work in the span is absent from the table rather than listed with zero.',
      'The capacity read is scoped too. It lists the platform-wide pools and the caller’s own tenant pools, and leaves another tenant’s pools out unless the caller is an administrator. Every headroom figure in it — the Headroom card on the Pools screen and each card on the Profile headroom screen — is computed against the caller’s own tenant pool, an administrator’s included, so it says how many more the caller could start, never how much the platform has. Each of those cards says which tenant it was computed for.',
      'A figure whose scope is unstated is the one people quote in a meeting as though it were the platform’s, so each of these screens prints the scope beside its figures.',
    ],
  },

  'park-on-missing-credential': {
    group: 'the-platform',
    title: 'A missing credential waits rather than fails',
    short:
      'A task whose runtime names a subscription provider with no account registered waits instead of failing. Waiting costs nothing, and it also runs nothing — so an empty pool is quiet rather than loud, and the quiet is the thing to notice.',
    long: [
      'The platform prefers waiting to failing wherever the obstacle is temporary, and an account that has not been added yet is exactly that.',
      'The cost of that choice is that nothing goes red. Work accumulates in a waiting state, consumes no capacity, and produces no error anyone is paged for.',
      'So the screens say it where it would otherwise be invisible: an empty account pool is reported as a real zero with its consequence attached, not as an empty table.',
    ],
  },

  // -------------------------------------------------------------------------
  // The runtime catalogue
  // -------------------------------------------------------------------------

  'runner-profile-by-name': {
    group: 'the-catalogue',
    title: 'A caller picks a name and supplies nothing else',
    short:
      'Never an image, a command, a size or a backend. Everything the catalogue shows is what that one name already decides, which is what lets the platform change any of it without a caller changing anything — and what stops a caller choosing where their code runs.',
    long: [
      'The API accepts a runtime name and refuses every name the catalogue does not carry. It accepts no image reference, no command, no resource specification and no backend parameter from a caller.',
      'That is a security boundary before it is a convenience: a caller who could name an image could run anything on the platform’s service accounts.',
      'It is also why the runtime catalogue is worth reading. Everything it shows for a runtime is a consequence of one string, and none of it is visible at the point the string is typed.',
    ],
  },

  'not-a-machine-inventory': {
    group: 'the-catalogue',
    title: 'Dispatch topology, not a machine inventory',
    short:
      'No route in this platform serves nodes, container executions or batch jobs, so none is drawn here and none is guessed at. What this screen shows is where work is sent, not what is running at this moment.',
    long: [
      'The catalogue describes how a name resolves: to a backend, an image, a size and a credential requirement. It is static in the sense that it does not move while work runs.',
      'Live load appears on the Runtimes screen only where a capacity read supplied it, and it is labelled as coming from there. Where that read did not complete, the columns are dashes rather than zeros.',
      'Nothing in this platform publishes its machines to a console, so the catalogue lists none. What is running at this moment is on the Agents screen.',
    ],
  },

  'declared-vs-resolved-backend': {
    group: 'the-catalogue',
    title: 'Declared backend and resolved backend',
    short:
      'A profile may declare that the platform should choose, and that declaration is not itself a place work can run. Where the two differ the card shows both: the declared value alone says nothing about where work goes, and the resolved value alone hides that the platform chose it.',
    long: [
      'Resolution happens on the platform, from the declaration plus whatever the platform knows. The result is the answer to "where does this run"; the declaration is the answer to "what did the profile ask for".',
      'Showing only one of them loses a different thing each way round, so where they differ both are shown and the card says which is which.',
      'Rows are grouped by the resolved value, because that is the grouping that corresponds to a pool and therefore to a ceiling.',
    ],
  },

  'catalogue-from-route': {
    group: 'the-catalogue',
    title: 'Nothing here is written down in this client',
    short:
      'Every name, size, weight, backend and timeout on this screen came from the catalogue route in this page load. A runtime added to the catalogue appears here with nobody editing the screen, and no figure here can disagree with the platform because none is stored here.',
    long: [
      'The catalogue is contract data that can gain entries, and a runtime added to it is on the Runtimes screen at the next load.',
      'So the Runtimes screen renders the response and nothing else. There is no fallback list, no hardcoded default and no enrichment from a table in the bundle.',
      'So a failed read leaves the Runtimes screen with nothing to show rather than an older copy. An empty catalogue after a failed read is a read to retry, not a catalogue with no runtimes.',
    ],
  },

  'credential-names-not-values': {
    group: 'the-catalogue',
    title: 'Credential names, never values',
    short:
      'The route publishes the environment variable names a runtime reads. It reads no environment, no secret store and no tenant document. Whether your tenant holds one of them is a different question, answered where accounts are managed.',
    long: [
      'A runtime declares which variables it needs and whether it needs all of them or any one of them. That flag matters: a list of two names without it tells a subscriber they must also buy metered access.',
      'So the flag is rendered as words rather than as a symbol, and the names are rendered as names. No value, no length and no presence check appears anywhere in the console.',
      'Whether the tenant reading the screen actually holds one is answered by the account pool, which is the screen that can answer it.',
    ],
  },

  // #126: the Runtimes screen's one always-present `?` opens this topic, so it
  // answers both words a card draws that its own facts do not explain --
  // `Sets it apart` and the `disabled` chip.
  'what-sets-it-apart-is-arithmetic': {
    group: 'the-catalogue',
    title: 'What sets a runtime apart, and what disabled means',
    short:
      'Each "sets it apart" line is arithmetic against the rest of the catalogue in the same response: longest, heaviest, the only one of its kind. Nobody wrote it. A disabled runtime stays listed so work that names it still renders, but the platform refuses new work for it, and the card gives its reason.',
    long: [
      'The comparison is computed: largest, smallest, only one of its kind, different backend from the rest. It is derived from the same response the cards are drawn from.',
      'When the computation finds nothing, the card says so rather than reaching for a sentence somebody typed. "Nothing separates it from the rest" is a measured answer.',
      'So a line that looks wrong can be checked against the other cards on the Runtimes screen, and it changes only when the catalogue does.',
      'Disabled is the catalogue’s own flag, not a reading of load. The runtime is still served because tasks already submitted under its name have to render, and the API refuses any new submission that names it. The reason printed on its card is the platform’s, and usually names the runtime to use instead.',
    ],
  },

  'runtime-needs-no-provider': {
    group: 'the-catalogue',
    title: 'A runtime that needs no credential',
    short:
      'Some runtimes consume no external provider’s quota, so they run for a tenant that has registered nothing at all. That is a measured answer rather than a missing one, which is why it is a word here and not a dash.',
    long: [
      'The catalogue distinguishes "this runtime names no provider" from "this runtime names a provider and lists no variable for it". They are different facts about the response.',
      'The first means nothing has to be registered before it will run. The second means the catalogue named a provider and published no variable name, which is what the response says and is not a failed read.',
      'Neither is an absence: both were found out, so the Runtimes screen states each in words rather than with a dash.',
    ],
  },

  // -------------------------------------------------------------------------
  // Submitting work
  // -------------------------------------------------------------------------

  'dispatch-strategies': {
    group: 'submitting-work',
    title: 'What a strategy publishes',
    short:
      'The strategy decides what leaves the platform: nothing at all, one pull request per step, or one for the whole workflow. The number on each option is computed from the steps on the form, so what you read is the consequence rather than a description of it.',
    long: [
      'The default publishes nothing. The patch is harvested into the task’s own artifacts and no branch is pushed, which is why the option says "no pull request" where the others say how many.',
      'The per-step option pushes each step’s branch and opens a pull request for each. Over six steps that is six, and the option says six rather than "one per step".',
      'The integrating option produces exactly one pull request for the whole workflow, opened by the step nothing depends on. The other steps push branches and open nothing.',
    ],
    // READ, not restated: the three names and their labels come from the
    // module that owns them, so a strategy renamed there is renamed here.
    values: () =>
      DISPATCH_STRATEGIES.map((s) => ({ term: s, note: STRATEGY_LABEL[s] })),
  },

  'integrate-needs-final-step': {
    group: 'submitting-work',
    title: 'One pull request needs exactly one final step',
    short:
      'Integrating names a final step that receives the other steps’ patches. A single task has no other steps, so it is refused there. A workflow needs exactly one step nothing depends on: two of them, or none, is refused with the graph named.',
    long: [
      'The platform resolves the integrating step as the workflow’s single sink — the one step no other step depends on. That is a property of the graph, not a field anyone sets.',
      'A graph with two sinks has two candidates and no rule to choose between them, so the platform refuses rather than picking. A graph with no sink is cyclic and is refused earlier, by name.',
      'The preview on the form is a preview. The platform decides, and nothing drawn on the Submit a workflow form prevents a submission — the worst a wrong preview costs is a caution that turns out not to apply.',
    ],
  },

  'dispatch-carrier': {
    group: 'submitting-work',
    title: 'What carries work between steps',
    short:
      'The carrier records how a step’s work is intended to reach the next one. It is stored on the task and returned by the API, and no worker code reads it yet — so it is a recorded preference rather than a behaviour, and the form says so on every choice.',
    long: [
      'The field exists so that the intent is captured at the moment it is expressed, rather than reconstructed later from what happened.',
      'Nothing acts on it today. A control that does nothing and does not say so is worse than no control, so on Submit a task and Submit a workflow the note sits beside the choice rather than behind a help card.',
      'When a worker does read it, the note goes away. Until then the honest description of the carrier choice is that it records an answer.',
    ],
    // The per-carrier sentence that used to sit under the picker, READ from
    // the module that owns it rather than copied into this file.
    values: () =>
      DISPATCH_CARRIERS.map((c) => ({ term: c, note: CARRIER_DETAIL[c] })),
  },

  'repository-url': {
    group: 'submitting-work',
    title: 'What the repository url does',
    short:
      'It is the repository the agent clones. Without one the agent starts in an empty workspace whatever the strategy says, and any choice that has to push is refused by the API for want of somewhere to push to.',
    long: [
      'The agent’s workspace is created empty and filled by cloning. No repository means no clone, which is a legitimate thing to ask for and an easy thing to ask for by accident.',
      'Whether it is required is decided by the platform from the strategy and the carrier together. Submit a task and Submit a workflow preview that decision so the refusal is not a surprise; it never blocks a submission on its own reading.',
      'If the preview and the platform ever disagree, the visible result is a caution that did not need to be there rather than a submission that could not be made.',
    ],
  },

  'dispatch-absent-is-old-api': {
    group: 'submitting-work',
    title: 'A dispatch that was never reported',
    short:
      'The API sends a dispatch block on every task and fills the defaults for tasks older than the field. Its absence is therefore a deployment older than the field, not a caller who chose to publish nothing — and nothing here says what that run would have published.',
    long: [
      'Two different facts would otherwise look identical: a caller who asked for nothing to be published, and an API that never told us what was asked for.',
      'The first is a choice with a known outcome. The second is a gap in the record, and any claim drawn over it would be invented.',
      'So a task with no dispatch block gets its own panel saying which of the two this is, in the same way a failed read never renders as empty data anywhere else here.',
    ],
  },

  'input-is-opaque': {
    group: 'submitting-work',
    title: 'The input is opaque to the platform',
    short:
      'Whatever you type is handed to the profile’s agent unread. The platform validates its size and nothing else, so nothing here interprets it, nothing here can warn you about it, and nothing here will reformat it on the way.',
    long: [
      'The input is a payload, not a command. The platform carries it and the agent decides what it means.',
      'That is why the input on the submit forms has no syntax help and no validation beyond a length: any check there would be this console guessing at a contract between a caller and an agent it cannot see.',
      'A refusal of the input therefore comes from the API and is shown exactly as the API worded it.',
    ],
  },

  'workflow-stages': {
    group: 'submitting-work',
    title: 'How a workflow runs its steps',
    short:
      'Steps run in stages. A step starts when every step it depends on has succeeded, and steps with nothing left to wait for run at the same time. A failure cancels its dependants — every step waiting on the failed one, directly or through another — and the steps that do not depend on it carry on.',
    long: [
      'A stage on Submit a workflow is a default for what a step waits for: a step in a later stage waits for every step in the stage before it, unless it is narrowed to a chosen set of earlier steps. The platform receives only the dependencies; stages are how the form lays them out.',
      'A step becomes eligible when every step it depends on has succeeded. Until then it holds no capacity and costs nothing. Steps whose dependencies have all succeeded run at the same time, within the ceilings of the pools they need.',
      'When a step fails for good, its retries spent, the platform cancels every step that depends on it, directly or through another step, because none of them could ever start. A step that does not depend on the failed one is not touched and runs to its own end.',
    ],
  },

  'runner-unavailable': {
    group: 'submitting-work',
    title: 'Why a runner is unavailable',
    short:
      'A runner is held back when the platform has not enabled it for this tenant, when its credential was refused, or when the form decides the runner itself: an issue run always plans and builds with one fixed runner. Hover a runner for the platform’s own reason.',
    long: [
      'The forms show the runners you can use and fold the rest under “Other runners”, each with a one-line reason. The platform’s own reason, as it wrote it, is that line’s tooltip.',
      'An issue run takes no runner: the request that creates it has no field for one, and the API plans and builds every step with the same runner. The form shows that runner and holds the others back for that reason, not because they are broken.',
      'A runner the platform disables carries the platform’s reason, which may name work still to land before it is enabled. Nothing on the form can enable it; an administrator of the platform can.',
    ],
  },

  'recent-submissions': {
    group: 'submitting-work',
    title: 'Where your recent submissions come from',
    short:
      'The API keeps no list of what each person submitted, but it says who submitted every task, workflow and issue run, and who you are. The card lists your newest lone tasks, workflows and issue runs from those reads, each a link to its page, and says which windows it looked in.',
    long: [
      'A workflow’s steps and an agent’s children are not listed as tasks of their own: you submitted the workflow, or the agent did. Older submissions outside the windows read are not listed, and the card says how far it looked.',
      'The settings a submission was made with are not served, so the card opens what you submitted rather than a form filled in from it. When who you are cannot be read, nothing is listed: nothing can be called yours.',
    ],
  },

  'room-unknown-not-zero': {
    group: 'submitting-work',
    title: 'Unknown room is not no room',
    short:
      'When a pool could not be read, how much room is left is unknown. Rendering that as zero would claim a measurement nobody has; rendering it as room would claim the opposite. The count of pools that could not be read is shown instead.',
    long: [
      'A profile’s ceiling is the minimum across its pools. One unreadable pool makes the minimum unknown, however well the others read.',
      'The temptation is to compute the minimum over what was read and caption it as the ceiling. That figure is a real minimum over a sample nobody asked for, and it is indistinguishable on screen from the real thing.',
      'So the panel says how many pools it could not read and prints no ceiling at all, which is the only honest answer available.',
    ],
  },

  // -------------------------------------------------------------------------
  // Running the account pool
  // -------------------------------------------------------------------------

  'sign-in-not-paste': {
    group: 'an-account',
    title: 'Adding an account is a sign-in',
    short:
      'Name it, start the sign-in, open the page it gives you, sign in as you normally would, and paste back the short code that page shows. There is no keychain item to find, no file to preserve, and nothing to run on your laptop.',
    long: [
      'The provider’s client accepts exactly one redirect target — its own callback page, which displays a code. A third-party application cannot register a redirect back to this console, so your browser cannot be sent here and the displayed code is what closes the loop.',
      'Nothing secret passes through the sign-in panel on Accounts. The verifier stays on the server, keyed by the sign-in; a verifier the browser holds is a flow that proves nothing. What comes back is an account and an expiry, never key material and never its length.',
      'That is the whole procedure. The only thing you paste is the short code the provider’s page shows; nothing in it asks for a key or a credential file.',
    ],
  },

  'credential-split': {
    group: 'an-account',
    title: 'Two secrets, and the split is the boundary',
    short:
      'What the sign-in yields is stored as two secrets: the exchangeable pair, which only the broker reads, and the short-lived token a pod mounts. A compromised pod therefore holds a credential that expires, not one that can mint successors forever.',
    long: [
      'One secret holds the pair the broker exchanges. Nothing outside the broker reads it, and no route returns it.',
      'The other holds only the short-lived token, and that is the one a tenant’s workload mounts. It expires on its own, and the sweep replaces it before it does.',
      'Neither is ever rendered anywhere in the console, not even as a length. If a pod is compromised, revoke the account’s sign-in.',
    ],
  },

  'credential-refresh-sweep': {
    group: 'an-account',
    title: 'How refreshing works',
    short:
      'One component may exchange a credential, because exchanging one revokes the token it replaces and two components doing it concurrently would break the account. The sweep visits every account, including idle ones, and usually has nothing to do.',
    long: [
      'An exchange happens inside the last stretch of the mounted token’s life. Before that, the sweep publishes the token it already holds if the pod-facing secret is missing it, and otherwise does nothing at all. "Nothing to do" is the healthy answer.',
      'Idle accounts are visited too. A pair that is never exchanged eventually dies, so a pool where three accounts are busy and two are idle is a pool where two are quietly rotting until the day you need them.',
      'Only the broker exchanges. Everything else, this console included, asks the broker.',
    ],
  },

  'refresh-now-probe': {
    group: 'an-account',
    // NO DOUBLE QUOTES IN A TITLE. `renderToStaticMarkup` escapes them, and
    // `tests/help.test.ts` asserts the title appears in the rendered Help page
    // by substring -- so a quoted title fails a test that is right to fail.
    title: 'What the refresh button does',
    short:
      'It runs the same code path the sweep runs, for one account, and reports which of its outcomes happened. It exists because a timer gives you no way to answer "did that work?", and a state chip is not that answer.',
    long: [
      'The button is a probe, not a repair. It performs the same exchange the sweep would perform and shows the broker’s own verdict fields rather than a summary of them.',
      'The raw fields are printed because they are what a log line or a bug report will be matched against later.',
      'One answer is worth reading closely: a verdict that reports an expiry already in the past means the published token is dead on arrival, and workloads will fail on it.',
    ],
  },

  'reauth-required': {
    group: 'an-account',
    title: 'When only a person can fix an account',
    short:
      'It means the exchangeable half is gone or unreadable. The broker stops trying on purpose: retrying every few minutes spends the token endpoint’s rate limit to learn the same answer. Nothing new is assigned, and agents already on it fail when their token expires.',
    long: [
      'Stopping is deliberate. An automatic retry loop against a credential that cannot be exchanged is a loop that will never succeed and will consume the rate limit that healthy accounts need.',
      'The fix is the same sign-in that adds an account, aimed at the label already in the pool: a login page and one short code. The id, the readings and the lending list are kept, because the label is the same.',
      'A new label instead would create a second account and leave the broken one sitting there, which is why this console offers the control on the row rather than in the add form.',
    ],
  },

  'reauth-does-not-unpause': {
    group: 'an-account',
    title: 'Signing in does not change state',
    short:
      'Replacing a credential deliberately leaves the account’s state exactly as it was. That is the rule that stops a replacement silently resuming an account somebody stopped on purpose — so putting the state back is a second, separate call, and this screen says whether it succeeded.',
    long: [
      'A credential and a state are two different decisions. Conflating them would mean that fixing a credential quietly reverses an operator’s decision to hold an account back.',
      'So the two calls are two calls, and both outcomes are reported. A credential that is in place and good, on an account that will not be assigned, is a success and a problem at the same time.',
      'When the second call fails, the panel says so in its heading rather than in a footnote, because the visible half of the operation succeeded and the invisible half is the one that matters.',
    ],
  },

  'second-browser-application': {
    group: 'an-account',
    title: 'A second account needs a second browser application',
    short:
      'Which organisation you sign in as comes from the cookies the browser already holds, and nothing in the request overrides it. A private window shares that jar, so it signs you in as the same organisation and the pool gains a row for a subscription you already had.',
    long: [
      'There is no account hint this platform could send that would change which organisation the provider signs you in as. It is decided entirely by the browser.',
      'A private window is not a different browser for this purpose. It shares the cookie jar, so it produces the same organisation and a duplicate row with nothing on any screen explaining why the pool did not really grow.',
      'What works is a different browser application, or signing out of the provider in this one first. That is why the warning is shown before the sign-in page is opened: afterwards the advice is useless.',
    ],
  },

  'signin-paste-the-code': {
    group: 'an-account',
    title: 'Paste all of the code',
    short:
      'The callback page shows the code, then a separator, then a long string. Paste all of it: the platform splits it and uses the second part to check the paste belongs to this sign-in rather than another tab’s. Pasting only the first part works too.',
    long: [
      'Two tabs each holding their own open sign-in is how a credential lands on the wrong account, and the second part of the code is what makes that detectable.',
      'The check runs before the platform reads anything else, so a paste from the wrong tab leaves both sign-ins exactly as they were.',
      'Pasting only the first part skips that check rather than failing it, which is why it is allowed and why it is not what the page asks for.',
    ],
  },

  'signin-deadlines': {
    group: 'an-account',
    title: 'Two clocks, and they are not the same one',
    short:
      'The platform holds its pending record for a period it reports, so that one is counted down honestly. The code on the callback page is the provider’s and expires on its own schedule, which this platform does not know — so no number is put on it.',
    long: [
      'A single countdown covering both would be a measured figure lending its credibility to a guess.',
      'The code is single-use and expires sooner than the platform’s hold, so the practical advice is to paste it when you see it rather than leaving the tab open.',
      'When the platform’s own deadline passes on this browser’s clock, the field stays live: the platform owns the clock, this one may be ahead of it, and being refused costs nothing.',
    ],
  },

  'signin-verify-by-reload': {
    group: 'an-account',
    title: 'Look at the pool before signing in again',
    short:
      'Several ways a sign-in can end leave this page unable to say whether an account was written. Reloading the pool and looking for the label is measurable; guessing from here is not. A repeat sign-in is not free, so looking comes first.',
    long: [
      'Where the platform accepted the sign-in but the answer was incomplete or lost, the account may well exist. Signing in again under the same label replaces a credential, which revokes the one before it.',
      'The reload clears the add form on Accounts, because it remounts the whole screen. So read the label and the lending list off the form before pressing anything: the platform keeps no copy of either once a sign-in ends.',
      'On a row that already exists the same rule applies in a different shape: treat the credential as possibly already replaced until you have looked, rather than repeating the sign-in blindly.',
    ],
  },

  'signin-201-no-name': {
    group: 'an-account',
    title: 'Accepted, but the answer did not name the account',
    short:
      'The success code is the answer the platform gives once the account has been written, and it deletes its pending record on that same path. What is missing from the answer is the account’s name, not the account.',
    long: [
      'The sign-in panel on Accounts receives this as a failure, because the response could not be parsed into an account, which is why the panel’s heading and tone are overridden: the loudest thing on a panel must not contradict its own text.',
      'Nothing is gained by signing in again before looking. The likeliest state of the world is that the account exists under the label that was typed.',
      'So the sign-in panel’s first instruction is to look, and its controls are a reload and a fresh start, in that order.',
    ],
  },

  'signin-is-over': {
    group: 'an-account',
    title: 'A sign-in the platform is no longer holding',
    short:
      'The platform deletes a pending record when it is too old, and also immediately after an account has been written. Pasting cannot reopen either one: a code is now checked against nothing. Which of the two happened is not something this page can measure.',
    long: [
      'When the record was deleted for age, the deletion happens before anything is redeemed, so nothing was created and nothing was changed. That case can be blunt about it.',
      'When the record is simply absent, the ordinary reason is that it was redeemed — the delete that does not raise is the one immediately after an account has been written. So absence is not "nothing happened".',
      'In both cases a fresh sign-in from the same page is the way forward, and in the second the pool should be read first.',
    ],
  },

  'signin-still-open': {
    group: 'an-account',
    title: 'A refusal that leaves the sign-in open',
    short:
      'A code that was mistyped, already spent, too old, or belongs to another tab costs one paste and nothing else. The platform deletes a sign-in only once an account exists, so the one on this page is untouched and still waiting for a code.',
    long: [
      'The check that a code belongs to this sign-in runs before the platform reads anything, so a paste from a second tab cannot disturb either sign-in.',
      'A refused code leaves the pending record in place, which is why the paste field stays live and why the label and the lending list do not need re-entering.',
      'Asking the provider for a fresh code reuses the same sign-in. Starting over abandons it and asks for a new one, which is a different thing and is offered separately.',
    ],
  },

  'signin-route-missing': {
    group: 'an-account',
    title: 'A deployment that does not serve the sign-in',
    short:
      'The API answered as an API does when it has not been given the route at all — not an answer about your request and not one about your account. Nothing was created and nothing was changed.',
    long: [
      'Both halves of the sign-in exist in this repository: the broker serves them and the API proxies them. An API that does not answer them is not the one the sign-in panel on Accounts was built against.',
      'The account pool itself loading is the evidence that your session and the account routes are fine, and that it is this one route that is missing.',
      'Nothing on the sign-in panel offers a retry, because a retry would re-send a request that never reached a handler.',
    ],
  },

  'signin-is-anthropics-page': {
    group: 'an-account',
    title: 'The sign-in page is the provider’s',
    short:
      'The page that opens is the provider’s own sign-in, and so is the page you land on afterwards. This platform never sees your password, and the only thing it receives is the short code that page displays.',
    long: [
      'The tab is opened synchronously inside the click, because opening it from a callback is the pattern popup blockers exist to stop — and a sign-in that silently does not open is indistinguishable from a broken platform.',
      'When the browser blocks it anyway, the panel says so and offers the link, rather than leaving a button that appears to have done nothing.',
      'The link is also the thing to carry to another browser application when the account being added belongs to a different organisation.',
    ],
  },

  'signin-holds-label-and-lending': {
    group: 'an-account',
    title: 'The sign-in holds the label and the lending list',
    short:
      'Both were sent when the sign-in started and the platform is holding them alongside it, so neither can be changed from here without starting a new one. It also keeps no copy of either once the sign-in ends.',
    long: [
      'The pending record is what decides where the account lands when the code comes back minutes later. The tenant on it is resolved from the verified session, not from anything the add form on Accounts sends.',
      'That is why the panel names what the sign-in is reserved for, in full, before the code is pasted: it is the last point at which the answer can be changed.',
      'And it is why the advice on every ending refusal is to read the label and the lending list off the form before reloading anything.',
    ],
  },

  'signin-keeps-readings': {
    group: 'an-account',
    title: 'What a replacement keeps',
    short:
      'Signing in under a label that already exists replaces the credential and nothing else. The id, the readings, the lending list and the state are carried through, so the row looks much as it did and its figures are the ones the table is already showing.',
    long: [
      'A re-registration is not a new account. Treating it as one would blank a row’s history every time somebody fixed its credential.',
      'So a receipt after a replacement reports the readings that were kept, with their age, rather than claiming there is no reading yet.',
      'Only a genuinely new account has never been observed, and its receipt says that instead — unmeasured rather than idle.',
    ],
  },

  'account-label-rules': {
    group: 'an-account',
    title: 'What a label may be',
    short:
      'Lowercase letters, digits and dashes, starting and ending alphanumeric, and short — because it becomes part of a secret name and a cluster annotation. The platform checks it before it hands back a sign-in link, so a name it cannot use costs a message rather than a wasted login.',
    long: [
      'The rule is the platform’s and is deliberately not repeated in this browser. A second copy would eventually refuse a name the platform would have taken.',
      'The check runs early, before any sign-in link exists, which is what makes a bad label cheap.',
      'A label that already exists in the pool is not an error: it means the sign-in will replace that account’s credential rather than add a second one, and the form says so beside the field.',
    ],
  },

  'account-owned-by-one-tenant': {
    group: 'an-account',
    title: 'An account is owned by exactly one tenant',
    short:
      'The API decides which from your verified session rather than from anything typed here. When the response does not name a tenant, this form cannot tell you whose pool an account would land in — and a sign-in is not something to spend on a guess.',
    long: [
      'Ownership is resolved server-side and echoed back, deliberately not taken from a second source, so the console can never be told it is looking at a tenant it is not.',
      'Without that echo the form could still submit safely, because the platform files the account under the verified session whatever the page thinks. What it could not do is tell the operator where it went.',
      'So the form withdraws rather than guessing, and says that this is a gap in what was read rather than a fault in the platform.',
    ],
  },

  lending: {
    group: 'an-account',
    title: 'What lending an account does',
    short:
      'An account serves its owner. Naming another tenant lets that tenant’s workloads mount this account’s token as well. Nothing else about the account changes, and the list can be edited per account afterwards.',
    long: [
      'Isolation is the default: a tenant’s workloads reach their own tenant’s accounts and nothing else.',
      'Lending narrows that isolation deliberately and with a name on it. It is a decision an operator makes and can reverse, not a side effect of anything.',
      'An owner cannot be lent their own account, so an owner in the list is dropped. That is called out rather than done silently, because a saved list that differs from the typed one with nothing explaining why is its own small bug.',
    ],
  },

  'lending-narrows-isolation': {
    group: 'an-account',
    title: 'Lending narrows isolation on purpose',
    short:
      'Per-tenant isolation is an invariant of this platform: own service account, own secrets, own storage prefix, own namespace. Lending is a named, reversible narrowing of it for one account, not a hole in it.',
    long: [
      'The invariant exists so that one tenant’s compromise is one tenant’s problem. Every lending default on the Accounts screen follows from it.',
      'Lending is the one place an operator may open a specific account to a specific tenant. It is per account, it is listed on the account, and it can be withdrawn.',
      'What it does not do is grant anything else. A borrower may mount the token; pausing, draining, re-lending, refreshing and signing in again stay with the owner.',
    ],
  },

  'lent-account': {
    group: 'an-account',
    title: 'An account lent to you',
    short:
      'Your agents can run on it. Pausing, draining, re-lending, refreshing and signing in again stay with its owner, and those routes answer here as though no such account existed — the same answer a label that does not exist gets, so asking cannot confirm somebody else’s account names.',
    long: [
      'The row appears because the account serves you. It carries no controls because the routes behind them are not yours to call.',
      'The refusal is deliberately indistinguishable from the refusal for a name that does not exist. A console that answered differently would be an oracle for other tenants’ account labels.',
      'When a lent account is broken, the thing to do is ask its owner. The Accounts screen separates that case from your own broken accounts for exactly that reason.',
    ],
  },

  'advisory-vs-lease': {
    group: 'an-account',
    // See the note on `refresh-now-probe`: no double quotes in a title.
    title: 'The agent count is advisory',
    short:
      'The lease is the authoritative record of who holds what. This counter exists to make a listing readable, and a disagreement between it and the holders view is not rounding — it is two different sources, one of which is the record.',
    long: [
      'The counter is maintained for display. It is cheap to read and it is not what any decision is made from.',
      'The lease is what the platform admits and reclaims against. Where the two differ, the lease is right.',
      'So this figure is labelled advisory wherever it appears, rather than being quietly shown beside figures that are authoritative.',
    ],
  },

  'account-states': {
    group: 'an-account',
    title: 'The states are not degrees of one thing',
    short:
      'One state is the only one a new agent may be started on. One stops new work while the agents already on it keep running. One moves them off. The last is a verdict the platform reached rather than a state to declare, and only a person clears it.',
    long: [
      'They are not a severity scale, and drawing them as one would suggest that the platform moves an account along it. It does not: an operator sets three of them and the platform sets the fourth.',
      'An account can be holding full headroom and still serve nobody, because headroom is not the gate — the state is, and only one state is assignable.',
      'The reason for a change is stored with it and shown beside the state, because the next person to look is usually not the person who changed it.',
    ],
  },

  'state-change-reason': {
    group: 'an-account',
    title: 'Why the reason is stored',
    short:
      'A state change carries the reason it was made, and the pool shows it beside the state. The next person to look is usually not you, and a stopped account with no reason is indistinguishable from one somebody forgot.',
    long: [
      'The reason is stored with the change rather than in a separate log, so it cannot be separated from the thing it explains.',
      'It is shown in the table, not only in the row’s detail, because the question "why is this one stopped" is asked while scanning.',
      'An empty reason is allowed and is its own answer: it means nobody recorded one.',
    ],
  },

  'account-removal-is-reversible': {
    group: 'an-account',
    title: 'Removing an account keeps its credential',
    short:
      'Removal takes the account out of the pool. The credential in the secret store is retained rather than deleted, so this is reversible by signing in again under the same label, and no version history is lost.',
    long: [
      'Remove, on the Accounts screen, is the destructive-looking action and also the reversible one, which is worth knowing before rather than after.',
      'What is not reversible by itself is the effect on agents currently holding the account. Moving it to a draining state first is what takes them off it cleanly.',
      'Remove is armed by typing the account’s label, because an accidental removal of the wrong row is the mistake this shape prevents.',
    ],
  },

  'provider-defines-windows': {
    group: 'an-account',
    title: 'The provider defines its own windows',
    short:
      'Window names come from the provider and a new one can appear at any time. Readings are keyed by those names, so a window with no column of its own is listed rather than dropped — a screen that read exactly two keys would make the next one invisible.',
    long: [
      'The account record keys its windows by the provider’s own names precisely so a new one needs no schema change.',
      'The Accounts table gives two of them columns because they are the two that exist today and the two an operator scans. Everything else the reading carried is shown in the opened row.',
      'When a reading carries no windows at all, the columns are unmeasured rather than zero, and the row says so.',
    ],
  },

  'accounts-table-shape': {
    group: 'an-account',
    title: 'The table is the shape the command line prints',
    short:
      'Same columns, same order, same figures, same tilde, same meanings. If you read that in a terminal, this is the same line. Every extra fact lives in the row you open, so the table you scan never changes shape.',
    long: [
      'Two tools that answer the same question in two shapes make a person translate between them under pressure.',
      'So the columns are fixed and the row is where detail goes. A row that grew a column for a special case would change the shape of every other row.',
      'The count over the Accounts table is part of the same promise: it says how many documents were read and how many could not be, because a row count computed over a partial read is not a count.',
    ],
  },

  'no-amber-band': {
    group: 'an-account',
    title: 'There is no amber band, deliberately',
    short:
      'Any threshold between "fine" and "getting full" would be a number invented in this browser. The platform’s own floor lives in the broker and nothing checks that a copy of it here still matches. The one treatment that is a fact is a window that is fully spent.',
    long: [
      'A warning colour is a claim about a threshold. A threshold nobody published is a threshold this console made up.',
      'The broker decides what it will and will not assign against, and it does not publish that figure to the console. A copy of it in the console would drift in silence.',
      'So the bar shows the measurement and marks only the condition that is not a judgement: spent is spent.',
    ],
  },

  'never-assigned-pool': {
    group: 'an-account',
    title: 'Nothing has ever been assigned',
    short:
      'On one row that means nothing: a new account has not been used yet. Across the whole pool it is what a pool no worker can reach looks like — accounts registered, nothing ever asking for one — and it is indistinguishable from a quiet week.',
    long: [
      'The two causes are an idle platform and a platform whose workers cannot reach the broker at all. They produce identical screens.',
      'So the pool-wide case is reported as its own line, with the two configuration mistakes that produce it named: the broker’s address missing from dispatched jobs, or a worker identity without permission to invoke it.',
      'Until a worker asks for an account, every figure on the Accounts screen describes accounts nothing is using.',
    ],
  },

  'unreadable-documents': {
    group: 'an-account',
    title: 'A count is only a fact if every document was read',
    short:
      'The store skips an account document it cannot parse, so that one bad document does not hide the pool. It reports how many it skipped, and this screen says so above the table — because a row count, a headroom figure and a "none need attention" are each computed over what was read.',
    long: [
      'Dropping an unparseable document is the right behaviour: the alternative is a pool that disappears because one record is malformed.',
      'What makes it safe is reporting the shortfall. A list that is quietly short is worse than a list that fails, because everything derived from it looks complete.',
      'Ids are shown for the documents belonging to the tenant reading the screen. The others are counted without being named, for the same reason a lent account’s controls are absent.',
    ],
  },

  'skipped-for-this-tenant': {
    group: 'an-account',
    title: 'Healthy, and still serving nobody here',
    short:
      'When a tenant reports that it could not read an account’s secret, the broker excludes that account for that tenant and for nobody else. Its state stays assignable, its windows stay real, and its headroom is genuinely there — just not for you.',
    long: [
      'Without a line saying so, such a row is the healthiest-looking row on the screen and nothing runs on it.',
      'The report expires, so one that keeps coming back is a missing permission on that secret for that tenant’s worker identity rather than an onboarding delay.',
      'It is reported before the broken accounts are, because a broken account already carries a chip and a reason and this one carries neither.',
    ],
  },

  // THE TIMELINE'S LEDGER (#185, owner decisions 2026-09-25). Three topics the
  // screen's footer index links, each the sentence a lane or a card would
  // otherwise have carried as prose. The one `?` on the screen opens
  // `success-rate`; the other two are reached by the index and by the cards'
  // own descriptions (`explain`), which draw nothing (B7.4).
  'success-rate': {
    group: 'reading-a-figure',
    title: 'The success rate, and what it leaves out',
    short:
      'Tasks that succeeded, over those that succeeded, failed or were dead-lettered, each placed by when it ended. Cancels are counted and drawn in a lane of their own, but left out of the rate. The band is a 95 % Wilson interval; a hollow point has under five decided, and a gap means nothing was decided.',
    long: [
      'The figure on the Timeline screen is the share of decided work that succeeded: tasks that succeeded, over those that succeeded, failed or were dead-lettered, each placed by the moment it ended. Every cancel is left out of it, because a cancel is somebody’s decision rather than a verdict on the work, and the cancels get a lane of their own on the same axis, split into the ones a person asked for, the ones a failure caused, and the ones a cancel caused. A failure’s cancels are counted as the failure’s however many steps down they reach, because a step stopped by a failure stops its own dependants in turn; a cancel’s are the steps below one somebody stopped.',
      'A rate over a handful of tasks says little, so every rate carries its 95 % Wilson interval: the band behind the line, and the range printed with the figure and in the readout. A point drawn hollow covers fewer than five decided tasks. A bucket in which nothing was decided has no point at all and the line breaks there, because a rate over nothing is undefined, never zero.',
      'The comparison with the previous span is dropped, and says why, when that span has a bucket that could not be read or had nothing decided: a delta over part of a span compares two different things.',
      'Tenant scope and platform scope are never drawn side by side. Platform scope is an administrator’s view of every tenant, and the verification tenant can be left out of it in one click; the exclusion names that tenant, so a tenant created later is still counted.',
    ],
    see: ['outcome-buckets', 'failure-classes'],
  },

  'outcome-buckets': {
    group: 'reading-a-figure',
    title: 'Buckets: sealed, so far, and not read',
    short:
      'Every bucket in the span is drawn, and a bucket before any task is a measured zero. The current bucket is dashed at its right edge and reads so far. A bucket the server could not read is hatched across every lane, carries no number and is left out of every total.',
    long: [
      'The Timeline screen asks the API for a real span, from a day to ninety days or a chosen range, and the server buckets it in the viewer’s time zone, choosing the bucket size unless one is picked. Every bucket from the start of the span to now is drawn, the empty ones included: tasks are never deleted, so an empty bucket is a measured zero, drawn as the tick at the axis rather than as a missing column.',
      'Every outcome is placed by the time its task ended. The throughput lane is the one exception: its submitted line is placed by the time each task was created, and its label says so. Work that has not ended has no end time, so it is on no bucket at all; it is counted live, without the span, in the card for work not finished yet.',
      'A bucket is sealed a quarter of an hour after it ends, once nothing more can land in it. Until then it is open: the current bucket reads so far and carries a dashed right edge, and a bucket that has only just ended reads as settling.',
      'A bucket the server could not read within its budget, or at all, is not read: a hatched band across every lane with no number in it, left out of every total, which then carries the partial mark. Reading again continues where the last read stopped. A task that ended with no end time recorded cannot be placed on the axis, and the chart says how many such tasks exist.',
    ],
  },

  'failure-classes': {
    group: 'reading-a-figure',
    title: 'Why a task failed, by class',
    short:
      'Each failed or dead-lettered task gets one class: from the cause the component that ended it recorded, and, for a task that ended before causes were recorded, from its final attempt’s exit and the words of its last error. Other and no reason recorded are always drawn, because an unmatched failure is still a failure.',
    long: [
      'The classes come in a fixed order that no filter re-ranks: runner error, timeout, lost worker, could not start, inputs unavailable, outputs missing, dispatch failed, other, and no reason recorded. They separate a failure of the platform (a lost worker, a task that could not start, a dispatch that failed) from the agent’s own work (a runner error, outputs missing), from what the agent was given (inputs unavailable: a file a step was promised by the step before it, which the worker would not stage, so the agent never started) and from its budget (a timeout).',
      'Since 25 September 2026 every component that ends a task records why, on the task: the worker, the reconciler, the scheduler and a cancel. The API reads that cause first and nothing else. A task that ended before then carries none, and its class comes from its final attempt: the exit that means the worker could not start is read first, and then the last error is matched against the exact words each writer uses. Those words are pinned by tests against their writers, so a reworded message fails a test instead of silently moving counts into other.',
      'Other is a failure whose recorded cause is not one of the nine classes, or, with none recorded, whose last error matched nothing and whose final attempt recorded no exit. No reason recorded is a failure with no cause and no last error at all. Both are measured facts and both are always drawn; a zero is the tick at the axis.',
    ],
  },

  'subscription-only-no-api-key': {
    group: 'an-account',
    title: 'One kind of credential, on purpose',
    short:
      'This pool handles a subscription sign-in and nothing else. The sign-in yields an exchangeable pair the broker can keep alive indefinitely, which is what makes "the only manual step is the first one" true. A key that cannot be exchanged has nothing for the sweep to keep alive.',
    long: [
      'There is one kind, so there is nothing to choose — but a value the request carries and the form never mentions is a decision made for the operator with nothing on screen admitting it. So it is shown, named, and said to be fixed.',
      'An account already registered under another provider cannot be signed in from Accounts, because the request carries no provider field and would file the row under something nobody chose on that screen.',
      'That case is refused and named rather than offered and surprising, and nothing about the account changes.',
    ],
  },

  'refresh-token-required': {
    group: 'an-account',
    title: 'A credential that cannot be exchanged is refused',
    short:
      'A sign-in normally yields a pair: a short-lived token and the half that mints its successors. If the answer carries only the first, the platform stores nothing and refuses. The refusal is the feature.',
    long: [
      'Such a credential cannot be exchanged, so when it expires a person has to log in again.',
      'Accepted, it would look perfectly healthy: the account would sit assignable, agents would be started on it, and at its first expiry every one of them would fail on an expired token with nothing on any screen pointing at the cause.',
      'Being refused costs one error message now. Being accepted costs an outage later, at a time nobody chose.',
    ],
  },

  'clipboard-secure-context': {
    group: 'an-account',
    title: 'When the browser refuses the clipboard',
    short:
      'Browsers refuse clipboard access outside a secure context and when the window is not focused. The link is on screen and selectable, so it can be copied by hand: it is the same value, and nothing is wrong with the sign-in.',
    long: [
      'The button reports the refusal rather than claiming success, so a button that does not say copied has copied nothing.',
      'The value itself is always rendered, so there is never a state in which the only way to obtain the link is a control that failed.',
      'This is also the case where copying by hand matters most: the link is what you carry to a second browser application.',
    ],
  },

  'withheld-total': {
    group: 'reading-a-figure',
    title: 'A total is withheld over a partial response',
    short:
      'A sum is shown only when every part of it arrived. A total over a partial response is a wrong number wearing the clothes of a right one, so the count of what is missing appears instead and no total is drawn at all.',
    long: [
      'Each row can be missing on its own, and a missing row is drawn as an absence rather than a zero. The sum is a different claim: it asserts that everything was counted.',
      'So the rule here is stricter than for a single figure. One row that did not arrive withholds the total entirely, and the panel says how many rows are in that state.',
      'So a withheld total is neither a zero nor a fault in the platform: at least one of its rows is unread, and the total returns once every row arrives.',
    ],
  },

  'blockers-at-an-instant': {
    group: 'reading-a-figure',
    title: 'A refusal was measured at an instant',
    short:
      'These lines are worked out from pool counts read at one moment. Slots are taken and released continuously, so each line says what those counts implied then — not what will happen when a task is actually submitted.',
    long: [
      'Admission is a transaction against live counters. Anything a console computes from a snapshot is a description of that snapshot.',
      'The age of the counts is therefore printed beside the conclusion, because the conclusion is only as current as they are.',
      'Every pool refusing a profile is listed, not only the tightest: two ceilings can bind at the same moment, and somebody shown one of them raises it, tries again, and is refused by the other.',
    ],
  },

  'pool-freshness': {
    group: 'the-platform',
    title: 'A pool’s timestamp is not a change time',
    short:
      'Pools created at provisioning time carry none, the API fills it with the moment of the request, and the broker rewrites it on every provider pool each pass whether or not anything changed. So it is never shown as "last changed".',
    long: [
      'Three different writers touch that field and none of them means "this value changed".',
      'What a reader actually wants — how old the figure on screen is — is the age of the console’s own read of it, and that is what the screens print.',
      'Showing the field as a change time would be the most plausible wrong number on the screen, because it looks exactly like the thing being asked for.',
    ],
  },

  'provider-quota-states': {
    group: 'the-platform',
    title: 'The provider states, and the one that is not health',
    short:
      'A provider quota document carries one of six states. The throttled one is the common case during a squeeze and the easiest to miss. The unknown one means no worker has reported recently: an absence of information, not an assurance.',
    long: [
      'Every state is shown by its own name on the Provider quota screen; none is folded into unknown.',
      'Unknown is drawn as an absence rather than as health. Nothing has reported, and nothing about the provider follows from that.',
      // CP-8 (#85): the column is `Quota cap` now, and the topic names it so.
      // AH-13 (#161): a long paragraph is only ever read on the Help page, so
      // it names the Provider quota screen rather than pointing at one.
      'A quota cap of zero on Provider quota is the opposite: it is returned deliberately when a provider is spent, disabled or cooling down, and it is the one zero there that means something rather than nothing.',
      // CP-9 (#85). The threshold is read from types.ts, not typed here. The
      // tick it is twice of is the broker's SWEEP, which reports nothing
      // (#159 review): this named "the broker's reporting interval", a report
      // that does not exist.
      'A state is only as current as its report, and a worker reports a document at the end of a clean run or on a 429. A reading older than twice the quota broker’s sweep, below, keeps its word but loses the healthy mark and carries its age: a document reported days ago says what was true then. The sweep rewrites a document only when its state changes, so on a quiet tenant every row goes stale, and that is the true age of what it says.',
    ],
    values: () => [
      {
        term: `${Math.round(QUOTA_STALE_AFTER_MS / 60_000)} minutes`,
        note: `twice the broker’s ${Math.round(QUOTA_SWEEP_INTERVAL_SECONDS / 60)}-minute sweep: an older reading is drawn stale`,
      },
    ],
  },

  // CP-8 AND CP-10 (#85): WHAT TWO RENAMED COLUMNS NAME AND CANNOT FINISH
  // SAYING. The column names carry the basis (`Quota cap`) and the window
  // (`429s (this run)`); this is the footer index's topic for the rest, so no
  // `?` goes inside the table.
  'quota-row-fields': {
    group: 'the-platform',
    title: 'The quota cap, and the 429 run',
    short:
      'The quota cap is one input to the pool named beside it; that pool’s own ceiling may be lower, and Pools says which value binds. The 429 count is this run’s: it returns to zero when the run ends, and the last 429 keeps its time.',
    long: [
      'The cap is the lowest of the document’s configured maximum, the adaptive target and the cap the provider’s own limits imply, and a spent, disabled or cooling provider forces it to zero. The broker writes it onto the provider pool of the document’s tenant. That pool is capped by more than the document, so its ceiling on Pools can be lower than the quota cap on Provider quota, and Pools’ “Set by” column says which value is the one that binds.',
      'A rate-limit run starts at a 429. It ends in one of two ways: a clean run reports the provider available again, or the broker retires the cooldown and the reset window once they have passed. Either one puts the count back to zero.',
      'The time of the last 429 is kept when the count is reset. So a zero beside a last 429 a day ago is correct: the run that 429 belonged to is over, and nothing has been rate-limited since.',
    ],
  },

  'quota-document-absent': {
    group: 'the-platform',
    title: 'A provider with no document does not appear',
    short:
      'This route lists quota documents, not providers. A provider no tenant has ever driven has no document, so its absence here means "never used" rather than "no such provider".',
    long: [
      'The tenant-scoped provider route derives its list from the runtime catalogue instead and does not have this gap, which is why the two screens can legitimately disagree about which providers exist.',
      'Reading an absence on Provider quota as "this provider is not configured" is the mistake the distinction exists to prevent.',
      'Nothing is filled in to close the gap, because a row invented for a provider with no document would carry no measurement at all.',
    ],
  },

  'ceiling-change-evicts-nothing': {
    group: 'the-platform',
    title: 'Lowering a ceiling evicts nothing',
    short:
      'A change writes the ceiling and nothing else. The in-use counter belongs to the admission transaction and is never touched here, so running work keeps its slots and the pool simply admits nothing new until it drains.',
    long: [
      'The two numbers have different owners. One is an operator’s decision; the other is the platform’s live accounting, and a console that wrote both would be able to invent capacity.',
      'So a ceiling set below what is currently in use is a legal, quiet state: the pool reads as over its ceiling until enough work finishes.',
      'On Pool limits such a pool carries a neutral “over ceiling” mark, not a fault. Wait for the work in use to finish, or raise the ceiling again if the change was a mistake.',
    ],
  },

  'lease-and-pool-are-two-records': {
    group: 'the-platform',
    title: 'The lease and the pool counter are two records of one fact',
    short:
      'Admission writes the lease and increments every pool in its list in one transaction, so a disagreement between them is never rounding. On its own it is not proof of a leak either: the lease side is computed over the rows this page loaded.',
    long: [
      'Because both sides are written together, they should agree exactly. That is what makes a difference worth looking at rather than tolerating.',
      'What weakens the inference is the page, not the platform: the route returns the newest rows and then drops released ones, so a truncated page can account for fewer units than the pools hold with nothing wrong anywhere.',
      'So the comparison is shown with the number of rows it was computed over, and the conclusion is left to a reader who can see both.',
    ],
  },
}

/**
 * WHERE A TOPIC'S ACTION CAN SEND A READER (#131): the screens a topic is
 * answered on, by the router's own address, with the name the panel gives
 * each. An address here that the router does not know is caught by
 * `src/__tests__/help.guidance.test.tsx`, and a name that drifts from the
 * panel's label is caught there too.
 */
export const HELP_PLACES = {
  'overview/now': 'Overview',
  'work/running': 'Agents',
  'work/workflows': 'Workflows',
  'work/timeline': 'Timeline',
  submit: 'Submit',
  'capacity/pools': 'Ceilings',
  'capacity/profiles': 'By runner profile',
  'capacity/catalogue': 'Runtimes',
  'capacity/holders': 'Holders',
  'capacity/accounts': 'Subscription accounts',
  'capacity/quota': 'Provider quota',
  'admin/limits': 'Pool limits',
  'admin/tenants': 'Tenants',
  'admin/counts': 'Platform counts',
  reference: 'API reads',
} as const

export type HelpPlace = keyof typeof HELP_PLACES

/**
 * What to do about what the topic describes, or where to look (#131: "what
 * you are seeing / what it means / what to do"). It is the last thing a topic
 * draws on the Help page. `at` links the screen that answers it.
 */
export interface HelpAct {
  say: string
  at?: HelpPlace
}

/**
 * THE SUBJECT AND THE ACTION, PER TOPIC (#130, #131).
 *
 * `subject` is the topic's NAME, two to six words -- what the Help index and
 * every footer index (`HelpLinks`) print. The `title` stays the claim, word for
 * word, because the `?` card beside a figure opens with it and renaming it is
 * the owner's choice rather than this record's.
 *
 * Kept as its own `Record<TopicId, ...>` rather than inside each spec so a new
 * topic without one is a type error in one place, and so the claims and
 * paragraphs above are not rewritten to carry it. The prose rule from the
 * header holds here too: no state, park reason, pool name or figure.
 */
const GUIDE: Record<TopicId, { subject: string; act: HelpAct }> = {
  'absent-vs-zero': {
    subject: 'Absent, zero, unread and stale',
    act: { say: 'Read the mark before the number: only a digit is a measurement. A “not read” mark means re-read the screen, not that the figure is zero.' },
  },
  'read-failed': {
    subject: 'A failed read',
    act: { say: 'Re-read the screen, and if the failure persists, check which route is failing and for how long.', at: 'reference' },
  },
  'token-cost': {
    subject: 'Token cost',
    act: { say: 'Treat a missing cost as unknown, not cheap; the attempt’s own record says whether its runner reported tokens.', at: 'work/timeline' },
  },
  'tokens-reported': {
    subject: 'Who reports tokens',
    act: { say: 'Check the runner profile before reading a missing count as zero use.', at: 'capacity/catalogue' },
  },
  'projected-not-measured': {
    subject: 'The tilde mark',
    act: { say: 'Re-read before acting on a figure with a tilde, and check how old its reading is.' },
  },
  'partial-read': {
    subject: 'Several failed reads',
    act: { say: 'Look at each failing route on its own; the message describes only the first.', at: 'reference' },
  },
  'binding-window': {
    subject: 'Utilisation of an account',
    act: { say: 'Watch the window that refuses first; refilling the other one frees nothing.', at: 'capacity/accounts' },
  },
  'all-clear-basis': {
    subject: 'What an all-clear covers',
    act: { say: 'Read how many checks ran before trusting a short list; a check that could not run is not a pass.', at: 'overview/now' },
  },
  'ambiguous-write': {
    subject: 'A write with no answer',
    act: { say: 'Re-read and look for what the write would have made before repeating it.' },
  },
  'success-rate': {
    subject: 'The success rate',
    act: { say: 'Read the band, not the point, when few tasks were decided; read cancels in their own lane.', at: 'work/timeline' },
  },
  'outcome-buckets': {
    subject: 'Timeline buckets',
    act: { say: 'Leave the current bucket out of any comparison until it is sealed, and re-read a hatched one.', at: 'work/timeline' },
  },
  'failure-classes': {
    subject: 'Failure classes',
    act: { say: 'Open a failed task to read its recorded cause; an unmatched class still counts as a failure.', at: 'work/running' },
  },
  'withheld-total': {
    subject: 'A withheld total',
    act: { say: 'Re-read until every part arrives; do not add the parts by hand.' },
  },
  'blockers-at-an-instant': {
    subject: 'Why work is refused',
    act: { say: 'Re-read before acting on a refusal, then look at the pool it names.', at: 'capacity/pools' },
  },
  'attempt-documents': {
    subject: 'When an attempt exists',
    act: { say: 'For a task with no attempt, look at capacity, not at the worker: nothing has been reserved yet.', at: 'capacity/pools' },
  },
  'outcome-explained': {
    subject: 'What an outcome describes',
    act: { say: 'Open the agent’s Artifacts tab for the files, and its Attempts tab for what each earlier attempt did.' },
  },
  'input-as-submitted': {
    subject: 'The submitted input',
    act: { say: 'Read the prompt as the agent received it; the masked count says how much of it is hidden.' },
  },
  'strategy-on-a-task': {
    subject: 'The strategy on a task',
    act: { say: 'Read the outcome for what was published; the chips say only what was asked for.' },
  },
  'event-paging': {
    subject: 'Event paging',
    act: { say: 'Treat an attempt with no events on the page as unknown; its later events may be on a page not fetched.' },
  },
  'masking-is-serve-time': {
    subject: 'Artifact masking',
    act: { say: 'If the masked count is above zero, rotate the credential that was found; masking does not clean the bucket.' },
  },
  'checkpoints': {
    subject: 'What a checkpoint holds',
    act: { say: 'Open an agent’s checkpoints to see how recent its last one is and how much a restart would lose.', at: 'work/running' },
  },
  'cpu-figures': {
    subject: 'CPU figures',
    act: { say: 'Compare the peak with the limit; a peak at the limit means the size is too small for the work.', at: 'capacity/catalogue' },
  },
  'oom-near-miss': {
    subject: 'The memory near-miss flag',
    act: { say: 'When the flag is set, pick a larger size for that work before it is killed.', at: 'capacity/catalogue' },
  },
  'peak-memory': {
    subject: 'Peak memory',
    act: { say: 'Wait for the attempt to end before reading its peak; a missing one after the end is unknown, not small.' },
  },
  'api-reads': {
    subject: 'The reads behind a screen',
    act: { say: 'Open the reads to see which route is failing and how old its last good payload is.', at: 'reference' },
  },
  capacity: {
    subject: 'What reserves capacity',
    act: { say: 'To see what is holding capacity now, look at the holders.', at: 'capacity/holders' },
  },
  'requests-are-ceilings': {
    subject: 'Requests equal limits',
    act: { say: 'Treat a bar near its limit as a risk, and move that work to a larger size.', at: 'capacity/catalogue' },
  },
  states: {
    subject: 'Task states',
    act: { say: 'Filter the agent list by state to see which tasks hold capacity and which are waiting.', at: 'work/running' },
  },
  'workspace-memory': {
    subject: 'Workspace memory',
    act: { say: 'Count the workspace against the memory ceiling; a large clone needs a larger size.', at: 'capacity/catalogue' },
  },
  'pools-all-at-once': {
    subject: 'Every pool or none',
    act: { say: 'Raise the pool that binds, not another one; the runner-profile view outlines it.', at: 'capacity/profiles' },
  },
  'units-not-agents': {
    subject: 'Units, not agents',
    act: { say: 'Divide a pool’s headroom by the size’s weight to get agents.', at: 'capacity/profiles' },
  },
  'what-a-pool-is': {
    subject: 'What a pool is',
    act: { say: 'See every pool, its ceiling and what is in use now.', at: 'capacity/pools' },
  },
  'paused-vs-full': {
    subject: 'Paused or full',
    act: { say: 'A full pool admits again as work finishes. A paused one needs an operator to resume it; raising its ceiling does not.', at: 'capacity/pools' },
  },
  'admin-gate-not-failure': {
    subject: 'Admin-only routes',
    act: { say: 'Nothing to fix. To read these routes, ask for the platform admin group.' },
  },
  'tenant-fields': {
    subject: 'Tenants columns',
    act: { say: 'To change a tenant’s ceiling, follow its Enforced figure to the pool it sets.', at: 'admin/tenants' },
  },
  'platform-counts': {
    subject: 'Platform counts',
    act: { say: 'Run the count when you need the figures; it is priced per run, so it does not refresh on its own.', at: 'admin/counts' },
  },
  'poll-cadence': {
    subject: 'What re-polls',
    act: { say: 'Press refresh on a figure that does not re-poll, and read its age before comparing it.' },
  },
  'tenant-scope': {
    subject: 'Whose figures these are',
    act: { say: 'Read a ceiling as your own tenant’s; for every tenant’s task counts, run Platform counts.', at: 'admin/counts' },
  },
  'park-on-missing-credential': {
    subject: 'A missing credential',
    act: { say: 'Register an account for that provider, or the task waits for ever.', at: 'capacity/accounts' },
  },
  'pool-freshness': {
    subject: 'A pool’s timestamp',
    act: { say: 'Do not read a pool’s timestamp as a change time; Pool limits says what is recorded about changes.', at: 'admin/limits' },
  },
  'provider-quota-states': {
    subject: 'Provider quota states',
    act: { say: 'Look for throttled providers first, and treat unknown as no information.', at: 'capacity/quota' },
  },
  'quota-row-fields': {
    subject: 'Quota cap and the 429 run',
    act: { say: 'Check which value binds the pool before raising anything.', at: 'capacity/pools' },
  },
  'quota-document-absent': {
    subject: 'A provider with no document',
    act: { say: 'Nothing to fix: the provider has not been driven yet.', at: 'capacity/quota' },
  },
  'ceiling-change-evicts-nothing': {
    subject: 'Lowering a ceiling',
    act: { say: 'To free capacity now, cancel work; lowering a ceiling only stops new work being admitted.', at: 'admin/limits' },
  },
  'lease-and-pool-are-two-records': {
    subject: 'Leases and pool counters',
    act: { say: 'Compare the two over a full page before calling a difference a leak.', at: 'capacity/holders' },
  },
  'runner-profile-by-name': {
    subject: 'Runner profiles by name',
    act: { say: 'Pick a runner profile by name; to see what each name decides, read the runtimes.', at: 'capacity/catalogue' },
  },
  'not-a-machine-inventory': {
    subject: 'Dispatch topology',
    act: { say: 'For what is running now, look at the agents, not the runtimes.', at: 'work/running' },
  },
  'declared-vs-resolved-backend': {
    subject: 'Declared and resolved backend',
    act: { say: 'Read the resolved backend for where work goes.', at: 'capacity/catalogue' },
  },
  'catalogue-from-route': {
    subject: 'Where the runtimes come from',
    act: { say: 'Re-read the runtimes to see a catalogue change; nothing in the console needs editing.', at: 'capacity/catalogue' },
  },
  'credential-names-not-values': {
    subject: 'Credential names',
    act: { say: 'To check what your tenant holds, look at its accounts.', at: 'capacity/accounts' },
  },
  'what-sets-it-apart-is-arithmetic': {
    subject: 'Runtime differences and disabled',
    act: { say: 'Move work off a disabled runtime; the card gives the reason it was disabled.', at: 'capacity/catalogue' },
  },
  'runtime-needs-no-provider': {
    subject: 'Runtimes without a credential',
    act: { say: 'Nothing to register: these run for any tenant.', at: 'capacity/catalogue' },
  },
  'dispatch-strategies': {
    subject: 'Dispatch strategies',
    act: { say: 'Pick the strategy by what you want published, and read the count on the option before submitting.', at: 'submit' },
  },
  'integrate-needs-final-step': {
    subject: 'Integrating into one pull request',
    act: { say: 'Give the workflow exactly one step nothing depends on, or pick another strategy.', at: 'submit' },
  },
  'dispatch-carrier': {
    subject: 'The step carrier',
    act: { say: 'Pick it as a recorded preference only; nothing reads it yet.', at: 'submit' },
  },
  'repository-url': {
    subject: 'The repository url',
    act: { say: 'Give a repository url for any strategy that pushes.', at: 'submit' },
  },
  'dispatch-absent-is-old-api': {
    subject: 'A missing dispatch block',
    act: { say: 'Read it as an older deployment; it says nothing about what the run published.' },
  },
  'input-is-opaque': {
    subject: 'The task input',
    act: { say: 'Write the input for the agent; check it yourself, because nothing else will.', at: 'submit' },
  },
  'workflow-stages': {
    subject: 'Workflow stages',
    act: { say: 'Open a workflow to see which stage each step is in and what a failure cancelled.', at: 'work/workflows' },
  },
  'runner-unavailable': {
    subject: 'An unavailable runner',
    act: { say: 'Pick a runner you can use; ask a platform administrator to enable another.', at: 'capacity/profiles' },
  },
  'recent-submissions': {
    subject: 'Recent submissions',
    act: { say: 'Start a new task, workflow or issue run from the cards above.', at: 'submit' },
  },
  'room-unknown-not-zero': {
    subject: 'Unknown room',
    act: { say: 'Re-read the pools before deciding there is no room.', at: 'capacity/pools' },
  },
  'sign-in-not-paste': {
    subject: 'Adding an account',
    act: { say: 'Start the sign-in from the account pool.', at: 'capacity/accounts' },
  },
  'credential-split': {
    subject: 'The two account secrets',
    act: { say: 'Nothing to do: the split is automatic. Revoke the account’s sign-in if a pod is compromised.' },
  },
  'credential-refresh-sweep': {
    subject: 'Credential refresh',
    act: { say: 'Leave refreshing to the sweep; press refresh on one account only to check it.', at: 'capacity/accounts' },
  },
  'refresh-now-probe': {
    subject: 'The refresh button',
    act: { say: 'Press it to find out whether one account’s credential can be exchanged now.', at: 'capacity/accounts' },
  },
  'reauth-required': {
    subject: 'Re-authentication required',
    act: { say: 'Sign in again under the same label; nothing else clears it.', at: 'capacity/accounts' },
  },
  'reauth-does-not-unpause': {
    subject: 'Signing in keeps state',
    act: { say: 'After signing in, set the account’s state back yourself if it was stopped.', at: 'capacity/accounts' },
  },
  'second-browser-application': {
    subject: 'A second organisation',
    act: { say: 'Use a second browser application, not a private window, to sign in as another organisation.' },
  },
  'signin-paste-the-code': {
    subject: 'Pasting the code',
    act: { say: 'Paste the whole string the callback page shows.' },
  },
  'signin-deadlines': {
    subject: 'Sign-in deadlines',
    act: { say: 'Paste the code soon after the callback page shows it; the provider’s clock is not shown.' },
  },
  'signin-verify-by-reload': {
    subject: 'Checking a sign-in landed',
    act: { say: 'Reload the account pool and look for the label before signing in again.', at: 'capacity/accounts' },
  },
  'signin-201-no-name': {
    subject: 'An unnamed success',
    act: { say: 'Reload the account pool to find the account by its label.', at: 'capacity/accounts' },
  },
  'signin-is-over': {
    subject: 'An expired sign-in',
    act: { say: 'Reload the account pool; if the label is not there, start a new sign-in.', at: 'capacity/accounts' },
  },
  'signin-still-open': {
    subject: 'A refused paste',
    act: { say: 'Paste the code again; the sign-in is still waiting.' },
  },
  'signin-route-missing': {
    subject: 'Sign-in not deployed',
    act: { say: 'Ask an operator to deploy an API that serves the sign-in route; nothing was created.' },
  },
  'signin-is-anthropics-page': {
    subject: 'The provider’s sign-in page',
    act: { say: 'Sign in on the provider’s page as you normally would, then paste the code it shows.' },
  },
  'signin-holds-label-and-lending': {
    subject: 'Label and lending at sign-in',
    act: { say: 'To change either, start a new sign-in; lending can also be edited on the account afterwards.', at: 'capacity/accounts' },
  },
  'signin-keeps-readings': {
    subject: 'Replacing a credential',
    act: { say: 'Sign in under the existing label to replace only the credential.', at: 'capacity/accounts' },
  },
  'account-label-rules': {
    subject: 'Account labels',
    act: { say: 'Use lowercase letters, digits and dashes, short.' },
  },
  'account-owned-by-one-tenant': {
    subject: 'Account ownership',
    act: { say: 'Check the tenant the session names before starting a sign-in.', at: 'capacity/accounts' },
  },
  lending: {
    subject: 'Lending an account',
    act: { say: 'Name the tenants to lend to on the account, and edit the list later as needed.', at: 'capacity/accounts' },
  },
  'lending-narrows-isolation': {
    subject: 'Lending and isolation',
    act: { say: 'Lend only to tenants you trust with the account’s token, and take the lending back when it is no longer needed.', at: 'capacity/accounts' },
  },
  'lent-account': {
    subject: 'An account lent to you',
    act: { say: 'Ask the owning tenant to pause, drain or re-sign-in the account.' },
  },
  'advisory-vs-lease': {
    subject: 'The advisory agent count',
    act: { say: 'For who really holds what, read the holders.', at: 'capacity/holders' },
  },
  'account-states': {
    subject: 'Account states',
    act: { say: 'Pause to stop new work, drain to move it off, and sign in again to clear the verdict.', at: 'capacity/accounts' },
  },
  'state-change-reason': {
    subject: 'State-change reasons',
    act: { say: 'Give a reason with every state change; the next reader needs it.', at: 'capacity/accounts' },
  },
  'account-removal-is-reversible': {
    subject: 'Removing an account',
    act: { say: 'To bring a removed account back, sign in again under the same label.', at: 'capacity/accounts' },
  },
  'provider-defines-windows': {
    subject: 'Provider windows',
    act: { say: 'Open the account row to read a window that has no column of its own.', at: 'capacity/accounts' },
  },
  'accounts-table-shape': {
    subject: 'The accounts table',
    act: { say: 'Open a row for everything the table does not show.', at: 'capacity/accounts' },
  },
  'no-amber-band': {
    subject: 'No amber band',
    act: { say: 'Watch for a window that is fully spent; nothing short of that is a verdict.', at: 'capacity/accounts' },
  },
  'never-assigned-pool': {
    subject: 'A never-assigned pool',
    act: { say: 'If no account in the pool was ever assigned, check that workers can reach it.', at: 'capacity/accounts' },
  },
  'unreadable-documents': {
    subject: 'Unreadable account documents',
    act: { say: 'When documents were skipped, read every count over the pool as a lower bound, and fix the bad document.', at: 'capacity/accounts' },
  },
  'skipped-for-this-tenant': {
    subject: 'Skipped for your tenant',
    act: { say: 'Fix your tenant’s access to the account’s secret; the account is healthy.', at: 'capacity/accounts' },
  },
  'subscription-only-no-api-key': {
    subject: 'Subscription sign-ins only',
    act: { say: 'Add the account by signing in; an API key has nowhere to go.', at: 'capacity/accounts' },
  },
  'refresh-token-required': {
    subject: 'A refused credential',
    act: { say: 'Sign in again so the answer carries both halves.', at: 'capacity/accounts' },
  },
  'clipboard-secure-context': {
    subject: 'A refused clipboard',
    act: { say: 'Select the link on screen and copy it by hand.' },
  },
}

/**
 * The exported record. `anchor` is filled in here, from the id, so the two can
 * never disagree and no caller can invent a third spelling. `subject` and `act`
 * come from `GUIDE`.
 */
export const HELP: Readonly<Record<TopicId, HelpTopic>> = Object.freeze(
  Object.fromEntries(
    (Object.keys(SPECS) as TopicId[]).map((id) => [id, { ...SPECS[id], ...GUIDE[id], anchor: helpAnchor(id) }]),
  ) as Record<TopicId, HelpTopic>,
)

/** Every topic id, in the order the Help section renders them. */
export const TOPIC_IDS: readonly TopicId[] = HELP_GROUPS.flatMap((g) =>
  (Object.keys(HELP) as TopicId[]).filter((id) => HELP[id].group === g.id),
)

/** A topic by id, or null. Used by the router, which gets its input from a URL. */
export function topicFor(id: string): HelpTopic | null {
  return Object.prototype.hasOwnProperty.call(HELP, id) ? HELP[id as TopicId] : null
}

/**
 * `REAL_STATES` order, filtered to a set.
 *
 * `CONCURRENCY_STATES` is a Set and sets have no meaningful order, so the
 * order shown is the one `REAL_STATES` declares — which is the lifecycle
 * order a reader expects. Still no literal: both come from types.ts.
 */
function orderedStates(set: ReadonlySet<TaskState>): readonly TaskState[] {
  return REAL_STATES.filter((s) => set.has(s))
}

