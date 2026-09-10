"""备课导师 · 教材导入命令行（开发者）
把教材文件（docx / pdf / txt / md）抽成纯文本，写入内置知识库 backend/textbook/，
重启 uvicorn 后由加载器按课题自动注入。

用法:
    python -m backend.import_textbook "人教版数学_必修一_第2章.docx"
    python -m backend.import_textbook a.pdf b.docx c.md
"""
import sys
from pathlib import Path

from .config import TEXTBOOK_DIR
from .parser import parse_file


def main(argv: list[str]) -> int:
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        print(__doc__)
        return 1
    TEXTBOOK_DIR.mkdir(parents=True, exist_ok=True)
    for path in files:
        p = Path(path)
        if not p.exists():
            print(f"[跳过] 找不到: {p}")
            continue
        try:
            parsed = parse_file(p.name, p.read_bytes())
        except ValueError as e:
            print(f"[失败] {p.name}: {e}（docx 需 python-docx，pdf 需 pymupdf）")
            continue
        text = parsed["text"].strip()
        if not text:
            print(f"[警告] {p.name}: 未提取到文字，可能是扫描件，跳过")
            continue
        target = TEXTBOOK_DIR / (p.stem + ".txt")
        target.write_text(text, encoding="utf-8")
        print(f"[已入库] {target}  ({len(text)} 字)")
    print("重启服务：python -m uvicorn backend.main:app --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))