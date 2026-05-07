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

            # IF trained on all 3 features (rps, error_rate, latency).
            # contamination=0.01 so only the most extreme 1% of normal
            # variance triggers — avoids noise from low-traffic baseline.
            self.model = IsolationForest(contamination=0.01, random_state=42)
            self.model.fit(X)

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

    _METRIC_KEYS = (
        ("rps",        "rps"),
        ("error_rate", "error_rate"),
        ("latency",    "p95_latency_ms"),
    )

    def _sigma(self, name: str, value: float) -> float:
        stats = self.baseline_stats[name]
        std   = stats["std"]
        if std > 0:
            return abs(value - stats["mean"]) / std
        # Baseline std is 0 (e.g. error_rate during a clean baseline) — any
        # deviation from the mean counts as a strong signal.
        return float("inf") if value != stats["mean"] else 0.0

    def _dominant_metric(self, rps: float, error_rate: float, latency: float):
        values = {"rps": rps, "error_rate": error_rate, "p95_latency_ms": latency}
        best_name, best_sigma = "rps", 0.0
        for label, key in self._METRIC_KEYS:
            sigma = self._sigma(key, values[key])
            if sigma > best_sigma:
                best_sigma, best_name = sigma, label
        return best_name, best_sigma

    def score_metrics(self, rps: float, error_rate: float, latency: float):
        """
        Isolation Forest detection across all 3 signals — returns
        (is_anomaly, score, trigger).

        trigger is one of:
          "error_rate_if"   — IF fired, error_rate is the dominant deviation
          "rps_latency_if"  — IF fired, RPS or latency is dominant
          None              — no anomaly
        """
        if not self.enabled:
            return False, 0.0, None

        X          = np.array([[rps, error_rate, latency]])
        is_if_anom = self.model.predict(X)[0] == -1
        score      = float(self.model.score_samples(X)[0])

        if not is_if_anom:
            return False, score, None

        dominant, _ = self._dominant_metric(rps, error_rate, latency)
        trigger = "error_rate_if" if dominant == "error_rate" else "rps_latency_if"
        return True, score, trigger

    def primary_cause(self, rps: float, error_rate: float, latency: float, trigger: str = None) -> str:
        """Human-readable description of what triggered the anomaly."""
        if not self.enabled or not trigger:
            return ""
        name, sigma = self._dominant_metric(rps, error_rate, latency)
        if sigma == float("inf"):
            return f"{name} (outside baseline distribution)"
        return f"{name} ({sigma:.1f}σ from baseline)"
