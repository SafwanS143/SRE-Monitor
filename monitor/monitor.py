import docker
import requests
import time
import os
import sqlite3
import datetime
import threading
from collections import deque
from datetime import datetime as dt

from anomaly_detector import AnomalyDetector

TARGET_URL        = os.getenv("TARGET_URL",        "http://target-api:8000/health")
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "5"))
FAILURE_THRESHOLD = int(os.getenv("FAILURE_THRESHOLD", "3"))
CONTAINER_NAME    = os.getenv("CONTAINER_NAME",    "target-api")
SLACK_WEBHOOK     = os.getenv("SLACK_WEBHOOK",     "")
PROMETHEUS_URL    = os.getenv("PROMETHEUS_URL",    "http://prometheus:9090")
DB_PATH           = "/data/incidents.db"

ANOMALY_ALERT_COOLDOWN = 60  # seconds between alerts of the same trigger type
PROBE_WINDOW_SEC       = 30  # rolling window for in-process error rate
ANOMALY_PROBE_INTERVAL = 1   # high-frequency probe cadence for anomaly signal

MONITOR_START = dt.utcnow()
client = docker.from_env()


# ── Database ───────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            type             TEXT    NOT NULL,
            timestamp        TEXT    NOT NULL,
            description      TEXT    NOT NULL,
            duration_seconds INTEGER,
            resolved         INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS slack_alerts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            type      TEXT NOT NULL,
            message   TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS anomaly_scores (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT    NOT NULL,
            rps            REAL,
            error_rate     REAL,
            p95_latency_ms REAL,
            score          REAL,
            is_anomaly     INTEGER DEFAULT 0,
            trigger        TEXT
        );
        CREATE TABLE IF NOT EXISTS baseline_stats (
            metric TEXT PRIMARY KEY,
            mean   REAL,
            std    REAL
        );
    """)
    con.commit()
    # Migrate existing DB if trigger column is missing
    try:
        con.execute("ALTER TABLE anomaly_scores ADD COLUMN trigger TEXT")
        con.commit()
    except Exception:
        pass
    con.close()


def open_incident(description):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO incidents (type, timestamp, description) VALUES (?, ?, ?)",
        ("failure", dt.utcnow().isoformat() + "Z", description),
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
        (duration_seconds, incident_id),
    )
    con.commit()
    con.close()


def log_slack_alert(alert_type, message):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO slack_alerts (type, message, timestamp) VALUES (?, ?, ?)",
        (alert_type, message, dt.utcnow().isoformat() + "Z"),
    )
    con.commit()
    con.close()


def log_anomaly_score(timestamp, rps, error_rate, latency, score, is_anomaly, trigger):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO anomaly_scores "
        "(timestamp, rps, error_rate, p95_latency_ms, score, is_anomaly, trigger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (timestamp, rps, error_rate, latency, score, int(is_anomaly), trigger),
    )
    # keep last 500 rows
    cur.execute(
        "DELETE FROM anomaly_scores WHERE id NOT IN "
        "(SELECT id FROM anomaly_scores ORDER BY id DESC LIMIT 500)"
    )
    con.commit()
    con.close()


def write_baseline_stats(detector: AnomalyDetector):
    if not detector.enabled:
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    for metric, stats in detector.baseline_stats.items():
        cur.execute(
            "INSERT OR REPLACE INTO baseline_stats (metric, mean, std) VALUES (?, ?, ?)",
            (metric, stats["mean"], stats["std"]),
        )
    con.commit()
    con.close()
    print("[ANOMALY] Baseline stats written to DB")


# ── Prometheus ─────────────────────────────────────────────────────────────

def prom_query(promql: str) -> float:
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=3,
        )
        body = r.json()
        if body.get("status") != "success":
            return 0.0
        data  = body["data"]
        rtype = data["resultType"]
        if rtype == "scalar":
            v = data["result"][1]
            return 0.0 if v == "NaN" else float(v)
        if rtype == "vector":
            vals = [
                float(res["value"][1])
                for res in data["result"]
                if res["value"][1] != "NaN"
            ]
            return sum(vals) if vals else 0.0
        return 0.0
    except Exception:
        return 0.0


def fetch_metrics():
    rps = prom_query('sum(rate(http_requests_total{handler="/health"}[15s]))')
    latency = prom_query(
        'histogram_quantile(0.95, sum by(le)'
        ' (rate(http_request_duration_seconds_bucket[15s]))) * 1000'
    )
    return rps, latency


# Probe outcomes are tracked in-process so error_rate is immediate (no Prometheus
# scrape lag) and survives container restarts that would reset Prom counters.
# Fed by BOTH the main 5s health-check loop and the high-frequency anomaly probe
# below — the latter gives the detector enough samples to catch probabilistic
# degradation that the slow probe would statistically miss.
probe_history: deque = deque()
probe_lock = threading.Lock()


def record_probe(success: bool) -> None:
    now = time.time()
    with probe_lock:
        probe_history.append((now, success))
        cutoff = now - PROBE_WINDOW_SEC
        while probe_history and probe_history[0][0] < cutoff:
            probe_history.popleft()


def probe_error_rate() -> float:
    with probe_lock:
        if not probe_history:
            return 0.0
        fails = sum(1 for _, ok in probe_history if not ok)
        return fails / len(probe_history) * 100.0


def clear_probe_history() -> None:
    with probe_lock:
        probe_history.clear()


def _anomaly_probe_loop() -> None:
    """Independent high-frequency probe — feeds error_rate signal only,
    does NOT drive restart logic."""
    while True:
        try:
            r = requests.get(TARGET_URL, timeout=2)
            record_probe(r.status_code == 200)
        except Exception:
            record_probe(False)
        time.sleep(ANOMALY_PROBE_INTERVAL)


# ── Health & restart ───────────────────────────────────────────────────────

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
        # Drop accumulated probe failures so the freshly-restarted container
        # isn't immediately flagged anomalous on stale window data.
        clear_probe_history()
        print(f"[RESTART] Container {CONTAINER_NAME} restarted")
    except Exception as e:
        print(f"[ERROR] Could not restart container: {e}")


# ── Slack ──────────────────────────────────────────────────────────────────

def send_slack(alert_type, message):
    timestamp    = dt.now().strftime("%H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    if SLACK_WEBHOOK:
        try:
            requests.post(SLACK_WEBHOOK, json={"text": full_message}, timeout=5)
        except Exception:
            pass
    log_slack_alert(alert_type, full_message)


# ── Main ───────────────────────────────────────────────────────────────────

init_db()
detector = AnomalyDetector("/app/baseline.csv")
write_baseline_stats(detector)

print("[MONITOR] Starting health monitor...")
threading.Thread(target=_anomaly_probe_loop, daemon=True).start()

was_healthy        = True
current_incident_id = None
failure_start      = None
consecutive_failures = 0
last_anomaly_alert = {     # unix ts of last Slack alert per trigger bucket
    "error_rate_if":  0.0,
    "rps_latency_if": 0.0,
}

while True:
    ts_str     = dt.utcnow().isoformat() + "Z"
    display_ts = time.strftime("%H:%M:%S")

    # ── Health check ──────────────────────────────────────────────────────
    healthy = check_health()
    record_probe(healthy)

    if healthy:
        print(f"[OK] {display_ts}")
        if not was_healthy:
            duration = int((dt.utcnow() - failure_start).total_seconds()) if failure_start else None
            if current_incident_id is not None:
                close_incident(current_incident_id, duration)
                current_incident_id = None
            failure_start = None
            send_slack("RECOVERED", f":white_check_mark: *RECOVERED* — `{CONTAINER_NAME}` is healthy again")
        consecutive_failures = 0
        was_healthy = True
    else:
        consecutive_failures += 1
        print(f"[FAIL] {display_ts} — service unhealthy (failure {consecutive_failures}/{FAILURE_THRESHOLD})")
        if consecutive_failures >= FAILURE_THRESHOLD:
            if was_healthy or consecutive_failures == FAILURE_THRESHOLD:
                failure_start = dt.utcnow()
                current_incident_id = open_incident(f"Service {CONTAINER_NAME} health check failed")
            restart_container()
            send_slack("FAILURE", f":rotating_light: *FAILURE DETECTED* — restarting `{CONTAINER_NAME}`")
            was_healthy = False

    # ── Anomaly scoring ───────────────────────────────────────────────────
    if detector.enabled:
        rps, latency = fetch_metrics()
        error_rate   = probe_error_rate()

        # Skip scoring when there's nothing to score on (startup / network gap)
        if rps == 0.0 and error_rate == 0.0 and latency == 0.0:
            pass
        else:
            is_anomaly, score, trigger = detector.score_metrics(rps, error_rate, latency)
            log_anomaly_score(ts_str, rps, error_rate, latency, score, is_anomaly, trigger)

            if is_anomaly:
                cause = detector.primary_cause(rps, error_rate, latency, trigger)
                cause_str = f" · {cause}" if cause else ""
                print(
                    f"[ANOMALY] {display_ts}{cause_str} — score={score:.3f} "
                    f"rps={rps:.2f} err={error_rate:.1f}% p95={latency:.0f}ms"
                )
                now = time.time()
                if now - last_anomaly_alert.get(trigger, 0.0) >= ANOMALY_ALERT_COOLDOWN:
                    send_slack(
                        "ANOMALY",
                        f":warning: *ANOMALY DETECTED*{cause_str} — score={score:.3f} "
                        f"rps={rps:.2f} err={error_rate:.1f}% p95={latency:.0f}ms",
                    )
                    last_anomaly_alert[trigger] = now

    time.sleep(CHECK_INTERVAL)
