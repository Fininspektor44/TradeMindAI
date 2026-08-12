from dataclasses import replace

from trademind.orchestrator.dispatcher import DispatchResult
from trademind.orchestrator.models import Task, TaskState
from trademind.orchestrator.notification import NotificationKind
from trademind.orchestrator.service import OrchestratorService, ServiceStatus


class StaticDispatcher:
    def __init__(self, task=None):
        self.task = task
        self.calls = 0

    def next_dispatch(self):
        self.calls += 1
        return DispatchResult(self.task)


class CountingEngine:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def step(self, task_id):
        self.calls += 1
        assert self.result is not None
        assert task_id == self.result.task_id
        return self.result


class MemorySink:
    def __init__(self):
        self.items = []

    def send(self, notification):
        self.items.append(notification)


def test_idle_ticks_make_zero_engine_or_model_work():
    dispatcher = StaticDispatcher(None)
    engine = CountingEngine()
    service = OrchestratorService(
        dispatcher=dispatcher,
        engine=engine,
        idle_sleep_seconds=0,
    )

    for _ in range(10_000):
        tick = service.run_once()
        assert tick.status is ServiceStatus.IDLE

    assert dispatcher.calls == 10_000
    assert engine.calls == 0


def test_human_required_generates_one_exception_notification():
    task = Task.new(task_id="T-human", goal="need approval")
    paused = replace(task, state=TaskState.HUMAN_REQUIRED, assigned_role=None)
    sink = MemorySink()
    service = OrchestratorService(
        dispatcher=StaticDispatcher(task),
        engine=CountingEngine(paused),
        notifications=sink,
        idle_sleep_seconds=0,
    )

    tick = service.run_once()

    assert tick.status is ServiceStatus.STOPPED
    assert tick.state is TaskState.HUMAN_REQUIRED
    assert len(sink.items) == 1
    assert sink.items[0].kind is NotificationKind.HUMAN_REQUIRED


def test_failed_task_generates_failure_notification():
    task = Task.new(task_id="T-fail", goal="fail closed")
    failed = replace(task, state=TaskState.FAILED, assigned_role=None)
    sink = MemorySink()
    service = OrchestratorService(
        dispatcher=StaticDispatcher(task),
        engine=CountingEngine(failed),
        notifications=sink,
        idle_sleep_seconds=0,
    )

    tick = service.run_once()

    assert tick.status is ServiceStatus.STOPPED
    assert tick.state is TaskState.FAILED
    assert sink.items[0].kind is NotificationKind.TASK_FAILED
