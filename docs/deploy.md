# 部署与 HTTPS

> 一键部署命令见 [README「快速开始」](../README.md#快速开始)，这里只写它背后的细节。

## 部署脚本做了什么

脚本先自检运行环境，缺什么自动装什么，再拉代码启动面板：

1. 自检系统与包管理器（Debian/Ubuntu、CentOS/RHEL/Rocky/Alma/Fedora、Alpine、openSUSE、Arch）；
2. `curl`/`wget` + `ca-certificates` → `git` → **docker + docker compose v2**（官方安装脚本 → 发行版仓库 → compose 独立二进制逐级兜底，守护进程没起会自动拉起）；`git` 装不上时自动退回源码包下载；
3. 克隆到 `~/iperf3-fleet` → 生成 `.env`（随机端口）→ `docker compose up -d --build` → 等健康检查通过后打印部署横幅（含初始登录密码）。

要点：

- **可重复执行**：再次运行等于升级（`git pull` + 重新构建），数据、端口、机器接入关系全部保留；之前用过 HTTPS 会自动带上 `--profile tls`，不会把 caddy 弄丢，并顺手清理被误挂载成目录的 `Caddyfile.local`。
- **非 root 也能跑**：会通过 `sudo` 提权（需要机器上有 sudo）。
- 国内装 Docker 慢/超时：命令改成 `... | DOCKER_MIRROR=Aliyun bash`；机器走代理：命令最前面加 `https_proxy=http://IP:端口`。
- 环境变量：`IPERF3_FLEET_DIR`（安装目录，默认 `~/iperf3-fleet`）、`IPERF3_FLEET_REPO_URL`、`IPERF3_FLEET_BRANCH`、`IPERF3_FLEET_TLS=1`（同时启用 HTTPS 网关）、`DOCKER_MIRROR`、`COMPOSE_MIRROR`、`IPERF3_FLEET_TARBALL`。
- 脚本需要 `bash`（Alpine 等默认 ash 的系统先 `apk add bash`）；机器上连 `curl` 都没有时先 `apt-get update && apt-get install -y curl`（或 `yum install -y curl` / `apk add curl`）。
- 端口随机生成（记录在 `.env`，升级/重启不变），登录账号密码随机生成，均在部署横幅里显示一次；忘记密码删 `data/auth.json` 重启重新生成。
- 面板容器默认只读文件系统 + 最小权限运行。

想手动控制每一步：

```bash
git clone https://github.com/se-tang/iperf3-fleet.git && cd iperf3-fleet && ([ -f .env ] || echo "PANEL_PORT=$(shuf -i 10000-30000 -n 1)" > .env) && docker compose up -d --build && sleep 5 && docker compose logs --tail 15 iperf3-fleet
```

## HTTPS（可选）

**方式 A：80/443 空闲** — Caddy 自动签发证书并反代（Caddy 本身就是反向代理，不需要再装 nginx / 宝塔）：

1. 域名 A 记录指向面板机公网 IP，并放行安全组/防火墙的 **80 与 443**（签发证书要求公网能访问 80）；
2. 写域名 + 开安全 Cookie，再拉起 Caddy：

```bash
cd ~/iperf3-fleet
echo "PANEL_DOMAIN=panel.example.com" >> .env    # ← 换成你自己的域名，必须设
echo "PANEL_COOKIE_SECURE=1" >> .env
cp -T Caddyfile.acme Caddyfile.local
docker compose --profile tls up -d
```

3. 确认证书签发与反代是否正常：

```bash
docker compose --profile tls logs --tail 30 caddy   # 出现 certificate obtained successfully 即成功
curl -I https://panel.example.com                   # 返回 200 / 302
```

`Caddyfile.acme` 里就是一句 `reverse_proxy iperf3-fleet:8000`，容器名走 compose 内网，端口不用填。

> `PANEL_DOMAIN` 没设或为空时 Caddy 起不来（Caddyfile 的站点地址变成空，日志报 Caddyfile 解析错误）——补上域名重新 `docker compose --profile tls up -d` 即可。
> 80 端口被别的服务占用时 ACME HTTP-01 校验必然失败（拿不到证书），请腾出 80 或改用方式 B。

**方式 B：80/443 被占用 + Cloudflare 橙云** — 用 Cloudflare 源证书（15 年有效，无需续期）。`Caddyfile.origin` 直接监听 `:443`，**不依赖 `PANEL_DOMAIN`**：

1. Cloudflare → SSL/TLS → **源服务器（Origin Server）** → 创建证书 → 把显示的证书和私钥分别存为面板机 `~/iperf3-fleet/certs/origin.pem`、`origin.key`
2. 切换 Caddy 配置并改端口（443 被占时用 8443）：

```bash
cd ~/iperf3-fleet
cp -T Caddyfile.origin Caddyfile.local
echo "CADDY_HTTPS=8443" >> .env
echo "PANEL_COOKIE_SECURE=1" >> .env
docker compose --profile tls up -d
```

3. Cloudflare **Rules → Origin Rules**（或 Rules → Overview → Create rule → Origin Rule）建一条规则：Hostname equals `panel.example.com` → Destination Port rewrite to `8443`
4. Cloudflare **SSL/TLS → 概述** 模式设为 **完全（严格）/ Full (strict)**

之后访问 `https://panel.example.com`。

> 若之前在未创建 `Caddyfile.local` 时启动过容器，Docker 会把它误建成一个**目录**（`ls -la` 显示为 `drwxr-xr-x`）。先 `rm -rf Caddyfile.local` 再重新执行上面的 `cp -T` 即可。

确认 HTTPS 正常后，在 `.env` 追加 `PANEL_BIND=127.0.0.1` 并 `docker compose --profile tls up -d`，关闭公网 HTTP 直连（面板与 Agent 均走域名接入）。

> ⚠️ 改成 `PANEL_BIND=127.0.0.1` 后，端口不再对外监听，**之前用 `http://IP:端口` 接入的 Agent 会全部失联**：回面板用 `https://你的域名` 重新生成接入命令，在每台机器上重跑一次即可（重复接入会用新地址重启服务，机器令牌不变）。还没切域名接入前，先别急着加 `PANEL_BIND`。

方式 B 下 Agent 真实 IP 取自 CF-Connecting-IP，**源站防火墙/安全组务必只放行 Cloudflare 回源网段**（https://www.cloudflare.com/ips/），否则能直连 Caddy 的人可自带该头伪造 agent_ip。

注意：登录限速按来源 IP 计，经反代部署时所有登录共享同一桶——他人 5 次失败会短暂锁住登录页（300 秒）。

## 升级

```bash
cd ~/iperf3-fleet && git pull && docker compose --profile tls up -d --build
```

或者直接重跑一键脚本（自动 `git pull` + 重新构建，并自动保留 tls profile）：

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/iperf3-fleet/main/install.sh | bash
```

数据、机器接入关系、端口全部保留，Agent 自动重连，登录密码不变。

若升级时 caddy 报 `path does not exist`，说明之前的误挂载产生了同名目录，清一次即可：

```bash
rm -rf ~/iperf3-fleet/Caddyfile.local && cp -T ~/iperf3-fleet/Caddyfile.acme ~/iperf3-fleet/Caddyfile.local && docker compose --profile tls up -d
```

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
