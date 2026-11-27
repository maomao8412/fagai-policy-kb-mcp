#!/usr/bin/env python3
"""API key 签发工具。

用法（在 /opt/kb_service 下执行）：

  ./venv/bin/python make_api_key.py new <用户名> [备注]
      给已有用户签一把 key。明文只打印这一次，记下来。
      用户不存在就先在网站注册，或用 addtokens.py 建。

  ./venv/bin/python make_api_key.py list [用户名]
      列出 key（只显示前缀，不显示明文）。

  ./venv/bin/python make_api_key.py revoke <key_id>
      吊销。key_id 从 list 里看。

  ./venv/bin/python make_api_key.py who <key>
      反查这把 key 属于谁、余额多少、调了多少次。

  ./venv/bin/python make_api_key.py grant <用户名> <token数> [备注]
      给用户加额度（等价于手工充值，走 recharge_log）。

为什么单独一个脚本而不做网页：
  发 key 是低频操作，客户可能只有几家。做成网页要多一套权限校验，
  反而增加被绕过的面。命令行 + 服务器登录已经够安全。
"""
import sys

import billing


def _usage() -> None:
    print(__doc__.strip())


def main() -> int:
    args = sys.argv[1:]
    if not args:
        _usage()
        return 1

    cmd = args[0].lower()

    if cmd == "new":
        if len(args) < 2:
            print("缺用户名")
            return 1
        name = args[1]
        note = args[2] if len(args) > 2 else ""
        u = billing.get_user_by_name(name)
        if not u:
            print(f"用户 {name!r} 不存在。先让对方在网站注册，或先建号。")
            return 1
        info = billing.create_api_key(u["id"], note)
        if not info:
            print("签发失败")
            return 1
        print()
        print("  ┌─────────────────────────────────────────────")
        print(f"  │ 用户    {info['username']}  (id={u['id']})")
        print(f"  │ 备注    {info['name'] or '（无）'}")
        print(f"  │ key_id  {info['id']}")
        print("  ├─────────────────────────────────────────────")
        print(f"  │ {info['key']}")
        print("  └─────────────────────────────────────────────")
        print()
        print("  明文只显示这一次，库里只存哈希，丢了只能重新签发。")
        print()
        print("  用法（MCP 客户端里配）：")
        print(f'    Authorization: Bearer {info["key"]}')
        print("    端点  https://kb.55160406.org/public/mcp")
        print()
        return 0

    if cmd == "list":
        uid = None
        if len(args) > 1:
            u = billing.get_user_by_name(args[1])
            if not u:
                print(f"用户 {args[1]!r} 不存在")
                return 1
            uid = u["id"]
        rows = billing.list_api_keys(uid)
        if not rows:
            print("没有 key。")
            return 0
        print(f"{'id':>5}  {'用户':<14} {'前缀':<16} {'状态':<4} "
              f"{'调用':>6} {'单位':>8}  备注")
        print("-" * 78)
        for r in rows:
            st = "有效" if r["status"] == 1 else "已吊销"
            print(f"{r['id']:>5}  {r['username']:<14} {r['key_prefix']:<16} "
                  f"{st:<4} {r['total_calls']:>6} {r['total_units']:>8}  "
                  f"{r['name'] or ''}")
        return 0

    if cmd == "revoke":
        if len(args) < 2:
            print("缺 key_id")
            return 1
        try:
            kid = int(args[1])
        except ValueError:
            print("key_id 必须是数字")
            return 1
        ok = billing.revoke_api_key(kid)
        print("✓ 已吊销" if ok else "✗ 没找到这把 key，或已经吊销过了")
        return 0 if ok else 1

    if cmd == "who":
        if len(args) < 2:
            print("缺 key")
            return 1
        info = billing.verify_api_key(args[1])
        if not info:
            print("✗ 无效或已吊销")
            return 1
        print(f"用户    {info['username']} (uid={info['uid']})")
        print(f"备注    {info['name'] or '（无）'}")
        print(f"key_id  {info['key_id']}")
        print(f"余额    {info['balance']} token")
        return 0

    if cmd == "grant":
        if len(args) < 3:
            print("用法：grant <用户名> <token数> [备注]")
            return 1
        u = billing.get_user_by_name(args[1])
        if not u:
            print(f"用户 {args[1]!r} 不存在")
            return 1
        try:
            n = int(args[2])
        except ValueError:
            print("token 数必须是整数")
            return 1
        note = args[3] if len(args) > 3 else "手工加额度"
        after = billing.add_tokens(u["id"], n, note=note, operator="cli")
        if not after:
            print("加额度失败")
            return 1
        print(f"✓ {u['username']} 余额 {after['tokens_balance']} token")
        return 0

    _usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
