#!/usr/bin/env bash
# 给发改政策知识库 MCP 端点接入 API key 计量计费。
#
# 改什么：
#   billing.py    + api_keys 表、签发/校验/吊销函数
#   mcp_server.py + contextvar 计费、Guard 认 key、启动时建表
#
# 行为变化（对现有用户无影响）：
#   /public/mcp 不带 Authorization  → 和现在完全一样，30 rpm/IP 免费
#   /public/mcp 带 fagai_xxx        → 跳过 IP 限流，按余额扣 token
#   余额 ≤ 0                        → 402，提示充值
#   key 无效                        → 401
#
# 用法（在服务器上，root）：
#   curl -fsSL https://raw.githubusercontent.com/maomao8412/fagai-policy-kb-mcp/main/deploy/enable_mcp_billing.sh -o /tmp/b.sh && sudo bash /tmp/b.sh
#
# 回滚：脚本失败会自动回滚。手工回滚：
#   cp /opt/kb_service/billing.py.bak-<时间> /opt/kb_service/billing.py
#   cp /opt/kb_service/mcp_server.py.bak-<时间> /opt/kb_service/mcp_server.py
#   systemctl restart fagai-mcp

set -uo pipefail

REPO="maomao8412/fagai-policy-kb-mcp"
BRANCH="${BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}/deploy/payload"
TS=$(date +%Y%m%d-%H%M%S)
EP_LOCAL="http://127.0.0.1:3010"
CORE="billing.py mcp_server.py"          # 要备份的
ALL="billing.py mcp_server.py make_api_key.py"  # 要下发的

say() { echo ""; echo "──────────────── $* ────────────────"; }
ok()  { echo "✓ $*"; }
bad() { echo "✗ $*"; }

# ── 0. 前置 ────────────────────────────────────────────────
say "0/6 前置检查"
[ "$(id -u)" = "0" ] || { bad "需要 root，用 sudo 跑"; exit 1; }

UNIT=$(systemctl show -p FragmentPath fagai-mcp --value 2>/dev/null)
if [ -z "$UNIT" ] || [ ! -f "$UNIT" ]; then
  bad "找不到 fagai-mcp.service，先确认 MCP 服务装好了"
  exit 1
fi
ok "unit  $UNIT"

WD=$(systemctl show -p WorkingDirectory fagai-mcp --value)
if [ -z "$WD" ] || [ ! -d "$WD" ]; then
  bad "读不到 WorkingDirectory（$WD）"
  exit 1
fi
ok "代码目录  $WD"

PYBIN=$(systemctl show -p ExecStart fagai-mcp --value \
        | grep -oP 'path=\K[^ ;]+' | head -1)
[ -n "$PYBIN" ] && [ -x "$PYBIN" ] || PYBIN=$(command -v python3)
ok "解释器  $PYBIN"

for f in $CORE; do
  [ -f "$WD/$f" ] || { bad "$WD/$f 不存在"; exit 1; }
done
ok "核心文件都在"

# 目录校验：万一 unit 里的 WorkingDirectory 指向别处（装过两遍、
# 或从 /root 迁到 /opt 没改 unit），覆盖会改到一份没人用的旧代码上，
# 用户以为生效了其实线上没变。用两个特征串确认这是真代码目录。
grep -q "KB_MCP_PORT" "$WD/mcp_server.py" 2>/dev/null \
  || { bad "$WD/mcp_server.py 里没有 KB_MCP_PORT，不像 MCP 服务代码"; exit 1; }
grep -q "def charge" "$WD/billing.py" 2>/dev/null \
  || { bad "$WD/billing.py 里没有 charge()，不像账务模块"; exit 1; }
if [ ! -f "$WD/billing.db" ]; then
  bad "$WD 下没有 billing.db —— 线上用户数据不在这，八成找错目录了"
  exit 1
fi
ok "目录校验通过（billing.db 存在，说明是线上在用的那份）"

# ── 1. 备份 ────────────────────────────────────────────────
say "1/6 备份"
for f in $CORE; do
  cp -a "$WD/$f" "$WD/$f.bak-$TS"
  ok "$f → $f.bak-$TS"
done

rollback() {
  bad "回滚中……"
  for f in $CORE; do
    [ -f "$WD/$f.bak-$TS" ] && cp -a "$WD/$f.bak-$TS" "$WD/$f"
  done
  systemctl restart fagai-mcp
  sleep 3
  bad "已回滚到改动前的版本，服务按旧代码运行"
  exit 1
}

# ── 2. 下载 ────────────────────────────────────────────────
say "2/6 下载新版本"
for f in $ALL; do
  # 加 ?t= 绕 CDN 缓存：raw.githubusercontent.com 对分支名有缓存，
  # 刚推上去的提交可能几分钟内还拉到旧内容，那样等于没改。
  if ! curl -fsSL --max-time 60 "$RAW/$f?t=$(date +%s)" -o "/tmp/new-$f"; then
    bad "下载 $f 失败（$RAW/$f）"
    rollback
  fi
  sz=$(stat -c%s "/tmp/new-$f")
  if [ "$sz" -lt 5000 ]; then
    bad "$f 只有 $sz 字节，不像完整文件"
    rollback
  fi
  ok "$f  $sz 字节"
done

# 再确认一遍拿到的是新版而不是缓存里的旧版
grep -q "def create_api_key" /tmp/new-billing.py \
  || { bad "下载到的 billing.py 没有 create_api_key，是旧版或缓存"; rollback; }
grep -q "BILL_UNITS" /tmp/new-mcp_server.py \
  || { bad "下载到的 mcp_server.py 没有 BILL_UNITS，是旧版或缓存"; rollback; }
ok "内容校验通过（确认是新版）"

# ── 3. 覆盖 + 语法检查 ─────────────────────────────────────
say "3/6 覆盖并做语法检查"
for f in $ALL; do
  if cmp -s "/tmp/new-$f" "$WD/$f" 2>/dev/null; then
    ok "$f 内容一致，跳过覆盖"
    continue
  fi
  cp "/tmp/new-$f" "$WD/$f"
  if ! "$PYBIN" -c "import ast,sys;ast.parse(open('$WD/$f',encoding='utf-8').read())" 2>/tmp/.syn; then
    bad "$f 语法错误："
    cat /tmp/.syn
    rollback
  fi
  ok "$f 已更新，语法通过"
done

# 建表：mcp_server 启动时也会 init_db，这里先跑一遍让验证阶段
# 就能用 verify_api_key，不用等第一次请求。
cd "$WD" || rollback
if "$PYBIN" -c "import billing;billing.init_db();print('api_keys 就绪')" 2>/tmp/.db; then
  ok "$(cat /tmp/.db)"
else
  bad "建表失败："
  cat /tmp/.db
  rollback
fi

# ── 4. 重启 ────────────────────────────────────────────────
say "4/6 重启 fagai-mcp"
systemctl restart fagai-mcp
ok "已下发重启，轮询探活……"

UP=0
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$EP_LOCAL/health" 2>/dev/null)
  if [ "$code" = "200" ]; then UP=1; echo "  ✓ /health 200（${i}s）"; break; fi
  sleep 1
done
if [ "$UP" != "1" ]; then
  bad "服务没起来，最后 20 行日志："
  journalctl -u fagai-mcp -n 20 --no-pager 2>/dev/null | tail -20
  rollback
fi

# 偏移索引预热要十几秒，等它打完日志再验工具
for i in $(seq 1 30); do
  if grep -q "偏移索引就绪" /var/log/fagai-mcp.log 2>/dev/null; then break; fi
  sleep 1
done
ok "启动完成"

# ── 5. 验证 ────────────────────────────────────────────────
say "5/6 验证"
FAIL=0

INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'

# a) 匿名 initialize 必须 200
c=$(curl -s -o /tmp/.anon -m 10 -w '%{http_code}' -X POST "$EP_LOCAL/public/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$INIT_BODY" 2>/dev/null)
if [ "$c" = "200" ]; then ok "匿名 initialize → 200（免费档未受影响）"
else bad "匿名 initialize → $c"; head -c 300 /tmp/.anon; FAIL=1; fi

# b) 假 key 必须 401
c=$(curl -s -o /tmp/.bad -m 10 -w '%{http_code}' -X POST "$EP_LOCAL/public/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Authorization: Bearer fagai_0000000000000000000000000000000000000000' \
    -d "$INIT_BODY" 2>/dev/null)
if [ "$c" = "401" ]; then ok "假 key → 401（鉴权生效）"
else bad "假 key → $c（应为 401）"; head -c 300 /tmp/.bad; FAIL=1; fi

# c) 授权端点 /mcp 不带 token 必须 401（原有鉴权没被破坏）
c=$(curl -s -o /dev/null -m 10 -w '%{http_code}' -X POST "$EP_LOCAL/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$INIT_BODY" 2>/dev/null)
if [ "$c" = "401" ]; then ok "/mcp 无 token → 401（原有鉴权完好）"
else bad "/mcp 无 token → $c（应为 401）"; FAIL=1; fi

# d) 工具清单还是 4 个
if "$PYBIN" - "$WD" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import mcp_server as M
names = sorted(M.BILL_UNITS)
want = ["corpus_stats", "get_article", "get_excerpt", "list_recent", "search_policy"]
assert names == want, f"工具清单不符：{names}"
print("  工具：" + ", ".join(names))
print("  单价：" + ", ".join(f"{k}={v}" for k, v in M.BILL_UNITS.items()))
PY
then ok "工具与单价表正确"
else bad "工具清单异常"; FAIL=1; fi

if [ "$FAIL" != "0" ]; then
  bad "验证未全过"
  rollback
fi

# ── 6. 收尾 ────────────────────────────────────────────────
say "6/6 完成"
echo "  备份保留在 $WD/*.bak-$TS"
echo ""
echo "  下一步：给某个用户签 key"
echo "    cd $WD && $PYBIN make_api_key.py new <用户名> <备注>"
echo ""
echo "  客户端配置："
echo "    endpoint  https://kb.55160406.org/public/mcp"
echo "    header    Authorization: Bearer fagai_xxx"
echo ""
echo "────────────────────────────────────────"
echo "✅ 全部通过"
