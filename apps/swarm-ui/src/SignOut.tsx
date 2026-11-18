/**
 * The Sign out control and the return-trip notice (signoutState.ts has the why).
 *
 * The button is drawn twice by Spine.tsx: on the panel's name row, as an icon
 * and the word (the word is dropped below 760px, styles.css `.sk-out-l`), and
 * on the collapsed spine beside the avatar, as the icon alone. Either way its
 * accessible name is "Sign out". It is a plain button with no shortcut: a
 * click or Enter/Space on it while focused, nothing else.
 */
import { useState } from 'react'

import { Banner, Button, ButtonLink } from './components'
import { accountChooserUrl, forgetSignOut, returnedAs, signOut } from './signoutState'

/** A door with an arrow leaving it, in the spine's 24px stroke style. */
function OutIcon() {
  return (
    <svg className="sk-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M14 4.5H7a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h7" />
      <path d="M11 12h9.5M17 8.5l3.5 3.5-3.5 3.5" />
    </svg>
  )
}

export function SignOutButton({ email, compact = false }: { email: string | null; compact?: boolean }) {
  return (
    <button
      type="button"
      className={compact ? 'sk-out is-icon' : 'sk-out'}
      aria-label="Sign out"
      title={email === null ? 'Sign out of SwarmCloud' : `Sign out ${email} from SwarmCloud (you stay signed in to Google)`}
      onClick={() => signOut(email)}
    >
      <OutIcon />
      {!compact && <span className="sk-out-l">Sign out</span>}
    </button>
  )
}

/**
 * IAP signed the same account straight back in: say so, and offer Google's
 * account chooser. Drawn only when this tab signed out and the SAME email
 * arrived; a different one is the sign-out having worked.
 */
export function SignedBackIn({ email }: { email: string }) {
  const [shown, setShown] = useState(() => returnedAs(email) === 'same')
  if (!shown) return null
  return (
    <div className="sk-back">
      <Banner
        tone="info"
        title={`You signed out, and Google signed you back in as ${email}.`}
        actions={
          <>
            <ButtonLink size="sm" href={accountChooserUrl(window.location.origin)}>
              Choose another account
            </ButtonLink>
            <Button
              size="sm"
              onClick={() => {
                forgetSignOut()
                setShown(false)
              }}
            >
              Stay as {email}
            </Button>
          </>
        }
      >
        That is the only Google account signed in to this browser, so Google picked it without asking. Choose another account to add or pick one in Google&rsquo;s account chooser; you stay signed in to Google either way. If Google does not bring you back here, open this address again.
      </Banner>
    </div>
  )
}
