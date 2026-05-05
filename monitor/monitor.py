import docker
import requests
import time
import os
import sqlite3
import datetime
from datetime import datetime as dt

TARGET_URL = os.getenv("TARGET_URL", "http://target-api:8000/health")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
CONTAINER_NAME = os.getenv("CONTAINER_NAME", "target-api")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")
DB_PATH = "/data/incidents.db"

MONITOR_START = dt.utcnow()

client = docker.from_env()


def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            description TEXT NOT NULL,
            duration_seconds INTEGER,
            resolved INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS slack_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


def open_incident(description):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO incidents (type, timestamp, description) VALUES (?, ?, ?)",
        ("failure", dt.utcnow().isoformat() + "Z", description)
    )
    con.commit()
    incident_id = cur.lastrowid
    con.close()
    return incident_id


def close_incident(incident_id, duration_seconds):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "UPDATE incidents SET resolved=1, duration_seconds=? WHERE id=?",
        (duration_seconds, incident_id)
    )
    con.commit()
    con.close()


def log_slack_alert(alert_type, message):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO slack_alerts (type, message, timestamp) VALUES (?, ?, ?)",
        (alert_type, message, dt.utcnow().isoformat() + "Z")
    )
    con.commit()
    con.close()


def send_slack(alert_type, message):
    timestamp = dt.now().strftime("%H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    if SLACK_WEBHOOK:
        try:
            requests.post(SLACK_WEBHOOK, json={"text": full_message}, timeout=5)
        except Exception:
            pass
    log_slack_alert(alert_type, full_message)


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
    except Exception as e:
        print(f"[ERROR] Could not restart container: {e}")


init_db()
print("[MONITOR] Starting health monitor...")
was_healthy = True
current_incident_id = None
failure_start = None

while True:
    healthy = check_health()
    ts = time.strftime("%H:%M:%S")

    if healthy:
        print(f"[OK] {ts}")
        if not was_healthy:
            duration = int((dt.utcnow() - failure_start).total_seconds()) if failure_start else None
            if current_incident_id is not None:
                close_incident(current_incident_id, duration)
                current_incident_id = None
            failure_start = None
            send_slack("RECOVERED", f":white_check_mark: *RECOVERED* — `{CONTAINER_NAME}` is healthy again")
        was_healthy = True
    else:
        print(f"[FAIL] {ts} — service unhealthy, triggering restart")
        if was_healthy:
            failure_start = dt.utcnow()
            current_incident_id = open_incident(f"Service {CONTAINER_NAME} health check failed")
        restart_container()
        send_slack("FAILURE", f":rotating_light: *FAILURE DETECTED* — restarting `{CONTAINER_NAME}`")
        was_healthy = False

    time.sleep(CHECK_INTERVAL)
