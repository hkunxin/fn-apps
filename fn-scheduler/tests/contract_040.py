"""前端 payload 与后端契约的对接测试（在 NAS 上跑）。

用的就是前端 `collectRcloneConfig()` 实际会发的键，包括空串字段。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(os.path.dirname(HERE), "app", "server")
BASE = os.path.join(tempfile.gettempdir(), "scheduler040c")
SOCK = os.path.join(BASE, "s.sock")
CONF = os.path.join(BASE, "rclone.conf")
SRC = os.path.join(BASE, "src")
DST = os.path.join(BASE, "dst")


def api(action, **body):
    payload = json.dumps(dict(body, action=action)).encode("utf-8")
    request = (
        "POST /api HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    ).encode("utf-8") + payload
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(60)
    client.connect(SOCK)
    client.sendall(request)
    chunks = []
    while True:
        data = client.recv(65536)
        if not data:
            break
        chunks.append(data)
    client.close()
    raw = b"".join(chunks)
    head, _, body_bytes = raw.partition(b"\r\n\r\n")
    text = body_bytes.decode("utf-8", "replace")
    try:
        return head.split(b"\r\n")[0].decode(), json.loads(text)
    except ValueError:
        return head.split(b"\r\n")[0].decode(), text


shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(os.path.join(BASE, "www"), exist_ok=True)
os.makedirs(os.path.join(SRC, "sub"), exist_ok=True)
open(os.path.join(SRC, "a.txt"), "w").write("hello\n")
open(os.path.join(SRC, "sub", "b.txt"), "w").write("world\n")
open(CONF, "w").write("[localtest]\ntype = local\n")

env = dict(os.environ)
env["RCLONE_CONFIG"] = CONF
log = open(os.path.join(BASE, "server.log"), "w")
server = subprocess.Popen(
    [
        sys.executable, os.path.join(SERVER_DIR, "scheduler.py"),
        "--unix-socket", SOCK, "--db", os.path.join(BASE, "s.db"),
        "--settings", os.path.join(BASE, "settings.json"),
        "--base-path", "/", "--www-root", os.path.join(BASE, "www"),
    ],
    stdout=log, stderr=subprocess.STDOUT, env=env,
)

try:
    for _ in range(200):
        if os.path.exists(SOCK):
            break
        time.sleep(0.1)
    time.sleep(0.5)

    # 前端 collectRcloneConfig() 的**原样**输出（含空串字段）
    frontend_config = {
        "mode": "copy",
        "source": SRC,
        "dest": "localtest:" + DST,
        "bwlimit": "",
        "exclude": [],
        "include": [],
        "max_age": "",
        "min_size": "",
        "links": "",
        "backup_dir": "",
        "dry_run": False,
    }
    status, created = api(
        "create-task",
        name="前端契约-copy",
        account="hkx",
        trigger_type="schedule",
        schedule_expression="0 3 * * *",
        task_kind="rclone",
        script_body="",
        pre_task_ids=[],
        rclone=frontend_config,
    )
    print("create:", status)
    print("  规范化后 rclone:", created.get("rclone"))
    print("  展示命令:", created.get("script_body"))
    task_id = created.get("id")

    # 试运行：前端传 dry_run:true
    status, run = api("run-task", id=task_id, dry_run=True)
    print("dry-run 触发:", status, run)
    time.sleep(3)
    _s, results = api("list-results", id=task_id, limit=1)
    row = (results.get("data") or [{}])[0]
    print("  试运行结果:", row.get("status"), "| exit:", row.get("exit_code"), "| trigger:", row.get("trigger_reason"))
    print("  目标目录（试运行不该写入）:", os.path.isdir(DST) and os.listdir(DST) or "空/不存在")

    # 真实执行
    print("真实运行:", api("run-task", id=task_id)[1])
    time.sleep(3)
    _s, results = api("list-results", id=task_id, limit=1)
    row = (results.get("data") or [{}])[0]
    print("  结果:", row.get("status"), "| stats:", row.get("stats"))

    # 设置：前端 token 输入框为空时**不发送** ha_token（此处验证不发送也不会清空）
    api("update-settings", ha_base_url="http://127.0.0.1:9", ha_token="token-abc", report_mode="all")
    _s, data = api("update-settings", ha_base_url="http://127.0.0.1:9", report_mode="all")
    print("只改模式后 token 是否保留:", data["data"]["ha_token_set"], "| 回显:", data["data"]["ha_token"])

    _s, q = api("report-status")
    print("report-status:", {k: q[k] for k in ("pending", "sent", "failed")})
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    shutil.rmtree(BASE, ignore_errors=True)
