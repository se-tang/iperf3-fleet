"""定时任务调度：按自定义间隔重复跑同一组前后端机器，用于长期对比与参照。

与手动测试的关系：测试执行仍然全部走 runner（同一套参数校验、串行通道、报告生成），
调度器只负责「什么时候开跑」和「这一轮结果记到哪个定时任务名下」。

几条刻意的设计：
  * 面板重启不丢任务：下次执行时间落库（next_run_at），重启后到点继续跑；
    重启时正在跑的那一轮标记为 interrupted，但**不动**下次执行时间。
  * 与手动测试冲突时默认「跳过本轮」，并且把跳过原因写清楚（面板上可见），
    而不是排队等待——长期任务排队会把后面每一轮都推后，对比曲线的时间轴就乱了。
  * 乱序调用 due_schedules 不会重复触发：触发前先把 next_run_at 推到未来。
"""
import datetime
import json
import threading
import time
import traceback

from . import db, quality, runner

# 检查周期：比最短间隔（5 分钟）小得多，保证不会漏掉任何一轮
TICK_SECONDS = 5
MIN_INTERVAL = 300          # 5 分钟
MAX_INTERVAL = 30 * 24 * 3600   # 30 天
_ON_BUSY = ('skip', 'wait')

_started = False
_start_lock = threading.Lock()


class InvalidSchedule(ValueError):
    """定时任务字段不合法（继承 ValueError，Web 层统一转成 400）。"""


def now_str(ts=None):
    return datetime.datetime.fromtimestamp(
        ts if ts is not None else time.time()).strftime('%Y-%m-%d %H:%M:%S')


def parse_time(text):
    """把库里的 'YYYY-mm-dd HH:MM:SS' 解析成时间戳；解析不了返回 None。"""
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return time.mktime(time.strptime(str(text)[:19], fmt))
        except (TypeError, ValueError):
            continue
    return None


def normalize_schedule(data, base=None):
    """校验并收敛定时任务字段；非法值直接报错，不静默改动用户输入。"""
    base = base or {}

    def pick(key, default):
        return data[key] if key in data else base.get(key, default)

    def num(key, lo, hi, label, default):
        raw = pick(key, default)
        if raw is None or str(raw).strip() == '':
            return default
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise InvalidSchedule(f'{label}必须是整数（收到 {raw!r}）')
        if not lo <= v <= hi:
            raise InvalidSchedule(f'{label}需在 {lo}–{hi} 之间（收到 {v}）')
        return v

    name = str(pick('name', '') or '').strip()[:64] or '定时任务'
    try:
        target_id = int(str(pick('target_id', 0)).strip())
    except (TypeError, ValueError):
        raise InvalidSchedule('请选择目标机器')
    if not target_id:
        raise InvalidSchedule('请选择目标机器')
    backends = []
    for x in (pick('backend_ids', []) or []):
        try:
            v = int(str(x).strip())
        except (TypeError, ValueError):
            raise InvalidSchedule(f'后端机器编号必须是整数（收到 {x!r}）')
        if v and v not in backends:
            backends.append(v)
    if not backends:
        raise InvalidSchedule('请至少选择一台后端机器')

    ip_version = 6 if str(pick('ip_version', 4)).strip() == '6' else 4
    interval = num('interval_seconds', MIN_INTERVAL, MAX_INTERVAL, '间隔时间（秒）',
                   base.get('interval_seconds') or 3600)
    on_busy = str(pick('on_busy', 'skip') or 'skip').strip()
    if on_busy not in _ON_BUSY:
        on_busy = 'skip'
    ports = {}
    raw_ports = pick('ports', {}) or {}
    if isinstance(raw_ports, dict):
        for k, v in raw_ports.items():
            try:
                mid = int(str(k).strip())
                p = int(str(v).strip())
            except (TypeError, ValueError):
                continue
            if 1 <= p <= 65535:
                ports[mid] = p
    enabled = pick('enabled', True)
    enabled = 0 if str(enabled) in ('0', 'False', 'false', '') else 1
    return {
        'name': name,
        'enabled': enabled,
        'target_id': target_id,
        'backend_ids': backends,
        'ip_version': ip_version,
        'streams': num('streams', 1, 32, '线程数', 1),
        'duration': num('duration', 1, 300, '单向测试时长（秒）', 10),
        'port': num('port', 1, 65535, '目标机端口', 5201),
        'ping_count': num('ping_count', 1, 2000, 'ping 次数', 200),
        'udp': 1 if pick('udp', 0) else 0,
        'udp_bandwidth': str(pick('udp_bandwidth', '100M') or '100M').strip().upper(),
        'ports': ports,
        'interval_seconds': interval,
        'on_busy': on_busy,
    }


def _validate_refs(s):
    """触发前检查机器是否还在、角色是否还对——机器被删/改角色时给出可读的原因。"""
    target = db.get_machine(s['target_id'])
    if not target:
        return None, [], '目标机器已被删除'
    if target['role'] != 'target':
        return None, [], f"机器「{target['name']}」的角色已不是「目标机器」"
    backends = []
    for bid in s['backend_ids']:
        m = db.get_machine(bid)
        if not m:
            return None, [], f'后端机器 #{bid} 已被删除'
        if m['role'] != 'backend':
            return None, [], f"机器「{m['name']}」的角色已不是「后端机器」"
        backends.append(m)
    return target, backends, ''


def schedule_summary(s):
    """面板列表用：定时任务 + 人类可读的下次时间与参数摘要。"""
    out = dict(s)
    nxt = s.get('next_run_at')
    out['next_run_in'] = ''
    ts = parse_time(nxt)
    if s.get('enabled') and ts:
        delta = int(ts - time.time())
        out['next_run_in'] = _human_delta(delta)
    out['interval_text'] = _human_duration(int(s.get('interval_seconds') or 0))
    out['backend_count'] = len(s.get('backend_ids') or [])
    tgt = db.get_machine(s['target_id'])
    out['target_name'] = (tgt or {}).get('name') or '（已删除）'
    out['target_host'] = (tgt or {}).get('agent_ip') or ''
    names = []
    for bid in (s.get('backend_ids') or []):
        m = db.get_machine(bid)
        names.append((m or {}).get('name') or f'#{bid}(已删除)')
    out['backend_names'] = names
    return out


def _human_duration(sec):
    """人话化的间隔文案。数字与单位之间用不换行空格，面板窄列里不会被拆行。"""
    sec = int(sec or 0)
    if sec <= 0:
        return '—'
    if sec % 86400 == 0:
        return f'{sec // 86400}\u00a0天'
    if sec % 3600 == 0:
        return f'{sec // 3600}\u00a0小时'
    if sec % 60 == 0:
        return f'{sec // 60}\u00a0分钟'
    return f'{sec}\u00a0秒'


def _human_delta(sec):
    if sec <= 0:
        return '即将执行'
    if sec < 60:
        return f'{sec} 秒后'
    if sec < 3600:
        return f'{sec // 60} 分钟后'
    if sec < 86400:
        return f'{sec // 3600} 小时 {sec % 3600 // 60} 分后'
    return f'{sec // 86400} 天 {sec % 86400 // 3600} 小时后'


# ---------------- 触发与推进 ----------------

def compute_next(interval_seconds, frm=None):
    """下一次执行时间 = 现在 + 间隔（固定间隔语义，避免延误后越跑越偏）。"""
    frm = frm if frm is not None else time.time()
    return now_str(frm + max(MIN_INTERVAL, int(interval_seconds or MIN_INTERVAL)))


def next_run_from(interval_seconds, frm=None):
    """由于「本轮被跳过 / 冲突」而重新排队时，给一个更短的复查间隔。"""
    frm = frm if frm is not None else time.time()
    return now_str(frm + max(60, min(int(interval_seconds or 0) or 60, 300)))


def trigger(sid, planned_at=None, manual=False):
    """触发一轮：返回 (ok, note)。冲突/校验失败都写回定时任务，面板可见。"""
    s = db.get_schedule(sid)
    if not s:
        return False, '定时任务不存在'
    if not s.get('enabled') and not manual:
        return False, '定时任务已停用'
    planned_at = planned_at or now_str()
    target, backends, err = _validate_refs(s)
    if err:
        db.record_schedule_run(sid, None, planned_at, status='skipped', note=err)
        db.update_schedule(sid, {'last_status': 'skipped', 'last_error': err,
                                 'last_run_at': now_str(),
                                 'skip_count': int(s.get('skip_count') or 0) + 1})
        db.update_schedule(sid, {'next_run_at': compute_next(s['interval_seconds'])})
        return False, err

    params = {
        'streams': s['streams'], 'duration': s['duration'], 'port': s['port'],
        'udp': bool(s['udp']), 'udp_bandwidth': s['udp_bandwidth'],
        'ping_count': s['ping_count'], 'ports': s.get('ports') or {},
    }
    try:
        run_id = runner.start_run(
            s['target_id'], s['backend_ids'], s['ip_version'], params=params,
            source='schedule', schedule_id=s['id'], label=f"定时任务「{s['name']}」")
    except (RuntimeError, ValueError) as e:
        note = str(e)
        busy = '已有测试任务正在运行' in note
        if busy and s.get('on_busy') == 'wait':
            # 选择「等待」：不记跳过，1–5 分钟后复查（同一个计划周期内不会漏掉这一轮）
            db.update_schedule(sid, {'next_run_at': next_run_from(s['interval_seconds']),
                                     'last_status': 'waiting',
                                     'last_error': '面板有测试在跑，本轮等待中'})
            return False, '等待中：' + note
        db.record_schedule_run(sid, None, planned_at, status='skipped', note=note)
        db.update_schedule(sid, {
            'last_status': 'skipped', 'last_error': note, 'last_run_at': now_str(),
            'skip_count': int(s.get('skip_count') or 0) + 1,
            'next_run_at': compute_next(s['interval_seconds']),
        })
        return False, note

    db.record_schedule_run(sid, run_id, planned_at, status='running',
                           note='手动立即执行' if manual else '')
    db.update_schedule(sid, {'last_status': 'running', 'last_run_id': run_id,
                             'last_run_at': now_str(), 'last_error': '',
                             'next_run_at': compute_next(s['interval_seconds'])})
    return True, f'已启动测试 #{run_id}'


def due_check_once():
    """跑一次到点检查（测试里直接调用，避免起线程）。"""
    now = now_str()
    for s in db.due_schedules(now):
        try:
            trigger(s['id'], planned_at=now)
        except Exception:
            traceback.print_exc()


# ---------------- 完成回填 ----------------

def finish_pending():
    """扫描「已启动但还没回填」的定时任务轮次，把结果写回。

    用轮询而不是回调：runner 不需要知道调度器的存在，面板重启后也能自愈。
    """
    for s in db.get_schedules():
        rid = s.get('last_run_id')
        if s.get('last_status') != 'running' or not rid:
            continue
        run = db.get_run(rid)
        if not run:
            db.finish_schedule_run(s['id'], rid, 'failed', '测试记录已不存在')
            continue
        if run['status'] in ('running', 'pending'):
            continue
        total, done, failed = db.run_counts(rid)
        note = run.get('error') or ''
        if not note and failed:
            note = f'{failed} 台后端失败'
        if not note and done == 0:
            note = '本轮全部失败'
        db.finish_schedule_run(s['id'], rid, run['status'], note)
        try:
            db.update_schedule(s['id'], {
                'next_run_at': db.get_schedule(s['id']).get('next_run_at')
                or compute_next(s['interval_seconds']),
            })
        except Exception:
            pass


def _loop():
    while True:
        try:
            finish_pending()
            due_check_once()
        except Exception:
            traceback.print_exc()
        time.sleep(TICK_SECONDS)


def start_scheduler():
    """启动后台调度线程（幂等，重复调用只生效一次）。"""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True).start()


# ---------------- 长期对比报告 ----------------

def _agg(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {'n': len(vals), 'avg': sum(vals) / len(vals), 'min': min(vals), 'max': max(vals)}


def comparison_data(sid, limit=100):
    """把该定时任务的历史轮次整理成「按机器 × 按轮次」的对比数据。"""
    s = db.get_schedule(sid)
    if not s:
        return None
    window = db.schedule_run_window(sid, limit=limit)
    machines = []
    index = {}
    rounds = []
    for entry in window:
        run = entry['run']
        rounds.append({
            'run_id': run['id'],
            'created_at': run['created_at'],
            'finished_at': run.get('finished_at') or '',
            'status': run['status'],
            'target_host': run.get('target_host') or '',
            'done': sum(1 for it in entry['items'] if it['status'] == 'done'),
            'total': len(entry['items']),
        })
        for it in entry['items']:
            key = it['machine_id'] if it['machine_id'] else f"name:{it['machine_name']}"
            if key not in index:
                index[key] = {
                    'key': key, 'machine_id': it['machine_id'],
                    'name': it['machine_name'], 'region': it['machine_region'],
                    'bandwidth': it['machine_bandwidth'],
                    'series': [], 'up': [], 'down': [], 'loss': [], 'rtt': [],
                    'mdev': [], 'retr': [], 'udp_loss': [], 'rating': [],
                }
                machines.append(index[key])
            m = index[key]
            mt = it.get('metrics_obj') or {}
            m['series'].append({
                'run_id': run['id'], 'at': run['created_at'], 'status': it['status'],
                'up': mt.get('up_mbits'), 'down': mt.get('down_mbits'),
                'loss': mt.get('loss_pct'), 'rtt': mt.get('rtt_avg'),
                'mdev': mt.get('mdev_ms'), 'retr': mt.get('retr_total'),
                'udp_loss': mt.get('up_udp_loss_pct'), 'rating': mt.get('rating'),
                'error': it.get('error') or '',
            })
            if it['status'] != 'done':
                continue
            m['up'].append(mt.get('up_mbits'))
            m['down'].append(mt.get('down_mbits'))
            m['loss'].append(mt.get('loss_pct'))
            m['rtt'].append(mt.get('rtt_avg'))
            m['mdev'].append(mt.get('mdev_ms'))
            m['retr'].append(mt.get('retr_total'))
            m['udp_loss'].append(mt.get('up_udp_loss_pct'))
            if mt.get('rating'):
                m['rating'].append(mt['rating'])

    stats = []
    for m in machines:
        ups, downs = m['up'], m['down']
        trend = None
        # 趋势用「后半段均值 vs 前半段均值」比较：比首末单点更抗抖动
        if len(ups) >= 4:
            half = len(ups) // 2
            a, b = _agg(ups[:half]), _agg(ups[half:])
            if a and b and a['avg']:
                trend = (b['avg'] - a['avg']) / a['avg'] * 100.0
        common = {'优秀': 4, '良好': 3, '一般': 2, '较差': 1}
        stats.append({
            'name': m['name'], 'region': m['region'], 'bandwidth': m['bandwidth'],
            'samples': len(ups),
            'up': _agg(ups), 'down': _agg(downs),
            'loss': _agg(m['loss']), 'rtt': _agg(m['rtt']),
            'mdev': _agg(m['mdev']), 'retr': _agg(m['retr']),
            'udp_loss': _agg(m['udp_loss']),
            'trend_pct': trend,
            'best_rating': max(m['rating'], key=lambda r: common.get(r, 0)) if m['rating'] else None,
            'worst_rating': min(m['rating'], key=lambda r: common.get(r, 0)) if m['rating'] else None,
        })

    return {
        'schedule': schedule_summary(s),
        'rounds': rounds,
        'machines': [{'name': m['name'], 'region': m['region'],
                      'bandwidth': m['bandwidth'], 'series': m['series']} for m in machines],
        'stats': stats,
    }


def build_comparison_report(sid, limit=100):
    """长期对比 Markdown 报告：轮次概览 + 每台机器的均值/极值/趋势。"""
    data = comparison_data(sid, limit=limit)
    if not data:
        return ''
    s = data['schedule']
    rounds = data['rounds']
    lines = ['# iperf3 长期对比报告', '']
    lines.append(f"- **定时任务**：{s['name']}（#{s['id']}，{'启用中' if s['enabled'] else '已停用'}）")
    lines.append(f"- **目标机器**：{s['target_name']}（{quality.mask_ip(s.get('target_host') or '')}）")
    lines.append(f"- **后端机器**：{'、'.join(s['backend_names']) or '—'}（{s['backend_count']} 台）")
    proto = 'IPv6' if int(s['ip_version']) == 6 else 'IPv4'
    kind = f"UDP（-b {s['udp_bandwidth']}/流）" if s['udp'] else 'TCP'
    lines.append(f"- **测试参数**：{proto} · {kind} · -P {s['streams']} · 上/下行各 {s['duration']} 秒"
                 f" · 端口 {s['port']} · ping {s['ping_count']} 次")
    finished = max(0, int(s['run_count']) - int(s['fail_count']))
    lines.append(f"- **间隔**：每 {s['interval_text']} · **已完成轮次**：{int(s['run_count'])} 轮"
                 f"（成功 {finished} / 失败 {s['fail_count']} / 未跑成 {s['skip_count']}）")
    lines.append(f"- **当前状态**：{s.get('last_status') or '待首次执行'}"
                 f"{'，' + s['last_error'] if s.get('last_error') else ''}")
    if rounds:
        lines.append(f"- **数据范围**：{rounds[0]['created_at']} ~ {rounds[-1]['created_at']}")
    lines.append('')

    if not rounds:
        lines.append('> 还没有完成任何一轮测试，稍后再来看。')
        return quality.mask_report_text('\n'.join(lines))

    lines.append('## 每台机器的汇总（跨全部轮次）')
    lines.append('')
    lines.append('| 后端机器 | 地区 | 样本 | 上行均值 (Mbit/s) | 下行均值 (Mbit/s) '
                 '| 丢包均值 | RTT 均值 | 抖动均值 | 重传均值 | 趋势（后半段 vs 前半段） | 最好/最差评价 |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|---|')
    for st in data['stats']:
        up, down, loss = st['up'], st['down'], st['loss']
        rtt, mdev, retr = st['rtt'], st['mdev'], st['retr']
        trend = '—'
        if st['trend_pct'] is not None:
            t = st['trend_pct']
            arrow = '↑' if t > 3 else ('↓' if t < -3 else '→')
            trend = f'{arrow} {t:+.1f}%'
        ratings = ' / '.join(x for x in (st['best_rating'], st['worst_rating']) if x) or '—'
        lines.append(
            f"| {st['name']} | {st['region'] or '—'} | {st['samples']} "
            f"| {quality.fmt_num(up['avg'] if up else None)} "
            f"| {quality.fmt_num(down['avg'] if down else None)} "
            f"| {quality.fmt_num(loss['avg'] if loss else None, 2)}% "
            f"| {quality.fmt_num(rtt['avg'] if rtt else None)} ms "
            f"| {quality.fmt_num(mdev['avg'] if mdev else None, 2)} ms "
            f"| {quality.fmt_num(retr['avg'] if retr else None)} "
            f"| {trend} | {ratings} |")
    lines.append('')

    lines.append('## 每轮明细')
    lines.append('')
    by_round = {}
    for m in data['machines']:
        for pt in m['series']:
            by_round.setdefault(pt['run_id'], []).append((m['name'], pt))
    lines.append('| 轮次 | 时间 | 状态 | 后端机器 | 上行 (Mbit/s) | 下行 (Mbit/s) | 丢包 | RTT avg | 评价 |')
    lines.append('|---|---|---|---|---|---|---|---|---|')
    for r in rounds:
        pts = by_round.get(r['run_id'], [])
        if not pts:
            lines.append(f"| #{r['run_id']} | {r['created_at']} | {r['status']} | — | — | — | — | — | — |")
            continue
        for i, (name, pt) in enumerate(pts):
            rid = f"#{r['run_id']}" if i == 0 else ''
            at = r['created_at'] if i == 0 else ''
            status = r['status'] if i == 0 else ''
            if pt['status'] != 'done':
                lines.append(f"| {rid} | {at} | {status} | {name} | ❌ {pt['error'] or pt['status']} "
                             '| — | — | — | — |')
                continue
            lines.append(
                f"| {rid} | {at} | {status} | {name} "
                f"| {quality.fmt_num(pt['up'])} | {quality.fmt_num(pt['down'])} "
                f"| {quality.fmt_num(pt['loss'])}% | {quality.fmt_num(pt['rtt'])} ms "
                f"| {pt['rating'] or '—'} |")
    lines.append('')
    lines.append('> 提示：趋势 = 后半段轮次均值相对前半段的涨跌（±3% 以内视为稳定）；'
                 '上行/下行单位为 Mbit/s，跨轮对比需保证测试参数一致（本报告的参数固定）。')
    return quality.mask_report_text('\n'.join(lines))
