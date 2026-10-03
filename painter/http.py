"""面向博物馆接口的克制型 HTTP 访问。

collectionapi.metmuseum.org 和 www.metmuseum.org 都在 Akamai 机器人防护之后：
普通 ``requests``/``curl`` 客户端因为 TLS 指纹会被识别，请求若干次后返回 403/429。
``curl_cffi`` 的浏览器指纹模拟能被这两个站点接受，所以所有请求都走同一个限速客户端，
并在被限流时重试/退避。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_requests

log = logging.getLogger(__name__)

# 这些状态码意味着「放慢速度 / 稍后重试」，而不是「资源不存在」。
RETRY_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})

DEFAULT_HEADERS = {
    "accept-language": "en-US,en;q=0.9",
}


class HttpError(RuntimeError):
    """用尽重试次数后仍然失败的请求。"""

    def __init__(self, url: str, status: int | None = None, detail: str = "") -> None:
        message = f"HTTP {status} for {url}" if status is not None else f"request failed for {url}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.url = url
        self.status = status


class RateLimiter:
    """线程安全的令牌桶；全进程共用一个实例来限制总请求速率。"""

    def __init__(self, rate_per_sec: float, burst: int = 1) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = float(rate_per_sec)
        self.capacity = float(max(1, burst))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


class HttpClient:
    """共享客户端。每个线程一个 ``Session``，全局一个限速器。"""

    def __init__(
        self,
        *,
        rate_per_sec: float = 4.0,
        timeout: float = 60.0,
        max_attempts: int = 5,
        impersonate: str = "chrome",
    ) -> None:
        self.limiter = RateLimiter(rate_per_sec)
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.impersonate = impersonate
        self._tls = threading.local()

    def _session(self) -> cffi_requests.Session:
        session = getattr(self._tls, "session", None)
        if session is None:
            session = cffi_requests.Session(impersonate=self.impersonate)
            session.headers.update(DEFAULT_HEADERS)
            self._tls.session = session
        return session

    def _sleep_backoff(self, attempt: int, status: int | None = None) -> None:
        delay = min(30.0, 2.0 ** attempt) + random.uniform(0.0, 0.75)
        if status == 403:
            # 被机器人防护硬拒绝（403）比 429 需要更长的冷却时间。
            delay *= 3
        log.debug("retrying in %.1fs (attempt %d, status %s)", delay, attempt + 1, status)
        time.sleep(delay)

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> cffi_requests.Response:
        """带限速和重试的 GET；返回任何状态码小于 400 的响应。"""
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self.limiter.acquire()
            try:
                response = self._session().get(url, headers=headers, timeout=self.timeout)
            except Exception as exc:  # 连接被重置、超时等
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    self._sleep_backoff(attempt)
                continue
            if response.status_code in RETRY_STATUS:
                last_error = HttpError(url, response.status_code)
                if attempt + 1 < self.max_attempts:
                    self._sleep_backoff(attempt, response.status_code)
                continue
            if response.status_code >= 400:
                raise HttpError(url, response.status_code)
            return response
        if isinstance(last_error, HttpError):
            raise last_error
        raise HttpError(url, None, str(last_error))

    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        response = self.get(url, headers={"accept": "application/json", **(headers or {})})
        return response.json()

    def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        response = self.get(url, headers={"accept": "text/html,application/xhtml+xml", **(headers or {})})
        return response.text

    def download(self, url: str, dest: Path) -> tuple[int, str]:
        """把 ``url`` 流式写入 ``dest``（原子替换）。返回 (字节数, content_type)。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self.limiter.acquire()
            try:
                response = self._session().get(url, timeout=self.timeout, stream=True)
                try:
                    if response.status_code in RETRY_STATUS:
                        last_error = HttpError(url, response.status_code)
                        if attempt + 1 < self.max_attempts:
                            self._sleep_backoff(attempt, response.status_code)
                        continue
                    if response.status_code >= 400:
                        raise HttpError(url, response.status_code)
                    content_type = response.headers.get("content-type", "")
                    written = 0
                    with tmp.open("wb") as handle:
                        for chunk in response.iter_content(1 << 16):
                            if chunk:
                                handle.write(chunk)
                                written += len(chunk)
                finally:
                    response.close()
            except HttpError:
                raise
            except Exception as exc:
                last_error = exc
                tmp.unlink(missing_ok=True)
                if attempt + 1 < self.max_attempts:
                    self._sleep_backoff(attempt)
                    continue
                break
            tmp.replace(dest)
            return written, content_type
        if isinstance(last_error, HttpError):
            raise last_error
        raise HttpError(url, None, str(last_error))
