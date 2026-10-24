"""설정은 전부 환경변수로 받는다 (12-factor). 로컬은 기본값으로 바로 돌아간다."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_path: Path
    # 비어 있으면 로그인 없이 쓴다 (로컬 개발용). 외부 공개 배포에서는 반드시 채운다.
    app_password: str | None
    session_secret: str

    # 백그라운드 크롤러
    crawl_enabled: bool
    crawl_interval_hours: float  # 몇 시간마다 한 번 돌지
    crawl_batch_size: int  # 한 번 돌 때 상세 페이지를 최대 몇 개 가져올지
    crawl_delay_seconds: float  # 요청 사이 간격 (네이버에 부담을 주지 않도록)
    recrawl_after_days: int  # 이 기간이 지난 작품은 다시 수집 (무료 회차 수가 바뀌므로)
    user_agent: str
    # 하루 한 번 DB 사본을 둘 디렉터리 (다른 디스크 권장). 비어 있으면 자동 백업을 안 한다.
    backup_dir: Path | None


def load_settings() -> Settings:
    return Settings(
        database_path=Path(os.environ.get("DATABASE_PATH", "data/my-webtoon.db")),
        app_password=os.environ.get("APP_PASSWORD") or None,
        session_secret=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
        crawl_enabled=_env_bool("CRAWL_ENABLED", False),
        crawl_interval_hours=float(os.environ.get("CRAWL_INTERVAL_HOURS", "6")),
        crawl_batch_size=int(os.environ.get("CRAWL_BATCH_SIZE", "300")),
        crawl_delay_seconds=float(os.environ.get("CRAWL_DELAY_SECONDS", "1.5")),
        recrawl_after_days=int(os.environ.get("RECRAWL_AFTER_DAYS", "7")),
        user_agent=os.environ.get(
            "CRAWL_USER_AGENT",
            "Mozilla/5.0 (compatible; my-webtoon-personal-recommender/0.1; "
            "+https://my-webtoon.shinkeonkim.com)",
        ),
        backup_dir=Path(os.environ["BACKUP_DIR"]) if os.environ.get("BACKUP_DIR") else None,
    )
