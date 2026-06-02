import sys, subprocess, pathlib, markdown

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CSS = """
@page { size: A4; margin: 9mm 11mm; }
* { box-sizing: border-box; }
body { font-family: "Microsoft YaHei","Segoe UI","Calibri",sans-serif;
       font-size: 8.9pt; line-height: 1.32; color: #1a1a1a; max-width: 100%; }
h1 { font-size: 13.5pt; margin: 0 0 4px; border-bottom: 2px solid #0A7B83; padding-bottom: 3px; color:#0A7B83; }
h2 { font-size: 10.3pt; margin: 8px 0 3px; color:#0A7B83; }
blockquote { background:#f4f8f8; border-left:3px solid #0A7B83; margin:5px 0;
             padding:5px 9px; font-size:8.2pt; color:#333; }
blockquote p { margin:2px 0; }
table { border-collapse: collapse; width:100%; margin:5px 0; font-size:8pt; }
th,td { border:1px solid #ccc; padding:2.5px 6px; text-align:left; vertical-align:top; }
th { background:#0A7B83; color:#fff; }
tr:nth-child(even) td { background:#f6f9f9; }
code { background:#eef2f2; padding:1px 3px; border-radius:3px; font-size:7.7pt;
       font-family:"Consolas",monospace; }
ul,ol { margin:3px 0 3px 0; padding-left:18px; }
li { margin:1px 0; }
hr { border:none; border-top:1px solid #ddd; margin:6px 0; }
p { margin:3px 0; }
strong { color:#0A2540; }
"""

def convert(md_path):
    md_path = pathlib.Path(md_path).resolve()
    html_body = markdown.markdown(
        md_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    html = f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html_body}</body></html>"
    html_path = md_path.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    pdf_path = md_path.with_suffix(".pdf")
    subprocess.run([
        EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}", html_path.as_uri(),
    ], check=True, timeout=90,
       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"OK -> {pdf_path}  ({pdf_path.stat().st_size} bytes)")

if __name__ == "__main__":
    for p in sys.argv[1:]:
        convert(p)
