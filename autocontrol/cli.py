"""Command line: serve, once, check, metrics, preview, chats and install."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from . import locks, projects
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
    from .lark.publisher import CardPublisher

    return CardPublisher(
        project.private,
        project.render,
        project.sanitize,
        config_path=project.lark_config,
        uuid_namespace=project.uuid_namespace,
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
            result = runner.run_once(observe_only=args.observe_only, phase=args.phase)
        except locks.AlreadyRunning:
            result = {'status': 'busy', 'code': 2}
        print(json.dumps(result, ensure_ascii=False))
        return int(result.get('code', 0))
    finally:
        publisher.close()


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
        return 0  # the watchdog found the service alive
    publisher = _publisher(project)
    try:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        runner = RoundRunner(project, publisher, metrics=_metrics(project))
        run_service(runner, stop=stop, observe_first=args.observe_first)
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

    lines = [
        f"{report['project']} 近 {report['days']} 天运行指标（自 {report['since']}，{report['rounds']} 轮）"
    ]
    lines.append(f"  最近一次成功：{report['last_success_at'] or '—'}")
    for name, stats in report['phases'].items():
        lines.append(
            f"  阶段 {name}: {stats['runs']} 次，成功 {pct(stats['success_rate'])}，超时 {pct(stats['timeout_rate'])}，"
            f"均值 {stats['avg_ms'] or '—'} ms，P95 {stats['p95_ms'] or '—'} ms，"
            f"请求 {stats['actions_sent']}/确认 {stats['actions_confirmed']}/未确认 {stats['actions_unconfirmed']}"
        )
        if stats['failure_stages']:
            lines.append(
                '      失败阶段：' + '，'.join(f'{k}×{v}' for k, v in stats['failure_stages'].items())
            )
    lines.append('  指标检查：')
    for check in report['checks']:
        mark = '—' if check['ok'] is None else ('✓' if check['ok'] else '✗')
        lines.append(
            f"    {mark} {check['title']}：{check['value'] if check['value'] is not None else '无数据'}（目标 {check['target']}）"
        )
    if report['tasks']:
        lines.append('  任务完成率：')
        for task, stats in report['tasks'].items():
            lines.append(
                f"    {task}: {pct(stats['completion_rate'])}（{stats['complete']}/{stats['days']} 天）"
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


def cmd_install(args) -> int:
    from .install import install

    project = _project(args)
    if why := _gate(project, args):
        print(why, file=sys.stderr)
        return 3
    install(project, args.python or sys.executable, ENTRY, start=not args.no_start)
    print(f'autocontrol watchdog installed for {project.name}')
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

    serve = sub.add_parser('serve', parents=[common], help='常驻调度（由 cron 看护）')
    serve.add_argument('--observe-first', action='store_true', help='启动后先只读发现计划')
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

    setup = sub.add_parser('install', parents=[common], help='安装 cron 看护并启动服务')
    setup.add_argument('--no-start', action='store_true', help='只写 crontab，不立即启动')
    setup.add_argument('--skip-admission', action='store_true', help='跳过准入记录校验（仅调试）')
    setup.set_defaults(func=cmd_install)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
