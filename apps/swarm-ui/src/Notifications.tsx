/**
 * THE BELL (#1206, proposal A -- the owner's pick, 2026-10-12 17:40 PDT).
 *
 * At the top-right of the header bar on every page. Its count is the unread
 * items that NEED YOU and nothing else: an update -- a task finished, a PR
 * merged -- is worth seeing and is never a reason to look now, and a count
 * that mixes the two trains people to ignore it. A measured zero draws no
 * badge, the way the Automate badge draws none.
 *
 * Clicked, it opens a ~392px drop-down anchored under it: "Needs you" above
 * "Updates", needs-you OLDEST first (the longest wait is the first to answer)
 * and updates NEWEST first. The server sorts the same way; the console sorts
 * again rather than trusting the wire for an order the reader acts on. A click
 * on an item goes to its page, marks it read and closes. Escape and a click
 * outside close it and give focus back to the bell. On a phone (below 760px,
 * where the spine becomes the phone header) it is a full-screen page instead.
 *
 * A ROUTE THAT ANSWERS 404 HIDES THE BELL. The console ships ahead of, or
 * against, an API without `/v1/notifications`; a bell there could only ever
 * say "not read", in every page's header, about a feature that is not served.
 * Any other failure keeps the bell and says, inside it, that the feed was not
 * read -- "nothing needs you" and "could not ask" are opposite facts.
 *
 * The feed itself is `notificationFeed.ts`: one poll for the bell and the
 * Admin pill together.
 */
import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore, type MouseEvent as ReactMouseEvent, type RefObject } from 'react'
import { loadQuestions, type NotificationFeed, type NotificationItem, type TaskQuestions } from './api'
import { routedClick } from './components'
import { errorHeading } from './fetch'
import { feedData, markAllRead, markRead, notificationsSnapshot, subscribeNotifications } from './notificationFeed'
import { pathToAddress } from './paths'
import { Icon, PANEL_PAGES, PANEL_TITLE } from './Spine'
import { timeAgo } from './types'
import './styles/notifications.css'

/** Where the muted line sends the reader: Settings › Profile (Spine.tsx `PROFILE_PATH`). */
const PROFILE_HREF = '/settings/profile'

/**
 * THE MUTED ITEMS THAT NEED YOU (user-profile.md §4.6, Q3). Every topic may be
 * muted, and a muted item leaves the list and the badge; the API counts it in
 * `muted.needs_you`. So a muted approval is QUIET, never invisible: one line
 * under the list says how many, and where to change it. Muted updates are not
 * announced. Read off the feed defensively: an API before PROFILE-BELL sends
 * no `muted`, which is no muted item.
 */
export function mutedNeedsYou(feed: NotificationFeed | null): number {
  const muted = (feed as { muted?: unknown } | null)?.muted
  if (typeof muted !== 'object' || muted === null) return 0
  const n = (muted as { needs_you?: unknown }).needs_you
  return typeof n === 'number' && Number.isInteger(n) && n > 0 ? n : 0
}

/** The phone band: the spine is the 44px phone header below 760px (Spine.tsx). */
export const NOTIFICATIONS_PHONE_QUERY = '(max-width: 759px)'

function subscribePhone(fn: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const q = window.matchMedia(NOTIFICATIONS_PHONE_QUERY)
  q.addEventListener?.('change', fn)
  return () => q.removeEventListener?.('change', fn)
}

function phoneNow(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(NOTIFICATIONS_PHONE_QUERY).matches
}

/** The feed, as the store holds it now. */
export function useNotificationFeed() {
  return useSyncExternalStore(subscribeNotifications, notificationsSnapshot, notificationsSnapshot)
}

/** Each group's icon: the spine's own icon for the place the item leads. */
const GROUP_ICON: Readonly<Record<NotificationItem['group'], string>> = {
  admin: 'people',
  approval: 'approvals',
  task: 'work',
  account: 'access',
  finished: 'check',
}

const GROUP_LABEL: Readonly<Record<NotificationItem['group'], string>> = {
  admin: 'Waiting for you',
  approval: 'Approval',
  task: 'In an agent',
  account: 'Account and access',
  finished: 'Finished',
}

const when = (i: NotificationItem) => {
  const t = new Date(i.at).getTime()
  return Number.isFinite(t) ? t : 0
}

/** Needs-you items oldest first, then updates newest first. */
export function orderFeed(items: readonly NotificationItem[]): { needs: NotificationItem[]; updates: NotificationItem[] } {
  return {
    needs: items.filter((i) => i.needs_you).sort((a, b) => when(a) - when(b)),
    updates: items.filter((i) => !i.needs_you).sort((a, b) => when(b) - when(a)),
  }
}

/**
 * Where an item leads, in the panel's own words: `/admin/people` is
 * Admin › People, an agent path is Work › Agents › <id>. Read from the same
 * page list the panel draws, so a renamed page is renamed here too. A path
 * this console does not serve is shown as the path.
 */
export function destinationOf(href: string): string[] {
  const p = pathToAddress(href.split(/[?#]/)[0] ?? '')
  if (p === null) return [href]
  const [head = '', ...rest] = (p.address.split('?')[0] ?? '').split('/')
  if (head === 'overview') return ['Overview']
  if (head === 'help') return ['Help']
  if (head === 'work' && rest[0] === 'task') return ['Work', 'Agents', decodeURIComponent(rest[1] ?? '')]
  if (!(head in PANEL_TITLE)) return [href]
  const section = head as keyof typeof PANEL_TITLE
  const at = `${head}/${rest[0] ?? ''}`
  const page = PANEL_PAGES[section].find((pg) => pg.to === at || (pg.kids ?? []).some((k) => k.to === at))
  // A page drawn under another names itself (Capacity › Provider quota, not
  // Capacity › Accounts): the not-found page offers it by this name (QA1 admin-shell-11).
  const kid = (page?.kids ?? []).find((k) => k.to === at)
  return page === undefined ? [PANEL_TITLE[section]] : [PANEL_TITLE[section], (kid ?? page).label]
}

/**
 * WHERE THE COUNT IS ANSWERED (QA1 C-27, live walk-through 2026-10-28): the
 * bell said "14 need you" while Overview said "Needs a look 3" and Approvals
 * "Nothing waiting", and nothing on screen said what the 14 were. The unread
 * items that need you, by the page that answers each, in the panel's words
 * and without an agent's id: "12 in Work › Agents · 2 in Admin › People and
 * roles". The largest first, a tie in the order the list draws them.
 */
export function needsSplit(items: readonly NotificationItem[]): string {
  const counts: { where: string; n: number }[] = []
  for (const item of orderFeed(items).needs) {
    if (item.read) continue
    const dest = destinationOf(item.href)
    const where = (dest[0] === 'Work' && dest[1] === 'Agents' ? dest.slice(0, 2) : dest).join(' › ')
    const had = counts.find((c) => c.where === where)
    if (had === undefined) counts.push({ where, n: 1 })
    else had.n += 1
  }
  return counts
    .map((c, i) => ({ ...c, i }))
    .sort((a, b) => b.n - a.n || a.i - b.i)
    .map((c) => `${c.n} in ${c.where}`)
    .join(' · ')
}

/** ONE NEEDS, TWO NEED (QA2 admin2-17): the bell's name, its live line and the
 * panel's chip all agree the verb with the count. */
function needVerb(count: number): string {
  return count === 1 ? 'needs' : 'need'
}

export function NotificationBell({ go }: { go: (to: string) => void }) {
  const feed = useNotificationFeed()
  const phone = useSyncExternalStore(subscribePhone, phoneNow, () => false)
  const [open, setOpen] = useState(false)
  const bellRef = useRef<HTMLButtonElement | null>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const panelId = useId()

  const close = useCallback((refocus: boolean) => {
    setOpen(false)
    if (refocus) bellRef.current?.focus()
  }, [])

  // ESCAPE AND A CLICK ELSEWHERE CLOSE IT. Listened for on the document only
  // while it is open, so a closed bell costs nothing on every keystroke.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      close(true)
    }
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node | null
      if (t !== null && rootRef.current?.contains(t)) return
      close(false)
    }
    // FOCUS ARRIVING OUTSIDE CLOSES IT TOO (QA2 admin2-09): Tab walked out of
    // the open dialog onto Help, Profile and the spine's collapse control
    // with the panel still drawn over them. Where focus LANDS is read, as the
    // Act-as list reads it; focus stays where Tab put it.
    const onIn = (e: FocusEvent) => {
      const t = e.target
      if (t instanceof Node && rootRef.current?.contains(t)) return
      close(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('focusin', onIn)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('focusin', onIn)
    }
  }, [open, close])

  // Focus moves into the panel when it opens, so the next Tab is its first control.
  useEffect(() => {
    if (open) panelRef.current?.focus()
  }, [open])

  if (feed.status === 'loading') return null
  if (feed.status === 'error' && feed.error.kind === 'not_found') return null

  const data = feedData(feed)
  const count = data?.unread_needs_you ?? 0
  const label = count > 0 ? `Notifications, ${count} ${needVerb(count)} you` : 'Notifications'
  const why = feed.status === 'error' || feed.status === 'stale' ? `Notifications not read: ${errorHeading(feed.error)}` : null

  return (
    <div className="nt" ref={rootRef}>
      <button
        type="button"
        ref={bellRef}
        className={`nt-bell${open ? ' is-open' : ''}`}
        aria-label={label}
        title={why ?? label}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="bell" className="nt-bell-ic" />
        {count > 0 && (
          <span className="nt-count" aria-hidden>
            {count}
          </span>
        )}
      </button>
      <span className="sk-vh" aria-live="polite">
        {/* ONE NEEDS, TWO NEED (QA2 admin2-17): it read "1 notification need you". */}
        {count > 0 ? `${count} notification${count === 1 ? '' : 's'} ${needVerb(count)} you` : ''}
      </span>
      {open && (
        <NotificationPanel
          id={panelId}
          panelRef={panelRef}
          phone={phone}
          data={data}
          why={why}
          onClose={() => close(true)}
          onProfile={() => {
            close(false)
            go(PROFILE_HREF)
          }}
          onOpen={(item) => {
            close(false)
            go(item.href)
            if (!item.read) void markRead([item.id])
          }}
        />
      )}
    </div>
  )
}

function NotificationPanel({
  id,
  panelRef,
  phone,
  data,
  why,
  onClose,
  onProfile,
  onOpen,
}: {
  id: string
  panelRef: RefObject<HTMLDivElement>
  phone: boolean
  data: NotificationFeed | null
  why: string | null
  onClose: () => void
  onProfile: () => void
  onOpen: (item: NotificationItem) => void
}) {
  const count = data?.unread_needs_you ?? 0
  const muted = mutedNeedsYou(data)
  const unread = data === null ? false : data.items.some((i) => !i.read)
  const { needs, updates } = orderFeed(data?.items ?? [])
  const split = count > 0 ? needsSplit(needs) : ''
  const now = Date.now()

  return (
    <div
      id={id}
      ref={panelRef}
      className={`nt-panel${phone ? ' is-page' : ''}`}
      role="dialog"
      aria-modal={phone ? true : undefined}
      aria-label="Notifications"
      tabIndex={-1}
    >
      <div className="nt-head">
        {phone && (
          <button type="button" className="nt-close" aria-label="Close notifications" onClick={onClose}>
            &#8592;
          </button>
        )}
        <h2 className="nt-h">Notifications</h2>
        {count > 0 && <span className="nt-chip">{count} {needVerb(count)} you</span>}
        <button type="button" className="nt-all" disabled={!unread} onClick={() => void markAllRead()}>
          Mark all read
        </button>
      </div>
      {why !== null && <p className="nt-why">{why}</p>}
      {split !== '' && <p className="nt-why nt-split">{split}</p>}
      {data !== null && needs.length === 0 && updates.length === 0 ? (
        <div className="nt-empty">
          <Icon name="check" className="nt-empty-ic" />
          <p className="nt-empty-h">Nothing needs you</p>
          <p className="nt-empty-p">Approvals, sign-ins and finished work will show up here.</p>
        </div>
      ) : (
        <div className="nt-body">
          {needs.length > 0 && <Group title="Needs you" items={needs} now={now} onOpen={onOpen} />}
          {updates.length > 0 && <Group title="Updates" items={updates} now={now} onOpen={onOpen} />}
        </div>
      )}
      {muted > 0 && (
        <p className="nt-why nt-muted">
          {muted} muted item{muted === 1 ? ' needs' : 's need'} you ·{' '}
          <a
            href={PROFILE_HREF}
            onClick={(e: ReactMouseEvent<HTMLAnchorElement>) => {
              if (!routedClick(e)) return
              e.preventDefault()
              onProfile()
            }}
          >
            change in Settings › Profile
          </a>
        </p>
      )}
      <p className="nt-foot">Read items clear after 30 days</p>
    </div>
  )
}

function Group({ title, items, now, onOpen }: { title: string; items: NotificationItem[]; now: number; onOpen: (item: NotificationItem) => void }) {
  return (
    <section className="nt-group">
      <h3>{title}</h3>
      <ul>
        {items.map((item) => (
          <Item key={item.id} item={item} now={now} onOpen={onOpen} />
        ))}
      </ul>
    </section>
  )
}

/** The feed's id of an agent-questions item, `task:questions:<task id>`. */
const QUESTIONS_ITEM = 'task:questions:'

/** How much of a question the bell shows: one line of the ~392px panel. */
export const QUESTION_CHARS = 90

/**
 * WHAT THE AGENT ASKED, IN THE BELL (QA14-UI, owner QA 2026-10-13): "Your
 * agent asked 1 question" with a task id said nothing to answer from. The
 * first question still open, or the first when every one has an answer,
 * cut at `QUESTION_CHARS` on a word where it can be. Null when there is no
 * question text to show -- the item then says what it said before.
 */
export function firstQuestion(read: TaskQuestions): string | null {
  const asked = read.questions.filter((q) => (q.question ?? '').trim() !== '')
  const q = asked.find((x) => x.answer === null || x.answer === undefined) ?? asked[0]
  if (q === undefined) return null
  const text = q.question!.replace(/\s+/g, ' ').trim()
  if (text.length <= QUESTION_CHARS) return text
  const cut = text.slice(0, QUESTION_CHARS - 1)
  const space = cut.lastIndexOf(' ')
  return `${(space > QUESTION_CHARS / 2 ? cut.slice(0, space) : cut).trimEnd()}…`
}

/** The question an agent-questions item names, read when the bell draws it; null for any other item. */
function useItemQuestion(item: NotificationItem): string | null {
  const taskId = item.id.startsWith(QUESTIONS_ITEM) ? item.id.slice(QUESTIONS_ITEM.length) : null
  const [text, setText] = useState<string | null>(null)
  useEffect(() => {
    if (taskId === null || taskId === '') return
    let live = true
    void loadQuestions(taskId).then((r) => {
      if (live && r.status === 'ok') setText(firstQuestion(r.data))
    })
    return () => {
      live = false
    }
  }, [taskId])
  return text
}

/**
 * ONE NOTIFICATION IS TWO LINES (VQ-057, visual QA 2026-10-26): its title and
 * one line under it. It printed the whole detail -- five lines of an agent's
 * outcome -- and a breadcrumb with the raw task id ("Work › Agents ›
 * task_9f3b… ›"), so a 390px screen held two notifications. The line is the
 * agent's question where there is one, else the detail with its line breaks
 * folded; the sheet cuts it at the edge, and the whole text is its tooltip
 * and is on the page the item opens. Where the item leads is the link's
 * tooltip, in the panel's words, without the id.
 */
export function excerptOf(item: NotificationItem, question: string | null): string | null {
  if (question !== null) return `\u201c${question}\u201d`
  const detail = (item.detail ?? '').replace(/\s+/g, ' ').trim()
  return detail === '' ? null : detail
}

/** Where an item leads, for its tooltip: the panel's words, an agent's id left out. */
export function leadsTo(href: string): string {
  const dest = destinationOf(href)
  return `Opens ${(dest[0] === 'Work' && dest[1] === 'Agents' ? dest.slice(0, 2) : dest).join(' › ')}`
}

function Item({ item, now, onOpen }: { item: NotificationItem; now: number; onOpen: (item: NotificationItem) => void }) {
  const question = useItemQuestion(item)
  const excerpt = excerptOf(item, question)
  const icon = GROUP_ICON[item.group] ?? 'work'
  return (
    <li className={`nt-item is-${item.group}${item.read ? '' : ' is-unread'}`}>
      <span className="nt-dot" aria-hidden />
      <span className="nt-ic" title={GROUP_LABEL[item.group]} aria-hidden>
        <Icon name={icon} className="nt-ic-svg" />
      </span>
      <a
        className="nt-main"
        href={item.href}
        title={leadsTo(item.href)}
        onClick={(e: ReactMouseEvent<HTMLAnchorElement>) => {
          if (!routedClick(e)) return
          e.preventDefault()
          onOpen(item)
        }}
      >
        {item.read ? null : <span className="sk-vh">Unread: </span>}
        <span className="nt-title">{item.title}</span>
        {excerpt !== null && (
          <span className={question !== null ? 'nt-excerpt nt-question' : 'nt-excerpt'} title={excerpt}>
            {excerpt}
          </span>
        )}
      </a>
      <span className="nt-side">
        <time dateTime={item.at} title={item.at}>
          {timeAgo(item.at, now)}
        </time>
        {!item.read && (
          // The console's small button (components.html A), not a text link
          // in the time's monospace.
          <button type="button" className="c-btn is-sm is-ghost nt-mark" aria-label={`Mark read: ${item.title}`} onClick={() => void markRead([item.id])}>
            Mark read
          </button>
        )}
      </span>
    </li>
  )
}
