"""SQLite 数据层 v2：机器（Agent 令牌接入）、任务队列、面板登录凭据。"""
import datetime
import ipaddress
import json
import os
import secrets
import sqlite3
import string
import threading
import time

from werkzeug.security import generate_password_hash

_DATA_DIR = os.environ.get('DATA_DIR')
if not _DATA_DIR:
    _DATA_DIR = '/data' if os.name != 'nt' else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, 'panel.db')
AUTH_PATH = os.path.join(_DATA_DIR, 'auth.json')
INITIAL_PW_PATH = os.path.join(_DATA_DIR, '.initial_password')
SECRET_PATH = os.path.join(_DATA_DIR, 'secret_key')

# agent 心跳在该秒数内视为在线
ONLINE_WINDOW = 25

_local = threading.local()


def get_db():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        _local.conn = conn
    return conn


SCHEMA = '''
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'backend',
    region TEXT NOT NULL DEFAULT '',
    bandwidth TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    sign_key TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    agent_ip TEXT NOT NULL DEFAULT '',
    ip4 TEXT NOT NULL DEFAULT '',
    ip6 TEXT NOT NULL DEFAULT '',
    addr_override TEXT NOT NULL DEFAULT '',
    probe_error TEXT NOT NULL DEFAULT '',
    probe_error_at TEXT,
    last_seen INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    target_host TEXT NOT NULL DEFAULT '',
    target_region TEXT NOT NULL DEFAULT '',
    target_bandwidth TEXT NOT NULL DEFAULT '',
    ip_version INTEGER NOT NULL DEFAULT 4,
    streams INTEGER NOT NULL DEFAULT 1,
    duration INTEGER NOT NULL DEFAULT 10,
    port INTEGER NOT NULL DEFAULT 5201,
    udp INTEGER NOT NULL DEFAULT 0,
    udp_bandwidth TEXT NOT NULL DEFAULT '100M',
    ping_count INTEGER NOT NULL DEFAULT 200,
    source TEXT NOT NULL DEFAULT 'manual',
    schedule_id INTEGER,
    served_ports TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    report TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    machine_id INTEGER,
    machine_name TEXT NOT NULL DEFAULT '',
    machine_host TEXT NOT NULL DEFAULT '',
    machine_region TEXT NOT NULL DEFAULT '',
    machine_bandwidth TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT NOT NULL DEFAULT '',
    ping_raw TEXT NOT NULL DEFAULT '',
    up_raw TEXT NOT NULL DEFAULT '',
    down_raw TEXT NOT NULL DEFAULT '',
    metrics TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL,
    cmd TEXT NOT NULL,
    timeout INTEGER NOT NULL DEFAULT 120,
    status TEXT NOT NULL DEFAULT 'queued',
    output TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_tombstones (
    token TEXT PRIMARY KEY,
    cmd_b64 TEXT NOT NULL DEFAULT '',
    sig TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    target_id INTEGER NOT NULL,
    backend_ids TEXT NOT NULL DEFAULT '[]',
    ip_version INTEGER NOT NULL DEFAULT 4,
    streams INTEGER NOT NULL DEFAULT 1,
    duration INTEGER NOT NULL DEFAULT 10,
    port INTEGER NOT NULL DEFAULT 5201,
    udp INTEGER NOT NULL DEFAULT 0,
    udp_bandwidth TEXT NOT NULL DEFAULT '100M',
    ping_count INTEGER NOT NULL DEFAULT 200,
    ports TEXT NOT NULL DEFAULT '{}',
    interval_seconds INTEGER NOT NULL DEFAULT 3600,
    on_busy TEXT NOT NULL DEFAULT 'skip',
    last_run_at TEXT,
    last_run_id INTEGER,
    last_status TEXT NOT NULL DEFAULT '',
    next_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    skip_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS schedule_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    run_id INTEGER,
    planned_at TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'running',
    note TEXT NOT NULL DEFAULT ''
);
'''


def init_db():
    db = get_db()
    # 旧版 SSH 凭据表结构 → 直接重建（v2 起不再保存任何 SSH 信息）
    cols = [r['name'] for r in db.execute('PRAGMA table_info(machines)').fetchall()]
    if cols and 'token' not in cols:
        db.executescript(
            'DROP TABLE IF EXISTS run_items; DROP TABLE IF EXISTS jobs; '
            'DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS machines;')
    db.executescript(SCHEMA)
    # 旧库补列/补表
    cols_items = [r['name'] for r in db.execute('PRAGMA table_info(run_items)').fetchall()]
    if cols_items and 'phase' not in cols_items:
        db.execute("ALTER TABLE run_items ADD COLUMN phase TEXT NOT NULL DEFAULT ''")
    # 每台后端机可以用各自的 iperf3 端口（0 = 跟随本次测试的默认端口）
    if cols_items and 'port' not in cols_items:
        db.execute('ALTER TABLE run_items ADD COLUMN port INTEGER NOT NULL DEFAULT 0')
    # 目标机在 NAT 后时，其本地监听端口可以与后端机连接端口不同（0 = 跟随默认端口）
    cols_m = [r['name'] for r in db.execute('PRAGMA table_info(machines)').fetchall()]
    if cols_m and 'sign_key' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN sign_key TEXT NOT NULL DEFAULT ''")
    # v2.7：测试地址按协议族分开保存（agent_ip 仍是「最近一次心跳的来源 IP」）
    if cols_m and 'ip4' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN ip4 TEXT NOT NULL DEFAULT ''")
    if cols_m and 'ip6' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN ip6 TEXT NOT NULL DEFAULT ''")
    # 地址探测失败原因（面板侧可见，避免「机器在线却一直无 IPv6」无据可查）
    if cols_m and 'probe_error' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN probe_error TEXT NOT NULL DEFAULT ''")
    if cols_m and 'probe_error_at' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN probe_error_at TEXT")
    # 手动指定测试地址（NAT / 反代后面板观测到的地址不对时兜底；留空 = 自动）
    if cols_m and 'addr_override' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN addr_override TEXT NOT NULL DEFAULT ''")
    cols_r = [r['name'] for r in db.execute('PRAGMA table_info(runs)').fetchall()]
    if cols_r and 'ip_version' not in cols_r:
        db.execute('ALTER TABLE runs ADD COLUMN ip_version INTEGER NOT NULL DEFAULT 4')
    # v2.8：测试参数从写死（单线程 / 10 秒 / 5201 / ping 200）改为每次可自定义
    for col, ddl in (('streams', 'INTEGER NOT NULL DEFAULT 1'),
                     ('duration', 'INTEGER NOT NULL DEFAULT 10'),
                     ('port', 'INTEGER NOT NULL DEFAULT 5201'),
                     ('udp', 'INTEGER NOT NULL DEFAULT 0'),
                     ('udp_bandwidth', "TEXT NOT NULL DEFAULT '100M'"),
                     ('ping_count', 'INTEGER NOT NULL DEFAULT 200')):
        if cols_r and col not in cols_r:
            db.execute(f'ALTER TABLE runs ADD COLUMN {col} {ddl}')
    # 注：曾短暂区分过「目标机监听端口 / 后端连接端口」（target_port 列），
    # 但 NAT 商家的端口映射基本都是同号映射，最终只保留一个端口列（port），
    # 旧库残留的 runs.target_port 列不再读取。
    for row in db.execute("SELECT id FROM machines WHERE sign_key=''").fetchall():
        db.execute('UPDATE machines SET sign_key=? WHERE id=?', (secrets.token_hex(32), row['id']))
    # v2.9：测试记录来源（手动 / 定时任务），供记录列表与对比报告区分
    if cols_r and 'source' not in cols_r:
        db.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if cols_r and 'schedule_id' not in cols_r:
        db.execute('ALTER TABLE runs ADD COLUMN schedule_id INTEGER')
    # 本轮在目标机上起过的 server 端口：面板重启后据此回收「孤儿 server」，
    # 否则残留进程会占着端口，让后（尤其是定时任务的下一轮）启动失败
    if cols_r and 'served_ports' not in cols_r:
        db.execute("ALTER TABLE runs ADD COLUMN served_ports TEXT NOT NULL DEFAULT ''")
    cols_tb = [r['name'] for r in db.execute('PRAGMA table_info(agent_tombstones)').fetchall()]
    if cols_tb and 'cmd_b64' not in cols_tb:
        # 墓碑是一次性瞬态数据，结构变化直接重建
        db.execute('DROP TABLE agent_tombstones')
        db.executescript(SCHEMA)
    db.commit()
    mark_stale_runs()
    ensure_auth()


def mark_stale_runs():
    db = get_db()
    db.execute(
        "UPDATE runs SET status='failed', error='面板服务重启导致测试中断', "
        "finished_at=datetime('now','localtime') WHERE status IN ('running','pending')")
    # 重启后没有任何测试应在运行，滞留的 agent 任务一并作废
    db.execute(
        "UPDATE jobs SET status='failed', exit_code=-1, output = output || ?, "
        "finished_at=datetime('now','localtime') WHERE status IN ('queued','running')",
        ('\n[面板] 面板重启，任务作废\n',))
    # 定时任务：把「中断」的那一轮标清楚，但**不动下次执行时间**——
    # 定时任务本身是长期挂着的，面板重启不该让它停掉，恢复后继续按原计划跑。
    db.execute(
        "UPDATE schedule_runs SET status='interrupted', note='面板服务重启导致中断' "
        "WHERE status='running'")
    db.execute(
        "UPDATE schedules SET last_status='interrupted', "
        "last_error='面板服务重启导致本轮中断' WHERE last_status='running'")
    db.commit()


def runs_with_orphan_ports():
    """面板重启时被中断、但目标机上可能还留着 iperf3 -s 的测试记录。

    中断发生在「server 已起、关闭指令还没送出去」的窗口里时，那个裸 server 会一直
    监听到存活上限到点，期间既占着端口（后一轮必然启动失败）又可能被扫到盗刷流量。
    这里把这类记录捞出来，等目标机 Agent 上线后按记录里的端口精确回收。
    """
    rows = get_db().execute(
        "SELECT id, target_id, target_name, served_ports FROM runs "
        "WHERE served_ports != '' AND status='failed' "
        "AND error LIKE '%面板服务重启%' ORDER BY id DESC LIMIT 20").fetchall()
    return [dict(r) for r in rows]


def mark_orphan_ports_cleared(rid):
    update_run(rid, served_ports='')


# ---------------- 登录凭据 ----------------

_USER_ALPHABET = string.ascii_letters + string.digits
_PASS_SPECIALS = "!@#$%^&*()-_=+[]{}?"
_PASS_ALPHABET = string.ascii_letters + string.digits + _PASS_SPECIALS


def _gen_username(n=8):
    return ''.join(secrets.choice(_USER_ALPHABET) for _ in range(n))


def _gen_password(n=16):
    """16 位随机密码：大小写字母 + 数字 + 特殊字符，保证四类至少各一。"""
    for _ in range(100):
        pw = ''.join(secrets.choice(_PASS_ALPHABET) for _ in range(n))
        if (any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in _PASS_SPECIALS for c in pw)):
            return pw
    raise RuntimeError('密码生成失败')


def ensure_auth():
    """首次启动生成随机用户名和密码：auth.json 只存哈希（PANEL_USER/PANEL_PASSWORD
    可覆盖）。初始密码另写 .initial_password（0600）供部署横幅显示一次；
    忘记密码时删除 auth.json 重启即可重新生成。"""
    env_user = os.environ.get('PANEL_USER')
    env_pw = os.environ.get('PANEL_PASSWORD')
    cur = _read_auth_file()
    if cur and cur.get('password_hash') and not env_user and not env_pw:
        return
    user = env_user or (cur or {}).get('user') or _gen_username()
    if env_pw:
        pw = env_pw
    elif cur and cur.get('password'):
        pw = str(cur['password'])      # 旧版明文凭据 → 迁移为哈希
    else:
        pw = _gen_password(16)
    with open(AUTH_PATH, 'w', encoding='utf-8') as f:
        json.dump({'user': user, 'password_hash': generate_password_hash(pw)},
                  f, ensure_ascii=False)
    try:
        os.chmod(AUTH_PATH, 0o600)
    except OSError:
        pass
    if not env_pw:
        with open(INITIAL_PW_PATH, 'w', encoding='utf-8') as f:
            f.write(pw)
        try:
            os.chmod(INITIAL_PW_PATH, 0o600)
        except OSError:
            pass


def _read_auth_file():
    try:
        with open(AUTH_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def get_auth():
    d = _read_auth_file() or {}
    return str(d.get('user') or 'admin'), str(d.get('password_hash') or '')


def get_secret_key():
    if not os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, 'w') as f:
            f.write(secrets.token_hex(32))
    with open(SECRET_PATH) as f:
        return f.read().strip()


# ---------------- machines ----------------

def machine_online(m):
    if not m:
        return False
    return bool(m.get('last_seen')) and (time.time() - m['last_seen']) <= ONLINE_WINDOW


def _row_online(m):
    return machine_online(m)


def _fill_sign_key(m):
    """兼容旧库自愈：机器缺签名密钥时即时补生成（正常情况下 init_db 迁移已补齐）。"""
    if m and not m.get('sign_key'):
        try:
            sk = secrets.token_hex(32)
            get_db().execute('UPDATE machines SET sign_key=? WHERE id=?', (sk, m['id']))
            get_db().commit()
            m['sign_key'] = sk
        except Exception:
            pass
    return m


def get_machines():
    rows = get_db().execute('SELECT * FROM machines ORDER BY id').fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m['online'] = machine_online(m)
        _fill_sign_key(m)
        out.append(m)
    return out


def get_machine(mid):
    r = get_db().execute('SELECT * FROM machines WHERE id=?', (mid,)).fetchone()
    if not r:
        return None
    m = dict(r)
    m['online'] = machine_online(m)
    return _fill_sign_key(m)


def get_machine_by_token(token):
    if not token:
        return None
    r = get_db().execute('SELECT * FROM machines WHERE token=?', (token,)).fetchone()
    if not r:
        return None
    m = dict(r)
    m['online'] = _row_online(m)
    return m


def create_machine(f):
    db = get_db()
    cur = db.execute(
        'INSERT INTO machines (name, role, region, bandwidth, addr_override, token, sign_key) '
        'VALUES (?,?,?,?,?,?,?)',
        (f['name'], f['role'], f['region'], f['bandwidth'],
         (f.get('addr_override') or '').strip(),
         secrets.token_hex(16), secrets.token_hex(32)))
    db.commit()
    return get_machine(cur.lastrowid)


def update_machine(mid, f):
    db = get_db()
    db.execute('UPDATE machines SET name=?, role=?, region=?, bandwidth=?, addr_override=? '
               'WHERE id=?',
               (f['name'], f['role'], f['region'], f['bandwidth'],
                (f.get('addr_override') or '').strip(), mid))
    db.commit()
    return get_machine(mid)


def regen_token(mid):
    db = get_db()
    db.execute('UPDATE machines SET token=?, sign_key=? WHERE id=?',
               (secrets.token_hex(16), secrets.token_hex(32), mid))
    db.commit()
    return get_machine(mid)


def ip_family(ip):
    """IP 协议族：4 / 6；非法地址返回 None。"""
    try:
        return ipaddress.ip_address(str(ip)).version
    except ValueError:
        return None


def machine_test_ip(m, ip_version=4):
    """取该机器用于测试的地址（默认 IPv4）。

    手动指定的测试地址优先——NAT / 反代后面板观测到的地址可能不对（例如前置 CDN
    把入口 IP 当成了机器 IP），这时在机器资料里填死真实地址即可。
    """
    want = 6 if int(ip_version or 4) == 6 else 4
    override = (m.get('addr_override') or '').strip()
    if override and ip_family(override) == want:
        return override
    return (m.get('ip6') if want == 6 else m.get('ip4')) or ''


def touch_machine(mid, hostname, ip):
    """记录心跳：agent_ip 存最近一次来源 IP；同时按协议族写入 ip4/ip6。

    双栈面板下心跳来源可能是 v4 也可能是 v6，按族分别保存后就不会互相覆盖，
    测试地址不会在两族之间跳动。
    """
    db = get_db()
    ip = (ip or '')[:64]
    sets = ['last_seen=?', 'hostname=?', 'agent_ip=?']
    args = [int(time.time()), (hostname or '')[:128], ip]
    fam = ip_family(ip)
    if fam == 4:
        sets.append('ip4=?')
        args.append(ip)
    elif fam == 6:
        sets.append('ip6=?')
        args.append(ip)
    args.append(mid)
    db.execute('UPDATE machines SET ' + ', '.join(sets) + ' WHERE id=?', args)
    db.commit()
    return get_machine(mid)


def set_machine_ips(mid, values, force=False):
    """写回面板探测到的地址。默认只补空缺（心跳观测到的地址更可信，不覆盖），
    force=True 用于用户手动「探测地址」时的强制刷新。"""
    db = get_db()
    row = db.execute('SELECT ip4, ip6 FROM machines WHERE id=?', (mid,)).fetchone()
    if not row:
        return None
    sets, args = [], []
    for col in ('ip4', 'ip6'):
        v = str(values.get(col) or '')[:64]
        if not v:
            continue
        if not force and row[col]:
            continue
        sets.append(f'{col}=?')
        args.append(v)
    if sets:
        args.append(mid)
        db.execute('UPDATE machines SET ' + ', '.join(sets) + ' WHERE id=?', args)
        db.commit()
    return get_machine(mid)


def set_machine_probe(mid, error=''):
    """记录/清除最近一次地址探测的失败原因（成功时传空串清除）。

    面板据此在机器列表上标出「为什么没有 IPv6」，不必再去翻容器日志或机器上的
    agent.log（例如 Agent 因任务编号回退而拒收探测任务时，这里会写明原因）。
    """
    db = get_db()
    if error:
        db.execute("UPDATE machines SET probe_error=?, "
                   "probe_error_at=datetime('now','localtime') WHERE id=?",
                   (str(error)[:300], mid))
    else:
        db.execute("UPDATE machines SET probe_error='', probe_error_at=NULL WHERE id=?", (mid,))
    db.commit()
    return get_machine(mid)


def delete_machine(mid, cmd_b64='', sig=''):
    """删除机器；曾上线过的留下令牌墓碑（内含已签名的卸载任务），
    Agent 下次心跳时自动执行卸载脚本。"""
    db = get_db()
    m = get_machine(mid)
    if not m:
        return
    if m['last_seen']:
        db.execute('INSERT OR REPLACE INTO agent_tombstones (token, cmd_b64, sig) VALUES (?,?,?)',
                   (m['token'], cmd_b64, sig))
    db.execute('DELETE FROM machines WHERE id=?', (mid,))
    db.execute('DELETE FROM jobs WHERE machine_id=?', (mid,))
    db.commit()


def pop_tombstone(token):
    """取出一次性墓碑：命中则删除并返回 (cmd_b64, sig)，面板据此下发已签名的卸载脚本。"""
    if not token:
        return None
    db = get_db()
    r = db.execute('SELECT cmd_b64, sig FROM agent_tombstones WHERE token=?', (token,)).fetchone()
    if not r:
        return None
    db.execute('DELETE FROM agent_tombstones WHERE token=?', (token,))
    db.commit()
    return dict(r)


# ---------------- runs ----------------

def _valid_port(v, default=5201):
    try:
        p = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return p if 1 <= p <= 65535 else default


def _port_or_follow(v):
    """0/空/非法 = 跟随本次测试的默认端口（真实校验在 runner.normalize_params）。"""
    try:
        p = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return p if 1 <= p <= 65535 else 0


def create_run(target, backend_ids, ip_version=4, target_host='', source='manual',
               schedule_id=None, **params):
    """新建一次测试记录；params 为本次测试参数（线程/时长/端口/协议/ping 次数）。

    params['port'] 是**目标机端口**：目标机 iperf3 -s 监听它，后端机默认也连它
    （NAT 商家的端口映射一般是同号映射，改这一个值即可）。
    params['ports'] 可选，{机器ID: 端口}：个别后端机单独走不同连接端口的场景。
    source / schedule_id 标记这次测试的来源（手动 or 某个定时任务）。
    """
    db = get_db()
    ip_version = 6 if int(ip_version or 4) == 6 else 4
    default_port = _valid_port(params.get('port') or 5201)
    ports = params.get('ports') or {}
    cur = db.execute(
        'INSERT INTO runs (target_id, target_name, target_host, target_region, target_bandwidth, '
        'ip_version, streams, duration, port, udp, udp_bandwidth, ping_count, source, schedule_id) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (target['id'], target['name'],
         target_host or (target['agent_ip'] or 'IP待agent上报'),
         target['region'], target['bandwidth'], ip_version,
         int(params.get('streams') or 1), int(params.get('duration') or 10),
         default_port, 1 if params.get('udp') else 0,
         str(params.get('udp_bandwidth') or '100M'), int(params.get('ping_count') or 200),
         str(source or 'manual')[:32], schedule_id))
    run_id = cur.lastrowid
    for bid in backend_ids:
        m = get_machine(bid)
        db.execute(
            'INSERT INTO run_items (run_id, machine_id, machine_name, machine_host, '
            'machine_region, machine_bandwidth, port) VALUES (?,?,?,?,?,?,?)',
            (run_id, m['id'], m['name'], m['agent_ip'] or 'IP待agent上报',
             m['region'], m['bandwidth'],
             _port_or_follow(ports.get(bid, ports.get(str(bid))))))
    db.commit()
    return run_id


def get_run(rid):
    r = get_db().execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
    return dict(r) if r else None


def get_runs(limit=50):
    rows = get_db().execute('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


_RUN_COLS = {'status', 'report', 'log', 'error', 'finished_at', 'served_ports'}


def update_run(rid, **fields):
    cols = [c for c in fields if c in _RUN_COLS]
    if not cols:
        return
    sql = 'UPDATE runs SET ' + ', '.join(f'{c}=?' for c in cols) + ' WHERE id=?'
    get_db().execute(sql, [fields[c] for c in cols] + [rid])
    get_db().commit()


def finish_run(rid, status, report, log_text, error=''):
    update_run(rid, status=status, report=report, log=log_text, error=error,
               finished_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    prune_history()


def prune_history(keep_runs=100):
    """测试记录只保留最近 100 条；已完结任务与过期令牌墓碑一并清理。"""
    db = get_db()
    row = db.execute('SELECT id FROM runs ORDER BY id DESC LIMIT 1 OFFSET ?',
                     (keep_runs - 1,)).fetchone()
    if row:
        cutoff = row['id']
        db.execute('DELETE FROM run_items WHERE run_id <= ?', (cutoff,))
        db.execute('DELETE FROM runs WHERE id <= ?', (cutoff,))
    db.execute("DELETE FROM jobs WHERE status IN ('finished','failed') "
               "AND created_at < datetime('now','-7 days')")
    db.execute("DELETE FROM agent_tombstones WHERE created_at < datetime('now','-7 days')")
    db.commit()


def run_counts(rid):
    row = get_db().execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        'FROM run_items WHERE run_id=?', (rid,)).fetchone()
    return row['total'] or 0, row['done'] or 0, row['failed'] or 0


def delete_run(rid):
    db = get_db()
    db.execute('DELETE FROM run_items WHERE run_id=?', (rid,))
    db.execute('DELETE FROM runs WHERE id=?', (rid,))
    db.commit()


# ---------------- run items ----------------

def get_run_items(rid):
    rows = get_db().execute('SELECT * FROM run_items WHERE run_id=? ORDER BY id', (rid,)).fetchall()
    return [dict(r) for r in rows]


def get_run_item(iid):
    r = get_db().execute('SELECT * FROM run_items WHERE id=?', (iid,)).fetchone()
    return dict(r) if r else None


_ITEM_COLS = {'status', 'phase', 'ping_raw', 'up_raw', 'down_raw', 'metrics', 'error'}


def update_item(iid, **fields):
    cols = [c for c in fields if c in _ITEM_COLS]
    if not cols:
        return
    sql = 'UPDATE run_items SET ' + ', '.join(f'{c}=?' for c in cols) + ' WHERE id=?'
    get_db().execute(sql, [fields[c] for c in cols] + [iid])
    get_db().commit()


def fail_open_items(rid, error):
    get_db().execute(
        "UPDATE run_items SET status='failed', error=? WHERE run_id=? AND status IN ('pending','running')",
        (error, rid))
    get_db().commit()


# ---------------- agent 任务队列 ----------------

_job_id_lock = threading.Lock()


def _alloc_job_id(db):
    """分配全局严格递增的任务编号（毫秒时间戳打底，永不复用）。

    Agent 端用 last_job_id 做防重放：比历史水位小的任务编号一律拒收。而
    AUTOINCREMENT 在面板数据库被重置后（重新部署 / 删 data 目录）会从 1 重新
    开始，此时每台 Agent 都会把新任务当成重放**静默拒绝**——机器显示在线，
    但地址探测不出来、任务全部超时，且只有机器本地日志能看到原因。
    用时间戳打底后，重置过的面板发号依然大于历史编号，旧 Agent 不重装即可自愈；
    同一库内又始终取「已有最大值 + 1」，因此仍然严格递增。
    """
    row = db.execute('SELECT MAX(id) AS m FROM jobs').fetchone()
    cur_max = int((row['m'] if row else 0) or 0)
    return max(int(time.time() * 1000), cur_max + 1)


def create_job(machine_id, cmd, timeout):
    db = get_db()
    with _job_id_lock:            # 编号分配 + 插入必须原子，否则并发会撞主键
        for _ in range(20):
            jid = _alloc_job_id(db)
            try:
                db.execute('INSERT INTO jobs (id, machine_id, cmd, timeout) VALUES (?,?,?,?)',
                           (jid, machine_id, cmd, int(timeout)))
                db.commit()
                return jid
            except sqlite3.IntegrityError:
                db.rollback()
                time.sleep(0.005)
    raise RuntimeError('任务编号分配失败，请重试')


def get_job(jid):
    r = get_db().execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    return dict(r) if r else None


def next_queued_job(machine_id):
    """领取最早排队的任务：置为 running 并记录领取时间。"""
    db = get_db()
    r = db.execute(
        "SELECT * FROM jobs WHERE machine_id=? AND status='queued' ORDER BY id LIMIT 1",
        (machine_id,)).fetchone()
    if not r:
        return None
    db.execute("UPDATE jobs SET status='running', started_at=datetime('now','localtime') WHERE id=?",
               (r['id'],))
    db.commit()
    return get_job(r['id'])


_JOB_OUTPUT_MAX_CHARS = 2_000_000


def append_job_output(jid, text):
    # 输出只保留末尾 2MB，防止异常任务把数据库撑爆（实时日志此前已流式转发）
    get_db().execute(
        'UPDATE jobs SET output = substr(output || ?, -?, ?) WHERE id=?',
        (text, _JOB_OUTPUT_MAX_CHARS, _JOB_OUTPUT_MAX_CHARS, jid))
    get_db().commit()


def finish_job(jid, exit_code):
    db = get_db()
    db.execute(
        "UPDATE jobs SET status=?, exit_code=?, finished_at=datetime('now','localtime') WHERE id=?",
        ('finished' if exit_code == 0 else 'failed', exit_code, jid))
    db.commit()


def fail_job(jid, reason):
    db = get_db()
    db.execute(
        "UPDATE jobs SET status='failed', exit_code=-1, output = output || ?, "
        "finished_at=datetime('now','localtime') WHERE id=?",
        (f'\n[面板] {reason}\n', jid))
    db.commit()


def cancel_queued_jobs(machine_ids):
    """把某些机器仍排队的旧任务作废（新任务开始前清理/停止时）。"""
    if not machine_ids:
        return
    marks = ','.join('?' * len(machine_ids))
    get_db().execute(
        f"UPDATE jobs SET status='failed', exit_code=-1, output = output || ? "
        f'WHERE status=\'queued\' AND machine_id IN ({marks})',
        ('[面板] 任务已取消\n', *machine_ids))
    get_db().commit()


# ---------------- 定时任务（长期重复测试，用于前后端长期对比） ----------------

# 定时任务的字段白名单：改这里就等于改写入面，避免任意列被前端操纵
_SCHED_COLS = {'name', 'enabled', 'target_id', 'backend_ids', 'ip_version', 'streams',
               'duration', 'port', 'udp', 'udp_bandwidth', 'ping_count', 'ports',
               'interval_seconds', 'on_busy', 'last_run_at', 'last_run_id', 'last_status',
               'next_run_at', 'run_count', 'fail_count', 'skip_count', 'last_error'}


def _sql_now(now=None):
    return now or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def get_schedules():
    rows = get_db().execute('SELECT * FROM schedules ORDER BY id').fetchall()
    return [_sched_row(r) for r in rows]


def get_schedule(sid):
    r = get_db().execute('SELECT * FROM schedules WHERE id=?', (sid,)).fetchone()
    return _sched_row(r) if r else None


def _sched_row(r):
    s = dict(r)
    s['backend_ids'] = _load_json(s.get('backend_ids'), [])
    s['ports'] = _load_json(s.get('ports'), {})
    s['enabled'] = bool(s.get('enabled'))
    return s


def _load_json(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        v = default
    return v if isinstance(v, type(default)) else default


def create_schedule(fields):
    db = get_db()
    cols = [c for c in fields if c in _SCHED_COLS]
    marks = ','.join('?' * len(cols))
    cur = db.execute(f'INSERT INTO schedules ({",".join(cols)}) VALUES ({marks})',
                     [_sched_value(c, fields[c]) for c in cols])
    db.commit()
    return get_schedule(cur.lastrowid)


def update_schedule(sid, fields):
    db = get_db()
    cols = [c for c in fields if c in _SCHED_COLS]
    if not cols:
        return get_schedule(sid)
    sql = 'UPDATE schedules SET ' + ', '.join(f'{c}=?' for c in cols) + ' WHERE id=?'
    db.execute(sql, [_sched_value(c, fields[c]) for c in cols] + [sid])
    db.commit()
    return get_schedule(sid)


def _sched_value(col, val):
    if col in ('enabled', 'ip_version', 'streams', 'duration', 'port', 'udp',
               'ping_count', 'interval_seconds', 'run_count', 'fail_count', 'skip_count'):
        return int(val or 0)
    if col in ('backend_ids', 'ports'):
        return json.dumps(val if val is not None else ([] if col == 'backend_ids' else {}),
                          ensure_ascii=False)
    return val


def delete_schedule(sid):
    db = get_db()
    db.execute('DELETE FROM schedule_runs WHERE schedule_id=?', (sid,))
    db.execute('DELETE FROM schedules WHERE id=?', (sid,))
    db.commit()


def set_schedule_enabled(sid, enabled):
    return update_schedule(sid, {'enabled': 1 if enabled else 0})


def due_schedules(now=None):
    """到点且启用中的定时任务（长时间没跑过、面板重启恢复后也会被选中）。"""
    rows = get_db().execute(
        "SELECT * FROM schedules WHERE enabled=1 AND "
        "COALESCE(next_run_at, datetime('now','localtime')) <= ? "
        'ORDER BY COALESCE(next_run_at, created_at) ASC', (_sql_now(now),)).fetchall()
    return [_sched_row(r) for r in rows]


def schedule_next_run(sid, next_at, note=None, failed=False, skipped=False):
    """推进下一次执行时间，并把本轮结果计数写回。"""
    fields = {'next_run_at': next_at}
    if skipped:
        fields['skip_count'] = (get_schedule(sid) or {}).get('skip_count', 0) + 1
    elif failed:
        fields['fail_count'] = (get_schedule(sid) or {}).get('fail_count', 0) + 1
    if note is not None:
        fields['last_error'] = note
    update_schedule(sid, fields)
    return get_schedule(sid)


def record_schedule_run(sid, run_id, planned_at, started_at=None, status='running', note=''):
    """写一条定时任务执行记录（run_id 为空表示这一轮没有真正跑起来）。"""
    db = get_db()
    cols = ['schedule_id', 'run_id', 'planned_at', 'status', 'note']
    vals = [sid, run_id, planned_at, status, note]
    if started_at:
        cols.insert(3, 'started_at')
        vals.insert(3, started_at)
    marks = ','.join('?' * len(cols))
    cur = db.execute(f'INSERT INTO schedule_runs ({",".join(cols)}) VALUES ({marks})', vals)
    db.commit()
    return cur.lastrowid


def finish_schedule_run(sid, run_id, status, note=''):
    """测试跑完：回填定时任务与这一轮的状态。"""
    db = get_db()
    row = db.execute(
        'SELECT id FROM schedule_runs WHERE schedule_id=? AND run_id=? ORDER BY id DESC LIMIT 1',
        (sid, run_id)).fetchone()
    if row:
        db.execute('UPDATE schedule_runs SET status=?, note=? WHERE id=?', (status, note, row['id']))
    db.commit()
    s = get_schedule(sid) or {}
    fields = {
        'last_status': status,
        'last_run_at': _sql_now(),
        'last_run_id': run_id,
        'run_count': int(s.get('run_count') or 0) + 1,
        'last_error': '' if status == 'finished' else note,
    }
    if status != 'finished':
        fields['fail_count'] = int(s.get('fail_count') or 0) + 1
    update_schedule(sid, fields)
    return get_schedule(sid)


def get_schedule_runs(sid, limit=200):
    rows = get_db().execute(
        'SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY id DESC LIMIT ?',
        (sid, limit)).fetchall()
    return [dict(r) for r in rows]


def schedule_run_window(sid, limit=100):
    """取该定时任务最近 limit 轮**真正跑起来**的测试记录（旧→新），供对比报告使用。"""
    db = get_db()
    rows = db.execute(
        'SELECT r.id FROM schedule_runs sr JOIN runs r ON r.id = sr.run_id '
        'WHERE sr.schedule_id=? AND sr.run_id IS NOT NULL '
        'ORDER BY sr.id DESC LIMIT ?', (sid, limit)).fetchall()
    ids = [r['id'] for r in rows][::-1]
    out = []
    for rid in ids:
        run = get_run(rid)
        if not run:
            continue
        items = get_run_items(rid)
        for it in items:
            try:
                it['metrics_obj'] = json.loads(it['metrics']) if it['metrics'] else None
            except (TypeError, ValueError):
                it['metrics_obj'] = None
        out.append({'run': run, 'items': items})
    return out
