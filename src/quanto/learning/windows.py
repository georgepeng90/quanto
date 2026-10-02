from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np

from quanto.learning.contracts import check_cancelled, timestamp
from quanto.learning.dataset import iter_dataset_bars, read_dataset_manifest
from quanto.learning.features import FEATURE_NAMES


def microseconds(value):
    return int(timestamp(value).timestamp() * 1_000_000)


ROW_DTYPE = np.dtype([("end", "i8"), ("start", "i8"), ("open", "f8"), ("close", "f8"), ("previous", "i8")])
SAMPLE_DTYPE = np.dtype([("anchor", "i8"), ("next", "i8"), ("target", "i8"), ("label", "f4"), ("label_end", "i8")])


class WindowStore:
    """Disk-backed two-dimensional features and compact sample indices."""

    def __init__(self, dataset_dir: Path, cache_dir: Path, config, cancelled=lambda: False):
        self.manifest = read_dataset_manifest(dataset_dir)
        self.config = config
        cache_dir.mkdir(parents=True, exist_ok=True)
        count = self.manifest["row_count"]
        self.features = np.lib.format.open_memmap(cache_dir / "features.npy", mode="w+", dtype="f4", shape=(count, len(FEATURE_NAMES)))
        self.rows = np.lib.format.open_memmap(cache_dir / "rows.npy", mode="w+", dtype=ROW_DTYPE, shape=(count,))
        self.samples = np.lib.format.open_memmap(cache_dir / "samples.npy", mode="w+", dtype=SAMPLE_DTYPE, shape=(count,))
        self._indices = {name: np.lib.format.open_memmap(cache_dir / f"{name}.npy", mode="w+", dtype="i8", shape=(count,)) for name in ("train", "validation", "test")}
        self.counts = {name: 0 for name in self._indices}
        try:
            self._build(dataset_dir, config, cancelled)
        except BaseException:
            self.close()
            raise

    def _build(self, dataset_dir, config, cancelled):
        bounds = {name: (microseconds(getattr(config, f"{name}_start")), microseconds(getattr(config, f"{name}_end"))) for name in self._indices}
        history, last, seen = {}, {}, {}
        total, mean, m2, sample_count = 0, np.zeros(len(FEATURE_NAMES)), np.zeros(len(FEATURE_NAMES)), 0
        for index, row in enumerate(iter_dataset_bars(dataset_dir)):
            if index % 8192 == 0:
                check_cancelled(cancelled)
            key = row["instrument_id"]
            features = np.array([row[name] for name in FEATURE_NAMES], dtype="f4")
            finish = microseconds(row["bar_end"])
            self.features[index] = features
            self.rows[index] = (finish, microseconds(row["bar_start"]), row["open"], row["close"], last.get(key, -1))
            last[key], seen[key] = index, seen.get(key, 0) + 1
            if bounds["train"][0] <= finish < bounds["train"][1]:
                total += 1
                delta = features - mean
                mean += delta / total
                m2 += delta * (features - mean)
            queue = history.setdefault(key, deque())
            queue.append((index, seen[key]))
            if len(queue) > config.horizon:
                anchor, ordinal = queue[0]
                if ordinal >= config.lookback:
                    next_index = queue[1][0]
                    label = np.log(row["close"]) - np.log(self.rows[next_index]["open"])
                    for split, (begin, end) in bounds.items():
                        if begin <= self.rows[anchor]["end"] < end and finish <= end:
                            self.samples[sample_count] = (anchor, next_index, index, label, finish)
                            self._indices[split][self.counts[split]] = sample_count
                            self.counts[split] += 1
                            sample_count += 1
                            break
                queue.popleft()
        if not total or any(not value for value in self.counts.values()):
            raise ValueError("each chronological partition requires valid lookback and purged label samples")
        std = np.sqrt(m2 / total)
        std[std < 1e-8] = 1.
        self.scaler = {"mean": mean.tolist(), "scale": std.tolist(), "count": total}
        self._mean = mean.astype("f4")
        self._scale = std.astype("f4")
        self.sample_count = sample_count

    def indices(self, split):
        return self._indices[split][:self.counts[split]]

    def window(self, anchor):
        result = np.empty((self.config.lookback, len(FEATURE_NAMES)), dtype="f4")
        for offset in range(self.config.lookback - 1, -1, -1):
            if anchor < 0:
                raise ValueError("insufficient window history")
            result[offset] = self.features[anchor]
            anchor = int(self.rows[anchor]["previous"])
        return (result - self._mean) / self._scale

    def close(self):
        for value in (self.features, self.rows, self.samples, *self._indices.values()):
            value.flush()
            value._mmap.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
