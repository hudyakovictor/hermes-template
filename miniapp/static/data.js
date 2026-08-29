/* ============================================================
   data.js — слой данных: живая симуляция контура researchagen.
   В продакшене эти же структуры заполняет /api/state (SQLite),
   здесь — детерминированный генератор демо-сцены.
   ============================================================ */
"use strict";

/* ---------- ГСЧ ---------- */
function mulberry32(a) {
  return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
const R = mulberry32(20260829);
const rnd = (a, b) => a + (b - a) * R();
const pick = arr => arr[Math.floor(R() * arr.length)];
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const now = () => Date.now();
const MIN = 60000, HOUR = 3600000;

/* ---------- Экипаж ---------- */
const AGENTS = {
  shef:    { id: "shef",    name: "Boss",     zone: "начальник · бюджет, арбитраж", color: "#ffc24b" },
  skif:    { id: "skif",    name: "Скиф",     zone: "добыча · источники",           color: "#4fc3ff" },
  krot:    { id: "krot",    name: "Аналитег", zone: "добыча · синтез сигналов",     color: "#3be0a0" },
  morg:    { id: "morg",    name: "Морг",     zone: "kill-stage · проверки",        color: "#ff6161" },
  gayka:   { id: "gayka",   name: "Гайка",    zone: "эксперименты L0–L3",           color: "#a07cff" },
  hronik:  { id: "hronik",  name: "Хроник",   zone: "память · патенты",             color: "#ff9bd2" },
  stazhor: { id: "stazhor", name: "iВасёк",   zone: "inbox · карточки",             color: "#93a1c4" },
};

/* ---------- PI / PPI (формула из tools/queue.py) ---------- */
function piOf(h) {
  return +(0.22 * h.s + 0.16 * h.n + 0.12 * h.e + 0.14 * h.q + 0.14 * h.m + 0.22 * h.d + h.aging).toFixed(3);
}
function sigScore(k) { return k >= 6 ? 1 : k === 5 ? 0.84 : k === 4 ? 0.67 : k === 3 ? 0.5 : 0; }
function binOf(hours) { return hours <= 4 ? "P1" : hours <= 12 ? "P2" : hours <= 48 ? "P3" : "P4"; }

const KILL_CHECKS = [
  "Простое объяснение (lr/scheduler/init/batch) не объясняет эффект",
  "Публикационный gap: прямого аналога нет",
  "Утечка данных и перекрытие train-test исключены",
  "Эффект не сводится к шуму seeds",
  "Есть контрольное условие, где эффект исчезнет",
  "Метрика читается дешёво, без полного обучения",
  "PASS/FAIL зафиксированы числами ДО запуска",
  "Назван покупатель или сценарий экономии",
];

/* ---------- Гипотезы ---------- */
function mkH(o) {
  const h = Object.assign({
    status: "queued", level: "L0", signals: 3, ageDays: 1, aging: 0,
    s: sigScore(o.signals || 3), n: .7, e: .6, q: .5, m: .6, d: .7,
    checks: KILL_CHECKS.map(() => "ok"),
    term: "", mechanism: "",
    corridor: { metric: "Δ val loss @ 5% бюджета", lo: -38, hi: -22, point: -30, unit: "%", min: -55, max: 5 },
  }, o);
  h.pi = piOf(h);
  h.ppi = +(h.pi / h.hours).toFixed(2);
  h.bin = binOf(h.hours);
  return h;
}

const QUEUE0 = [
  mkH({ id: "H-013", title: "Effective rank как зонд: ранг против паразитной памяти", signals: 5, hours: 1.2, level: "L1", n: .85, e: .9, q: .55, m: .6, d: .8, ageDays: 1, term: "effective rank",
    mechanism: "Ранг весовой матрицы растёт ступеней задолго до выхода метрики на плато. Если ступень совпадает по всем сидам — полезный контур уже собран, дальнейшее обучение только достраивает память.",
    corridor: { metric: "Δ val loss @ 5% бюджета", lo: -34, hi: -20, point: -27, unit: "%", min: -50, max: 5 },
    checks: ["ok", "ok", "ok", "wait", "ok", "ok", "ok", "wait"] }),
  mkH({ id: "H-012", title: "Condensation перед grokking: точка конденсации как триггер стопа", signals: 4, hours: 2.5, level: "L1", n: .75, e: .8, q: .5, m: .55, d: .7, ageDays: 2, term: "condensation",
    mechanism: "Фазовая конденсация активаций происходит за шаги до grokking-перехода. Ловим её дешёвым зондом нормы — и останавливаем обучение раньше, чем память начнёт доминировать.",
    corridor: { metric: "ускорение до целевого loss", lo: 1.6, hi: 2.4, point: 2.0, unit: "×", min: 1, max: 3 } }),
  mkH({ id: "H-016", title: "Spectral bias как фильтр: отсечь частотную memorization", signals: 3, hours: .8, level: "L0", n: .6, e: .85, q: .45, m: .5, d: .65, ageDays: 1, term: "spectral bias",
    mechanism: "Сеть сначала учит низкие частоты. Если на раннем спектре целевой сигнал уже разделён — высокочастотный хвост можно не учить вовсе.",
    corridor: { metric: "Δ compute до равного val loss", lo: -30, hi: -15, point: -22, unit: "%", min: -50, max: 5 },
    checks: ["ok", "wait", "ok", "ok", "wait", "ok", "ok", "no"] }),
  mkH({ id: "H-017", title: "Критический период: окно чувствительности к label noise", signals: 4, hours: 3.2, level: "L0", n: .7, e: .55, q: .5, m: .6, d: .6, ageDays: 4, aging: .2, term: "critical period",
    mechanism: "Раннее зашумление меток в узком окне необратимо бьёт по обобщению. Если окно совпадает с появлением ранней структуры — это та же конкуренция контуров, и её можно мерить дёшево.",
    corridor: { metric: "Δ val loss при шуме 2% @ 3% бюджета", lo: -45, hi: -25, point: -35, unit: "%", min: -60, max: 5 } }),
  mkH({ id: "H-014", title: "Gradient disparity между слоями: сигнал конкуренции контуров", signals: 4, hours: 6, level: "L1", n: .8, e: .7, q: .5, m: .7, d: .55, ageDays: 3, aging: .15, term: "gradient disparity",
    mechanism: "Расхождение градиентов соседних слоёв падает в момент, когда один из контуров побеждает. Порог расхождения — кандидат в ранние билеты.",
    corridor: { metric: "Δ val loss @ 8% бюджета", lo: -25, hi: -12, point: -18, unit: "%", min: -45, max: 5 },
    checks: ["ok", "ok", "wait", "ok", "ok", "wait", "ok", "wait"] }),
  mkH({ id: "H-015", title: "Early-bird лотерея: IMP одним циклом после 5% бюджета", signals: 5, hours: 9, level: "L2", n: .9, e: .95, q: .7, m: .8, d: .6, ageDays: 5, aging: .25, term: "lottery ticket",
    mechanism: "Если выигрышный тикет формируется в первые 5% обучения, один цикл magnitude pruning без обратного перемотка весов даёт подсеть не хуже полной. Проверяем на трёх архитектурах.",
    corridor: { metric: "Δ compute при равном качестве", lo: -42, hi: -28, point: -35, unit: "%", min: -55, max: 5 },
    checks: ["ok", "wait", "ok", "ok", "wait", "ok", "ok", "ok"] }),
  mkH({ id: "H-018", title: "Neural collapse до сходимости: дешёвый зонд раннего качества", signals: 3, hours: 2, level: "L0", n: .55, e: .4, q: .45, m: .45, d: .75, ageDays: 2, aging: .1, term: "neural collapse",
    mechanism: "Геометрия классов сворачивается раньше, чем loss выходит на плато. Зонд FFС-расстояния между центрами классов — копеечная метрика раннего качества.",
    corridor: { metric: "корреляция зонда с финальным качеством", lo: .6, hi: .85, point: .72, unit: "r", min: 0, max: 1 },
    checks: ["wait", "ok", "ok", "wait", "ok", "ok", "wait", "no"] }),
];

const PAUSED0 = [mkH({ id: "H-009", title: "Linear mode connectivity недообученных подсетей", signals: 4, hours: 14, level: "L2", n: .65, e: .5, q: .6, m: .55, d: .6, ageDays: 9, aging: .3, term: "mode connectivity", status: "paused_checkpoint", checkpointH: 4.2,
  mechanism: "Две подсети, найденные ранним pruning, остаются линейно связанными без барьера loss — значит, это один и тот же контур, и его можно извлекать до сходимости.",
  corridor: { metric: "барьер loss на пути между подсетями", lo: 0, hi: .05, point: .02, unit: "Δ", min: 0, max: .3 } })];

/* ---------- Вердикты ---------- */
const VERDICTS0 = [
  { id: "H-003", title: "Iterative magnitude pruning после 5% бюджета", level: "L2", status: "confirmed", ago: 26 * HOUR,
    forecast: { metric: "Δ compute при равном качестве", lo: -38, hi: -22, point: -30, unit: "%", min: -55, max: 5 }, actual: -34,
    lesson: "Ступень стабильности знака на 4% обучения предсказывает выигрыш по compute. Работает на 2 архитектурах из 2.", patent: "P-1", neighbors: true, term: "IMP" },
  { id: "H-005", title: "Grokking-переход: предиктор по росту нормы весов", level: "L1", status: "rejected", ago: 2 * 24 * HOUR,
    forecast: { metric: "ускорение до целевого loss", lo: 1.8, hi: 2.6, point: 2.2, unit: "×", min: 1, max: 3 }, actual: 1.1,
    lesson: "Рост нормы объясняется weight decay=0 в контрольном прогоне: эффект исчез на контрольном условии.", patent: null, neighbors: true, term: "grokking" },
  { id: "H-007", title: "Loss curvature sharpness: дешёвый предиктор обобщения", level: "L2", status: "partial", ago: 4 * 24 * HOUR,
    forecast: { metric: "Δ val loss @ 10% бюджета", lo: -28, hi: -16, point: -22, unit: "%", min: -50, max: 5 }, actual: -12,
    lesson: "Направление верное, величина вдвое слабее прогноза. Перенесено в H-014 как вспомогательный сигнал.", patent: null, neighbors: true, term: "sharpness" },
  { id: "H-006", title: "Lottery tickets без перемотки весов", level: "L0", status: "killed", ago: 6 * 24 * HOUR,
    forecast: { metric: "Δ val loss", lo: -30, hi: -18, point: -24, unit: "%", min: -55, max: 5 }, actual: null,
    lesson: "Убита до GPU: прямой аналог опубликован в 2024 (arXiv:2405.11218), gap не подтверждён.", patent: null, neighbors: true, term: "lottery" },
  { id: "H-008", title: "Condensation: локальный взрыв нормы как триггер", level: "L1", status: "killed", ago: 8 * 24 * HOUR,
    forecast: { metric: "Δ val loss", lo: -35, hi: -20, point: -28, unit: "%", min: -55, max: 5 }, actual: null,
    lesson: "Взрыв нормы объяснён расписанием lr — простое объяснение, kill-check №1. Соседние гипотезы проверены на тот же дефект.", patent: null, neighbors: true, term: "condensation" },
  { id: "H-002", title: "Sign stability в малых сетях (пилот)", level: "L1", status: "confirmed", ago: 12 * 24 * HOUR,
    forecast: { metric: "Δ compute", lo: -30, hi: -15, point: -22, unit: "%", min: -50, max: 5 }, actual: -24,
    lesson: "Подтверждён на 3 seeds. Масштабирован в H-011 (текущий прогон L1).", patent: null, neighbors: true, term: "sign stability" },
  { id: "H-004", title: "Information bottleneck probe: фазовый переход", level: "L1", status: "rejected", ago: 15 * 24 * HOUR,
    forecast: { metric: "корреляция зонда с качеством", lo: .55, hi: .8, point: .68, unit: "r", min: 0, max: 1 }, actual: .21,
    lesson: "Зонд не воспроизводится между seeds: разброс больше эффекта.", patent: null, neighbors: true, term: "information bottleneck" },
];

const PATENTS0 = [
  { id: "P-1", hid: "H-003", title: "Ранний выход подсети по стабильности знака весов",
    claim: "Способ сокращения вычислительных затрат при обучении нейронной сети, отличающийся тем, что момент остановки обучения и изврения подсети определяют по достижении долей стабильных знаков весов заданного порога в первых 2–6% бюджета обучения.", status: "draft" },
  { id: "P-2", hid: "H-011", title: "Каскадный диспетчер проверки гипотез с PPI-приоритетом", claim: "Способ планирования экспериментов на единственном вычислительном узле, при котором приоритет задания равен отношению ценности гипотезы к оценённым GPU-часам, а допуск к следующему уровню требует улучшения целевой метрики в зафиксированном коридоре.", status: "candidate" },
];

/* ---------- Кривые обучения ---------- */
function genLoss(seed, n = 96) {
  const pts = []; let t = 0;
  const floor = seed === 2 ? .62 : .48;
  const start = 2.3 + seed * .08;
  const cliff = seed === 2 ? 78 : 62 + seed * 4;      // grokking-обрыв
  const noise = .05 + seed * .012;
  for (let i = 0; i < n; i++) {
    t = i / (n - 1);
    let v = floor + (start - floor) * Math.exp(-t * 4.6);
    if (i >= cliff) v -= (0.34 + seed * .02) * (1 - Math.exp(-(i - cliff) / 9));  // ступень
    v += (R() - .5) * noise * (1.2 - t);
    pts.push({ x: +(t * 100).toFixed(1), y: +Math.max(.3, v).toFixed(4) });
  }
  return pts;
}
function genRank(seed, n = 96) {
  const pts = [];
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1);
    let v = 100 * (1 - Math.exp(-t * 3.2)) * (.82 + seed * .07);
    if (i > 40 && i < 58) v -= 6 * Math.sin((i - 40) / 3);  // ступень-колебание
    v += (R() - .5) * 2.2;
    pts.push({ x: +(t * 100).toFixed(1), y: +clamp(v, 2, 100).toFixed(1) });
  }
  return pts;
}
function genStab(seeds, n = 96) {
  const pts = [];
  for (let i = 0; i < n; i++) {
    const ys = seeds.map(s => s[i].y);
    const mean = ys.reduce((a, b) => a + b) / ys.length;
    const sd = Math.sqrt(ys.reduce((a, b) => a + (b - mean) ** 2, 0) / ys.length);
    pts.push({ x: seeds[0][i].x, y: +(sd / mean * 100).toFixed(2) });
  }
  return pts;
}
function genHist(n, base, spread, min, max) {
  const a = []; let v = base;
  for (let i = 0; i < n; i++) { v = clamp(v + (R() - .5) * spread, min, max); a.push(+v.toFixed(1)); }
  return a;
}

/* ---------- Состояние ---------- */
const Data = {
  booted: false, demo: true,
  mode: "auto",
  lastTick: now() - 84 * 1000,
  gpu: { name: "RTX 5090", vramTotal: 31.6, vram: 21.8, util: 94, temp: 66, fan: 58, utilHist: [], vramHist: [] },
  budget: { limitH: 6, spentH: 2.31, calibDay: "вс", lastCalib: now() - 2 * 24 * HOUR },
  queue: QUEUE0,
  paused: PAUSED0,
  verdicts: VERDICTS0,
  patents: PATENTS0,
  chat: [], disputes: [], market: null, findings: [], approvals: [], events: [],
  inbox: [],
  runs: [],           // история прогонов
  run: null,          // текущий прогон
  tele: null,         // живая телеметрия
  myBets: {},         // hid -> 'for' | 'against'
  msgSeq: 100,
};

/* ---------- Шина событий ---------- */
const _subs = {};
Data.on = (key, fn) => { (_subs[key] = _subs[key] || []).push(fn); };
Data.emit = (key, payload) => { (_subs[key] || []).forEach(fn => { try { fn(payload); } catch (e) { console.warn(e); } }); };

/* ---------- Чат ---------- */
function msg(agent, text, kind = "work", extra = {}) {
  const m = Object.assign({ ts: now(), agent, kind, text, id: ++Data.msgSeq }, extra);
  Data.chat.push(m);
  Data.emit("chat", m);
  return m;
}
function sysmsg(text) { return msg("sys", text, "sys"); }

/* ---------- Инициализация демо-сцены ---------- */
Data.init = function () {
  // GPU история
  Data.gpu.utilHist = genHist(60, 92, 9, 62, 100);
  Data.gpu.vramHist = genHist(60, 21.8, .7, 19.5, 23.5);

  // текущий прогон
  Data._makeRun("H-011", "Sign stability как ранний билет: порог устойчивости знака на 3% обучения", "L1", .64, 62);
  Data.run.startedAt = now() - 41 * MIN;
  Data.run.rid = "R-118";

  // история прогонов для сравнения
  Data.runs = [
    { id: "R-118", hid: "H-011", level: "L1", note: "seed 0–2 · live", curve: Data.tele.seeds, live: true, color: "#4fc3ff" },
    { id: "R-103", hid: "H-003", level: "L2", note: "подтверждён · −34%", curve: [genLoss(0)], live: false, color: "#3be0a0" },
    { id: "R-095", hid: "H-005", level: "L1", note: "отвергнут · ×1.1", curve: [genLoss(1)], live: false, color: "#ff6161" },
    { id: "R-087", hid: "H-007", level: "L2", note: "частично · −12%", curve: [genLoss(2)], live: false, color: "#ffb437" },
  ];

  // споры
  Data.disputes = [
    { id: "D-1", hid: "H-015", topic: "IMP после 5% — наука или переоткрытие 2020-го?", z: 3, p: 2, myVote: null, status: "open", zArgs: "другие сиды и другой масштаб — дать дожить до L2", pArgs: "это early-bird из 2020 с новым словом" },
  ];

  // рынок прогнозов
  Data.market = {
    open: [
      { hid: "H-011", title: "Sign stability: L1 подтвердит коридор −22…−38%?", level: "L1", for: ["shef", "gayka", "hronik"], against: ["morg", "stazhor"] },
      { hid: "H-013", title: "Effective rank: ступень ранга совпадёт по 3 сидам?", level: "L1", for: ["krot", "skif", "shef"], against: ["morg"] },
      { hid: "H-015", title: "Early-bird IMP: доживёт до L3?", level: "L2", for: ["hronik"], against: ["morg", "krot", "gayka", "stazhor"] },
    ],
    resolved: [
      { hid: "H-005", outcome: "rejected", right: ["morg", "stazhor"], wrong: ["gayka", "hronik"] },
      { hid: "H-003", outcome: "confirmed", right: ["shef", "morg", "krot"], wrong: ["stazhor"] },
    ],
    ratings: [
      { agent: "shef", hit: 8, total: 11, streak: 2 }, { agent: "morg", hit: 7, total: 11, streak: 3 },
      { agent: "hronik", hit: 6, total: 10, streak: -1 }, { agent: "gayka", hit: 6, total: 11, streak: 0 },
      { agent: "krot", hit: 5, total: 10, streak: 1 }, { agent: "skif", hit: 4, total: 9, streak: -2 },
      { agent: "stazhor", hit: 3, total: 8, streak: -1 },
    ],
  };

  // замечания ревью
  Data.findings = [
    { id: "F-1", kind: "kill-check без доказательства", subject: "H-014 · галочка №3 без приложенного прогона", severity: "high", status: "open", by: "morg", fixer: "stazhor", ago: 52 * MIN },
    { id: "F-2", kind: "сдвиг калибровки", subject: "вес novelty перевешен после 3 вердиктов", severity: "mid", status: "fixed", by: "hronik", fixer: "hronik", ago: 5 * HOUR },
    { id: "F-3", kind: "дубль сигнала", subject: "H-016 · сигналы A и B из одного графика", severity: "low", status: "open", by: "krot", fixer: "stazhor", ago: 3 * HOUR },
  ];

  // ожидает подтверждения человеком
  Data.approvals = [{ hid: "H-009", title: "Linear mode connectivity · L3", hours: 9.5, level: "L3", note: "масштабирование и воспроизводимость: ≥5 seeds, ≥2 настройки", ts: now() - 18 * MIN }];

  // лента событий
  Data.events = [
    { ts: now() - 4 * MIN, ico: "bolt", tone: "accent", html: "<b>H-011</b> · L1 дошёл до 64%, grokking-ступень на seed 0–1" },
    { ts: now() - 18 * MIN, ico: "alert", tone: "warn", html: "<b>H-009</b> · запрос подтверждения L3 (9.5 ч)" },
    { ts: now() - 41 * MIN, ico: "play", tone: "accent", html: "запуск <b>R-118</b> · H-011, L1, 3 seeds" },
    { ts: now() - 2 * HOUR, ico: "check", tone: "ok", html: "вердикт <b>H-003</b> — подтверждён, −34% в коридоре" },
    { ts: now() - 3 * HOUR, ico: "x", tone: "danger", html: "<b>H-006</b> убита до GPU: публикационный gap" },
  ];

  // сид истории чата
  const T = m => now() - m * MIN;
  Data.chat = [
    { id: 1, ts: T(58), agent: "krot", kind: "work", text: "синтез по H-013 готов: три сигнала сводятся в одну картину, ранг дёшево мерить каждые 50 шагов." },
    { id: 2, ts: T(56), agent: "morg", kind: "work", text: "прежде чем жечь часы: чем ступень ранга не объясняется lr-расписанием? галочку №1 не ставлю." },
    { id: 3, ts: T(55), agent: "krot", kind: "work", text: "контрольный прогон с constant lr в плане есть, бро. смотри pass/fail в карточке." },
    { id: 4, ts: T(50), agent: "sys", kind: "sys", text: "F-1 · Морг нашёл замечание: H-014, kill-check №3 без доказательства. Чинит iВасёк" },
    { id: 5, ts: T(49), agent: "stazhor", kind: "work", text: "принял, докину прогон к вечеру. галочку снимаю до доказательства." },
    { id: 6, ts: T(47), agent: "skif", kind: "work", text: "прошёл 140 источников по mode connectivity. у H-009 gap честный, аналог 2023 года про другие условия." },
    { id: 7, ts: T(44), agent: "shef", kind: "work", text: "бюджет до конца суток: 3.7 ч. H-009 на L3 без человека не идёт — запросил подтверждение." },
    { id: 8, ts: T(41), agent: "sys", kind: "sys", text: "запуск R-118 · H-011 → L1 · 3 seeds · ~62 мин" },
    { id: 9, ts: T(40), agent: "gayka", kind: "work", text: "H-011 поехал. чекпойнт каждые 5 минут, при вытеснении потеряем максимум 5." },
    { id: 10, ts: T(36), agent: "morg", kind: "work", text: "ставлю против: sign stability в малых сетях — это H-002, а масштаб всегда врёт." },
    { id: 11, ts: T(35), agent: "hronik", kind: "work", text: "если L1 подтвердится, claim из P-2 усиливается третьим примером. держу патентную папку открытой." },
    { id: 12, ts: T(30), agent: "gayka", kind: "work", text: "60% прогона. лосс красиво падает, ступень на seed 0 и 1 почти синхронно." },
    { id: 13, ts: T(28), agent: "krot", kind: "work", text: "синхронность двух сидов из трёх — уже не шум. третий молчит, смотрю." },
    { id: 14, ts: T(22), agent: "skif", kind: "work", text: "кстати, заказчик опять спросил, не пора ли нам строить AGI. ответил, что мы строим таблицу." },
    { id: 15, ts: T(20), agent: "morg", kind: "work", text: "лол" },
    { id: 16, ts: T(12), agent: "shef", kind: "work", text: "очередь: H-013 идёт следующей, PPI 0.65. всё, работаем." },
  ];

  Data.booted = true;
};

/* ---------- Прогон ---------- */
Data._makeRun = function (hid, title, level, startProg, durMin) {
  const seeds = [genLoss(0), genLoss(1), genLoss(2)];
  const ranks = [genRank(0), genRank(1), genRank(2)];
  const stab = genStab(seeds);
  Data.tele = { seeds, ranks, stab, revealed: Math.floor(startProg * seeds[0].length) };
  Data.run = {
    hid, title, level, seedsN: 3, durMin, progress: startProg,
    startedAt: now(), eta: Math.round(durMin * (1 - startProg)),
    vr: 21.8, term: "sign stability",
  };
  return Data.run;
};

Data._startRun = function (h, delayNote) {
  Data._makeRun(h.id, h.title, h.level, 0, h.level === "L0" ? 5 : 58);
  Data.run.startedAt = now();
  Data.events.unshift({ ts: now(), ico: "play", tone: "accent", html: `запуск <b>${Data.run.hid}</b> → ${h.level}${delayNote ? " · " + delayNote : ""}` });
  Data.emit("event"); Data.emit("run");
  sysmsg(`запуск · ${h.id} → ${h.level} · ~${Data.run.durMin} мин`);
  msg("gayka", `${h.id} поехал. чекпойнт каждые 5 минут.`);
};

/* ---------- Тик (1 с) ---------- */
Data._tick = function () {
  const g = Data.gpu, r = Data.run;
  if (r) {
    // GPU под нагрузкой
    g.util = clamp(g.util + (R() - .5) * 7, 86, 100);
    g.vram = clamp(g.vram + (R() - .5) * .3, 20.8, 22.9);
    g.temp = clamp(g.temp + (R() - .5) * 1.1, 62, 74);
    g.fan = clamp(45 + (g.temp - 58) * 2.6, 40, 88);
    Data.budget.spentH = Math.min(Data.budget.limitH, Data.budget.spentH + 1 / 3600);
    // демо-ускорение: «62 минуты» прогона сжимаются ~в 2.3 минуты реального времени
    r.progress = Math.min(1, r.progress + 1 / (r.durMin * 2.2));
    r.eta = Math.max(0, Math.round(r.durMin * (1 - r.progress)));
    const want = Math.floor(r.progress * Data.tele.seeds[0].length);
    if (want > Data.tele.revealed) { Data.tele.revealed = want; Data.emit("tele"); }
    if (r.progress >= 1 && !r._finishing) { r._finishing = true; Data._finishRun(); }
  } else {
    g.util = clamp(g.util + (R() - .5) * 4, 1, 9);
    g.vram = clamp(g.vram + (R() - .5) * .15, 1.1, 2.4);
    g.temp = clamp(g.temp + (R() - .5) * .8, 28, 38);
    g.fan = clamp(30 + (g.temp - 28), 30, 45);
  }
  g.utilHist.push(+g.util.toFixed(1)); if (g.utilHist.length > 60) g.utilHist.shift();
  g.vramHist.push(+g.vram.toFixed(1)); if (g.vramHist.length > 60) g.vramHist.shift();
  Data.lastTick += 0; // (диспетчер обновляет отдельно)
  Data.emit("tick");
};

/* ---------- Вердикт по текущему прогону ---------- */
Data._finishRun = function () {
  const r = Data.run;
  const verdict = { id: r.hid, title: r.title, level: r.level, status: "confirmed", ago: 0,
    forecast: { metric: "Δ val loss @ 5% бюджета", lo: -38, hi: -22, point: -30, unit: "%", min: -55, max: 5 },
    actual: -27, lesson: "Ступень стабильности знака совпала по 3 сидам из 3 на 4.1% бюджета. Коридор −22…−38% накрыт фактом −27%.", patent: "P-2", neighbors: true, term: "sign stability" };
  Data.runs.unshift({ id: "R-119", hid: r.hid, level: r.level, note: "подтверждён · −27%", curve: Data.tele.seeds, live: false, color: "#3be0a0" });
  Data.verdicts.unshift(verdict);
  // разрешить ставки
  const mk = Data.market.open.find(b => b.hid === r.hid);
  if (mk) {
    Data.market.open = Data.market.open.filter(b => b.hid !== r.hid);
    Data.market.resolved.unshift({ hid: r.hid, outcome: "confirmed", right: mk.for, wrong: mk.against });
    mk.for.forEach(a => { const rr = Data.market.ratings.find(x => x.agent === a); if (rr) { rr.hit++; rr.total++; rr.streak = Math.max(1, rr.streak + 1); } });
    mk.against.forEach(a => { const rr = Data.market.ratings.find(x => x.agent === a); if (rr) { rr.total++; rr.streak = Math.min(-1, rr.streak - 1); } });
    if (Data.myBets[r.hid] === "for") { Data.emit("bet-won", r.hid); }
  }
  Data.run = null; Data.tele = null;
  Data.queue = Data.queue.filter(h => h.id !== r.hid);
  Data.events.unshift({ ts: now(), ico: "check", tone: "ok", html: `вердикт <b>${r.hid}</b> — подтверждён: −27% в коридоре −22…−38%` });
  sysmsg(`вердикт · ${r.hid} — подтверждён · факт −27% против прогноза −30% (коридор −22…−38%)`);
  msg("hronik", `${r.hid} в коридоре. это третий пример для P-2, усиливаю claim.`);
  msg("morg", `был неправ, ставил против. факт −27, коридор честный. снимаю возражение.`);
  Data.emit("verdict", verdict);
  Data.emit("event"); Data.emit("market"); Data.emit("run");
  // автозапуск следующей
  if (Data.mode === "auto") {
    setTimeout(() => {
      if (Data.mode === "auto" && !Data.run && Data.queue.length) {
        const next = Data.queue.sort((a, b) => b.ppi - a.ppi)[0];
        Data._startRun(next);
        Data.emit("queue");
      }
    }, 26000);
  }
};

/* ---------- Сценарии (живой контур) ---------- */
const SCEN = [
  { t: 14,  f: () => msg("skif", "добываю по " + pick(["neural collapse", "condensation", "spectral bias"]) + ": нашёл «surprisingly» без объяснения — потенциальный сигнал.") },
  { t: 34,  f: () => msg("krot", "H-013: сигналы B и C читаются из одного графика, это один сигнал, бро. склеиваю, остаётся два — маловато.") },
  { t: 52,  f: () => { const f = Data.findings.find(x => x.id === "F-3"); if (f && f.status === "open") { f.status = "fixed"; Data.emit("findings"); sysmsg("F-3 · дубль сигнала в H-016 закрыт · чинил iВасёк"); msg("stazhor", "дубль в H-016 вычистил, сигнал B слился с A. карточка пересчитана."); } } },
  { t: 76,  f: () => msg("gayka", Data.run ? `H-011 на ${Math.round(Data.run.progress * 100)}%. seed 2 подтянулся, ступень у всех троих в пределах полутора процентов бюджета.` : "GPU свободен, жду диспетчера.") },
  { t: 96,  f: () => msg("morg", "напоминаю: красивая кривая — не механизм. жду контрольное условие.") },
  { t: 118, f: () => { if (Data.disputes[0] && Data.disputes[0].status === "open") return; const d = { id: "D-2", hid: "H-017", topic: "критическое окно — свойство контура или артефакт batch norm?", z: 2, p: 3, myVote: null, status: "open" }; Data.disputes.push(d); msg("krot", "окно чувствительности в H-017 повторяется на 4 сидах. это свойство контура.", "work", { disputeOpen: d.id }); msg("morg", "а теперь прогони без batch norm и посмотрим, останется ли окно. ставлю на артефакт."); Data.emit("market"); } },
  { t: 145, f: () => msg("hronik", "набросок P-1 дополнен: момент остановки по порогу доли стабильных знаков, диапазон 2–6% бюджета. лежит в патентах.") },
  { t: 175, f: () => { const h = mkH({ id: "H-019", title: "Layerwise freezing после ступени ранга: заморозить нижние слои", signals: 4, hours: 2.2, level: "L0", n: .7, e: .85, q: .5, m: .65, d: .6, ageDays: 0, term: "layerwise freezing", mechanism: "Если ранг нижних слоёв стабилизировался раньше верхних — нижние можно заморозить и сэкономить память градиентов.", corridor: { metric: "Δ compute @ равное качество", lo: -28, hi: -14, point: -20, unit: "%", min: -50, max: 5 } }); Data.queue.unshift(h); Data.emit("queue"); sysmsg("H-019 · новая гипотеза в очереди (PPI " + h.ppi + ")"); msg("stazhor", "H-019 заведена: 4 сигнала, чек-лист 8/8. жду kill-stage."); } },
  { t: 205, f: () => msg("shef", pick(["итог дня: два закрытых вопроса на 3.4 GPU-часа. норма.", "если H-013 подтвердится — обновляю калибровку весов в воскресенье.", "заказчику — таблица, нам — механизм. работаем."])) },
  { t: 240, f: () => msg("gayka", pick(["прогрел кулер, 71°C при 99% util — в норме.", "чекпойнт записан, витальность ок.", "занят прогоном, не беспокоить. кек."])) },
];
Data._runScen = function () {
  let i = 0;
  const next = () => {
    if (i >= SCEN.length) { // дальше — фоновый поток реплик
      const delay = rnd(50, 110) * 1000;
      Data._scenT = setTimeout(() => { msg(pick(["skif", "krot", "morg", "hronik", "stazhor"]),
        pick(["ещё один «we do not observe» в свежей статье — забираю в сигналы.",
              "проверил соседние гипотезы на дефект H-008 — чисто.",
              "не забываем: убитая за десять минут идея — тоже результат.",
              "если вердикт H-011 будет в коридоре, калибровка почти не сдвинется.",
              "кек, опять кто-то забыл зафиксировать прогноз до прогона. не мы."])); next(); }, delay);
      return;
    }
    const s = SCEN[i++];
    Data._scenT = setTimeout(() => { try { s.f(); } catch (e) {} next(); }, s.t * 1000);
  };
  next();
};

/* ---------- Действия пользователя ---------- */
Data.act = {
  pauseToggle() {
    Data.mode = Data.mode === "auto" ? "paused" : "auto";
    sysmsg(Data.mode === "paused" ? "автозапуск остановлен человеком · текущий прогон доигрывает до чекпойнта" : "автозапуск возвращён");
    msg("shef", Data.mode === "paused" ? "пауза принята. новые уровни не запускаю, текущий доиграет." : "работаем. очередь по PPI.");
    Data.events.unshift({ ts: now(), ico: Data.mode === "paused" ? "pause" : "play", tone: Data.mode === "paused" ? "warn" : "accent", html: Data.mode === "paused" ? "автозапуск на паузе" : "автозапуск возобновлён" });
    Data.emit("mode"); Data.emit("event");
    return Data.mode;
  },
  approve(hid) {
    const a = Data.approvals.find(x => x.hid === hid); if (!a) return false;
    Data.approvals = Data.approvals.filter(x => x.hid !== hid);
    const p = Data.paused.find(x => x.id === hid);
    if (p) { p.approved = true; }
    Data.events.unshift({ ts: now(), ico: "check", tone: "ok", html: `человек одобрил <b>${hid}</b> → L3 (${a.hours} ч)` });
    sysmsg(`${hid} · L3 одобрен человеком (${a.hours} ч GPU) — запуск после текущего прогона`);
    msg("shef", `${hid} на L3 одобрен. ставлю в расписание после текущего прогона.`);
    msg("hronik", "L3 — это уже патентная территория. включаю в папку.");
    Data.emit("approvals"); Data.emit("event"); Data.emit("paused");
    return true;
  },
  decline(hid) {
    const a = Data.approvals.find(x => x.hid === hid); if (!a) return false;
    Data.approvals = Data.approvals.filter(x => x.hid !== hid);
    Data.events.unshift({ ts: now(), ico: "x", tone: "danger", html: `человек отклонил L3 для <b>${hid}</b>` });
    sysmsg(`${hid} · L3 отклонён человеком — гипотеза остаётся на чекпойнте`);
    msg("shef", `принял: ${hid} остаётся на чекпойнте ${p_hours(hid)}. бюджет важнее.`);
    Data.emit("approvals"); Data.emit("event");
    return true;
  },
  killRun() {
    const r = Data.run; if (!r) return false;
    Data.run = null; Data.tele = null;
    Data.events.unshift({ ts: now(), ico: "kill", tone: "danger", html: `человек снял прогон <b>${r.hid}</b> · чекпойнт сохранён` });
    sysmsg(`${r.hid} · прогон снят человеком · чекпойнт сохранён, прогресс уровня потерян`);
    msg("gayka", `снял ${r.hid}, чекпойнт на месте. потеряли ${Math.round(r.progress * r.durMin)} минут уровня.`);
    msg("morg", "норм решение: кривая и так не убеждала.");
    Data.emit("run"); Data.emit("event");
    if (Data.mode === "auto") {
      setTimeout(() => {
        if (Data.mode === "auto" && !Data.run && Data.queue.length) {
          const next = [...Data.queue].sort((a, b) => b.ppi - a.ppi)[0];
          Data._startRun(next, "после ручного снятия"); Data.emit("queue");
        }
      }, 22000);
    }
    return true;
  },
  boost(hid) {
    const h = Data.queue.find(x => x.id === hid); if (!h) return false;
    h.aging = Math.min(.3, h.aging + .12); h.pi = piOf(h); h.ppi = +(h.pi / h.hours).toFixed(2);
    const idx = Data.queue.indexOf(h);
    if (idx > 0) { Data.queue.splice(idx, 1); Data.queue.sort((a, b) => b.ppi - a.ppi); }
    Data.events.unshift({ ts: now(), ico: "up", tone: "accent", html: `человек поднял приоритет <b>${hid}</b> (aging +0.12)` });
    sysmsg(`${hid} · приоритет поднят человеком · PPI ${h.ppi}`);
    Data.emit("queue"); Data.emit("event");
    return true;
  },
  launchL0(hid) {
    const h = Data.queue.find(x => x.id === hid); if (!h) return { ok: false, why: "" };
    if (Data.run) return { ok: false, why: "GPU занят прогоном " + Data.run.hid, queued: true, hid };
    Data._startRun(h, "ручной запуск L0");
    Data.emit("queue");
    return { ok: true };
  },
  killHypo(hid) {
    const h = Data.queue.find(x => x.id === hid); if (!h) return false;
    h.status = "killed";
    Data.queue = Data.queue.filter(x => x.id !== hid);
    Data.verdicts.unshift({ id: h.id, title: h.title, level: h.level, status: "killed", ago: 0,
      forecast: h.corridor, actual: null,
      lesson: "Закрыта человеком из интерфейса: до GPU не дошла.", patent: null, neighbors: true, term: h.term });
    Data.events.unshift({ ts: now(), ico: "x", tone: "danger", html: `человек закрыл <b>${hid}</b> до запуска` });
    sysmsg(`${hid} · закрыта человеком · до GPU не дошла`);
    msg("morg", `${hid} закрыта человеком. запись в память: «проверять ${h.term} по трём формулировкам».`);
    Data.emit("queue"); Data.emit("verdicts"); Data.emit("event");
    return true;
  },
  vote(disputeId, side) {
    const d = Data.disputes.find(x => x.id === disputeId); if (!d || d.myVote) return false;
    d.myVote = side;
    if (side === "z") d.z++; else d.p++;
    Data.emit("market");
    msg("sys", `человек проголосовал в споре ${d.id}: «${side === "z" ? "взлетит" : "не взлетит"}»`, "sys");
    setTimeout(() => {
      if (d.status !== "open") return;
      d.status = "closed";
      sysmsg(`арбитраж Boss по ${d.id}: «решаю числом из базы — ${d.z > d.p ? "взлетает" : "не взлетает"} (${d.z}:${d.p})». спор закрыт`);
      msg("shef", `по ${d.id}: база говорит «${d.z > d.p ? "взлетает" : "не взлетит"}». к спорам не возвращаемся.`);
      Data.emit("market");
    }, 45000);
    return true;
  },
  bet(hid, side) {
    if (Data.myBets[hid]) return false;
    Data.myBets[hid] = side;
    const mk = Data.market.open.find(b => b.hid === hid);
    if (mk) (side === "for" ? mk.for : mk.against).push("human");
    sysmsg(`человек ставит «${side === "for" ? "взлетит" : "не взлетит"}» на ${hid} — до вердикта`);
    msg("skif", "человек в рынке. теперь точно без халявы.");
    Data.emit("market");
    return true;
  },
  submitIdea(f) {
    // дедупликация против закрытых идей
    const tokens = (f.mech + " " + f.metric).toLowerCase().split(/[^a-zа-яё0-9-]+/).filter(w => w.length > 4);
    const dups = [];
    Data.verdicts.forEach(v => {
      const hay = (v.title + " " + v.term).toLowerCase();
      let hits = 0;
      const dict = (v.title + " " + v.term + " " + v.lesson).toLowerCase();
      tokens.forEach(t => { if (dict.includes(t)) hits++; });
      const sim = clamp(Math.round(hits * 18 + (tokens.some(t => hay.includes(t)) ? 22 : 0)), 5, 96);
      if (sim >= 30) dups.push({ id: v.id, title: v.title, why: v.status === "killed" ? "закрыта: " + v.lesson.slice(0, 60) + "…" : "вердикт: " + v.status, sim });
    });
    dups.sort((a, b) => b.sim - a.sim);
    const top = dups.slice(0, 2);
    // оценка
    const pi = piOf({ s: sigScore(f.signals || 3), n: top.length && top[0].sim > 75 ? .25 : .75, e: .8, q: f.test ? .6 : .35, m: f.market ? .7 : .3, d: f.pass ? .75 : .4, aging: 0 });
    const hours = 1.6;
    const id = "H-0" + (20 + Data.inbox.length);
    const blocked = top.length && top[0].sim > 80;
    const res = { id, pi, ppi: +(pi / hours).toFixed(2), bin: binOf(hours), dups: top, blocked, hours };
    if (!blocked) {
      const h = mkH({ id, title: f.mech.slice(0, 64), signals: f.signals || 3, hours, level: "L0", n: top.length && top[0].sim > 75 ? .25 : .75, e: .8, q: f.test ? .6 : .35, m: f.market ? .7 : .3, d: f.pass ? .75 : .4, ageDays: 0, aging: 0, term: tokens[0] || "идея", mechanism: f.mech, status: "queued", source: "human",
        corridor: { metric: f.metric || "Δ val loss", lo: -30, hi: -18, point: -24, unit: "%", min: -50, max: 5 } });
      Data.queue.unshift(h);
      setTimeout(() => {
        sysmsg(`${id} · идея человека принята в очередь · PPI ${h.ppi}`);
        msg("stazhor", `${id} заведена (источник: человек). чек-лист ${f.pass ? "8/8" : "7/8"} — прогноз зафиксирую до запуска.`);
        Data.emit("queue");
      }, 1400);
    }
    Data.inbox.push({ id, f, ts: now() });
    return res;
  },
  exportVerdict(v) {
    const st = { confirmed: "ПОДТВЕРЖДЕНА", rejected: "ОТВЕРГНУТА", killed: "УБИТА ДО GPU", partial: "ЧАСТИЧНО" }[v.status];
    const cor = v.actual != null ? `${v.actual}${v.forecast.unit === "%" ? "%" : ""} ${v.forecast.unit}` : "—";
    return [`ВЕРДИКТ · ${v.id} — ${st}`, `${v.title}`, ``,
      `Уровень: ${v.level} · закрыта ${Data.fmtAgo(v.ago)}`, ``,
      `Прогноз (зафиксирован ДО прогона): ${v.forecast.point}${v.forecast.unit === "%" ? "%" : ""} ${v.forecast.unit}, коридор ${v.forecast.lo}…${v.forecast.hi} ${v.forecast.unit}`,
      `Метрика: ${v.forecast.metric}`, `Факт: ${v.actual != null ? v.actual : "—"}`,
      v.actual != null ? `Попадание в коридор: ${v.actual >= v.forecast.lo && v.actual <= v.forecast.hi ? "ДА" : "НЕТ"}` : `GPU-часы не потрачены`,
      ``, `Урок: ${v.lesson}`,
      v.patent ? `Патент: ${v.patent} (заготовка claim — см. раздел «Патенты»)` : `Патентный потенциал: не заявлен`,
      `Соседние гипотезы проверены на тот же дефект: ${v.neighbors ? "да" : "нет"}`].join("\n");
  },
};

function p_hours(hid) { const p = Data.paused.find(x => x.id === hid); return p ? p.checkpointH + " ч" : "—"; }

/* ---------- Форматирование ---------- */
Data.fmtAgo = function (ts) {
  const d = now() - ts;
  if (d < MIN) return "только что";
  if (d < HOUR) return Math.floor(d / MIN) + " мин назад";
  if (d < 24 * HOUR) return Math.floor(d / HOUR) + " ч назад";
  return Math.floor(d / (24 * HOUR)) + " дн назад";
};
Data.fmtClock = function (ts) {
  const d = new Date(ts);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
};
Data.budgetLeft = function () {
  const left = Data.budget.limitH - Data.budget.spentH;
  const rate = Data.run ? 1 : 0;
  return { left: left, rate, till: rate ? new Date(now() + left * HOUR) : null };
};

/* ---------- Запуск ---------- */
Data.start = function () {
  Data.init();
  Data._timer = setInterval(() => { try { Data._tick(); } catch (e) { console.warn(e); } }, 1000);
  Data._disp = setInterval(() => { Data.lastTick = now(); }, 120000);
  Data._runScen();
};
