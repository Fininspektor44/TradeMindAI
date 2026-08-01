# TradeMind v1.6 Unified Signal Center

## Цель

Единый read-only центр объединяет уже существующие исследования, не заменяя и не изменяя их:

- FX Research v1.4.2;
- SMC + Fibonacci OTE v1.5;
- Volume Intelligence v1.4 как источник микроструктуры.

Новый модуль не отправляет ордера и не требует нового советника или графика MT5.

## Сценарии

Центр сохраняет отдельную статистику для:

- internal/swing BOS, CHoCH и BREAK;
- ликвидностного sweep;
- FVG;
- согласованной и конфликтующей структуры;
- RVOL, tick acceleration, quote pressure, absorption и volume impulse;
- низкого, высокого и расширяющегося спреда;
- OTE 61.8, 70.5, 79, всей зоны и подтвержденной зоны;
- BOS + sweep, CHoCH + FVG, sweep + FVG;
- BOS/CHoCH/BREAK + OTE, sweep + OTE, FVG + OTE;
- multi-factor и full-confluence вариантов.

OTE не является обязательным фильтром для остальных сигналов.

## Горизонты и единицы

Центр намеренно не смешивает разные определения результата:

- FX Research: 3/6/12 M5 баров отображаются как M15, M30 и H1, результат в ATR;
- SMC OTE: H3, H6 и H12, результат в R.

Статистика группируется отдельно по источнику, сценарию, инструменту, направлению, горизонту и единице измерения.

## Quality Score

Quality Score 0-100 является прозрачным исследовательским рейтингом, а не обещанием прибыли. Он использует исходный score и добавляет или вычитает фиксированные баллы за подтвержденные факторы, включая структуру, sweep, FVG, OTE, H1/H4, RVOL, tick acceleration, quote pressure и spread regime.

Сравниваются ALL, Score >=60, >=70 и >=80.

## Выходы

- `data/unified_signal_center_v1_6/signals.csv`
- `data/unified_signal_center_v1_6/latest.csv`
- `data/unified_signal_center_v1_6/dashboard/index.html`

## Запуск

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v160_unified_center.ps1" -OpenDashboard
```

## Автоматический запуск

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_v160_unified_center_task.ps1" -EveryMinutes 5 -RunNow
```

Задача Windows: `TradeMindAI-v1.6-UnifiedCenter`.
