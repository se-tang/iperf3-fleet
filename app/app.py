"""iperf3 测试面板 Web 服务：机器管理 API + 测试任务 API。"""
import json
import re

from flask import Flask, Response, jsonify, render_template, request

from . import db, runner
from . import ssh_utils as su

app = Flask(__name__)
app.json.ensure_ascii = False


@app.route('/')
def index():
    return render_template('index.html')


# ---------------- machines ----------------

def sanitize_machine(m):
    return {k: m[k] for k in ('id', 'name', 'host', 'ssh_port', 'ssh_user',
                              'auth_type', 'role', 'bandwidth', 'region', 'created_at')}


def _norm_key(key_str):
    lines = [ln.strip() for ln in (key_str or '').splitlines() if ln.strip()]
    return '\n'.join(lines)


def _machine_fields(data, is_new, old=None):
    def s(k, default=''):
        v = data.get(k)
        if v is None:
            return default
        return str(v).strip()

    name = s('name')
    host = s('host')
    if not name or not host:
        raise ValueError('名称和地址不能为空')
    role = s('role', 'backend')
    if role not in ('backend', 'target'):
        raise ValueError('角色无效')
    auth_type = s('auth_type', 'password')
    if auth_type not in ('password', 'key'):
        raise ValueError('认证方式无效')
    try:
        port = int(s('ssh_port', '22') or 22)
    except ValueError:
        raise ValueError('SSH 端口无效')
    if not (1 <= port <= 65535):
        raise ValueError('SSH 端口无效')
    fields = {
        'name': name,
        'host': host,
        'ssh_port': port,
        'ssh_user': s('ssh_user', 'root') or 'root',
        'auth_type': auth_type,
        'password': s('password'),
        'private_key': _norm_key(data.get('private_key') or ''),
        'key_passphrase': s('key_passphrase'),
        'role': role,
        'bandwidth': s('bandwidth'),
        'region': s('region'),
    }
    if not is_new and old is not None:
        # 编辑时认证信息留空表示保持不变
        if not fields['password']:
            fields['password'] = old['password']
        if not fields['private_key']:
            fields['private_key'] = old['private_key']
        if not fields['key_passphrase']:
            fields['key_passphrase'] = old['key_passphrase']
    return fields


@app.get('/api/machines')
def api_machines():
    return jsonify([sanitize_machine(m) for m in db.get_machines()])


@app.post('/api/machines')
def api_machine_create():
    data = request.get_json(force=True, silent=True) or {}
    fields = _machine_fields(data, is_new=True)
    return jsonify(sanitize_machine(db.create_machine(fields)))


@app.put('/api/machines/<int:mid>')
def api_machine_update(mid):
    old = db.get_machine(mid)
    if not old:
        return jsonify({'error': '机器不存在'}), 404
    data = request.get_json(force=True, silent=True) or {}
    fields = _machine_fields(data, is_new=False, old=old)
    return jsonify(sanitize_machine(db.update_machine(mid, fields)))


@app.delete('/api/machines/<int:mid>')
def api_machine_delete(mid):
    if not db.get_machine(mid):
        return jsonify({'error': '机器不存在'}), 404
    db.delete_machine(mid)
    return jsonify({'ok': True})


@app.post('/api/machines/<int:mid>/check')
def api_machine_check(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    try:
        ssh = su.connect(m)
    except Exception as e:
        return jsonify({'ok': False, 'ssh': False, 'message': f'SSH 连接失败: {e}'})
    try:
        code, out = su.run_cmd(
            ssh,
            'command -v iperf3 >/dev/null 2>&1 && echo "INSTALLED:$(iperf3 --version 2>&1 | head -n1)" '
            '|| echo NOT_INSTALLED',
            timeout=30)
        out = (out or '').strip()
        if 'NOT_INSTALLED' in out:
            return jsonify({'ok': True, 'ssh': True, 'iperf3': False,
                            'message': 'SSH 连接正常；iperf3 未安装'})
        if 'INSTALLED:' in out:
            ver = out.split('INSTALLED:', 1)[1].splitlines()[0]
            return jsonify({'ok': True, 'ssh': True, 'iperf3': True, 'version': ver,
                            'message': f'SSH 连接正常；iperf3 已安装（{ver}）'})
        return jsonify({'ok': True, 'ssh': True, 'iperf3': False,
                        'message': 'SSH 连接正常；iperf3 状态未知'})
    except Exception as e:
        return jsonify({'ok': False, 'message': f'检测失败: {e}'})
    finally:
        try:
            ssh.close()
        except Exception:
            pass


@app.post('/api/machines/<int:mid>/install')
def api_machine_install(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    try:
        ssh = su.connect(m)
    except Exception as e:
        return jsonify({'ok': False, 'message': f'SSH 连接失败: {e}'})
    try:
        code, out = su.run_cmd(ssh, su.SCRIPT_ENSURE, timeout=600)
        if code != 0:
            return jsonify({'ok': False, 'message': '安装失败: ' + (out or '').strip()[-300:]})
        setup = '；'.join(ln for ln in (out or '').splitlines() if ln.startswith('[setup]'))
        return jsonify({'ok': True, 'message': setup or '安装完成'})
    except Exception as e:
        return jsonify({'ok': False, 'message': f'安装失败: {e}'})
    finally:
        try:
            ssh.close()
        except Exception:
            pass


# ---------------- runs ----------------

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
