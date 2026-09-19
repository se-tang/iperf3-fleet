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


if __name__ == '__main__':
    test_parse()
    test_units()
    test_evaluate()
    test_report()
    print('\nALL TESTS PASSED')
