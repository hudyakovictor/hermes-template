/* ============================================================
   core.js — каркас: Telegram SDK, утилиты, тосты, шторки,
   hold-to-confirm, свайп-строки, pull-to-refresh, роутер табов
   ============================================================ */
"use strict";

/* ---------- DOM утилиты ---------- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function icon(name, cls = "") { return `<svg class="${cls}"><use href="#i-${name}"/></svg>`; }
function tween(from, to, dur, fn) {
  const t0 = performance.now();
  const step = t => {
    const k = Math.min(1, (t - t0) / dur);
    fn(from + (to - from) * (1 - Math.pow(1 - k, 3)));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
const nf = (v, d = 0) => Number(v).toFixed(d).replace(".", ",");

/* ---------- Telegram ---------- */
const TG = (() => {
  const wa = window.Telegram && window.Telegram.WebApp;
  const ok = !!wa && !!wa.initDataUnsafe;
  const api = {
    wa, ok,
    ready() { try { wa && wa.ready(); wa && wa.expand(); } catch (e) {} },
    user() { try { return (wa && wa.initDataUnsafe && wa.initDataUnsafe.user) || null; } catch (e) { return null; } },
    haptic(kind = "light") {
      try {
        if (!wa || !wa.HapticFeedback) return;
        if (kind === "sel") wa.HapticFeedback.selectionChanged();
        else if (kind === "err" || kind === "ok" || kind === "warn") wa.HapticFeedback.notificationOccurred(kind);
        else wa.HapticFeedback.impactOccurred(kind);
      } catch (e) {}
    },
    close() { try { wa && wa.close(); } catch (e) {} },
    vibrate(ms = 12) { try { navigator.vibrate && navigator.vibrate(ms); } catch (e) {} },
    theme() {
      if (!ok || !wa.themeParams) return null;
      return { scheme: wa.colorScheme, params: wa.themeParams };
    },
    backButton(cb) {
      try {
        if (!ok) return;
        wa.onEvent("backButtonClicked", cb);
        wa.BackButton.show();
      } catch (e) {}
    },
    backButtonOff() { try { ok && wa.BackButton.hide(); } catch (e) {} },
    mainButton(text, cb, opts = {}) {
      try {
        if (!ok || !wa.MainButton) return false;
        const b = wa.MainButton;
        b.setText(text); b.onClick(cb);
        b.setParams({ color: opts.color || "#2ea6ff", text_color: "#ffffff", is_active: true, is_visible: true });
        return true;
      } catch (e) { return false; }
    },
    mainButtonOff() { try { ok && wa.MainButton.hide(); } catch (e) {} },
  };
  return api;
})();

/* ---------- Тосты ---------- */
const Toast = (() => {
  const root = () => $("#toast-root");
  function show(text, type = "info", ms = 3200) {
    const ic = { ok: "check", warn: "alert", err: "x", info: "bolt" }[type] || "bolt";
    const t = el(`<div class="toast ${type}">${icon(ic)}<span>${text}</span></div>`);
    root().appendChild(t);
    TG.haptic(type === "err" ? "err" : type === "ok" ? "ok" : "light");
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 320); }, ms);
    return t;
  }
  return { show };
})();

/* ---------- Шторки ---------- */
const Sheet = (() => {
  const root = $("#sheet-root");
  let stack = [];

  function open(html, { onOpen, onClose, full = false } = {}) {
    const backdrop = el(`<div class="backdrop"></div>`);
    const sheet = el(`<div class="sheet" role="dialog" aria-modal="true"${full ? ' style="height:92%"' : ""}>
      <div class="sheet-grab"><i></i></div>${html}</div>`);
    root.classList.add("open");
    root.appendChild(backdrop); root.appendChild(sheet);
    requestAnimationFrame(() => { backdrop.classList.add("on"); sheet.classList.add("on"); });
    const item = { backdrop, sheet, onClose, dead: false };

    const close = (viaTg) => {
      if (item.dead) return; item.dead = true;
      backdrop.classList.remove("on"); sheet.classList.remove("on");
      TG.haptic("light");
      setTimeout(() => { backdrop.remove(); sheet.remove(); if (!root.querySelector(".sheet")) root.classList.remove("open"); }, 360);
      stack = stack.filter(x => x !== item);
      TG.backButtonOff();
      if (stack.length) TG.backButton(() => closeTop());
      if (onClose) onClose();
    };
    item.close = close;
    backdrop.addEventListener("click", () => close());
    TG.backButton(() => closeTop());

    // перетаскивание за «ручку»
    const grab = sheet.querySelector(".sheet-grab");
    let sy = null, dy = 0;
    const start = y => { sy = y; sheet.style.transition = "none"; };
    const move = y => { if (sy == null) return; dy = Math.max(0, y - sy); sheet.style.transform = `translateY(${dy}px)`; };
    const end = () => {
      if (sy == null) return;
      sheet.style.transition = ""; sheet.style.transform = "";
      if (dy > 110) close();
      sy = null; dy = 0;
    };
    grab.addEventListener("touchstart", e => start(e.touches[0].clientY), { passive: true });
    grab.addEventListener("touchmove", e => move(e.touches[0].clientY), { passive: true });
    grab.addEventListener("touchend", end);
    grab.addEventListener("mousedown", e => start(e.clientY));
    window.addEventListener("mousemove", e => move(e.clientY));
    window.addEventListener("mouseup", end);

    stack.push(item);
    TG.haptic("light");
    if (onOpen) onOpen(sheet);
    return item;
  }
  function closeTop() { const t = stack[stack.length - 1]; if (t) t.close(true); }
  return { open, closeTop, get count() { return stack.length; } };
})();

/* ---------- Hold-to-confirm (защита критичных действий) ---------- */
function holdable(btn, onConfirm, ms = 1100) {
  let t0 = null, raf = null, done = false;
  const fill = el(`<span class="hold-fill"></span>`);
  if (!btn.querySelector(".hold-fill")) btn.prepend(fill);
  const stop = (fire) => {
    if (raf) cancelAnimationFrame(raf);
    raf = null; t0 = null;
    fill.style.width = "0%";
    btn.classList.remove("done");
    if (fire && !done) { done = true; setTimeout(() => { done = false; }, 800); onConfirm(); }
  };
  const tickHold = () => {
    if (t0 == null) return;
    const k = Math.min(1, (performance.now() - t0) / ms);
    fill.style.width = k * 100 + "%";
    if (k >= 1) { stop(true); return; }
    raf = requestAnimationFrame(tickHold);
  };
  const down = e => { e.preventDefault(); t0 = performance.now(); TG.vibrate(8); tickHold(); };
  btn.addEventListener("pointerdown", down);
  btn.addEventListener("pointerup", () => stop(false));
  btn.addEventListener("pointerleave", () => stop(false));
  btn.addEventListener("pointercancel", () => stop(false));
  btn.addEventListener("contextmenu", e => e.preventDefault());
}

/* ---------- Свайп-строка (действия под карточкой) ---------- */
function swipeRow(fgHtml, actions, { onTap } = {}) {
  const wrap = el(`<div class="swipe-wrap"></div>`);
  const bg = el(`<div class="swipe-bg">${actions.map(a => `<button class="sw-act" data-sw="${a.id}" aria-label="${a.label}">${icon(a.icon)}<span>${a.label}</span></button>`).join("")}</div>`);
  const fg = el(`<div class="swipe-fg">${fgHtml}</div>`);
  wrap.appendChild(bg); wrap.appendChild(fg);
  let sx = null, x = 0, openState = false, lock = false;
  const W = 92;
  const setX = v => { x = v; fg.style.transform = `translateX(${v}px)`; };
  fg.addEventListener("touchstart", e => { if (e.touches.length > 1) return; sx = e.touches[0].clientX - x; lock = false; fg.style.transition = "none"; }, { passive: true });
  fg.addEventListener("touchmove", e => {
    if (sx == null) return;
    let nx = e.touches[0].clientX - sx;
    if (!lock && Math.abs(nx) > 8) lock = true;
    if (lock) { setX(Math.max(-W, Math.min(0, nx))); e.preventDefault(); }
  }, { passive: false });
  fg.addEventListener("touchend", () => {
    if (sx == null) return;
    fg.style.transition = "transform .25s cubic-bezier(.22,1.1,.36,1)";
    const wasOpen = openState;
    openState = x < -W / 2;
    setX(openState ? -W : 0);
    if (openState && !wasOpen) TG.haptic("light");
    if (!lock && !wasOpen && onTap) onTap();
    sx = null;
  });
  fg.addEventListener("click", e => {
    if (openState) { openState = false; fg.style.transition = ""; setX(0); return; }
    if (onTap) onTap(e);
  });
  bg.addEventListener("click", e => {
    const b = e.target.closest("[data-sw]"); if (!b) return;
    const a = actions.find(x => x.id === b.dataset.sw);
    openState = false; fg.style.transition = ""; setX(0);
    if (a) a.fn();
  });
  wrap._fg = fg;
  return wrap;
}

/* ---------- Pull-to-refresh ---------- */
function attachPTR(scrollEl, onRefresh) {
  const spin = el(`<div class="ptr-spin">${icon("refresh")}</div>`);
  scrollEl.parentElement.style.position = "relative";
  scrollEl.parentElement.appendChild(spin);
  let sy = null, pulling = false, fired = false;
  const TH = 64;
  scrollEl.addEventListener("touchstart", e => {
    if (scrollEl.scrollTop <= 0) { sy = e.touches[0].clientY; pulling = true; fired = false; }
  }, { passive: true });
  scrollEl.addEventListener("touchmove", e => {
    if (!pulling || sy == null) return;
    const d = e.touches[0].clientY - sy;
    if (d <= 0 || scrollEl.scrollTop > 0) { spin.classList.remove("on"); return; }
    const k = Math.min(1, d / (TH * 1.6));
    spin.classList.add("on");
    spin.style.translate = `-50% ${Math.min(46, d * .4)}px`;
    spin.firstChild.style.rotate = d * 1.2 + "deg";
    if (k >= 1 && !fired) fired = true;
  }, { passive: true });
  scrollEl.addEventListener("touchend", () => {
    if (pulling && fired) {
      spin.classList.add("spin");
      TG.haptic("medium");
      Promise.resolve(onRefresh()).finally(() => {
        setTimeout(() => { spin.classList.remove("spin", "on"); spin.style.translate = "-50% 0"; }, 500);
      });
    } else { spin.classList.remove("on"); spin.style.translate = "-50% 0"; }
    pulling = false; sy = null;
  });
}

/* ---------- Роутер табов ---------- */
const Tabs = (() => {
  const order = ["dash", "pipe", "tele", "crew", "verd"];
  let cur = "dash", prev = "dash";
  const TITLES = {
    dash: ["Пульт", ""], pipe: ["Конвейер", ""], tele: ["Телеметрия", ""],
    crew: ["Экипаж", ""], verd: ["Итоги", ""],
  };
  function go(name) {
    if (!order.includes(name) || name === cur) return;
    prev = cur; cur = name;
    $$(".tab").forEach(t => t.classList.toggle("on", t.dataset.tab === name));
    $$(".screen").forEach(s => {
      const isCur = s.id === "s-" + name;
      s.classList.toggle("on", isCur);
      s.classList.toggle("left", order.indexOf(name) > order.indexOf(s.id.slice(2)) && !isCur);
    });
    TG.haptic("sel");
    const [t, sub] = TITLES[name];
    $("#tb-title").textContent = t;
    if (window.Screens && Screens.onTab) Screens.onTab(name);
  }
  function init() {
    $$(".tab").forEach(b => b.addEventListener("click", () => go(b.dataset.tab)));
  }
  return { go, init, get cur() { return cur; }, TITLES };
})();

/* ---------- Общие UI-компоненты ---------- */
const UI = {
  statusChip(st) {
    const map = {
      running: ["st-run", "считает"], queued: ["st-mut", "в очереди"], paused_checkpoint: ["st-warn", "чекпойнт"],
      confirmed: ["st-ok", "подтверждена"], rejected: ["st-bad", "отвергнута"], killed: ["st-mut", "убита"], partial: ["st-warn", "частично"],
    };
    const [cls, label] = map[st] || ["st-mut", st];
    return `<span class="st ${cls}"><i class="dot"></i>${label}</span>`;
  },
  lvBadge(lv) { return `<span class="lv lv${lv[1]}">${lv}</span>`; },
  binBadge(b) { return `<span class="bin b-${b}">${b}</span>`; },
  corridorHTML(c, opts = {}) {
    const range = c.max - c.min;
    const pct = v => clamp((v - c.min) / range * 100, 0, 100);
    const unit = c.unit === "%" ? "%" : " " + c.unit;
    let pt = "";
    if (opts.actual != null) {
      const inC = opts.actual >= c.lo && opts.actual <= c.hi;
      const color = c.unit === "r" ? (inC ? "var(--ok)" : "var(--danger)") : (inC ? "var(--ok)" : "var(--danger)");
      pt = `<div class="pt" style="left:${pct(opts.actual)}%;background:${color};color:#04141f">${opts.actual}</div>`;
    } else if (opts.forecast) {
      pt = `<div class="pt" style="left:${pct(c.point)}%;background:var(--accent);color:#04202f">${c.point}</div>`;
    }
    return `<div class="corridor">
      <div class="track"></div>
      <div class="zone" style="left:${pct(c.lo)}%;width:${pct(c.hi) - pct(c.lo)}%"></div>
      ${pt}
      <div class="lbl" style="left:0">${c.min}${unit}</div>
      <div class="lbl" style="right:0">${c.max}${unit}</div>
    </div>
    <div class="between tiny muted" style="padding:0 2px">
      <span class="mono">коридор: <b class="mono" style="color:var(--ok)">${c.lo}…${c.hi}${unit}</b></span>
      <span>${esc(c.metric)}</span>
    </div>`;
  },
  piBreakdown(h) {
    const rows = [["S", "сигналы", h.s], ["N", "gap", h.n], ["E", "ранность", h.e], ["Q", "стандарт", h.q], ["M", "деньги", h.m], ["D", "PASS/FAIL", h.d]];
    const w = { S: .22, N: .16, E: .12, Q: .14, M: .14, D: .22 };
    return rows.map(([k, label, v]) => `
      <div class="pi-row"><b>${k}</b><span>${label}<i class="tiny" style="color:var(--faint)"> ·${w[k]}</i></span>
      <div class="meter thin"><i style="width:${Math.round(v * 100)}%"></i></div><i class="mono">${v.toFixed(2)}</i></div>`).join("")
      + `<div class="pi-row" style="margin-top:4px"><b></b><span>aging</span><div class="meter thin violet"><i style="width:${Math.round((h.aging / .3) * 100)}%"></i></div><i class="mono">+${h.aging.toFixed(2)}</i></div>
         <div class="between" style="margin-top:8px"><span class="caps">PI</span><b class="mono" style="font-size:15px">${h.pi.toFixed(3)}</b></div>
         <div class="between"><span class="caps">PPI = PI / ${nf(h.hours, 1)} ч</span><b class="mono" style="font-size:15px;color:var(--accent)">${h.ppi.toFixed(2)}</b></div>`;
  },
  checksHTML(checks) {
    return KILL_CHECKS.map((c, i) => {
      const st = checks[i] || "ok";
      const cls = st === "ok" ? "ok" : st === "wait" ? "wait" : "no";
      const ic = st === "ok" ? "check" : st === "wait" ? "clock" : "x";
      return `<div class="check ${cls}"><span class="cb ${cls}">${icon(ic)}</span><p>${c}</p></div>`;
    }).join("");
  },
  avatar(agentId, size = 34) {
    const a = AGENTS[agentId];
    if (!a) return "";
    const init = a.name[0] === "i" ? "iВ" : a.name[0];
    return `<div class="ava" style="width:${size}px;height:${size}px;background:${a.color}22;color:${a.color};border:1px solid ${a.color}44">${init}</div>`;
  },
  avatarMini(agentId) {
    const a = AGENTS[agentId] || { color: "#93a1c4", name: "?" };
    return `<span title="${esc(a.name)}" style="display:inline-flex;width:20px;height:20px;border-radius:7px;background:${a.color}25;color:${a.color};font-size:9px;font-weight:800;align-items:center;justify-content:center;font-family:var(--mono)">${a.name[0]}</span>`;
  },
  agentOf(id) { return AGENTS[id] || { id, name: id, zone: "", color: "#93a1c4" }; },
};

window.$ = $; window.$$ = $$; window.el = el; window.esc = esc; window.icon = icon;
window.TG = TG; window.Toast = Toast; window.Sheet = Sheet; window.holdable = holdable;
window.swipeRow = swipeRow; window.attachPTR = attachPTR; window.Tabs = Tabs; window.UI = UI; window.nf = nf; window.tween = tween;
