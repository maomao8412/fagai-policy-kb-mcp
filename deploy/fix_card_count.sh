#!/bin/bash
# 修正服务卡片里的语料规模数字：21,656 → 21,658。
#
# 背景：patch_server_card.sh 里写的 21,656 是建卡时的旧快照，线上实际
# 21,658 篇（差 2 篇，是之后补录的）。这个数字会通过
# /.well-known/mcp/server-card.json 对外展示，客户端和目录站会读，
# 对不上就属于对外元数据不准，所以修掉。
#
# 只替换字符串常量，不碰任何逻辑。改完做语法检查，失败自动回滚。
set -u

APP=/opt/kb_service
PY=$APP/app.py
OLD="21,656"
NEW="21,658"
TS=$(date +%Y%m%d-%H%M%S)

cd "$APP" || { echo "✗ 进不去 $APP"; exit 1; }
[ -f "$PY" ] || { echo "✗ 找不到 $PY"; exit 1; }

echo "──────────────── 1/4 检查 ────────────────"
CNT=$(grep -c "$OLD" "$PY" 2>/dev/null || true)
if [ "${CNT:-0}" = "0" ]; then
  if grep -q "$NEW" "$PY"; then
    echo "✓ 已经是 $NEW，无需修改"
    SKIP=1
  else
    echo "✗ app.py 里既没有 $OLD 也没有 $NEW，卡片路由可能没装上"
    echo "  先跑 patch_server_card.sh 再回来"
    exit 1
  fi
else
  echo "找到 $CNT 处 $OLD"
  SKIP=0
fi

if [ "$SKIP" = "1" ]; then
  echo "──────────────── 直接验证 ────────────────"
else
  echo "──────────────── 2/4 备份 ────────────────"
  cp -a "$PY" "$PY.bak-$TS" || { echo "✗ 备份失败"; exit 1; }
  echo "✓ 已备份到 $PY.bak-$TS"

  echo "──────────────── 3/4 替换并做语法检查 ────────────────"
  # 用 python 原地替换，避免 sed 处理 UTF-8 中文和逗号时的转义坑
  python3 - "$PY" "$OLD" "$NEW" <<'PYEOF' || { echo "✗ 替换失败"; cp -a "$PY.bak-$TS" "$PY"; exit 1; }
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path, encoding='utf-8').read()
n = s.count(old)
s = s.replace(old, new)
open(path, 'w', encoding='utf-8').write(s)
print(f"  替换了 {n} 处")
PYEOF

  PY_BIN=./venv/bin/python
  [ -x "$PY_BIN" ] || PY_BIN=python3
  if ! "$PY_BIN" -c "import ast;ast.parse(open('app.py',encoding='utf-8').read())" 2>/tmp/.syn; then
    echo "✗ 语法没过："; cat /tmp/.syn
    cp -a "$PY.bak-$TS" "$PY"
    echo "  已回滚，没有重启服务"
    exit 1
  fi
  echo "✓ 语法通过"

  echo "──────────────── 4/4 重启并验证 ────────────────"
  echo "  重启期间检索服务会中断几秒"
  systemctl restart kb || { echo "✗ 重启失败"; exit 1; }

  UP=0
  for i in $(seq 1 40); do
    if ! systemctl is-active --quiet kb; then
      echo "✗ kb 进程退出了（第 ${i}s）"
      journalctl -u kb -n 20 --no-pager
      cp -a "$PY.bak-$TS" "$PY" && systemctl restart kb
      echo "  已回滚"
      exit 1
    fi
    if curl -s -m 2 -o /dev/null "http://127.0.0.1:3004/" ; then
      UP=1; echo "✓ kb 服务已就绪（${i}s）"; break
    fi
    sleep 1
  done
  [ "$UP" = "1" ] || { echo "✗ 40 秒内没监听上 3004"; journalctl -u kb -n 20 --no-pager; exit 1; }
fi

echo ""
OK=0
FAIL=0
for P in "/.well-known/mcp/server-card.json" "/.well-known/mcp.json"; do
  for BASE in "http://127.0.0.1:3004" "https://kb.55160406.org"; do
    SEP="?"; [ "${BASE#http://}" != "$BASE" ] && SEP="?t=$(date +%s)&"
    URL="${BASE}${P}${SEP}"
    BODY=$(curl -s -m 8 "$URL" 2>/dev/null)
    CODE=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null)
    if [ "$CODE" = "200" ] && printf '%s' "$BODY" | grep -q "21,658"; then
      echo "✓ ${P} → 200，已含 21,658"
      OK=$((OK+1))
    elif [ "$CODE" = "200" ]; then
      echo "✗ ${P} → 200，但内容里没有 21,658（可能是缓存）"
      FAIL=$((FAIL+1))
    else
      echo "✗ ${P} → ${CODE}"
      FAIL=$((FAIL+1))
    fi
  done
done

echo "────────────────────────────────────────"
if [ "$FAIL" = "0" ]; then
  echo "✅ 全部通过"
  echo "服务卡片：https://kb.55160406.org/.well-known/mcp/server-card.json"
else
  echo "⚠️ 有 $FAIL 项没过"
  [ "$SKIP" = "0" ] && echo "回滚命令：cp -a $PY.bak-$TS $PY && systemctl restart kb"
  exit 1
fi
