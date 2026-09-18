"""iperf3-fleet 面板 Web 服务：登录验证、机器管理（Agent 接入）、测试任务、Agent API。"""
import base64
import datetime
import json
import os
import secrets

from flask import Flask, Response, jsonify, redirect, render_template, request, session

from . import db, runner

app = Flask(__name__)
app.json.ensure_ascii = False
app.secret_key = db.get_secret_key()
app.permanent_session_lifetime = datetime.timedelta(days=30)

AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent')

# 无需登录即可访问：登录页/登录接口/健康检查/Agent 脚本与 Agent API（用令牌认证）
_PUBLIC_PREFIXES = ('/agent/', '/api/agent/')
_PUBLIC_PATHS = {'/login', '/api/login', '/api/health', '/favicon.ico'}


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
    return render_template('index.html')


@app.route('/login')
def login_page():
    if session.get('user'):
        return redirect('/')
    return render_template('login.html')


@app.post('/api/login')
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    user, password = db.get_auth()
    ok = (secrets.compare_digest(str(data.get('user') or ''), user)
          and secrets.compare_digest(str(data.get('password') or ''), password))
    if not ok:
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
    def s(k, default=''):
        v = data.get(k)
        return default if v is None else str(v).strip()

    name = s('name')
    if not name:
        raise ValueError('名称不能为空')
    role = s('role', 'backend')
    if role not in ('backend', 'target'):
        raise ValueError('角色无效')
    return {'name': name, 'role': role, 'region': s('region'), 'bandwidth': s('bandwidth')}


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
    if not db.get_machine(mid):
        return jsonify({'error': '机器不存在'}), 404
    db.delete_machine(mid)
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


@app.post('/api/agent/heartbeat')
def agent_heartbeat():
    m = _agent_machine()
    if not m:
        return jsonify({'error': 'invalid token'}), 403
    m = db.touch_machine(m['id'], request.headers.get('X-Agent-Host', ''),
                         request.remote_addr or '')
    job = db.next_queued_job(m['id'])
    lines = ['status=ok']
    if job:
        cmd_b64 = base64.b64encode(job['cmd'].encode('utf-8')).decode('ascii')
        lines += [f"job_id={job['id']}", f"timeout={job['timeout']}", f'cmd_b64={cmd_b64}']
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
        run['report'] or '',
        mimetype='text/markdown; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=iperf3-report-{rid}.md'})


@app.get('/api/status')
def api_status():
    return jsonify({'active_run_id': runner.active_run_id()})


@app.errorhandler(ValueError)
def handle_value_error(e):
    return jsonify({'error': str(e)}), 400


if __name__ == '__main__':
    db.init_db()
    app.run(host='0.0.0.0', port=8000, threaded=True)
