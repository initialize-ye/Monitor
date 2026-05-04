# QQ 关键词转发机器人

这是一个基于 `NoneBot2 + OneBot v11 + NapCatQQ` 的最小可运行示例。

推荐部署方式：

- `Ubuntu 云服务器`：运行本项目的 Python 服务
- `Windows 本机`：运行 `QQ + NapCatQQ`
- `NapCatQQ` 通过 `OneBot v11 反向 WebSocket` 主动连接云服务器

功能：

- 监听指定 QQ 群消息
- 检测关键词或正则
- 命中后转发给一个或多个指定 QQ
- 简单去重，避免短时间重复转发
- 支持通过 `rules.json` 管理多群规则
- 支持通过私聊命令增删查群规则和关键词

## 1. 架构说明

这套项目不建议把 `QQ` 和 `NapCatQQ` 直接放在 Ubuntu 上。

更稳的方案是：

- 服务器 `119.29.55.112` 上运行 `NoneBot2`
- 你自己的 Windows 电脑上运行 `QQ + NapCatQQ`
- Windows 上的 NapCat 反向连接到服务器

数据流：

```text
QQ群消息
-> Windows QQ / NapCatQQ
-> 反向 WebSocket
-> Ubuntu 服务器上的 NoneBot2
-> 命中关键词
-> NapCat 代发私聊消息
```

## 2. Ubuntu 服务器要求

- Ubuntu 22.04 或更高
- Python 3.10 或更高
- 服务器安全组放行 `8080/TCP`
- 一个公网 IP：`119.29.55.112`

## 3. 安装依赖

### Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv
cd /path/to/Monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Windows 本地开发

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 4. 配置

复制配置文件：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

- `HOST` / `PORT`：NoneBot 服务监听地址
- `ONEBOT_ACCESS_TOKEN`：如果你在 NapCat 配了 token，这里保持一致
- `ALLOWED_GROUPS`：要监听的群号，多个逗号分隔
- `TARGET_QQS`：要转发到的 QQ 号，多个逗号分隔
- `ADMIN_QQS`：允许私聊管理关键词的 QQ，留空时默认等于 `TARGET_QQS`
- `KEYWORDS`：旧版默认关键词，仅迁移时使用
- `KEYWORDS_FILE`：旧版关键词文件路径，仅迁移时使用
- `RULES_FILE`：多群规则文件路径，默认 `rules.json`
- `CASE_SENSITIVE`：是否大小写敏感
- `USE_REGEX`：是否把 `KEYWORDS` 当成正则表达式

服务器部署建议：

```env
HOST=0.0.0.0
PORT=8080
ONEBOT_ACCESS_TOKEN=change-me
ALLOWED_GROUPS=554817535
TARGET_QQS=2731811629
ADMIN_QQS=2731811629
KEYWORDS=裤子
KEYWORDS_FILE=keywords.json
RULES_FILE=rules.json
CASE_SENSITIVE=false
USE_REGEX=false
```

## 5. Ubuntu 启动机器人

```bash
cd /path/to/Monitor
source .venv/bin/activate
python3 bot.py
```

启动后会监听：

```text
0.0.0.0:8080
```

## 6. 配置服务器防火墙

如果你用了 `ufw`：

```bash
sudo ufw allow 8080/tcp
sudo ufw status
```

如果你用的是云厂商安全组，也需要放行：

```text
TCP 8080
来源建议先限制为你自己的出口 IP
```

## 7. 配置 NapCatQQ

在你 Windows 机器上的 NapCatQQ 中启用 `OneBot v11`，推荐使用 `反向 WebSocket`。

把上报地址配置为：

```text
ws://119.29.55.112:8080/onebot/v11/ws
```

如果配置了访问令牌，NapCat 和 `.env` 里的 `ONEBOT_ACCESS_TOKEN` 要保持一致。

建议同时配置 token，避免公网裸露接口。

## 8. 转发逻辑

插件位于 `plugins/keyword_forward.py`。

处理流程：

1. 只接收群消息
2. 按 `group_id` 匹配对应规则
3. 提取纯文本
4. 按该群自己的关键词或正则匹配
5. 命中后发私聊给该群规则里的目标 QQ

## 9. 多群规则配置

默认规则文件是项目根目录下的 `rules.json`：

```json
{
  "rules": [
    {
      "group_id": 554817535,
      "targets": [2731811629],
      "keywords": ["裤子"],
      "enabled": true,
      "use_regex": false
    }
  ]
}
```

你直接改这个文件即可，机器人会在收到下一条消息时自动重新加载，不需要重启服务。

也可以用命令行脚本：

```bash
cd /home/ubuntu/Monitor
. .venv/bin/activate
python3 manage_keywords.py list
python3 manage_keywords.py addgroup 123456789
python3 manage_keywords.py addkw 554817535 鞋子
python3 manage_keywords.py delkw 554817535 裤子
python3 manage_keywords.py setkw 554817535 裤子 鞋子 外套
```

## 10. 私聊命令管理规则

默认允许 `ADMIN_QQS` 中的 QQ 给机器人发送私聊命令。

支持的命令：

```text
rule list
rule addgroup 123456789
rule delgroup 123456789
rule addtarget 554817535 2731811629
rule deltarget 554817535 2731811629
rule enable 554817535
rule disable 554817535
kw list 554817535
kw add 554817535 裤子
kw remove 554817535 裤子
kw set 554817535 裤子,鞋子,外套
rule help
```

说明：

- `rule list`：查看全部群规则
- `rule addgroup`：新增一个群规则
- `rule addtarget`：给群规则增加转发目标
- `kw list`：查看某个群的关键词
- `kw add/remove/set`：修改某个群的关键词
- 命令修改后会立即写入 `rules.json`

## 11. systemd 守护进程

可以在 Ubuntu 上创建 `/etc/systemd/system/qq-forward-bot.service`：

```ini
[Unit]
Description=QQ Forward Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/Monitor
ExecStart=/path/to/Monitor/.venv/bin/python /path/to/Monitor/bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable qq-forward-bot
sudo systemctl start qq-forward-bot
sudo systemctl status qq-forward-bot
```

## 12. 常见问题

### 发不出去私聊

可能原因：

- 目标 QQ 不是好友
- QQ 客户端风控
- NapCat 登录状态异常
- token 或网络配置不一致

### 收不到群消息

重点检查：

- NapCat 的 OneBot v11 是否已启用
- 是否配置成了 `反向 WebSocket`
- 地址是否为 `ws://119.29.55.112:8080/onebot/v11/ws`
- NoneBot 是否已启动
- 群号是否配置正确
- 云服务器 `8080` 是否已放行
- 服务器进程是否监听 `0.0.0.0`

### 想支持更复杂规则

建议后续加：

- 每个群单独配置关键词
- 每个关键词转发给不同的人
- SQLite 持久化配置
- 管理命令
- 图片、@、回复消息解析

## 13. 风险提示

QQ 自动化存在账号风控风险。请只在你明确有权限的群里使用，并控制转发频率。
