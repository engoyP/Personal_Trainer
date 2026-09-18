"""程序执行链路追踪：把「路由派发 → 模型调用 → 工具/节点流转」等事件串成一条流水线，
供前端类终端面板滚动展示。

设计取舍：
  - **内存总线 + 轮询**（不用 WebSocket）：LLM 调用在 FastAPI 同步线程里执行，
    跨线程广播要处理事件循环线程安全，容易踩坑。改用「前端 1s 轮询增量」，
    简单可靠，对「看执行链路」这个诉求 1s 延迟完全够用。
  - **不落盘**：只保留最近 N 条事件（环形缓冲），重启即清空。
  - **追踪失败绝不影响主流程**：emit 全程 try/except 包死。
"""
from __future__ import annotations

import threading
import time

_MAX_EVENTS = 500

_BUS = None


class TraceBus:
    def __init__(self):
        self._events: list[dict] = []
        self._counter = 0
        self._lock = threading.Lock()

    def emit(self, level: str, label: str, detail: str = "") -> None:
        with self._lock:
            self._counter += 1
            self._events.append({
                "id": self._counter,
                "ts": time.strftime("%H:%M:%S"),
                "level": level,
                "label": label,
                "detail": detail,
            })
            if len(self._events) > _MAX_EVENTS:
                self._events = self._events[-_MAX_EVENTS:]

    @property
    def counter(self) -> int:
        with self._lock:
            return self._counter

    def since(self, since_id: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["id"] > since_id]

    def tail(self, n: int = 200) -> list[dict]:
        with self._lock:
            return self._events[-n:]


def get_bus() -> TraceBus:
    global _BUS
    if _BUS is None:
        _BUS = TraceBus()
    return _BUS


def emit(level: str, label: str, detail: str = "") -> None:
    """埋点入口。level: route / llm / node / tool / semantic / info / error"""
    try:
        get_bus().emit(level, label, detail)
    except Exception:
        pass


def node(name: str):
    """装饰器：给图节点加「进入 / 完成（耗时）/ 失败」追踪。支持同步和 async 节点。"""
    import asyncio

    def deco(fn):
        if asyncio.iscoroutinefunction(fn):
            async def awrapper(*args, **kwargs):
                emit("node", f"▸ {name}", "开始")
                t0 = time.time()
                try:
                    result = await fn(*args, **kwargs)
                    emit("node", f"✓ {name}", f"{time.time() - t0:.2f}s")
                    return result
                except Exception as e:  # noqa: BLE001
                    emit("error", f"✗ {name}", f"{type(e).__name__}: {e}")
                    raise
            awrapper.__name__ = getattr(fn, "__name__", name)
            return awrapper

        def wrapper(*args, **kwargs):
            emit("node", f"▸ {name}", "开始")
            t0 = time.time()
            try:
                result = fn(*args, **kwargs)
                emit("node", f"✓ {name}", f"{time.time() - t0:.2f}s")
                return result
            except Exception as e:  # noqa: BLE001
                emit("error", f"✗ {name}", f"{type(e).__name__}: {e}")
                raise
        wrapper.__name__ = getattr(fn, "__name__", name)
        return wrapper
    return deco
