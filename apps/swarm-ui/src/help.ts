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

import { CONCURRENCY_STATES, REAL_STATES, type TaskState } from './types'

/**
 * Every topic. Adding a member here without adding an entry to `TOPICS` is a
 * type error, which is the point: `Record<TopicId, ...>` is exhaustive.
 */
export type TopicId =
  | 'absent-vs-zero'
  | 'attempt-documents'
  | 'capacity'
  | 'checkpoints'
  | 'cpu-not-sampled'
  | 'oom-near-miss'
  | 'peak-memory'
  | 'read-failed'
  | 'requests-are-ceilings'
  | 'states'
  | 'token-cost'
  | 'tokens-reported'
  | 'workspace-memory'

/** The Help section's headings, in the order it renders them. */
export type HelpGroupId = 'reading-a-figure' | 'the-platform' | 'an-attempt'

export const HELP_GROUPS: readonly { id: HelpGroupId; title: string }[] = [
  { id: 'reading-a-figure', title: 'Reading a figure' },
  { id: 'an-attempt', title: 'Reading an attempt' },
  { id: 'the-platform', title: 'How the platform behaves' },
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
  group: HelpGroupId
}

/** The route the Help section lives at. One spelling, used by the router. */
export const HELP_ROUTE = 'help'

/** `#help/absent-vs-zero`. The only place this string is built. */
export function helpAnchor(topic: TopicId): string {
  return `${HELP_ROUTE}/${topic}`
}

type TopicSpec = Omit<HelpTopic, 'anchor'>

const SPECS: Record<TopicId, TopicSpec> = {
  'absent-vs-zero': {
    group: 'reading-a-figure',
    title: 'Absent is not zero',
    short:
      'A figure this platform never measured is written as a phrase, on a dashed tile. A figure it did measure is written as a digit. The two never share a shape, so a missing number can never be read as a small one.',
    long: [
      'Every number on these screens is one of three things: measured, never measured, or not read. They are three different facts and drawing them alike was this UI’s defining bug.',
      'A measured figure renders as a digit on a solid tile — including a measured zero, which is a real result and is shown as one. A figure the platform never recorded renders as a short phrase on a dashed tile in the faint colour, because a phrase cannot be mistaken for a quantity and a zero can. A figure a failed read left behind renders as a phrase too, but in the warning colour, because the platform may well hold the number and we simply did not get it.',
      'The same rule runs through the bars: a track whose ceiling could not be read is hatched with no fill, because an empty plain track reads as “0% used” — a claim about a measurement nobody has.',
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
      'Nothing writes a manifest of an archive’s contents, so no screen can list the files in a checkpoint. The id, the size and the uri are the whole record, and the uri is what to fetch. A screen that drew a file tree here would be inventing it.',
      'A checkpoint count of zero is a measured zero — it means no attempt document lists one. It is not a failed read, and it is shown as a digit for that reason.',
    ],
  },

  'cpu-not-sampled': {
    group: 'an-attempt',
    title: 'CPU is never sampled',
    short:
      'The sampler measures memory and disk. The cpu bar is drawn with its request and a hatched track rather than left out, because a requested-against-used panel that silently drops part of the envelope reads as if cpu were known to be fine.',
    long: [
      'The worker’s sampler records memory and disk. It does not record cpu.',
      'The cpu bar is still drawn, with its requested figure and a hatched track. Omitting it would be worse: a panel headed “requested vs utilised” that quietly shows two of three dimensions invites the reader to conclude the third was fine, which is a conclusion nobody measured.',
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

  'peak-memory': {
    group: 'an-attempt',
    title: 'When peak memory is written',
    short:
      'Peak memory is written when an attempt ends. An attempt still running has none, so the figure is absent rather than zero, and a run whose peak is absent may have used any amount at all.',
    long: [
      'The worker writes peak resident memory at the end of an attempt, from the sampler it ran throughout. An attempt that is still going has not written it yet.',
      'A run-level peak is the worst single attempt, not a total across attempts, and the tile says so. A total would be meaningless: three attempts of 600 MiB each did not use 1.8 GiB at any moment.',
      'When no attempt has written one, the tile shows a phrase rather than a zero. A zero would assert the run used no memory, which is the one thing that cannot be true.',
    ],
  },

  'read-failed': {
    group: 'reading-a-figure',
    title: 'A failed read is not an empty result',
    short:
      'When a query fails, no number derived from it appears anywhere — not a zero, not a blank. A failed read says nothing about the platform, and the panel says which of the two happened.',
    long: [
      'This app exists because of one bug: a failed probe rendered as an absence. A sweep of the platform’s operational scripts found 56 places where a read failure was printed as “nothing to report”, including a status tool that said “no services deployed” when a session had simply expired.',
      'So every read here returns a value that forces the question. There is no path to the rows that does not decide, separately, what an empty answer means and what a missing answer means. A component cannot render an empty list for a permission error, because a permission error never produces a list.',
      'On screen the two are told apart without colour: a failed panel carries its own marker and its own heading, and no figure below it may be treated as a measurement.',
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
      'The states a task document can actually hold are listed below, with the ones that reserve capacity marked.',
    ],
    // READ from types.ts. Nothing below is typed into this file.
    values: () =>
      REAL_STATES.map((s) => ({
        term: s,
        note: CONCURRENCY_STATES.has(s) ? 'reserves capacity' : 'waiting \u2014 costs nothing',
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
      'Nothing here includes compute, storage or database cost. No billing integration exists to read them from.',
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
    ],
  },

  'workspace-memory': {
    group: 'the-platform',
    title: 'The workspace is a slice of memory',
    short:
      'The workspace is a memory-backed filesystem, not capacity on top of the memory ceiling. Workspace bytes come out of the same allowance the agent runs in.',
    long: [
      'The Terraform provider cannot express the disk-backed workspace this platform originally wanted — the field it would need accepts only the memory-backed form — so the workspace is a tmpfs.',
      'That has one consequence worth stating plainly: files the agent writes consume the memory ceiling shown above them. A workspace is not extra capacity, and a large checkout is indistinguishable, to the OOM killer, from a large process.',
      'The path this leaves the deployment on is the fully-supported one, which does support live migration. That covers infrastructure moves. It does not cover a cancellation, a reclaim or a crash, which is why checkpointing is still mandatory.',
    ],
  },
}

/**
 * The exported record. `anchor` is filled in here, from the id, so the two can
 * never disagree and no caller can invent a third spelling.
 */
export const HELP: Readonly<Record<TopicId, HelpTopic>> = Object.freeze(
  Object.fromEntries(
    (Object.keys(SPECS) as TopicId[]).map((id) => [id, { ...SPECS[id], anchor: helpAnchor(id) }]),
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

