"""응답 시간 기록 — 외부 모니터링 도구 없이 '요즘 느린 화면이 있나'를 보는 최소한의 장치.

최근 MAX 개 요청의 (경로 패턴, 걸린 ms, 상태 코드)를 메모리에만 둔다. 파드가 재시작하면 비워진다.
경로는 '/comic/123' 이 아니라 라우트 패턴 '/comic/{product_no}' 로 묶는다 — 작품마다 따로 세면 의미가 없으니까.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

MAX = 2000
SKIP = ("/static", "/healthz", "/readyz")  # 프로브·정적 파일은 빼야 실제 화면 속도가 보인다


@dataclass
class RouteStats:
    route: str
    count: int
    p50: float
    p95: float
    max: float
    errors: int  # 5xx


class RequestLog:
    def __init__(self) -> None:
        self._items: deque[tuple[str, float, int]] = deque(maxlen=MAX)
        self._lock = threading.Lock()
        self.started = time.time()

    def add(self, route: str, ms: float, status: int) -> None:
        if route.startswith(SKIP):
            return
        with self._lock:
            self._items.append((route, ms, status))

    def summary(self) -> list[RouteStats]:
        with self._lock:
            items = list(self._items)
        by_route: dict[str, list[tuple[float, int]]] = {}
        for route, ms, status in items:
            by_route.setdefault(route, []).append((ms, status))
        stats = []
        for route, rows in by_route.items():
            times = sorted(ms for ms, _ in rows)
            stats.append(
                RouteStats(
                    route=route,
                    count=len(times),
                    p50=_pct(times, 0.5),
                    p95=_pct(times, 0.95),
                    max=times[-1],
                    errors=sum(1 for _, status in rows if status >= 500),
                )
            )
        return sorted(stats, key=lambda s: -s.p95)

    def __len__(self) -> int:
        return len(self._items)


def _pct(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]
