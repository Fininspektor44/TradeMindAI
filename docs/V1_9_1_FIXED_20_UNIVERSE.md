# TradeMind AI v1.9.1 Fixed Bybit Universe

## Назначение

Публичный read-only мониторинг фиксированного набора из 20 инструментов Bybit Linear.
Состав не меняется по обороту и не ротируется автоматически.

## Фиксированный список

1. BTCUSDT
2. ETHUSDT
3. UNIUSDT
4. JTOUSDT
5. SOLUSDT
6. BZUSDT
7. NEARUSDT
8. AKEUSDT
9. ONDOUSDT
10. POPCATUSDT
11. XMRUSDT
12. MYXUSDT
13. AAVEUSDT
14. ZECUSDT
15. HYPEUSDT
16. LDOUSDT
17. PUMPFUNUSDT
18. GRASSUSDT
19. XAUTUSDT
20. 1000PEPEUSDT

Если любой запрошенный инструмент отсутствует в публичном списке торгуемых USDT perpetual,
запуск завершается с точным перечнем недоступных символов. Сервис не подменяет их другими
монетами.

## Данные

Для каждого инструмента используются публичные каналы:

- `publicTrade`;
- `orderbook.50`;
- `tickers`;
- `kline.5`.

Сохраняются M5 OHLC, объём, turnover, taker buy/sell, delta, CVD, скорость сделок,
крупнейшая сделка, spread, imbalance стакана 5/10/50, mark/index basis, open interest
и funding.

## Безопасность

API-ключ, доступ к аккаунту и торговые функции не используются. Поток хранится отдельно
от Paper Gate до проверки стабильности и качества данных.

## Проверка списка

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v190_bybit.ps1" -DiscoverOnly -OpenDashboard
```

## Пробный поток

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v190_bybit.ps1" -RunSeconds 90 -OpenDashboard
```

## Постоянная задача

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_v190_bybit_task.ps1" -RunNow
```
