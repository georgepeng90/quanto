# 本地预览切换记录

2026-09-30，用户授权自主完成后续环节。已将 `http://127.0.0.1:8765` 切换为主仓 `v2` 的存储实现；没有迁移或改写历史数据、快照及报告。

## 当前运行位置

| 用途 | 位置 |
| --- | --- |
| 源码、工作目录及前端 | `D:\Projects\quanto`，前端为 `web/dist` |
| Python | `C:\Users\georg\.codex\worktrees\web-backtest-workbench\quanto\.venv\Scripts\python.exe` |
| 工作区 | `C:\Users\georg\.codex\worktrees\web-backtest-workbench\quanto\data\workbench` |
| 产物 | `C:\Users\georg\.codex\worktrees\web-backtest-workbench\quanto\outputs\workbench` |
| 切换前备份 | `D:\Projects\quanto\outputs\backups\preview-before-v2-20260930-031722` |
| 当前启动日志及验收记录 | `D:\Projects\quanto\outputs\preview-v2` |

旧 worktree 的解释器和工作区仍被使用，不可归档。主仓默认 `data/workbench` 是另一个工作区；直接运行默认启动脚本不会自动选择以上历史记录。

## 已完成验证

- 停旧服务前确认无排队、执行或取消中的任务。使用 SQLite backup API 备份数据库，`integrity_check` 通过；449 个文件复制后逐一校验哈希。切换后这些原文件仍与备份一致。
- 主仓前端通过构建清单验证；原两份 v1 快照通过精确环境和文件验证。
- 原运行 `e30cd205-3576-45e7-a652-4951a4645473` 精确重跑为 `19274a21-9b4f-4140-9682-030754f9d187`：仍引用同一原始快照，最终产物清单字节相同。
- 原 CSV 数据通过当前 HTTP 上传/导入流程形成 Parquet 新版本 `f68b9a97-a24b-4694-ad66-fed5968fa38e`，原版本不变。
- 从历史运行克隆实验，以权重 0.7 和 0.5 提交两次新回测：`4ba07233-20a5-4ab6-83b6-a55ccc9d73e4`、`bd866262-4d01-4f73-9a21-ea1b8e0d4eb4`。均成功，快照为 schema v2，共享同一数据对象。比较接口确认可直接比较，差异仅为有效策略参数。
- CSV/JSON ZIP 校验通过，独立 HTML 可获取；浏览器确认新版结果显示 22/22 交易日完成、绩效及明确的合成数据标记。
- 验收后共 5 次成功回测、1 次成功演示任务、1 次成功导入任务；无失败、中断或遗留排队任务。

这些记录验证的是存储与运行流程，使用合成行情，不构成策略收益验证。验收详情在忽略目录下的 `outputs/preview-v2/acceptance.json`，没有写入会话令牌。

## 换行符修正

主仓 Windows 检出曾把 `uv.lock` 转为 CRLF；内容与原环境相同，但原始字节哈希不同，导致精确重跑预检失败。已添加 `.gitattributes` 固定该文件为 LF，并规范当前工作文件。两份历史快照随后通过精确验证；没有变更依赖、冻结文件或环境比较逻辑。

## 再次启动同一预览

先停止占用 `8765` 的当前服务并确认任务空闲，再从 `D:\Projects\quanto` 执行下面的前台命令。不要同时启动两个指向同一工作区的服务；独占调度锁会拒绝第二个实例。

```powershell
$previewPython = 'C:\Users\georg\.codex\worktrees\web-backtest-workbench\quanto\.venv\Scripts\python.exe'
$previewWorkspace = 'C:\Users\georg\.codex\worktrees\web-backtest-workbench\quanto\data\workbench'
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = 'D:\Projects\quanto\src'
try {
    & $previewPython -m quanto.cli serve --host 127.0.0.1 --port 8765 --workspace $previewWorkspace --frontend-dir 'D:\Projects\quanto\web\dist'
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
```

关闭前台服务使用 Ctrl+C。保留上述 Python 路径和依赖有助于满足历史精确重跑条件；切换到主仓 `.venv` 不等同于原环境。备份目录含当时的绝对路径引用，不能当作已完成路径迁移的新工作区直接启动。
