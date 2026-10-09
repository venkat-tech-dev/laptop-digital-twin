"""Isolation Forest (Liu, Ting & Zhou, 2008) and its extended variant, in plain Python.

Why not scikit-learn: the model is small (a few features, <= 64 trees of <= 128 samples), training
runs once an hour per device, and scoring one vector costs ~64 tree walks. A dependency-free
implementation keeps the backend light, is deterministic (seeded), serialises to JSON (no pickle:
a stored model can never execute code) and can run unchanged on the endpoint agent later.

Scores follow the paper: s(x) = 2^(-E[h(x)] / c(n)); ~0.5 = normal, -> 1 = isolated quickly.
The alert threshold is not a fixed 0.6: it is the ``threshold_quantile`` of the scores of the clean
training data, i.e. "more isolated than 99.5 % of this device's normal minutes".
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.anomalies.stats import median, quantile

EULER = 0.5772156649
THRESHOLD_ROWS = 3000


def c_factor(n: int) -> float:
    """Average path length of an unsuccessful BST search (normalisation constant)."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1) + EULER) - 2.0 * (n - 1) / n


@dataclass
class RobustScaler:
    centers: list[float]
    scales: list[float]

    @staticmethod
    def fit(rows: Sequence[Sequence[float]], floors: Sequence[float]) -> RobustScaler:
        cols = list(zip(*rows, strict=True))
        centers, scales = [], []
        for col, floor in zip(cols, floors, strict=True):
            s = sorted(col)
            centers.append(median(s))
            scales.append(max(quantile(s, 0.75) - quantile(s, 0.25), floor))
        return RobustScaler(centers, scales)

    def transform(self, row: Sequence[float]) -> list[float]:
        return [(x - c) / s for x, c, s in zip(row, self.centers, self.scales, strict=True)]


class IsolationForest:
    """Isolation Forest; ``extended=True`` uses random oblique hyperplanes (Extended Isolation Forest,
    Hariri, Kind & Brunner 2018) instead of single-feature cuts, so a *broken relationship* between
    signals (busy CPU at idle-level temperature) is isolated, not only extreme single values."""

    def __init__(
        self, trees: list[dict[str, list[Any]]], sample_size: int, n_features: int, extended: bool = False
    ) -> None:
        self.trees = trees
        self.sample_size = sample_size
        self.n_features = n_features
        self.extended = extended
        self._c = c_factor(sample_size)

    # -------------------------------------------------------------------- fit
    @staticmethod
    def fit(
        rows: Sequence[Sequence[float]], n_trees: int, sample_size: int, seed: int, extended: bool = False
    ) -> IsolationForest:
        if not rows:
            raise ValueError("no training rows")
        rng = random.Random(seed)  # noqa: S311 - reproducible model, not security
        n_features = len(rows[0])
        size = min(sample_size, len(rows))
        max_depth = math.ceil(math.log2(max(size, 2)))
        trees = []
        for _ in range(n_trees):
            sample = rng.sample(list(rows), size)
            trees.append(IsolationForest._grow(sample, max_depth, rng, n_features, extended))
        return IsolationForest(trees, size, n_features, extended)

    @staticmethod
    def _grow(
        sample: list[Sequence[float]], max_depth: int, rng: random.Random, nf: int, extended: bool
    ) -> dict[str, list[Any]]:
        # flat arrays: feature (-1 = leaf; extended: 1 = split), split offset, left, right, size,
        # and for extended trees the hyperplane normal "w" (x goes left when w.x < s)
        t: dict[str, list[Any]] = {"f": [], "s": [], "l": [], "r": [], "n": []}
        if extended:
            t["w"] = []

        def leaf(idx: int, n: int) -> int:
            t["f"][idx], t["n"][idx] = -1, n
            return idx

        def node(rows: list[Sequence[float]], depth: int) -> int:
            idx = len(t["f"])
            for k in t:
                t[k].append(0)
            if depth >= max_depth or len(rows) <= 1:
                return leaf(idx, len(rows))
            lo = [min(r[f] for r in rows) for f in range(nf)]
            hi = [max(r[f] for r in rows) for f in range(nf)]
            candidates = [f for f in range(nf) if lo[f] < hi[f]]
            if not candidates:
                return leaf(idx, len(rows))
            if extended:
                w = [rng.gauss(0.0, 1.0) if f in candidates else 0.0 for f in range(nf)]
                point = [rng.uniform(lo[f], hi[f]) for f in range(nf)]
                split = sum(wi * pi for wi, pi in zip(w, point, strict=True))
                proj = [sum(wi * xi for wi, xi in zip(w, r, strict=True)) for r in rows]
                left = [r for r, v in zip(rows, proj, strict=True) if v < split]
                right = [r for r, v in zip(rows, proj, strict=True) if v >= split]
                t["f"][idx], t["s"][idx], t["w"][idx] = 1, split, w
            else:
                f = rng.choice(candidates)
                split = rng.uniform(lo[f], hi[f])
                left = [r for r in rows if r[f] < split]
                right = [r for r in rows if r[f] >= split]
                t["f"][idx], t["s"][idx] = f, split
            t["l"][idx] = node(left, depth + 1)
            t["r"][idx] = node(right, depth + 1)
            return idx

        node(sample, 0)
        return t

    # ------------------------------------------------------------------ score
    def path_length(self, x: Sequence[float], tree: dict[str, list[Any]]) -> float:
        i, depth = 0, 0
        f_, s_, l_, r_, n_ = tree["f"], tree["s"], tree["l"], tree["r"], tree["n"]
        w_ = tree.get("w")
        while f_[i] >= 0:
            if w_ is not None:
                go_left = sum(wi * xi for wi, xi in zip(w_[i], x, strict=True)) < s_[i]
            else:
                go_left = x[int(f_[i])] < s_[i]
            i = int(l_[i] if go_left else r_[i])
            depth += 1
        return depth + c_factor(int(n_[i]))

    def score(self, x: Sequence[float]) -> float:
        mean_h = sum(self.path_length(x, t) for t in self.trees) / len(self.trees)
        return 2.0 ** (-mean_h / self._c) if self._c > 0 else 0.5

    # ------------------------------------------------------------- serialize
    def to_dict(self) -> dict[str, Any]:
        def compact(t: dict[str, list[Any]]) -> dict[str, list[Any]]:
            out: dict[str, list[Any]] = {
                "f": [int(v) for v in t["f"]],
                "s": [round(v, 6) for v in t["s"]],
                "l": [int(v) for v in t["l"]],
                "r": [int(v) for v in t["r"]],
                "n": [int(v) for v in t["n"]],
            }
            if "w" in t:
                out["w"] = [[round(c, 6) for c in w] if isinstance(w, list) else 0 for w in t["w"]]
            return out

        return {
            "sample_size": self.sample_size,
            "n_features": self.n_features,
            "extended": self.extended,
            "trees": [compact(t) for t in self.trees],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> IsolationForest:
        trees = [{k: list(v) for k, v in t.items()} for t in d["trees"]]
        n_features = int(d["n_features"])
        for t in trees:  # validate structure: a malformed artifact must fail loudly, not mis-score
            n = len(t["f"])
            if any(len(t[k]) != n for k in ("s", "l", "r", "n")) or ("w" in t and len(t["w"]) != n):
                raise ValueError("malformed isolation tree")
            if any(not 0 <= int(c) < n for c in (*t["l"], *t["r"])):
                raise ValueError("isolation tree child index out of range")
            if "w" in t and any(isinstance(w, list) and len(w) != n_features for w in t["w"]):
                raise ValueError("isolation tree hyperplane has the wrong dimension")
        return IsolationForest(trees, int(d["sample_size"]), n_features, bool(d.get("extended", False)))


@dataclass(frozen=True, slots=True)
class Relation:
    """A learned linear relation between two physically coupled signals on this device
    (target ~ intercept + slope x driver). Its residual is a model feature: it isolates broken
    relationships ("temperature 7 C lower than usual for this CPU load") that single values miss."""

    target: str
    driver: str
    slope: float
    intercept: float
    floor: float

    @property
    def name(self) -> str:
        return f"{self.target}~{self.driver}"

    def residual(self, target: float, driver: float) -> float:
        return target - (self.intercept + self.slope * driver)

    @staticmethod
    def fit(target: str, driver: str, ys: Sequence[float], xs: Sequence[float], floor: float) -> Relation:
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den if den > 0 else 0.0
        return Relation(target, driver, slope, my - slope * mx, floor)


@dataclass
class MultivariateModel:
    """A versioned per-device model: relations + scaler + forest + learned threshold + feature order."""

    model_id: str
    device_id: str
    version: int
    features: list[str]  # signal ids, in raw vector order
    scaler: RobustScaler
    forest: IsolationForest
    threshold: float
    n_train: int
    trained_from: str
    trained_until: str
    params: dict[str, Any]
    relations: list[Relation] = field(default_factory=list)

    @property
    def model_features(self) -> list[str]:
        return [*self.features, *(r.name for r in self.relations)]

    def expand(self, raw: Sequence[float]) -> list[float]:
        idx = {f: i for i, f in enumerate(self.features)}
        return [*raw, *(r.residual(raw[idx[r.target]], raw[idx[r.driver]]) for r in self.relations)]

    def score(self, raw: Sequence[float]) -> float:
        return self.forest.score(self.scaler.transform(self.expand(raw)))

    def contributions(self, raw: Sequence[float]) -> list[tuple[str, float]]:
        """Per-feature robust deviation (|scaled value|): which signals / relations make the vector
        unusual. Relation features are named ``target~driver``."""
        z = self.scaler.transform(self.expand(raw))
        return sorted(zip(self.model_features, (abs(v) for v in z), strict=True), key=lambda p: -p[1])

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "device_id": self.device_id,
            "version": self.version,
            "features": self.features,
            "relations": [
                {
                    "target": r.target,
                    "driver": r.driver,
                    "slope": r.slope,
                    "intercept": r.intercept,
                    "floor": r.floor,
                }
                for r in self.relations
            ],
            "scaler": {"centers": self.scaler.centers, "scales": self.scaler.scales},
            "forest": self.forest.to_dict(),
            "threshold": self.threshold,
            "n_train": self.n_train,
            "trained_from": self.trained_from,
            "trained_until": self.trained_until,
            "params": self.params,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> MultivariateModel:
        relations = [
            Relation(
                str(r["target"]),
                str(r["driver"]),
                float(r["slope"]),
                float(r["intercept"]),
                float(r["floor"]),
            )
            for r in d.get("relations") or []
        ]
        features = [str(f) for f in d["features"]]
        if any(r.target not in features or r.driver not in features for r in relations):
            raise ValueError("relation refers to an unknown feature")
        model = MultivariateModel(
            str(d["model_id"]),
            str(d["device_id"]),
            int(d["version"]),
            features,
            RobustScaler(
                [float(c) for c in d["scaler"]["centers"]], [float(c) for c in d["scaler"]["scales"]]
            ),
            IsolationForest.from_dict(d["forest"]),
            float(d["threshold"]),
            int(d["n_train"]),
            str(d["trained_from"]),
            str(d["trained_until"]),
            dict(d["params"]),
            relations,
        )
        width = len(model.model_features)
        if len(model.scaler.centers) != width or model.forest.n_features != width:
            raise ValueError("model artifact dimensions do not match its features")
        return model


def train_model(
    device_id: str,
    version: int,
    features: list[str],
    rows: Sequence[Sequence[float]],
    floors: Sequence[float],
    trained_from: str,
    trained_until: str,
    n_trees: int,
    sample_size: int,
    threshold_quantile: float,
    seed: int,
    extended: bool = True,
    relations: Sequence[tuple[str, str, float]] = (),
) -> MultivariateModel:
    """``relations``: (target, driver, residual floor) pairs to learn when both signals are features.
    Training uses only the rows given (clean, past data: the caller cuts incidents and the future)."""
    idx = {f: i for i, f in enumerate(features)}
    learned = [
        Relation.fit(t, d, [r[idx[t]] for r in rows], [r[idx[d]] for r in rows], floor)
        for t, d, floor in relations
        if t in idx and d in idx
    ]
    probe = MultivariateModel(
        "",
        device_id,
        version,
        features,
        RobustScaler([], []),
        IsolationForest([], 1, 0),
        0.0,
        0,
        "",
        "",
        {},
        learned,
    )
    expanded = [probe.expand(r) for r in rows]
    scaler = RobustScaler.fit(expanded, [*floors, *(r.floor for r in learned)])
    scaled = [scaler.transform(r) for r in expanded]
    forest = IsolationForest.fit(scaled, n_trees, sample_size, seed, extended=extended)
    # threshold from (at most) THRESHOLD_ROWS evenly spaced training rows: same quantile, bounded cost
    step = max(1, len(scaled) // THRESHOLD_ROWS)
    scores = sorted(forest.score(r) for r in scaled[::step])
    threshold = quantile(scores, threshold_quantile)
    return MultivariateModel(
        f"iforest-{device_id}-v{version}",
        device_id,
        version,
        features,
        scaler,
        forest,
        threshold,
        len(rows),
        trained_from,
        trained_until,
        {
            "algorithm": "extended_isolation_forest" if extended else "isolation_forest",
            "trees": n_trees,
            "sample_size": sample_size,
            "threshold_quantile": threshold_quantile,
            "seed": seed,
        },
        learned,
    )


def select_features(minute_rows: dict[str, dict[int, float]], min_rows: int) -> tuple[list[str], list[int]]:
    """Features (signals) and the minutes where all of them are present. If too few complete
    minutes exist, the signal with the shortest history is dropped first (a new or intermittent
    sensor must not block the model for the others) while at least two features remain."""
    features = sorted(minute_rows)
    while len(features) >= 2:
        common = sorted(set.intersection(*(set(minute_rows[f]) for f in features)))
        if len(common) >= min_rows:
            return features, common
        features.remove(min(features, key=lambda f: (len(minute_rows[f]), f)))
    return features, []
