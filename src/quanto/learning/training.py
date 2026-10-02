from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import numpy as np

from quanto.learning.artifacts import read_model_manifest, write_manifest
from quanto.learning.contracts import TrainingSpec, check_cancelled
from quanto.learning.dataset import read_dataset_manifest
from quanto.learning.features import FEATURE_NAMES, FEATURE_VERSION
from quanto.learning.models import build_model, resolve_device
from quanto.learning.windows import WindowStore
from quanto.objects import digest, file_hash, json_bytes, safe_name


def training_identity(config):
    return digest(json_bytes(config.model_dump(mode="json", exclude={"name", "epochs"})))


def read_checkpoint(root: Path):
    import torch

    root = Path(root)
    if (root / "checkpoints").is_dir():
        if (root / "manifest.json").exists():
            read_model_manifest(root)
        root = root / "checkpoints"
    metadata = json.loads((root / "checkpoint.json").read_bytes())
    filename = safe_name(metadata["file"])
    target = root / filename
    if target.is_symlink() or file_hash(target) != metadata["hash"]:
        raise ValueError("checkpoint integrity hash mismatch")
    checkpoint = torch.load(target, map_location="cpu", weights_only=True)
    for key in ("dataset_id", "config_id", "epoch"):
        if checkpoint[key] != metadata[key]:
            raise ValueError("checkpoint identity mismatch")
    return checkpoint


def train_model(spec: dict, dataset_dir: Path, output_dir: Path, emit, cancelled, resume_dir: Path | None = None) -> dict:
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import torch

    config = TrainingSpec.model_validate(spec)
    dataset = read_dataset_manifest(dataset_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("model output directory must be empty")
    if shutil.disk_usage(output_dir).free < dataset["row_count"] * 128 + 64 * 1024 * 1024:
        raise ValueError("insufficient free disk space for compact training window cache")
    check_cancelled(cancelled)
    device = resolve_device(config.device)
    torch.manual_seed(config.seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(config.seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    model = build_model(config, len(FEATURE_NAMES)).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
    checkpoint_dir = output_dir / "checkpoints"
    checkpoint_dir.mkdir()
    curves, first_epoch, best_epoch, stale, best_loss, best_state = [], 0, 0, 0, float("inf"), None
    if resume_dir is not None:
        checkpoint = read_checkpoint(resume_dir)
        if checkpoint["dataset_id"] != dataset["id"] or checkpoint["config_id"] != training_identity(config):
            raise ValueError("resume dataset or training configuration identity mismatch")
        first_epoch = checkpoint["epoch"]
        if config.epochs <= first_epoch:
            raise ValueError("resume epochs must exceed completed checkpoint epoch")
        model.load_state_dict(checkpoint["model"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        torch.set_rng_state(checkpoint["rng"])
        if device == "cuda" and checkpoint["cuda_rng"]:
            torch.cuda.set_rng_state_all(checkpoint["cuda_rng"])
        curves, best_epoch, best_loss, best_state, stale = checkpoint["curves"], checkpoint["best_epoch"], checkpoint["best_loss"], checkpoint["best_model"], checkpoint["stale"]
    cache_dir = output_dir / "window-cache"
    try:
        with WindowStore(Path(dataset_dir), cache_dir, config, cancelled) as windows:
            if resume_dir is not None and checkpoint["scaler"] != windows.scaler:
                raise ValueError("resume normalization identity mismatch")

            def batches(split):
                indices = windows.indices(split)
                for offset in range(0, len(indices), config.batch_size):
                    check_cancelled(cancelled)
                    samples = windows.samples[indices[offset:offset + config.batch_size]]
                    values = np.stack([windows.window(int(sample["anchor"])) for sample in samples])
                    yield torch.from_numpy(values).to(device), torch.from_numpy(samples["label"].copy()).to(device)

            def evaluate(split):
                model.eval()
                squared, absolute, count = 0., 0., 0
                with torch.inference_mode():
                    for values, labels in batches(split):
                        difference = model(values) - labels
                        if not torch.isfinite(difference).all():
                            raise ValueError("nonfinite evaluation predictions; inspect feature scale")
                        squared += difference.square().sum().item()
                        absolute += difference.abs().sum().item()
                        count += len(labels)
                return {"mse": squared / count, "mae": absolute / count, "samples": count}

            for epoch in range(first_epoch + 1, config.epochs + 1):
                model.train()
                total, count = 0., 0
                for values, labels in batches("train"):
                    optimizer.zero_grad(set_to_none=True)
                    loss = (model(values) - labels).square().mean()
                    if not torch.isfinite(loss):
                        raise ValueError("nonfinite training loss; inspect input scale and learning rate")
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.)
                    optimizer.step()
                    total += loss.item() * len(labels)
                    count += len(labels)
                validation = evaluate("validation")
                curves.append(dict(epoch=epoch, train_mse=total / count, validation_mse=validation["mse"]))
                if validation["mse"] < best_loss:
                    best_loss, best_epoch, stale = validation["mse"], epoch, 0
                    best_state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
                else:
                    stale += 1
                check_cancelled(cancelled)
                checkpoint = dict(dataset_id=dataset["id"], config_id=training_identity(config), epoch=epoch,
                    model={name: value.detach().cpu() for name, value in model.state_dict().items()}, optimizer=optimizer.state_dict(),
                    best_model=best_state, best_loss=best_loss, best_epoch=best_epoch, stale=stale, curves=curves,
                    rng=torch.get_rng_state(), cuda_rng=torch.cuda.get_rng_state_all() if device == "cuda" else [], scaler=windows.scaler)
                filename = f"epoch-{epoch:04d}.pt"
                temporary = checkpoint_dir / "checkpoint.tmp"
                torch.save(checkpoint, temporary)
                temporary.replace(checkpoint_dir / filename)
                metadata = dict(file=filename, hash=file_hash(checkpoint_dir / filename), dataset_id=dataset["id"], config_id=training_identity(config), epoch=epoch)
                temporary = checkpoint_dir / "checkpoint.json.tmp"
                temporary.write_bytes(json_bytes(metadata))
                temporary.replace(checkpoint_dir / "checkpoint.json")
                emit(dict(kind="progress", message=f"epoch {epoch}/{config.epochs}", progress=epoch / config.epochs, **curves[-1], device=device))
                check_cancelled(cancelled)
                if stale >= config.patience:
                    break
            model.load_state_dict(best_state)
            # The test partition is evaluated only after validation freezes the weights.
            metrics = {name: evaluate(name) for name in ("train", "validation", "test")}
            metrics["samples"] = windows.counts.copy()
            scaler = windows.scaler
    except torch.cuda.OutOfMemoryError as exc:
        raise ValueError("CUDA memory exhausted; reduce batch_size, lookback or hidden_size, or select CPU") from exc
    finally:
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
    check_cancelled(cancelled)
    torch.save(best_state, output_dir / "weights.pt")
    (output_dir / "curves.json").write_bytes(json_bytes(curves))
    runtime = dict(torch_version=torch.__version__, cuda_version=torch.version.cuda, device=device,
        device_name=torch.cuda.get_device_name() if device == "cuda" else "CPU", seed=config.seed, deterministic_algorithms=True,
        cudnn_deterministic=True, cudnn_benchmark=False, cublas_workspace_config=os.environ["CUBLAS_WORKSPACE_CONFIG"])
    manifest = write_manifest(output_dir, dict(kind="model", version=1, name=config.name, config=config.model_dump(mode="json"),
        dataset_id=dataset["id"], dataset_snapshot_id=dataset["snapshot_id"], frequency_minutes=dataset["frequency_minutes"],
        feature_version=FEATURE_VERSION, features=list(FEATURE_NAMES), scaler=scaler, cutoff=config.validation_end.isoformat(),
        metrics=metrics, best_epoch=best_epoch, epochs_completed=curves[-1]["epoch"], runtime=runtime, limitations=dataset["limitations"],
        resume_parent=str(resume_dir) if resume_dir is not None else None))
    emit(dict(kind="progress", message="model trained", progress=1., metrics=metrics))
    return manifest
