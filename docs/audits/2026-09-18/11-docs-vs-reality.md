# task_ecc2ee9f395142bd9822  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Compare docs/ and README.md against the actual code and CONTRACT.md. Find claims that are no longer true, or were never true. Note that the platform recently moved from 'zero idle cost' to running a GKE Autopilot cluster, and that gVisor was measured at 2.5x overhead rather than the 10-15% assumed. Quote each claim, say why it is wrong, say what is actually true. At most 6.

## Report

## Summary

I compared README.md and docs/ against CONTRACT.md and the actual Terraform code (`terraform/environments/{dev,prod}/*.tfvars` show `enable_gke_autopilot = true` is already live). Four claims no longer hold up, all centered on the same root cause — the platform's "zero-idle" framing predates the GKE Autopilot cluster now actually running:

1. **README.md:3** — the "zero-idle" tagline itself, contradicted by the always-on cluster fee.
2. **README.md:44** — "idle cost is zero," true only for the four Cloud Run services, misleading next to the GKE clause.
3. **docs/cost-control.md:17** — the cost breakdown table has no line for the GKE cluster-management fee at all.
4. **docs/BUILD_PROMPT_V2.md:453** — says fleet cost is "~0 when idle," directly contradicting that same document's §2.1 finding (~$0.10/hr cluster fee) just paragraphs earlier.

I didn't find any other doc repeating the old "~10–15%" gVisor overhead assumption — that number only appears inside BUILD_PROMPT_V2.md itself, where it's already correctly flagged and replaced with the measured 2.5×, so there was no separate stale claim to report there.
