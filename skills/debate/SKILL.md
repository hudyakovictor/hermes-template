---
name: debate
description: Показать последнюю открытую Conclave-комнату и короткий transcript в Telegram.
version: 1.0.0
---

# /debate — читать комнату споров

Не создавай новый worker и не делай Qwen-вызов. Покажи последнюю открытую room:

```bash
python tools/rg.py conclave transcript --send --limit 80
```

Если room нет, сообщи это одной строкой. Для подробного управления используй
`/conclave`; для scientific state — только `/h`, `/kill`, `/v` и очередь.
Transcript содержит только короткие русские public messages; English internal
reasoning и chain-of-thought туда не попадают.
