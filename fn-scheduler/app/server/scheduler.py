# -*- coding: utf-8 -*-
#
# Copyright (C) 2022 Ing <https://github.com/wjz304>
#
# This is free software, licensed under the MIT License.
# See /LICENSE for more information.
#


import argparse
import collections
import getpass
import json
import logging
import mimetypes
import os
import re
import signal
import socketserver
import sqlite3
import sys
import threading
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typing import Any, Callable, Dict, Iterator, List, Optional, Set, overload
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from subprocess import PIPE, STDOUT, Popen, TimeoutExpired, run

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide flock
    fcntl = None

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-POSIX systems
    grp = None  # type: ignore
    pwd = None  # type: ignore
from urllib.parse import parse_qs, urlsplit, urlunsplit, unquote

# 同目录模块（spec-040）。scheduler.py 是以脚本方式启动的，sys.path[0] 就是 server/，
# 但打包后若有人从别处 import，这里补一条兜底，避免 ImportError 把整个服务打死。
try:
    import rclone as rclone_mod
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rclone as rclone_mod

try:
    import report as report_mod
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import report as report_mod


def _env(name, default=""):
    """Read a TRIM_* / SCHEDULER_* env var with a default."""
    return os.environ.get(name, "").strip() or default


APP_NAME = _env("TRIM_APPNAME", "fn-scheduler")
ROOT_DIR = Path(__file__).resolve().parent

DB_LATEST_VERSION = 5

TASK_TIMEOUT = 900
CONDITION_TIMEOUT = 60
MAX_LOOKAHEAD_MINUTES = 60 * 24 * 366  # one leap year
RESULT_LOG_PREVIEW_LIMIT = 4000
CONDITION_LOG_PREVIEW_LIMIT = 240
RESULT_RETENTION_PER_TASK = 200
EVENT_TYPE_SCRIPT = "script"
EVENT_TYPE_BOOT = "system_boot"
EVENT_TYPE_SHUTDOWN = "system_shutdown"
EVENT_TYPES = {EVENT_TYPE_SCRIPT, EVENT_TYPE_BOOT, EVENT_TYPE_SHUTDOWN}

# spec-040：任务类型。script = 现有行为（bash -c script_body）；rclone = 结构化备份/同步任务。
TASK_KIND_SCRIPT = "script"
TASK_KIND_RCLONE = "rclone"
TASK_KINDS = (TASK_KIND_SCRIPT, TASK_KIND_RCLONE)

# 上报状态（与 rclone.py 的 STATUS_*、mnn_home 的 job 状态保持一致）
REPORT_ALL = "all"
REPORT_FAIL = "fail"
REPORT_OFF = "off"
REPORT_MODES = (REPORT_ALL, REPORT_FAIL, REPORT_OFF)

# 触发方式里带试运行：`run-task` 传 trigger=dry-run 时任务不写目标端
TRIGGER_DRY_RUN = "dry-run"
QUIET_ACCESS_LOG_PATHS = {"/api/tasks", "/api/health", "/api/tasks/version"}
LOG_POLLING_REQUESTS = False


def _detect_default_account():
    for env_key in ("SCHEDULER_DEFAULT_ACCOUNT", "USERNAME", "USER"):
        value = os.environ.get(env_key)
        if value:
            return value
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - fallback only
        return "current_user"


DEFAULT_ACCOUNT_NAME = _detect_default_account()
ALLOWED_ACCOUNT_GIDS = (0, 1000, 1001)
POSIX_ACCOUNT_SUPPORT = os.name == "posix" and pwd is not None and grp is not None


def normalize_base_path(raw):
    base = (raw or "/").strip()
    if not base:
        base = "/"
    if not base.startswith("/"):
        base = f"/{base}"
    if len(base) > 1 and base.endswith("/"):
        base = base.rstrip("/")
    return base or "/"


@overload
def strip_wrapping_quotes(value: None) -> None: ...


@overload
def strip_wrapping_quotes(value: str) -> str: ...


def strip_wrapping_quotes(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {'"', "'"}:
        return trimmed[1:-1]
    return trimmed


def parse_bool_value(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def summarize_log_text(text, limit=CONDITION_LOG_PREVIEW_LIMIT):
    normalized = str(text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return ""
    single_line = normalized.replace("\n", "\\n")
    if limit >= 0 and len(single_line) > limit:
        return f"{single_line[:limit]}..."
    return single_line


def serialize_result_row(row, include_log=True, log_limit=None):
    payload = dict(row)
    log_text = payload.get("log") or ""
    if not isinstance(log_text, str):
        log_text = str(log_text)

    log_size = len(log_text)
    if log_limit is not None and log_limit >= 0:
        log_preview = log_text[:log_limit]
        log_truncated = log_size > log_limit
    else:
        log_preview = log_text
        log_truncated = False

    payload["log_size"] = log_size
    payload["log_preview"] = log_preview
    payload["log_truncated"] = log_truncated

    if include_log:
        payload["log"] = log_text
    else:
        payload.pop("log", None)

    return payload


# ---------------------------------------------------------------------------
# Standard HTTP helpers
# ---------------------------------------------------------------------------

REQUEST_CONTEXT = threading.local()

CONTEXT = None  # SchedulerContext


@contextmanager
def request_context(
    method,
    query="",
    headers=None,
    body=b"",
    handler=None,
):
    """Push per-request context so helpers (json_response, request_body, ...)
    can find the active handler without threading globals."""
    previous = getattr(REQUEST_CONTEXT, "value", None)
    try:
        REQUEST_CONTEXT.value = {
            "method": (method or "GET").upper(),
            "query": query or "",
            "headers": headers or {},
            "body": body or b"",
            "handler": handler,
        }
        yield
    finally:
        if previous is None:
            if hasattr(REQUEST_CONTEXT, "value"):
                del REQUEST_CONTEXT.value
        else:
            REQUEST_CONTEXT.value = previous


def current_request():
    return getattr(REQUEST_CONTEXT, "value", {}) or {}


def header_value(headers, name):
    """Case-insensitive header lookup."""
    if not headers:
        return ""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def normalize_status(status):
    """Normalize a status into (code, "code phrase"); callers may pass an
    HTTPStatus, an int, or a "200 OK" string."""
    if isinstance(status, HTTPStatus):
        return status.value, f"{status.value} {status.phrase}"
    if isinstance(status, int):
        try:
            phrase = HTTPStatus(status).phrase
        except Exception:
            phrase = "OK"
        return status, f"{status} {phrase}"
    text = str(status or "200 OK").strip()
    if not text:
        return 200, "200 OK"
    first, _, rest = text.partition(" ")
    if first.isdigit():
        code = int(first)
        if not rest:
            try:
                rest = HTTPStatus(code).phrase
            except Exception:
                rest = ""
        return code, f"{code} {rest}".strip()
    return 200, "200 OK"


def json_response(payload, status: str | int | HTTPStatus = "200 OK"):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    code, status_text = normalize_status(status)
    handler = current_request().get("handler")
    if handler is not None:
        try:
            handler.send_response(code)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        return
    sys.stdout.write(f"Status: {status_text}\r\n")
    sys.stdout.write("Content-Type: application/json; charset=utf-8\r\n")
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(body)


def request_body():
    """Return parsed request params: JSON/body for POST/PUT/PATCH, otherwise
    query string."""
    request = current_request()
    if request:
        method = request.get("method", "GET").upper()
        body = request.get("body", b"") or b""
        query_string = request.get("query", "") or ""
        content_type = header_value(request.get("headers", {}), "Content-Type")
        if method in {"POST", "PUT", "PATCH"}:
            raw = body.decode("utf-8", "replace") if body else ""
            if "application/json" in content_type:
                return json.loads(raw or "{}")
            parsed = parse_qs(raw, keep_blank_values=True)
            return {key: values[-1] for key, values in parsed.items()}
        parsed = parse_qs(query_string, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    if method in {"POST", "PUT", "PATCH"}:
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
        raw = sys.stdin.buffer.read(length).decode("utf-8", "replace") if length else ""
        if "application/json" in (os.environ.get("CONTENT_TYPE", "") or ""):
            return json.loads(raw or "{}")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}
    parsed = parse_qs(os.environ.get("QUERY_STRING", ""), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _send_error(status, message=None):
    """发送错误响应（模块级辅助）。委托当前 handler 的 send_error，
    未关联 handler 时退化为 CGI 风格输出。"""
    handler = current_request().get("handler")
    if handler is not None:
        handler.send_error(status, message)
        return
    code, _ = normalize_status(status)
    sys.stdout.write(f"Status: {code}\r\n")
    sys.stdout.write(f"Error: {message or ''}\r\n\r\n")
    sys.stdout.flush()


class SchedulerSettings:
    MASKED = "***set***"

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = {
            "task_timeout": TASK_TIMEOUT,
            "condition_timeout": CONDITION_TIMEOUT,
            "result_log_preview_limit": RESULT_LOG_PREVIEW_LIMIT,
            "result_retention_per_task": RESULT_RETENTION_PER_TASK,
            # spec-040
            "rclone_bin": "rclone",
            "backup_root_allowlist": [],
            "ha_base_url": "",
            "ha_token": "",
            "report_mode": REPORT_ALL,
            "report_retry_seconds": 30,
            "report_max_attempts": 20,
        }
        self._load()

    def _sanitize(self, raw):
        data = dict(self._data)

        def _read_int(key, minimum):
            if key not in raw:
                return
            value = int(raw[key])
            if value < minimum:
                raise ValueError(f"{key} must be >= {minimum}")
            data[key] = value

        def _read_str(key, max_len=512):
            if key not in raw:
                return
            value = raw[key]
            value = "" if value is None else str(value).strip()
            if len(value) > max_len:
                raise ValueError(f"{key} is too long (max {max_len})")
            data[key] = value

        _read_int("task_timeout", 0)
        _read_int("condition_timeout", 1)
        _read_int("result_log_preview_limit", 256)
        _read_int("result_retention_per_task", 0)
        _read_int("report_retry_seconds", 5)
        _read_int("report_max_attempts", 1)

        _read_str("rclone_bin")
        if re.search(r"[\s;|&$`'\"<>]", data.get("rclone_bin") or ""):
            raise ValueError("rclone_bin must not contain whitespace or shell metacharacters")

        _read_str("ha_base_url")
        base_url = data.get("ha_base_url") or ""
        if base_url and not re.match(r"^https?://[^\s/]+", base_url):
            raise ValueError("ha_base_url must start with http:// or https://")
        data["ha_base_url"] = base_url.rstrip("/")

        # token 特殊处理：掩码值或空串都表示「保持原值不变」，避免读一次写一次就把令牌抹掉
        if "ha_token" in raw:
            token = "" if raw.get("ha_token") is None else str(raw.get("ha_token")).strip()
            if token and token != self.MASKED:
                data["ha_token"] = token

        if "report_mode" in raw:
            mode = str(raw.get("report_mode") or "").strip().lower()
            if mode not in REPORT_MODES:
                raise ValueError(f"report_mode must be one of {', '.join(REPORT_MODES)}")
            data["report_mode"] = mode

        if "backup_root_allowlist" in raw:
            allow = raw.get("backup_root_allowlist") or []
            if isinstance(allow, str):
                allow = [item for item in allow.splitlines()]
            if not isinstance(allow, (list, tuple)):
                raise ValueError("backup_root_allowlist must be a list of paths")
            cleaned = []
            for item in allow:
                text = str(item or "").strip()
                if not text:
                    continue
                if not text.startswith("/"):
                    raise ValueError("backup_root_allowlist entries must be absolute paths")
                if ".." in text.split("/"):
                    raise ValueError("backup_root_allowlist entries must not contain ..")
                if text.rstrip("/") not in cleaned:
                    cleaned.append(text.rstrip("/") or "/")
            data["backup_root_allowlist"] = cleaned

        return data

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                with self._lock:
                    self._data = self._sanitize(loaded)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Failed to load scheduler settings from %s: %s", self.path, exc
            )

    def _save(self):
        settings_dir = os.path.dirname(self.path)
        if settings_dir:
            os.makedirs(settings_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix="scheduler-settings-", suffix=".json", dir=settings_dir or None
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def to_dict(self, mask_secrets=True):
        with self._lock:
            data = dict(self._data)
        if mask_secrets and data.get("ha_token"):
            # R39/R43：接口只回显「已设置」，永不回显令牌本身
            data["ha_token"] = self.MASKED
        data["ha_token_set"] = bool(self._data.get("ha_token"))
        return data

    def update(self, raw):
        with self._lock:
            self._data = self._sanitize(raw)
            self._save()
            # 必须返回**打码后**的副本：这里回显原始 dict 会把 HA 令牌直接吐给前端（R39）
            return self.to_dict()

    @property
    def task_timeout(self):
        with self._lock:
            return int(self._data["task_timeout"])

    @property
    def condition_timeout(self):
        with self._lock:
            return int(self._data["condition_timeout"])

    @property
    def result_log_preview_limit(self):
        with self._lock:
            return int(self._data["result_log_preview_limit"])

    @property
    def result_retention_per_task(self):
        with self._lock:
            return int(self._data["result_retention_per_task"])

    # ---- spec-040 ----------------------------------------------------

    @property
    def rclone_bin(self):
        with self._lock:
            return str(self._data.get("rclone_bin") or "rclone")

    @property
    def backup_root_allowlist(self):
        with self._lock:
            return list(self._data.get("backup_root_allowlist") or [])

    @property
    def ha_base_url(self):
        with self._lock:
            return str(self._data.get("ha_base_url") or "")

    @property
    def ha_token(self):
        with self._lock:
            return str(self._data.get("ha_token") or "")

    @property
    def report_mode(self):
        with self._lock:
            return str(self._data.get("report_mode") or REPORT_ALL)

    @property
    def report_retry_seconds(self):
        with self._lock:
            return int(self._data.get("report_retry_seconds") or 30)

    @property
    def report_max_attempts(self):
        with self._lock:
            return int(self._data.get("report_max_attempts") or 20)


logger = logging.getLogger("fn_scheduler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# 上报线程（spec-040）。在 main() 里创建，任务执行完只往队列里丢，由它负责发。
REPORTER: Optional["report_mod.HaReporter"] = None


def time_now():
    IS_LOCAL_TIME = True
    if IS_LOCAL_TIME:
        # 返回本地时间（无时区信息，和服务器系统时间一致）
        return datetime.now()
    else:
        # 带时区信息的 UTC 时间
        return datetime.now(timezone.utc)


def isoformat(dt):
    if dt is None:
        return None
    # 直接用本地时间的 ISO 格式
    return dt.replace(microsecond=0).isoformat(sep=" ")


def parse_iso(value):
    if not value:
        return None
    try:
        # 兼容带空格的本地时间字符串
        dt = datetime.fromisoformat(value.replace("T", " "))
        # 如果是带时区的，转为本地无时区
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def list_allowed_accounts():
    """Return distinct account names whose primary or supplemental group is allowed."""

    if not POSIX_ACCOUNT_SUPPORT:
        return [DEFAULT_ACCOUNT_NAME] if DEFAULT_ACCOUNT_NAME else []

    accounts: Set[str] = set()
    try:
        for entry in pwd.getpwall():  # type: ignore[attr-defined]
            if entry.pw_gid in ALLOWED_ACCOUNT_GIDS:
                accounts.add(entry.pw_name)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to enumerate passwd entries: %s", exc)

    for gid in ALLOWED_ACCOUNT_GIDS:
        try:
            group = grp.getgrgid(gid)  # type: ignore[attr-defined]
        except KeyError:
            continue
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to read group %s: %s", gid, exc)
            continue
        for member in group.gr_mem:
            if member:
                accounts.add(member)

    return sorted(accounts)


def ensure_account_allowed(account):
    allowed = list_allowed_accounts()
    if not allowed:
        if POSIX_ACCOUNT_SUPPORT:
            raise ValueError("no allowed accounts found in system groups 0/1000/1001")
        raise ValueError("current system cannot determine default account")
    if not POSIX_ACCOUNT_SUPPORT:
        default_account = allowed[0]
        if account and account != default_account:
            raise ValueError(
                f"Windows environment only supports using account {default_account}"
            )
        return default_account
    if account not in allowed:
        raise ValueError("account must belong to system groups 0/1000/1001")
    return account


def prepare_task_account_context(
    task,
):
    if not POSIX_ACCOUNT_SUPPORT:
        return (None, None)
    account = task.get("account")
    if not account:
        return (None, None)
    try:
        pw_record = pwd.getpwnam(account)  # type: ignore[attr-defined]
    except KeyError as exc:
        raise RuntimeError(
            f"account {account} does not exist, cannot execute task"
        ) from exc

    target_uid = pw_record.pw_uid
    target_gid = pw_record.pw_gid
    current_uid = os.geteuid()  # pyright: ignore[reportAttributeAccessIssue]

    if current_uid == target_uid:
        return (None, pw_record.pw_dir)

    if current_uid != 0:
        raise PermissionError(
            "scheduler service must run as root to switch task execution account"
        )

    supplemental: List[int] = []
    try:
        supplemental = [entry.gr_gid for entry in grp.getgrall() if account in entry.gr_mem]  # type: ignore[attr-defined]
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "failed to get supplemental groups for account %s: %s", account, exc
        )

    groups = sorted(set([target_gid, *supplemental]))

    def _changer():
        os.setgid(target_gid)  # pyright: ignore[reportAttributeAccessIssue]
        if groups:
            os.setgroups(groups)  # pyright: ignore[reportAttributeAccessIssue]
        os.setuid(target_uid)  # pyright: ignore[reportAttributeAccessIssue]

    return (_changer, pw_record.pw_dir)


def build_task_environment(task, trigger_reason, home_dir=None):
    env = os.environ.copy()
    if home_dir:
        env["HOME"] = home_dir
    env.update(
        {
            "SCHEDULER_TASK_ID": str(task["id"]),
            "SCHEDULER_TASK_NAME": task["name"],
            "SCHEDULER_TASK_ACCOUNT": task["account"],
            "SCHEDULER_TRIGGER": trigger_reason,
        }
    )
    return env


###############################################################################
# Cron expression parsing
###############################################################################


class CronExpression:
    """Minimal 5-field cron parser supporting ranges, lists, and steps."""

    FIELD_SPECS = (
        ("minute", 0, 59, 60),
        ("hour", 0, 23, 24),
        ("day", 1, 31, 31),
        ("month", 1, 12, 12),
        ("weekday", 0, 6, 7),
    )

    def __init__(self, expression):
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError("Cron expression must contain 5 fields")
        self.fields: List[List[int]] = []
        self._wildcards: List[bool] = []
        for part, spec in zip(parts, self.FIELD_SPECS):
            expanded, wildcard = self._expand_field(part, spec)
            self.fields.append(expanded)
            self._wildcards.append(wildcard)

    def _expand_field(self, token, spec):
        name, min_value, max_value, span = spec
        values: set[int] = set()
        wildcard = False
        items = token.split(",")
        for raw_item in items:
            original_item = raw_item.strip() or "*"
            item = original_item
            step = 1
            if "/" in original_item:
                base, step_str = original_item.split("/", 1)
                item = base or "*"
                step = int(step_str)
                if step <= 0:
                    raise ValueError(f"Invalid step for {name}")
            expanded = self._expand_range(item, min_value, max_value)
            if not expanded:
                raise ValueError(f"Invalid {name} segment: {item}")
            start_val = expanded[0]
            for value in expanded:
                if (value - start_val) % step == 0:
                    values.add(value)
            wildcard = wildcard or (original_item == "*")
        if not values:
            raise ValueError(f"No values computed for {name}")
        if name == "weekday":
            normalized = set()
            for val in values:
                normalized.add(0 if val == 7 else val)
            values = normalized
        if not all(min_value <= v <= max_value for v in values):
            raise ValueError(f"{name} values out of range")
        full_span = len(values) == span
        return sorted(values), (wildcard or full_span)

    def _expand_range(self, item, min_value, max_value):
        if item == "*":
            return list(range(min_value, max_value + 1))
        if item.isdigit():
            return [int(item)]
        if "-" in item:
            start_str, end_str = item.split("-", 1)
            start = int(start_str)
            end = int(end_str)
            if start > end:
                raise ValueError("Cron range start greater than end")
            return list(range(start, end + 1))
        raise ValueError("Unsupported cron token")

    def next_after(self, moment):
        base = moment.replace(second=0, microsecond=0)
        candidate = base
        for _ in range(MAX_LOOKAHEAD_MINUTES):
            candidate += timedelta(minutes=1)
            if self._matches(candidate):
                return candidate
        raise ValueError("Unable to compute next run within lookahead window")

    def _matches(self, candidate):
        minute, hour = candidate.minute, candidate.hour
        day, month = candidate.day, candidate.month
        weekday = candidate.weekday()
        dom_match = day in self.fields[2]
        dow_match = weekday in self.fields[4]
        dom_wildcard = self._wildcards[2]
        dow_wildcard = self._wildcards[4]

        if dom_wildcard and dow_wildcard:
            calendar_ok = True
        elif dom_wildcard:
            calendar_ok = dow_match
        elif dow_wildcard:
            calendar_ok = dom_match
        else:
            calendar_ok = dom_match or dow_match

        return (
            minute in self.fields[0]
            and hour in self.fields[1]
            and month in self.fields[3]
            and calendar_ok
        )


###############################################################################
# Database layer
###############################################################################


class Database:
    def __init__(self, path, result_retention_per_task=RESULT_RETENTION_PER_TASK):
        self.path = path
        self.result_retention_per_task = result_retention_per_task
        db_dir = os.path.dirname(path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA user_version;")
            (version,) = cur.fetchone()
            if version < 1:
                self._create_schema(cur)
                version = DB_LATEST_VERSION
                cur.execute(f"PRAGMA user_version={DB_LATEST_VERSION};")
            if version < 2:
                try:
                    cur.execute(
                        "ALTER TABLE tasks ADD COLUMN event_type TEXT NOT NULL DEFAULT 'script';"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
                cur.execute("PRAGMA user_version=2;")
                version = 2
            if version < 3:
                for statement in (
                    "ALTER TABLE tasks ADD COLUMN keep_success_log INTEGER NOT NULL DEFAULT 1;",
                    "ALTER TABLE tasks ADD COLUMN keep_failure_log INTEGER NOT NULL DEFAULT 1;",
                ):
                    try:
                        cur.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
                cur.execute("PRAGMA user_version=3;")
                version = 3
            if version < 4:
                for statement in (
                    "ALTER TABLE tasks ADD COLUMN latest_status TEXT;",
                    "ALTER TABLE tasks ADD COLUMN latest_trigger_reason TEXT;",
                    "ALTER TABLE tasks ADD COLUMN latest_started_at TEXT;",
                    "ALTER TABLE tasks ADD COLUMN latest_finished_at TEXT;",
                ):
                    try:
                        cur.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
                cur.execute("""
                    UPDATE tasks
                    SET
                        latest_status = (
                            SELECT status FROM task_results
                            WHERE task_results.task_id = tasks.id
                            ORDER BY started_at DESC, id DESC
                            LIMIT 1
                        ),
                        latest_trigger_reason = (
                            SELECT trigger_reason FROM task_results
                            WHERE task_results.task_id = tasks.id
                            ORDER BY started_at DESC, id DESC
                            LIMIT 1
                        ),
                        latest_started_at = (
                            SELECT started_at FROM task_results
                            WHERE task_results.task_id = tasks.id
                            ORDER BY started_at DESC, id DESC
                            LIMIT 1
                        ),
                        latest_finished_at = (
                            SELECT finished_at FROM task_results
                            WHERE task_results.task_id = tasks.id
                            ORDER BY started_at DESC, id DESC
                            LIMIT 1
                        )
                    WHERE EXISTS (
                        SELECT 1 FROM task_results WHERE task_results.task_id = tasks.id
                    )
                    """)
                cur.execute("PRAGMA user_version=4;")
                version = 4
            if version < 5:
                # spec-040：任务类型 + 结构化结果 + 上报队列。
                # 迁移必须幂等（用户可能手工回退过 PRAGMA user_version），所以逐条 try。
                for statement in (
                    "ALTER TABLE tasks ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'script';",
                    "ALTER TABLE tasks ADD COLUMN rclone_config TEXT NOT NULL DEFAULT '{}';",
                    "ALTER TABLE task_results ADD COLUMN exit_code INTEGER;",
                    "ALTER TABLE task_results ADD COLUMN stats TEXT;",
                    "ALTER TABLE task_results ADD COLUMN run_id TEXT;",
                    "ALTER TABLE task_results ADD COLUMN report_state TEXT;",
                    "ALTER TABLE task_results ADD COLUMN reported_at TEXT;",
                ):
                    try:
                        cur.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
                cur.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS report_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL UNIQUE,
                        task_id INTEGER,
                        payload TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_report_queue_pending
                        ON report_queue(state, next_attempt_at);
                    """
                )
                cur.execute("PRAGMA user_version=5;")
                version = 5
            if version < DB_LATEST_VERSION:
                cur.execute(f"PRAGMA user_version={DB_LATEST_VERSION};")
            self._conn.commit()
        if self.result_retention_per_task > 0:
            deleted = self.prune_all_finished_results()
            if deleted > 0:
                logger.info(
                    "Pruned %s finished task results (retention per task=%s)",
                    deleted,
                    self.result_retention_per_task,
                )

        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT COUNT(1) FROM sqlite_master WHERE type='table' AND name='templates'"
                )
                row = cur.fetchone()
                count = int(row[0]) if row else 0
                if count == 0:
                    # 兼容 ≤ v1.0.7 升级场景；如果 templates 表不存在，创建之（与 _create_schema 中定义一致）
                    cur.executescript("""
                        CREATE TABLE IF NOT EXISTS templates (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            key TEXT NOT NULL UNIQUE,
                            name TEXT NOT NULL,
                            script_body TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        """)
                    self._conn.commit()
        except Exception:
            logger.exception("Failed to create templates tables")
            pass

    def _create_schema(self, cur):
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                account TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                schedule_expression TEXT,
                condition_script TEXT,
                condition_interval INTEGER NOT NULL DEFAULT 60,
                event_type TEXT NOT NULL DEFAULT 'script',
                task_kind TEXT NOT NULL DEFAULT 'script',
                rclone_config TEXT NOT NULL DEFAULT '{}',
                is_active INTEGER NOT NULL DEFAULT 1,
                keep_success_log INTEGER NOT NULL DEFAULT 1,
                keep_failure_log INTEGER NOT NULL DEFAULT 1,
                pre_task_ids TEXT NOT NULL DEFAULT '[]',
                script_body TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at TEXT,
                last_condition_check_at TEXT,
                latest_status TEXT,
                latest_trigger_reason TEXT,
                latest_started_at TEXT,
                latest_finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                trigger_reason TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                exit_code INTEGER,
                stats TEXT,
                run_id TEXT,
                report_state TEXT,
                reported_at TEXT,
                log TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_task_results_task ON task_results(task_id, started_at DESC);

            CREATE TABLE IF NOT EXISTS report_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                task_id INTEGER,
                payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_report_queue_pending
                ON report_queue(state, next_attempt_at);
            
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                script_body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

    def close(self):
        with self._lock:
            self._conn.close()

    # Utility methods -----------------------------------------------------
    def _row_to_dict(self, row):
        data = dict(row)
        data["is_active"] = bool(data.get("is_active"))
        data["keep_success_log"] = bool(data.get("keep_success_log", 1))
        data["keep_failure_log"] = bool(data.get("keep_failure_log", 1))
        data["condition_interval"] = int(data.get("condition_interval", 60))
        data["pre_task_ids"] = json.loads(data.get("pre_task_ids") or "[]")
        data["event_type"] = data.get("event_type") or EVENT_TYPE_SCRIPT
        data["task_kind"] = data.get("task_kind") or TASK_KIND_SCRIPT
        try:
            parsed = json.loads(data.get("rclone_config") or "{}")
            data["rclone"] = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            data["rclone"] = {}
        return data

    # Templates management ----------------------------------------------
    def list_templates(self):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM templates ORDER BY id ASC")
            rows = [dict(row) for row in cur.fetchall()]
        return rows

    def get_template(self, template_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM templates WHERE id=?", (template_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def create_template(self, payload):
        now = isoformat(time_now())
        key = (payload.get("key") or "").strip()
        name = (payload.get("name") or "").strip()
        script_body = (payload.get("script_body") or "").strip()
        if not name:
            raise ValueError("template name is required")
        if not script_body:
            raise ValueError("template script body is required")
        if not key:
            # 自动生成 key（基于 name）
            base = name.lower().replace(" ", "_")
            key = base
            idx = 1
            while True:
                cur = self._conn.execute(
                    "SELECT COUNT(1) FROM templates WHERE key=?", (key,)
                )
                (count,) = cur.fetchone()
                if count == 0:
                    break
                idx += 1
                key = f"{base}_{idx}"
        now_iso = now
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO templates (key, name, script_body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (key, name, script_body, now_iso, now_iso),
                )
                self._conn.commit()
                tid = cur.lastrowid
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "unique" in msg or "templates.key" in msg:
                    raise ValueError("template key already exists") from exc
                raise ValueError("database integrity error") from exc
        return self.get_template(tid)  # type: ignore

    def update_template(self, template_id, payload):
        existing = self.get_template(template_id)
        if not existing:
            return None
        name = payload.get("name", existing.get("name", "")).strip()
        script_body = payload.get(
            "script_body", existing.get("script_body", "")
        ).strip()
        key = payload.get("key", existing.get("key", "")).strip()
        if not name:
            raise ValueError("template name is required")
        if not script_body:
            raise ValueError("template script body is required")
        updated_at = isoformat(time_now())
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE templates SET key=?, name=?, script_body=?, updated_at=? WHERE id=?",
                    (key, name, script_body, updated_at, template_id),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as exc:
            msg = str(exc).lower()
            if "unique" in msg or "templates.key" in msg:
                raise ValueError("template key already exists") from exc
            raise ValueError("database integrity error") from exc
        return self.get_template(template_id)

    def delete_template(self, template_id):
        with self._lock:
            cur = self._conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def import_templates(self, mapping):
        """Import templates from a mapping like templates.json (key -> {name, script_body}).
        Returns summary counts: inserted, updated"""
        inserted = 0
        updated = 0
        now = isoformat(time_now())
        with self._lock:
            for key, meta in (mapping or {}).items():
                name = (meta.get("name") or key).strip()
                script_body = (meta.get("script_body") or "").strip()
                if not script_body:
                    continue
                cur = self._conn.execute("SELECT id FROM templates WHERE key=?", (key,))
                row = cur.fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE templates SET name=?, script_body=?, updated_at=? WHERE key=?",
                        (name, script_body, now, key),
                    )
                    updated += 1
                else:
                    self._conn.execute(
                        "INSERT INTO templates (key, name, script_body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (key, name, script_body, now, now),
                    )
                    inserted += 1
            self._conn.commit()
        return {"inserted": inserted, "updated": updated}

    def export_templates(self):
        out: Dict[str, Dict[str, str]] = {}
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, name, script_body FROM templates ORDER BY id ASC"
            )
            for row in cur.fetchall():
                out[row[0]] = {"name": row[1], "script_body": row[2]}
        return out

    def list_tasks(self):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tasks ORDER BY id ASC")
            rows = [self._row_to_dict(row) for row in cur.fetchall()]
        return rows

    def get_task(self, task_id):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def create_task(self, payload):
        now = isoformat(time_now())
        task = self._prepare_task_payload(payload, is_update=False)
        task["created_at"] = now
        task["updated_at"] = now
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO tasks (
                        name, account, trigger_type, schedule_expression, condition_script,
                        condition_interval, event_type, task_kind, rclone_config,
                        is_active, keep_success_log, keep_failure_log,
                        pre_task_ids, script_body,
                        last_run_at, next_run_at, last_condition_check_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["name"],
                        task["account"],
                        task["trigger_type"],
                        task.get("schedule_expression"),
                        task.get("condition_script"),
                        task["condition_interval"],
                        task["event_type"],
                        task["task_kind"],
                        json.dumps(task.get("rclone") or {}, ensure_ascii=False),
                        1 if task["is_active"] else 0,
                        1 if task["keep_success_log"] else 0,
                        1 if task["keep_failure_log"] else 0,
                        json.dumps(task["pre_task_ids"]),
                        task["script_body"],
                        task.get("last_run_at"),
                        task.get("next_run_at"),
                        task.get("last_condition_check_at"),
                        task["created_at"],
                        task["updated_at"],
                    ),
                )
                task_id = cur.lastrowid
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "unique" in msg or "tasks.name" in msg:
                    raise ValueError("task name already exists") from exc
                raise ValueError("database integrity error") from exc
        return self.get_task(task_id)  # type: ignore

    def update_task(self, task_id, payload):
        existing = self.get_task(task_id)
        if not existing:
            return None
        payload = dict(payload)
        existing_is_active = parse_bool_value(existing.get("is_active"), default=True)
        next_trigger_type = payload.get("trigger_type", existing.get("trigger_type"))
        next_event_type = payload.get(
            "event_type", existing.get("event_type") or EVENT_TYPE_SCRIPT
        )
        next_is_active = parse_bool_value(
            payload.get("is_active"), default=existing_is_active
        )
        was_schedule = existing.get("trigger_type") == "schedule"
        is_schedule = next_trigger_type == "schedule"
        reactivated = not existing_is_active and next_is_active

        # 检查 Cron 表达式是否变更，变更则强制 next_run_at 重新计算
        old_expr = existing.get("schedule_expression")
        new_expr = payload.get("schedule_expression", old_expr)
        if was_schedule and old_expr != new_expr and new_expr:
            payload["next_run_at"] = None  # 让 _prepare_task_payload 自动计算

        if is_schedule and (reactivated or not was_schedule):
            payload["next_run_at"] = None

        was_script_event = (
            existing.get("trigger_type") == "event"
            and (existing.get("event_type") or EVENT_TYPE_SCRIPT) == EVENT_TYPE_SCRIPT
        )
        is_script_event = (
            next_trigger_type == "event" and next_event_type == EVENT_TYPE_SCRIPT
        )
        if is_script_event:
            switched_to_script_event = not was_script_event
            condition_script_changed = (
                "condition_script" in payload
                and (payload.get("condition_script") or "").strip()
                != (existing.get("condition_script") or "").strip()
            )
            condition_interval_changed = "condition_interval" in payload and int(
                payload.get(
                    "condition_interval", existing.get("condition_interval", 60)
                )
            ) != int(existing.get("condition_interval", 60))
            if (
                reactivated
                or switched_to_script_event
                or condition_script_changed
                or condition_interval_changed
            ):
                payload["last_condition_check_at"] = None

        task = self._prepare_task_payload({**existing, **payload}, is_update=True)
        task["updated_at"] = isoformat(time_now())
        try:
            with self._lock:
                self._conn.execute(
                    """
                        UPDATE tasks SET
                            name=?, account=?, trigger_type=?, schedule_expression=?, condition_script=?,
                            condition_interval=?, event_type=?, task_kind=?, rclone_config=?,
                            is_active=?, keep_success_log=?, keep_failure_log=?,
                            pre_task_ids=?, script_body=?,
                            last_run_at=?, next_run_at=?, last_condition_check_at=?, updated_at=?
                        WHERE id=?
                        """,
                    (
                        task["name"],
                        task["account"],
                        task["trigger_type"],
                        task.get("schedule_expression"),
                        task.get("condition_script"),
                        task["condition_interval"],
                        task["event_type"],
                        task["task_kind"],
                        json.dumps(task.get("rclone") or {}, ensure_ascii=False),
                        1 if task["is_active"] else 0,
                        1 if task["keep_success_log"] else 0,
                        1 if task["keep_failure_log"] else 0,
                        json.dumps(task["pre_task_ids"]),
                        task["script_body"],
                        task.get("last_run_at"),
                        task.get("next_run_at"),
                        task.get("last_condition_check_at"),
                        task["updated_at"],
                        task_id,
                    ),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as exc:
            msg = str(exc).lower()
            if "unique" in msg or "tasks.name" in msg:
                raise ValueError("task name already exists") from exc
            raise ValueError("database integrity error") from exc
        return self.get_task(task_id)

    def delete_task(self, task_id):
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def prune_finished_results(self, task_id):
        if self.result_retention_per_task <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM task_results
                WHERE task_id = ?
                  AND status != 'running'
                  AND id NOT IN (
                      SELECT id FROM task_results
                      WHERE task_id = ?
                        AND status != 'running'
                      ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
                      LIMIT ?
                  )
                """,
                (task_id, task_id, self.result_retention_per_task),
            )
            self._conn.commit()
            return cur.rowcount

    def prune_all_finished_results(self):
        if self.result_retention_per_task <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute("SELECT id FROM tasks")
            task_ids = [int(row[0]) for row in cur.fetchall()]
        deleted = 0
        for task_id in task_ids:
            deleted += self.prune_finished_results(task_id)
        return deleted

    def record_result_start(self, task_id, trigger_reason, run_id=None, task_kind=None):
        now = isoformat(time_now())
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO task_results(task_id, status, trigger_reason, started_at, run_id)
                VALUES (?, 'running', ?, ?, ?)
                """,
                (task_id, trigger_reason, now, run_id),
            )
            self._update_task_latest_result_locked(
                task_id, "running", trigger_reason, now, None
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def record_skipped_result(self, task_id, trigger_reason, reason):
        """记一条 `skipped`：任务因为目标被占用/依赖未满足而没跑。

        必须落库，不能只写日志——否则用户会以为它跑了（R10）。
        """
        now = isoformat(time_now())
        run_id = f"t{task_id}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO task_results(task_id, status, trigger_reason, started_at, finished_at, run_id, log)
                VALUES (?, 'skipped', ?, ?, ?, ?, ?)
                """,
                (task_id, trigger_reason, now, now, run_id, reason),
            )
            self._update_task_latest_result_locked(
                task_id, "skipped", trigger_reason, now, now
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def _update_task_latest_result_locked(
        self,
        task_id,
        status,
        trigger_reason,
        started_at,
        finished_at,
    ):
        self._conn.execute(
            """
            UPDATE tasks
            SET latest_status=?,
                latest_trigger_reason=?,
                latest_started_at=?,
                latest_finished_at=?
            WHERE id=?
            """,
            (status, trigger_reason, started_at, finished_at, task_id),
        )

    def _should_keep_result_record_locked(self, task_id, status):
        if task_id is None:
            return True
        cur = self._conn.execute(
            "SELECT keep_success_log, keep_failure_log FROM tasks WHERE id=?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return True
        if status == "success":
            return bool(row["keep_success_log"])
        return bool(row["keep_failure_log"])

    def finalize_result(self, result_id, status, log_text, exit_code=None, stats=None):
        now = isoformat(time_now())
        task_id: Optional[int] = None
        with self._lock:
            cur = self._conn.execute(
                "SELECT task_id, trigger_reason, started_at, status FROM task_results WHERE id=?",
                (result_id,),
            )
            row = cur.fetchone()
            if not row or row["status"] != "running":
                return
            task_id = int(row["task_id"])
            self._update_task_latest_result_locked(
                task_id,
                status,
                row["trigger_reason"],
                row["started_at"],
                now,
            )
            if self._should_keep_result_record_locked(task_id, status):
                self._conn.execute(
                    """
                    UPDATE task_results
                    SET status=?, finished_at=?, log=?, exit_code=?, stats=?
                    WHERE id=?
                    """,
                    (
                        status,
                        now,
                        log_text,
                        exit_code,
                        json.dumps(stats, ensure_ascii=False) if stats is not None else None,
                        result_id,
                    ),
                )
            else:
                self._conn.execute("DELETE FROM task_results WHERE id=?", (result_id,))
            self._conn.commit()
        if task_id is not None:
            self.prune_finished_results(task_id)

    def record_finished_result(self, task_id, trigger_reason, status, log_text):
        result_id = self.record_result_start(task_id, trigger_reason)
        self.finalize_result(result_id, status, log_text)
        return result_id

    def fetch_results(self, task_id, limit=50, offset=0):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id=? ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return rows

    # ---- spec-040 上报队列 -------------------------------------------

    def enqueue_report(self, run_id, task_id, payload):
        """写入待上报事件。同一 `run_id` 重复写入直接忽略（R20 幂等）。"""
        now = isoformat(time_now())
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO report_queue(run_id, task_id, payload, state, attempts, next_attempt_at, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        now,
                        now,
                    ),
                )
                self._conn.execute(
                    "UPDATE task_results SET report_state='pending' WHERE run_id=?",
                    (run_id,),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # run_id 已存在：幂等，不算错误
                self._conn.rollback()
                return False

    def fetch_due_reports(self, moment, limit=20):
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM report_queue
                WHERE state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                ORDER BY id ASC LIMIT ?
                """,
                (isoformat(moment), limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_report_sent(self, queue_id, run_id):
        now = isoformat(time_now())
        with self._lock:
            # 同时清掉 last_error：补发成功后还留着「连不上 HA」，设置页会一直显示过期错误
            self._conn.execute(
                "UPDATE report_queue SET state='sent', last_error=NULL, updated_at=? WHERE id=?",
                (now, queue_id),
            )
            if run_id:
                self._conn.execute(
                    "UPDATE task_results SET report_state='ok', reported_at=? WHERE run_id=?",
                    (now, run_id),
                )
            self._conn.commit()

    def mark_report_failed(self, queue_id, error, next_attempt_at, run_id=None, dead=False):
        now = isoformat(time_now())
        with self._lock:
            self._conn.execute(
                """
                UPDATE report_queue
                SET state=?, attempts=attempts+1, last_error=?, next_attempt_at=?, updated_at=?
                WHERE id=?
                """,
                ("failed" if dead else "pending", str(error)[:500], next_attempt_at, now, queue_id),
            )
            if run_id:
                self._conn.execute(
                    "UPDATE task_results SET report_state=? WHERE run_id=?",
                    ("failed" if dead else "pending", run_id),
                )
            self._conn.commit()

    def report_queue_stats(self):
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT state, COUNT(1) AS n FROM report_queue GROUP BY state
                """
            )
            counts = {row["state"]: int(row["n"]) for row in cur.fetchall()}
            cur = self._conn.execute(
                "SELECT last_error, updated_at FROM report_queue "
                "WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        return {
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "last_error": (row["last_error"] if row else "") or "",
            "last_attempt_at": (row["updated_at"] if row else "") or "",
        }

    def fetch_result(self, task_id, result_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id=? AND id=?",
                (task_id, result_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_result_row(self, result_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM task_results WHERE id=?", (result_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def delete_results(self, task_id, result_id=None):
        with self._lock:
            if result_id is None:
                cur = self._conn.execute(
                    "DELETE FROM task_results WHERE task_id=?", (task_id,)
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM task_results WHERE task_id=? AND id=?",
                    (task_id, result_id),
                )
            self._conn.commit()
            return cur.rowcount

    def get_latest_result(self, task_id):
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    latest_status,
                    latest_trigger_reason,
                    latest_started_at,
                    latest_finished_at
                FROM tasks
                WHERE id=?
                """,
                (task_id,),
            )
            task_row = cur.fetchone()
            if task_row and task_row["latest_status"]:
                return {
                    "id": None,
                    "task_id": task_id,
                    "status": task_row["latest_status"],
                    "trigger_reason": task_row["latest_trigger_reason"] or "",
                    "started_at": task_row["latest_started_at"],
                    "finished_at": task_row["latest_finished_at"],
                    "log": None,
                    "snapshot": True,
                }
            cur = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id=? ORDER BY started_at DESC, id DESC LIMIT 1",
                (task_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def has_running_instance(self, task_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(1) FROM task_results WHERE task_id=? AND status='running'",
                (task_id,),
            )
            (count,) = cur.fetchone()
        return count > 0

    def fetch_running_instances(self):
        with self._lock:
            cur = self._conn.execute(
                "SELECT task_id, started_at FROM task_results WHERE status='running'"
            )
            return [dict(row) for row in cur.fetchall()]

    def finalize_stale_running_instances(self, task_id, reason="stopped by user"):
        now = isoformat(time_now())
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT trigger_reason, started_at
                FROM task_results
                WHERE task_id=? AND status='running'
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
                (task_id,),
            )
            latest_running = cur.fetchone()
            keep_result = self._should_keep_result_record_locked(task_id, "failed")
            if not keep_result:
                cur = self._conn.execute(
                    "DELETE FROM task_results WHERE task_id=? AND status='running'",
                    (task_id,),
                )
                if cur.rowcount > 0 and latest_running:
                    self._update_task_latest_result_locked(
                        task_id,
                        "failed",
                        latest_running["trigger_reason"],
                        latest_running["started_at"],
                        now,
                    )
                self._conn.commit()
                return cur.rowcount
            cur = self._conn.execute(
                """
                UPDATE task_results
                SET status='failed',
                    finished_at=?,
                    log=CASE
                        WHEN log IS NULL OR log='' THEN ?
                        ELSE log || '\n' || ?
                    END
                WHERE task_id=? AND status='running'
                """,
                (now, reason, reason, task_id),
            )
            if cur.rowcount > 0 and latest_running:
                self._update_task_latest_result_locked(
                    task_id,
                    "failed",
                    latest_running["trigger_reason"],
                    latest_running["started_at"],
                    now,
                )
            self._conn.commit()
            return cur.rowcount

    def update_last_run(self, task_id):
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET last_run_at=?, updated_at=? WHERE id=?",
                (isoformat(time_now()), isoformat(time_now()), task_id),
            )
            self._conn.commit()

    def schedule_next_run(self, task_id, expression, base=None):
        if not expression:
            return None
        cron = CronExpression(expression)
        next_dt = cron.next_after(base or time_now())
        next_iso = isoformat(next_dt)
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET next_run_at=?, updated_at=? WHERE id=?",
                (next_iso, isoformat(time_now()), task_id),
            )
            self._conn.commit()
        return next_iso

    def update_condition_check(self, task_id):
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET last_condition_check_at=?, updated_at=? WHERE id=?",
                (isoformat(time_now()), isoformat(time_now()), task_id),
            )
            self._conn.commit()

    def fetch_due_tasks(self, moment):
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE trigger_type='schedule' AND is_active=1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (isoformat(moment),),
            )
            rows = [self._row_to_dict(row) for row in cur.fetchall()]
        return rows

    def fetch_event_tasks(self, event_type=None):
        query = "SELECT * FROM tasks WHERE trigger_type='event' AND is_active=1"
        params: List[Any] = []
        if event_type:
            query += " AND event_type=?"
            params.append(event_type)
        query += " ORDER BY id ASC"
        with self._lock:
            cur = self._conn.execute(query, params)
            rows = [self._row_to_dict(row) for row in cur.fetchall()]
        return rows

    # Payload utilities ---------------------------------------------------
    def _prepare_task_payload(self, payload, is_update):
        trigger_type = payload.get("trigger_type", "schedule")
        if trigger_type not in {"schedule", "event"}:
            raise ValueError("trigger_type must be 'schedule' or 'event'")
        name = payload.get("name", "").strip()
        account_raw = payload.get("account", "")
        account = account_raw.strip()
        if not account and not POSIX_ACCOUNT_SUPPORT:
            account = DEFAULT_ACCOUNT_NAME
        if not name:
            raise ValueError("task name is required")
        if not account:
            raise ValueError("account is required")
        account = ensure_account_allowed(account)

        # spec-040：按任务类型分流。rclone 任务不要求 script_body——它由结构化字段编译而来，
        # 用户不该看到、也不该编辑那段命令。
        task_kind = (payload.get("task_kind") or TASK_KIND_SCRIPT)
        task_kind = task_kind.strip() if isinstance(task_kind, str) else TASK_KIND_SCRIPT
        if task_kind not in TASK_KINDS:
            raise ValueError(f"task_kind must be one of {', '.join(TASK_KINDS)}")

        rclone_config: Dict[str, Any] = {}
        if task_kind == TASK_KIND_RCLONE:
            raw_config = payload.get("rclone")
            if isinstance(raw_config, str):
                try:
                    raw_config = json.loads(raw_config)
                except json.JSONDecodeError as exc:
                    raise ValueError("rclone config format error") from exc
            if not isinstance(raw_config, dict):
                raise ValueError("rclone config is required")
            rclone_config = dict(raw_config)
            # 破坏性模式（sync 会删目标端多余文件）必须显式确认，这是防止「备份把备份盘清空」的唯一门。
            wants_dry_run = parse_bool_value(rclone_config.get("dry_run"), default=False)
            if rclone_mod.is_destructive(rclone_config, dry_run=wants_dry_run):
                if not parse_bool_value(payload.get("confirm_destructive"), default=False):
                    raise ValueError(
                        "sync 会删除目标端多余文件，需要 confirm_destructive=true 才能保存"
                    )
            try:
                argv = rclone_mod.build_argv(
                    rclone_config, rclone_bin="rclone", dry_run=wants_dry_run
                )
            except rclone_mod.ConfigError as exc:
                raise ValueError(f"rclone config invalid: {exc.message}") from exc
            # 展示用：只作记录与日志，真正执行仍走 argv（不经过 shell）
            script_body = " ".join(argv)
            rclone_config = rclone_mod.normalize_config(rclone_config)
        else:
            if isinstance(payload.get("rclone"), dict) and payload.get("rclone"):
                raise ValueError("script 任务不接受 rclone 配置")
            rclone_config = {}
            script_body = payload.get("script_body", "").strip()
            if not script_body:
                raise ValueError("script body is required")

        is_active = parse_bool_value(payload.get("is_active"), default=True)
        keep_success_log = parse_bool_value(
            payload.get("keep_success_log"), default=True
        )
        keep_failure_log = parse_bool_value(
            payload.get("keep_failure_log"), default=True
        )
        schedule_expression_raw = payload.get("schedule_expression")
        schedule_expression = (
            schedule_expression_raw.strip()
            if isinstance(schedule_expression_raw, str)
            else schedule_expression_raw
        )
        condition_script_raw = payload.get("condition_script")
        condition_script = (
            condition_script_raw.strip()
            if isinstance(condition_script_raw, str)
            else condition_script_raw
        )
        condition_interval = max(10, int(payload.get("condition_interval", 60)))
        event_type_raw = payload.get("event_type")
        event_type = (
            (event_type_raw or EVENT_TYPE_SCRIPT).strip()
            if isinstance(event_type_raw, str)
            else (event_type_raw or EVENT_TYPE_SCRIPT)
        )
        pre_task_ids = payload.get("pre_task_ids") or []
        if isinstance(pre_task_ids, str):
            try:
                pre_task_ids = json.loads(pre_task_ids)
            except json.JSONDecodeError as exc:
                raise ValueError("pre_task_ids format error") from exc
        current_id = payload.get("id")
        if current_id is not None:
            current_id = int(current_id)
        cleaned: List[int] = []
        for tid in pre_task_ids:
            tid_int = int(tid)
            if current_id is not None and tid_int == current_id:
                continue
            if tid_int not in cleaned:
                cleaned.append(tid_int)
        pre_task_ids = cleaned

        next_run_at: Optional[str] = payload.get("next_run_at")
        last_condition_check_at = payload.get("last_condition_check_at")

        if trigger_type == "schedule":
            if not schedule_expression:
                raise ValueError("schedule expression is required")
            cron = CronExpression(schedule_expression)
            if not is_update or not next_run_at:
                next_run_at = isoformat(cron.next_after(time_now()))
            condition_script = None
            event_type = EVENT_TYPE_SCRIPT
        else:
            if event_type not in EVENT_TYPES:
                raise ValueError("event type is not supported")
            if event_type == EVENT_TYPE_SCRIPT:
                if not condition_script:
                    raise ValueError("event tasks require condition script")
                last_condition_check_at = payload.get("last_condition_check_at")
            else:
                condition_script = None
                last_condition_check_at = None
            schedule_expression = None
            next_run_at = None

        return {
            "name": name,
            "account": account,
            "trigger_type": trigger_type,
            "schedule_expression": schedule_expression,
            "condition_script": condition_script,
            "condition_interval": condition_interval,
            "event_type": event_type,
            "task_kind": task_kind,
            "rclone": rclone_config,
            "is_active": is_active,
            "keep_success_log": keep_success_log,
            "keep_failure_log": keep_failure_log,
            "pre_task_ids": pre_task_ids,
            "script_body": script_body,
            "last_run_at": payload.get("last_run_at"),
            "next_run_at": next_run_at,
            "last_condition_check_at": last_condition_check_at,
        }


###############################################################################
# Scheduler engine
###############################################################################


def build_report_payload(task, result_row, status, stats, exit_code, log_text):
    """组装上报事件，字段与 `mnn_home.report_job` 的契约一一对应（design.md「契约 2」）。

    只包含路径与统计，不含文件内容（R44）。
    """
    task_id = int(task["id"])
    kind = task.get("task_kind") or TASK_KIND_SCRIPT
    name = task.get("name") or f"任务 {task_id}"
    payload_stats = dict(stats or {})
    if exit_code is not None:
        payload_stats["exit_code"] = exit_code

    text = log_text or ""
    # 失败摘要只取 ERROR/Failed 行，且只要最后几条——整段日志放到 log_tail 里
    error_lines = [
        line for line in text.splitlines() if "ERROR" in line or "Failed" in line
    ]
    excerpt = "\n".join(error_lines[-3:])[:800]
    if not excerpt and status not in ("success", "dry_run", "skipped"):
        excerpt = text[-800:]

    if kind == TASK_KIND_RCLONE:
        summary = rclone_mod.summarize(payload_stats, status)
        target = str((task.get("rclone") or {}).get("dest") or "")
        job_key = rclone_mod.job_key(name, task_id, prefix=TASK_KIND_RCLONE)
    else:
        summary = {
            "success": "成功",
            "failed": "失败",
            "skipped": "跳过",
            "dry_run": "试运行",
            "partial": "部分失败",
        }.get(status, status)
        target = ""
        job_key = rclone_mod.job_key(name, task_id, prefix="task")

    return {
        "job_key": job_key,
        "run_id": result_row.get("run_id") or "",
        "name": name,
        "kind": kind,
        "status": status,
        "started_at": result_row.get("started_at") or "",
        "finished_at": result_row.get("finished_at") or "",
        "trigger": result_row.get("trigger_reason") or "",
        "summary": summary,
        "stats": payload_stats,
        "target": target,
        "error_excerpt": excerpt,
        "log_tail": text[-4000:],
    }


class RcloneTargetLocks:
    """按目标 remote 的写入互斥（spec-040 R10）。

    为什么按 remote 而不是全局一把锁：全局锁会让「备份到 A」和「同步到 B」互相排队，
    而它们根本不冲突；真正会互相破坏的是**同一个 remote 上的两个写入型任务**。

    锁在**启动线程之前**就拿到，抢不到直接告诉调用方，而不是先起线程再在后台失败——
    后者会让界面看到「已排队」却什么也没发生。
    """

    _lock = threading.RLock()
    _holders: Dict[str, str] = {}  # remote -> 描述（任务名#id）

    @classmethod
    def try_acquire(cls, key: str, holder: str = "") -> bool:
        if not key:
            return True
        with cls._lock:
            if key in cls._holders:
                return False
            cls._holders[key] = holder
            return True

    @classmethod
    def release(cls, key: str) -> None:
        if not key:
            return
        with cls._lock:
            cls._holders.pop(key, None)

    @classmethod
    def holder(cls, key: str) -> Optional[str]:
        with cls._lock:
            return cls._holders.get(key)


class TaskRunner(threading.Thread):
    _running_lock = threading.RLock()
    _running_processes: Dict[int, Set[Popen[str]]] = {}
    # R16：运行中进度。刻意放内存而不是 DB——
    # 每秒一条 UPDATE 会和结果写入抢同一把库锁，而且进程一重启进度本来就失去意义。
    _progress_lock = threading.RLock()
    _progress: Dict[int, Dict[str, object]] = {}

    @classmethod
    def set_progress(cls, task_id, progress):
        with cls._progress_lock:
            cls._progress[int(task_id)] = dict(progress)

    @classmethod
    def get_progress(cls, task_id):
        with cls._progress_lock:
            data = cls._progress.get(int(task_id))
            return dict(data) if data else None

    @classmethod
    def clear_progress(cls, task_id):
        with cls._progress_lock:
            cls._progress.pop(int(task_id), None)

    def __init__(
        self,
        db,
        task,
        trigger_reason,
        settings,
        lock_key: str = "",
    ):
        super().__init__(daemon=True)
        self.db = db
        self.task = task
        self.trigger_reason = trigger_reason
        self.settings = settings
        self.lock_key = lock_key or ""

    def run(self):
        task_id = self.task["id"]
        logger.info("Executing task %s (%s)", task_id, self.trigger_reason)
        run_id = f"t{task_id}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        result_id = self.db.record_result_start(
            task_id,
            self.trigger_reason,
            run_id=run_id,
            task_kind=self.task.get("task_kind") or TASK_KIND_SCRIPT,
        )
        execution_timeout = self.settings.task_timeout or None
        status: str = "failed"
        log_text: str = "unknown error"
        stats: Optional[Dict[str, Optional[int]]] = None
        exit_code: Optional[int] = None
        try:
            if (self.task.get("task_kind") or TASK_KIND_SCRIPT) == TASK_KIND_RCLONE:
                log_text, status, exit_code, stats = self._execute_rclone(run_id)
            else:
                log_text, status = self._execute_script(
                    self.task["script_body"], execution_timeout
                )
        except Exception as exc:  # pylint: disable=broad-except
            status = "failed"
            log_text = f"task execution exception: {exc!r}"
        finally:
            try:
                self.db.finalize_result(
                    result_id, status, log_text, exit_code=exit_code, stats=stats
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Failed to finalize result %s for task %s: %s",
                    result_id,
                    task_id,
                    exc,
                    exc_info=True,
                )
            finally:
                # 锁必须在这里放：无论成功、失败还是异常，不放就会永久占住这个 remote，
                # 之后同名目标的所有任务都会拿到 target_busy。
                RcloneTargetLocks.release(self.lock_key)
            try:
                self._maybe_report(result_id, run_id, status, stats, exit_code, log_text)
            except Exception as exc:  # pylint: disable=broad-except
                # R29：上报是旁路，失败不许影响任务结果与本地记录
                logger.exception("任务 %s 上报入队失败：%s", task_id, exc)
            try:
                self.db.update_last_run(task_id)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Failed to update last_run for task %s: %s",
                    task_id,
                    exc,
                    exc_info=True,
                )

    def _execute_script(self, script, timeout):
        cmd = self._build_command(script)
        preexec_fn, home_dir = self._prepare_account_context()
        env = build_task_environment(self.task, self.trigger_reason, home_dir)
        task_id = int(self.task["id"])
        try:
            process: Popen[str] = Popen(
                cmd,
                stdout=PIPE,
                stderr=PIPE,
                text=True,
                env=env,
                preexec_fn=preexec_fn,
            )
            self._register_process(task_id, process)
            stdout, stderr = process.communicate(timeout=timeout)
        except TimeoutExpired as exc:
            try:
                # Kill the whole process tree so children holding the pipes
                # (e.g. `sleep` or daemonized processes) cannot keep
                # communicate() blocked until they exit on their own.
                TaskRunner.terminate_task_processes(task_id, grace_seconds=1.0)
                proc = locals().get("process")
                if proc is not None:
                    proc.wait(timeout=5)
            except Exception:
                pass
            return f"task execution timeout (> {timeout}s): {exc}", "failed"
        except Exception as exc:  # pylint: disable=broad-except
            return str(exc), "failed"
        finally:
            proc = locals().get("process")
            if proc is not None:
                self._unregister_process(task_id, proc)
        output = (stdout or "") + (stderr or "")
        status = "success" if process.returncode == 0 else "failed"
        return output.strip(), status

    def _maybe_report(self, result_id, run_id, status, stats, exit_code, log_text):
        """按 `report_mode` 决定是否把这次执行放进上报队列（R30）。

        注意这里只**入队**，不发送：发送由后台线程做，这样执行线程不被网络拖慢，
        HA 不可达时也不会让任务看起来「卡住」。
        """
        mode = self.settings.report_mode
        if mode == REPORT_OFF or REPORTER is None:
            return
        # 没配 HA 地址/令牌时直接不排队：否则全新安装上每跑一次任务就积一条永远发不出去的
        # 事件，队列只增不减（实测升级后 pending 立刻变 1）。用户之后配上令牌，只管往后的事件。
        if not REPORTER.configured():
            return
        if mode == REPORT_FAIL and status in ("success", "dry_run", "skipped"):
            return
        row = self.db.get_result_row(result_id)
        if not row:
            # 结果记录被 keep_*_log 策略删掉了：用内存里的信息凑一个最小事件
            row = {
                "run_id": run_id,
                "started_at": "",
                "finished_at": "",
                "trigger_reason": self.trigger_reason,
            }
        row["run_id"] = row.get("run_id") or run_id
        payload = build_report_payload(
            self.task, row, status, stats, exit_code, log_text
        )
        queued = self.db.enqueue_report(run_id, int(self.task["id"]), payload)
        if queued:
            logger.info("任务 %s 结果已入上报队列（%s）", self.task["id"], status)

    def _execute_rclone(self, run_id):
        """执行一次 rclone 任务（spec-040）。

        与 `_execute_script` 的三点区别：
        - **不经 shell**：argv 直接给 Popen，路径里的空格/中文/`$()` 不会被二次解释；
        - 结果除了退出码还要带**结构化统计**（R8）；
        - 退出码分三档而不是「非 0 即失败」（R9）。

        返回 (log_text, status, exit_code, stats)。
        """
        config = dict(self.task.get("rclone") or {})
        dry_run = self.trigger_reason == TRIGGER_DRY_RUN or parse_bool_value(
            config.get("dry_run"), default=False
        )
        try:
            argv = rclone_mod.build_argv(
                config, rclone_bin=self.settings.rclone_bin, dry_run=dry_run
            )
        except rclone_mod.ConfigError as exc:
            return (f"rclone 配置无效：{exc.message}", "failed", None, None)

        preexec_fn, home_dir = self._prepare_account_context()
        env = build_task_environment(self.task, self.trigger_reason, home_dir)
        task_id = int(self.task["id"])
        execution_timeout = self.settings.task_timeout or None
        logger.info(
            "Running rclone task %s: %s",
            task_id,
            " ".join(argv),
        )

        process = None
        # 逐行收集：既要给 parse_stats 一个完整文本，也要边读边出进度（R16）。
        # 用 deque 只留最近若干行做进度解析窗口——统计块最多十来行，
        # 没必要每次都对整个输出重跑正则。
        collected: List[str] = []
        recent: collections.deque = collections.deque(maxlen=60)

        def _pump():
            try:
                stream = process.stdout
                if stream is None:
                    return
                for raw in stream:
                    line = (raw or "").rstrip("\n")
                    collected.append(line)
                    if not rclone_mod.is_stats_line(line):
                        continue
                    recent.append(line)
                    snapshot = rclone_mod.parse_progress("\n".join(recent))
                    if snapshot:
                        snapshot["updated_at"] = isoformat(time_now())
                        TaskRunner.set_progress(task_id, snapshot)
            except Exception:  # pylint: disable=broad-except
                # 进程被超时终止时读流会抛，这是预期路径，不当错误处理
                pass

        reader = None
        try:
            process = Popen(
                argv,
                stdout=PIPE,
                stderr=STDOUT,  # rclone 的日志走 stderr，合并才能保持时序、一处解析
                text=True,
                env=env,
                preexec_fn=preexec_fn,
            )
            self._register_process(task_id, process)
            reader = threading.Thread(target=_pump, daemon=True)
            reader.start()
            process.wait(timeout=execution_timeout)
            # 等读流线程把尾巴收完，否则最后一块统计可能还没进 collected
            reader.join(timeout=5)
        except TimeoutExpired:
            try:
                TaskRunner.terminate_task_processes(task_id, grace_seconds=1.0)
                if process is not None:
                    process.wait(timeout=5)
            except Exception:
                pass
            return (
                f"rclone 执行超时（> {execution_timeout}s），已终止进程树",
                "failed",
                None,
                None,
            )
        except FileNotFoundError:
            return (
                f"找不到 rclone（{self.settings.rclone_bin}）。"
                "请在设置里指定正确路径，或先在系统上安装 rclone。",
                "failed",
                None,
                None,
            )
        except Exception as exc:  # pylint: disable=broad-except
            return (f"rclone 启动失败：{exc!r}", "failed", None, None)
        finally:
            if process is not None:
                self._unregister_process(task_id, process)
            TaskRunner.clear_progress(task_id)

        exit_code = process.returncode
        # 每秒一块的统计刷屏在这里折叠成「普通日志 + 最后一块」，
        # 否则 4000 字的日志预览会被中间块挤光（R8 的现场要留给用户看）。
        log_text, last_block = rclone_mod.fold_stats_stream(collected)
        if last_block:
            log_text = (log_text + "\n" + "\n".join(last_block)).strip()
        text = log_text
        stats = rclone_mod.parse_stats(text)
        if stats:
            stats["exit_code"] = exit_code

        status = rclone_mod.classify_exit(exit_code)
        if dry_run and status != rclone_mod.STATUS_FAILED:
            # 试运行不能算「成功备份」，否则界面上会与真实执行混淆（R12）
            status = "dry_run"
        logger.info(
            "rclone task %s finished: exit=%s status=%s stats=%s",
            task_id,
            exit_code,
            status,
            stats,
        )
        return text.strip(), status, exit_code, stats

    @classmethod
    def _register_process(cls, task_id, process):
        with cls._running_lock:
            cls._running_processes.setdefault(task_id, set()).add(process)

    @classmethod
    def _unregister_process(cls, task_id, process):
        with cls._running_lock:
            processes = cls._running_processes.get(task_id)
            if not processes:
                return
            processes.discard(process)
            if not processes:
                cls._running_processes.pop(task_id, None)

    @classmethod
    def terminate_task_processes(cls, task_id, grace_seconds=3.0):
        with cls._running_lock:
            processes = list(cls._running_processes.get(task_id, set()))

        # Kill the whole process tree: the tracked Popen handles plus any
        # descendant processes carrying the SCHEDULER_TASK_ID marker.
        # Descendants that keep the stdout/stderr pipes open (e.g. `sleep` or
        # daemonized children) would otherwise outlive the shell and keep
        # communicate() blocked until they exit on their own.
        pids: Set[int] = set()
        for process in processes:
            if process.poll() is not None:
                continue
            if process.pid is not None:
                pids.add(process.pid)
        pids.update(cls._find_task_pids(task_id))

        if not pids:
            return {
                "targeted": 0,
                "terminated": 0,
                "killed": 0,
                "already_exited": 0,
            }
        return cls._terminate_pids(sorted(pids), grace_seconds)

    @staticmethod
    def _find_task_pids(task_id):
        if os.name != "posix":
            return []
        target = f"SCHEDULER_TASK_ID={task_id}".encode("utf-8")
        current_pid = os.getpid()
        found: Set[int] = set()
        proc_root = "/proc"
        try:
            entries = os.listdir(proc_root)
        except Exception:
            return []

        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == current_pid:
                continue
            env_path = os.path.join(proc_root, entry, "environ")
            try:
                with open(env_path, "rb") as env_file:
                    content = env_file.read()
            except Exception:
                continue
            if target in content:
                found.add(pid)
        return sorted(found)

    @staticmethod
    def _terminate_pids(pids, grace_seconds=3.0):
        terminated = 0
        killed = 0
        already_exited = 0
        alive_after_terminate: List[int] = []

        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                terminated += 1
                alive_after_terminate.append(pid)
            except ProcessLookupError:
                already_exited += 1
            except PermissionError:
                continue

        deadline = time.monotonic() + max(0.0, grace_seconds)
        survivors: List[int] = []
        for pid in alive_after_terminate:
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                time.sleep(0.1)
            else:
                survivors.append(pid)

        for pid in survivors:
            try:
                os.kill(
                    pid, signal.SIGKILL  # pyright: ignore[reportAttributeAccessIssue]
                )
                killed += 1
            except ProcessLookupError:
                already_exited += 1
            except PermissionError:
                continue

        return {
            "targeted": len(pids),
            "terminated": terminated,
            "killed": killed,
            "already_exited": already_exited,
        }

    @staticmethod
    def _build_command(script):
        if os.name == "nt":
            return [
                "powershell",
                "-NoLogo",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ]
        return ["/bin/bash", "-c", script]

    def _prepare_account_context(
        self,
    ):
        return prepare_task_account_context(self.task)


class SchedulerEngine:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        # 记录服务启动时间，用于跳过重启前已过期的定时任务
        self.started_at: Optional[datetime] = None

    def start(self):
        # 标记启动时刻，之后复核过期任务时会基于此时间跳过历史遗留的执行
        self.started_at = time_now()
        recovered = 0
        for running in self.db.fetch_running_instances():
            recovered += self.db.finalize_stale_running_instances(
                int(running["task_id"]),
                reason="interrupted by scheduler restart",
            )
        if recovered:
            logger.warning(
                "Recovered %s stale running task result(s) after scheduler restart",
                recovered,
            )
        self.thread.start()
        self._trigger_system_event(EVENT_TYPE_BOOT)

    def stop(self):
        self.stop_event.set()
        for running in self.db.fetch_running_instances():
            task_id = int(running["task_id"])
            TaskRunner.terminate_task_processes(task_id, grace_seconds=1.0)
            if self.db.has_running_instance(task_id):
                self.db.finalize_stale_running_instances(
                    task_id,
                    reason="interrupted by scheduler shutdown",
                )
        self._trigger_system_event(EVENT_TYPE_SHUTDOWN)
        self.thread.join(timeout=5)

    # Internal ------------------------------------------------------------
    def _loop(self):
        while not self.stop_event.is_set():
            now = time_now()
            try:
                self._process_due_tasks(now)
                self._process_event_tasks(now)
                self._enforce_task_timeouts(now)
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Scheduler loop error: %s", exc)
            self.stop_event.wait(1)

    def _process_due_tasks(self, moment):
        for task in self.db.fetch_due_tasks(moment):
            # 跳过那些在服务启动之前就已经过期的任务（避免重启后回放执行）
            try:
                next_run_dt = parse_iso(task.get("next_run_at"))
            except Exception:
                next_run_dt = None
            if self.started_at and next_run_dt and next_run_dt < self.started_at:
                logger.info(
                    "Skipping expired task %s scheduled at %s (service started at %s)",
                    task.get("id"),
                    task.get("next_run_at"),
                    isoformat(self.started_at),
                )
                # 重新安排到下一个可用时间，但不执行错过的运行
                try:
                    self.db.schedule_next_run(
                        task["id"], task["schedule_expression"], self.started_at
                    )
                except Exception:
                    logger.exception(
                        "Failed to reschedule expired task %s", task.get("id")
                    )
                continue
            if self.db.has_running_instance(task["id"]):
                logger.info("Task %s still running, skip", task["id"])
                continue
            if not self._dependencies_met(task):
                self._record_dependency_block(task, "schedule")
                # re-schedule shortly in future to retry
                self.db.schedule_next_run(
                    task["id"],
                    task["schedule_expression"],
                    moment + timedelta(minutes=1),
                )
                continue
            started, code = self.spawn(task, "schedule")
            if not started:
                self.db.record_skipped_result(
                    task["id"], "schedule", f"跳过：同一 remote 上有写入型任务在跑（{code}）"
                )
            self.db.schedule_next_run(task["id"], task["schedule_expression"], moment)

    def spawn(self, task, trigger_reason):
        """按任务类型决定是否先加锁，再启动执行线程。

        返回 `(ok, code)`。锁在**启动线程前**拿，抢不到立即返回，避免出现
        「界面显示已排队、实际什么都没发生」（R10）。
        """
        lock_key = ""
        if (task.get("task_kind") or TASK_KIND_SCRIPT) == TASK_KIND_RCLONE:
            lock_key = rclone_mod.lock_key(task.get("rclone") or {}) or ""
            holder = f"{task.get('name') or task.get('id')}"
            if lock_key and not RcloneTargetLocks.try_acquire(lock_key, holder):
                return False, "target_busy"
        TaskRunner(
            self.db, task, trigger_reason, self.settings, lock_key=lock_key
        ).start()
        return True, ""

    def _process_event_tasks(self, moment):
        for task in self.db.fetch_event_tasks(event_type=EVENT_TYPE_SCRIPT):
            last_check = parse_iso(task.get("last_condition_check_at"))
            interval = task.get("condition_interval", 60)
            if last_check and (moment - last_check).total_seconds() < interval:
                continue
            self.db.update_condition_check(task["id"])
            if not task.get("condition_script"):
                continue
            ok = self._run_condition(task, "condition")
            if not ok:
                continue
            if self.db.has_running_instance(task["id"]):
                continue
            if not self._dependencies_met(task):
                self._record_dependency_block(task, "condition")
                continue
            started, code = self.spawn(task, "condition")
            if not started:
                self.db.record_skipped_result(
                    task["id"], "condition", f"目标 {code}：同一 remote 上有写入型任务在跑"
                )

    def _enforce_task_timeouts(self, moment):
        timeout = self.settings.task_timeout
        if not timeout or timeout <= 0:
            return
        for running in self.db.fetch_running_instances():
            task_id = int(running["task_id"])
            started_at = parse_iso(running["started_at"])
            if not started_at:
                continue
            elapsed = (moment - started_at).total_seconds()
            if elapsed <= timeout:
                continue
            logger.warning(
                "Task %s exceeded %ss timeout (elapsed %.0fs), terminating processes",
                task_id,
                timeout,
                elapsed,
            )
            summary = TaskRunner.terminate_task_processes(task_id)
            logger.info("Timeout termination for task %s: %s", task_id, summary)

    def _run_condition(self, task, trigger_reason):
        command = TaskRunner._build_command(task["condition_script"])
        preexec_fn, home_dir = prepare_task_account_context(task)
        env = build_task_environment(task, "condition_check", home_dir)
        try:
            completed = run(
                command,
                capture_output=True,
                text=True,
                env=env,
                preexec_fn=preexec_fn,
                timeout=self.settings.condition_timeout,
                check=False,
            )
        except TimeoutExpired as exc:
            logger.warning("Condition script timeout for task %s: %s", task["id"], exc)
            return False
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Condition script for task %s failed: %s", task["id"], exc)
            return False
        output_preview = summarize_log_text(
            (completed.stdout or "") + (completed.stderr or "")
        )
        if completed.returncode != 0:
            logger.info(
                "Condition check task %s not matched (exit=%s%s)",
                task["id"],
                completed.returncode,
                f", output={output_preview}" if output_preview else "",
            )
            condition_log = f"condition is not matched (exit={completed.returncode})"
            full_output = ((completed.stdout or "") + (completed.stderr or "")).strip()
            if full_output:
                condition_log = f"{condition_log}\n{full_output}"
            self.db.record_finished_result(
                int(task["id"]),
                trigger_reason,
                "condition_failed",
                condition_log,
            )
            return False
        logger.info(
            "Condition check task %s matched (exit=%s%s)",
            task["id"],
            completed.returncode,
            f", output={output_preview}" if output_preview else "",
        )
        return True

    def _dependencies_met(self, task):
        return not self._dependency_block_reasons(task)

    def _dependency_block_reasons(self, task):
        deps = task.get("pre_task_ids") or []
        reasons: List[str] = []
        for dep_id in deps:
            dep_task = self.db.get_task(dep_id)
            dep_label = (
                f"{dep_task.get('name')}#{dep_id}" if dep_task else f"task#{dep_id}"
            )
            result = self.db.get_latest_result(dep_id)
            if not result:
                reasons.append(f"{dep_label}=no-result")
                continue
            status = result.get("status") or "unknown"
            if status == "success":
                continue
            finished_at = result.get("finished_at") or result.get("started_at") or "-"
            trigger_reason = result.get("trigger_reason") or "-"
            reasons.append(f"{dep_label}={status}@{finished_at}[{trigger_reason}]")
        return reasons

    def _log_dependency_block(self, task, context):
        reasons = self._dependency_block_reasons(task)
        if not reasons:
            return
        logger.info(
            "Task %s blocked by dependencies during %s: %s",
            task["id"],
            context,
            ", ".join(reasons),
        )

    def _record_dependency_block(self, task, context):
        reasons = self._dependency_block_reasons(task)
        if not reasons:
            return False
        detail = ", ".join(reasons)
        logger.info(
            "Task %s blocked by dependencies during %s: %s",
            task["id"],
            context,
            detail,
        )
        self.db.record_finished_result(
            int(task["id"]),
            context,
            "pretask_failed",
            f"dependencies are not met: {detail}",
        )
        return True

    def _trigger_system_event(self, event_type):
        if event_type not in {EVENT_TYPE_BOOT, EVENT_TYPE_SHUTDOWN}:
            return
        trigger_reason = (
            "system_boot" if event_type == EVENT_TYPE_BOOT else "system_shutdown"
        )
        runners: List[TaskRunner] = []
        for task in self.db.fetch_event_tasks(event_type=event_type):
            if self.db.has_running_instance(task["id"]):
                continue
            if not self._dependencies_met(task):
                self._record_dependency_block(task, trigger_reason)
                continue
            lock_key = ""
            if (task.get("task_kind") or TASK_KIND_SCRIPT) == TASK_KIND_RCLONE:
                lock_key = rclone_mod.lock_key(task.get("rclone") or {}) or ""
                if lock_key and not RcloneTargetLocks.try_acquire(
                    lock_key, f"{task.get('name') or task['id']}"
                ):
                    self.db.record_skipped_result(
                        task["id"],
                        trigger_reason,
                        "跳过：同一 remote 上有写入型任务在跑",
                    )
                    continue
            runner = TaskRunner(
                self.db, task, trigger_reason, self.settings, lock_key=lock_key
            )
            runner.start()
            runners.append(runner)
        if event_type == EVENT_TYPE_BOOT:
            return
        for runner in runners:
            runner.join()

    def check_manual_run_allowed(self, task):
        if (
            task.get("trigger_type") == "event"
            and (task.get("event_type") or EVENT_TYPE_SCRIPT) == EVENT_TYPE_SCRIPT
        ):
            if not task.get("condition_script"):
                logger.info(
                    "Task %s manual trigger skipped because condition script is empty",
                    task["id"],
                )
                return False, "condition"
            if not self._run_condition(task, "manual"):
                logger.info(
                    "Task %s manual trigger skipped because condition is not matched",
                    task["id"],
                )
                return False, "condition"
        if not self._dependencies_met(task):
            self._record_dependency_block(task, "manual")
            return False, "dependencies"
        return True, ""


def acquire_instance_lock(socket_path):
    if fcntl is None:
        return None
    lock_path = Path(f"{socket_path}.lock")
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(  # pyright: ignore[reportAttributeAccessIssue]
            lock_file.fileno(),
            fcntl.LOCK_EX  # pyright: ignore[reportAttributeAccessIssue]
            | fcntl.LOCK_NB,  # pyright: ignore[reportAttributeAccessIssue]
        )
    except (BlockingIOError, OSError):
        lock_file.close()
        raise RuntimeError("another scheduler instance is already running")
    return lock_file


###############################################################################
# HTTP layer
###############################################################################


class SchedulerContext:
    def __init__(self, db, engine, settings):
        self.db = db
        self.engine = engine
        self.settings = settings


def get_context() -> SchedulerContext:
    if CONTEXT is None:
        raise RuntimeError("scheduler context is not initialized")
    return CONTEXT


class ThreadingUnixHTTPServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer,  # pyright: ignore[reportAttributeAccessIssue]
):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        socket_path,
        handler_class,
        *,
        base_path="/",
        www_root=None,
    ):
        self.server_name = APP_NAME
        self.server_port = 0
        self.base_path = normalize_base_path(base_path)
        if www_root is None:
            raise ValueError("www_root is required")
        self.www_root = Path(www_root)
        socket_path = Path(socket_path)
        if socket_path.exists():
            socket_path.unlink()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            str(socket_path), handler_class  # pyright: ignore[reportCallIssue]
        )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    # 实际的 endpoint 分发在模块级 dispatch() 里完成。
    def route(self):
        if not self._require_auth():
            return
        if not self._ensure_base_path():
            return
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api"):
            self.serve_api(parsed.query)
            return
        if self.command == "HEAD":
            self._serve_static(head_only=True)
            return
        self._serve_static()

    def serve_api(self, query):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {key: value for key, value in self.headers.items()}
        with request_context(
            self.command,
            query=query,
            headers=headers,
            body=body,
            handler=self,
        ):
            dispatch()

    def log_message(self, format, *args):
        if not LOG_POLLING_REQUESTS:
            request_line = str(args[0]) if args else ""
            try:
                status_code = int(args[1]) if len(args) > 1 else 0
            except (TypeError, ValueError):
                status_code = 0
            request_path = urlsplit(self.path).path if getattr(self, "path", "") else ""
            request_method = getattr(self, "command", "")
            if (
                (
                    request_path in QUIET_ACCESS_LOG_PATHS
                    or request_line.startswith("GET /api/tasks ")
                    or request_line.startswith("HEAD /api/tasks ")
                    or request_line.startswith("GET /api/health ")
                    or request_line.startswith("HEAD /api/health ")
                    or request_line.startswith("GET /api/tasks/version ")
                    or request_line.startswith("HEAD /api/tasks/version ")
                )
                and request_method in {"GET", "HEAD", ""}
                and 200 <= status_code < 400
            ):
                return
        ca = getattr(self, "client_address", None)
        if isinstance(ca, (list, tuple)) and ca:
            addr = ca[0]
        else:
            addr = ca or "-"
        logger.info("%s - - %s", addr, format % args)

    def _require_auth(self):
        # 鉴权由应用网关/前端完成，后端一律放行。
        return True

    def _ensure_base_path(self):
        base_path = getattr(self.server, "base_path", "/")  # type: ignore[attr-defined]
        if base_path in ("", "/"):
            return True
        parsed = urlsplit(self.path)
        if parsed.path == base_path:
            location = f"{base_path}/"
            if parsed.query:
                location = f"{location}?{parsed.query}"
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", location)
            self.end_headers()
            return False
        if not parsed.path.startswith(base_path):
            self.send_error(HTTPStatus.NOT_FOUND, "Base path mismatch")
            return False
        stripped_path = parsed.path[len(base_path) :] or "/"
        if not stripped_path.startswith("/"):
            stripped_path = f"/{stripped_path}"
        rebuilt = parsed._replace(path=stripped_path)
        self.path = urlunsplit(rebuilt)
        return True

    def _serve_static(self, head_only=False):
        www_root = Path(self.server.www_root)  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        request_path = unquote(parsed.path or "/")
        if request_path in ("", "/"):
            request_path = "/index.html"
        elif request_path.endswith("/"):
            request_path = f"{request_path}index.html"

        rel_path = request_path.lstrip("/")
        target_path = (www_root / rel_path).resolve()
        if not (target_path == www_root or target_path.is_relative_to(www_root)):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not target_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type, _ = mimetypes.guess_type(str(target_path))
        if not content_type:
            content_type = "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type = f"{content_type}; charset=utf-8"

        try:
            file_size = target_path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            static_name = target_path.name
            if static_name == "index.html" or str(target_path).endswith(
                (".js", ".css")
            ):
                cache_control = "no-store"
            else:
                cache_control = "public, max-age=3600"
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            if head_only:
                return
            with open(target_path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError as exc:
            logger.warning("Failed to serve static file %s: %s", target_path, exc)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to read file")


def list_accounts():
    payload = {
        "data": list_allowed_accounts(),
        "meta": {
            "posix_supported": POSIX_ACCOUNT_SUPPORT,
            "default_account": DEFAULT_ACCOUNT_NAME,
        },
    }
    json_response(payload)


def get_settings():
    ctx = get_context()
    json_response({"data": ctx.settings.to_dict()})


def update_settings(payload):
    ctx = get_context()
    updated = ctx.settings.update(payload)
    ctx.db.result_retention_per_task = updated["result_retention_per_task"]
    pruned = ctx.db.prune_all_finished_results()
    json_response({"data": updated, "pruned": pruned})


def list_tasks():
    ctx = get_context()
    tasks = ctx.db.list_tasks()
    for task in tasks:
        task["latest_result"] = ctx.db.get_latest_result(task["id"])
    json_response({"data": tasks})


def get_task(task_id):
    ctx = get_context()
    task = ctx.db.get_task(task_id)
    if not task:
        _send_error(HTTPStatus.NOT_FOUND, "Task not found")
        return
    task["latest_result"] = ctx.db.get_latest_result(task_id)
    json_response(task)


def create_task(payload):
    ctx = get_context()
    _check_backup_allowlist(payload)
    try:
        task = ctx.db.create_task(payload)
    except sqlite3.IntegrityError as exc:
        # Convert DB constraint errors (e.g. unique name) to client 400
        logger.info("create_task integrity error: %s", exc)
        json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    json_response(task, status=HTTPStatus.CREATED)


def update_task(task_id, payload):
    ctx = get_context()
    _check_backup_allowlist(payload)
    try:
        task = ctx.db.update_task(task_id, payload)
    except sqlite3.IntegrityError as exc:
        logger.info("update_task integrity error: %s", exc)
        json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    if not task:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    json_response(task)


def _stop_before_delete(task_id):
    """删任务前先停掉它的进程。

    不这么做会留下两个坑（都是实测踩到的）：
    - 记录被删了，进程还在往目标目录写文件，用户以为「已经删掉了」；
    - 它占着的 remote 锁要等自己跑完才释放，那段时间同名目标的任务全部
      返回 `target_busy`（实测卡了 90 秒才恢复）。
    锁由 TaskRunner 的 finally 释放，所以这里只需要让进程退出即可。
    """
    try:
        TaskRunner.terminate_task_processes(task_id, grace_seconds=1.0)
        TaskRunner.clear_progress(task_id)
    except Exception:  # pylint: disable=broad-except
        logger.exception("failed to stop task %s before deleting it", task_id)


def delete_task(task_id):
    ctx = get_context()
    _stop_before_delete(task_id)
    deleted = ctx.db.delete_task(task_id)
    if not deleted:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    json_response({"deleted": True})


def delete_results(task_id, result_id):
    ctx = get_context()
    deleted = ctx.db.delete_results(task_id, result_id)
    json_response({"deleted": deleted})


def list_templates():
    ctx = get_context()
    json_response({"data": ctx.db.list_templates()})


def export_templates():
    ctx = get_context()
    mapping = ctx.db.export_templates()
    # 返回为原生对象，保持与 templates.json 兼容
    json_response(mapping)


def import_templates(payload):
    ctx = get_context()
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        json_response(
            {"error": "import data should be an object mapping"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    invalid_keys = []
    for k, v in mapping.items():
        if not isinstance(v, dict):
            invalid_keys.append(k)
            continue
        # 必须包含 script_body 字段且为字符串
        if not isinstance(v.get("script_body"), str) or not v.get("script_body"):
            invalid_keys.append(k)
    if invalid_keys:
        json_response(
            {"error": "invalid template entries", "invalid_keys": invalid_keys},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    summary = ctx.db.import_templates(mapping)
    json_response({"imported": summary})


def create_template(payload):
    ctx = get_context()
    tpl = ctx.db.create_template(payload)
    json_response(tpl, status=HTTPStatus.CREATED)


def get_template(tpl_id):
    ctx = get_context()
    tpl = ctx.db.get_template(tpl_id)
    if not tpl:
        _send_error(HTTPStatus.NOT_FOUND, "Template not found")
        return
    json_response(tpl)


def update_template(tpl_id, payload):
    ctx = get_context()
    tpl = ctx.db.update_template(tpl_id, payload)
    if not tpl:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    json_response(tpl)


def delete_template(tpl_id):
    ctx = get_context()
    tpl = ctx.db.delete_template(tpl_id)
    if not tpl:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    json_response({"deleted": True})


def batch_tasks(payload):
    ctx = get_context()
    action = (payload.get("batch_action") or "").strip().lower()
    task_ids_payload = payload.get("task_ids")
    if not isinstance(task_ids_payload, list) or not task_ids_payload:
        raise ValueError("task_ids cannot be empty")
    task_ids = []
    for raw in task_ids_payload:
        try:
            tid = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("task_ids must contain valid task ids") from exc
        if tid > 0 and tid not in task_ids:
            task_ids.append(tid)
    if not task_ids:
        raise ValueError("task_ids must contain valid task ids")

    if action not in {"delete", "enable", "disable", "run", "stop"}:
        raise ValueError("action is not supported")

    result: Dict[str, List[int]] = {"missing": []}

    for task_id in task_ids:
        task = ctx.db.get_task(task_id)
        if not task:
            result.setdefault("missing", []).append(task_id)
            continue

        if action == "delete":
            _stop_before_delete(task_id)
            if ctx.db.delete_task(task_id):
                result.setdefault("deleted", []).append(task_id)
            else:
                result.setdefault("missing", []).append(task_id)
            continue

        if action in {"enable", "disable"}:
            target_state = action == "enable"
            if bool(task["is_active"]) == target_state:
                result.setdefault("unchanged", []).append(task_id)
                continue
            ctx.db.update_task(task_id, {"is_active": target_state})
            result.setdefault("updated", []).append(task_id)
            continue

        if action == "run":
            if ctx.db.has_running_instance(task_id):
                result.setdefault("running", []).append(task_id)
                continue
            allowed, reason = ctx.engine.check_manual_run_allowed(task)
            if not allowed:
                if reason == "condition":
                    result.setdefault("condition_failed", []).append(task_id)
                else:
                    result.setdefault("pretask_failed", []).append(task_id)
                continue
            started, code = ctx.engine.spawn(task, "manual")
            if not started:
                result.setdefault("target_busy", []).append(task_id)
                continue
            if task.get("trigger_type") == "schedule" and task.get(
                "schedule_expression"
            ):
                try:
                    ctx.db.schedule_next_run(task_id, task["schedule_expression"])
                except Exception:
                    logger.exception(
                        "Failed to reschedule task %s after batch manual run",
                        task_id,
                    )
            result.setdefault("queued", []).append(task_id)
            continue

        if action == "stop":
            summary = TaskRunner.terminate_task_processes(task_id)
            if summary["targeted"] > 0 and (
                summary["terminated"] > 0 or summary["killed"] > 0
            ):
                result.setdefault("stopped", []).append(task_id)
            else:
                stale_cleared = 0
                if ctx.db.has_running_instance(task_id):
                    stale_cleared = ctx.db.finalize_stale_running_instances(
                        task_id,
                        reason="stopped by user (no live process found)",
                    )
                if stale_cleared > 0:
                    result.setdefault("stopped", []).append(task_id)
                else:
                    result.setdefault("not_running", []).append(task_id)
            continue

    payload = {"action": action, "result": result}
    json_response(payload)


def run_task(task_id):
    ctx = get_context()
    task = ctx.db.get_task(task_id)
    if not task:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    if ctx.db.has_running_instance(task_id):
        json_response({"error": "task is running"}, status=HTTPStatus.CONFLICT)
        return
    allowed, reason = ctx.engine.check_manual_run_allowed(task)
    if not allowed:
        error_message = (
            "condition is not matched"
            if reason == "condition"
            else "dependencies are not met"
        )
        json_response({"error": error_message}, status=HTTPStatus.BAD_REQUEST)
        return
    # spec-040 R12：试运行单独触发，不写目标端，也不污染正常执行历史
    payload = request_body() or {}
    dry_run = parse_bool_value(payload.get("dry_run"), default=False) or (
        payload.get("trigger") == TRIGGER_DRY_RUN
    )
    trigger = TRIGGER_DRY_RUN if dry_run else "manual"
    started, code = ctx.engine.spawn(task, trigger)
    if not started:
        holder = RcloneTargetLocks.holder(
            rclone_mod.lock_key(task.get("rclone") or {}) or ""
        )
        json_response(
            {
                "error": "target is busy",
                "code": code,
                "hint": f"目标正被「{holder}」使用，等它结束再试",
            },
            status=HTTPStatus.CONFLICT,
        )
        return
    if task.get("trigger_type") == "schedule" and task.get("schedule_expression"):
        try:
            ctx.db.schedule_next_run(task_id, task["schedule_expression"])
        except Exception:
            logger.exception("Failed to reschedule task %s after manual run", task_id)
    json_response({"queued": True, "trigger": trigger})


def stop_task(task_id):
    ctx = get_context()
    task = ctx.db.get_task(task_id)
    if not task:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    summary = TaskRunner.terminate_task_processes(task_id)
    stopped = summary["targeted"] > 0 and (
        summary["terminated"] > 0 or summary["killed"] > 0
    )
    if not stopped and ctx.db.has_running_instance(task_id):
        stale_cleared = ctx.db.finalize_stale_running_instances(
            task_id,
            reason="stopped by user (no live process found)",
        )
        stopped = stale_cleared > 0
    if not stopped:
        json_response(
            {"stopped": False, "reason": "not_running", "summary": summary},
            status=HTTPStatus.CONFLICT,
        )
        return
    json_response({"stopped": True, "summary": summary})


def toggle_task(task_id, payload):
    ctx = get_context()
    task = ctx.db.get_task(task_id)
    if not task:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    is_active = parse_bool_value(
        payload.get("is_active"), default=not task["is_active"]
    )
    updated = ctx.db.update_task(task_id, {"is_active": is_active})
    json_response(updated)


def list_results(task_id, payload):
    ctx = get_context()
    limit = _as_int(payload.get("limit", 50))
    offset = _as_int(payload.get("offset", 0))
    summary_mode = str(payload.get("summary", "0")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    log_limit = _as_int(payload.get("log_limit", ctx.settings.result_log_preview_limit))
    results = ctx.db.fetch_results(task_id, limit=limit, offset=offset)
    payload_list = [
        serialize_result_row(
            row,
            include_log=not summary_mode,
            log_limit=log_limit if summary_mode else None,
        )
        for row in results
    ]
    json_response({"data": payload_list})


def get_result(task_id, result_id):
    ctx = get_context()
    result = ctx.db.fetch_result(task_id, result_id)
    if not result:
        _send_error(HTTPStatus.NOT_FOUND)
        return
    json_response({"data": serialize_result_row(result, include_log=True)})


def fs_target(path):
    """从请求 path 解析到服务器上的目标路径。"""
    if not path:
        path = "/"
    # allow absolute paths; otherwise treat relative to server root
    target = Path(path) if Path(path).is_absolute() else ROOT_DIR / path
    return str(target)


def fs_read(payload):
    target = fs_target(payload.get("path") or "/")
    read_fs(target)


def fs_write(payload):
    target = fs_target(payload.get("path") or "/")
    write_fs(target, payload)


def read_fs(target):
    # 返回文件内容为纯文本
    if not os.path.exists(target):
        _send_error(HTTPStatus.NOT_FOUND, "File not found")
        return
    if not os.path.isfile(target):
        _send_error(HTTPStatus.BAD_REQUEST, "Not a file")
        return
    try:
        # 尝试以文本方式读取
        with open(target, "rb") as fh:
            data = fh.read()
        # 优先按 UTF-8 解码，失败回退到 latin-1 以避免解码错误
        try:
            text = data.decode("utf-8")
        except Exception:
            text = data.decode("latin-1")
        body = text.encode("utf-8")
        handler = current_request().get("handler")
        if handler is not None:
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
    except PermissionError:
        _send_error(HTTPStatus.FORBIDDEN, "Permission denied")
    except Exception as exc:
        logger.exception("read_fs error: %s", exc)
        json_response(
            {"error": "internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR
        )


def write_fs(target, payload):
    # 将 body 中提供的 {"content": "..."} 写入 target 路径
    try:
        if not isinstance(payload, dict) or "content" not in payload:
            json_response({"error": "missing content"}, status=HTTPStatus.BAD_REQUEST)
            return
        content = payload.get("content", "")
        if not isinstance(content, str):
            json_response(
                {"error": "content must be a string"}, status=HTTPStatus.BAD_REQUEST
            )
            return
        parent = os.path.dirname(target) or "/"
        # 确保父目录存在（尝试创建）
        if not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception:
                json_response(
                    {"error": "parent directory missing and cannot be created"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
        # 以 utf-8 写文件
        try:
            with open(target, "wb") as fh:
                fh.write(content.encode("utf-8"))
            json_response({"written": True, "path": target})
        except PermissionError:
            json_response({"error": "permission denied"}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            logger.exception("write_fs error: %s", exc)
            json_response(
                {"error": "internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )
    except Exception as exc:
        logger.exception("write_fs top-level error: %s", exc)
        json_response(
            {"error": "internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR
        )


def health():
    ctx = get_context()
    tasks = ctx.db.list_tasks()
    payload = {
        "time": isoformat(time_now()),
        "task_count": len(tasks),
    }
    json_response(payload)


###############################################################################
# spec-040：rclone 相关接口
###############################################################################


def _check_backup_allowlist(payload):
    """R41：可选的「允许备份的本地根目录」白名单。

    未配置时不限制（默认），配置后源目录必须落在其中之一里。
    """
    allow = get_context().settings.backup_root_allowlist
    if not allow:
        return
    config = payload.get("rclone") or {}
    if not isinstance(config, dict):
        return
    source = str(config.get("source") or "").strip()
    if not source:
        return
    normalized = source.rstrip("/") or "/"
    for root in allow:
        root = root.rstrip("/") or "/"
        if normalized == root or normalized.startswith(root + "/"):
            return
    raise ValueError(
        "源目录不在允许的备份范围里：" + "、".join(allow) + "（可在设置里调整）"
    )


def _run_rclone(args, timeout=20, config_path=None):
    """跑一条**只读**的 rclone 命令，返回 (exit_code, 合并输出)。失败时 exit_code 为 None。

    `config_path` 用来指定**某个账号**的 rclone.conf。remote 是分账号的：不指定就会
    读服务账号（root）的配置，于是「在界面里给 hkx 建好了 remote，任务表单的下拉却还是空的」
    ——2026-11-12 实测踩到，别再把它当默认行为。
    """
    binary = get_context().settings.rclone_bin or "rclone"
    argv = [binary]
    if config_path:
        argv.extend(["--config", config_path])
    argv.extend(list(args))
    try:
        proc = run(
            argv,
            stdout=PIPE,
            stderr=STDOUT,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"找不到 rclone（{binary}），请先在系统上安装或在设置里指定路径"
    except TimeoutExpired:
        return None, f"rclone 响应超时（{timeout}s）"
    except Exception as exc:  # pylint: disable=broad-except
        return None, f"rclone 执行失败：{exc!r}"
    return proc.returncode, (proc.stdout or "").strip()


def _capability_payload():
    detail = {"bin": get_context().settings.rclone_bin, "available": False}
    rc, version_text = _run_rclone(["version"], timeout=10)
    if rc is None:
        detail["message"] = version_text
        return detail
    _help_rc, help_text = _run_rclone(["help", "flags"], timeout=15)
    caps = rclone_mod.probe_capabilities(version_text, help_text or "")
    detail.update(
        {
            "available": True,
            "version": caps["version"],
            "missing": caps["missing"],
            "usable": caps["ok"],
        }
    )
    if not caps["ok"]:
        detail["message"] = "缺少必要参数：" + ", ".join(caps["missing"])
    return detail


def rclone_capability():
    detail = _capability_payload()
    json_response({"ok": bool(detail.get("available")), "rclone": detail})


def list_remotes(payload=None):
    """返回 remote 的**名称与类型**。绝不读取 rclone.conf 里的凭据（R3）。

    给了 `account` 就按**该账号**的配置列（remote 是分账号的，任务按账号执行）；
    没给则退回按服务账号列，仅为兼容旧调用。
    """
    payload = payload or {}
    account = str(payload.get("account") or "").strip()
    detail = _capability_payload()

    if account:
        try:
            text, path = _read_remote_config(account)
        except ValueError as exc:
            json_response(
                {"ok": False, "remotes": [], "rclone": detail, "message": str(exc)}
            )
            return
        remotes = [
            {"name": name, "type": values.get("type", "")}
            for name, values in rclone_mod.parse_config_sections(text).items()
        ]
        remotes.sort(key=lambda item: item["name"].lower())
        message = "" if remotes else (
            "这个账号下还没有 remote：点右边的「管理」新建一个（支持 SMB / WebDAV / SFTP）。"
            "想手工配置的话，请用该账号执行、不要加 sudo——配置会写到 root 下，任务执行时读不到。"
        )
        json_response(
            {
                "ok": True,
                "account": account,
                "path": str(path),
                "remotes": remotes,
                "rclone": detail,
                "message": message,
                "warning": "",
            }
        )
        return

    rc, output = _run_rclone(["listremotes", "--long"])
    if rc is None:
        json_response({"ok": False, "remotes": [], "rclone": detail, "message": output})
        return
    remotes = rclone_mod.parse_remotes(output)
    message = "" if remotes else (
        "还没有配置任何 remote：点右边的「管理」新建一个（支持 SMB / WebDAV / SFTP）。"
        "想手工配置的话，请用该账号执行、不要加 sudo——配置会写到 root 下，任务执行时读不到。"
    )
    json_response(
        {
            "ok": True,
            "remotes": remotes,
            "rclone": detail,
            "message": message,
            "warning": output if rc != 0 else "",
        }
    )


def probe_remote(payload):
    """保存前自检目标是否可达（R4）。

    `account` 给定时用该账号的配置去探——remote 是分账号的，用服务账号（root）的配置
    去探，明明建好了也会报 `remote_not_found`。
    """
    account = str(payload.get("account") or "").strip()
    dest = str(payload.get("dest") or "").strip()
    if dest:
        try:
            remote, path = rclone_mod.validate_dest(dest)
        except rclone_mod.ConfigError as exc:
            json_response({"ok": False, "code": exc.code, "message": exc.message})
            return
    else:
        remote = str(payload.get("remote") or "").strip()
        path = str(payload.get("path") or "").strip()
        if not remote:
            json_response({"ok": False, "code": "invalid_remote", "message": "缺少 remote"})
            return

    config_path = None
    if account:
        try:
            _text, config_file = _read_remote_config(account)
        except ValueError as exc:
            json_response({"ok": False, "code": "invalid_account", "message": str(exc)})
            return
        if config_file.exists():
            config_path = str(config_file)

    target = f"{remote}:{path}"
    rc, output = _run_rclone(
        ["lsd", target, "--max-depth", "1"], timeout=25, config_path=config_path
    )
    if rc is None:
        json_response({"ok": False, "code": "unavailable", "message": output, "target": target})
        return
    result = rclone_mod.classify_probe(rc, output)
    result["target"] = target
    result["exit_code"] = rc
    json_response(result)


# ---------------------------------------------------------------- remote 配置管理（R15）


def _account_home(account):
    """账号的 `(uid, gid, home)`。

    remote 配置必须写在**任务所用账号**的 HOME 下：rclone 按 `$HOME/.config/rclone`
    找配置，服务以 root 跑而任务按账号跑，两者 HOME 不同。这一点在 P1 联调时踩过——
    在 root 下配好 remote，任务执行时照样报 `didn't find section in config file`。
    """
    if not POSIX_ACCOUNT_SUPPORT:
        raise ValueError("当前系统不支持多账号，无法管理 remote 配置")
    name = str(account or "").strip()
    if not name:
        raise ValueError("请选择账号")
    ensure_account_allowed(name)
    try:
        record = pwd.getpwnam(name)  # type: ignore[attr-defined]
    except KeyError as exc:
        raise ValueError(f"账号 {name} 不存在") from exc
    return record.pw_uid, record.pw_gid, record.pw_dir


def _remote_config_file(account):
    uid, gid, home = _account_home(account)
    return Path(home) / ".config" / "rclone" / "rclone.conf", uid, gid


def _ensure_owned(path, uid, gid):
    """把新建出来的目录/文件交给任务账号（非 root 时只能跳过）。"""
    try:
        os.chown(path, uid, gid)  # pyright: ignore[reportAttributeAccessIssue]
    except (AttributeError, PermissionError, OSError):
        pass


def _read_remote_config(account):
    path, _uid, _gid = _remote_config_file(account)
    try:
        return path.read_text(encoding="utf-8"), path
    except FileNotFoundError:
        return "", path
    except OSError as exc:
        raise ValueError(f"读取 rclone 配置失败：{exc}") from exc


def _write_remote_config(account, text):
    """写回 rclone.conf：临时文件 + 原子替换，权限 600、属主交给任务账号。

    属主必须是对的：服务是 root，`mkdir` 出来的目录默认归 root，不 chown 的话
    任务账号读不到自己的配置（P1 联调时就是这个坑的变体）。
    """
    path, uid, gid = _remote_config_file(account)
    cfg_dir = path.parent
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        _ensure_owned(cfg_dir, uid, gid)
        os.chmod(cfg_dir, 0o700)
        if cfg_dir.parent.name == ".config":
            _ensure_owned(cfg_dir.parent, uid, gid)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        _ensure_owned(tmp, uid, gid)
        os.replace(tmp, path)
    except OSError as exc:
        raise ValueError(f"写入 rclone 配置失败：{exc}") from exc
    return path


def _obscure_password(plain):
    """明文密码 → rclone obscure 密文。

    密码走 **stdin**（`rclone obscure -`），所以不会出现在进程列表或日志里。
    rclone 只接受密文：直接写明文，认证时会 `couldn't decrypt password`。
    """
    binary = get_context().settings.rclone_bin or "rclone"
    argv = rclone_mod.obscure_argv(binary)
    try:
        proc = run(argv, input=plain, text=True, capture_output=True, timeout=20)
    except FileNotFoundError as exc:
        raise ValueError(f"找不到 rclone（{binary}），无法加密密码") from exc
    except TimeoutExpired as exc:
        raise ValueError("rclone obscure 超时") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        raise ValueError("rclone obscure 失败" + (f"：{detail}" if detail else ""))
    result = (proc.stdout or "").strip()
    if not rclone_mod.is_obscured(result):
        raise ValueError("rclone obscure 返回了非预期结果")
    return result


def remote_schema():
    """给前端用的后端定义与可选账号（不含任何用户数据）。"""
    json_response(
        {
            "ok": True,
            "types": rclone_mod.remote_type_specs(),
            "accounts": list_allowed_accounts(),
            "secret_mask": rclone_mod.SECRET_MASK,
        }
    )


def list_remote_configs(payload):
    account = str(payload.get("account") or "").strip()
    try:
        text, path = _read_remote_config(account)
    except ValueError as exc:
        json_response({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    remotes = []
    for name, values in rclone_mod.parse_config_sections(text).items():
        kind = values.get("type", "")
        remotes.append(
            {
                "name": name,
                "type": kind,
                "values": rclone_mod.mask_config(values),
                "supported": kind in rclone_mod.REMOTE_TYPES,
            }
        )
    remotes.sort(key=lambda item: item["name"].lower())
    json_response(
        {"ok": True, "account": account, "path": str(path), "remotes": remotes}
    )


def save_remote(payload):
    account = str(payload.get("account") or "").strip()
    try:
        name = rclone_mod.validate_remote_name(payload.get("name"))
        params = rclone_mod.normalize_remote_params(
            payload.get("type"), payload.get("params")
        )
        text, _path = _read_remote_config(account)
    except (ValueError, rclone_mod.ConfigError) as exc:
        json_response({"ok": False, "error": getattr(exc, "message", str(exc))}, HTTPStatus.BAD_REQUEST)
        return

    existing = rclone_mod.parse_config_sections(text).get(name, {})
    spec = rclone_mod.REMOTE_TYPES.get(params["type"], {"fields": []})
    secret_fields = [field["name"] for field in spec["fields"] if field.get("secret")]
    try:
        for field_name in secret_fields:
            value = params.get(field_name)
            if value == rclone_mod.SECRET_MASK:
                # 回传打码值 = 「不修改」，沿用文件里的原值（可能已是密文）
                params[field_name] = existing.get(field_name, "")
            elif value:
                params[field_name] = _obscure_password(value)
    except ValueError as exc:
        json_response({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return

    new_text = rclone_mod.upsert_section(text, name, params)
    try:
        path = _write_remote_config(account, new_text)
    except ValueError as exc:
        json_response({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return

    logger.info("remote %s saved for account %s (%s)", name, account, path)
    json_response(
        {
            "ok": True,
            "name": name,
            "type": params["type"],
            "values": rclone_mod.mask_config(params),
            "path": str(path),
        }
    )


def delete_remote(payload):
    account = str(payload.get("account") or "").strip()
    try:
        name = rclone_mod.validate_remote_name(payload.get("name"))
        text, _path = _read_remote_config(account)
    except (ValueError, rclone_mod.ConfigError) as exc:
        json_response({"ok": False, "error": getattr(exc, "message", str(exc))}, HTTPStatus.BAD_REQUEST)
        return

    new_text, removed = rclone_mod.remove_section(text, name)
    if not removed:
        json_response(
            {"ok": False, "error": f"remote {name} 不存在"}, HTTPStatus.NOT_FOUND
        )
        return
    try:
        _write_remote_config(account, new_text)
    except ValueError as exc:
        json_response({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return
    logger.info("remote %s deleted for account %s", name, account)
    json_response({"ok": True, "removed": True, "name": name})


def fs_list(payload):
    """列目录（spec-040 R2）。

    现有 `fs-read` 只读**文件内容**，没有目录列举，所以这是新增能力。
    只接受绝对路径、拒绝 `..`：服务以 root 运行，路径校验必须在服务端做（R42）。
    """
    raw = str(payload.get("path") or "/").strip()
    if not raw.startswith("/"):
        json_response({"ok": False, "error": "path must be absolute"}, status=HTTPStatus.BAD_REQUEST)
        return
    if ".." in raw.split("/"):
        json_response(
            {"ok": False, "error": "path traversal is not allowed"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    path = "/" + "/".join(seg for seg in raw.split("/") if seg not in ("", "."))
    if not os.path.isdir(path):
        json_response(
            {"ok": False, "error": "directory not found", "path": path},
            status=HTTPStatus.NOT_FOUND,
        )
        return

    dirs_only = parse_bool_value(payload.get("dirs_only"), default=True)
    include_hidden = parse_bool_value(payload.get("include_hidden"), default=False)
    limit = 500
    entries = []
    truncated = False
    try:
        with os.scandir(path) as iterator:
            for item in iterator:
                if len(entries) >= limit:
                    truncated = True
                    break
                if not include_hidden and item.name.startswith("."):
                    continue
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if dirs_only and not is_dir:
                    continue
                try:
                    stat_result = item.stat(follow_symlinks=False)
                    size = int(stat_result.st_size)
                    modified = int(stat_result.st_mtime)
                except OSError:
                    size, modified = 0, 0
                entries.append(
                    {
                        "name": item.name,
                        "path": os.path.join(path, item.name),
                        "is_dir": is_dir,
                        "size": size,
                        "mtime": modified,
                    }
                )
    except PermissionError:
        json_response(
            {"ok": False, "error": "permission denied", "path": path},
            status=HTTPStatus.FORBIDDEN,
        )
        return

    entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
    parent = None if path == "/" else (os.path.dirname(path) or "/")
    json_response(
        {
            "ok": True,
            "path": path,
            "parent": parent,
            "entries": entries,
            "truncated": truncated,
        }
    )


def report_status():
    ctx = get_context()
    settings = ctx.settings
    stats = ctx.db.report_queue_stats()
    json_response(
        {
            "ok": True,
            "configured": bool(settings.ha_base_url and settings.ha_token),
            "base_url": settings.ha_base_url,
            "token_set": bool(settings.ha_token),
            "mode": settings.report_mode,
            "retry_seconds": settings.report_retry_seconds,
            "max_attempts": settings.report_max_attempts,
            **stats,
        }
    )


def task_progress(payload):
    """R16：某条任务当前的执行进度。

    进度只活在内存里（见 `TaskRunner._progress`）：任务结束或服务重启就没了，
    那时 `running=false`、`progress=null`，前端据此停止轮询。
    """
    ctx = get_context()
    try:
        task_id = int(payload.get("id"))
    except (TypeError, ValueError):
        json_response({"ok": False, "error": "invalid task id"}, HTTPStatus.BAD_REQUEST)
        return
    running = bool(ctx.db.has_running_instance(task_id))
    json_response(
        {
            "ok": True,
            "id": task_id,
            "running": running,
            "progress": TaskRunner.get_progress(task_id),
        }
    )


def report_test():
    ctx = get_context()
    if REPORTER is None:
        json_response({"ok": False, "message": "上报线程未启动"})
        return
    moment = isoformat(time_now())
    payload = {
        "job_key": "fn-scheduler-selftest",
        "run_id": f"selftest-{int(time.time())}",
        "name": "fn-scheduler 自检",
        "kind": "script",
        "status": "success",
        "started_at": moment,
        "finished_at": moment,
        "trigger": "test",
        "summary": "这是一条来自 fn-scheduler 设置页的测试事件",
        "stats": {},
        "target": "",
        "error_excerpt": "",
        "log_tail": "",
    }
    ok, message = REPORTER.send_now(payload)
    json_response({"ok": ok, "message": message})


def dispatch():
    """Endpoint 分发（/api/*）。后端：POST JSON body 携带 action 分发。
    从 request_context 取当前 handler，再由 action 路由到对应处理函数。"""
    handler = current_request().get("handler")
    if handler is None:
        json_response({"error": "no request handler"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return
    try:
        payload = request_body() or {}
        action = payload.get("action")
        if not action:
            json_response({"error": "missing action"}, HTTPStatus.BAD_REQUEST)
            return
        if action == "health":
            health()
        elif action == "list-accounts":
            list_accounts()
        elif action == "get-settings":
            get_settings()
        elif action == "update-settings":
            update_settings(payload)
        elif action == "list-tasks":
            list_tasks()
        elif action == "create-task":
            create_task(payload)
        elif action == "get-task":
            get_task(_as_int(payload.get("id")))
        elif action == "update-task":
            update_task(_as_int(payload.get("id")), payload)
        elif action == "delete-task":
            delete_task(_as_int(payload.get("id")))
        elif action == "run-task":
            run_task(_as_int(payload.get("id")))
        elif action == "stop-task":
            stop_task(_as_int(payload.get("id")))
        elif action == "toggle-task":
            toggle_task(_as_int(payload.get("id")), payload)
        elif action == "batch-tasks":
            batch_tasks(payload)
        elif action == "list-results":
            list_results(_as_int(payload.get("id")), payload)
        elif action == "get-result":
            get_result(_as_int(payload.get("id")), _as_int(payload.get("result_id")))
        elif action == "delete-results":
            result_id = payload.get("result_id")
            delete_results(
                _as_int(payload.get("id")),
                _as_int(result_id) if result_id not in (None, "") else None,
            )
        elif action == "clear-results":
            delete_results(_as_int(payload.get("id")), None)
        elif action == "list-templates":
            list_templates()
        elif action == "get-template":
            get_template(_as_int(payload.get("id")))
        elif action == "create-template":
            create_template(payload)
        elif action == "update-template":
            update_template(_as_int(payload.get("id")), payload)
        elif action == "delete-template":
            delete_template(_as_int(payload.get("id")))
        elif action == "export-templates":
            export_templates()
        elif action == "import-templates":
            import_templates(payload)
        elif action == "fs-read":
            fs_read(payload)
        elif action == "fs-write":
            fs_write(payload)
        # ---- spec-040 ----
        elif action == "list-remotes":
            list_remotes(payload)
        elif action == "rclone-capability":
            rclone_capability()
        elif action == "probe-remote":
            probe_remote(payload)
        elif action == "fs-list":
            fs_list(payload)
        elif action == "report-status":
            report_status()
        elif action == "report-test":
            report_test()
        elif action == "task-progress":
            task_progress(payload)
        elif action == "remote-schema":
            remote_schema()
        elif action == "list-remote-configs":
            list_remote_configs(payload)
        elif action == "save-remote":
            save_remote(payload)
        elif action == "delete-remote":
            delete_remote(payload)
        else:
            json_response(
                {"ok": False, "error": f"unsupported action: {action}"},
                HTTPStatus.BAD_REQUEST,
            )
    except ValueError as exc:
        json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("API error: %s", exc)
        json_response(
            {"ok": False, "error": "internal server error"},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid id: {value}") from exc


###############################################################################
# Entrypoint
###############################################################################


def main():
    parser = argparse.ArgumentParser(description="Scheduler Service")
    parser.add_argument("--unix-socket", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--base-path", default="/")
    # 不设必填：fnOS 应用中心拉起本应用时只传 socket/db/settings 三个参数，
    # 把 www-root 做成必填会让「从应用中心启动」直接失败（实测 error: the following
    # arguments are required: --www-root）。默认取 server 同级的 www/，与本应用的安装
    # 布局一致；显式传入仍然优先。
    parser.add_argument("--www-root", default=None)
    args = parser.parse_args()

    db_path = str(Path(strip_wrapping_quotes(args.db)))
    settings_path = str(Path(strip_wrapping_quotes(args.settings)))
    www_root = str(
        Path(strip_wrapping_quotes(args.www_root))
        if args.www_root
        else Path(__file__).resolve().parent.parent / "www"
    )
    normalized_base = normalize_base_path(args.base_path)
    socket_path = Path(strip_wrapping_quotes(args.unix_socket))
    instance_lock = acquire_instance_lock(socket_path)

    settings = SchedulerSettings(settings_path)
    database = Database(
        db_path, result_retention_per_task=settings.result_retention_per_task
    )
    engine = SchedulerEngine(database, settings)
    ctx = SchedulerContext(database, engine, settings)

    global REPORTER
    REPORTER = report_mod.HaReporter(database, settings)

    httpd = ThreadingUnixHTTPServer(
        socket_path,
        Handler,
        base_path=normalized_base,
        www_root=Path(www_root),
    )
    global CONTEXT
    CONTEXT = ctx

    def _shutdown(_signum, _frame):
        logger.info("Received signal %s, shutting down scheduler...", _signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            signal.signal(getattr(signal, sig_name), _shutdown)

    logger.info(
        "Starting scheduler on http+unix://%s%s (db=%s, www=%s)",
        socket_path,
        normalized_base,
        db_path,
        www_root,
    )
    try:
        engine.start()
        if REPORTER is not None:
            REPORTER.start()
            logger.info(
                "上报线程已启动（mode=%s, ha=%s）",
                settings.report_mode,
                settings.ha_base_url or "未配置",
            )
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down scheduler...")
    finally:
        if REPORTER is not None:
            REPORTER.stop()
        httpd.server_close()
        engine.stop()
        database.close()
        if socket_path.exists():
            try:
                socket_path.unlink()
            except OSError:
                pass
        if instance_lock is not None:
            instance_lock.close()


if __name__ == "__main__":
    main()
