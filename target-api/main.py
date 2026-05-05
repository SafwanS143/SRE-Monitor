from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from prometheus_fastapi_instrumentator import Instrumentator
import sqlite3
import datetime
import re
import docker

APP_START = datetime.datetime.utcnow()
DB_PATH = "/data/incidents.db"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

Instrumentator().instrument(app).expose(app)
healthy = True

_docker_client = docker.from_env()

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
    if healthy:
        return {"status": "ok"}
    return JSONResponse(content={"status": "unhealthy"}, status_code=500)


@app.post("/chaos")
def chaos():
    global healthy
    healthy = False
    return {"message": "chaos triggered — /health now returns 500"}


@app.post("/recover")
def recover():
    global healthy
    healthy = True
    return {"message": "recovered — /health now returns 200"}


@app.get("/api/status")
def api_status():
    uptime_seconds = int((datetime.datetime.utcnow() - APP_START).total_seconds())
    incidents = []
    slack_alerts = []
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
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
        con.close()
    except Exception:
        pass
    return {
        "status": "ok" if healthy else "unhealthy",
        "uptime_seconds": uptime_seconds,
        "incidents": incidents,
        "slack_alerts": slack_alerts,
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
