# TradeMind SER8 MT5 Demo Order Executor v1.3

## v1.3 — экспорт истории ордеров/сделок для автоматической сверки

SER8 теперь корректно сохраняет принятые брокером pending LIMIT-ноги как
`PENDING`, но раньше их перевод в `FILLED`/`CANCELLED`/`EXPIRED`/
`REJECTED` требовал ручной команды. Ни account-, ни positions-, ни
symbols-снапшот не могут ответить на вопрос "что случилось с конкретным
`order_ticket`": строка позиции несёт СОБСТВЕННЫЙ `position_ticket`,
а не тикет исходного pending-ордера, а два pending LIMIT-ноги с
одинаковым символом/стороной (реальная, намеренная форма SER8
multi-entry) неразличимы по символу/стороне/объёму.

Добавлены ровно два новых read-only экспорта на ТОМ ЖЕ таймере
risk-refresh (`InpRiskRefreshSeconds`), без нового EA и без нового
таймера:

- `mt5_risk_orders_utc_<login>.csv` — каждый ордер с магиком
  `InpMagicNumber`, активный (`OrdersTotal()`) или из истории
  (`HistorySelect`/`HistoryOrdersTotal()`, окно `InpHistoryLookbackDays`
  дней, по умолчанию 30) со своим `ENUM_ORDER_STATE`
  (`PLACED`/`FILLED`/`CANCELED`/`EXPIRED`/`REJECTED`/`PARTIAL`/...);
- `mt5_risk_deals_utc_<login>.csv` — соответствующая история сделок за то
  же окно, где `order_ticket` -- это `DEAL_ORDER` (прямая, авторитетная
  ссылка на исходный ордер), плюс реальные `price`/`volume`/
  `deal_ticket`/`position_id`.

Оба файла пишутся атомарно (temp-файл + `FileMove(...,FILE_REWRITE)`),
тем же способом, что и `ExportPositionSnapshot` (v1.2). Ни один из них не
делает ни одного вызова `OrderSend`/`CTrade` -- строго read-only.

Python-сторона: `trademind.ser8_mt5_execution_reconciliation` читает эти
два файла и через `SER8DemoOrderSendControl.reconcile_pending_leg`
(без изменений в её собственной строгой валидации) автоматически
переводит PENDING-ноги в терминальное состояние -- никогда не отправляя
ордер повторно и никогда не угадывая исход по одному лишь исчезновению
ордера из экспорта. См. `scripts/reconcile_ser8_mt5_execution.py` и
`scripts/install_ser8_mt5_reconciliation.ps1`.

## v1.2 — атомарная запись positions-снапшота (исправление NUL-файла)

Реальный сбой на Windows: `ser8_research_risk_gate` отказывал с

```
MT5 account/instrument snapshot could not be verified:
mt5_risk_positions_utc_67206924.csv missing required fields.
```

Прямая проверка на Windows показала: `mt5_risk_positions_utc_<login>.csv`
имел размер 162 байта (ровно длина одной строки заголовка) и состоял ИЗ
ОДНИХ НУЛЕВЫХ БАЙТОВ (`Format-Hex` -> все `00`, `read_bytes()` в Python ->
`b'\x00\x00...'`). Это не проблема кодировки (UTF-16 дал бы чередующиеся
байты, не сплошные нули) и не ошибка Python-парсера -- `mt5_risk_adapter.
_read_csv_stable` корректно отказал ("missing fields: ..."), потому что
файл был буквально повреждён на диске.

**Первопричина** (подтверждена прямым сравнением исходников, а не
предположением): в отличие от `AppendAccountSnapshot` (который
ДОПИСЫВАЕТ одну новую строку в уже полный, почти не меняющийся файл),
`ExportPositionSnapshot` полностью ПЕРЕЗАПИСЫВАЕТ весь positions-файл на
каждом вызове -- так и должно быть, снапшот позиций отражает текущий
момент, а не историю. Открытие и перезапись НАПРЯМУЮ по тому же самому
известному имени файла, которое Python-сторона одновременно опрашивает и
читает, не атомарно: между `FileOpen` (усекает существующий файл) и
`FileClose` (когда новое содержимое полностью записано и сброшено на
диск) существует реальное окно, в течение которого читатель может
увидеть усечённый, пустой или -- как и было воспроизведено на Windows --
обнулённый (NTFS может сделать новую длину файла видимой другому процессу
раньше, чем фактические байты физически туда попадут) файл.

**Исправление**: `ExportPositionSnapshot` теперь пишет полный новый
снапшот во временный файл (`mt5_risk_positions_utc_<login>.csv.tmp`),
сбрасывает и закрывает его, и только затем атомарно заменяет реальный
файл одним вызовом `FileMove(...,FILE_REWRITE)` -- тем же самым
переименованием, которое `ReadAndConsumeRequest` уже использует для
одноразового потребления файла запроса (не новый параллельный механизм).
Читатель реального имени файла теперь может увидеть только ПОЛНЫЙ
предыдущий снапшот или ПОЛНЫЙ новый -- никогда промежуточное, усечённое
или обнулённое состояние. При нуле открытых позиций файл всегда содержит
валидный заголовок без строк данных -- никогда не становится
нулевой длины или обнулённым.

Python-сторона (`mt5_risk_adapter.py`) не изменена и не ослаблена: она
по-прежнему обязана видеть все `POSITION_REQUIRED_FIELDS` в заголовке и
корректно отказывает на любой повреждённый файл.

## v1.1 — объединение с Risk Snapshot Exporter (ОДИН EA вместо двух)

Начиная с v1.1, `TradeMind_Demo_Order_Executor_v1.mq5` дополнительно
выполняет ту же самую read-only функциональность, что и отдельный
`TradeMind_MT5_Risk_Snapshot_Exporter.mq5` (account/positions/Market Watch
snapshot), на своём собственном независимо настраиваемом таймере
(`InpRiskRefreshSeconds`, по умолчанию 30с, минимум 10с). Для SER8
demo-цепочки достаточно прикрепить **ОДИН EA к ОДНОМУ графику** -- второй
советник для risk-снапшота **больше не требуется**.

- Используется РОВНО ОДИН `EventSetTimer()` на весь файл (таймер работает
  на более мелком из двух настроенных интервалов `InpPollSeconds`/
  `InpRiskRefreshSeconds`; каждая из двух задач -- опрос ордеров и
  risk-снапшот -- запускается по своему собственному, независимо
  отслеживаемому времени последнего запуска, а не по общему тику).
- Risk-снапшот функции (`AppendAccountSnapshot`/`ExportPositionSnapshot`/
  `ExportSymbolSnapshot`) перенесены дословно, с той же самой схемой
  колонок и теми же именами файлов
  (`mt5_risk_account_utc_<login>.csv`,
  `mt5_risk_positions_utc_<login>.csv`,
  `mt5_risk_symbols_utc_<login>.csv`), что и в
  `TradeMind_MT5_Risk_Snapshot_Exporter.mq5` -- Python-сторона
  (`mt5_risk_adapter.py`, `scripts/run_ser8_real_demo_pipeline.py`) не
  требует никаких изменений схемы.
- Риск-снапшот строго read-only: не содержит ни одного вызова
  `CTrade`/`OrderSend`, никогда не участвует в решении об исполнении
  ордера. Ошибка записи снапшота (например, файл занят) логируется и
  пропускается -- она НИКОГДА не авторизует и не блокирует ордер. Отказ
  ордера (reject/malformed) точно так же никогда не пропускает следующий
  запланированный снапшот -- обе задачи полностью независимы в
  `OnTimer()`.
- **Позиционирование объёма (lot size) по-прежнему НИКОГДА не вычисляется
  в MQL5.** `volume` в исполнителе всегда приходит только из уже
  авторизованного файла запроса (`ser8_demo_order_request_<login>.csv`),
  который, в свою очередь, содержит ровно то значение, которое вычислил
  `risk_manager.evaluate_risk` -> `RiskDecision.SizedOrder` на
  Python-стороне. Эта цепочка (MT5 snapshot -> Python SER8 Risk Manager ->
  RiskDecision.SizedOrder -> authorization/claim -> executor получает
  фиксированный volume -> broker order) в v1.1 не изменилась ни на одно
  поле.
- Отдельный `TradeMind_MT5_Risk_Snapshot_Exporter.mq5` остаётся в
  репозитории без изменений -- он всё ещё нужен для НЕ-SER8 конвейеров
  (`run_v118_mt5_risk_adapter.ps1`, `run_v119_signal_to_risk_bridge.ps1`,
  `run_v121_live_signal_runtime.ps1`, `run_v130_breakeven_runtime.ps1`),
  которые продолжают ожидать те же самые `mt5_risk_*_utc_*.csv` файлы
  независимо от того, что делает SER8. Для production SER8 demo-пути его
  больше не нужно прикреплять.

## Назначение

Этот блок — последний шаг перед первой контролируемой DEMO-сделкой. Он не
изобретает ничего нового по сравнению с уже закрытой цепочкой ACCEPTED ->
scope -> RiskDecision -> ExecutionAuthorization -> one-shot claim; он только
превращает уже one-shot-claimed авторизацию в ровно один запрос на ордер,
отправляет его ровно один раз через инжектируемый транспорт, и проверяет/
сохраняет результат брокера.

Цепочка:

```text
valid ExecutionAuthorizationClaimV1
        v
DemoAccountAuthorizationV1 (существующий, неизменённый gate)
        v
DemoOrderRequestV1 (symbol/action/order_type/volume из RiskDecision.orders[0],
                     sl/tp из ТОГО ЖЕ SignalCandidate.plan -- ничего не придумано)
        v
атомарный one-shot send-guard (SQLite, PRIMARY KEY claim_id)
        v
DemoOrderTransport (FakeDemoOrderTransport в тестах /
                     FileBridgeDemoOrderTransport в проде)
        v CSV in Common\Files\TradeMindAI
TradeMind_Demo_Order_Executor_v1.mq5 (EXECUTOR ONLY)
        v CTrade.Buy/Sell/BuyLimit/SellLimit/BuyStop/SellStop -- ровно один раз
Реальный MT5 DEMO ордер
        v CSV result обратно
DemoOrderExecutionReceiptV1 (persisted, immutable)
```

## Что это НЕ делает

- не отправляет реальный live-ордер (только DEMO/PAPER, через явный allowlist
  без override/force/bypass);
- не изобретает volume/price/SL/TP -- всё берётся из уже верифицированной
  цепочки RiskDecision + TradePlan;
- не повторяет отправку автоматически ни при какой ошибке, requote, partial
  fill или таймауте -- при неопределённом исходе результат сохраняется как
  `UNKNOWN` и требует ручной сверки;
- не содержит grid/averaging/martingale/сигнальной логики на стороне MQL5 --
  исполнитель только исполняет уже принятое SER8 решение.

## Python-адаптер

`trademind.ser8_mt5_demo_order_send.SER8DemoOrderSendControl.send(claim,
decision, candidate, allowlist=..., now=...)` — полностью протестирован с
`FakeDemoOrderTransport` (28 тестов), включая: точное построение запроса,
неверный/просроченный/подделанный claim, больше одного `SizedOrder`,
конкурентные вызовы (ровно один успешный send), отклонение/requote/partial
fill/malformed результат, отказ транспорта без автоповтора, и полную
провенанс-цепочку в сохранённой квитанции.

## MQL5-исполнитель (не скомпилирован и не запущен в этой среде)

Файл: `mt5/TradeMind_Demo_Order_Executor_v1.mq5`.

- читает не более одного pending-запроса за тик таймера
  (`ser8_demo_order_request_<login>.csv`);
- немедленно переименовывает файл запроса в `.consumed` перед отправкой
  ордера -- запрос не может быть прочитан повторно;
- независимо проверяет `demo_account_id` запроса против текущего
  `AccountInfoInteger(ACCOUNT_LOGIN)` терминала -- SER8 уже проверил это на
  своей стороне, но исполнитель никогда не доверяет этому "на слово";
  опционально можно жёстко закрепить один login через `InpApprovedDemoLogin`;
  проверяет magic number запроса против `InpMagicNumber`;
  отклоняет запрос, если символ недоступен в Market Watch;
- вызывает ровно ОДИН `trade.Buy/Sell/BuyLimit/SellLimit/BuyStop/SellStop` в
  зависимости от `order_type`/`action` запроса;
- пишет ровно один результат (`ser8_demo_order_result_<login>.csv`) с
  retcode/order_ticket/deal_ticket/filled_volume/filled_price;
- не содержит `OnTick`, grid/averaging/martingale логики, и не принимает
  никаких торговых решений сама -- SER8 остаётся единственным источником
  авторизации;
- дополнительно (v1.1), на независимом таймере, пишет read-only
  account/positions/symbols risk-снапшот -- см. раздел "v1.1" выше.

**Этот файл не был скомпилирован MetaEditor и не запускался ни на одном
терминале.** Его синтаксис написан вручную, по образцу уже работающих
экспортёров этого репозитория (`TradeMind_MT5_Risk_Snapshot_Exporter.mq5`),
но требует реальной компиляции и проверки перед первым использованием.

## Ручная валидация первой DEMO-сделки (обязательно вручную, под наблюдением)

**Ничего из этого не выполняется автоматически. Никогда не подключайте
live-счёт к этому исполнителю.**

1. **Скомпилировать исполнитель.** Открыть
   `mt5/TradeMind_Demo_Order_Executor_v1.mq5` в MetaEditor на Windows,
   `Compile`, убедиться в отсутствии ошибок/предупреждений. Это ЕДИНСТВЕННЫЙ
   `.mq5`, который нужно скомпилировать для SER8 demo-пути.
2. **Установить/прикрепить.** Либо запустить
   `scripts\install_ser8_demo_order_executor.ps1` (копирует исходник в
   `MQL5\Experts\TradeMindAI` каждого найденного терминала, аналогично
   `install_v118_mt5_risk_exporter.ps1`), либо скопировать `.ex5` вручную в
   `MQL5\Experts` терминала (`%APPDATA%\MetaQuotes\Terminal\<id>\MQL5\Experts`).
   Прикрепить советник к ОДНОМУ графику на **DEMO/PAPER-счёте** -- это
   ЕДИНСТВЕННЫЙ EA, который нужно прикрепить; отдельный
   `TradeMind_MT5_Risk_Snapshot_Exporter.mq5` для SER8 demo-пути прикреплять
   не нужно. Разрешить автоматическую торговлю (`AutoTrading` включён)
   только для этого графика.
3. **Проверить DEMO login.** Убедиться, что `AccountInfoInteger(ACCOUNT_LOGIN)`
   этого терминала -- это именно тот `account_id`, который будет в
   `DemoAccountAllowlistV1` на Python-стороне. Никогда не использовать
   номер живого счёта.
4. **Проверить символ.** Убедиться, что символ будущей demo-сделки виден в
   Market Watch терминала и торгуется (`trade_mode` не `DISABLED`/`CLOSEONLY`).
5. **Запустить SER8-запрос.** На Python-стороне: собрать реальный
   `ExecutionAuthorizationClaimV1` через уже закрытую цепочку, создать
   `SER8DemoOrderSendControl` с `FileBridgeDemoOrderTransport(common_files_dir=...,
   login=<тот же demo login>)`, и вызвать `.send(claim, decision, candidate,
   allowlist=DemoAccountAllowlistV1(account_ids=(<demo login>,)))`.
6. **Наблюдать ровно один ордер.** В терминале должна появиться ровно одна
   новая позиция/ордер с ожидаемым symbol/action/volume. Если появилось
   больше одного -- немедленно остановить исполнитель (`Remove Expert`) и
   разбираться вручную; это не ожидаемое поведение и в тестах не
   воспроизводится (guard гарантирует ровно одну попытку на claim_id).
7. **Проверить ticket/deal/SL/TP.** Сверить `order_ticket`/`deal_ticket` в
   написанном `ser8_demo_order_result_<login>.csv` с реальной историей
   сделок терминала; сверить, что SL/TP позиции в терминале совпадают с
   `candidate.plan.stop_price`/`targets[0]`, которые были в запросе.
8. **Проверить сохранённую квитанцию.** В SQLite (`ser8_mt5_demo_order_receipts`,
   тот же файл БД, что и `HypothesisRegistry`) должна быть ровно одна строка
   для этого `claim_id` с `result_state="FILLED"` и совпадающими
   ticket/volume/price.
9. **Доказать, что повторный вызов не может отправить снова.** Вызвать
   `.send(...)` ещё раз с ТЕМИ ЖЕ `claim`/`decision`/`candidate` -- должно
   поднять `SER8DemoOrderAlreadyAttemptedError`, транспорт НЕ должен быть
   вызван повторно, и в терминале не должно появиться никакого нового
   ордера.

Ни один реальный ордер не был отправлен в рамках разработки этой задачи.
Шаги выше должны быть выполнены человеком на реальном Windows-терминале,
результат должен быть предоставлен явно, прежде чем эта цепочка может
считаться подтверждённой на реальном DEMO-счёте.
