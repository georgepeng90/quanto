"""Project configuration and directory bootstrap helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ProjectConfig:
    """Central configuration shared by the runnable entry points."""

    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])
    data_dir_name: str = "data"
    notebooks_dir_name: str = "notebooks"
    raw_data_dir_name: str = "raw"
    processed_data_dir_name: str = "processed"
    models_dir_name: str = "models"
    reports_dir_name: str = "reports"

    @property
    def data_dir(self) -> Path:
        return self.project_root / self.data_dir_name

    @property
    def notebooks_dir(self) -> Path:
        return self.project_root / self.notebooks_dir_name

    @property
    def raw_data_dir(self) -> Path:
        return self.data_dir / self.raw_data_dir_name

    @property
    def processed_data_dir(self) -> Path:
        return self.data_dir / self.processed_data_dir_name

    @property
    def models_dir(self) -> Path:
        return self.project_root / self.models_dir_name

    @property
    def reports_dir(self) -> Path:
        return self.project_root / self.reports_dir_name

    @property
    def required_directories(self) -> tuple[Path, ...]:
        """Directories needed by the minimal runnable pipeline."""
        return (
            self.data_dir,
            self.raw_data_dir,
            self.processed_data_dir,
            self.models_dir,
            self.reports_dir,
            self.notebooks_dir,
        )


def load_config() -> ProjectConfig:
    """Load project configuration.

    This placeholder keeps the startup path stable while the project grows to
    support environment files or typed settings.
    """
    return ProjectConfig()


def ensure_directories(config: ProjectConfig) -> list[Path]:
    """Create directories required by the current project phase."""
    created_or_existing: list[Path] = []
    for directory in config.required_directories:
        directory.mkdir(parents=True, exist_ok=True)
        created_or_existing.append(directory)
    return created_or_existing


def format_paths(paths: Iterable[Path], root: Path) -> str:
    """Format paths relative to the project root for console output."""
    return ", ".join(str(path.relative_to(root)) for path in paths)


if __name__ == "__main__":
    config = load_config()
    directories = ensure_directories(config)
    print("当前阶段：配置加载与目录初始化")
    print(f"项目根目录：{config.project_root}")
    print(f"已确认目录：{format_paths(directories, config.project_root)}")
