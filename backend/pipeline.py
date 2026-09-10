"""备课导师 · 评估流水线（六层 P1-P6）+ 学生预判
可解释性机制在此落地：
  1. 证据链校验：LLM 返回的 quote 必须是教案原文子串（防幻觉硬闸）
  2. 置信度代码化：evidence.type 由 domain.confidence_for 判定，LLM 返回值被覆盖
  3. 每阶段产物写入 traces（可追溯，直接产出复赛合规材料）
"""
import asyncio
import json
import re
import time

from . import db, llm, domain, profile
from .knowledge import TEXTBOOK_TREE, textbook_excerpt

DIMENSIONS = [
    ("d1", "① 目标可测性"),
    ("d2", "② 重难点处理"),
    ("d3", "③ 例题适配"),
    ("d4", "④ 学生预判"),
    ("d5", "⑤ 课堂结构"),
]

# 阶段进度（供异步任务层更新进度条）
STAGES = [
    ("P1 解析教案", 8),
    ("P2 结构化抽取", 28),
    ("P3 上下文装配", 45),
    ("P4 五维评估", 72),
    ("P5 汇总仲裁", 86),
    ("P6 生成报告", 100),
]


def run_eval(topic: str, text: str, task_id: str | None = None, on_progress=None) -> dict:
    """执行完整六层流水线，返回报告 dict。on_progress(stage_name, percent) 可选。"""
    def _set(name, pct):
        if on_progress:
            on_progress(name, pct)

    _set("P1 解析教案", 8)
    obstacles = db.search_obstacles(topic, limit=5)
    mastery = db.get_mastery()
    last_adopted = _last_adopted(topic)          # 采纳闭环上下文

    _set("P2 结构化抽取", 28)
    structured = _structure(topic, text, task_id, obstacles)   # {objectives, difficulties, examples, process, parse_mode}

    _set("P3 上下文装配", 45)
    context = {
        "topic": topic,
        "obstacles": obstacles,
        "mastery": mastery,
        "last_adopted": last_adopted,
        "book_no": db.get_settings()["book_no"],
        "textbook": textbook_excerpt(topic),          # 教材原文（编辑文件+重启即导入）
    }

    _set("P4 五维评估", 72)
    dims = _eval_five(topic, text, structured, context, task_id)

    _set("P5 汇总仲裁", 86)
    top3 = _arbitrate(topic, text, structured, dims, obstacles, task_id)

    _set("P6 生成报告", 100)
    report = {
        "summary": _make_summary(topic, structured, top3, obstacles),
        "top3": top3,
        "dims": dims,
        "parse_mode": structured.get("parse_mode", "structured"),
        "obstacle_hits": len(obstacles),
        "mock": llm.MOCK_MODE,
    }
    db.save_feedback(topic, report, source_text=text)
    profile.update_profile()          # 教师画像：教案评估历史累积
    return report


def _last_adopted(topic: str) -> list:
    """采纳闭环：该课题最近一次反馈里被采纳的建议"""
    rows = db.get_conn().execute(
        "SELECT report FROM feedbacks WHERE topic=? ORDER BY id DESC LIMIT 1", (topic,)).fetchall()
    if not rows:
        return []
    try:
        report = json.loads(rows[0]["report"])
    except (json.JSONDecodeError, TypeError):
        return []
    return [t.get("title") for t in report.get("top3", [])]


# ==================== P2 结构化 ====================
_DIFF_PATTERN = re.compile(r"难点[:：]\s*([^\n]+)")
_EXAMPLE_PATTERN = re.compile(r"例题\s*\d*[:：]?\s*([^\n]+)")

def _structure(topic, text, task_id, obstacles) -> dict:
    """P2：抽取目标/难点/例题。LLM 模式走 chat_json；mock 用规则正则（契约同构）。"""
    if llm.MOCK_MODE:
        objectives = re.findall(r"\d[.、)）]\s*([^\n]+)", _obj_pattern(text))
        diff = _DIFF_PATTERN.search(text)
        examples = _EXAMPLE_PATTERN.findall(text)
        structured = {
            "objectives": objectives[:6],
            "difficulties": [diff.group(1)] if diff else [],
            "examples": [e.strip()[:40] for e in examples][:6],
            "parse_mode": "structured" if (objectives or diff or examples) else "fallback_generic",
        }
        db.save_trace("P2_structure", task_id, topic, [o["symptom"] for o in obstacles],
                      f"objectives={len(objectives)}, examples={len(examples)}", True, 0)
        return structured
    sys = ("你是教案解析助手。将教案文本解析为 JSON，字段缺失填 null，不要编造。"
           '格式：{"objectives": ["..."], "difficulties": ["..."], "examples": ["..."], "parse_mode": "structured"}')
    try:
        out, ms = llm.latency_of(lambda: llm.chat_json(sys, f"课题：{topic}\n\n教案：\n{text[:6000]}"))
        obj = [o for o in (out.get("objectives") or []) if isinstance(o, str)][:6]
        diff = [d for d in (out.get("difficulties") or []) if isinstance(d, str)][:3]
        ex = [e for e in (out.get("examples") or []) if isinstance(e, str)][:6]
        db.save_trace("P2_structure", task_id, topic, [o["symptom"] for o in obstacles],
                      json.dumps(out, ensure_ascii=False)[:300], False, ms)
        return {"objectives": obj, "difficulties": diff, "examples": ex,
                "parse_mode": "structured" if (obj or diff or ex) else "fallback_generic"}
    except llm.LLMError:
        return {"objectives": [], "difficulties": [], "examples": [], "parse_mode": "fallback_generic"}

def _obj_pattern(text):
    m = re.search(r"教学目标[:：]\s*([\s\S]{0,500})", text)
    return m.group(1) if m else ""


# ==================== P4 五维并联 ====================
_DIM_PROMPTS = {
    "d1": ("① 教学目标可测性", "评判目标是否含可观测行为动词与达标标准，是否可验收。"),
    "d2": ("② 重难点处理", "评判重难点是否有学生可操作的任务/脚手架/辨析设问，而非仅教师讲解。"),
    "d3": ("③ 例题适配", "评判例题与目标对应关系、难度梯度、是否缺变式。"),
    "d4": ("④ 学生预判", "结合认知障碍库判断学生会卡在哪、典型错答与应对讲法。"),
    "d5": ("⑤ 课堂结构", "评判时间分配（导入/新授/练习/检测/小结）与环节闭环。"),
}

def _clip(text, max_len=120):
    return text if len(text) <= max_len else text[:max_len] + "…"

def _dim_prompt(dim_id, dim_name, text, structured, context):
    return (
        "你是资深数学教研员，基于证据审查教案。只输出 JSON："
        '{"issues": [{"t": 问题简述, "q": 教案原文引用(必须逐字来自教案), "f": 可操作改法}]}。'
        "引用必须是教案原文的连续片段，禁止改写或编造。"
        f"\n维度：{_DIM_PROMPTS[dim_id][0]}。审查要点：{_DIM_PROMPTS[dim_id][1]}"
        f"\n课题：{context['topic']}；结构化抽取：目标={structured.get('objectives')}，"
        f"难点={structured.get('difficulties')}，例题={structured.get('examples')}"
        f"\n本班掌握度：{', '.join(m['chapter'] + '[' + m['level'] + ']' for m in context['mastery']) or '未设置'}"
        f"\n认知障碍库命中：{', '.join(o['symptom'] for o in context['obstacles']) or '无'}"
        f"\n教材原文（供对照知识点，勿改引用于教案引用 q）：{_clip(context.get('textbook', ''))}"
        f"\n上次已采纳建议：{'、'.join(context['last_adopted']) or '无'}"
    )

_MOCK_DIMS = {
    "d1": {"name": "① 目标可测性", "issues": [
        {"t": "目标2 同样缺少达标标准", "q": "掌握用定义判断单调性的方法",
         "f": "补达标标准：能独立完成 1 道定义法证明题，且步骤完整。",
         "evidence": domain.evidence_meta("generic")},
        {"t": "建议：目标按「行为动词 + 内容 + 达标」统一改写", "q": "教学目标",
         "f": "示例：「学生能依据定义判断具体函数的单调性，独立完成 4 题对 3 题」。",
         "evidence": domain.evidence_meta("generic")},
    ]},
    "d2": {"name": "② 重难点处理", "issues": [
        {"t": "难点突破依赖教师讲解，缺少学生可操作的任务", "q": "用定义法证明函数的单调性",
         "f": "将证明拆为「定区间 → 作差 → 变形 → 判号」四步填空任务。",
         "evidence": domain.evidence_meta("generic")},
        {"t": "未覆盖认知障碍「任意性理解不足」", "q": "单调性定义",
         "f": "增加一次辨析设问：「只取两个点判断可以吗？」引发讨论。",
         "evidence": domain.evidence_meta("generic")},
    ]},
    "d3": {"name": "③ 例题适配", "issues": [
        {"t": "例题均对应目标 2，目标 3（单调区间）无例题覆盖", "q": "目标",
         "f": "补一道「指出 f(x)=|x| 的单调区间」例题。",
         "evidence": domain.evidence_meta("generic")},
        {"t": "缺少变式题", "q": "例题",
         "f": "可加变式：将定义域改为 (1,+∞)，结论变化引发思考。",
         "evidence": domain.evidence_meta("generic")},
    ]},
    "d4": {"name": "④ 学生预判", "issues": [
        {"t": "对定义中「任意 x1<x2」缺乏辨析，把验证当成举例", "q": "单调性定义",
         "f": "导入加一次辨析设问：只取两个点判断可以吗？",
         "evidence": domain.evidence_meta("generic")},
        {"t": "证明题不作差变形，直接说「不会」", "q": "定义法证明",
         "f": "弱档先给填空式变形提示卡",
         "evidence": domain.evidence_meta("generic")},
    ]},
    "d5": {"name": "⑤ 课堂结构", "issues": [
        {"t": "新授时间占比不足（预计 38%）", "q": "教学过程",
         "f": "压缩导入至 5 分钟，新授提到 ≥50%。",
         "evidence": domain.evidence_meta("generic")},
        {"t": "缺少目标达成检测环节", "q": "无检测",
         "f": "小结前加 2 道限时小测（含 1 道变式）。",
         "evidence": domain.evidence_meta("generic")},
    ]},
}

def _eval_five(topic, text, structured, context, task_id) -> dict:
    """P4：五个维度并联。mock 返回内置（d4 注入障碍库命中，置信度代码化）；
    真实模式 asyncio 并发 5 个 chat_json，失败维度标记 degraded 不拖垮整体。"""
    if llm.MOCK_MODE:
        dims = json.loads(json.dumps(_MOCK_DIMS, ensure_ascii=False))
        if context["obstacles"]:
            issues = []
            for ob in context["obstacles"][:3]:
                issues.append({
                    "t": f"卡点：{ob['symptom'][:18]}（{ob['confidence']}置信）",
                    "q": "认知障碍库命中",
                    "f": ob["fix"],
                    "evidence": domain.evidence_meta("obstacle_hit", ob["confidence"]),
                })
            dims["d4"] = {"name": "④ 学生预判", "issues": issues}
        db.save_trace("P4_eval", task_id, topic, [o["symptom"] for o in context["obstacles"]],
                      f"5 dims, d4 hit={len(context['obstacles'])}", True, 0)
        return dims

    async def one(dim_id, dim_name):
        sys = _dim_prompt(dim_id, dim_name, text, structured, context)
        try:
            # 同步 httpx 调用会阻塞事件循环 → 放到线程池实现真并发（5 维同时请求）
            out, ms = await asyncio.to_thread(
                lambda: llm.latency_of(lambda: llm.chat_json(sys, f"课题：{topic}\n\n教案：\n{text[:6000]}", 0.3)))
            issues = out.get("issues") or []
            for i in issues:                       # 证据链 + 置信度代码化
                i["evidence"] = domain.evidence_meta("generic")
                if not domain.verify_quote(i.get("q", ""), text):
                    i["quote_verified"] = False
            db.save_trace(f"P4_{dim_id}", task_id, topic, [], json.dumps(issues, ensure_ascii=False)[:300], False, ms)
            return dim_id, {"name": dim_name, "issues": issues}
        except llm.LLMError:
            return dim_id, {"name": dim_name, "issues": [], "degraded": True}

    async def _gather_all():                       # coroutine 创建必须发生在事件循环建立之后
        return await asyncio.gather(*[one(did, dname) for did, dname in DIMENSIONS])

    results = asyncio.run(_gather_all())
    return {did: r for did, r in results}


# ==================== P5 汇总仲裁 ====================
_MOCK_TOP3 = [
    {"dim": "① 教学目标可测性", "title": "教学目标缺少可观测行为，难以判断「是否达成」",
     "quote": "理解函数单调性的概念",
     "fix": "补可观测行为：学生能独立画出给定函数的单调区间并用定义式验证；达标标准：4 题对 3 题。",
     "evidence": domain.evidence_meta("generic"),
     "arbitration_reason": "目标不可测会让整节课「无法验收」，影响面最大"},
    {"dim": "② 重难点处理", "title": "难点「定义法证明单调性」缺少脚手架，直接跳到完整证明",
     "quote": "难点：用定义法证明函数的单调性",
     "fix": "拆三步台阶：定区间取两点 → 作差变形 → 判号下结论。先做一步一个问题的「填空式证明」，再放手独立完成。",
     "evidence": domain.evidence_meta("generic"),
     "arbitration_reason": "难点无脚手架是弱档学生当场放弃的头号原因"},
    {"dim": "④ 学生预判", "title": "卡点：对定义中「任意 x1<x2」缺乏辨析，把验证当成举例",
     "quote": "单调性定义",
     "fix": "导入加一次辨析设问：只取两个点判断可以吗？",
     "evidence": domain.evidence_meta("obstacle_hit", "高"),
     "arbitration_reason": "障碍库高置信命中，本班第2章掌握度「弱」应优先应对"},
]

def _arbitrate(topic, text, structured, dims, obstacles, task_id) -> list:
    """P5：去重 → 消解冲突 → Top3。mock 返回内置；真实模式 LLM 仲裁 + 证据链硬闸。"""
    if llm.MOCK_MODE:
        db.save_trace("P5_arbitrate", task_id, topic, [o["symptom"] for o in obstacles],
                      "top3 mock", True, 0)
        return json.loads(json.dumps(_MOCK_TOP3, ensure_ascii=False))
    flat = []
    for did, d in dims.items():
        for iss in d.get("issues", []):
            flat.append({"dim": d["name"], "title": iss.get("t", ""),
                         "quote": iss.get("q", ""), "fix": iss.get("f", ""),
                         "evidence": iss.get("evidence")})
    sys = ("你是教案反馈汇总负责人。下面是各维度的候选问题。任务：去重、消解冲突、"
           "选出最该改的 3 处，每条附一句理由（按对教学目标达成的影响排序）。"
           "只输出 JSON：{\"top3\": [{\"dim\": 维度名, \"title\": 问题, \"quote\": 原文引用, "
           "\"fix\": 可操作改法, \"arbitration_reason\": 理由}]}")
    try:
        out, ms = llm.latency_of(lambda: llm.chat_json(
            sys, json.dumps({"candidates": flat}, ensure_ascii=False), 0.3))
        top3 = (out.get("top3") or [])[:3]
        kept = []
        for t in top3:                              # 证据链硬闸：quote 必须原文命中
            if domain.verify_quote(t.get("quote", ""), text):
                t["evidence"] = t.get("evidence") or domain.evidence_meta("generic")
                kept.append(t)
        db.save_trace("P5_arbitrate", task_id, topic, [], json.dumps(kept, ensure_ascii=False)[:300], False, ms)
        return kept or json.loads(json.dumps(_MOCK_TOP3, ensure_ascii=False))
    except llm.LLMError:
        return json.loads(json.dumps(_MOCK_TOP3, ensure_ascii=False))


def _make_summary(topic, structured, top3, obstacles) -> str:
    if obstacles:
        first = obstacles[0]["symptom"][:20]
        return (f"总体来看，这份教案结构完整、例题贴合目标。最值得优先处理的三处已用红笔标出："
                f"目标表述偏抽象、难点突破缺少脚手架、例题梯度跳跃。学生预判维度提示："
                f"「{first}」是本班弱档学生的高频卡点，建议在导入环节增加一次辨析设问。")
    return ("总体来看，这份教案结构完整、例题贴合目标。最值得优先处理的三处已用红笔标出："
            "目标表述偏抽象、难点突破缺少脚手架、例题梯度跳跃。")


# ==================== 学生预判 ====================
_CONF_RANK = {"高": 3, "中": 2, "低": 1}

def predict(topic: str) -> dict:
    """预判卡点。置信度一律由 domain 代码判定；班级掌握度仅影响排序（弱信号）。"""
    obstacles = db.search_obstacles(topic, limit=5)
    mastery = {m["chapter"]: m["level"] for m in db.get_mastery()}
    weak_hit = _weak_chapter_of(obstacles, mastery)

    if llm.MOCK_MODE:
        cards = _mock_cards(obstacles, weak_hit)
    else:
        cards = _llm_cards(topic, obstacles, mastery, weak_hit)
    cards.sort(key=lambda c: _CONF_RANK.get(c.get("confidence", "低"), 0), reverse=True)
    return {"cards": cards[:5], "mock": llm.MOCK_MODE, "weak_hit": weak_hit}


def _weak_chapter_of(obstacles, mastery) -> bool:
    """障碍库命中条目是否落在掌握度「弱」的章节（仅影响排序，不抬置信度）"""
    weak_names = {k for k, v in mastery.items() if v == "弱"}
    for ob in obstacles:
        for wn in weak_names:
            if wn.split("·")[0] in ob["chapter"] or ob["chapter"] in wn:
                return True
    return False


def _mock_cards(obstacles, weak_hit) -> list:
    cards = []
    for ob in obstacles[:5]:
        cards.append({
            "step": ob["symptom"],
            "wrong": f"典型错答：「{ob['symptom']}」",
            "cause": ob["cause"],
            "fix": ob["fix"],
            "confidence": domain.confidence_for("obstacle_hit", ob["confidence"]),
            "dim": "认知障碍库命中",
            "evidence": domain.evidence_meta("obstacle_hit", ob["confidence"]),
        })
    fallbacks = [
        {"step": "定义法证明无思路，不会作差变形", "wrong": "「不会」或直接卡住",
         "cause": "变形技巧储备不足", "fix": "弱档先给填空式变形提示",
         "confidence": "中", "dim": "通用教学经验",
         "evidence": domain.evidence_meta("generic")},
        {"step": "不区分「单调区间」与「在区间上单调」", "wrong": "两者混用",
         "cause": "概念边界不清", "fix": "用 |x| 图像做对比辨析",
         "confidence": "中", "dim": "通用教学经验",
         "evidence": domain.evidence_meta("generic")},
        {"step": "证明题格式不规范（忘了设 x1<x2）", "wrong": "直接写变形，缺四步",
         "cause": "证明框架不熟", "fix": "提供四步模板",
         "confidence": "低", "dim": "仅推测",
         "evidence": domain.evidence_meta("speculation")},
    ]
    for fb in fallbacks:
        if len(cards) >= 5:
            break
        cards.append(fb)
    return cards


def _llm_cards(topic, obstacles, mastery, weak_hit) -> list:
    ob_txt = "\n".join(f"- {o['symptom']}（{o['cause']}；{o['fix']}；置信度{o['confidence']}）" for o in obstacles) or "（无）"
    m_txt = "、".join(f"{k}掌握度[{v}]" for k, v in mastery.items()) or "（未设置）"
    sys = ("你是资深数学教师。根据认知障碍库与本班掌握度，预判下节课学生可能卡住的 3-5 个点。"
           "只输出 JSON：{\"cards\": [{\"step\": 卡点, \"wrong\": 典型错答, \"cause\": 错因, "
           "\"fix\": 建议教法, \"dim\": 来源}]}。注意：卡点描述引用要具体；不要给置信度（系统判定）。")
    try:
        tbk = textbook_excerpt(topic, 2000)
        out, ms = llm.latency_of(lambda: llm.chat_json(
            sys, f"课题：{topic}\n障碍库：\n{ob_txt}\n\n本班掌握度：{m_txt}"
                 f"\n教材原文（备查）：\n{tbk or '（未导入）'}", 0.4))
        cards = []
        for c in (out.get("cards") or [])[:5]:
            c["confidence"] = domain.confidence_for("obstacle_hit")   # 命中障碍库即高，代码判定
            c["evidence"] = domain.evidence_meta("obstacle_hit", "高")
            cards.append(c)
        db.save_trace("predict", None, topic, [o["symptom"] for o in obstacles],
                      json.dumps(cards, ensure_ascii=False)[:300], False, ms)
        return cards
    except llm.LLMError:
        return _mock_cards(obstacles, weak_hit)
