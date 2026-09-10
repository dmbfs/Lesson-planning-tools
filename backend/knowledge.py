"""备课导师 · 认知障碍库 seed（自编，可开源；复赛扩至 50-100 条）"""
import re

OBSTACLES = [
    # ===== 函数单调性（当前演示课题）=====
    dict(topic="函数的单调性", chapter="函数单调性", symptom="「取 x=1, x=2，f(1)<f(2)，所以是增函数」",
         cause="对定义中『任意 x1<x2』缺乏辨析，把验证当成举例",
         fix="导入加一次辨析设问：只取两个点判断可以吗？", evidence="自编", confidence="高"),
    dict(topic="函数的单调性", chapter="函数单调性", symptom="「sin(α+β)=sinαcosβ-cosαsinβ」符号写反",
         cause="和差公式符号规则混淆",
         fix="给记忆锚点：正弦符号照抄、余弦符号相反", evidence="自编", confidence="高"),
    dict(topic="函数的单调性", chapter="函数单调性", symptom="证明题不作差变形，直接说「不会」",
         cause="配方法/通分等变形技巧储备不足",
         fix="弱档先给填空式变形提示卡", evidence="自编", confidence="中"),
    dict(topic="函数的单调性", chapter="函数单调性", symptom="「在 (0,+∞) 上单调」与「单调递增区间为 (0,+∞)」混用",
         cause="单调区间与在区间上单调概念边界不清",
         fix="用 |x| 图像做对比辨析", evidence="自编", confidence="中"),
    dict(topic="函数的单调性", chapter="函数单调性", symptom="证明题缺『任取、作差、定号、结论』四步，直接写变形",
         cause="对证明框架不熟悉",
         fix="提供四步模板 + 例题示范", evidence="自编", confidence="低"),
    # ===== 三角恒等变换 =====
    dict(topic="三角恒等变换", chapter="两角和与差公式", symptom="sin(α+β) 展开时把 sinαsinβ 项也算进去",
         cause="对展开式结构记忆混乱",
         fix="先口头复述展开式再做题；给出推导验证一遍", evidence="自编", confidence="高"),
    dict(topic="三角恒等变换", chapter="两角和与差公式", symptom="二倍角公式 cos2α 只记得一种形式",
         cause="二倍角三种形式未串成一体",
         fix="从 cos2α=cos²α-sin²α 推导另两种，形成记忆链", evidence="自编", confidence="中"),
    dict(topic="三角恒等变换", chapter="辅助角公式", symptom="as sin x + b cos x 提取根号下 a²+b² 后角度写错",
         cause="辅助角公式的相位推导不熟",
         fix="先用具体数值例题走一遍提取→配角流程", evidence="自编", confidence="中"),
    # ===== 平面向量 =====
    dict(topic="平面向量", chapter="向量的线性运算", symptom="把向量的模当长度直接加减，忽略方向",
         cause="向量与数量概念混淆",
         fix="强调向量=方向+大小，举例平行四边形法则", evidence="自编", confidence="高"),
    dict(topic="平面向量", chapter="向量的数量积", symptom="a·b=0 就以为 a 与 b 垂直，漏掉零向量情况",
         cause="数量积为零的充要条件记忆不完整",
         fix="补充讨论 a=0 或 b=0 的边界情形", evidence="自编", confidence="中"),
    # ===== 正弦定理 =====
    dict(topic="正弦定理", chapter="正弦定理", symptom="已知两边一角求另一角时出现两解，漏讨论",
         cause="对正弦定理解三角形多解性不敏感",
         fix="画图讨论角的范围，确认解的个数", evidence="自编", confidence="中"),
]

# 教材目录（人教A版）：用于推导"先备知识边界" prereq_range
TEXTBOOK_TREE = {
    "必修一": [
        ("第1章", "集合与常用逻辑用语"),
        ("第2章", "一元二次函数、方程和不等式"),
        ("第3章", "函数的概念与性质"),
        ("第4章", "指数函数与对数函数"),
        ("第5章", "三角函数"),
    ],
    "必修二": [
        ("第1章", "平面向量及其应用"),
        ("第2章", "三角恒等变换"),
        ("第3章", "正弦定理、余弦定理及解三角形"),
        ("第4章", "立体几何初步"),
        ("第5章", "统计与概率"),
    ],
}

# ==================== 教材原文加载（编辑文件 + 重启即导入）====================
# 把教材原始文本放入 data/textbook/*.txt|*.md，进程启动时载入内存（不建向量库，
# 按文件名/标题匹配课题后截取章节原文注入 LLM 上下文供其"学习"）。
from .config import TEXTBOOK_DIR   # noqa: E402

_TBK = None   # 缓存：None=未加载，{} = 已扫描（可能为空目录）

# ---------- mtime 增量缓存 ----------
# {文件路径: (mtime, 条目列表)}；仅对 mtime 变化/新增的文件重新解析拆章，
# 未变化的文件直接用缓存结果，避免每次重扫全部文件。
_FILE_CACHE = {}

# ---------- 正文倒排索引（懒构建） ----------
# {字符: {条目在 _TBK 中的下标}}；基于 id(_TBK) 作版本号，与缓存联动失效。
_TEXT_INDEX = None
_TEXT_INDEX_VER = None


def _parse_file(f) -> list:
    """解析单个教材文件为条目列表（与 _split_chapters 拆分逻辑一致）。"""
    try:
        text = f.read_text(encoding="utf-8-sig", errors="ignore").strip()   # 去 BOM，兼容 Windows 保存
    except OSError:
        return []
    if not text:
        return []
    parts = _split_chapters(text)
    out = []
    if len(parts) == 1:
        base = f.stem.strip()
        out.append({"file": f.name, "title": base, "topic": base, "text": parts[0][1]})
    else:
        for chap, body in parts:
            if not body.strip():
                continue
            t = f"{f.stem}_{chap}".strip()
            out.append({"file": f.name, "title": t, "topic": t, "text": body})
    return out


def reload_textbook() -> list:
    """扫描 data/textbook/ 目录，解析每份文件并缓存。返回条目列表。
    - 单个文件若含 ≥2 处「第N章」标题，自动按章拆分，便于整本教材导入。
    - 拆分后条目名 = 文件名_第N章（如「必修二_第2章」），供课题匹配。
    - 按文件 mtime 增量缓存：仅新增或内容变化的文件重新解析，未变化的直接复用缓存；
      被删除的文件对应缓存自动失效。"""
    global _TBK
    # 收集当前实际存在的文件
    if TEXTBOOK_DIR.exists():
        files = sorted(TEXTBOOK_DIR.glob("*.txt")) + sorted(TEXTBOOK_DIR.glob("*.md"))
    else:
        files = []
    cur = set(files)
    # 清理已删除文件的缓存
    for p in list(_FILE_CACHE):
        if p not in cur:
            del _FILE_CACHE[p]
    # 逐文件：命中缓存直接复用，否则解析并写回缓存
    result = []
    for f in files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        cached = _FILE_CACHE.get(f)
        if cached is not None and cached[0] == mtime:
            result.extend(cached[1])
            continue
        els = _parse_file(f)
        _FILE_CACHE[f] = (mtime, els)
        result.extend(els)
    _TBK = result
    return _TBK

_CHAPTER_RE = re.compile(r"^\s*(第[一二三四五六七八九十百0-9]+章[^\n]*)", re.M)

def _split_chapters(text: str) -> list:
    """按「第N章」标题切分章节；不足 2 章则整体返回单一章节。返回 [(章节名, 正文)]。"""
    marks = list(_CHAPTER_RE.finditer(text))
    if len(marks) < 2:
        return [("", text)]
    parts = []
    for i, m in enumerate(marks):
        head = m.group(1).strip()
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        parts.append((head, text[start:end].strip()))
    return parts

def loaded_textbook() -> list:
    if _TBK is None:
        reload_textbook()
    return list(_TBK)

_TOKEN_CHAR_RE = re.compile(r"[0-9a-zA-Z\u4e00-\u9fff]")

def _index_keys(text: str):
    """把正文拆成索引键：逐个字符（CJK 与 ASCII 数字字母，ASCII 统一小写）。
    逐字建索引保证任意 topic 子串都必能被倒排表命中，语义与逐条 `in` 完全一致。"""
    return [m.group(0).lower() for m in _TOKEN_CHAR_RE.finditer(text)]

def _text_index():
    """懒构建正文倒排索引：{键 -> {条目在 _TBK 中的下标}}。
    以 id(_TBK) 为版本号，reload_textbook() 重建列表后自动失效并重建。"""
    global _TEXT_INDEX, _TEXT_INDEX_VER
    if _TEXT_INDEX is None or _TEXT_INDEX_VER != id(_TBK):
        idx = {}
        for i, e in enumerate(_TBK or []):
            for k in set(_index_keys(e["text"])):
                idx.setdefault(k, set()).add(i)
        _TEXT_INDEX = idx
        _TEXT_INDEX_VER = id(_TBK)
    return _TEXT_INDEX

def search_textbook(topic: str, limit: int = 1) -> list:
    """按课题关键词匹配教材章节。
    优先标题子串命中；若命中不足，回退到正文全文搜索。"""
    entries = loaded_textbook()
    hit = []
    for e in entries:
        if e["topic"] in topic or topic in e["topic"]:
            hit.append(e)
            if len(hit) >= limit:
                return hit
    if hit:
        return hit
    # 回退：正文全文搜索（用懒构建的倒排索引先收窄候选集，再逐条校验子串，
    # 保证与旧版逐条 `in` 的匹配语义完全一致，同时避免 O(N*len) 全量扫描）
    keys = _index_keys(topic)
    if keys:
        idx = _text_index()
        rarest = min(keys, key=lambda k: len(idx.get(k, ())))
        cand = idx.get(rarest, set())
    else:
        cand = None   # 空 topic：旧版 `"" in text` 恒真，故候选=全部条目
    for i, e in enumerate(entries):
        if cand is not None and i not in cand:
            continue
        if topic in e["text"]:
            hit.append(e)
            if len(hit) >= limit:
                break
    return hit

def textbook_excerpt(topic: str, max_chars: int = 4000) -> str:
    """返回与课题最匹配的教材原文片段；无匹配或未导入时返回空串（不注入上下文）。"""
    try:
        entries = search_textbook(topic)
    except Exception:
        return ""
    if not entries:
        return ""
    e = entries[0]
    return f"{e['title']}\n{e['text'][:max_chars]}"

__all__ = ["OBSTACLES", "TEXTBOOK_TREE", "reload_textbook", "loaded_textbook",
           "search_textbook", "textbook_excerpt"]
