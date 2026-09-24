# iperf3-fleet

Docker 化的 iperf3 多机线路质量测试面板：一台**目标机**（被测端）+ 多台**后端机**（发起端），逐台测速并生成带线路质量评价的 Markdown 报告。机器通过 Agent 接入，**面板不保存任何 SSH 凭据**。

- 一键部署：全新机器一条命令，缺 `git` / `docker` 自动装好
- iperf3 上行/下行 + ping 200 次：测速全局串行（避免互相抢带宽），ping 并行
- 报告含丢包、RTT、抖动、上下行、重传、线路质量评价与每台原始数据
- 默认 IPv4，双端都有 IPv6 时可切换 IPv6 测试
- 面板容器只读文件系统 + 最小权限运行；端口与登录密码随机生成
- 可选 HTTPS：Caddy 自动签发证书，或 Cloudflare 源证书（配橙云）

## 部署

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/iperf3-fleet/main/install.sh | bash
```

装完会打印面板地址和初始登录账号密码（只显示一次，请立即保存）。

## 使用

1. 登录面板 → 「添加机器」（名称 / 角色 / 地区 / 带宽）→ 复制**接入命令**，到目标机器上以 root 执行 → 等它上线；
2. 选目标机、勾选后端机 → 「开始测试」（默认 IPv4；要测 IPv6 需双端都支持，会二次确认）；
3. 完成后复制或下载 Markdown 报告。删除机器会自动卸载该机器上的 Agent。

目标机需放行 `5201/TCP` 与 ICMP（建议只放行后端机 IP），面板机放行 `.env` 里的端口。

## 升级

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/iperf3-fleet/main/install.sh | bash
```

数据、机器接入关系、端口全部保留，Agent 自动重连，登录密码不变。

## 文档

| 文档 | 内容 |
|:--|:--|
| [部署与 HTTPS](docs/deploy.md) | 部署脚本做了什么、环境变量、域名与证书（Caddy / Cloudflare）、升级细节、卸载 |
| [使用细节](docs/usage.md) | 测试流程与耗时、IPv4/IPv6 协议选择、机器地址自动采集、报告说明 |
| [排障](docs/troubleshooting.md) | 机器在线却探测不到 IPv6、任务全部超时、证书签发失败等 |
| [开发](docs/development.md) | 目录结构、提交前的防呆钩子、怎么跑测试 |
