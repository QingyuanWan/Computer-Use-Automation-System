"""PANEL_JS_SOURCE — the in-browser takeover overlay (ADR-007), injected via page.add_init_script.

An idempotent IIFE: guards on `window.__ifaiEscalation` (never installs twice) and on the bar element id
(never duplicates DOM), builds a right-edge collapsed bar (40px) + a slide-in panel (320px), and exposes
`window.__ifaiEscalation.{escalate,setState,expand,collapse}` for the Python handler to drive via
page.evaluate. Buttons call `window.resumeAutomation(outcome, note)` — the page.expose_binding bridge to
Python. Kept as a plain string (no template substitution) so the JS is verbatim.
"""
from __future__ import annotations

PANEL_JS_SOURCE = r"""
(() => {
  if (window.__ifaiEscalation) return;                 // idempotent: never install the API twice
  const BAR_ID = 'interfaceai-escalation-bar';
  const PANEL_ID = 'interfaceai-escalation-panel';

  function build() {
    if (!document.body) return;
    if (document.getElementById(BAR_ID)) return;        // DOM-level guard: never duplicate the panel
    const style = document.createElement('style');
    style.textContent = `
      #${BAR_ID}{position:fixed;top:0;right:0;width:40px;height:100vh;z-index:2147483647;
        background:#1b7f3b;display:flex;align-items:center;justify-content:center;cursor:pointer;
        color:#fff;font:16px sans-serif;transition:background .3s}
      #${BAR_ID}.paused{background:#e08a1e;animation:ifai-pulse 1s infinite}
      #${BAR_ID}.blocked{background:#c0392b}
      @keyframes ifai-pulse{0%{opacity:1}50%{opacity:.45}100%{opacity:1}}
      #${PANEL_ID}{position:fixed;top:0;right:-320px;width:320px;height:100vh;z-index:2147483647;
        background:#fff;box-shadow:-2px 0 8px rgba(0,0,0,.25);transition:right .3s ease;
        box-sizing:border-box;padding:16px;color:#222;font:13px/1.5 sans-serif}
      #${PANEL_ID}.open{right:40px}
      #${PANEL_ID} .ifai-row{margin:6px 0;word-break:break-all}
      #${PANEL_ID} button{display:block;width:100%;margin:6px 0;padding:8px;cursor:pointer;
        border:1px solid #ccc;border-radius:4px;background:#f5f5f5}
      #${PANEL_ID} button:disabled{opacity:.4;cursor:not-allowed}
      #${PANEL_ID} textarea{width:100%;margin-top:8px;box-sizing:border-box}
      #${PANEL_ID} #ifai-prompt{margin:8px 0;padding:8px;background:#fff7e6;border:1px solid #e0b566;
        border-radius:4px;font-weight:bold;white-space:pre-wrap}
      #${PANEL_ID}.planned .ifai-reactive{display:none}
      #${PANEL_ID}:not(.planned) .ifai-planned{display:none}`;
    document.head.appendChild(style);

    const bar = document.createElement('div');
    bar.id = BAR_ID; bar.textContent = '⚙'; bar.title = 'interface.ai automation';
    bar.addEventListener('click', () => toggle());

    const panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.innerHTML =
      '<div class="ifai-row"><b>interface.ai — automation</b></div>' +
      '<div class="ifai-row" id="ifai-cap"></div>' +
      '<div class="ifai-row" id="ifai-step"></div>' +
      '<div class="ifai-row" id="ifai-reason"></div>' +
      '<div class="ifai-row" id="ifai-hint" style="display:none"></div>' +
      '<div class="ifai-row" id="ifai-url"></div>';
    // planned-mode prompt (shown verbatim to the human)
    const prompt = document.createElement('div');
    prompt.id = 'ifai-prompt'; prompt.className = 'ifai-planned';
    panel.appendChild(prompt);
    const mk = (label, outcome, cls) => {
      const b = document.createElement('button');
      b.textContent = label; b.dataset.outcome = outcome; b.className = cls;
      b.addEventListener('click', () => resolve(outcome));
      return b;
    };
    panel.appendChild(mk('Resume', 'resume', 'ifai-reactive'));
    panel.appendChild(mk('Take over & resume', 'takeover_resume', 'ifai-reactive'));
    panel.appendChild(mk('Abort', 'abort', 'ifai-reactive'));
    panel.appendChild(mk('Done', 'planned_done', 'ifai-planned'));   // planned mode: single button
    const note = document.createElement('textarea');
    note.id = 'ifai-note'; note.rows = 3; note.placeholder = 'What did you do? (optional)';
    panel.appendChild(note);

    document.body.appendChild(bar);
    document.body.appendChild(panel);
    setButtons(false);
    applyState('running');
  }

  function setButtons(enabled) {
    document.querySelectorAll('#' + PANEL_ID + ' button').forEach(b => { b.disabled = !enabled; });
  }
  function toggle(force) {
    const p = document.getElementById(PANEL_ID); if (!p) return;
    const open = (force === undefined) ? !p.classList.contains('open') : force;
    p.classList.toggle('open', open);
  }
  function applyState(s) {
    const bar = document.getElementById(BAR_ID); if (!bar) return;
    bar.classList.remove('paused', 'blocked');
    if (s === 'paused') bar.classList.add('paused');
    else if (s === 'blocked') bar.classList.add('blocked');
  }
  function resolve(outcome) {
    const t = document.getElementById('ifai-note');
    const note = t ? t.value : '';
    setButtons(false);
    applyState(outcome === 'abort' ? 'blocked' : 'running');
    toggle(false);
    if (window.resumeAutomation) window.resumeAutomation(outcome, note);   // -> Python (expose_binding)
  }
  function setText(id, txt) { const e = document.getElementById(id); if (e) e.textContent = txt; }
  function setHint(hint) {                 // D3-α: "About this step:" row; hidden entirely when no hint
    const e = document.getElementById('ifai-hint'); if (!e) return;
    if (hint) { e.textContent = 'About this step: ' + hint; e.style.display = 'block'; }
    else { e.textContent = ''; e.style.display = 'none'; }
  }

  function setMode(planned) {
    const p = document.getElementById(PANEL_ID); if (p) p.classList.toggle('planned', !!planned);
  }

  window.__ifaiEscalation = {
    escalate(ctx) {                       // REACTIVE mode: three buttons
      ctx = ctx || {};
      setMode(false);
      setText('ifai-cap', 'Capability: ' + (ctx.capability || '—'));
      setText('ifai-step', 'Step: ' + (ctx.step || '—'));
      setText('ifai-reason', 'Reason: ' + (ctx.reason || '—'));
      setHint(ctx.hint);
      setText('ifai-url', 'URL: ' + (ctx.url || '—'));
      applyState('paused');
      setButtons(true);
      toggle(true);                       // auto-expand on escalation trigger
    },
    escalatePlanned(ctx) {                // PLANNED mode: prompt + single Done button
      ctx = ctx || {};
      setMode(true);
      setText('ifai-cap', 'Capability: ' + (ctx.capability || '—'));
      setText('ifai-step', '');
      setText('ifai-reason', 'Reason: ' + (ctx.reason || '—'));
      setHint(ctx.hint);
      setText('ifai-url', '');
      setText('ifai-prompt', ctx.prompt || 'Human input needed.');
      applyState('paused');
      setButtons(true);
      toggle(true);
    },
    setState(s) { applyState(s); if (s !== 'paused') setButtons(false); },
    expand() { toggle(true); },
    collapse() { toggle(false); },
  };

  if (document.body) build();
  else document.addEventListener('DOMContentLoaded', build);
})();
"""
