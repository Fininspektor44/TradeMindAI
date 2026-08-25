# TradeMind Signal Passport Factory 1.0

## Назначение

Signal Passport Factory соединяет теневое исследование v1.16 с Signal-to-Risk Bridge v1.19.
Она не создаёт рыночную идею самостоятельно. Фабрика берёт уже сформированного кандидата,
строит статистику точной группы `SIM_V1`, повторно проверяет gate и выдаёт неизменяемый
паспорт только при состоянии `PUBLISHABLE`.

## Цепочка

```text
Market candidate
  -> exact SIM_V1 setup key
  -> outcomes completed before candidate creation
  -> HistoricalEvidence
  -> quality / Wilson lower / PF / EV / RR / drift gate
  -> PUBLISHABLE passport inbox
  -> Signal-to-Risk Bridge
  -> account-specific ALLOW or BLOCK
```

## Защита от утечки будущего

Для кандидата используются только исходы, у которых:

```text
outcome.completed_at <= candidate.created_at
```

Исход самого кандидата исключается. Завершённый кандидат никогда не получает паспорт.
Старый кандидат не может стать торговым предложением задним числом после накопления новой
статистики.

## Состояния

- `PASSPORTS_READY`: создан или уже существует хотя бы один свежий publishable-паспорт.
- `WAITING_NO_FRESH_CANDIDATES`: нет кандидатов младше установленного лимита времени.
- `WAITING_NO_PUBLISHABLE_PASSPORT`: свежие кандидаты есть, но gate их не пропустил.

## Файлы

```text
data/signal_passport_factory_v1/status.json
data/signal_passport_factory_v1/evaluations.json
data/signal_passport_factory_v1/passports/<signal_id>.json
data/signal_passport_factory_v1/archive/
data/signal_passport_factory_v1/quarantine/
```

Bridge должен читать только каталог `passports`. Просроченные паспорта автоматически
перемещаются в `archive`, повреждённые или находящиеся в будущем попадают в `quarantine`.

## Запуск фабрики

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v120_signal_passport_factory.ps1"
```

## Полная read-only цепочка с MT5

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v120_signal_pipeline.ps1" -Login "77053345"
```

## Границы безопасности

- ордера выключены;
- публикация выключена;
- брокерский API не вызывается;
- настройки MT5 и советников не меняются;
- сеточные роботы не используются как источник сигнала;
- `PUBLISHABLE` не означает автоматическое исполнение;
- `ALLOW` Bridge означает только прохождение риск-проверок.
