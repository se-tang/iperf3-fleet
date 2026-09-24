# 排障

先看两处日志：

```bash
# 面板侧
docker compose --profile tls logs --tail 100 iperf3-fleet
# 节点机侧
tail -20 /var/lib/iperf3-fleet/agent.log
```

| 现象 | 原因 / 处理 |
|:--|:--|
| 机器显示**在线**，但测试地址旁有 ⚠️、或一直显示「无 IPv6」 | 面板下发的地址探测任务没被 Agent 执行完。机器列表的 ⚠️ 鼠标悬停会写明原因；面板日志 `grep 探测` 可见 `[探测] 机器 #N 地址: {...}`（成功）或 `未获取到全局地址`。 |
| 机器上 `agent.log` 出现「**拒绝执行：任务 #N 为重放**」 | Agent 本地记录的任务编号水位比面板当前编号大：面板数据库被重置（重新部署 / 删过 `data`）后任务编号从 1 重新开始，Agent 会把之后所有任务当重放**静默拒绝**。当前版本的面板任务编号改为时间戳打底，**升级即自愈，无需重装任何 Agent**；重新执行一次接入命令也会重置水位。 |
| 测试任务全部「任务执行超时」，机器上却没有日志 | 同上，Agent 拒收了任务。 |
| 机器上 `ip -6 addr show scope global` 只有 `fe80::` 或 `fc00::` / `fd00::` | 这台机器确实没有**全局可路由**的 IPv6（链路本地 / ULA 不能用于跨公网测试），面板显示「无 IPv6」是正确结果，不是探测故障。 |
| 机器在线但地址一直停在「探测中…」 | 心跳正常只说明连通；地址要靠探测任务补齐，点「探测地址」手动重试即可。 |
| Agent 一直不上线 | 机器上 `systemctl status iperf3-fleet-agent` 与 `agent.log`：`curl_exit=60` 多为证书问题（用 IP 接入却配了 https 域名），`curl_exit=28` 是连不上面板（防火墙 / 安全组 / DNS），`curl_exit=22` 是令牌被面板拒绝（机器被删过，需重新接入）。 |
| 面板打不开 | `docker compose ps` 看容器状态与健康检查；`docker compose logs --tail 50 iperf3-fleet`。 |
| Caddy 证书签不下来 | 域名 A 记录是否指向本机、安全组是否放行 80/443、80 是否被别的服务占用；`docker compose --profile tls logs --tail 30 caddy`。 |
| 面板容器反复重启 | 常见是端口冲突（改 `.env` 的 `PANEL_PORT` 后 `docker compose up -d`）或磁盘空间不足。 |
