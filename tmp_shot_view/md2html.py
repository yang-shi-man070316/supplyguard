# -*- coding: utf-8 -*-
"""SupplyGuard 技术文档 md -> 单文件 HTML（供 Edge headless 打印 PDF）。
无美化：仅字体/字号/表格边框等基础排版。"""
import base64, io, json, os, re, sys, zlib, urllib.request

SRC = r"e:\supplyguard\技术文档\SupplyGuard_技术文档_v1.1.md"
OUT = r"e:\supplyguard\tmp_shot_view\v11.html"
DOCDIR = os.path.dirname(SRC)

def fetch_mermaid(code):
    j = json.dumps({"code": code, "mermaid": {"theme": "default"}}).encode()
    comp = zlib.compress(j, 9)
    b64 = base64.urlsafe_b64encode(comp).decode().rstrip("=")
    url = "https://mermaid.ink/img/pako:%s?type=png&bgColor=FFFFFF" % b64
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    data = urllib.request.urlopen(req, timeout=30).read()
    # mermaid.ink 返回 RGBA PNG，Edge 打印时 alpha 通道会丢内容；转白底 JPEG
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()

def inline(s):
    # 先占位提取图片 URL（避免尖括号/& 被 HTML 转义破坏）
    stash = []
    def _img(m):
        stash.append(m.group(2))
        return "@@IMG%d@@" % len(stash)
    s = re.sub(r"!\[([^\]]*)\]\((<[^>]+>|[^)\s]+)\)", _img, s)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 链接 -> 纯文本
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    # 加粗
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    # 行内代码
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    for idx, url in enumerate(stash, 1):
        s = s.replace("@@IMG%d@@" % idx, '<img src="%s">' % url)
    return s

def img_src(p):
    if p.startswith("file://"):
        return p
    if p.startswith("<") and p.endswith(">"):
        p = p[1:-1]
    p = p.replace("%20", " ")
    p = os.path.normpath(os.path.join(DOCDIR, p))
    return "file:///" + p.replace("\\", "/").replace(" ", "%20")

def parse(lines):
    out = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        # 代码块
        if ln.startswith("```"):
            lang = ln[3:].strip()
            buf = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1  # skip closing ```
            code = "\n".join(buf)
            if lang == "mermaid":
                data = fetch_mermaid(code)
                jpg = os.path.join(os.path.dirname(OUT), "_mm%d.jpg" % len(out))
                io.open(jpg, "wb").write(data)
                out.append('<p style="text-align:center"><img src="file:///%s" style="max-width:80%%"></p>' % jpg.replace("\\", "/"))
            else:
                out.append("<pre>" + inline(code) + "</pre>")
            continue
        # 表格
        if ln.lstrip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i+1]):
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells); i += 1
            hdr = rows[0]; body = rows[2:]  # rows[1] 为分隔行
            html = ['<table>', "<tr>" + "".join("<th>%s</th>" % inline(c) for c in hdr) + "</tr>"]
            for r in body:
                html.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in r) + "</tr>")
            html.append("</table>")
            out.append("\n".join(html))
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lv = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lv, inline(m.group(2)), lv))
            i += 1; continue
        # 分隔线
        if re.match(r"^\s*-{3,}\s*$", ln):
            out.append("<hr>")
            i += 1; continue
        # 引用块（以 **图 X-Y** 开头的识别为图注）
        if ln.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i][1:].strip()); i += 1
            text = "<br>".join(inline(b) for b in buf)
            cls = ' class="figcap"' if text.startswith("<b>图 ") else ""
            out.append("<blockquote%s>%s</blockquote>" % (cls, text))
            continue
        # 列表
        if re.match(r"^\s*[-*]\s+", ln) or re.match(r"^\s*\d+\.\s+", ln):
            ordered = bool(re.match(r"^\s*\d+\.\s+", ln))
            items = []
            while i < n:
                m2 = re.match(r"^\s*([-*]|\d+\.)\s+(.*)$", lines[i])
                if m2:
                    items.append(m2.group(2)); i += 1
                elif lines[i].strip() and not re.match(r"^\s*(#{1,6}\s|```|>)", lines[i]) and items:
                    items[-1] += " " + lines[i].strip(); i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(t) for t in items), tag))
            continue
        # 空行
        if not ln.strip():
            i += 1; continue
        # 段落
        buf = []
        while i < n and lines[i].strip() and not re.match(r"^\s*(#{1,6}\s|```|>|\||[-*]\s|\d+\.\s|-{3,})", lines[i]):
            buf.append(lines[i].strip()); i += 1
        out.append("<p>" + inline("<br>".join(buf)) + "</p>")
    return "\n".join(out)

def main():
    text = io.open(SRC, encoding="utf-8").read()
    body = parse(text.splitlines())
    # 修正图片相对路径为 file://
    body = re.sub(r'<img src="([^"]+)"', lambda m: '<img src="%s"' % img_src(m.group(1)), body)
    css = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Microsoft YaHei", "SimSun", sans-serif; font-size: 10.5pt;
       line-height: 1.55; color: #222; margin: 0; }
h1 { font-size: 19pt; text-align: center; color: #1f3b57; margin: 4mm 0 4mm; }
h1 + p { text-align: center; font-size: 10pt; color: #5a6b7d; }
h2 { font-size: 14pt; color: #1f3b57; margin: 7mm 0 3mm; padding-bottom: 1.5mm;
     border-bottom: 1px solid #d5dee8; page-break-after: avoid; }
h3 { font-size: 11.5pt; color: #2c3e50; margin: 4mm 0 2mm; page-break-after: avoid; }
p { margin: 2mm 0; text-align: left; }
table { border-collapse: collapse; width: 100%; margin: 2mm 0 3mm; font-size: 9.5pt; }
th { background: #eef3f8; color: #1f3b57; text-align: left; }
th, td { border: 1px solid #c9d4e0; padding: 2.5px 6px; vertical-align: top; }
tr:nth-child(even) td { background: #f8fafc; }
tr { page-break-inside: avoid; }
blockquote { margin: 2mm 0 2mm 4mm; padding: 2mm 3mm; border-left: 3px solid #23405e;
             background: #f7f9fb; }
blockquote.figcap { margin: 1mm 0 3mm; padding: 0; border: none; background: none;
                    text-align: center; font-size: 9.5pt; color: #5a6b7d; }
pre { font-family: Consolas, monospace; font-size: 8.5pt; white-space: pre-wrap;
      margin: 2mm 0; padding: 2mm 3mm; background: #f5f7fa;
      border: 1px solid #dde4ec; border-radius: 3px; }
code { font-family: Consolas, monospace; font-size: 9.5pt; background: #eef1f5;
       padding: 0 2px; border-radius: 2px; word-break: break-all; }
pre code { background: none; padding: 0; }
img { max-width: 100%; border: 1px solid #d5dee8; }
hr { border: none; border-top: 1px solid #d5dee8; margin: 4mm 0; }
li { margin: 1mm 0; }
"""
    html = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>SupplyGuard 技术文档 v1.1</title>'
            "<style>%s</style></head><body>%s</body></html>" % (css, body))
    io.open(OUT, "w", encoding="utf-8").write(html)
    print("HTML written:", OUT, len(html), "bytes")

if __name__ == "__main__":
    main()
