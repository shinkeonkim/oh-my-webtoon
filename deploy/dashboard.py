"""Grafana 대시보드 JSON 을 만든다:  uv run python deploy/dashboard.py > deploy/grafana-dashboard.json

JSON 을 손으로 고치면 패널 id·좌표가 금방 엉킨다. 그래서 패널 목록만 여기 적고 JSON 은 생성한다.
kustomization.yaml 의 configMapGenerator 가 이 JSON 을 grafana_dashboard: "1" 라벨 ConfigMap 으로 만들고,
oh-my-homelab 의 Grafana 사이드카가 모든 네임스페이스에서 그 라벨을 찾아 대시보드로 등록한다.
메트릭 이름은 src/my_webtoon/web/prom.py.
"""

from __future__ import annotations

import json

NS = 'namespace="my-webtoon"'
PROM = {"type": "prometheus", "uid": "${datasource}"}
LOKI = {"type": "loki", "uid": "${loki}"}

panels: list[dict] = []
_y = 0


def row(title: str) -> None:
    global _y
    panels.append(
        {
            "type": "row",
            "title": title,
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y},
            "panels": [],
        }
    )
    _y += 1


def place(items: list[dict], h: int) -> None:
    """한 줄에 가로로 나란히. 너비는 24 를 나눠 갖는다 (w 를 준 패널은 그 값)."""
    global _y
    free = 24 - sum(p.get("w", 0) for p in items)
    auto = [p for p in items if "w" not in p]
    x = 0
    for p in items:
        w = p.pop("w", None) or free // len(auto)
        p["gridPos"] = {"h": h, "w": w, "x": x, "y": _y}
        x += w
        panels.append(p)
    _y += h


def target(expr: str, legend: str = "", instant: bool = False) -> dict:
    return {
        "datasource": PROM,
        "expr": expr,
        "legendFormat": legend,
        "refId": "A",
        "instant": instant,
    }


def stat(title: str, expr: str, unit: str = "short", thresholds=None, desc: str = "", **kw) -> dict:
    steps = thresholds or [{"color": "green", "value": None}]
    return {
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "targets": [target(expr, instant=True)],
        "fieldConfig": {
            "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}, **kw},
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "background" if thresholds else "value",
            "graphMode": "none",
        },
    }


def ts(
    title: str, targets: list[dict], unit: str = "short", desc: str = "", stack: bool = False, **kw
) -> dict:
    for i, t in enumerate(targets):
        t["refId"] = chr(65 + i)
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "fillOpacity": 15,
                    "lineWidth": 2,
                    "stacking": {"mode": "normal" if stack else "none"},
                    "showPoints": "never",
                },
                **kw,
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


HOURS = [
    {"color": "green", "value": None},
    {"color": "orange", "value": 6 * 3600},
    {"color": "red", "value": 12 * 3600},
]
REQ = f"mywebtoon_http_requests_total{{{NS}}}"

row("한눈에")
place(
    [
        stat(
            "파드",
            f'sum(up{{{NS}, job="my-webtoon"}})',
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            desc="/metrics 를 긁을 수 있는 파드 수. 배포 중엔 잠깐 2",
        ),
        stat("작품", f"max(sum by (pod) (mywebtoon_comics{{{NS}}}))"),
        stat("무료 회차", f"max(mywebtoon_free_episodes{{{NS}}})"),
        stat("평가", f"max(sum by (pod) (mywebtoon_reviews{{{NS}}}))"),
        stat("수집 대기", f'max(mywebtoon_crawl_queue{{{NS}, state="pending"}})'),
        stat(
            "마지막 수집 후",
            f"time() - max(mywebtoon_crawl_last_finished_timestamp_seconds{{{NS}}})",
            "s",
            HOURS,
            desc="3시간마다 돈다. 12시간 넘으면 알림",
        ),
        stat(
            "마지막 백업 후",
            f"time() - max(mywebtoon_backup_last_timestamp_seconds{{{NS}}})",
            "s",
            [
                {"color": "green", "value": None},
                {"color": "orange", "value": 26 * 3600},
                {"color": "red", "value": 36 * 3600},
            ],
            desc="하루 한 번. 36시간 넘으면 알림",
        ),
        stat(
            "백업 검사",
            f"min(mywebtoon_backup_last_ok{{{NS}}})",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            mappings=[{"type": "value", "options": {"0": {"text": "실패"}, "1": {"text": "통과"}}}],
        ),
    ],
    4,
)

row("웹 요청")
place(
    [
        ts(
            "요청 수 (라우트별, 분당)",
            [target(f"sum by (route) (rate({REQ}[5m])) * 60", "{{route}}")],
            "short",
            stack=True,
        ),
        ts(
            "응답 시간 p50 / p95",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(mywebtoon_http_request_duration_seconds_bucket{{{NS}}}[5m])))",
                    "p95",
                ),
                target(
                    f"histogram_quantile(0.5, sum by (le) (rate(mywebtoon_http_request_duration_seconds_bucket{{{NS}}}[5m])))",
                    "p50",
                ),
            ],
            "s",
        ),
        ts(
            "느린 라우트 p95",
            [
                target(
                    f"topk(5, histogram_quantile(0.95, sum by (le, route) (rate(mywebtoon_http_request_duration_seconds_bucket{{{NS}}}[15m]))))",
                    "{{route}}",
                )
            ],
            "s",
        ),
        ts(
            "상태 코드 (분당)",
            [target(f"sum by (status) (rate({REQ}[5m])) * 60", "{{status}}")],
            "short",
            stack=True,
        ),
    ],
    8,
)

row("수집")
place(
    [
        ts(
            "수집 큐",
            [target(f"max by (state) (mywebtoon_crawl_queue{{{NS}}})", "{{state}}")],
            "short",
        ),
        ts(
            "수집한 작품",
            [target(f"max by (status) (mywebtoon_comics{{{NS}}})", "{{status}}")],
            "short",
            stack=True,
        ),
        ts(
            "마지막 수집 결과",
            [target(f"max by (result) (mywebtoon_crawl_last_pages{{{NS}}})", "{{result}}")],
            "short",
            desc="한 번 돌 때 가져온/새로 발견한/실패/건너뛴 페이지 수",
        ),
        ts(
            "지난 24시간 변경 (소식)",
            [target(f"max by (field) (mywebtoon_comic_changes_24h{{{NS}}})", "{{field}}")],
            "short",
            stack=True,
        ),
    ],
    8,
)

row("자원")
place(
    [
        ts(
            "메모리 (RSS)",
            [target(f'process_resident_memory_bytes{{{NS}, job="my-webtoon"}}', "{{pod}}")],
            "bytes",
            desc="limit 1Gi",
        ),
        ts(
            "CPU",
            [target(f'rate(process_cpu_seconds_total{{{NS}, job="my-webtoon"}}[5m])', "{{pod}}")],
            "percentunit",
        ),
        ts("DB 크기", [target(f"max(mywebtoon_db_size_bytes{{{NS}}})", "my-webtoon.db")], "bytes"),
        ts(
            "수집 중 / 준비됨",
            [
                target(f"max(mywebtoon_crawl_running{{{NS}}})", "수집 중"),
                target(f"min(mywebtoon_ready{{{NS}}})", "준비됨"),
            ],
            "short",
            max=1,
            min=0,
        ),
    ],
    7,
)

row("로그")
place(
    [
        {
            "type": "logs",
            "title": "경고·에러 로그",
            "datasource": LOKI,
            "targets": [
                {
                    "datasource": LOKI,
                    "expr": '{namespace="my-webtoon"} |~ "(?i)warn|error|exception|traceback|실패"',
                    "refId": "A",
                }
            ],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"},
        }
    ],
    9,
)

dashboard = {
    "uid": "my-webtoon",
    "title": "my-webtoon",
    "tags": ["my-webtoon"],
    "timezone": "Asia/Seoul",
    "schemaVersion": 39,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "templating": {
        "list": [
            {
                "name": "datasource",
                "label": "Prometheus",
                "type": "datasource",
                "query": "prometheus",
                "current": {"text": "Prometheus", "value": "prometheus"},
            },
            {
                "name": "loki",
                "label": "Loki",
                "type": "datasource",
                "query": "loki",
                "current": {"text": "Loki", "value": "Loki"},
            },
        ]
    },
    "links": [
        {
            "title": "my-webtoon",
            "url": "https://my-webtoon.shinkeonkim.com/crawl",
            "type": "link",
            "targetBlank": True,
        }
    ],
    "panels": [{**p, "id": i + 1} for i, p in enumerate(panels)],
}

if __name__ == "__main__":
    print(json.dumps(dashboard, ensure_ascii=False, indent=1))
