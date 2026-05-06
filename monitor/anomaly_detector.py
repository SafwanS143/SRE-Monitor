import csv
import logging

import numpy as np
from sklearn.ensemble import IsolationForest

log = logging.getLogger("anomaly_detector")


class AnomalyDetector:
    def __init__(self, baseline_path: str = "/app/baseline.csv"):
        self.enabled        = False
        self.model          = None
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
            self.model = IsolationForest(contamination=0.05, random_state=42)
            self.model.fit(X)

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
        """Return (is_anomaly, score). Score is more negative for stronger anomalies."""
        if not self.enabled:
            return False, 0.0
        X = np.array([[rps, error_rate, latency]])
        is_anomaly = self.model.predict(X)[0] == -1
        score      = float(self.model.score_samples(X)[0])
        return bool(is_anomaly), score

    def primary_cause(self, rps: float, error_rate: float, latency: float) -> str:
        """Return which metric deviates most from baseline in σ terms, e.g. 'error_rate (8.2σ)'."""
        if not self.enabled:
            return ""
        candidates = {
            "error_rate": (error_rate, self.baseline_stats["error_rate"]),
            "latency":    (latency,    self.baseline_stats["p95_latency_ms"]),
            "rps":        (rps,        self.baseline_stats["rps"]),
        }
        best, best_sigma = "", 0.0
        for name, (val, stats) in candidates.items():
            std = stats["std"]
            sigma = abs(val - stats["mean"]) / std if std > 0 else 0.0
            if sigma > best_sigma:
                best_sigma, best = sigma, name
        if best_sigma < 1.0:
            return ""
        return f"{best} ({best_sigma:.1f}σ from baseline)"
