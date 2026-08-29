/* ============================================================================
   researchagen Mini App — логика интерфейса.
   Экраны: Пульт · Конвейер · Графики · Экипаж · Вердикты + визард идеи.
   Работает и внутри Telegram (WebApp SDK, хаптика, BackButton, MainButton),
   и в обычном браузере (демо-режим с симуляцией лаборатории).
   ========================================================================== */
"use strict";

/* --------------------------------------------------------------- окружение */
const tg = (window.Telegram && window.Telegram.WebApp) || null;
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const S = {
  data: null, route: "dash",
  sub: { pipe: "queue", crew: "chat" },
  metric: "loss", logY: false,
  compare: new Set(["cur"]),
  verdictFilter: "all",
  sheet: null, wizard: null,
  offline: false, tick: 0, lastFetchOk: 0,
  scrubText: null, chatStick: true,
};

/* --------------------------------------------------------------- словари */
const AGENT_COLOR = { shef: "#5b8cff", skif: "#35d07f", krot: "#38c8e8", morg: "#ff5c6c",
  gayka: "#f5a623", hronik: "#8f7bff", stazhor: "#8d9cb3", human: "#e9eff8" };
const STATUS_RU = { queued: "в очереди", running: "на GPU", blocked: "блокировка",
  paused_checkpoint: "пауза-чекпойнт", confirmed: "подтверждена", partial: "частично",
  rejected: "опровергнута", killed: "снята", archived: "архив" };
const KIND_RU = { confirmed: "Подтверждено", partial: "Частично", rejected: "Опровергнуто", killed: "Снято" };
const CHECK_RU = { pass: "✓", fail: "✕", run: "↻", wait: "•" };
/* SVG-иконки: одна оптическая масса (24×24, currentColor), рендер 16–18px */
const ICO = {
  pause: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1.3"/><rect x="14" y="5" width="4" height="14" rx="1.3"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5z"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.2"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V6M6 12l6-6 6 6"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3.5 2.5 20h19L12 3.5z"/><path d="M12 10v4.5" stroke-linecap="round"/><circle cx="12" cy="17.3" r="1" fill="currentColor" stroke="none"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5" stroke-linecap="round"/><circle cx="12" cy="7.6" r="1" fill="currentColor" stroke="none"/></svg>',
};
const BIN_COLOR = { P1: "ok", P2: "acc", P3: "warn", P4: "err" };
const ICOS = {
  bolt: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M13.2 2 4.5 13.6h6L9.4 22l9.1-11.6h-6.2z"/></svg>',
  chip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/><rect x="10" y="10" width="4" height="4" rx="1"/><path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3" stroke-linecap="round"/></svg>',
  battery: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="8" width="15.5" height="8.5" rx="2.5"/><path d="M21.2 11v2.6" stroke-linecap="round"/><path d="M6.5 10.7v3.4M10 10.7v3.4" stroke-width="2.2" stroke-linecap="round"/></svg>',
  target: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.6"/><circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/></svg>',
  flask: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M9.5 3h5M10.5 3v5.2L5.4 17a2.6 2.6 0 0 0 2.2 4h8.8a2.6 2.6 0 0 0 2.2-4l-5.1-8.8V3"/><path d="M7.4 14.5h9.2" stroke-linecap="round"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M12 4a5.5 5.5 0 0 0-5.5 5.5c0 4-1.5 5.5-1.5 5.5h14s-1.5-1.5-1.5-5.5A5.5 5.5 0 0 0 12 4z"/><path d="M10.2 18.5a2 2 0 0 0 3.6 0" stroke-linecap="round"/></svg>',
  wave: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 13h3l2.5-6 4 10 3-7 1.8 3H21"/></svg>',
};
const SRC_RU = { dr: "экипаж", telegram: "человек", human: "человек", miniapp: "человек" };

/* --------------------------------------------------------------- утилиты */
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (x, d = 2) => (x == null || isNaN(+x)) ? "—" : (+x).toFixed(d).replace(/\.?0+$/, "");
const fmtPct = (x, d = 1) => x == null ? "—" : (x > 0 ? "+" : "") + (+x).toFixed(d).replace(/\.0$/, "") + "%";
const fmtMin = (m) => m == null ? "—" : m >= 90 ? (m / 60).toFixed(m % 60 >= 6 ? 1 : 0).replace(/\.0$/, "") + " ч" : Math.round(m) + " мин";
const timeHM = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); };
const agoTxt = (iso) => {
  const d = new Date(iso); if (isNaN(d)) return "";
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return "только что";
  if (s < 3600) return Math.round(s / 60) + " мин назад";
  if (s < 86400) return Math.round(s / 3600) + " ч назад";
  return Math.round(s / 86400) + " дн назад";
};
const initials = (name) => (name || "?").replace(/[^A-Za-zА-Яа-я0-9]/g, "").slice(0, 2).toUpperCase() || "?";

function haptic(kind) {
  if (!tg || !tg.HapticFeedback) return;
  try {
    if (kind === "select") tg.HapticFeedback.selectionChanged();
    else if (kind === "ok") tg.HapticFeedback.notificationOccurred("success");
    else if (kind === "err") tg.HapticFeedback.notificationOccurred("error");
    else if (kind === "warn") tg.HapticFeedback.notificationOccurred("warning");
    else tg.HapticFeedback.impactOccurred("light");
  } catch (e) { /* хаптика недоступна — не критично */ }
}

function toast(text, kind) {
  const root = $("#toast-root");
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  const ico = kind === "ok" ? ICO.check : kind === "err" ? ICO.x : ICO.info;
  el.innerHTML = `<span class="t-ico ${kind || ""}">${ico}</span><span>${esc(text)}</span>`;
  root.appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .3s, transform .3s"; el.style.opacity = "0"; el.style.transform = "translateY(6px)"; }, 2600);
  setTimeout(() => el.remove(), 3000);
}

async function api(body) {
  try {
    const r = await fetch("/api/action", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!j.ok && j.err) toast(j.err, "err");
    return j;
  } catch (e) { toast("Нет связи с лабораторией", "err"); return { ok: false }; }
}

async function refresh() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    const j = await r.json();
    S.data = j; S.offline = false; S.lastFetchOk = Date.now();
    try { localStorage.setItem("rg_seenChat", String(seenChatCount())); } catch (e) {}
  } catch (e) {
    S.offline = true;
  }
  renderHeader();
  if (!S.sheet) {
    const k = renderKey();
    if (k !== S._key) { S._key = k; renderScreen(); }
  }
}

/* Diff-ключ: перерисовываем экран только когда данные реально изменились —
   иначе каждые 4 с перерендер сбивал бы тапы и скролл. */
function renderKey() {
  const d = S.data;
  if (!d) return "empty";
  const c = d.current;
  return JSON.stringify([
    S.route, S.sub, S.metric, S.logY, [...S.compare].sort(), S.verdictFilter, S.offline,
    d.mode, d.gov.autostart,
    (d.gov.budget_hours.used || 0).toFixed(1),
    d.gpu.available, Math.round((d.gpu.util || 0) / 5), Math.round((d.gpu.temp || 0) / 2),
    (d.gpu.used_gb || 0).toFixed(1),
    c && c.hid, c && c.elapsed_min && Math.round(c.elapsed_min),
    d.approvals.length, d.crew.chat.length, d.crew.chat.length ? d.crew.chat.at(-1).ts : 0,
    d.queue.map((h) => [h.id, h.status, h.ppi, h.checks_pass, h.level].join(":")).join("|"),
    d.verdicts.length, d.crew.remarks.map((x) => x.status).join(),
    (d.crew.leaders || []).map((l) => [l.agent, l.bets].join(":")).join(),
  ]);
}

/*_seenChat logic*/
function seenChatCount() {
  const c = localStorage.getItem("rg_seenChat");
  return c == null ? (S.data ? S.data.crew.chat.length : 0) : +c;
}

/* --------------------------------------------------------------- шаблоны */
const avatar = (id, size) => {
  const a = (S.data ? S.data.crew.agents : []).find((x) => x.id === id);
  const nm = id === "human" ? "Человек" : (a ? a.name : id);
  return `<span class="avatar ${size || ""}" style="background:${AGENT_COLOR[id] || "#667"}" title="${esc(nm)}">${esc(initials(nm))}</span>`;
};

function ladderHTML(level) {
  const cur = { "—": -1, L0: 0, L1: 1, L2: 2, L3: 3 }[level] ?? -1;
  return `<span class="ladder"><span>каскад</span>${[0, 1, 2, 3].map((i) =>
    `<i class="${i < cur ? "done" : i === cur ? "now" : ""}" title="L${i}"></i>`).join("")}<span style="margin-left:4px">${cur >= 0 ? "L" + cur : "—"}</span></span>`;
}

function corridorHTML(h, actual) {
  const mid = (Number(h.forecast_low) + Number(h.forecast_high)) / 2;
  const vals = [Number(h.forecast_low), Number(h.forecast_high)];
  if (actual != null && !isNaN(+actual)) vals.push(+actual);
  if (!isNaN(mid)) vals.push(mid);
  const lo = Math.min(...vals) * 1.15, hi = Math.max(...vals) * 0.85;
  const span = hi - lo || 1;
  const pos = (v) => Math.max(0, Math.min(100, (v - lo) / span * 100));
  const bl = pos(Number(h.forecast_low)), bw = Math.max(2, pos(Number(h.forecast_high)) - bl);
  const fp = isNaN(mid) ? 50 : pos(mid);
  const ap = (actual != null && !isNaN(+actual)) ? pos(+actual) : null;
  return `<div class="corridor">
    <div class="c-track">
      <span class="c-band" style="left:${bl}%;width:${bw}%"></span>
      <span class="c-fore" style="left:calc(${fp}% - 1px)"></span>
      ${ap != null ? `<span class="c-fore" style="left:calc(${ap}% - 1px);background:var(--acc);box-shadow:0 0 8px var(--acc)"></span>` : ""}
    </div>
    <div class="c-lbl"><span>${fmtPct(h.forecast_low)}</span><span>обещают: ${esc(h.forecast)} · коридор ${fmtPct(h.forecast_low)}…${fmtPct(h.forecast_high)}</span><span>${fmtPct(h.forecast_high)}</span></div>
  </div>`;
}

function progressHTML(p, paused) {
  return `<div class="progress ${paused ? "paused" : ""}"><span class="fill" style="width:${Math.round(p * 100)}%"></span></div>`;
}

/* --------------------------------------------------------------- хедер */
function renderHeader() {
  const upd = $("#upd");
  if (S.offline) { upd.textContent = "нет связи"; return; }
  if (!S.data) { upd.textContent = "—"; return; }
  const s = Math.round((Date.now() - S.lastFetchOk) / 1000);
  upd.textContent = s < 4 ? "живые данные" : s + " с назад";
}

/* ============================================================================
   ЭКРАН: ПУЛЬТ
   ========================================================================== */
function screenDash() {
  const d = S.data;
  if (!d) return skeletonHTML();
  const g = d.gov, gpu = d.gpu, cur = d.current, st = d.stats;

  const hero = cur ? `
    <section class="card task-hero">
      <div class="th-top">
        <span class="chip acc">${ICOS.bolt} на GPU</span>
        <span class="chip mono">${esc(cur.hid)}</span>
        <span class="chip violet">${esc(cur.level || "")}</span>
        ${cur.dry_run ? `<span class="chip warn">dry-run</span>` : ""}
      </div>
      <h3>${esc((d.queue.find((h) => h.id === cur.hid) || {}).title || cur.hid)}</h3>
      <div class="th-sub">идёт ${fmtMin(cur.elapsed_min)} · статус пишет диспетчер (тик каждые 2 мин)</div>
      <div class="split">
        <button class="btn block" data-act="pause">${ICO.pause} Пауза · чекпойнт</button>
        <button class="btn danger hold-btn block" data-act="kill" data-hid="${esc(cur.hid)}" data-hold>
          <span class="hold-fill"></span>
          <span class="btn-inner">${ICO.stop} Снять <small style="font-weight:600;opacity:.75">удержать 1,2 с</small></span>
        </button>
      </div>
    </section>` : `
    <section class="card">
      <div class="card-label"><span class="cl-ico" style="color:var(--ok)">${ICOS.wave}</span><span>Контур жив</span><span class="r">${g.autostart ? "автозапуск вкл" : "пауза"}</span></div>
      <div class="empty" style="padding:14px 4px"><div class="e-ico">🛰</div>Прогонов нет — GPU свободен. Диспетчер проверяет очередь каждые 2 минуты и сам берёт лучшую по PPI.</div>
      <div class="hyp-meta" style="gap:6px">
        <span class="chip dim">${esc(g.platform || "")}</span>
        <span class="chip dim">${st.queue_len} в очереди</span>
        <span class="chip dim">тик · 2 мин</span>
      </div>
      <div class="split" style="margin-top:14px">
        ${g.autostart ? `<button class="btn block" data-act="pause">${ICO.pause} Пауза</button>`
                      : `<button class="btn primary block" data-act="resume">${ICO.play} Вернуть автозапуск</button>`}
        <button class="btn block" data-act="nav" data-nav="pipe">Очередь ›</button>
      </div>
    </section>`;

  const approvals = d.approvals.length ? `
    <section class="card approve-card">
      <div class="card-label"><span class="cl-ico" style="color:var(--warn)">${ICOS.bell}</span><span>Ждёт решения человека</span><span class="r">${d.approvals.length}</span></div>
      ${d.approvals.map((a) => `
        <div style="margin-bottom:10px">
          <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
            <b class="mono" style="font-size:14.5px">${esc(a.hid)}</b>
            <span class="chip warn">${esc(a.level || "L2")} · ${fmtN(a.hours, 1)} GPU-ч</span>
            <span class="chip dim">PPI ${fmtN(a.ppi)}</span>
          </div>
          <div style="font-size:15px;margin:6px 0 8px">${esc(a.title)}</div>
          <div class="note">${esc(a.note || "")}</div>
          <div class="split" style="margin-top:9px">
            <button class="btn ok sm block" data-act="approve" data-hid="${esc(a.hid)}" data-ok="1">${ICO.check} Одобрить</button>
            <button class="btn danger sm block ${S.pendingReject === a.hid ? "" : "ghost"}" data-act="approve" data-hid="${esc(a.hid)}" data-ok="0">${S.pendingReject === a.hid ? "Точно отклонить?" : "Отклонить"}</button>
          </div>
        </div>`).join("")}
    </section>` : "";

  // живая лента: последняя реплика, чекпойнт, последний вердикт
  const lastMsg = d.crew.chat.length ? d.crew.chat[d.crew.chat.length - 1] : null;
  const lastMsgName = lastMsg ? ((d.crew.agents || []).find((a) => a.id === lastMsg.agent) || {}).name || lastMsg.agent : "";
  const checkpoint = d.queue.find((h) => h.status === "paused_checkpoint");
  const lastVerdict = d.verdicts[0];
  const nowRows = [
    lastMsg ? { ico: ICOS.wave, color: "var(--acc-br)", nav: "crew", html: `<b style="color:${AGENT_COLOR[lastMsg.agent] || "var(--tx)"}">${esc(lastMsgName)}</b> · ${esc(lastMsg.text.length > 68 ? lastMsg.text.slice(0, 68) + "…" : lastMsg.text)}` } : null,
    checkpoint ? { ico: ICOS.flask, color: "var(--warn)", nav: "pipe", hid: checkpoint.id, html: `<b class="mono">${esc(checkpoint.id)}</b> на чекпойнте — жду вердикта (${esc(checkpoint.title.slice(0, 44))}${checkpoint.title.length > 44 ? "…" : ""})` } : null,
    lastVerdict ? { ico: ICO.check, color: lastVerdict.kind === "confirmed" ? "var(--ok)" : lastVerdict.kind === "rejected" || lastVerdict.kind === "killed" ? "var(--err)" : "var(--warn)", nav: "verdicts", html: `вердикт <b class="mono">${esc(lastVerdict.hid)}</b>: ${KIND_RU[lastVerdict.kind] || lastVerdict.kind}${lastVerdict.deviation != null ? ` · Δ ${fmtPct(lastVerdict.deviation, 0)}` : ""}` } : null,
  ].filter(Boolean);
  const nowBlock = nowRows.length ? `
    <section class="card">
      <div class="card-label"><span class="cl-ico">${ICOS.wave}</span><span>Сейчас в лаборатории</span><span class="r mono">${d.crew.chat_total} реплик всего</span></div>
      ${nowRows.map((r) => `
        <div style="display:flex;gap:11px;align-items:flex-start;padding:9px 2px;border-bottom:1px solid var(--line)">
          <span class="cl-ico" style="width:17px;height:17px;color:${r.color};margin-top:2px">${r.ico}</span>
          <div style="flex:1;min-width:0;font-size:14.5px;line-height:1.45;color:var(--tx2)">${r.html}</div>
          <button class="rl-chev" ${r.hid ? `data-act="open-hyp" data-hid="${esc(r.hid)}"` : `data-act="nav" data-nav="${r.nav || "dash"}"`} style="background:none;border:0;color:var(--tx3);font-size:18px;padding:0 2px">›</button>
        </div>`).join("")}
    </section>` : "";

  const nextQ = d.queue.find((h) => h.status === "queued");
  const next = nextQ ? `
    <section class="rowlink" data-act="open-hyp" data-hid="${esc(nextQ.id)}">
      <span class="cl-ico" style="width:18px;height:18px;color:var(--acc-br)">${ICOS.bolt}</span>
      <div style="min-width:0;flex:1">
        <div style="font-size:15px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(nextQ.title)}</div>
        <div class="note mono">${esc(nextQ.id)} · PPI ${fmtN(nextQ.ppi)} · ${fmtN(nextQ.est_hours, 1)} ч</div>
      </div>
      <span class="rl-chev">›</span>
    </section>` : "";

  const wr = st.win_rate == null ? null : Math.round(st.win_rate * (st.win_rate <= 1 ? 100 : 1));
  return `
    ${hero}
    ${approvals}
    ${nowBlock}
    <section class="card">
      <div class="card-label"><span class="cl-ico">${ICOS.chip}</span><span>${gpu.available ? esc(gpu.name) : "GPU"}</span><span class="r">${g.autostart ? "автозапуск вкл" : "автозапуск выкл"}</span></div>
      ${gpu.available ? `
      <div class="gpu-grid">
        <div class="ring">
          <svg width="104" height="104" viewBox="0 0 104 104">
            <defs>
              <linearGradient id="ringg" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#5b8cff"/><stop offset="1" stop-color="#8f7bff"/>
              </linearGradient>
            </defs>
            <circle cx="52" cy="52" r="44" fill="none" stroke="var(--card3)" stroke-width="9"/>
            <circle cx="52" cy="52" r="44" fill="none" stroke="${gpu.util > 5 ? "url(#ringg)" : "var(--tx3)"}" stroke-width="9" stroke-linecap="round"
              stroke-dasharray="${(2 * Math.PI * 44 * gpu.util / 100).toFixed(1)} ${(2 * Math.PI * 44).toFixed(1)}"/>
          </svg>
          <span class="val"><div><b>${gpu.util}%</b><i>util</i></div></span>
        </div>
        <div>
          <div class="meter">
            <div class="meter-head"><span>VRAM</span><b>${fmtN(gpu.used_gb, 1)} / ${fmtN(gpu.total_gb, 1)} ГБ</b></div>
            <div class="bar"><span class="fill" style="width:${gpu.total_gb ? (gpu.used_gb / gpu.total_gb * 100).toFixed(1) : 0}%;background:linear-gradient(90deg,var(--acc),var(--violet))"></span></div>
          </div>
          <div class="meter">
            <div class="meter-head"><span>Температура</span><b>${gpu.temp}°C</b></div>
            <div class="temp-scale"><span class="temp-needle" style="left:calc(${Math.min(100, gpu.temp / 95 * 100).toFixed(1)}% - 2px)"></span></div>
          </div>
        </div>
      </div>` : `
      <div class="empty"><div class="e-ico">🔌</div>GPU недоступен на этом узле${g.platform ? ` (${esc(g.platform)}${g.debug ? ", debug" : ""})` : ""}. Очередь копится — диспетчер запустит лучшую гипотезу, как появится свободная карта.</div>`}
    </section>
    <section class="card">
      <div class="card-label"><span class="cl-ico" style="color:var(--acc-br)">${ICOS.battery}</span><span>Суточный лимит</span><span class="r mono">${fmtN(g.budget_hours.used, 1)} / ${fmtN(g.budget_hours.limit, 0)} ч</span></div>
      <div class="budget-stats">
        <div class="kvv"><b>${fmtN(g.budget_hours.used, 1)}<em> ч</em></b><span>потрачено</span></div>
        <div class="kvv"><b class="${(g.budget_hours.limit - g.budget_hours.used) < 4 ? "delta-pos" : ""}">${fmtN(g.budget_hours.limit - g.budget_hours.used, 1)} ч</b><span>осталось</span></div>
        ${g.budget_tasks.has_tasks_counter ? `<div class="kvv"><b>${g.budget_tasks.used}<em> / ${g.budget_tasks.limit}</em></b><span>запусков дня</span></div>` : `<div class="kvv"><b>${st.verdicts_total}</b><span>вердиктов всего</span></div>`}
      </div>
      <div class="bar" style="height:10px"><span class="fill" style="width:${(g.budget_hours.used / g.budget_hours.limit * 100).toFixed(1)}%;background:linear-gradient(90deg,var(--acc),var(--violet))"></span></div>
      <div class="note" style="margin-top:8px">Дорогой прогон (&gt; ${g.approval_hours} ч) требует одобрения человека. Запуски решает диспетчер по PPI.</div>
    </section>
    <section class="kpi-grid">
      <div class="kpi"><span class="k-ico">${ICOS.target}</span><b>${st.calibration == null ? "—" : st.calibration + "%"}<em>точность</em></b><span>калибровка прогнозов</span></div>
      <div class="kpi"><span class="k-ico">${ICO.check}</span><b>${wr == null ? "—" : wr + "%"}</b><span>доля подтверждений</span></div>
      <div class="kpi"><span class="k-ico">${ICOS.flask}</span><b>${st.queue_len}</b><span>живых гипотез в очереди</span></div>
      <div class="kpi"><span class="k-ico">${ICOS.bell}</span><b>${st.open_remarks}</b><span>открытых замечаний ревью</span></div>
    </section>
    ${next}
    <section class="split">
      <button class="btn primary block" data-act="wizard">${ICO.plus} Подать идею</button>
      <button class="btn block" data-act="nav" data-nav="crew">💬 Экипаж</button>
    </section>`;
}

/* ============================================================================
   ЭКРАН: КОНВЕЙЕР
   ========================================================================== */
function hypCardHTML(h) {
  const statusChip = h.status === "running" ? `<span class="chip acc">на GPU</span>`
    : h.status === "blocked" ? `<span class="chip err">блок</span>`
    : h.status === "paused_checkpoint" ? `<span class="chip warn">пауза</span>`
    : h.status === "killed" ? `<span class="chip err">снята</span>` : "";
  const srcChip = SRC_RU[h.source] ? `<span class="chip ${h.source === "dr" ? "dim" : "violet"}">${SRC_RU[h.source]}</span>` : "";
  const ppiTop = S.data ? Math.max(...S.data.queue.map((x) => x.ppi || 0), 0.001) : 1;
  const ppiCol = h.ppi >= ppiTop * 0.66 ? "var(--ok)" : h.ppi >= ppiTop * 0.33 ? "var(--warn)" : "var(--tx3)";
  const needsApproval = h.status === "queued" && h.est_hours > (S.data.gov.approval_hours || 12) && !h.approved;
  return `
  <article class="card hyp-card" data-act="open-hyp" data-hid="${esc(h.id)}">
    <div class="hyp-row1">
      <b class="hid" style="color:${h.status === "running" ? "var(--acc)" : h.status === "blocked" || h.status === "killed" ? "var(--err)" : "var(--tx2)"}">${esc(h.id)}</b>
      <span class="chip ${BIN_COLOR[h.bin] || "dim"}">${esc(h.bin)}</span>
      <span class="chip dim">${fmtN(h.est_hours, 1)} ч</span>
      ${statusChip}
      ${needsApproval ? `<span class="chip warn">ждёт /approve</span>` : ""}
      <span class="ppi-badge"><b style="color:${ppiCol}">${fmtN(h.ppi)}</b><span>PPI оч/ч</span></span>
    </div>
    <div class="hyp-title">${esc(h.title)}</div>
    <div class="hyp-meta">
      <span class="chip dim">PI ${fmtN(h.pi)}</span>
      <span class="chip dim">сигналы ${h.signals}</span>
      <span class="chip ${h.checks_pass >= 8 ? "ok" : h.checks_pass >= 1 ? "warn" : "err"}">kill ${h.checks_pass}/8</span>
      ${ladderHTML(h.level)}
      ${srcChip}
    </div>
    ${h.note ? `<div class="note" style="margin-top:8px">${esc(h.note)}</div>` : ""}
  </article>`;
}

function screenPipe() {
  const d = S.data;
  if (!d) return skeletonHTML();
  const tab = S.sub.pipe;
  let list = d.queue;
  if (tab === "queue") list = list.filter((h) => ["queued", "running", "paused_checkpoint", "blocked"].includes(h.status));
  if (tab === "human") list = list.filter((h) => h.source === "human");
  const order = { running: 0, paused_checkpoint: 1, queued: 2, blocked: 3 };
  list = [...list].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || b.ppi - a.ppi);
  return `
    <div class="screen-title"><h1>Конвейер гипотез</h1><span class="sub">${list.length} · по PPI</span></div>
    <div class="seg">
      <button class="${tab === "queue" ? "on" : ""}" data-act="pipe-tab" data-v="queue">Активные</button>
      <button class="${tab === "all" ? "on" : ""}" data-act="pipe-tab" data-v="all">Все</button>
      <button class="${tab === "human" ? "on" : ""}" data-act="pipe-tab" data-v="human">От человека</button>
    </div>
    ${list.length ? `<div class="list-gap">${list.map(hypCardHTML).join("")}</div>`
      : `<div class="empty"><div class="e-ico">🗂</div>Пусто. ${tab === "human" ? "Подай первую идею — кнопка «＋»." : "Очередь разобрана."}</div>`}
    <div class="note" style="text-align:center">PPI = PI / GPU-час · P1 ≤4ч · P2 ≤12ч · P3 ≤48ч · P4 &gt;48ч</div>`;
}

/* ============================================================================
   ЭКРАН: ТЕЛЕМЕТРИЯ
   ========================================================================== */
function screenLive() {
  const d = S.data;
  if (!d) return skeletonHTML();
  const cur = d.current;
  const vs = d.verdicts.filter((v) => v.forecast != null && v.actual != null);
  const hero = cur ? `
    <section class="card">
      <div class="run-hero">
        <span class="live-dot"></span>
        <b class="mono">${esc(cur.hid)}</b>
        <span class="chip violet">${esc(cur.level || "")}</span>
        ${cur.dry_run ? `<span class="chip warn">dry-run</span>` : ""}
        <span class="chip dim">идёт ${fmtMin(cur.elapsed_min)}</span>
      </div>
      <div class="note" style="margin-top:9px">Живые кривые обучения появятся, когда прогон пишет метрики; статус прогона — факты из experiments.</div>
    </section>` : `
    <section class="card"><div class="empty"><div class="e-ico">📉</div>Активного прогона нет. Диспетчер проверяет очередь каждые 2 мин.</div></section>`;

  return `
    <div class="screen-title"><h1>Прогнозы против факта</h1><span class="sub">${vs.length} вердиктов</span></div>
    ${hero}
    <section class="card chart-card">
      <div class="legend">
        <span class="li"><span class="sw" style="background:#9889ff"></span>обещали</span>
        <span class="li"><span class="sw" style="background:#6b97ff"></span>получили</span>
      </div>
      <div class="chart-wrap"><canvas id="ch-calib"></canvas></div>
      <div class="readout" id="ch-readout">коснись графика — точные значения</div>
    </section>
    <section class="card">
      <div class="card-label"><span>Вердикты по порядку</span><span class="r">отклонение от прогноза</span></div>
      ${vs.length ? `<table class="cmp-table">
        <tr><th>Гипотеза</th><th>Обещали</th><th>Факт</th><th>Δ</th><th>GPU-ч</th></tr>
        ${vs.slice(0, 10).map((v) => `<tr>
          <td><b class="mono">${esc(v.hid)}</b> <span class="chip ${v.kind === "confirmed" ? "ok" : v.kind === "partial" ? "warn" : "err"}" style="font-size:10.5px">${KIND_RU[v.kind]}</span></td>
          <td class="mono">${fmtPct(v.forecast, 0)}</td>
          <td class="mono">${fmtPct(v.actual, 0)}</td>
          <td><b class="${Math.abs(v.deviation) > 40 ? "delta-pos" : "delta-neg"}">${fmtPct(v.deviation, 0)}</b></td>
          <td class="mono">${fmtN(v.gpu_hours, 1)}</td>
        </tr>`).join("")}
      </table>` : `<div class="empty">Пока нет вердиктов с прогнозом и фактом — график появится после первого закрытия.</div>`}
    </section>`;
}

function drawLiveCharts() {
  const d = S.data;
  if (!d) return;
  const el = $("#ch-calib");
  if (!el) return;
  const vs = d.verdicts.filter((v) => v.forecast != null && v.actual != null).slice().reverse();
  if (!vs.length) return;
  Charts.line("ch-calib", {
    series: [
      { id: "fore", label: "обещали", color: "#9889ff", width: 2, dash: [6, 4],
        data: vs.map((v, i) => [i + 1, v.forecast]) },
      { id: "act", label: "получили", color: "#6b97ff", width: 2.2,
        data: vs.map((v, i) => [i + 1, v.actual]) },
    ],
    height: 200,
    fmtX: (x) => "#" + Math.round(x),
    fmtY: (v) => v.toFixed(0) + "%",
    onScrub: (vals) => {
      const ro = $("#ch-readout");
      if (!ro) return;
      if (!vals || !vals.act) { ro.textContent = "коснись графика — точные значения"; return; }
      const v = vs[Math.min(vs.length - 1, Math.max(0, Math.round(vals.act.x) - 1))];
      ro.innerHTML = v
        ? `<b class="mono">${esc(v.hid)}</b> · обещали <b class="mono">${fmtPct(v.forecast, 0)}</b> · получили <b class="mono">${fmtPct(v.actual, 0)}</b> · Δ <b>${fmtPct(v.deviation, 0)}</b>`
        : "коснись графика — точные значения";
    },
  });
}

/* ============================================================================
   ЭКРАН: ЭКИПАЖ
   ========================================================================== */
function disputeHTML(m) {
  if (!m || !m.dispute_id) return "";
  return `<div class="dispute">
    <div class="q">Спор в чате · закрывает арбитраж Boss числом из базы</div>
    <div class="note">Реплики с меткой спора — часть сцены взаимного ревью. Человек наблюдает; решение принимает Boss по данным SQLite.</div>
  </div>`;
}

function chatMsgHTML(m, i) {
  const hlc = m.kind === "bet" ? "hl-ok" : m.kind === "necro" ? "hl-err" : m.kind === "review" ? "hl-warn" : "";
  const name = m.name || ((S.data.crew.agents || []).find((a) => a.id === m.agent) || {}).name || m.agent;
  const hid = m.hid || (HID_RE_CHAT.lastIndex = 0, (HID_RE_CHAT.exec(m.text) || [])[1]);
  return `
    <div class="msg ${hlc}" data-idx="${i}">
      ${avatar(m.agent)}
      <div class="m-body">
        <div class="m-head"><span class="m-name" style="color:${AGENT_COLOR[m.agent] || "var(--tx)"}">${esc(name)}</span><span class="m-time">${timeHM(m.ts)}</span></div>
        <div class="m-text">${esc(m.text)}${hid ? ` <button class="m-hid mono" data-act="open-hyp" data-hid="${esc(hid)}">${esc(hid)}</button>` : ""}${m.dispute_id ? ` <span class="chip violet" style="font-size:11px">спор</span>` : ""}</div>
        ${m.dispute_id ? disputeHTML(m) : ""}
      </div>
    </div>`;
}
const HID_RE_CHAT = /\bH-\d{3,4}\b/;

function screenCrew() {
  const d = S.data;
  if (!d) return skeletonHTML();
  const c = d.crew, tab = S.sub.crew;
  const roster = `<div class="roster">${c.agents.map((a) => `
    <button class="agent-chip" data-act="agent" data-id="${esc(a.id)}">
      ${avatar(a.id)}
      <span>${esc(a.name)}</span>
    </button>`).join("")}</div>`;

  let body = "";
  if (tab === "chat") {
    body = c.chat.length
      ? `<div class="chat" id="chat-list">${c.chat.map(chatMsgHTML).join("")}</div>`
      : `<div class="empty"><div class="e-ico">💬</div>Чат пуст: сцены генерирует код по событиям контура (запуск, вердикт, ревью).</div>`;
  } else if (tab === "review") {
    body = c.remarks.length ? `<div class="list-gap">${c.remarks.map((r) => `
      <div class="remark ${r.status === "closed" ? "closed" : ""}">
        ${avatar(r.from)}
        <div style="flex:1;min-width:0">
          <div class="r-txt">${esc(r.text)}</div>
          <div class="r-meta">
            ${r.hid ? `<button class="chip dim mono" data-act="open-hyp" data-hid="${esc(r.hid)}">${esc(r.hid)}</button>` : ""}
            <span class="chip ${r.status === "closed" ? "ok" : "warn"}">${r.status === "closed" ? "закрыто" : "открыто"}</span>
            <span>чинит экипаж в ближайший тик</span>
          </div>
        </div>
      </div>`).join("")}</div>`
      : `<div class="empty"><div class="e-ico">🧹</div>Замечаний нет — взаимное ревью чисто</div>`;
  } else {
    const open = c.bets || [];
    body = `
      <div class="card-label" style="margin-top:2px">Открытые ставки · закрываются вердиктом</div>
      ${open.length ? open.map((b) => {
        const total = b.up.length + b.down.length || 1;
        return `
        <div class="bet-row">
          <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
            <b class="mono" style="font-size:14px">${esc(b.hid)}</b>
            <span class="chip ${b.status === "running" ? "acc" : "dim"}">${b.status === "running" ? "на GPU" : STATUS_RU[b.status] || b.status}</span>
          </div>
          <div style="font-size:15px;font-weight:600;margin:7px 0 3px">${esc(b.title)}</div>
          <div class="bet-bar"><span class="b-up" style="width:${b.up.length / total * 100}%"></span><span class="b-down"></span></div>
          <div style="display:flex;justify-content:space-between;font-size:13px;color:var(--tx2)">
            <span style="color:var(--ok)">▲ ${b.up.length ? esc(b.up.join(", ")) : "—"}</span>
            <span style="color:var(--err)">▼ ${b.down.length ? esc(b.down.join(", ")) : "—"}</span>
          </div>
        </div>`;
      }).join("") : `<div class="empty">Открытых ставок нет</div>`}
      <div class="card-label" style="margin-top:6px">Рейтинг точности (hit-rate · Brier)</div>
      <div class="card">
        ${c.leaders.length ? c.leaders.map((l, i) => {
          const a = c.agents.find((x) => x.id === l.agent) || {};
          return `<div class="lead-row">
            <span class="lead-rank">${i + 1}</span>
            ${avatar(l.agent, "sm")}
            <div class="lead-mid">
              <div class="lm1"><span style="color:${AGENT_COLOR[l.agent]}">${esc(a.name || l.agent)}</span><span class="mono">${Math.round((l.rate || 0) * 100)}%</span></div>
              <div class="rate-bar"><i style="width:${(l.rate || 0) * 100}%"></i></div>
              <div class="lm2"><span>${l.bets} ставок закрыто</span>${l.brier != null ? `<span>Brier ${l.brier}</span>` : ""}</div>
            </div>
          </div>`;
        }).join("") : `<div class="empty">Ставок ещё не закрыто: рейтинг появится после первых вердиктов</div>`}
      </div>`;
  }

  const openRm = c.remarks.filter((r) => r.status === "open").length;
  return `
    <div class="screen-title"><h1>Экипаж</h1><span class="sub">${c.agents.length} агентов · ${c.chat_total} реплик</span></div>
    ${roster}
    <div class="seg violet">
      <button class="${tab === "chat" ? "on" : ""}" data-act="crew-tab" data-v="chat">Чат</button>
      <button class="${tab === "review" ? "on" : ""}" data-act="crew-tab" data-v="review">Ревью ${openRm ? "· " + openRm : ""}</button>
      <button class="${tab === "bets" ? "on" : ""}" data-act="crew-tab" data-v="bets">Ставки</button>
    </div>
    ${body}`;
}

/* ============================================================================
   ЭКРАН: ВЕРДИКТЫ
   ========================================================================== */
function dumbbellHTML(v) {
  if (v.forecast == null || v.actual == null) return `<div class="note">Снята до эксперимента — факт не тратил GPU-часы.</div>`;
  const lo = Math.min(v.forecast, v.actual) * 1.2, hi = Math.max(v.forecast, v.actual) * 0.85;
  const span = hi - lo || 1;
  const cl = (p) => Math.max(5, Math.min(95, p));
  const fp = cl((v.forecast - lo) / span * 100), ap = cl((v.actual - lo) / span * 100);
  return `
    <div class="dumbbell">
      <span class="d-track"></span>
      <span class="d-line" style="left:${Math.min(fp, ap)}%;width:${Math.abs(ap - fp)}%"></span>
      <span class="d-pt fore" style="left:calc(${fp}% - 7px)"></span>
      <span class="d-pt act" style="left:calc(${ap}% - 7px)"></span>
    </div>
    <div class="d-caps">
      <span class="c-l">● обещали ${fmtPct(v.forecast, 0)}</span>
      <b class="c-m ${Math.abs(v.deviation) > 40 ? "delta-pos" : "delta-neg"}">${fmtPct(v.deviation, 0)}</b>
      <span class="c-r">получили ${fmtPct(v.actual, 0)} ●</span>
    </div>`;
}

function verdictCardHTML(v) {
  const seeds = `<span class="seeds">${Array.from({ length: v.seeds_total }, (_, i) => `<i class="${i < v.seeds_pass ? "on" : ""}"></i>`).join("")}</span>`;
  return `
    <article class="card verdict-card ${v.kind}" data-act="open-verdict" data-id="${esc(v.id)}">
      <div class="hyp-row1">
        <b class="hid">${esc(v.hid)}</b>
        <span class="stamp ${v.kind}">${KIND_RU[v.kind]}</span>
        ${v.patent ? `<span class="chip acc" style="margin-left:auto">📄 патент</span>` : ""}
      </div>
      <div class="hyp-title" style="margin:8px 0 2px">${esc(v.title)}</div>
      ${dumbbellHTML(v)}
      <div class="hyp-meta" style="margin-top:9px">
        ${v.seeds_total > 0 ? `<span class="chip dim">${seeds} seeds</span>` : ""}
        <span class="chip dim">${fmtN(v.gpu_hours, 1)} GPU-ч</span>
      </div>
    </article>`;
}

function screenVerdicts() {
  const d = S.data;
  if (!d) return skeletonHTML();
  const vs = d.verdicts;
  const f = S.verdictFilter;
  let list = f === "all" ? vs : f === "patents" ? vs.filter((v) => v.patent) : vs.filter((v) => v.kind === f);
  const counts = { confirmed: 0, partial: 0, rejected: 0, killed: 0 };
  vs.forEach((v) => counts[v.kind]++);
  const tot = vs.length || 1;
  return `
    <div class="screen-title"><h1>Вердикты и ценность</h1><span class="sub">${vs.length} шт.</span></div>
    <section class="card">
      <div class="card-label"><span>Что обещали против факта</span><span class="r">калибровка ${d.stats.calibration == null ? "—" : d.stats.calibration + "%"}</span></div>
      <div class="stack-bar">
        <i style="width:${counts.confirmed / tot * 100}%;background:var(--ok)"></i>
        <i style="width:${counts.partial / tot * 100}%;background:var(--warn)"></i>
        <i style="width:${counts.rejected / tot * 100}%;background:var(--err)"></i>
        <i style="width:${counts.killed / tot * 100}%;background:var(--tx3)"></i>
      </div>
      <div class="kv" style="margin-top:10px">
        <div class="kvv"><b style="color:var(--ok)">${counts.confirmed}</b><span>подтверждено</span></div>
        <div class="kvv"><b style="color:var(--warn)">${counts.partial}</b><span>частично</span></div>
        <div class="kvv"><b style="color:var(--err)">${counts.rejected}</b><span>опровержено</span></div>
        <div class="kvv"><b>${counts.killed}</b><span>снято рано</span></div>
      </div>
      <div class="note">Ранние снятия сэкономили ${d.stats.gpu_saved_h} GPU-ч.</div>
    </section>
    <div class="chips">
      <button class="fchip ${f === "all" ? "on" : ""}" data-act="vfilter" data-v="all">Все</button>
      <button class="fchip ${f === "confirmed" ? "on" : ""}" data-act="vfilter" data-v="confirmed">Подтверждено</button>
      <button class="fchip ${f === "partial" ? "on" : ""}" data-act="vfilter" data-v="partial">Частично</button>
      <button class="fchip ${f === "rejected" ? "on" : ""}" data-act="vfilter" data-v="rejected">Опровергнуто</button>
      <button class="fchip ${f === "killed" ? "on" : ""}" data-act="vfilter" data-v="killed">Снято</button>
      <button class="fchip ${f === "patents" ? "on" : ""}" data-act="vfilter" data-v="patents">📄 Патенты</button>
    </div>
    ${list.length ? `<div class="list-gap">${list.map(verdictCardHTML).join("")}</div>` : `<div class="empty"><div class="e-ico">🗃</div>Ничего по фильтру</div>`}`;
}

/* --------------------------------------------------------------- скелетон */
function skeletonHTML() {
  return `<div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card" style="height:160px"></div><div class="skeleton skeleton-card" style="height:90px"></div>`;
}

/* ============================================================================
   ШИТЫ (нижние панели)
   ========================================================================== */
function openSheet(html, cls) {
  closeSheet(true);
  const backdrop = document.createElement("div");
  backdrop.className = "sheet-backdrop";
  const sheet = document.createElement("div");
  sheet.className = "sheet" + (cls ? " " + cls : "");
  sheet.innerHTML = `<div class="sheet-grab"></div><div class="sheet-head">
      <h2 id="sheet-title"></h2><button class="sheet-x" data-act="sheet-close" aria-label="Закрыть">${ICO.x}</button>
    </div><div class="sheet-body" id="sheet-body">${html}</div>`;
  $("#sheet-root").appendChild(backdrop);
  $("#sheet-root").appendChild(sheet);
  requestAnimationFrame(() => { backdrop.classList.add("show"); sheet.classList.add("show"); });
  backdrop.addEventListener("click", () => closeSheet());
  S.sheet = { el: sheet, backdrop };
  updateBackButton();
}

function closeSheet(instant) {
  const st = S.sheet;
  if (tg && tg.MainButton) try { tg.MainButton.hide(); } catch (e) {}
  if (S.wizard && !instant) { S.wizard = null; }
  if (!st) { updateBackButton(); return; }
  S.sheet = null;
  const rm = () => { st.el.remove(); st.backdrop.remove(); };
  if (instant) rm();
  else {
    st.backdrop.classList.remove("show"); st.el.classList.remove("show");
    setTimeout(rm, 250);
  }
  S._key = null;               // экран под щитом обновится сразу
  if (!instant) renderScreen();
  updateBackButton();
}

function setSheetTitle(t) { const el = $("#sheet-title"); if (el) el.textContent = t; }

/* --------------------------------------------------------------- гипотеза */
function openHyp(hid) {
  const d = S.data;
  const h = d && d.queue.find((x) => x.id === hid);
  if (!h) { toast("Гипотеза не найдена", "err"); return; }
  const cur = d.current && d.current.hid === h.id ? d.current : null;
  const src = SRC_RU[h.source] || h.source;
  const runs = [["L0", "5 мин"], ["L1", "≈2 ч"], ["L2", "часы: порог /approve"]];
  const needsApproval = h.status === "queued" && h.est_hours > d.gov.approval_hours && !h.approved;
  openSheet(`
    <div>
      <div class="hyp-row1">
        <b class="hid">${esc(h.id)}</b>
        <span class="chip ${BIN_COLOR[h.bin] || "dim"}">${esc(h.bin)}</span>
        <span class="chip ${h.status === "running" ? "acc" : h.status === "blocked" || h.status === "killed" ? "err" : h.status === "paused_checkpoint" ? "warn" : "dim"}">${STATUS_RU[h.status] || h.status}</span>
        <span class="ppi-badge"><b>${fmtN(h.ppi)}</b><span>PPI оч/ч</span></span>
      </div>
      <h3 style="font-size:17.5px;margin:10px 0 9px;line-height:1.35">${esc(h.title)}</h3>
      <div class="kv">
        <div class="kvv"><b>${fmtN(h.pi)}</b><span>PI</span></div>
        <div class="kvv"><b>${fmtN(h.est_hours, 1)} ч</b><span>оценка GPU</span></div>
        <div class="kvv"><b>${h.signals}</b><span>сигналов</span></div>
        <div class="kvv"><b>${fmtN(h.age_days, 1)} дн</b><span>в очереди</span></div>
        <div class="kvv"><b style="font-size:14px">${esc(src)}</b><span>источник</span></div>
      </div>
      ${ladderHTML(h.level)}
    </div>
    ${cur ? `<div class="note">На GPU сейчас: идёт ${fmtMin(cur.elapsed_min)}. Статус пишет диспетчер.</div>` : ""}
    <div>
      <div class="card-label">Коридор прогноза (зафиксирован до запуска)</div>
      ${h.forecast != null ? corridorHTML(h) : `<div class="note">Прогноз не зафиксирован — вердикт без него невозможен.</div>`}
    </div>
    <div>
      <div class="card-label">Kill-стадия · подтверждено ${h.checks_pass}/8</div>
      <div class="checks">
        ${(d.checks || []).map((c, i) => `
          <div class="check ${i < h.checks_pass ? "pass" : "wait"}">
            <span class="ic">${i < h.checks_pass ? "✓" : "•"}</span>
            <span class="ct">${esc(c)}</span>
          </div>`).join("")}
      </div>
      <div class="split" style="margin-top:10px">
        <button class="btn sm block" data-act="run-check" data-hid="${esc(h.id)}">Проверить гейтом</button>
        <button class="btn sm block ghost" data-act="sheet-close">Закрыть</button>
      </div>
      <div class="qlist" id="gate-result" style="margin-top:8px"></div>
    </div>
    ${h.status === "queued" || h.status === "blocked" ? `
    <div>
      <div class="card-label">Запустить уровень вручную</div>
      <div class="split">
        ${runs.map(([lv, note]) => `<button class="btn sm block ${lv === "L2" ? "ghost" : ""}" data-act="run-level" data-hid="${esc(h.id)}" data-lv="${lv}">${lv}</button>`).join("")}
      </div>
      <div class="note" style="margin-top:7px">${needsApproval
        ? `Дороже ${d.gov.approval_hours} ч — сначала одобрение (кнопка на Пульте или /approve ${esc(h.id)})`
        : `Бюджет: ${fmtN(d.gov.budget_hours.used, 1)}/${fmtN(d.gov.budget_hours.limit, 0)} ч · запуск идёт через штатный dispatch`}</div>
    </div>` : ""}
    ${h.note ? `<div class="note">${esc(h.note)}</div>` : ""}
  `);
  setSheetTitle("Гипотеза");
}

/* --------------------------------------------------------------- агент */
function openAgent(id) {
  const d = S.data;
  const a = d && d.crew.agents.find((x) => x.id === id);
  if (!a) return;
  const l = d.crew.leaders.find((x) => x.agent === id);
  const msgs = d.crew.chat.filter((m) => m.agent === id).slice(-4).reverse();
  openSheet(`
    <div style="display:flex;gap:13px;align-items:center">
      ${avatar(id, "lg")}
      <div><b style="font-size:18.5px">${esc(a.name)}</b><div class="note">${esc(a.zone)}</div></div>
    </div>
    ${l ? `<div class="kpi-grid" style="width:100%">
      <div class="kpi"><b>${Math.round(l.rate * 100)}%</b><span>точность ставок</span></div>
      <div class="kpi"><b>${l.bets}</b><span>ставок сделано</span></div>
      <div class="kpi"><b>${l.brier}</b><span>Brier-скор</span></div>
      <div class="kpi"><b>${l.streak > 0 ? "🔥 " + l.streak : l.streak < 0 ? "❄ " + (-l.streak) : "—"}</b><span>серия попаданий</span></div>
    </div>` : ""}
    <div class="card-label">Последние реплики</div>
    ${msgs.length ? msgs.map((m) => `
      <div class="msg"><div class="m-body">
        <div class="m-head"><span class="m-time">${timeHM(m.ts)}</span></div>
        <div class="m-text">${esc(m.text)}</div>
      </div></div>`).join("") : `<div class="empty">Пока молчит</div>`}
  `);
  setSheetTitle("Профиль агента");
}

/* --------------------------------------------------------------- вердикт */
function openVerdict(id) {
  const d = S.data;
  const v = d && d.verdicts.find((x) => x.id === id);
  if (!v) return;
  const report = [
    `Вердикт ${v.hid} — ${KIND_RU[v.kind]}`,
    `Что проверяли: ${v.checked}`,
    v.forecast != null ? `Прогноз: ${fmtPct(v.forecast)} ${v.unit}` : null,
    v.actual != null ? `Факт: ${fmtPct(v.actual)} ${v.unit}` : null,
    v.deviation != null ? `Отклонение: ${fmtPct(v.deviation)}% от прогноза` : null,
    `Seeds: ${v.seeds_pass}/${v.seeds_total}${v.sigma != null ? `, σ=${v.sigma}` : ""}`,
    `GPU-часов: ${v.gpu_hours}`,
    `Что меняется: ${v.changes}`,
    `Следующее действие: ${v.next}`,
    v.patent ? `Патент: ${v.patent.title} (${v.patent.claims} пунктов формулы, ${v.patent.status})` : null,
  ].filter(Boolean).join("\n");
  window.__lastReport = report;
  openSheet(`
    <div class="hyp-row1">
      <b class="hid">${esc(v.hid)}</b>
      <span class="stamp ${v.kind}">${KIND_RU[v.kind]}</span>
      <span class="chip dim" style="margin-left:auto">${agoTxt(v.ts)}</span>
    </div>
    <h3 style="font-size:17.5px;margin:9px 0;line-height:1.35">${esc(v.title)}</h3>
    ${dumbbellHTML(v)}
    <div class="divider"></div>
    <div><div class="card-label">Что проверяли</div><div style="font-size:15px">${esc(v.checked)}</div></div>
    ${v.forecast != null ? `<div><div class="card-label">Числа</div>
      <div class="kv">
        <div class="kvv"><b>${fmtPct(v.forecast, 0)}</b><span>прогноз</span></div>
        <div class="kvv"><b>${fmtPct(v.actual, 0)}</b><span>факт</span></div>
        <div class="kvv"><b class="${Math.abs(v.deviation) > 40 ? "delta-pos" : "delta-neg"}">${fmtPct(v.deviation, 0)}</b><span>отклонение</span></div>
        <div class="kvv"><b>${v.seeds_pass}/${v.seeds_total}</b><span>seeds</span></div>
        <div class="kvv"><b>σ ${v.sigma ?? "—"}</b><span>разброс</span></div>
        <div class="kvv"><b>${fmtN(v.gpu_hours, 1)}</b><span>GPU-ч</span></div>
      </div></div>` : ""}
    ${v.changes ? `<div><div class="card-label">Что меняется</div><div style="font-size:15px">${esc(v.changes)}</div></div>` : ""}
    ${v.next ? `<div><div class="card-label">Следующее действие</div><div style="font-size:15px">${esc(v.next)}</div></div>` : ""}
    ${v.patent ? `
      <div class="patent-box">
        <div class="card-label" style="color:var(--cyan)">Проект патентной заявки · ${esc(v.patent.status)}</div>
        <div style="font-size:15px;font-weight:600;line-height:1.4">${esc(v.patent.title)}</div>
        <div class="note" style="margin-top:5px">${v.patent.claims} пункта формулы. Коммерческий потенциал: ${Math.round(v.commercial * 100)}%.</div>
      </div>` : `<div class="note">Коммерческий потенциал: ${Math.round((v.commercial || 0) * 100)}%.</div>`}
    <div class="split">
      <button class="btn primary block" data-act="share-report">↗ Экспорт отчёта</button>
      <button class="btn block" data-act="copy-report">⧉ Копировать</button>
    </div>
  `);
  setSheetTitle("Вердикт");
}

/* ============================================================================
   ВИЗАРД ИДЕИ
   ========================================================================== */
const W_DEFAULT = {
  step: 1, text: "", metric: "val loss", forecast: -15, early_pct: 2, hours: 2,
  signals: 3, novelty: 0.7, standard: 0.5, money: 0.4, decidability: 0.7,
  buyer: "", check: null, result: null,
};
const MECH = ["если", "то", "потому", "механизм", "вызывает", "предсказывает", "коррелирует", "приводит", "когда"];
const BANNED = ["перспективно", "многообещающе", "возможно улучшение", "выглядит интересно", "promising"];

function computePiJS(w) {
  const scale = { 0: 0, 1: 0, 2: 0, 3: 0.5, 4: 0.67, 5: 0.84 };
  const S_ = scale[w.signals] ?? 1;
  const E = Math.max(0, Math.min(1, 1 - (Math.max(0.5, w.early_pct) - 1) / 9));
  const parts = { S: 0.22 * S_, N: 0.16 * w.novelty, E: 0.12 * E, Q: 0.14 * w.standard, M: 0.14 * w.money, D: 0.22 * w.decidability };
  const pi = Object.values(parts).reduce((a, b) => a + b, 0);
  return { pi, ppi: pi / Math.max(0.25, w.hours), parts };
}

function ideaQuality(text) {
  const t = (text || "").toLowerCase();
  const checks = [
    { ok: (text || "").length > 60, okTxt: "механизм виден", badTxt: "слишком коротко: механизма не видно" },
    { ok: /\d/.test(text || ""), okTxt: "есть числа — PASS/FAIL сформулировать можно", badTxt: "нет чисел: критерий PASS/FAIL не сформулировать" },
    { ok: MECH.some((m) => t.includes(m)), okTxt: "есть причинная связка «если → то»", badTxt: "нет связки «если X, то Y»" },
    { ok: !BANNED.some((m) => t.includes(m)), okTxt: "язык проверяемый, без «многообещающе»", badTxt: "запрещённые слова — вердикт такое не примет" },
  ];
  const score = checks.filter((c) => c.ok).length;
  return { checks, score };
}

function openWizard() {
  S.wizard = Object.assign({}, W_DEFAULT);
  renderWizard();
}

function renderWizard() {
  const w = S.wizard;
  if (!w) return;
  const d = S.data;
  const stepsBar = `<div class="wiz-steps">${[1, 2, 3, 4].map((i) => `<i class="${w.step >= i ? "on" : ""}"></i>`).join("")}</div>`;
  let body = "";

  if (w.step === 1) {
    const qt = ideaQuality(w.text);
    body = `
      ${stepsBar}
      <div>
        <div class="field">
          <label>Суть механизма</label>
          <textarea class="tin area" id="wz-text" placeholder="Если к 2% обучения стабильность знаков градиентов выходит на плато, то раннее прореживание по норме весов сохраняет качество — экономия 20% compute на CIFAR-10." maxlength="600">${esc(w.text)}</textarea>
          <div class="qmeter">${qt.checks.map((c, i) => `<i class="${i < qt.score ? (qt.score < 3 ? "mid" : "on") : ""}"></i>`).join("")}</div>
          <div class="qlist">${qt.checks.map((c) => `<div class="${c.ok ? "ok" : "bad"}"><span>${c.ok ? "✓" : "!"}</span>${c.ok ? c.okTxt : c.badTxt}</div>`).join("")}</div>
        </div>
      </div>
      <div class="note">Проверим формулировку и дубликаты — до экипажа.</div>
      <button class="btn primary block" id="wz-next" ${w.text.trim().length < 20 ? "disabled" : ""}>Далее →</button>`;
  } else if (w.step === 2) {
    const pi = computePiJS(w);
    const queued = d ? d.queue.filter((h) => h.status === "queued") : [];
    const pos = 1 + queued.filter((h) => h.ppi > pi.ppi).length;
    const bin = pi.ppi <= 0 ? "—" : w.hours <= 4 ? "P1" : w.hours <= 12 ? "P2" : w.hours <= 48 ? "P3" : "P4";
    const maxPart = Math.max(...Object.values(pi.parts), 0.01);
    body = `
      ${stepsBar}
      <div class="field">
        <label>Метрика эффекта</label>
        <select class="sel" id="wz-metric">
          ${["val loss", "точность avg", "compute", "шаг перехода grokking", "sharpness"].map((m) =>
            `<option value="${m}" ${w.metric === m ? "selected" : ""}>${m}</option>`).join("")}
        </select>
      </div>
      <div class="field">
        <label>Прогноз эффекта (числом, зафиксируется до запуска)</label>
        <input class="tin mono" type="number" id="wz-forecast" value="${w.forecast}" step="1">
      </div>
      <div class="field">
        <label>Ранность: на каком % обучения виден признак — <output>${w.early_pct}%</output></label>
        <div class="slider-row"><input type="range" id="wz-early" min="0.5" max="10" step="0.5" value="${w.early_pct}"><output class="mono">${w.early_pct}%</output></div>
        <div class="note">1% хода — максимум веса E; 10% и позже — ноль.</div>
      </div>
      <div class="field">
        <label>Оценка GPU-часов</label>
        <div class="slider-row"><input type="range" id="wz-hours" min="0.25" max="16" step="0.25" value="${w.hours}"><output class="mono">${fmtN(w.hours, 2)} ч · ${bin}</output></div>
      </div>
      <div class="field">
        <label>Независимых сигналов: ${w.signals}</label>
        <div class="seg" style="margin-top:2px">
          ${[0, 1, 2, 3, 4, 5, 6].map((n) => `<button class="${w.signals === n ? "on" : ""}" data-act="wz-signals" data-v="${n}">${n}</button>`).join("")}
        </div>
        <div class="note">Меньше 3 независимых — S=0; зависимые считаются одним.</div>
      </div>
      ${[["novelty", "Новизна (публикационный gap)", w.novelty], ["standard", "Шанс стать стандартом", w.standard], ["money", "Коммерческий потенциал", w.money], ["decidability", "Однозначность PASS/FAIL", w.decidability]].map(([k, lbl, v]) => `
        <div class="field">
          <label>${lbl}</label>
          <div class="slider-row"><input type="range" data-wz="${k}" min="0" max="1" step="0.05" value="${v}"><output class="mono">${Math.round(v * 100)}%</output></div>
        </div>`).join("")}
      <div class="field">
        <label>Потенциальный покупатель / рынок</label>
        <input class="tin" id="wz-buyer" placeholder="кто заплатит: студии с своими моделями, экономия GPU-ч…" value="${esc(w.buyer)}">
      </div>
      <div class="card" style="background:var(--card2)">
        <div class="card-label">Симуляция приоритета · та же формула, что решает очередь</div>
        <div class="pi-parts">
          ${Object.entries(pi.parts).map(([k, v]) => `
            <div class="pi-part"><span class="mono" style="font-weight:700">${k}</span>
              <span class="pp-bar"><i style="width:${v / maxPart * 100}%"></i></span><b>${v.toFixed(3)}</b></div>`).join("")}
        </div>
        <div class="kv">
          <div class="kvv"><b>${pi.pi.toFixed(3)}</b><span>PI</span></div>
          <div class="kvv"><b style="color:var(--ok)">${pi.ppi.toFixed(2)}</b><span>PPI оч/ч</span></div>
          <div class="kvv"><b>${pos}</b><span>место в очереди из ${queued.length + 1}</span></div>
        </div>
      </div>
      <div class="split">
        <button class="btn block" id="wz-back" data-act="wz-back">← Назад</button>
        <button class="btn primary block" id="wz-next">Далее →</button>
      </div>`;
  } else if (w.step === 3) {
    body = `
      ${stepsBar}
      <div class="card-label" style="margin-top:0">Проверка на дубликаты и качество формулировки</div>
      <button class="btn primary block" id="wz-check" ${w.check ? "disabled" : ""}>${w.check ? "✓ Проверено" : "Проверить идею"}</button>
      ${!w.check ? `` : `
        ${w.check.matches.length ? w.check.matches.map((m) => `
          <div class="dup-row">
            <div class="dup-sim"><b class="mono" style="color:${m.sim > 0.45 ? "var(--err)" : m.sim > 0.25 ? "var(--warn)" : "var(--tx2)"}">${Math.round(m.sim * 100)}%</b><span>сходство</span></div>
            <div style="flex:1;min-width:0">
              <div style="font-size:14.5px;font-weight:600;line-height:1.35">${esc(m.title)}</div>
              <div class="note">${esc(m.why)} · <span class="mono">${esc(m.id)}</span></div>
            </div>
          </div>`).join("") : `<div class="banner" style="background:var(--ok-soft);border:1px solid color-mix(in srgb,var(--ok) 40%,transparent);color:var(--ok)">✓ Прямых дублей нет — идея проходит в разбор экипажа</div>`}
        ${w.check.notes.length ? `<div class="qlist">${w.check.notes.map((n) => `<div class="bad"><span>!</span>${esc(n)}</div>`).join("")}</div>` : ""}
        ${w.check.matches.some((m) => m.sim > 0.45) ? `<div class="banner warn">${ICO.warn}<span>Похоже на дубль — экипаж снимет идею до GPU. Заостри отличие механизма.</span></div>` : ""}
      `}
      <div class="split">
        <button class="btn block" id="wz-back" data-act="wz-back">← Назад</button>
        <button class="btn primary block" id="wz-submit" ${w.check ? "" : "disabled"}>Отправить экипажу</button>
      </div>`;
  } else {
    const r = w.result;
    if (!r.ok) {
      // живой контур отклонил идею (обычно дубль) — показываем причину
      body = `
        ${stepsBar}
        <div class="banner warn">${ICO.warn}<span>Идея не принята: ${esc(r.reason || "причина в логе идей")}</span></div>
        <div class="note">Дубликаты отклоняются на входе — это защита бюджета. Заостри отличие механизма и попробуй снова.</div>
        <div class="split">
          <button class="btn block" data-act="wz-back">← Исправить</button>
          <button class="btn primary block" data-act="sheet-close">Закрыть</button>
        </div>`;
    } else {
      body = `
        ${stepsBar}
        <div class="success-check">
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none"><path d="m5 13 4.2 4.2L19 7.4" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div style="text-align:center">
          <b style="font-size:17px">${esc(r.inbox_id || "Идея")} → inbox</b>
          <div class="note" style="margin-top:4px">Экипаж разберёт на ближайшем тике: kill-стадия, PI/PPI, очередь или лог отклонённых.</div>
        </div>
        <div class="kpi-grid">
          <div class="kpi"><b>${(r.estimate && r.estimate.signals) ?? "—"}</b><span>оценка сигналов</span></div>
          <div class="kpi"><b>${fmtN((r.estimate && r.estimate.hours) || w.hours, 1)} ч</b><span>оценка GPU</span></div>
        </div>
        <div class="note mono">${esc(r.next || "python tools/rg.py ideas")}</div>
        <div class="split">
          <button class="btn block" data-act="sheet-close">Закрыть</button>
          <button class="btn primary block" data-act="wz-goto" data-nav="crew">Чат экипажа</button>
        </div>`;
    }
  }

  openSheet(`<div id="wiz">${body}</div>`, "full");
  setSheetTitle(w.step >= 4 ? "Идея принята" : "Новая идея · шаг " + Math.min(w.step, 3) + " из 3");
  bindWizard();
}

function bindWizard() {
  const w = S.wizard;
  if (!w) return;
  const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };
  on("#wz-text", "input", (e) => { w.text = e.target.value; refreshWizardLight(); });
  on("#wz-next", "click", () => { haptic(); if (w.step === 1) { w.step = 2; renderWizard(); } else if (w.step === 2) { w.step = 3; renderWizard(); } });
  on("#wz-back", "click", () => { haptic(); w.step--; renderWizard(); });
  on("#wz-metric", "change", (e) => { w.metric = e.target.value; });
  on("#wz-forecast", "change", (e) => { w.forecast = +e.target.value || 0; });
  on("#wz-early", "input", (e) => { w.early_pct = +e.target.value; syncOut(e.target, w.early_pct + "%"); updateSim(); });
  on("#wz-hours", "input", (e) => { w.hours = +e.target.value; updateSim(); });
  on("#wz-buyer", "input", (e) => { w.buyer = e.target.value; });
  on("#wz-check", "click", async () => {
    const btn = $("#wz-check");
    if (btn) { btn.disabled = true; btn.textContent = "Проверяю…"; }
    const j = await api({ type: "idea_check", text: w.text, hours: w.hours });
    if (j.ok) { w.check = { matches: j.matches || [], notes: j.notes || [] }; haptic("ok"); renderWizard(); }
    else if (btn) { btn.disabled = false; btn.textContent = "Проверить идею"; }
  });
  on("#wz-submit", "click", async () => {
    const btn = $("#wz-submit");
    if (btn) { btn.disabled = true; btn.textContent = "Отправляю…"; }
    const j = await api({ type: "submit_idea", text: w.text, hours: w.hours, early_pct: w.early_pct,
      signals: w.signals, novelty: w.novelty, standard: w.standard, money: w.money,
      decidability: w.decidability, forecast: `${fmtPct(w.forecast)} ${w.metric}` });
    w.result = j; w.step = 4; haptic(j.ok ? "ok" : "warn");
    await refresh(); renderWizard();
  });
  $$("#sheet-body input[data-wz]").forEach((el) => el.addEventListener("input", (e) => {
    w[e.target.dataset.wz] = +e.target.value;
    const out = e.target.parentElement.querySelector("output");
    if (out) out.textContent = Math.round(e.target.value * 100) + "%";
    updateSim();
  }));
  function syncOut(input, txt) {
    const out = input.parentElement.querySelector("output");
    if (out) out.textContent = txt;
  }
  function updateSim() {
    // лёгкое обновление блока симуляции без перерисовки всего шага
    const pi = computePiJS(w);
    const b = $("#sheet-body .card");
    if (!b) return;
    const kvs = b.querySelectorAll(".kvv b");
    if (kvs.length >= 3) {
      kvs[0].textContent = pi.pi.toFixed(3);
      kvs[1].textContent = pi.ppi.toFixed(2);
    }
    const bins = $("#wz-hours") ? $("#wz-hours").parentElement.querySelector("output") : null;
    if (bins) bins.textContent = `${fmtN(w.hours, 2)} ч · ${w.hours <= 4 ? "P1" : w.hours <= 12 ? "P2" : "P3"}`;
    const maxPart = Math.max(...Object.values(pi.parts), 0.01);
    b.querySelectorAll(".pi-part").forEach((row, i) => {
      const k = Object.keys(pi.parts)[i];
      row.querySelector("i").style.width = pi.parts[k] / maxPart * 100 + "%";
      row.querySelector("b").textContent = pi.parts[k].toFixed(3);
    });
  }
  function refreshWizardLight() {
    const qt = ideaQuality(w.text);
    const meter = $("#sheet-body .qmeter");
    if (meter) meter.innerHTML = qt.checks.map((c, i) => `<i class="${i < qt.score ? (qt.score < 3 ? "mid" : "on") : ""}"></i>`).join("");
    const list = $("#sheet-body .qlist");
    if (list) list.innerHTML = qt.checks.map((c) => `<div class="${c.ok ? "ok" : "bad"}"><span>${c.ok ? "✓" : "!"}</span>${c.ok ? c.okTxt : c.badTxt}</div>`).join("");
    const nx = $("#wz-next");
    if (nx) nx.disabled = w.text.trim().length < 20;
  }
}

/* ============================================================================
   РОУТЕР И РЕНДЕР
   ========================================================================== */
const SCREENS = { dash: screenDash, pipe: screenPipe, live: screenLive, crew: screenCrew, verdicts: screenVerdicts };

function renderScreen() {
  const scr = $("#screen");
  const keepScroll = window.scrollY;
  scr.innerHTML = (S.offline ? `<div class="banner err">${ICO.warn}<span>Нет связи с лабораторией · повтор каждые 4 с</span></div>` : "") + SCREENS[S.route]();
  scr.classList.toggle("offline-pad", S.offline);
  window.scrollTo(0, keepScroll);
  $$(".tab").forEach((t) => t.classList.toggle("on", t.dataset.nav === S.route));
  $("#fab").hidden = S.route !== "pipe";
  // бейджи
  const d = S.data;
  const bp = $("#badge-pipe"), bc = $("#badge-crew");
  if (d) {
    const qn = d.stats.queue_len;
    bp.hidden = !qn; bp.textContent = qn;
    const seen = +localStorage.getItem("rg_seenChat") || 0;
    const unread = Math.max(0, d.crew.chat.length - seen);
    bc.hidden = !(unread && S.route !== "crew"); bc.textContent = unread > 9 ? "9+" : unread;
  }
  if (S.route === "live") requestAnimationFrame(drawLiveCharts);
  if (S.route === "crew" && S.sub.crew === "chat") {
    const list = $("#chat-list");
    if (list) requestAnimationFrame(() => { if (S.chatStick) list.scrollIntoView({ block: "end" }); });
  } else if (S.route === "live") { /* уже отрисован */ }
}

function go(route) {
  if (S.route === route) return;
  S.route = route;
  haptic("select");
  if (route === "crew") { try { localStorage.setItem("rg_seenChat", String(S.data ? S.data.crew.chat.length : 0)); } catch (e) {} }
  renderScreen();
  window.scrollTo({ top: 0 });
}

/* --------------------------------------------------------------- BackButton */
function updateBackButton() {
  if (!tg || !tg.BackButton) return;
  try {
    if (S.sheet) tg.BackButton.show();
    else if (S.route !== "dash") tg.BackButton.show();
    else tg.BackButton.hide();
  } catch (e) {}
}

/* ============================================================================
   СОБЫТИЯ
   ========================================================================== */
document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const holdBtn = el.closest("[data-hold]");
  if (holdBtn) { if (!holdBtn._holdDone) return; holdBtn._holdDone = false; }
  const act = el.dataset.act;
  const d = S.data;

  switch (act) {
    case "nav": go(el.dataset.nav); break;
    case "pipe-tab": S.sub.pipe = el.dataset.v; haptic("select"); renderScreen(); break;
    case "crew-tab": S.sub.crew = el.dataset.v; haptic("select"); renderScreen(); break;
    case "metric": S.metric = el.dataset.v; haptic("select"); renderScreen(); break;
    case "logy": S.logY = !S.logY; haptic(); renderScreen(); break;
    case "cmp": {
      const k = el.dataset.k;
      if (S.compare.has(k)) S.compare.delete(k); else S.compare.add(k);
      haptic("select"); renderScreen(); break;
    }
    case "vfilter": S.verdictFilter = el.dataset.v; haptic("select"); renderScreen(); break;
    case "sheet-close": closeSheet(); break;
    case "open-hyp": haptic(); openHyp(el.dataset.hid); break;
    case "open-verdict": haptic(); openVerdict(el.dataset.id); break;
    case "agent": haptic(); openAgent(el.dataset.id); break;
    case "wizard": haptic(); openWizard(); break;
    case "wz-signals": S.wizard.signals = +el.dataset.v; haptic("select"); renderWizard(); break;
    case "wz-back": break;
    case "wz-goto": S.wizard = null; closeSheet(true); go(el.dataset.nav); renderScreen(); break;

    case "pause": {
      haptic("warn");
      const j = await api({ type: "pause" });
      if (j.ok) { toast("Контур на паузе: чекпойнт снят, GPU освобождается", "ok"); refresh(); }
      break;
    }
    case "resume": {
      haptic();
      const j = await api({ type: "resume" });
      if (j.ok) { toast("Автозапуск возвращён", "ok"); refresh(); }
      break;
    }
    case "kill": {
      haptic("warn");
      const j = await api({ type: "kill_task", hid: el.dataset.hid });
      if (j.ok) { toast("Задача снята с GPU. Некролог — в чате экипажа", "ok"); refresh(); }
      break;
    }
    case "approve": {
      const ok = el.dataset.ok === "1", id = el.dataset.hid;
      if (!ok && S.pendingReject !== id) { S.pendingReject = id; haptic("warn"); renderScreen(); setTimeout(() => { if (S.pendingReject === id) { S.pendingReject = null; renderScreen(); } }, 3200); break; }
      haptic(ok ? "ok" : "warn");
      const j = await api({ type: "approve", hid: id, ok });
      if (j.ok) { S.pendingReject = null; toast(ok ? "Прогон одобрен — встаёт в план" : "Дорогой прогон отклонён, гипотеза закрыта", ok ? "ok" : ""); refresh(); }
      break;
    }
    case "run-check": {
      haptic();
      el.disabled = true; el.textContent = "Гейт…";
      const j = await api({ type: "run_check", hid: el.dataset.hid });
      const box = $("#gate-result");
      if (box && j && typeof j === "object") {
        const problems = j.problems || [];
        box.innerHTML = problems.length
          ? `<div class="note" style="margin-bottom:5px">Гейт: запуск запрещён, ${problems.length} замеч.</div>` +
            problems.map((p) => `<div class="bad"><span>!</span>${esc(p)}</div>`).join("")
          : `<div class="ok"><span>✓</span>гейт пройден: запуск разрешён</div>`;
      } else if (box) box.innerHTML = `<div class="note">Гейт не дал ответа</div>`;
      el.disabled = false; el.textContent = "Проверить гейтом";
      await refresh();
      break;
    }
    case "run-level": {
      haptic();
      const j = await api({ type: "run_level", hid: el.dataset.hid, level: el.dataset.lv });
      if (j.ok) { toast(`${el.dataset.lv}: запуск пошёл через dispatch`, "ok"); }
      else if (j.approval) toast("Дороже порога — нужно одобрение человека", "warn");
      else toast(j.reason ? `Гейт: ${String(j.reason).slice(0, 90)}` : "Гейт не пройден", "err");
      await refresh(); if (S.sheet) openHyp(el.dataset.hid);
      break;
    }
    case "share-report": {
      const text = window.__lastReport || "";
      if (navigator.share) { try { await navigator.share({ title: "Вердикт researchagen", text }); haptic("ok"); } catch (err) { } }
      else { try { await navigator.clipboard.writeText(text); toast("Отчёт скопирован в буфер", "ok"); } catch (err) { toast("Экспорт недоступен в этом окружении", "err"); } }
      break;
    }
    case "copy-report": {
      const text = window.__lastReport || "";
      try { await navigator.clipboard.writeText(text); toast("Скопировано", "ok"); haptic("ok"); } catch (err) { toast("Буфер недоступен", "err"); }
      break;
    }
  }
});

/* удержание для критических кнопок: обычный тап не срабатывает */
document.addEventListener("pointerdown", (e) => {
  const btn = e.target.closest("[data-hold]");
  if (!btn) return;
  haptic();
  btn.classList.add("holding");
  const fill = btn.querySelector(".hold-fill");
  if (fill) { fill.style.transition = "width 1.15s linear"; requestAnimationFrame(() => { fill.style.width = "100%"; }); }
  btn._holdTimer = setTimeout(() => {
    btn._holdDone = true;               // разрешаем один клик-выстрел
    haptic("warn");
    if (fill) { fill.style.transition = "width .2s"; fill.style.width = "0%"; }
    btn.classList.remove("holding");
    btn.click();
  }, 1150);
  const cancel = () => {
    clearTimeout(btn._holdTimer);
    btn.classList.remove("holding");
    if (fill) { fill.style.transition = "width .15s"; fill.style.width = "0%"; }
    btn.removeEventListener("pointerup", cancel);
    btn.removeEventListener("pointerleave", cancel);
  };
  btn.addEventListener("pointerup", cancel);
  btn.addEventListener("pointerleave", cancel);
});

/* чат: прилипание к низу, пока пользователь не уехал вверх */
document.addEventListener("scroll", () => {
  const d = document.documentElement;
  S.chatStick = (d.scrollHeight - window.scrollY - window.innerHeight) < 140;
}, { passive: true });

/* ============================================================================
   СТАРТ
   ========================================================================== */
function initTG() {
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    // Дизайн всегда тёмный: независимо от темы Telegram остаёмся в dark.
    if (tg.setHeaderColor) tg.setHeaderColor("#05070e");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#05070e");
    if (tg.BackButton) tg.BackButton.onClick(() => {
      if (S.sheet) { if (S.wizard) S.wizard = null; closeSheet(); }
      else go("dash");
    });
    if (tg.MainButton) tg.MainButton.onClick(() => {
      const nx = $("#wz-next"), sub = $("#wz-submit");
      if (sub && !sub.disabled) sub.click();
      else if (nx && !nx.disabled) nx.click();
    });
    const u = tg.initDataUnsafe?.user;
    if (u && u.first_name) window.__userName = u.first_name;
  } catch (e) { /* среда Telegram может быть урезанной */ }
}

async function boot() {
  initTG();
  renderScreen();
  await refresh();
  setInterval(refresh, 4000);
  setInterval(renderHeader, 1000);
}

boot();
