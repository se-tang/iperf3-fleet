"""用样例数据验证解析/评价/报告逻辑。直接运行: python tests/test_sample.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='iperf3-fleet-test-')

from app import quality  # noqa: E402

PING_RAW = """PING 1.2.3.4 (1.2.3.4) 56(84) bytes of data.
64 bytes from 1.2.3.4: icmp_seq=1 ttl=52 time=144.3 ms
64 bytes from 1.2.3.4: icmp_seq=2 ttl=52 time=144.4 ms

--- 1.2.3.4 ping statistics ---
200 packets transmitted, 200 received, 0% packet loss, time 199034ms
rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms
"""

UP_RAW = """Connecting to host 1.2.3.4, port 5201
[  5] local 10.0.0.1 port 51234 connected to 1.2.3.4 port 5201
[ ID] Interval           Transfer     Bitrate         Retr  Cwnd
[  5]   0.00-1.00   sec  16.4 MBytes   137 Mbits/sec    0   1.20 MBytes
[  5]   0.00-2.00   sec  32.7 MBytes   137 Mbits/sec    0   1.45 MBytes
- - - - - - - - - - - - - - - - - - - - - - - -
[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.00  sec   163 MBytes   137 Mbits/sec    0            sender
[  5]   0.00-10.15  sec   163 MBytes   135 Mbits/sec                  receiver
"""

DOWN_RAW = """Connecting to host 1.2.3.4, port 5201 (reverse mode)
[  5] local 10.0.0.1 port 51236 connected to 1.2.3.4 port 5201
[ ID] Interval           Transfer     Bitrate         Retr  Cwnd
[  5]   0.00-1.00   sec  15.0 MBytes   126 Mbits/sec
[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.15  sec   147 MBytes   121 Mbits/sec    0            sender
[  5]   0.00-10.00  sec   129 MBytes   108 Mbits/sec                  receiver
"""


def test_parse():
    mt = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    assert mt['loss_pct'] == 0.0, mt
    assert abs(mt['rtt_avg'] - 144.402) < 1e-6, mt
    assert abs(mt['mdev_ms'] - 0.255) < 1e-6, mt
    assert abs(mt['up_mbits'] - 137.0) < 1e-6, mt
    assert abs(mt['down_mbits'] - 108.0) < 1e-6, mt
    assert mt['retr_total'] == 0, mt
    assert mt['ping_block'] == (
        '200 packets transmitted, 200 received, 0% packet loss, time 199034ms\n'
        'rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms'), mt['ping_block']
    assert '0.00-10.00  sec   163 MBytes   137 Mbits/sec    0            sender' in mt['up_block']
    assert mt['up_block'].count('sender') == 1 and mt['up_block'].count('receiver') == 1
    assert '[ ID] Interval' in mt['up_block'] and '[ ID] Interval' in mt['down_block']
    print('parse_metrics OK:', {k: mt[k] for k in ('up_mbits', 'down_mbits', 'loss_pct', 'rtt_avg', 'mdev_ms', 'retr_total')})
    return mt


def test_units():
    # Gbits / GBytes 单位换算
    us, ur = quality.parse_iperf('[  5]   0.00-10.00  sec   1.19 GBytes   1.02 Gbits/sec    3            sender')
    assert abs(us['mbits'] - 1020.0) < 1e-6
    assert abs(us['transfer_mb'] - 1.19 * 1024) < 1e-6
    assert us['retr'] == 3
    print('unit conversion OK')


def test_evaluate():
    mt = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    quality.evaluate(mt, {'bandwidth': '500M'})
    assert mt['rating'] in ('优秀', '良好', '一般', '较差'), mt
    assert '标称' in mt['rating_detail'], mt
    print('evaluate OK:', mt['rating'], '|', mt['rating_detail'])


def test_report():
    mt = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    quality.evaluate(mt, {'bandwidth': '500M'})
    run = {
        'created_at': '2026-09-19 12:00:00', 'finished_at': '2026-09-19 12:10:00',
        'target_name': '目标VPS', 'target_host': '1.2.3.4',
        'target_region': '洛杉矶', 'target_bandwidth': '1G', 'error': '',
    }
    items = [
        {'machine_name': 'HK-01', 'machine_region': '香港', 'machine_bandwidth': '500M',
         'machine_host': '5.6.7.8', 'status': 'done', 'error': '',
         'metrics': __import__('json').dumps(mt, ensure_ascii=False)},
        {'machine_name': 'JP-02', 'machine_region': '东京', 'machine_bandwidth': '200M',
         'machine_host': '9.9.9.9', 'status': 'failed', 'error': 'SSH 连接失败', 'metrics': ''},
    ]
    report = quality.build_report(run, {'name': '目标VPS', 'host': '1.2.3.4', 'region': '洛杉矶', 'bandwidth': '1G'}, items)
    assert '| HK-01 | 香港 | 500M | 0% | 144.4 ms | 0.26 ms | 137 Mbit/s | 108 Mbit/s | 0 |' in report
    assert '❌ 失败：SSH 连接失败' in report
    assert '### 后端机器1：HK-01' in report
    assert 'rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms' in report
    # IP 脱敏：C/D 段打码
    assert '1.2.*.*' in report and '1.2.3.4' not in report
    assert '5.6.*.*' in report and '5.6.7.8' not in report
    print('---- 报告预览 ----')
    print(report)
    return report


def test_xff_and_limiter():
    """CF-Connecting-IP 优先（CF 前置场景）+ XFF 取尾项 + 伪造不可绕过限速。"""
    from app import db
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()

    # 1) CF 直连（remote 属于 CF 网段）：CF-Connecting-IP 最可信
    m = db.create_machine({'name': 'xff', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m['token'],
                    'CF-Connecting-IP': '203.0.113.7',
                    'X-Forwarded-For': '1.2.3.4'},
           environ_base={'REMOTE_ADDR': '172.70.0.5'})
    assert db.get_machine(m['id'])['agent_ip'] == '203.0.113.7'

    # 2) Caddy 反代（remote 为内网、非 CF 网段）：自带/走私的 CF 头必须被忽略，
    #    以 Caddy 覆盖后的 XFF 为准
    m1b = db.create_machine({'name': 'xff-caddy', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m1b['token'],
                    'CF-Connecting-IP': '9.9.9.9',
                    'X-Forwarded-For': '203.0.113.7'},
           environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert db.get_machine(m1b['id'])['agent_ip'] == '203.0.113.7'

    # 2b) 方式 B 实际链路（CF 橙云 → Caddy → 面板）：Caddyfile.origin 用
    #     header_up 把 XFF 覆盖为 CF-Connecting-IP（CF 写入的 Agent 真实 IP），
    #     请求自带的其他转发头/走私值被覆盖丢弃
    m1c = db.create_machine({'name': 'xff-cfchain', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m1c['token'],
                    'CF-Connecting-IP': '173.245.48.5',
                    'X-Forwarded-For': '203.0.113.77'},
           environ_base={'REMOTE_ADDR': '172.18.0.1'})
    assert db.get_machine(m1c['id'])['agent_ip'] == '203.0.113.77'

    # 3) 公网直连伪造 XFF/CF 头 → 一律不采信，取 remote_addr
    m2 = db.create_machine({'name': 'xff2', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m2['token'], 'X-Forwarded-For': '6.6.6.6',
                    'CF-Connecting-IP': '7.7.7.7'},
           environ_base={'REMOTE_ADDR': '93.184.216.34'})
    assert db.get_machine(m2['id'])['agent_ip'] == '93.184.216.34'

    # 4) 限速桶用 remote_addr：轮换伪造头无法绕过，5 次失败即锁定
    for i in range(5):
        c.post('/api/login',
               headers={'X-Forwarded-For': f'10.0.0.{i}', 'CF-Connecting-IP': f'10.1.1.{i}'},
               environ_base={'REMOTE_ADDR': '127.0.0.1'},
               json={'user': 'x', 'password': 'y'})
    resp = c.post('/api/login',
                  headers={'X-Forwarded-For': '10.9.9.9', 'CF-Connecting-IP': '10.1.1.99'},
                  environ_base={'REMOTE_ADDR': '127.0.0.1'},
                  json={'user': 'x', 'password': 'y'})
    assert resp.status_code == 429, resp.get_data(as_text=True)
    print('CF-Connecting-IP 优先 / XFF 取尾项 / 伪造不可信 / 限速不可绕过 OK')


def test_test_protocol():
    """IPv4/IPv6 测试地址：心跳按协议族分别记录、探测结果只补空缺、报告标注协议。"""
    import json as _json
    from app import agent_jobs, db, runner

    # 1) 双栈面板下心跳来源会在 v4 / v6 之间跳动：两族地址必须都留下，不能互相覆盖
    m = db.create_machine({'name': 'dual', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m['id'], 'dual', '93.184.216.34')
    db.touch_machine(m['id'], 'dual', '2606:4700:4700::1111')
    got = db.get_machine(m['id'])
    assert got['ip4'] == '93.184.216.34', got
    assert got['ip6'] == '2606:4700:4700::1111', got

    # 2) 默认取 IPv4，只有显式要求时才取 IPv6
    assert db.machine_test_ip(got, 4) == '93.184.216.34'
    assert db.machine_test_ip(got, 6) == '2606:4700:4700::1111'

    # 3) 探测结果只补空缺（心跳观测到的地址更可信），force=True 才覆盖
    m2 = db.create_machine({'name': 'nat', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m2['id'], 'nat', '93.184.216.34')          # NAT 后的公网出口
    db.set_machine_ips(m2['id'], {'ip4': '10.0.0.5', 'ip6': '2606:4700:4700::1111'})
    got2 = db.get_machine(m2['id'])
    assert got2['ip4'] == '93.184.216.34', got2      # 私网探测值不覆盖观测值
    assert got2['ip6'] == '2606:4700:4700::1111', got2
    db.set_machine_ips(m2['id'], {'ip4': '93.184.216.99'}, force=True)
    assert db.get_machine(m2['id'])['ip4'] == '93.184.216.99'

    # 4) 探测输出解析：只收「协议族匹配 + 全局可路由」的地址
    found = agent_jobs.parse_ip_report(
        'IPV4=93.184.216.34\nIPV6=2606:4700:4700::1111\nIPV4=192.168.1.5\nIPV6=fd00::1\n')
    assert found == {'ip4': '93.184.216.34', 'ip6': '2606:4700:4700::1111'}, found
    assert agent_jobs.parse_ip_report('IPV4=2606:4700::1\nIPV6=93.184.216.34\n') == {}

    # 5) 目标机缺 IPv6 时，IPv6 测试必须直接拒绝（而不是跑出一堆失败）
    backend = db.create_machine({'name': 'be', 'role': 'backend', 'region': '', 'bandwidth': ''})
    db.touch_machine(backend['id'], 'be', '93.184.216.34')
    only4 = db.create_machine({'name': 'only4', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(only4['id'], 'only4', '93.184.216.34')
    try:
        runner.start_run(only4['id'], [backend['id']], 6)
        raise AssertionError('缺 IPv6 地址时不应允许 IPv6 测试')
    except RuntimeError as e:
        assert 'IPv6' in str(e), e
    assert runner.active_run_id() is None

    # 6) 报告标注测试协议，并按 IPv6 脱敏
    run6 = {'created_at': 't0', 'finished_at': 't1', 'ip_version': 6, 'error': '',
            'target_name': 'only6', 'target_host': '2606:4700:4700::1111'}
    rep = quality.build_report(run6, {'name': 'only6', 'host': '2606:4700:4700::1111',
                                      'region': '洛杉矶', 'bandwidth': '1G'}, [])
    assert '- **测试协议**：IPv6' in rep, rep
    assert '2606:4700:****' in rep and '1111' not in rep.split('测试协议')[0], rep
    run4 = dict(run6, ip_version=4, target_host='93.184.216.34')
    rep4 = quality.build_report(run4, {'name': 'v4', 'host': '93.184.216.34',
                                        'region': '', 'bandwidth': ''}, [])
    assert '- **测试协议**：IPv4' in rep4, rep4
    assert '93.184.*.*' in rep4
    print('测试协议 / 地址探测 / 报告标注 OK')


def test_discovery_flow():
    """端到端：面板派发地址探测任务 → Agent 回报 → 地址入库（走真实 Agent API 与签名任务）。"""
    import threading
    import time
    from app import db, runner
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()

    m = db.create_machine({'name': 'disc', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m['id'], 'disc', '93.184.216.34')      # 只有 IPv4（心跳观测）

    result = {}
    t = threading.Thread(target=lambda: result.update(runner.discover_machine(m['id']) or {}),
                         daemon=True)
    t.start()
    # 面板把探测任务排进队列后，模拟 Agent 领取并回传本机地址
    job = None
    for _ in range(100):
        job = db.get_db().execute(
            "SELECT * FROM jobs WHERE machine_id=? AND status='queued' ORDER BY id DESC",
            (m['id'],)).fetchone()
        if job:
            break
        time.sleep(0.05)
    assert job, '面板未派发地址探测任务'
    body = 'IPV4=93.184.216.34\nIPV6=2606:4700:4700::1111\n'
    assert 'IPV4=' in job['cmd'] or 'IPV4' in job['cmd'], job['cmd']
    c.post('/api/agent/output',
           headers={'X-Agent-Token': m['token']},
           data={'job_id': job['id'], 'text': body, 'done': '1', 'exit_code': '0'})
    t.join(timeout=10)
    got = db.get_machine(m['id'])
    assert got['ip6'] == '2606:4700:4700::1111', got
    assert got['ip4'] == '93.184.216.34', got
    print('地址探测端到端 OK:', result)


def test_probe_script():
    """SCRIPT_REPORT_IPS 的地址提取：稳定 v6 优先、只有临时地址也能取到、ULA/链路本地忽略。"""
    if os.name != 'posix':
        print('跳过 SCRIPT_REPORT_IPS 测试（仅 POSIX 环境执行）')
        return
    import shutil
    import subprocess
    from app import agent_jobs as aj

    if not shutil.which('bash') or not shutil.which('awk'):
        print('跳过 SCRIPT_REPORT_IPS 测试（缺 bash/awk）')
        return

    tmp = tempfile.mkdtemp(prefix='iperf3-fleet-probe-')
    script = os.path.join(tmp, 'probe.sh')
    with open(script, 'w', encoding='utf-8') as f:
        f.write(aj.SCRIPT_REPORT_IPS + '\n')

    # 假 ip / hostname：数据文件放在 stub 同目录，脚本内用 $(dirname "$0") 定位，
    # 不把绝对路径写进 stub（Windows 反斜杠路径在 MSYS 下不可靠）
    bins = {}
    for name, with_ip in (('bin', True), ('bin_noip', False)):
        d = os.path.join(tmp, name)
        os.makedirs(d)
        p = os.path.join(d, 'hostname')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n[ "$1" = "-I" ] && cat "$(dirname "$0")/hn.txt"\n')
        os.chmod(p, 0o755)
        if with_ip:
            p = os.path.join(d, 'ip')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('#!/bin/sh\n'
                        'd=$(dirname "$0")\n'
                        'case "$*" in\n'
                        '  "-4 route get 1.1.1.1") cat "$d/ip4.txt" ;;\n'
                        '  *"addr show"*) cat "$d/ip6.txt" ;;\n'
                        'esac\n')
            os.chmod(p, 0o755)
        bins[name] = d

    V4 = '1.1.1.1 via 10.0.0.1 dev eth0 src 93.184.216.34 uid 0\n'
    STABLE_TMP = ('2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
                  '    inet6 2408:8207:1234:5678::1/64 scope global\n'
                  '       valid_lft forever preferred_lft forever\n'
                  '    inet6 2408:8207:1234:5678:abcd:ef01:2345:6789/64 scope global temporary dynamic\n')
    TMP_ONLY = ('2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
                '    inet6 240e:3b0:1234:5678:9abc:def0:1234:5678/64 scope global temporary dynamic\n')
    ULA_LINK = ('2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
                '    inet6 fd00::1/64 scope global\n'
                '    inet6 fe80::216:3eff:fe12:3456/64 scope link\n')
    HN_MIX = '10.0.0.8 240e:3b0:1234:5678:9abc:def0:1234:5678 fe80::1'

    cases = [
        ('稳定地址优先于临时地址', 'bin', V4, STABLE_TMP, '',
         '93.184.216.34', '2408:8207:1234:5678::1'),
        ('只有临时地址时退用临时地址', 'bin', V4, TMP_ONLY, '',
         '93.184.216.34', '240e:3b0:1234:5678:9abc:def0:1234:5678'),
        ('腾讯云 /128 scope global dynamic', 'bin', V4,
         '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
         '    inet6 2402:4e00:1020:1404:0:9a3f:1b2c:3d4e/128 scope global dynamic\n',
         '', '93.184.216.34', '2402:4e00:1020:1404:0:9a3f:1b2c:3d4e'),
        ('ULA / 链路本地必须忽略', 'bin', V4, ULA_LINK, '', '93.184.216.34', ''),
        ('无 ip 命令时回退 hostname -I', 'bin_noip', '', '', HN_MIX,
         '10.0.0.8', '240e:3b0:1234:5678:9abc:def0:1234:5678'),
    ]
    for title, binname, v4_out, v6_out, hn_out, want4, want6 in cases:
        for fn, text in (('ip4.txt', v4_out), ('ip6.txt', v6_out), ('hn.txt', hn_out)):
            with open(os.path.join(bins[binname], fn), 'w', encoding='utf-8') as f:
                f.write(text)
        env = dict(os.environ)
        env['PATH'] = bins[binname] + os.pathsep + os.environ.get('PATH', '')
        if binname == 'bin_noip':
            # 该用例依赖 stub 版的 hostname：个别环境（如 MSYS）PATH 会被重排导致
            # 系统 hostname 抢先，这时跳过而不是误报失败
            got_hn = subprocess.run(['bash', '-c', 'hostname -I'], capture_output=True,
                                    text=True, env=env).stdout.strip()
            if got_hn != hn_out.strip():
                print('跳过「无 ip 命令」用例：本环境的 hostname 无法被 stub 覆盖')
                continue
        out = subprocess.run(['bash', script], capture_output=True, text=True, env=env).stdout
        vals = dict(ln.split('=', 1) for ln in out.splitlines() if '=' in ln)
        assert vals.get('IPV4') == want4, (title, out)
        assert vals.get('IPV6') == want6, (title, out)

    # 探测结果能被面板解析（临时地址同样有效）
    got = aj.parse_ip_report('IPV4=93.184.216.34\nIPV6=240e:3b0:1234:5678:9abc:def0:1234:5678\n')
    assert got == {'ip4': '93.184.216.34',
                   'ip6': '240e:3b0:1234:5678:9abc:def0:1234:5678'}, got
    print('SCRIPT_REPORT_IPS 地址提取 OK（稳定/临时/ULA/无 ip 四种情况）')


def test_job_ids():
    """任务编号必须跨「面板数据库重置」继续递增。

    Agent 端防重放是「编号比本地水位小就拒收」，而 AUTOINCREMENT 在面板重置后会
    从 1 重新开始 —— 那会让整队 Agent 静默拒收所有任务（机器在线、却探测不到地址、
    测试全部超时）。所以编号必须用时间戳打底。
    """
    import time as _time
    from app import db
    db.init_db()
    m = db.create_machine({'name': 'jid', 'role': 'backend', 'region': '', 'bandwidth': ''})
    a = db.create_job(m['id'], 'echo a', 10)
    b = db.create_job(m['id'], 'echo b', 10)
    assert a > 10 ** 12, a                    # 时间戳打底，不再是 1、2、3
    assert b > a, (a, b)
    _time.sleep(0.005)                        # 模拟「面板数据库被重置」：清空任务表
    db.get_db().execute('DELETE FROM jobs')
    db.get_db().commit()
    c = db.create_job(m['id'], 'echo c', 10)
    assert c > b, (b, c)
    print('任务编号跨重置递增 OK:', a, b, c)


def test_probe_error_surfaced():
    """探测结果与失败原因都要落到机器资料上，面板才能说明「为什么没有 IPv6」。"""
    import threading
    import time as _time
    from app import db, runner
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()
    m = db.create_machine({'name': 'probe', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m['id'], 'probe', '93.184.216.34')

    def probe_round(body):
        res = {}
        t = threading.Thread(
            target=lambda: res.update(runner.discover_machine(m['id']) or {}), daemon=True)
        t.start()
        job = None
        for _ in range(100):
            job = db.get_db().execute(
                "SELECT * FROM jobs WHERE machine_id=? AND status='queued' ORDER BY id DESC",
                (m['id'],)).fetchone()
            if job:
                break
            _time.sleep(0.05)
        assert job, '面板未派发地址探测任务'
        c.post('/api/agent/output',
               headers={'X-Agent-Token': m['token']},
               data={'job_id': job['id'], 'text': body, 'done': '1', 'exit_code': '0'})
        t.join(timeout=10)
        return db.get_machine(m['id'])

    # 1) 探到全局地址：写入资料并清空失败原因
    got = probe_round('IPV4=93.184.216.34\nIPV6=240e:3b0:1234:5678::9\n')
    assert got['ip6'] == '240e:3b0:1234:5678::9', got
    assert got['probe_error'] == '', got

    # 2) 只有私网 / ULA：地址不写，但原因要留在机器上供面板显示
    got = probe_round('IPV4=10.0.0.8\nIPV6=fd00::1\n')
    assert '全局' in got['probe_error'], got

    # 3) 任务没回传（Agent 拒收/离线）时，提示要能指路
    assert 'agent.log' in runner._probe_fail_reason(TimeoutError('[x] 任务执行超时(20s): r4='))
    print('地址探测失败原因回传 OK:', got['probe_error'][:30])


def test_test_params():
    """测试参数：校验、iperf3 命令拼装、端口/协议落到 Agent 脚本上。"""
    from app import agent_jobs as aj
    from app import runner

    # 1) 默认值与历史行为一致（单线程 / 10 秒 / 5201 / ping 200 / TCP）
    cfg = runner.normalize_params({})
    assert cfg == {'streams': 1, 'duration': 10, 'port': 5201, 'target_port': 0,
                   'ping_count': 200, 'udp': False, 'udp_bandwidth': '100M', 'ports': {}}, cfg

    # 2) 自定义值（字符串数字、带宽大小写归一）
    cfg = runner.normalize_params({'streams': '4', 'duration': 30, 'port': '6500',
                                   'target_port': '6500', 'ping_count': 50,
                                   'udp': True, 'udp_bandwidth': '200m'})
    assert cfg == {'streams': 4, 'duration': 30, 'port': 6500, 'target_port': 6500,
                   'ping_count': 50, 'udp': True, 'udp_bandwidth': '200M', 'ports': {}}, cfg

    # 3) 越界/非法值必须报错，而不是悄悄换成别的参数
    for bad in ({'streams': 0}, {'streams': 99}, {'duration': 0}, {'duration': 9999},
                {'port': 0}, {'port': 70000}, {'ping_count': 0}, {'ping_count': 99999},
                {'streams': 'x'}, {'udp_bandwidth': '100'}):
        try:
            runner.normalize_params(bad)
            raise AssertionError(f'{bad} 应该被拒绝')
        except RuntimeError:
            pass

    # 4) 命令拼装：端口 / 线程 / 时长 / UDP / 下行
    tcp = dict(cfg, udp=False, udp_bandwidth='100M')
    assert runner.build_iperf_cmd('1.2.3.4', tcp) == 'iperf3 -c 1.2.3.4 -p 6500 -t 30 -P 4', tcp
    assert runner.build_iperf_cmd('1.2.3.4', tcp, reverse=True) == \
        'iperf3 -c 1.2.3.4 -p 6500 -t 30 -P 4 -R'
    udp_cmd = runner.build_iperf_cmd('2606:4700:4700::1111', dict(tcp, udp=True, udp_bandwidth='200M'))
    assert udp_cmd == 'iperf3 -c 2606:4700:4700::1111 -p 6500 -t 30 -P 4 -u -b 200M', udp_cmd

    # 5) 端口等参数要真的落到 Agent 脚本里；每台后端机可各用各的端口，
    #    所以起 server 的脚本必须只清本端口，不能把别的端口的 server 一起杀了
    srv = aj.script_start_server(6500)
    assert 'PORT=6500' in srv and '/tmp/iperf3-server-$PORT.pid' in srv, srv
    assert '5201' not in srv, srv
    assert 'iperf3 -s -p $PORT' in srv, srv                 # 只清同端口残留
    assert "pkill -f 'iperf3 -s'" not in srv, srv           # 不能无差别清杀
    assert 'port="6500"' in aj.script_check_port('1.2.3.4', 6500)
    assert '/tmp/iperf3-server*.pid' in aj.SCRIPT_STOP_SERVER   # 收尾时全部停掉
    assert aj.valid_port(0) == 5201 and aj.valid_port(70000) == 5201
    assert aj.valid_port('abc') == 5201 and aj.valid_port('6500') == 6500

    # 6) API 层：非法参数返回 400 且说明原因（合法参数会真的开跑，不在此测试）
    from app import db
    from app.app import app as flask_app
    db.init_db()
    t = db.create_machine({'name': 'param-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'param-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c = flask_app.test_client()
    _login(c)
    r = c.post('/api/runs', json={'target_id': t['id'], 'backend_ids': [b['id']], 'streams': 999})
    assert r.status_code == 400 and '线程数' in r.get_json()['error'], r.get_json()
    r = c.post('/api/runs', json={'target_id': t['id'], 'backend_ids': [b['id']],
                                  'udp': True, 'udp_bandwidth': '100'})
    assert r.status_code == 400 and '带宽' in r.get_json()['error'], r.get_json()

    # 7) 每台后端机可以单独指定端口：逐台落库 + 命令用各自的端口
    b2 = db.create_machine({'name': 'param-b2', 'role': 'backend', 'region': '', 'bandwidth': ''})
    run_id = db.create_run(t, [b['id'], b2['id']], 4, target_host='93.184.216.34',
                           port=5201, target_port=5201, ports={b['id']: 6001},
                           streams=2, duration=5, udp=False, udp_bandwidth='100M', ping_count=10)
    items = db.get_run_items(run_id)
    assert [it['port'] for it in items] == [6001, 0], items      # 0 = 跟随默认端口
    assert db.get_run(run_id)['target_port'] == 5201
    rcfg = runner.run_cfg(db.get_run(run_id))
    assert [runner.item_port(it, rcfg) for it in items] == [6001, 5201]
    assert runner.build_iperf_cmd('93.184.216.34', runner.item_cfg(rcfg, items[0])) == \
        'iperf3 -c 93.184.216.34 -p 6001 -t 5 -P 2'
    assert runner.build_iperf_cmd('93.184.216.34', runner.item_cfg(rcfg, items[1])) == \
        'iperf3 -c 93.184.216.34 -p 5201 -t 5 -P 2'
    assert runner.normalize_params({'ports': {'3': '6001'}})['ports'] == {3: 6001}
    for bad in ({'ports': {'1': 0}}, {'ports': {'1': 99999}}, {'ports': {'1': 'x'}},
                {'ports': {'x': 5201}}):
        try:
            runner.normalize_params(bad)
            raise AssertionError(f'{bad} 应该被拒绝')
        except RuntimeError:
            pass

    # 8) 目标机监听端口：NAT 后的目标机本机监听端口可与后端连接端口不同
    assert runner.normalize_params({'target_port': ''})['target_port'] == 0     # 留空 = 跟随
    try:
        runner.normalize_params({'target_port': 70000})
        raise AssertionError('越界的目标机监听端口应被拒绝')
    except RuntimeError:
        pass
    nat = runner.normalize_params({'port': 30001, 'target_port': 5201})
    assert nat['port'] == 30001 and nat['target_port'] == 5201
    nat_cfg = dict(runner.DEFAULT_PARAMS, port=30001, target_port=5201)
    follow = [{'machine_name': 'A', 'port': 0}, {'machine_name': 'B', 'port': 0}]
    assert runner.target_port_of(nat_cfg) == 5201
    assert runner.serve_ports(follow, nat_cfg) == [5201]        # 只监听目标机本机端口
    assert [runner.item_port(it, nat_cfg) for it in follow] == [30001, 30001]
    # 多机各用各端口（无 NAT）：目标机监听端口 + 各机单独指定的连接端口都要起
    cfg2 = dict(runner.DEFAULT_PARAMS, port=5201, target_port=0)
    items2 = [{'machine_name': 'A', 'port': 6001}, {'machine_name': 'B', 'port': 0},
              {'machine_name': 'C', 'port': 6002}]
    assert runner.serve_ports(items2, cfg2) == [5201, 6001, 6002]
    assert [runner.item_port(it, cfg2) for it in items2] == [6001, 5201, 6002]
    assert runner.target_port_of(dict(runner.DEFAULT_PARAMS, port=6500, target_port=0)) == 6500
    print('测试参数校验 / iperf3 命令拼装 OK:', runner.build_iperf_cmd('1.2.3.4', tcp))


def _login(client):
    """测试用：读面板首次初始化写在 .initial_password 的密码登录一次。

    先清掉登录限速状态——前面的用例会故意打失败登录把测试客户端 IP 锁住。
    """
    from app import db
    from app import app as app_module
    app_module._login_fails.clear()
    user, _ = db.get_auth()
    with open(db.INITIAL_PW_PATH, encoding='utf-8') as f:
        pw = f.read().strip()
    r = client.post('/api/login', json={'user': user, 'password': pw})
    assert r.status_code == 200, r.get_json()


def test_multistream_and_udp_parse():
    """多线程（-P）取 [SUM] 汇总行；UDP 的丢包/抖动取接收端，且不适用重传。"""
    from app import quality

    up = ('[ ID] Interval           Transfer     Bitrate         Retr\n'
          '[  4]   0.00-10.00  sec  25.0 MBytes  21.0 Mbits/sec    0             sender\n'
          '[  4]   0.00-10.00  sec  24.9 MBytes  20.9 Mbits/sec                  receiver\n'
          '[  6]   0.00-10.00  sec  25.1 MBytes  21.0 Mbits/sec    1             sender\n'
          '[  6]   0.00-10.00  sec  25.0 MBytes  20.9 Mbits/sec                  receiver\n'
          '[SUM]   0.00-10.00  sec   100 MBytes  83.9 Mbits/sec    1             sender\n'
          '[SUM]   0.00-10.00  sec  99.8 MBytes  83.7 Mbits/sec                  receiver\n')
    s, r = quality.parse_iperf(up)
    assert s['mbits'] == 83.9 and r['mbits'] == 83.7 and s['retr'] == 1, (s, r)
    assert 'SUM' in quality.summary_block(up)

    udp_up = ('[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams\n'
              '[  4]   0.00-10.00  sec   115 MBytes  96.4 Mbits/sec  0.000 ms  0/8320 (0%)  sender\n'
              '[  4]   0.00-10.00  sec   114 MBytes  95.9 Mbits/sec  0.019 ms  12/8320 (0.14%)  receiver\n')
    udp_down = ('[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams\n'
                '[  4]   0.00-10.00  sec   110 MBytes  92.3 Mbits/sec  0.000 ms  0/7960 (0%)  sender\n'
                '[  4]   0.00-10.00  sec   109 MBytes  91.6 Mbits/sec  0.031 ms  40/7960 (0.5%)  receiver\n')
    mt = quality.parse_metrics(PING_RAW, udp_up, udp_down)
    assert mt['udp'] is True
    assert mt['up_udp_loss_pct'] == 0.14 and mt['down_udp_loss_pct'] == 0.5, mt
    assert mt['up_jitter_ms'] == 0.019 and mt['down_jitter_ms'] == 0.031, mt
    assert mt['retr_total'] is None, mt          # UDP 没有重传概念
    assert mt['up_mbits'] == 96.4 and mt['down_mbits'] == 91.6, mt
    quality.evaluate(mt, {'bandwidth': ''})
    assert 'UDP 丢包' in mt['rating_detail'] and 'iperf3 抖动' in mt['rating_detail'], mt
    print('多线程 / UDP 解析 OK:', mt['rating'], '|', mt['rating_detail'])


def test_report_params_and_udp_columns():
    """报告写明本次参数；UDP 测试把「重传」列换成 UDP 丢包 / 抖动。"""
    import json as _json
    from app import quality

    udp_up = ('[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams\n'
              '[  4]   0.00-10.00  sec   115 MBytes  96.4 Mbits/sec  0.000 ms  0/8320 (0%)  sender\n'
              '[  4]   0.00-10.00  sec   114 MBytes  95.9 Mbits/sec  0.019 ms  12/8320 (0.14%)  receiver\n')
    mt = quality.parse_metrics(PING_RAW, udp_up, udp_up)
    quality.evaluate(mt, {'bandwidth': ''})
    items = [{'machine_name': 'HK-01', 'machine_region': '香港', 'machine_bandwidth': '500M',
              'machine_host': '5.6.7.8', 'status': 'done', 'error': '', 'port': 6001,
              'metrics': _json.dumps(mt, ensure_ascii=False)},
             {'machine_name': 'JP-02', 'machine_region': '东京', 'machine_bandwidth': '200M',
              'machine_host': '9.9.9.9', 'status': 'done', 'error': '', 'port': 5201,
              'metrics': _json.dumps(mt, ensure_ascii=False)}]
    run = {'created_at': 't0', 'finished_at': 't1', 'ip_version': 4, 'error': '',
           'target_name': 'T', 'target_host': '1.2.3.4', 'streams': 4, 'duration': 30,
           'port': 5201, 'udp': 1, 'udp_bandwidth': '200M', 'ping_count': 50}
    rep = quality.build_report(run, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''}, items)
    assert ('- **测试参数**：iperf3 4 线程（-P）× 上行 / 下行（-R）各 30 秒（-t），'
            '端口 5201，UDP（-u，目标带宽 200M/流）') in rep, rep
    assert 'UDP 丢包 上/下' in rep and 'UDP 抖动 上/下' in rep, rep
    assert '重传' not in rep.split('## 原始数据')[0], rep
    # 逐台端口：不同端口时列一行「各机连接端口」，原始数据小节也标出端口
    assert '- **各机连接端口**：HK-01 6001 · JP-02 5201' in rep, rep
    assert '### 后端机器1：HK-01（香港 · 500M · 5.6.*.* · 连接端口 6001）' in rep, rep
    assert '### 后端机器2：JP-02（东京 · 200M · 9.9.*.* · 连接端口 5201）' in rep, rep

    # 只有一台机器、端口一致时不出现「各机连接端口」这一行
    one = [dict(items[0], port=5201)]
    rep3 = quality.build_report(run, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''}, one)
    assert '各机连接端口' not in rep3, rep3

    # 目标机在 NAT 后：报告要写清「本机监听端口」与「后端连接端口」
    nat_run = dict(run, port=30001, target_port=5201, udp=0, streams=1, duration=10,
                   udp_bandwidth='100M', ping_count=200)
    rep4 = quality.build_report(nat_run, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''},
                                [dict(items[0], port=0)])
    assert '端口：目标机监听 5201，后端连接 30001' in rep4, rep4
    assert '### 后端机器1：HK-01（香港 · 500M · 5.6.*.* · 连接端口 30001）' in rep4, rep4

    # 旧记录（没有参数列）退回默认参数，TCP 表头保留重传列
    old = {'created_at': 't0', 'finished_at': 't1', 'ip_version': 4, 'error': '',
           'target_name': 'T', 'target_host': '1.2.3.4'}
    rep2 = quality.build_report(old, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''}, items)
    assert ('- **测试参数**：iperf3 1 线程（-P）× 上行 / 下行（-R）各 10 秒（-t），'
            '端口 5201，TCP') in rep2, rep2
    assert '| 重传 |' in rep2, rep2
    print('报告参数行 / UDP 列 / 目标机端口 OK')


if __name__ == '__main__':
    test_parse()
    test_units()
    test_evaluate()
    test_report()
    test_xff_and_limiter()
    test_test_protocol()
    test_discovery_flow()
    test_probe_script()
    test_job_ids()
    test_probe_error_surfaced()
    test_test_params()
    test_multistream_and_udp_parse()
    test_report_params_and_udp_columns()
    print('\nALL TESTS PASSED')
