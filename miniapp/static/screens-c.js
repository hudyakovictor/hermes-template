/* ============================================================
   screens-c.js — ИТОГИ (Verdicts) + ПОДАЧА ИДЕЙ (Idea Intake)
   ============================================================ */
"use strict";

/* ================= ИТОГИ ================= */
const Verd = (() => {
  let filter = "all";
  const FILTERS = [["all", "Все"], ["confirmed", "Подтверждено"], ["rejected", "Отвергнуто"], ["killed", "Убито"], ["partial", "Частично"]];

  const ST = {
    confirmed: { chip: "chip-ok", ico: "check", label: "подтверждена" },
    rejected: { chip: "chip-danger", ico: "x", label: "отвергнута" },
    killed: { chip: "", ico: "kill", label: "убита до GPU" },
    partial: { chip: "chip-warn", ico: "alert", label: "частично" },
  };

  function calibHTML() {
    const total = Data.verdicts.filter(v => v.actual != null).length || 1;
    const inC = Data.verdicts.filter(v => v.actual != null && v.actual >= v.forecast.lo && v.actual <= v.forecast.hi).length;
    const pct = Math.round(inC / total * 100);
    return `
      <div class="card hair">
        <div class="donut-wrap">
          <div class="donut" id="v-donut"></div>
          <div class="grow stack" style="gap:7px">
            <b style="font-size:14.5px">Калибровка прогнозов</b>
            <span class="small" style="color:var(--text-2);line-height:1.5">${inC} из ${total} вердиктов попали в обещанный коридор — прогнозы фиксируются до прогона, вердикт без прогноза невозможен.</span>
            <span class="tiny muted">веса PI пересчитываются каждое воскресенье · последняя: ${Data.fmtAgo(Data.budget.lastCalib)}</span>
          </div>
        </div>
      </div>`;
  }

  function cardHTML(v) {
    const st = ST[v.status];
    const f = v.forecast, unit = f.unit === "%" ? "%" : " " + f.unit;
    const inC = v.actual != null && v.actual >= f.lo && v.actual <= f.hi;
    const w = x => clamp((x - f.min) / (f.max - f.min) * 100, 2, 100);
    return `
      <div class="card link-row" data-action="v-open" data-h="${v.id}">
        <div class="qi">
          <span class="chip ${st.chip}" style="padding:4px 10px">${icon(st.ico)}${st.label}</span>
          <span class="mono small" style="font-weight:800">${v.id}</span>
          ${UI.lvBadge(v.level)}
          ${v.patent ? `<span class="chip chip-violet">${icon("star")}${v.patent}</span>` : ""}
          <span class="grow"></span><span class="tiny muted mono">${Data.fmtAgo(v.ago)}</span>
        </div>
        <h4 style="font-size:13.8px">${esc(v.title)}</h4>
        ${v.actual != null ? `
        <div class="dual">
          <div class="d-row"><b>обещали</b><div class="d-bar f"><i style="width:${w(f.point)}%"></i></div><span>${f.point}${unit}</span></div>
          <div class="d-row"><b>получили</b><div class="d-bar ${inC ? "a" : "bad"}"><i style="width:${w(v.actual)}%"></i></div><span style="color:${inC ? "var(--ok)" : "var(--danger)"}">${v.actual}${unit}</span></div>
        </div>
        <div class="between" style="margin-top:8px">
          <span class="tiny muted">коридор ${f.lo}…${f.hi}${unit}</span>
          ${v.actual != null ? `<span class="delta-chip" style="color:${inC ? "var(--ok)" : "var(--danger)"};background:${inC ? "var(--ok-dim)" : "var(--danger-dim)"}">${inC ? "в коридоре" : "мимо на " + nf(Math.abs(v.actual - (v.actual < f.lo ? f.lo : f.hi)), Math.abs(f.hi - f.lo) < 1 ? 2 : 0) + unit}</span>` : ""}
        </div>` : `
        <div class="tiny muted" style="margin-top:8px">до GPU не дошла — ${v.status === "killed" ? "часы не потрачены" : ""}</div>`}
      </div>`;
  }

  function patentsHTML() {
    return Data.patents.map(p => `
      <div class="card" data-action="patent-open" data-h="${p.id}">
        <div class="between" style="margin-bottom:5px">
          <span class="chip chip-violet">${icon("star")}${p.id} · ${p.status === "draft" ? "черновик claim" : "кандидат"}</span>
          <span class="mono small muted">${p.hid}</span>
        </div>
        <div style="font-size:13.5px;font-weight:700;line-height:1.4">${esc(p.title)}</div>
        <div class="tiny muted" style="margin-top:5px;line-height:1.5">${esc(p.claim.slice(0, 110))}…</div>
        <button class="btn btn-ghost btn-sm" style="margin-top:10px" data-action="patent-open" data-h="${p.id}">${icon("doc")}Открыть и экспортировать</button>
      </div>`).join("");
  }

  function render() {
    const sc = $("#sc-verd");
    const list = Data.verdicts.filter(v => filter === "all" || v.status === filter);
    sc.innerHTML = `
      ${calibHTML()}
      <div class="filters" id="v-filters">${FILTERS.map(([k, l]) => `<span class="chip ${k === filter ? "on" : ""}" data-f="${k}">${l} · ${k === "all" ? Data.verdicts.length : Data.verdicts.filter(v => v.status === k).length}</span>`).join("")}</div>
      <div id="v-list">${list.map(cardHTML).join("") || `<div class="empty">${icon("shield")}<b>Пусто</b><span>под этот фильтр вердиктов нет</span></div>`}</div>
      <div class="sec-t"><h3>Патентные заготовки</h3><span class="tiny muted">${Data.patents.length} шт</span></div>
      <div id="v-patents">${patentsHTML()}</div>`;
    const d = $("#v-donut");
    Charts.donut(d, [
      { value: Data.verdicts.filter(v => v.actual != null && v.actual >= v.forecast.lo && v.actual <= v.forecast.hi).length, color: "#3be0a0" },
      { value: Data.verdicts.filter(v => v.actual != null && !(v.actual >= v.forecast.lo && v.actual <= v.forecast.hi)).length, color: "#ff6161" },
      { value: Data.verdicts.filter(v => v.actual == null).length, color: "#586180" },
    ]);
    d.insertAdjacentHTML("beforeend", `<div class="donut-c"><b id="v-donut-n"></b><span>в коридоре</span></div>`);
    $("#v-donut-n").textContent = Math.round(Data.verdicts.filter(v => v.actual != null && v.actual >= v.forecast.lo && v.actual <= v.forecast.hi).length / (Data.verdicts.filter(v => v.actual != null).length || 1) * 100) + "%";
    $("#v-filters").addEventListener("click", e => {
      const c = e.target.closest("[data-f]"); if (!c) return;
      filter = c.dataset.f;
      $$("#v-filters .chip").forEach(x => x.classList.toggle("on", x.dataset.f === filter));
      $("#v-list").innerHTML = Data.verdicts.filter(v => filter === "all" || v.status === filter).map(cardHTML).join("") || `<div class="empty">${icon("shield")}<b>Пусто</b><span>под этот фильтр вердиктов нет</span></div>`;
      TG.haptic("sel");
    });
  }

  function openVerdict(hid) {
    const v = Data.verdicts.find(x => x.id === hid); if (!v) return;
    const f = v.forecast, unit = f.unit === "%" ? "%" : " " + f.unit;
    const st = ST[v.status];
    Sheet.open(`
      <div class="sheet-head">
        <span class="chip ${st.chip}">${icon(st.ico)}${st.label}</span>
        <div class="grow"><div class="sheet-title mono">${v.id} ${UI.lvBadge(v.level)}</div>
        <div class="sheet-sub">закрыт ${Data.fmtAgo(v.ago)} · ${esc(v.term || "")}</div></div>
      </div>
      <div class="sheet-body">
        <h3 style="font-size:16px;line-height:1.4;margin-bottom:12px">${esc(v.title)}</h3>

        <div class="sec-t" style="margin-top:0"><h3>Обещали vs получили</h3></div>
        <div class="card" style="margin:0">
          ${UI.corridorHTML(f, { actual: v.actual })}
          ${v.actual != null ? `
          <div class="kv" style="margin-top:10px"><b>точечный прогноз</b><span>${f.point}${unit}</span></div>
          <div class="kv"><b>факт</b><span style="color:${v.actual >= f.lo && v.actual <= f.hi ? "var(--ok)" : "var(--danger)"}">${v.actual}${unit}</span></div>
          <div class="kv"><b>метрика</b><span style="font-family:inherit;font-size:12px">${esc(f.metric)}</span></div>` : `
          <div class="kv"><b>GPU-часы</b><span>0.0 — проверена до запуска</span></div>`}
        </div>

        <div class="sec-t"><h3>Урок в память</h3></div>
        <div class="card" style="margin:0"><p class="small" style="line-height:1.6;color:var(--text-2)">${esc(v.lesson)}</p>
        <div class="tiny" style="color:var(--muted);margin-top:9px">${icon("shield")} соседние гипотезы проверены на тот же дефект: ${v.neighbors ? "да" : "нет"}</div></div>

        ${v.patent ? `
        <div class="sec-t"><h3>Патентная линия</h3></div>
        <div class="card" style="margin:0;border-color:color-mix(in srgb,var(--violet) 35%,transparent)">
          <div class="row" style="gap:8px">${icon("star")}<b class="small">${v.patent} · заготовка claim</b></div>
          <p class="tiny" style="color:var(--text-2);line-height:1.6;margin-top:7px">${esc((Data.patents.find(p => p.id === v.patent) || {}).claim || "")}</p>
        </div>` : ""}
        <div style="height:12px"></div>
      </div>
      <div class="sheet-cta">
        <button class="btn btn-primary grow" data-action="v-export" data-h="${v.id}">${icon("share")}Экспорт отчёта</button>
      </div>`);
  }

  function refresh() {
    if (!$("#sc-verd")) return;
    render();
  }

  return { render, openVerdict, refresh };
})();

/* ================= ПОДАЧА ИДЕИ ================= */
const Idea = (() => {
  let step = 1;
  let form = { mech: "", metric: "Δ val loss", test: "", pass: "", market: "", signals: 3 };
  let dupResult = null, checking = false, result = null;

  const MARKETS = [["cloud", "облачные провайдеры"], ["train", "training-платформы"], ["edge", "edge / on-device"], ["auto", "автопилот и робототехника"], ["science", "научные пакеты"], ["ip", "продажа IP / лицензия"]];

  function quality() {
    const words = form.mech.trim().split(/\s+/).filter(Boolean);
    const hasNum = /\d/.test(form.mech);
    const terms = ["ранн", "порог", "ступень", "структур", "контур", "признак", "зонд", "конденсац", "ранг", "знак", "частот", "мемор", "обобщен", "pruning", "маски", "слой"].some(t => form.mech.toLowerCase().includes(t));
    let score = 0;
    if (words.length >= 6) score++;
    if (words.length >= 14) score++;
    if (hasNum) score++;
    if (terms) score++;
    if (form.pass && /\d/.test(form.pass)) score++;
    return score; // 0..5
  }

  function dupCheck() {
    checking = true; dupResult = null;
    const box = $("#i-dup", sheetEl()); if (box) box.innerHTML = `<div class="row small" style="gap:8px;color:var(--muted)"><span class="dot" style="background:var(--accent);animation:blink 1s infinite"></span>проверяю против ${Data.verdicts.length} закрытых идей и памяти контура…</div>`;
    setTimeout(() => {
      checking = false;
      const tokens = (form.mech + " " + form.metric).toLowerCase().split(/[^a-zа-яё0-9-]+/).filter(w => w.length > 4);
      const dups = [];
      Data.verdicts.forEach(v => {
        const dict = (v.title + " " + v.term + " " + v.lesson).toLowerCase();
        let hits = 0;
        tokens.forEach(t => { if (dict.includes(t)) hits++; });
        const hay = (v.title + " " + v.term).toLowerCase();
        const sim = clamp(Math.round(hits * 18 + (tokens.some(t => hay.includes(t)) ? 22 : 0)), 3, 96);
        if (sim >= 30) dups.push({ id: v.id, title: v.title, why: v.status === "killed" ? "закрыта: " + v.lesson.slice(0, 66) + "…" : "вердикт: " + ST_LABEL[v.status], sim, status: v.status });
      });
      dups.sort((a, b) => b.sim - a.sim);
      dupResult = dups.slice(0, 2);
      paintDup();
    }, 700 + Math.random() * 500);
  }

  const ST_LABEL = { confirmed: "подтверждена", rejected: "отвергнута", killed: "убита до GPU", partial: "частично" };

  function paintDup() {
    const box = $("#i-dup", sheetEl()); if (!box) return;
    if (checking) return;
    if (!dupResult || !dupResult.length) {
      box.innerHTML = `<div class="row small" style="gap:8px;color:var(--ok)">${icon("check")}совпадений с закрытыми идеями не найдено — формулировка проходит дедупликацию</div>`;
      return;
    }
    box.innerHTML = dupResult.map(d => `
      <div class="dup ${d.sim > 80 ? "high" : ""}">
        <div class="between"><b class="small mono">${d.id} · ${d.sim}% схожести</b><span class="tiny muted">${ST_LABEL[d.status]}</span></div>
        <div class="sim-bar"><i style="width:${d.sim}%;background:${d.sim > 80 ? "var(--danger)" : d.sim > 55 ? "var(--warn)" : "var(--ok)"}"></i></div>
        <div class="tiny" style="color:var(--text-2);margin-top:4px">${esc(d.title)}</div>
        <div class="tiny muted" style="margin-top:3px">${esc(d.why)}</div>
      </div>`).join("")
      + (dupResult[0].sim > 80 ? `<div class="row small" style="gap:7px;color:var(--danger);margin-top:9px">${icon("alert")}почти дубль: уточните, чем механизм отличается от ${dupResult[0].id}</div>` : "");
  }

  function sheetEl() { return document.querySelector(".sheet"); }

  function stepHTML() {
    const q = quality();
    if (step === 1) return `
      <div class="field">
        <label>Суть механизма <em>что и почему должно сработать</em></label>
        <textarea class="ta" id="f-mech" rows="4" placeholder="Например: если доля стабильных знаков весов переваливает порог в первые 4% обучения, полезный контур уже собран — дальше учится только память, и обучение можно останавливать">${esc(form.mech)}</textarea>
        <div class="row" style="justify-content:space-between;margin-top:6px">
          <span class="tiny muted" id="f-mech-cnt">0 знаков</span>
          <span class="tiny muted">качество формулировки:</span>
        </div>
        <div class="q-meter" id="f-q">${[1, 2, 3, 4, 5].map(i => `<i class="${q >= i ? "on" : ""}"></i>`).join("")}</div>
      </div>
      <div id="i-dup"></div>`;
    if (step === 2) return `
      <div class="field">
        <label>Целевая метрика</label>
        <div class="chip-select" id="f-metric">
          ${["Δ val loss", "Δ compute", "ускорение ×", "корреляция r", "Δ VRAM"].map(m => `<span class="chip ${form.metric === m ? "on" : ""}" data-v="${m}">${m}</span>`).join("")}
        </div>
      </div>
      <div class="field">
        <label>Дешёвый минимальный тест <em>L0 ≤ 5 мин</em></label>
        <textarea class="ta" id="f-test" rows="2" placeholder="toy-сетка 2 слоя, синтетика, один сид — 3 минуты GPU">${esc(form.test)}</textarea>
      </div>
      <div class="field">
        <label>PASS-критерий числами <em>зафиксируется до запуска</em></label>
        <input class="inp" id="f-pass" placeholder="эффект ≥ −18% при p &lt; 0.05 на 3 seeds" value="${esc(form.pass)}">
      </div>
      <div class="field">
        <label>Независимых сигналов в вашу пользу</label>
        <div class="chip-select" id="f-signals">${[2, 3, 4, 5].map(n => `<span class="chip ${form.signals === n ? "on" : ""}" data-v="${n}">${n}${n < 3 ? " · мало" : ""}</span>`).join("")}</div>
        <div class="hint">меньше 3 независимых сигналов — kill-stage завернёт до GPU (S=0 в формуле PI)</div>
      </div>`;
    // step 3
    return `
      <div class="field">
        <label>Кому это продадим <em>покупатель или сценарий экономии</em></label>
        <div class="chip-select" id="f-market">${MARKETS.map(([k, l]) => `<span class="chip ${form.market === k ? "on" : ""}" data-v="${k}">${l}</span>`).join("")}</div>
      </div>
      <div class="field">
        <label>Ожидаемый эффект для покупателя</label>
        <input class="inp" id="f-eff" placeholder="−25% счёта за обучение при том же качестве" value="${esc(form.eff || "")}">
      </div>
      <div class="card hair" style="margin-top:4px">
        <div class="row" style="gap:14px">
          <div class="score-orb" id="i-orb"></div>
          <div class="grow stack" style="gap:6px">
            <b style="font-size:14px">Симуляция приоритета</b>
            <span class="small" style="color:var(--text-2)" id="i-sim-text">…</span>
          </div>
        </div>
      </div>
      <div class="tiny" style="color:var(--faint);margin-top:10px;line-height:1.6">Отправка фиксирует прогноз до запуска: вердикт без зафиксированного прогноза в этом контуре невозможен. Идею примет iВасёк, kill-stage проверит за 0 GPU-часов.</div>`;
  }

  function paintOrb() {
    const orb = $("#i-orb"); if (!orb) return;
    const res = simScore();
    const g = Charts.ring(orb, { size: 86, stroke: 8, value: 0, max: 1, cls: "" });
    orb.querySelector(".ring-c").innerHTML = `<div class="ring-num" style="font-size:19px">${res.ppi.toFixed(2)}</div><div class="ring-lbl">PPI</div>`;
    g.set(res.pi);
    $("#i-sim-text").innerHTML = `PI <b class="mono">${res.pi.toFixed(2)}</b> · корзина <b>${res.bin}</b> · ${res.blocked
      ? `<span style="color:var(--danger)">дубль: контур предложит уточнить формулировку</span>`
      : `встанет в очередь <b>${1 + Math.min(2, Math.floor(res.ppi))}</b>-й · старт после текущего прогона`}`;
  }

  function simScore() {
    const q = quality();
    const pi = piOf({ s: sigScore(form.signals), n: dupResult && dupResult[0] && dupResult[0].sim > 75 ? .25 : .75, e: .8, q: form.test ? .6 : .35, m: form.market ? .7 : .3, d: form.pass && /\d/.test(form.pass) ? .75 : .4, aging: 0 });
    return { pi, ppi: +(pi / 1.6).toFixed(2), bin: binOf(1.6), blocked: dupResult && dupResult[0] && dupResult[0].sim > 80 };
  }

  function bindStep(sheet) {
    if (step === 1) {
      const ta = $("#f-mech", sheet);
      ta.addEventListener("input", () => {
        form.mech = ta.value;
        $("#f-mech-cnt", sheet).textContent = form.mech.length + " знаков";
        const q = quality();
        $$("#f-q i", sheet).forEach((i, ix) => i.className = q >= ix + 1 ? "on" : "");
        const cta = $("#i-cta", sheet);
        if (cta) cta.disabled = !canNext();
        clearTimeout(bindStep._t);
        if (form.mech.trim().length > 24) bindStep._t = setTimeout(dupCheck, 650);
      });
      if (form.mech.trim().length > 24 && !dupResult && !checking) dupCheck(); else paintDup();
    }
    if (step === 2) {
      $("#f-metric", sheet).addEventListener("click", e => {
        const c = e.target.closest("[data-v]"); if (!c) return;
        form.metric = c.dataset.v;
        $$("#f-metric .chip", sheet).forEach(x => x.classList.toggle("on", x.dataset.v === form.metric));
        TG.haptic("sel");
      });
      $("#f-test", sheet).addEventListener("input", e => form.test = e.target.value);
      $("#f-pass", sheet).addEventListener("input", e => form.pass = e.target.value);
      $("#f-signals", sheet).addEventListener("click", e => {
        const c = e.target.closest("[data-v]"); if (!c) return;
        form.signals = +c.dataset.v;
        $$("#f-signals .chip", sheet).forEach(x => x.classList.toggle("on", +x.dataset.v === form.signals));
        TG.haptic("sel");
      });
    }
    if (step === 3) {
      $("#f-market", sheet).addEventListener("click", e => {
        const c = e.target.closest("[data-v]"); if (!c) return;
        form.market = c.dataset.v;
        $$("#f-market .chip", sheet).forEach(x => x.classList.toggle("on", x.dataset.v === form.market));
        TG.haptic("sel"); paintOrb();
      });
      $("#f-eff", sheet).addEventListener("input", e => form.eff = e.target.value);
      paintOrb();
    }
    // шаги-индикатор
    $$(".steps i", sheet).forEach((i, ix) => i.className = ix < step ? "done" : "");
    const title = $("#i-title", sheet), cta = $("#i-cta", sheet);
    if (title) title.textContent = ["Механизм", "Метрика и тест", "Рынок и приоритет"][step - 1];
    if (cta) {
      if (step < 3) { cta.innerHTML = `${icon("chev-r")}Далее`; }
      else { cta.innerHTML = `${icon("bolt")}Отправить в очередь`; cta.classList.remove("btn-primary"); cta.classList.add("btn-primary"); }
    }
  }

  function canNext() {
    if (step === 1) return form.mech.trim().length > 24;
    if (step === 2) return true;
    return true;
  }

  function open() {
    step = 1; dupResult = null; checking = false;
    Sheet.open(`
      <div class="sheet-head">
        <div class="grow">
          <div class="row" style="gap:8px">${icon("bulb")}<div class="sheet-title" id="i-title">Механизм</div></div>
          <div class="sheet-sub">идея человека · проверка дублей и симуляция приоритета — сразу</div>
        </div>
      </div>
      <div class="sheet-body">
        <div class="steps"><i></i><i></i><i></i></div>
        <div id="i-body"></div>
      </div>
      <div class="sheet-cta"><button class="btn btn-primary" id="i-cta" style="width:100%">${icon("chev-r")}Далее</button></div>`,
      {
        onOpen(sheet) {
          const body = $("#i-body", sheet), cta = $("#i-cta", sheet);
          const paint = () => { body.innerHTML = stepHTML(); bindStep(sheet); cta.disabled = step === 1 && !canNext(); };
          paint();
          cta.addEventListener("click", () => {
            if (step < 3) {
              if (!canNext()) { Toast.show("опишите механизм подробнее — хотя бы пара предложений", "warn"); return; }
              step++; TG.haptic("medium");
              body.animate?.([{ opacity: .3, transform: "translateX(14px)" }, {}], { duration: 220, easing: "ease-out" });
              paint();
            } else {
              const res = Data.act.submitIdea(form);
              result = res;
              if (res.blocked) {
                Toast.show("почти дубль закрытой идеи — уточните отличие от " + res.dups[0].id, "err", 4200);
                TG.haptic("err");
                return;
              }
              TG.haptic("ok");
              Sheet.closeTop();
              setTimeout(() => {
                Sheet.open(`
                  <div class="sheet-body" style="padding-top:26px;text-align:center">
                    <div style="width:74px;height:74px;margin:0 auto 14px;border-radius:24px;background:var(--grad-accent);display:flex;align-items:center;justify-content:center;color:#fff;box-shadow:0 14px 40px rgba(79,195,255,.4)">${icon("check")}</div>
                    <h3 style="font-size:18px;margin-bottom:6px">Идея ${res.id} принята</h3>
                    <p class="small" style="color:var(--text-2);line-height:1.6">PI ${res.pi.toFixed(2)} · PPI ${res.ppi.toFixed(2)} · корзина ${res.bin}<br>стоит в очереди, iВасёк завёл карточку и отнёс прогнозу отдельную строку в памяти.</p>
                    <div class="card" style="text-align:left;margin-top:14px">
                      <div class="kv"><b>перед GPU</b><span>kill-stage · 8 проверок</span></div>
                      <div class="kv"><b>проверка дублей</b><span>${res.dups.length ? res.dups[0].id + " · " + res.dups[0].sim + "%" : "чисто"}</span></div>
                      <div class="kv"><b>если пройдёт</b><span>L0 · 5 мин</span></div>
                    </div>
                  </div>
                  <div class="sheet-cta"><button class="btn btn-primary" style="width:100%" data-action="idea-done">${icon("flow")}Смотреть в конвейере</button></div>`);
              }, 420);
            }
          });
        }
      });
    TG.haptic("medium");
  }

  function reset() { form = { mech: "", metric: "Δ val loss", test: "", pass: "", market: "", signals: 3 }; dupResult = null; result = null; step = 1; }
  return { open, reset };
})();

window.Verd = Verd; window.Idea = Idea;
