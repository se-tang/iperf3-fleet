"""SQLite 数据层：机器、测试任务、测试结果。"""
import datetime
import os
import sqlite3
import threading

_DATA_DIR = os.environ.get('DATA_DIR')
if not _DATA_DIR:
    _DATA_DIR = '/data' if os.name != 'nt' else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, 'panel.db')

_local = threading.local()


def get_db():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        _local.conn = conn
    return conn


def init_db():
    get_db().executescript('''
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    ssh_port INTEGER NOT NULL DEFAULT 22,
    ssh_user TEXT NOT NULL DEFAULT 'root',
    auth_type TEXT NOT NULL DEFAULT 'password',
    password TEXT NOT NULL DEFAULT '',
    private_key TEXT NOT NULL DEFAULT '',
    key_passphrase TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'backend',
    bandwidth TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
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
    ping_raw TEXT NOT NULL DEFAULT '',
    up_raw TEXT NOT NULL DEFAULT '',
    down_raw TEXT NOT NULL DEFAULT '',
    metrics TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
''')
    get_db().commit()
    mark_stale_runs()


def mark_stale_runs():
    """服务重启后，把中断的任务标记为失败。"""
    db = get_db()
    db.execute(
        "UPDATE runs SET status='failed', error='面板服务重启导致测试中断', "
        "finished_at=datetime('now','localtime') WHERE status IN ('running','pending')")
    db.commit()


# ---------------- machines ----------------

def get_machines():
    rows = get_db().execute('SELECT * FROM machines ORDER BY id').fetchall()
    return [dict(r) for r in rows]


def get_machine(mid):
    r = get_db().execute('SELECT * FROM machines WHERE id=?', (mid,)).fetchone()
    return dict(r) if r else None


def create_machine(f):
    db = get_db()
    cur = db.execute(
        'INSERT INTO machines (name, host, ssh_port, ssh_user, auth_type, password, '
        'private_key, key_passphrase, role, bandwidth, region) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (f['name'], f['host'], f['ssh_port'], f['ssh_user'], f['auth_type'],
         f['password'], f['private_key'], f['key_passphrase'],
         f['role'], f['bandwidth'], f['region']))
    db.commit()
    return get_machine(cur.lastrowid)


def update_machine(mid, f):
    db = get_db()
    db.execute(
        'UPDATE machines SET name=?, host=?, ssh_port=?, ssh_user=?, auth_type=?, '
        'password=?, private_key=?, key_passphrase=?, role=?, bandwidth=?, region=? WHERE id=?',
        (f['name'], f['host'], f['ssh_port'], f['ssh_user'], f['auth_type'],
         f['password'], f['private_key'], f['key_passphrase'],
         f['role'], f['bandwidth'], f['region'], mid))
    db.commit()
    return get_machine(mid)


def delete_machine(mid):
    db = get_db()
    db.execute('DELETE FROM machines WHERE id=?', (mid,))
    db.commit()


# ---------------- runs ----------------

def create_run(target, backend_ids):
    db = get_db()
    cur = db.execute(
        'INSERT INTO runs (target_id, target_name, target_host, target_region, target_bandwidth) '
        'VALUES (?,?,?,?,?)',
        (target['id'], target['name'], target['host'], target['region'], target['bandwidth']))
    run_id = cur.lastrowid
    for bid in backend_ids:
        m = get_machine(bid)
        db.execute(
            'INSERT INTO run_items (run_id, machine_id, machine_name, machine_host, '
            'machine_region, machine_bandwidth) VALUES (?,?,?,?,?,?)',
            (run_id, m['id'], m['name'], m['host'], m['region'], m['bandwidth']))
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


_ITEM_COLS = {'status', 'ping_raw', 'up_raw', 'down_raw', 'metrics', 'error'}


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
