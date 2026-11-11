"""spec-040 批次 4：fn-scheduler → HA 端到端（#44 正常 / #45 断网补发 / #46 失败注入）。"""

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
BASE = os.path.join(tempfile.gettempdir(), "s040e2e")
SOCK = os.path.join(BASE, "s.sock")
CONF = os.path.join(BASE, "rclone.conf")
SRC = os.path.join(BASE, "src")
DST = os.path.join(BASE, "dst")
HA = "http://127.0.0.1:8123"
TOKEN = open("/home/hkx/.ha_token", encoding="utf-8").read().strip()


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
    _head, _, body_bytes = raw.partition(b"\r\n\r\n")
    text = body_bytes.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def wait_result(task_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = api("list-results", id=task_id, limit=1)
        rows = data.get("data") or []
        if rows and rows[0].get("status") != "running":
            return rows[0]
        time.sleep(0.5)
    return None


def ha_query(job_key):
    payload = json.dumps({"job_key": job_key, "limit": 5}).encode("utf-8")
    request = (
        "POST /api/services/mnn_home/jobs_query?return_response HTTP/1.1\r\n"
        "Host: 127.0.0.1:8123\r\nContent-Type: application/json\r\n"
        f"Authorization: Bearer {TOKEN}\r\nContent-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("utf-8") + payload
    client = socket.create_connection(("127.0.0.1", 8123), timeout=20)
    client.sendall(request)
    chunks = []
    while True:
        data = client.recv(65536)
        if not data:
            break
        chunks.append(data)
    client.close()
    _head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8", "replace")).get("service_response", {})


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

    print("=== 配置 HA 上报（走真实 update-settings 路径）===")
    cfg = api(
        "update-settings",
        ha_base_url=HA,
        ha_token=TOKEN,
        report_mode="all",
        report_retry_seconds=5,
    )
    print("  token 回显:", cfg["data"]["ha_token"], "| token_set:", cfg["data"]["ha_token_set"])

    print("\n=== report-test（设置页那个按钮）===")
    print(" ", api("report-test"))

    print("\n=== #44 正常链路：建任务 → 执行 → 上报 ===")
    task = api(
        "create-task", name="端到端-照片备份", account="hkx",
        trigger_type="schedule", schedule_expression="0 3 * * *",
        task_kind="rclone",
        rclone={"mode": "copy", "source": SRC, "dest": "localtest:" + DST},
    )
    task_id = task["id"]
    print("  任务:", task_id, "| job_key 预期: rclone-%d" % task_id)
    print("  运行:", api("run-task", id=task_id))
    row = wait_result(task_id)
    print("  结果:", row["status"], "| stats:", row["stats"])
    for _ in range(20):
        if (api("report-status") or {}).get("sent", 0) >= 1:
            break
        time.sleep(1)
    print("  上报状态:", {k: api("report-status")[k] for k in ("pending", "sent", "failed", "last_error")})
    got = ha_query("rclone-%d" % task_id)
    print("  HA 里查到:", json.dumps(got, ensure_ascii=False)[:400])

    print("\n=== #46 失败注入：源目录不存在 ===")
    bad = api(
        "create-task", name="端到端-坏路径", account="hkx",
        trigger_type="schedule", schedule_expression="0 4 * * *",
        task_kind="rclone",
        rclone={"mode": "copy", "source": os.path.join(BASE, "nope"), "dest": "localtest:" + DST},
    )
    bad_id = bad["id"]
    api("run-task", id=bad_id)
    bad_row = wait_result(bad_id)
    print("  结果:", bad_row["status"], "| exit:", bad_row["exit_code"], "| stats:", bad_row["stats"])
    time.sleep(8)
    print("  HA 里查到:", json.dumps(ha_query("rclone-%d" % bad_id), ensure_ascii=False)[:400])

    print("\n=== #45 断网补发：先把 HA 地址指向死端口 ===")
    api("update-settings", ha_base_url="http://127.0.0.1:9")
    off = api(
        "create-task", name="端到端-断网期间", account="hkx",
        trigger_type="schedule", schedule_expression="0 5 * * *",
        task_kind="rclone",
        rclone={"mode": "copy", "source": SRC, "dest": "localtest:" + DST + "-off"},
    )
    off_id = off["id"]
    api("run-task", id=off_id)
    wait_result(off_id)
    time.sleep(9)
    st = api("report-status")
    print("  断网时队列:", {k: st[k] for k in ("pending", "sent", "failed")}, "| 错误:", st["last_error"])

    print("  恢复 HA 地址，等自动补发...")
    api("update-settings", ha_base_url=HA)
    for _ in range(40):
        st = api("report-status")
        if st["pending"] == 0:
            break
        time.sleep(2)
    print("  补发后队列:", {k: st[k] for k in ("pending", "sent", "failed")})
    got_off = ha_query("rclone-%d" % off_id)
    print("  HA 里查到:", json.dumps(got_off, ensure_ascii=False)[:400])

    print("\n=== 最终 report-status ===")
    print(" ", api("report-status"))
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    print("\n=== 服务日志里的上报行 ===")
    for line in open(os.path.join(BASE, "server.log"), encoding="utf-8", errors="replace"):
        if "上报" in line or "rclone task" in line:
            print(" ", line.rstrip())
    shutil.rmtree(BASE, ignore_errors=True)
