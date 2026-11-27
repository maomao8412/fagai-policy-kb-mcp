"""按 token 计费的账号与账务模块。

用 SQLite 单文件，跟着服务走，不依赖外部数据库。计费单位是 token：
用户充值拿到 token 额度，每次问答按 Dify 返回的 usage.total_tokens 扣减。

为什么按 total_tokens 而不是 completion_tokens：
这个 Agent 应用每次请求都带完整工具定义和知识库召回结果，实测问一句「你好」
prompt 就有 16857 token，completion 才 645。只算输出会把成本算漏 96%。

登录态用 HMAC 签名 token，不建 session 表：格式是 base64(uid|exp|sig)，
服务端无状态校验，重启不掉登录。
"""

import base64
import datetime
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BILLING_DB") or os.path.join(BASE_DIR, "billing.db")
SECRET_FILE = os.path.join(BASE_DIR, ".billing_secret")

TOKEN_TTL = 30 * 24 * 3600  # 登录态有效期 30 天
PBKDF2_ROUNDS = 120_000


def _secret() -> bytes:
    """签名密钥。优先读环境变量，其次读本地文件，都没有就生成一个。

    文件权限设成 600，同目录下别人读不到。改了密钥等于让所有人重新登录，
    所以只生成一次。
    """
    s = (os.environ.get("BILLING_SECRET") or "").strip()
    if s:
        return s.encode("utf-8")
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            return f.read().strip()
    k = secrets.token_hex(32).encode("utf-8")
    with open(SECRET_FILE, "wb") as f:
        f.write(k)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return k


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    # WAL 让读不阻塞写，扣费时并发问答不会互相等
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pwd_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  tokens_balance INTEGER NOT NULL DEFAULT 0,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  role TEXT NOT NULL DEFAULT 'user',
  status INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  conversation_id TEXT,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  query TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id, id DESC);
CREATE TABLE IF NOT EXISTS recharge_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  tokens INTEGER NOT NULL,
  note TEXT,
  operator TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recharge_user ON recharge_log(user_id, id DESC);
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  tokens INTEGER NOT NULL,
  price REAL NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0,
  status INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS redeem_codes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  plan_id INTEGER,
  plan_name TEXT,
  tokens INTEGER NOT NULL,
  price REAL NOT NULL DEFAULT 0,
  status INTEGER NOT NULL DEFAULT 0,
  used_by INTEGER,
  used_by_name TEXT,
  used_at INTEGER,
  note TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_code_status ON redeem_codes(status, id DESC);
CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  key_hash TEXT UNIQUE NOT NULL,
  key_prefix TEXT NOT NULL,
  name TEXT DEFAULT '',
  status INTEGER NOT NULL DEFAULT 1,
  last_used_at INTEGER,
  total_calls INTEGER NOT NULL DEFAULT 0,
  total_units INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_apikey_user ON api_keys(user_id, id DESC);
CREATE TABLE IF NOT EXISTS login_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  username TEXT,
  method TEXT NOT NULL DEFAULT 'password',
  ok INTEGER NOT NULL DEFAULT 1,
  ip TEXT,
  ua TEXT,
  detail TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_time ON login_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_user ON login_log(user_id, id DESC);
"""


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def _hash(pwd: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pwd.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()


def create_user(username: str, password: str, tokens: int = 0,
                role: str = "user") -> Dict:
    """开一个账号。用户名已存在时抛 ValueError。"""
    username = (username or "").strip()
    if not username or len(username) < 2:
        raise ValueError("用户名至少 2 个字符")
    if not password or len(password) < 6:
        raise ValueError("密码至少 6 位")
    salt = secrets.token_hex(16)
    now = int(time.time())
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(username, pwd_hash, salt, tokens_balance,"
                " tokens_used, role, status, created_at)"
                " VALUES(?,?,?,?,0,?,1,?)",
                (username, _hash(password, salt), salt, int(tokens), role, now),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"用户名 {username} 已存在")
        uid = cur.lastrowid
        if tokens:
            c.execute(
                "INSERT INTO recharge_log(user_id, username, tokens, note,"
                " operator, created_at) VALUES(?,?,?,?,?,?)",
                (uid, username, int(tokens), "开户赠送", "system", now),
            )
    return get_user(uid)


def register_user(username: str, tokens: int = 0,
                  password: Optional[str] = None) -> Dict:
    """自助注册。手机号即账号，不用设密码。

    给一个随机密码占位，客户永远用不到——登录走手机号免密。
    用户名已存在时抛 ValueError，调用方据此判断「已注册过」。
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("手机号不能为空")
    pwd = password or secrets.token_urlsafe(18)
    salt = secrets.token_hex(16)
    now = int(time.time())
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(username, pwd_hash, salt, tokens_balance,"
                " tokens_used, role, status, created_at)"
                " VALUES(?,?,?,?,0,'user',1,?)",
                (username, _hash(pwd, salt), salt, int(tokens), now),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"{username} 已注册")
        uid = cur.lastrowid
        if tokens:
            c.execute(
                "INSERT INTO recharge_log(user_id, username, tokens, note,"
                " operator, created_at) VALUES(?,?,?,?,?,?)",
                (uid, username, int(tokens), "自助注册赠送", "system", now),
            )
    return get_user(uid)


def signups_from_ip(ip: str, hours: int = 24) -> int:
    """同一 IP 这段时间内注册了几个号。

    自助注册是公开入口，没这道闸，一个脚本能刷出几百个号把体验
    额度薅干净。按 IP 限流够用，误伤概率很低。
    """
    if not ip:
        return 0
    since = int(time.time()) - int(hours) * 3600
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n FROM login_log WHERE ip=? AND method='signup'"
            " AND created_at>=?", (ip, since)).fetchone()
    return row["n"] if row else 0


def verify_user(username: str, password: str) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=?", ((username or "").strip(),)
        ).fetchone()
    if not row:
        return None
    if not hmac.compare_digest(_hash(password, row["salt"]), row["pwd_hash"]):
        return None
    if not row["status"]:
        return None
    return _row_to_user(row)


def get_user(uid: int) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_name(username: str) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _row_to_user(row) if row else None


def _row_to_user(row: sqlite3.Row) -> Dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "tokens_balance": row["tokens_balance"],
        "tokens_used": row["tokens_used"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def list_users() -> List[Dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, username, tokens_balance, tokens_used, role, status,"
            " created_at FROM users ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def set_status(uid: int, status: int) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE users SET status=? WHERE id=?", (int(status), uid))
    return cur.rowcount > 0


def add_tokens(uid: int, tokens: int, note: str = "",
               operator: str = "admin") -> Optional[Dict]:
    """充值。tokens 可以是负数，用来手工扣减或纠错。"""
    now = int(time.time())
    with _conn() as c:
        row = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE users SET tokens_balance = tokens_balance + ? WHERE id=?",
            (int(tokens), uid),
        )
        c.execute(
            "INSERT INTO recharge_log(user_id, username, tokens, note, operator,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (uid, row["username"], int(tokens), note, operator, now),
        )
    return get_user(uid)


def charge(uid: int, usage: Dict, conversation_id: str = "",
           query: str = "") -> int:
    """扣一次问答的费用，返回实际扣掉的 token 数。

    只扣余额、不因为余额不够而拒绝——请求已经跑完，token 已经烧掉了，
    这里拒绝没有意义，只会让账对不上。余额变负由下一次请求的前置检查拦住。
    """
    prompt_t = int(usage.get("prompt_tokens") or 0)
    compl_t = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt_t + compl_t))
    if total <= 0:
        return 0
    now = int(time.time())
    with _conn() as c:
        row = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        name = row["username"] if row else ""
        c.execute(
            "UPDATE users SET tokens_balance = tokens_balance - ?,"
            " tokens_used = tokens_used + ? WHERE id=?",
            (total, total, uid),
        )
        c.execute(
            "INSERT INTO usage_log(user_id, username, conversation_id,"
            " prompt_tokens, completion_tokens, total_tokens, query, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (uid, name, conversation_id or "", prompt_t, compl_t, total,
             (query or "")[:120], now),
        )
    return total


def usage_list(uid: Optional[int] = None, limit: int = 50) -> List[Dict]:
    sql = ("SELECT id, username, conversation_id, prompt_tokens, completion_tokens,"
           " total_tokens, query, created_at FROM usage_log")
    args: tuple = ()
    if uid is not None:
        sql += " WHERE user_id=?"
        args = (uid,)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (int(limit),)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def recharge_list(uid: Optional[int] = None, limit: int = 50) -> List[Dict]:
    sql = ("SELECT id, username, tokens, note, operator, created_at"
           " FROM recharge_log")
    args: tuple = ()
    if uid is not None:
        sql += " WHERE user_id=?"
        args = (uid,)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (int(limit),)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def stats() -> Dict:
    now = int(time.time())
    day_ago = now - 86400
    with _conn() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        total = c.execute(
            "SELECT COALESCE(SUM(total_tokens),0) t FROM usage_log").fetchone()["t"]
        today = c.execute(
            "SELECT COALESCE(SUM(total_tokens),0) t FROM usage_log WHERE created_at>=?",
            (day_ago,)).fetchone()["t"]
        calls = c.execute("SELECT COUNT(*) n FROM usage_log").fetchone()["n"]
    return {"users": users, "total_tokens": total, "day_tokens": today,
            "calls": calls}


def usage_stats(uid: Optional[int] = None, days: int = 14) -> Dict:
    """用量统计：总览 + 按天曲线 + 明细。

    created_at 存的是 unix 秒，按天聚合要转到北京时间再切，
    否则晚上 8 点之后的量会被算到前一天去。
    """
    days = max(1, min(int(days), 90))
    now = int(time.time())
    tz = "+8 hours"
    day_start = f"strftime('%Y-%m-%d', {now}, 'unixepoch', '{tz}')"
    since = now - days * 86400

    where = "WHERE user_id=?" if uid is not None else ""
    wargs: tuple = (uid,) if uid is not None else ()

    with _conn() as c:
        row = c.execute(
            f"SELECT COUNT(*) calls,"
            f" COALESCE(SUM(total_tokens),0) total,"
            f" COALESCE(SUM(prompt_tokens),0) prompt,"
            f" COALESCE(SUM(completion_tokens),0) completion"
            f" FROM usage_log {where}", wargs).fetchone()

        today = c.execute(
            f"SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) t"
            f" FROM usage_log {where}{' AND' if where else 'WHERE'}"
            f" strftime('%Y-%m-%d', created_at, 'unixepoch', '{tz}') = {day_start}",
            wargs).fetchone()

        week = c.execute(
            f"SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) t"
            f" FROM usage_log {where}{' AND' if where else 'WHERE'}"
            f" created_at >= ?", wargs + (now - 7 * 86400,)).fetchone()

        daily_rows = c.execute(
            f"SELECT strftime('%Y-%m-%d', created_at, 'unixepoch', '{tz}') d,"
            f" COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens"
            f" FROM usage_log {where}{' AND' if where else 'WHERE'}"
            f" created_at >= ? GROUP BY d ORDER BY d",
            wargs + (since,)).fetchall()
        daily = {r["d"]: {"calls": r["calls"], "tokens": r["tokens"]}
                 for r in daily_rows}

        recent = c.execute(
            f"SELECT username, prompt_tokens, completion_tokens, total_tokens,"
            f" query, created_at FROM usage_log {where}"
            f" ORDER BY id DESC LIMIT 20", wargs).fetchall()

    # 把没量的日子补成 0，前端画柱状图不用自己填洞
    series: List[Dict] = []
    base = datetime.date.today()
    for i in range(days - 1, -1, -1):
        d = (base - datetime.timedelta(days=i)).isoformat()
        hit = daily.get(d) or {"calls": 0, "tokens": 0}
        series.append({"date": d, "calls": hit["calls"], "tokens": hit["tokens"]})

    calls = row["calls"] or 0
    out = {
        "days": days,
        "calls_total": calls,
        "tokens_total": row["total"],
        "prompt_total": row["prompt"],
        "completion_total": row["completion"],
        "avg_per_call": int(row["total"] / calls) if calls else 0,
        "calls_today": today["calls"],
        "tokens_today": today["t"],
        "calls_week": week["calls"],
        "tokens_week": week["t"],
        "daily": series,
        "recent": [dict(r) for r in recent],
    }

    if uid is not None:
        u = get_user(uid)
        if u:
            out["balance"] = u["tokens_balance"]
            out["tokens_used"] = u["tokens_used"]
            # 按最近 7 天日均推算还能用几天，让用户心里有数
            per_day = (week["t"] / 7.0) if week["t"] else 0
            out["days_left"] = round(u["tokens_balance"] / per_day, 1) if per_day else None
    else:
        with _conn() as c:
            tops = c.execute(
                "SELECT username, COUNT(*) calls,"
                " COALESCE(SUM(total_tokens),0) tokens"
                " FROM usage_log GROUP BY username"
                " ORDER BY tokens DESC LIMIT 10").fetchall()
            active = c.execute(
                "SELECT COUNT(DISTINCT user_id) n FROM usage_log"
                " WHERE created_at >= ?", (now - 86400,)).fetchone()["n"]
            bal = c.execute(
                "SELECT COALESCE(SUM(tokens_balance),0) b FROM users").fetchone()["b"]
        out["top_users"] = [dict(r) for r in tops]
        out["active_today"] = active
        out["balance_total"] = bal

    return out



# ── 登录态 ──────────────────────────────────────────────────

def make_token(uid: int, ttl: int = TOKEN_TTL) -> str:
    exp = int(time.time()) + ttl
    payload = f"{uid}|{exp}".encode("utf-8")
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + b"|" + sig).decode("ascii")


def verify_token(token: str) -> Optional[int]:
    """验签并返回 uid；过期或签名不对返回 None。"""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        uid_s, exp_s, sig = raw.rsplit(b"|", 2)
        payload = uid_s + b"|" + exp_s
        want = hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(sig, want):
            return None
        if int(exp_s) < int(time.time()):
            return None
        return int(uid_s)
    except Exception:
        return None


def ensure_admin(username: str = "admin", password: str = "") -> None:
    """首次启动时建管理员。密码取环境变量，没配就随机生成并打印到日志。"""
    init_db()
    if get_user_by_name(username):
        return
    pwd = (os.environ.get("BILLING_ADMIN_PWD") or password or "").strip()
    generated = False
    if not pwd:
        pwd = secrets.token_urlsafe(9)
        generated = True
    create_user(username, pwd, tokens=0, role="admin")
    if generated:
        print(f"[billing] 已创建管理员 {username}，初始密码：{pwd}"
              f"（请登录后立即修改，并写入 .env 的 BILLING_ADMIN_PWD）")
    else:
        print(f"[billing] 已创建管理员 {username}")


# ── 套餐与兑换码 ────────────────────────────────────────────
# 收款和记账分开：钱怎么进来是支付渠道的事，进来之后统一变成兑换码。
# 这样换渠道（个人码、支付宝、插件）都不用动账务逻辑，码是唯一的入口凭证。

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I/O/0/1，避免手抄错


def _gen_code(prefix: str = "FG") -> str:
    """生成 FG-XXXX-XXXX 形式的码。"""
    def blk():
        return "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    return f"{prefix}-{blk()}-{blk()}"


def list_plans(only_active: bool = True) -> List[Dict]:
    sql = "SELECT id, name, tokens, price, sort, status FROM plans"
    if only_active:
        sql += " WHERE status=1"
    sql += " ORDER BY sort ASC, id ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


def upsert_plan(plan_id: Optional[int], name: str, tokens: int, price: float,
                sort: int = 0, status: int = 1) -> Dict:
    with _conn() as c:
        if plan_id:
            c.execute(
                "UPDATE plans SET name=?, tokens=?, price=?, sort=?, status=?"
                " WHERE id=?",
                (name, int(tokens), float(price), int(sort), int(status), plan_id))
        else:
            cur = c.execute(
                "INSERT INTO plans(name, tokens, price, sort, status)"
                " VALUES(?,?,?,?,?)",
                (name, int(tokens), float(price), int(sort), int(status)))
            plan_id = cur.lastrowid
    with _conn() as c:
        row = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    return dict(row) if row else {}


def create_codes(count: int, tokens: int, price: float = 0,
                 plan_id: Optional[int] = None, plan_name: str = "",
                 note: str = "") -> List[str]:
    """批量发码。返回生成的码列表。

    码是钱，重复了就白送，所以插入时靠 UNIQUE 约束兜底，
    撞了就换一个重试，不靠概率赌。
    """
    count = max(1, min(int(count), 500))
    now = int(time.time())
    out: List[str] = []
    with _conn() as c:
        for _ in range(count):
            for _try in range(20):
                code = _gen_code()
                try:
                    c.execute(
                        "INSERT INTO redeem_codes(code, plan_id, plan_name, tokens,"
                        " price, status, note, created_at)"
                        " VALUES(?,?,?,?,?,0,?,?)",
                        (code, plan_id, plan_name or "", int(tokens),
                         float(price), note or "", now))
                    out.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue
    return out


def redeem(uid: int, code: str) -> Dict:
    """兑换一个码。返回 {ok, tokens, msg}。

    抢占式更新：只有把 status 从 0 改成 1 成功的那一次才算数，
    并发重复提交同一个码只会有一个成功，不会双倍加钱。
    """
    code = (code or "").strip().upper().replace(" ", "")
    if not code:
        return {"ok": False, "tokens": 0, "msg": "请输入兑换码"}
    now = int(time.time())
    with _conn() as c:
        row = c.execute(
            "SELECT id, tokens, price, plan_name, status FROM redeem_codes"
            " WHERE code=?", (code,)).fetchone()
        if not row:
            return {"ok": False, "tokens": 0, "msg": "兑换码不存在，请核对后重试"}
        if row["status"]:
            return {"ok": False, "tokens": 0, "msg": "这个码已经被用过了"}
        u = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        name = u["username"] if u else ""
        cur = c.execute(
            "UPDATE redeem_codes SET status=1, used_by=?, used_by_name=?, used_at=?"
            " WHERE id=? AND status=0", (uid, name, now, row["id"]))
        if cur.rowcount != 1:
            return {"ok": False, "tokens": 0, "msg": "这个码已经被用过了"}
        c.execute(
            "UPDATE users SET tokens_balance = tokens_balance + ? WHERE id=?",
            (row["tokens"], uid))
        c.execute(
            "INSERT INTO recharge_log(user_id, username, tokens, note, operator,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (uid, name, row["tokens"],
             f"兑换码 {code}" + (f"（{row['plan_name']}）" if row["plan_name"] else ""),
             "redeem", now))
    fresh = get_user(uid)
    return {"ok": True, "tokens": row["tokens"], "msg": "兑换成功",
            "balance": fresh["tokens_balance"] if fresh else 0}


def code_list(status: Optional[int] = None, limit: int = 200) -> List[Dict]:
    sql = ("SELECT id, code, plan_name, tokens, price, status, used_by_name,"
           " used_at, note, created_at FROM redeem_codes")
    args: tuple = ()
    if status is not None:
        sql += " WHERE status=?"
        args = (int(status),)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (int(limit),)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ── 登录记录 ────────────────────────────────────────────────
# 谁在什么时候从哪儿进来的都留痕。出了纠纷能查，
# 也看得出哪个客户是真在用、哪个只登了一次就没再来。

def log_login(uid: Optional[int], username: str, method: str = "password",
              ok: bool = True, ip: str = "", ua: str = "",
              detail: str = "") -> None:
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO login_log(user_id, username, method, ok, ip, ua,"
                " detail, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (uid, (username or "")[:64], method, 1 if ok else 0,
                 (ip or "")[:64], (ua or "")[:300], (detail or "")[:200],
                 int(time.time())))
    except Exception as e:
        # 记日志失败不能连累登录本身
        print(f"[billing] 写登录日志失败：{e}")


def login_list(uid: Optional[int] = None, limit: int = 100) -> List[Dict]:
    sql = ("SELECT id, user_id, username, method, ok, ip, ua, detail, created_at"
           " FROM login_log")
    args: tuple = ()
    if uid is not None:
        sql += " WHERE user_id=?"
        args = (uid,)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (int(limit),)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def login_stats() -> Dict:
    now = int(time.time())
    day = now - 86400
    week = now - 7 * 86400
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) n FROM login_log WHERE ok=1").fetchone()["n"]
        today = c.execute(
            "SELECT COUNT(*) n FROM login_log WHERE ok=1 AND created_at>=?",
            (day,)).fetchone()["n"]
        users_today = c.execute(
            "SELECT COUNT(DISTINCT user_id) n FROM login_log"
            " WHERE ok=1 AND created_at>=?", (day,)).fetchone()["n"]
        users_week = c.execute(
            "SELECT COUNT(DISTINCT user_id) n FROM login_log"
            " WHERE ok=1 AND created_at>=?", (week,)).fetchone()["n"]
        failed = c.execute(
            "SELECT COUNT(*) n FROM login_log WHERE ok=0 AND created_at>=?",
            (day,)).fetchone()["n"]
    return {"logins_total": total, "logins_today": today,
            "users_today": users_today, "users_week": users_week,
            "failed_today": failed}


# ── API Key：给程序和外部 agent 用的长期凭证 ─────────────────
# 与登录 token 的区别：token 是浏览器会话用的、30 天过期、放 header 里
# 也没法吊销；API key 是发给客户或程序长期用的，可吊销、可统计、按次扣费。
# 库里只存 SHA256，明文只在签发时返回一次——服务端自己也拿不回来。

API_KEY_PREFIX = "fagai_"


def _key_hash(key: str) -> str:
    return hashlib.sha256((key or "").strip().encode()).hexdigest()


def create_api_key(uid: int, name: str = "") -> Optional[Dict]:
    """签发一把 API key，返回含明文的 dict（仅此一次）。"""
    u = get_user(uid)
    if not u:
        return None
    raw = API_KEY_PREFIX + secrets.token_hex(20)
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO api_keys(user_id, username, key_hash, key_prefix,"
            " name, status, created_at) VALUES(?,?,?,?,?,1,?)",
            (int(uid), u["username"], _key_hash(raw), raw[:14], name or "", now),
        )
        kid = cur.lastrowid
    return {"id": kid, "key": raw, "key_prefix": raw[:14],
            "name": name or "", "username": u["username"]}


def verify_api_key(key: str) -> Optional[Dict]:
    """校验 key。返回 {key_id, uid, username, name, balance}，无效返回 None。

    同时检查用户是否被停用——吊销单个 key 和停用整个账号是两件事，
    这里都拦住。
    """
    k = (key or "").strip()
    if not k.startswith(API_KEY_PREFIX):
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT k.id, k.user_id, k.username, k.name, k.status,"
            " u.tokens_balance, u.status AS ustatus"
            " FROM api_keys k JOIN users u ON u.id = k.user_id"
            " WHERE k.key_hash=?", (_key_hash(k),),
        ).fetchone()
    if not row or row["status"] != 1 or row["ustatus"] != 1:
        return None
    return {"key_id": row["id"], "uid": row["user_id"],
            "username": row["username"], "name": row["name"] or "",
            "balance": int(row["tokens_balance"])}


def touch_api_key(key_id: int, units: int = 0) -> None:
    """记一次调用。用量统计独立于 usage_log，方便按 key 出账单。"""
    with _conn() as c:
        c.execute(
            "UPDATE api_keys SET last_used_at=?, total_calls=total_calls+1,"
            " total_units=total_units+? WHERE id=?",
            (int(time.time()), int(units), int(key_id)),
        )


def list_api_keys(uid: Optional[int] = None, limit: int = 100) -> List[Dict]:
    sql = ("SELECT id, user_id, username, key_prefix, name, status,"
           " last_used_at, total_calls, total_units, created_at FROM api_keys")
    args: tuple = ()
    if uid is not None:
        sql += " WHERE user_id=?"
        args = (int(uid),)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (int(limit),)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def revoke_api_key(key_id: int, uid: Optional[int] = None) -> bool:
    """吊销一把 key。传 uid 时只能吊销自己的。"""
    sql = "UPDATE api_keys SET status=0 WHERE id=?"
    args: tuple = (int(key_id),)
    if uid is not None:
        sql += " AND user_id=?"
        args = args + (int(uid),)
    with _conn() as c:
        return c.execute(sql, args).rowcount > 0
