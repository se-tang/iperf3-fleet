# iperf3-fleet

Docker 化的 iperf3 多机线路质量测试面板。Agent 接入（面板不保存任何 SSH 凭据），指定一台**目标机器**（被测端），其余为**后端机器**（发起端），一键完成逐台测试并生成带线路质量评价的 Markdown 报告。

## 部署

整行复制到服务器执行：

```bash
git clone https://github.com/se-tang/iperf3-fleet.git && cd iperf3-fleet && ([ -f .env ] || echo "PANEL_PORT=$(shuf -i 10000-30000 -n 1)" > .env) && docker compose up -d --build && sleep 5 && docker compose logs --tail 15 iperf3-fleet
```

- 面板端口随机生成（10000-30000），记录在 `.env`，升级/重启不变；改端口编辑 `.env` 后 `docker compose up -d`。
- 登录账号/密码首次启动随机生成（横幅里显示，存于 `./data/auth.json`），支持 `PANEL_USER`/`PANEL_PASSWORD` 覆盖。

## 使用

1. 登录面板 → 添加机器（名称/角色/地区/带宽）→ 复制接入命令到机器 root 执行 → 上线。
2. 选目标机、勾后端机 → 开始测试（iperf3 上/下行各 10 秒全局串行，ping 200 次并行；总时长 ≈ 1 分钟 + 台数×25 秒 + 3.5 分钟）。
3. 完成后复制/下载 Markdown 报告：汇总表（丢包、RTT、抖动、上下行、重传、**线路质量评价**）+ 每台原始数据。

质量评价依据：丢包率、抖动(mdev)、重传、上下行收发比、标称带宽达成率。评级：优秀 / 良好 / 一般 / 较差。删除机器时会自动向其下发 Agent 卸载脚本（离线机器上线后补执行）。

## 升级

```bash
cd ~/iperf3-fleet && git pull && docker compose up -d --build
```

数据、机器接入关系、端口全部保留，Agent 无需重装（自动重连），数据库结构变化自动迁移。

## 卸载

面板机（代码目录内）：

```bash
docker compose down && rm -rf data && rm -f .env && docker rmi iperf3-fleet:latest
```

节点机（面板还在时）：

```bash
curl -fsSL http://面板IP:端口/agent/uninstall.sh | bash
```

面板已删则在节点机直接执行：

```bash
systemctl disable --now iperf3-fleet-agent 2>/dev/null; rm -f /etc/systemd/system/iperf3-fleet-agent.service; systemctl daemon-reload 2>/dev/null; pkill -f "iperf3-fleet/agent.sh" 2>/dev/null; crontab -l 2>/dev/null | grep -v "iperf3-fleet/agent.sh" | crontab - 2>/dev/null; rm -rf /usr/local/lib/iperf3-fleet /etc/iperf3-fleet /var/lib/iperf3-fleet; echo "✅ agent 已卸载"
```

## 安全

- Agent 只执行带面板 HMAC-SHA256 签名的任务；签名密钥仅在接入命令中一次性下发，中间人篡改任务会被 Agent 拒绝（防重放：任务编号递增）。升级后建议重新执行一次接入命令以启用验签。
- 面板登录限速（同 IP 60 秒 5 次失败锁 5 分钟）、请求体限制、安全响应头、SameSite Cookie。
- HTTP 明文下令牌可被窃听（可伪造上报、读取结果）。跨公网建议套 HTTPS 反代（Caddy 示例），并设置 `PANEL_COOKIE_SECURE=1`：
  ```
  panel.example.com {
      reverse_proxy 127.0.0.1:8088
  }
  ```
- 目标机需放行 `5201/TCP` 与 ICMP（测试前自动预检）；面板机放行 `.env` 里的端口。

## 其他

- Agent 组成：`/usr/local/lib/iperf3-fleet/agent.sh`（心跳+执行）、`/etc/iperf3-fleet/agent.conf`、systemd 服务 `iperf3-fleet-agent`（无 systemd 时 nohup+cron 兜底）。
- 同一时刻一个测试任务，可中途停止；测试记录保留最近 100 条。
- 数据在 `./data`，不入 git；仓库不含任何密钥。
- 本地自测：`python tests/test_sample.py`；模拟节点调试：`bash tests/fake_agent.sh <令牌> [面板地址] [秒数] [签名密钥]`。

## 结构

```
iperf3-fleet/
├── docker-compose.yml / Dockerfile / entrypoint.sh / banner.py
├── app/
│   ├── app.py          # Flask：登录、机器管理、Agent API、测试任务
│   ├── runner.py       # 流水线编排（iperf3 串行 / ping 并行）
│   ├── agent_jobs.py   # 派发的 shell 任务
│   ├── agent/          # 节点端安装/卸载脚本
│   ├── quality.py      # 解析、质量评价、报告生成
│   ├── db.py           # SQLite：机器/任务队列/记录/凭据
│   └── templates/      # 登录页 + 面板页
└── tests/              # 样例自测 + 模拟 agent
```
