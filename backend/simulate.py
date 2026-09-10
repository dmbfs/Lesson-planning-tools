"""备课导师 · 模拟试讲引擎
- persona 由纯代码构建（先备边界硬规则 + 障碍注入 + 档位参数），LLM 只负责在边界内扮演
- 每轮/小结均写 traces，persona JSON 整体落痕（复赛"模拟逻辑、字段含义"合规材料）
"""
import json
import random

from . import llm, db, domain, profile
from .knowledge import TEXTBOOK_TREE

# 三档语料（mock 模式，与前端演示一致）
MOCK_REPLIES = {
    "优": [
        "老师，我懂了。x1 小于 x2 的时候 f(x1) 也小于 f(x2)，那就说明它越走越高，是增函数。",
        "哦！所以不能只取两个点，得是「任意」取两个点，不然可能会恰好选到特例。",
        "这个例题我可以试试自己证明。先任取 x1<x2，然后作差 f(x1)-f(x2)，变形…（思考）这里是配方法。",
        "明白，符号这里的规则我记住了：正弦照抄、余弦相反。",
        "老师，如果我把区间换成 (1,+∞)，结论还一样吗？我想试试变式。",
    ],
    "中": [
        "嗯…我大概懂了，就是函数图像往右上走就是增函数。（有点不确定）",
        "老师，为什么要强调「任意」两个字？我取两个点验证了不行吗？",
        "作差以后我变形卡住了…f(x1)-f(x2) 应该是正的，但我不知道下面怎么写。",
        "啊，我写错了，sin(α+β) 我把符号写反了…应该是 sinαcosβ 加 cosαsinβ？",
        "（犹豫）单调区间和「在区间上单调」……这个不是一回事吗？",
    ],
    "弱": [
        "（沉默了一会儿）老师……我没太听懂。为什么 x1<x2，函数值也得跟着变？",
        "取两个点？为什么要取两个？是不是随便取一对就够了……",
        "（小声）我不会做差…配方法是什么？我们以前好像讲过但我忘了。",
        "（点头但不说话，看起来没懂）",
        "符号……sin、cos 我总混。老师能不能再讲一遍？",
    ],
}

LEVEL_PROFILES = {
    "优": "已学范围掌握得很扎实，能跟变式。",
    "中": "已学范围大体掌握，个别细节有漏洞。",
    "弱": "已学范围也有不少漏洞，可能装懂或沉默。",
}


def build_persona(topic: str, level: str) -> dict:
    """学生人设（纯代码构建，确定性）：
    先备边界 = 教材目录推导（硬规则，与档位无关）
    档位差异 = 已学范围内掌握扎实度 + 典型错答暴露频率
    """
    s = db.get_settings()
    prereq_range = domain.compute_prereq_range(s["book_no"], s["chapter"], s["section"], TEXTBOOK_TREE)
    obstacles = db.search_obstacles(topic, limit=4)
    return {
        "topic": topic,
        "book_no": s["book_no"],
        "chapter": s["chapter"],
        "section": s["section"],
        "level": level,
        "prereq_range": prereq_range,
        "level_profile": LEVEL_PROFILES[level],
        "obstacles": [o["symptom"] for o in obstacles],
    }


def chat_reply(topic: str, level: str, history: list) -> dict:
    """试讲对话：返回 {reply, persona}。persona 每次构建（档位切换即时生效）。"""
    persona = build_persona(topic, level)
    if llm.MOCK_MODE:
        reply = MOCK_REPLIES[level][random.randrange(len(MOCK_REPLIES[level]))]
        db.save_trace(f"sim_chat_{level}", None, topic, persona["obstacles"], reply, True, 0, persona)
        return {"reply": reply, "persona": persona}
    try:
        reply, ms = llm.latency_of(lambda: _llm_reply(persona, history))
        db.save_trace(f"sim_chat_{level}", None, topic, persona["obstacles"], reply[:200], False, ms, persona)
        return {"reply": reply, "persona": persona}
    except llm.LLMError:
        reply = MOCK_REPLIES[level][random.randrange(len(MOCK_REPLIES[level]))]
        return {"reply": reply, "persona": persona}


def _llm_reply(persona, history) -> str:
    sys = ("你现在扮演一名高中生，正在听老师讲课。这是课堂对话，你是学生不是助手。\n"
           f"【知识范围边界（硬规则）】你已掌握{persona['prereq_range']}；本课题及之后的内容你还没学，不会使用。\n"
           f"【层次档位】{persona['level']}。已学范围内：{persona['level_profile']}\n"
           f"【典型易错点】{'；'.join(persona['obstacles'])}，讲到相关内容时按档位概率性暴露。\n"
           "行为规则：1 保持学生身份，不用教师语言；2 超出知识范围的内容一律不会，不要脑补；"
           "3 提问按档位作答，不会就暴露典型错答、说不太懂或沉默；4 听不懂用学生的方式追问；"
           "5 不抢答、不总结；6 回复简短口语化。")
    user = "\n".join(f"{'老师' if m['who'] == 'teacher' else '学生'}：{m['text']}" for m in history[-10:])
    return llm.chat(sys, user, temperature=0.7)


def make_summary(topic: str, level: str, history: list, level_switches: list) -> dict:
    """会话小结：顺利点 / 卡点 / 调整建议（+ 档位对比），写回记忆 + 留痕"""
    persona = build_persona(topic, level)
    if llm.MOCK_MODE:
        summary = _mock_summary(level, level_switches)
        db.save_trace("sim_summary", None, topic, persona["obstacles"],
                      json.dumps(summary, ensure_ascii=False)[:300], True, 0, persona)
    else:
        try:
            summary, ms = llm.latency_of(lambda: _llm_summary(topic, history, level_switches))
            db.save_trace("sim_summary", None, topic, persona["obstacles"],
                          json.dumps(summary, ensure_ascii=False)[:300], False, ms, persona)
        except llm.LLMError:
            summary = _mock_summary(level, level_switches)
    db.save_sim_session(topic, level, level_switches, persona, history, summary)
    profile.update_profile()          # 教师画像：试讲交互历史累积
    return summary


def _mock_summary(level, level_switches):
    ok = [
        "用「爬坡」类比引入单调性，中档学生当场理解并复述正确",
        "例题 1 采用「先画图、再验证」两步走，弱档学生也能跟上",
    ]
    issues = [
        "讲「定义法证明」时弱档学生追问「老师，为什么取 x1<x2 就够，不能取到所有？」，对应认知障碍「对任意性理解不足」",
        "讲到辅助角公式，中档学生出现典型错答 sin(α+β) 符号写反",
    ]
    advice = [
        "在引入定义后补一句「这里的任意两个字是这道题的全部意义」，预判弱档「任意性」卡点",
        "证明例题前先给一道填空式证明作为脚手架",
    ]
    if level_switches:
        advice.append(f"档位对比：本次共切换 {len(level_switches)} 次（{'、'.join(level_switches)}）。"
                      "优档 1 遍通过；中档在符号处卡 1 次；弱档在任意性与符号处各卡 1 次 → "
                      "分层练习建议：弱档先做符号辨析题，优档直接上综合变式")
    return {"ok": ok, "issues": issues, "advice": advice}


def _llm_summary(topic, history, level_switches):
    transcript = "\n".join(f"{'老师' if m['who'] == 'teacher' else '学生'}：{m['text']}" for m in history)
    sys = ("你是教研员，根据试讲对话生成会话小结。只输出 JSON："
           "{\"ok\": [顺利通过的讲法], \"issues\": [卡点（引用对话原文）], "
           "\"advice\": [对教案/讲法的可操作调整建议]}。")
    user = f"课题：{topic}\n档位切换记录：{'、'.join(level_switches) or '无'}\n\n对话记录：\n{transcript}"
    return llm.chat_json(sys, user, temperature=0.3)
