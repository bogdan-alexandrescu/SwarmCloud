# SwarmCloud console mock-ups and the owner's picks

These are the approved design mock-ups for the console, kept here so that a
lane building a screen can read the frames it is building. Every page is
standalone HTML: open it in a browser. All data on them is **example data**;
the user is "Operator" and hosts are `swarm.example.com`.

Each page has an inventory measured from the code at the time it was drawn,
variants A/B/C with Idea / Pros / Risks, and a recommendation. **Build the
picked variant only.** The other variants are kept because the reasons they
lost are the record of the decision.

## Rules that override the mock-ups

These were decided after batch 1 and 2 were drawn, and win wherever a frame
disagrees:

* **Type:** 12px floor, no all-caps, no letter-spacing. The batch 1-2 frames
  still draw small caps eyebrows; do not copy them.
* **Theme:** a light / dark / system toggle in the panel's user footer.
* **Honesty:** an unknown figure is a dash with its reason, never 0
  (docs/web-ui/redesign-v2.md, "Honesty rules").

## Batch 1-2 (picked 2026-10-01; the console is built on these)

| Page | Pick |
|---|---|
| brand.html | Palette A "Open sky" (accent #0369a1 light / #7cc8f8 dark), mark M1 Hive; spine logo 44px Hive in a white→#7dd3fc gradient, no tile |
| navigation.html | V2 Sky spine: 84px spine of sections + 236px panel of the section's pages; Submit is the sky button at the top of the spine; phone header takes the spine colour |
| overview.html | O1 Lead and ledger |
| agents.html | V1 compact list + wide detail, collapse toggle, divider snapping 64px / 380px / 50%, width remembered per device |
| workflows.html | V2 full-width pages (/workflows list + /workflows/<id>), Recent (5) switcher in the panel |
| capacity.html | Ceilings table by family with "Needs a look" first; Accounts as a split view; polling Pools/Holders 30s, Accounts 60s, paused in hidden tabs |
| admin-help.html | Pool limits = L2 side editor with impact and history, typed confirm for 0 or a cut of half or more; Help = H1 one page per group with search |
| submit.html | /submit chooser page (N opens it); one-page task form; workflow form stacked top to bottom with on_step_failure and priority in it |

## Batch 3 (picked 2026-10-02)

| Page | Pick |
|---|---|
| components.html | **A as built**: 32px rows, 8px controls, 12px cards, pill chips; control border token at 3:1 (#7d8ea3 light / #5f7087 dark); radius scale 4/8/9/12/14/999; table heads 12px sentence case; toasts only acknowledge the viewer's own action (failures stay as banners); bars 6px; hatching means only "not measured". One canonical set replaces the 12 button / 11 chip / 7 bar styles in the live app |
| states.html | **C three tiers**: whole-app states take over the content area with the nav still usable; page states are one banner over dimmed older data; region states show in place. Plus: typed confirm on Cancel workflow while a step runs; a "tab was hidden, reading now" line; an offline banner |
| timeline.html | **A swimlanes**: one lane per agent grouped under its workflow; bars are attempts holding capacity, hatched bands are parks (no capacity), a cut bar is a fenced generation. Two pages: /timeline Lanes (default) and /timeline/outcomes (today's ledger, unchanged). Reads /v1/attempts and events; no new route |
| intake-tenants.html | **1A**: a third card on /submit opens /submit/issue, with the issue previewed before submit (title, body, labels), runner, plan approval, auto-merge off by default and disabled until #295, fix rounds (3, range 1-5); a closed issue warns with "Plan it anyway"; submitting lands on a new Work › Runs page at /runs/<id>. **Tenant switcher 2A** on top of #501: collapsed-spine tenant tile, phone header chip, a switch-back toast, and an unsent form is KEPT with its button naming the new tenant |
| agent-detail-2.html | **A in the split detail**: a Children tab on a parent and a "child of …" link on a child; a verdict card on a review step (findings by severity, "Not graded" while the verdict has no severity field); the merge step's ordered checklist card; the Code card opens with one sentence on what happened, then the base-pin line, then the per-file diff; FAILED published_nothing and PUBLISH_REFUSED drawn |
| viewers.html | **The log dock is SUPERSEDED (owner decision 2026-10-04): logs are a tab.** The owner does not want the log component overlapping the agent details, so the bottom dock -- its strip, resize handle, `Open full` overlay and bottom sheet -- is gone. A full-height **Logs** tab sits beside Details / Attempts / Artifacts / Checkpoints / Children at `/agents/<tab>/<id>/logs` and fills the whole detail area under the header: the four streams as a segmented control (Transcript / Agent stdout / Agent stderr / Runner), attempt picker by generation, search, Follow (live agents only), Wrap, jump to error, masked count, the notice when output is missing between two reads, and the stream metadata (gs path, size) folded by default; one row of controls, overflow into More. A running agent opens on Logs, a finished one on Details, whose one-line last log line opens the tab. A phone has the same tab, full screen. Pick A's viewers for text, JSON, markdown, image, binary and checkpoint contents stand |
| wide-workflows.html | **A, extends what is live**: stage rows top to bottom, a stage never wraps; a wide stage's band gains a state-mix bar, failed → running → parked steps as chips, "holds N slots · M waiting hold none", edges bundled one lane per target and lit on hover/select, 1:1 children aligned under their parents; a band count opens the Table filtered to that stage and state. A step skipped by its verdict gate (`result_summary.verdict_gate.agent_ran: false`) gets its own look |

The live app was audited against the batch 1-2 picks on 2026-10-02: #503.
