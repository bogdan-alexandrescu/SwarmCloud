f='styles.css'
s=open(f).read()
def rep(a,b,count=1):
    global s
    assert a in s, a[:90]
    s=s.replace(a,b,count)
rep(""".sub,
.state p,""",""".state p,""")
a=s.index("/* `--ctl-s5`, not 24px (TS-21): 24 was one of seven values on this sheet that")
b=s.index("  font: inherit; cursor: pointer; padding: 0;\n}\n",a)+len("  font: inherit; cursor: pointer; padding: 0;\n}\n")
s=s[:a]+"""/* THE 16px SUMMARY LINE UNDER EVERY SCREEN TITLE (`.sub`) IS GONE (#138,
   owner ruling 2026-10-07, design-system §6.12: title left, actions right, no
   subtitle). Its refresh is `.c-refresh` in the head's actions, and the
   screen's count is `.c-count-note` over its first card -- both in the page
   head block (sec 17c). */
"""+s[b:]
rep(""".sub button[disabled] {
  opacity: .5;
  cursor: not-allowed;
}""",""".c-refresh[disabled] {
  opacity: .5;
  cursor: not-allowed;
}""")
rep(""".ctl-link,
.sub button,
.ctl-dock-tools a,""",""".ctl-link,
.ctl-dock-tools a,""")
rep(""".ctl-link:hover,
.sub button:not(:disabled):hover,""",""".ctl-link:hover,""")
rep(""".ctl-link:focus-visible,
.sub button:focus-visible,""",""".ctl-link:focus-visible,""")
rep("""/* `refresh` / `paused 12s`, in the summary line under every screen title. It
   is on all fifteen routes, which makes it the most-tabbed control in the app
   after the rail. */
.sub button:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; }""","""/* `⟳ 12 s` / `paused 12 s` / `Paused · resume`, the refresh in every page
   head's actions (#138). It is on every polled and read-once route, which
   makes it the most-tabbed control in the app after the rail. */
.c-refresh:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; }""")
rep("""  /* `.sub button` is every page head's refresh (Shell's and Overview's,
     which was `.ov-refresh` until the #503 swap). */
  .ctl-q-glyph,
  .sub button,""","""  /* `.c-refresh` is every page head's refresh (Shell's `RefreshControl`,
     which Overview and the Timeline draw too, #138). */
  .ctl-q-glyph,
  .c-refresh,""")
rep("""  .ctl-q-glyph::after,
  .sub button::after,""","""  .ctl-q-glyph::after,
  .c-refresh::after,""")
rep("""   is `PageHead` in Shell.tsx -- `.head > h1` over one `.sub` line of
   provenance -- and Help and Platform counts moved onto it from here.""","""   is `PageHead` in Shell.tsx -- `.head > h1` left, `.c-acts` right, and no
   line under the title (#138) -- and Help and Platform counts moved onto it
   from here.""")
a=s.index("/* --- the page head: ONE row (#503) ---")
b=s.index("/* A PLACEHOLDER IS AN INSTRUCTION, NOT A VALUE")
s=s[:a]+"""/* --- 17c. the page head: TITLE LEFT, ACTIONS RIGHT (#138) ---------------- */
/* §6.12, the owner's ruling of 2026-10-07: the title and its `?` on the left,
   the head's actions on the right (`.c-acts`), and NOTHING under the title.
   The 16px summary line and the meta chip that followed it on this row are
   gone: the count is `.c-count-note` over the first card, and the refresh is
   `.c-refresh`, a quiet control carrying the screen's ticking age. */
.c-phead { display: flex; flex-wrap: nowrap; align-items: center; gap: var(--ctl-s2) var(--ctl-s3); margin: 0 0 var(--ctl-s3); min-width: 0; }
.c-phead > .head { margin: 0; flex: none; align-items: center; gap: var(--ctl-s2); flex-wrap: nowrap; }
.c-phead > .c-acts { margin-left: auto; flex: 0 1 auto; min-width: 0; display: flex; flex-wrap: nowrap; align-items: center; justify-content: flex-end; gap: var(--ctl-s2) var(--ctl-s3); font: var(--t-micro)/var(--lh-micro) var(--font); color: var(--text-dim); }
.c-phead > .c-acts:empty { display: none; }
.c-acts > .ctl-head-age { margin: 0; min-width: 0; font: inherit; color: inherit; white-space: nowrap; }
/* QUIET: no box, no underline, no accent at rest -- the age is a reading,
   and the glyph says it is also the way to renew it. Ink on hover, the accent
   only as the focus ring (sec 1.3). Sans: a relative age is words, not a
   timestamp (Q1); tabular, so a ticking age does not jitter the row. */
.c-refresh { background: none; border: 0; padding: 2px 4px; margin: 0; border-radius: var(--ctl-radius-sm); font: var(--t-micro)/var(--lh-micro) var(--font); font-variant-numeric: tabular-nums; color: var(--text-dim); white-space: nowrap; cursor: pointer; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.c-refresh:not(:disabled):hover { color: var(--text); }
.c-refresh.is-stale { color: var(--warn); }
.c-refresh.is-paused { color: var(--text); font-weight: 500; }
/* THE COUNT, OVER THE FIRST CARD (#138): what was read, at the micro step,
   where it was read -- not a line under the title. */
.c-count-note { margin: 0 0 var(--ctl-s2); font: var(--t-micro)/var(--lh-micro) var(--font); color: var(--text-dim); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
"""+s[b:]
rep("""@media (max-width: 759px) {
  .c-phead { flex-wrap: wrap; }
  .c-phead > .sub { flex-wrap: wrap; }""","""@media (max-width: 759px) {
  .c-phead { flex-wrap: wrap; }
  .c-phead > .c-acts { flex-wrap: wrap; }""")
# old .c-meta / .c-age rules
a=s.index("/* THE SAME PILL IN BOTH THEMES (U10a D40")
b=s.index(".c-age > .ctl-head-age { margin: 0; min-width: 0; font: inherit; color: inherit; }\n")+len(".c-age > .ctl-head-age { margin: 0; min-width: 0; font: inherit; color: inherit; }\n")
s=s[:a]+s[b:]
open(f,'w').write(s)
