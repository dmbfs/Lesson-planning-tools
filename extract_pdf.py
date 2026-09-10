import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import fitz

path = r'c:\Users\35805\WorkBuddy\2026-08-03-23-00-56\eed923c4-570c-4f5e-bb18-4f451fb97ced.pdf'
doc = fitz.open(path)
out = []
for i, page in enumerate(doc):
    out.append(f'--- page {i+1} ---\n' + page.get_text())
with open(r'c:\Users\35805\WorkBuddy\2026-08-03-23-00-56\参赛手册.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done, pages:', doc.page_count)
