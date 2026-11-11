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
 * API KEYS IS AN EMPTY STATE until the broker and swarm-api lanes (LP-B1,
 * LP-A1) ship the key calls: nothing reads keys yet, so there is no read mark
 * claiming one succeeded, and Add key is a disabled button whose (i) says why.
 * No visible prose (§6.5): each explanation is a tooltip behind an (i).
 */
import { Info } from './AccountsVerdict'
import { LlmPlansScreen } from './LlmPlans'
import { PageHead } from './Shell'
import { Button, Segmented } from './components'

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

/** API Keys' empty state: no key calls exist yet, so Add key is disabled and its (i) says why. */
function ApiKeys() {
  return (
    <section className="c-emp" aria-label="API Keys">
      <h3>
        No API keys
        <Info about="No API keys" tip="Agents fall back to keys only after the plans, so a page with none is valid." />
      </h3>
      <span className="c-btn-wrap">
        <Button kind="primary" disabled>
          Add key
        </Button>
        <Info about="Add key" tip="Adding a key opens once the broker and swarm-api take keys." />
      </span>
    </section>
  )
}

export function LlmProvidersScreen({ sub }: { sub: LlmSubPage }) {
  // HEADED WITH THE TAB'S OWN WORDS (test_nav_headings_agree.py), the strip
  // under the head as the picked direction and Pools draw it. ONE HEAD A PAGE
  // (qa.head Q2/Q10): on Plans the table's `Screen` head is the page's,
  // titled "LLM Providers", with its refresh and its age. `Screen` draws the
  // tab's (i) by the title and the strip under the head in every read state
  // (the owner's answer of 11 November 2026), so both show while the plans
  // load and after their read failed. API Keys reads nothing and draws the
  // head itself, (i) by the title, the same placement.
  if (sub === 'plans') {
    return (
      <LlmPlansScreen
        title="LLM Providers"
        info={INFO}
        nav={
          <div className="cap-seg">
            <SubPages sub={sub} />
          </div>
        }
      />
    )
  }
  return (
    <>
      <PageHead title="LLM Providers" info={INFO} />
      <div className="cap-seg">
        <SubPages sub={sub} />
      </div>
      <ApiKeys />
    </>
  )
}
