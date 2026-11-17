"""Approximate HTML preview of a rendered card, for reviewing layout offline.

This is a development aid only: it understands the handful of components the
board uses (header, markdown, checker, collapsible_panel) and imitates their
look. Lark itself remains the reference rendering.
"""

from __future__ import annotations

from html import escape
import re

COLORS = {
    'grey': '#8f959e',
    'red': '#f54a45',
    'blue': '#3370ff',
    'green': '#2ea121',
    'orange': '#de7802',
    'wathet': '#1b9be0',
    'neutral': '#646a73',
}
HEADERS = {
    'blue': ('#e1eaff', '#245bdb'),
    'green': ('#d9f5d6', '#237b19'),
    'orange': ('#feead2', '#b26206'),
    'red': ('#fde2e2', '#c02a26'),
    'wathet': ('#e1f6fe', '#0f7bb5'),
    'grey': ('#eff0f1', '#51565d'),
    'default': ('#ffffff', '#1f2329'),
}
FONT = re.compile(r'&lt;font color=&#x27;([a-z]+)&#x27;&gt;(.*?)&lt;/font&gt;', re.S)


def _inline(text: str) -> str:
    html = escape(text)
    html = FONT.sub(lambda m: f'<span style="color:{COLORS.get(m[1], m[1])}">{m[2]}</span>', html)
    html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', html)
    html = re.sub(r'~~(.+?)~~', r'<s>\1</s>', html)
    return html.replace('\n', '<br>')


def _element(element: dict) -> str:
    tag = element.get('tag')
    if tag == 'markdown':
        size = 'small' if element.get('text_size') == 'notation' else ''
        indent = ' indent' if element.get('margin', '0px').endswith('24px') else ''
        return f'<div class="md {size}{indent}">{_inline(element.get("content", ""))}</div>'
    if tag == 'checker':
        checked = element.get('checked')
        text = _inline(element.get('text', {}).get('content', ''))
        return (
            f'<div class="todo{" done" if checked else ""}">'
            f'<span class="box">{"✓" if checked else ""}</span><span class="text">{text}</span></div>'
        )
    if tag == 'div':
        icon = element.get('icon') or {}
        token = icon.get('token')
        glyph = {
            'ellipse_outlined': '',  # drawn as an outlined ring by CSS
            'record_outlined': '●',
            'succeed_filled': '✓',
            'error_filled': '✕',
            'warning-hollow_filled': '!',
        }.get(token, '•')
        ring = ' ring' if token == 'ellipse_outlined' else ''
        color = COLORS.get(icon.get('color'), '#646a73')
        text = _inline(element.get('text', {}).get('content', ''))
        return (
            f'<div class="todo"><span class="mark{ring}" style="color:{color};border-color:{color}">'
            f'{glyph}</span><span class="text">{text}</span></div>'
        )
    if tag == 'collapsible_panel':
        title = _inline(element.get('header', {}).get('title', {}).get('content', ''))
        inner = ''.join(_element(child) for child in element.get('elements', ()))
        opened = ' open' if element.get('expanded') else ''
        return (
            f'<details{opened}><summary>{title}</summary><div class="panel">{inner}</div></details>'
        )
    return f'<pre>{escape(str(element))}</pre>'


def card_html(card: dict, *, title: str = '卡片预览') -> str:
    header = card.get('header', {})
    background, foreground = HEADERS.get(header.get('template', 'default'), HEADERS['default'])
    tags = ''.join(
        f'<span class="tag" style="color:{COLORS.get(tag.get("color"), "#646a73")};'
        f'border-color:{COLORS.get(tag.get("color"), "#646a73")}">'
        f'{escape(tag.get("text", {}).get("content", ""))}</span>'
        for tag in header.get('text_tag_list', ())
    )
    body = ''.join(_element(element) for element in card.get('body', {}).get('elements', ()))
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
  body {{ margin: 0; padding: 24px 16px; background: #f2f3f5;
         font: 14px/1.6 -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; color: #1f2329; }}
  .card {{ max-width: 420px; margin: 0 auto; background: #fff; border-radius: 10px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; }}
  .head {{ padding: 12px 16px 10px; background: {background}; color: {foreground}; }}
  .head h1 {{ margin: 0; font-size: 16px; font-weight: 600; display: flex; gap: 6px;
              align-items: center; flex-wrap: wrap; }}
  .head p {{ margin: 2px 0 0; font-size: 12px; opacity: .85; }}
  .tag {{ font-size: 11px; font-weight: 500; border: 1px solid; border-radius: 4px;
          padding: 0 5px; background: #fff; }}
  .body {{ padding: 8px 16px 12px; display: flex; flex-direction: column; gap: 8px; }}
  details summary {{ list-style: none; cursor: pointer; padding: 4px 0; display: flex; }}
  details > summary::after {{ content: "⌄"; margin-left: auto; color: #8f959e; }}
  details[open] > summary::after {{ content: "⌃"; }}
  .panel {{ display: flex; flex-direction: column; gap: 6px; padding-bottom: 4px; }}
  .todo {{ display: flex; gap: 8px; align-items: flex-start; }}
  .box {{ flex: none; width: 14px; height: 14px; margin-top: 3px; border: 1.5px solid #bbbfc4;
          border-radius: 3px; font-size: 11px; line-height: 14px; text-align: center; color: #fff; }}
  .todo.done .box {{ background: #3370ff; border-color: #3370ff; }}
  .mark {{ flex: none; width: 14px; height: 14px; margin-top: 3px; font-size: 12px; font-weight: 700;
           line-height: 14px; text-align: center; }}
  .mark.ring {{ box-sizing: border-box; width: 12px; height: 12px; margin: 4px 1px 0;
                border: 1.5px solid; border-radius: 50%; }}
  .todo.done .text {{ text-decoration: line-through; opacity: .5; }}
  .md.small {{ font-size: 12px; }}
  .md.indent {{ margin: -4px 0 0 24px; }}
  .note {{ max-width: 420px; margin: 12px auto 0; font-size: 12px; color: #8f959e; }}
</style></head>
<body><div class="card">
<div class="head"><h1>{escape(header.get('title', {}).get('content', ''))}{tags}</h1>
<p>{escape(header.get('subtitle', {}).get('content', ''))}</p></div>
<div class="body">{body}</div></div>
<div class="note">本地示意预览，飞书客户端的实际渲染为准。</div>
</body></html>
'''
