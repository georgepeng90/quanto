"""Minimal GRU model entry point for auxiliary validation."""

from __future__ import annotations

from config import ensure_directories, format_paths, load_config


def main() -> None:
    """Bootstrap the GRU training/validation stage."""
    config = load_config()
    directories = ensure_directories(config)

    print("当前阶段：GRU 模型训练准备")
    print(f"项目根目录：{config.project_root}")
    print(f"训练数据目录：{config.processed_data_dir.relative_to(config.project_root)}")
    print(f"模型输出目录：{config.models_dir.relative_to(config.project_root)}")
    print(f"已确认目录：{format_paths(directories, config.project_root)}")
    print("状态：最小入口已就绪，后续实现滚动样本外训练与验证。")


if __name__ == "__main__":
    main()
