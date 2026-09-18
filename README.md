# iperf3-fleet

一个 Docker 化的 iperf3 多机线路质量测试面板。网页上添加若干台 VPS（**Agent 接入，面板不保存任何 SSH 密码/密钥**），指定其中一台为**目标机器**（被测端），其余为**后端机器**（发起测试端），一键即可逐台完成线路测试并生成带**线路质量评价**的 Markdown 报告。

## 一键部署（复制即用）

**方式一：git 克隆** —— 下面整行复制到你的服务器终端执行即可（`git clone` 会自动创建目录，不需要提前手动建）：

```bash
git clone https://github.com/se-tang/iperf3-fleet.git && cd iperf3-fleet && docker compose up -d --build && sleep 5 && docker compose logs --tail 15 iperf3-fleet
```

**方式二：网页下载 ZIP** —— 仓库页面右上角 **Code → Download ZIP**，把压缩包传到你的机器解压，进入解压出来的目录（一般叫 `iperf3-fleet-main`），执行：

```bash
docker compose up -d --build && sleep 5 && docker compose logs --tail 15 iperf3-fleet
```

> 原则就一条：**在你放代码的那个目录里执行 `docker compose up -d --build`**，面板就部署在哪，对目录名没有任何要求。

部署完成（或随时执行 `docker compose logs iperf3-fleet`）会看到这样的提示：

```
==================================================
 ✅  iperf3-fleet 面板已部署成功！
     面板地址:  http://1.2.3.4:8088
     登录账号:  admin
     登录密码:  fleet-3fa2b90c
 下一步: 打开面板 → 添加机器 → 复制接入命令到机器上执行
==================================================
```

- 面板带**登录验证**：首次启动自动生成随机密码（同时保存在 `./data/auth.json`，可修改该文件后 `docker compose restart` 生效；也可用环境变量 `PANEL_USER`/`PANEL_PASSWORD` 指定）。
- 若修改了 compose 里的端口映射，请同步修改环境变量 `PANEL_PORT`，横幅里的地址才会正确。

## 功能

- **Agent 接入，零凭据存储**：添加机器只填名称/角色/地区/带宽，保存后面板给出一条接入命令，在机器上以 root 执行即完成接入；面板与机器之间由 Agent 主动外连（机器无需开放任何入站管理端口），令牌可随时重置。
- **登录验证**：所有页面与接口需要登录，内部数据不裸奔。
- **iperf3 自动安装**：测试时自动通过 Agent 在目标机/后端机检测并安装 iperf3（apt / dnf / yum+EPEL / apk / zypper）。
- **自动测试流程**（逐台串行，单线程 10 秒规范）：
  1. 目标机启动 `iperf3 -s`（掉线自动重启，测试结束自动关闭）；
  2. 每台后端依次执行 `iperf3 -c 目标 -t 10`（上行）、`iperf3 -c 目标 -R -t 10`（下行）、`ping -c 200 -i 1 目标`；
  3. 一台测完自动测下一台；实时日志、可中途停止。
- **测试报告**：Markdown 汇总表（后端机器、地区、带宽、丢包率、RTT、抖动、上行/下行、重传、**线路质量评价**列）+ 每台机器原始摘录，一键复制/下载 `.md`。
- **质量评价依据**：丢包率、抖动(mdev)、重传次数、上下行收发比、**标称带宽达成率**。评级：优秀 / 良好 / 一般 / 较差。

## 使用步骤

1. **登录面板**：用部署提示里的账号密码登录。
2. **添加机器**：填名称、角色（后端/目标）、地区、带宽 → 保存 → 面板切换到「接入 Agent」页 → 复制命令到该机器上以 root 执行 → 等待显示「✅ 已上线」。机器离线后重新接入可点列表里的「接入命令」按钮；令牌泄露可一键重置。
3. **发起测试**：选择目标机器、勾选后端机器 → 开始测试 → 查看实时日志。
4. **复制报告**：测试完成后复制 Markdown 报告或下载 `.md`。

Agent 在机器上装了什么：`/usr/local/lib/iperf3-fleet/agent.sh`（心跳+执行循环）、`/etc/iperf3-fleet/agent.conf`（面板地址与令牌）、systemd 服务 `iperf3-fleet-agent`（无 systemd 时用 nohup+cron 兜底）。

## 报告样式

| 后端机器 | 地区 | 带宽 | 丢包率 | RTT avg | 抖动 mdev | 上行 (sender) | 下行 (receiver) | 重传 | 线路质量评价 |
|---|---|---|---|---|---|---|---|---|---|
| HK-01 | 香港 | 500M | 0% | 144.4 ms | 0.26 ms | 137 Mbit/s | 108 Mbit/s | 0 | **一般**：0%丢包，抖动0.26ms极小，全程无重传，上行仅约标称500M的27%，下行仅约标称500M的22% |

原始数据部分（每台机器一段）：

```
200 packets transmitted, 200 received, 0% packet loss, time 199034ms
rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms

[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.00  sec   163 MBytes   137 Mbits/sec    0            sender
[  5]   0.00-10.15  sec   163 MBytes   135 Mbits/sec                  receiver

[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.15  sec   147 MBytes   121 Mbits/sec    0            sender
[  5]   0.00-10.00  sec   129 MBytes   108 Mbits/sec                  receiver
```

## 一键卸载

**卸载面板端**（在部署面板的机器上；若已身处代码目录内，跳过第一行）：

```bash
cd ~/iperf3-fleet
docker compose down && rm -rf data && docker rmi iperf3-fleet:latest
```

连同代码目录一起删除：

```bash
cd ~ && rm -rf iperf3-fleet
```

**卸载机器端 Agent**（在每台接入过的机器上执行；面板还在时最方便）：

```bash
curl -fsSL http://面板IP:8088/agent/uninstall.sh | bash
```

如果面板已经删掉了，直接在机器上执行这段（与卸载脚本等效）：

```bash
systemctl disable --now iperf3-fleet-agent 2>/dev/null; rm -f /etc/systemd/system/iperf3-fleet-agent.service; systemctl daemon-reload 2>/dev/null; pkill -f "iperf3-fleet/agent.sh" 2>/dev/null; crontab -l 2>/dev/null | grep -v "iperf3-fleet/agent.sh" | crontab - 2>/dev/null; rm -rf /usr/local/lib/iperf3-fleet /etc/iperf3-fleet /var/lib/iperf3-fleet; echo "✅ agent 已卸载"
```

## 注意事项

- **目标机防火墙**需放行 `5201/TCP`（iperf3 默认端口）并允许 ICMP；**面板机**需放行 `8088/TCP`。
- 机器上的 Agent 以 root 运行（安装 iperf3 需要），Agent 只执行面板下发的测试相关命令。
- Agent 与面板之间是 HTTP + 令牌认证；跨公网使用建议在面板前套一层 Nginx/Caddy HTTPS 反代。
- 面板数据（SQLite、登录凭据）在 `./data` 目录，**不会被 git 提交**；仓库里不含任何密钥。
- iperf3 测试走的是 Agent 上报的出口 IP（`Agent IP` 列）；若多台机器在同一 NAT 后面，它们互相可能无法直连 5201 端口，这类环境不适合本工具的组网。
- 同一时刻只允许一个测试任务运行，可在详情页随时停止。

## 项目结构

```
iperf3-fleet/
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh        # 容器入口：启动面板 + 打印部署成功横幅
├── banner.py            # 部署横幅（面板地址/账号密码）
├── requirements.txt
├── app/
│   ├── app.py           # Flask：登录验证、机器管理、Agent API、测试任务
│   ├── runner.py        # 测试编排（任务派发/轮询、实时日志、停止）
│   ├── agent_jobs.py    # 派发给机器执行的 shell 任务（装 iperf3、启停 server 等）
│   ├── agent/
│   │   ├── install.sh   # 机器端一键安装脚本（面板下发）
│   │   └── uninstall.sh # 机器端一键卸载脚本
│   ├── quality.py       # 结果解析、质量评价、Markdown 报告生成
│   ├── db.py            # SQLite：机器(令牌)/任务队列/测试记录/登录凭据
│   └── templates/       # 登录页 + 面板页
└── tests/test_sample.py # 解析/评价/报告的样例数据自测
```

## 自测

```bash
python tests/test_sample.py
```
