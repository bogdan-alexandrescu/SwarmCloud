// JEV-USAGE-PAGE (part of #2688): `GET /v1/jev-usage` as swarm-api serves it
// (`swarm_api.jevusage.summarise`), for the dev console and the page's test.
// No rate is set, so every `usd` is null; one day's record is a row with no
// per-kind split, and one call was skipped for the per-task call limit.

const outcomes = (ok: number, failed = 0, skipped: Record<string, number> = {}) => ({
  ok,
  failed,
  skipped: Object.values(skipped).reduce((a, b) => a + b, 0),
  skipped_by_reason: skipped,
})

export function fixtureJevUsage(): Record<string, unknown> {
  return {
    tenant_id: 'eng',
    window: { from: '2026-10-04', to: '2026-11-02', days: 30 },
    rate: { input_usd_per_million: null, output_usd_per_million: null, set: false },
    days: [
      {
        day: '2026-11-02',
        calls: 4,
        input_tokens: 11_000,
        output_tokens: 150,
        outcomes: outcomes(3, 1, { budget_calls: 1, oversize: 2 }),
        usd: null,
        kinds: [
          {
            kind: 'J1',
            group: 'worker_judgment',
            calls: 2,
            input_tokens: 5_000,
            output_tokens: 150,
            outcomes: outcomes(2, 0, { oversize: 2 }),
            usd: null,
          },
          {
            kind: 'J4',
            group: 'quality_gate',
            calls: 2,
            input_tokens: 6_000,
            output_tokens: null,
            outcomes: outcomes(1, 1, { budget_calls: 1 }),
            usd: null,
          },
        ],
      },
      {
        day: '2026-10-20',
        calls: 2,
        input_tokens: 4_000,
        output_tokens: null,
        outcomes: outcomes(0),
        usd: null,
        kinds: [
          {
            kind: 'unattributed',
            group: 'unattributed',
            calls: 2,
            input_tokens: 4_000,
            output_tokens: null,
            outcomes: outcomes(0),
            usd: null,
          },
        ],
      },
    ],
    total: {
      calls: 6,
      input_tokens: 15_000,
      output_tokens: 150,
      outcomes: outcomes(3, 1, { budget_calls: 1, oversize: 2 }),
      usd: null,
      by_kind: [],
    },
    undated: null,
    month: { month: '2026-11', used_tokens: 11_000, cap_tokens: 50_000, used_fraction: 0.22 },
  }
}
