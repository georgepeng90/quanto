from quanto.cli import build_parser, main


def test_management_commands_are_available():
    parser = build_parser()
    assert parser.parse_args(["datasets", "list"]).command == "datasets"
    assert parser.parse_args(["runs", "get", "example"]).identity == "example"
    assert parser.parse_args(["serve"]).host == "127.0.0.1"
    assert main(["datasets", "list"]) == 2


def test_framework_commands_and_legacy_write_removal():
    parser = build_parser()
    assert parser.parse_args(["baskets", "show", "basket-id", "--revision", "2"]).revision == 2
    assert parser.parse_args(["baskets", "revise", "basket-id", "--json", "request.json"]).identity == "basket-id"
    assert parser.parse_args(["definitions", "list"]).action == "list"
    assert parser.parse_args(["capabilities", "check", "--json", "request.json"]).action == "check"
