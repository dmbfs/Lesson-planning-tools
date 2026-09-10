"""备课导师 · 数据层（SQLite，零外部依赖）
5 张业务表（PRD 4.3）+ 3 张工程表：
  eval_tasks   异步评估任务状态
  traces       LLM 调用留痕（复赛合规材料的数据基础）
  usage_ledger 调用量记账（PRD 3.3 成本护栏）
"""
import json
import sqlite3
from pathlib import Path

from .config import DB_PATH
from .knowledge import OBSTACLES

_conn = None

# ---------- 连接与初始化 ----------
def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
        _seed_defaults(_conn)
    return _conn

def _init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        book TEXT NOT NULL DEFAULT '人教A版',
        book_no TEXT NOT NULL DEFAULT '必修二',
        chapter INTEGER NOT NULL DEFAULT 1,
        section INTEGER NOT NULL DEFAULT 3,
        topic TEXT NOT NULL DEFAULT '函数的单调性',
        lesson_type TEXT NOT NULL DEFAULT '新授课',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS mastery (
        chapter TEXT PRIMARY KEY,
        level TEXT NOT NULL DEFAULT '中' CHECK(level IN ('好','中','弱'))
    );
    CREATE TABLE IF NOT EXISTS obstacles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        chapter TEXT NOT NULL,
        symptom TEXT NOT NULL,
        cause TEXT NOT NULL,
        fix TEXT NOT NULL,
        evidence TEXT NOT NULL,
        confidence TEXT NOT NULL DEFAULT '高' CHECK(confidence IN ('高','中','低'))
    );
    CREATE TABLE IF NOT EXISTS feedbacks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        book_no TEXT, chapter INTEGER, section INTEGER,
        report TEXT NOT NULL,
        adopted TEXT NOT NULL DEFAULT '[]',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS simulation_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        book_no TEXT, chapter INTEGER, section INTEGER,
        student_level TEXT NOT NULL,
        level_switches TEXT NOT NULL DEFAULT '[]',
        persona TEXT NOT NULL DEFAULT '{}',
        transcript TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- 工程表：异步评估任务
    CREATE TABLE IF NOT EXISTS eval_tasks (
        task_id TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed')),
        progress INTEGER NOT NULL DEFAULT 0,
        stage TEXT NOT NULL DEFAULT '',
        result TEXT,            -- 完成时报告 JSON
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- 工程表：LLM 调用留痕（可追溯）
    CREATE TABLE IF NOT EXISTS traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage TEXT NOT NULL,          -- 流水线阶段 / 试讲轮次
        task_id TEXT,                 -- 关联评估任务（可为空）
        prompt_topic TEXT NOT NULL,   -- 本次调用的课题/任务主题
        obstacles_hit TEXT NOT NULL DEFAULT '[]',  -- 注入的障碍库条目 id/症状
        persona TEXT,                 -- 试讲人设 JSON（模拟逻辑留痕）
        output TEXT,                  -- 模型输出摘要
        mock INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- 工程表：调用量记账（成本护栏）
    CREATE TABLE IF NOT EXISTS usage_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL DEFAULT 'demo',   -- 演示版固定 demo，多用户时替换为 teacher_id
        stage TEXT NOT NULL,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- 模型配置（运行时切换，前端可改）：mode=builtin 用 .env 内置模型；custom 用用户自接
    CREATE TABLE IF NOT EXISTS model_config (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        mode TEXT NOT NULL DEFAULT 'builtin' CHECK(mode IN ('builtin','custom')),
        base_url TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        api_key TEXT NOT NULL DEFAULT ''
    );
    -- 教师画像（人格报告 + 个人档案，单行）：由教案评估历史 + 试讲对话确定性信号累积生成
    CREATE TABLE IF NOT EXISTS teacher_profile (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        lesson_count INTEGER NOT NULL DEFAULT 0,
        sim_count INTEGER NOT NULL DEFAULT 0,
        adopted_count INTEGER NOT NULL DEFAULT 0,
        adopted_top3 INTEGER NOT NULL DEFAULT 0,
        signals TEXT NOT NULL DEFAULT '{}',   -- 确定性行为信号 JSON（可解释）
        signals_hash TEXT NOT NULL DEFAULT '',
        report TEXT,                          -- 人格报告 JSON（达到阈值后才生成）
        needs_more INTEGER NOT NULL DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    _migrate_schema(conn)
    conn.commit()

def _migrate_schema(conn):
    """列迁移：旧库平滑升级。CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
    因此这里用 PRAGMA 检查，缺失的列单独 ALTER TABLE 补上。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feedbacks)").fetchall()}
    if "source_text" not in cols:
        conn.execute("ALTER TABLE feedbacks ADD COLUMN source_text TEXT NOT NULL DEFAULT ''")

def _seed_defaults(conn):
    conn.execute("INSERT OR IGNORE INTO settings(id) VALUES(1)")
    conn.execute("INSERT OR IGNORE INTO model_config(id) VALUES(1)")
    conn.execute("INSERT OR IGNORE INTO teacher_profile(id) VALUES(1)")
    # 认知障碍库 seed（幂等，按 symptom 去重）
    for o in OBSTACLES:
        conn.execute(
            "INSERT OR IGNORE INTO obstacles(topic, chapter, symptom, cause, fix, evidence, confidence) "
            "SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM obstacles WHERE symptom=?)",
            (o["topic"], o["chapter"], o["symptom"], o["cause"], o["fix"], o["evidence"], o["confidence"], o["symptom"]))
    default_mastery = [
        ("第1章 平面向量", "好"),
        ("第2章 三角恒等变换·基础", "中"),
        ("第2章 两角和与差公式", "弱"),
        ("第3章 正弦定理", "中"),
    ]
    for ch, lv in default_mastery:
        conn.execute("INSERT OR IGNORE INTO mastery(chapter, level) VALUES(?,?)", (ch, lv))
    conn.commit()

# ---------- 障碍库检索 ----------
def search_obstacles(keyword: str, limit: int = 8) -> list:
    """MVP：SQLite LIKE 关键词检索（复赛升级 Chroma + bge-m3）"""
    kw = f"%{keyword}%"
    rows = get_conn().execute(
        "SELECT * FROM obstacles WHERE topic LIKE ? OR chapter LIKE ? OR symptom LIKE ? OR cause LIKE ? ORDER BY id LIMIT ?",
        (kw, kw, kw, kw, limit)).fetchall()
    return [dict(r) for r in rows]

# ---------- 设置 / 掌握度 ----------
def get_settings() -> dict:
    row = get_conn().execute("SELECT * FROM settings WHERE id=1").fetchone()
    return dict(row) if row else {}

def save_settings(**kw):
    conn = get_conn()
    conn.execute(
        "UPDATE settings SET book=?, book_no=?, chapter=?, section=?, topic=?, lesson_type=? WHERE id=1",
        (kw.get("book", "人教A版"), kw.get("book_no", "必修二"),
         kw.get("chapter", 1), kw.get("section", 3),
         kw.get("topic", "函数的单调性"), kw.get("lesson_type", "新授课")))
    conn.commit()
    return get_settings()

def get_mastery() -> list:
    rows = get_conn().execute("SELECT chapter, level FROM mastery ORDER BY rowid").fetchall()
    return [dict(r) for r in rows]

def set_mastery(chapter: str, level: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO mastery(chapter, level) VALUES(?,?)", (chapter, level))
    conn.commit()

# ---------- 评估任务 ----------
def create_eval_task(task_id: str, topic: str) -> None:
    get_conn().execute("INSERT INTO eval_tasks(task_id, topic) VALUES(?,?)", (task_id, topic))
    get_conn().commit()

def update_eval_task(task_id: str, **kw) -> None:
    """安全更新：只允许列出的字段"""
    allowed = {"status", "progress", "stage", "result", "error"}
    sets, vals = [], []
    for k, v in kw.items():
        if k in allowed:
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(task_id)
    get_conn().execute(f"UPDATE eval_tasks SET {', '.join(sets)} WHERE task_id=?", vals)
    get_conn().commit()

def get_eval_task(task_id: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM eval_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    return d

# ---------- 反馈 / 试讲 ----------
def save_feedback(topic, report: dict, source_text: str = "") -> int:
    conn = get_conn()
    s = get_settings()
    cur = conn.execute(
        "INSERT INTO feedbacks(topic, book_no, chapter, section, report, source_text) VALUES(?,?,?,?,?,?)",
        (topic, s["book_no"], s["chapter"], s["section"], json.dumps(report, ensure_ascii=False), source_text))
    conn.commit()
    return cur.lastrowid

def update_feedback_adopted(fid: int, adopted: list):
    conn = get_conn()
    conn.execute("UPDATE feedbacks SET adopted=? WHERE id=?", (json.dumps(adopted, ensure_ascii=False), fid))
    conn.commit()

def save_sim_session(topic, level, switches, persona, transcript, summary) -> int:
    conn = get_conn()
    s = get_settings()
    cur = conn.execute(
        "INSERT INTO simulation_sessions(topic, book_no, chapter, section, student_level, level_switches, persona, transcript, summary) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (topic, s["book_no"], s["chapter"], s["section"], level,
         json.dumps(switches, ensure_ascii=False),
         json.dumps(persona, ensure_ascii=False),
         json.dumps(transcript, ensure_ascii=False),
         json.dumps(summary, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid

# ---------- 模型配置（运行时切换） ----------
def get_model_config() -> dict:
    row = get_conn().execute("SELECT * FROM model_config WHERE id=1").fetchone()
    return dict(row) if row else {"mode": "builtin", "base_url": "", "model": "", "api_key": ""}

def save_model_config(mode: str, base_url: str = "", model: str = "", api_key: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO model_config(id, mode, base_url, model, api_key) VALUES(1,?,?,?,?)",
        (mode, base_url, model, api_key))
    conn.commit()
    return get_model_config()

# ---------- 留痕 / 用量（工程表 DAO） ----------
def save_trace(stage: str, task_id: str | None, topic: str, obstacles_hit: list,
               output: str, mock: bool, latency_ms: int, persona: dict | None = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO traces(stage, task_id, prompt_topic, obstacles_hit, persona, output, mock, latency_ms) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (stage, task_id, topic, json.dumps(obstacles_hit, ensure_ascii=False),
         json.dumps(persona, ensure_ascii=False) if persona else None,
         output, 1 if mock else 0, latency_ms))
    conn.commit()
    return cur.lastrowid

def add_usage(stage: str, tokens_in: int, tokens_out: int, actor: str = "demo"):
    conn = get_conn()
    conn.execute("INSERT INTO usage_ledger(actor, stage, tokens_in, tokens_out) VALUES(?,?,?,?)",
                 (actor, stage, tokens_in, tokens_out))
    conn.commit()

def get_usage_summary() -> dict:
    """当月调用量统计（成本护栏展示）"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tin, COALESCE(SUM(tokens_out),0) AS tout "
        "FROM usage_ledger WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')").fetchone()
    return dict(row)

def recent_traces(limit: int = 20) -> list:
    rows = get_conn().execute(
        "SELECT stage, prompt_topic, obstacles_hit, mock, latency_ms, created_at FROM traces "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

# ---------- 教师画像（人格报告 / 个人档案） ----------
def get_profile() -> dict:
    row = get_conn().execute("SELECT * FROM teacher_profile WHERE id=1").fetchone()
    if not row:
        return {"lesson_count": 0, "sim_count": 0, "adopted_count": 0, "adopted_top3": 0,
                "signals": {}, "signals_hash": "", "report": None, "needs_more": 1}
    d = dict(row)
    try:
        d["signals"] = json.loads(d.get("signals") or "{}")
    except json.JSONDecodeError:
        d["signals"] = {}
    if d.get("report"):
        try:
            d["report"] = json.loads(d["report"])
        except json.JSONDecodeError:
            d["report"] = None
    else:
        d["report"] = None
    return d

def save_profile(lesson_count, sim_count, adopted_count, adopted_top3,
                 signals: dict, signals_hash: str, report: dict | None, needs_more: bool):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO teacher_profile(id) VALUES(1)")  # 幂等：确保单行存在
    conn.execute(
        "UPDATE teacher_profile SET lesson_count=?, sim_count=?, adopted_count=?, adopted_top3=?, "
        "signals=?, signals_hash=?, report=?, needs_more=?, updated_at=CURRENT_TIMESTAMP WHERE id=1",
        (lesson_count, sim_count, adopted_count, adopted_top3,
         json.dumps(signals, ensure_ascii=False), signals_hash,
         json.dumps(report, ensure_ascii=False) if report else None, 1 if needs_more else 0))
    conn.commit()
