"""部署成功横幅：打印面板地址、登录账号密码。"""
import json
import os
import urllib.request

port = os.environ.get('PANEL_PORT', '8080')
auth_path = os.path.join(os.environ.get('DATA_DIR', '/data'), 'auth.json')

ip = ''
try:
    ip = urllib.request.urlopen('https://api.ipify.org', timeout=3).read().decode().strip()
except Exception:
    pass

user, pw = '', ''
try:
    with open(auth_path, encoding='utf-8') as f:
        d = json.load(f)
    user = d.get('user') or ''
    pw = d.get('password') or ''
except Exception:
    pass

if not ip:
    ip = '<面板机IP>'

print(f"""
==================================================
 ✅  iperf3-fleet 面板已部署成功！
     面板地址:  http://{ip}:{port}
     登录账号:  {user or '（见 auth.json）'}
     登录密码:  {pw or '（见 auth.json）'}
 下一步: 打开面板 → 添加机器 → 复制接入命令到机器上执行
==================================================""", flush=True)
