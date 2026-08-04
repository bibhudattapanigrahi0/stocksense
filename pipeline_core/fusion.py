"""
Learned idea fusion — a small logistic model that combines an idea's signals into one win
probability, learned from the realized trade ledger.

Phase 4 calibrates the single advertised win-rate against reality (isotonic). This goes further: it
learns how to *combine* several idea features — conviction, advertised win-rate, average Sharpe —
into P(win) by fitting an L2-regularized logistic regression on closed tracked ideas. To stay honest
about overfitting it validates **walk-forward**: the ledger is split in time, the model is fit on the
earlier part and scored on the later part, and it is marked `active` only if it beats simply
predicting the base win-rate out-of-sample (lower log-loss). When active, its `model_win_pct` feeds
position sizing ahead of the single-feature calibration.

Dependency-free: logistic regression is a few dozen lines of numpy gradient descent. Degrades
gracefully — too little data, or no out-of-sample edge → inactive, and callers keep the calibrated /
advertised win-rate.

Scope note: this fuses *idea-level* features (the ledger actually labels these with realized
outcomes). Learning the screening composite (fundamental/technical/sentiment/risk weights in
orchestrator.py) would need a persisted (sub-scores → forward-return) training log, which does not
exist yet — that is a data-collection prerequisite, deliberately out of scope here.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

FEATURES = ("conviction", "avg_win_rate", "avg_sharpe")


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def fit_logistic(X, y, *, l2: float = 1.0, iters: int = 600, lr: float = 0.2) -> dict:
    """Standardized, L2-regularized logistic regression via gradient descent. X:(n,d), y:(n,)∈{0,1}.
    Returns a compact model bundle (standardization + weights) that `predict` consumes."""
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    n, d = X.shape
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0
    Xs = (X - mean) / std
    w = np.zeros(d)
    b = 0.0
    for _ in range(iters):
        p = _sigmoid(Xs @ w + b)
        err = p - y
        w -= lr * (Xs.T @ err / n + l2 * w / n)
        b -= lr * err.mean()
    return {"w": w.tolist(), "b": float(b), "mean": mean.tolist(), "std": std.tolist(), "n": int(n)}


def predict(model: dict, X) -> np.ndarray:
    X = np.asarray(X, float)
    mean = np.asarray(model["mean"])
    std = np.asarray(model["std"])
    return _sigmoid(((X - mean) / std) @ np.asarray(model["w"]) + model["b"])


def _logloss(y, p, eps: float = 1e-12) -> float:
    p = np.clip(np.asarray(p, float), eps, 1 - eps)
    y = np.asarray(y, float)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def _features(idea: dict) -> Optional[list[float]]:
    conv, wr, sh = idea.get("conviction"), idea.get("avg_win_rate"), idea.get("avg_sharpe")
    if conv is None or wr is None:
        return None
    wr = float(wr)
    return [float(conv), wr / 100.0 if wr > 1 else wr, float(sh) if sh is not None else 0.0]


def build_idea_model(market: str, *, min_samples: int = 30, val_frac: float = 0.3, l2: float = 1.0) -> dict:
    """Fit + walk-forward-validate an idea-win model from the closed trade ledger. `active` only when
    it beats the base rate out-of-sample. Returns a bundle (with the deployable `model` when active)."""
    from pipeline_core.backtest import runner  # lazy: avoid an import cycle

    mk = (market or "IN").upper()
    closed = [t for t in runner._load_tracked()
              if t.get("closed") and (t.get("market") or "IN").upper() == mk]
    rows: list[tuple[list[float], float]] = []
    for t in closed:
        f = _features(t)
        r = t.get("realized_return_pct")
        if f is None or r is None:
            continue
        rows.append((f, 1.0 if r > 0 else 0.0))

    n = len(rows)
    base = {"market": mk, "features": list(FEATURES), "min_samples": min_samples, "n": n, "model": None}
    if n < min_samples:
        return {**base, "active": False, "reason": "insufficient data"}

    k = max(1, int(n * (1 - val_frac)))
    tr, va = rows[:k], rows[k:]                       # ledger is append-order ≈ chronological
    ytr = [r[1] for r in tr]
    if len(va) < 5 or len(set(ytr)) < 2:
        return {**base, "active": False, "reason": "insufficient validation split"}

    model = fit_logistic([r[0] for r in tr], ytr, l2=l2)
    base_rate = float(np.mean(ytr))
    yva = [r[1] for r in va]
    ll_model = _logloss(yva, predict(model, [r[0] for r in va]))
    ll_base = _logloss(yva, np.full(len(yva), base_rate))
    active = ll_model < ll_base

    return {
        **base, "active": bool(active),
        "val_logloss": round(ll_model, 4), "base_logloss": round(ll_base, 4),
        "improvement": round(ll_base - ll_model, 4), "base_rate_pct": round(base_rate * 100, 1),
        # Refit on ALL rows for deployment (more data) once the OOS check has passed.
        "model": fit_logistic([r[0] for r in rows], [r[1] for r in rows], l2=l2) if active else None,
    }


def model_win_pct(bundle: Optional[dict], idea: dict) -> Optional[float]:
    """The model's P(win) as a percentage for one idea, or None if the model is inactive / features
    are missing."""
    if not bundle or not bundle.get("active") or not bundle.get("model"):
        return None
    f = _features(idea)
    if f is None:
        return None
    return round(float(predict(bundle["model"], [f])[0]) * 100.0, 1)


def public_view(bundle: dict) -> dict:
    """API-facing view — drops the raw model weights, keeps the diagnostics."""
    return {k: v for k, v in bundle.items() if k != "model"}
