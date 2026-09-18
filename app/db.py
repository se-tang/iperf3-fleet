"""SQLite 数据层 v2：机器（Agent 令牌接入）、任务队列、面板登录凭据。"""
import datetime
import json
import os
import secrets
import sqlite3
import string
import threading
import time

_DATA_DIR = os.environ.get('DATA_DIR')
if not _DATA_DIR:
    _DATA_DIR = '/data' if os.name != 'nt' else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, 'panel.db')
AUTH_PATH = os.path.join(_DATA_DIR, 'auth.json')
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
    cols_m = [r['name'] for r in db.execute('PRAGMA table_info(machines)').fetchall()]
    if cols_m and 'sign_key' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN sign_key TEXT NOT NULL DEFAULT ''")
    for row in db.execute("SELECT id FROM machines WHERE sign_key=''").fetchall():
        db.execute('UPDATE machines SET sign_key=? WHERE id=?', (secrets.token_hex(32), row['id']))
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
    db.commit()


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
    """首次启动生成随机用户名和随机密码写入 auth.json；可用环境变量 PANEL_USER/PANEL_PASSWORD 覆盖。"""
    env_user = os.environ.get('PANEL_USER')
    env_pw = os.environ.get('PANEL_PASSWORD')
    if os.path.exists(AUTH_PATH) and not env_user and not env_pw:
        return
    user = env_user or _gen_username()
    pw = env_pw or _gen_password(16)
    with open(AUTH_PATH, 'w', encoding='utf-8') as f:
        json.dump({'user': user, 'password': pw}, f, ensure_ascii=False)
    try:
        os.chmod(AUTH_PATH, 0o600)
    except OSError:
        pass


def get_auth():
    try:
        with open(AUTH_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return str(d.get('user') or 'admin'), str(d.get('password') or '')
    except Exception:
        return 'admin', ''


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


def get_machines():
    rows = get_db().execute('SELECT * FROM machines ORDER BY id').fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m['online'] = _row_online(m)
        out.append(m)
    return out


def get_machine(mid):
    r = get_db().execute('SELECT * FROM machines WHERE id=?', (mid,)).fetchone()
    if not r:
        return None
    m = dict(r)
    m['online'] = _row_online(m)
    return m


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
        'INSERT INTO machines (name, role, region, bandwidth, token, sign_key) VALUES (?,?,?,?,?,?)',
        (f['name'], f['role'], f['region'], f['bandwidth'],
         secrets.token_hex(16), secrets.token_hex(32)))
    db.commit()
    return get_machine(cur.lastrowid)


def update_machine(mid, f):
    db = get_db()
    db.execute('UPDATE machines SET name=?, role=?, region=?, bandwidth=? WHERE id=?',
               (f['name'], f['role'], f['region'], f['bandwidth'], mid))
    db.commit()
    return get_machine(mid)


def regen_token(mid):
    db = get_db()
    db.execute('UPDATE machines SET token=?, sign_key=? WHERE id=?',
               (secrets.token_hex(16), secrets.token_hex(32), mid))
    db.commit()
    return get_machine(mid)


def touch_machine(mid, hostname, ip):
    db = get_db()
    db.execute('UPDATE machines SET last_seen=?, hostname=?, agent_ip=? WHERE id=?',
               (int(time.time()), (hostname or '')[:128], (ip or '')[:64], mid))
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

def create_run(target, backend_ids):
    db = get_db()
    cur = db.execute(
        'INSERT INTO runs (target_id, target_name, target_host, target_region, target_bandwidth) '
        'VALUES (?,?,?,?,?)',
        (target['id'], target['name'], target['agent_ip'] or 'IP待agent上报',
         target['region'], target['bandwidth']))
    run_id = cur.lastrowid
    for bid in backend_ids:
        m = get_machine(bid)
        db.execute(
            'INSERT INTO run_items (run_id, machine_id, machine_name, machine_host, '
            'machine_region, machine_bandwidth) VALUES (?,?,?,?,?,?)',
            (run_id, m['id'], m['name'], m['agent_ip'] or 'IP待agent上报',
             m['region'], m['bandwidth']))
    db.commit()
    return run_id


def get_run(rid):
    r = get_db().execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
    return dict(r) if r else None


def get_runs(limit=50):
    rows = get_db().execute('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


_RUN_COLS = {'status', 'report', 'log', 'error', 'finished_at'}


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

def create_job(machine_id, cmd, timeout):
    db = get_db()
    cur = db.execute(
        'INSERT INTO jobs (machine_id, cmd, timeout) VALUES (?,?,?)',
        (machine_id, cmd, int(timeout)))
    db.commit()
    return cur.lastrowid


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
