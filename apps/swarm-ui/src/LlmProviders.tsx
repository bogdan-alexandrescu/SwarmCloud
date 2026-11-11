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
import { Info } from './AccountsVerdict'
import { LlmApiKeysScreen } from './LlmApiKeys'
import { LlmPlansScreen } from './LlmPlans'
import { Segmented } from './components'

/** Capacity's tab id (App.tsx SECTIONS). */
export const LLM_PROVIDERS_TAB = 'llm-providers'

/** The tab's two sub-pages, in the control's order; the first is the tab's default. */
export const LLM_SUB_PAGES = ['plans', 'api-keys'] as const
export type LlmSubPage = (typeof LLM_SUB_PAGES)[number]

const SUB_LABEL: Readonly<Record<LlmSubPage, string>> = { plans: 'Plans', 'api-keys': 'API Keys' }

/** A sub-page off an address segment: anything else is Plans, the tab's first. */
export function llmSubPage(seg: string | null | undefined): LlmSubPage {
  return seg === 'api-keys' ? 'api-keys' : 'plans'
}

/** A sub-page's router address (`capacity/llm-providers/<sub>`). */
export function llmProvidersAddress(sub: LlmSubPage): string {
  return `capacity/${LLM_PROVIDERS_TAB}/${sub}`
}

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
  <Info about="LLM Providers" tip="Agents take a plan first, then the shared API key pool; Voice it then falls to its own dedicated OpenAI key. The vendors never stand in for each other." />
)

export function LlmProvidersScreen({ sub }: { sub: LlmSubPage }) {
  // HEADED WITH THE TAB'S OWN WORDS (test_nav_headings_agree.py), the strip
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
  return <LlmApiKeysScreen title="LLM Providers" info={INFO} nav={nav} />
}
