# 备课导师 · Lesson Planning Tools

> 青年教师的备课反馈与模拟试讲助手 —— 让每一位新老师，都像身边坐着一位老教师。

面向入职 1-3 年的青年数学教师：上传教案即获得「红笔批注」式的 AI 备课反馈，与多水平虚拟学生完成模拟试讲，课前预判学生的认知障碍，并持续积累个人教学风格画像。

## 功能特性

### 教案评估（红笔批注）
- 支持 docx / pdf / txt / md 教案上传，自动解析并识别课题
- 异步 AI 评估，反馈按「最该改的 3 处」置顶排序
- 批注式建议可逐条勾选采纳，采纳记录留存

### 模拟试讲
- 虚拟学生分「强 / 中 / 弱」三档水平，试讲中可动态切换
- 学生提问贴近真实课堂的常见困惑
- 试讲结束生成小结：讲解节奏、互动设计、水平切换时机的复盘

### 学生预判
- 课前预判本课题学生易犯的认知障碍（症状 / 成因 / 干预建议）
- 内置自编认知障碍库，覆盖函数单调性、三角恒等变换、平面向量等章节

### 班级与进度
- 教材目录树（人教A版 必修一 / 必修二）驱动的章节进度设置
- 分章节掌握度设置，影响预判与试讲的学情基线

### 教师画像
- 基于历史评估与试讲数据，合成带证据的教学风格洞察
- 数据不足时明确提示「还需多少样本」，不编造结论

### 模型配置（OpenAI 兼容）
- 支持内置网关或自接任意 OpenAI 兼容端点（DeepSeek / OpenAI / 中转站 / 本地 vLLM·Ollama）
- 运行时切换，一键测试连通性
- 未配置 API Key 时自动降级为 MOCK 演示模式，开箱即用

### 留痕与用量
- LLM 调用全留痕（可追溯）
- 用量统计（成本护栏）

## 技术栈

- **后端**：Python · FastAPI · SQLite（零外部服务）
- **前端**：原生 HTML / CSS / JS，零构建，FastAPI 静态托管
- **LLM**：OpenAI 兼容接口，密钥走环境变量，不入库不落码

## 快速开始

```bash
# 1. 安装依赖
pip install fastapi uvicorn pydantic httpx
pip install python-docx pymupdf   # 可选：支持 docx / pdf 教案解析

# 2. 配置模型密钥（复制模板并填入自己的 Key）
cp .env.example .env

# 3. 启动
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000> 即可使用。

> 不配置 `.env` 也能跑：应用自动进入 MOCK 演示模式，评估与试讲返回内置示例。

## 目录结构

```
backend/
  main.py            # FastAPI 入口：API + 静态托管
  pipeline.py        # 教案评估流水线
  simulate.py        # 模拟试讲引擎
  profile.py         # 教师画像
  knowledge.py       # 认知障碍库（自编，可开源）
  llm.py             # OpenAI 兼容网关（重试 / mock / 自接切换）
  db.py              # SQLite 持久化
  tasks.py           # 异步评估任务
  parser.py          # 教案解析（docx/pdf/txt/md）
  domain.py          # 领域校验（课型白名单等）
frontend/
  index.html         # 单页应用（纸墨 + 红笔批注设计语言）
.env.example         # 模型配置模板
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | 登录（演示） |
| POST | `/api/upload` | 上传并解析教案 |
| POST | `/api/evaluate` | 创建异步评估任务 |
| GET | `/api/evaluate/{task_id}` | 查询评估结果 |
| POST | `/api/predict` | 学生预判 |
| POST | `/api/sim/chat` | 模拟试讲对话 |
| POST | `/api/sim/summary` | 试讲小结 |
| GET/POST | `/api/settings` | 班级设置 |
| GET/POST | `/api/model` | 模型配置（含连通性测试） |
| GET | `/api/profile` | 教师画像 |

## 说明

- 本仓库只包含代码：教材原文与提取数据因体积与版权原因不入库，克隆后 `backend/textbook/` 为空不影响启动（知识库自动降级为内置障碍库）
- 数据库 `data/beike.db` 首次运行自动创建
- `.env` 永不入库，密钥仅通过环境变量注入
