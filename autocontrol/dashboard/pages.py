"""HTML for the web board: the overview, one project's day, and short messages.

A card is drawn from the same card JSON the Lark bot used (header, markdown,
checkers, icon rows, folding panels), so the board reads exactly like the
card. Pages are self-contained: no external scripts, styles or fonts.
"""

from __future__ import annotations

from datetime import date
from html import escape
import re

WEEKDAYS = '一二三四五六日'
COLOR_NAMES = frozenset({'grey', 'red', 'blue', 'green', 'orange', 'wathet', 'neutral'})
HEAD_NAMES = frozenset({'blue', 'green', 'orange', 'red', 'wathet', 'grey'})
FONT = re.compile(
    r'&lt;font color=(?:&#x27;|&quot;)([a-z]+)(?:&#x27;|&quot;)&gt;(.*?)&lt;/font&gt;', re.S
)
ICONS = {
    'ellipse_outlined': ('', ' ring'),
    'yes_outlined': ('✓', ' ring'),
    'warning_outlined': ('!', ' ring'),
    'maybe_outlined': ('?', ' ring'),
    'time_outlined': ('', ' ring clock'),
    'record_outlined': ('●', ''),
    'succeed_filled': ('✓', ''),
    'error_filled': ('✕', ''),
    'warning-hollow_filled': ('!', ''),
}
SLOT_STATES = {
    'restarting': '调度重启中',
    'stopped': '调度已停止',
    'stopping': '调度停止中',
    'disabled': '已停用',
    'invalid': '清单无效',
    'elsewhere': '调度在总控外运行',
    'start_failed': '调度启动失败',
    'pending': '等待启动',
}
MODES = {'observe': '只读读取', 'execute': '执行'}
ROUND_STATES = {
    'finished': '完成',
    'observed': '读取完成',
    'failed': '失败',
    'busy': '跳过（正忙）',
    'timeout': '超时',
    'interrupted': '中断',
}

STYLE = '''
:root{--bg:#f2f3f5;--panel:#fff;--text:#1f2329;--muted:#646a73;--faint:#8f959e;--line:#e4e5e7;
--grey:#8f959e;--red:#f54a45;--blue:#3370ff;--green:#2ea121;--orange:#de7802;--wathet:#1b9be0;
--neutral:#646a73;--hb-blue:#e1eaff;--hf-blue:#245bdb;--hb-green:#d9f5d6;--hf-green:#237b19;
--hb-orange:#feead2;--hf-orange:#b26206;--hb-red:#fde2e2;--hf-red:#c02a26;--hb-wathet:#e1f6fe;
--hf-wathet:#0f7bb5;--hb-grey:#eff0f1;--hf-grey:#51565d;--hb-default:#fff;--hf-default:#1f2329;
color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--panel:#222326;--text:#e8e9eb;--muted:#a6a9ad;
--faint:#7d8187;--line:#34363a;--grey:#8a8f96;--red:#f76964;--blue:#4c88ff;--green:#4cbf3f;
--orange:#f0a23a;--wathet:#3fb5f0;--neutral:#a6a9ad;--hb-blue:#1c2c52;--hf-blue:#9ab8ff;
--hb-green:#183a17;--hf-green:#8fdc84;--hb-orange:#45300f;--hf-orange:#f7c27a;--hb-red:#4a1f1f;
--hf-red:#ff9c98;--hb-wathet:#123446;--hf-wathet:#8fd6fa;--hb-grey:#2b2d31;--hf-grey:#c3c6ca;
--hb-default:#222326;--hf-default:#e8e9eb}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue","Microsoft YaHei",sans-serif;
-webkit-text-size-adjust:100%}
a{color:var(--blue);text-decoration:none}
.wrap{max-width:720px;margin:0 auto;padding:12px 16px 32px}
.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0 2px}
.brand{font-size:17px;font-weight:600;margin-right:auto}
.pill{font-size:12px;border-radius:10px;padding:1px 8px;border:1px solid;white-space:nowrap}
.pill.ok{color:var(--green);border-color:var(--green)}
.pill.bad{color:var(--red);border-color:var(--red)}
.meta{font-size:12px;color:var(--faint);margin-bottom:12px}
.note{font-size:12px;color:var(--muted);background:var(--panel);border-radius:8px;padding:8px 12px;margin:0 0 12px}
.list{display:flex;flex-direction:column;gap:10px}
.proj{display:block;background:var(--panel);border-radius:10px;padding:10px 14px;color:var(--text);
border-left:4px solid var(--grey);box-shadow:0 1px 3px rgba(0,0,0,.06)}
.proj.h-blue{border-left-color:var(--blue)}.proj.h-green{border-left-color:var(--green)}
.proj.h-orange{border-left-color:var(--orange)}.proj.h-red{border-left-color:var(--red)}
.proj.h-wathet{border-left-color:var(--wathet)}
.proj .r1{display:flex;align-items:baseline;gap:8px}
.proj .name{font-weight:600;font-size:15px;margin-right:auto}
.proj .day{font-size:12px;color:var(--faint);white-space:nowrap}
.proj .sum{color:var(--muted);font-size:13px}
.bar{height:4px;border-radius:2px;background:var(--line);margin:6px 0 2px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--blue);border-radius:2px}
.proj.h-green .bar i{background:var(--green)}.proj.h-orange .bar i{background:var(--orange)}
.proj.h-red .bar i{background:var(--red)}
.proj .warn{color:var(--red);font-size:12px}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.tag{font-size:11px;font-weight:500;border:1px solid currentColor;border-radius:4px;padding:0 5px;
background:var(--panel)}
.crumbs{margin:4px 0 8px;font-size:13px}
.days{display:flex;align-items:center;gap:10px;font-size:13px;margin:0 0 10px;color:var(--muted)}
.days .now{font-weight:600;color:var(--text)}
.days .gap{flex:1}
.card{background:var(--panel);border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:hidden}
.head{padding:12px 16px 10px;background:var(--hb-default);color:var(--hf-default)}
.head.h-blue{background:var(--hb-blue);color:var(--hf-blue)}
.head.h-green{background:var(--hb-green);color:var(--hf-green)}
.head.h-orange{background:var(--hb-orange);color:var(--hf-orange)}
.head.h-red{background:var(--hb-red);color:var(--hf-red)}
.head.h-wathet{background:var(--hb-wathet);color:var(--hf-wathet)}
.head.h-grey{background:var(--hb-grey);color:var(--hf-grey)}
.head h1{margin:0;font-size:16px;font-weight:600;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.head p{margin:2px 0 0;font-size:12px;opacity:.85}
.body{padding:8px 16px 12px;display:flex;flex-direction:column;gap:8px}
details summary{list-style:none;cursor:pointer;padding:4px 0;display:flex;gap:6px;align-items:flex-start}
details summary .t{flex:1;min-width:0}
details summary::-webkit-details-marker{display:none}
details>summary::after{content:"⌄";margin-left:auto;color:var(--faint)}
details[open]>summary::after{content:"⌃"}
.panel{display:flex;flex-direction:column;gap:6px;padding-bottom:4px}
.todo{display:flex;gap:8px;align-items:flex-start}
.box{flex:none;width:14px;height:14px;margin-top:3px;border:1.5px solid var(--faint);border-radius:3px;
font-size:11px;line-height:12px;text-align:center;color:#fff}
.todo.done .box{background:var(--blue);border-color:var(--blue)}
.todo.done .text{text-decoration:line-through;opacity:.55}
.mark{flex:none;width:14px;height:14px;margin-top:3px;font-size:12px;font-weight:700;line-height:14px;
text-align:center;color:var(--neutral);border-color:var(--neutral)}
.mark.ring{width:13px;height:13px;margin:3.5px .5px 0;border:1.4px solid;border-radius:50%;font-size:8px;
line-height:10px;position:relative}
.mark.spin{border-color:var(--blue);border-top-color:transparent;animation:spin 1.08s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.mark.clock::before,.mark.clock::after{content:"";position:absolute;left:4.6px;background:currentColor;
width:1.3px;border-radius:1px}
.mark.clock::before{top:2px;height:4.2px}
.mark.clock::after{top:5px;height:3.4px;transform:rotate(-90deg);transform-origin:50% .6px}
.md.small{font-size:12px}.md.indent{margin:-4px 0 0 24px}
.c-grey{color:var(--grey)}.c-red{color:var(--red)}.c-blue{color:var(--blue)}.c-green{color:var(--green)}
.c-orange{color:var(--orange)}.c-wathet{color:var(--wathet)}.c-neutral{color:var(--neutral)}
.mark.c-grey,.tag.c-grey{border-color:var(--grey)}.mark.c-red,.tag.c-red{border-color:var(--red)}
.mark.c-blue,.tag.c-blue{border-color:var(--blue)}.mark.c-green,.tag.c-green{border-color:var(--green)}
.mark.c-orange,.tag.c-orange{border-color:var(--orange)}.mark.c-wathet,.tag.c-wathet{border-color:var(--wathet)}
.section{background:var(--panel);border-radius:10px;padding:10px 14px;margin-top:12px}
.section h2{font-size:14px;margin:0 0 6px}
.rounds{list-style:none;margin:0;padding:0;font-size:13px}
.rounds li{display:flex;gap:8px;padding:3px 0;border-top:1px solid var(--line)}
.rounds li:first-child{border-top:0}
.rounds .when{color:var(--faint);white-space:nowrap}
.rounds .what{flex:1}
.rounds .bad{color:var(--red)}
.rounds .dur{color:var(--faint);white-space:nowrap}
.log{margin:0;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;
word-break:break-all;color:var(--muted);max-height:420px;overflow:auto}
.kv{font-size:13px;color:var(--muted)}
.empty{color:var(--faint);padding:12px 0}
.foot{margin-top:20px;font-size:12px;color:var(--faint);text-align:center}
.msg{max-width:420px;margin:64px auto;background:var(--panel);border-radius:10px;padding:20px 22px}
.msg h1{font-size:17px;margin:0 0 8px}
.msg p{margin:6px 0;color:var(--muted)}
.btn{display:inline-block;margin-top:10px;background:var(--blue);color:#fff;border-radius:6px;padding:6px 16px;
border:0;font:inherit;cursor:pointer}
.btn.danger{background:var(--red)}
.btn.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
.btn.small{margin:0;padding:2px 10px;font-size:12px}
form.inline{display:inline;margin:0}
.people{list-style:none;margin:0;padding:0;font-size:14px}
.people li{display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--line)}
.people li:first-child{border-top:0}
.people .who{flex:1;min-width:0}
.people .when{color:var(--faint);font-size:12px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
'''

REFRESH = '''
(function(){
var main=document.getElementById('main');if(!main)return;
function keep(){var o={};main.querySelectorAll('details[data-key]').forEach(function(d){o[d.dataset.key]=d.open});return o}
function put(o){main.querySelectorAll('details[data-key]').forEach(function(d){if(d.dataset.key in o)d.open=o[d.dataset.key]})}
var busy=false;
function refresh(){
if(busy||document.visibilityState!=='visible')return;busy=true;
fetch(location.href,{headers:{'X-Board-Partial':'1'},credentials:'same-origin',cache:'no-store'})
.then(function(r){if(r.status===401){location.reload();return null}
var t=r.headers.get('X-Board-Time');var s=document.getElementById('stamp');if(t&&s)s.textContent=t;
return r.ok?r.text():null})
.then(function(html){if(html!==null&&html!==undefined){var o=keep();main.innerHTML=html;put(o)}})
.catch(function(){}).then(function(){busy=false})}
setInterval(refresh,30000);
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible')refresh()});
})();
'''


def _color(name) -> str:
    return f' c-{name}' if name in COLOR_NAMES else ''


def inline(text) -> str:
    """Lark markdown as the card uses it: <font color>, **bold**, ~~strike~~, line breaks."""
    html = escape(text if isinstance(text, str) else '')
    html = FONT.sub(
        lambda m: f'<span class="{_color(m[1]).strip()}">{m[2]}</span>'
        if m[1] in COLOR_NAMES
        else m[2],
        html,
    )
    html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', html)
    html = re.sub(r'~~(.+?)~~', r'<s>\1</s>', html)
    return html.replace('\n', '<br>')


def _text(element) -> str:
    text = element.get('text') if isinstance(element.get('text'), dict) else {}
    return inline(text.get('content', ''))


def element_html(element) -> str:
    if not isinstance(element, dict):
        return ''
    tag = element.get('tag')
    if tag == 'markdown':
        size = ' small' if element.get('text_size') == 'notation' else ''
        indent = ' indent' if str(element.get('margin', '0px')).endswith('24px') else ''
        return f'<div class="md{size}{indent}">{inline(element.get("content", ""))}</div>'
    if tag == 'checker':
        done = ' done' if element.get('checked') else ''
        tick = '✓' if element.get('checked') else ''
        return f'<div class="todo{done}"><span class="box">{tick}</span><span class="text">{_text(element)}</span></div>'
    if tag == 'div':
        icon = element.get('icon') if isinstance(element.get('icon'), dict) else {}
        token = icon.get('token')
        if icon.get('tag') == 'custom_icon' or token == 'loading_outlined':
            return f'<div class="todo"><span class="mark ring spin"></span><span class="text">{_text(element)}</span></div>'
        glyph, shape = ICONS.get(token, ('•', ''))
        return (
            f'<div class="todo"><span class="mark{shape}{_color(icon.get("color"))}">{glyph}</span>'
            f'<span class="text">{_text(element)}</span></div>'
        )
    if tag == 'collapsible_panel':
        header = element.get('header') if isinstance(element.get('header'), dict) else {}
        title = header.get('title') if isinstance(header.get('title'), dict) else {}
        inner = ''.join(element_html(child) for child in element.get('elements') or ())
        key = escape(str(element.get('element_id') or ''))
        opened = ' open' if element.get('expanded') else ''
        return (
            f'<details data-key="{key}"{opened}><summary><span class="t">{inline(title.get("content", ""))}'
            f'</span></summary><div class="panel">{inner}</div></details>'
        )
    return ''


def _header(card) -> dict:
    return card.get('header') if isinstance(card.get('header'), dict) else {}


def _template(card) -> str:
    name = _header(card).get('template')
    return f' h-{name}' if name in HEAD_NAMES else ''


def _tags(card, skip='') -> str:
    tags = []
    for tag in _header(card).get('text_tag_list') or ():
        text = tag.get('text', {}).get('content', '') if isinstance(tag, dict) else ''
        if text and text not in skip:
            tags.append(f'<span class="tag{_color(tag.get("color"))}">{escape(text)}</span>')
    return ''.join(tags)


def summary_line(card) -> str:
    config = card.get('config') if isinstance(card.get('config'), dict) else {}
    summary = config.get('summary') if isinstance(config.get('summary'), dict) else {}
    return summary.get('content', '') if isinstance(summary.get('content'), str) else ''


def card_html(card) -> str:
    header = _header(card)
    title = escape(header.get('title', {}).get('content', ''))
    subtitle = escape(header.get('subtitle', {}).get('content', ''))
    body = card.get('body') if isinstance(card.get('body'), dict) else {}
    elements = ''.join(element_html(element) for element in body.get('elements') or ())
    return (
        f'<div class="card"><div class="head{_template(card)}"><h1>{title}{_tags(card)}</h1>'
        f'<p>{subtitle}</p></div><div class="body">{elements}</div></div>'
    )


def day_label(day) -> str:
    try:
        value = date.fromisoformat(day)
    except (TypeError, ValueError):
        return escape(str(day or ''))
    return f'{value.month}月{value.day}日 周{WEEKDAYS[value.weekday()]}'


def _console_pill(console) -> str:
    if console.get('running'):
        return '<span class="pill ok">总控运行中</span>'
    return '<span class="pill bad">总控未运行</span>'


def page(title, main, *, nonce, stamp='', console=None, user=None, note='', admin=False) -> str:
    """A whole page; ``main`` is also what an auto-refresh swaps in."""
    pill = _console_pill(console) if console is not None else ''
    meta = (
        f'<div class="meta">更新于 <span id="stamp">{escape(stamp)}</span> · 每 30 秒自动刷新</div>'
        if stamp
        else ''
    )
    members = ' · <a href="/admin">成员</a>' if admin else ''
    foot = (
        f'<div class="foot">已登录：{escape(user)}{members} · <a href="/logout">退出</a></div>'
        if user
        else ''
    )
    script = f'<script nonce="{nonce}">{REFRESH}</script>' if stamp else ''
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>{escape(title)}</title><style>{STYLE}</style></head>
<body><div class="wrap"><div class="top"><a class="brand" href="/" style="color:inherit">自动执行看板</a>{pill}</div>
{meta}{note}<main id="main">{main}</main>{foot}</div>{script}</body></html>
'''


def overview_main(boards, console) -> str:
    """One row per project: its card's summary, colour and tags."""
    if not boards:
        return '<div class="empty">登记表里还没有项目。</div>'
    slots = console.get('projects') if isinstance(console.get('projects'), dict) else {}
    rows = []
    for board in boards:
        name = board['name']
        card = board.get('card')
        slot = slots.get(name, {}) if isinstance(slots.get(name), dict) else {}
        warnings = []
        if console.get('running') and slot.get('state') in SLOT_STATES:
            warnings.append(SLOT_STATES[slot['state']])
        if slot.get('problem'):
            warnings.append(f"问题：{slot['problem']}")
        if board.get('error'):
            warnings.append(board['error'])
        day = '' if board.get('day') == board.get('today') else day_label(board.get('day'))
        summary = summary_line(card) if card else '今天还没有卡片'
        warn = f'<div class="warn">{escape("；".join(warnings))}</div>' if warnings else ''
        # The header tags mostly repeat the summary line; keep only what it lacks.
        extra = _tags(card, skip=summary) if card else ''
        tags = f'<div class="tags">{extra}</div>' if extra else ''
        done, total = board.get('progress') or (0, 0)
        bar = (
            f'<div class="bar"><i style="width:{round(100 * done / total)}%"></i></div>'
            if total
            else ''
        )
        rows.append(
            f'<a class="proj{_template(card) if card else ""}" href="/p/{escape(name)}">'
            f'<div class="r1"><span class="name">{escape(board["title"])}</span>'
            f'<span class="day">{day}</span></div>'
            f'<div class="sum">{escape(summary)}</div>{bar}{tags}{warn}</a>'
        )
    return f'<div class="list">{"".join(rows)}</div>'


def _round_row(row, tz) -> str:
    from .data import stamp

    status = row.get('status')
    code = row.get('code')
    bad = status not in ('finished', 'observed') or (code or 0) != 0
    if status == 'finished' and code not in (None, 0):
        result = '跑完，有阶段失败'
    else:
        result = ROUND_STATES.get(status, escape(str(status)))
        if code not in (None, 0):
            result += f'（退出码 {escape(str(code))}）'
    seconds = (row.get('duration_ms') or 0) // 1000
    duration = f'{seconds // 60} 分 {seconds % 60} 秒' if seconds >= 60 else f'{seconds} 秒'
    mode = MODES.get(row.get('mode'), escape(str(row.get('mode') or '')))
    phases = f" · {row['phases']} 个阶段" if row.get('phases') else ''
    return (
        f'<li><span class="when">{escape(stamp(row.get("started_at"), tz))}</span>'
        f'<span class="what">{mode}{phases} · <span class="{"bad" if bad else ""}">{result}</span></span>'
        f'<span class="dur">{duration}</span></li>'
    )


def project_main(board, *, rounds, log, slot, tz) -> str:
    """One project's day: the card, then its recent rounds, process and log."""
    name = escape(board['name'])
    days = board.get('days') or []
    day = board.get('day')
    older = next((d for d in days if day and d < day), None)
    newer = next((d for d in reversed(days) if day and d > day), None)
    nav = ['<div class="days">']
    nav.append(
        f'<a href="/p/{name}?day={escape(older)}">‹ {escape(older[5:])}</a>'
        if older
        else '<span></span>'
    )
    nav.append(
        f'<span class="gap"></span><span class="now">{day_label(day)}</span><span class="gap"></span>'
    )
    if newer:
        target = '' if newer == board.get('today') else f'?day={escape(newer)}'
        nav.append(f'<a href="/p/{name}{target}">{escape(newer[5:])} ›</a>')
    else:
        nav.append('<span></span>')
    nav.append('</div>')
    if board.get('card'):
        card = card_html(board['card'])
    elif board.get('error'):
        card = f'<div class="section"><div class="warn">{escape(board["error"])}</div></div>'
    else:
        card = '<div class="section empty">这一天还没有卡片。</div>'
    parts = ['<div class="crumbs"><a href="/">‹ 全部项目</a></div>', ''.join(nav), card]
    if rounds:
        items = ''.join(_round_row(row, tz) for row in rounds)
        parts.append(f'<div class="section"><h2>最近几轮</h2><ul class="rounds">{items}</ul></div>')
    if slot:
        facts = [
            SLOT_STATES.get(
                slot.get('state'),
                '调度运行中' if slot.get('state') == 'running' else str(slot.get('state') or '—'),
            )
        ]
        if slot.get('pid'):
            facts.append(f"pid {slot['pid']}")
        if slot.get('started_at'):
            facts.append(f"启动于 {slot['started_at']}")
        if slot.get('restarts'):
            facts.append(f"异常退出 {slot['restarts']} 次")
        lines = [' · '.join(escape(str(fact)) for fact in facts)]
        if slot.get('problem'):
            lines.append(f"问题：{escape(str(slot['problem']))}")
        if slot.get('pending'):
            lines.append(f"待加载：{escape(str(slot['pending']))}")
        parts.append(
            '<div class="section"><h2>调度进程</h2><div class="kv">'
            + '<br>'.join(lines)
            + '</div></div>'
        )
    if log:
        text = escape('\n'.join(reversed(log)))
        parts.append(f'<div class="section"><h2>调度日志（新的在上）</h2><pre class="log">{text}</pre></div>')
    return ''.join(parts)


def _form(action, fields, label, kind='', csrf='') -> str:
    hidden = ''.join(
        f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
        for key, value in (*fields.items(), ('csrf', csrf))
    )
    return (
        f'<form class="inline" method="post" action="{escape(action)}">{hidden}'
        f'<button class="btn small {kind}" type="submit">{escape(label)}</button></form>'
    )


def _when(stamp, tz) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(float(stamp), tz).strftime('%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def members_main(*, owner, allowed, requests, notify, csrf, tz) -> str:
    """The owner's page: pending requests first, then everyone who may look."""
    parts = ['<div class="crumbs"><a href="/">‹ 全部项目</a></div>']
    pending = [item for item in requests if item.get('status') == 'pending']
    denied = [item for item in requests if item.get('status') == 'denied']
    if pending:
        rows = ''.join(
            f'<li><span class="who">{escape(item.get("name") or "未命名")}'
            f'<br><span class="when">申请于 {_when(item.get("last") or item.get("asked"), tz)}</span></span>'
            + _form(f'/admin/requests/{item["id"]}', {'do': 'approve'}, '批准', '', csrf)
            + _form(f'/admin/requests/{item["id"]}', {'do': 'deny'}, '拒绝', 'ghost', csrf)
            + '</li>'
            for item in pending
        )
        parts.append(f'<div class="section"><h2>待批准</h2><ul class="people">{rows}</ul></div>')
    rows = [
        f'<li><span class="who">{escape(owner or "管理员")}</span><span class="when">管理员</span></li>'
    ]
    for person in allowed:
        rows.append(
            f'<li><span class="who">{escape(person["name"])}</span>'
            + _form(
                '/admin/people', {'do': 'remove', 'open_id': person['open_id']}, '移出', 'ghost', csrf
            )
            + '</li>'
        )
    parts.append(
        f'<div class="section"><h2>能查看看板的人</h2><ul class="people">{"".join(rows)}</ul></div>'
    )
    if denied:
        rows = ''.join(
            f'<li><span class="who">{escape(item.get("name") or "未命名")}'
            f'<br><span class="when">拒绝于 {_when(item.get("decided"), tz)}</span></span>'
            + _form(f'/admin/requests/{item["id"]}', {'do': 'approve'}, '改为批准', 'ghost', csrf)
            + '</li>'
            for item in denied
        )
        parts.append(f'<div class="section"><h2>已拒绝</h2><ul class="people">{rows}</ul></div>')
    tip = (
        '有人申请时，群机器人会在飞书里提醒你。'
        if notify
        else '还没配置群机器人提醒：有人申请时只会出现在这一页（配置：run.py dashboard webhook）。'
    )
    parts.append(f'<div class="note" style="margin-top:12px">{escape(tip)}</div>')
    return ''.join(parts)


def confirm_main(item, verb, csrf) -> str:
    """One request from a card button: confirm with a tap (the link alone changes nothing)."""
    name = escape(item.get('name') or '未命名')
    if item.get('status') != 'pending' and not (
        verb == 'approve' and item.get('status') == 'denied'
    ):
        done = {'approved': '已经批准过了', 'denied': '已经拒绝过了'}.get(item.get('status'), '已经处理过了')
        return (
            f'<div class="section"><h2>{name}</h2><p class="kv">{done}。</p>'
            '<div class="actions"><a class="btn ghost" href="/admin">成员管理</a></div></div>'
        )
    question = f'让 {name} 查看看板？' if verb == 'approve' else f'拒绝 {name} 的申请？'
    button = _form(
        f'/admin/requests/{item["id"]}',
        {'do': verb},
        '批准' if verb == 'approve' else '拒绝',
        '' if verb == 'approve' else 'danger',
        csrf,
    )
    return (
        f'<div class="section"><h2>{question}</h2>'
        '<p class="kv">批准后对方重新打开看板就能看到；以后随时可以在「成员」页移出。</p>'
        f'<div class="actions">{button}<a class="btn ghost small" href="/admin">去成员管理</a></div></div>'
    )


def message(title, lines, *, link=None, label=None, nonce='') -> str:
    body = ''.join(f'<p>{escape(line)}</p>' for line in lines)
    button = f'<a class="btn" href="{escape(link)}">{escape(label or "继续")}</a>' if link else ''
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{escape(title)}</title><style>{STYLE}</style></head>
<body><div class="msg"><h1>{escape(title)}</h1>{body}{button}</div></body></html>
'''
