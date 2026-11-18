/**
 * VOICE IT'S CARD CATALOGUE, AS THE CONSOLE NAMES IT (docs/design/voice-it.md
 * §4). The schema itself is swarm-api's (swarm_api/voice/cards.py): it
 * validates every card write and is served to the planner as strict tool
 * schemas. This file is the console's side of the same list: every kind, the
 * fields every card carries, each kind's content, body fields, states and
 * actions, the content types with the renderer each names, and the common
 * actions. tests/unit/control_plane/test_voice_cards.py reads it and fails
 * when the two sides name different kinds, fields, states, actions or
 * contents, the way test_issue_forms.py holds the forms to SECTIONS.
 *
 * The console draws only the kinds listed here; a card of any other kind is
 * not drawn. A credential card names a secret and never carries its value: it
 * has no field that could hold one.
 *
 * KEEP THE LITERALS PLAIN: one quoted string per name, so the test can read
 * them without a TypeScript parser.
 */

export const CARD_STATES = [
  'proposed',
  'acting',
  'waiting',
  'done',
  'failed',
  'cancelled',
  'dismissed',
  'accepted',
] as const

export type CardState = (typeof CARD_STATES)[number]

/** Who created a card. */
export const CARD_CREATORS = ['planner', 'platform', 'person'] as const

export type CardCreator = (typeof CARD_CREATORS)[number]

/** What a card may point at. */
export const CARD_REFERENCES = [
  'agent',
  'workflow',
  'run',
  'schedule',
  'pull_request',
  'issue',
] as const

export type CardReference = (typeof CARD_REFERENCES)[number]

/** The fields every card carries, in swarm-api's order. */
export const CARD_FIELDS = [
  'card_id',
  'kind',
  'project_id',
  'session_id',
  'version',
  'state',
  'title',
  'created_by',
  'references',
  'summary',
  'at',
  'outcome',
  'body',
] as const

export type CardField = (typeof CARD_FIELDS)[number]

/**
 * How a card's body is drawn (§3.13.6): a content type and the renderer that
 * draws it, or `generic` (its fields as text), which also draws any kind this
 * console does not know. Several kinds may share one content. The renderers
 * themselves are the viewer's; this list only names them. `frames` draws
 * only inside a sandboxed frame: its script may run, never with
 * allow-same-origin and never with network (question 20).
 */
export interface CardContentSpec {
  content: string
  renderer: string
}

export const CARD_CONTENTS: readonly CardContentSpec[] = [
  { content: 'options', renderer: 'options' },
  { content: 'frames', renderer: 'frames' },
  { content: 'graph', renderer: 'graph' },
  { content: 'code', renderer: 'code' },
  { content: 'diff', renderer: 'diff' },
  { content: 'markdown', renderer: 'markdown' },
  { content: 'tests', renderer: 'tests' },
  { content: 'work', renderer: 'work' },
  { content: 'review', renderer: 'review' },
  { content: 'plan', renderer: 'plan' },
  { content: 'budget', renderer: 'budget' },
  { content: 'stop', renderer: 'stop' },
  { content: 'details_input', renderer: 'details_input' },
  { content: 'credential_input', renderer: 'credential_input' },
  { content: 'rescue', renderer: 'rescue' },
  { content: 'readback', renderer: 'readback' },
  { content: 'navigate', renderer: 'navigate' },
  { content: 'generic', renderer: 'generic' },
]

/**
 * What every kind also takes. Open full size is the console's alone and is
 * never posted; a comment waits for the next planner turn, and Send
 * (`send_comments`) sends the waiting comments now: an action, not a state.
 */
export const COMMON_ACTIONS = ['open_full_size', 'comment', 'send_comments', 'ask_about', 'dismiss'] as const

export type CommonAction = (typeof COMMON_ACTIONS)[number]

/** The actions that decide a card and leave its server-set `outcome`. */
export const DECIDING_ACTIONS = ['pick', 'mix', 'pick_frame', 'approve', 'dismiss'] as const

/** A person's deciding action on a card, as swarm-api stores it. */
export interface CardOutcome {
  action: (typeof DECIDING_ACTIONS)[number]
  summary: string
  by: 'person'
  at: string
}

export interface CardKindSpec {
  kind: string
  label: string
  /** How its body is drawn: a `CARD_CONTENTS` name. */
  content: string
  /** The body's top-level fields. */
  fields: readonly string[]
  states: readonly CardState[]
  /** Its own actions; every kind also takes `COMMON_ACTIONS`. */
  actions: readonly string[]
}

export const CARD_KINDS: readonly CardKindSpec[] = [
  {
    kind: 'work',
    label: 'Work',
    content: 'work',
    fields: ['target_kind', 'target_id', 'repository', 'lanes', 'live_state'],
    states: ['proposed', 'acting', 'waiting', 'done', 'failed', 'cancelled'],
    actions: ['open_in_work', 'cancel', 'pause', 'resume', 'hold'],
  },
  {
    kind: 'review',
    label: 'Review',
    content: 'review',
    fields: ['change_summary', 'checks', 'verdict', 'merge_state', 'release_state'],
    states: ['waiting', 'done', 'failed'],
    actions: ['open_diff', 'ask_for_change', 'merge'],
  },
  {
    kind: 'proposal',
    label: 'Proposal',
    content: 'options',
    fields: ['question', 'options', 'recommendation'],
    states: ['proposed', 'done', 'dismissed'],
    actions: ['pick', 'mix', 'ask_again'],
  },
  {
    kind: 'diagram',
    label: 'Diagram',
    content: 'graph',
    fields: ['diagram_type', 'source', 'graph', 'blob_key', 'repaired'],
    states: ['proposed', 'done', 'failed'],
    actions: ['edit_by_voice', 'file_issue_from_node'],
  },
  {
    kind: 'mockup',
    label: 'Mock-up',
    content: 'frames',
    fields: ['drawn_by', 'frames', 'viewport', 'height'],
    states: ['proposed', 'waiting', 'done'],
    actions: ['pick_frame', 'comment'],
  },
  {
    kind: 'wireframe',
    label: 'Wireframe',
    content: 'frames',
    fields: ['frames', 'viewport', 'height', 'annotations'],
    states: ['proposed', 'done', 'dismissed'],
    actions: ['approve', 'comment', 'ask_again'],
  },
  {
    kind: 'code',
    label: 'Code',
    content: 'code',
    fields: ['repository', 'path', 'ref', 'language', 'first_line', 'text'],
    states: ['proposed', 'done'],
    actions: ['comment', 'ask_again'],
  },
  {
    kind: 'diff',
    label: 'Diff',
    content: 'diff',
    fields: ['repository', 'pull_request', 'files', 'blob_key'],
    states: ['proposed', 'waiting', 'done'],
    actions: ['approve', 'ask_for_change', 'comment'],
  },
  {
    kind: 'document',
    label: 'Document',
    content: 'markdown',
    fields: ['doc_type', 'markdown', 'headings'],
    states: ['proposed', 'done'],
    actions: ['approve', 'comment', 'ask_again'],
  },
  {
    kind: 'test_results',
    label: 'Test results',
    content: 'tests',
    fields: ['source', 'totals', 'cases'],
    states: ['waiting', 'done', 'failed'],
    actions: ['rerun_failed', 'file_issue'],
  },
  {
    kind: 'details_input',
    label: 'Details',
    content: 'details_input',
    fields: ['question', 'options', 'free_text', 'answer', 'task_id'],
    states: ['waiting', 'done'],
    actions: ['answer'],
  },
  {
    kind: 'credential_input',
    label: 'Credential',
    content: 'credential_input',
    fields: ['name', 'provider', 'why', 'status'],
    states: ['waiting', 'done'],
    actions: ['set_it'],
  },
  {
    kind: 'plan',
    label: 'Plan',
    content: 'plan',
    fields: ['goal', 'size', 'waves', 'reserved_usd', 'session_cap_usd', 'day_cap_usd'],
    states: ['proposed', 'acting', 'done'],
    actions: ['start', 'change', 'split'],
  },
  {
    kind: 'budget',
    label: 'Budget',
    content: 'budget',
    fields: ['lines', 'spent_usd', 'reserved_usd', 'session_cap_usd', 'day_cap_usd', 'not_counted'],
    states: ['waiting', 'done'],
    actions: ['raise'],
  },
  {
    kind: 'stop',
    label: 'Stop',
    content: 'stop',
    fields: ['what', 'rule', 'where'],
    states: ['waiting', 'done'],
    actions: ['open_where'],
  },
  {
    kind: 'rescue',
    label: 'Rescue',
    content: 'rescue',
    fields: ['subject', 'failure', 'cause', 'evidence', 'fix'],
    states: ['proposed', 'acting', 'done'],
    actions: ['fix_it', 'explain_more'],
  },
  {
    kind: 'readback',
    label: 'Read-back',
    content: 'readback',
    fields: ['readback_id', 'heard', 'action', 'target_name', 'status', 'expires_at'],
    states: ['waiting', 'acting', 'done', 'dismissed', 'cancelled'],
    actions: ['go', 'not_that'],
  },
  {
    kind: 'spec',
    label: 'Spec',
    content: 'markdown',
    fields: ['markdown', 'headings'],
    states: ['proposed', 'done'],
    actions: ['comment'],
  },
  {
    kind: 'navigate',
    label: 'Navigate',
    content: 'navigate',
    fields: ['destination', 'object', 'why', 'accepted_by'],
    states: ['proposed', 'accepted', 'done', 'dismissed'],
    actions: ['open'],
  },
]
