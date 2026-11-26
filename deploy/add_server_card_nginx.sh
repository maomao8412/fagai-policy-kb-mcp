#!/bin/bash
# 给 kb.55160406.org 挂 MCP 服务卡片发现端点。
#
# 用 nginx 精确匹配直接返回静态 JSON，不动 guard.py——那个文件 26KB，
# 整体覆盖的风险远大于改一行 nginx。两个发现路径都挂：
#   /.well-known/mcp/server-card.json   （SEP-1649）
#   /.well-known/mcp.json               （SEP-1960）
# 两个提案都还没并入正式规范，客户端各实现各的，同一份内容挂两处成本为零。
#
# 幂等：重复跑不会重复插 location。任何一步不过自动回滚。
set -u

CARD_DIR=/opt/kb_service/static
CARD=$CARD_DIR/server-card.json
TS=$(date +%Y%m%d-%H%M%S)

if [ "$(id -u)" != "0" ]; then
  echo "✗ 需要 root。用 sudo bash 跑："
  echo "  curl -fsSL <脚本地址> -o /tmp/c.sh && sudo bash /tmp/c.sh"
  exit 1
fi

echo "────────────────────────────────────────"
echo " 1/5  定位 nginx 配置"
echo "────────────────────────────────────────"
CONF=$(grep -rlE "^[[:space:]]*server_name[^;]*kb\.55160406\.org" /etc/nginx/ 2>/dev/null \
       | grep -v '\.bak' | grep -v '\.orig' | head -1)
if [ -z "$CONF" ]; then
  echo "✗ 在 /etc/nginx/ 下没找到含 kb.55160406.org 的配置"
  echo "  手工确认：grep -rl kb.55160406.org /etc/nginx/"
  exit 1
fi
echo "✓ 配置文件：$CONF"

echo "────────────────────────────────────────"
echo " 2/5  写静态卡片"
echo "────────────────────────────────────────"
mkdir -p "$CARD_DIR"
cat > "$CARD" <<'CARD_JSON_EOF'
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/mcp-server-card/v1.json",
  "version": "1.1.0",
  "protocolVersion": "2025-06-18",
  "serverInfo": {
    "name": "fagai-policy-kb",
    "title": "中国发展改革政策知识库",
    "version": "1.1.0",
    "description": "中国发展改革政策知识库，收录国家与省级发改政策文件 2.1 万余篇、13.6 万条切片，支持关键词检索政策原文片段、按地域筛选、按文号或标题取全文。",
    "homepage": "https://kb.55160406.org/"
  },
  "transport": {
    "type": "streamable-http",
    "endpoint": "/public/mcp"
  },
  "capabilities": {
    "tools": {
      "listChanged": false
    },
    "resources": {
      "subscribe": false,
      "listChanged": false
    },
    "prompts": {
      "listChanged": false
    }
  },
  "authentication": {
    "required": false,
    "schemes": []
  },
  "instructions": "中国发展改革领域政策知识库，覆盖国家政策、省级政策、规划纲要、项目经验四类语料，共 21,656 篇正式文件、136,176 条切片。回答政策依据类问题前先用 search_policy 检索，需要确认文件身份或看正文开头时用 get_excerpt 取节选，需要逐条引用全文时再用授权端点的 get_article。所有结论都应标注文件名和文号。这是公开只读端点，提供检索、正文节选和统计。",
  "tools": [
    {
      "name": "search_policy",
      "description": "检索国家、省级、规划三类政策库，返回带出处的原文片段。用户问政策依据、申报条件、支持标准、某地有什么政策、「有没有文件规定」这类问题时先用这个工具拿原文再回答。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "检索问题，用自然语言描述即可"
          },
          "region": {
            "type": "string",
            "description": "限定地域，如「四川」，留空表示不限"
          },
          "top_k": {
            "type": "integer",
            "default": 5,
            "description": "返回条数，默认 5"
          }
        },
        "required": [
          "query"
        ]
      }
    },
    {
      "name": "get_excerpt",
      "description": "按文号或标题取政策正文节选（默认前 1200 字，上限 2000 字），返回标题、文号、发文机关、日期、来源链接和正文开头。search_policy 只给片段，找到候选文件后需要确认「是不是这一份」或看发文背景与第一章内容时用这个。完整全文需授权端点。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "doc_no": {
            "type": "string",
            "description": "文号，如「国令第712号」「川办发〔2026〕42号」，支持模糊匹配"
          },
          "title": {
            "type": "string",
            "description": "标题关键词，如「政府投资条例」。doc_no 为空时才用"
          },
          "max_chars": {
            "type": "integer",
            "default": 1200,
            "description": "正文返回字数，默认 1200，范围 300~2000"
          }
        }
      }
    },
    {
      "name": "list_recent",
      "description": "列出库中最新收录的政策文件，按发布日期倒序。用户问「最近有什么新政策」「本月出台了哪些文件」「XX 省最近发了什么」这类没有具体检索词的问题时用。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "province": {
            "type": "string",
            "description": "省份简称，如「四川」，留空表示全部"
          },
          "limit": {
            "type": "integer",
            "default": 20,
            "description": "返回条数，默认 20"
          }
        }
      }
    },
    {
      "name": "corpus_stats",
      "description": "查看知识库规模和覆盖范围。用户问「库里有多少文件」「覆盖哪些省」「有没有 XX 领域的政策」时用，先确认覆盖范围再决定要不要检索，避免答非所问。",
      "inputSchema": {
        "type": "object",
        "properties": {}
      }
    }
  ]
}

CARD_JSON_EOF
chmod 644 "$CARD"
# nginx worker 要能读到这个文件，父目录必须可 traverse。
# 只加 o+x 不加 o+r：别人能穿过去拿这一个文件，但列不出目录内容。
ORIG_APP_MODE=$(stat -c %a /opt/kb_service)
chmod o+x /opt/kb_service
chmod 755 "$CARD_DIR"
echo "✓ 已写入 $CARD（$(wc -c < "$CARD") 字节）"

echo "────────────────────────────────────────"
echo " 3/5  插入 location"
echo "────────────────────────────────────────"
cp -a "$CONF" "$CONF.bak-$TS"
echo "  已备份配置到 $CONF.bak-$TS"
INS=$(python3 - "$CONF" <<'PY_EOF'
import re, sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()

if "mcp-server-card" in src:
    print("SKIP")
    sys.exit(0)

lines = src.split("\n")
target = None
for i, l in enumerate(lines):
    if "server_name" in l and "kb.55160406.org" in l:
        target = i
        break
if target is None:
    print("NOSERVER"); sys.exit(1)

start = None
for i in range(target, -1, -1):
    if re.search(r"\bserver\s*\{", lines[i]):
        start = i
        break
if start is None:
    print("NOBLOCK"); sys.exit(1)

depth, end = 0, None
for i in range(start, len(lines)):
    if lines[i].lstrip().startswith("#"):
        continue
    depth += lines[i].count("{") - lines[i].count("}")
    if depth == 0 and i > start:
        end = i
        break
if end is None:
    print("NOEND"); sys.exit(1)

block = """
    # >>> mcp-server-card  MCP 服务卡片，纯公开元数据，不碰下游服务
    location = /.well-known/mcp/server-card.json {
        alias /opt/kb_service/static/server-card.json;
        default_type application/json;
        add_header Cache-Control "public, max-age=300";
    }
    location = /.well-known/mcp.json {
        alias /opt/kb_service/static/server-card.json;
        default_type application/json;
        add_header Cache-Control "public, max-age=300";
    }
    # <<< mcp-server-card"""

lines.insert(end, block)
open(path, "w", encoding="utf-8").write("\n".join(lines))
print(f"INSERTED {end + 1}")
PY_EOF
)

case "$INS" in
  SKIP*)     echo "✓ location 已存在，跳过" ;;
  INSERTED*) echo "✓ 已插入（server block 第 ${INS#INSERTED } 行前）" ;;
  *)
    echo "✗ 插入失败：$INS"
    echo "  回滚：chmod $ORIG_APP_MODE /opt/kb_service; rm -f $CARD"
    exit 1 ;;
esac

echo "────────────────────────────────────────"
echo " 4/5  校验并 reload"
echo "────────────────────────────────────────"
if nginx -t >/tmp/.nginx_t 2>&1; then
  tail -2 /tmp/.nginx_t
else
  echo "✗ nginx -t 没通过，回滚"
  tail -5 /tmp/.nginx_t
  cp -a "$CONF.bak-$TS" "$CONF"
  if nginx -t >/dev/null 2>&1; then nginx -s reload; echo "✓ 已回滚，nginx 正常"; fi
  exit 1
fi
nginx -s reload 2>/dev/null || systemctl reload nginx
sleep 2

echo "────────────────────────────────────────"
echo " 5/5  验证"
echo "────────────────────────────────────────"
FAIL=0
for P in /.well-known/mcp/server-card.json /.well-known/mcp.json; do
  CODE=$(curl -s -m 15 -o /tmp/.card -w "%{http_code}" \
         "https://kb.55160406.org$P?t=$(date +%s)")
  if [ "$CODE" = "200" ] && grep -q '"serverInfo"' /tmp/.card; then
    echo "✓ $P  → 200，$(wc -c < /tmp/.card) 字节"
  else
    echo "✗ $P  → $CODE"
    head -c 200 /tmp/.card; echo ""
    FAIL=1
  fi
done

echo "────────────────────────────────────────"
if [ "$FAIL" = "0" ]; then
  echo "✅ 全部通过"
  echo ""
  echo "  服务卡片：https://kb.55160406.org/.well-known/mcp/server-card.json"
  echo "  备用路径：https://kb.55160406.org/.well-known/mcp.json"
else
  echo "⚠️ 有检查项未通过"
  echo "  回滚配置：cp -a $CONF.bak-$TS $CONF && nginx -t && nginx -s reload"
  echo "  Cloudflare 可能缓存了 404，带 ?t=时间戳 再试"
fi
