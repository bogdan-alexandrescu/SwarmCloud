// The LLM providers tab's addresses (LlmProviders.tsx), apart from the page so
// App.tsx routes its sub-pages without loading it.

/** Capacity's tab id (App.tsx SECTIONS). */
export const LLM_PROVIDERS_TAB = 'llm-providers'

/** The tab's two sub-pages, in the control's order; the first is the tab's default. */
export const LLM_SUB_PAGES = ['plans', 'api-keys'] as const
export type LlmSubPage = (typeof LLM_SUB_PAGES)[number]

/** A sub-page off an address segment: anything else is Plans, the tab's first. */
export function llmSubPage(seg: string | null | undefined): LlmSubPage {
  return seg === 'api-keys' ? 'api-keys' : 'plans'
}

/** A sub-page's router address (`capacity/llm-providers/<sub>`). */
export function llmProvidersAddress(sub: LlmSubPage): string {
  return `capacity/${LLM_PROVIDERS_TAB}/${sub}`
}
