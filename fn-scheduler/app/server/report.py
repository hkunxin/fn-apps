# -*- coding: utf-8 -*-
"""把任务执行结果上报给 HA 的 `mnn_home`（spec-040 R17–R30）。

为什么要有队列，而不是执行完直接 POST：

HA 重启、路由器抖动、令牌过期都会让上报失败，而这几种情况恰恰是这个功能的**主场景**——
用户就是因为「不在电脑前」才需要看结果。直接 POST 等于把事件丢进日志里。
所以事件先落 SQLite 的 `report_queue`，后台线程按退避重试，HA 恢复后自动补发。

两条容易被改坏的性质：

1. **补发保留原始时间**（R22）：payload 里的 `started_at` / `finished_at` 由执行侧生成，
   队列只负责什么时候发，绝不改成「发出去的时刻」——否则手机上看到的时间全是错的。
2. **上报是旁路**（R29）：任何上报失败都不影响任务本身的执行结果与本地记录。
"""

import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("fn_scheduler.report")

REPORT_PATH = "/api/services/mnn_home/report_job?return_response"

# 这些错误码意味着「再怎么重试也不会成功」，不必占着队列反复打
FATAL_CODES = frozenset({"invalid_job_key", "invalid_status", "invalid_payload"})

MAX_BACKOFF_SECONDS = 600


def _now():
    """本地朴素时间。

    **必须和 scheduler.py 的 `time_now()` 同口径**：库里 `next_attempt_at` 存的是
    `2026-11-11 17:54:01` 这种本地无时区字符串，这里若用 UTC aware 时间做字符串比较，
    `17` 永远大于 `09`，队列里的行永远「还没到期」——上报会静默停摆。
    """
    return datetime.now()


def _iso(moment):
    return moment.replace(microsecond=0).isoformat(sep=" ")


class HaReporter(threading.Thread):
    """后台补发线程。没有配置 HA 时它照样在跑，只是 `flush_once` 直接返回。"""

    def __init__(self, db, settings, poll_seconds: int = 5):
        super().__init__(daemon=True)
        self.db = db
        self.settings = settings
        self.poll_seconds = max(2, int(poll_seconds))
        self.stop_event = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 线程

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.flush_once()
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("上报线程异常：%s", exc)
            self.stop_event.wait(self.poll_seconds)

    # ---------------------------------------------------------------- 发送

    def configured(self) -> bool:
        return bool(self.settings.ha_base_url) and bool(self.settings.ha_token)

    def flush_once(self) -> int:
        """把到期的待上报事件发出去，返回成功条数。"""
        if not self.configured():
            return 0
        sent = 0
        for row in self.db.fetch_due_reports(_now(), limit=20):
            ok, error, fatal = self._post(row["payload"])
            if ok:
                self.db.mark_report_sent(row["id"], row.get("run_id"))
                sent += 1
                continue
            attempts = int(row.get("attempts") or 0) + 1
            dead = fatal or attempts >= self.settings.report_max_attempts
            next_at = _now() + timedelta(seconds=self._backoff(attempts))
            self.db.mark_report_failed(
                row["id"], error, _iso(next_at), row.get("run_id"), dead=dead
            )
            if dead:
                logger.warning("上报放弃（%s 次仍失败）：%s", attempts, error)
        return sent

    def send_now(self, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """设置页的「发一条测试」用：不入队，立即发一次。"""
        if not self.configured():
            return False, "还没配置 HA 地址或令牌"
        ok, error, _fatal = self._post(json.dumps(payload, ensure_ascii=False))
        return ok, error

    def _backoff(self, attempts: int) -> int:
        base = max(5, int(self.settings.report_retry_seconds))
        return min(base * (2 ** min(attempts - 1, 6)), MAX_BACKOFF_SECONDS)

    def _post(self, payload_json: str) -> Tuple[bool, str, bool]:
        """返回 (成功, 错误说明, 是否不可重试)。"""
        url = self.settings.ha_base_url.rstrip("/") + REPORT_PATH
        body = payload_json.encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.settings.ha_token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code in (401, 403):
                return False, f"HA 拒绝访问（HTTP {exc.code}），检查令牌", False
            return False, f"HTTP {exc.code}: {detail or exc.reason}", False
        except urllib.error.URLError as exc:
            return False, f"连不上 HA：{exc.reason}", False
        except Exception as exc:  # pylint: disable=broad-except
            return False, f"上报异常：{exc!r}", False

        try:
            data = json.loads(raw)
        except ValueError:
            return False, f"HA 返回的不是 JSON：{raw[:200]}", False

        if isinstance(data, dict) and data.get("ok") is False:
            code = str(data.get("code") or "")
            message = str(data.get("message") or "")
            return False, f"{code}: {message}" if code else (message or "HA 返回 ok=false"), code in FATAL_CODES
        return True, "", False
