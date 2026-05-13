"""Minimal data-fetching entry point for the quant rotation project."""

from __future__ import annotations

from config import ensure_directories, format_paths, load_config


def main() -> None:
    """Bootstrap the data-fetching stage."""
    config = load_config()
    directories = ensure_directories(config)

    print("当前阶段：数据拉取准备")
    print(f"项目根目录：{config.project_root}")
    print(f"数据输出目录：{config.raw_data_dir.relative_to(config.project_root)}")
    print(f"已确认目录：{format_paths(directories, config.project_root)}")
    print("状态：最小入口已就绪，后续接入 AKShare 行业与基准数据。")


if __name__ == "__main__":
    main()
