"""备课导师 · 教案解析（docx / txt / md / pdf 文字层）"""
import re

try:
    import docx  # python-docx
except ImportError:
    docx = None

try:
    import fitz  # pymupdf
except ImportError:
    fitz = None


def parse_file(filename: str, data: bytes) -> dict:
    """按扩展名解析教案，返回 {text, topic}"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "docx":
        text = _parse_docx(data)
    elif ext == "pdf":
        text = _parse_pdf(data)
    else:  # txt / md / 其他，按纯文本
        text = data.decode("utf-8", errors="ignore")
    text = _clean(text)
    topic = _guess_topic(text)
    return {"text": text, "topic": topic, "filename": filename}


def _parse_docx(data: bytes):
    if docx is None:
        raise ValueError("缺少 python-docx 依赖")
    import io
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _parse_pdf(data: bytes):
    if fitz is None:
        raise ValueError("缺少 pymupdf 依赖")
    import io
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _guess_topic(text: str) -> str:
    """从教案里猜课题名：优先匹配『课题：xxx』『XXX（第N课时）』等"""
    patterns = [
        r"(?:课题|课题名称)[：:]\s*([^\n]{2,30})",
        r"(?:教学内容|本节内容)[：:]\s*([^\n]{2,30})",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1).strip()
    # 首行非空行截断
    first = next((l.strip() for l in text.split("\n") if l.strip()), "")
    return first[:20] if first else "未命名教案"
