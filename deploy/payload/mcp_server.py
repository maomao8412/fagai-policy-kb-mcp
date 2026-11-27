"""发改政策知识库 MCP 服务

把现有的四库检索能力（国家 / 省 / 规划 / 经验）按 MCP 协议对外暴露。

设计原则：
  - 不重写检索逻辑。直接复用 app.py 里的 search_bailian_all，
    地域加权、精确条文命中、规划意图加权全部保持一致，
    避免两套代码漂移出不同答案。
  - tool 粒度要细。不暴露一个 ask(question) 的大黑盒——那种 tool
    模型不知道什么时候该调、也不知道调完拿到什么。拆成
    「检索 / 取原文 / 列最新 / 看规模」四个动作，描述写清适用场景。
  - 双传输入口。/mcp 走 streamable-http（百炼 Managed Agent、扣子、
    新版客户端），/sse 走 SSE（百炼 Responses API 只认这个协议）。
  - 公开只读端点。/public/mcp 不鉴权、只给检索和统计、带 IP 限流。
    官方 MCP Registry 要求 initialize 和 tools/list 能被匿名探测，
    否则目录里只有一条空记录。全文接口不放在公开端点上——那是
    最有价值的部分，不该让爬虫白拿。

启动：
  set -a && . ./.env && set +a
  python3 mcp_server.py                    # 默认 0.0.0.0:3010
  KB_MCP_PORT=3010 KB_MCP_TOKEN=xxx python3 mcp_server.py
"""
import contextvars
import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Dict, List

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

HOST = os.environ.get("KB_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("KB_MCP_PORT", "3010"))
# 空字符串 = 不鉴权（仅限内网自用）
TOKEN = os.environ.get("KB_MCP_TOKEN", "")

MAX_ARTICLE_CHARS = 20000
# 公开端点限流：每 IP 每分钟次数。目录爬虫只会调 initialize/tools/list，
# 真正吃检索的是被收录之后的调用，给到 30 次足够正常试用。
PUBLIC_RATE_LIMIT = int(os.environ.get("KB_MCP_PUBLIC_RPM", "30"))

# ── 计量计费：公开端点带 API key 时按次扣 token ──────────────
# 免费档不认人，按 IP 限流；带 key 的走付费通道，不限 IP、按余额扣。
# 扣费发生在工具函数内部而不是中间件——中间件要读 body 才能知道调了
# 哪个工具，而 MCP 是流式的，读 body 会把请求体吃掉。用 contextvar
# 把「这次请求是谁」从中间件传到工具函数，路径最短也最不容易出错。
#
# 定价逻辑：一次问答实测约 1.7 万 token（prompt 占 96%）。这里按
# 「一次工具调用 ≈ 一次轻量问答的零头」定价，1 万 token 的套餐能跑
# 100 次检索或 33 次取正文，客户感知是「几块钱能查一个月」。
BILL_UNITS = {
    "search_policy": 100,   # 检索：向量召回 + 重排
    "get_excerpt": 300,     # 取正文节选：要读偏移索引 + 读盘
    "list_recent": 100,     # 列最新：扫 meta
    "get_article": 500,     # 取全文：只有授权端点有
    "corpus_stats": 0,      # 统计：免费，用来探活
}

_BILL_CTX: contextvars.ContextVar = contextvars.ContextVar("kb_bill", default=None)


def _bill(tool: str, query: str = "") -> None:
    """扣一次费用。没有 key（免费档）就什么都不做。

    扣费失败只打日志不抛异常——用户已经拿到结果了，这时候报错只会
    让人以为工具坏了。余额变负由下一次请求的前置检查拦住。
    """
    ctx = _BILL_CTX.get()
    if not ctx:
        return
    units = BILL_UNITS.get(tool, 0)
    if units <= 0:
        return
    try:
        import billing
        billing.charge(ctx["uid"], {"total_tokens": units},
                       conversation_id=f"mcp-key-{ctx['key_id']}",
                       query=(query or "")[:120])
        billing.touch_api_key(ctx["key_id"], units)
    except Exception as e:
        print(f"[mcp][bill] {tool} 扣费失败：{e}", flush=True)


_KEY_PREFIX = "fagai_"


def _verify_key(key: str):
    """校验 API key。billing 模块异常时按无效处理（失败关闭）。

    不能失败开放：数据库锁住就放行，等于任何人拿假 key 都能白用。
    代价是 billing 真挂时付费用户也被挡住，但那时免费档还能用。
    """
    try:
        import billing
        return billing.verify_api_key(key)
    except Exception as e:
        print(f"[mcp][bill] 校验 key 失败：{e}", flush=True)
        return None


def _corpus_scale() -> str:
    """语料规模从 meta.json 现读，不写死。

    写死过一次：代码里留了「1200+ 篇」，而线上实际是 2.1 万篇，
    差 18 倍，外部 agent 每次连上来先读到这个错的量级。
    """
    try:
        from config import META_PATH
        with open(META_PATH, encoding="utf-8") as f:
            m = json.load(f)
        d, c = int(m.get("docs") or 0), int(m.get("chunks") or 0)
        if d:
            return f"共 {d:,} 篇正式文件、{c:,} 条切片。"
    except Exception:
        pass
    return "覆盖国家政策、省级政策、规划纲要、项目经验四类语料。"


INSTRUCTIONS = (
    "中国发展改革领域政策知识库，"
    + _corpus_scale()
    + "回答政策依据类问题前先用 search_policy 检索，需要逐条引用原文时"
    "再用 get_article 取全文。所有结论都应标注文件名和文号。"
)

mcp = FastMCP(
    "fagai-policy-kb",
    instructions=INSTRUCTIONS,
    streamable_http_path="/mcp",
    sse_path="/sse",
    message_path="/messages",
    host=HOST,
    port=PORT,
)

# 公开只读端点。目录站点和匿名客户端从这里发现工具。
public_mcp = FastMCP(
    "fagai-policy-kb-public",
    instructions=(
        INSTRUCTIONS
        + " 这是公开只读端点，提供检索、正文节选和统计；"
        "完整全文需使用授权端点 /mcp 的 get_article。"
    ),
    streamable_http_path="/public/mcp",
    sse_path="/public/sse",
    message_path="/public/messages",
    host=HOST,
    port=PORT,
)


# ── 懒加载：MCP 握手要快，别让 import app 拖慢启动 ────────────
@functools.lru_cache(maxsize=1)
def _kb():
    """复用 app.py 的检索实现，不复制逻辑。"""
    import app as kb_module
    return kb_module


# ── 正文定位：按 doc_id 建字节偏移索引，不把切片读进内存 ──────
# 之前这里用 lru_cache 把 13.6 万条切片全量读进来，占 200MB 以上，线上进程
# 被内核 OOM Killer 反复杀掉（systemd 日志 status=9/KILL）。现在只索引
# 「文号 / 标题 / 字节区间」，正文按偏移 seek 现读，常驻内存 15MB 左右。
SPAN_INDEX_NAME = "doc_span.json"
SCAN_MAX_HITS = 3000          # 兜底扫描时最多接受多少条候选行

_RE_DOC_ID = re.compile(rb'"doc_id"\s*:\s*"((?:[^"\\]|\\.)*)"')
_RE_DOC_NO = re.compile(rb'"doc_no"\s*:\s*"((?:[^"\\]|\\.)*)"')
_RE_TITLE = re.compile(rb'"title"\s*:\s*"((?:[^"\\]|\\.)*)"')

_span_lock = threading.Lock()


def _json_str(raw: bytes) -> str:
    """反转义 JSON 字符串体。包一层引号交给 json 处理最省事。"""
    try:
        return json.loads(b'"' + raw + b'"')
    except Exception:
        return raw.decode("utf-8", "replace")


def _grab(pattern, raw: bytes) -> str:
    m = pattern.search(raw)
    return _json_str(m.group(1)) if m else ""


def _span_index_path() -> str:
    from config import CHUNKS_PATH
    return os.path.join(os.path.dirname(str(CHUNKS_PATH)), SPAN_INDEX_NAME)


def _build_span_index() -> Dict[str, Dict]:
    """扫一遍 chunks.jsonl，建 doc_id → {文号, 标题, 字节区间}。

    切片按 doc_id 连续排列，相邻切片能拼成一个区间就拼起来，省内存。
    全程只跑正则不做完整 JSON 解析，几十万行约十几秒。
    """
    from config import CHUNKS_PATH
    idx: Dict[str, Dict] = {}
    offset = 0
    with open(CHUNKS_PATH, "rb") as f:
        for raw in f:
            size = len(raw)
            did = _grab(_RE_DOC_ID, raw)
            if did:
                rec = idx.get(did)
                if rec is None:
                    idx[did] = {"n": _grab(_RE_DOC_NO, raw),
                                "t": _grab(_RE_TITLE, raw),
                                "s": [[offset, size]]}
                else:
                    last = rec["s"][-1]
                    if last[0] + last[1] == offset:
                        last[1] += size
                    else:
                        rec["s"].append([offset, size])
            offset += size
    return idx


@functools.lru_cache(maxsize=1)
def _span_index() -> Dict[str, Dict]:
    """加载偏移索引，没有就现建并落盘。索引约 3MB，常驻内存 15MB 左右。"""
    path = _span_index_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    with _span_lock:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        idx = _build_span_index()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            pass
        return idx


def _read_spans(spans) -> List[Dict]:
    """按字节区间把切片读出来。一份文件通常几 KB 到几十 KB。"""
    from config import CHUNKS_PATH
    out: List[Dict] = []
    with open(CHUNKS_PATH, "rb") as f:
        for start, size in spans:
            f.seek(start)
            for line in f.read(size).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _scan_chunks(key: str = "", tkey: str = "") -> List[Dict]:
    """兜底：索引建不起来时用 grep 扫候选行再逐行校验。

    比索引慢一个数量级，只在 _span_index() 抛异常时才走到这里。
    必须加 -a：切片文件里有少量非 UTF-8 字节，grep 会判成二进制文件、
    只回一句 binary file matches 而不输出行，结果就是永远查不到东西。
    """
    from config import CHUNKS_PATH
    key = (key or "").strip()
    tkey = (tkey or "").strip()
    needle = key or tkey
    if not needle:
        return []
    probe = "".join(ch for ch in needle if ch not in '"\\')[:12] or needle[:8]

    hits: List[str] = []
    grep_ok = False
    try:
        p = subprocess.Popen(
            ["grep", "-a", "-n", "-F", "-m", str(SCAN_MAX_HITS), "-e", probe,
             str(CHUNKS_PATH)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="replace",
        )
        for raw in p.stdout:
            hits.append(raw.partition(":")[2])
        grep_ok = p.wait(timeout=120) in (0, 1)
    except Exception:
        grep_ok = False

    if not grep_ok:
        hits = []
        with open(CHUNKS_PATH, encoding="utf-8") as f:
            for raw in f:
                if probe in raw:
                    hits.append(raw)
                    if len(hits) >= SCAN_MAX_HITS:
                        break

    out: List[Dict] = []
    for raw in hits:
        raw = raw.strip()
        if not raw:
            continue
        try:
            c = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if key:
            if key in (c.get("doc_no") or ""):
                out.append(c)
        elif tkey and tkey in (c.get("title") or ""):
            out.append(c)
    return out


@functools.lru_cache(maxsize=1)
def _meta() -> Dict:
    from config import META_PATH
    with open(META_PATH, encoding="utf-8") as f:
        return json.load(f)


def _fmt_hits(records: List[Dict]) -> str:
    if not records:
        return "未检索到相关政策。可以换更宽泛的说法，或去掉地域限定再试一次。"
    blocks = []
    for i, r in enumerate(records, 1):
        md = r.get("metadata") or {}
        level = md.get("level", "")
        head = "｜".join(
            x for x in [
                level,
                md.get("province", "") if "省" in level else "",
                md.get("issuer", ""),
                md.get("doc_no", ""),
                md.get("pub_date", ""),
            ] if x
        )
        # title 形如「文号｜文件名｜章节」，文号已在 head 里出现过就去掉，别重复
        title = r.get("title", "")
        doc_no = md.get("doc_no", "")
        if doc_no and title.startswith(doc_no):
            title = title[len(doc_no):].lstrip("｜| 　")
        body = (r.get("content") or "").strip()
        # _mk_record 已经把出处拼在 content 首行，避免重复
        blocks.append(
            f"[{i}] 《{title}》"
            + (f"（{head}）" if head else "")
            + f"\n相关度 {r.get('score', 0):.3f}\n{body}"
        )
    return "\n\n---\n\n".join(blocks)


# ── 工具实现：主端点和公开端点共用同一份代码 ─────────────────
def search_policy(query: str, region: str = "", top_k: int = 5) -> str:
    """检索国家、省级、规划三类政策库，返回带出处的原文片段。

    什么时候用：用户问政策依据、申报条件、支持标准、某地有什么政策、
    「有没有文件规定」这类问题时，先用这个工具拿原文再回答。

    Args:
        query: 检索问题，用自然语言描述即可。例：「高标准农田建设每亩补助多少」
        region: 限定地域，填省份简称（如「四川」「山东」）或 6 位行政区划码
                （如「510000」）。留空表示不限地域，全国范围检索。
        top_k: 返回条数，1-10，默认 5。

    Returns:
        每条包含文件名、发文机关、文号、发布日期、相关度分数和原文片段。
    """
    top_k = max(1, min(int(top_k or 5), 10))
    records = _kb().search_bailian_all(
        query, top_k=top_k, score_threshold=0.0, region=(region or None)
    )
    _bill("search_policy", query)
    return _fmt_hits(records)


def _locate_doc(doc_no: str = "", title: str = ""):
    """按文号或标题定位到唯一一份文件。

    同一文号可能命中多份文件（同名文号跨年、跨省），取切片数最多的那份
    ——切片最多通常就是正文最全的那份。

    Returns:
        (doc_id, items)；找不到返回 (None, [])。items 已按 chunk_idx 排好。
    """
    key = (doc_no or "").strip()
    tkey = (title or "").strip()
    if not key and not tkey:
        return None, []

    # 优先走偏移索引：遍历 2 万多个文件的元数据，毫秒级。
    try:
        idx = _span_index()
    except Exception:
        idx = None

    if idx:
        best_id, best_spans, best_size = None, None, 0
        for did, rec in idx.items():
            if key:
                hit = key in (rec.get("n") or "") or key in did
            else:
                hit = tkey in (rec.get("t") or "") or tkey in did
            if not hit:
                continue
            spans = rec.get("s") or []
            size = sum(sp[1] for sp in spans)
            if size > best_size:
                best_id, best_spans, best_size = did, spans, size
        if best_id and best_spans:
            items = _read_spans(best_spans)
            if items:
                items.sort(key=lambda c: c.get("chunk_idx", 0))
                return best_id, items

    # 索引不可用或没命中，退回 grep 扫描。
    matched = _scan_chunks(key, tkey)
    if not matched:
        return None, []

    by_doc: Dict[str, List[Dict]] = {}
    for c in matched:
        by_doc.setdefault(c.get("doc_id") or "", []).append(c)
    doc_id, items = max(by_doc.items(), key=lambda kv: len(kv[1]))
    items.sort(key=lambda c: c.get("chunk_idx", 0))
    return doc_id, items


def get_article(doc_no: str = "", title: str = "") -> str:
    """按文号或文件标题取政策原文全文。

    什么时候用：已经通过 search_policy 定位到某份文件，需要逐条引用、
    核对具体条款、看完整章节时用这个。只想要片段不要用，直接看
    search_policy 的返回即可。

    Args:
        doc_no: 文号，如「国令第712号」「川办发〔2026〕42号」。
                支持模糊匹配，只要包含即可。
        title: 文件标题关键词，如「政府投资条例」。doc_no 为空时才用。

    Returns:
        该文件全部段落拼接的正文，含标题、文号、发文机关、来源链接。
    """
    key = (doc_no or "").strip()
    tkey = (title or "").strip()
    if not key and not tkey:
        return "请提供 doc_no（文号）或 title（标题关键词）之一。"

    doc_id, items = _locate_doc(key, tkey)
    if not items:
        return f"库里没有找到匹配的文件（doc_no={key!r}, title={tkey!r}）。先用 search_policy 检索确认文件名。"

    first = items[0]
    head = [
        f"# {first.get('title', '')}",
        "",
        "｜".join(
            x for x in [
                first.get("doc_no", ""),
                first.get("issuer", ""),
                first.get("pub_date", ""),
                first.get("level", ""),
                first.get("province", ""),
            ] if x
        ),
        "",
    ]
    body = "\n\n".join((c.get("text") or "").strip() for c in items)
    if first.get("source"):
        head.append(f"来源：{first['source']}")
        head.append("")

    text = "\n".join(head) + body
    if len(text) > MAX_ARTICLE_CHARS:
        text = (
            text[:MAX_ARTICLE_CHARS]
            + f"\n\n……（全文 {len(text)} 字，此处截断到前 {MAX_ARTICLE_CHARS} 字）"
        )
    _bill("get_article", f"{key}|{tkey}")
    return text


# 公开端点的正文节选配额。全进程计数，不分 IP——IP 层已经有一道 30 rpm，
# 这里再卡一道总量，防止换 IP 并发把库拖走。正常会话一次问询调几次就够。
EXCERPT_DEFAULT_CHARS = 1200
EXCERPT_MAX_CHARS = 2000
EXCERPT_GLOBAL_RPM = int(os.environ.get("KB_MCP_EXCERPT_RPM", "60"))
_excerpt_hits: deque = deque()


def _excerpt_quota_ok() -> bool:
    now = time.time()
    while _excerpt_hits and now - _excerpt_hits[0] > 60:
        _excerpt_hits.popleft()
    if len(_excerpt_hits) >= EXCERPT_GLOBAL_RPM:
        return False
    _excerpt_hits.append(now)
    return True


def get_excerpt(doc_no: str = "", title: str = "", max_chars: int = 1200) -> str:
    """按文号或标题取政策正文节选（前若干字），用于确认文件内容和核心条款。

    什么时候用：已经通过 search_policy 定位到候选文件，需要确认「是不是
    这一份」、看正文开头和发文背景时用这个。要逐条引用、核对全部章节，
    请改用授权端点的 get_article 取完整全文。

    Args:
        doc_no: 文号，如「国令第712号」「川办发〔2026〕42号」。
                支持模糊匹配，只要包含即可。
        title: 文件标题关键词，如「政府投资条例」。doc_no 为空时才用。
        max_chars: 正文最多返回多少字。默认 1200，允许 300~2000。

    Returns:
        标题、文号、发文机关、日期、层级、地域、来源链接，加正文前 max_chars 字。
    """
    if not _excerpt_quota_ok():
        return (
            f"正文节选接口已达公开配额（每分钟 {EXCERPT_GLOBAL_RPM} 次）。"
            "请稍后再试；需要批量取全文请申请授权令牌。"
        )

    try:
        n = int(max_chars)
    except (TypeError, ValueError):
        n = EXCERPT_DEFAULT_CHARS
    n = max(300, min(n, EXCERPT_MAX_CHARS))

    key = (doc_no or "").strip()
    tkey = (title or "").strip()
    if not key and not tkey:
        return "请提供 doc_no（文号）或 title（标题关键词）之一。"

    doc_id, items = _locate_doc(key, tkey)
    if not items:
        return (
            f"库里没有找到匹配的文件（doc_no={key!r}, title={tkey!r}）。"
            "先用 search_policy 检索确认文件名。"
        )

    first = items[0]
    meta = "｜".join(
        x for x in [
            first.get("doc_no", ""),
            first.get("issuer", ""),
            first.get("pub_date", ""),
            first.get("level", ""),
            first.get("province", ""),
        ] if x
    )

    body = "\n\n".join((c.get("text") or "").strip() for c in items)
    total = len(body)
    cut = body[:n]
    if total > n:
        cut += f"\n\n……（正文共 {total} 字，此处为前 {n} 字节选）"

    out = [f"# {first.get('title', '')}", "", meta, ""]
    if first.get("source"):
        out += [f"来源：{first['source']}", ""]
    out.append(cut)
    out += [
        "",
        "──",
        "以上为节选。完整全文（逐条引用、核对全部章节）需使用授权端点 /mcp 的 get_article。",
    ]
    _bill("get_excerpt", f"{key}|{tkey}")
    return "\n".join(out)


def list_recent(province: str = "", limit: int = 20) -> str:
    """列出库中最新收录的政策文件，按发布日期倒序。

    什么时候用：用户问「最近有什么新政策」「本月出台了哪些文件」
    「XX 省最近发了什么」这类没有具体检索词、只想看动态的问题。

    Args:
        province: 限定省份简称，如「四川」。留空表示全部。
        limit: 返回条数，1-50，默认 20。

    Returns:
        文件清单，每行包含日期、标题、文号、层级。
    """
    limit = max(1, min(int(limit or 20), 50))
    docs = _meta().get("doc_list") or []

    def _date_key(d: Dict) -> str:
        raw = d.get("pub_date") or d.get("date") or ""
        # 「2019年05月05日」→「20190505」便于排序
        return "".join(ch for ch in raw if ch.isdigit())[:8] or "00000000"

    rows = docs
    if province:
        p = province.strip()
        rows = [d for d in rows if p in (d.get("province") or "")
                or p in (d.get("title") or "")]
    rows = sorted(rows, key=_date_key, reverse=True)[:limit]

    if not rows:
        return "没有匹配的文件。"

    lines = [f"共 {len(rows)} 条（库内文档总数 {len(docs)}）："]
    for d in rows:
        tag = "｜".join(
            x for x in [
                d.get("level", ""),
                d.get("province", "") if d.get("level") == "省" else "",
                d.get("doc_no", ""),
            ] if x
        )
        lines.append(
            f"- {d.get('pub_date') or d.get('date') or '日期不详'} "
            f"《{d.get('title', '')}》" + (f"（{tag}）" if tag else "")
        )
    _bill("list_recent", province)
    return "\n".join(lines)


def corpus_stats() -> str:
    """查看知识库规模和覆盖范围。

    什么时候用：用户问「库里有多少文件」「覆盖哪些省」「有没有 XX 领域的
    政策」时用，先确认覆盖范围再决定要不要检索，避免答非所问。
    """
    meta = _meta()
    docs = meta.get("doc_list") or []
    # 切片总数直接读 meta，不去加载切片文件——全量加载会 OOM。
    chunks_n = int(meta.get("chunks") or 0)

    by_level: Dict[str, int] = {}
    by_prov: Dict[str, int] = {}
    by_year: Dict[str, int] = {}
    for d in docs:
        lv = d.get("level") or "未标注"
        by_level[lv] = by_level.get(lv, 0) + 1
        pv = d.get("province") or ""
        if pv:
            by_prov[pv] = by_prov.get(pv, 0) + 1
        y = "".join(ch for ch in (d.get("pub_date") or d.get("date") or "")
                    if ch.isdigit())[:4]
        if y:
            by_year[y] = by_year.get(y, 0) + 1

    out = [
        "## 发改政策知识库",
        "",
        f"- 文档总数：{len(docs)} 篇",
        f"- 切片总数：{chunks_n} 条",
        f"- 覆盖层级：" + "、".join(
            f"{k} {v} 篇" for k, v in sorted(by_level.items(), key=lambda x: -x[1])),
    ]
    if by_prov:
        top = sorted(by_prov.items(), key=lambda x: -x[1])
        out.append("- 省级覆盖（前 15）：" + "、".join(
            f"{k} {v}" for k, v in top[:15]))
    if by_year:
        years = sorted(by_year.items(), key=lambda x: x[0], reverse=True)[:8]
        out.append("- 年度分布（近 8 年）：" + "、".join(
            f"{k} 年 {v} 篇" for k, v in years))
    out.append("")
    out.append("检索库：国家政策库、省级政策库、规划纲要库（经验库单独计费，未并入）。")
    return "\n".join(out)


# 主端点：四个工具全给
mcp.add_tool(search_policy)
mcp.add_tool(get_article)
mcp.add_tool(list_recent)
mcp.add_tool(corpus_stats)

# 公开端点：检索 + 正文节选 + 统计。get_article 不上——完整全文是这库最
# 值钱的部分，不该让匿名爬虫整篇拖走；但只给检索会断链：agent 找到文件后
# 拿不到正文，Glama 的 TDQS 评审就卡在 Completeness 3/5。
# get_excerpt 只给前 1200 字（上限 2000），够确认是不是这份文件，不够拼出全库。
public_mcp.add_tool(search_policy)
public_mcp.add_tool(get_excerpt)
public_mcp.add_tool(list_recent)
public_mcp.add_tool(corpus_stats)


@mcp.resource("policy://index")
def policy_index() -> str:
    """知识库目录概览，供客户端在会话开始时加载。"""
    return corpus_stats()


@public_mcp.resource("policy://index")
def public_policy_index() -> str:
    """知识库目录概览，供客户端在会话开始时加载。"""
    return corpus_stats()


# ── 中间件 ───────────────────────────────────────────────────
class Guard(BaseHTTPMiddleware):
    """两条规则：

    1. /public/* 不校验 token，但按 IP 限流。
    2. 其余路径要求 Bearer token（TOKEN 为空时整体放行，内网自用模式）。
    """

    def __init__(self, app, token: str, rpm: int):
        super().__init__(app)
        self.token = token
        self.rpm = rpm
        self._hits: Dict[str, deque] = defaultdict(deque)

    def _rate_ok(self, ip: str) -> bool:
        now = time.time()
        q = self._hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.rpm:
            return False
        q.append(now)
        return True

    async def dispatch(self, request, call_next):
        path = request.url.path
        # /mcp-health 是 cloudflared 隧道里的探活别名——隧道路由按 path
        # 前缀分发，直接用 /health 会和主服务的 /health 抢位置。
        if path.rstrip("/") in ("/health", "/mcp-health", ""):
            return JSONResponse({"status": "ok",
                                 "auth": "enabled" if self.token else "disabled"})

        auth = request.headers.get("authorization", "")
        got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        if path.startswith("/public"):
            # 带 API key 的走付费通道：不限 IP、按余额扣费。
            # 顺序很重要——先认 key 再算 IP，否则付费用户会被免费档的
            # 30 rpm 挡住，买了额度还用不了。
            if got.startswith(_KEY_PREFIX):
                info = _verify_key(got)
                if info is None:
                    return JSONResponse(
                        {"error": "invalid_api_key",
                         "detail": "API key 无效或已吊销。免费使用请去掉 Authorization 头。"},
                        status_code=401,
                    )
                if info["balance"] <= 0:
                    return JSONResponse(
                        {"error": "insufficient_balance",
                         "balance": info["balance"],
                         "detail": "余额不足。请充值，或去掉 Authorization 头走免费档。"},
                        status_code=402,
                    )
                tok = _BILL_CTX.set(info)
                try:
                    return await call_next(request)
                finally:
                    _BILL_CTX.reset(tok)

            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
                or (request.client.host if request.client else "unknown")
            if not self._rate_ok(ip):
                return JSONResponse(
                    {"error": "rate_limited",
                     "detail": f"公开端点限 {self.rpm} 次/分钟，"
                               "需要更高配额请申请 API key"
                               "（Authorization: Bearer fagai_xxx）"},
                    status_code=429,
                )
            return await call_next(request)

        if not self.token:
            return await call_next(request)

        if got != self.token:
            return JSONResponse(
                {"error": "unauthorized",
                 "detail": "需要 Authorization: Bearer <token>；公开只读端点见 /public/mcp"},
                status_code=401,
            )
        return await call_next(request)


def _warm_span_index() -> None:
    """后台把偏移索引建好。

    这一步只存文号、标题和字节区间，占内存十几 MB，不会重演「全量加载
    切片被 OOM 杀掉」。不预热的话，服务刚重启后第一次取正文要现建索引，
    得等十几秒。这行日志也是部署脚本判断「可以验了」的依据，别删。
    """
    try:
        n = len(_span_index())
        print(f"[mcp] 偏移索引就绪：{n} 个文件可定位", flush=True)
    except Exception as e:
        print(f"[mcp] 偏移索引构建失败：{e}", flush=True)


def build_app() -> Starlette:
    """一个进程提供四个入口。

    /mcp          → Streamable HTTP，全量工具，需鉴权
    /sse          → SSE，全量工具，需鉴权
    /public/mcp   → Streamable HTTP，只读三件套，匿名 + 限流
    /public/sse   → SSE，只读三件套，匿名 + 限流

    不套 Mount：Mount 会把 /mcp 307 重定向到 /mcp/，而 httpx（MCP 客户端
    底层）默认不跟随重定向，握手会直接失败。这里直接合并四个子 app 的
    路由，路径原样保留。lifespan 要同时拉起两个 session_manager，
    否则对应端点的会话管理器不会启动。
    """
    main_http = mcp.streamable_http_app()          # 同时初始化 session_manager
    main_sse = mcp.sse_app()
    pub_http = public_mcp.streamable_http_app()    # 同上
    pub_sse = public_mcp.sse_app()

    routes = (list(main_http.routes) + list(main_sse.routes)
              + list(pub_http.routes) + list(pub_sse.routes))

    async def lifespan(app):
        async with mcp.session_manager.run():
            async with public_mcp.session_manager.run():
                # 后台建偏移索引。别改回全量加载切片——那会把 13.6 万条
                # 正文读进内存，启动几秒就被内核 OOM Killer 杀掉。
                threading.Thread(target=_warm_span_index, daemon=True).start()
                yield

    return Starlette(
        routes=routes,
        middleware=[Middleware(Guard, token=TOKEN, rpm=PUBLIC_RATE_LIMIT)],
        lifespan=lifespan,
    )


app = build_app()

# 把 api_keys 表建好。mcp_server 独立于 app.py 启动，不能指望主服务
# 先跑过 init_db。executescript 全是 IF NOT EXISTS，重复跑无害。
try:
    import billing as _billing
    _billing.init_db()
except Exception as _e:
    print(f"[mcp][bill] 建表失败：{_e}", flush=True)


if __name__ == "__main__":
    import uvicorn

    print(f"[mcp] fagai-policy-kb  listening on http://{HOST}:{PORT}")
    print(f"[mcp]   /mcp          streamable-http, 4 tools, auth required")
    print(f"[mcp]   /sse          sse,            4 tools, auth required")
    print(f"[mcp]   /public/mcp   streamable-http, 3 tools, anonymous, "
          f"{PUBLIC_RATE_LIMIT} rpm")
    print(f"[mcp]   /public/sse   sse,            3 tools, anonymous, "
          f"{PUBLIC_RATE_LIMIT} rpm")
    print(f"[mcp]   auth token   {'set' if TOKEN else 'DISABLED'}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
