"""备课导师 · 异步评估任务（内存任务表 + 后台线程）
MVP 单机单进程，无需 Celery/Redis。任务状态落库 eval_tasks（可恢复展示）。
"""
import threading
import uuid

from . import db, pipeline

_LOCK = threading.Lock()
_ACTIVE = set()          # 运行中的 task_id 集合（仅内存追踪，状态以 DB 为准）


def create_task(topic: str, text: str) -> str:
    """创建评估任务并立即在后台线程执行，返回 task_id"""
    task_id = uuid.uuid4().hex[:12]
    db.create_eval_task(task_id, topic)
    with _LOCK:
        _ACTIVE.add(task_id)
    t = threading.Thread(target=_run, args=(task_id, topic, text), daemon=True)
    t.start()
    return task_id


def _run(task_id: str, topic: str, text: str):
    """后台执行六层流水线，每阶段进度写回 eval_tasks"""
    db.update_eval_task(task_id, status="running", stage="P1 解析教案")
    try:
        report = pipeline.run_eval(topic, text, task_id=task_id, on_progress=_progress(task_id))
        db.update_eval_task(task_id, status="done", progress=100, stage="完成", result=report)
    except Exception as e:                       # 流水线任何阶段异常 → 任务失败而非 500
        db.update_eval_task(task_id, status="failed", error=str(e))
    finally:
        with _LOCK:
            _ACTIVE.discard(task_id)


def _progress(task_id: str):
    def cb(stage: str, pct: int):
        db.update_eval_task(task_id, stage=stage, progress=pct)
    return cb


def get_task(task_id: str) -> dict | None:
    return db.get_eval_task(task_id)
