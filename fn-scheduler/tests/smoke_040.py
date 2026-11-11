"""spec-040 端到端冒烟。

必须在 Linux（有 unix socket + rclone）上跑，用 `type = local` 的临时 remote，
所以**不需要任何云盘凭据**。跑法：

    cd fn-scheduler && python3 tests/smoke_040.py

2026-11-11 在 fnOS（rclone v3.67.8）上跑通。
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
BASE = os.path.join(tempfile.gettempdir(), "scheduler040")
SOCK = os.path.join(BASE, "s.sock")
DB = os.path.join(BASE, "s.db")
SETTINGS = os.path.join(BASE, "settings.json")
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
    status = head.split(b"\r\n")[0].decode("utf-8", "replace")
    text = body_bytes.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def wait_socket(timeout=20):
    for _ in range(timeout * 10):
        if os.path.exists(SOCK):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(SOCK)
                probe.close()
                return True
            except OSError:
                pass
        time.sleep(0.1)
    return False


shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(os.path.join(BASE, "www"), exist_ok=True)
os.makedirs(os.path.join(SRC, "sub"), exist_ok=True)
with open(os.path.join(SRC, "a.txt"), "w") as handle:
    handle.write("hello\n")
with open(os.path.join(SRC, "sub", "b.txt"), "w") as handle:
    handle.write("world\n")
with open(CONF, "w") as handle:
    # type=local 的 remote：rclone 完全支持，用来在没有云凭据的情况下打通链路
    handle.write("[localtest]\ntype = local\n")

env = dict(os.environ)
env["RCLONE_CONFIG"] = CONF
log = open(os.path.join(BASE, "server.log"), "w")
server = subprocess.Popen(
    [
        sys.executable,
        os.path.join(SERVER_DIR, "scheduler.py"),
        "--unix-socket", SOCK,
        "--db", DB,
        "--settings", SETTINGS,
        "--base-path", "/",
        "--www-root", os.path.join(BASE, "www"),
    ],
    stdout=log,
    stderr=subprocess.STDOUT,
    env=env,
)

try:
    if not wait_socket():
        print("服务没起来")
        print(open(os.path.join(BASE, "server.log")).read()[-3000:])
        raise SystemExit(1)

    print("== rclone-capability ==")
    print(api("rclone-capability")[1])

    print("== list-remotes ==")
    print(api("list-remotes")[1])

    print("== fs-list / ==")
    status, data = api("fs-list", path=BASE)
    print(status, [e["name"] for e in data.get("entries", [])], "truncated=", data.get("truncated"))

    print("== fs-list 穿越应被拒 ==")
    print(api("fs-list", path="/tmp/../../etc")[1])

    print("== probe-remote ==")
    print(api("probe-remote", dest="localtest:" + DST)[1])
    print("== probe-remote 不存在的 remote ==")
    print(api("probe-remote", dest="nosuchremote:/x")[1])

    print("== 建 rclone 任务（copy）==")
    status, task = api(
        "create-task",
        name="冒烟-copy",
        account="hkx",
        trigger_type="schedule",
        schedule_expression="0 3 * * *",
        task_kind="rclone",
        rclone={"mode": "copy", "source": SRC, "dest": "localtest:" + DST, "transfers": 2},
    )
    print(status, {k: task.get(k) for k in ("id", "task_kind", "script_body", "rclone")})
    task_id = task.get("id")

    print("== 建 sync 未确认（应 400）==")
    status, data = api(
        "create-task",
        name="冒烟-sync-未确认",
        account="hkx",
        trigger_type="schedule",
        schedule_expression="0 4 * * *",
        task_kind="rclone",
        rclone={"mode": "sync", "source": SRC, "dest": "localtest:" + DST},
    )
    print(status, data)

    print("== 配一个连不上的 HA（验证队列不丢）==")
    status, settings_data = api(
        "update-settings", ha_base_url="http://127.0.0.1:9", ha_token="fake", report_mode="all"
    )
    print(status, "返回的 token 字段 =", settings_data["data"]["ha_token"], "| token_set =", settings_data["data"]["ha_token_set"])

    print("== run-task ==")
    print(api("run-task", id=task_id)[1])

    deadline = time.time() + 40
    result = None
    while time.time() < deadline:
        _status, data = api("list-results", id=task_id, limit=1)
        results = data.get("data") or []
        if results and results[0].get("status") != "running":
            result = results[0]
            break
        time.sleep(0.5)
    print("== 执行结果 ==")
    print(json.dumps(result, ensure_ascii=False) if result else "超时")

    print("== 目标目录 ==")
    for root, _dirs, files in os.walk(DST):
        for name in files:
            print(" ", os.path.join(root, name))

    print("== 等上报线程尝试一次（HA 连不上）==")
    time.sleep(8)
    print("== report-status（应 pending>=1 且有 last_error）==")
    print(api("report-status")[1])

    print("== 撞锁：同一 remote 再建一个 sync 并同时触发 ==")
    status, task2 = api(
        "create-task",
        name="冒烟-sync",
        account="hkx",
        trigger_type="schedule",
        schedule_expression="0 5 * * *",
        task_kind="rclone",
        confirm_destructive=True,
        rclone={"mode": "sync", "source": SRC, "dest": "localtest:" + DST},
    )
    print("第二个任务:", task2.get("id"))
    print("run#1:", api("run-task", id=task_id)[1])
    print("run#2（同 remote，应 busy）:", api("run-task", id=task2.get("id")))
    time.sleep(3)

finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    print("== 服务日志尾部 ==")
    print(open(os.path.join(BASE, "server.log")).read()[-1500:])
    shutil.rmtree(BASE, ignore_errors=True)
    print("已清理")
