from quanto.cli import build_parser, main


def test_management_commands_are_available():
    parser = build_parser()
    assert parser.parse_args(["datasets", "list"]).command == "datasets"
    assert parser.parse_args(["runs", "get", "example"]).identity == "example"
    assert parser.parse_args(["serve"]).host == "127.0.0.1"
    assert main(["datasets", "list"]) == 2
