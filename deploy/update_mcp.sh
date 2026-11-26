#!/bin/bash
# 更新 kb.55160406.org 的 MCP 服务代码（只替换 mcp_server.py，不动 app.py / guard.py）。
#
# 用法（在服务器上以 root 执行）：
#   curl -fsSL https://raw.githubusercontent.com/maomao8412/fagai-policy-kb-mcp/main/deploy/update_mcp.sh | bash
#
# 做三件事：
#   1. 备份现有 mcp_server.py
#   2. 拉新版、语法检查、重启 fagai-mcp
#   3. 验证：instructions 里的语料规模不再是 1200+，公开端点有 4 个工具
# 任何一步不过就自动回滚，不留半截状态。
set -u

APP=/opt/kb_service
RAW=https://raw.githubusercontent.com/maomao8412/fagai-policy-kb-mcp/main/deploy/mcp_server.py
TS=$(date +%Y%m%d-%H%M%S)
BAK="$APP/mcp_server.py.bak-$TS"

if [ "$(id -u)" != "0" ]; then
  echo "✗ 需要 root 权限。当前用户：$(whoami)"
  echo ""
  echo "  正确跑法（先下载再提权，别用管道，否则 sudo 读不到密码）："
  echo "  curl -fsSL https://raw.githubusercontent.com/maomao8412/fagai-policy-kb-mcp/main/deploy/update_mcp.sh -o /tmp/u.sh && sudo bash /tmp/u.sh"
  exit 1
fi

cd "$APP" || { echo "✗ 找不到 $APP"; exit 1; }

echo "────────────────────────────────────────"
echo " 1/5  备份现有代码"
echo "────────────────────────────────────────"
cp -a mcp_server.py "$BAK" || { echo "✗ 备份失败，中止"; exit 1; }
echo "✓ 已备份到 $BAK"

echo "────────────────────────────────────────"
echo " 2/5  拉取新版"
echo "────────────────────────────────────────"
if ! curl -fsSL -m 60 "$RAW" -o mcp_server.py.new; then
  echo "✗ 下载失败，检查服务器能否访问 raw.githubusercontent.com"
  rm -f mcp_server.py.new
  exit 1
fi
SIZE=$(wc -c < mcp_server.py.new)
if [ "$SIZE" -lt 10000 ]; then
  echo "✗ 下载内容异常（只有 $SIZE 字节），中止"
  rm -f mcp_server.py.new
  exit 1
fi
echo "✓ 下载完成，$SIZE 字节"

echo "────────────────────────────────────────"
echo " 3/5  语法检查"
echo "────────────────────────────────────────"
if ! python3 -m py_compile mcp_server.py.new 2>/tmp/compile_err; then
  echo "✗ 语法检查不通过："
  cat /tmp/compile_err
  rm -f mcp_server.py.new
  exit 1
fi
echo "✓ 语法通过"

mv mcp_server.py.new mcp_server.py
chmod 644 mcp_server.py

echo "────────────────────────────────────────"
echo " 4/5  重启服务"
echo "────────────────────────────────────────"
systemctl restart fagai-mcp || { echo "✗ 重启失败，回滚"; cp -a "$BAK" mcp_server.py; systemctl restart fagai-mcp; exit 1; }
sleep 4

if ! systemctl is-active --quiet fagai-mcp; then
  echo "✗ 服务未起来，回滚"
  journalctl -u fagai-mcp -n 20 --no-pager
  cp -a "$BAK" mcp_server.py
  systemctl restart fagai-mcp
  exit 1
fi
echo "✓ 服务已启动"

echo "────────────────────────────────────────"
echo " 5/5  验证"
echo "────────────────────────────────────────"
FAIL=0

# 5.1 本地 health
if curl -fsS -m 10 http://127.0.0.1:3010/mcp-health >/dev/null 2>&1; then
  echo "✓ 本地 health 200"
else
  echo "✗ 本地 health 不通"
  FAIL=1
fi

# 5.2 instructions 里不能还是 1200+，且要出现真实量级
# 注意：MCP Streamable HTTP 是有状态的，tools/list 必须带上 initialize
# 返回的 Mcp-Session-Id，否则服务端直接 400 "Missing session ID"。
curl -fsS -m 15 -D /tmp/mcp_init_h.txt -o /tmp/mcp_init_b.txt \
  -X POST http://127.0.0.1:3010/public/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"1.0"}}}' 2>/dev/null

INIT=$(cat /tmp/mcp_init_b.txt 2>/dev/null)
SID=$(grep -i '^mcp-session-id:' /tmp/mcp_init_h.txt 2>/dev/null | tr -d '\r' | awk '{print $2}' | head -1)

if echo "$INIT" | grep -q "1200+"; then
  echo "✗ instructions 仍是旧的「1200+ 篇」"
  FAIL=1
elif echo "$INIT" | grep -qE "共 [0-9,]+ 篇正式文件"; then
  SCALE=$(echo "$INIT" | grep -oE "共 [0-9,]+ 篇正式文件、[0-9,]+ 条切片" | head -1)
  echo "✓ instructions 语料规模：$SCALE"
else
  echo "✗ instructions 里没读到语料规模"
  echo "$INIT" | head -c 400
  FAIL=1
fi

if [ -z "$SID" ]; then
  echo "✗ initialize 没返回 Mcp-Session-Id，握手异常"
  FAIL=1
fi

# 5.3 公开端点工具数必须是 4（带 session 才拿得到）
if [ -n "$SID" ]; then
  TOOLS=$(curl -fsS -m 15 -X POST http://127.0.0.1:3010/public/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SID" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' 2>/dev/null)

  if echo "$TOOLS" | grep -q '"get_excerpt"'; then
    echo "✓ 公开端点已注册 get_excerpt"
  else
    echo "✗ 公开端点没有 get_excerpt"
    echo "$TOOLS" | head -c 400
    FAIL=1
  fi

  # 5.4 实测调用一次，确认不是只注册了名字
  EXC=$(curl -fsS -m 25 -X POST http://127.0.0.1:3010/public/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SID" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_excerpt","arguments":{"doc_no":"国令第712号","max_chars":300}}}' 2>/dev/null)

  if echo "$EXC" | grep -q '"isError":true'; then
    echo "✗ get_excerpt 调用报错"
    echo "$EXC" | head -c 400
    FAIL=1
  elif echo "$EXC" | grep -q '"result"'; then
    echo "✓ get_excerpt 实测可取正文（政府投资条例）"
  else
    echo "✗ get_excerpt 调用无返回"
    echo "$EXC" | head -c 400
    FAIL=1
  fi
fi

echo "────────────────────────────────────────"
if [ "$FAIL" = "0" ]; then
  echo "✅ 全部通过"
  echo ""
  echo "下一步：外部验证"
  echo "  curl -s https://kb.55160406.org/.well-known/mcp/server-card.json | head -c 200"
  echo "  Glama 会在 24 小时内重新探测并更新 TDQS 评分"
  echo ""
  echo "回滚命令（如果之后发现问题）："
  echo "  cp -a $BAK $APP/mcp_server.py && systemctl restart fagai-mcp"
else
  echo "⚠️ 有检查项未通过，回滚到更新前"
  cp -a "$BAK" mcp_server.py
  systemctl restart fagai-mcp
  sleep 3
  systemctl is-active --quiet fagai-mcp && echo "✓ 已回滚，服务正常" || echo "✗ 回滚后服务仍异常，需要人工介入"
  exit 1
fi
