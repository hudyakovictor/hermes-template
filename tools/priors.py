#!/usr/bin/env python3
"""researchagen — prior-art поиск по реестру открытых источников.

Правило kill-проверки №2: «Публикационный gap: прямого аналога нет
(arXiv + Semantic Scholar/OpenAlex проверены)». Этот модуль делает проверку
исполнимой: один запрос — много источников, каждый отвечает независимо.

Принципы:
  * покрытие важнее глубины: по умолчанию опрашиваются ВСЕ источники реестра,
    покрытие = ответившие/все; планка для честного вывода — ≥ 0.9;
  * офлайн — не кража: недоступный источник помечается ошибкой, результат
    честно показывает покрытие (вывод «аналогов нет» при покрытии < 0.9 запрещён);
  * кэш в state/priors_cache.json (TTL 7 дней) — повторные запросы бесплатны;
  * только stdlib, таймаут на каждый источник, никакой магии.

CLI:
  python tools/priors.py search "hierarchical cache long context" [--json] [--fresh]
  python tools/priors.py sources
Выход: 0 — покрытие ≥ 0.9; 1 — ниже (вывод делать нельзя); 2 — ошибка команды.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import core

CACHE_TTL_SEC = 7 * 24 * 3600
TIMEOUT_SEC = 6
COVERAGE_MIN = 0.9          # планка «90% источников» для честного вывода
CACHE_PATH = os.path.join(core.STATE_DIR, "priors_cache.json")


def _init_paths() -> str:
    core.ensure_dirs()
    return CACHE_PATH


# --- реестр источников: имя → (URL-шаблон, парсер) -------------------------

def _parse_arxiv(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<entry>(.*?)</entry>", text, re.S):
        e = m.group(1)
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        u = re.search(r'href="([^"]+abs[^"]+)"', e)
        out.append({"source": "arxiv", "title": (t.group(1).strip() if t else "")[:200],
                    "url": u.group(1) if u else ""})
    return out[:5]


def _parse_s2(text: str) -> list[dict]:
    data = json.loads(text)
    return [{"source": "semantic_scholar", "title": (p.get("title") or "")[:200],
             "url": p.get("url", "")}
            for p in data.get("data", [])[:5]]


def _parse_openalex(text: str) -> list[dict]:
    data = json.loads(text)
    return [{"source": "openalex", "title": (w.get("display_name") or "")[:200],
             "url": w.get("id", "")}
            for w in data.get("results", [])[:5]]


def _parse_crossref(text: str) -> list[dict]:
    data = json.loads(text)
    items = data.get("message", {}).get("items", [])
    return [{"source": "crossref", "title": ((i.get("title") or [""])[0])[:200],
             "url": f"https://doi.org/{i.get('DOI', '')}"}
            for i in items[:5]]


def _parse_github(text: str) -> list[dict]:
    data = json.loads(text)
    return [{"source": "github", "title": (r.get("full_name") or "")[:200],
             "url": r.get("html_url", "")}
            for r in data.get("items", [])[:5]]


def _parse_patents(text: str) -> list[dict]:
    """Google Patents XHR: вытащить заголовки без полного HTML-парсера."""
    out = []
    for m in re.finditer(r'"title":"((?:[^"\\]|\\.){5,200}?)"', text):
        title = m.group(1).encode().decode("unicode_escape", "replace")
        if title and title not in [r["title"] for r in out]:
            out.append({"source": "google_patents", "title": title[:200], "url": ""})
        if len(out) >= 5:
            break
    return out


SOURCES: dict[str, tuple[str, Callable[[str], list[dict]]]] = {
    "arxiv": ("http://export.arxiv.org/api/query?search_query=all:%22{q}%22"
              "&max_results=5", _parse_arxiv),
    "semantic_scholar": ("https://api.semanticscholar.org/graph/v1/paper/search"
                         "?query={q}&fields=title,url&limit=5", _parse_s2),
    "openalex": ("https://api.openalex.org/works?search={q}&per-page=5",
                 _parse_openalex),
    "crossref": ("https://api.crossref.org/works?query={q}&rows=5"
                 "&select=title,DOI", _parse_crossref),
    "github": ("https://api.github.com/search/repositories?q={q}&per_page=5",
               _parse_github),
    "google_patents": ("https://patents.google.com/xhr/query?url=q%3D{q}",
                       _parse_patents),
}


# --- кэш -------------------------------------------------------------------

def _cache_load() -> dict:
    _init_paths()
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_save(cache: dict) -> None:
    core.ensure_dirs()
    path = core.safe_path(os.path.relpath(CACHE_PATH, core.ROOT), "кэш")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "researchagen/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return resp.read().decode("utf-8", errors="replace")


def search(query: str, fresh: bool = False, fetch=None) -> dict:
    """Опросить ВСЕ источники реестра по запросу.

    Возвращает {query, coverage, ok, sources: {имя: {ok, n, error}}, results}.
    `ok` = покрытие ≥ 0.9: только тогда вывод «аналогов нет/есть» честен.
    `fetch` — инъекция для тестов (по умолчанию настоящий HTTP).
    """
    query = (query or "").strip()
    if not query:
        return {"query": "", "coverage": 0.0, "ok": False, "sources": {},
                "results": [], "error": "пустой запрос"}
    fetch = fetch or _fetch
    cache = {} if fresh else _cache_load()
    key = query.lower()
    now = time.time()
    hit = cache.get(key)
    if hit and now - hit.get("ts", 0) < CACHE_TTL_SEC and hit.get("complete"):
        return hit["data"]

    q = urllib.parse.quote(query)
    report: dict = {"query": query, "sources": {}, "results": []}
    ok_n = 0
    for name, (url_tpl, parser) in SOURCES.items():
        entry = {"ok": False, "n": 0, "error": ""}
        try:
            body = fetch(url_tpl.format(q=q))
            found = parser(body)
            entry.update(ok=True, n=len(found))
            report["results"].extend(found)
            ok_n += 1
        except Exception as exc:  # noqa: BLE001 — источник независим, идём дальше
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        report["sources"][name] = entry
    report["coverage"] = round(ok_n / len(SOURCES), 2)
    report["ok"] = report["coverage"] >= COVERAGE_MIN
    report["verdict"] = ("покрытие достаточное — вывод обоснован" if report["ok"]
                         else f"покрытие {report['coverage']:.0%} < 90% — "
                              f"вывод «аналогов нет» делать нельзя")
    cache[key] = {"ts": now, "complete": True, "data": report}
    _cache_save(cache)
    return report


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "sources"

    if cmd == "sources":
        data = {"sources": list(SOURCES), "coverage_min": COVERAGE_MIN}
        core.emit(data, as_json,
                  "Источники prior-art: " + ", ".join(SOURCES)
                  + f"\nПланка честного вывода: покрытие ≥ {COVERAGE_MIN:.0%}")
        return 0

    if cmd == "search":
        query = argv[2] if len(argv) > 2 else ""
        if not query.strip():
            core.fail("нужен запрос: python tools/priors.py search \"текст\"")
        report = search(query, fresh=core.flag(argv, "fresh"))
        ok_n = sum(1 for st in report["sources"].values() if st["ok"])
        lines = [f"Prior-art по «{query}»: покрытие {report['coverage']:.0%} "
                 f"({ok_n}/{len(report['sources'])} источников)"]
        for name, st in report["sources"].items():
            lines.append(f"  • {name}: "
                         + (f"{st['n']} находок" if st["ok"] else f"недоступен — {st['error']}"))
        lines.append("  " + report.get("verdict", ""))
        tops = [r for r in report["results"] if r.get("title")][:8]
        for r in tops:
            lines.append(f"  – [{r['source']}] {r['title']}")
        core.emit(report, as_json, "\n".join(lines))
        return 0 if report.get("ok") else 1

    core.fail(f"неизвестная команда {cmd!r} (sources | search \"запрос\")")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
