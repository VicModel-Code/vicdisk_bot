# Telegram 网盘机器人

一个基于 Python 的 Telegram 文件分享机器人，支持通过提取码分享文件、频道关注验证、图片/视频水印、群发广播等功能。

## 功能特性

- **文件管理** — 上传文件（图片、视频、文档、音频等），自动生成提取码和分享链接
- **提取码系统** — 支持批量生成提取码，可设置使用次数限制
- **频道关注验证** — 支持全局和按文件组设置前置频道，用户需关注后才能提取文件
- **水印系统** — 上传时自动为图片/视频添加水印，支持位置、透明度、颜色、平铺等配置
- **广播系统** — 向所有活跃用户群发消息，支持文字+文件混合发送
- **内容保护** — 可按文件组开启防转发/防保存
- **本地 API 支持** — 可对接 Telegram Bot API Local Server，突破 20MB 文件大小限制

## 快速开始

### 环境要求

- Python 3.10+
- ffmpeg（可选，用于视频水印）

### 安装

```bash
git clone <repo-url>
cd tg_pan_bot
pip install -r requirements.txt
```

### 配置

复制环境变量模板并填写配置：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

| 变量 | 说明 | 必填 |
|---|---|---|
| `BOT_TOKEN` | 从 [@BotFather](https://t.me/BotFather) 获取的 Bot Token | ✅ |
| `ADMIN_SECRET` | 管理员认证密码 | ✅ |
| `DB_PATH` | SQLite 数据库路径，默认 `bot.db` | |
| `API_BASE_URL` | Telegram Bot API Local Server 地址 | |
| `API_BASE_FILE_URL` | Local Server 文件下载地址 | |

### 运行

```bash
python bot.py
```

## 使用指南

### 管理员认证

向机器人发送 `/admin <密码>` 即可认证为管理员（消息会自动删除以保护密码安全）。支持多管理员，任何人使用正确密码均可认证。

### 上传文件

1. 在管理面板点击 **📤 存文件**
2. 输入文件组标题
3. 发送一个或多个文件（支持图片、视频、文档、音频、语音、GIF）
4. 点击 **✅ 完成上传**
5. 自动生成提取码和分享链接：`https://t.me/<bot>?start=<code>`

### 提取文件

用户通过分享链接或直接发送提取码给机器人即可获取文件。

### 频道关注验证

- **全局频道**：所有文件提取前需关注
- **文件组频道**：仅特定文件组需额外关注

机器人需要在目标频道中拥有管理员权限。

### 水印配置

在管理面板的水印设置中可配置：

| 参数 | 说明 | 默认值 |
|---|---|---|
| 水印文字 | 自定义文本 | — |
| 字体大小 | 8-200 | 36 |
| 位置 | 居中/四角/平铺 | 居中 |
| 透明度 | 0.0-1.0 | 0.3 |
| 颜色 | HEX 颜色值 | #FFFFFF |
| 旋转角度 | -180° ~ 180° | 0° |

### 广播

管理员可向所有活跃用户群发消息，支持文字和文件混合发送，发送间隔 50ms 以避免触发 Telegram 频率限制。

## 项目结构

```
tg_pan_bot/
├── bot.py              # 入口文件，注册 Handler 并启动 Bot
├── config.py           # 配置项（环境变量、常量）
├── db.py               # 数据库层（SQLite + aiosqlite）
├── utils.py            # 工具函数（提取码生成、频率限制等）
├── watermark.py        # 图片/视频水印处理
├── handlers/
│   ├── admin.py        # 管理员命令和交互
│   ├── channel.py      # 频道管理相关
│   └── user.py         # 用户端处理（提取文件等）
├── requirements.txt    # Python 依赖
├── .env.example        # 环境变量模板
└── .env                # 环境变量（不纳入版本控制）
```

## 服务器部署

### 创建虚拟环境

```bash
cd /root/tg_pan_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 配置 Systemd 守护进程

使用 Systemd 管理机器人进程，实现：关闭 SSH 后继续运行、崩溃自动重启、开机自动启动。

**1. 创建服务文件**

```bash
sudo nano /etc/systemd/system/tgbot.service
```

**2. 写入以下配置**（请将路径修改为你的实际项目路径）：

```ini
[Unit]
Description=Telegram Bot Service
After=network.target

[Service]
User=root
WorkingDirectory=/root/tg_pan_bot
ExecStart=/root/tg_pan_bot/.venv/bin/python bot.py

Restart=always
RestartSec=5

StandardOutput=append:/var/log/tgbot.log
StandardError=append:/var/log/tgbot.log

[Install]
WantedBy=multi-user.target
```

**3. 激活并启动**

```bash
# 重新加载系统服务配置
systemctl daemon-reload

# 设置开机自启
systemctl enable tgbot

# 立即启动服务
systemctl start tgbot

# 查看运行状态
systemctl status tgbot
```

### 日常运维命令

```bash
# 查看运行状态
systemctl status tgbot

# 查看实时日志
tail -f /var/log/tgbot.log
# 或者
journalctl -u tgbot -f

# 更新代码后重启
git pull
systemctl restart tgbot

# 停止机器人
systemctl stop tgbot
```

## 技术细节

- **异步架构**：基于 `python-telegram-bot` v21 的异步 API
- **数据库**：SQLite WAL 模式 + 外键级联删除
- **频率限制**：每用户 30 秒内最多 5 次提取请求
- **提取码**：8 位字母数字组合，`secrets` 模块生成，原子性使用计数防止并发超额
- **智能分组发送**：自动按文件类型分组，遵守 Telegram 每组最多 10 个文件的限制
- **防重复点击**：上传完成和广播确认均有防重复处理

## 许可证

MIT
