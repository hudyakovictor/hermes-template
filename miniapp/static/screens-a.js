/* ============================================================
   screens-a.js — ПУЛЬТ (Dashboard) + КОНВЕЙЕР (Pipeline)
   ============================================================ */
"use strict";

const Screens = window.Screens || {};
window.Screens = Screens;

/* ================= ПУЛЬТ ================= */
const Dash = (() => {
  let refs = {};
  let ringApi = null, sparkCv = null;

  function render() {
    const s = Data;
    const sc = $("#sc-dash");
    sc.innerHTML = `
      <div class="between" style="margin:2px 2px 10px">
        <span class="chip ${s.mode === "auto" ? "chip-ok" : "chip-warn"}" id="d-mode">${s.mode === "auto" ? icon("bolt") + "АВТО" : icon("pause") + "ПАУЗА"}</span>
        <span class="tiny muted" id="d-tick">тик диспетчера: 1 мин назад</span>
      </div>
      <div id="d-approvals"></div>

      <div class="card hair">
        <div class="between" style="margin-bottom:10px">
          <div class="row" style="gap:8px">${icon("chip")}<b style="font-size:14px">GPU · ${s.gpu.name}</b></div>
          <span class="chip ${s.gpu.temp > 72 ? "chip-danger" : s.gpu.temp > 68 ? "chip-warn" : "chip-ok"}" id="d-temp">
            ${icon("temp")}<span id="d-temp-v">${Math.round(s.gpu.temp)}°C</span></span>
        </div>
        <div class="gpu-ring-wrap">
          <div id="d-ring"></div>
          <div class="grow stack" style="gap:8px">
            <div class="between"><span class="caps">загрузка</span><b class="mono" id="d-util" style="font-size:17px">${Math.round(s.gpu.util)}%</b></div>
            <div class="between"><span class="caps">память</span><b class="mono" id="d-vram" style="font-size:17px">${nf(s.gpu.vram, 1)}<span class="muted small"> / ${nf(s.gpu.vramTotal, 1)} ГБ</span></b></div>
            <div class="between"><span class="caps">кулер</span><b class="mono" id="d-fan" style="font-size:17px">${Math.round(s.gpu.fan)}%</b></div>
            <div class="meter thin"><i id="d-vram-bar" style="width:${Math.round(s.gpu.vram / s.gpu.vramTotal * 100)}%"></i></div>
          </div>
        </div>
        <div class="divider"></div>
        <div class="row" style="justify-content:space-between">
          <span class="caps">утилизация · 60 с</span>
          <span class="st ${s.run ? "st-run" : "st-mut"}"><i class="dot"></i>${s.run ? "прогон" : "свободен"}</span>
        </div>
        <canvas id="d-spark" style="width:100%;height:44px;margin-top:6px"></canvas>
      </div>

      <div class="card" id="d-run-card"></div>

      <div class="card">
        <div class="between" style="margin-bottom:9px">
          <div class="row" style="gap:8px">${icon("clock")}<b style="font-size:14px">Суточный бюджет GPU</b></div>
          <span class="chip" id="d-budget-left"></span>
        </div>
        <div class="row" style="justify-content:space-between;align-items:baseline;margin-bottom:7px">
          <b class="mono" style="font-size:26px;letter-spacing:-.03em" id="d-budget-spent">${nf(s.budget.spentH, 2)}</b>
          <span class="muted small">из ${nf(s.budget.limitH, 1)} ч · сгорает <span class="mono" id="d-burn">1.0 ч/ч</span></span>
        </div>
        <div class="prog" id="d-budget-bar"><i></i></div>
        <div class="between tiny muted" style="margin-top:6px">
          <span id="d-budget-proj">—</span>
          <span>сброс в 00:00</span>
        </div>
      </div>

      <div class="hgrid g4" id="d-stats"></div>

      <div class="sec-t"><h3>Лента контура</h3><button data-action="go-pipe">весь конвейер ${icon("chev-r")}</button></div>
      <div class="card" style="padding:6px 14px" id="d-events"></div>
    `;
    bind();
  }

  function approvalsHTML() {
    return Data.approvals.map(a => `
      <div class="card approval" data-h="${a.hid}">
        <div class="row">
          <div class="ap-ico">${icon("alert")}</div>
          <div class="grow">
            <div style="font-size:13.5px;font-weight:800">Нужно подтверждение: ${a.hid} → ${a.level}</div>
            <div class="tiny muted" style="margin-top:2px">${esc(a.title)} · ${nf(a.hours, 1)} ч GPU · ${esc(a.note)}</div>
          </div>
        </div>
        <div class="row" style="gap:8px;margin-top:11px">
          <button class="btn btn-ok btn-sm grow" data-action="approve" data-h="${a.hid}">${icon("check")}Одобрить ${nf(a.hours, 1)} ч</button>
          <button class="btn btn-ghost btn-sm" data-action="decline" data-h="${a.hid}">Отклонить</button>
        </div>
      </div>`).join("");
  }

  function runCardHTML() {
    const r = Data.run;
    if (!r) return `
      <div class="row" style="gap:10px">
        <div class="ap-ico" style="background:var(--surface-2);color:var(--muted)">${icon("chip")}</div>
        <div class="grow"><b style="font-size:14px">GPU свободен</b>
        <div class="tiny muted" style="margin-top:2px">${Data.mode === "auto" ? "диспетчер подберёт следующую по PPI" : "автозапуск на паузе — запустите вручную"}</div></div>
      </div>`;
    return `
      <div class="between" style="margin-bottom:8px">
        <div class="row" style="gap:8px">${icon("play")}<b style="font-size:14px">Сейчас считает</b></div>
        ${UI.lvBadge(r.level)}
      </div>
      <div class="row" style="gap:10px;margin-bottom:10px">
        <span class="ppi-num" style="color:var(--accent);font-size:18px">${r.hid}</span>
        <span class="grow ellip" style="font-size:13px;color:var(--text-2)">${esc(r.title)}</span>
      </div>
      <div class="prog"><i id="d-run-prog" style="width:${Math.round(r.progress * 100)}%"></i></div>
      <div class="between tiny muted" style="margin-top:7px">
        <span class="mono" id="d-run-pct">${Math.round(r.progress * 100)}%</span>
        <span class="mono">осталось ~<b id="d-run-eta" style="color:var(--text-2)">${r.eta} мин</b></span>
        <span class="mono">${r.seedsN} seeds · ${icon("dice")}</span>
      </div>
      <div class="row" style="gap:8px;margin-top:12px">
        <button class="btn btn-ghost btn-sm grow" data-action="pause-toggle" id="d-pause-btn">
          ${Data.mode === "auto" ? icon("pause") + "Пауза автозапуска" : icon("play") + "Вернуть автозапуск"}</button>
        <button class="btn btn-danger btn-sm hold" id="d-kill-btn" style="flex:1.2">
          <span class="relative" style="position:relative;z-index:2;display:flex;align-items:center;gap:7px;justify-content:center">${icon("kill")}Снять прогон</span>
        </button>
      </div>
      <div class="tiny" style="color:var(--faint);margin-top:8px;text-align:center">«Снять» — удерживайте кнопку 1.1 с: чекпойнт сохранится, прогресс уровня пропадёт</div>`;
  }

  function bind() {
    refs = {
      tick: $("#d-tick"), tempV: $("#d-temp-v"), temp: $("#d-temp"),
      util: $("#d-util"), vram: $("#d-vram"), fan: $("#d-fan"), vramBar: $("#d-vram-bar"),
      spark: $("#d-spark"),
    };
    // кольцо VRAM
    const rm = $("#d-ring");
    ringApi = Charts.ring(rm, { value: 0, max: 100 });
    rm.querySelector(".ring-c").innerHTML = `
      <div class="ring-num" id="d-ring-v">0<i style="font-size:12px;font-style:normal;color:var(--muted)">%</i></div>
      <div class="ring-lbl">VRAM</div>`;
    refs.ringV = $("#d-ring-v").firstChild;
    refs.ringV.nodeValue = "0";
    refs.ringPct = $("#d-ring-v");

    sparkCv = refs.spark;
    // статистика
    $("#d-stats").innerHTML = [
      [Data.queue.length, "в очереди"], [Data.verdicts.filter(v => v.ago < 24 * HOUR).length, "вердиктов/24ч"],
      [Data.verdicts.filter(v => v.status === "confirmed").length + "/" + Data.verdicts.length, "подтверждено"],
      [nf(64, 0) + "%", "прогнозов в коридоре"],
    ].map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
    $("#d-events").innerHTML = eventsHTML();

    bindRun();
    updateApprovals();
  }

  function bindRun() {
    const c = $("#d-run-card");
    c.innerHTML = runCardHTML();
    const kb = $("#d-kill-btn");
    if (kb) holdable(kb, () => Actions.killRun());
  }

  function eventsHTML() {
    const tone = { ok: "var(--ok-dim);color:var(--ok)", accent: "color-mix(in srgb,var(--accent) 12%,transparent);color:var(--accent)", warn: "var(--warn-dim);color:var(--warn)", danger: "var(--danger-dim);color:var(--danger)" };
    return Data.events.slice(0, 5).map(e => `
      <div class="evt">
        <span class="evt-ico" style="background:${tone[e.tone] || tone.accent}">${icon(e.ico)}</span>
        <p>${e.html}</p><time>${Data.fmtAgo(e.ts)}</time>
      </div>`).join("");
  }

  function updateApprovals() {
    const box = $("#d-approvals"); if (!box) return;
    box.innerHTML = approvalsHTML();
  }

  /* обновления раз в секунду */
  function tick() {
    const g = Data.gpu;
    if (!refs.tick) return;
    refs.tick.textContent = "тик диспетчера: " + Data.fmtAgo(Data.lastTick);
    refs.util.textContent = Math.round(g.util) + "%";
    refs.vram.innerHTML = `${nf(g.vram, 1)}<span class="muted small"> / ${nf(g.vramTotal, 1)} ГБ</span>`;
    refs.fan.textContent = Math.round(g.fan) + "%";
    refs.tempV.textContent = Math.round(g.temp) + "°C";
    refs.temp.className = "chip " + (g.temp > 72 ? "chip-danger" : g.temp > 68 ? "chip-warn" : "chip-ok");
    const vramPct = Math.round(g.vram / g.vramTotal * 100);
    refs.vramBar.style.width = vramPct + "%";
    ringApi.set(vramPct);
    if (refs.ringV) refs.ringV.nodeValue = vramPct;
    Charts.spark(sparkCv, g.utilHist, Data.run ? "#4fc3ff" : "#7e8aa5", { min: 0, max: 100 });
    // бюджет
    const b = Data.budget, left = b.limitH - b.spentH;
    $("#d-budget-spent").textContent = nf(b.spentH, 2);
    $("#d-budget-bar").firstElementChild.style.width = Math.min(100, b.spentH / b.limitH * 100) + "%";
    $("#d-budget-bar").firstElementChild.style.background = left < 1 ? "linear-gradient(90deg,#ff6161,#ffb437)" : "";
    $("#d-budget-left").textContent = "осталось " + nf(left, 1) + " ч";
    $("#d-budget-left").className = "chip " + (left < 1 ? "chip-danger" : left < 2 ? "chip-warn" : "chip-ok");
    $("#d-budget-proj").textContent = Data.run
      ? `при таком темпе лимит в ${(() => { const t = new Date(now() + left * HOUR); return String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0"); })()}`
      : "расход остановлен";
    $("#d-burn").textContent = Data.run ? "1.0 ч/ч" : "0.0 ч/ч";
    // прогон
    if (Data.run) {
      const p = $("#d-run-pct"), e = $("#d-run-eta"), bar = $("#d-run-prog");
      if (p) p.textContent = Math.round(Data.run.progress * 100) + "%";
      if (e) e.textContent = Data.run.eta + " мин";
      if (bar) bar.style.width = Math.round(Data.run.progress * 100) + "%";
    }
  }

  function refreshHard() {
    // полный перерендер карточек при событиях
    if (!$("#sc-dash")) return;
    bindRun();
    updateApprovals();
    $("#d-events").innerHTML = eventsHTML();
    $("#d-mode").className = "chip " + (Data.mode === "auto" ? "chip-ok" : "chip-warn");
    $("#d-mode").innerHTML = Data.mode === "auto" ? icon("bolt") + "АВТО" : icon("pause") + "ПАУЗА";
    $("#d-stats").innerHTML = [
      [Data.queue.length, "в очереди"], [Data.verdicts.filter(v => v.ago < 24 * HOUR).length, "вердиктов/24ч"],
      [Data.verdicts.filter(v => v.status === "confirmed").length + "/" + Data.verdicts.length, "подтверждено"],
      ["64%", "прогнозов в коридоре"],
    ].map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
    const kb = $("#d-kill-btn"); if (kb) holdable(kb, () => Actions.killRun());
    tick();
  }

  return { render, tick, refreshHard };
})();

/* ================= КОНВЕЙЕР ================= */
const Pipe = (() => {
  let seg = "queue";

  const SEGS = [["queue", "Очередь"], ["gpu", "На GPU"], ["paused", "Пауза"], ["closed", "Закрыто"]];

  function railHTML() {
    const levels = ["L0", "L1", "L2", "L3"];
    const counts = levels.map(l => ({
      q: Data.queue.filter(h => h.level === l).length + (Data.run && Data.run.level === l ? 1 : 0),
      closed: Data.verdicts.filter(v => v.level === l).length,
    }));
    const maxQ = Math.max(1, ...counts.map(c => c.q + c.closed));
    const cols = ["var(--l0)", "var(--l1)", "var(--l2)", "var(--l3)"];
    return `<div class="rail">${levels.map((l, i) => `
      <div class="rl" style="color:${cols[i]}">
        <div class="rl-dot"></div>
        <b style="color:${cols[i]}">${l}</b><span>${counts[i].q} активн · ${counts[i].closed} закр</span>
      </div>${i < 3 ? `<div class="bar"><i style="width:${Math.round((counts[i + 1].q / maxQ) * 100)}%;background:${cols[i + 1]}"></i></div>` : ""}`).join("")}
    </div>
    <div class="tiny muted" style="margin-top:8px;line-height:1.6">
      каскад: L0 ≤5 мин → L1 ≤1 ч → L2 ≤8 ч → L3 по решению человека.
      дальше прошла гипотеза — тем дороже каждый её час.
    </div>`;
  }

  function qCardHTML(h) {
    return `
      <div class="qi">
        ${UI.binBadge(h.bin)}<span class="mono small" style="font-weight:800;color:var(--accent)">${h.id}</span>
        ${UI.lvBadge(h.level)}<span class="tiny muted mono">${h.signals} сигн · ${nf(h.hours, 1)} ч</span>
        <span class="grow"></span>
        <div><div class="ppi-num" style="color:var(--accent)">${h.ppi.toFixed(2)}</div><div class="ppi-lbl">PPI</div></div>
      </div>
      <h4>${esc(h.title)}</h4>
      <div class="mini-meters">
        <div class="grow stack" style="gap:3px">
          <div class="between tiny"><span class="caps">PI ${h.pi.toFixed(2)}</span><span class="tiny muted mono">${h.aging ? "+" + h.aging.toFixed(2) + " aging" : "—"}</span></div>
          <div class="meter thin"><i style="width:${Math.round(h.pi * 100)}%"></i></div>
        </div>
        <span class="icon-btn" style="width:40px;height:40px;pointer-events:none;color:var(--faint)">${icon("chev-r")}</span>
      </div>`;
  }

  function queueActions(h) {
    return [
      { id: "up", label: "выше", icon: "up", fn: () => Actions.boost(h.id) },
      { id: "go", label: "L0", icon: "play", fn: () => Actions.launchL0(h.id) },
      { id: "kill", label: "закрыть", icon: "x", fn: () => Actions.killHypo(h.id) },
    ];
  }

  function fill() {
    const box = $("#p-list"); if (!box) return;
    if (seg === "queue") {
      const items = [...Data.queue].sort((a, b) => b.ppi - a.ppi);
      box.innerHTML = "";
      if (!items.length) {
        box.innerHTML = `<div class="empty">${icon("flow")}<b>Очередь пуста</b><span>экипаж добывает новые сигналы<br>или добавьте идею кнопкой «+»</span></div>`;
        return;
      }
      items.forEach(h => box.appendChild(swipeRow(qCardHTML(h), queueActions(h), { onTap: () => HypoSheet.open(h.id) })));
      box.appendChild(el(`<div class="tiny" style="color:var(--faint);text-align:center;margin:8px 0 4px">свайп по карточке — быстрые действия · сортировка по PPI</div>`));
      return;
    }
    box.innerHTML = listHTML();
  }

  function listHTML() {
    if (seg === "queue") return "";
    if (seg === "gpu") {
      const r = Data.run;
      if (!r) return `<div class="empty">${icon("chip")}<b>GPU свободен</b><span>${Data.mode === "auto" ? "диспетчер запустит следующую задачу" : "автозапуск на паузе"}</span></div>`;
      return `<div class="card hair">
        <div class="between" style="margin-bottom:8px">${UI.lvBadge(r.level)}${UI.statusChip("running")}</div>
        <div class="row" style="gap:10px;margin-bottom:10px">
          <span class="ppi-num" style="color:var(--accent);font-size:18px">${r.hid}</span>
          <span class="grow ellip small">${esc(r.title)}</span>
        </div>
        <div class="prog"><i style="width:${Math.round(r.progress * 100)}%"></i></div>
        <div class="between tiny muted" style="margin-top:7px">
          <span class="mono">${Math.round(r.progress * 100)}%</span>
          <span class="mono">осталось ~${r.eta} мин · ${r.seedsN} seeds</span>
        </div>
        <button class="btn btn-ghost btn-sm" style="margin-top:11px" data-action="go-tele">${icon("pulse")}Живая телеметрия прогона</button>
      </div>`;
    }
    if (seg === "paused") {
      if (!Data.paused.length) return `<div class="empty">${icon("pause")}<b>Пауз нет</b><span>все чекпойнты либо дорешены, либо в очереди</span></div>`;
      return Data.paused.map(h => `
        <div class="card" data-action="h-open" data-h="${h.id}">
          <div class="qi">${UI.binBadge(h.bin)}<span class="mono small" style="font-weight:800;color:var(--warn)">${h.id}</span>${UI.lvBadge(h.level)}${UI.statusChip("paused_checkpoint")}
          ${h.approved ? `<span class="chip chip-ok">${icon("check")}L3 одобрен</span>` : ""}</div>
          <h4>${esc(h.title)}</h4>
          <div class="between tiny muted" style="margin-top:8px">
            <span class="mono">чекпойнт ${nf(h.checkpointH, 1)} ч · PPI ${h.ppi.toFixed(2)}</span>
            <span>пауза с ${Data.fmtAgo(h._pa || (h._pa = now() - 2 * 24 * HOUR))}</span>
          </div>
        </div>`).join("");
    }
    // closed
    return Data.verdicts.slice(0, 8).map(v => `
      <div class="card link-row" data-action="v-open" data-h="${v.id}">
        <div class="qi">
          ${UI.lvBadge(v.level)}<span class="mono small" style="font-weight:800">${v.id}</span>
          ${UI.statusChip(v.status)}<span class="grow"></span><span class="tiny muted mono">${Data.fmtAgo(v.ago)}</span>
        </div>
        <h4>${esc(v.title)}</h4>
      </div>`).join("");
  }

  function render() {
    const sc = $("#sc-pipe");
    sc.innerHTML = `
      <div class="card hair">${railHTML()}</div>
      <div class="seg" id="p-seg">${SEGS.map(([k, l]) => `<button data-seg="${k}" class="${k === seg ? "on" : ""}">${l}</button>`).join("")}</div>
      <div id="p-list"></div>`;
    fill();
    $("#p-seg").addEventListener("click", e => {
      const b = e.target.closest("[data-seg]"); if (!b) return;
      seg = b.dataset.seg;
      $$("#p-seg button").forEach(x => x.classList.toggle("on", x.dataset.seg === seg));
      fill();
      $("#p-list").animate?.([{ opacity: .4, transform: "translateY(6px)" }, {}], { duration: 240, easing: "ease-out" });
      TG.haptic("sel");
    });
  }

  function refresh() { if ($("#p-list")) { fill(); const rail = $(".card.hair", $("#sc-pipe")); if (rail) rail.innerHTML = railHTML(); } }
  return { render, refresh, setSeg(k) { seg = k; } };
})();

/* ================= Карточка гипотезы (шторка) ================= */
const HypoSheet = (() => {
  function open(hid) {
    const h = Data.queue.find(x => x.id === hid) || Data.paused.find(x => x.id === hid);
    if (!h) return;
    const inQueue = Data.queue.includes(h);
    const done = h.checks.filter(c => c === "ok").length;
    Sheet.open(`
      <div class="sheet-head">
        ${UI.binBadge(h.bin)}
        <div class="grow">
          <div class="sheet-title mono">${h.id} ${UI.lvBadge(h.level)}</div>
          <div class="sheet-sub">${esc(h.term || "гипотеза")} · ${h.signals} независимых сигнала · ${nf(h.hours, 1)} ч GPU</div>
        </div>
        ${UI.statusChip(h.status)}
      </div>
      <div class="sheet-body">
        <h3 style="font-size:16px;line-height:1.4;margin-bottom:10px">${esc(h.title)}</h3>
        <p class="small" style="color:var(--text-2);line-height:1.6">${esc(h.mechanism)}</p>

        <div class="sec-t"><h3>Ценность: PI / PPI</h3></div>
        <div class="card" style="margin:0">${UI.piBreakdown(h)}</div>

        <div class="sec-t"><h3>Ожидаемый коридор эффекта</h3><span class="chip chip-accent">прогноз до запуска</span></div>
        <div class="card" style="margin:0">${UI.corridorHTML(h.corridor, { forecast: true })}</div>

        <div class="sec-t"><h3>Kill-проверки · ${done}/8</h3><span class="tiny muted">гейт перед GPU</span></div>
        <div class="card" style="padding:4px 14px">${UI.checksHTML(h.checks)}</div>

        ${h.checkpointH ? `<div class="card" style="margin-top:12px"><div class="between"><span class="caps">чекпойнт</span><b class="mono">${nf(h.checkpointH, 1)} ч</b></div></div>` : ""}
        <div style="height:14px"></div>
      </div>
      ${inQueue ? `
      <div class="sheet-cta">
        <button class="btn btn-primary grow" data-action="h-launch" data-h="${h.id}">${icon("play")}Запустить L0</button>
        <button class="icon-btn" style="width:46px;height:46px" data-action="h-boost" data-h="${h.id}" aria-label="Поднять приоритет">${icon("up")}</button>
        <button class="icon-btn" style="width:46px;height:46px;color:var(--danger)" data-action="h-kill" data-h="${h.id}" aria-label="Закрыть гипотезу">${icon("x")}</button>
      </div>` : ""}`);
  }
  return { open };
})();

window.Dash = Dash; window.Pipe = Pipe; window.HypoSheet = HypoSheet;
