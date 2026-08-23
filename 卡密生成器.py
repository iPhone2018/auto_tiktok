# -*- coding: utf-8 -*-
"""
抖音火花助手 - 卡密生成器(仅卖家使用!)

⚠️ 私钥文件 seller_ed25519_private.pem 是核心资产:
   - 不要放入 git、不要随程序分发、不要发给任何人
   - 建议离线保管(加密 U 盘/密码管理器)
   - 泄露私钥 = 任何人都能自己造卡密

用法:
  python 卡密生成器.py init                    # 首次:生成密钥对,打印公钥
  python 卡密生成器.py gen -m <机器码> -u <抖音标识> -d 30   # 绑定抖音账号的 30 天卡密
  python 卡密生成器.py gen -m <机器码> -u <抖音标识> -H 1    # 绑定抖音账号的 1 小时卡密
  python 卡密生成器.py gen -m <机器码> -d 90 -n 5 -o cards.txt   # 批量 5 张,写入文件
  python 卡密生成器.py verify -k <卡密>          # 发货前自校验

注意:-u 为买家登录抖音后,程序「设置-授权信息」中显示的抖音标识。
     卡密绑定抖音标识后,买家必须在绑定的抖音账号登录状态下才能激活,
     激活后更换其他抖音账号将导致授权失效。

init 之后把打印出来的 PUBLIC_KEY_B64 粘贴到「抖音自动续火花-后端.py」
的 LICENSE_PUBLIC_KEY_B64 常量里,然后重新打包程序。

换绑(买家重装系统导致机器码变化)= 用新机器码重新 gen 即可,零成本。
建议卖家登记 card_id ↔ 买家机器码,便于处理"删库重放"类纠纷。
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    print('缺少 cryptography 库,请先执行: pip install cryptography')
    sys.exit(1)

KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seller_ed25519_private.pem')
CARD_VERSION = 1
MAX_DAYS = 3650
MAX_HOURS = 72


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_private_key() -> Ed25519PrivateKey:
    if not os.path.exists(KEY_FILE):
        print(f'未找到私钥文件 {KEY_FILE},请先运行: python 卡密生成器.py init')
        sys.exit(1)
    with open(KEY_FILE, 'rb') as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def cmd_init(_args):
    if os.path.exists(KEY_FILE):
        print(f'❌ 私钥文件已存在: {KEY_FILE}(重复 init 会覆盖,请先备份;确认覆盖请删除该文件后重试)')
        sys.exit(1)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(KEY_FILE, 'wb') as f:
        f.write(pem)
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    # 混淆形式:与后端 _decode_license_public_key 一致的算法
    key_bytes = hashlib.sha256(b'DouyinSpark-license-key-v1').digest()
    blob = bytes(ord(c) ^ key_bytes[i % len(key_bytes)] for i, c in enumerate(pub_b64))
    obf = base64.b64encode(blob).decode()
    print(f'✅ 密钥对已生成: {KEY_FILE}')
    print(f'⚠️ 请妥善离线保管私钥文件,泄露即失去卡密控制权!')
    print()
    print('请把下面这行粘贴到「抖音自动续火花-后端.py」的 _LICENSE_PUBLIC_KEY_OBFUSCATED 常量(混淆形式,用户看不到明文):')
    print()
    print(f'_LICENSE_PUBLIC_KEY_OBFUSCATED = \'{obf}\'')
    print()
    print(f'(明文公钥仅供备份参考,勿打印给买家: {pub_b64})')
    print()
    print('粘贴完成后重新打包程序即可生效。')


def _normalize_machine(machine: str) -> str:
    return (machine or '').strip().upper().replace(' ', '').replace('-', '')


def _group_token(token: str) -> str:
    """每 4 字符一组,用 - 分组,便于复制传播(程序侧自动去除分组符)"""
    return '-'.join(token[i:i + 4] for i in range(0, len(token), 4))


def build_card(private_key: Ed25519PrivateKey, machine: str, days: int, hours: int = 0, douyin_id: str = '') -> str:
    payload = json.dumps({
        'v': CARD_VERSION,
        'machine': machine,
        'days': days,
        'hours': hours,
        'douyin_id': douyin_id or '',
        'card_id': secrets.token_hex(8),
        'issued_at': _now_utc_iso(),
    }, sort_keys=True, separators=(',', ':')).encode('utf-8')
    sig = private_key.sign(payload)
    token = base64.b64encode(payload).decode() + '.' + base64.b64encode(sig).decode()
    return 'DY-' + _group_token(token)


def cmd_gen(args):
    machine = _normalize_machine(args.machine)
    if len(machine) != 32:
        print(f'⚠️ 机器码应为 32 位(当前 {len(machine)} 位): {machine}')
        print('   请核对买家提供的机器码(程序界面上的「一键复制」)')
        sys.exit(1)
    if not (0 <= args.days <= MAX_DAYS):
        print(f'❌ 天数需在 0~{MAX_DAYS} 之间')
        sys.exit(1)
    if not (0 <= args.hours <= MAX_HOURS):
        print(f'❌ 小时数需在 0~{MAX_HOURS} 之间')
        sys.exit(1)
    if args.days + args.hours <= 0:
        print('❌ 天数与小时数不能同时为 0')
        sys.exit(1)
    douyin_id = (args.douyin_id or '').strip()
    if douyin_id and len(douyin_id) > 64:
        print('❌ 抖音标识长度异常,请核对买家提供的内容')
        sys.exit(1)
    key = load_private_key()
    cards = [build_card(key, machine, args.days, args.hours, douyin_id) for _ in range(args.n)]
    desc = f'{args.days} 天' if args.days else f'{args.hours} 小时'
    if args.days and args.hours:
        desc = f'{args.days} 天 {args.hours} 小时'
    bind_note = f',绑定抖音标识 {douyin_id}' if douyin_id else '(未绑定抖音标识,旧格式)'
    print(f'✅ 已生成 {len(cards)} 张 {desc}卡密(机器码 {machine}{bind_note}):')
    print()
    for c in cards:
        print(c)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(cards) + '\n')
        print(f'\n已写入文件: {args.out}')


def cmd_verify(args):
    # base64 大小写敏感,不转大写
    card = (args.card or '').strip().replace(' ', '')
    if card.startswith('DY-'):
        card = card[3:]
    elif card.startswith('DY'):
        card = card[2:]
    card = card.replace('-', '')
    try:
        payload_b64, sig_b64 = card.split('.', 1)
        payload = base64.b64decode(payload_b64)
        sig = base64.b64decode(sig_b64)
    except Exception:
        print('❌ 卡密格式错误')
        sys.exit(1)
    key = load_private_key()
    try:
        key.public_key().verify(sig, payload)
    except Exception:
        print('❌ 签名校验失败(该卡密不是本私钥签发)')
        sys.exit(1)
    try:
        data = json.loads(payload.decode('utf-8'))
    except Exception:
        print('❌ 卡密数据损坏')
        sys.exit(1)
    print('✅ 验签通过,卡密内容:')
    for k in ('v', 'machine', 'days', 'hours', 'douyin_id', 'card_id', 'issued_at'):
        print(f'   {k}: {data.get(k)}')


def main():
    parser = argparse.ArgumentParser(description='抖音火花助手 卡密生成器(卖家工具)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init', help='生成 Ed25519 密钥对并打印公钥')
    p_gen = sub.add_parser('gen', help='生成卡密')
    p_gen.add_argument('-m', '--machine', required=True, help='买家机器码')
    p_gen.add_argument('-d', '--days', type=int, default=0, help='授权天数(默认 0)')
    p_gen.add_argument('-H', '--hours', type=int, default=0, help='授权小时数(默认 0,可与天数同用)')
    p_gen.add_argument('-u', '--douyin-id', default='', help='绑定买家抖音标识(程序设置页显示的抖音标识)')
    p_gen.add_argument('-n', type=int, default=1, help='生成张数(默认 1)')
    p_gen.add_argument('-o', '--out', default=None, help='输出到文件')
    p_verify = sub.add_parser('verify', help='校验卡密')
    p_verify.add_argument('-k', '--card', required=True, help='卡密内容')
    args = parser.parse_args()
    {'init': cmd_init, 'gen': cmd_gen, 'verify': cmd_verify}[args.cmd](args)


if __name__ == '__main__':
    main()
