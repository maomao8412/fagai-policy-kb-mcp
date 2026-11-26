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
# 两个源都试。raw.githubusercontent.com 的 CDN 缓存能扛十几分钟，
# 刚 push 完访问它会拿到旧文件，?t= 参数也绕不过去；jsdelivr 相对及时。
RAW_JD=https://cdn.jsdelivr.net/gh/maomao8412/fagai-policy-kb-mcp@main/deploy/mcp_server.py
RAW_GH=https://raw.githubusercontent.com/maomao8412/fagai-policy-kb-mcp/main/deploy/mcp_server.py
# 新版的版本标记：有 _warm_chunks 才算拿到对的文件，光看 HTTP 200 不够。
VERSION_MARK=_warm_chunks
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
GOT=0
for U in "$RAW_JD" "$RAW_GH"; do
  echo "  尝试：$U"
  if curl -fsSL -m 60 "$U" -o mcp_server.py.new 2>/dev/null; then
    SIZE=$(wc -c < mcp_server.py.new)
    if [ "$SIZE" -lt 10000 ]; then
      echo "    ✗ 内容太小（$SIZE 字节），可能是错误页"
    elif ! grep -q "$VERSION_MARK" mcp_server.py.new; then
      echo "    ✗ 拿到的是旧版（缺版本标记 $VERSION_MARK），换下一个源"
    else
      GOT=1
      echo "  ✓ 下载完成，$SIZE 字节，版本标记已确认"
      break
    fi
  else
    echo "    ✗ 连不上"
  fi
done

if [ "$GOT" != "1" ]; then
  echo "✗ 所有源都拿不到新版。可能刚 push 完 CDN 还没同步，等 5 分钟重跑。"
  rm -f mcp_server.py.new
  exit 1
fi

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
sleep 8

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

  # 5.4 实测调用一次，确认不是只注册了名字。
  # 线上 13.6 万条切片，服务刚重启时后台还在加载，此时调用会被阻塞。
  # 所以这里轮询：最多 12 轮、每轮 20 秒超时、间隔 5 秒，约 4 分钟上限。
  echo "  等待切片预热（首次加载 13.6 万条，可能要几十秒）..."
  EXC_OK=0
  EXC_CODE=""
  EXC_BODY=""
  for i in $(seq 1 12); do
    EXC=$(curl -sS -m 20 -w "\n__HTTP__%{http_code}" -X POST http://127.0.0.1:3010/public/mcp \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -H "Mcp-Session-Id: $SID" \
      -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_excerpt","arguments":{"doc_no":"国令第712号","max_chars":300}}}' 2>/dev/null)
    EXC_CODE=$(echo "$EXC" | grep -o '__HTTP__[0-9]*' | tail -1 | sed 's/__HTTP__//')
    EXC_BODY=$(echo "$EXC" | sed 's/__HTTP__[0-9]*$//')
    if echo "$EXC_BODY" | grep -q '"result"'; then EXC_OK=1; break; fi
    if echo "$EXC_BODY" | grep -q '"isError":true'; then break; fi
    echo "    第 $i 轮未就绪，5 秒后重试"
    sleep 5
  done

  if [ "$EXC_OK" = "1" ]; then
    echo "✓ get_excerpt 实测可取正文（政府投资条例）"
  elif echo "$EXC_BODY" | grep -q '"isError":true'; then
    echo "✗ get_excerpt 调用报错（HTTP $EXC_CODE）"
    echo "$EXC_BODY" | head -c 500
    echo ""
    echo "  排查：journalctl -u fagai-mcp -n 30 --no-pager"
    FAIL=1
  else
    echo "✗ get_excerpt 调用无返回（HTTP ${EXC_CODE:-超时}）"
    echo "$EXC_BODY" | head -c 500
    echo ""
    echo "  排查：journalctl -u fagai-mcp -n 30 --no-pager"
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
