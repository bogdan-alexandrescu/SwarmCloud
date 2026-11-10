import json, re, subprocess, sys
from pathlib import Path
from swarm_browser import live

BASE = "http://127.0.0.1:5173"
OUT = Path(sys.argv[1]); ONLY = set(sys.argv[2].split(",")) if len(sys.argv) > 2 and sys.argv[2] else None
THEMES = sys.argv[3].split(",") if len(sys.argv) > 3 else ["light", "dark"]
SERIALIZE = """() => {
  document.querySelectorAll('input, textarea').forEach(e => { if (e.type === 'checkbox' || e.type === 'radio') { if (e.checked) e.setAttribute('checked', ''); else e.removeAttribute('checked'); } else e.setAttribute('value', e.value); });
  document.querySelectorAll('select').forEach(s => [...s.options].forEach(o => o.selected ? o.setAttribute('selected', '') : o.removeAttribute('selected')));
  document.querySelectorAll('[data-mock-scroll]').forEach(e => e.removeAttribute('data-mock-scroll'));
  document.querySelectorAll('*').forEach(e => { if (e.scrollTop > 0 || e.scrollLeft > 0) e.setAttribute('data-mock-scroll', e.scrollLeft + ',' + e.scrollTop); });
  document.documentElement.setAttribute('data-mock-win', window.scrollX + ',' + window.scrollY);
  const css = [];
  for (const sh of document.styleSheets) { try { css.push([...sh.cssRules].map(r => r.cssText).join('\\n')); } catch (e) {} }
  const c = document.documentElement.cloneNode(true);
  c.removeAttribute('data-theme');
  c.querySelectorAll('script, link[rel=stylesheet], link[rel=modulepreload], style').forEach(e => e.remove());
  const st = document.createElement('style'); st.textContent = css.join('\\n'); c.querySelector('head').appendChild(st);
  return '<!doctype html>' + c.outerHTML;
}"""
THEME = ("<script>var t=new URLSearchParams(location.search).get('theme');"
         "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);"
         "addEventListener('load',function(){document.querySelectorAll('[data-mock-scroll]').forEach(function(e){var v=e.getAttribute('data-mock-scroll').split(',');e.scrollLeft=+v[0];e.scrollTop=+v[1];});"
         "var w=(document.documentElement.getAttribute('data-mock-win')||'0,0').split(',');scrollTo(+w[0],+w[1]);});</script>")
FONT = re.compile(r'url\("?(/[^)"]+\.(?:woff2?|ttf|otf))"?\)')
fonts = set()

LP = "/capacity/llm-providers?lp=cards"
def fill(sel, val):
    def act(pg):
        pg.fill(sel, val)
    return act
def lend390(pg):
    pg.evaluate("() => { const p = document.querySelector('.lp-panel'); const r = p.getBoundingClientRect(); const top = document.querySelector('.c-topbar, header'); const h = top ? top.getBoundingClientRect().bottom : 56; let el = p.parentElement; while (el && !(el.scrollHeight > el.clientHeight + 4 && getComputedStyle(el).overflowY !== 'visible')) el = el.parentElement; const y = r.top - h - 64; if (el && el !== document.body && el !== document.documentElement) el.scrollTop += y; else window.scrollBy(0, y); }")
AGENT = sys.argv[4] if len(sys.argv) > 4 else "/agents/live/task_a073aff5?lp=cards"
FRAMES = [
    ("cards-plans", "/capacity/accounts?lp=cards", "desktop", None),
    ("cards-plan-caps", LP + "&tab=plans&open=p-ops&do=caps", "desktop", None),
    ("cards-plan-lend", LP + "&tab=plans&open=p-spare&do=lend", "desktop", None),
    ("cards-keys", LP + "&tab=api-keys", "desktop", None),
    ("cards-add-plan", LP + "&tab=plans&do=add-code", "desktop", fill("#signin-code", "QX7-M2PK-44RD")),
    ("cards-add-key", LP + "&tab=api-keys&do=add-form", "desktop", fill("#add-key", "pasted-value-for-the-mockup")),
    ("cards-add-key-done", LP + "&tab=api-keys&do=add-done", "desktop", None),
    ("cards-lend", LP + "&tab=api-keys&open=k-ci&do=lend", "desktop", None),
    ("cards-caps", LP + "&tab=api-keys&open=k-ci&do=caps", "desktop", None),
    ("cards-replace", LP + "&tab=api-keys&open=k-ci&do=replace", "desktop", fill("#rep-k-ci", "pasted-value-for-the-mockup")),
    ("cards-delete", LP + "&tab=api-keys&open=k-ci&do=delete", "desktop", None),
    ("cards-exhausted", LP + "&tab=api-keys&open=k-backup", "desktop", None),
    ("cards-voice-settings", "/voice/settings?lp=cards", "desktop", None),
    ("cards-voice-tier", "/voice/talk?lp=cards", "desktop", None),
    ("cards-attempt-tier", AGENT, "desktop", None),
    ("cards-many", LP + "&tab=api-keys&fx=many", "desktop", None),
    ("cards-many-plans", LP + "&tab=plans&fx=many", "desktop", None),
    ("cards-empty-keys", LP + "&tab=api-keys&fx=empty", "desktop", None),
    ("cards-keys-390", LP + "&tab=api-keys", "phone-390", None),
    ("cards-add-key-390", LP + "&tab=api-keys&do=add-form", "phone-390", fill("#add-key", "pasted-value-for-the-mockup")),
    ("cards-lend-390", LP + "&tab=api-keys&open=k-ci&do=lend", "phone-390", lend390),
]
pw = None
try:
    for theme in THEMES:
        for device in ("desktop", "phone-390"):
            todo = [f for f in FRAMES if f[2] == device and (not ONLY or f[0] in ONLY)]
            if not todo: continue
            if pw: pw.stop()
            subprocess.run(["swarm-handoff", "device", device], check=True, capture_output=True)
            subprocess.run(["swarm-handoff", "theme", theme], check=True, capture_output=True)
            pw, br, ctx = live()
            for name, url, _d, act in todo:
                pg = ctx.new_page()
                pg.add_init_script(f"try{{localStorage.setItem('swarm.theme','{theme}')}}catch(e){{}}")
                pg.goto(BASE + url); pg.wait_for_timeout(1600)
                if act: act(pg)
                pg.wait_for_timeout(500)
                d = OUT / "frames" / ("dark" if theme == "dark" else ""); d.mkdir(parents=True, exist_ok=True)
                pg.screenshot(path=str(d / f"{name}.png"))
                if theme == "light":
                    html = pg.evaluate(SERIALIZE)
                    fonts.update(FONT.findall(html))
                    html = FONT.sub(lambda m: f'url("../assets/{m.group(1).rsplit("/", 1)[-1]}")', html)
                    vp = "1440x900" if device == "desktop" else "390x844"
                    meta = f'<meta name="mockup-viewport" content="{vp}">'
                    (OUT / "frames" / f"{name}.html").write_text(html.replace("<head>", "<head>" + meta + THEME, 1))
                m = pg.evaluate("() => ({w: innerWidth, sw: document.documentElement.scrollWidth, url: location.pathname + location.search})")
                print(theme, name, m, flush=True)
                pg.close()
    (OUT / "assets").mkdir(parents=True, exist_ok=True)
    for f in fonts:
        import urllib.request
        (OUT / "assets" / f.rsplit("/", 1)[-1]).write_bytes(urllib.request.urlopen(BASE + f).read())
    print("fonts", len(fonts))
finally:
    pw.stop()
