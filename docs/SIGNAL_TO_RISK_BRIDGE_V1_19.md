# TradeMind Signal-to-Risk Bridge v1.19

## Назначение

Мост соединяет три уже существующих слоя:

1. паспорт рыночного сигнала Signal Intelligence;
2. свежий read-only снимок счёта MT5;
3. Risk Manager Core 1.0.

Результат: один account-specific пакет `ALLOW` или `BLOCK` с точными объёмами ступеней, денежным риском, маржой и причинами решения.

## Что мост не делает

- не сканирует рынок;
- не создаёт сигнал из статистики роботов;
- не публикует сигнал;
- не вызывает торговый API;
- не отправляет, не закрывает и не изменяет ордера;
- не меняет настройки MT5 или советников.

## Проверка паспорта

Перед расчётом счёта мост независимо проверяет:

- корневой `signal_id` совпадает с хешем неизменяемого кандидата;
- статистика относится к точному `SIM_V1` setup key кандидата;
- сохранённое состояние равно `PUBLISHABLE`;
- все сохранённые gate checks истинны;
- ордера в safety boundary выключены;
- роботный мониторинг не использован как первичный триггер;
- независимый повторный расчёт quality, Wilson lower, EV и остальных gate checks снова даёт `PUBLISHABLE`.

Надписи `PUBLISHABLE` в изменённом вручную JSON недостаточно.

## Режимы входа

### Явный паспорт

Используется `-Passport`. Любая ошибка паспорта завершает запуск с ошибкой.

### Inbox паспортов

Используется `-PassportsDir`. Мост проверяет все JSON, выбирает самый свежий независимо подтверждённый паспорт. Когда подходящего паспорта нет, состояние:

`WAITING_NO_PUBLISHABLE_PASSPORT`

Это нормальный режим ожидания, а не разрешение на сделку.

## Выход

Для счёта создаются:

- `status.json`;
- `latest_decision.json`, когда паспорт найден;
- `history/<decision_id>.json`;
- событие `SIGNAL_TO_RISK_DECISION` в hash-chain journal.

`ALLOW` означает только прохождение gate и риск-проверок. Исполнение остаётся выключенным.

## Запуск

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v119_signal_to_risk_bridge.ps1" -Login "67206924"
```

Для конкретного паспорта:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v119_signal_to_risk_bridge.ps1" -Login "67206924" -Passport ".\data\signal_intelligence_v1_16\passports\signal.json"
```
