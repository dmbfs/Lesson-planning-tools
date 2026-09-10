"""备课导师 · 扫描版教材 OCR 转换（开发者，针对扫描 PDF）

扫描版教材是图片、没有文字层，需 OCR 识别后转入内置知识库。
本脚本用 RapidOCR（ONNX，无需 paddlepaddle）把 PDF 每页渲染成图再识别文字，
结果写入 backend/textbook/，重启 uvicorn 后由加载器按章节注入。

运行（用独立 Python 3.11，避免污染你的 Python 3.14）:
    uv run --python 3.11 --with pymupdf --with numpy \
           --with rapidocr-onnxruntime python backend/ocr_textbook.py "扫描教材.pdf"

可一次传多个 PDF / 目录。
"""
import sys
from pathlib import Path

from .config import TEXTBOOK_DIR  # 复用目录配置（backend/textbook）

DPI = 200          # 渲染分辨率，越大越清晰但也越慢；扫描件 200 足够
_SCALE = DPI / 72  # → fitz.Matrix 倍数


def _render(pix):
    import numpy as np
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    elif pix.n == 1:
        arr = np.repeat(arr, 3, axis=2)
    return arr[:, :, ::-1].copy()  # RGB→BGR，RapidOCR 期望 BGR


def ocr_pdf(pdf_path: Path, engine, out_path: Path) -> int:
    import fitz
    doc = fitz.open(str(pdf_path))
    blocks: list[str] = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(_SCALE, _SCALE), alpha=False)
        img = _render(pix)
        res, _ = engine(img)
        texts = [r[1] for r in res] if res else []
        head = f"\n[第{i + 1}页]" if (i + 1) % 25 == 1 or i == 0 else ""
        blocks.append(head + "\n".join(texts))
    body = "\n".join(b for b in blocks if b and b.strip("\n"))
    out_path.write_text(body, encoding="utf-8")
    return len(body)


def main(argv: list[str]) -> int:
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    TEXTBOOK_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for arg in argv:
        if arg.startswith("-"):
            continue
        p = Path(arg)
        if p.is_dir():
            items = sorted(x for x in p.glob("*.pdf")) + sorted(x for x in p.glob("*.PDF"))
        else:
            items = [p] if p.exists() else []
        for pdf in items:
            target = TEXTBOOK_DIR / (pdf.stem + ".txt")
            n = ocr_pdf(pdf, engine, target)
            if n:
                print(f"[OCR完成] {pdf.name} → {target.name}  ({n} 字)")
                ok += 1
            else:
                print(f"[警告] {pdf.name}: 未识别到文字")
    print(f"共 {ok} 份完成，重启服务生效")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))