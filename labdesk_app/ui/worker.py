"""Run blocking work (REST, virsh, D-Bus) off the GUI thread."""
from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable, args, kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
        except BaseException as e:  # noqa: BLE001 - reported to the UI
            e.__traceback_text__ = traceback.format_exc()
            self.signals.failed.emit(e)
        else:
            self.signals.done.emit(res)


class TaskRunner(QObject):
    """submit(fn, *args, on_done=..., on_error=...) -> callbacks run on the GUI thread."""

    busy_changed = Signal(int)

    def __init__(self, parent=None, max_threads: int = 6):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max_threads)
        self._tasks: set[_Task] = set()

    def submit(self, fn: Callable, *args, on_done: Callable | None = None,
               on_error: Callable | None = None, **kwargs) -> None:
        task = _Task(fn, args, kwargs)

        def finish(_=None, t=task):
            self._tasks.discard(t)
            self.busy_changed.emit(len(self._tasks))

        if on_done:
            task.signals.done.connect(on_done)
        if on_error:
            task.signals.failed.connect(on_error)
        task.signals.done.connect(finish)
        task.signals.failed.connect(finish)
        self._tasks.add(task)
        self.busy_changed.emit(len(self._tasks))
        self.pool.start(task)

    @property
    def active(self) -> int:
        return len(self._tasks)

    def wait(self, msecs: int = 30000) -> bool:
        return self.pool.waitForDone(msecs)
