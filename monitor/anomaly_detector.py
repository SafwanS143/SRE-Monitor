import csv
import logging

import numpy as np
from sklearn.ensemble import IsolationForest

log = logging.getLogger("anomaly_detector")


class AnomalyDetector:
    # column index, public name, baseline_stats key
    _METRICS = (
        (0, "rps",        "rps"),
        (1, "error_rate", "error_rate"),
        (2, "latency",    "p95_latency_ms"),
    )

    def __init__(self, baseline_path: str = "/app/baseline.csv"):
        self.enabled        = False
        self.models         = {}     # one IsolationForest per metric (1-D each)
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

            # One IsolationForest per metric (1-D each). A single multi-feature
            # IF dilutes single-axis anomalies — only ~1/n splits land on a
            # given axis, so a 100% error_rate spike with otherwise-normal
            # rps/latency may not be flagged. Per-metric IFs give each axis
            # full detection power.
            #
            # Degenerate (std=0) columns can't be learned by IF on their own —
            # random splits on a constant produce no isolation. Inject tiny
            # synthetic noise on those columns *only* for fitting so IF has
            # variance to learn against. baseline_stats keeps the true std=0
            # for σ-attribution.
            rng = np.random.default_rng(42)
            self.models = {}
            for idx, name, _ in self._METRICS:
                col = X[:, idx].reshape(-1, 1).copy()
                if col.std() == 0:
                    col = col + rng.normal(0, 1e-3, col.shape)
                # contamination=0.05 — in 1-D, IF effectively does boundary
                # detection: a test point outside the training range gets a
                # path length similar to the boundary training samples. With
                # contamination=0.01 the threshold sits at the single most
                # extreme training score, so points just past the boundary
                # are borderline. 0.05 sets the threshold inside the bulk of
                # the distribution, so out-of-range test points fire
                # reliably without false-positives during normal operation
                # (live values sit near the median, far from the 5% tail).
                m = IsolationForest(contamination=0.05, random_state=42)
                m.fit(col)
                self.models[name] = m

            # baseline_stats for all 3 metrics — used for σ-attribution and
            # for the dashboard normal band rendering.
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
        for _, label, key in self._METRICS:
            sigma = self._sigma(key, values[key])
            if sigma > best_sigma:
                best_sigma, best_name = sigma, label
        return best_name, best_sigma

    def score_metrics(self, rps: float, error_rate: float, latency: float):
        """
        Per-metric Isolation Forest detection — returns (is_anomaly, score, trigger).

        Runs an independent IF on each of {rps, error_rate, latency}; flags
        anomaly if ANY model fires. Returns the most-anomalous metric's score
        and bucketises the trigger.

        trigger is one of:
          "error_rate_if"   — error_rate IF fired (alone or as dominant deviation)
          "rps_latency_if"  — RPS or latency IF fired
          None              — no anomaly
        """
        if not self.enabled:
            return False, 0.0, None

        values   = {"rps": rps, "error_rate": error_rate, "latency": latency}
        fired    = {}
        scores   = {}
        for _, name, _ in self._METRICS:
            x         = np.array([[values[name]]])
            fired[name]  = self.models[name].predict(x)[0] == -1
            scores[name] = float(self.models[name].score_samples(x)[0])

        # Most-anomalous score across all metrics (lowest IF score = most anomalous)
        score = min(scores.values())

        if not any(fired.values()):
            return False, score, None

        # Trigger bucket: error_rate gets its own bucket; rps/latency share one
        if fired["error_rate"]:
            # If error_rate fired, it almost always dominates — but if rps or
            # latency *also* fired with a more anomalous score, defer to the
            # dominant-metric heuristic for the bucket label.
            dominant, _ = self._dominant_metric(rps, error_rate, latency)
            trigger = "error_rate_if" if dominant == "error_rate" else "rps_latency_if"
        else:
            trigger = "rps_latency_if"

        return True, score, trigger

    def primary_cause(self, rps: float, error_rate: float, latency: float, trigger: str = None) -> str:
        """Human-readable description of what triggered the anomaly."""
        if not self.enabled or not trigger:
            return ""
        name, sigma = self._dominant_metric(rps, error_rate, latency)
        if sigma == float("inf"):
            return f"{name} (outside baseline distribution)"
        return f"{name} ({sigma:.1f}σ from baseline)"
