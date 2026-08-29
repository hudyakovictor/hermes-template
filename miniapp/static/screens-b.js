/* ============================================================
   screens-b.js — ТЕЛЕМЕТРИЯ (Live) + ЭКИПАЖ (Crew & Market)
   ============================================================ */
"use strict";

/* ================= ТЕЛЕМЕТРИЯ ================= */
const Tele = (() => {
  let metric = "loss", logOn = false, chart = null, tip = null;
  let compareSel = null;         // [runId,...] когда включено сравнение
  let sparks = { grad: [], lr: [], vram: [] };
  const SEED_COLORS = ["#4fc3ff", "#8b7cff", "#3be0a0"];

  function seedSeries(which) {
    const t = Data.tele; if (!t) return [];
    const src = which === "rank" ? t.ranks : t.seeds;
    return src.map((pts, i) => ({
      id: "s" + i, name: "seed " + i, color: SEED_COLORS[i],
      points: pts.slice(0, Math.max(2, t.revealed)),
    }));
  }
  function stabSeries() {
    const t = Data.tele; if (!t) return [];
    return [{ id: "st", name: "разброс seeds", color: "#ffb437", points: t.stab.slice(0, Math.max(2, t.revealed)) }];
  }

  function runSeries() {
    const t = Data.tele;
    if (compareSel) {
      return Data.runs.filter(r => compareSel.includes(r.id)).map(r => ({
        id: r.id, name: r.id + " · " + r.hid, color: r.color, points: r.curve[0],
      }));
    }
    if (!t) {
      const last = Data.runs[0];
      if (!last) return [];
      return [{ id: "last", name: last.id + " (последний)", color: "#7e8aa5", points: last.curve[0] }];
    }
    if (metric === "loss") return seedSeries("loss");
    if (metric === "rank") return seedSeries("rank");
    return stabSeries();
  }

  const METRICS = [["loss", "Loss"], ["rank", "Eff. rank"], ["stab", "Устойчивость"]];

  function render() {
    const sc = $("#sc-tele");
    sc.innerHTML = `
      <div id="t-run"></div>
      <div class="seg" id="t-metric" style="margin-bottom:10px">${METRICS.map(([k, l]) => `<button data-m="${k}" class="${k === metric ? "on" : ""}">${l}</button>`).join("")}</div>
      <div class="card chart-card">
        <div class="between" style="padding:0 6px 8px">
          <span class="caps" id="t-chart-title">обучение · live</span>
          <span class="st st-run" id="t-live-flag" style="display:none"><i class="dot"></i>стрим</span>
        </div>
        <div class="chart-box tall">
          <canvas id="t-chart"></canvas>
          <div class="chart-tip" id="t-tip"></div>
        </div>
        <div class="legend" id="t-legend"></div>
        <div class="chart-ctl">
          <span class="chip ${metric === "loss" ? "on" : ""}" id="t-log">log шкала</span>
          <span class="chip" id="t-compare">${icon("compare")}сравнить прогоны</span>
          ${compareSel ? `<span class="chip chip-danger" id="t-live-back">${icon("x")}вернуться к live</span>` : ""}
        </div>
      </div>
      <div class="hgrid g3">
        <div class="spark"><span class="tiny muted mono">‖grad‖</span><canvas id="t-sp1"></canvas></div>
        <div class="spark"><span class="tiny muted mono">lr</span><canvas id="t-sp2"></canvas></div>
        <div class="spark"><span class="tiny muted mono">VRAM ГБ</span><canvas id="t-sp3"></canvas></div>
      </div>
      <div class="sec-t"><h3>Прогоны</h3><span class="tiny muted">до 3 к сравнению</span></div>
      <div id="t-runs"></div>`;
    tip = $("#t-tip");
    chart = new Charts.LineChart($("#t-chart"), {
      tooltip: tip, window: 0, gridY: 4, gridX: 4, log: false,
      yFmt: v => metric === "stab" ? v.toFixed(1) + "%" : v.toFixed(2),
      xFmt: v => Math.round(v) + "%",
    });
    $("#t-metric").addEventListener("click", e => {
      const b = e.target.closest("[data-m]"); if (!b) return;
      metric = b.dataset.m;
      if (metric !== "loss") { logOn = false; }
      $$("#t-metric button").forEach(x => x.classList.toggle("on", x.dataset.m === metric));
      $("#t-log").style.display = metric === "loss" ? "" : "none";
      $("#t-log").classList.toggle("on", false);
      chart.setLog(false);
      redraw(); TG.haptic("sel");
    });
    $("#t-log").addEventListener("click", () => {
      logOn = !logOn; $("#t-log").classList.toggle("on", logOn); chart.setLog(logOn); TG.haptic("sel");
    });
    $("#t-compare").addEventListener("click", () => CompareSheet.open());
    const back = $("#t-live-back");
    if (back) back.addEventListener("click", () => { compareSel = null; render(); });
    // спарклайны-данные
    if (!sparks.grad.length) {
      let g = 1.9, lr = 3e-3;
      for (let i = 0; i < 40; i++) { g = Math.max(.22, g * .955 + (Math.random() - .5) * .07); sparks.grad.push(+g.toFixed(3)); lr = 3e-4 + (3e-3 - 3e-4) * .5 * (1 + Math.cos(Math.PI * i / 39)); sparks.lr.push(+(lr * 1e3).toFixed(2)); }
      sparks.vram = Data.gpu.vramHist.slice(-40);
    }
    drawRunCard(); drawRuns(); redraw();
  }

  function drawRunCard() {
    const box = $("#t-run"); if (!box) return;
    const r = Data.run;
    box.innerHTML = r ? `
      <div class="card hair">
        <div class="between" style="margin-bottom:8px">
          <div class="row" style="gap:9px"><span class="mono" style="font-weight:800;color:var(--accent)">${r.hid}</span>${UI.lvBadge(r.level)}</div>
          ${UI.statusChip("running")}
        </div>
        <div class="small" style="color:var(--text-2);margin-bottom:10px">${esc(r.title)}</div>
        <div class="prog"><i style="width:${Math.round(r.progress * 100)}%"></i></div>
        <div class="between tiny muted" style="margin-top:7px">
          <span class="mono">${Math.round(r.progress * 100)}% бюджета прогона</span>
          <span class="mono">~${r.eta} мин до вердикта уровня</span>
        </div>
      </div>` : `
      <div class="card">
        <div class="row" style="gap:10px">
          <div class="ap-ico" style="background:var(--surface-2);color:var(--muted)">${icon("check")}</div>
          <div class="grow"><b style="font-size:14px">GPU свободен — стрим остановлен</b>
          <div class="tiny muted" style="margin-top:2px">на графике — последний завершённый прогон</div></div>
        </div>
      </div>`;
  }

  function drawRuns() {
    const box = $("#t-runs"); if (!box) return;
    box.innerHTML = Data.runs.map(r => `
      <div class="run-row ${compareSel && compareSel.includes(r.id) ? "sel" : ""}" data-run="${r.id}">
        <div class="rr">${icon(r.live ? "pulse" : "doc")}</div>
        <div class="grow"><b class="mono small" style="font-weight:800">${r.id}</b> <span class="lv lv${r.level[1]}" style="margin-left:4px">${r.level}</span>
          <div class="tiny muted">${r.hid} · ${esc(r.note)}</div></div>
        ${icon("chev-r")}
      </div>`).join("");
    $$(".run-row", box).forEach(row => row.addEventListener("click", () => {
      const id = row.dataset.run;
      const run = Data.runs.find(x => x.id === id); if (!run || run.live) return;
      compareSel = compareSel || [];
      if (compareSel.includes(id)) compareSel = compareSel.filter(x => x !== id);
      else if (compareSel.length >= 3) { Toast.show("максимум 3 прогона", "warn"); return; }
      else compareSel.push(id);
      if (!compareSel.length) compareSel = null;
      render();
    }));
  }

  function redraw() {
    if (!chart) return;
    chart.opts.yFmt = metric === "stab" ? v => v.toFixed(1) + "%" : v => v.toFixed(2);
    chart.setData(runSeries());
    const t = Data.tele;
    $("#t-chart-title").textContent = compareSel ? "сравнение прогонов" : metric === "loss" ? "функция потерь · 3 seeds" : metric === "rank" ? "эффективный ранг весов" : "разброс между seeds (CV, %)";
    $("#t-live-flag").style.display = (t && !compareSel) ? "" : "none";
    const leg = $("#t-legend"); if (!leg) return;
    leg.innerHTML = chart.series.map(s => `
      <span class="lg ${s.hidden ? "off" : ""}" data-lg="${s.id}"><i style="background:${s.color}"></i>${s.name}</span>`).join("");
    $$(".lg", leg).forEach(x => x.addEventListener("click", () => {
      chart.toggle(x.dataset.lg); redraw(); TG.haptic("sel");
    }));
  }

  function tick() {
    if (!chart) return;
    // спарклайны
    const g = sparks.grad, l = sparks.lr, v = sparks.vram;
    g.push(+Math.max(.22, g[g.length - 1] * .99 + (Math.random() - .5) * .06).toFixed(3)); if (g.length > 40) g.shift();
    l.push(l.shift()); // косинусное расписание крутится, а не вырождается в прямую
    v.push(Data.gpu.vram); if (v.length > 40) v.shift();
    Charts.spark($("#t-sp1"), g, "#8b7cff");
    Charts.spark($("#t-sp2"), l, "#4fc3ff", { min: 0 });
    Charts.spark($("#t-sp3"), v, "#3be0a0", { min: 0, max: 32 });
    if (Data.run) {
      const p = $("#t-run .prog>i"); if (p) p.style.width = Math.round(Data.run.progress * 100) + "%";
    }
  }

  Data.on("tele", () => { if (!compareSel) redraw(); });

  function setCompare(ids) {
    compareSel = ids && ids.length ? ids : null;
    render();
  }

  return { render, tick, get metric() { return metric; }, redraw, setCompare };
})();

/* Шторка сравнения прогонов */
const CompareSheet = (() => {
  function open() {
    const item = Sheet.open(`
      <div class="sheet-head"><div class="grow"><div class="sheet-title">Сравнение прогонов</div>
      <div class="sheet-sub">отметьте до 3 прогонов — кривые наложатся на один график</div></div></div>
      <div class="sheet-body" id="cmp-list" style="padding-top:4px">
        ${Data.runs.filter(r => !r.live).map(r => `
          <div class="run-row" data-run="${r.id}" style="cursor:pointer">
            <div class="rr" style="color:${r.color}">${icon("doc")}</div>
            <div class="grow"><b class="mono small" style="font-weight:800">${r.id}</b> <span class="lv lv${r.level[1]}" style="margin-left:4px">${r.level}</span>
              <div class="tiny muted">${r.hid} · ${esc(r.note)}</div></div>
            <span class="cb wait" style="border-radius:8px;width:24px;height:24px;display:flex;align-items:center;justify-content:center;background:var(--surface-2)"></span>
          </div>`).join("")}
        <div style="height:10px"></div>
      </div>
      <div class="sheet-cta"><button class="btn btn-primary grow" id="cmp-go" disabled>${icon("compare")}Наложить кривые</button></div>`,
      {
        onOpen(sheet) {
          const sel = new Set();
          const go = $("#cmp-go", sheet);
          const upd = () => { go.disabled = sel.size === 0; go.textContent = sel.size ? `Наложить кривые (${sel.size})` : "Наложить кривые"; };
          $$(".run-row", sheet).forEach(row => {
            row.addEventListener("click", () => {
              const id = row.dataset.run;
              if (sel.has(id)) { sel.delete(id); row.classList.remove("sel"); }
              else if (sel.size >= 3) { Toast.show("максимум 3 прогона", "warn"); return; }
              else { sel.add(id); row.classList.add("sel"); }
              row.querySelector(".cb").innerHTML = sel.has(id) ? icon("check") : "";
              row.querySelector(".cb").style.color = sel.has(id) ? "var(--accent)" : "var(--faint)";
              upd(); TG.haptic("light");
            });
          });
          go.addEventListener("click", () => {
            Tele.setCompare([...sel]);
            item.close();
            Toast.show(sel.size + " прогона наложены · коснитесь графика", "info");
          });
        }
      });
  }
  return { open };
})();

/* ================= ЭКИПАЖ ================= */
const Crew = (() => {
  let seg = "chat";
  const SEGS = [["chat", "Чат"], ["market", "Ставки"], ["review", "Ревью"]];
  let renderedDisputes = new Set();

  function chatHTML() {
    const list = Data.chat.slice(-60);
    let html = "";
    let prevAgent = null;
    list.forEach(m => {
      if (m.kind === "sys") { html += `<div class="sysmsg"><span>${esc(m.text)}</span></div>`; prevAgent = null; return; }
      const a = UI.agentOf(m.agent);
      const showHead = m.agent !== prevAgent;
      prevAgent = m.agent;
      html += `
        <div class="msg">
          ${UI.avatar(m.agent)}
          <div class="bd">
            ${showHead ? `<div class="nm"><b style="color:${a.color}">${esc(a.name)}</b><span>${esc(a.zone)}</span><time>${Data.fmtClock(m.ts)}</time></div>` : ""}
            <div class="txt">${esc(m.text)}</div>
          </div>
        </div>`;
      if (m.disputeOpen) {
        const d = Data.disputes.find(x => x.id === m.disputeOpen);
        if (d) { html += disputeHTML(d); renderedDisputes.add(d.id); }
      }
    });
    // открытые споры, ещё не показанные в ленте — в конец
    Data.disputes.forEach(d => {
      if (!renderedDisputes.has(d.id) && seg === "chat") { html += disputeHTML(d); renderedDisputes.add(d.id); }
    });
    return html;
  }

  function disputeHTML(d) {
    const total = d.z + d.p || 1;
    const zPct = Math.round(d.z / total * 100);
    const voted = d.myVote;
    const closed = d.status === "closed";
    return `
      <div class="dispute" data-d="${d.id}">
        <div class="dp-h">${icon("scale")}СПОР · ${d.id} · ${d.hid}${closed ? " · закрыт арбитражем" : ""}</div>
        <p><b>${esc(d.topic)}</b></p>
        <p class="tiny">за: ${esc(d.zArgs || "")} · против: ${esc(d.pArgs || "")}</p>
        <div class="vote-split"><i class="vz" style="width:${zPct}%"></i><i class="vp" style="width:${100 - zPct}%"></i></div>
        <div class="between tiny" style="margin-bottom:8px">
          <span style="color:var(--ok)">взлетит · ${d.z}</span><span style="color:var(--danger)">не взлетит · ${d.p}</span>
        </div>
        ${closed || voted ? `
          <div class="tiny muted" style="text-align:center;padding:2px 0">${voted ? `ваш голос: «${voted === "z" ? "взлетит" : "не взлетит"}» · ` : ""}голосование ${closed ? "закрыто" : "идёт"}</div>
        ` : `
          <div class="vote-btns">
            <button class="btn btn-ok btn-sm" data-action="vote" data-d="${d.id}" data-side="z">${icon("up")}Взлетит</button>
            <button class="btn btn-danger btn-sm" data-action="vote" data-d="${d.id}" data-side="p">${icon("x")}Не взлетит</button>
          </div>`}
      </div>`;
  }

  function marketHTML() {
    const mk = Data.market;
    const open = mk.open.map(b => {
      const total = b.for.length + b.against.length || 1;
      const zp = Math.round(b.for.length / total * 100);
      const myBet = Data.myBets[b.hid];
      return `
        <div class="card">
          <div class="between" style="margin-bottom:2px"><span class="mono small" style="font-weight:800;color:var(--accent)">${b.hid}</span>${UI.lvBadge(b.level)}</div>
          <div class="small" style="line-height:1.4;margin-bottom:4px">${esc(b.title)}</div>
          <div class="mkt-bar"><i style="width:${zp}%;background:var(--ok)"></i><i style="width:${100 - zp}%;background:var(--danger)"></i></div>
          <div class="row" style="justify-content:space-between">
            <div class="row" style="gap:4px;flex-wrap:wrap">${b.for.map(a => a === "human" ? UI.avatarMini("shef") : UI.avatarMini(a)).join("")}</div>
            <div class="row" style="gap:4px;flex-wrap:wrap">${b.against.map(a => a === "human" ? UI.avatarMini("shef") : UI.avatarMini(a)).join("")}</div>
          </div>
          ${myBet ? `<div class="tiny" style="color:var(--muted);margin-top:8px;text-align:center">ваша ставка: «${myBet === "for" ? "взлетит" : "не взлетит"}» · до вердикта</div>` : `
          <div class="row" style="gap:8px;margin-top:9px">
            <button class="btn btn-ok btn-sm grow" data-action="bet" data-h="${b.hid}" data-side="for">${icon("up")}Ставлю: взлетит</button>
            <button class="btn btn-danger btn-sm grow" data-action="bet" data-h="${b.hid}" data-side="against">${icon("x")}не взлетит</button>
          </div>`}
        </div>`;
    }).join("") || `<div class="empty">${icon("scale")}<b>Открытых ставок нет</b><span>ставки появляются перед каждым прогоном уровня</span></div>`;

    const rates = [...mk.ratings].sort((a, b) => b.hit / b.total - a.hit / a.total).map(r => {
      const a = UI.agentOf(r.agent);
      const pct = Math.round(r.hit / r.total * 100);
      return `
        <div class="rate-row">
          ${UI.avatar(r.agent, 32)}
          <div class="grow">
            <div class="row" style="gap:7px"><span class="rr-n" style="color:${a.color}">${esc(a.name)}</span>
            ${r.streak >= 2 ? `<span class="streak">${icon("flame")}${r.streak}</span>` : r.streak <= -2 ? `<span class="streak" style="color:var(--faint)">↓${-r.streak}</span>` : ""}</div>
            <div class="meter thin" style="margin-top:5px"><i style="width:${pct}%"></i></div>
          </div>
          <div style="text-align:right"><div class="rr-a" style="color:${pct >= 60 ? "var(--ok)" : pct >= 45 ? "var(--warn)" : "var(--danger)"}">${pct}%</div>
          <div class="tiny muted mono">${r.hit}/${r.total}</div></div>
        </div>`;
    }).join("");

    const resolved = mk.resolved.map(r => `
      <div class="evt">
        <span class="evt-ico" style="background:${r.outcome === "confirmed" ? "var(--ok-dim);color:var(--ok)" : "var(--danger-dim);color:var(--danger)"}">${icon(r.outcome === "confirmed" ? "check" : "x")}</span>
        <p><b>${r.hid}</b> — ${r.outcome === "confirmed" ? "подтверждена" : "отвергнута"}. угадали: ${r.right.map(a => UI.agentOf(a).name).join(", ")}</p>
      </div>`).join("");

    return `
      <div class="sec-t" style="margin-top:2px"><h3>Открытые ставки</h3><span class="tiny muted">до вердикта</span></div>${open}
      <div class="sec-t"><h3>Точность экипажа</h3><span class="tiny muted">калибруется по вердиктам</span></div>
      <div class="card" style="padding:6px 14px">${rates}</div>
      <div class="sec-t"><h3>Рассчитанные ставки</h3></div>
      <div class="card" style="padding:6px 14px">${resolved}</div>`;
  }

  function reviewHTML() {
    const sev = { high: ["высокая", "chip-danger"], mid: ["средняя", "chip-warn"], low: ["низкая", ""] };
    return Data.findings.map(f => `
      <div class="card fnd ${f.severity}">
        <div class="between" style="margin-bottom:5px">
          <span class="mono small" style="font-weight:800">${f.id}</span>
          <span class="chip ${sev[f.severity][1]}">${sev[f.severity][0]}</span>
        </div>
        <div style="font-size:13.5px;font-weight:700;line-height:1.35">${esc(f.subject)}</div>
        <div class="tiny muted" style="margin-top:3px">${esc(f.kind)}</div>
        <div class="between" style="margin-top:9px">
          <span class="tiny muted">нашёл: ${UI.agentOf(f.by).name} · чинит: ${UI.agentOf(f.fixer).name}</span>
          <span class="chip ${f.status === "fixed" ? "chip-ok" : "chip-warn"}">${f.status === "fixed" ? icon("check") + "закрыто" : "открыто"}</span>
        </div>
      </div>`).join("");
  }

  function render() {
    const sc = $("#sc-crew");
    renderedDisputes = new Set();
    sc.innerHTML = `
      <div class="seg" id="c-seg" style="margin:2px 0 12px">${SEGS.map(([k, l]) => `<button data-seg="${k}" class="${k === seg ? "on" : ""}">${l}</button>`).join("")}</div>
      <div id="c-list"></div>`;
    fill();
    $("#c-seg").addEventListener("click", e => {
      const b = e.target.closest("[data-seg]"); if (!b) return;
      seg = b.dataset.seg;
      $$("#c-seg button").forEach(x => x.classList.toggle("on", x.dataset.seg === seg));
      renderedDisputes = new Set();
      fill(); TG.haptic("sel");
    });
  }

  function fill() {
    const box = $("#c-list"); if (!box) return;
    if (seg === "chat") {
      box.innerHTML = chatHTML();
      requestAnimationFrame(() => { const sc = $("#sc-crew"); sc.scrollTop = sc.scrollHeight; });
    } else if (seg === "market") box.innerHTML = marketHTML();
    else box.innerHTML = reviewHTML();
  }

  function onChat(m) {
    if (seg !== "chat" || !$("#c-list")) return;
    const sc = $("#sc-crew");
    const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 240;
    fill();
    if (!nearBottom) sc.scrollTop = sc.scrollHeight;
  }

  return { render, refresh: fill, onChat, get seg() { return seg; } };
})();

window.Tele = Tele; window.Crew = Crew; window.CompareSheet = CompareSheet;
