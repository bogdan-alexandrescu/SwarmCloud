import './styles/help.css'
import { useEffect, useRef, useState, type Ref } from 'react'
import { HELP, HELP_GROUPS, HELP_PLACES, HELP_ROUTE, TOPIC_IDS, topicFor, type HelpGroupId, type TopicId } from './help'
import { addressToPath } from './paths'
import { Absent } from './primitives'
import { CountNote, PageHead } from './Shell'

/**
 * THE HELP SECTION (docs/web-ui/ui-audit-and-build-prompt.md §B7.3).
 *
 * NAMED `HelpSection.tsx`, NOT `Help.tsx`, AND THAT IS NOT A PREFERENCE. The
 * data module beside it is `help.ts`, and macOS ships a case-insensitive
 * filesystem: `./help` and `./Help` resolve to the same file there and to two
 * different files on the Linux image this is built on. `tsc` rejects the pair
 * outright (TS1149), which is the good outcome -- the bad one is a build that
 * works on a laptop and resolves the wrong module in CI.
 *
 * The place every sentence that used to sit under a panel now lives. It is
 * reached from the `?` in the head and from the footer link on every card --
 * and it is deliberately NOT a rail item, for the same reason the API
 * reference is not one: it is a thing you look up, not a thing you work in,
 * and a rail entry would put it beside the five questions people actually
 * arrive with.
 *
 * IT RENDERS `help.ts` AND NOTHING ELSE. Not a second copy of the text, not a
 * curated subset -- the same record the cards read, walked in order. That is
 * the property the brief asked for: a topic cannot exist in a card and be
 * missing from this page, because there is only one list. A `#help/<id>` link
 * from a card is therefore never dead, and `tests/help.test.ts` asserts it for
 * every card in the tree.
 *
 * Every figure and every enum member on this page is READ from the module that
 * owns it, at render time. Nothing here is a typed-out copy of a frozen value.
 * See the header of `help.ts` for why that rule is absolute in TypeScript
 * specifically.
 */
export function HelpScreen({ topic }: { topic: string }) {
  // H1 (admin-help.html, the owner's pick 2026-10-01): ONE PAGE PER GROUP, at
  // `/help/<group>`, with a topic at `/help/<group>#<topic>`. `topic` is the
  // route's tail: a group id, a topic id (whose group is then the page), or
  // empty for `/help`, which opens the first group -- so there is always one
  // group on screen and the panel always has one to mark. The panel is the
  // table of contents (Spine.tsx); this page draws no index of its own.
  const groupId = HELP_GROUPS.some((g) => g.id === topic) ? topic : null
  const wanted = groupId === null ? topicFor(topic) : null
  const page = helpPageOf(topic)
  const group = HELP_GROUPS.find((g) => g.id === page)!
  const target = useRef<HTMLDivElement | null>(null)
  // SEARCH ACROSS ALL TOPICS, whatever group is open: names, claims, cards and
  // the long form, case-insensitively. Nothing is fetched.
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const hits = q === '' ? [] : TOPIC_IDS.filter((id) => matchOf(id, q) !== null)

  // Deep links are the point of the anchors, so one has to actually land --
  // BUT ONLY WHEN IT IS NOT ALREADY IN VIEW (AH-16). The viewport is
  // `.ctl-scroll` clipped to the window, because the dock under the scroller
  // can be dragged taller (App.tsx, styles.css `.ctl-frame`), and the band
  // starts under the topic's own `scroll-margin-top`, read from the element
  // rather than restated. jsdom implements no `scrollIntoView`.
  useEffect(() => {
    const el = target.current
    if (el === null || typeof el.scrollIntoView !== 'function') return
    const box = el.getBoundingClientRect()
    const port = el.closest('.ctl-scroll')?.getBoundingClientRect() ?? null
    const clear = Number.parseFloat(window.getComputedStyle(el).scrollMarginTop ?? '')
    const top = Math.max(0, port?.top ?? 0) + (Number.isFinite(clear) && clear > 0 ? clear : 0)
    const bottom = Math.min(window.innerHeight, port?.bottom ?? window.innerHeight)
    const inView = box.top >= top && box.bottom <= bottom
    if (!inView) el.scrollIntoView({ block: 'start' })
  }, [topic])

  const ids = TOPIC_IDS.filter((id) => HELP[id].group === page)
  const shownGroups = HELP_GROUPS.filter((g) => TOPIC_IDS.some((id) => HELP[id].group === g.id))
  // THE HEAD LINE (AH-25): what the page holds and which topic is showing,
  // facts joined by `·`, every one READ from the registry. Help reads nothing,
  // so it has no provenance to print.
  const line =
    q !== ''
      ? `${hits.length} of ${TOPIC_IDS.length} topics match “${query.trim()}”`
      : `${ids.length} on this page · ${TOPIC_IDS.length} topics in ${shownGroups.length} groups` +
        (wanted === null ? '' : ` · showing ${wanted.title}`)

  return (
    <>
      {/* THE ONE PAGE HEAD (AH-25), `PageHead`, titled with the group the page
          is (H1) -- or Help while a search spans every group. Its line is
          the note over the topics, not a line under the title (#138). */}
      <PageHead title={q === '' ? group.title : 'Help'} />
      <CountNote>{line}</CountNote>

      {/* THE WHOLE COLUMN'S WIDTH (H1), because it searches every group. */}
      <label className="help-search">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`Search all ${TOPIC_IDS.length} topics`}
          aria-label="Search all topics"
        />
      </label>

      {/* ON A PHONE THE PANEL IS BEHIND THE MENU, so the group is a select
          under the search (H1 phone frame). The sheet hides it where the
          panel is on screen. */}
      <div className="help-group-pick">
        <select
          aria-label="Help group"
          value={page}
          onChange={(e) => {
            // The router follows a `#help/<group>` hash (App.tsx `onHash`).
            window.location.hash = `${HELP_ROUTE}/${e.target.value}`
          }}
        >
          {shownGroups.map((g) => (
            <option key={g.id} value={g.id}>
              {g.title}
            </option>
          ))}
        </select>
      </div>

      {/* AN UNKNOWN TOPIC IS A REAL ANSWER, SO IT IS THE DEFAULT EMPTY STATE
          (AH-17): the `real zero` mark, the heading, one sentence, a way back
          to the top of Help, and the one large break before the page. */}
      {topic !== '' && groupId === null && wanted === null && (
        <div style={{ marginBottom: 'var(--ctl-s5)' }}>
          <Absent
            kind="zero"
            heading={`No help topic is called ${topic}`}
            say={`This build carries no help topic called ${topic}.`}
            link={{ href: `#${HELP_ROUTE}`, label: 'All topics' }}
          >
            The first group of what this build does carry is below.
          </Absent>
        </div>
      )}

      {q !== '' ? (
        hits.length === 0 ? (
          <p className="ctl-em">No topic mentions {query.trim()}.</p>
        ) : (
          <ul className="help-hits" aria-label="Search results">
            {hits.map((id) => (
              <Hit key={id} id={id} q={q} />
            ))}
          </ul>
        )
      ) : (
        // One group, one page; its title is the page head's.
        <section className="help-group" aria-label={group.title}>
          {ids.map((id) => (
            <Topic key={id} id={id} highlighted={id === topic} innerRef={id === topic ? target : null} />
          ))}
        </section>
      )}
    </>
  )
}

/**
 * The group page a Help route tail opens: the group itself, a topic's group,
 * or -- for `/help` and for a topic this build lacks -- the first group, so a
 * page is always one group and the panel always marks it. App.tsx reads this
 * to light the panel's entry.
 */
export function helpPageOf(tail: string): HelpGroupId {
  const named = HELP_GROUPS.find((g) => g.id === tail)
  if (named !== undefined) return named.id
  const t = topicFor(tail)
  if (t !== null) return t.group
  return HELP_GROUPS.find((g) => TOPIC_IDS.some((id) => HELP[id].group === g.id))!.id
}

/** Where `q` first appears in a topic -- its name, claim, card or long form -- or null. */
function matchOf(id: TopicId, q: string): { text: string; at: number } | null {
  const t = HELP[id]
  for (const text of [t.subject, t.title, t.short, ...t.long]) {
    const at = text.toLowerCase().indexOf(q)
    if (at !== -1) return { text, at }
  }
  return null
}

/**
 * One search result (H1 dark frame): the topic's name, its group, and the
 * words around the match with the match marked. A link to the topic, which
 * routes to its group page and lands on it.
 */
function Hit({ id, q }: { id: TopicId; q: string }) {
  const t = HELP[id]
  const m = matchOf(id, q)!
  const from = Math.max(0, m.at - 48)
  const to = Math.min(m.text.length, m.at + q.length + 72)
  return (
    <li>
      <a className="help-hit" href={`#${t.anchor}`}>
        <b>{t.subject}</b>
        <small>{HELP_GROUPS.find((g) => g.id === t.group)?.title}</small>
        <span>
          {from > 0 && '…'}
          {m.text.slice(from, m.at)}
          <mark>{m.text.slice(m.at, m.at + q.length)}</mark>
          {m.text.slice(m.at + q.length, to)}
          {to < m.text.length && '…'}
        </span>
      </a>
    </li>
  )
}

/**
 * One topic, at its anchor, as H1's card: its name and copyable anchor, then
 * three rows -- You see (the claim, `title`, which the `?` card beside a figure
 * opens with), It means (the long form, the values read from their owner, and
 * the topics it sends a reader on to) and What to do (`act`, with the screen
 * that answers it).
 *
 * `id` on the wrapper IS the anchor, so `#help/absent-vs-zero` both routes
 * here and scrolls to the right card. `innerRef`, not `ref`: React 18 reserves
 * `ref` on a function component and would drop it silently.
 *
 * THE DEEP-LINKED TOPIC IS `.is-current`, §1.3's selection treatment: a
 * surface step and an ink border, on a border every card declares, so
 * marking one changes a colour and moves nothing.
 */
function Topic({
  id,
  highlighted,
  innerRef,
}: {
  id: TopicId
  highlighted: boolean
  innerRef: Ref<HTMLDivElement> | null
}) {
  const t = HELP[id]
  const values = t.values?.() ?? []
  const see = t.see ?? []

  return (
    <div ref={innerRef} id={t.anchor} className={highlighted ? 'help-topic is-current' : 'help-topic'}>
      <div className="help-topic-head">
        <h3>{t.subject}</h3>
        <TopicAnchor id={id} />
      </div>
      <dl className="help-rows">
        <div>
          <dt>You see</dt>
          <dd>{t.title}</dd>
        </div>
        <div>
          <dt>It means</dt>
          <dd>
            {t.long.map((para, i) => (
              <p key={i}>{para}</p>
            ))}
            {values.length > 0 && (
              <dl className="help-values">
                {values.map((v) => (
                  // The terms are the API's enums as their owner spells them --
                  // `LEASED` -- and `.help-values dt` lowercases them by style
                  // (CH-3), so the string is still the owner's spelling.
                  <div key={v.term}>
                    <dt>{v.term}</dt>
                    <dd>{v.note}</dd>
                  </div>
                ))}
              </dl>
            )}
            {/* THE TOPICS THIS ONE SENDS A READER ON TO, as links (AH-21). */}
            {see.length > 0 && (
              <p className="help-topic-see">
                See also{' '}
                {see.map((other, i) => (
                  <span key={other}>
                    {i > 0 && ' · '}
                    <a className="ctl-link" href={`#${HELP[other].anchor}`}>
                      {HELP[other].title}
                    </a>
                  </span>
                ))}
              </p>
            )}
          </dd>
        </div>
        {/* WHAT TO DO, OR WHERE TO LOOK (#131): the last row, with the screen
            that answers it as a link. */}
        <div className="help-topic-act">
          <dt>What to do</dt>
          <dd>
            {t.act.say}
            {t.act.at !== undefined && (
              <>
                {' '}
                <a className="ctl-link" href={`#${t.act.at}`}>
                  Open {HELP_PLACES[t.act.at]}
                </a>
              </>
            )}
          </dd>
        </div>
      </dl>
    </div>
  )
}

/**
 * THE ANCHOR, AS A LINK AND A COPY CONTROL (#130). It was faint text nobody
 * could click. The link is the topic's own `#help/<id>`; the copy control
 * writes the address the router settles on (`/help/<group>#<id>`), which is
 * what a reader pasting it to somebody else needs. A refused clipboard says
 * so and leaves the link on screen to be copied by hand
 * (`clipboard-secure-context`).
 */
/** How long `copied` stays, as on the agent header's Copy link (AgentSplit.tsx). */
const COPY_SAID_MS = 4000

function TopicAnchor({ id }: { id: TopicId }) {
  const t = HELP[id]
  const [said, setSaid] = useState('')
  // THE SAME CONFIRMATION AS THE AGENT HEADER'S COPY LINK (U10a, owner QA
  // 2026-10-04): `copied` beside the control, gone after the same 4s, and
  // the button itself says it while it shows -- the reader is looking at
  // the button they clicked, not at the line beside it.
  useEffect(() => {
    if (said === '') return
    const id = setTimeout(() => setSaid(''), COPY_SAID_MS)
    return () => clearTimeout(id)
  }, [said])
  const copy = () => {
    const url = `${window.location.origin}${addressToPath(t.anchor)}`
    const c = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (c === undefined) return setSaid('could not copy; select the link instead')
    c.writeText(url).then(
      () => setSaid('copied'),
      () => setSaid('could not copy; select the link instead'),
    )
  }
  // In the card's head beside its name, as H1 draws it: `#<id> · copy link`.
  return (
    <span className="help-topic-anchor">
      <a href={`#${t.anchor}`}>#{id}</a>
      <button type="button" onClick={copy} aria-label={`Copy a link to ${t.subject}`}>
        {said === 'copied' ? 'copied ✓' : 'copy link'}
      </button>
      <span role="status">{said}</span>
    </span>
  )
}
