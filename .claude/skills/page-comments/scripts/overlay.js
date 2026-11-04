// The Comment button page-comments.py adds to pages. Comments are posted back to the server
// that served this script, which types them into the agent session.
(() => {
  if (window.__pageComments) return;
  window.__pageComments = true;
  const TOKEN = "__TOKEN__";
  const BASE = new URL(".", (document.currentScript && document.currentScript.src) || "__BASE__").href;
  const send = window.fetch.bind(window);

  function init() {
    const style = document.createElement("style");
    style.textContent = `
      .pc-ui, .pc-ui * { all: revert; box-sizing: border-box; font: 13px/1.4 -apple-system, system-ui, sans-serif; }
      .pc-btn { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; padding: 9px 14px; border: 0;
        border-radius: 999px; background: #1f6feb; color: #fff; font-weight: 600; cursor: pointer;
        box-shadow: 0 2px 10px rgba(0,0,0,.3); }
      .pc-btn.on { background: #cf222e; }
      .pc-hl { outline: 2px solid #1f6feb !important; outline-offset: 2px !important; cursor: crosshair !important; }
      .pc-box { position: fixed; z-index: 2147483647; width: 320px; padding: 10px; border-radius: 10px;
        background: #fff; color: #1f2328; border: 1px solid #d0d7de; box-shadow: 0 8px 24px rgba(0,0,0,.25); }
      .pc-box .pc-tgt { color: #59636e; font-size: 11px; margin: 0 0 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .pc-box textarea { display: block; width: 100%; height: 64px; padding: 6px; border: 1px solid #d0d7de; border-radius: 6px;
        resize: vertical; color: #1f2328; background: #fff; }
      .pc-box .pc-row { display: flex; gap: 6px; justify-content: flex-end; align-items: center; margin-top: 8px; }
      .pc-box .pc-hint { margin-right: auto; color: #59636e; font-size: 11px; }
      .pc-box button { padding: 5px 12px; border-radius: 6px; border: 1px solid #d0d7de; background: #f6f8fa; color: #1f2328; cursor: pointer; }
      .pc-box button.pc-send { background: #1f6feb; border-color: #1f6feb; color: #fff; }
      .pc-pin { position: absolute; z-index: 2147483646; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
        background: #1f6feb; color: #fff; font: 600 11px/18px system-ui; text-align: center; pointer-events: none; }
      .pc-toast { position: fixed; right: 16px; bottom: 60px; z-index: 2147483647; padding: 8px 12px; border-radius: 8px;
        background: #1f2328; color: #fff; }`;
    document.head.appendChild(style);

    const btn = document.createElement("button");
    btn.className = "pc-ui pc-btn";
    btn.textContent = "Comment";
    btn.title = "Faster: ⌥-click anything, or press C";
    document.body.appendChild(btn);

    let on = false, hovered = null, lastTarget = null, box = null, count = 0;
    const ours = (el) => el && el.closest && el.closest(".pc-ui");
    const typing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
    const unhover = () => { if (hovered) hovered.classList.remove("pc-hl"); hovered = null; };
    const hover = (el) => {
      if (!el || !el.classList || ours(el) || el === hovered) return;
      unhover();
      hovered = el;
      hovered.classList.add("pc-hl");
    };
    const setOn = (v) => {
      on = v;
      btn.classList.toggle("on", v);
      btn.textContent = v ? "Pick an element… (Esc)" : "Comment";
      if (!v) unhover();
    };
    btn.addEventListener("click", (e) => { e.stopPropagation(); closeBox(); setOn(!on); });

    const toast = (text, ms = 2500) => {
      const t = document.createElement("div");
      t.className = "pc-ui pc-toast";
      t.textContent = text;
      document.body.appendChild(t);
      setTimeout(() => t.remove(), ms);
    };

    const selectorOf = (el) => {
      if (el.id) return "#" + CSS.escape(el.id);
      const parts = [];
      for (let n = el; n && n.nodeType === 1 && n !== document.body && parts.length < 4; n = n.parentElement) {
        if (n.id) { parts.unshift("#" + CSS.escape(n.id)); break; }
        let s = n.tagName.toLowerCase();
        const cls = [...n.classList].filter((c) => !c.startsWith("pc-")).slice(0, 2);
        if (cls.length) s += "." + cls.map((c) => CSS.escape(c)).join(".");
        const sib = n.parentElement ? [...n.parentElement.children].filter((c) => c.tagName === n.tagName) : [];
        if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(n) + 1})`;
        parts.unshift(s);
      }
      return parts.join(" > ");
    };
    const snippetOf = (el) =>
      (el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("alt") || el.getAttribute("title") || "")
        .replace(/\s+/g, " ").trim().slice(0, 120);

    function closeBox() { if (box) box.remove(); box = null; }

    function openBox(el) {
      closeBox();
      const r = el.getBoundingClientRect();
      box = document.createElement("div");
      box.className = "pc-ui pc-box";
      const tgt = document.createElement("div");
      tgt.className = "pc-tgt";
      const snip = snippetOf(el);
      tgt.textContent = "On " + el.tagName.toLowerCase() + (snip ? ' "' + snip + '"' : "");
      const ta = document.createElement("textarea");
      ta.placeholder = "What should Claude change or look at?";
      const row = document.createElement("div");
      row.className = "pc-row";
      const hint = document.createElement("span");
      hint.className = "pc-hint";
      hint.textContent = "↩ send · Esc cancel";
      const cancel = document.createElement("button");
      cancel.textContent = "Cancel";
      const go = document.createElement("button");
      go.className = "pc-send";
      go.textContent = "Send to Claude";
      row.append(hint, cancel, go);
      box.append(tgt, ta, row);
      document.body.appendChild(box);
      const top = Math.min(r.bottom + 8, window.innerHeight - box.offsetHeight - 8);
      box.style.top = Math.max(8, top) + "px";
      box.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 336)) + "px";
      ta.focus();
      cancel.onclick = closeBox;
      const submit = async () => {
        const text = ta.value.trim();
        if (!text) return ta.focus();
        go.disabled = true;
        go.textContent = "Sending…";
        try {
          const res = await send(BASE + "comment", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Comment-Token": TOKEN },
            body: JSON.stringify({ text, url: location.href, selector: selectorOf(el), tag: el.tagName.toLowerCase(), snippet: snip }),
          });
          if (!res.ok) throw new Error((await res.text()) || res.status);
          closeBox();
          const pin = document.createElement("div");
          pin.className = "pc-ui pc-pin";
          pin.textContent = String(++count);
          const rr = el.getBoundingClientRect();
          pin.style.left = rr.left + window.scrollX - 6 + "px";
          pin.style.top = rr.top + window.scrollY - 6 + "px";
          document.body.appendChild(pin);
          toast("Sent to Claude");
        } catch (err) {
          go.disabled = false;
          go.textContent = "Send to Claude";
          toast("Couldn't send: " + err.message);
        }
      };
      go.onclick = submit;
      ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
        if (e.key === "Escape") closeBox();
      });
    }

    // The Comment button or C turns pick mode on; holding ⌥ picks without it.
    document.addEventListener("mousemove", (e) => {
      lastTarget = e.target;
      if (on || e.altKey) hover(e.target);
      else if (hovered) unhover();
    }, true);
    document.addEventListener("click", (e) => {
      if (!(on || e.altKey) || ours(e.target)) return;
      e.preventDefault();
      e.stopPropagation();
      const el = e.target;
      setOn(false);
      unhover();
      openBox(el);
    }, true);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Alt" && !box) hover(lastTarget);
      if (e.key === "Escape" && on) setOn(false);
      if ((e.key === "c" || e.key === "C") && !e.metaKey && !e.ctrlKey && !e.altKey && !box && !typing(e.target)) {
        e.preventDefault();
        setOn(!on);
      }
    }, true);
    document.addEventListener("keyup", (e) => { if (e.key === "Alt" && !on) unhover(); }, true);
    window.addEventListener("blur", () => { if (!on) unhover(); });

    let tipShown = false;
    try { tipShown = !!sessionStorage.getItem("pc-tip"); sessionStorage.setItem("pc-tip", "1"); } catch (_) {}
    if (!tipShown) toast("Comment: ⌥-click anything, or press C", 4000);
  }

  // Wait for the page to finish loading so the button doesn't disturb a framework's hydration.
  if (document.readyState === "complete") setTimeout(init, 300);
  else window.addEventListener("load", () => setTimeout(init, 300));
})();
