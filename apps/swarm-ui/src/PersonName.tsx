/**
 * A PERSON, AS OTHER PEOPLE SEE THEM (docs/design/user-profile.md §4.4, Q5):
 * `Name · email`, or the email alone when the person has set no name.
 *
 * The name is a label the person chose on Profile, never an identity: every
 * check that decides what someone may do uses the verified email, so the email
 * is always printed beside the name. A name the reader cannot tell from
 * somebody else's still arrives next to an address they can.
 *
 * WHEN SPACE IS SHORT THE NAME GIVES WAY, NEVER THE EMAIL. The name is the
 * part that can be chosen, so it is the part that is cut: it shrinks and ends
 * in an ellipsis while the email keeps its full width. A name is at most 64
 * characters (`display_names.check`), and a 64-character name in a narrow box
 * still leaves the email whole -- the case `person.name.test.tsx` renders.
 *
 * The styles are inline so the rule travels with the component into every
 * page that prints a person, whatever stylesheet that page loads. The value is
 * printed verbatim: an API value that is not an address (an approval's expiry
 * actor) prints alone, as it did before names existed.
 *
 * `data-person-name` is the marker the tests find on each caller (Approvals'
 * `decided_by`, People's row, the workflow page's owner, the task page's
 * submitter), so a caller that goes back to printing the email by hand fails.
 *
 * FIRST NAME BY DEFAULT, FULL NAME WHERE THE PERSON IS THE SUBJECT (owner,
 * 2026-10-19, user-profile.md §7.1 amendment): `variant="first"` prints the
 * saved name's first word, and only Admin › People and Settings › Profile pass
 * `full`. The email still stands beside the name either way, and the whole
 * name is in the title, so the short form hides nothing a hover cannot show.
 */
import type { CSSProperties } from 'react'

const BOX: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'baseline',
  minWidth: 0,
  maxWidth: '100%',
  verticalAlign: 'bottom',
  whiteSpace: 'nowrap',
}

/** Shrinks first, to nothing if it must, and says it was cut. */
const NAME: CSSProperties = {
  flexGrow: 0,
  flexShrink: 1,
  flexBasis: 'auto',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

/** `pre`: a flex item would trim the spaces around the dot. */
const DOT: CSSProperties = { flexShrink: 0, whiteSpace: 'pre' }

/** Never shrinks: the address is the part that identifies. */
const EMAIL: CSSProperties = { flexShrink: 0, whiteSpace: 'nowrap' }

export type PersonNameVariant = 'first' | 'full'

/**
 * The first word of a saved name, split on whitespace only: a hyphen or an
 * apostrophe is part of the word ("Mary-Jane O'Neil" is "Mary-Jane"), and a
 * one-word name is the whole name.
 */
export function firstName(name: string): string {
  return name.trim().split(/\s+/)[0] ?? ''
}

export function PersonName({
  email,
  name,
  className,
  variant = 'first',
}: {
  email: string
  name?: string | null
  className?: string
  variant?: PersonNameVariant
}) {
  const whole = typeof name === 'string' && name.trim() !== '' ? name : null
  const shown = whole === null ? null : variant === 'full' ? whole : firstName(whole)
  return (
    <span
      className={className === undefined ? 'person-name' : `person-name ${className}`}
      data-person-name=""
      data-email={email}
      title={whole === null ? email : `${whole} · ${email}`}
      style={BOX}
    >
      {shown !== null && (
        <>
          <span className="person-name-name" style={NAME}>
            {shown}
          </span>
          <span className="person-name-dot" style={DOT} aria-hidden="true">
            {' · '}
          </span>
        </>
      )}
      <span className="person-name-email" style={EMAIL}>
        {email}
      </span>
    </span>
  )
}
