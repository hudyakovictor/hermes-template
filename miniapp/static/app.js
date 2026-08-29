/* ============================================================
   app.js — запуск, маршрутизация действий, живые подписки
   ============================================================ */
"use strict";

/* ---------- Действия (data-action) ---------- */
const Actions = {
  "go-pipe": () => Tabs.go("pipe"),
  "go-tele": () => Tabs.go("tele"),
  "pause-toggle": () => {
    const mode = Data.act.pauseToggle();
    Toast.show(mode === "paused" ? "автозапуск на паузе · текущий прогон доигрывает" : "автозапуск возобновлён", mode === "paused" ? "warn" : "ok");
    Dash.refreshHard();
  },
  "approve": h => {
    if (Data.act.approve(h)) Toast.show(`${h} · L3 одобрен — встанет в расписание после текущего прогона`, "ok");
    Dash.refreshHard();
  },
  "decline": h => {
    if (Data.act.decline(h)) Toast.show(`${h} · L3 отклонён, гипотеза остаётся на чекпойнте`, "warn");
    Dash.refreshHard();
  },
  "killRun": () => {
    const hid = Data.run ? Data.run.hid : null;
    if (Data.act.killRun()) Toast.show(`прогон ${hid} снят · чекпойнт сохранён`, "warn", 3600);
    Dash.refreshHard();
  },
  "boost": h => {
    if (Data.act.boost(h)) Toast.show(`${h} · приоритет поднут (aging +0.12)`, "ok");
    Pipe.refresh();
  },
  "launchL0": h => Actions.launchFlow(h),
  "h-launch": h => { Sheet.closeTop(); setTimeout(() => Actions.launchFlow(h), 300); },
  "h-boost": h => { Sheet.closeTop(); if (Data.act.boost(h)) Toast.show(`${h} поднят в очереди`, "ok"); },
  "h-kill": h => { Sheet.closeTop(); setTimeout(() => Actions.killHypoConfirm(h), 300); },
  "killHypo": h => Actions.killHypoConfirm(h),
  "h-open": h => HypoSheet.open(h),
  "v-open": h => Verd.openVerdict(h),
  "patent-open": id => Actions.patentSheet(id),
  "v-export": h => {
    const v = Data.verdicts.find(x => x.id === h); if (!v) return;
    const text = Data.act.exportVerdict(v);
    copyText(text, `отчёт ${h} скопирован — можно вставить в чат или документ`);
  },
  "vote": (h, side, elBtn) => {
    const d = (elBtn && elBtn.dataset.d) || h;
    if (Data.act.vote(d, side)) {
      Toast.show("голос учтён · арбитраж Boss по базе через минуту", "info");
      Crew.refresh();
    }
  },
  "bet": (h, side) => {
    if (Data.act.bet(h, side)) { Toast.show("ставка принята · до вердикта", "ok"); Crew.refresh(); }
  },
  "idea-done": () => {
    Sheet.closeTop();
    Idea.reset();
    Tabs.go("pipe");
    Pipe.setSeg("queue"); Pipe.refresh();
  },
  "export-digest": () => {
    const lines = ["СВОДКА researchagen · " + new Date().toLocaleString("ru-RU"), ""];
    lines.push(`Режим: ${Data.mode === "auto" ? "авто" : "пауза"} · GPU: ${Data.run ? Data.run.hid + " " + Math.round(Data.run.progress * 100) + "%" : "свободен"}`);
    lines.push(`Бюджет: ${nf(Data.budget.spentH, 2)} / ${nf(Data.budget.limitH, 1)} ч · очередь: ${Data.queue.length} гипотез`);
    lines.push("", "Последние события:");
    Data.events.slice(0, 6).forEach(e => lines.push("· " + e.html.replace(/<[^>]+>/g, "")));
    copyText(lines.join("\n"), "сводка скопирована в буфер");
  },
};

function copyText(text, okMsg) {
  const done = () => Toast.show(okMsg || "скопировано", "ok");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { Toast.show("не удалось скопировать", "err"); }
  ta.remove();
}

/* ---------- Подтверждение закрытия гипотезы ---------- */
Actions.killHypoConfirm = function (h) {
  const hyp = Data.queue.find(x => x.id === h); if (!hyp) return;
  Sheet.open(`
    <div class="sheet-head">
      <div class="ap-ico" style="background:var(--danger-dim);color:var(--danger)">${icon("kill")}</div>
      <div class="grow"><div class="sheet-title">Закрыть ${h} до GPU?</div>
      <div class="sheet-sub">0 GPU-часов будет потрачено — это нормальный результат контура</div></div>
    </div>
    <div class="sheet-body">
      <div class="card" style="margin:0">
        <h4 style="font-size:14px;line-height:1.4;margin-bottom:8px">${esc(hyp.title)}</h4>
        <div class="kv"><b>уровень</b><span>${hyp.level} · ${nf(hyp.hours, 1)} ч оценки</span></div>
        <div class="kv"><b>PPI</b><span>${hyp.ppi.toFixed(2)}</span></div>
        <div class="kv"><b>часы на GPU</b><span style="color:var(--ok)">0.0 — убита до запуска</span></div>
      </div>
      <div class="field" style="margin-top:14px">
        <label>Урок в память (что проверить в следующий раз)</label>
        <input class="inp" id="kill-lesson" value="проверять «${esc(hyp.term || "идею")}» по трём формулировкам gap">
      </div>
      <div style="height:6px"></div>
    </div>
    <div class="sheet-cta">
      <button class="btn btn-ghost grow" id="kill-cancel">Оставить</button>
      <button class="btn btn-danger hold" id="kill-go" style="flex:1.4">
        <span style="position:relative;z-index:2;display:flex;align-items:center;gap:7px;justify-content:center">${icon("kill")}Закрыть — удержать</span>
      </button>
    </div>`,
    { onOpen(sheet) {
      $("#kill-cancel", sheet).addEventListener("click", () => Sheet.closeTop());
      holdable($("#kill-go", sheet), () => {
        Sheet.closeTop();
        if (Data.act.killHypo(h)) Toast.show(`${h} закрыта до GPU · урок записан в память`, "warn", 3600);
        Pipe.refresh();
      });
    } });
};

/* ---------- Запуск L0: если GPU занят — выбор ---------- */
Actions.launchFlow = function (h) {
  const res = Data.act.launchL0(h);
  if (res.ok) { Toast.show(`${h} · L0 запущен вручную`, "ok"); Pipe.refresh(); Dash.refreshHard(); return; }
  if (!res.queued) return;
  const busy = Data.run;
  Sheet.open(`
    <div class="sheet-head">
      <div class="ap-ico" style="background:var(--warn-dim);color:var(--warn)">${icon("alert")}</div>
      <div class="grow"><div class="sheet-title">GPU занят</div>
      <div class="sheet-sub">${busy.hid} · ${busy.level} · осталось ~${busy.eta} мин (${Math.round(busy.progress * 100)}%)</div></div>
    </div>
    <div class="sheet-body">
      <p class="small" style="color:var(--text-2);line-height:1.6;margin-bottom:12px">Один эксперимент — одна карта. Чтобы запустить L0 для ${h} прямо сейчас, придётся вытеснить текущий прогон: чекпойнт сохранится, но прогресс уровня сгорит.</p>
      <div class="card" style="margin:0">
        <div class="kv"><b>текущий прогон</b><span>${busy.hid} · ${Math.round(busy.progress * 100)}%</span></div>
        <div class="kv"><b>потеря при вытеснении</b><span style="color:var(--danger)">~${Math.round(busy.progress * busy.durMin)} мин уровня</span></div>
        <div class="kv"><b>новый прогон</b><span>${h} · L0 · 5 мин</span></div>
      </div>
      <div style="height:8px"></div>
    </div>
    <div class="sheet-cta">
      <button class="btn btn-ghost grow" id="lf-queue">${icon("clock")}В очередь первым</button>
      <button class="btn btn-danger hold" id="lf-preempt" style="flex:1.2">
        <span style="position:relative;z-index:2;display:flex;align-items:center;gap:7px;justify-content:center">${icon("kill")}Вытеснить — удержать</span>
      </button>
    </div>`,
    { onOpen(sheet) {
      $("#lf-queue", sheet).addEventListener("click", () => {
        Sheet.closeTop();
        if (Data.act.boost(h)) Toast.show(`${h} встанет первым после текущего прогона`, "ok");
        Pipe.refresh();
      });
      holdable($("#lf-preempt", sheet), () => {
        Sheet.closeTop();
        Data.act.killRun();
        setTimeout(() => {
          Data.act.launchL0(h);
          Toast.show(`${h} · L0 запущен, ${busy.hid} на чекпойнте`, "warn");
          Pipe.refresh(); Dash.refreshHard();
        }, 400);
      });
    } });
};

/* ---------- Патентная шторка ---------- */
Actions.patentSheet = function (id) {
  const p = Data.patents.find(x => x.id === id); if (!p) return;
  Sheet.open(`
    <div class="sheet-head">
      <span class="chip chip-violet">${icon("star")}${p.id} · ${p.status === "draft" ? "черновик" : "кандидат"}</span>
      <div class="grow"><div class="sheet-title">${esc(p.title)}</div><div class="sheet-sub">из гипотезы ${p.hid}</div></div>
    </div>
    <div class="sheet-body">
      <div class="sec-t" style="margin-top:0"><h3>Формула (пункт 1)</h3></div>
      <div class="card" style="margin:0"><p class="small" style="line-height:1.7;color:var(--text-2)">${esc(p.claim)}</p></div>
      <div class="sec-t"><h3>Цепочка доказательств</h3></div>
      <div class="card" style="margin:0">
        <div class="kv"><b>гипотеза-источник</b><span>${p.hid}</span></div>
        <div class="kv"><b>уровень доказанности</b><span>${p.hid === "H-003" ? "L2 · 2 архитектуры" : "L1 · 3 seeds (ждёт L2/L3)"}</span></div>
        <div class="kv"><b>воспроизводимость</b><span>${p.hid === "H-003" ? "≥ 5 seeds, 2 настройки" : "3 seeds"}</span></div>
      </div>
      <div style="height:8px"></div>
    </div>
    <div class="sheet-cta"><button class="btn btn-primary grow" data-action="patent-copy" data-h="${p.id}">${icon("copy")}Скопировать материалы заявки</button></div>`);
};
Actions["patent-copy"] = id => {
  const p = Data.patents.find(x => x.id === id); if (!p) return;
  copyText(`ПАТЕНТНАЯ ЗАГОТОВКА ${p.id} (из ${p.hid})\n${p.title}\n\nФормула, п.1: ${p.claim}\n\nСтатус: ${p.status}`, `материалы ${p.id} скопированы`);
};

/* ---------- Глобальный обработчик кликов ---------- */
window.Actions = Actions; window.Data = Data;
document.addEventListener("click", e => {
  const a = e.target.closest("[data-action]"); if (!a) return;
  const fn = Actions[a.dataset.action];
  if (fn) fn(a.dataset.h, a.dataset.side, a);
});

/* ---------- Тема ---------- */
function applyTheme() {
  const t = TG.theme();
  const root = document.documentElement;
  if (!t) { root.dataset.scheme = "dark"; return; }
  root.dataset.scheme = t.scheme === "light" ? "light" : "dark";
  try {
    const p = t.params;
    if (p.bg_color) root.style.setProperty("--bg", p.bg_color);
    if (p.secondary_bg_color) root.style.setProperty("--surface", p.secondary_bg_color);
    if (p.text_color) root.style.setProperty("--text", p.text_color);
    if (p.hint_color) root.style.setProperty("--muted", p.hint_color);
    if (p.link_color) root.style.setProperty("--accent", p.link_color);
    if (p.button_color) root.style.setProperty("--accent-deep", p.button_color);
    TG.wa.setHeaderColor && TG.wa.setHeaderColor("bg_color");
    TG.wa.setBackgroundColor && TG.wa.setBackgroundColor("bg_color");
  } catch (err) {}
}

/* ---------- Подзаголовок и действия топбара ---------- */
function topbarFor(tab) {
  const sub = $("#tb-sub"), acts = $("#tb-actions");
  const r = Data.run;
  const base = `тик ${Data.fmtAgo(Data.lastTick)} · ${r ? r.hid + " " + Math.round(r.progress * 100) + "%" : "GPU свободен"}`;
  sub.textContent = TG.ok && TG.user() ? `${TG.user().first_name || "оператор"} · ` + base : base;
  const btn = (id, ic, label, on) => `<button class="icon-btn ${on ? "on" : ""}" id="${id}" aria-label="${label}">${icon(ic)}</button>`;
  if (tab === "dash") acts.innerHTML = btn("tb-mode", Data.mode === "auto" ? "pause" : "play", "автозапуск", Data.mode !== "auto");
  else if (tab === "tele") acts.innerHTML = btn("tb-cmp", "compare", "сравнить прогоны");
  else if (tab === "verd") acts.innerHTML = btn("tb-dig", "share", "сводка");
  else acts.innerHTML = "";
  const m = $("#tb-mode"); if (m) m.addEventListener("click", () => Actions["pause-toggle"]());
  const c = $("#tb-cmp"); if (c) c.addEventListener("click", () => CompareSheet.open());
  const d = $("#tb-dig"); if (d) d.addEventListener("click", () => Actions["export-digest"]());
}

/* ---------- Ленивый рендер экранов ---------- */
const rendered = {};
Screens.onTab = function (tab) {
  if (tab === "pipe" && !rendered.pipe) { Pipe.render(); rendered.pipe = true; }
  if (tab === "tele" && !rendered.tele) { Tele.render(); rendered.tele = true; }
  if (tab === "crew" && !rendered.crew) { Crew.render(); rendered.crew = true; }
  if (tab === "verd" && !rendered.verd) { Verd.render(); rendered.verd = true; }
  topbarFor(tab);
  if (tab === "crew" && Crew.seg === "chat") {
    const sc = $("#sc-crew"); sc.scrollTop = sc.scrollHeight;
  }
};

/* ---------- Запуск ---------- */
function boot() {
  TG.ready();
  applyTheme();
  try {
    if (TG.ok) {
      TG.wa.onEvent("themeChanged", applyTheme);
      TG.wa.enableClosingConfirmation && TG.wa.enableClosingConfirmation();
      TG.wa.disableVerticalSwipes && TG.wa.disableVerticalSwipes();
    }
  } catch (e) {}

  Data.start();
  Tabs.init();
  Dash.render();
  Pipe.render(); rendered.pipe = true;
  $("#tb-title").textContent = "Пульт";
  topbarFor("dash");
  Tabs.go("dash");
  $("#s-dash").classList.add("on");
  $$(".tab").forEach(t => t.classList.toggle("on", t.dataset.tab === "dash"));

  // шторка идеи по FAB
  $("#fab").addEventListener("click", () => Idea.open());

  // обновление данных
  Data.on("tick", () => {
    Dash.tick();
    Tele.tick();
    if (Tabs.cur === "dash") topbarFor("dash");
  });
  const hard = () => { Dash.refreshHard(); Pipe.refresh(); };
  Data.on("mode", hard);
  Data.on("run", () => { hard(); if (rendered.tele) Tele.render(); });
  Data.on("event", () => { if (Tabs.cur === "dash" || $("#d-events")) Dash.refreshHard(); });
  Data.on("approvals", () => Dash.refreshHard());
  Data.on("queue", () => Pipe.refresh());
  Data.on("paused", () => Pipe.refresh());
  Data.on("chat", m => Crew.onChat(m));
  Data.on("findings", () => { if (Crew.seg === "review") Crew.refresh(); });
  Data.on("market", () => { if (Crew.seg === "market" || Crew.seg === "chat") Crew.refresh(); });
  Data.on("verdict", v => {
    Verd.refresh();
    Toast.show(`вердикт: ${v.id} — подтверждён · −27% в коридоре`, "ok", 4200);
    TG.haptic("ok");
  });
  Data.on("bet-won", hid => Toast.show(`ваша ставка на ${hid} сыграла · +1 к вашей точности`, "ok", 4200));

  // pull-to-refresh на каждом экране
  ["dash", "pipe", "tele", "crew", "verd"].forEach(k => {
    attachPTR($("#sc-" + k), () => new Promise(res => {
      Data.gpu.util = clamp(Data.gpu.util + rnd(-6, 6), Data.run ? 80 : 2, 100);
      Data.lastTick = now() - Math.round(rnd(4, 40)) * 1000;
      setTimeout(() => {
        Toast.show("данные контура обновлены · " + Data.fmtClock(now()), "info", 1800);
        res();
      }, 600);
    }));
  });

  // ресайз графиков
  window.addEventListener("resize", () => { if (rendered.tele) Tele.render(); Dash.tick(); });
  try { TG.ok && TG.wa.onEvent("viewportChanged", () => { if (rendered.tele) Tele.render(); }); } catch (e) {}

  // убрать заставку
  setTimeout(() => {
    $("#boot").classList.add("off");
    $("#topbar").hidden = false;
    $("#tabbar").hidden = false;
    setTimeout(() => $("#boot").remove(), 500);
    if (!TG.ok) {
      document.body.insertAdjacentHTML("beforeend", `<div class="badge-demo">демо · данные симулируются</div>`);
    }
    Toast.show(TG.ok ? "пульт подключён к контуру researchagen" : "демо-режим: контур симулируется локально", "info", 2800);
  }, 650);
}

document.addEventListener("DOMContentLoaded", boot);
