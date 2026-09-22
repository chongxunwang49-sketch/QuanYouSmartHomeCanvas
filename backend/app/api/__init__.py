"""HTTP 接口层。"""

from .tasks import TaskManager, get_task_manager

__all__ = ["TaskManager", "get_task_manager"]
