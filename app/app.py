"""iperf3-fleet 面板 Web 服务：登录验证、机器管理（Agent 接入）、测试任务、Agent API。"""
import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, quality, runner

APP_VERSION = '2.5.0'

app = Flask(__name__)
app.json.ensure_ascii = False
app.secret_key = db.get_secret_key()
app.permanent_session_lifetime = datetime.timedelta(days=30)

# 安全配置：请求体上限 + 会话 Cookie 属性
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('PANEL_COOKIE_SECURE') == '1':
    # 套了 HTTPS 反代时设置，Cookie 只经加密连接传输
    app.config['SESSION_COOKIE_SECURE'] = True

AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent')

# 无需登录即可访问：登录页/登录接口/健康检查/Agent 脚本与 Agent API（用令牌认证）
_PUBLIC_PREFIXES = ('/agent/', '/api/agent/')
_PUBLIC_PATHS = {'/login', '/api/login', '/api/health', '/favicon.ico'}

# 登录防爆破：同一 IP 60 秒内失败 5 次 → 锁定 300 秒
_FAIL_WINDOW = 60.0
_FAIL_MAX = 5
_LOCK_SECONDS = 300
_login_fails = {}  # ip -> {'fails': [时间戳], 'locked_until': 时间戳}
_lf_lock = threading.Lock()


def _client_ip():
    """真实客户端 IP：直连取 remote_addr；经反代（remote 为内网 IP 且带
    X-Forwarded-For）时取 XFF **最后一项**——Caddy 等代理把真实客户端 IP
    追加到末尾，取首项会被请求自带的伪造头欺骗（限速绕过/agent_ip 伪造）。"""
    ra = request.remote_addr or ''
    ip = ra
    try:
        behind_proxy = ipaddress.ip_address(ra).is_private
    except ValueError:
        behind_proxy = False
    xff = request.headers.get('X-Forwarded-For', '')
    if behind_proxy and xff:
        last = xff.split(',')[-1].strip()
        try:
            ipaddress.ip_address(last)
            ip = last
        except ValueError:
            pass
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        ip = ra
    return ip[:64]


def _login_locked_seconds(ip):
    with _lf_lock:
        rec = _login_fails.get(ip)
        if not rec:
            return 0
        remain = rec.get('locked_until', 0) - time.time()
        return max(0, int(remain))


def _record_login_fail(ip):
    now = time.time()
    with _lf_lock:
        rec = _login_fails.setdefault(ip, {'fails': [], 'locked_until': 0})
        rec['fails'] = [t for t in rec['fails'] if now - t < _FAIL_WINDOW]
        rec['fails'].append(now)
        if len(rec['fails']) >= _FAIL_MAX:
            rec['locked_until'] = now + _LOCK_SECONDS
            rec['fails'] = []


@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    # 页面与 API 一律禁止浏览器缓存：升级后旧页面/旧数据不会再被使用
    if resp.mimetype == 'text/html' or request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.before_request
def csrf_guard():
    """CSRF 防线：浏览器发起的跨站写请求（Origin 与站点不符）直接拒绝。
    Agent 接口用令牌认证，不经过 Cookie，不在限制范围内。"""
    if request.method not in ('POST', 'PUT', 'DELETE'):
        return None
    p = request.path
    if not p.startswith('/api/') or p.startswith('/api/agent/'):
        return None
    origin = request.headers.get('Origin')
    if origin and urlsplit(origin).netloc != request.host:
        return jsonify({'error': '已拒绝跨站请求'}), 403
    return None


def _job_sig(sign_key, job_id, cmd_b64):
    """任务签名：Agent 用接入时一次性下发的签名密钥验签后才执行，
    防止中间人篡改心跳响应注入命令（签名密钥从不在网络中传输）。"""
    if not sign_key:
        return ''
    return hmac.new(sign_key.encode('utf-8'),
                    f'{job_id}:{cmd_b64}'.encode('utf-8'),
                    hashlib.sha256).hexdigest()


@app.before_request
def auth_gate():
    p = request.path
    if p in _PUBLIC_PATHS or p.startswith(_PUBLIC_PREFIXES):
        return None
    if p == '/':
        if not session.get('user'):
            return redirect('/login')
        return None
    if p.startswith('/api/'):
        if not session.get('user'):
            return jsonify({'error': '未登录或登录已过期'}), 401
    return None


@app.route('/')
def index():
    return render_template('index.html', version=APP_VERSION)


@app.route('/login')
def login_page():
    if session.get('user'):
        return redirect('/')
    return render_template('login.html', version=APP_VERSION)


@app.post('/api/login')
def api_login():
    ip = _client_ip()
    locked = _login_locked_seconds(ip)
    if locked:
        return jsonify({'error': f'失败次数过多，已锁定，请 {locked} 秒后再试'}), 429
    data = request.get_json(force=True, silent=True) or {}
    user, pw_hash = db.get_auth()
    ok = (secrets.compare_digest(str(data.get('user') or ''), user)
          and bool(pw_hash)
          and check_password_hash(pw_hash, str(data.get('password') or '')))
    if not ok:
        _record_login_fail(ip)
        return jsonify({'error': '用户名或密码错误'}), 401
    session.clear()
    session['user'] = user
    session.permanent = True
    return jsonify({'ok': True})


@app.post('/api/logout')
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@app.get('/api/me')
def api_me():
    return jsonify({'user': session.get('user') or ''})


@app.get('/api/health')
def api_health():
    return jsonify({'ok': True})


# ---------------- 机器管理 ----------------

@app.get('/api/machines')
def api_machines():
    return jsonify(db.get_machines())


@app.get('/api/machines/<int:mid>')
def api_machine_get(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    return jsonify(m)


def _machine_fields(data):
    def clean(k, maxlen, default=''):
        v = data.get(k)
        s = default if v is None else str(v)
        # 过滤竖线/换行，避免破坏 Markdown 报告表格；并限制长度
        s = re.sub(r'[|\r\n\t]+', ' ', s).strip()
        return s[:maxlen]

    name = clean('name', 64)
    if not name:
        raise ValueError('名称不能为空')
    role = clean('role', 16, 'backend')
    if role not in ('backend', 'target'):
        raise ValueError('角色无效')
    return {'name': name, 'role': role,
            'region': clean('region', 32), 'bandwidth': clean('bandwidth', 32)}


@app.post('/api/machines')
def api_machine_create():
    data = request.get_json(force=True, silent=True) or {}
    fields = _machine_fields(data)
    return jsonify(db.create_machine(fields))


@app.put('/api/machines/<int:mid>')
def api_machine_update(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    data = request.get_json(force=True, silent=True) or {}
    fields = _machine_fields(data)
    return jsonify(db.update_machine(mid, fields))


@app.post('/api/machines/<int:mid>/regen-token')
def api_machine_regen(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    return jsonify(db.regen_token(mid))


@app.delete('/api/machines/<int:mid>')
def api_machine_delete(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    cmd_b64 = sig = ''
    if m['last_seen'] and m['sign_key']:
        cmd_b64 = base64.b64encode(_uninstall_cmd().encode('utf-8')).decode('ascii')
        sig = _job_sig(m['sign_key'], 'tombstone-uninstall', cmd_b64)
    db.delete_machine(mid, cmd_b64, sig)
    return jsonify({'ok': True})


# ---------------- Agent 脚本下发（公开，令牌在命令参数里） ----------------

def _serve_agent_file(name):
    path = os.path.join(AGENT_DIR, name)
    with open(path, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/x-shellscript; charset=utf-8')


@app.get('/agent/install.sh')
def agent_install():
    return _serve_agent_file('install.sh')


@app.get('/agent/uninstall.sh')
def agent_uninstall():
    return _serve_agent_file('uninstall.sh')


# ---------------- Agent API（令牌认证） ----------------

def _agent_machine():
    return db.get_machine_by_token(request.headers.get('X-Agent-Token', ''))


def _uninstall_cmd():
    with open(os.path.join(AGENT_DIR, 'uninstall.sh'), encoding='utf-8') as f:
        return f.read()


@app.post('/api/agent/heartbeat')
def agent_heartbeat():
    m = _agent_machine()
    if not m:
        # 已删除机器的残留 Agent：返回一次性墓碑任务 = 已签名的卸载脚本，Agent 验签后执行
        tomb = db.pop_tombstone(request.headers.get('X-Agent-Token', ''))
        if tomb:
            body = ('status=ok\njob_id=tombstone-uninstall\ntimeout=60\n'
                    f"cmd_b64={tomb['cmd_b64']}\nsig={tomb['sig']}\n")
            return Response(body, mimetype='text/plain; charset=utf-8')
        return jsonify({'error': 'invalid token'}), 403
    m = db.touch_machine(m['id'], request.headers.get('X-Agent-Host', ''),
                         _client_ip())
    job = db.next_queued_job(m['id'])
    lines = ['status=ok']
    if job:
        cmd_b64 = base64.b64encode(job['cmd'].encode('utf-8')).decode('ascii')
        lines += [f"job_id={job['id']}", f"timeout={job['timeout']}", f'cmd_b64={cmd_b64}',
                  f"sig={_job_sig(m['sign_key'], job['id'], cmd_b64)}"]
    return Response('\n'.join(lines) + '\n', mimetype='text/plain; charset=utf-8')


@app.post('/api/agent/output')
def agent_output():
    m = _agent_machine()
    if not m:
        return jsonify({'error': 'invalid token'}), 403
    jid = request.form.get('job_id', type=int)
    job = db.get_job(jid) if jid else None
    if not job or job['machine_id'] != m['id']:
        return jsonify({'error': 'job not found'}), 404
    text = request.form.get('text', '')
    if text:
        db.append_job_output(jid, text)
    if request.form.get('done') == '1':
        exit_code = request.form.get('exit_code', type=int)
        db.finish_job(jid, exit_code if exit_code is not None else -1)
    return Response('status=ok\n', mimetype='text/plain; charset=utf-8')


# ---------------- 测试任务 ----------------

@app.post('/api/runs')
def api_run_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        target_id = int(data.get('target_id'))
        backend_ids = [int(x) for x in (data.get('backend_ids') or [])]
        run_id = runner.start_run(target_id, backend_ids)
    except (RuntimeError, ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'run_id': run_id})


@app.get('/api/runs')
def api_runs():
    runs = db.get_runs()
    for r in runs:
        total, done, failed = db.run_counts(r['id'])
        r['backend_total'] = total
        r['done_count'] = done
        r['fail_count'] = failed
    return jsonify(runs)


@app.get('/api/runs/<int:rid>')
def api_run_detail(rid):
    run = db.get_run(rid)
    if not run:
        return jsonify({'error': '测试记录不存在'}), 404
    items = db.get_run_items(rid)
    for it in items:
        it['metrics_obj'] = json.loads(it['metrics']) if it['metrics'] else None
    # 报告文本统一脱敏（覆盖历史报告）
    run['report'] = quality.mask_report_text(run['report'])
    return jsonify({'run': run, 'items': items, 'log': runner.get_log(rid)})


@app.post('/api/runs/<int:rid>/stop')
def api_run_stop(rid):
    if not runner.stop_run(rid):
        return jsonify({'error': '该测试未在运行'}), 400
    return jsonify({'ok': True})


@app.delete('/api/runs/<int:rid>')
def api_run_delete(rid):
    if runner.stop_run(rid):
        return jsonify({'error': '测试进行中，请先停止再删除'}), 400
    if not db.get_run(rid):
        return jsonify({'error': '测试记录不存在'}), 404
    db.delete_run(rid)
    return jsonify({'ok': True})


@app.get('/api/runs/<int:rid>/report')
def api_run_report(rid):
    run = db.get_run(rid)
    if not run:
        return jsonify({'error': '测试记录不存在'}), 404
    return Response(
        quality.mask_report_text(run['report'] or ''),
        mimetype='text/markdown; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=iperf3-report-{rid}.md'})


@app.get('/api/status')
def api_status():
    return jsonify({'active_run_id': runner.active_run_id(), 'version': APP_VERSION})


@app.errorhandler(ValueError)
def handle_value_error(e):
    return jsonify({'error': str(e)}), 400


if __name__ == '__main__':
    db.init_db()
    from waitress import serve
    # clear_untrusted_proxy_headers=False：保留 X-Forwarded-For 交给 _client_ip()
    # 按规则采信（仅反代/内网来源），否则经 Caddy 接入的 Agent 真实 IP 会被剥离
    serve(app, host='0.0.0.0', port=8000, threads=8, clear_untrusted_proxy_headers=False)
