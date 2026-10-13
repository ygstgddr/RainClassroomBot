"""Load trusted bundled UI files without publishing an HTTP directory."""
from pathlib import Path
import re
import sys


def load_document():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'gui' / 'web'
    html = (base / 'index.html').read_text(encoding='utf8')
    css = (base / 'app.css').read_text(encoding='utf8')
    javascript = (base / 'app.js').read_text(encoding='utf8')
    html, replaced_css = re.subn(r'<link\b[^>]*href=[\"\']app\.css[\"\'][^>]*>',
                                 lambda _: '<style>' + css + '</style>', html)
    html, replaced_js = re.subn(r'<script\b[^>]*src=[\"\']app\.js[\"\'][^>]*>\s*</script>',
                                lambda _: '<script>' + javascript + '</script>', html)
    if replaced_css != 1 or replaced_js != 1:
        raise RuntimeError('界面资源未完整加载，请重新解压整个程序包')
    return html
