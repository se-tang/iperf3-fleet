# iperf3-fleet

Docker 化的 iperf3 多机线路质量测试面板。Agent 接入（面板不保存任何 SSH 凭据），一台**目标机器**（被测端）+ 多台**后端机器**（发起端），自动逐台测试并生成带线路质量评价的 Markdown 报告。

## 部署

```bash
git clone https://github.com/se-tang/iperf3-fleet.git && cd iperf3-fleet && ([ -f .env ] || echo "PANEL_PORT=$(shuf -i 10000-30000 -n 1)" > .env) && docker compose up -d --build && sleep 5 && docker compose logs --tail 15 iperf3-fleet
```

- 端口随机生成（记录在 `.env`，升级/重启不变），登录账号密码随机生成，均在部署横幅里显示。
- 面板容器默认只读文件系统 + 最小权限运行。

## HTTPS（可选）

**方式 A：80/443 空闲** — Caddy 自动签发证书，部署命令执行完即可：

```bash
cd ~/iperf3-fleet && git pull && docker compose --profile tls up -d
```

**方式 B：80/443 被占用 + Cloudflare 橙云** — 用 Cloudflare 源证书（15 年有效，无需续期）：

1. Cloudflare → SSL/TLS → **源服务器（Origin Server）** → 创建证书 → 把显示的证书和私钥分别存为面板机 `~/iperf3-fleet/certs/origin.pem`、`origin.key`
2. 切换 Caddy 配置并改端口（443 被占时用 8443）：

```bash
cd ~/iperf3-fleet
cp Caddyfile.origin Caddyfile
echo "CADDY_HTTPS=8443" >> .env
echo "PANEL_COOKIE_SECURE=1" >> .env
docker compose --profile tls up -d
```

3. Cloudflare **Rules → Origin Rules**（或 Rules → Overview → Create rule → Origin Rule）建一条规则：Hostname equals `panel.example.com` → Destination Port rewrite to `8443`
4. Cloudflare **SSL/TLS → 概述** 模式设为 **完全（严格）/ Full (strict)**

之后访问 `https://panel.example.com`。确认 HTTPS 正常后可删 compose 里的 `ports` 端口映射关闭 HTTP 直连。

## 使用

1. 登录面板 → 添加机器（名称/角色/地区/带宽）→ 复制接入命令到机器 root 执行 → 上线。
2. 选目标机、勾后端机 → 开始测试：iperf3 上/下行各 10 秒全局串行，ping 200 次并行，总时长 ≈ 1 分钟 + 台数×25 秒 + 3.5 分钟。
3. 完成后复制/下载 Markdown 报告（丢包、RTT、抖动、上下行、重传、线路质量评价 + 每台原始数据）。删除机器会自动卸载其 Agent。

注意：目标机放行 `5201/TCP` 与 ICMP，面板机放行 `.env` 里的端口；跨公网建议套 HTTPS 反代。

## 升级

```bash
cd ~/iperf3-fleet && git pull && docker compose up -d --build
```

数据、机器接入关系、端口全部保留，Agent 自动重连。

## 卸载

面板机：

```bash
cd ~/iperf3-fleet && docker compose down && rm -rf data && rm -f .env && docker rmi iperf3-fleet:latest
```

节点机：

```bash
curl -fsSL http://面板IP:端口/agent/uninstall.sh | bash
```

面板已删则在节点机直接执行：

```bash
systemctl disable --now iperf3-fleet-agent 2>/dev/null; rm -f /etc/systemd/system/iperf3-fleet-agent.service; systemctl daemon-reload 2>/dev/null; pkill -f "iperf3-fleet/agent.sh" 2>/dev/null; rm -rf /usr/local/lib/iperf3-fleet /etc/iperf3-fleet /var/lib/iperf3-fleet
```
