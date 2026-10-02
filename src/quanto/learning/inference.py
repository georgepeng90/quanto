from __future__ import annotations

from collections import deque
import os
from pathlib import Path

import numpy as np

from quanto.learning.artifacts import read_model_manifest
from quanto.learning.contracts import TrainingSpec, check_cancelled, timestamp
from quanto.learning.dataset import iter_dataset_bars, read_dataset_manifest
from quanto.learning.features import FEATURE_NAMES, FEATURE_VERSION, STEP_POLICY
from quanto.learning.models import build_model, resolve_device


def iter_inference_rows(model_dir: Path, dataset_dir: Path, start, end, cancelled):
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import torch

    model_dir, dataset_dir = Path(model_dir), Path(dataset_dir)
    manifest, dataset = read_model_manifest(model_dir), read_dataset_manifest(dataset_dir)
    if manifest["frequency_minutes"] != dataset["frequency_minutes"] or manifest["feature_version"] != FEATURE_VERSION or tuple(manifest["features"]) != FEATURE_NAMES:
        raise ValueError("incompatible model/dataset frequency or feature schema")
    if manifest.get("step_policy") != STEP_POLICY or manifest["step_policy"] != dataset["step_policy"]:
        raise ValueError("incompatible model/dataset step policy")
    config = TrainingSpec.model_validate(manifest["config"])
    device = resolve_device(config.device)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    model = build_model(config, len(FEATURE_NAMES)).to(device)
    model.load_state_dict(torch.load(model_dir / "weights.pt", map_location="cpu", weights_only=True))
    model.eval()
    mean, scale = np.asarray(manifest["scaler"]["mean"], dtype="f4"), np.asarray(manifest["scaler"]["scale"], dtype="f4")
    start, end = timestamp(start), timestamp(end)
    if start >= end:
        raise ValueError("nonempty inference interval required")
    history, pending, windows, positions = {}, [], [], []

    def flush():
        check_cancelled(cancelled)
        if windows:
            try:
                with torch.inference_mode():
                    scores = model(torch.from_numpy(np.stack(windows)).to(device)).cpu().numpy()
            except torch.cuda.OutOfMemoryError as exc:
                raise ValueError("CUDA memory exhausted during inference; use a model configured with a smaller batch_size or CPU device") from exc
            for position, score in zip(positions, scores):
                if not np.isfinite(score):
                    raise ValueError("model produced a nonfinite score")
                pending[position]["score"] = float(score)
        yield from pending
        pending.clear()
        windows.clear()
        positions.clear()

    for row in iter_dataset_bars(dataset_dir, end=end):
        check_cancelled(cancelled)
        queue = history.setdefault(row["instrument_id"], deque(maxlen=config.lookback))
        queue.append(np.asarray([row[name] for name in FEATURE_NAMES], dtype="f4"))
        if timestamp(row["bar_start"]) < start:
            continue
        row["score"] = None
        pending.append(row)
        if len(queue) == config.lookback:
            positions.append(len(pending) - 1)
            windows.append((np.stack(queue) - mean) / scale)
        if len(pending) >= config.batch_size:
            yield from flush()
    yield from flush()
