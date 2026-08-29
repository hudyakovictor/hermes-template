---
name: gpu
description: Состояние GPU и бюджета: свободная VRAM, текущий прогон, потраченные GPU-часы за сутки.
version: 1.0.0
---

# /gpu — ресурс

```bash
python tools/gpu.py show
python tools/dispatch.py running
```

## Как правильно читать память

Свободная VRAM берётся из `nvidia-smi` (или `torch.cuda.mem_get_info`), НИКОГДА из
`torch.cuda.memory_allocated()`. Причина: Qwen в Ollama живёт в другом процессе,
его память нашему процессу не видна — именно так получаются OOM через три
минуты после «памяти достаточно».

## Планирование на 24 GB (RTX 5090)

Модель Qwen 27B в Q6 с KV-cache Q8 занимает большую часть карты. Поэтому:

- эксперименты проектируются под МАЛЫЕ модели (до сотен миллионов параметров);
- если гипотеза требует больше — срабатывает kill-проверка № 6 (нерешаемость);
- если нужна вся карта целиком — выгрузи модель (`ollama stop`), скажи человеку, что
  на время прогона агент отвечает медленнее, и верни модель после.

Лимиты живут в `config.yaml` → `researchagen.limits`:
`gpu_free_gb_required`, `daily_gpu_hours_budget`, `approval_gpu_hours`,
`max_parallel_experiments` (всегда 1).
