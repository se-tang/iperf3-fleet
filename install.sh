#!/bin/bash
# ============================================================================
# iperf3-fleet 面板一键部署脚本（全新机器可直接运行）
#
#   curl -fsSL https://raw.githubusercontent.com/se-tang/iperf3-fleet/main/install.sh | bash
#
# 脚本先自检运行环境，缺什么自动装什么，再部署面板（每一步都幂等，
# 重复执行 = 升级，数据 / 端口 / 机器接入关系全部保留）：
#
#   1. curl/wget、ca-certificates —— 下载工具
#   2. git                        —— 拉取与更新代码（装不上时自动退回源码包下载）
#   3. docker + docker compose v2 —— 官方安装脚本 → 发行版仓库 → 独立二进制，逐级兜底
#   4. 克隆/更新仓库 → 生成 .env（随机端口）→ docker compose up -d --build → 等待就绪
#
# 支持的发行版：Debian/Ubuntu、CentOS/RHEL/Rocky/Alma/Fedora、Alpine、
#               openSUSE/SLES、Arch（其余系统会给出明确的失败原因）
#
# 可选环境变量（均有默认值，通常无需设置）：
#   IPERF3_FLEET_DIR       安装目录，默认 $HOME/iperf3-fleet
#   IPERF3_FLEET_REPO_URL  仓库地址，默认 https://github.com/se-tang/iperf3-fleet.git
#   IPERF3_FLEET_BRANCH    分支，默认 main
#   IPERF3_FLEET_TLS=1     同时启用 HTTPS 网关（caddy，需 80/443 空闲）
#   DOCKER_MIRROR=Aliyun   用国内镜像源装 Docker（可选值见 https://get.docker.com）
#   COMPOSE_MIRROR=...     compose 独立二进制的下载前缀（默认 GitHub 官方）
#   https_proxy=...        拉代码 / 装包走代理，例如 https_proxy=http://127.0.0.1:7890
# ============================================================================
set -u
umask 022

REPO_URL="${IPERF3_FLEET_REPO_URL:-https://github.com/se-tang/iperf3-fleet.git}"
BRANCH="${IPERF3_FLEET_BRANCH:-main}"
INSTALL_DIR="${IPERF3_FLEET_DIR:-$HOME/iperf3-fleet}"

# ---------------------------------------------------------------------------
# 输出与通用工具
# ---------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }
info() { printf '   %s\n' "$*"; }
step() { printf '\n==== %s ====\n' "$*"; }
ok()   { printf '✅ %s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*"; }
die()  { printf '❌ %s\n' "$*" >&2; exit 1; }

if [ -z "${BASH_VERSION:-}" ]; then
  if have bash && [ -n "${0:-}" ] && [ -f "${0:-}" ]; then exec bash "$0" "$@"; fi
  die "本脚本需要 bash：Debian/Ubuntu 用 apt-get install -y bash，Alpine 用 apk add bash"
fi

if [ "$(id -u)" = "0" ]; then
  SUDO=""
else
  have sudo || die "需要 root 权限：请用 root 执行，或先安装 sudo（Debian/Ubuntu: apt-get install -y sudo）"
  SUDO="sudo"
  sudo -n true 2>/dev/null || info "非 root 运行：需要提权的步骤会调用 sudo（可能提示输入密码）"
fi
as_root() { if [ -n "$SUDO" ]; then $SUDO "$@"; else "$@"; fi }
docker_cli() { if [ -n "$SUDO" ]; then $SUDO docker "$@"; else docker "$@"; fi }
compose() { docker_cli compose "$@"; }

# 推导源码包地址（git 不可用时的兜底）
derive_tarball_url() {
  case "$REPO_URL" in
    https://github.com/*)
      _slug="${REPO_URL#https://github.com/}"
      _slug="${_slug%.git}"
      printf 'https://codeload.github.com/%s/tar.gz/refs/heads/%s' "$_slug" "$BRANCH" ;;
    *) printf '' ;;
  esac
}
TARBALL_URL="${IPERF3_FLEET_TARBALL:-$(derive_tarball_url)}"

# ---------------------------------------------------------------------------
# 系统与包管理器探测
# ---------------------------------------------------------------------------
OS_PRETTY="unknown"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release 2>/dev/null || true
  OS_PRETTY="${PRETTY_NAME:-${ID:-unknown}}"
fi
ARCH="$(uname -m 2>/dev/null || echo unknown)"

PKG="none"
for _c in apt-get dnf yum apk zypper pacman; do
  if have "$_c"; then
    case "$_c" in
      apt-get) PKG=apt ;;
      dnf) PKG=dnf ;;
      yum) PKG=yum ;;
      apk) PKG=apk ;;
      zypper) PKG=zypper ;;
      pacman) PKG=pacman ;;
    esac
    break
  fi
done

pm_update() {
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive as_root apt-get update -qq ;;
    dnf) as_root dnf -q makecache ;;
    yum) as_root yum -q makecache ;;
    apk) as_root apk update -q ;;
    zypper) as_root zypper --non-interactive --quiet refresh ;;
    pacman) as_root pacman -Sy --noconfirm --quiet ;;
    *) return 1 ;;
  esac
}
pm_install() { # $@ = 当前发行版仓库里的包名
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive as_root apt-get install -y --no-install-recommends "$@" ;;
    dnf) as_root dnf install -y "$@" ;;
    yum) as_root yum install -y "$@" ;;
    apk) as_root apk add --no-cache "$@" ;;
    zypper) as_root zypper --non-interactive install "$@" ;;
    pacman) as_root pacman -S --noconfirm --needed "$@" ;;
    *) return 1 ;;
  esac
}

fetch() { # $1=url $2=输出文件
  if have curl; then
    curl -fsSL --connect-timeout 15 --retry 2 -o "$2" "$1"
  elif have wget; then
    wget -q -T 15 -O "$2" "$1"
  else
    return 1
  fi
}

# ---------------------------------------------------------------------------
# 自检 1/4：下载工具与证书
# ---------------------------------------------------------------------------
step "自检运行环境"
info "系统: $OS_PRETTY ($ARCH)，包管理器: $PKG"

ensure_downloader() {
  if have curl || have wget; then return 0; fi
  printf '   curl / wget 均缺失 → 自动安装\n'
  [ "$PKG" = "none" ] && return 1
  pm_update >/dev/null 2>&1 || true
  pm_install curl >/dev/null 2>&1 || pm_install wget >/dev/null 2>&1 || true
  have curl || have wget
}
ensure_downloader || die "缺少 curl/wget 且自动安装失败，请手动安装后重试"
if have curl; then info "curl: $(curl --version 2>/dev/null | head -n1)"; else info "wget: $(wget --version 2>/dev/null | head -n1)"; fi

ensure_ca() {
  [ -e /etc/ssl/certs/ca-certificates.crt ] && return 0
  [ -e /etc/pki/tls/certs/ca-bundle.crt ] && return 0
  [ -e /etc/ssl/cert.pem ] && return 0
  [ "$PKG" = "none" ] && return 0
  pm_install ca-certificates >/dev/null 2>&1 || true
  return 0
}
ensure_ca

# ---------------------------------------------------------------------------
# 自检 2/4：git
# ---------------------------------------------------------------------------
ensure_git() {
  if have git; then info "git: $(git --version 2>/dev/null)"; return 0; fi
  if [ "$PKG" = "none" ]; then warn "git 缺失，且未识别到包管理器，将改用源码包方式获取代码"; return 1; fi
  printf '   git 缺失 → 自动安装\n'
  pm_update >/dev/null 2>&1 || true
  pm_install git >/dev/null 2>&1 || true
  if have git; then info "git: $(git --version 2>/dev/null)"; return 0; fi
  warn "git 自动安装失败，将改用源码包方式获取代码（不影响部署，仅升级时不便）"
  return 1
}
ensure_git || true

# ---------------------------------------------------------------------------
# 自检 3/4：docker 与 docker compose v2
# ---------------------------------------------------------------------------
install_docker_official() {
  _tmp="$(mktemp)"
  if ! fetch https://get.docker.com "$_tmp"; then rm -f "$_tmp"; return 1; fi
  if [ -n "${DOCKER_MIRROR:-}" ]; then
    info "执行 Docker 官方安装脚本（镜像源：$DOCKER_MIRROR）"
    as_root sh "$_tmp" --mirror "$DOCKER_MIRROR" || { rm -f "$_tmp"; return 1; }
  else
    info "执行 Docker 官方安装脚本 https://get.docker.com"
    as_root sh "$_tmp" || { rm -f "$_tmp"; return 1; }
  fi
  rm -f "$_tmp"
  have docker
}

install_docker_distro() {
  [ "$PKG" = "none" ] && return 1
  info "改用发行版仓库安装 docker"
  pm_update >/dev/null 2>&1 || true
  case "$PKG" in
    apt) pm_install docker.io || return 1 ;;
    dnf | yum) pm_install docker-ce || pm_install moby-engine || pm_install docker || return 1 ;;
    apk | zypper | pacman) pm_install docker || return 1 ;;
    *) return 1 ;;
  esac
  have docker
}

install_compose_binary() {
  case "$ARCH" in
    x86_64 | amd64) _carch=x86_64 ;;
    aarch64 | arm64) _carch=aarch64 ;;
    armv7l) _carch=armv7 ;;
    armv6l) _carch=armv6 ;;
    i386 | i686) _carch=i386 ;;
    *) warn "未知架构 $ARCH，跳过 compose 独立二进制安装"; return 1 ;;
  esac
  _url="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${_carch}"
  if [ -n "${COMPOSE_MIRROR:-}" ]; then
    _url="${COMPOSE_MIRROR%/}/docker-compose-linux-${_carch}"
  fi
  info "下载 docker compose v2 独立二进制：$_url"
  _tmp="$(mktemp)"
  if ! fetch "$_url" "$_tmp"; then rm -f "$_tmp"; return 1; fi
  as_root mkdir -p /usr/local/lib/docker/cli-plugins || { rm -f "$_tmp"; return 1; }
  as_root install -m 0755 "$_tmp" /usr/local/lib/docker/cli-plugins/docker-compose || { rm -f "$_tmp"; return 1; }
  rm -f "$_tmp"
  compose version >/dev/null 2>&1
}

ensure_compose() {
  compose version >/dev/null 2>&1 && return 0
  info "docker compose v2 缺失 → 尝试从发行版仓库补齐"
  case "$PKG" in
    apt) pm_install docker-compose-plugin >/dev/null 2>&1 || pm_install docker-compose-v2 >/dev/null 2>&1 || true ;;
    dnf | yum) pm_install docker-compose-plugin >/dev/null 2>&1 || true ;;
    apk) pm_install docker-cli-compose >/dev/null 2>&1 || true ;;
    zypper | pacman) pm_install docker-compose >/dev/null 2>&1 || true ;;
  esac
  compose version >/dev/null 2>&1 && return 0
  install_compose_binary
}

start_docker_daemon() {
  docker_cli info >/dev/null 2>&1 && return 0
  info "docker 守护进程未运行 → 尝试启动"
  if have systemctl; then
    as_root systemctl enable --now docker >/dev/null 2>&1 || as_root systemctl start docker >/dev/null 2>&1 || true
  elif have rc-service; then
    as_root rc-service docker start >/dev/null 2>&1 || true
    as_root rc-update add docker default >/dev/null 2>&1 || true
  elif have service; then
    as_root service docker start >/dev/null 2>&1 || true
  fi
  _i=0
  while [ "$_i" -lt 30 ]; do
    docker_cli info >/dev/null 2>&1 && return 0
    _i=$((_i + 1))
    sleep 1
  done
  return 1
}

if have docker; then
  info "docker: $(docker --version 2>/dev/null)"
else
  printf '   docker 缺失 → 自动安装\n'
  install_docker_official || install_docker_distro || true
  have docker || die "Docker 自动安装失败。请手动安装 Docker 后重新执行本脚本（参考 https://docs.docker.com/engine/install/）"
  info "docker: $(docker --version 2>/dev/null)"
fi

start_docker_daemon || die "Docker 守护进程启动失败：请手动执行 systemctl start docker 后重新运行本脚本（OpenVZ/LXC 类容器化 VPS 通常无法运行 Docker）"

if compose version >/dev/null 2>&1; then
  info "compose: $(compose version 2>/dev/null | head -n1)"
else
  printf '   docker compose v2 缺失 → 自动安装\n'
  ensure_compose || die "docker compose v2 安装失败：请手动安装 docker-compose-plugin 后重新运行本脚本"
  info "compose: $(compose version 2>/dev/null | head -n1)"
fi

# ---------------------------------------------------------------------------
# 自检 4/4：获取代码（脚本本身就在仓库里时直接使用当前目录）
# ---------------------------------------------------------------------------
step "准备代码"
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/docker-compose.yml" ] && [ -d "$SELF_DIR/.git" ]; then
  INSTALL_DIR="$SELF_DIR"
  info "脚本位于仓库目录内，直接使用：$INSTALL_DIR"
fi

fetch_repo() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    info "更新已有仓库：git pull --ff-only"
    git -C "$INSTALL_DIR" pull --ff-only || warn "git pull 失败（可能存在本地改动），继续用当前代码部署"
    return 0
  fi
  if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    warn "$INSTALL_DIR 已存在且不是 git 仓库，直接使用该目录"
    return 0
  fi
  mkdir -p "$(dirname "$INSTALL_DIR")" 2>/dev/null || true
  if have git; then
    info "克隆仓库 $REPO_URL（分支 $BRANCH）"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" && return 0
    warn "git clone 失败 → 改用源码包"
  fi
  [ -n "$TARBALL_URL" ] || return 1
  info "下载源码包：$TARBALL_URL"
  _tmp="$(mktemp)"
  _dir="$(mktemp -d)"
  if ! fetch "$TARBALL_URL" "$_tmp"; then rm -f "$_tmp"; rm -rf "$_dir"; return 1; fi
  if ! tar -xzf "$_tmp" -C "$_dir"; then rm -f "$_tmp"; rm -rf "$_dir"; return 1; fi
  _src="$(find "$_dir" -maxdepth 1 -mindepth 1 -type d | head -n1)"
  mkdir -p "$INSTALL_DIR"
  cp -a "$_src/." "$INSTALL_DIR/" || { rm -f "$_tmp"; rm -rf "$_dir"; return 1; }
  rm -f "$_tmp"
  rm -rf "$_dir"
  [ -f "$INSTALL_DIR/docker-compose.yml" ]
}
fetch_repo || die "获取代码失败：请检查机器能否访问 $REPO_URL（被墙可设 https_proxy 后重试）"
ok "代码就绪：$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 生成/读取 .env（端口随机生成后固定不变，升级保持不变）
# ---------------------------------------------------------------------------
rand_port() {
  if have shuf; then shuf -i 10000-30000 -n 1; return; fi
  if [ -r /dev/urandom ]; then
    od -An -N2 -tu2 /dev/urandom 2>/dev/null | tr -d ' \n' | awk '{print 10000 + ($1 % 20001)}'
    return
  fi
  printf '%s\n' "$((10000 + (RANDOM % 20001)))"
}
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }

if [ -f "$INSTALL_DIR/.env" ]; then
  info ".env 已存在，保留原有配置（端口 / 绑定 / 域名不变）"
else
  _p=""
  _i=0
  while [ "$_i" -lt 30 ]; do
    _p="$(rand_port)"
    port_in_use "$_p" || break
    _p=""
    _i=$((_i + 1))
  done
  [ -n "$_p" ] || _p="$(rand_port)"
  printf 'PANEL_PORT=%s\n' "$_p" > "$INSTALL_DIR/.env"
  info "已生成 $INSTALL_DIR/.env（随机端口 $_p）"
fi
PANEL_PORT="$(sed -n 's/^PANEL_PORT=//p' "$INSTALL_DIR/.env" | tail -n1)"
PANEL_BIND="$(sed -n 's/^PANEL_BIND=//p' "$INSTALL_DIR/.env" | tail -n1)"
PANEL_DOMAIN="$(sed -n 's/^PANEL_DOMAIN=//p' "$INSTALL_DIR/.env" | tail -n1)"
[ -n "${PANEL_PORT:-}" ] || PANEL_PORT=8088
# 面板绑定的网卡地址（空/0.0.0.0/127.0.0.1 时本机健康检查走回环）
HEALTH_HOST="127.0.0.1"
case "${PANEL_BIND:-}" in
  "" | 0.0.0.0 | 127.0.0.1 | "::" | "[::]") ;;
  *) HEALTH_HOST="$PANEL_BIND" ;;
esac

# ---------------------------------------------------------------------------
# 部署（自动识别是否需要带 tls profile，避免升级时把 caddy 丢了）
# ---------------------------------------------------------------------------
step "部署面板"
TLS=0
[ "${IPERF3_FLEET_TLS:-}" = "1" ] && TLS=1
if docker_cli ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'iperf3-fleet-caddy'; then
  TLS=1
fi
COMPOSE_ARGS=""
if [ "$TLS" = "1" ]; then
  COMPOSE_ARGS="--profile tls"
  # 历史误挂载会把 Caddyfile.local 建成目录，导致 caddy 静默启动失败
  if [ -d "$INSTALL_DIR/Caddyfile.local" ]; then
    warn "Caddyfile.local 是目录（历史误挂载残留），已清理"
    rm -rf "$INSTALL_DIR/Caddyfile.local"
  fi
  if [ ! -f "$INSTALL_DIR/Caddyfile.local" ] && [ -f "$INSTALL_DIR/Caddyfile.acme" ]; then
    cp -T "$INSTALL_DIR/Caddyfile.acme" "$INSTALL_DIR/Caddyfile.local" 2>/dev/null \
      || cp "$INSTALL_DIR/Caddyfile.acme" "$INSTALL_DIR/Caddyfile.local"
    info '已生成 Caddyfile.local（默认 ACME 自动签发，需 80/443 空闲；改用 CF 源证书见 README）'
  fi
  info "已启用 HTTPS 网关（caddy）"
fi

cd "$INSTALL_DIR" || die "无法进入 $INSTALL_DIR"
info "docker compose $COMPOSE_ARGS up -d --build（首次部署需要构建镜像，请耐心等待）"
# shellcheck disable=SC2086
compose $COMPOSE_ARGS up -d --build || die "docker compose 启动失败，请查看上方日志"

step "等待面板就绪"
CID="$(compose ps -q iperf3-fleet 2>/dev/null | head -n1)"
HEALTHY=0
_i=0
while [ "$_i" -lt 90 ]; do
  if [ -n "$CID" ]; then
    _st="$(docker_cli inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CID" 2>/dev/null)"
    if [ "$_st" = "healthy" ]; then HEALTHY=1; break; fi
    if [ "$_st" = "none" ] && have curl && curl -fsS -m 3 "http://$HEALTH_HOST:$PANEL_PORT/api/health" >/dev/null 2>&1; then HEALTHY=1; break; fi
    case "$_st" in exited | dead) break ;; esac
  fi
  _i=$((_i + 1))
  sleep 2
done

if [ "$HEALTHY" != "1" ]; then
  warn "面板健康检查未通过，下面是容器日志："
  # shellcheck disable=SC2086
  compose $COMPOSE_ARGS logs --tail 40 iperf3-fleet 2>/dev/null || true
  die "部署未成功。常见原因：端口 $PANEL_PORT 被占用（改 .env 后重跑）、磁盘空间不足、拉取镜像超时（可配 Docker 镜像加速）"
fi
ok "面板已就绪"

# 等部署横幅打印完（横幅里带一次性初始登录密码），再整体输出日志
_i=0
while [ "$_i" -lt 15 ]; do
  if compose logs --tail 40 iperf3-fleet 2>/dev/null | grep -q "部署成功"; then break; fi
  _i=$((_i + 1))
  sleep 2
done

step "部署完成"
# shellcheck disable=SC2086
compose $COMPOSE_ARGS logs --tail 60 iperf3-fleet 2>/dev/null || true

PANEL_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
[ -n "${PANEL_IP:-}" ] || PANEL_IP="<面板机IP>"
printf '\n'
ok "面板目录：$INSTALL_DIR"
ok "访问地址：http://$PANEL_IP:$PANEL_PORT （HTTPS 域名：${PANEL_DOMAIN:-未配置}）"
printf '   登录账号密码见上面的部署横幅（只在首次部署时生成，忘记密码：删掉 %s/data/auth.json 后 %s up -d 重新生成）\n' \
  "$INSTALL_DIR" "docker compose"
printf '   升级：cd %s && git pull && docker compose %s up -d --build\n' "$INSTALL_DIR" "$COMPOSE_ARGS"
printf '   卸载：cd %s && docker compose down && rm -rf data .env\n' "$INSTALL_DIR"
printf '   下一步：登录面板 → 添加机器 → 复制接入命令到被测机 root 执行\n'
