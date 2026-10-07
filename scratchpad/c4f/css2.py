f='styles/agents.css'
s=open(f).read()
a=""".app.has-inspector > .work .c-phead > .sub { flex: 1 1 0; min-width: 0; flex-wrap: nowrap; overflow: hidden; }"""
assert a in s
s=s.replace(a,""".app.has-inspector > .work .c-phead > .c-acts { flex: 1 1 0; min-width: 0; flex-wrap: nowrap; overflow: hidden; }""")
s=s.replace("""   the split the title keeps its whole width and never breaks; the meta chip
   and then the freshness give way, each with an ellipsis. */""","""   the split the title keeps its whole width and never breaks; the head's
   actions give way, the refresh's words with an ellipsis. */""")
a=s.index("/* THE PROVENANCE KEEPS ITS CONTROL (U11a N10")
b=s.index(".app.has-inspector > .work .c-phead .c-meta { flex: 0 1 auto; }\n")+len(".app.has-inspector > .work .c-phead .c-meta { flex: 0 1 auto; }\n")
s=s[:a]+"""/* THE REFRESH GIVES WAY, NOT THE TITLE (U11a N10, owner QA 2026-10-04;
   #138). Beside an open agent the list head has ~150px for its actions; the
   refresh control (`.c-refresh`, Shell.tsx `RefreshControl`) is the one that
   shrinks, its words cut with an ellipsis and whole in its title and its
   accessible name. It is one button, so cutting it can never cut the press
   off its age. */
.app.has-inspector > .work .c-phead .c-acts > .c-refresh { flex: 0 1 auto; }
"""+s[b:]
open(f,'w').write(s)

f='styles/overview.css'
s=open(f).read()
for a,b in [("""  .ov-page > .c-phead .c-age { margin-left: 0; }\n""",""),
            ("""  /* THE `?` AND THE TENANT CHIP SHARE THE LINE (U10a D39, owner QA at 390px,
     2026-10-04): with the h1 stepped aside the head held only the `?`, and
     the row wrapped it onto a line of its own above the chip. The row does
     not wrap; the provenance wraps inside its own half when it must. */""","""  /* THE `?` AND THE REFRESH SHARE THE LINE (U10a D39, owner QA at 390px,
     2026-10-04): with the h1 stepped aside the head held only the `?`, and
     the row wrapped it onto a line of its own. The row does not wrap; the
     actions wrap inside their own half when they must. */"""),
            ("""  .ov-page > .c-phead > .sub { flex: 1 1 0; min-width: 0; }""","""  .ov-page > .c-phead > .c-acts { flex: 1 1 0; min-width: 0; }""")]:
    assert a in s, a[:60]
    s=s.replace(a,b)
open(f,'w').write(s)

f='styles/intake.css'
s=open(f).read()
a=""".rn-run .c-phead > .sub { flex: 1 1 0; min-width: 0; max-width: 100%; }"""
assert a in s
s=s.replace(a,""".rn-run .c-phead > .c-acts { flex: 1 1 0; min-width: 0; max-width: 100%; }""")
s=s.replace("""/* THE META TAKES WHAT THE ROW LEAVES (browser QA D11): a zero basis, cut
   with its title, and never wider than the column. */""","""/* THE ACTIONS TAKE WHAT THE ROW LEAVES (browser QA D11): a zero basis, cut
   with their title, and never wider than the column. */""")
open(f,'w').write(s)

f='styles/admin.css'
s=open(f).read()
a=s.index("/* PLATFORM COUNTS: THE RUN IS A BUTTON BESIDE THE TITLE")
b=s.index(".c-phead .counts-run { align-self: center; }")
s=s[:a]+"""/* PLATFORM COUNTS: THE RUN IS THE HEAD'S ACTION (#503 frames 4-5 of
   admin-help.html; AH-25 and #138, owner ruling 2026-10-07). It was an
   underlined `.sub button` inside the provenance sentence, then a button
   beside the title -- a second head shape on one Admin tab. It sits in the
   head's actions now, right of the title like every screen's refresh, after
   what the last count found. Drawn to components.html A -- 32px tall,
   primary before the first run and plain after it, when it is a re-run --
   except the radius: A's 8px is off spacing.test.tsx's corner scale (0, 2,
   6, 10, 14, 999), so the control takes the scale's control radius, 6px. */
.c-phead .counts-prov { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
"""+s[b:]
open(f,'w').write(s)
