# -*- coding: utf-8 -*-
"""rclone.py 的单测。零依赖，`python -m unittest discover -s tests` 直接跑。

这些用例锁住的是 spec-040 里最容易被改坏的判断：
argv 是否仍是一个「不做二次解释」的字符串数组、退出码分级、以及「解析不到不许填 0」。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(os.path.dirname(HERE), "app", "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import rclone  # noqa: E402


# 实测本机（fnOS /usr/trim/bin/rclone）的输出样本
VERSION_SAMPLE = """rclone v3.67.8
- os/version: debian 12.13 (64 bit)
- os/kernel: 6.18.18.c1186-trim (x86_64)
- os/type: linux
- os/arch: amd64
- go/version: go1.24.13
- go/linking: dynamic
- go/tags: none
"""

FLAGS_SAMPLE = """  -n, --dry-run         Do a trial run with no permanent changes
      --stats-log-level LogLevel            Log level to show --stats output
      --backup-dir string               Make backups into hierarchy based in DIR
      --bwlimit BwTimetable                Bandwidth limit in KiB/s
      --exclude stringArray                 Exclude files matching pattern
      --include stringArray                 Include files matching pattern
"""

# 以下三段都是 fnOS `/usr/trim/bin/rclone` 的**真实输出**（2026-11-11 实跑抓取），
# 不是按文档手写的样例。改动解析逻辑时以这三段为准。
COPY_SUCCESS = """2026/11/11 17:31:54 NOTICE: Config file "/root/.config/rclone/rclone.conf" not found - using defaults
2026/11/11 17:31:54 NOTICE: 
Transferred:   \t    1.907 MiB / 1.907 MiB, 100%, 0 B/s, ETA -
Transferred:            3 / 3, 100%
Elapsed time:         0.0s
"""

# 目标已一致：没有文件需要传，rclone 连 `Transferred: 3 / 3` 都不打，只剩 Checks
COPY_NOOP = """2026/11/11 17:32:26 NOTICE: 
Transferred:   \t          0 B / 0 B, -, 0 B/s, ETA -
Checks:                 3 / 3, 100%
Elapsed time:         0.0s
"""

# 源目录不存在：退出码 3，且 Errors 那行带后缀 `(retrying may help)`
COPY_FAILED = """2026/11/11 17:32:26 ERROR : Local file system at /tmp/rcl/nope: error reading source root directory: directory not found
2026/11/11 17:32:26 ERROR : Attempt 3/3 failed with 1 errors and: directory not found
2026/11/11 17:32:26 NOTICE: 
Transferred:   \t          0 B / 0 B, -, 0 B/s, ETA -
Errors:                 1 (retrying may help)
Elapsed time:         0.0s

2026/11/11 17:32:26 Failed to copy: directory not found
"""


class VersionAndCapabilityTest(unittest.TestCase):
    def test_parse_version_from_fnOS_build(self):
        # fnOS 自带版报 v3.67.8，不是官方 1.x 序列，但仍要能取出来用于展示
        self.assertEqual(rclone.parse_version(VERSION_SAMPLE), "v3.67.8")

    def test_parse_version_missing(self):
        self.assertIsNone(rclone.parse_version("command not found"))
        self.assertIsNone(rclone.parse_version(""))

    def test_parse_flag_help_ignores_short_flags(self):
        flags = rclone.parse_flag_help(FLAGS_SAMPLE)
        self.assertIn("--stats-log-level", flags)
        self.assertIn("--backup-dir", flags)
        self.assertFalse(any(flag in flags for flag in ("-n", "--")))

    def test_parse_flag_help_ignores_flags_mentioned_in_description(self):
        """描述里出现的 `--stats` 不是参数，扫整行会把它变成假阳性。"""
        flags = rclone.parse_flag_help("      --stats-log-level LogLevel   Log level to show --stats output\n")
        self.assertEqual(flags, ["--stats-log-level"])

    def test_probe_capabilities_all_present(self):
        result = rclone.probe_capabilities(VERSION_SAMPLE, FLAGS_SAMPLE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["version"], "v3.67.8")

    def test_probe_capabilities_reports_missing(self):
        result = rclone.probe_capabilities(VERSION_SAMPLE, "--stats-one-line")
        self.assertFalse(result["ok"])
        self.assertIn("--backup-dir", result["missing"])


class RemoteParsingTest(unittest.TestCase):
    def test_parse_long_output(self):
        text = "onedrive:      onedrive\nmy smb share:  smb\n"
        self.assertEqual(
            rclone.parse_remotes(text),
            [{"name": "onedrive", "type": "onedrive"}, {"name": "my smb share", "type": "smb"}],
        )

    def test_parse_short_output_has_empty_type(self):
        self.assertEqual(rclone.parse_remotes("onedrive:\n"), [{"name": "onedrive", "type": ""}])

    def test_parse_empty_output(self):
        self.assertEqual(rclone.parse_remotes(""), [])

    def test_remote_of(self):
        self.assertEqual(rclone.remote_of("onedrive:Photos"), "onedrive")
        self.assertEqual(rclone.remote_of("my smb:share/x"), "my smb")

    def test_remote_of_rejects_local_path(self):
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.remote_of("/volume1/photos")
        self.assertEqual(ctx.exception.code, "invalid_dest")


class ValidationTest(unittest.TestCase):
    def test_dest_requires_colon(self):
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.validate_dest("/volume1/photos")
        self.assertEqual(ctx.exception.code, "invalid_dest")

    def test_dest_rejects_traversal(self):
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.validate_dest("onedrive:Photos/../../etc")
        self.assertEqual(ctx.exception.code, "path_traversal")

    def test_dest_allows_empty_path(self):
        self.assertEqual(rclone.validate_dest("onedrive:"), ("onedrive", ""))

    def test_source_rejects_root(self):
        # 对 / 做 sync 只可能是误操作，必须挡住
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.validate_source("/")
        self.assertEqual(ctx.exception.code, "invalid_source")

    def test_source_rejects_relative(self):
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.validate_source("photos/2024")
        self.assertEqual(ctx.exception.code, "invalid_source")

    def test_source_rejects_traversal(self):
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.validate_source("/volume1/../etc")
        self.assertEqual(ctx.exception.code, "path_traversal")

    def test_source_normalizes_trailing_slash(self):
        self.assertEqual(rclone.validate_source("/volume1/photos/"), "/volume1/photos")

    def test_bwlimit_rejects_shell_metachar(self):
        # 尾随换行会被 strip 掉（正常输入），所以这里只放**内部**含元字符的样例
        for bad in ("10M; rm -rf /", "10M && ls", "$(id)", "10M\nrm -rf /"):
            with self.assertRaises(rclone.ConfigError):
                rclone._validate_bwlimit(bad)

    def test_bwlimit_accepts_trailing_space(self):
        self.assertEqual(rclone._validate_bwlimit("10M\n"), "10M")

    def test_transfers_range(self):
        with self.assertRaises(rclone.ConfigError):
            rclone._validate_int(0, "invalid_transfers", "并发传输数", 1, 64)
        with self.assertRaises(rclone.ConfigError):
            rclone._validate_int(65, "invalid_transfers", "并发传输数", 1, 64)

    def test_max_age_and_min_size(self):
        self.assertEqual(rclone._validate_max_age("30d"), "30d")
        self.assertEqual(rclone._validate_max_age("1h30m"), "1h30m")
        with self.assertRaises(rclone.ConfigError):
            rclone._validate_max_age("30 days")
        self.assertEqual(rclone._validate_min_size("1M"), "1M")
        with self.assertRaises(rclone.ConfigError):
            rclone._validate_min_size("big")


class BuildArgvTest(unittest.TestCase):
    def test_minimal_copy(self):
        argv = rclone.build_argv({"mode": "copy", "source": "/vol1/photos", "dest": "onedrive:Photos"})
        self.assertEqual(argv[:4], ["rclone", "copy", "/vol1/photos", "onedrive:Photos"])
        self.assertNotIn("--dry-run", argv)
        # 统计参数必须带上，但**不能**带 --stats-one-line（会把文件数/Errors/耗时全吃掉）
        self.assertIn("--stats-log-level", argv)
        self.assertIn("NOTICE", argv)
        self.assertNotIn("--stats-one-line", argv)

    def test_weird_path_stays_single_argument(self):
        """R5：路径里的空格、中文、$() 必须原样是一整个元素，不能被拆开或解释。"""
        source = "/vol1/我的 照片/$HOME;rm -rf"
        argv = rclone.build_argv({"mode": "copy", "source": source, "dest": "onedrive:Photos"})
        self.assertIn(source, argv)
        self.assertEqual(len([item for item in argv if "照片" in item]), 1)

    def test_full_options(self):
        argv = rclone.build_argv(
            {
                "mode": "sync",
                "source": "/vol1/photos",
                "dest": "onedrive:Photos",
                "bwlimit": "10M",
                "transfers": 4,
                "checkers": 8,
                "exclude": ["*.tmp", "**/.cache/**"],
                "max_age": "30d",
                "min_size": "1M",
                "backup_dir": "onedrive:Photos-old",
            }
        )
        joined = " ".join(argv)
        self.assertIn("--bwlimit 10M", joined)
        self.assertIn("--transfers 4", joined)
        self.assertIn("--checkers 8", joined)
        self.assertIn("--exclude *.tmp", joined)
        self.assertIn("--max-age 30d", joined)
        self.assertIn("--min-size 1M", joined)
        self.assertIn("--backup-dir onedrive:Photos-old", joined)

    def test_dry_run_flag(self):
        cfg = {"mode": "copy", "source": "/vol1/a", "dest": "r:b"}
        self.assertIn("--dry-run", rclone.build_argv(cfg, dry_run=True))
        self.assertIn("--dry-run", rclone.build_argv(dict(cfg, dry_run=True)))

    def test_invalid_mode(self):
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.build_argv({"mode": "move", "source": "/vol1/a", "dest": "r:b"})
        self.assertEqual(ctx.exception.code, "invalid_mode")

    def test_invalid_binary(self):
        with self.assertRaises(rclone.ConfigError) as ctx:
            rclone.build_argv(
                {"mode": "copy", "source": "/vol1/a", "dest": "r:b"},
                rclone_bin="rclone && rm -rf /",
            )
        self.assertEqual(ctx.exception.code, "invalid_bin")

    def test_config_must_be_dict(self):
        with self.assertRaises(rclone.ConfigError):
            rclone.build_argv([])  # type: ignore[arg-type]


class ExitCodeTest(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(rclone.classify_exit(0), rclone.STATUS_SUCCESS)
        self.assertEqual(rclone.classify_exit(9), rclone.STATUS_SUCCESS)  # 没有文件需要传
        self.assertEqual(rclone.classify_exit(5), rclone.STATUS_PARTIAL)
        self.assertEqual(rclone.classify_exit(6), rclone.STATUS_PARTIAL)
        for code in (1, 2, 3, 4, 7, 8):
            self.assertEqual(rclone.classify_exit(code), rclone.STATUS_FAILED)

    def test_none_is_failure(self):
        self.assertEqual(rclone.classify_exit(None), rclone.STATUS_FAILED)


class ParseStatsTest(unittest.TestCase):
    def test_success_block(self):
        stats = rclone.parse_stats(COPY_SUCCESS)
        self.assertEqual(stats["files"], 3)
        self.assertEqual(stats["bytes"], int(1.907 * 1024 * 1024))
        self.assertEqual(stats["elapsed_seconds"], 0.0)

    def test_zero_valued_lines_are_absent_so_count_as_zero(self):
        """rclone 把值为 0 的行整行省掉：成功时根本没有 `Errors:` 这行，
        但「有统计块却没有该行」必须读成 0，不能读成"不知道"。"""
        stats = rclone.parse_stats(COPY_SUCCESS)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["deleted"], 0)

    def test_noop_copy_has_no_file_count(self):
        """目标已一致时 rclone 连文件数行都不打，这时 files 只能是 None。"""
        stats = rclone.parse_stats(COPY_NOOP)
        self.assertIsNone(stats["files"])
        self.assertEqual(stats["bytes"], 0)
        self.assertEqual(stats["errors"], 0)

    def test_failure_block(self):
        stats = rclone.parse_stats(COPY_FAILED)
        # `Errors: 1 (retrying may help)` —— 数字后面还有后缀，不能因此漏解析
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["bytes"], 0)
        self.assertIsNone(stats["files"])

    def test_no_stats_block_returns_none_not_zero(self):
        """R8：连统计块都没有（进程被杀、参数错）时所有字段必须是 None。
        这里**不能**套用「缺行=0」的规则——那是另一回事。"""
        stats = rclone.parse_stats("rclone: unknown flag: --nope\n")
        self.assertIsNone(stats["files"])
        self.assertIsNone(stats["bytes"])
        self.assertIsNone(stats["errors"])
        self.assertIsNone(stats["deleted"])
        self.assertIsNone(stats["elapsed_seconds"])

    def test_last_block_wins(self):
        """运行中每 30s 打一块，取最后一块而不是第一块。"""
        text = COPY_SUCCESS + "\n" + COPY_FAILED
        stats = rclone.parse_stats(text)
        self.assertEqual(stats["errors"], 1)

    def test_empty_input(self):
        stats = rclone.parse_stats("")
        self.assertIsNone(stats["files"])
        self.assertIsNone(stats["errors"])


class SummaryAndHelpersTest(unittest.TestCase):
    def test_summarize_skips_missing_fields(self):
        text = rclone.summarize({"files": None, "bytes": None, "errors": None, "elapsed_seconds": None}, "success")
        self.assertEqual(text, "成功")

    def test_summarize_full(self):
        text = rclone.summarize(
            {"files": 1284, "bytes": 1024 * 1024, "errors": 2, "elapsed_seconds": 252.0},
            "partial",
        )
        self.assertIn("部分失败", text)
        self.assertIn("1284 个文件", text)
        self.assertIn("2 个失败", text)

    def test_is_destructive(self):
        self.assertTrue(rclone.is_destructive({"mode": "sync"}))
        self.assertFalse(rclone.is_destructive({"mode": "copy"}))
        self.assertFalse(rclone.is_destructive({"mode": "check"}))
        self.assertFalse(rclone.is_destructive({"mode": "sync", "dry_run": True}))
        self.assertFalse(rclone.is_destructive({"mode": "sync"}, dry_run=True))

    def test_lock_key(self):
        self.assertEqual(rclone.lock_key({"mode": "sync", "dest": "onedrive:x"}), "onedrive")
        self.assertEqual(rclone.lock_key({"mode": "copy", "dest": "onedrive:x"}), "onedrive")
        self.assertIsNone(rclone.lock_key({"mode": "check", "dest": "onedrive:x"}))
        self.assertIsNone(rclone.lock_key({"mode": "sync", "dest": "/local"}))

    def test_job_key_stable_by_id(self):
        self.assertEqual(rclone.job_key("照片备份", 42), "rclone-42")
        self.assertEqual(rclone.job_key("Photo Backup", 42), "rclone-42-photo-backup")
        # 名字改了 job_key 不变（历史不会被拆成两个 job）
        self.assertEqual(rclone.job_key("照片备份-改名", 42), "rclone-42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
