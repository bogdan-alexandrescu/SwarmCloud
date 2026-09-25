import { useEffect, useRef, type Ref } from 'react'
import { HELP, HELP_GROUPS, HELP_ROUTE, TOPIC_IDS, topicFor, type TopicId } from './help'
import { Absent } from './primitives'
import { PageHead } from './Shell'

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

  // THE HEAD LINE (AH-25): what the page is, and which topic is showing. Help
  // reads nothing, so it has no provenance to print, and §6.12 names it as
  // the one head whose line says those two things instead. Both are READ from
  // the registry, so the line cannot describe a page other than this one.
  //
  // IT IS NOT AH-15'S LINE BACK. That was a description sentence -- "why a
  // figure on these screens looks the way it does" -- true of about one topic
  // in eight, which is why AH-15 deleted it. This line is the head-line shape
  // every screen uses, facts joined by `·`, and each fact is true of the
  // whole page. A group with no topic is not drawn below, so it is not
  // counted here.
  const groups = HELP_GROUPS.filter((g) => TOPIC_IDS.some((id) => HELP[id].group === g.id)).length
  const line =
    `${TOPIC_IDS.length} topics in ${groups} groups` + (wanted === null ? '' : ` · showing ${wanted.title}`)

  return (
    <>
      {/* THE ONE PAGE HEAD (AH-25). It was `.ctl-page-head`, a second shape of
          head beside the `.head` fourteen routes draw through `Screen`. It is
          `PageHead` now, the same markup, with the line above under the title. */}
      <PageHead title="Help">{line}</PageHead>

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
 *
 * A ROW OF ITS GROUP, NOT A BOX (AH-18, design-system §13.3). Every topic was
 * an inline-styled bordered panel -- a border, a surface, a radius, the panel
 * padding -- because styles.css belonged to another track at the time. Under
 * §13.3 the group is the region and a topic is a row inside it, and a row
 * draws nothing: topics are separated by `--ctl-s5` of space and nothing else.
 * Everything that was inline here -- the h3, the paragraphs, the terms and the
 * anchor line -- is `.help-topic` in styles.css now, where the cascade tests in
 * `src/__tests__/shell.test.tsx` can ask what a browser would draw.
 *
 * THE DEEP-LINKED TOPIC IS `.is-current`, set from `highlighted`, and takes
 * §1.3's selection treatment: a `--surface-2` fill and a 2px `--text` rule on
 * the inline-start edge. Every topic declares that rule transparent, so
 * marking one current changes a colour and a fill and moves nothing.
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
      <h3>{t.title}</h3>

      {t.long.map((para, i) => (
        <p key={i}>{para}</p>
      ))}

      {values.length > 0 && (
        <dl>
          {values.map((v) => (
            // `display: contents` in the sheet, so each pair's `dt` and `dd`
            // are the grid's own cells and the terms share one column.
            <div key={v.term}>
              {/* The terms are the API's enums as their owner spells them --
                  `LEASED` -- and `.help-topic dt` lowercases them by style
                  (CH-3), so the string is still the owner's spelling for a
                  copy, a search, and the anti-drift test in tests/help.test.ts. */}
              <dt>{v.term}</dt>
              <dd>{v.note}</dd>
            </div>
          ))}
        </dl>
      )}

      {/* THE TOPICS THIS ONE SENDS A READER ON TO, as links (AH-21: the
          budget paragraph in `tenant-fields` cross-links `token-cost`). A
          title quoted in prose is a link nobody can follow. */}
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

      <p className="help-topic-anchor">#{t.anchor}</p>
    </div>
  )
}
