"""备课导师 · 后端配置"""
import os
from pathlib import Path

# 路径
BASE_DIR = Path(__file__).resolve().parent.parent          # 项目根目录
FRONTEND_DIR = BASE_DIR / "frontend"                      # 前端静态目录
DB_PATH = BASE_DIR / "data" / "beike.db"                  # SQLite 数据库
TEXTBOOK_DIR = BASE_DIR / "backend" / "textbook"          # 内置教材原文（随 agent 分发，改文件+重启即生效）

# 零依赖 .env 加载（已有环境变量优先，便于 CI/部署时用环境变量覆盖）
def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

_load_dotenv(BASE_DIR / ".env")

# LLM 配置（OpenAI 兼容格式，可接任意兼容端点：DeepSeek / OpenAI / 中转站 / 本地 vLLM·Ollama）
# 设置了 LLM_API_KEY 走真实模型，否则自动 mock（演示模式）
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")  # 留空则默认 DeepSeek
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")                  # 自定义模型名，如 gpt-4o-mini / qwen2.5

# LLM 请求超时（秒）与网络失败重试次数（可配置，从环境变量读，带默认值与容错）
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

LLM_TIMEOUT = _env_float("LLM_TIMEOUT", 120)      # chat 请求超时（秒）
LLM_RETRIES = _env_int("LLM_RETRIES", 1)          # 网络失败时的重试次数

# 默认班级设置
DEFAULT_SETTINGS = {
    "book": "人教A版",
    "book_no": "必修二",
    "chapter": 1,
    "section": 3,
    "topic": "函数的单调性",
    "lesson_type": "新授课",
}
