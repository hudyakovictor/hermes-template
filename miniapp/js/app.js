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
const MODE_RU = { discover: "поиск идей", triage: "отбор", testing: "прогон", analyze: "разбор", paused: "пауза" };
const CHECK_RU = { pass: "✓", fail: "✕", run: "↻", wait: "•" };
const BIN_COLOR = { P1: "ok", P2: "acc", P3: "warn", P4: "err" };

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
  el.innerHTML = `<span>${kind === "ok" ? "✓" : kind === "err" ? "✕" : "ℹ"}</span><span>${esc(text)}</span>`;
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
    d.mode, d.gov.mode, d.gov.autostart,
    d.gov.budget_hours.used.toFixed(1), d.gov.budget_tasks.used,
    Math.round(d.gpu.util / 5), Math.round(d.gpu.temp / 2), d.gpu.used_gb.toFixed(1),
    c && c.hid, c && Math.round((c.progress || 0) * 200), c && c.eta_min,
    c && c.loss_now && c.loss_now.toFixed(3), c && c.status,
    d.approvals.length, d.crew.chat.length, d.crew.chat.length ? d.crew.chat.at(-1).id : 0,
    d.queue.map((h) => [h.id, h.status, h.ppi, h.checks_pass, h.level].join(":")).join("|"),
    d.verdicts.length, d.crew.remarks.map((x) => x.status).join(),
    JSON.stringify(d.user_votes || {}),
    (d.crew.chat.filter((m) => m.dispute).map((m) => m.dispute.options.map((o) => o.votes).join()) || []).join(),
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
  const d = S.data;
  const conn = $("#conn"), connTxt = $("#conn-txt"), upd = $("#upd"), mode = $("#mode-line");
  if (S.offline) { conn.className = "conn off"; connTxt.textContent = "нет связи"; }
  else if (d && d.mode === "demo") { conn.className = "conn demo"; connTxt.textContent = "демо-симуляция"; }
  else { conn.className = "conn"; connTxt.textContent = "онлайн"; }
  if (!d) { mode.textContent = "…"; upd.textContent = "—"; return; }
  const g = d.gov;
  mode.textContent = `${MODE_RU[g.mode] || g.mode} · бюджет ${fmtN(g.budget_hours.used, 1)}/${g.budget_hours.limit} ч`;
  const s = Math.round((Date.now() - S.lastFetchOk) / 1000);
  upd.textContent = s < 4 ? "только что" : s + " с назад";
}

/* ============================================================================
   ЭКРАН: ПУЛЬТ
   ========================================================================== */
function screenDash() {
  const d = S.data;
  if (!d) return skeletonHTML();
  const g = d.gov, gpu = d.gpu, cur = d.current, st = d.stats;
  const util = gpu.util || 0;
  const free = Math.max(0, gpu.total_gb - gpu.used_gb);
  const ringCol = util > 5 ? "var(--acc)" : "var(--tx3)";
  const C = 2 * Math.PI * 44;

  const hero = cur ? `
    <section class="card task-hero">
      <div class="th-top">
        <span class="chip ${cur.status === "paused" ? "warn" : "acc"}">${cur.status === "paused" ? "⏸ пауза" : "считается"}</span>
        <span class="chip mono">${esc(cur.hid)}</span>
        <span class="chip violet">${cur.level}</span>
        <span class="chip dim">сид ${cur.seed}/${cur.seeds_total}</span>
      </div>
      <h3>${esc(cur.title)}</h3>
      <div class="th-sub">шаг <b class="mono num">${(cur.steps / 1000).toFixed(1)}k</b> из ${(cur.steps_total / 1000).toFixed(0)}k · прошло ${fmtMin(cur.elapsed_min)} · осталось ≈ ${fmtMin(cur.eta_min)}</div>
      ${progressHTML(cur.progress, cur.status === "paused")}
      <div class="progress-row"><span>прогон</span><span>${Math.round(cur.progress * 100)}%</span><span>базовая кривая</span></div>
      <div class="kv">
        <div class="kvv"><b class="mono">${cur.loss_now != null ? cur.loss_now.toFixed(4) : "—"}</b><span>loss сейчас</span></div>
        <div class="kvv"><b class="mono">${cur.base_now != null ? cur.base_now.toFixed(4) : "—"}</b><span>базовая</span></div>
        <div class="kvv"><b class="${cur.loss_now && cur.base_now ? (cur.loss_now < cur.base_now ? "delta-neg" : "delta-pos") : ""}">${cur.loss_now && cur.base_now ? fmtPct((cur.loss_now - cur.base_now) / cur.base_now * 100) : "—"}</b><span>к базе</span></div>
      </div>
      <div class="split">
        ${cur.status === "paused"
          ? `<button class="btn primary block" data-act="resume">▸ Продолжить</button>`
          : `<button class="btn block" data-act="pause">⏸ Пауза (с чекпойнтом)</button>`}
        <button class="btn danger hold-btn block" data-act="kill" data-hid="${esc(cur.hid)}" data-hold>
          <span class="hold-fill"></span>
          <span class="btn-inner">⏺ Снять <small style="font-weight:600;opacity:.75">удержать</small></span>
        </button>
      </div>
    </section>` : `
    <section class="card">
      <div class="card-label">GPU свободен</div>
      <div class="empty"><div class="e-ico">🛰</div>Прогонов нет. ${g.autostart ? "Диспетчер ждёт очередь (каждые 2 мин)." : "Автозапуск остановлен человеком."}</div>
      ${g.autostart ? "" : `<button class="btn primary block" data-act="resume">▸ Вернуть автозапуск</button>`}
    </section>`;

  const approvals = d.approvals.length ? `
    <section class="card approve-card">
      <div class="card-label"><span>Ждёт твоего решения</span><span class="r">${d.approvals.length}</span></div>
      ${d.approvals.map((a) => `
        <div style="margin-bottom:10px">
          <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
            <b class="mono" style="font-size:14.5px">${esc(a.hid)}</b>
            <span class="chip warn">${a.level} · ${fmtN(a.hours, 1)} GPU-ч</span>
            <span class="chip dim">PPI ${fmtN(a.ppi)}</span>
          </div>
          <div style="font-size:15px;margin:6px 0 8px">${esc(a.title)}</div>
          <div class="note">Порог подтверждения: ${g.approval_hours} GPU-ч. ${esc(a.note || "")}</div>
          <div class="split" style="margin-top:9px">
            <button class="btn ok sm block" data-act="approve" data-id="${esc(a.id)}" data-ok="1">✓ Одобрить</button>
            <button class="btn danger sm block ${S.pendingReject === a.id ? "" : "ghost"}" data-act="approve" data-id="${esc(a.id)}" data-ok="0">${S.pendingReject === a.id ? "Точно отклонить?" : "Отклонить"}</button>
          </div>
        </div>`).join("")}
    </section>` : "";

  const nextQ = d.queue.find((h) => h.status === "queued");
  const next = nextQ ? `
    <section class="rowlink" data-act="open-hyp" data-hid="${esc(nextQ.id)}">
      <span class="chip acc">дальше</span>
      <div style="min-width:0;flex:1">
        <div style="font-size:15px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(nextQ.title)}</div>
        <div class="note mono">${esc(nextQ.id)} · PPI ${fmtN(nextQ.ppi)} · ${fmtN(nextQ.est_hours, 1)} ч</div>
      </div>
      <span class="rl-chev">›</span>
    </section>` : "";

  return `
    ${hero}
    ${approvals}
    <section class="card">
      <div class="card-label"><span>${esc(gpu.name)}</span><span class="r">автозапуск: ${g.autostart ? "вкл" : "выкл"}</span></div>
      <div class="gpu-grid">
        <div class="ring">
          <svg width="104" height="104" viewBox="0 0 104 104">
            <defs>
              <linearGradient id="ringg" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#5b8cff"/><stop offset="1" stop-color="#8f7bff"/>
              </linearGradient>
            </defs>
            <circle cx="52" cy="52" r="44" fill="none" stroke="var(--card3)" stroke-width="9"/>
            <circle cx="52" cy="52" r="44" fill="none" stroke="${util > 5 ? "url(#ringg)" : "var(--tx3)"}" stroke-width="9" stroke-linecap="round"
              stroke-dasharray="${(C * util / 100).toFixed(1)} ${C.toFixed(1)}"/>
          </svg>
          <span class="val"><div><b>${util}%</b><i>util</i></div></span>
        </div>
        <div>
          <div class="meter">
            <div class="meter-head"><span>VRAM</span><b>${fmtN(gpu.used_gb, 1)} / ${fmtN(gpu.total_gb, 1)} ГБ · своб. ${fmtN(free, 1)}</b></div>
            <div class="bar">
              <span class="fill" style="width:${(gpu.used_gb / gpu.total_gb * 100).toFixed(1)}%;background:${free < gpu.critical_free ? "var(--err)" : free < gpu.low_free ? "var(--warn)" : "linear-gradient(90deg,var(--acc),var(--violet))"}"></span>
              <span class="tick warn" style="left:${(1 - gpu.low_free / gpu.total_gb) * 100}%" title="мало"></span>
              <span class="tick crit" style="left:${(1 - gpu.critical_free / gpu.total_gb) * 100}%" title="крит"></span>
            </div>
          </div>
          <div class="meter">
            <div class="meter-head"><span>Температура</span><b>${gpu.temp}°C</b></div>
            <div class="temp-scale"><span class="temp-needle" style="left:calc(${Math.min(100, gpu.temp / 95 * 100).toFixed(1)}% - 2px)"></span></div>
          </div>
        </div>
      </div>
    </section>
    <section class="card">
      <div class="card-label"><span>Суточный бюджет GPU</span><span class="r">${MODE_RU[g.mode] || g.mode}</span></div>
      <div class="kv" style="margin:4px 0 10px">
        <div class="kvv"><b>${fmtN(g.budget_hours.used, 1)}<em> / ${g.budget_hours.limit} ч</em></b><span>потрачено</span></div>
        <div class="kvv"><b class="${g.daily_left_h < 4 ? "delta-pos" : ""}">${fmtN(g.daily_left_h, 1)} ч</b><span>осталось</span></div>
        <div class="kvv"><b>${g.budget_tasks.used}<em> / ${g.budget_tasks.limit}</em></b><span>задач дня</span></div>
      </div>
      <div class="bar" style="height:10px"><span class="fill" style="width:${(g.budget_hours.used / g.budget_hours.limit * 100).toFixed(1)}%;background:${g.budget_hours.used / g.budget_hours.limit > .8 ? "var(--warn)" : "linear-gradient(90deg,var(--acc),var(--violet))"}"></span></div>
      <div class="note" style="margin-top:8px">Вытеснение: при PPI ×${g.preempt_ratio} выше у очереди — прогон прерывается с чекпойнтом.</div>
    </section>
    <section class="kpi-grid">
      <div class="kpi"><b>${st.calibration == null ? "—" : st.calibration + "%"}<em>точность</em></b><span>калибровка прогнозов</span></div>
      <div class="kpi"><b>${st.win_rate}%</b><span>подтверждено вердиктов</span></div>
      <div class="kpi"><b>−${st.gpu_saved_h}<em>GPU-ч</em></b><span>сэкономлено ранними снятиями</span></div>
      <div class="kpi"><b>${st.open_bets}</b><span>открытых ставок экипажа</span></div>
    </section>
    ${next}
    <section class="split">
      <button class="btn primary block" data-act="wizard">＋ Подать идею</button>
      <button class="btn block" data-act="nav" data-nav="crew">💬 Экипаж</button>
    </section>`;
}

/* ============================================================================
   ЭКРАН: КОНВЕЙЕР
   ========================================================================== */
function hypCardHTML(h) {
  const statusChip = h.status === "running" ? `<span class="chip acc">на GPU</span>`
    : h.status === "blocked" ? `<span class="chip err">блок</span>`
    : h.status === "paused_checkpoint" ? `<span class="chip warn">пауза</span>` : "";
  const betIcons = (h.bets.up.length || h.bets.down.length)
    ? `<span class="chip dim">${h.bets.up.length ? "▲" + h.bets.up.length : ""}${h.bets.up.length && h.bets.down.length ? " " : ""}${h.bets.down.length ? "▼" + h.bets.down.length : ""} ставки</span>` : "";
  const mine = h.source === "human" ? `<span class="chip violet">от человека</span>` : "";
  const ppiTop = S.data ? Math.max(...S.data.queue.map((x) => x.ppi || 0)) : 1;
  const ppiCol = h.ppi >= ppiTop * 0.66 ? "var(--ok)" : h.ppi >= ppiTop * 0.33 ? "var(--warn)" : "var(--tx3)";
  return `
  <article class="card hyp-card" data-act="open-hyp" data-hid="${esc(h.id)}">
    <div class="hyp-row1">
      <b class="hid" style="color:${h.status === "running" ? "var(--acc)" : h.status === "blocked" ? "var(--err)" : "var(--tx2)"}">${esc(h.id)}</b>
      <span class="chip ${BIN_COLOR[h.bin] || "dim"}">${h.bin}</span>
      <span class="chip dim">${fmtN(h.est_hours, 1)} ч</span>
      ${statusChip}
      <span class="ppi-badge"><b style="color:${ppiCol}">${fmtN(h.ppi)}</b><span>PPI оч/ч</span></span>
    </div>
    <div class="hyp-title">${esc(h.title)}</div>
    <div class="hyp-meta">
      <span class="chip dim">PI ${fmtN(h.pi)}</span>
      <span class="chip dim">сигналы ${h.signals}</span>
      <span class="chip ${h.checks_pass >= 8 ? "ok" : h.checks_pass >= 5 ? "warn" : "err"}">kill ${h.checks_pass}/8</span>
      ${ladderHTML(h.level)}
      ${mine} ${betIcons}
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
    <div class="note" style="text-align:center">PPI = PI / GPU-час. Корзины: P1 ≤4ч · P2 ≤12ч · P3 ≤48ч · P4 дальше.</div>`;
}

/* ============================================================================
   ЭКРАН: ТЕЛЕМЕТРИЯ
   ========================================================================== */
function screenLive() {
  const d = S.data;
  if (!d) return skeletonHTML();
  const run = d.runs.find((r) => r.status === "running") || d.runs[0];
  const hist = d.history || [];
  const cmpColors = { cur: "#5b8cff", h0: "#8f7bff", h1: "#f5a623", h2: "#38c8e8" };
  const cmpLabels = { cur: "H-041 L1 · сейчас", h0: "R-116 L0", h1: "R-112 L0", h2: "R-108 L1" };

  const hero = run ? `
    <section class="card">
      <div class="run-hero">
        <span class="live-dot"></span>
        <b class="mono">${esc(run.hid)}</b>
        <span class="chip violet">${run.level}</span>
        <span class="chip dim">сид ${run.seed}/${run.seeds_total}</span>
        <span class="chip dim">шаг ${(run.steps_done / 1000).toFixed(1)}k</span>
        <span class="chip ${run.eta_min < 10 ? "ok" : "dim"}">ETA ${fmtMin(run.eta_min)}</span>
      </div>
      <div style="margin-top:9px">${progressHTML(run.progress, run.status === "paused")}</div>
      <div class="progress-row"><span>${run.status === "paused" ? "пауза" : "учится"}</span><span>${Math.round(run.progress * 100)}%</span></div>
    </section>` : `<div class="empty"><div class="e-ico">📉</div>Нет активного прогона</div>`;

  const metrics = [
    ["loss", "Loss"], ["rank", "Ранг весов"], ["stab", "Sign-stability"],
  ];
  const legend = S.metric === "loss"
    ? `<span class="li"><span class="sw" style="background:#5b8cff"></span>прогон</span><span class="li"><span class="sw" style="background:#5d6c82"></span>базовая</span>`
    : S.metric === "rank"
      ? `<span class="li"><span class="sw" style="background:#8f7bff"></span>effective rank</span>`
      : `<span class="li"><span class="sw" style="background:#5b8cff"></span>сид 1</span><span class="li"><span class="sw" style="background:#35d07f"></span>сид 2</span><span class="li"><span class="sw" style="background:#f5a623"></span>сид 3</span>`;

  const cmpRows = hist.map((h, i) => {
    const key = "h" + i;
    const on = S.compare.has(key);
    return `<button class="fchip ${on ? "on" : ""}" data-act="cmp" data-k="${key}">${esc(h.id)} · ${esc(h.hid)} ${h.level}</button>`;
  }).join("");

  const cmpTable = `
    <table class="cmp-table">
      <tr><th>Прогон</th><th>Финал</th><th>Δ к базе</th><th>Время</th></tr>
      ${S.compare.has("cur") && run ? `<tr><td>${esc(run.hid)} ${run.level} <span class="chip ok" style="font-size:9px;padding:1px 6px">live</span></td><td class="mono">${(run.series.loss_run.at(-1) || [0, "—"])[1].toFixed?.(3) ?? "—"}</td><td>…</td><td>${fmtMin(run.elapsed_min)}</td></tr>` : ""}
      ${hist.map((h, i) => S.compare.has("h" + i) ? `<tr><td>${esc(h.hid)} ${h.level}</td><td class="mono">${h.final.toFixed(3)}</td><td><b class="${h.delta < 0 ? "delta-neg" : "delta-pos"}">${fmtPct(h.delta)}</b></td><td>${h.minutes} мин</td></tr>` : "").join("")}
    </table>`;

  return `
    <div class="screen-title"><h1>Ход экспериментов</h1><span class="sub">live-телеметрия</span></div>
    ${hero}
    <div class="seg">
      ${metrics.map(([k, l]) => `<button class="${S.metric === k ? "on" : ""}" data-act="metric" data-v="${k}">${l}</button>`).join("")}
      ${S.metric === "loss" ? `<button class="${S.logY ? "on" : ""}" data-act="logy" data-v="1">log</button>` : ""}
    </div>
    <section class="card chart-card">
      <div class="legend">${legend}</div>
      <div class="chart-wrap"><canvas id="ch-main"></canvas></div>
      <div class="readout" id="ch-readout">проведи пальцем по графику — увидишь точные значения</div>
    </section>
    <section class="card">
      <div class="card-label"><span>Сравнение прогонов</span><span class="r">loss, % хода</span></div>
      <div class="chips" style="margin-bottom:10px">
        <button class="fchip ${S.compare.has("cur") ? "on" : ""}" data-act="cmp" data-k="cur">${run ? esc(run.hid) + " L1 (текущий)" : "текущий"}</button>
        ${cmpRows}
      </div>
      <div class="chart-wrap"><canvas id="ch-cmp"></canvas></div>
      ${cmpTable}
    </section>`;
}

function drawLiveCharts() {
  const d = S.data;
  if (!d) return;
  const run = d.runs.find((r) => r.status === "running") || d.runs[0];
  const main = $("#ch-main");
  if (main && run) {
    const sers = [];
    if (S.metric === "loss") {
      sers.push({ id: "base", label: "базовая", color: "#5d6c82", dash: [5, 4], data: run.series.loss_base, width: 1.5 });
      sers.push({ id: "run", label: "прогон", color: "#5b8cff", data: run.series.loss_run, fill: "rgba(91,140,255,.20)" });
    } else if (S.metric === "rank") {
      sers.push({ id: "rank", label: "rank", color: "#8f7bff", data: run.series.rank, fill: "rgba(143,123,255,.16)" });
    } else {
      ["#5b8cff", "#35d07f", "#f5a623"].forEach((c, i) =>
        sers.push({ id: "s" + i, label: "сид " + (i + 1), color: c, data: run.series.stab[i] || [], width: 1.6 }));
    }
    Charts.line("ch-main", {
      series: sers, height: 215, logY: S.metric === "loss" && S.logY,
      hlines: S.metric === "stab" ? [{ y: 0.75, label: "порог 0.75", color: "rgba(245,166,35,.55)" }] : [],
      fmtY: (v) => S.metric === "stab" ? v.toFixed(2) : v.toFixed(3),
      fmtX: Charts.fmtK,
      onScrub: (vals) => {
        const ro = $("#ch-readout");
        if (!ro) return;
        if (!vals) { ro.textContent = "проведи пальцем по графику — увидишь точные значения"; return; }
        const xs = ["base", "run", "rank", "s0", "s1", "s2"].filter((k) => vals[k]);
        const lbl = { base: "база", run: "прогон", rank: "ранг", s0: "сид 1", s1: "сид 2", s2: "сид 3" };
        ro.innerHTML = `<b class="mono">шаг ${Charts.fmtK(xs.map((k) => vals[k].x).reduce((a, b) => Math.max(a, b), 0))}</b>` +
          xs.map((k) => ` · ${lbl[k]} <b class="mono">${vals[k].y.toFixed(4)}</b>`).join("");
      },
    });
  }
  const cmp = $("#ch-cmp");
  if (cmp) {
    const sers = [];
    if (S.compare.has("cur") && run && run.series.loss_run.length) {
      const tot = run.series.loss_run.at(-1)[0] || 1;
      sers.push({ id: "cur", label: "текущий", color: "#5b8cff", width: 2,
        data: run.series.loss_run.map((p) => [p[0] / tot * 100, p[1]]) });
    }
    (d.history || []).forEach((h, i) => {
      if (!S.compare.has("h" + i)) return;
      const tot = (h.series.at(-1) || [1, 0])[0];
      const col = ["#8f7bff", "#f5a623", "#38c8e8"][i % 3];
      sers.push({ id: h.id, label: h.id, color: col, width: 1.6,
        data: h.series_run.map((p) => [p[0] / tot * 100, p[1]]) });
    });
    Charts.line("ch-cmp", { series: sers, height: 170, fmtY: (v) => v.toFixed(2), fmtX: (v) => Math.round(v) + "%" });
  }
}

/* ============================================================================
   ЭКРАН: ЭКИПАЖ
   ========================================================================== */
function disputeHTML(ds) {
  if (!ds) return "";
  const total = ds.options.reduce((s, o) => s + o.votes, 0) || 1;
  const voted = ds._voted || (S.data && (S.data.user_votes || {})[ds.id]);
  if (ds.closed || voted) {
    return `<div class="dispute">
      <div class="q">⚖ ${esc(ds.q)}</div>
      ${ds.options.map((o) => `
        <div class="vrow"><span>${esc(o.label)}</span>
          <span class="vbar"><i style="width:${Math.round(o.votes / total * 100)}%"></i></span>
          <b class="mono num">${Math.round(o.votes / total * 100)}%</b></div>`).join("")}
      ${voted ? `<div class="boss-line">твой голос учтён с весом 2 · решение принимает Boss по базе</div>` : ""}
      ${ds.boss ? `<div class="boss-line">Boss: ${esc(ds.boss)}</div>` : ""}
    </div>`;
  }
  return `<div class="dispute">
    <div class="q">⚖ ${esc(ds.q)}</div>
    <div class="vote-opts">
      ${ds.options.map((o) => `<button class="vote-opt" data-act="vote" data-d="${esc(ds.id)}" data-o="${esc(o.id)}"><span>${esc(o.label)}</span><span>${o.votes}</span></button>`).join("")}
    </div>
    <div class="note" style="margin-top:7px">Голос человека весит ×2, но спор закрывает арбитраж Boss'а числом из базы.</div>
  </div>`;
}

function chatMsgHTML(m, i) {
  const hl = m.kind === "bet" ? ' style="background:var(--ok-soft)"' : m.kind === "necro" ? ' style="background:var(--err-soft)"' : (m.kind === "review" ? ' style="background:var(--warn-soft)"' : "");
  const agent = (S.data.crew.agents || []).find((a) => a.id === m.agent);
  return `
    <div class="msg" ${hl} data-idx="${i}">
      ${avatar(m.agent)}
      <div class="m-body">
        <div class="m-head"><span class="m-name" style="color:${AGENT_COLOR[m.agent] || "var(--tx)"}">${esc(agent ? agent.name : m.agent)}</span><span class="m-zone">${esc(agent ? agent.short : "")}</span><span class="m-time">${timeHM(m.ts)}</span></div>
        <div class="m-text">${esc(m.text)}${m.hid ? ` <button class="chip acc m-hid mono" data-act="open-hyp" data-hid="${esc(m.hid)}">${esc(m.hid)}</button>` : ""}</div>
        ${disputeHTML(m.dispute)}
      </div>
    </div>`;
}

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
    body = `<div class="chat" id="chat-list">${c.chat.map(chatMsgHTML).join("")}</div>`;
  } else if (tab === "review") {
    body = c.remarks.length ? `<div class="list-gap">${c.remarks.map((r) => `
      <div class="remark ${r.status === "closed" ? "closed" : ""}">
        ${avatar(r.from)}
        <div style="flex:1;min-width:0">
          <div class="r-txt">${esc(r.text)}</div>
          <div class="r-meta">
            <span style="color:${AGENT_COLOR[r.from]}">${esc((c.agents.find((a) => a.id === r.from) || {}).name || r.from)}</span>
            <span>→</span>
            <span style="color:${AGENT_COLOR[r.to]}">${esc((c.agents.find((a) => a.id === r.to) || {}).name || r.to)}</span>
            ${r.hid ? `<button class="chip dim mono" data-act="open-hyp" data-hid="${esc(r.hid)}">${esc(r.hid)}</button>` : ""}
            <span class="chip ${r.status === "closed" ? "ok" : "warn"}">${r.status === "closed" ? "закрыто" : "открыто"}</span>
            <span>${agoTxt(r.ts)}</span>
          </div>
        </div>
      </div>`).join("")}</div>`
      : `<div class="empty"><div class="e-ico">🧹</div>Замечаний нет — экипаж чист</div>`;
  } else {
    const open = c.bets.filter((b) => b.status === "queued" || b.status === "running" || b.status === "blocked");
    const total = (b) => b.up.length + b.down.length || 1;
    body = `
      <div class="card-label" style="margin-top:2px">Открытые ставки · закрываются вердиктом</div>
      ${open.length ? open.map((b) => `
        <div class="bet-row">
          <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
            <b class="mono" style="font-size:14px">${esc(b.hid)}</b>
            <span class="chip ${b.status === "running" ? "acc" : "dim"}">${b.status === "running" ? "на GPU" : "в очереди"}</span>
          </div>
          <div style="font-size:15px;font-weight:600;margin:7px 0 3px">${esc(b.title)}</div>
          <div class="bet-bar"><span class="b-up" style="width:${b.up.length / total(b) * 100}%"></span><span class="b-down"></span></div>
          <div style="display:flex;justify-content:space-between;font-size:13px;color:var(--tx2)">
            <span style="color:var(--ok)">▲ взлетит: ${b.up.length ? esc(b.up.join(", ")) : "—"}</span>
            <span style="color:var(--err)">не взлетит: ${b.down.length ? esc(b.down.join(", ")) : "—"}</span>
          </div>
        </div>`).join("") : `<div class="empty">Открытых ставок нет</div>`}
      <div class="card-label" style="margin-top:6px">Рейтинг точности (hit-rate · Brier · серия)</div>
      <div class="card">
        ${c.leaders.map((l, i) => {
          const a = c.agents.find((x) => x.id === l.agent) || {};
          return `<div class="lead-row">
            <span class="lead-rank">${i + 1}</span>
            ${avatar(l.agent, "sm")}
            <div class="lead-mid">
              <div class="lm1"><span style="color:${AGENT_COLOR[l.agent]}">${esc(a.name || l.agent)}</span><span class="mono">${Math.round(l.rate * 100)}%</span></div>
              <div class="rate-bar"><i style="width:${l.rate * 100}%"></i></div>
              <div class="lm2"><span>${l.bets} ставок</span><span>Brier ${l.brier}</span><span>${l.streak > 0 ? "🔥 " + l.streak : l.streak < 0 ? "❄ " + (-l.streak) : "—"}</span></div>
            </div>
          </div>`;
        }).join("")}
      </div>`;
  }

  const openRm = c.remarks.filter((r) => r.status === "open").length;
  return `
    <div class="screen-title"><h1>Экипаж</h1><span class="sub">7 агентов · 0 GPU-ч на чат</span></div>
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
  const fp = (v.forecast - lo) / span * 100, ap = (v.actual - lo) / span * 100;
  return `
    <div class="dumbbell">
      <span class="d-track"></span>
      <span class="d-line" style="left:${Math.min(fp, ap)}%;width:${Math.abs(ap - fp)}%"></span>
      <span class="d-pt fore" style="left:calc(${fp}% - 6px)"></span><span class="d-lbl" style="left:${fp}%;transform:translateX(-50%)">обещали ${fmtPct(v.forecast, 0)}</span>
      <span class="d-pt act" style="left:calc(${ap}% - 6px)"></span><span class="d-lbl" style="left:${ap}%;transform:translateX(-50%)">получили ${fmtPct(v.actual, 0)}</span>
    </div>
    <div class="note">отклонение от прогноза: <b class="mono" style="color:${Math.abs(v.deviation) > 40 ? "var(--err)" : "var(--ok)"}">${fmtPct(v.deviation)}</b> · ${esc(v.unit)}</div>`;
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
        <span class="chip dim">${seeds} seeds</span>
        <span class="chip dim">${fmtN(v.gpu_hours, 1)} GPU-ч</span>
        ${v.commercial >= 0.5 ? `<span class="chip ok">коммерция ${Math.round(v.commercial * 100)}%</span>` : ""}
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
      <div class="note">Убитая за 10 минут идея — успешный результат: −${d.stats.gpu_saved_h} GPU-ч сэкономлено ранними снятиями.</div>
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
      <h2 id="sheet-title"></h2><button class="sheet-x" data-act="sheet-close">✕</button>
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
  const src = h.source === "human" ? "человек" : (d.crew.agents.find((a) => a.id === h.source) || {}).name || h.source;
  const runs = [["L0", 0.1], ["L1", 2], ["L2", 13.5], ["L3", 30]];
  const open = `
    <div>
      <div class="hyp-row1">
        <b class="hid">${esc(h.id)}</b>
        <span class="chip ${BIN_COLOR[h.bin] || "dim"}">${h.bin}</span>
        <span class="chip ${h.status === "running" ? "acc" : h.status === "blocked" ? "err" : h.status === "paused_checkpoint" ? "warn" : "dim"}">${STATUS_RU[h.status] || h.status}</span>
        <span class="ppi-badge"><b>${fmtN(h.ppi)}</b><span>PPI оч/ч</span></span>
      </div>
      <h3 style="font-size:17.5px;margin:10px 0 9px;line-height:1.35">${esc(h.title)}</h3>
      <div class="kv">
        <div class="kvv"><b>${fmtN(h.pi)}</b><span>PI</span></div>
        <div class="kvv"><b>${fmtN(h.est_hours, 1)} ч</b><span>оценка</span></div>
        <div class="kvv"><b>${h.signals}</b><span>сигналов</span></div>
        <div class="kvv"><b>${h.seeds}</b><span>seeds</span></div>
        <div class="kvv"><b>${fmtN(h.age_days, 1)} дн</b><span>в очереди</span></div>
        <div class="kvv"><b style="font-size:14px">${esc(src)}</b><span>источник</span></div>
      </div>
      ${ladderHTML(h.level)}
    </div>
    ${cur ? `<div>${progressHTML(cur.progress)}<div class="progress-row"><span>на GPU сейчас</span><span>${Math.round(cur.progress * 100)}%</span><span>ETA ${fmtMin(cur.eta_min)}</span></div></div>` : ""}
    <div>
      <div class="card-label" style="margin-top:4px">Коридор эффекта</div>
      ${corridorHTML(h)}
    </div>
    <div>
      <div class="card-label">Kill-проверки (до GPU) · ${h.checks_pass}/8</div>
      <div class="checks">
        ${h.checks.map((c) => `
          <div class="check ${c.s}">
            <span class="ic">${CHECK_RU[c.s]}</span>
            <span class="ct">${esc(S.data.checks[c.i] || "")}${c.s === "fail" && h.note ? ` <b>· ${esc(h.note)}</b>` : ""}</span>
            ${c.s === "wait" ? `<button class="btn sm ca" data-act="run-check" data-hid="${esc(h.id)}" data-i="${c.i}">Запустить</button>` : ""}
          </div>`).join("")}
      </div>
    </div>
    ${(h.bets.up.length || h.bets.down.length) ? `
    <div>
      <div class="card-label">Ставки экипажа</div>
      <div style="display:flex;gap:16px;font-size:14px">
        <span style="color:var(--ok)">▲ ${esc(h.bets.up.join(", ") || "—")}</span>
        <span style="color:var(--err)">▼ ${esc(h.bets.down.join(", ") || "—")}</span>
      </div>
      <div class="note">Закрываются вердиктом: confirmed/partial — выигрывают «за».</div>
    </div>` : ""}
    <div class="split">
      <button class="btn block" data-act="boost" data-hid="${esc(h.id)}">↑ Приоритет (aging +2 дня)</button>
    </div>
    <div>
      <div class="card-label">Запустить уровень вручную</div>
      <div class="split">
        ${runs.slice(0, 3).map(([lv, hh]) => `<button class="btn sm block ${lv === "L2" ? "ghost" : ""}" data-act="run-level" data-hid="${esc(h.id)}" data-lv="${lv}">${lv} · ${fmtN(hh, 1)}ч</button>`).join("")}
      </div>
      <div class="note" style="margin-top:7px">L2 дороже 12 GPU-ч — уйдёт на подтверждение человеку. Списание бюджета: ${fmtN(S.data.gov.budget_hours.used, 1)}/${S.data.gov.budget_hours.limit} ч.</div>
    </div>`;
  openSheet(open);
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
    <div><div class="card-label">Что меняется</div><div style="font-size:15px">${esc(v.changes)}</div></div>
    <div><div class="card-label">Следующее действие</div><div style="font-size:15px">${esc(v.next)}</div></div>
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
  const q = (w.step === 3 && w.qualityCache);
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
      <div class="note">Система мгновенно проверит формулировку и сравнит с закрытыми идеями — до того, как идея попадёт к экипажу.</div>
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
        <div class="note">1% обучения → максимальный вес E=1.0, 10% и позже → 0. Раньше — ценнее.</div>
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
        <div class="note">Меньше 3 независимых — PI обнулится по сигналу S. Зависимые сигналы = один сигнал.</div>
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
      ${!w.check ? `<div class="note">Система сравнит текст с закрытыми идеями и очередью: дубликаты видны до GPU-часов.</div>` : `
        ${w.check.matches.length ? w.check.matches.map((m) => `
          <div class="dup-row">
            <div class="dup-sim"><b class="mono" style="color:${m.sim > 0.45 ? "var(--err)" : m.sim > 0.25 ? "var(--warn)" : "var(--tx2)"}">${Math.round(m.sim * 100)}%</b><span>сходство</span></div>
            <div style="flex:1;min-width:0">
              <div style="font-size:14.5px;font-weight:600;line-height:1.35">${esc(m.title)}</div>
              <div class="note">${esc(m.why)} · <span class="mono">${esc(m.id)}</span></div>
            </div>
          </div>`).join("") : `<div class="banner" style="background:var(--ok-soft);border:1px solid color-mix(in srgb,var(--ok) 40%,transparent);color:var(--ok)">✓ Прямых дублей нет — идея проходит в разбор экипажа</div>`}
        ${w.check.notes.length ? `<div class="qlist">${w.check.notes.map((n) => `<div class="bad"><span>!</span>${esc(n)}</div>`).join("")}</div>` : ""}
        ${w.check.matches.some((m) => m.sim > 0.45) ? `<div class="banner warn">⚠ Похоже на дубль: экипаж, скорее всего, снимет идею до GPU. Стоит заострить отличие механизма.</div>` : ""}
      `}
      <div class="split">
        <button class="btn block" id="wz-back" data-act="wz-back">← Назад</button>
        <button class="btn primary block" id="wz-submit" ${w.check ? "" : "disabled"}>Отправить экипажу</button>
      </div>`;
  } else {
    const r = w.result;
    body = `
      ${stepsBar}
      <div class="success-check">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none"><path d="m5 13 4.2 4.2L19 7.4" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
      <div style="text-align:center">
        <b style="font-size:17px">Карточка ${esc(r.hid)} создана</b>
        <div class="note" style="margin-top:4px">Идея у экипажа: iВасёк завёл карточку, Морг готовит kill-проверки, ставки открыты.</div>
      </div>
      <div class="kpi-grid">
        <div class="kpi"><b>${r.pi.toFixed(3)}</b><span>PI</span></div>
        <div class="kpi"><b style="color:var(--ok)">${r.ppi.toFixed(2)}</b><span>PPI оч/ч</span></div>
        <div class="kpi"><b>${r.position}<em> из ${r.of}</em></b><span>место в очереди</span></div>
        <div class="kpi"><b>2▲ 1▼</b><span>ставки экипажа</span></div>
      </div>
      <div class="split">
        <button class="btn block" data-act="wz-goto" data-nav="pipe">В конвейер</button>
        <button class="btn primary block" data-act="wz-goto" data-nav="crew">Чат экипажа</button>
      </div>`;
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
    if (j.ok) {
      w.result = j; w.step = 4; haptic("ok");
      await refresh(); renderWizard();
    } else if (btn) { btn.disabled = false; btn.textContent = "Отправить экипажу"; }
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
  scr.innerHTML = (S.offline ? `<div class="banner err"><span>⚠</span><span>Нет связи с лабораторией. Повтор попытки каждые 4 с.</span></div>` : "") + SCREENS[S.route]();
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
      const ok = el.dataset.ok === "1", id = el.dataset.id;
      if (!ok && S.pendingReject !== id) { S.pendingReject = id; haptic("warn"); renderScreen(); setTimeout(() => { if (S.pendingReject === id) { S.pendingReject = null; renderScreen(); } }, 3200); break; }
      haptic(ok ? "ok" : "warn");
      const j = await api({ type: "approve", id, ok });
      if (j.ok) { S.pendingReject = null; toast(ok ? "Прогон одобрен — встаёт в план" : "Дорогой прогон отклонён, гипотеза закрыта", ok ? "ok" : ""); refresh(); }
      break;
    }
    case "boost": {
      haptic();
      const j = await api({ type: "boost", hid: el.dataset.hid });
      if (j.ok) { toast(`Приоритет поднят: PPI ${fmtN(j.ppi)} оч/ч`, "ok"); await refresh(); if (S.sheet) openHyp(el.dataset.hid); }
      break;
    }
    case "run-check": {
      haptic();
      el.disabled = true; el.textContent = "Идёт…";
      const j = await api({ type: "run_check", hid: el.dataset.hid, i: +el.dataset.i });
      if (j.ok) { toast("Проверка запущена — Морг готовит контраргументы"); await refresh(); if (S.sheet) openHyp(el.dataset.hid); setTimeout(() => { if (S.sheet) openHyp(el.dataset.hid); }, 8500); }
      break;
    }
    case "run-level": {
      haptic();
      const j = await api({ type: "run_level", hid: el.dataset.hid, level: el.dataset.lv });
      if (j.ok) {
        if (j.approval) toast("Дороже 12 GPU-ч — заявка ушла на подтверждение (Пульт)", "warn");
        else toast(`${el.dataset.lv} поставлен в план`, "ok");
        await refresh(); if (S.sheet) openHyp(el.dataset.hid);
      }
      break;
    }
    case "vote": {
      haptic();
      const j = await api({ type: "vote", dispute_id: el.dataset.d, option: el.dataset.o });
      if (j.ok) { toast("Голос учтён (вес ×2)", "ok"); refresh(); }
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
