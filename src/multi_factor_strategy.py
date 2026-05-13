"""Minimal traditional multi-factor strategy entry point."""

from __future__ import annotations

from config import ensure_directories, format_paths, load_config


def main() -> None:
    """Bootstrap the traditional multi-factor backtest stage."""
    config = load_config()
    directories = ensure_directories(config)

    print("当前阶段：传统多因子回测准备")
    print(f"项目根目录：{config.project_root}")
    print(f"输入数据目录：{config.processed_data_dir.relative_to(config.project_root)}")
    print(f"报告输出目录：{config.reports_dir.relative_to(config.project_root)}")
    print(f"已确认目录：{format_paths(directories, config.project_root)}")
    print("状态：最小入口已就绪，后续实现因子打分、调仓与回测。")


if __name__ == "__main__":
    main()
