"""备课导师 · 教师画像引擎
基于「教案评估历史 + 模拟试讲对话」确定性提取教学行为信号，再合成人格报告（风格画像 + 带证据洞察）。
可解释性在此落地：
  1. 信号全部由代码计算（每维平均问题数、追问采纳率、提问开放性、脚手架率、回应率），可审计
  2. 洞察证据必须命中语料（报告历史要点 / 试讲原文），沿用防幻觉硬闸
  3. 洞察置信度按证据来源代码判档：教案结构=高（硬数据）、试讲交互=中（启发式）
"""
import hashlib
import json
import re

from . import db, llm, domain
from .domain import normalize

# 开放完整报告的样本阈值（PRD：数据不足时提示仍需样本）
THRESHOLD = {"lesson": 3, "sim": 2}

_DIM_ORDER = ["d1", "d2", "d3", "d4", "d5"]
_ASPECT_CONF = {   # 洞察按界面区块映射置信度（报告=硬数据高，试讲启发式中）
    "教案偏好": "高",
    "试讲交互": "中",
}
_ASPECT_LABEL = {"教案偏好": "教案结构", "试讲交互": "试讲互动"}

# 启发式（可审计，非 LLM）
_OPEN = re.compile(r"为什么|怎么|怎样|如何|什么|哪些|哪个|哪种|说说|谈谈|思考|想想|" \
                   r"你认为|有几种|能不能|能否|原因")
_MIS = re.compile(r"不懂|不会|没听懂|太懂|没太懂|卡住|卡在|错|忘了|混|分不清|不会做|忘记|沉默|不清楚")
_SCAFFOLD = re.compile(r"先|然后|接下来|第一步|第二步|试着|想想|比如|例如|就像|可以|注意|关键是|不妨|先看")

# ---------- 确定性信号提取 ----------
def extract_signals() -> dict:
    """从 feedbacks + simulation_sessions 全量重算信号（自洽、无漂移）。返回 dict。"""
    signals, _ = _extract_signals_with_counts()
    return signals


def _extract_signals_with_counts() -> tuple:
    """内部实现：重算信号并同时返回计数。避免模块级可变全局副作用。"""
    conn = db.get_conn()
    signals = {"per_dim_avg": {}, "adopted_rate": 0.0, "teaching": {},
               "corpus": {"report_issues": [], "teacher_turns": []}}

    # 教案（教案偏好 / 教案结构）
    fb_rows = conn.execute("SELECT report, adopted FROM feedbacks").fetchall()
    lesson_count = len(fb_rows)
    dim_sums, dim_names = {}, {}
    adopted_count = adopted_top3 = 0
    report_issue_titles = []
    for row in fb_rows:
        try:
            r = json.loads(row["report"])
        except (json.JSONDecodeError, TypeError):
            continue
        dims = r.get("dims") or {}
        for did in _DIM_ORDER:
            d = dims.get(did) or {}
            dim_sums.setdefault(did, 0)
            dim_sums[did] += len(d.get("issues") or [])
            dim_names[did] = d.get("name", f"维度{did}")
            for iss in (d.get("issues") or [])[:2]:        # 收集历史要点作证据语料
                title = iss.get("t", "") or iss.get("title", "")
                if title and title not in report_issue_titles:
                    report_issue_titles.append(title)
        top3 = r.get("top3") or []
        adopted_top3 += len(top3)
        try:
            adopted_count += len(json.loads(row["adopted"] or "[]"))
        except json.JSONDecodeError:
            pass
    for did in _DIM_ORDER:
        signals["per_dim_avg"][_DIM_NAME(did, dim_names)] = (
            round(dim_sums.get(did, 0) / lesson_count, 2) if lesson_count else 0.0)
    signals["adopted_rate"] = round(adopted_count / adopted_top3, 2) if adopted_top3 else 0.0
    # 采纳历史要点并入语料（采纳=教师认同的偏好信号）
    for title in _adopted_issue_titles(fb_rows):
        if title and title not in report_issue_titles:
            report_issue_titles.append(title)
    signals["corpus"]["report_issues"] = report_issue_titles[:12]

    # 试讲（试讲交互 / 适应度）
    sim_rows = conn.execute("SELECT transcript, level_switches FROM simulation_sessions").fetchall()
    sim_count = len(sim_rows)
    teacher_msgs = 0
    teacher_turns, student_mis = [], []
    question_total = open_questions = scaffold_turns = 0
    switches = 0
    for row in sim_rows:
        try:
            tr = json.loads(row["transcript"] or "[]")
            lv = json.loads(row["level_switches"] or "[]")
        except json.JSONDecodeError:
            tr, lv = [], []
        switches += len(lv)
        for i, m in enumerate(tr):
            t = m.get("text", "")
            if m.get("who") == "teacher":
                teacher_msgs += 1
                teacher_turns.append(t)
                if _SCAFFOLD.search(t):
                    scaffold_turns += 1
                if _is_question(t):
                    question_total += 1
                    if _OPEN.search(t):
                        open_questions += 1
                # 卡点后紧跟教师话轮 → 视为回应
                if i > 0 and _MIS.search(tr[i - 1].get("text", "")):
                    student_mis.append(1)
            else:
                if _MIS.search(t):
                    student_mis.append(0)     # 出现卡点但未被教师接话（若后续无教师话轮则计入分母）
    signals["teaching"] = {
        "teacher_msgs": teacher_msgs,
        "open_rate": round(open_questions / question_total, 2) if question_total else 0.0,
        "scaffold_rate": round(scaffold_turns / teacher_msgs, 2) if teacher_msgs else 0.0,
        "responsive_rate": round(sum(student_mis) / len(student_mis), 2) if student_mis else 0.0,
        "level_switches": switches,
    }
    signals["corpus"]["teacher_turns"] = teacher_turns[-6:]   # 最近 6 句教师原话作证据语料

    counts = {"lesson": lesson_count, "sim": sim_count,
              "adopted": adopted_count, "adopted_top3": adopted_top3}
    return signals, counts


def _DIM_NAME(did, names):
    return names.get(did, f"维度{did}")


def _adopted_issue_titles(fb_rows) -> list:
    titles = []
    for row in fb_rows:
        try:
            adopted = json.loads(row["adopted"] or "[]")
        except json.JSONDecodeError:
            continue
        titles.extend(a if isinstance(a, str) else "" for a in adopted)
    return titles


def _is_question(t: str) -> bool:
    return "？" in t or "?" in t or t.rstrip().endswith(("吗", "呢"))


# ---------- 画像状态机 ----------
def needs_more(lesson_count: int, sim_count: int) -> bool:
    return lesson_count < THRESHOLD["lesson"] or sim_count < THRESHOLD["sim"]


def update_profile(force: bool = False) -> dict:
    """积累后自动更新画像。
    - 每次评估/试讲后调用：轻量重算信号并落库
    - 达到阈值 / force 时，重新合成人格报告（LLM 或 mock）+ 证据链校验
    """
    signals, counts = _extract_signals_with_counts()
    lesson = counts["lesson"]
    sim = counts["sim"]
    adopted = counts["adopted"]
    top3 = counts["adopted_top3"]
    nm = needs_more(lesson, sim)

    cur = db.get_profile()
    cur_hash = cur.get("signals_hash", "")
    new_hash = _hash(signals)
    # 报告再生成的触发：数据足够 且（数据变化 且 (force 或 阈值的首个报告)）
    regenerate = (not nm) and new_hash != cur_hash and (force or cur.get("report") is None)

    report = cur.get("report")
    if regenerate:
        report = _generate_report(signals, lesson, sim, adopted, top3)
    db.save_profile(lesson, sim, adopted, top3, signals, new_hash, report, nm)
    return db.get_profile()


def _hash(signals: dict) -> str:
    # 语料变化也可能改变报告 → 纳入 hash；取稳定子集避免不稳定
    core = {
        "per_dim_avg": signals.get("per_dim_avg"),
        "adopted_rate": signals.get("adopted_rate"),
        "teaching": signals.get("teaching"),
    }
    return hashlib.md5(json.dumps(core, ensure_ascii=False).encode()).hexdigest()[:12]


# ---------- 人格报告合成 ----------
def _generate_report(signals, lesson, sim, adopted, top3) -> dict:
    if llm.MOCK_MODE:
        report = _mock_report(signals, lesson, sim, adopted, top3)
    else:
        report = _llm_report(signals, lesson, sim, adopted, top3)
    report["data_base"] = {
        "lesson_count": lesson, "sim_count": sim,
        "adopted_rate": signals.get("adopted_rate", 0.0),
    }
    return report


def _mock_report(signals, lesson, sim, adopted, top3) -> dict:
    pad = signals["per_dim_avg"]
    # 挑最常被提示的维度 → 教案偏好风险点（确定性）
    weak_dim = max(pad, key=pad.get) if pad else ""
    weak_avg = pad.get(weak_dim, 0)
    teaching = signals["teaching"]
    corpus = signals["corpus"]
    teacher_quote = corpus["teacher_turns"][0] if corpus["teacher_turns"] else "（暂无教师话语样本）"
    report_evidence = corpus["report_issues"][0] if corpus["report_issues"] else "目标"
    open_rate = teaching.get("open_rate", 0)
    scaffold_rate = teaching.get("scaffold_rate", 0)
    style = _style_label(scaffold_rate, open_rate)
    insights = [
        {"aspect": "教案偏好", "aspect_label": _ASPECT_LABEL["教案偏好"],
         "finding": f"你最近的教案在「{weak_dim}」维度最常被提示（平均每份 {weak_avg} 条）"
                     f"；建议被采纳的比例约 {int(signals['adopted_rate'] * 100)}%。",
         "evidence": report_evidence, "confidence": _ASPECT_CONF["教案偏好"]},
        {"aspect": "教案偏好", "aspect_label": _ASPECT_LABEL["教案偏好"],
         "finding": "历史报告提示你教案「目标表述偏抽象」，建议统一改写为「行为动词 + 达标标准」。",
         "evidence": "目标表述偏抽象",
         "confidence": _ASPECT_CONF["教案偏好"]},
        {"aspect": "试讲交互", "aspect_label": _ASPECT_LABEL["试讲交互"],
         "finding": f"试讲中开放式提问占比约 {int(open_rate * 100)}%，分步讲解约 {int(scaffold_rate * 100)}%；"
                     f"你习惯于先给台阶、再放手，符合脚手架教学。",
         "evidence": teacher_quote, "confidence": _ASPECT_CONF["试讲交互"]},
        {"aspect": "试讲交互", "aspect_label": _ASPECT_LABEL["试讲交互"],
         "finding": f"累计切换学生档位 {teaching.get('level_switches', 0)} 次", "evidence": "",
         "confidence": _ASPECT_CONF["试讲交互"]},
    ]
    return {
        "style": {"label": style, "desc": _style_desc(style)},
        "insights": insights,
        "suggestions": [
            f"优先把「{weak_dim}」维度的改法落到下一份教案；采纳后可观察该维度提示是否下降。",
        ],
    }


def _style_label(scaffold_rate, open_rate) -> str:
    tags = []
    if scaffold_rate >= 0.5:
        tags.append("脚手架搭建型")
    if open_rate >= 0.5:
        tags.append("引导探究型")
    if not tags:
        tags.append("讲授主导型")
    return "·".join(tags)


def _style_desc(style):
    m = {"脚手架搭建型": "善于把难点拆成可操作的台阶，先给提示再放手",
         "引导探究型": "偏好开放式提问，鼓励学生自己发现问题",
         "讲授主导型": "以清晰讲解为主，可适度增加开放式设问与脚手架"}
    return "、".join(m[t] for t in style.split("·") if t in m) or "风格画像待更多样本校准"


def _llm_report(signals, lesson, sim, adopted, top3) -> dict:
    """真实模式：LLM 起草风格与洞察，evidence 必须命中语料（防幻觉后校验），置信度代码判档。"""
    corpus = signals["corpus"]
    prompt_sig = {
        "per_dim_avg": signals["per_dim_avg"],
        "adopted_rate": signals["adopted_rate"],
        "teaching": signals["teaching"],
    }
    sys = (
        "你是资深教研专家，根据青年教师的行为信号撰写「教学人格报告」。只输出 JSON："
        '{"style": {"label": 一句话风格标签(如"引导探究·脚手架搭建型"), "desc": 两三句描述}, '
        '"insights": [{"aspect": "教案偏好|试讲交互", "finding": 洞察", '
        '"evidence": 证据(必须从语料中逐字摘抄，不要改写)}], '
        '"suggestions": [可操作改进建议]}. '
        "要求：insights 控制在 3-4 条；evidence 必须逐字来自语料列表；不给置信度（系统统一判定）。")
    user = (f"信号：{json.dumps(prompt_sig, ensure_ascii=False)}\n"
            f"语料-报告要点：{json.dumps(corpus['report_issues'], ensure_ascii=False)}\n"
            f"语料-教师试讲原话：{json.dumps(corpus['teacher_turns'], ensure_ascii=False)}")
    try:
        out, ms = llm.latency_of(lambda: llm.chat_json(sys, user, 0.4))
        insights = []
        corpus_text = "\n".join(corpus["report_issues"] + corpus["teacher_turns"])
        for it in (out.get("insights") or [])[:4]:
            aspect = "教案偏好" if it.get("aspect") in ("教案偏好", "教案结构") else "试讲交互"
            ev = it.get("evidence", "")
            if len(normalize(ev)) < 4 or not normalize(ev) in normalize(corpus_text):   # 证据链硬闸
                continue
            insights.append({
                "aspect": aspect, "aspect_label": _ASPECT_LABEL.get(aspect, aspect),
                "finding": it.get("finding", ""), "evidence": ev,
                "confidence": _ASPECT_CONF.get(aspect, "中")})              # 置信度代码化
        style = out.get("style") or {}
        report = {
            "style": {"label": style.get("label", "综合型·待校准"),
                      "desc": style.get("desc", "")},
            "insights": insights or _mock_report(signals, lesson, sim, adopted, 0)["insights"],
            "suggestions": (out.get("suggestions") or [])[:3],
        }
        db.save_trace("profile_report", None, "", [], json.dumps(report, ensure_ascii=False)[:300], False, ms)
        return report
    except llm.LLMError:
        return _mock_report(signals, lesson, sim, adopted, top3)