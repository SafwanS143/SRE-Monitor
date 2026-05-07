"""
Standalone baseline collection script — run once on EC2 before deploying anomaly detection.

Usage:
    pip install requests
    python3 collect_baseline.py

Connects to Prometheus (default: localhost:9090, port-forwarded from the container),
samples every 5s for 10 minutes, and writes baseline.csv to the current directory.
Commit baseline.csv so it survives terraform destroy/apply cycles.
"""
import csv
import os
import sys
import time

import requests

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
OUTPUT_FILE    = os.getenv("BASELINE_FILE", "baseline.csv")
DURATION_S     = 1200   # 20 minutes
INTERVAL_S     = 5


def prom_query(promql: str) -> float:
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=5,
        )
        body = r.json()
        if body.get("status") != "success":
            return 0.0
        data = body["data"]
        if data["resultType"] == "scalar":
            v = data["result"][1]
            return 0.0 if v == "NaN" else float(v)
        if data["resultType"] == "vector":
            vals = [
                float(res["value"][1])
                for res in data["result"]
                if res["value"][1] != "NaN"
            ]
            return sum(vals) if vals else 0.0
        return 0.0
    except Exception as exc:
        print(f"[WARN] prom_query failed for {promql!r}: {exc}", file=sys.stderr)
        return 0.0


def main():
    total_samples = DURATION_S // INTERVAL_S
    print(f"[COLLECT] Sampling {total_samples} points over {DURATION_S // 60} minutes → {OUTPUT_FILE}")
    print(f"[COLLECT] Prometheus: {PROMETHEUS_URL}")

    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "rps", "error_rate", "p95_latency_ms"])

        for i in range(total_samples):
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            rps = prom_query('sum(rate(http_requests_total{handler="/health"}[15s]))')
            error_rate = prom_query(
                'rate(http_requests_total{handler="/health",status="500"}[15s])'
                ' / rate(http_requests_total{handler="/health"}[15s]) * 100'
            )
            latency = prom_query(
                'histogram_quantile(0.95, sum by(le)'
                ' (rate(http_request_duration_seconds_bucket[15s]))) * 1000'
            )

            writer.writerow([ts, round(rps, 4), round(error_rate, 4), round(latency, 4)])
            f.flush()

            print(
                f"[{i + 1:3}/{total_samples}] {ts}  "
                f"rps={rps:.3f}  err={error_rate:.3f}%  p95={latency:.1f}ms"
            )

            if i < total_samples - 1:
                time.sleep(INTERVAL_S)

    print(f"[COLLECT] Done — {total_samples} samples written to {OUTPUT_FILE}")
    print("[COLLECT] Commit baseline.csv to git before next deploy.")


if __name__ == "__main__":
    main()
