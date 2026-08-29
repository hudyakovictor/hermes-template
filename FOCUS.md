# FOCUS — текущий домен

> Единственное место, где живёт домен. Меняй этот файл — метод и личность не трогаются.

## Домен
**Training dynamics: раннее выделение полезной структуры, конкуренция контуров,
отделение полезного контура от паразитной памяти и шумового переобучения.**

## Рабочая формулировка вопроса
Существует ли в первые проценты обучения устойчиво измеримый признак того, что
полезный вычислительный контур уже сформирован, и можно ли по этому признаку
(а) остановить обучение раньше, (б) извлечь/усилить контур, (в) получить целевое
качество дешевле, чем при полном обучении?

## Ключевые термины для поиска (оригинальные)
`early bird ticket`, `lottery ticket`, `iterative magnitude pruning`, `sign stability`,
`gradient disparity`, `loss curvature / sharpness`, `linear mode connectivity`,
`grokking`, `condensation`, `neural collapse`, `effective rank`, `spectral bias`,
`memorization vs generalization circuits`, `subnetwork competition`, `weight norm ranking`,
`layerwise freezing`, `low-rank extraction`, `pruning improves generalization`,
`critical learning period`, `information bottleneck phase transition`.

## Запросы ищут аномалии, не определения
- `"anomaly in <term>"`, `"failed to reproduce <term>"`, `"contrary to expectation"`
- `"we do not observe"`, `"surprisingly"`, `"remains unexplained"`
- arXiv: `ti:"<term>" ANDNOT abs:survey`, `abs:"we were unable"`
- «rarely measured»: метрики, которые считают один раз в статье и больше никогда.

## Границы (сюда не уходить)
- философия сознания, «мозг без памяти», аналогии с человеком;
- чистая теория без дешёвого измеримого теста;
- архитектурные новинки ради новизны (домен — про динамику, не про новый блок);
- RLHF/alignment, если это не про раннюю структуру.

## Что считается прогрессом домена
1. новый **измеримый** ранний предиктор полезной структуры;
2. независимость предиктора от 3+ простых объяснений (lr, init, batch, регуляризация);
3. воспроизводимость на ≥ 3 seeds и ≥ 2 архитектурах;
4. выигрыш в compute/времени/данных при неизменном качестве;
5. формализуемость как метод (patent-shaped claim).

## Стоп-условие домена
Если за 30 живых гипотез ни один ранний предиктор не выжил до L2 — домен
переформулируется (запись в `memory/`, обязательный разбор «почему»), а не
повторяется по кругу.
