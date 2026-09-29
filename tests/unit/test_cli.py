import pytest

from quanto.cli import build_parser, main
from quanto.data.loaders import load_dataset


def test_sample_command_remains_usable(tmp_path):
    assert main(["generate-sample-data", "--output-dir", str(tmp_path), "--years", "1"]) == 0
    assert load_dataset(tmp_path).manifest.is_demo


def test_fetch_command_remains_available_and_retired_run_command_is_absent():
    parser = build_parser()
    assert parser.parse_args(["fetch-data", "--quiet"]).command == "fetch-data"
    with pytest.raises(SystemExit):
        parser.parse_args(["run-backtest"])
