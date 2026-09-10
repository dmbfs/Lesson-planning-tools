"""备课导师 · FastAPI 入口：API + 异步评估 + 静态托管"""
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, parser, pipeline, simulate, tasks, llm, domain, profile
from .config import FRONTEND_DIR
from .knowledge import TEXTBOOK_TREE

app = FastAPI(title="备课导师 API", version="0.2")

# ---------- 请求模型 ----------
class LoginReq(BaseModel):
    phone: str
    code: str

class EvaluateReq(BaseModel):
    text: str
    topic: str = ""
    lesson_type: str = "新授课"

class PredictReq(BaseModel):
    topic: str

class ChatReq(BaseModel):
    topic: str
    level: str = "中"
    history: list = []

class SummaryReq(BaseModel):
    topic: str
    level: str = "中"
    history: list = []
    level_switches: list = []

class AdoptReq(BaseModel):
    feedback_id: int
    adopted: list = []

class SettingsReq(BaseModel):
    book: str = "人教A版"
    book_no: str = "必修二"
    chapter: int = 1
    section: int = 3
    topic: str = "函数的单调性"
    lesson_type: str = "新授课"

class MasteryReq(BaseModel):
    chapter: str
    level: str

class ModelReq(BaseModel):
    mode: str = "builtin"            # builtin 内置（tokendance）/ custom 自接
    base_url: str = ""
    model: str = ""
    api_key: str = ""

# ---------- 状态 / 登录 ----------
@app.get("/api/status")
def status():
    return {"ok": True, "mock": llm.MOCK_MODE,
            "model": llm.LLM_MODEL,
            "msg": "MOCK 模式：未配置 LLM_API_KEY，评估/试讲返回内置示例" if llm.MOCK_MODE
                   else f"真实模型模式：{llm.LLM_MODEL}"}

@app.post("/api/login")
def login(req: LoginReq):
    if not req.phone or not req.code:
        raise HTTPException(400, "手机号与验证码必填")
    return {"token": "demo-token", "name": "林老师", "phone": req.phone}

# ---------- 教案反馈（异步评估） ----------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    try:
        parsed = parser.parse_file(file.filename or "upload.txt", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"text": parsed["text"], "topic": parsed["topic"], "filename": parsed["filename"]}

@app.post("/api/evaluate")
def evaluate_api(req: EvaluateReq):
    """创建异步评估任务。课型校验在前端/此处做白名单判定（PRD 红线）。"""
    gt = domain.check_lesson_type(req.lesson_type)
    if not gt["ok"]:
        raise HTTPException(400, gt["reason"])
    topic = req.topic or db.get_settings()["topic"]
    task_id = tasks.create_task(topic, req.text)
    return {"task_id": task_id, "topic": topic}

@app.get("/api/evaluate/{task_id}")
def evaluate_status(task_id: str):
    t = tasks.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t

@app.post("/api/adopt")
def adopt(req: AdoptReq):
    db.update_feedback_adopted(req.feedback_id, req.adopted)
    return {"ok": True}

# ---------- 学生预判 ----------
@app.post("/api/predict")
def predict_api(req: PredictReq):
    return pipeline.predict(req.topic or db.get_settings()["topic"])

# ---------- 模拟试讲 ----------
@app.post("/api/sim/chat")
def sim_chat(req: ChatReq):
    return simulate.chat_reply(req.topic, req.level, req.history)

@app.post("/api/sim/summary")
def sim_summary(req: SummaryReq):
    return simulate.make_summary(req.topic, req.level, req.history, req.level_switches)

# ---------- 教材目录 / 班级设置 ----------
@app.get("/api/textbook")
def textbook_tree():
    """返回内置知识库的教材目录树（必修一/二 → 章节），供前端「进度设置」动态下拉。"""
    return {"books": TEXTBOOK_TREE}

@app.get("/api/settings")
def get_settings():
    s = db.get_settings()
    s["mastery"] = db.get_mastery()
    s["textbook"] = TEXTBOOK_TREE
    return s

@app.post("/api/settings")
def save_settings(req: SettingsReq):
    return db.save_settings(**req.model_dump())

@app.post("/api/mastery")
def set_mastery(req: MasteryReq):
    db.set_mastery(req.chapter, req.level)
    return {"ok": True}

# ---------- 模型配置（内置 tokendance / 用户自接，运行时切换） ----------
@app.get("/api/model")
def get_model():
    """返回当前模型配置（key 脱敏，不回传完整 key）"""
    mc = db.get_model_config()
    return {
        "mode": mc["mode"],
        "base_url": mc["base_url"],
        "model": mc["model"],
        "has_key": bool(mc["api_key"]),
        "key_masked": (mc["api_key"][:6] + "****" + mc["api_key"][-4:]) if mc["api_key"] else "",
        "builtin_base_url": llm.LLM_BASE_URL,
        "builtin_model": llm.LLM_MODEL,
        "mock": llm.is_mock(),
    }

@app.post("/api/model")
def save_model(req: ModelReq):
    """保存模型配置。mode=builtin 时忽略自定义三项；mode=custom 时校验并保存。"""
    if req.mode not in ("builtin", "custom"):
        raise HTTPException(400, "mode 必须为 builtin 或 custom")
    if req.mode == "custom":
        if not req.base_url.strip() or not req.model.strip():
            raise HTTPException(400, "自接模型必须填写 Base URL 与模型名")
        if not req.api_key.strip():
            raise HTTPException(400, "自接模型必须填写 API Key")
    db.save_model_config(req.mode, req.base_url.strip(), req.model.strip(), req.api_key.strip())
    return {"ok": True, "mode": req.mode, "mock": llm.is_mock()}

@app.post("/api/model/test")
def test_model(req: ModelReq):
    """用给定配置发一次最小请求验证连通性（不保存）"""
    if not req.base_url.strip() or not req.model.strip():
        raise HTTPException(400, "请先填写 Base URL 与模型名")
    return llm.test_connection(req.api_key.strip(), req.base_url.strip(), req.model.strip())

# ---------- 教师画像（人格报告 / 个人档案） ----------
@app.get("/api/profile")
def get_profile():
    """返回画像：数据积累 + 风格画像 + 带证据的洞察。数据不足时需多视角提示。"""
    p = db.get_profile()
    report = p.get("report")
    stale = False
    if report:
        db_ = report.get("data_base") or {}
        stale = (p.get("lesson_count", 0) > db_.get("lesson_count", 0)
                 or p.get("sim_count", 0) > db_.get("sim_count", 0))
    return {
        "lesson_count": p.get("lesson_count", 0),
        "sim_count": p.get("sim_count", 0),
        "adopted_rate": (p.get("signals") or {}).get("adopted_rate", 0.0),
        "needs_more": bool(p.get("needs_more", 1)),
        "needed": {"lesson": profile.THRESHOLD["lesson"], "sim": profile.THRESHOLD["sim"]},
        "report": report,
        "stale": stale,
        "mock": llm.MOCK_MODE,
    }

@app.post("/api/profile/refresh")
def refresh_profile():
    """强制重新合成人格报告（数据足够时）"""
    p = profile.update_profile(force=True)
    if p.get("needs_more"):
        raise HTTPException(400, "样本不足，暂无法生成完整画像")
    return {"ok": True, "mock": llm.MOCK_MODE}

# ---------- 留痕 / 用量（可追溯 & 成本护栏） ----------
@app.get("/api/traces")
def traces(limit: int = 20):
    return {"mock": llm.MOCK_MODE, "traces": db.recent_traces(limit)}

@app.get("/api/usage")
def usage():
    return db.get_usage_summary()

# ---------- 静态托管（前端） ----------
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
