"""Stable human- and machine-readable output for Bottom Detection."""

from __future__ import annotations

from typing import Iterable, List, Optional

from .state import Evidence, Hypothesis


_STATUS_TEXT = {
    "candidate": "кандидат: нужна проверка и независимые источники",
    "evaluated": "оценён: решение о продвижении принимает основной контур",
    "promoted": "передан в основную очередь researchagen",
    "rejected": "отклонён на исследовательском слое",
    "archived": "архивирован",
}


def format_verdict(
    hypothesis: Hypothesis,
    evidence: Optional[Iterable[Evidence]] = None,
) -> str:
    """Render exactly SIGNAL/HYPOTHESIS/EXPERIMENT PLAN/VERDICT sections."""

    evidence_list: List[Evidence] = list(evidence or [])
    if evidence_list:
        signal_lines = [
            f"- {item.source}: {item.claim} "
            f"(independence={item.independent_key or 'unknown'}, "
            f"strength={item.strength:.2f})"
            for item in evidence_list
        ]
    else:
        signal_lines = ["- нет проверенных внешних свидетельств; это не вывод"]
    pass_fail = hypothesis.metadata.get("pass_fail", "задать числовой PASS/FAIL до запуска")
    level = hypothesis.metadata.get("level", "L0")
    forecast = "не задан" if hypothesis.forecast is None else f"{hypothesis.forecast:g}%"
    status = _STATUS_TEXT.get(hypothesis.status, hypothesis.status)
    return "\n".join(
        [
            "SIGNAL",
            *signal_lines,
            "",
            "HYPOTHESIS",
            hypothesis.text,
            f"Механизм: {hypothesis.mechanism or 'не сформулирован'}",
            f"Независимых источников: {len({e.independent_key for e in evidence_list if e.independent_key})}",
            "",
            "EXPERIMENT PLAN",
            f"Уровень: {level}; оценка: {hypothesis.estimated_hours:.2f} GPU-ч",
            f"Критерий: {pass_fail}",
            f"Прогноз до запуска: {forecast}",
            "",
            "VERDICT",
            f"Статус: {status}",
            f"Priority: {hypothesis.priority:.3f}",
            "Следующее действие: независимая проверка, затем promotion в основную очередь."
            if hypothesis.status != "promoted"
            else "Следующее действие: заполнить карточку и пройти tools/hypo.py check.",
        ]
    )


__all__ = ["format_verdict"]
