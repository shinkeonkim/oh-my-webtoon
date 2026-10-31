"""사용 가이드(/guide) 스크린샷을 다시 찍는다 — 화면을 바꾼 뒤 가이드 그림을 맞출 때.

    uv run --with websockets python scripts/guide_screenshots.py [--base https://my-webtoon.shinkeonkim.com]

- 데모 계정(/demo)으로 찍는다. 가이드는 공개 화면이라 진짜 계정의 평가가 찍히면 안 된다.
  내 브라우저가 아니라 임시 프로필의 헤드리스 Chrome 이라 내 로그인도 건드리지 않는다.
- 화면마다 라이트·다크 두 장 → src/my_webtoon/web/static/guide/<이름>-light.webp / -dark.webp
  가이드는 보는 사람의 테마에 맞는 쪽을 보여 준다 (style.css .shot).
- 평가를 저장하는 버튼은 누르지 않는다 (데모 계정 평가는 매일 밤 되돌아가지만 그래도).
- Chrome DevTools Protocol 을 websockets 로 직접 쓴다 (Playwright 드라이버 없이 설치된 Chrome 만으로).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT = Path(__file__).resolve().parent.parent / "src/my_webtoon/web/static/guide"
PORT = 9339
DESKTOP = (1280, 820)
MOBILE = (390, 844)
QUALITY = 78
# 데모 안내 띠는 가이드 그림에선 군더더기라 숨긴다
HIDE_CSS = ".alert--info { display: none !important; }"


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1)
        self.events: list[dict] = []

    async def send(self, method: str, **params):
        msg_id = next(self.ids)
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    async def js(self, expr: str):
        res = await self.send(
            "Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in res:
            raise RuntimeError(res["exceptionDetails"])
        return res["result"].get("value")

    async def wait_for(self, expr: str, timeout: float = 10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await self.js(
                f"!!({expr})"
            ):  # DOM 요소는 값으로 넘어오면 {} 라서 불리언으로 바꿔 받는다
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(expr)

    async def goto(self, url: str):
        await self.send("Page.navigate", url=url)
        await asyncio.sleep(0.3)
        await self.wait_for("document.readyState === 'complete'")
        await self.js(f"""(() => {{ const s = document.createElement('style'); s.textContent = {json.dumps(HIDE_CSS)};
                         document.head.appendChild(s); }})()""")
        # 표지 그림이 다 뜰 때까지 (lazy 는 화면 안쪽만)
        await self.wait_for(
            "[...document.images].filter(i => i.getBoundingClientRect().top < innerHeight)"
            ".every(i => i.complete)",
            timeout=15,
        )
        await asyncio.sleep(0.4)

    async def size(self, width: int, height: int, mobile: bool = False):
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            width=width, height=height, deviceScaleFactor=2, mobile=mobile,
        )  # fmt: skip

    async def theme(self, scheme: str):
        await self.send(
            "Emulation.setEmulatedMedia",
            features=[{"name": "prefers-color-scheme", "value": scheme}],
        )

    async def rect(self, selectors: list[str], pad: int = 12) -> dict:
        """여러 요소를 감싸는 사각형 (페이지 좌표). 첫 요소를 화면 안으로 스크롤한다."""
        return await self.js(f"""(() => {{
            const els = {json.dumps(selectors)}.map(s => document.querySelector(s)).filter(Boolean);
            if (!els.length) throw new Error('없음: ' + {json.dumps(selectors)});
            const rs = els.map(e => e.getBoundingClientRect());
            const x0 = Math.min(...rs.map(r => r.left)), y0 = Math.min(...rs.map(r => r.top));
            const x1 = Math.max(...rs.map(r => r.right)), y1 = Math.max(...rs.map(r => r.bottom));
            const p = {pad};
            return {{x: Math.max(0, x0 - p + scrollX), y: Math.max(0, y0 - p + scrollY),
                     width: Math.min(innerWidth, x1 + p) - Math.max(0, x0 - p), height: y1 - y0 + 2 * p, scale: 1}};
        }})()""")

    async def shot(self, path: Path, clip: dict | None = None):
        params = {"format": "webp", "quality": QUALITY, "captureBeyondViewport": clip is not None}
        if clip:
            params["clip"] = clip
        data = await self.send("Page.captureScreenshot", **params)
        path.write_bytes(base64.b64decode(data["data"]))
        print(f"  {path.name:28} {path.stat().st_size // 1024:4d} KB")


async def capture(page: Page, base: str, scheme: str):
    out = lambda name: OUT / f"{name}-{scheme}.webp"  # noqa: E731
    await page.theme(scheme)
    await page.size(*DESKTOP)

    # 추천 화면 전체 (위쪽)
    await page.goto(f"{base}/")
    await page.js("scrollTo(0, 0)")
    await page.shot(out("recs"))
    # 추천 카드 한 장
    await page.shot(out("card"), await page.rect([".grid .card"]))
    first = await page.js(
        "document.querySelector('.grid .card a.card__cover').getAttribute('href')"
    )

    # 위 메뉴 검색 자동완성
    await page.js("""(() => { const i = document.querySelector('.search__input'); i.focus();
                     i.value = '회귀'; i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    await page.wait_for("document.querySelector('#search-suggest .search__item img')")
    # 열림 표시는 '입력칸에 포커스가 있을 때'만 붙는데, 바로 전 캡처가 포커스를 가져갈 수 있어 직접 연다
    await page.js("document.getElementById('search-suggest').classList.add('is-open')")
    await asyncio.sleep(0.8)
    await page.shot(out("search"), await page.rect([".search", "#search-suggest"], pad=16))
    await page.js("document.querySelector('.search__input').blur(); document.body.click()")

    # 내 메뉴 (테마 고르기)
    await page.js("document.querySelector('details.usermenu').open = true")
    await asyncio.sleep(0.2)
    await page.shot(
        out("usermenu"), await page.rect([".usermenu__button", ".usermenu__panel"], pad=16)
    )
    await page.js("document.querySelector('details.usermenu').open = false")

    # 작품 상세 (추천도 상자 · 평가 위젯)
    await page.goto(f"{base}{first}")
    await page.shot(out("detail"), await page.rect([".detail"]))

    # 빠른 평가 한 장
    await page.goto(f"{base}/quick")
    await page.shot(out("quick"), await page.rect([".quick__card"]))

    # 평가(찾아서 평가) 화면 위쪽
    await page.goto(f"{base}/rate")
    await page.shot(
        out("rate"), await page.rect([".page-header", ".toolbar", ".grid .card"], pad=16)
    )

    # 소식
    await page.goto(f"{base}/news")
    await page.js("scrollTo(0, 0)")
    await page.shot(out("news"))

    # 내 취향
    await page.goto(f"{base}/me")
    taste = await page.js("location.pathname + '/taste'")
    await page.goto(f"{base}{taste}")
    await page.shot(out("taste"))

    # 휴대폰
    await page.size(*MOBILE, mobile=True)
    await page.goto(f"{base}/")
    await page.js("scrollTo(0, 0)")
    await page.shot(out("mobile"))


async def main(base: str):
    OUT.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="guide-chrome-"))
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={profile}", "--hide-scrollbars", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                break
            except OSError:
                time.sleep(0.2)
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=50 * 2**20) as ws:
            page = Page(ws)
            await page.send("Page.enable")
            await page.send(
                "Emulation.setFocusEmulationEnabled", enabled=True
            )  # 헤드리스도 '포커스 있음'으로
            await page.size(*DESKTOP)
            await page.goto(f"{base}/demo")  # 데모 계정으로 로그인 (쿠키는 이 임시 프로필에만)
            for scheme in ("light", "dark"):
                print(scheme)
                await capture(page, base, scheme)
    finally:
        chrome.terminate()
        chrome.wait(10)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="https://my-webtoon.shinkeonkim.com")
    asyncio.run(main(ap.parse_args().base.rstrip("/")))
