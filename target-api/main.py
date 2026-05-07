from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from prometheus_fastapi_instrumentator import Instrumentator, metrics
import sqlite3
import datetime
import os
import re
import random
import threading
import time as time_module
import logging
import docker
import requests as http

APP_START = datetime.datetime.utcnow()
DB_PATH = "/data/incidents.db"
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")

log = logging.getLogger("target-api")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# should_group_status_codes=False so the `status` label is the raw code ("500"),
# not the grouped form ("5xx"). Queries elsewhere filter on status="500".
Instrumentator(should_group_status_codes=False).add(
    metrics.requests()
).add(metrics.latency()).instrument(app).expose(app)
degradation_level = 0.0

_docker_client = docker.from_env()


def _ensure_sample_table():
    try:
        con = sqlite3.connect(DB_PATH)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS metric_samples (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      TEXT    NOT NULL,
                rps            REAL    DEFAULT 0,
                error_rate_pct REAL    DEFAULT 0,
                p95_latency_ms REAL    DEFAULT 0
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
    except Exception as exc:
        log.error("Failed to ensure DB tables: %s", exc)

_ensure_sample_table()


def prom_query(promql: str) -> float:
    try:
        r = http.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=3,
        )
        body = r.json()
        if body.get("status") != "success":
            log.warning("prom non-success for %r: %s", promql, body)
            return 0.0
        data = body["data"]
        rtype = data["resultType"]
        if rtype == "scalar":
            v = data["result"][1]
            return 0.0 if v == "NaN" else float(v)
        if rtype == "vector":
            vals = [float(res["value"][1]) for res in data["result"] if res["value"][1] != "NaN"]
            return sum(vals) if vals else 0.0
        return 0.0
    except Exception as exc:
        log.error("prom_query failed for %r: %s", promql, exc)
        return 0.0


# Matches the RFC3339 timestamp Docker prepends when timestamps=True
_DOCKER_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})\.\S+\s+")
# Matches [LEVEL] rest-of-line
_LOG_LINE = re.compile(r"^\[([A-Z]+)\]\s*(.*)")
# Matches an embedded HH:MM:SS at the start of the rest, optionally followed by " — message"
_EMBEDDED_TIME = re.compile(r"^(\d{2}:\d{2}:\d{2})\s*(?:—\s*)?(.*)")

_DEFAULT_MESSAGES = {
    "OK": "health check passed",
    "MONITOR": "monitor started",
    "RESTART": "container restarted",
    "ERROR": "error",
    "FAIL": "service unhealthy",
    "ANOMALY": "anomaly detected",
}


def _parse_log_line(raw: str):
    line = raw.strip()
    if not line:
        return None

    docker_ts = ""
    m = _DOCKER_TS.match(line)
    if m:
        docker_ts = m.group(1)
        line = line[m.end():]

    m = _LOG_LINE.match(line)
    if not m:
        return None

    level = m.group(1)
    rest = m.group(2).strip()

    m2 = _EMBEDDED_TIME.match(rest)
    if m2:
        timestamp = m2.group(1)
        tail = m2.group(2).strip()
        message = tail if tail else _DEFAULT_MESSAGES.get(level, "")
    else:
        timestamp = docker_ts
        message = rest if rest else _DEFAULT_MESSAGES.get(level, "")

    return {"timestamp": timestamp, "level": level, "message": message}


@app.get("/health")
def health():
    if random.random() < degradation_level:
        return JSONResponse(content={"status": "unhealthy"}, status_code=500)
    return {"status": "ok"}


def _run_degradation():
    global degradation_level
    for _ in range(4):
        time_module.sleep(2)
        degradation_level = min(degradation_level + 0.25, 1.0)


@app.post("/chaos")
def chaos():
    global degradation_level
    degradation_level = 0.0
    threading.Thread(target=_run_degradation, daemon=True).start()
    return {"message": "gradual degradation started — degradation_level reaches 1.0 over ~8s"}


@app.post("/recover")
def recover():
    global degradation_level
    degradation_level = 0.0
    return {"message": "recovered — /health now returns 200"}


@app.post("/reset")
def reset():
    """Wipe runtime history (incidents, alerts, anomaly scores, metric samples).
    Keeps baseline_stats so anomaly detection stays trained."""
    global degradation_level
    degradation_level = 0.0
    cleared = {}
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        for table in ("incidents", "slack_alerts", "anomaly_scores", "metric_samples"):
            try:
                cur.execute(f"DELETE FROM {table}")
                cleared[table] = cur.rowcount
            except sqlite3.OperationalError:
                cleared[table] = "missing"
        con.commit()
        con.close()
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)
    return {"message": "reset complete", "cleared": cleared}


@app.get("/api/status")
def api_status():
    uptime_seconds = int((datetime.datetime.utcnow() - APP_START).total_seconds())

    # Prometheus: RPS, latency, and real-time HTTP error rate
    rps_raw = prom_query('sum(rate(http_requests_total{handler="/health"}[15s]))')
    p95_raw = prom_query(
        'histogram_quantile(0.95, sum by(le) (rate(http_request_duration_seconds_bucket[15s]))) * 1000'
    )
    p95_latency_ms = round(p95_raw, 1) if p95_raw > 0 else 0.0

    # Real-time 5xx rate from Prometheus — used for status, not the incident DB.
    # The incident DB lags (requires 3 consecutive failures) so it misses probabilistic degradation.
    http_5xx_pct = prom_query(
        'rate(http_requests_total{handler="/health",status="500"}[15s])'
        ' / rate(http_requests_total{handler="/health"}[15s]) * 100'
    )
    if http_5xx_pct >= 50.0:
        derived_status = "unhealthy"
    elif http_5xx_pct >= 1.0:
        derived_status = "degrading"
    else:
        derived_status = "ok"

    # Incident-based error rate is kept for the chart history (captures brief full outages
    # that reset Prometheus counters on container restart).

    incidents = []
    slack_alerts = []
    metrics_history = []
    anomaly_timeseries = []
    anomaly_events = []
    baseline_stats = None
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()

        # Persist current sample (error_rate_pct filled in after incident calculation)
        cur.execute(
            "INSERT INTO metric_samples (timestamp, rps, error_rate_pct, p95_latency_ms) VALUES (?, ?, ?, ?)",
            (datetime.datetime.utcnow().isoformat() + "Z", round(rps_raw, 4), 0.0, p95_latency_ms),
        )
        # Keep table bounded to last 200 rows
        cur.execute(
            "DELETE FROM metric_samples WHERE id NOT IN "
            "(SELECT id FROM metric_samples ORDER BY id DESC LIMIT 200)"
        )

        # Load last 60 samples for chart seeding
        cur.execute(
            "SELECT timestamp, rps, error_rate_pct, p95_latency_ms "
            "FROM metric_samples ORDER BY id DESC LIMIT 60"
        )
        metrics_history = [
            {"timestamp": r[0], "rps": r[1], "error_rate_pct": r[2], "p95_latency_ms": r[3]}
            for r in reversed(cur.fetchall())
        ]

        cur.execute(
            "SELECT id, type, timestamp, description, duration_seconds, resolved "
            "FROM incidents ORDER BY id DESC LIMIT 50"
        )
        for row in cur.fetchall():
            incidents.append({
                "id": row[0],
                "type": row[1],
                "timestamp": row[2],
                "description": row[3],
                "duration_seconds": row[4],
                "resolved": bool(row[5]),
            })
        cur.execute(
            "SELECT id, type, message, timestamp FROM slack_alerts ORDER BY id DESC LIMIT 50"
        )
        for row in cur.fetchall():
            slack_alerts.append({
                "id": row[0],
                "type": row[1],
                "message": row[2],
                "timestamp": row[3],
            })

        # Anomaly timeseries — last 60 scoring events for the chart (oldest first)
        cur.execute(
            "SELECT timestamp, rps, error_rate, p95_latency_ms, score, is_anomaly, trigger "
            "FROM anomaly_scores ORDER BY id DESC LIMIT 60"
        )
        anomaly_timeseries = [
            {
                "timestamp":      r[0],
                "rps":            r[1],
                "error_rate":     r[2],
                "p95_latency_ms": r[3],
                "score":          r[4],
                "is_anomaly":     bool(r[5]),
                "trigger":        r[6],
            }
            for r in reversed(cur.fetchall())
        ]

        # Anomaly events — last 20 confirmed anomalies for the event feed (newest first)
        cur.execute(
            "SELECT timestamp, rps, error_rate, p95_latency_ms, score, trigger "
            "FROM anomaly_scores WHERE is_anomaly=1 ORDER BY id DESC LIMIT 20"
        )
        anomaly_events = [
            {
                "timestamp":      r[0],
                "rps":            r[1],
                "error_rate":     r[2],
                "p95_latency_ms": r[3],
                "score":          r[4],
                "trigger":        r[5],
            }
            for r in cur.fetchall()
        ]

        # Baseline stats for normal-band rendering in the dashboard
        cur.execute("SELECT metric, mean, std FROM baseline_stats")
        rows = cur.fetchall()
        baseline_stats = {r[0]: {"mean": r[1], "std": r[2]} for r in rows} if rows else None

        # Compute error rate from incident downtime in last 5 minutes
        # (more reliable than Prometheus for brief outages that reset counters on restart)
        now_dt = datetime.datetime.utcnow()
        window_sec = 300.0
        window_start_dt = now_dt - datetime.timedelta(seconds=window_sec)
        downtime_sec = 0.0
        for inc in incidents:
            try:
                inc_start = datetime.datetime.fromisoformat(inc["timestamp"].rstrip("Z"))
                inc_end = (
                    inc_start + datetime.timedelta(seconds=inc["duration_seconds"])
                    if inc["resolved"] and inc["duration_seconds"] is not None
                    else now_dt
                )
                overlap_start = max(inc_start, window_start_dt)
                overlap_end = min(inc_end, now_dt)
                if overlap_end > overlap_start:
                    downtime_sec += (overlap_end - overlap_start).total_seconds()
            except Exception:
                pass
        error_rate_pct = round(min(downtime_sec / window_sec * 100, 100.0), 2)

        # Back-fill the error_rate_pct into the sample we just inserted
        cur.execute(
            "UPDATE metric_samples SET error_rate_pct=? WHERE id=(SELECT MAX(id) FROM metric_samples)",
            (error_rate_pct,),
        )
        con.commit()
        con.close()
    except Exception as exc:
        log.error("DB error in api_status: %s", exc)
        error_rate_pct = 0.0
    return {
        "status": derived_status,
        "uptime_seconds": uptime_seconds,
        "rps": round(rps_raw, 2),
        "error_rate_pct": error_rate_pct,
        "p95_latency_ms": p95_latency_ms,
        "metrics_history": metrics_history,
        "incidents": incidents,
        "slack_alerts": slack_alerts,
        "anomaly_timeseries": anomaly_timeseries,
        "anomaly_events": anomaly_events,
        "baseline_stats": baseline_stats,
    }


@app.get("/api/logs")
def api_logs():
    try:
        container = _docker_client.containers.get("monitor")
        raw = container.logs(tail=100, timestamps=True).decode("utf-8", errors="replace")
    except Exception:
        return {"lines": []}

    lines = []
    for raw_line in raw.splitlines():
        parsed = _parse_log_line(raw_line)
        if parsed:
            lines.append(parsed)

    return {"lines": lines}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
