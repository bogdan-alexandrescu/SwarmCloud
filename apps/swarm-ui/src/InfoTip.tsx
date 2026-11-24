import { useId, useState } from 'react'
import { buttonClass } from './components'

/**
 * THE (i) (the owner's standing rule, 2026-11-05: no explanatory prose on the
 * surface). A sentence that explains a label sits behind this button beside
 * it: open on hover, on focus and on a tap (Safari never focuses a tapped
 * button, so focus alone is not enough), closed on Escape, on the pointer
 * leaving and on a tap elsewhere. The tip stays in the document, hidden, as
 * the button's description, so a screen reader and a test read it without
 * opening it; `data-help-description` keeps it out of the words the prose
 * ratchet counts (proseapp.tsx), as Profile's AboutTip and HelpNote are kept.
 * `.c-tip-host` and `.c-tip` are the components' tooltip; `end` anchors the
 * tip's right edge to the button, for an (i) near the right of the screen.
 * The negative block margin keeps the 28px button from heightening a line of
 * text it sits in.
 * Exported for every page that draws one. It has its own module, apart from
 * People.tsx, so a page that draws an (i) does not load People with it.
 */
export function InfoTip({ about, tip, end = false }: { about: string; tip: string; end?: boolean }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  return (
    <span className="c-tip-host info-tip" style={{ marginBlock: -6, verticalAlign: 'middle' }} onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })}
        aria-label={`About ${about}`}
        aria-describedby={id}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setOpen(false)
        }}
      >
        <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 11v5.5M12 7.75v.01" />
        </svg>
      </button>
      <span role="tooltip" id={id} className="c-tip" data-help-description="" hidden={!open} style={end ? { left: 'auto', right: 0 } : undefined}>
        {tip}
      </span>
    </span>
  )
}

/** What an action did or refused, as a page draws it: one short line, and the sentence in full behind its (i) (issue 81). */
export interface Said {
  line: string
  tip?: string | undefined
}

export function SaidLine({ said, about }: { said: Said; about: string }) {
  return (
    <>
      {said.line}
      {said.tip !== undefined && (
        <>
          {' '}
          <InfoTip about={about} tip={said.tip} />
        </>
      )}
    </>
  )
}
