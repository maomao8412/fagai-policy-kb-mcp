#!/bin/bash
# 给 kb.55160406.org 挂 MCP 服务卡片发现端点。
#
# 为什么不改 guard.py：guard.py 里的 app 是 AgentGuard（ASGI 中间件实例），
# 没有 .get 方法，往上加路由会直接 AttributeError 把主服务带崩。
# 它内部包装的 from app import app 才是真正的 FastAPI 实例，所以把路由
# 追加到 app.py 末尾——只追加、不改任何现有代码，风险最小。
#
# 两个路径都挂：
#   /.well-known/mcp/server-card.json   （SEP-1649）
#   /.well-known/mcp.json               （SEP-1960）
#
# 幂等：已加过就跳过。语法不过自动回滚。
set -u

APP=/opt/kb_service
PY=$APP/app.py
TS=$(date +%Y%m%d-%H%M%S)

if [ "$(id -u)" != "0" ]; then
  echo "✗ 需要 root：curl -fsSL <地址> -o /tmp/p.sh && sudo bash /tmp/p.sh"
  exit 1
fi
cd "$APP" || { echo "✗ 找不到 $APP"; exit 1; }

echo "────────────────────────────────────────"
echo " 1/5  检查是否已加过"
echo "────────────────────────────────────────"
if grep -q "_MCP_SERVER_CARD" "$PY"; then
  echo "✓ app.py 里已有卡片路由，跳过"
  ADDED=0
else
  ADDED=1
  echo "  未加过，继续"
fi

if [ "$ADDED" = "1" ]; then
  echo "────────────────────────────────────────"
  echo " 2/5  备份 app.py"
  echo "────────────────────────────────────────"
  cp -a "$PY" "$PY.bak-$TS" || { echo "✗ 备份失败"; exit 1; }
  echo "✓ 已备份到 $PY.bak-$TS（$(wc -l < "$PY") 行）"

  echo "────────────────────────────────────────"
  echo " 3/5  追加路由到 app.py 末尾"
  echo "────────────────────────────────────────"
  cat >> "$PY" <<'PATCH_EOF'


# ── MCP 服务卡片发现端点（deploy/patch_server_card.sh 追加，勿手改）──────
# 两个路径都挂：SEP-1649 定的是 /.well-known/mcp/server-card.json，
# SEP-1960 定的是 /.well-known/mcp.json。两个提案都还没并入正式规范，
# 客户端各实现各的，同一份内容挂两处成本为零。
# 放在 app.py 而不是 guard.py：guard.py 里的 app 是 ASGI 中间件实例，
# 没有 .get；真正的 FastAPI 实例是这里的 app。
_MCP_SERVER_CARD = {'$schema': 'https://static.modelcontextprotocol.io/schemas/mcp-server-card/v1.json',
 'version': '1.1.0',
 'protocolVersion': '2025-06-18',
 'serverInfo': {'name': 'fagai-policy-kb',
                'title': '中国发展改革政策知识库',
                'version': '1.1.0',
                'description': '中国发展改革政策知识库，收录国家与省级发改政策文件 2.1 万余篇、13.6 '
                               '万条切片，支持关键词检索政策原文片段、按地域筛选、按文号或标题取全文。',
                'homepage': 'https://kb.55160406.org/'},
 'transport': {'type': 'streamable-http', 'endpoint': '/public/mcp'},
 'capabilities': {'tools': {'listChanged': False},
                  'resources': {'subscribe': False, 'listChanged': False},
                  'prompts': {'listChanged': False}},
 'authentication': {'required': False, 'schemes': []},
 'instructions': '中国发展改革领域政策知识库，覆盖国家政策、省级政策、规划纲要、项目经验四类语料，共 21,658 篇正式文件、136,176 '
                 '条切片。回答政策依据类问题前先用 search_policy 检索，需要确认文件身份或看正文开头时用 get_excerpt '
                 '取节选，需要逐条引用全文时再用授权端点的 '
                 'get_article。所有结论都应标注文件名和文号。这是公开只读端点，提供检索、正文节选和统计。',
 'tools': [{'name': 'search_policy',
            'description': '检索国家、省级、规划三类政策库，返回带出处的原文片段。用户问政策依据、申报条件、支持标准、某地有什么政策、「有没有文件规定」这类问题时先用这个工具拿原文再回答。',
            'inputSchema': {'type': 'object',
                            'properties': {'query': {'type': 'string',
                                                     'description': '检索问题，用自然语言描述即可'},
                                           'region': {'type': 'string',
                                                      'description': '限定地域，如「四川」，留空表示不限'},
                                           'top_k': {'type': 'integer',
                                                     'default': 5,
                                                     'description': '返回条数，默认 5'}},
                            'required': ['query']}},
           {'name': 'get_excerpt',
            'description': '按文号或标题取政策正文节选（默认前 1200 字，上限 2000 '
                           '字），返回标题、文号、发文机关、日期、来源链接和正文开头。search_policy '
                           '只给片段，找到候选文件后需要确认「是不是这一份」或看发文背景与第一章内容时用这个。完整全文需授权端点。',
            'inputSchema': {'type': 'object',
                            'properties': {'doc_no': {'type': 'string',
                                                      'description': '文号，如「国令第712号」「川办发〔2026〕42号」，支持模糊匹配'},
                                           'title': {'type': 'string',
                                                     'description': '标题关键词，如「政府投资条例」。doc_no '
                                                                    '为空时才用'},
                                           'max_chars': {'type': 'integer',
                                                         'default': 1200,
                                                         'description': '正文返回字数，默认 '
                                                                        '1200，范围 '
                                                                        '300~2000'}}}},
           {'name': 'list_recent',
            'description': '列出库中最新收录的政策文件，按发布日期倒序。用户问「最近有什么新政策」「本月出台了哪些文件」「XX '
                           '省最近发了什么」这类没有具体检索词的问题时用。',
            'inputSchema': {'type': 'object',
                            'properties': {'province': {'type': 'string',
                                                        'description': '省份简称，如「四川」，留空表示全部'},
                                           'limit': {'type': 'integer',
                                                     'default': 20,
                                                     'description': '返回条数，默认 20'}}}},
           {'name': 'corpus_stats',
            'description': '查看知识库规模和覆盖范围。用户问「库里有多少文件」「覆盖哪些省」「有没有 XX '
                           '领域的政策」时用，先确认覆盖范围再决定要不要检索，避免答非所问。',
            'inputSchema': {'type': 'object', 'properties': {}}}]}


@app.get("/.well-known/mcp/server-card.json")
@app.get("/.well-known/mcp.json")
async def _mcp_server_card():
    """返回服务卡片。纯公开元数据，匿名可读，允许跨域。"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        _MCP_SERVER_CARD,
        headers={
            "Cache-Control": "public, max-age=300",
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
        },
    )
PATCH_EOF
  echo "✓ 已追加（现 $(wc -l < "$PY") 行）"

  echo "────────────────────────────────────────"
  echo " 4/5  语法检查"
  echo "────────────────────────────────────────"
  PY_BIN=./venv/bin/python
  [ -x "$PY_BIN" ] || PY_BIN=python3
  if ! "$PY_BIN" -c "import ast;ast.parse(open('app.py').read())" 2>/tmp/.syn; then
    echo "✗ 语法没过："; cat /tmp/.syn
    cp -a "$PY.bak-$TS" "$PY"
    echo "  已回滚，没有重启服务"
    exit 1
  fi
  echo "✓ 语法通过"
fi

echo "────────────────────────────────────────"
echo " 5/5  重启并验证"
echo "────────────────────────────────────────"
echo "  重启期间检索服务会中断几秒"
systemctl restart kb || { echo "✗ 重启失败"; exit 1; }

# 轮询等 3004 起来，最多 40 秒。别用固定 sleep，机器慢时容易误判失败。
UP=0
for i in $(seq 1 40); do
  if ! systemctl is-active --quiet kb; then
    echo "✗ kb 进程退出了（第 ${i}s）"
    journalctl -u kb -n 20 --no-pager
    [ "$ADDED" = "1" ] && cp -a "$PY.bak-$TS" "$PY" && systemctl restart kb
    echo "  已回滚"
    exit 1
  fi
  if curl -s -m 2 -o /dev/null "http://127.0.0.1:3004/" ; then
    UP=1; echo "✓ kb 服务已就绪（${i}s）"; break
  fi
  sleep 1
done
[ "$UP" = "1" ] || { echo "✗ 40 秒内没监听上 3004"; journalctl -u kb -n 20 --no-pager; exit 1; }

FAIL=0
echo "  -- 本地 --"
for P in /.well-known/mcp/server-card.json /.well-known/mcp.json; do
  CODE=$(curl -s -m 10 -o /tmp/.card -w "%{http_code}" "http://127.0.0.1:3004$P")
  if [ "$CODE" = "200" ] && grep -q '"serverInfo"' /tmp/.card; then
    echo "  ✓ $P  → 200（$(wc -c < /tmp/.card) 字节）"
  else
    echo "  ✗ $P  → $CODE"; head -c 200 /tmp/.card; echo ""; FAIL=1
  fi
done

echo "  -- 公网 --"
for P in /.well-known/mcp/server-card.json /.well-known/mcp.json; do
  CODE=$(curl -s -m 15 -o /tmp/.card2 -w "%{http_code}" "https://kb.55160406.org$P?t=$(date +%s)")
  if [ "$CODE" = "200" ] && grep -q '"serverInfo"' /tmp/.card2; then
    echo "  ✓ $P  → 200（$(wc -c < /tmp/.card2) 字节）"
  else
    echo "  ✗ $P  → $CODE"; head -c 200 /tmp/.card2; echo ""; FAIL=1
  fi
done

echo "────────────────────────────────────────"
if [ "$FAIL" = "0" ]; then
  echo "✅ 全部通过"
  echo ""
  echo "  服务卡片：https://kb.55160406.org/.well-known/mcp/server-card.json"
else
  echo "⚠️ 有检查项未通过"
  [ "$ADDED" = "1" ] && echo "  回滚：cp -a $PY.bak-$TS $PY && systemctl restart kb"
  echo "  Cloudflare 可能缓存 404，带 ?t=时间戳 再试一次"
fi
