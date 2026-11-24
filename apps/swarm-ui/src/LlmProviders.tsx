/**
 * CAPACITY › LLM PROVIDERS (lane LP-U1; docs/design/llm-providers.md §6.1, the
 * owner's pick of 10 November 2026, direction "Tables and a side sheet").
 *
 * One Capacity tab with two sub-pages, each its own address: Plans
 * (`/capacity/llm-providers/plans`), the subscription plans, and API Keys
 * (`/capacity/llm-providers/api-keys`), the developer keys. The old
 * `/capacity/accounts` lands on Plans with its `?account=` kept (App.tsx
 * `fromAddress`).
 *
 * THE SUB-PAGE CONTROL IS NAVIGATION, drawn the way the picked direction draws
 * it and the way Pools' Ceilings | By runner profile strip already is: links in
 * a labelled `nav`, the current one `aria-current="page"` (`Segmented`'s link
 * form). The side panel carries the same two as LLM Providers' first children
 * (Spine.tsx `PANEL_PAGES`).
 *
 * PLANS IS THE PICKED TABLE (lane LP-U2, LlmPlans.tsx): the plans grouped by
 * vendor, a line per lend under its plan, and every plan control in a side
 * sheet; `?account=` opens that plan's Manage sheet. Its head is the page's
 * one head, titled "LLM Providers".
 *
 * API KEYS IS THE CARDS DIRECTION (lane LP-U3, LlmApiKeys.tsx; the owner's
 * ask of 11 November 2026): one card a key, grouped by vendor, with its
 * spend, holders, lends and controls. Its head is the page's one head, as
 * Plans' is, with the tab's (i) and the strip under it.
 *
 * No visible prose (§6.5): each explanation is a tooltip behind an (i).
 */
import type { ReactNode } from 'react'
import { loadApiKeys } from './api'
import { Info } from './AccountsVerdict'
import { ACCOUNTS_POLL_MS, capacityPoll } from './capacityPoll'
import { LlmApiKeysBody } from './LlmApiKeys'
import { LlmPlansScreen } from './LlmPlans'
import { Screen } from './Shell'
import { Segmented } from './components'
import { LLM_SUB_PAGES, llmProvidersAddress, type LlmSubPage } from './llmPaths'

export { LLM_PROVIDERS_TAB, LLM_SUB_PAGES, llmProvidersAddress, llmSubPage, type LlmSubPage } from './llmPaths'

const SUB_LABEL: Readonly<Record<LlmSubPage, string>> = { plans: 'Plans', 'api-keys': 'API Keys' }

/** Plans | API Keys, each its own address. */
function SubPages({ sub }: { sub: LlmSubPage }) {
  return (
    <Segmented
      label="LLM Providers"
      value={sub}
      options={LLM_SUB_PAGES.map((s) => ({ key: s, label: SUB_LABEL[s], href: `/${llmProvidersAddress(s)}` }))}
    />
  )
}

/** What the tab is for, behind its (i) (no visible prose, §6.5). */
const INFO = (
  <Info about="LLM Providers" tip="Agents take a plan first, then the shared API key pool; Voice it then falls to its own OpenAI key. The vendors never stand in for each other." />
)

/**
 * API Keys: `Screen` draws the one head, titled with the tab's words, its (i)
 * and the strip under it in every read state; the body is the key cards
 * (LlmApiKeys.tsx) once the list has been read.
 */
function ApiKeysPage({ nav }: { nav: ReactNode }) {
  return (
    // Re-read every 30 s (60 s on a phone), as Plans does, paused while the tab is hidden.
    <Screen title="LLM Providers" info={INFO} nav={nav} load={loadApiKeys} pollMs={capacityPoll(ACCOUNTS_POLL_MS)}>
      {(data) => <LlmApiKeysBody board={data} />}
    </Screen>
  )
}

export function LlmProvidersScreen({ sub }: { sub: LlmSubPage }) {
  // HEADED WITH THE TAB'S OWN WORDS (a nav-headings guard test holds it), the strip
  // under the head as the picked direction and Pools draw it. ONE HEAD A PAGE
  // (qa.head Q2/Q10): on Plans the table's `Screen` head is the page's,
  // titled "LLM Providers", with its refresh and its age. `Screen` draws the
  // tab's (i) by the title and the strip under the head in every read state
  // (the owner's answer of 11 November 2026), so both show while the plans
  // load and after their read failed. API Keys' cards draw the same head.
  const nav = (
    <div className="cap-seg">
      <SubPages sub={sub} />
    </div>
  )
  if (sub === 'plans') return <LlmPlansScreen title="LLM Providers" info={INFO} nav={nav} />
  return <ApiKeysPage nav={nav} />
}
