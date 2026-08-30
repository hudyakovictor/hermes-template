# Futures Academy — план продукта и реализации

## Что взято из концепта Three.js Game Skills

Репозиторий рекомендует для polished browser games: TypeScript + Vite, Three.js modules, официальные `three/addons`, Rapier для сложной физики, WebGL/WebGPU с fallback, раздельные слои gameplay/entities/UI/assets, seeded RNG, Playwright smoke/visual tests и обязательные mobile/performance проверки. Это хороший production-подход, но для экранного прототипа лучше не тащить 3D-движок в каждый UI-экран.

## Рекомендуемый стек

| Слой | Выбор | Почему |
|---|---|---|
| App shell | React + TypeScript + Vite | быстрый итерационный UI, типизация состояния, удобные screen flows |
| 3D-слой | Three.js + `three/addons` | интерактивный market-world, avatar, визуальные market events |
| Графики | Lightweight Charts (лицензию проверить) или собственный Canvas | trading-grade свечи, crosshair, zoom; Canvas для game HUD |
| Motion/UI | CSS + Framer Motion (опционально) | responsive layout, accessible controls, мягкая feedback-анимация |
| State | Zustand | session, portfolio, mission, market snapshot без тяжёлой архитектуры |
| Physics | Rapier только если появятся физические объекты | не нужен в первой версии; не использовать ради декора |
| Data | Mock market engine → WebSocket adapter | детерминированная симуляция сначала; API подключается без переписывания UI |
| QA | Vitest + Playwright + screenshot/pixel smoke | проверка игровых сценариев, 390/768/1440 viewport и canvas |

Текущий `miniapp` — zero-dependency визуальный прототип: он намеренно запускается простым Python static server и не является финальным production stack.

## Приоритеты экранов

### P0 — вертикальный срез (сначала)
1. **Терминал / Home** — баланс, BTC chart, рыночное настроение, активная миссия. Это главный экран и точка возврата.
2. **Trade decision / Scenario** — выбор LONG/SHORT, entry, stop-loss, take-profit, размер риска. Обязательный feedback: почему решение безопасно/опасно.
3. **Portfolio** — открытая позиция, PnL, история, закрытие сделки. Без этого нет законченного цикла обучения.

### P1 — обучение и удержание
4. **Обучение** — карта уроков, прогресс, запуск сценария. Текущий прототип уже содержит карточки уроков.
5. **Миссии** — ежедневная серия, XP, streak и рейтинг. Связывает механику с привычкой.
6. **Market event / Result** — итог сделки: прогноз vs факт, риск, ошибки, награда и короткая подсказка.

### P2 — расширение после playtest
7. **Asset explorer** — BTC/ETH/SOL, watchlist, свечные режимы.
8. **Leaderboard / social** — только после подтверждения retention; не отвлекать от обучения.
9. **3D market world** — атмосферный визуальный слой, который не блокирует trading loop.
10. **Профиль, настройки, accessibility, sound** — production hardening.

## Порядок разработки

1. Зафиксировать learning promise и метрики: completion первого сценария, ошибочный риск, D1/D7 retention; реальные деньги запрещены.
2. Сверстать design tokens, responsive shell и P0 screens. Прогнать 390×844, 768×1024, 1440×900.
3. Сделать deterministic market engine: seed, OHLC candles, volatility regimes, event queue, pause/rewind для тестов.
4. Реализовать trade state machine: `idle → planned → open → closed → reviewed`; сделки только paper.
5. Подключить PnL/risk math и explainability: риск в долларах, R-multiple, max drawdown, комиссии.
6. Добавить tutorial overlays и safe defaults (stop-loss обязателен, leverage ограничен).
7. Playtest с 5–8 людьми, сократить friction до первого осмысленного решения менее 90 секунд.
8. После подтверждения loop добавить missions, XP, result review и только затем Three.js ambient world.
9. QA: functional smoke, screenshot baselines, reduced-motion, keyboard, touch, low-end GPU, performance budget 60fps HUD / 30fps low-end 3D.

## Критерии “99/500 факторов”

Не обещать субъективные 99 баллов до измерений. Для каждого экрана нужен scorecard: hierarchy, contrast, spacing, typography, states (loading/empty/error/success), touch targets ≥44px, keyboard focus, reduced motion, latency, responsive behavior, no accidental real-money affordances, clear risk language, deterministic replay, analytics events и screenshot regression. Сначала достигаем 99% прохождения автоматических и ручных gates, затем полируем art direction.

## Что уже сделано в прототипе

`miniapp/` теперь показывает полноценный high-fidelity shell Futures Academy: тёмная финансовая визуальная система, desktop rail/mobile bottom nav, Terminal, Training Hall, Portfolio, Missions, responsive chart canvas, paper-mode messaging, simulated PnL and mission interactions. Это база для дальнейшего переноса в React/Three.js без изменения пользовательской IA.
