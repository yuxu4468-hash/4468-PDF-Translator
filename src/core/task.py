"""翻译任务的状态与注册表。

前端通过轮询 `/api/translate/progress/<task_id>` 获取进度，因此这里维护一个
线程安全的任务注册表，任务在后台线程里跑，状态与日志随时可读。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from ..shared import constants

# 任务状态
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

# 阶段
STAGE_QUEUED = "queued"
STAGE_PARSING = "parsing"
STAGE_OCR = "ocr"
STAGE_TRANSLATING = "translating"
STAGE_TYPESETTING = "typesetting"
STAGE_EXPORTING = "exporting"
STAGE_FINISHED = "finished"

# 各阶段在总进度中的区间（起, 止）
STAGE_WEIGHTS = {
    STAGE_QUEUED: (0.0, 2.0),
    STAGE_PARSING: (2.0, 6.0),
    STAGE_OCR: (6.0, 26.0),
    STAGE_TRANSLATING: (26.0, 88.0),
    STAGE_TYPESETTING: (88.0, 96.0),
    STAGE_EXPORTING: (96.0, 100.0),
    STAGE_FINISHED: (100.0, 100.0),
}

STAGE_LABELS = {
    STAGE_QUEUED: "排队中",
    STAGE_PARSING: "正在解析 PDF",
    STAGE_OCR: "正在识别扫描页（OCR）",
    STAGE_TRANSLATING: "正在翻译",
    STAGE_TYPESETTING: "正在回填排版",
    STAGE_EXPORTING: "正在导出文件",
    STAGE_FINISHED: "已完成",
}


@dataclass
class Task:
    """一个翻译任务的可观测状态。"""

    task_id: str
    file_id: str
    filename: str
    status: str = STATUS_PENDING
    stage: str = STAGE_QUEUED
    message: str = "任务已创建"
    error: str = ""

    # 进度
    batch_done: int = 0
    batch_total: int = 0
    paragraph_done: int = 0
    paragraph_total: int = 0
    page_total: int = 0

    # 时间
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    elapsed: float = 0.0
    eta: float = -1.0

    # 结果
    logs: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)

    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        """追加一条日志（同时写标准日志）。"""
        from ..shared.logger import get_logger

        get_logger("Task").info("[%s] %s", self.task_id, message)
        line = time.strftime("%H:%M:%S") + "  " + str(message)
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > constants.MAX_LOG_LINES:
                del self.logs[: len(self.logs) - constants.MAX_LOG_LINES]

    # ------------------------------------------------------------------
    def percent(self) -> float:
        """综合各阶段计算总进度百分比。"""
        low, high = STAGE_WEIGHTS.get(self.stage, (0.0, 100.0))
        if self.stage == STAGE_TRANSLATING:
            ratio = (self.batch_done / self.batch_total) if self.batch_total else 0.0
        elif self.stage == STAGE_TYPESETTING:
            ratio = (self.paragraph_done / self.paragraph_total) if self.paragraph_total else 0.0
        elif self.stage in (STAGE_FINISHED,) or self.status == STATUS_DONE:
            return 100.0
        else:
            ratio = 0.0
        return round(min(100.0, low + (high - low) * max(0.0, min(1.0, ratio))), 1)

    def refresh_clock(self) -> None:
        """刷新耗时与预计剩余时间。"""
        if not self.started_at:
            return
        end = self.finished_at or time.time()
        self.elapsed = end - self.started_at
        percent = self.percent()
        if 0 < percent < 100 and self.elapsed > 1:
            self.eta = self.elapsed * (100.0 - percent) / percent
        elif percent >= 100:
            self.eta = 0.0

    def to_dict(self) -> dict:
        with self.lock:
            self.refresh_clock()
            return {
                "task_id": self.task_id,
                "file_id": self.file_id,
                "filename": self.filename,
                "status": self.status,
                "stage": self.stage,
                "stage_label": STAGE_LABELS.get(self.stage, self.stage),
                "message": self.message,
                "error": self.error,
                "batch_done": self.batch_done,
                "batch_total": self.batch_total,
                "paragraph_done": self.paragraph_done,
                "paragraph_total": self.paragraph_total,
                "page_current": min(self.paragraph_done, self.page_total) if self.page_total else 0,
                "page_total": self.page_total,
                "percent": self.percent(),
                "elapsed": round(self.elapsed, 1),
                "eta": round(self.eta, 1) if self.eta is not None and self.eta >= 0 else -1,
                "logs": list(self.logs),
                "files": list(self.files),
                "stats": dict(self.stats),
            }

    def history_entry(self) -> dict:
        return {
            "task_id": self.task_id,
            "file_id": self.file_id,
            "filename": self.filename,
            "status": self.status,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at)),
            "paragraphs": self.paragraph_total,
            "elapsed": round(self.elapsed, 1),
            "stage": self.stage,
        }


class TaskManager:
    """线程安全的任务注册表。"""

    def __init__(self, max_tasks: int = constants.MAX_TASKS_KEPT):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self.max_tasks = max_tasks

    def create(self, file_id: str, filename: str, options: dict | None = None) -> Task:
        task_id = "t-" + uuid.uuid4().hex[:10]
        task = Task(task_id=task_id, file_id=file_id, filename=filename, options=options or {})
        with self._lock:
            self._tasks[task_id] = task
            self._evict_locked()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def remove(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

    def all(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def _evict_locked(self) -> None:
        """超出上限时移除最旧的已结束任务（不删产物文件）。"""
        if len(self._tasks) <= self.max_tasks:
            return
        finished = [
            task for task in self._tasks.values()
            if task.status in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)
        ]
        finished.sort(key=lambda task: task.created_at)
        for task in finished[: max(0, len(self._tasks) - self.max_tasks)]:
            self._tasks.pop(task.task_id, None)


# 全局单例
task_manager = TaskManager()
