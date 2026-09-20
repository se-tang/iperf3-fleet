"""iperf3 / ping 输出解析、线路质量评价、Markdown 报告生成。"""
import json
import re

# iperf3 汇总行，如: [  5]   0.00-10.00  sec   163 MBytes   137 Mbits/sec    0            sender
SUM_RE = re.compile(
    r'^\[\s*(?:\d+|SUM)\s*\]\s+([\d.]+)\s*-\s*([\d.]+)\s+sec\s+'
    r'([\d.]+)\s+([KMG]?Bytes)\s+([\d.]+)\s+([KMG]?bits)/sec(?:\s+(\d+))?\s+(sender|receiver)\s*$',
    re.IGNORECASE)
# 汇总区表头，如: [ ID] Interval           Transfer     Bitrate         Retr
HEADER_RE = re.compile(r'^\[\s*ID\s*\]')


def _to_mbits(val, unit):
    v = float(val)
    u = unit.lower()
    if u.startswith('g'):
        return v * 1000.0
    if u.startswith('k'):
        return v / 1000.0
    if u.startswith('m'):
        return v
    return v / 1000000.0


def _to_mb(val, unit):
    v = float(val)
    u = unit.lower()
    if u.startswith('g'):
        return v * 1024.0
    if u.startswith('k'):
        return v / 1024.0
    return v


def parse_iperf(out):
    """返回 (sender, receiver) 两条汇总行，各含 mbits / transfer_mb / retr。"""
    sender = None
    receiver = None
    for line in (out or '').splitlines():
        m = SUM_RE.match(line.strip())
        if not m:
            continue
        entry = {
            'transfer_mb': _to_mb(m.group(3), m.group(4)),
            'mbits': _to_mbits(m.group(5), m.group(6)),
            'retr': int(m.group(7)) if m.group(7) else 0,
        }
        if m.group(8).lower() == 'sender':
            sender = entry
        else:
            receiver = entry
    return sender, receiver


def summary_block(out):
    """提取 iperf3 输出末尾的汇总区：表头 + sender + receiver 三行。"""
    header = None
    rows = []
    for line in (out or '').splitlines():
        s = line.rstrip()
        if HEADER_RE.match(s.strip()) and 'Interval' in s:
            header = s
        if SUM_RE.match(s.strip()):
            rows.append(s)
    parts = ([header] if header else []) + rows
    return '\n'.join(parts)


def parse_ping(out):
    r = {}
    m = re.search(r'(\d+)\s*packets transmitted', out or '')
    if m:
        r['pkts_transmitted'] = int(m.group(1))
    m = re.search(r'(\d+)\s*received', out or '')
    if m:
        r['pkts_received'] = int(m.group(1))
    m = re.search(r'([\d.]+)%\s*packet loss', out or '')
    if m:
        r['loss_pct'] = float(m.group(1))
    m = re.search(r'=\s*([\d.]+)\/([\d.]+)\/([\d.]+)\/([\d.]+)\s*ms', out or '')
    if m:
        r['rtt_min'] = float(m.group(1))
        r['rtt_avg'] = float(m.group(2))
        r['rtt_max'] = float(m.group(3))
        r['mdev_ms'] = float(m.group(4))
    return r


def ping_block(out):
    """提取 ping 汇总两行：transmitted... / rtt min/avg/max/mdev..."""
    keep = []
    for line in (out or '').splitlines():
        s = line.strip()
        if 'packets transmitted' in s or s.startswith('rtt ') or s.startswith('round-trip '):
            keep.append(s)
    return '\n'.join(keep)


def parse_metrics(ping_raw, up_raw, down_raw):
    """把三段原始输出解析为结构化指标（含报告用的三段摘录块）。"""
    mt = parse_ping(ping_raw)
    us, ur = parse_iperf(up_raw)
    ds, dr = parse_iperf(down_raw)
    mt['up_sender_mbits'] = us['mbits'] if us else None
    mt['up_receiver_mbits'] = ur['mbits'] if ur else None
    mt['down_sender_mbits'] = ds['mbits'] if ds else None
    mt['down_receiver_mbits'] = dr['mbits'] if dr else None
    # 上行取正向测试的 sender，下行取 -R 测试的 receiver
    mt['up_mbits'] = mt['up_sender_mbits']
    mt['down_mbits'] = mt['down_receiver_mbits']
    retrs = [e['retr'] for e in (us, ds) if e and e.get('retr') is not None]
    mt['retr_total'] = sum(retrs) if retrs else None
    mt['ping_block'] = ping_block(ping_raw)
    mt['up_block'] = summary_block(up_raw)
    mt['down_block'] = summary_block(down_raw)
    return mt


def fmt_num(v, nd=1):
    if v is None:
        return '—'
    s = f'{float(v):.{nd}f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def parse_bw(label):
    """解析带宽标记为 Mbit/s，如 '500M'->500, '1G'->1000, '不限'->None。"""
    if not label:
        return None
    m = re.search(r'(\d+(?:\.\d+)?)\s*([GgMm])?', str(label))
    if not m:
        return None
    v = float(m.group(1))
    u = (m.group(2) or 'm').lower()
    return v * 1000.0 if u == 'g' else v


def evaluate(mt, machine):
    """按丢包/抖动/重传/收发比/标称带宽达成率给线路打分并写回 mt。"""
    notes = []
    pen = 0

    loss = mt.get('loss_pct')
    if loss is not None:
        if loss == 0:
            notes.append('0%丢包')
        elif loss <= 1:
            pen += 1
            notes.append(f'{fmt_num(loss)}%轻微丢包')
        elif loss <= 3:
            pen += 2
            notes.append(f'{fmt_num(loss)}%丢包偏多')
        else:
            pen += 3
            notes.append(f'{fmt_num(loss)}%严重丢包')

    mdev = mt.get('mdev_ms')
    if mdev is not None:
        if mdev <= 2:
            notes.append(f'抖动{fmt_num(mdev, 2)}ms极小')
        elif mdev <= 5:
            pen += 1
            notes.append(f'抖动{fmt_num(mdev, 2)}ms略大')
        elif mdev <= 15:
            pen += 2
            notes.append(f'抖动{fmt_num(mdev, 2)}ms偏大')
        else:
            pen += 3
            notes.append(f'抖动{fmt_num(mdev, 2)}ms很大')

    retr = mt.get('retr_total')
    if retr is not None:
        if retr == 0:
            notes.append('全程无重传')
        elif retr <= 10:
            pen += 1
            notes.append(f'{retr}次重传')
        elif retr <= 50:
            pen += 2
            notes.append(f'重传{retr}次较多')
        else:
            pen += 3
            notes.append(f'重传{retr}次严重')

    us_, ur_ = mt.get('up_sender_mbits'), mt.get('up_receiver_mbits')
    if us_ and ur_ and ur_ / us_ < 0.9:
        pen += 1 if ur_ / us_ >= 0.7 else 2
        notes.append('上行接收率偏低，传输中有损耗')
    ds_, dr_ = mt.get('down_sender_mbits'), mt.get('down_receiver_mbits')
    if ds_ and dr_ and dr_ / ds_ < 0.9:
        pen += 1 if dr_ / ds_ >= 0.7 else 2
        notes.append('下行接收率偏低，传输中有损耗')

    labeled = parse_bw(machine.get('bandwidth'))
    if labeled:
        for direction, val in (('上行', mt.get('up_mbits')), ('下行', mt.get('down_mbits'))):
            if not val:
                continue
            pct = val / labeled * 100
            if pct < 50:
                pen += 1
                notes.append(f'{direction}仅约标称{labeled:g}M的{pct:.0f}%')
            elif pct < 80:
                pen += 1
                notes.append(f'{direction}低于标称带宽（{fmt_num(val)}/{labeled:g}M）')
            elif pct > 110:
                notes.append(f'{direction}超出标称带宽（{fmt_num(val)}/{labeled:g}M）')

    rating = '优秀' if pen == 0 else '良好' if pen <= 2 else '一般' if pen <= 4 else '较差'
    mt['rating'] = rating
    mt['rating_detail'] = '，'.join(notes) if notes else '—'
    mt['rating_penalty'] = pen
    return mt


def mask_ip(ip):
    """报告脱敏：IPv4 只保留 A.B 两段（C/D 段打码）；IPv6 保留前两组。"""
    if not ip:
        return ip
    parts = ip.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return f'{parts[0]}.{parts[1]}.*.*'
    m = re.match(r'^([0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}):', ip)
    if m:
        return m.group(1) + ':****'
    return ip


# 兜底打码：对整份报告文本生效，覆盖历史报告与原始数据中残留的 IP
_IPV4_RE = re.compile(r'\b(\d{1,3}\.\d{1,3})\.\d{1,3}\.\d{1,3}\b')


def mask_report_text(text):
    if not text:
        return text
    return _IPV4_RE.sub(r'\1.*.*', text)


def build_report(run, target, items):
    """生成 Markdown 报告：汇总表（含线路质量评价列）+ 每台机器的原始摘录。"""
    proto = 'IPv6' if int(run.get('ip_version') or 4) == 6 else 'IPv4'
    lines = []
    lines.append('# iperf3 线路质量测试报告')
    lines.append('')
    lines.append(
        f"- **目标机器**：{target['name']}"
        f"（{target.get('region') or '地区未标记'} · {target.get('bandwidth') or '带宽未标记'} · `{mask_ip(target.get('host'))}`）")
    lines.append(f"- **测试协议**：{proto}（后端发起端与目标被测端均使用 {proto}）")
    lines.append(f"- **测试时间**：{run['created_at']} ~ {run.get('finished_at') or ''}")
    lines.append('- **测试方式**：iperf3 单线程 10 秒 ×（上行 / 下行 -R）+ `ping -c 200 -i 1`；iperf3 全局串行，ping 与其它机器的 iperf3 并行')
    lines.append('')
    lines.append('| 后端机器 | 地区 | 带宽 | 丢包率 | RTT avg | 抖动 mdev | 上行 (sender) | 下行 (receiver) | 重传 | 线路质量评价 |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|')
    for it in items:
        name = it['machine_name']
        region = it['machine_region'] or '—'
        bw = it['machine_bandwidth'] or '—'
        if it['status'] == 'done' and it['metrics']:
            mt = json.loads(it['metrics'])
            lines.append(
                f"| {name} | {region} | {bw} "
                f"| {fmt_num(mt.get('loss_pct'))}% | {fmt_num(mt.get('rtt_avg'))} ms "
                f"| {fmt_num(mt.get('mdev_ms'), 2)} ms "
                f"| {fmt_num(mt.get('up_mbits'))} Mbit/s | {fmt_num(mt.get('down_mbits'))} Mbit/s "
                f"| {fmt_num(mt.get('retr_total'), 0)} "
                f"| **{mt.get('rating', '—')}**：{mt.get('rating_detail', '—')} |")
        else:
            err = (it['error'] or it['status']).replace('|', '\\|').replace('\n', ' ')
            lines.append(f"| {name} | {region} | {bw} | — | — | — | — | — | — | ❌ 失败：{err} |")

    lines.append('')
    lines.append('## 原始数据')
    for idx, it in enumerate(items, 1):
        lines.append('')
        lines.append(
            f"### 后端机器{idx}：{it['machine_name']}"
            f"（{it['machine_region'] or '未标记'} · {it['machine_bandwidth'] or '未标记'} · {mask_ip(it['machine_host'])}）")
        lines.append('')
        lines.append('```')
        if it['status'] == 'done' and it['metrics']:
            mt = json.loads(it['metrics'])
            blocks = [mt.get('ping_block'), mt.get('up_block'), mt.get('down_block')]
            body = '\n\n'.join(b for b in blocks if b)
            lines.append(body if body else '（无输出）')
        else:
            lines.append(f"测试失败：{it['error'] or it['status']}")
        lines.append('```')
    if run.get('error'):
        lines.append('')
        lines.append(f"> ⚠️ {run['error']}")
    return mask_report_text('\n'.join(lines))
