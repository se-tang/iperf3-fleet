# iperf3-fleet

Docker 化的 iperf3 多机线路质量测试面板：一台**目标机**（被测端）+ 多台**后端机**（发起端），逐台测速并生成带线路质量评价的 Markdown 报告。机器通过 Agent 接入，**面板不保存任何 SSH 凭据**。

- 一键部署：全新机器一条命令，缺 `git` / `docker` 自动装好
- 测试参数可调：iperf3 线程数 `-P`、单向时长 `-t`、端口 `-p`、TCP / UDP（`-u -b`）、ping 次数 `-c`
- 测速全局串行（避免互相抢带宽），ping 并行；报告含丢包、RTT、抖动、上下行、重传、线路质量评价与每台原始数据
- 默认 IPv4，双端都有 IPv6 时可切换 IPv6 测试
- 面板容器只读文件系统 + 最小权限运行；端口与登录密码随机生成
- 可选 HTTPS：Caddy 自动签发证书，或 Cloudflare 源证书（配橙云）

## 快速开始

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/iperf3-fleet/main/install.sh | bash
```

装完会打印面板地址和初始登录账号密码（只显示一次，请立即保存）。脚本会先自检环境，缺 `git` / `docker` 自动装好。

1. 登录面板 → 「添加机器」（名称 / 角色 / 地区 / 带宽）→ 复制**接入命令**，到目标机器上以 root 执行 → 等它上线；
2. 选目标机、勾选后端机，按需设置测试参数 → 「开始测试」（默认 IPv4；要测 IPv6 需双端都支持，会二次确认）；
3. 完成后复制或下载 Markdown 报告。删除机器会自动卸载该机器上的 Agent。

## 文档

| 文档 | 内容 |
|:--|:--|
| [部署与 HTTPS](docs/deploy.md) | 部署脚本细节、环境变量、域名与证书（Caddy / Cloudflare）、**升级**、卸载 |
| [使用细节](docs/usage.md) | 可调参数对照表、测试流程与耗时、IPv4/IPv6、地址自动采集、报告、端口放行 |
| [排障](docs/troubleshooting.md) | 机器在线却探测不到 IPv6、任务全部超时、证书签发失败等 |
| [开发](docs/development.md) | 目录结构、跑测试、防呆钩子、改动生效范围 |
