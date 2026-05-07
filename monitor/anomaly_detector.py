import csv
import logging

import numpy as np
from sklearn.ensemble import IsolationForest

log = logging.getLogger("anomaly_detector")


class AnomalyDetector:
    def __init__(self, baseline_path: str = "/app/baseline.csv"):
        self.enabled        = False
        self.model          = None   # IF trained on RPS + latency only
        self.baseline_stats = None
        self._load(baseline_path)

    def _load(self, path: str) -> None:
        try:
            rows = []
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        rows.append([
                            float(row["rps"]),
                            float(row["error_rate"]),
                            float(row["p95_latency_ms"]),
                        ])
                    except (KeyError, ValueError):
                        continue

            if len(rows) < 10:
                log.error(
                    "[ANOMALY] baseline.csv has only %d valid rows (need ≥10) "
                    "— anomaly detection disabled",
                    len(rows),
                )
                return

            X = np.array(rows)

            # IF trained on RPS + latency only (columns 0 and 2).
            # contamination=0.01 so only the most extreme 1% of normal
            # variance triggers — avoids noise from low-traffic baseline.
            X_rl = X[:, [0, 2]]
            self.model = IsolationForest(contamination=0.01, random_state=42)
            self.model.fit(X_rl)

            # baseline_stats for all 3 metrics: used for the error_rate
            # threshold check and for the dashboard normal band.
            self.baseline_stats = {
                "rps":            {"mean": float(np.mean(X[:, 0])), "std": float(np.std(X[:, 0]))},
                "error_rate":     {"mean": float(np.mean(X[:, 1])), "std": float(np.std(X[:, 1]))},
                "p95_latency_ms": {"mean": float(np.mean(X[:, 2])), "std": float(np.std(X[:, 2]))},
            }
            self.enabled = True
            log.info("[ANOMALY] Trained on %d baseline samples — detection enabled", len(rows))

        except FileNotFoundError:
            log.error(
                "[ANOMALY] baseline.csv not found at %s — "
                "run collect_baseline.py first. Anomaly detection disabled.",
                path,
            )
        except Exception as exc:
            log.error("[ANOMALY] Initialisation failed: %s — anomaly detection disabled", exc)

    def score_metrics(self, rps: float, error_rate: float, latency: float):
        """
        Hybrid detection — returns (is_anomaly, score, trigger).

        trigger is one of:
          "error_rate_threshold"  — error_rate exceeded mean + 2σ
          "rps_latency_if"        — Isolation Forest flagged RPS/latency
          None                    — no anomaly
        """
        if not self.enabled:
            return False, 0.0, None

        # Primary signal: simple threshold on error_rate
        er_stats  = self.baseline_stats["error_rate"]
        threshold = er_stats["mean"] + 2 * er_stats["std"]
        error_rate_triggered = error_rate > threshold

        # Secondary signal: IF on RPS + latency
        X_rl        = np.array([[rps, latency]])
        is_if_anom  = self.model.predict(X_rl)[0] == -1
        score       = float(self.model.score_samples(X_rl)[0])

        if error_rate_triggered:
            return True, score, "error_rate_threshold"
        if is_if_anom:
            return True, score, "rps_latency_if"
        return False, score, None

    def primary_cause(self, rps: float, error_rate: float, latency: float, trigger: str = None) -> str:
        """Human-readable description of what triggered the anomaly."""
        if not self.enabled or not trigger:
            return ""
        if trigger == "error_rate_threshold":
            stats = self.baseline_stats["error_rate"]
            std   = stats["std"]
            sigma = abs(error_rate - stats["mean"]) / std if std > 0 else 0.0
            return f"error_rate ({sigma:.1f}σ above threshold)"
        if trigger == "rps_latency_if":
            best, best_sigma = "", 0.0
            for name, val, key in [("rps", rps, "rps"), ("latency", latency, "p95_latency_ms")]:
                stats = self.baseline_stats[key]
                std   = stats["std"]
                sigma = abs(val - stats["mean"]) / std if std > 0 else 0.0
                if sigma > best_sigma:
                    best_sigma, best = sigma, name
            return f"{best} ({best_sigma:.1f}σ from baseline)"
        return ""
