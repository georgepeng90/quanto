from __future__ import annotations


def build_model(config, feature_count):
    from torch import nn

    if config.model_type == "linear":
        return nn.Sequential(nn.Flatten(), nn.Linear(config.lookback * feature_count, 1), nn.Flatten(0))

    class GRURegressor(nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = nn.GRU(feature_count, config.hidden_size, config.layers, batch_first=True)
            self.head = nn.Linear(config.hidden_size, 1)

        def forward(self, values):
            output, _ = self.gru(values)
            return self.head(output[:, -1]).squeeze(-1)

    return GRURegressor()


def resolve_device(requested):
    import torch

    if requested == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA was requested but is unavailable; install a compatible PyTorch CUDA wheel or select CPU")
    return "cuda" if requested == "cuda" or (requested == "auto" and torch.cuda.is_available()) else "cpu"
