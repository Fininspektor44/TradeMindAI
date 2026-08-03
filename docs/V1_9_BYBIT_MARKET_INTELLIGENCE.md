# TradeMind AI v1.9 Bybit Market Intelligence

## Назначение

Отдельный публичный read-only поток биржевой микроструктуры для криптовалют.
Модуль не использует API-ключ, не подключается к счёту и не содержит торговых функций.

## Динамическая вселенная

Сервис всегда оставляет BTCUSDT и ETHUSDT, затем добавляет восемь самых ликвидных
USDT perpetual альткоинов по `turnover24h`. Состав пересматривается каждые 6 часов.
Исключаются неактивные инструменты и стейблкоины.

По умолчанию:

- всего 10 инструментов;
- минимальный суточный оборот для альткоина: 5 000 000 USDT;
- таймфрейм агрегации: M5;
- источник: `BYBIT_LINEAR`.

## Публичные потоки

Для каждого инструмента сервис подписывается на:

- `publicTrade.{symbol}`;
- `orderbook.50.{symbol}`;
- `tickers.{symbol}`;
- `kline.5.{symbol}`.

## Признаки

На каждой закрытой M5-свече сохраняются:

- OHLC, объём и оборот;
- число сделок и скорость сделок;
- taker buy/sell quantity и turnover;
- delta и накопительный CVD по обороту;
- крупнейшая и средняя сделка;
- spread в bps;
- imbalance стакана на глубинах 5, 10 и 50;
- mark price, index price и basis;
- open interest и его стоимость;
- funding rate;
- 24h turnover и изменение цены.

## Выходные файлы

Каталог `data/bybit_v1_9`:

- `bybit_bars.csv` — каноническая история M5;
- `latest.csv` — последняя закрытая свеча каждого инструмента;
- `universe.csv` — текущий динамический топ;
- `status.json` — heartbeat и состояние подключения;
- `dashboard/index.html` — локальная панель;
- `logs/` — журнал процесса.

Ключ дедупликации: `symbol + start_ms`.

## Ручная проверка вселенной

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v190_bybit.ps1" -DiscoverOnly -OpenDashboard
```

## Короткий пробный запуск

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v190_bybit.ps1" -RunSeconds 90 -OpenDashboard
```

## Установка постоянной задачи

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_v190_bybit_task.ps1" -TopN 10 -RunNow
```

Задача запускается при входе пользователя в Windows и автоматически перезапускается
через одну минуту при аварийном завершении.

## Проверка

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_v190_bybit.ps1" -OpenDashboard
```

## Ограничение v1.9.0

Поток Bybit пока хранится отдельно и не влияет на Paper Gate. Сначала необходимо
проверить стабильность WebSocket, полноту M5-баров и качество признаков. После этого
следующий этап объединит Bybit-признаки с соответствующими BTC/ETH/альткоин-сценариями
RoboForex без смешивания цен и источников.
