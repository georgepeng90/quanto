from __future__ import annotations

import contextlib
import json
import subprocess
import sys

import pandas as pd
import pyarrow as pa

from quanto.data.contracts import MarketDataError
from quanto.data.providers.base import FetchContext


FUNCTIONS = {"fund_etf_hist_em", "stock_zh_index_daily_em", "tool_trade_date_hist_sina"}


def run_sdk_call(function: str, kwargs: dict, context: FetchContext) -> pd.DataFrame:
    if function not in FUNCTIONS:
        raise ValueError("unsupported SDK function")
    context.remaining()
    process = subprocess.Popen([sys.executable, "-m", "quanto.data.providers.sdk_call"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    data = json.dumps({"function": function, "kwargs": kwargs}).encode()
    try:
        while True:
            try:
                output, error = process.communicate(data, timeout=min(.1, context.remaining()))
                break
            except subprocess.TimeoutExpired:
                data = None
        context.remaining()
        if process.returncode:
            raise MarketDataError("FAILED", error.decode(errors="replace")[-1000:])
        return pa.ipc.open_stream(output).read_all().to_pandas()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def main() -> int:
    request = json.load(sys.stdin)
    if request["function"] not in FUNCTIONS:
        return 2
    with contextlib.redirect_stdout(sys.stderr):
        import akshare
        frame = getattr(akshare, request["function"])(**request["kwargs"])
    table = pa.Table.from_pandas(frame, preserve_index=False)
    with pa.ipc.new_stream(sys.stdout.buffer, table.schema) as writer:
        writer.write_table(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
