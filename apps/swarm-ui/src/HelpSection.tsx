import { useEffect, useRef, type Ref } from 'react'
import { HELP, HELP_GROUPS, TOPIC_IDS, topicFor, type TopicId } from './help'

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
  useEffect(() => {
    target.current?.scrollIntoView({ block: 'start' })
  }, [topic])

  return (
    <>
      <div className="head">
        <h1>Help</h1>
      </div>
      <p className="sub">
        Why a figure on these screens looks the way it does.
        {wanted !== null && <> Showing {wanted.title}.</>}
      </p>

      {topic !== '' && wanted === null && (
        <div className="ctl-empty is-partial" role="status">
          <h3>
            No help topic is called <code>{topic}</code>
          </h3>
          <p>
            The link that brought you here names a topic this build does not
            carry. Everything this build does carry is below.
          </p>
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
        borderLeft: highlighted ? '3px solid var(--text-dim)' : '1px solid var(--line)',
        borderRadius: 'var(--radius)',
        background: 'var(--surface)',
        padding: 'var(--ctl-s4)',
        marginBottom: 'var(--ctl-s3)',
        scrollMarginTop: 'var(--ctl-s5)',
      }}
    >
      <h3 style={{ margin: '0 0 var(--ctl-s2)', fontSize: '14.5px', fontWeight: 600 }}>
        {t.title}
      </h3>

      {t.long.map((para, i) => (
        <p
          key={i}
          style={{
            margin: '0 0 var(--ctl-s2)',
            fontSize: '13px',
            lineHeight: 1.6,
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
              <dt style={{ font: '600 11px/1.7 var(--mono)', color: 'var(--text)' }}>{v.term}</dt>
              <dd
                style={{
                  margin: 0,
                  fontSize: '12px',
                  lineHeight: 1.7,
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
          font: '10.5px/1.5 var(--mono)',
          color: 'var(--text-faint)',
        }}
      >
        #{t.anchor}
      </p>
    </div>
  )
}
