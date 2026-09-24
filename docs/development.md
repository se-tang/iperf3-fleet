# 开发

## 目录结构

```
app/
  app.py              面板 Web 服务：登录、机器管理、Agent API、任务接口
  db.py               SQLite 数据层：机器、任务队列、测试记录、登录凭据
  runner.py           测试编排：环境准备 → iperf3 串行 → ping 并行 → 报告
  agent_jobs.py       下发给 Agent 执行的脚本（装 iperf3、起 server、地址探测）
  agent/              节点机接入 / 卸载脚本（install.sh 由面板下发给节点）
  templates/          前端单页 index.html
tests/test_sample.py  解析 / 评价 / 报告 / 协议 / 地址探测 / 任务编号 测试
install.sh            面板一键部署脚本
Caddyfile.acme        自动签证书反代模板（方式 A）
Caddyfile.origin      Cloudflare 源证书反代模板（方式 B）
docker-compose.yml    面板（+ 可选 caddy profile）
```

## 跑测试

```bash
python tests/test_sample.py     # 直接运行全部用例
```

纯标准库，不需要装依赖；涉及 shell 的用例（`SCRIPT_REPORT_IPS` 地址提取）只在 POSIX（Linux / macOS / 容器）下执行，Windows 上自动跳过。

## 提交前

```bash
git config core.hooksPath hooks
```

启用防呆钩子：拦截 `data*/`、`auth.json`、`secret_key`、`panel.db*` 及疑似密钥内容的提交（确认为误报时 `--no-verify` 可跳过）。

## 改动的生效范围

| 改什么 | 怎么生效 |
|:--|:--|
| 面板（`app.py` / `db.py` / `runner.py` / `agent_jobs.py` / 模板） | 面板机 `git pull && docker compose up -d --build`，节点机无需任何操作 |
| `app/agent/install.sh`（Agent 本体） | 必须到节点机重跑一次接入命令（面板会下发新脚本） |

`app/agent/install.sh` 会下发到所有节点，**重复执行必须幂等**；下发给 Agent 的任务脚本（`agent_jobs.py`）会随心跳生效，不用重装 Agent。
