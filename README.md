# iperf3 线路测试面板

一个 Docker 化的 iperf3 多机线路质量测试面板。在网页上添加若干台 VPS（SSH 方式管理），指定其中一台为**目标机器**（被测端），其余为**后端机器**（发起测试端），一键即可逐台完成线路测试并生成带**线路质量评价**的 Markdown 报告。

## 一键部署（复制即用）

**方式一：git 克隆** —— 下面整行复制到你的服务器终端执行即可（`git clone` 会自动创建目录，不需要提前手动建）：

```bash
git clone https://github.com/se-tang/iperf3-fleet.git && cd iperf3-fleet && docker compose up -d --build
```

**方式二：网页下载 ZIP** —— 仓库页面右上角 **Code → Download ZIP**，把压缩包传到你的机器解压，进入解压出来的目录（一般叫 `iperf3-fleet-main`），执行：

```bash
docker compose up -d --build
```

> 原则就一条：**在你放代码的那个目录里执行 `docker compose up -d --build`**，面板就部署在哪，对目录名没有任何要求。

部署完成后浏览器打开 `http://服务器IP:8088` 进入面板。测试数据（SQLite 数据库 + 测试记录）保存在代码目录下的 `./data` 文件夹，升级代码不影响数据。

> 不想用 Docker 也可以：`pip install -r requirements.txt && python -m app.app`（Windows 默认数据目录为项目下 `data/`，Linux 下为 `/data`，可用环境变量 `DATA_DIR` 覆盖）。

## 功能

- **机器管理**：添加/编辑/删除机器，支持命名、**带宽标记**（如 `500M`/`1G`）、**地区标记**（如 `香港`），角色分为「后端机器」和「目标机器」。
- **iperf3 自动安装**：测试前自动检测目标机/后端机是否安装 iperf3（以及 ping），缺失时通过 apt / dnf / yum(+EPEL) / apk / zypper 自动安装；面板上也可手动对单台机器执行「测SSH + 自动安装」。
- **自动测试流程**（逐台串行，符合单线程 10 秒规范）：
  1. 目标机启动 `iperf3 -s`（掉线自动重启，测试结束自动关闭）；
  2. 每台后端依次执行：
     - `iperf3 -c 目标IP -t 10`（上行）
     - `iperf3 -c 目标IP -R -t 10`（下行）
     - `ping -c 200 -i 1 目标IP`（200 次，约 200 秒）
  3. 一台测完自动测下一台；全程实时日志可在面板查看，支持中途停止。
- **测试报告**：自动汇总为 Markdown 表格（后端机器、地区、带宽、丢包率、RTT、抖动、上行/下行、重传、**线路质量评价**列），并附每台机器的原始摘录（ping 汇总两行 + 上下行汇总三行）。支持一键复制 / 下载 `.md`。
- **质量评价依据**：丢包率、抖动(mdev)、重传次数、上下行收发比，以及**标称带宽达成率**（如标记 `500M` 实测只有 137M 会在评价中指出）。评级：优秀 / 良好 / 一般 / 较差。

## 使用步骤

1. **添加机器**：点击「添加机器」，填写名称、IP、SSH 端口/用户、密码或私钥，设置角色（后端/目标），并填写带宽与地区标记。
2. **测SSH（可选但推荐）**：验证 SSH 可连、iperf3 是否已装，未装可一键自动安装。
3. **发起测试**：选择目标机器、勾选要参与的后端机器，点击「开始测试」。每台后端约需 4 分钟（10s + 10s + 200s）。
4. **查看/复制报告**：测试完成后在详情页查看汇总表与原始数据，点「复制 Markdown 报告」即可粘贴到别处。

## 报告样式

汇总表（每台一行，最后一列为线路质量评价）：

| 后端机器 | 地区 | 带宽 | 丢包率 | RTT avg | 抖动 mdev | 上行 (sender) | 下行 (receiver) | 重传 | 线路质量评价 |
|---|---|---|---|---|---|---|---|---|---|
| HK-01 | 香港 | 500M | 0% | 144.4 ms | 0.26 ms | 137 Mbit/s | 108 Mbit/s | 0 | **一般**：0%丢包，抖动0.26ms极小，全程无重传，上行仅约标称500M的27%，下行仅约标称500M的22% |

原始数据部分与手工测试的格式一致（每台机器一段）：

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

## 注意事项

- **目标机防火墙**需放行 `5201/TCP`（iperf3 默认端口），并允许 ICMP（否则 ping 全丢包）。
- 建议 SSH 账号是 **root**，或具有免密 sudo 的账号（自动安装 iperf3 需要）。
- SSH 密码/私钥保存在面板容器的 SQLite（`./data/panel.db`）中，请自行保证面板机的安全，不要把面板暴露到公网未加防护的环境。
- 面板启动 `iperf3 -s` 时会 `pkill iperf3` 清理目标机上残留的 iperf3 进程，请勿在目标机上跑其他 iperf3 任务。
- 同一时刻只允许一个测试任务运行（避免互相干扰），可在任务详情页随时停止。

## 项目结构

```
iperf3-fleet/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── app/
│   ├── app.py          # Flask API
│   ├── runner.py       # 测试编排（串行执行、实时日志、停止）
│   ├── ssh_utils.py    # SSH 连接/执行、iperf3 自动安装、server 启停
│   ├── quality.py      # 结果解析、质量评价、Markdown 报告生成
│   ├── db.py           # SQLite 存取
│   └── templates/index.html
└── tests/test_sample.py  # 解析/评价/报告的样例数据自测
```

## 自测

```bash
python tests/test_sample.py
```
