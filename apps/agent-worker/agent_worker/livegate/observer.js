// The live gate's mask observer (docs/design/live-browser.md §5.3.2).
//
// Runs in an ISOLATED WORLD of every frame (`livegate.mask.MaskCdp.install`):
// it shares the page's DOM but none of its JavaScript, so a page script can
// neither see this code nor call the binding it reports through, which exists
// only in this world. It reports, and decides nothing: which text is a secret
// is decided by the gate, in Python, with `swarm_redaction`'s rules.
//
// Two messages, both JSON through the binding:
//
//   {"v":1,"k":"layout","doc":D,"e":N}
//       sent the moment something may have moved or changed (a mutation, in
//       the document or an open shadow root; a scroll; a resize, of the
//       viewport or of any element the last report measured; typing; a
//       pointer or key event that can flip :hover, :active or :focus; a
//       load, including an <img> or <iframe> whose `load` never reaches
//       `window`). It is sent from the event, observer or mutation callback,
//       before the next paint, so the gate hears that its last report is
//       stale before the pixels change.
//   {"v":1,"k":"report","doc":D,"e":N,"vp":[...],"f":[...],"t":[...],"o":[...],"tr":B}
//       measured in the next animation frame (after layout, before paint),
//       carrying the epoch N it was measured at. Also sent once a second
//       with no change, so a gate that missed one (a reconnect, a report
//       raced by a navigation) recovers without the page having to move.
//
// `doc` is random per document instance. Field ids and iframe ids are
// per-document numbers this world assigns; the gate keeps its own copy of the
// "ever a password" set by id.
//
// What is reported is bounded (MAX_*). A report that hit a bound says so
// ("tr": true) and the gate blanks the whole page area for it: text this
// observer did not report is text no rule was run on.
(() => {
  "use strict";

  const BINDING = "__swarmLiveMask";
  if (globalThis.__swarmLiveMaskInstalled) return;
  globalThis.__swarmLiveMaskInstalled = true;
  const send = globalThis[BINDING];
  // No binding: no reports, and the gate shows nothing of this frame.
  if (typeof send !== "function") return;

  const SENSITIVE_AUTOCOMPLETE = new Set([
    "current-password", "new-password", "one-time-code", "cc-number", "cc-csc",
  ]);
  const MAX_NODES_VISITED = 60000;
  const MAX_TEXT_ITEMS = 6000;
  const MAX_TEXT_CHARS = 512 * 1024;
  const HEARTBEAT_MS = 1000;
  const VALUE_INPUT_TYPES = new Set([
    "", "text", "search", "url", "email", "tel", "number",
  ]);
  const INLINE_DISPLAYS = new Set(["inline", "contents", "ruby", "ruby-text"]);
  // Elements whose size can change with no mutation: a load, a decode, a
  // video's metadata. Their resize is announced through `resizes`.
  const REPLACED = new Set([
    "img", "picture", "video", "audio", "canvas", "svg", "iframe", "frame",
    "embed", "object", "input", "textarea", "select",
  ]);
  // Listened for on `window`, capturing. :hover, :active and :focus-visible
  // reveal or move content with no mutation; the pointer or key event that
  // flips them is the only announcement there is.
  const WINDOW_TRIGGERS = [
    "scroll", "resize", "input", "change", "load", "pageshow",
    "transitionrun", "transitionend", "transitioncancel", "animationstart",
    "animationiteration", "animationend", "animationcancel", "focusin", "focusout", "toggle",
    "pointerover", "pointerout", "pointerdown", "pointerup", "pointercancel",
    "keydown", "keyup",
  ];
  // Listened for on `document` and on every open shadow root, capturing. The
  // HTML spec's "get the parent" of a Document is null for `load`, so an
  // <img> or <iframe> that finishes loading after the page did never reaches
  // a listener on `window`; and a non-composed event inside a shadow root
  // never leaves it.
  const DOCUMENT_TRIGGERS = ["load", "error"];
  const MUTATIONS = {
    subtree: true, childList: true, attributes: true, characterData: true,
  };

  const doc = (globalThis.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : String(Math.random()).slice(2) + String(Date.now());
  let epoch = 0;
  let scheduled = false;
  let nextId = 1;
  const ids = new WeakMap();
  const everPassword = new WeakSet();
  let tracked = [];
  let trackedSignature = "";

  function idOf(el) {
    let id = ids.get(el);
    if (id === undefined) {
      id = nextId++;
      ids.set(el, id);
    }
    return id;
  }
  // Called by the gate on an iframe element (DOM.resolveNode into this
  // world), to link a child frame to the box this frame reports for it.
  globalThis.__swarmLiveMaskOwnerId = idOf;

  function post(message) {
    message.v = 1;
    message.doc = doc;
    try {
      send(JSON.stringify(message));
    } catch (_) {
      // Nothing to do: an unsent report is a stale frame at the gate.
    }
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(measure);
  }

  function layout() {
    epoch += 1;
    post({ k: "layout", e: epoch });
    schedule();
  }

  function rect(r) {
    return [r.left, r.top, r.width, r.height];
  }

  function onScreen(r) {
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
      r.top < innerHeight && r.left < innerWidth;
  }

  function sensitive(el) {
    const type = String(el.type || "").toLowerCase();
    if (type === "password") everPassword.add(el);
    const tokens = String(el.getAttribute("autocomplete") || "").toLowerCase().split(/\s+/);
    return everPassword.has(el) || tokens.some((t) => SENSITIVE_AUTOCOMPLETE.has(t));
  }

  function measure() {
    scheduled = false;
    const blocks = new Map();
    let blockIds = 0;
    // Each field's value is its own block: two fields side by side are not
    // one string.
    let valueBlocks = 0;
    function blockOf(node) {
      let el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
      while (el) {
        const known = blocks.get(el);
        if (known !== undefined) return known;
        const display = getComputedStyle(el).display;
        if (!INLINE_DISPLAYS.has(display) || !el.parentElement) {
          const id = ++blockIds;
          blocks.set(el, id);
          sized.push(el);
          return id;
        }
        el = el.parentElement;
      }
      return 0;
    }

    // Every element whose box this report depends on. Its size changing
    // (an image loading above a paragraph, a :hover reveal growing a block)
    // moves what this report measured, so the ResizeObserver announces it.
    const sized = [];
    if (document.documentElement) sized.push(document.documentElement);
    if (document.body) sized.push(document.body);
    const fields = [];
    const texts = [];
    const owners = [];
    const nextTracked = [];
    let chars = 0;
    let visited = 0;
    let truncated = false;
    const range = document.createRange();

    function addText(s, block, r) {
      if (texts.length >= MAX_TEXT_ITEMS || chars + s.length > MAX_TEXT_CHARS) {
        truncated = true;
        return;
      }
      chars += s.length;
      texts.push({ s, b: block, r: rect(r) });
    }

    const roots = document.documentElement ? [document.documentElement] : [];
    while (roots.length && !truncated) {
      const walker = document.createTreeWalker(
        roots.pop(), NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      for (let node = walker.currentNode; node && !truncated; node = walker.nextNode()) {
        if (++visited > MAX_NODES_VISITED) {
          truncated = true;
          break;
        }
        if (node.nodeType === Node.TEXT_NODE) {
          if (!node.data.trim()) continue;
          range.selectNodeContents(node);
          const r = range.getBoundingClientRect();
          if (onScreen(r)) addText(node.data, blockOf(node), r);
          continue;
        }
        const el = node;
        if (el.shadowRoot) { // open roots only (§5.3.4)
          watchRoot(el.shadowRoot);
          roots.push(el.shadowRoot);
        }
        const tag = el.localName;
        if (REPLACED.has(tag)) sized.push(el);
        if (tag === "input") {
          const r = el.getBoundingClientRect();
          if (sensitive(el)) {
            nextTracked.push(el);
            fields.push({
              id: idOf(el),
              type: String(el.type || "").toLowerCase(),
              ac: String(el.getAttribute("autocomplete") || ""),
              ever: everPassword.has(el),
              r: rect(r),
            });
          } else if (VALUE_INPUT_TYPES.has(String(el.type || "").toLowerCase()) &&
                     el.value && onScreen(r)) {
            // A field's value is visible text too: an API key in a read-only
            // "copy" box, or what a person types into an ordinary field.
            addText(el.value, -(++valueBlocks), r);
          }
        } else if (tag === "textarea") {
          const r = el.getBoundingClientRect();
          if (el.value && onScreen(r)) addText(el.value, -(++valueBlocks), r);
        } else if (tag === "iframe" || tag === "frame") {
          const r = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          const left = r.left + el.clientLeft + parseFloat(style.paddingLeft || "0");
          const top = r.top + el.clientTop + parseFloat(style.paddingTop || "0");
          owners.push({
            id: idOf(el),
            r: [left, top, Math.max(0, el.clientWidth - parseFloat(style.paddingLeft || "0") -
                                     parseFloat(style.paddingRight || "0")),
                Math.max(0, el.clientHeight - parseFloat(style.paddingTop || "0") -
                            parseFloat(style.paddingBottom || "0"))],
          });
        }
      }
    }

    tracked = nextTracked;
    trackedSignature = signature();
    watchSizes(sized);
    const vv = globalThis.visualViewport;
    post({
      k: "report",
      e: epoch,
      // The visual viewport's size too: the mask sizes the page on screen as
      // that times the scale, which is the device's viewport even when a
      // phone preset shows the page zoomed out to fit (mask.py, QA12-01).
      vp: [innerWidth, innerHeight, vv ? vv.offsetLeft : 0, vv ? vv.offsetTop : 0,
           vv ? vv.scale : 1, vv ? vv.width : innerWidth, vv ? vv.height : innerHeight],
      f: fields,
      t: texts,
      o: owners,
      tr: truncated,
    });
  }

  // Where the sensitive fields are, and how far the page is scrolled. An
  // animation or a transition moves a field without a mutation or an event;
  // checking this every frame catches it.
  function signature() {
    const vv = globalThis.visualViewport;
    const parts = [scrollX, scrollY, innerWidth, innerHeight];
    if (vv) parts.push(vv.offsetLeft, vv.offsetTop, vv.scale);
    for (const el of tracked) {
      const r = el.getBoundingClientRect();
      parts.push(r.left, r.top, r.width, r.height, String(el.type));
    }
    return parts.join(",");
  }

  // A transition or animation moves text for its whole run with nothing
  // announced after the first event, and a transform fires no ResizeObserver.
  // While any is running, announce layout every frame so the gate never
  // trusts a report older than the frame (fail closed, design 5.3.2).
  function animating() {
    if (typeof document.getAnimations !== "function") return false;
    return document.getAnimations().some((a) => a.playState === "running");
  }

  function watch() {
    if (animating()) layout();
    else if (!scheduled && signature() !== trackedSignature) layout();
    requestAnimationFrame(watch);
  }

  const capture = { capture: true, passive: true };
  // A MutationObserver on `document` is not told about mutations inside a
  // shadow root, so each open root measure() walks gets its own observation.
  const mutations = new MutationObserver(layout);
  const watchedRoots = new WeakSet();
  function watchRoot(root) {
    if (watchedRoots.has(root)) return;
    watchedRoots.add(root);
    mutations.observe(root, MUTATIONS);
    for (const type of DOCUMENT_TRIGGERS) root.addEventListener(type, layout, capture);
  }

  // The size each element had when the last report measured it. A
  // ResizeObserver also delivers one notification per newly observed
  // element; comparing against the measured size keeps that one from
  // announcing a change that did not happen, without missing one that did.
  let measuredSizes = new WeakMap();
  function sizeOf(el) {
    const r = el.getBoundingClientRect();
    return r.width + "x" + r.height;
  }
  const resizes = new ResizeObserver((entries) => {
    for (const entry of entries) {
      if (measuredSizes.get(entry.target) !== sizeOf(entry.target)) {
        layout();
        return;
      }
    }
  });
  function watchSizes(elements) {
    // Re-observed from scratch each report, so an element no longer on the
    // page is not held.
    resizes.disconnect();
    measuredSizes = new WeakMap();
    for (const el of elements) {
      measuredSizes.set(el, sizeOf(el));
      resizes.observe(el);
    }
  }

  mutations.observe(document, MUTATIONS);
  for (const type of WINDOW_TRIGGERS) addEventListener(type, layout, capture);
  for (const type of DOCUMENT_TRIGGERS) document.addEventListener(type, layout, capture);
  if (globalThis.visualViewport) {
    visualViewport.addEventListener("resize", layout, { passive: true });
    visualViewport.addEventListener("scroll", layout, { passive: true });
  }
  if (document.fonts) document.fonts.addEventListener("loadingdone", layout);
  setInterval(schedule, HEARTBEAT_MS);

  layout();
  requestAnimationFrame(watch);
})();
