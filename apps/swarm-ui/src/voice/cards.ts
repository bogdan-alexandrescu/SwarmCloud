/**
 * VOICE IT'S CARD CATALOGUE, AS THE CONSOLE NAMES IT (docs/design/voice-it.md
 * §4). The schema itself is swarm-api's (swarm_api/voice/cards.py): it
 * validates every card write and is served to the planner as strict tool
 * schemas. This file is the console's side of the same list: every kind, the
 * fields every card carries, each kind's body fields, its states and its
 * actions. tests/unit/control_plane/test_voice_cards.py reads it and fails
 * when the two sides name different kinds, fields, states or actions, the
 * way test_issue_forms.py holds the forms to SECTIONS.
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
  'body',
] as const

export type CardField = (typeof CARD_FIELDS)[number]

export interface CardKindSpec {
  kind: string
  label: string
  /** The body's top-level fields. */
  fields: readonly string[]
  states: readonly CardState[]
  actions: readonly string[]
}

export const CARD_KINDS: readonly CardKindSpec[] = [
  {
    kind: 'work',
    label: 'Work',
    fields: ['target_kind', 'target_id', 'repository', 'lanes', 'live_state'],
    states: ['proposed', 'acting', 'waiting', 'done', 'failed', 'cancelled'],
    actions: ['open_in_work', 'cancel', 'pause', 'resume', 'hold'],
  },
  {
    kind: 'review',
    label: 'Review',
    fields: ['change_summary', 'checks', 'verdict', 'merge_state', 'release_state'],
    states: ['waiting', 'done', 'failed'],
    actions: ['open_diff', 'ask_for_change', 'merge'],
  },
  {
    kind: 'proposal',
    label: 'Proposal',
    fields: ['question', 'options', 'recommendation'],
    states: ['proposed', 'done', 'dismissed'],
    actions: ['pick', 'mix', 'ask_again'],
  },
  {
    kind: 'diagram',
    label: 'Diagram',
    fields: ['diagram_type', 'source', 'graph', 'blob_key', 'repaired'],
    states: ['proposed', 'done', 'failed'],
    actions: ['edit_by_voice', 'open_full_size', 'file_issue_from_node'],
  },
  {
    kind: 'mockup',
    label: 'Mock-up',
    fields: ['drawn_by', 'frames'],
    states: ['proposed', 'waiting', 'done'],
    actions: ['pick_frame', 'comment', 'open_full_size'],
  },
  {
    kind: 'details_input',
    label: 'Details',
    fields: ['question', 'options', 'free_text', 'answer', 'task_id'],
    states: ['waiting', 'done'],
    actions: ['answer'],
  },
  {
    kind: 'credential_input',
    label: 'Credential',
    fields: ['name', 'provider', 'why', 'status'],
    states: ['waiting', 'done'],
    actions: ['set_it'],
  },
  {
    kind: 'plan',
    label: 'Plan',
    fields: ['goal', 'size', 'waves', 'reserved_usd', 'session_cap_usd', 'day_cap_usd'],
    states: ['proposed', 'acting', 'done'],
    actions: ['start', 'change', 'split'],
  },
  {
    kind: 'budget',
    label: 'Budget',
    fields: ['lines', 'spent_usd', 'reserved_usd', 'session_cap_usd', 'day_cap_usd', 'not_counted'],
    states: ['waiting', 'done'],
    actions: ['raise'],
  },
  {
    kind: 'stop',
    label: 'Stop',
    fields: ['what', 'rule', 'where'],
    states: ['waiting', 'done'],
    actions: ['open_where'],
  },
  {
    kind: 'rescue',
    label: 'Rescue',
    fields: ['subject', 'failure', 'cause', 'evidence', 'fix'],
    states: ['proposed', 'acting', 'done'],
    actions: ['fix_it', 'explain_more'],
  },
  {
    kind: 'readback',
    label: 'Read-back',
    fields: ['readback_id', 'heard', 'action', 'target_name', 'status', 'expires_at'],
    states: ['waiting', 'acting', 'done', 'dismissed', 'cancelled'],
    actions: ['go', 'not_that'],
  },
  {
    kind: 'spec',
    label: 'Spec',
    fields: ['markdown', 'headings'],
    states: ['proposed', 'done'],
    actions: ['comment', 'open_full_size'],
  },
]
