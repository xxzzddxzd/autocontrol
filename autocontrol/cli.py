"""Command line: console, autostart, register, projects, serve, once, check, metrics, preview,
chats, dashboard and cards."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from . import locks, projects
from .board.card import CardRenderer
from .board.preview import card_html

ENTRY = Path(__file__).resolve().parents[1] / 'run.py'


def _project(args):
    options = {'workdir': args.workdir, 'private': args.private, 'python': args.python}
    if args.manifest:
        from .projects.manifest import ManifestError, ManifestProject

        try:
            return ManifestProject(args.manifest, **options)
        except ManifestError as error:
            raise SystemExit('接入清单无效：\n  ' + '\n  '.join(error.problems)) from None
    return projects.load(args.project, **options)


def _gate(project, args) -> str | None:
    """Protocol projects run only after their current manifest passed admission."""
    if project.manifest_path is None or args.skip_admission:
        return None
    from .admission import admitted

    ok, why = admitted(project)
    return None if ok else why


def _publisher(project):
    from .lark.publisher import CardPublisher, cards_switch_path

    return CardPublisher(
        project.private,
        project.render,
        project.sanitize,
        config_path=project.lark_config,
        uuid_namespace=project.uuid_namespace,
        switch_path=cards_switch_path(),
    )


def _metrics(project):
    from .metrics import MetricsStore

    try:
        return MetricsStore(project.metrics_db)
    except Exception:
        return None  # metrics are optional; a broken store never blocks a round


def cmd_once(args) -> int:
    from .runner import RoundRunner

    project = _project(args)
    if args.phase and args.phase not in project.commands:
        raise SystemExit(f'unknown phase {args.phase!r}; choose from {", ".join(project.commands)}')
    if why := _gate(project, args):
        print(why, file=sys.stderr)
        return 3
    publisher = _publisher(project)
    try:
        runner = RoundRunner(project, publisher, metrics=_metrics(project))
        try:
            result = runner.run_once(observe_only=args.observe_only, phase=args.phase, manual=True)
        except locks.AlreadyRunning:
            result = {'status': 'busy', 'code': 2}
        print(json.dumps(result, ensure_ascii=False))
        return int(result.get('code', 0))
    finally:
        publisher.close()


# Why the console (re)started a scheduler; written to its log.
START_REASONS = {
    'boot': '总控启动',
    'code': '总控代码更新',
    'manifest': '新清单通过准入',
    'settings': '登记表设置变更',
    'restart': '被停止后重启',
    'crash': '异常退出后重启',
    'manual': '手动启动',
}


def _serve_log(project):
    """One timestamped line per scheduling decision, on stderr (the console's log file)."""

    def log(text: str) -> None:
        try:
            stamp = project.schedule.now().strftime('%m-%d %H:%M:%S')
            print(f'{stamp} {text}', file=sys.stderr, flush=True)
        except Exception:
            pass  # a log line never affects scheduling

    return log


def cmd_serve(args) -> int:
    from .runner import RoundRunner
    from .service import run_service

    project = _project(args)
    if why := _gate(project, args):
        print(why, file=sys.stderr)
        return 3
    try:
        lock_fd = locks.acquire(project.coordinator_lock)
    except locks.AlreadyRunning:
        return 0  # this project's scheduler is already running
    publisher = _publisher(project)
    try:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        runner = RoundRunner(project, publisher, metrics=_metrics(project))
        log = _serve_log(project)
        start = '不登录，沿用已保存的卡片' if args.resume else '先只读读取' if args.observe_first else '立即补跑一轮'
        log(f'调度启动（{START_REASONS.get(args.reason, args.reason)}）：{start}，pid {os.getpid()}')
        run_service(
            runner, stop=stop, observe_first=args.observe_first, resume=args.resume, log=log
        )
        return 0
    finally:
        publisher.close()
        os.close(lock_fd)


def cmd_check(args) -> int:
    from .admission import format_report, run_check

    if args.manifest:
        report = run_check(args.manifest, workdir=args.workdir, private=args.private,
                           live=not args.static, repeat=args.repeat)  # fmt: skip
    else:
        from .admission import run_adapter_check

        report = run_adapter_check(_project(args), live=not args.static)

    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
    return 0 if report.get('passed') else 1


def cmd_metrics(args) -> int:
    from .metrics import MetricsStore

    project = _project(args)
    if not project.metrics_db.exists():
        print('还没有运行指标记录', file=sys.stderr)
        return 1
    report = MetricsStore(project.metrics_db).report(
        project.name, days=args.days, today=project.schedule.service_day(),
        phase_timeout=project.phase_timeout,
    )  # fmt: skip
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_metrics(report))
    return 0 if report['healthy'] else 1


def format_metrics(report) -> str:
    def pct(value):
        return '—' if value is None else f'{value * 100:.1f}%'

    def num(value):
        return '—' if value is None else value

    lines = [
        f"{report['project']} 近 {report['days']} 天运行指标（自 {report['since']}，{report['rounds']} 轮）"
    ]
    lines.append(f"  最近一次成功：{report['last_success_at'] or '—'}")
    for name, stats in report['phases'].items():
        lines.append(
            f"  阶段 {name}: {stats['runs']} 次，成功 {pct(stats['success_rate'])}，超时 {pct(stats['timeout_rate'])}，"
            f"均值 {stats['avg_ms'] or '—'} ms，P95 {stats['p95_ms'] or '—'} ms，"
            f"请求 {stats['actions_sent']}/确认 {num(stats['actions_confirmed'])}/未确认 {num(stats['actions_unconfirmed'])}"
        )
        if stats['failure_stages']:
            lines.append(
                '      失败阶段：' + '，'.join(f'{k}×{v}' for k, v in stats['failure_stages'].items())
            )
    lines.append('  指标检查：')
    for check in report['checks']:
        mark = '—' if check['ok'] is None else ('✓' if check['ok'] else '✗')
        which = ''
        if check['indicator'] == 'task_failure_streak' and report.get('task_streaks'):
            which = '，' + '、'.join(
                f'{task} {count} 轮' for task, count in report['task_streaks'].items()
            )
        lines.append(
            f"    {mark} {check['title']}：{check['value'] if check['value'] is not None else '无数据'}（目标 {check['target']}{which}）"
        )
    if report['tasks']:
        lines.append('  任务完成率（按卡片口径：今日已无可做算完成，暂无流程、错过窗口不计）：')
        for task, stats in report['tasks'].items():
            today = '，今日未完' if stats.get('open_today') else ''
            lines.append(
                f"    {task}: {pct(stats['completion_rate'])}（{stats['complete']}/{stats['days']} 天{today}）"
            )
    return '\n'.join(lines)


def cmd_schema(args) -> int:
    from .schemas import export

    for path in export(args.out):
        print(path)
    return 0


def cmd_preview(args) -> int:
    project = _project(args)
    if args.snapshot:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding='utf-8'))
    else:
        if project.manifest_path is not None:
            raise SystemExit('协议项目请用 --snapshot 指定快照（或运行 once --observe-only 查看真实卡片）')
        from importlib import import_module

        demo = import_module(f'autocontrol.projects.{project.name}.demo')
        snapshot = demo.demo_snapshot(project, args.demo)
    card = project.render(snapshot)
    text = json.dumps(card, ensure_ascii=False, indent=2)
    size = len(json.dumps(card, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    print(f'卡片 {size} 字节，{CardRenderer.tags(card)} 个元素和组件（飞书上限 200）', file=sys.stderr)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
    else:
        print(text)
    if args.html:
        Path(args.html).write_text(card_html(card), encoding='utf-8')
    return 0


def cmd_chats(args) -> int:
    from .lark.client import DeliveryError, FeishuClient, load_config

    project = _project(args)
    try:
        chats = FeishuClient(load_config(project.lark_config)).list_chats()
    except DeliveryError as exc:
        print(json.dumps({'status': 'failed', 'error_code': exc.code, 'api_code': exc.api_code}))
        return 1
    print(json.dumps(chats, ensure_ascii=False, indent=2))
    return 0


def cmd_console(args) -> int:
    from .console import Console

    console = Console(args.registry, entry=ENTRY)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        return console.run(stop)
    except locks.AlreadyRunning:
        print('总控已在运行', file=sys.stderr)
        return 0


def cmd_dashboard(args) -> int:
    from . import registry
    from .dashboard import config as settings

    registry_path = Path(args.registry) if args.registry else registry.default_path()
    path = settings.path_for(registry_path.parent)
    if args.action == 'setup':
        try:
            config = settings.setup(path, public_url=args.url, app=args.app, port=args.port)
        except settings.ConfigError as error:
            print(f'设置无效：{error}', file=sys.stderr)
            return 1
        print('\n'.join(settings.summary(config)))
        if config['public_url']:
            print(f"飞书应用要登记的重定向 URL：{config['public_url']}/auth/callback")
        return 0
    if args.action == 'webhook':
        return _dashboard_webhook(path, args.value)
    if args.action in ('status', 'allow', 'deny'):
        try:
            config = settings.load(path)
        except FileNotFoundError:
            print('看板还没配置：先运行 run.py dashboard setup --url https://<地址> --app <lark_app.json>')
            return 1
        except settings.ConfigError as error:
            print(f'看板设置无效：{error}', file=sys.stderr)
            return 1
        if args.action != 'status':
            if not args.value or not settings.OPEN_ID.fullmatch(args.value):
                print('要写一个 open_id（ou_ 开头）', file=sys.stderr)
                return 1
            people = [item for item in config['allow'] if item != args.value]
            config['allow'] = people + ([args.value] if args.action == 'allow' else [])
            settings.save(path, config)
            print(('已加入' if args.action == 'allow' else '已移出') + '白名单，立即生效')
        print('\n'.join(settings.summary(config)))
        return 0
    from .dashboard.data import Source
    from .dashboard.server import App, serve

    if args.dev:
        port = args.port or settings.DEFAULT_PORT + 8
    else:
        try:
            config = settings.load(path)
        except FileNotFoundError:
            print('看板还没配置（run.py dashboard setup）', file=sys.stderr)
            return 2
        except settings.ConfigError as error:
            print(f'看板设置无效：{error}', file=sys.stderr)
            return 2
        port = config['port']
    lock_fd = None
    if not args.dev:
        try:
            lock_fd = locks.acquire(registry_path.parent / 'dashboard.lock')
        except locks.AlreadyRunning:
            print('看板已经在运行', file=sys.stderr)
            return 75  # the console's HELPER_ELSEWHERE
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        app = App(path, source=Source(registry_path), dev=args.dev)
        return serve(app, port, stop)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _dashboard_webhook(path, value, *, ask=None) -> int:
    """Save (or switch off, or test) the group bot that tells the owner about requests."""
    import getpass

    from .dashboard import config as settings, notify

    ask = ask or getpass.getpass
    try:
        config = settings.load(path)
    except FileNotFoundError:
        print('看板还没配置：先运行 run.py dashboard setup')
        return 1
    except settings.ConfigError as error:
        print(f'看板设置无效：{error}', file=sys.stderr)
        return 1
    if value == 'off':
        config['notify'] = None
        settings.save(path, config)
        print('已关闭申请提醒：有人申请只会出现在看板的「成员」页。')
        return 0
    if value not in (None, 'test'):
        print('用法：run.py dashboard webhook [test | off]', file=sys.stderr)
        return 1
    if value is None:
        print('在飞书群里添加「自定义机器人」，复制它的 webhook 地址；建议同时开启「签名校验」。')
        hook = ask('webhook 地址（输入时不显示）：').strip()
        if not notify.valid_hook(hook):
            print('这不是飞书群机器人的 webhook 地址（应以 https://open.feishu.cn/open-apis/bot/v2/hook/ 开头）')
            return 1
        secret = ask('签名校验密钥（没开签名校验就直接回车）：').strip() or None
        config['notify'] = {'webhook': hook, 'secret': secret}
        settings.save(path, config)
        print('已保存，看板几秒内生效。')
    elif not config.get('notify'):
        print('还没配置群机器人：运行 run.py dashboard webhook')
        return 1
    try:
        notify.send(
            config['notify']['webhook'],
            notify.text_message('自动执行看板：申请提醒已接通。以后有人申请查看看板，会在这里提醒你。'),
            secret=config['notify'].get('secret'),
        )
    except notify.NotifyError as error:
        print(f'测试消息没发出去（{error}）。检查 webhook 地址和签名密钥后运行 run.py dashboard webhook test')
        return 1
    print('已往群里发了一条测试消息。')
    return 0


def cmd_cards(args) -> int:
    from . import registry
    from .lark.publisher import cards_off, set_cards

    folder = (Path(args.registry) if args.registry else registry.default_path()).parent
    if args.state != 'status':
        set_cards(args.state == 'on', folder)
    off = cards_off(folder)
    print('飞书卡片：' + ('已停发，快照只存本地给看板用' if off else '照常发送'))
    if args.state != 'status':
        print('各项目的调度进程几秒内生效，不用重启。')
    return 0


def cmd_autostart(args) -> int:
    from . import autostart, registry

    if args.disable:
        removed = autostart.disable()
        print('已取消总控的开机自启' if removed else '本来就没有配置开机自启')
        return 0
    path = autostart.enable(
        sys.executable, ENTRY, log=registry.CONSOLE_DIR / 'logs' / 'console.log'
    )
    print(f'已配置开机自启：{path}')
    print('launchd 在登录时启动总控，总控崩溃时自动拉起。')
    print(f'用 SIGTERM 停掉的总控不会被拉起，要再启动时运行：launchctl kickstart {autostart.service()}')
    return 0


def cmd_register(args) -> int:
    from . import registry

    project = _project(args)
    if why := _gate(project, args):
        print(why, file=sys.stderr)
        return 3
    if not Path(project.lark_config).is_file():
        print('私有目录里缺少飞书配置，卡片发不出去', file=sys.stderr)
        return 3
    entry = registry.entry_for(project, python=args.python, enabled=not args.disable)
    try:
        registry.upsert(args.registry, entry)
    except registry.RegistryError as error:
        print(f'登记失败：{error}', file=sys.stderr)
        return 1
    registry.write_legacy_wrapper(ENTRY, project)
    print(f'已登记 {project.name}' + ('（暂停调度）' if args.disable else '，总控会在 30 秒内接管'))
    return 0


def cmd_unregister(args) -> int:
    from . import registry

    if registry.remove(args.registry, args.name):
        print(f'已移除 {args.name}，它的调度会在当前这一轮结束后停止')
        return 0
    print(f'登记表里没有 {args.name}', file=sys.stderr)
    return 1


def cmd_projects(args) -> int:
    from . import registry

    path = Path(args.registry) if args.registry else registry.default_path()
    try:
        entries = registry.load(path)
    except registry.RegistryError as error:
        print(f'登记表无法读取：{error}', file=sys.stderr)
        return 1
    try:
        status = json.loads((path.parent / 'status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        status = {}
    running = not locks.is_free(path.parent / 'console.lock')
    if args.json:
        payload = {'console_running': running, 'projects': entries, 'status': status}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    head = '总控：运行中' if running else '总控：未运行'
    if running and status.get('pid'):
        head += f"（pid {status['pid']}，启动于 {status.get('started_at')}）"
    from . import autostart

    agent = autostart.state()
    lines = [head, '开机自启：' + (f'launchd（{agent}）' if agent else '未配置'), f'登记表：{path}']
    states = status.get('projects', {}) if running else {}
    for entry in entries:
        info = states.get(entry['name'], {})
        last = info.get('last_exit') or {}
        parts = [
            entry['name'],
            '启用' if entry['enabled'] else '停用',
            info.get('state', '—'),
            f"pid {info['pid']}" if info.get('pid') else 'pid —',
            f"异常退出 {info.get('restarts', 0)} 次",
        ]
        if last:
            parts.append(f"上次退出码 {last.get('code')}")
        if info.get('reloads'):
            # Reloads load a newly admitted manifest or new console code.
            parts.append(f"自动重新加载 {info['reloads']} 次")
        lines.append('  ' + '　'.join(str(part) for part in parts))
        if info.get('problem'):
            lines.append(f"      问题：{info['problem']}")
        if info.get('pending'):
            lines.append(f"      待加载：{info['pending']}")
    if running and status.get('registry_error'):
        lines.append(f"  登记表错误：{status['registry_error']}")
    helpers = (
        status.get('helpers', {}) if running and isinstance(status.get('helpers'), dict) else {}
    )
    for name, info in helpers.items():
        parts = [
            {'dashboard': '看板', 'tunnel': '隧道'}.get(name, name),
            info.get('state', '—'),
            f"pid {info['pid']}" if info.get('pid') else 'pid —',
        ]
        if info.get('restarts'):
            parts.append(f"异常退出 {info['restarts']} 次")
        lines.append('  ' + '　'.join(str(part) for part in parts))
    if running and status.get('helper_error'):
        lines.append(f"  {status['helper_error']}")
    from .lark.publisher import cards_off

    lines.append('飞书卡片：' + ('已停发（只存看板）' if cards_off(path.parent) else '照常发送'))
    print('\n'.join(lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='autocontrol', description='本地自动执行总控台')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--project', default='yslzm', help='内置适配器项目名（默认 yslzm）')
    common.add_argument('--manifest', type=Path, help='协议项目的接入清单 autocontrol.json')
    common.add_argument('--workdir', type=Path, help='覆盖执行器所在目录')
    common.add_argument('--private', type=Path, help='覆盖私有目录（锁、结果、指标、卡片绑定）')
    common.add_argument('--python', help='运行 yslzm 执行器的 Python 解释器')
    sub = parser.add_subparsers(dest='command', required=True)

    console = sub.add_parser('console', help='总控主进程：按项目登记表统一调度所有项目')
    console.add_argument(
        '--registry', type=Path, help='项目登记表（默认 ~/.config/autocontrol/console/projects.json）'
    )
    console.set_defaults(func=cmd_console)

    board = sub.add_parser(
        'dashboard', help='网页看板：总控托管运行；setup / status / allow / deny / webhook 管理设置'
    )
    board.add_argument('action', nargs='?', default='serve',
                       choices=('serve', 'setup', 'status', 'allow', 'deny', 'webhook'))  # fmt: skip
    board.add_argument('value', nargs='?', help='allow / deny 的 open_id；webhook 的 test / off')
    board.add_argument('--registry', type=Path, help='项目登记表（默认同 console）')
    board.add_argument('--url', help='setup：看板的公网地址（https://…）')
    board.add_argument('--app', help='setup：用来登录的飞书应用配置 lark_app.json')
    board.add_argument('--port', type=int, help='本机端口（setup 写进设置；--dev 时临时用）')
    board.add_argument('--dev', action='store_true', help='本机预览：不登录，只在 127.0.0.1 上')
    board.set_defaults(func=cmd_dashboard)

    cards = sub.add_parser('cards', help='飞书卡片总开关：off 只存本地给看板用，on 恢复发送')
    cards.add_argument('state', choices=('on', 'off', 'status'))
    cards.add_argument('--registry', type=Path, help='项目登记表（默认同 console）')
    cards.set_defaults(func=cmd_cards)

    starting = sub.add_parser('autostart', help='用 launchd 在登录时启动总控，崩溃时自动拉起')
    starting.add_argument('--disable', action='store_true', help='取消开机自启（会停掉 launchd 启动的总控）')
    starting.set_defaults(func=cmd_autostart)

    register = sub.add_parser('register', parents=[common], help='登记项目，交给总控统一调度（上线）')
    register.add_argument('--registry', type=Path, help='项目登记表（默认同 console）')
    register.add_argument('--disable', action='store_true', help='登记但暂停调度')
    register.add_argument('--skip-admission', action='store_true', help='跳过准入记录校验（仅调试）')
    register.set_defaults(func=cmd_register)

    unregister = sub.add_parser('unregister', help='从登记表移除项目（下线）')
    unregister.add_argument('name', help='项目名')
    unregister.add_argument('--registry', type=Path, help='项目登记表（默认同 console）')
    unregister.set_defaults(func=cmd_unregister)

    listing = sub.add_parser('projects', help='列出登记的项目和总控运行状态')
    listing.add_argument('--registry', type=Path, help='项目登记表（默认同 console）')
    listing.add_argument('--json', action='store_true')
    listing.set_defaults(func=cmd_projects)

    serve = sub.add_parser('serve', parents=[common], help='单个项目的常驻调度（总控为每个登记项目各启动一个）')
    serve.add_argument('--observe-first', action='store_true', help='启动后先只读发现计划')
    serve.add_argument('--resume', action='store_true', help='不登录，沿用已保存的卡片，等下一轮（总控代码更新后用）')
    serve.add_argument(
        '--reason', choices=sorted(START_REASONS), default='manual', help='启动原因（写进调度日志）'
    )
    serve.add_argument('--skip-admission', action='store_true', help='跳过准入记录校验（仅调试）')
    serve.set_defaults(func=cmd_serve)

    once = sub.add_parser('once', parents=[common], help='立即执行一轮')
    once.add_argument('--observe-only', action='store_true', help='只读发现当日计划并更新卡片')
    once.add_argument('--phase', help='只执行一个阶段')
    once.add_argument('--skip-admission', action='store_true', help='跳过准入记录校验（仅调试）')
    once.set_defaults(func=cmd_once)

    check = sub.add_parser('check', parents=[common], help='准入检查（协议项目）')
    check.add_argument('--static', action='store_true', help='只校验清单，不运行脚本')
    check.add_argument('--repeat', type=int, default=1, help='只读发现次数，≥2 时检查一致性')
    check.add_argument('--json', action='store_true', help='输出 JSON 报告')
    check.set_defaults(func=cmd_check)

    metrics = sub.add_parser('metrics', parents=[common], help='运行指标与健康检查')
    metrics.add_argument('--days', type=int, default=7)
    metrics.add_argument('--json', action='store_true')
    metrics.set_defaults(func=cmd_metrics)

    preview = sub.add_parser('preview', parents=[common], help='本地渲染卡片 JSON / HTML 预览')
    preview.add_argument('--snapshot', help='展示快照 JSON 文件；缺省使用示例数据')
    preview.add_argument('--demo', choices=('running', 'finished', 'done'), default='running')
    preview.add_argument('--out', help='卡片 JSON 输出文件（缺省打印）')
    preview.add_argument('--html', help='同时输出 HTML 示意预览')
    preview.set_defaults(func=cmd_preview)

    schema = sub.add_parser('schema', help='导出协议 JSON Schema（清单 / 事件 / 结果）')
    schema.add_argument('--out', default='docs/schemas', help='输出目录')
    schema.set_defaults(func=cmd_schema)

    chats = sub.add_parser('chats', parents=[common], help='列出机器人所在的群（查 chat_id）')
    chats.set_defaults(func=cmd_chats)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
