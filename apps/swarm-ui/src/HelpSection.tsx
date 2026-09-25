import { useEffect, useRef, type Ref } from 'react'
import { HELP, HELP_GROUPS, HELP_ROUTE, TOPIC_IDS, topicFor, type TopicId } from './help'
import { Absent } from './primitives'

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
  const wanted = topicFor(topic)
  const target = useRef<HTMLDivElement | null>(null)

  // Deep links are the point of the anchors, so one has to actually land.
  //
  // BUT ONLY WHEN IT IS NOT ALREADY IN VIEW (AH-16). A topic already on screen
  // was jumped to the top edge anyway, taking the group heading above it out
  // of sight for no gain -- the reader was already looking at it. The rect is
  // read once, before any scroll: fully inside the viewport means stay put.
  // jsdom implements no `scrollIntoView`, hence the guard.
  //
  // THE VIEWPORT IS `.ctl-scroll`, NOT THE WINDOW. The frame is two grid rows,
  // the scroller and the dock under it, and the dock's height can be dragged
  // (App.tsx, styles.css `.ctl-frame`). A topic whose top sits in the dock's
  // band is below the scroller's bottom edge and out of sight, yet still above
  // `window.innerHeight` -- measured against the window it counted as in view
  // and the deep link left it under the dock. So the visible band is the
  // scroller's box clipped to the window; outside the frame (a test, a future
  // page with no scroller) it is the window alone.
  useEffect(() => {
    const el = target.current
    if (el === null || typeof el.scrollIntoView !== 'function') return
    const box = el.getBoundingClientRect()
    const port = el.closest('.ctl-scroll')?.getBoundingClientRect() ?? null
    const top = Math.max(0, port?.top ?? 0)
    const bottom = Math.min(window.innerHeight, port?.bottom ?? window.innerHeight)
    const inView = box.top >= top && box.bottom <= bottom
    if (!inView) el.scrollIntoView({ block: 'start' })
  }, [topic])

  return (
    <>
      {/* NO SUBTITLE (AH-15). §6.12: a page head is a title and its actions,
          and nothing else. This one said "Why a figure on these screens looks
          the way it does" -- true of about one topic in eight -- and then
          restated the deep-linked topic's title, which the highlighted topic
          below already says in its own heading. It is `.ctl-page-head`, the
          §6.12 primitive the API reads page already uses, because `.head` left
          the spacing under the title to the subtitle that is gone. */}
      <div className="ctl-page-head">
        <h1>Help</h1>
      </div>

      {/* AN UNKNOWN TOPIC IS A REAL ANSWER, SO IT IS THE DEFAULT EMPTY STATE
          (AH-17). It was a warn-coloured `.is-partial` panel with two
          sentences, no mark, no link and no gap to the first group under it.
          Nothing about a stale link is PARTIAL -- this build carries no such
          topic, which is a complete and correct answer -- so it is §6.9's
          fixed shape: the `real zero` mark, the heading, one sentence, a way
          back to the top of Help, and the one large break before the page. */}
      {topic !== '' && wanted === null && (
        <div style={{ marginBottom: 'var(--ctl-s5)' }}>
          <Absent
            kind="zero"
            heading={`No help topic is called ${topic}`}
            say={`This build carries no help topic called ${topic}.`}
            link={{ href: `#${HELP_ROUTE}`, label: 'All topics' }}
          >
            Everything this build does carry is below.
          </Absent>
        </div>
      )}

      {HELP_GROUPS.map((g) => {
        const ids = TOPIC_IDS.filter((id) => HELP[id].group === g.id)
        if (ids.length === 0) return null
        return (
          <section className="section" key={g.id}>
            <h2>{g.title}</h2>
            {ids.map((id) => (
              <Topic
                key={id}
                id={id}
                highlighted={id === topic}
                innerRef={id === topic ? target : null}
              />
            ))}
          </section>
        )
      })}
    </>
  )
}

/**
 * One topic, at its anchor.
 *
 * `id` on the wrapper IS the anchor, so `#help/absent-vs-zero` both routes
 * here and scrolls to the right block. The scroll is driven by a ref rather
 * than by the browser's own fragment handling: the hash IS the router here, so
 * the fragment is already spent on the route and nothing will scroll for us.
 *
 * `innerRef`, not `ref` -- React 18 reserves `ref` on a function component and
 * would drop it silently, leaving a deep link that routes correctly and lands
 * at the top of the page.
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

  return (
    <div
      ref={innerRef}
      id={t.anchor}
      style={{
        // styles.css belongs to another track this pass, so the box is inline
        // from existing tokens -- the call AgentDetail.tsx already made. An
        // inline style adds no selector and cannot reach another screen.
        border: '1px solid var(--line)',
        // THE DEEP-LINK TARGET IS §1.3's SELECTED TREATMENT (AH-16): a surface
        // step and a 2px rule in ink. It was a 3px rule in --text-dim and no
        // surface change -- the weakest ink on the page marking the one block
        // the reader was sent to.
        borderLeft: highlighted ? '2px solid var(--text)' : '1px solid var(--line)',
        borderRadius: 'var(--radius)',
        background: highlighted ? 'var(--surface-2)' : 'var(--surface)',
        padding: 'var(--ctl-pad-chrome)',
        marginBottom: 'var(--ctl-s3)',
        // THREE OF THE LARGE BREAK, not one. At --ctl-s5 alone a target that
        // opens its group landed with the group's own heading clipped off the
        // top; this leaves room for that heading and its margin above the
        // topic, so the reader sees which group they landed in.
        scrollMarginTop: 'calc(var(--ctl-s5) * 3)',
      }}
    >
      <h3
        style={{
          margin: '0 0 var(--ctl-s2)',
          // A TOPIC IS A CARD, AND A CARD'S TITLE IS --t-lead (AH-10). This was
          // --t-title, "the panel title of that section", which put the page's
          // h1, each group's h2 and every topic's h3 at the same 18px/600 --
          // one size for three ranks. The topic is one bordered card among
          // many under its group heading, and reads as that now.
          fontSize: 'var(--t-lead)',
          lineHeight: 'var(--lh-lead)',
          fontWeight: 600,
        }}
      >
        {t.title}
      </h3>

      {t.long.map((para, i) => (
        <p
          key={i}
          style={{
            margin: '0 0 var(--ctl-s2)',
            // --t-body, not --t-lead: Help is a reference document and draws
            // many paragraphs; --t-lead is the ONE paragraph a screen leads
            // with, and a document has no such thing.
            fontSize: 'var(--t-body)',
            lineHeight: 'var(--lh-body)',
            color: 'var(--text-dim)',
            maxWidth: '68ch',
          }}
        >
          {para}
        </p>
      ))}

      {values.length > 0 && (
        <dl
          style={{
            display: 'grid',
            gridTemplateColumns: 'max-content 1fr',
            gap: '4px var(--ctl-s3)',
            margin: 'var(--ctl-s3) 0 0',
            alignItems: 'baseline',
          }}
        >
          {values.map((v) => (
            <div key={v.term} style={{ display: 'contents' }}>
              <dt
                style={{
                  fontWeight: 600,
                  fontSize: 'var(--t-meta)',
                  lineHeight: 'var(--lh-meta)',
                  fontFamily: 'var(--mono)',
                  color: 'var(--text)',
                  // The terms are the API's enums as their owner spells them
                  // -- `LEASED` -- and a list of raw capitals shouts every
                  // one (CH-3). Lowercased by style, so the string is still
                  // the owner's spelling for a copy, a search, and the
                  // anti-drift test in tests/help.test.ts.
                  textTransform: 'lowercase',
                }}
              >
                {v.term}
              </dt>
              <dd
                style={{
                  margin: 0,
                  fontSize: 'var(--t-body)',
                  lineHeight: 'var(--lh-body)',
                  color: 'var(--text-dim)',
                }}
              >
                {v.note}
              </dd>
            </div>
          ))}
        </dl>
      )}

      <p
        style={{
          margin: 'var(--ctl-s3) 0 0',
          // A raw anchor id: --t-micro, and it went UP from 10.5px.
          fontSize: 'var(--t-micro)',
          lineHeight: 'var(--lh-micro)',
          fontFamily: 'var(--mono)',
          color: 'var(--text-faint)',
        }}
      >
        #{t.anchor}
      </p>
    </div>
  )
}
