"""备课导师 · 确定性规则层（纯函数，无 LLM、无 SQL）
可解释性的实体：判定、排序、边界、置信度全部由代码决定，不随 LLM 措辞漂移。
"""
import re

# ==================== 课型门槛（PRD 2.1：MVP 仅新授课） ====================
SUPPORTED_LESSON_TYPES = {"新授课", "新授"}
UNSUPPORTED_REASON = "五维评估 rubric 基于新授课结构，复习课/习题课/试卷讲评课暂不支持"

def check_lesson_type(lesson_type: str) -> dict:
    """课型白名单校验。返回 {ok, reason}，纯代码判定，不交给模型理解。"""
    lt = (lesson_type or "").strip()
    if not lt:
        return {"ok": True, "reason": ""}          # 未选择课型，默认按新授课处理
    if lt in SUPPORTED_LESSON_TYPES:
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": UNSUPPORTED_REASON}

# ==================== 置信度 = 证据强度（PRD 6.4，与掌握度脱钩） ====================
# 证据类型 → 置信度档位。障碍库命中取条目自带 confidence（高/中/低，来自课标/教研/自编）；
# 通用教学经验 = 中；纯推测 = 低。班级掌握度「弱」只影响排序，不经过本函数。
def confidence_for(evidence_type: str, stored: str = "") -> str:
    """代码判定置信度，LLM 返回值一律被此函数覆盖。
    evidence_type: obstacle_hit(障碍库命中) / generic(通用教学经验) / speculation(纯推测)
    """
    if evidence_type == "obstacle_hit":
        return stored if stored in ("高", "中", "低") else "高"
    if evidence_type == "generic":
        return "中"
    return "低"

def evidence_meta(evidence_type: str, source: str = "") -> dict:
    """构造 evidence 对象（可追溯）：type + source + confidence"""
    return {
        "type": evidence_type,
        "source": source or ("障碍库命中" if evidence_type == "obstacle_hit"
                             else "通用教学经验" if evidence_type == "generic" else "仅推测"),
        "confidence": confidence_for(evidence_type, source),
    }

# ==================== 先备知识边界（硬规则，PRD v1.4） ====================
def compute_prereq_range(book_no: str, chapter: int, section: int,
                         textbook_tree: dict) -> str:
    """当前课题之前的全部教材内容。如：必修二 第1章第3小节
    -> 「必修一全部、必修二第1章第1、2小节」"""
    if book_no not in textbook_tree:
        return f"{book_no} 全部已学内容（教材目录未收录，按全部已学处理）"

    books = list(textbook_tree.keys())
    idx = books.index(book_no)

    parts = []
    # 更早的册：全部
    for b in books[:idx]:
        parts.append(f"{b}全部")
    # 当前册：本章之前的小节 + 更早的章
    cur_chapters = textbook_tree[book_no]
    if 0 < chapter <= len(cur_chapters):
        ch_name = cur_chapters[chapter - 1][1]
    else:
        ch_name = f"第{chapter}章"
    if section > 1:
        parts.append(f"{book_no}{ch_name}第1-{section - 1}节")
    if chapter > 1:
        for i in range(chapter - 1):
            parts.append(f"{book_no}{cur_chapters[i][1]}")
    if not parts:
        return "本课题之前无前置教材内容"
    return "、".join(parts)

# ==================== 证据链校验（防幻觉硬闸） ====================
def normalize(text: str) -> str:
    """归一化：去空白/全角标点差异，用于引用校验"""
    return re.sub(r"[\s，。；：、,.、:;()（）“”\"'‘’·\-—–]+", "", text or "")


_norm = normalize    # 兼容旧名

def verify_quote(quote: str, raw_text: str) -> bool:
    """LLM 返回的建议必须引用教案原文子串。不是子串 → 视为幻觉，丢弃/降级。"""
    q = _norm(quote)
    if len(q) < 3:            # 引用太短无意义
        return False
    return q in _norm(raw_text)
