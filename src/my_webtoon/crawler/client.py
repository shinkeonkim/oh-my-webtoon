"""정중한(polite) HTTP 클라이언트.

- 요청 사이에 항상 `delay_seconds` 이상 쉰다 (기본 1.5초).
- robots.txt 가 막아 둔 경로는 요청 자체를 하지 않는다.
  (네이버 시리즈는 /comic/categoryProductList.series 같은 목록 페이지를 막아 두었다.
   그래서 이 프로젝트는 목록 대신 TOP100/무료관 + 상세 페이지 링크를 따라가는 방식을 쓴다.)
- 5xx/네트워크 오류는 지수 백오프로 몇 번 다시 시도한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://series.naver.com"


@dataclass
class Page:
    url: str  # 리다이렉트를 따라간 최종 URL
    status: int
    text: str


class RobotsBlocked(Exception):
    pass


class PoliteClient:
    def __init__(self, user_agent: str, delay_seconds: float = 1.5, max_retries: int = 3):
        self.delay_seconds = delay_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "ko-KR,ko;q=0.9"},
            follow_redirects=True,
            timeout=20,
        )
        self._robots = self._load_robots(user_agent)

    def _load_robots(self, user_agent: str) -> RobotFileParser:
        robots = RobotFileParser()
        try:
            resp = self._client.get(f"{BASE_URL}/robots.txt")
            robots.parse(resp.text.splitlines())
        except httpx.HTTPError:
            log.warning("robots.txt 를 못 읽었다 — 보수적으로 목록 페이지는 계속 피한다")
            robots.parse(["User-agent: *", "Disallow: /comic/categoryProductList.series"])
        self._user_agent = user_agent
        return robots

    def allowed(self, url: str) -> bool:
        return self._robots.can_fetch(self._user_agent, url)

    def _wait_turn(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def get(self, path_or_url: str) -> Page:
        url = path_or_url if urlparse(path_or_url).scheme else BASE_URL + path_or_url
        if not self.allowed(url):
            raise RobotsBlocked(url)

        for attempt in range(1, self.max_retries + 1):
            self._wait_turn()
            try:
                resp = self._client.get(url)
            except httpx.HTTPError as exc:
                if attempt == self.max_retries:
                    raise
                log.info("요청 실패 %s (%s) — %d번째 재시도", url, exc, attempt)
            else:
                if resp.status_code < 500 or attempt == self.max_retries:
                    return Page(str(resp.url), resp.status_code, resp.text)
                log.info("%s 가 %d 를 돌려줬다 — %d번째 재시도", url, resp.status_code, attempt)
            time.sleep(self.delay_seconds * 2**attempt)
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
