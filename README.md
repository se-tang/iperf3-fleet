# iperf3-fleet

Docker 化的 iperf3 多机线路质量测试面板。Agent 接入（面板不保存任何 SSH 凭据），一台**目标机器**（被测端）+ 多台**后端机器**（发起端），自动逐台测试并生成带线路质量评价的 Markdown 报告。

## 部署

全新机器一条命令（脚本先自检运行环境，缺 `git` / `docker` 自动装好，再拉代码并启动面板）：

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/iperf3-fleet/main/install.sh | bash
```

脚本做的事：

1. 自检系统与包管理器（Debian/Ubuntu、CentOS/RHEL/Rocky/Alma/Fedora、Alpine、openSUSE、Arch）；
2. 缺什么装什么：`curl`/`wget` + `ca-certificates` → `git` → **docker + docker compose v2**（官方安装脚本 → 发行版仓库 → compose 独立二进制，逐级兜底；守护进程没起也会自动拉起）；
3. 克隆到 `~/iperf3-fleet` → 生成 `.env`（随机端口）→ `docker compose up -d --build` → 等健康检查通过后打印部署横幅（含初始登录密码）。

- **可重复执行**：再次运行等于升级（`git pull` + 重新构建），数据、端口、机器接入关系全部保留；之前用过 HTTPS 会自动带上 `--profile tls`，不会把 caddy 弄丢。
- **非 root 也能跑**：会通过 `sudo` 提权（需要机器上有 sudo）。
- 国内装 Docker 慢/超时：`curl -fsSL https://raw.githubusercontent.com/se-tang/iperf3-fleet/main/install.sh | DOCKER_MIRROR=Aliyun bash`；机器走代理：命令前加 `https_proxy=http://IP:端口`。
- 可用环境变量：`IPERF3_FLEET_DIR`（安装目录）、`IPERF3_FLEET_BRANCH`（分支）、`IPERF3_FLEET_TLS=1`（同时启用 HTTPS 网关）、`DOCKER_MIRROR`、`COMPOSE_MIRROR`。
- 脚本需要 `bash`：用上面的 `| bash` 执行即可（Alpine 等默认 ash 的系统先 `apk add bash`）。
- 机器上连 `curl` 都没有时（极少见），先 `apt-get update && apt-get install -y curl`（Debian/Ubuntu）或 `yum install -y curl`（CentOS/RHEL）或 `apk add curl`（Alpine）。

想手动控制每一步：

```bash
git clone https://github.com/se-tang/iperf3-fleet.git && cd iperf3-fleet && ([ -f .env ] || echo "PANEL_PORT=$(shuf -i 10000-30000 -n 1)" > .env) && docker compose up -d --build && sleep 5 && docker compose logs --tail 15 iperf3-fleet
```

- 端口随机生成（记录在 `.env`，升级/重启不变），登录账号密码随机生成，均在部署横幅里显示一次；忘记密码删 `data/auth.json` 重启重新生成。
- 面板容器默认只读文件系统 + 最小权限运行。

## HTTPS（可选）

**方式 A：80/443 空闲** — Caddy 自动签发证书：

```bash
cd ~/iperf3-fleet && git pull && cp -T Caddyfile.acme Caddyfile.local && docker compose --profile tls up -d
```

**方式 B：80/443 被占用 + Cloudflare 橙云** — 用 Cloudflare 源证书（15 年有效，无需续期）：

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

确认 HTTPS 正常后，在 `.env` 追加 `PANEL_BIND=127.0.0.1` 并 `docker compose up -d`，关闭公网 HTTP 直连（面板与 Agent 均走域名接入）。
方式 B 下 Agent 真实 IP 取自 CF-Connecting-IP，**源站防火墙/安全组务必只放行 Cloudflare 回源网段**（https://www.cloudflare.com/ips/），否则能直连 Caddy 的人可自带该头伪造 agent_ip。

注意：登录限速按来源 IP 计，经反代部署时所有登录共享同一桶——他人 5 次失败会短暂锁住登录页（300 秒）。

## 使用

1. 登录面板 → 添加机器（名称/角色/地区/带宽）→ 复制接入命令到机器 root 执行 → 上线（接入脚本会自动补齐 `curl`、`openssl`，全新机器可直接跑；`iperf3`/`ping` 由面板在开测前自动安装）。
2. 选目标机、勾后端机 → 开始测试：iperf3 上/下行各 10 秒全局串行，ping 200 次并行，总时长 ≈ 1 分钟 + 台数×25 秒 + 3.5 分钟。
3. 完成后复制/下载 Markdown 报告（丢包、RTT、抖动、上下行、重传、线路质量评价 + 每台原始数据）。删除机器会自动卸载其 Agent。

### 测试协议（IPv4 / IPv6）

默认 **IPv4**：iperf3 与 ping 都用目标机的 IPv4 地址，两端固定为 IPv4——避免个别机器没有 IPv6 时整轮测试失败。

需要测 IPv6 时，勾选「**使用 IPv6 测试（需双端均支持 IPv6）**」（会二次确认）。勾选后改用目标机的 IPv6 地址，**所有选中的后端机器都必须具备 IPv6 连通性**，否则该台机器会失败；面板会在选项旁提示未探测到 IPv6 的后端机器。

机器地址由面板自动采集，无需手填：

- Agent 心跳的来源 IP 按协议族分别记录（双栈面板下 v4/v6 互不覆盖，测试地址不会在两族之间跳动）；
- 地址不全时，面板会通过签名任务让 Agent 在本机执行一次地址探测（`ip route get` / `ip -6 addr`，只做本地查询、不访问外网、不产生流量），因此**面板只监听 IPv4 也能拿到机器的 IPv6**；
- 机器列表的「测试地址」列显示 v4 / v6，可用「探测地址」手动刷新（例如机器换 IP 后）；
- Agent 无需升级即可支持新地址采集。

> 探测只接受全局可路由地址（私网、链路本地、ULA、临时 IPv6 地址会被忽略）；处于 NAT 后的机器会沿用面板观测到的出口 IP。

注意：目标机放行 `5201/TCP` 与 ICMP（安全组建议仅放行后端机 IP，防扫描盗刷流量）；面板机放行 `.env` 里的端口；跨公网建议套 HTTPS 反代。

## 仓库贡献

执行 `git config core.hooksPath hooks` 启用防呆钩子：拦截 `data*/`、`auth.json`、`secret_key`、`panel.db*` 及疑似密钥内容的提交。

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
