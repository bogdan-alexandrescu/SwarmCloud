// The company's name, spelled once for the whole console.
//
// WHY THIS FILE EXISTS. Owner decision, 2026-10-19 (#1647): the console names
// no company. Its copy says "inside your network", its examples are the RFC
// 2606 example.com family, and the GitHub App's name is whatever the API
// serves. `test/no-company-name.test.mjs` walks apps/swarm-ui and fails on any
// line that matches the pattern below, so the pattern has to live somewhere
// that walk skips -- and this is the one file it skips, by path. The component
// tests that assert a rendered screen does not name the company import it from
// here rather than spelling it themselves, which would put the name back into
// src/.
//
// Nothing else belongs in this file: whatever is added here is invisible to
// the guard.
export const COMPANY_NAME = /saga/i

// The path the guard exempts, relative to apps/swarm-ui.
export const COMPANY_NAME_FILE = 'test/companyName.ts'
