# TradeMind SER8 MT5 Demo Order Executor v1.0

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
  авторизации.

**Этот файл не был скомпилирован MetaEditor и не запускался ни на одном
терминале.** Его синтаксис написан вручную, по образцу уже работающих
экспортёров этого репозитория (`TradeMind_MT5_Risk_Snapshot_Exporter.mq5`),
но требует реальной компиляции и проверки перед первым использованием.

## Ручная валидация первой DEMO-сделки (обязательно вручную, под наблюдением)

**Ничего из этого не выполняется автоматически. Никогда не подключайте
live-счёт к этому исполнителю.**

1. **Скомпилировать исполнитель.** Открыть
   `mt5/TradeMind_Demo_Order_Executor_v1.mq5` в MetaEditor на Windows,
   `Compile`, убедиться в отсутствии ошибок/предупреждений.
2. **Установить/прикрепить.** Скопировать `.ex5` в `MQL5\Experts` терминала
   (тот же механизм, что и для существующих экспортёров -- общая папка
   `%APPDATA%\MetaQuotes\Terminal\<id>\MQL5\Experts`). Прикрепить советник
   к ОДНОМУ графику на **DEMO/PAPER-счёте**. Разрешить автоматическую
   торговлю (`AutoTrading` включён) только для этого графика.
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
