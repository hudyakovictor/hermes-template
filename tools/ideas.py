#!/usr/bin/env python3
"""researchagen — конвейер идей от человека: разбор → очередь ИЛИ лог неэффективных.

Поток (таймлайн от старта до конца):
  1. /idea "текст"       — идея попадает в inbox, экипаж обсуждает поступление;
                           дубли ловятся СРАЗУ: «это уже рассматривали»;
  2. /triage IN-XXX      — агенты обсуждают идею с цифрами (перспективность PI,
                           приоритет PPI), решение по порогам: сигналы ≥ 3 и
                           PI ≥ 0.40 → очередь (карточка + ставки), иначе —
                           в лог неэффективных с причиной;
  3. /ideas              — очередь рассмотренных идей и лог отклонённых;
  4. любая новая идея/гипотеза дублируется против лога на ранней стадии.

Оценка без флагов — предварительная, по маркерам текста (ссылки, числа,
покупатель, метрика); флаги агента (--signals --money ...) её перекрывают.

CLI:
  python tools/ideas.py submit "текст идеи" [--from telegram]
  python tools/ideas.py triage IN-001 [--title "..." --signals 3 --forecast 10 ...]
  python tools/ideas.py log [--verdict rejected|queued|duplicate]
  python tools/ideas.py dup "текст"          — проверка на дубли
"""

from __future__ import annotations

import re
import sys
import time

import core
import crew
import hypo
import inbox
import queue as q

DUP_THRESHOLD = 0.55          # выше — считаем дубликатом
PI_MIN = 0.40                 # порог перспективности для постановки в очередь
SIGNALS_MIN = 3               # и три независимых источника

STOPWORDS = set("""и в во не что он на я с со как а то все она так его но да ты к у же
вы за бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну
вдруг ли если уже или ни быть был него до вас нибудь опять уж вам сказал ведь
там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была
сам чтоб без будто человек чего раз тоже себе под жизнь будет ж тогда кто этот
того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
сейчас были куда зачем сказать всех никогда сегодня можно при наконец два об
другой хоть после над больше тот через эти нас про всего них какая много разве
три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им
более всегда конечно всю между это""".split())

SOURCE_MARKERS = ("http", "arxiv", "doi", "github", "paper", "ссылк", "статья",
                  "статье", "препринт", "репо", "бенчмарк", "benchmark")
MONEY_WORDS = ("прода", "покупат", "клиент", "экономи", "стоим", "доход", "выруч",
               "спрос", "монетиз", "лиценз")
METRIC_WORDS = ("метрик", "измер", "замер", "bench", "скор", "loss", "качеств",
                "точност")
NOVELTY_WORDS = ("нов", "никто", "первый", "впервые", "gap", "не замечал",
                 "неизвестн")


def _tokens(text: str) -> set[str]:
    flat = re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е"))
    return {t for t in flat.split() if len(t) >= 3 and t not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Похожесть идей: Jaccard + вхождение короткого текста в длинный."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    jac = len(inter) / len(ta | tb)
    contain = len(inter) / min(len(ta), len(tb))
    return round(max(jac, 0.9 * contain), 3)


# ---------------------------------------------------------------- сигнал-банк
# Двусторонняя память истории: гипотеза может умереть, а её сигналы — нет.
# confirmed/reusable блоки предлагаются как строительные блоки новых гипотез,
# refuted — «это уже проверено тестом», чтобы не гонять одно и то же.

BANK_THRESHOLD = 0.5          # ниже порога дублей: ловим именно сигналы
CLAIM_RE = re.compile(r'claim:\s*"([^"]{10,})"')


def bank_save(conn, hid: str, title: str, card_text: str, outcome: str,
              evidence: str) -> int:
    """Записать гипотезу в банк: заголовок (проверен тестом) + сигналы карточки.

    outcome для заголовка — вердикт (confirmed/refuted/partial); для сигналов
    из карточки — 'reusable' у непрошедших (сигналы не опровергнуты, гипотеза
    падала) и 'confirmed' у подтверждённых.
    """
    rows = [(hid, title.strip(), outcome, evidence, core.iso())]
    for claim in CLAIM_RE.findall(card_text or ""):
        rows.append((hid, claim.strip(),
                     "confirmed" if outcome == "confirmed" else "reusable",
                     evidence, core.iso()))
    conn.executemany(
        "INSERT INTO signal_bank (hid, claim, outcome, evidence, created_at)"
        " VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def check_signal_bank(conn, text: str) -> list[dict]:
    """Совпадения идеи с банком сигналов — память прошлых прогонов.

    confirmed/reusable → «строительный блок доступен» (плюс к новой гипотезе),
    refuted → «уже проверено тестом» (не рассматривать повторно).
    """
    out: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT hid, claim, outcome, evidence FROM signal_bank").fetchall()
    except Exception:  # noqa: BLE001 — старая база без таблицы
        return out
    for r in rows:
        score = similarity(text, r["claim"] or "")
        if score >= BANK_THRESHOLD:
            out.append({"hid": r["hid"], "claim": r["claim"],
                        "outcome": r["outcome"], "evidence": r["evidence"],
                        "score": score})
    return sorted(out, key=lambda m: -m["score"])[:5]


def signal_bank_list(conn) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT hid, claim, outcome, evidence, created_at FROM signal_bank"
            " ORDER BY signal_id DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def find_duplicates(conn, text: str, threshold: float = DUP_THRESHOLD) -> list[dict]:
    """Дубли против: лога идей (вкл. отклонённые) и всех гипотез в базе.

    Возвращает отсортированный по похожести список с вердиктом и причиной —
    из него строится ранний ответ «это уже рассматривали».
    """
    out = []
    try:
        rows = conn.execute(
            "SELECT idea_id, title, text, verdict, reason, hypo_id FROM idea_log"
        ).fetchall()
    except Exception:  # noqa: BLE001 — таблицы может не быть на старой базе
        rows = []
    for r in rows:
        score = max(similarity(text, r["title"] or ""), similarity(text, r["text"] or ""))
        if score >= threshold:
            out.append({"kind": "идея", "id": r["idea_id"], "score": score,
                        "verdict": r["verdict"], "why": r["reason"] or "",
                        "hypo_id": r["hypo_id"]})
    for r in conn.execute(
            "SELECT id, title, status FROM hypotheses").fetchall():
        score = similarity(text, r["title"] or "")
        if score >= threshold:
            out.append({"kind": "гипотеза", "id": r["id"], "score": score,
                        "verdict": r["status"], "why": ""})
    return sorted(out, key=lambda d: -d["score"])


def quick_estimate(text: str) -> dict:
    """Предварительная оценка факторов по маркерам текста (без LLM, честно

    помечается как предварительная: финальные цифры даёт агент на /triage).
    """
    t = (text or "").lower()
    signals = min(SIGNALS_MIN, sum(1 for m in SOURCE_MARKERS if m in t))
    novelty = 0.5 + (0.2 if any(w in t for w in NOVELTY_WORDS) else 0.0)
    money = 0.3 + (0.3 if any(w in t for w in MONEY_WORDS) else 0.0)
    decidability = 0.4 + (0.3 if any(w in t for w in METRIC_WORDS) else 0.0)
    early = 4.0 if "ранн" in t or "early" in t else 10.0
    return {"signals": signals, "novelty": round(min(novelty, 1.0), 2),
            "money": round(min(money, 1.0), 2),
            "decidability": round(min(decidability, 1.0), 2),
            "early_pct": early, "standard": 0.4, "est_hours": 4.0,
            "preliminary": True}


def _log_idea(conn, idea_id: str, text: str, title: str, verdict: str,
              reason: str, pi=None, ppi=None, hypo_id=None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO idea_log (idea_id, text, title, verdict, reason,"
        " pi, ppi, hypo_id, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (idea_id, text, title, verdict, reason,
         None if pi is None else round(float(pi), 3),
         None if ppi is None else round(float(ppi), 3),
         hypo_id, "human", core.iso()))
    conn.commit()


def submit(text: str, source: str = "telegram") -> dict:
    """Точка входа бота: идея → ранний дубль-чек → inbox + обсуждение поступления."""
    text = inbox.sanitize(text)
    if not text:
        core.fail("идея пустая — нечего разбирать")
    conn = core.db()
    try:
        dups = find_duplicates(conn, text)
        if dups:
            dup = dups[0]
            verdict_ru = {"rejected": "отклонено", "queued": "в очереди",
                          "duplicate": "дубликат"}.get(dup["verdict"], dup["verdict"])
            why = f" {dup['why']}" if dup["why"] else ""
            reason = (f"дубль {dup['kind']} {dup['id']} "
                      f"(похожесть {dup['score']:.0%}, {verdict_ru}){why}")
            if dup["verdict"] == "queued" and dup.get("hypo_id"):
                reason += f" — уже в очереди {dup['hypo_id']}"
            _log_idea(conn, f"DUP-{int(time.time() * 1000) % 100000:05d}",
                      text, text[:80], "duplicate", reason)
            crew.safe_emit("idea_dup", conn=conn, ctx={
                "dup_id": dup["id"], "dup_verdict": verdict_ru,
                "dup_why": dup["why"] or "причина в логе",
                "score": f"{dup['score']:.0%}"})
            core.log_event(conn, "ideas.duplicate", None, dup=dup["id"],
                           score=dup["score"])
            return {"ok": False, "duplicate": True, "reason": reason, "dups": dups}
        bank_matches = check_signal_bank(conn, text)
        est = quick_estimate(text)
        item = inbox.add(text, source=source)
        crew.safe_emit("idea_intake", conn=conn, ctx={
            "iid": item["id"], "title": text[:80],
            "signals_est": est["signals"]})
        core.log_event(conn, "ideas.submit", None, inbox_id=item["id"])
        res = {"ok": True, "inbox_id": item["id"], "estimate": est,
               "next": f"разбор: python tools/rg.py triage {item['id']}"}
        if bank_matches:
            res["signal_matches"] = bank_matches
            refuted = [m for m in bank_matches if m["outcome"] == "refuted"]
            reusable = [m for m in bank_matches if m["outcome"] != "refuted"]
            if reusable:
                crew.safe_emit("signal_recall", conn=conn, ctx={
                    "iid": item["id"], "hid": reusable[0]["hid"],
                    "outcome": reusable[0]["outcome"],
                    "claim": reusable[0]["claim"][:70]})
                core.log_event(conn, "ideas.signal_match", None,
                               inbox_id=item["id"], bank_hid=reusable[0]["hid"],
                               outcome=reusable[0]["outcome"])
            if refuted:
                res["warning"] = (f"похоже на проверенное и опровергнутое "
                                  f"({refuted[0]['hid']}): уточни отличие механизма")
        return res
    finally:
        conn.close()


def _gaps(est: dict, pi: float) -> list[str]:
    gaps = []
    if est["signals"] < SIGNALS_MIN:
        gaps.append(f"сигналов {est['signals']} < {SIGNALS_MIN}")
    if pi < PI_MIN:
        gaps.append(f"перспективность {pi:.2f} < {PI_MIN:.2f}")
    if est["money"] < 0.5:
        gaps.append("не назван покупатель")
    return gaps


def triage(inbox_id: str, factors: dict | None = None,
           title: str | None = None, forecast=None) -> dict:
    """Разбор идеи экипажем: обсуждение с цифрами → очередь или лог неэффективных."""
    items = inbox._load()
    target = next((i for i in items if i["id"] == inbox_id), None)
    if target is None:
        core.fail(f"{inbox_id} не найден в inbox")
    if target["state"] != "new":
        core.fail(f"{inbox_id}: уже обработан ({target['state']})")
    text = target["text"]
    conn = core.db()
    try:
        dups = [d for d in find_duplicates(conn, text) if d["id"] != inbox_id]
        if dups:
            dup = dups[0]
            inbox.drop(inbox_id, why=f"дубль {dup['id']}")
            _log_idea(conn, inbox_id, text, title or text[:80], "rejected",
                      f"дубль {dup['kind']} {dup['id']}", hypo_id=None)
            crew.safe_emit("idea_dup", conn=conn, ctx={
                "dup_id": dup["id"],
                "dup_verdict": {"rejected": "отклонено"}.get(
                    dup["verdict"], dup["verdict"]),
                "dup_why": dup["why"] or "причина в логе",
                "score": f"{dup['score']:.0%}"})
            return {"ok": False, "duplicate": True, "dups": dups}

        est = quick_estimate(text)
        for k, v in (factors or {}).items():
            if v is None:
                continue
            est[k] = int(v) if k in ("signals", "demand_signals") else v
        row = {"signals": est["signals"], "novelty": est["novelty"],
               "early_pct": est["early_pct"], "standard": est["standard"],
               "money": est["money"], "decidability": est["decidability"],
               "est_hours": est["est_hours"], "created_at": core.iso()}
        pi = q.pi(row, with_aging=False)
        ppi = q.ppi(row)
        note = ("предварительная оценка по тексту" if est.get("preliminary")
                else "оценка агента")
        crew.safe_emit("idea_review", conn=conn, ctx={
            "iid": inbox_id, "title": title or text[:80],
            "pi": f"{pi:.2f}", "ppi": f"{ppi:.2f}",
            "signals": est["signals"], "money": f"{est['money']:.1f}",
            "note": note})

        gaps = _gaps(est, pi)
        if gaps:
            reason = "; ".join(gaps)
            inbox.drop(inbox_id, why=reason)
            _log_idea(conn, inbox_id, text, title or text[:80], "rejected",
                      reason, pi=pi, ppi=ppi)
            crew.safe_emit("idea_rejected", conn=conn, ctx={
                "iid": inbox_id, "reason": reason})
            core.log_event(conn, "ideas.rejected", None, inbox_id=inbox_id,
                           pi=round(pi, 3))
            return {"ok": True, "verdict": "rejected", "reason": reason,
                    "pi": round(pi, 3), "ppi": round(ppi, 3)}

        fields = {"title": title or text[:80], "signals": est["signals"],
                  "novelty": est["novelty"], "early_pct": est["early_pct"],
                  "standard": est["standard"], "money": est["money"],
                  "decidability": est["decidability"],
                  "est_hours": est["est_hours"], "source": f"inbox:{inbox_id}"}
        if forecast not in (None, ""):
            fields["forecast"] = forecast
        created = inbox.take(inbox_id, fields)
        hid = created["id"]
        bets = crew.place_bets(conn, hid, created.get("p_repro"))
        _log_idea(conn, inbox_id, text, fields["title"], "queued",
                  f"PI {pi:.2f}, PPI {ppi:.2f}", pi=pi, ppi=ppi, hypo_id=hid)
        crew.safe_emit("idea_queued", conn=conn, ctx={
            "hid": hid, "iid": inbox_id, "title": fields["title"],
            "forecast": "—" if created.get("forecast") is None
            else f"{float(created['forecast']):g}%",
            "ppi": f"{ppi:.2f}",
            "bets_line": crew._bets_line(conn, hid)})
        core.log_event(conn, "ideas.queued", hid, inbox_id=inbox_id,
                       pi=round(pi, 3), bets=len(bets))
        return {"ok": True, "verdict": "queued", "hid": hid, "pi": round(pi, 3),
                "ppi": round(ppi, 3), "bets": len(bets),
                "forecast": created.get("forecast")}
    finally:
        conn.close()


def log_rows(conn, verdict: str | None = None) -> list[dict]:
    sql = "SELECT idea_id, title, verdict, reason, pi, ppi, hypo_id, created_at" \
          " FROM idea_log"
    args = ()
    if verdict:
        sql += " WHERE verdict=?"
        args = (verdict,)
    sql += " ORDER BY rowid DESC LIMIT 50"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def main(argv: list[str]) -> int:
    core.load_env()
    as_json = core.wants_json(argv)
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[1] if len(argv) > 1 else "log"

    if cmd == "submit":
        text = argv[2] if len(argv) > 2 else ""
        if not text.strip():
            core.fail('нужен текст: python tools/ideas.py submit "идея"')
        res = submit(text, source=core.arg(argv, "from", "telegram"))
        if res.get("duplicate"):
            core.emit(res, as_json,
                      f"Дубль: {res['reason']}\nНовое — с новыми данными, "
                      f"иначе ответ тот же.")
            return 1
        bank_txt = ""
        matches = res.get("signal_matches") or []
        if matches:
            ru = {"confirmed": "подтверждён тестом", "refuted": "опровергнут",
                  "partial": "частично", "reusable": "жив, переиспользуем"}
            lines = [f"  • {m['hid']}: {ru.get(m['outcome'], m['outcome'])}"
                     f" — {m['claim'][:60]}" for m in matches[:3]]
            bank_txt = "\nПамять истории (банк сигналов):\n" + "\n".join(lines)
        warn = f"\n⚠️ {res['warning']}" if res.get("warning") else ""
        core.emit(res, as_json,
                  f"Идея {res['inbox_id']} принята в разбор. Экипаж обсуждает.\n"
                  f"Оценка (предварительная): сигналов {res['estimate']['signals']}/3,"
                  f" money {res['estimate']['money']:.1f}.\n"
                  f"Разбор: python tools/rg.py triage {res['inbox_id']}"
                  + bank_txt + warn)
        return 0

    if cmd == "triage":
        inbox_id = argv[2] if len(argv) > 2 else ""
        if not inbox_id:
            core.fail("нужен id: python tools/ideas.py triage IN-001 [--flags]")
        factors = {k: core.to_number(core.arg(argv, flag), k)
                   for k, flag in (("signals", "signals"), ("novelty", "novelty"),
                                   ("money", "money"), ("decidability", "decidability"),
                                   ("est_hours", "hours"), ("early_pct", "early"))
                   if core.arg(argv, flag) not in (None, "")}
        res = triage(inbox_id, factors=factors,
                     title=core.arg(argv, "title"),
                     forecast=core.arg(argv, "forecast"))
        if res.get("duplicate"):
            core.emit(res, as_json, f"Дубль: см. {res['dups'][0]['id']}")
            return 1
        if res["verdict"] == "queued":
            core.emit(res, as_json,
                      f"{inbox_id} → очередь: {res['hid']}\n"
                      f"перспективность {res['pi']:.2f}, приоритет {res['ppi']:.2f}, "
                      f"ставок {res['bets']}\nДальше: python tools/rg.py check {res['hid']}")
        else:
            core.emit(res, as_json,
                      f"{inbox_id} → лог неэффективных: {res['reason']}\n"
                      f"Дубли этой идеи будут отклоняться сразу.")
        return 0

    if cmd == "log":
        conn = core.db()
        try:
            verdict = core.arg(argv, "verdict")
            rows = log_rows(conn, verdict)
        finally:
            conn.close()
        text = core.table(
            [[r["idea_id"], (r["title"] or "")[:48], r["verdict"],
              (r["reason"] or "")[:52]] for r in rows],
            ["id", "идея", "решение", "причина"]) if rows else "Идей в логе нет."
        core.emit(rows, as_json, text)
        return 0

    if cmd == "dup":
        text = argv[2] if len(argv) > 2 else ""
        if not text.strip():
            core.fail('нужен текст: python tools/ideas.py dup "идея"')
        conn = core.db()
        try:
            dups = find_duplicates(conn, text)
        finally:
            conn.close()
        if not dups:
            core.emit({"dups": []}, as_json, "Дублей нет — идея новая для системы.")
            return 0
        lines = [f"Дубли ({len(dups)}):"]
        for d in dups[:5]:
            lines.append(f"  • {d['kind']} {d['id']} — {d['verdict']} "
                         f"(похожесть {d['score']:.0%}) {d['why']}")
        core.emit({"dups": dups}, as_json, "\n".join(lines))
        return 1

    core.fail(f"неизвестная команда {cmd!r} (submit | triage | log | dup)")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
