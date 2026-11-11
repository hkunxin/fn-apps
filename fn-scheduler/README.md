# 任务计划（fn-scheduler）· rclone 备份与执行上报

本目录是 fnOS 的「任务计划」应用。除原有的脚本任务外，本分支（`feat/040-rclone`）新增
**rclone 备份/同步任务**与**执行结果上报到 Home Assistant**。变更要点见
`E:/znzl/specs/040-rclone备份同步与执行状态上报/`。

## 两种任务类型

| 类型 | 执行方式 | 说明 |
|---|---|---|
| `script` | `bash -c <script_body>` | 原有行为，未改动 |
| `rclone` | argv 直接交给 `Popen`（**不经 shell**） | 由结构化字段编译出命令，用户看不到也不需要编辑命令 |

rclone 任务的可配置项：模式（`copy` / `sync` / `check`）、源目录、目标 `remote:path`、
带宽限制、并发传输数、校验并发数、排除/包含规则、文件年龄上限、最小文件大小、软链接策略、
删除前保留副本目录、试运行。

两条硬规则：

- **`sync` 会删除目标端多余文件**，保存时必须带 `confirm_destructive=true`，否则接口返回 400；
  前端把它做成必勾的红色确认。
- **不经 shell**：路径里的空格、中文、`$(...)` 都只是普通字符，不会被二次解释。

## 依赖

- rclone：默认取 `PATH` 里的 `rclone`，可在设置里改成绝对路径（如 `/usr/bin/rclone`）。
- **不按版本号判断能力**：启动自检读 `rclone help flags`，看具体参数在不在
  （fnOS 自带的是 `v3.67.8`，不是官方版本序列）。

## 上报到 Home Assistant

设置页填 HA 地址与**长期令牌**，可选上报模式：全部 / 仅失败 / 关闭。令牌在接口里只回显
`***set***`，写回时空串或掩码值都表示「保持原值」。

事件先落本地 `report_queue` 表，后台线程按指数退避补发；**补发保留原始执行时间**，不会改成
发送时刻。HA 不可达不影响任务本身的结果与本地记录。

事件体（`mnn_home.report_job`）：

```json
{
  "job_key": "rclone-42-photo-backup",
  "run_id": "t42-1762935600-a1b2c3d4",
  "name": "照片备份",
  "kind": "rclone",
  "status": "success",              // success | partial | failed | skipped | dry_run
  "started_at": "2026-11-11 20:40:45",
  "finished_at": "2026-11-11 20:40:45",
  "trigger": "schedule",            // schedule | manual | event | test | dry-run
  "summary": "成功：2 个文件，12 B，耗时 0.0s",
  "stats": {"files": 2, "bytes": 12, "errors": 0, "exit_code": 0},
  "target": "onedrive:Photos",
  "error_excerpt": "",
  "log_tail": "..."
}
```

`job_key` 用 `rclone-<任务 id>-<名字 slug>`：**名字改了 job_key 不变**，否则 HA 里的历史会被拆成两条。

## 退出码与统计的两个坑（踩过，别改回去）

1. **不要加 `--stats-one-line`**：它会把 `Transferred: 3 / 3`、`Errors:`、`Elapsed time:` 全部吃掉，
   只剩一行传输量，结构化结果就没了。默认参数是 `--stats 30s --stats-log-level NOTICE`。
2. **rclone 会把值为 0 的统计行整行省略**（成功时根本没有 `Errors:` 那一行）。
   所以「有统计块但缺该行」读作 0，「压根没有统计块」留 `null`——两者不能混。

另外：源目录不存在时 rclone 退出码是 **3**（不是 1），失败会默认重试 3 次。

## 测试

```bash
python tests/test_rclone.py     # 43 项，纯逻辑，任意平台
python tests/smoke_040.py       # 需要在 Linux（unix socket + rclone），用 type=local 的假 remote
python tests/contract_040.py    # 前后端 payload 契约（同上）
python tests/e2e_040.py         # 三端端到端，需要真的 HA 地址与令牌
```

`smoke/contract` 用 `[localtest] type = local` 的临时 remote，**不需要任何云盘凭据**。
