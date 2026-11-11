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

## 在界面里管 remote（1.5.0）

任务表单的「目标 remote」下拉旁有「管理」：可以选账号、新建/编辑/删除 SMB / WebDAV / SFTP 三种 remote。

三个要点，都是踩过坑才写下来的：

1. **remote 属于账号，不属于服务。** 配置写在 `<该账号 HOME>/.config/rclone/rclone.conf`，
   因为任务是以任务里选定的账号执行的。用 `sudo rclone config` 配到 root 下，任务跑起来
   照样报 `didn't find section in config file`。
2. **密码只写不读。** 提交的是明文，服务端用 `rclone obscure -` 转成密文再落盘；之后任何
   接口、列表、日志都只回 `***set***`。编辑时密码框永远是空的，留空表示「不修改」。
3. **保存只改这一个 section。** 文件里其它 remote 与注释原样保留，不会被重排。

## 运行中进度（1.5.0）

rclone 任务跑起来后，任务表「上次结果」那一格会变成实时进度（`3% · 12 MB / 372 MB · big1.bin (6%)`），
前端每 1.5 秒问一次 `task-progress`，没有运行中的任务就停止轮询。

实现上有一个必须知道的取舍：`--stats` 是 **1s**（进度要密度），而 1 秒一块的统计会把日志刷爆。
所以执行器**边读边折叠**，只保留最后一块统计 + 普通日志行——74 秒的任务日志从约 370 行压到 3 行。
折叠时有个坑：rclone 最后一块统计后面还跟着空行，早期实现因此把整块丢掉，导致任务成功但
`stats` 全变 `null`、日志为空。改的时候务必保留「块结束也要记住它」的逻辑。

## 退出码与统计的两个坑（踩过，别改回去）

1. **不要加 `--stats-one-line`**：它会把 `Transferred: 3 / 3`、`Errors:`、`Elapsed time:` 全部吃掉，
   只剩一行传输量，结构化结果就没了。默认参数是 `--stats 30s --stats-log-level NOTICE`。
2. **rclone 会把值为 0 的统计行整行省略**（成功时根本没有 `Errors:` 那一行）。
   所以「有统计块但缺该行」读作 0，「压根没有统计块」留 `null`——两者不能混。

另外：源目录不存在时 rclone 退出码是 **3**（不是 1），失败会默认重试 3 次。

## 测试

- `tests/smoke_040.py` / `tests/e2e_040.py` / `tests/contract_040.py`：见文件头说明
- `task-progress`、`save-remote` 这类运行时行为没有单测，靠上面几个脚本在真机上跑

```bash
python tests/test_rclone.py     # 43 项，纯逻辑，任意平台
python tests/smoke_040.py       # 需要在 Linux（unix socket + rclone），用 type=local 的假 remote
python tests/contract_040.py    # 前后端 payload 契约（同上）
python tests/e2e_040.py         # 三端端到端，需要真的 HA 地址与令牌
```

`smoke/contract` 用 `[localtest] type = local` 的临时 remote，**不需要任何云盘凭据**。
