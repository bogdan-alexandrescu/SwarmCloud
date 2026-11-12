// MOCK-HOOK FILE
/**
 * MOCK-UP DATA for the Voice it projects direction "tabs" (?voiceprojects=tabs).
 * Invented projects, people and numbers only: nothing here is served, and the
 * brief, the states that move and the catch-up are not built server-side.
 */

export type ProjectState = 'progress' | 'waiting' | 'completed' | 'cancelled' | 'archived'

export interface MockBrief {
  goal: string
  done: string[]
  decided: string[]
  open: string[]
  next: string[]
}

export interface MockProject {
  id: string
  title: string
  state: ProjectState
  /** When its last session started, for sorting. */
  lastAt: string
  /** The same, as the list says it. */
  lastWord: string
  openCards: number
  /** The one line the row shows: the next step, the outcome, or the reason. */
  line: string
  /** Waiting only: what it asks of the owner, the one place a state shows its word. */
  ask?: string
  /** The outcome the close dialog offers, true to the brief. */
  closeAs?: string
  brief: MockBrief
}

/** A card in Talk, one project's piece of work. */
export interface MockCard {
  title: string
  state: 'working' | 'waiting' | 'open' | 'done'
  line: string
  options?: string[]
}

export const STATE_WORD: Readonly<Record<ProjectState, string>> = {
  progress: 'In progress',
  waiting: 'Waiting',
  completed: 'Completed',
  cancelled: 'Cancelled',
  archived: 'Archived',
}

/** What each state means: shown only in the mark's tooltip. */
export const STATE_MEANS: Readonly<Record<ProjectState, string>> = {
  progress: 'In progress: Voice it is still working on it, and its cards are open.',
  waiting: 'Waiting: nothing moves until you answer what it asked you.',
  completed: 'Completed: you marked it done; its open cards were closed.',
  cancelled: 'Cancelled: you stopped it; what it decided is kept in its brief.',
  archived: 'Archived: older than 60 days with no session; Resume brings it back.',
}

export const MOCK_PROJECTS: readonly MockProject[] = [
  {
    id: 'vp_mock_bakery',
    title: 'Bakery website',
    state: 'progress',
    lastAt: '2026-11-10T16:40:00Z',
    lastWord: 'Yesterday 16:40',
    openCards: 3,
    line: 'Write the order form and hook it to the kitchen inbox',
    closeAs: 'Home and menu pages drafted; no order form',
    brief: {
      goal: 'A five-page site for the corner bakery: menu, opening hours, pre-orders for pickup.',
      done: ['Home and menu pages drafted', 'Photo set picked from the shared folder', 'Agent built the page skeleton (run 41, passed)'],
      decided: ['Pickup only, no delivery', 'Warm palette from the shop sign', 'Orders land in the kitchen inbox, not a dashboard'],
      open: ['Order form fields', 'Who answers order emails on Sundays', 'Domain name'],
      next: ['Write the order form and hook it to the kitchen inbox', 'Draft the opening-hours page', 'Settle Sunday cover with you'],
    },
  },
  {
    id: 'vp_mock_newsletter',
    title: 'Newsletter signup flow',
    state: 'waiting',
    lastAt: '2026-11-09T10:15:00Z',
    lastWord: '9 Nov 10:15',
    openCards: 2,
    line: 'Pick one of the two signup forms',
    ask: 'Waiting on your pick',
    brief: {
      goal: 'A signup that asks for an email first and preferences after the first issue.',
      done: ['Two forms mocked: one step, two steps', 'Welcome email drafted'],
      decided: ['Double opt-in', 'No pop-up on the home page'],
      open: ['One-step or two-step form: your pick'],
      next: ['Build the form you pick', 'Wire the welcome email', 'Test with three invented addresses'],
    },
  },
  {
    id: 'vp_mock_menu',
    title: 'Menu app',
    state: 'completed',
    lastAt: '2026-11-04T14:05:00Z',
    lastWord: '4 Nov 14:05',
    openCards: 0,
    line: 'Shipped in pull request #214',
    brief: {
      goal: 'A phone menu with allergens, opened from a code on each table.',
      done: ['App built and reviewed', 'Allergen list checked', 'Merged as pull request #214'],
      decided: ['No ordering from the table', 'Allergens as icons and words'],
      open: [],
      next: ['Nothing planned'],
    },
  },
  {
    id: 'vp_mock_loyalty',
    title: 'Loyalty card idea',
    state: 'cancelled',
    lastAt: '2026-11-02T09:30:00Z',
    lastWord: '2 Nov 09:30',
    openCards: 0,
    line: 'Not worth it before spring',
    brief: {
      goal: 'A stamp card on the phone: every tenth coffee free.',
      done: ['Two card designs sketched', 'Costs estimated'],
      decided: ['Not before spring: too few regulars in winter'],
      open: [],
      next: ['Nothing planned'],
    },
  },
  {
    id: 'vp_mock_booking',
    title: 'Booking calendar for the studio',
    state: 'progress',
    lastAt: '2026-11-07T18:20:00Z',
    lastWord: '7 Nov 18:20',
    openCards: 1,
    line: 'Add the cancellation window to the booking page',
    brief: {
      goal: 'Let clients book the upstairs studio by the hour.',
      done: ['Calendar page built', 'Prices set per hour'],
      decided: ['Card payment at booking', '24-hour cancellation'],
      open: ['Refund wording'],
      next: ['Add the cancellation window to the booking page', 'Send a test booking'],
    },
  },
  {
    id: 'vp_mock_invoice',
    title: 'Invoice reminder emails',
    state: 'waiting',
    lastAt: '2026-11-05T11:00:00Z',
    lastWord: '5 Nov 11:00',
    openCards: 1,
    line: 'Approve the second reminder text',
    ask: 'Waiting on your approval',
    brief: {
      goal: 'Polite reminders 7 and 21 days after an invoice is due.',
      done: ['First reminder written and approved'],
      decided: ['Two reminders, never three'],
      open: ['Second reminder text: your approval'],
      next: ['Send both reminders to a test inbox'],
    },
  },
  {
    id: 'vp_mock_gallery',
    title: 'Photo gallery cleanup',
    state: 'completed',
    lastAt: '2026-10-28T15:45:00Z',
    lastWord: '28 Oct 15:45',
    openCards: 0,
    line: 'Shipped in pull request #198',
    brief: {
      goal: 'Smaller images and alt text across the gallery.',
      done: ['Images resized', 'Alt text written for 64 photos', 'Merged as pull request #198'],
      decided: ['Keep originals in the archive bucket'],
      open: [],
      next: ['Nothing planned'],
    },
  },
  {
    id: 'vp_mock_recipe',
    title: 'Recipe cost calculator',
    state: 'completed',
    lastAt: '2026-10-21T13:10:00Z',
    lastWord: '21 Oct 13:10',
    openCards: 0,
    line: 'Done: the sheet is in the shared drive',
    brief: {
      goal: 'What each recipe costs at current flour and butter prices.',
      done: ['Sheet built from 18 recipes'],
      decided: ['Prices updated by hand monthly'],
      open: [],
      next: ['Nothing planned'],
    },
  },
  {
    id: 'vp_mock_giftcard',
    title: 'Gift card checkout',
    state: 'cancelled',
    lastAt: '2026-10-14T17:00:00Z',
    lastWord: '14 Oct 17:00',
    openCards: 0,
    line: 'The till already sells gift cards',
    brief: {
      goal: 'Sell gift cards online.',
      done: ['Checkout options compared'],
      decided: ['Dropped: the till already does it'],
      open: [],
      next: ['Nothing planned'],
    },
  },
  {
    id: 'vp_mock_survey',
    title: 'Customer survey',
    state: 'completed',
    lastAt: '2026-10-02T12:30:00Z',
    lastWord: '2 Oct 12:30',
    openCards: 0,
    line: 'Done: 41 answers summed up',
    brief: {
      goal: 'Ask regulars what to bake on Sundays.',
      done: ['Survey sent', '41 answers summed up'],
      decided: ['Rye on Sundays from November'],
      open: [],
      next: ['Nothing planned'],
    },
  },
  {
    id: 'vp_mock_rota',
    title: 'Staff rota planner',
    state: 'archived',
    lastAt: '2026-08-19T08:50:00Z',
    lastWord: '19 Aug 08:50',
    openCards: 0,
    line: 'Stopped at the shift template',
    brief: {
      goal: 'A weekly rota that respects who can open the shop.',
      done: ['Shift template drafted'],
      decided: ['Rota published on Thursdays'],
      open: ['Holiday swaps'],
      next: ['Finish the shift template'],
    },
  },
  {
    id: 'vp_mock_delivery',
    title: 'Delivery zone map',
    state: 'archived',
    lastAt: '2026-07-30T16:00:00Z',
    lastWord: '30 Jul 16:00',
    openCards: 0,
    line: 'Stopped at the zone borders',
    brief: {
      goal: 'Show which streets the van reaches.',
      done: ['Map drawn'],
      decided: ['Three zones, priced by distance'],
      open: ['Whether delivery happens at all'],
      next: ['Settle delivery first'],
    },
  },
]

/** Each project's cards in Talk: its open ones are what Projects counts. */
export const MOCK_CARDS: Readonly<Record<string, MockCard[]>> = {
  vp_mock_bakery: [
    { title: 'Order form', state: 'working', line: 'Agent writing the form and the inbox hook · 6 min in' },
    { title: 'Sunday cover', state: 'waiting', line: 'Who answers order emails on Sundays?', options: ['Me', 'Sam', 'Nobody: auto-reply'] },
    { title: 'Domain name', state: 'open', line: 'Three names checked, two free' },
    { title: 'Home and menu pages', state: 'done', line: 'Drafted, run 41 passed' },
    { title: 'Photo set', state: 'done', line: '12 photos picked' },
  ],
  vp_mock_newsletter: [
    { title: 'Pick a signup form', state: 'waiting', line: 'One step asks for the email only; two steps adds topics.', options: ['One step', 'Two steps'] },
    { title: 'Welcome email', state: 'open', line: 'Drafted, waits on the form' },
    { title: 'Two forms mocked', state: 'done', line: 'Both on the staging page' },
  ],
  vp_mock_prices: [{ title: 'Plan: studio price list', state: 'working', line: 'Drafting the plan · asks the room rates next' }],
}

/** Which direction the mock-up draws, from the address: `?voiceprojects=tabs`. */
export function mockDirection(): string | null {
  if (typeof window === 'undefined') return null
  return new URLSearchParams(window.location.search).get('voiceprojects')
}

/** A mock-up parameter from the address. */
export function mockParam(name: string): string | null {
  if (typeof window === 'undefined') return null
  return new URLSearchParams(window.location.search).get(name)
}
