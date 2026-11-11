# -*- coding: utf-8 -*-
"""rclone 能力探测、参数编译与执行结果解析（spec-040）。

这个模块**刻意只依赖标准库**，也不 import 本工程其他模块。理由：
argv 怎么拼、退出码怎么读、帮助文本里有哪些 flag，这些判断全都能在裸解释器里
用 `python -m unittest` 验完，不需要 fnOS、不需要 HA、更不需要真跑一次 rclone。

设计上的三条硬约束（对应 spec-040 的 R5 / R7 / R8）：

1. **只产出 argv 数组**，绝不拼 shell 字符串。路径里的空格、中文、`$()` 都只是
   一个字符串元素，`Popen(argv, shell=False)` 不会再解释一遍。
2. **非法参数在编译期就抛**，不进 argv。宁可保存时失败，也不要半夜跑到一半发现
   `--bwlimit` 写错。
3. **解析不到就给 None**，不填 0。rclone 的统计输出跨版本有差异，填 0 会让
   "没统计到" 和 "真的传了 0 个" 混为一谈。
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 执行模式：sync / copy / check
MODE_SYNC = "sync"
MODE_COPY = "copy"
MODE_CHECK = "check"
MODES = (MODE_SYNC, MODE_COPY, MODE_CHECK)

# 会写目标端的模式（需要按 remote 加锁、需要算「删了多少」）
WRITE_MODES = (MODE_SYNC, MODE_COPY)

# 会删除目标端多余文件的模式（必须二次确认，见 R6）
DESTRUCTIVE_MODES = (MODE_SYNC,)

# 结果状态（与上报 status 一一对应）
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# rclone 退出码语义（https://rclone.org/docs/#exit-code）
#   0 成功
#   1 语法/用法错误
#   2 未分类错误
#   3 目录不存在
#   4 文件不存在
#   5 临时错误（重试后仍失败，常意味着个别文件失败但整体跑完）
#   6 较小错误（同上）
#   7 致命错误
#   8 传输超过上限
#   9 成功，但没有文件需要传
EXIT_PARTIAL = frozenset({5, 6})
EXIT_NOOP_SUCCESS = frozenset({9})

# 编译 argv 时默认带上的统计参数。
#
# 两条都是实测出来的，不要凭文档改回去：
#
# 1. **不要加 `--stats-one-line`**。它会把统计压成
#    `NOTICE:  1.907 MiB / 1.907 MiB, 100%, 0 B/s, ETA -` 一行，
#    **文件数 `Transferred: 3 / 3`、`Errors:`、`Elapsed time:` 全部消失**，
#    而这三样正是 R8 要求的结构化结果。实测于 fnOS `/usr/trim/bin/rclone`。
# 2. `--stats-log-level NOTICE` 必须有：统计块默认走 INFO，不加它默认 verbosity 下
#    压根不输出，解析就永远是 None。
#
# `--stats 30s` 是给长时间任务降噪用的：最后一次统计块无论如何都会打印，
# 中间块打太密只会把 4000 字的日志预览挤掉。
DEFAULT_STATS_ARGS = ("--stats", "30s", "--stats-log-level", "NOTICE")

# 能力探测要求的 flag（R11：不比版本号，比能力）
DEFAULT_REQUIRED_FLAGS = ("--stats-log-level", "--backup-dir", "--bwlimit", "--exclude", "--include")

_SIZE_UNITS = {
    "B": 1,
    "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4, "PIB": 1024 ** 5,
    "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4, "PB": 1000 ** 5,
}

_SIZE_TOKEN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]i?B|B)\b", re.IGNORECASE)
# 用户输入的大小：rclone 的 SizeSuffix 接受 `1M` / `1Mi` / `1MiB` / `1MB` / `512K` / `0`，
# 不带 B 后缀也要放行（`--min-size 1M` 是官方文档里的写法）。
_SIZE_INPUT = re.compile(r"^[0-9]+(?:\.[0-9]+)?\s?(?:[KMGTPE](?:i)?(?:B)?|B)?$", re.IGNORECASE)
# 耗时里的数字可能是小数：实测输出是 `Elapsed time: 1m2.5s`，不带小数位会解析成 65s。
_DURATION_TOKEN = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d|w|M|y)")
_CLOCK_DURATION = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")

_REMOTE_NAME = re.compile(r"^[A-Za-z0-9_.\- ]+$")


class ConfigError(ValueError):
    """配置非法。`code` 供接口层翻译成机器可读错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------- 版本与能力


def parse_version(version_text: str) -> Optional[str]:
    """从 `rclone version` 的输出里取版本串。

    实测本机报 `v3.67.8`（fnOS 自带的重新打包版，并非官方版本序列），所以这里只做
    **展示用途**，任何逻辑判断都不许拿它比大小。
    """
    match = re.search(r"^rclone\s+(v[0-9][0-9A-Za-z.\-+]*)", version_text or "", re.M)
    if match:
        return match.group(1)
    match = re.search(r"\b(v\d+\.\d+\.\d+[\w.\-+]*)", version_text or "")
    return match.group(1) if match else None


def parse_flag_help(help_text: str) -> List[str]:
    """从 `rclone help flags` 的输出里抽出所有长参数名。

    行形如：

         -n, --dry-run         Do a trial run with no permanent changes
             --stats-log-level LogLevel            Log level to show --stats output

    **只能看参数列**（第一段），不能扫整行：否则描述里的 `--stats` 会被当成一个真实参数，
    能力探测就此产生假阳性。
    """
    flags: List[str] = []
    for line in (help_text or "").splitlines():
        if "--" not in line:
            continue
        # 参数与描述之间是 2 个以上空格；参数与取值之间只有 1 个空格
        column = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
        for token in re.findall(r"--[A-Za-z0-9][A-Za-z0-9\-]*", column):
            if token not in flags:
                flags.append(token)
    return flags


def probe_capabilities(
    version_text: str,
    help_text: str,
    required: Iterable[str] = DEFAULT_REQUIRED_FLAGS,
) -> Dict[str, Any]:
    """能力探测结果。`ok` 只看 required 是否齐备，不看版本号。"""
    flags = parse_flag_help(help_text)
    have = set(flags)
    missing = [flag for flag in required if flag not in have]
    return {
        "version": parse_version(version_text),
        "flags": flags,
        "missing": missing,
        "ok": not missing,
    }


# ---------------------------------------------------------------- remote


def parse_remotes(list_output: str) -> List[Dict[str, str]]:
    """解析 `rclone listremotes --long`。

    输出形如：

        onedrive:      onedrive
        my smb share:  smb

    也可能只有 `name:`（不带 `--long`）。**只取名字与类型**，绝不碰凭据。
    """
    remotes: List[Dict[str, str]] = []
    for raw in (list_output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if not name:
            continue
        kind = rest.strip().split()[0] if rest.strip() else ""
        remotes.append({"name": name, "type": kind})
    return remotes


def remote_of(dest: str) -> str:
    """从 `remote:path` 取出 remote 名（并发锁的 key）。"""
    if ":" not in (dest or ""):
        raise ConfigError("invalid_dest", "目标必须写成「remote:路径」的形式，例如 onedrive:Photos")
    return dest.split(":", 1)[0].strip()


def validate_dest(dest: Any) -> Tuple[str, str]:
    """校验并拆成 (remote, path)。path 允许为空（表示 remote 根）。"""
    text = _text(dest)
    if not text:
        raise ConfigError("missing_dest", "缺少目标（remote:路径）")
    if ":" not in text:
        raise ConfigError("invalid_dest", "目标必须写成「remote:路径」的形式，例如 onedrive:Photos")
    remote, _, path = text.partition(":")
    remote = remote.strip()
    if not remote:
        raise ConfigError("invalid_dest", "目标缺少 remote 名")
    if not _REMOTE_NAME.match(remote):
        raise ConfigError("invalid_remote", "remote 名只允许字母、数字、空格和 _ . -")
    path = path.strip()
    if ".." in path.replace("\\", "/").split("/"):
        raise ConfigError("path_traversal", "目标路径不允许包含 ..")
    if "\n" in path or "\r" in path:
        raise ConfigError("invalid_dest", "目标路径不允许换行")
    return remote, path


def validate_source(source: Any) -> str:
    """校验本地源目录。

    - 必须绝对路径；
    - 不允许 `..` 段（R40）；
    - **不接受 `/`**：对根目录做 sync 等于把整机往网盘推，也等于可能清空目标，
      这种输入只可能是误操作。
    """
    text = _text(source)
    if not text:
        raise ConfigError("missing_source", "缺少源目录")
    normalized = text.replace("\\", "/")
    if not normalized.startswith("/"):
        raise ConfigError("invalid_source", "源目录必须是绝对路径")
    if "\n" in normalized or "\r" in normalized:
        raise ConfigError("invalid_source", "源目录不允许换行")
    parts = [seg for seg in normalized.split("/") if seg not in ("", ".")]
    if ".." in parts:
        raise ConfigError("path_traversal", "源目录不允许包含 ..")
    if not parts:
        raise ConfigError("invalid_source", "不允许以 / 作为源目录")
    return "/" + "/".join(parts)


def validate_backup_dir(value: Any) -> str:
    """`--backup-dir` 既可以是本地路径也可以是 remote:path，校验到「没有换行、没有 ..」即可。"""
    text = _text(value)
    if not text:
        raise ConfigError("invalid_backup_dir", "备份目录不能为空")
    normalized = text.replace("\\", "/")
    if "\n" in normalized or "\r" in normalized:
        raise ConfigError("invalid_backup_dir", "备份目录不允许换行")
    if ".." in normalized.split("/"):
        raise ConfigError("path_traversal", "备份目录不允许包含 ..")
    return text


def _validate_bwlimit(value: Any) -> str:
    """带宽限制。支持 `10M`、`1.5M`、`10M:1M` 这类时间表写法，但拒绝空白与 shell 元字符。"""
    text = _text(value)
    if not text:
        raise ConfigError("invalid_bwlimit", "带宽限制不能为空")
    if len(text) > 64 or re.search(r"[\s;|&$`'\"<>]", text):
        raise ConfigError("invalid_bwlimit", "带宽限制格式非法（示例：10M、1.5M、10M:1M）")
    if not re.match(r"^[0-9][0-9A-Za-z.:,]*$", text):
        raise ConfigError("invalid_bwlimit", "带宽限制格式非法（示例：10M、1.5M、10M:1M）")
    return text


def _validate_int(value: Any, code: str, label: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(code, "%s 必须是整数" % label) from exc
    if number < minimum or number > maximum:
        raise ConfigError(code, "%s 必须在 %d~%d 之间" % (label, minimum, maximum))
    return number


def _validate_patterns(values: Any, code: str, label: str) -> List[str]:
    if values in (None, "", []):
        return []
    if isinstance(values, str):
        values = values.splitlines()
    if not isinstance(values, (list, tuple)):
        raise ConfigError(code, "%s 必须是字符串数组" % label)
    cleaned: List[str] = []
    for item in values:
        pattern = _text(item)
        if not pattern:
            continue
        if len(pattern) > 512:
            raise ConfigError(code, "%s 单条最长 512 字符" % label)
        if "\n" in pattern or "\r" in pattern:
            raise ConfigError(code, "%s 不允许换行" % label)
        cleaned.append(pattern)
    return cleaned


def _validate_max_age(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if not re.match(r"^(?:\d+(?:ms|s|m|h|d|w|M|y))+$", text):
        raise ConfigError("invalid_max_age", "文件年龄上限格式非法（示例：30d、1h30m）")
    return text


def _validate_min_size(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if not _SIZE_INPUT.fullmatch(text):
        raise ConfigError("invalid_min_size", "最小文件大小格式非法（示例：1M、512K）")
    return text


def _validate_links(value: Any) -> str:
    text = _text(value)
    if not text or text == "skip":
        return "skip"
    if not re.match(r"^(?:translate|include)(?::[0-9]+)?$", text):
        raise ConfigError("invalid_links", "软链接策略只接受 skip / translate[:N] / include[:N]")
    return text


# ---------------------------------------------------------------- argv 编译


def build_argv(
    config: Dict[str, Any],
    rclone_bin: str = "rclone",
    dry_run: bool = False,
) -> List[str]:
    """把结构化配置编译成 argv。

    `config` 期望字段（都可缺省，缺省即不传该参数）：

        mode        sync | copy | check（必填）
        source      绝对路径（必填）
        dest        remote:path（必填）
        bwlimit     "10M" / "1.5M" / "10M:1M"
        transfers   1..64
        checkers    1..64
        exclude     [str, ...]
        include     [str, ...]
        max_age     "30d"
        min_size    "1M"
        links       "skip" | "translate" | "translate:1" | ...
        backup_dir  本地路径或 remote:path
        dry_run     bool（也可用函数参数覆盖）

    返回值**只包含字符串元素**，直接喂给 `Popen(argv, shell=False)`。
    """
    if not isinstance(config, dict):
        raise ConfigError("invalid_config", "rclone 配置必须是对象")

    mode = _text(config.get("mode")).lower()
    if mode not in MODES:
        raise ConfigError("invalid_mode", "执行模式只接受 sync / copy / check")

    source = validate_source(config.get("source"))
    dest = _text(config.get("dest"))
    validate_dest(dest)

    binary = _text(rclone_bin) or "rclone"
    if re.search(r"[\s;|&$`'\"<>]", binary):
        raise ConfigError("invalid_bin", "rclone 路径不能包含空白或 shell 元字符")

    argv: List[str] = [binary, mode, source, dest]

    bwlimit = _text(config.get("bwlimit"))
    if bwlimit:
        argv.extend(["--bwlimit", _validate_bwlimit(bwlimit)])

    if config.get("transfers") not in (None, ""):
        argv.extend(
            ["--transfers", str(_validate_int(config.get("transfers"), "invalid_transfers", "并发传输数", 1, 64))]
        )

    if config.get("checkers") not in (None, ""):
        argv.extend(
            ["--checkers", str(_validate_int(config.get("checkers"), "invalid_checkers", "校验并发数", 1, 64))]
        )

    for pattern in _validate_patterns(config.get("exclude"), "invalid_exclude", "排除规则"):
        argv.extend(["--exclude", pattern])
    for pattern in _validate_patterns(config.get("include"), "invalid_include", "包含规则"):
        argv.extend(["--include", pattern])

    max_age = _validate_max_age(config.get("max_age"))
    if max_age:
        argv.extend(["--max-age", max_age])

    min_size = _validate_min_size(config.get("min_size"))
    if min_size:
        argv.extend(["--min-size", min_size])

    if config.get("links") not in (None, ""):
        links = _validate_links(config.get("links"))
        # rclone 的 --links 是布尔开关，取值靠 `--links` / `--translate-links` 区分；
        # 这里只用最保守的写法：显式 skip 表示不带该参数。
        if links == "skip":
            argv.append("--links=false")
        elif links == "translate" or links.startswith("translate:"):
            argv.append("--links")
        else:
            argv.append("--links")

    backup_dir = _text(config.get("backup_dir"))
    if backup_dir:
        argv.extend(["--backup-dir", validate_backup_dir(backup_dir)])

    if config.get("dry_run") or dry_run:
        argv.append("--dry-run")

    argv.extend(DEFAULT_STATS_ARGS)
    return argv


def is_destructive(config: Dict[str, Any], dry_run: bool = False) -> bool:
    """这条配置会不会删目标端文件。`sync` 且不是试运行才算。"""
    if dry_run or (isinstance(config, dict) and config.get("dry_run")):
        return False
    return _text((config or {}).get("mode")).lower() in DESTRUCTIVE_MODES


def lock_key(config: Dict[str, Any]) -> Optional[str]:
    """并发锁的 key = remote 名。check 模式不写目标，返回 None 表示不需要加锁。"""
    mode = _text((config or {}).get("mode")).lower()
    if mode not in WRITE_MODES:
        return None
    try:
        return remote_of(_text(config.get("dest")))
    except ConfigError:
        return None


# ---------------------------------------------------------------- 结果解析


def classify_exit(code: Optional[int]) -> str:
    """退出码 → 三档状态。非 0 一律 failed 是不可接受的：会淹没真实故障。"""
    if code is None:
        return STATUS_FAILED
    if code == 0 or code in EXIT_NOOP_SUCCESS:
        return STATUS_SUCCESS
    if code in EXIT_PARTIAL:
        return STATUS_PARTIAL
    return STATUS_FAILED


def _to_bytes(number: str, unit: str) -> Optional[int]:
    multiplier = _SIZE_UNITS.get(unit.upper())
    if multiplier is None:
        return None
    try:
        return int(float(number) * multiplier)
    except (TypeError, ValueError):
        return None


def _last_match(pattern: re.Pattern, text: str):
    found = None
    for found in pattern.finditer(text):
        pass
    return found


def _parse_elapsed(text: str) -> Optional[float]:
    clock = _last_match(re.compile(r"Elapsed time:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)"), text)
    if clock:
        hours, minutes, seconds = int(clock.group(1)), int(clock.group(2)), float(clock.group(3))
        return round(hours * 3600 + minutes * 60 + seconds, 1)

    tail = re.search(r"Elapsed time:\s*([0-9].*)$", text, re.M)
    if not tail:
        return None
    total = 0.0
    matched = False
    for number, unit in _DURATION_TOKEN.findall(tail.group(1)):
        matched = True
        factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit)
        if factor is None:  # M / y 不在耗时里出现，跳过
            continue
        total += float(number) * factor
    return round(total, 1) if matched else None


def parse_stats(text: str) -> Dict[str, Optional[int]]:
    """从 rclone 输出里抽统计。

    只取**最后一条**：运行中每 `--stats` 秒打一块，最后一块才是终值。

    两条实测语义（fnOS rclone 输出，别按直觉改）：

    - rclone 的统计块**会把值为 0 的行整行省掉**：成功跑完时根本没有 `Errors:` 这一行。
      所以「有统计块但没有 Errors」= 0 个错误，而「连统计块都没有」= 不知道（None）。
      这两种情况必须区分，否则失败时会被误读成 0 错误。
    - `--stats-one-line` 下没有文件数与耗时行，所以本模块不带那个参数（见 DEFAULT_STATS_ARGS）。

    返回：files / bytes / errors / deleted / elapsed_seconds
    """
    payload = text or ""

    # 先判断有没有统计块。`Elapsed time:` 是块尾标记，正常结束时一定有。
    has_block = bool(re.search(r"(?:^|\s)(?:Transferred|Checks|Elapsed time):", payload, re.M))

    files: Optional[int] = None
    matched = _last_match(re.compile(r"Transferred:\s+(\d+)\s*/\s*(\d+)"), payload)
    if matched:
        files = int(matched.group(1))

    byte_count: Optional[int] = None
    # 不锚定行首标签：统计块里字节行是 `Transferred:  \t  1.907 MiB / 1.907 MiB, 100%`，
    # 中间那个 tab 会挡住部分正则写法。
    matched = _last_match(
        re.compile(
            r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]i?B|B)\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]i?B|B)\b",
            re.IGNORECASE,
        ),
        payload,
    )
    if matched:
        byte_count = _to_bytes(matched.group(1), matched.group(2))

    errors: Optional[int] = None
    matched = _last_match(re.compile(r"Errors:\s+(\d+)"), payload)
    if matched:
        errors = int(matched.group(1))
    elif has_block:
        errors = 0  # rclone 只在非 0 时打印这一行

    deleted: Optional[int] = None
    matched = _last_match(re.compile(r"Deleted:\s+(\d+)"), payload)
    if matched:
        deleted = int(matched.group(1))
    elif has_block:
        deleted = 0

    return {
        "files": files,
        "bytes": byte_count,
        "errors": errors,
        "deleted": deleted,
        "elapsed_seconds": _parse_elapsed(payload),
    }


def summarize(stats: Dict[str, Optional[int]], status: str) -> str:
    """给上报用的中文摘要。缺失的字段直接不提，不用 0 占位。"""
    parts: List[str] = []
    if stats.get("files") is not None:
        parts.append("%d 个文件" % stats["files"])
    if stats.get("bytes") is not None:
        parts.append(_human_size(stats["bytes"]))
    if stats.get("errors"):
        parts.append("%d 个失败" % stats["errors"])
    elapsed = stats.get("elapsed_seconds")
    if elapsed is not None:
        # 短任务用一位小数，否则 `0.2s` 会被打成「耗时 0s」
        parts.append(("耗时 %.1fs" if elapsed < 10 else "耗时 %.0fs") % elapsed)
    prefix = {STATUS_SUCCESS: "成功", STATUS_PARTIAL: "部分失败", STATUS_FAILED: "失败"}.get(status, status)
    return prefix + ("：" + "，".join(parts) if parts else "")


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return ("%.1f %s" % (value, unit)) if unit != "B" else ("%d B" % value)
        value /= 1024
    return "%d B" % size


def normalize_config(config: Any) -> Dict[str, Any]:
    """把用户提交的配置收敛成**只含已知字段**的规范形态，用于落库。

    未知字段直接丢弃（前端可能带着 UI 专用键），空值一律省略——存一堆 `""` 会让
    「没设」和「设成空」在界面上分不清。
    """
    if not isinstance(config, dict):
        raise ConfigError("invalid_config", "rclone 配置必须是对象")

    cleaned: Dict[str, Any] = {
        "mode": _text(config.get("mode")).lower(),
        "source": validate_source(config.get("source")),
        "dest": _text(config.get("dest")),
    }
    validate_dest(cleaned["dest"])

    bwlimit = _text(config.get("bwlimit"))
    if bwlimit:
        cleaned["bwlimit"] = _validate_bwlimit(bwlimit)

    for key, code, label in (
        ("transfers", "invalid_transfers", "并发传输数"),
        ("checkers", "invalid_checkers", "校验并发数"),
    ):
        if config.get(key) not in (None, ""):
            cleaned[key] = _validate_int(config.get(key), code, label, 1, 64)

    exclude = _validate_patterns(config.get("exclude"), "invalid_exclude", "排除规则")
    if exclude:
        cleaned["exclude"] = exclude
    include = _validate_patterns(config.get("include"), "invalid_include", "包含规则")
    if include:
        cleaned["include"] = include

    max_age = _validate_max_age(config.get("max_age"))
    if max_age:
        cleaned["max_age"] = max_age
    min_size = _validate_min_size(config.get("min_size"))
    if min_size:
        cleaned["min_size"] = min_size

    if config.get("links") not in (None, ""):
        cleaned["links"] = _validate_links(config.get("links"))

    backup_dir = _text(config.get("backup_dir"))
    if backup_dir:
        cleaned["backup_dir"] = validate_backup_dir(backup_dir)

    cleaned["dry_run"] = bool(config.get("dry_run"))
    return cleaned


def classify_probe(exit_code: Optional[int], output: str) -> Dict[str, Any]:
    """把 `rclone lsd` 的结果翻译成「保存前自检」能显示的原因（R4）。

    区分的粒度只到「用户能据此采取行动」：
    认证失败让他去重配 remote，连不上让他查网络/地址，路径不存在让他改路径。
    """
    text = (output or "")
    lowered = text.lower()

    if exit_code == 0:
        return {"ok": True, "code": "ok", "message": "远端可访问"}

    if re.search(r"didn't find section in config file|did not find section|unknown backend", lowered):
        return {
            "ok": False,
            "code": "remote_not_found",
            "message": "这个 remote 在本机 rclone 配置里不存在",
        }
    if re.search(r"403|401|unauthor|forbidden|invalid_grant|token|credential|login", lowered):
        return {
            "ok": False,
            "code": "auth_failed",
            "message": "认证失败：remote 的凭据可能已过期，请重新配置 rclone",
        }
    if re.search(r"directory not found|no such file|not found", lowered):
        return {"ok": False, "code": "path_not_found", "message": "目标路径不存在"}
    if re.search(
        r"no such host|dial tcp|connection refused|i/o timeout|timeout|network is unreachable",
        lowered,
    ):
        return {"ok": False, "code": "unreachable", "message": "连不上远端：检查网络或地址"}
    if exit_code == 3:
        return {"ok": False, "code": "path_not_found", "message": "目标路径不存在"}
    return {
        "ok": False,
        "code": "unknown",
        "message": text.strip().splitlines()[-1][:200] if text.strip() else "自检失败（无输出）",
    }


def job_key(name: Any, task_id: Any, prefix: str = "rclone") -> str:
    """上报用的稳定 job_key：`<prefix>-<id>-<slug>`。

    用任务 id 打头而不是名字：名字改了 job_key 不该变，否则历史会被拆成两个 job。
    """
    slug = re.sub(r"[^a-z0-9]+", "-", _text(name).lower()).strip("-")[:32]
    return "%s-%s%s" % (prefix, int(task_id), ("-" + slug) if slug else "")
