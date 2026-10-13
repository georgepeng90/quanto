from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import socket

import httpx
from quanto.workbench.frontend import validate_frontend


class WorkbenchClient:
    def __init__(self, base_url: str, token: str, transport=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport = transport

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        client = self.transport or httpx.Client(timeout=30)
        try:
            response = client.request(method, f"{self.base_url}/api{path}",
                                      headers={"Authorization": f"Bearer {self.token}"}, json=body)
            response.raise_for_status()
            return response.json()
        finally:
            if self.transport is None:
                client.close()

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quanto", description="本机回测工作台")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--token", help="本机会话令牌；也可设置 QUANTO_TOKEN")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本机 API 和网页服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--workspace", type=Path, default=Path("data/workbench"))
    serve.add_argument("--frontend-dir", type=Path)
    for name in ("datasets", "strategies", "experiments", "runs"):
        group = commands.add_parser(name, help=f"管理 {name}")
        actions = group.add_subparsers(dest="action", required=True)
        actions.add_parser("list")
        get = actions.add_parser("get")
        get.add_argument("identity")
        if name != "runs":
            create = actions.add_parser("create")
            create.add_argument("--json", required=True, help="JSON 文件路径")
        if name == "experiments":
            update = actions.add_parser("update")
            update.add_argument("identity")
            update.add_argument("--json", required=True)
        if name == "runs":
            submit = actions.add_parser("submit")
            submit.add_argument("--json", required=True)
            for action in ("cancel", "rerun", "clone", "results", "logs"):
                entry = actions.add_parser(action)
                entry.add_argument("identity")
                if action == "rerun":
                    entry.add_argument("--json", required=True)
    baskets = commands.add_parser("baskets", help="Manage versioned baskets")
    screening = commands.add_parser("screening", help="提交公司研究、查看和导出选股结果")
    screening_actions = screening.add_subparsers(dest="action", required=True)
    listing = screening_actions.add_parser("list")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--cursor", default="0")
    submit = screening_actions.add_parser("submit")
    submit.add_argument("--json", required=True)
    screening_actions.add_parser("show").add_argument("identity")
    export = screening_actions.add_parser("export")
    export.add_argument("identity")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--overwrite", action="store_true")
    use = screening_actions.add_parser("use")
    use.add_argument("identity")
    use.add_argument("--name", required=True)
    use.add_argument("--key", required=True)
    basket_actions = baskets.add_subparsers(dest="action", required=True)
    basket_actions.add_parser("list")
    create = basket_actions.add_parser("create")
    create.add_argument("--json", required=True)
    revise = basket_actions.add_parser("revise")
    revise.add_argument("identity")
    revise.add_argument("--json", required=True)
    show = basket_actions.add_parser("show")
    show.add_argument("identity")
    show.add_argument("--revision", type=int)
    definitions = commands.add_parser("definitions", help="List installed definitions")
    definitions.add_subparsers(dest="action", required=True).add_parser("list")
    capabilities = commands.add_parser("capabilities", help="Check framework capabilities")
    check = capabilities.add_subparsers(dest="action", required=True).add_parser("check")
    check.add_argument("--json", required=True)
    compare = commands.add_parser("compare", help="比较运行")
    compare.add_argument("run_ids", nargs="+")
    learning = commands.add_parser("learning", help="分钟数据训练、模型和回测")
    actions = learning.add_subparsers(dest="action", required=True)
    actions.add_parser("overview")
    get = actions.add_parser("get")
    get.add_argument("resource", choices=("datasets", "models", "backtests"))
    get.add_argument("identity")
    for action in ("logs", "cancel"):
        item = actions.add_parser(action)
        item.add_argument("identity")
    investing = commands.add_parser("investing", help="ETF 周/月自主策略训练与冻结回测")
    actions = investing.add_subparsers(dest="action", required=True)
    actions.add_parser("overview")
    actions.add_parser("models")
    create_model = actions.add_parser("create-model", help="创建固定篮子模型，不启动训练")
    create_model.add_argument("--json", required=True)
    for action in ("train-model", "model-backtest"):
        item = actions.add_parser(action)
        item.add_argument("identity")
        item.add_argument("--json", required=True)
    get = actions.add_parser("get")
    get.add_argument("resource", choices=("models", "policies", "backtests"))
    get.add_argument("identity")
    for action in ("logs", "cancel"):
        item = actions.add_parser(action)
        item.add_argument("identity")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        from quanto.api.app import create_app
        from quanto.api.security import local_host
        import uvicorn
        if not local_host(args.host):
            print("INVALID_HOST: 仅允许回环地址", file=sys.stderr)
            return 2
        checkout = Path.cwd().resolve()
        frontend = (args.frontend_dir or checkout / "web" / "dist").resolve()
        try:
            validate_frontend(checkout, frontend)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        # Bind before creating workspace/scheduler; pass this same socket to Uvicorn.
        listener = socket.socket(socket.AF_INET6 if ":" in args.host else socket.AF_INET)
        try:
            listener.bind((args.host, args.port))
            listener.listen(128)
        except (OSError, OverflowError) as exc:
            listener.close()
            print(f"PORT_IN_USE: 无法绑定 {args.host}:{args.port}，请检查端口和地址：{exc}", file=sys.stderr)
            return 2
        try:
            app = create_app(args.workspace, frontend, templates_dir=checkout / "strategies")
            server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port))
            server.run(sockets=[listener])
        finally:
            listener.close()
        return 0
    token = args.token or os.environ.get("QUANTO_TOKEN", "")
    if not token:
        print("UNAUTHORIZED: 请提供 --token 或 QUANTO_TOKEN", file=sys.stderr)
        return 2
    client = WorkbenchClient(args.url, token)
    try:
        if args.command == "screening":
            if args.action == "submit":
                result = client.post("/screenings", json.loads(Path(args.json).read_text(encoding="utf-8")))
            elif args.action == "list":
                from urllib.parse import urlencode
                result = client.get("/screenings?" + urlencode({"limit": args.limit, "cursor": args.cursor}))
            elif args.action == "show":
                result = client.get(f"/screenings/{args.identity}")
            elif args.action == "use":
                result = client.post(f"/screenings/{args.identity}/basket", {"name": args.name, "idempotency_key": args.key})
            else:
                result = client.get(f"/screenings/{args.identity}/export")
                with args.output.open("w" if args.overwrite else "x", encoding="utf-8") as output:
                    json.dump(result, output, ensure_ascii=False, allow_nan=False)
        elif args.command == "baskets":
            if args.action == "list":
                result = client.get("/baskets")
            elif args.action == "show":
                path = f"/baskets/{args.identity}"
                if args.revision is not None:
                    path += f"/revisions/{args.revision}"
                result = client.get(path)
            elif args.action == "create":
                result = client.post("/baskets", json.loads(Path(args.json).read_text(encoding="utf-8")))
            else:
                result = client.post(f"/baskets/{args.identity}/revisions",
                                     json.loads(Path(args.json).read_text(encoding="utf-8")))
        elif args.command == "definitions":
            result = client.get("/definitions")
        elif args.command == "capabilities":
            result = client.post("/capabilities/check", json.loads(Path(args.json).read_text(encoding="utf-8")))
        elif args.command in {"learning", "investing"}:
            prefix = f"/{args.command}"
            if args.action == "overview":
                result = client.get(f"{prefix}/overview")
            elif args.action == "models":
                result = client.get(f"{prefix}/models")
            elif args.action == "create-model":
                result = client.post(f"{prefix}/models", json.loads(Path(args.json).read_text(encoding="utf-8")))
            elif args.action in {"train-model", "model-backtest"}:
                action = 'train' if args.action == 'train-model' else 'backtests'
                result = client.post(f"{prefix}/models/{args.identity}/{action}", json.loads(Path(args.json).read_text(encoding="utf-8")))
            elif args.action == "get":
                result = client.get(f"{prefix}/{args.resource}/{args.identity}")
            elif args.action == "logs":
                result = client.get(f"/jobs/{args.identity}/logs")
            elif args.action == "cancel":
                result = client.post(f"/jobs/{args.identity}/cancel", {})
        elif args.command == "compare":
            result = client.post("/comparisons", {"run_ids": args.run_ids})
        else:
            root = f"/{args.command}"
            action = args.action
            body = json.loads(Path(args.json).read_text(encoding="utf-8")) if hasattr(args, "json") else None
            if action == "list":
                result = client.get(root)
            elif action == "get":
                result = client.get(f"{root}/{args.identity}")
            elif action in ("create", "submit"):
                result = client.post(root, body)
            elif action == "update":
                result = client.request("PUT", f"{root}/{args.identity}", body)
            elif action in ("rerun", "cancel", "clone"):
                result = client.post(f"{root}/{args.identity}/{action}", body or {})
            else:
                result = client.get(f"{root}/{args.identity}/{action}")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"REQUEST_FAILED: 请求失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
