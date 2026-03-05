# Telegram 公开频道监控（Telethon + Server酱）

支持两种运行模式：

1. **Telethon 模式（推荐）**：需要 `api_id/api_hash/phone_number`，事件驱动，实时性更高。
2. **公开频道免登录模式**：当你暂时拿不到 `api_id/api_hash` 时，程序自动轮询 `https://t.me/s/<channel>` 页面，不需要 Telegram 登录。

> 说明：Telegram 官方 MTProto 监听必须依赖 `api_id/api_hash`。免登录模式是工程兜底方案，属于网页轮询，不是 MTProto 实时订阅。

## 目录结构

```text
.
├── config.py
├── config.yaml.example
├── data/
├── Dockerfile
├── main.py
├── notifier
│   └── serverchan_send.py
├── requirements.txt
├── storage
│   └── state.py
├── telegram-monitor.service.example
└── utils
    └── logging.py
```

## Python 版本

- 兼容 **Python 3.9+**（已移除 `dataclass(slots=True)` 与 3.10 专属类型语法）。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml
python main.py
```

## 配置说明

- `telegram.api_id/api_hash/phone_number` 全部配置后：走 Telethon 模式。
- 若缺少其中任意项：自动切换公开频道免登录轮询模式。
- `channels.targets` 支持：
  - `@channelname`
  - `https://t.me/channelname`
  - `https://t.me/s/channelname`

关键配置：

- `runtime.backfill_limit`：启动补偿拉取条数。
- `runtime.dedup_window_days`：去重历史保留天数。
- `runtime.public_poll_interval_sec`：公开频道轮询间隔。

## 可靠性

- SQLite 去重表：`processed_messages`
- 频道游标表：`channel_cursor`
- 启动 backfill 补偿
- 失败重试 + 指数退避
- 结构化 JSON 日志
- `SIGINT/SIGTERM` 优雅退出
- Windows 兼容：`add_signal_handler` 不可用时自动跳过

## Docker（可选）

```bash
docker build -t tg-monitor .
docker run --rm -it \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/data:/app/data \
  tg-monitor
```

## systemd（可选）

```bash
sudo cp telegram-monitor.service.example /etc/systemd/system/telegram-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable telegram-monitor
sudo systemctl start telegram-monitor
```


## Linux 稳定运行建议

- 建议使用 `systemd` 托管（示例见 `telegram-monitor.service.example`），已配置 `Restart=always`，进程异常退出会自动重启。
- 本版本已增强 `Ctrl+C`/`SIGTERM` 处理，正常可快速退出。
- 若你观察到只有健康检查日志，请先把 `runtime.log_level` 设为 `DEBUG`，可看到 `poll_cycle`（公开轮询周期）或 `processed_*`（消息处理）日志。


## 如何校验“最后一条消息拿不到”

新增了诊断脚本：`scripts/validate_public_tail.py`，用于对比公开列表页和单条详情页，定位到底是“源页面没更新”还是“监控逻辑漏掉了最后一条”。

```bash
python3 scripts/validate_public_tail.py https://t.me/s/journey_of_someone --sample 5
```

输出重点字段：
- `latest_from_list`: 列表页能看到的最新消息 ID
- `latest_detail_available`: 该 ID 的详情页是否可访问
- `possible_truncated`: 列表页文本是否比详情页短（可能被截断）

判定建议：
- 若 `latest_detail_available=true` 且脚本可看到最新 ID，但主程序日志没有对应 `processed_*`，说明是主程序逻辑问题。
- 若列表页和详情页都看不到最新消息，多数是 Telegram Web 公开页更新延迟/缓存/限流，不是你的程序单独问题。
