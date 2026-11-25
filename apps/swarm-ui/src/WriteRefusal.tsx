import { InfoTip } from './InfoTip'

/** How writeFailure (RepositoriesParts.tsx) begins its sentence for a route that is not there yet. */
const NOT_SERVED = 'Not served yet'

/**
 * A refused write's words in a banner (issue 439): a route not there yet is
 * one short line, writeFailure's sentence its (i), word for word; any other
 * refusal is the API's own message, shown as it came.
 */
export function WriteRefusal({ text }: Readonly<{ text: string }>) {
  if (!text.startsWith(`${NOT_SERVED}:`)) return <>{text}</>
  return (
    <>
      {NOT_SERVED} <InfoTip about="this refusal" tip={text} />
    </>
  )
}
