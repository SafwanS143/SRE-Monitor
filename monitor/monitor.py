import docker
import requests
import time
import os
from datetime import datetime

TARGET_URL = os.getenv("TARGET_URL", "http://target-api:8000/health")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
CONTAINER_NAME = os.getenv("CONTAINER_NAME", "target-api")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")

client = docker.from_env()


def send_slack(message):
    if SLACK_WEBHOOK:
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            requests.post(SLACK_WEBHOOK, json={"text": f"[{timestamp}] {message}"}, timeout=5)
        except Exception:
            pass


def check_health():
    try:
        r = requests.get(TARGET_URL, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def restart_container():
    try:
        container = client.containers.get(CONTAINER_NAME)
        container.restart()
        print(f"[RESTART] Container {CONTAINER_NAME} restarted")
        send_slack(f":rotating_light: *FAILURE DETECTED* — restarting `{CONTAINER_NAME}`")
    except Exception as e:
        print(f"[ERROR] Could not restart container: {e}")


print("[MONITOR] Starting health monitor...")
was_healthy = True

while True:
    healthy = check_health()
    ts = time.strftime("%H:%M:%S")

    if healthy:
        print(f"[OK] {ts}")
        if not was_healthy:
            send_slack(f":white_check_mark: *RECOVERED* — `{CONTAINER_NAME}` is healthy again")
        was_healthy = True
    else:
        print(f"[FAIL] {ts} — service unhealthy, triggering restart")
        restart_container()
        was_healthy = False

    time.sleep(CHECK_INTERVAL)
