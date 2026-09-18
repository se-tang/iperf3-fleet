"""测试编排：目标机起 iperf3 -s，后端机器逐台串行执行 上/下行 iperf3 + ping。"""
import json
import shlex
import threading
import time
import traceback

from . import db, quality
from . import ssh_utils as su

_state_lock = threading.Lock()
_active = {}  # run_id -> {'stop': bool, 'log': [..]}


def active_run_id():
    with _state_lock:
        for rid in _active:
            return rid
    return None


def start_run(target_id, backend_ids):
    with _state_lock:
        if _active:
            raise RuntimeError('已有测试任务正在运行，请等待完成或先停止')
        target = db.get_machine(target_id)
        if not target:
            raise RuntimeError('目标机器不存在')
        if target['role'] != 'target':
            raise RuntimeError(f'机器「{target["name"]}」的角色不是「目标机器」，请先在机器管理中修改角色')
        if not backend_ids:
            raise RuntimeError('请至少选择一台后端机器')
        for bid in backend_ids:
            if not db.get_machine(bid):
                raise RuntimeError(f'后端机器 #{bid} 不存在')
        run_id = db.create_run(target, backend_ids)
        _active[run_id] = {'stop': False, 'log': []}
    threading.Thread(target=_worker, args=(run_id, target), daemon=True).start()
    return run_id


def stop_run(run_id):
    st = _active.get(run_id)
    if st:
        st['stop'] = True
        return True
    return False


def get_log(run_id):
    st = _active.get(run_id)
    if st:
        return list(st['log'])
    run = db.get_run(run_id)
    return run['log'].splitlines() if run and run['log'] else []


def _worker(run_id, target):
    st = _active[run_id]
    log_lines = st['log']

    def log(msg):
        log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(log_lines) > 4000:
            del log_lines[:len(log_lines) - 4000]

    def stop_check():
        if st['stop']:
            raise su.RunAborted()

    ssh_t = None
    status = 'finished'
    error = ''
    try:
        log(f"=== 测试开始 | 目标机器: {target['name']} ({target['host']}) ===")
        ssh_t = su.connect(target)
        log(f"[目标] SSH 连接成功 {target['host']}:{target['ssh_port']}")
        log('[目标] 检查 iperf3 / ping（缺失时自动安装）...')
        code, out = su.run_cmd(ssh_t, su.SCRIPT_ENSURE, timeout=600,
                               logcb=lambda s: log(f'[目标] {s}'), stop_check=stop_check)
        if code != 0:
            raise RuntimeError('目标机 iperf3/ping 安装失败: ' + (out or '').strip()[-300:])
        su.ensure_server(ssh_t, logcb=lambda s: log(f'[目标] {s}'))
        log('[目标] iperf3 -s 已就绪，等待后端机器连接 (端口 5201)')

        items = db.get_run_items(run_id)
        for it in items:
            if st['stop']:
                raise su.RunAborted()
            _run_backend(st, log, stop_check, ssh_t, target, it)

        log('=== 全部后端测试结束，正在关闭目标机 iperf3 server ===')
        su.stop_server(ssh_t)
    except su.RunAborted:
        status = 'stopped'
        error = '用户手动停止测试'
        log('=== 测试被手动停止 ===')
        db.fail_open_items(run_id, '测试被手动停止')
    except Exception as e:
        status = 'failed'
        error = str(e) or e.__class__.__name__
        log(f'[致命错误] {error}')
        log(traceback.format_exc()[-800:])
        db.fail_open_items(run_id, '目标机异常，未执行')
    finally:
        if ssh_t:
            try:
                ssh_t.close()
            except Exception:
                pass

        run = db.get_run(run_id)
        items = db.get_run_items(run_id)
        total = len(items)
        done = sum(1 for i in items if i['status'] == 'done')
        if status == 'finished':
            if total and done == total:
                status = 'finished'
            elif done:
                status = 'partial'
            else:
                status = 'failed'
        target_info = {
            'name': run['target_name'], 'host': run['target_host'],
            'region': run['target_region'], 'bandwidth': run['target_bandwidth'],
        }
        report = quality.build_report(run, target_info, items)
        db.finish_run(run_id, status=status, report=report,
                      log_text='\n'.join(log_lines), error=error)
        log(f'=== 测试结束，状态: {status}，报告已生成 ===')
        with _state_lock:
            _active.pop(run_id, None)


def _run_backend(st, log, stop_check, ssh_t, target, it):
    iid = it['id']
    name = it['machine_name']
    db.update_item(iid, status='running')
    log(f"--- 后端 {name} ({it['machine_host']}) 开始 ---")
    ssh = None
    try:
        m = db.get_machine(it['machine_id']) if it['machine_id'] else None
        if not m:
            raise RuntimeError('机器已被删除，无法连接')
        ssh = su.connect(m)
        log(f'[{name}] SSH 连接成功')
        code, out = su.run_cmd(ssh, su.SCRIPT_ENSURE, timeout=600,
                               logcb=lambda s: log(f'[{name}] {s}'), stop_check=stop_check)
        if code != 0:
            raise RuntimeError('iperf3/ping 检查安装失败: ' + (out or '').strip()[-300:])

        # 目标机 server 掉线（如上一轮异常）则自动重启
        try:
            msg = su.ensure_server(ssh_t)
            if 'RESTARTED' in msg or 'SERVER_STARTED' in msg:
                log('[目标] 检测到 iperf3 server 掉线，已重新启动')
        except Exception as e:
            raise RuntimeError(f'目标机 iperf3 server 不可用: {e}')

        tq = shlex.quote(target['host'])

        cmd_up = f'iperf3 -c {tq} -t 10'
        log(f'[{name}] $ {cmd_up}')
        code, up_raw = su.run_cmd(ssh, cmd_up, timeout=90,
                                  logcb=lambda s: log(f'[{name}] {s}'), stop_check=stop_check)
        if code != 0:
            raise RuntimeError('上行测试失败: ' + (up_raw or '').strip()[-200:])
        time.sleep(1)

        cmd_down = f'iperf3 -c {tq} -R -t 10'
        log(f'[{name}] $ {cmd_down}')
        code, down_raw = su.run_cmd(ssh, cmd_down, timeout=90,
                                    logcb=lambda s: log(f'[{name}] {s}'), stop_check=stop_check)
        if code != 0:
            raise RuntimeError('下行测试失败: ' + (down_raw or '').strip()[-200:])
        time.sleep(1)

        cmd_ping = f'ping -c 200 -i 1 {tq}'
        log(f'[{name}] $ {cmd_ping}  （约需 200 秒）')
        code, ping_raw = su.run_cmd(ssh, cmd_ping, timeout=300,
                                    logcb=lambda s: log(f'[{name}] {s}'), stop_check=stop_check)
        if 'packets transmitted' not in (ping_raw or ''):
            raise RuntimeError('ping 失败: ' + (ping_raw or '').strip()[-200:])

        metrics = quality.parse_metrics(ping_raw, up_raw, down_raw)
        quality.evaluate(metrics, it)
        db.update_item(iid, status='done', ping_raw=ping_raw, up_raw=up_raw,
                       down_raw=down_raw,
                       metrics=json.dumps(metrics, ensure_ascii=False))
        log(f"[{name}] ✅ 完成 | 上行 {quality.fmt_num(metrics.get('up_mbits'))} Mbit/s "
            f"| 下行 {quality.fmt_num(metrics.get('down_mbits'))} Mbit/s "
            f"| 丢包 {quality.fmt_num(metrics.get('loss_pct'))}% "
            f"| 评价: {metrics.get('rating')}")
    except su.RunAborted:
        db.update_item(iid, status='failed', error='手动停止')
        raise
    except Exception as e:
        db.update_item(iid, status='failed', error=str(e))
        log(f'[{name}] ❌ 失败: {e}')
    finally:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
