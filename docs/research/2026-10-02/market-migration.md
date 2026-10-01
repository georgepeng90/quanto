# 共享行情库迁移记录

实际 A 股行情统一由 `quanto.data` 管理。全局配置位于 `~/.quanto/market.json`，本机唯一共享根为 `D:/Projects/quanto/data/market`；工作区、代码 checkout、当前目录及数据供应商都不改变这个路径。`MarketData()` 与工作台获取任务使用模块解析出的共享根。研究工作区只保存实验、策略、回测数据包和结果。

2026-10-02 迁移了旧 `data/raw` 的 61 个 CSV，共 101,096 行。逐文件核对既有审计记录的 SHA-256，原始字节进入 `evidence/`；逐行核对标准 Parquet 的行情值、日期、UTC 区间、字符串代码及行数。全部通过后删除旧 CSV 和其 `.gitattributes`。目录不再作为缓存入口，也不再跟踪于当前 Git 分支。

[机器可读迁移报告](market-migration.json)记录旧路径、原始 SHA-256、标准对象、来源、口径、行数和迁移时的活动状态。旧审计 JSON 的字节保持不变；其中 `data/raw` 路径是历史来源标签，报告中的 `source_evidence_hash` 和 `object_hash` 才是新库定位依据。原始 CSV 字节可通过 `MarketData.read_evidence(hash)` / `read_evidence_async(hash)` 读取并校验，不需要恢复旧目录。

两个时期的前复权序列锚点可能不同，不能拼接。28 个重叠标的默认采用较新的腾讯冻结序列；另 5 个 ETF 使用唯一存在的旧序列，共 33 个活动标的（32 ETF 与沪深 300）。旧重叠版本仍作为不可变来源证据保存在同一个共享库。来源单位、复权和历史可知时间未获充分核实，继续标记相应限制。此次导入只保存实际行情，没有根据观察到的交易日伪造官方日历，也不自动产生 ready 快照；回测或训练前仍按完整需求预缓存。

旧目录删除后可直接复核共享库，无需原 CSV。活动状态是迁移时的记录；将来刷新停用旧序列不会影响历史证据复核，重复执行已完成的迁移也不会重新启用旧序列：

```powershell
uv run --no-sync python -m scripts.migrate_raw_market_data --report docs/research/2026-10-02/market-migration.json --verify
```

全局配置和实际数据不进 Git。迁移报告进 Git 以保留来源映射；已冻结的研究数据包继续用于历史重放。历史产物内当时生成的来源检查脚本可能仍引用旧路径，复核退役来源应使用上述证据接口和迁移报告。
