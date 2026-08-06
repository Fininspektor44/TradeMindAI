# TradeMind v1.24 Crypto Signal Adapter

## Назначение

v1.24 подключает существующий read-only контур Bybit к общей платформе Signal Intelligence и Product UI.

Адаптер не запрашивает Bybit напрямую. Он читает только локальные результаты уже работающих модулей:

- `data/bybit_v1_9/bybit_bars.csv`;
- `data/bybit_shadow_v1_10/decisions.csv`;
- `data/bybit_shadow_v1_10/signals.csv`.

## Реальный источник данных

Bybit Shadow v1.10 уже использует:

- M5 как триггер;
- M15 как подтверждение;
- H1 как контекст;
- цену и объём;
- delta и CVD;
- интенсивность и размер сделок;
- spread;
- дисбаланс стакана;
- open interest;
- funding;
- basis.

Crypto Signal Adapter преобразует эти решения в неизменяемый `SignalCandidate`, совместимый с Signal Passport Factory.

## Отдельная статистика

Криптовалютные кандидаты и исходы хранятся отдельно от Forex:

- `data/crypto_signal_intelligence_v1_24/candidates.jsonl`;
- `data/crypto_signal_intelligence_v1_24/outcomes.jsonl`;
- `data/crypto_signal_intelligence_v1_24/factory/`.

Forex и Crypto не смешиваются в одной исторической группе. Объединяется только пользовательская лента Product UI.

## Product UI

Product UI v1.24 добавляет:

- переключатель `Все рынки / Forex / Crypto`;
- пометку `CRYPTO · BYBIT`;
- реальные Bybit M5-свечи;
- Entry, Stop и цели;
- H1/M15/M5 контекст;
- delta и стакан;
- funding, basis и OI;
- отдельное состояние Crypto Factory.

## Честная граница SMC и Fibonacci

Bybit Shadow v1.10 не формирует независимую геометрию BOS, CHoCH, sweep, FVG и Fibonacci OTE. Поэтому v1.24 не выдумывает эти признаки:

- фактор Fibonacci получает нулевой вклад;
- отсутствие OTE явно указано в объяснении;
- геометрия сделки берётся из зафиксированного Bybit shadow-решения.

Отдельный crypto structure engine является самостоятельным будущим этапом и не подменяется текущим адаптером.

## Risk Manager

В v1.24 Crypto проходит исследовательский Passport Factory, но не получает account-specific расчёт Bybit Risk Manager. Для этого потребуется отдельный read-only снимок Bybit-счёта и спецификации контракта.

Forex Risk Manager и MT5 Bridge продолжают работать без изменений.

## Безопасность

- read-only;
- API-ключи не используются;
- сетевые запросы из адаптера отсутствуют;
- ордера не отправляются;
- публикация выключена;
- исходные CSV не изменяются;
- существующие задачи Bybit Collector и Bybit Shadow не перезапускаются;
- при отсутствии или ошибке Crypto-источника Forex runtime продолжает работать.
