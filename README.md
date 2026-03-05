# Telegram 公开频道监控（Telethon + Server酱）

本项目使用 **Telethon (MTProto)** 监听一个或多个 Telegram 公开频道，并在新消息出现时通过 **Server酱** 推送通知。

## 目录结构

```text
.
├── config.py
├── config.yaml.example
├── data/                         # 运行时目录（session / sqlite）
├── Dockerfile
├── main.py
├── notifier
│   └── serverchan_send.py        # vendored Server酱发送器
├── requirements.txt
├── storage
│   └── state.py                  # 去重与游标状态
├── telegram-monitor.service.example
└── utils
    └── logging.py                # JSON 结构化日志
```

## 快速开始（本机运行）

1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. 配置文件

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，填入：
- `telegram.api_id`
- `telegram.api_hash`
- `telegram.phone_number`
- `telegram.twofa_password`（如无可留空）
- `notify.serverchan_sendkey`
- `channels.targets`

3. 启动

```bash
python main.py
```

首次运行会触发登录流程：
- 程序发送 Telegram 验证码到你的账号。
- 终端输入验证码。
- 如账号开启二步验证，程序会使用 `twofa_password` 完成登录。
- 登录成功后会在 `telegram.session_path` 保存 session 文件；后续自动登录。

## 可靠性设计

- **事件驱动监听**：`events.NewMessage` 监听目标频道。
- **去重**：`processed_messages(channel_key, message_id)` 避免重复通知。
- **断线补偿**：启动时按 `backfill_limit` + `channel_cursor` 补拉未处理消息。
- **指数退避**：通知失败与 Telegram 连接异常均有重试与退避。
- **优雅退出**：捕获 `SIGINT`/`SIGTERM`，关闭 Telethon client。
- **结构化日志**：JSON 日志，包含 `action/channel/message_id` 字段。

## Docker（可选）

```bash
docker build -t tg-monitor .
docker run --rm -it \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/data:/app/data \
  tg-monitor
```

> 注意：首次登录需要交互输入验证码，请使用 `-it`。

## systemd（可选）

1. 复制服务文件：

```bash
sudo cp telegram-monitor.service.example /etc/systemd/system/telegram-monitor.service
```

2. 按需修改：
- `WorkingDirectory`
- `ExecStart`
- `User/Group`

3. 启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-monitor
sudo systemctl start telegram-monitor
sudo systemctl status telegram-monitor
```

