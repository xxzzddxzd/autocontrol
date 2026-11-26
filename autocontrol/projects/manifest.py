"""Protocol projects: an automation script described by an ``autocontrol.json``.

The script implements worker protocol v1 (:mod:`autocontrol.protocol`); the
manifest declares how to start it, its phases, schedule and checklist. No
Python code inside the console is needed to onboard such a script.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import protocol, storage
from ..board.catalog import DEMANDS, DONE_FIELD, ENDPOINTS, Catalog, Section, TaskSpec
from ..schedule import Schedule, _clock
from ..worker import last_json_line, run_streamed
from .base import Project

NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')
PHASE = re.compile(r'[a-z][a-z0-9_]{0,31}')
REASON = re.compile(r'[a-z][a-z0-9_]{0,47}')
MANIFEST_KEYS = frozenset(
    '''protocol name display_name workdir private lark_config command phases observe_phase
    timeouts schedule account_lock run_scope sections phase_titles tasks reasons short_reasons
    count_keys units'''.split()
)
TASK_KEYS = frozenset(
    ('id', 'title', 'section', 'kind', 'parent', 'seed', 'demand', 'endpoint', 'discovery',
     'done_text', 'progress_text', 'phase')
)  # fmt: skip
# A finished row's text: a few words around its {count_key} fields.
DONE_WORDS = 24
DONE_FIELDS = 4
PHASE_STATES = {
    'complete': ('complete', 'skipped'),
    'incomplete': ('partial',),
    'waiting': ('waiting',),
    'failed': ('failed',),
    'result_unavailable': ('timeout', 'unverified'),
    'not_started': ('busy',),
}


class ManifestError(ValueError):
    """The manifest does not describe a valid protocol project."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__('; '.join(self.problems))


def _text(value, limit) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= limit
        and not any(ch in value for ch in '<>*~`[]\n')
    )


def _text_template_problems(ident, field, template, declared) -> list[str]:
    if type(template) is not str:
        return [f'task {ident} {field} must be a string']
    names = DONE_FIELD.findall(template)
    words = DONE_FIELD.sub('', template)
    if (
        not 1 <= len(names) <= DONE_FIELDS
        or len(words) > DONE_WORDS
        or any(ch in words for ch in '{}<>*~`[]\n')
    ):
        return [
            f'task {ident} {field} must be up to {DONE_WORDS} characters'
            f' around 1-{DONE_FIELDS} {{count_key}} fields'
        ]
    unknown = sorted(set(names) - declared)
    if unknown:
        return [f'task {ident} {field} uses undeclared count_keys: {", ".join(unknown)}']
    return []


def validate_manifest(data) -> list[str]:
    """Every problem found in a manifest (empty when valid)."""
    if type(data) is not dict:
        return ['manifest must be a JSON object']
    problems = [f'unknown field: {key}' for key in data if key not in MANIFEST_KEYS]
    if data.get('protocol') != protocol.VERSION:
        problems.append(f'protocol must be {protocol.VERSION}')
    if type(data.get('name')) is not str or not NAME.fullmatch(data['name']):
        problems.append('name must match [a-z][a-z0-9_-]{0,31}')
    if not _text(data.get('display_name'), 32):
        problems.append('display_name must be 1-32 plain characters')
    for key in ('workdir',):
        if type(data.get(key)) is not str or not data[key]:
            problems.append(f'{key} must be a path')
    for key in ('private', 'lark_config', 'account_lock'):
        if data.get(key) is not None and (type(data[key]) is not str or not data[key]):
            problems.append(f'{key} must be a path or null')
    command = data.get('command')
    if (
        type(command) is not list
        or not command
        or not all(type(part) is str and part for part in command)
        or not any('{phase}' in part for part in command)
    ):
        problems.append('command must be a non-empty list of strings containing "{phase}"')
    else:
        for part in command:
            try:
                part.format(phase='x', service_day='2026-01-01', mode='execute', python='python3')
            except (KeyError, IndexError, ValueError):
                problems.append(
                    'command placeholders must be {phase}, {service_day}, {mode} or {python}'
                )
                break
    phases = data.get('phases')
    if (
        type(phases) is not list
        or not phases
        or not all(type(p) is str and PHASE.fullmatch(p) for p in phases)
    ):
        problems.append('phases must be a non-empty list of [a-z][a-z0-9_]* names')
        phases = []
    elif len(set(phases)) != len(phases):
        problems.append('phases must be unique')
    observe = data.get('observe_phase', 'status')
    if type(observe) is not str or not PHASE.fullmatch(observe) or observe in phases:
        problems.append('observe_phase must be a phase name not listed in phases')
    timeouts = data.get('timeouts', {})
    if (
        type(timeouts) is not dict
        or set(timeouts) - {'phase', 'observe'}
        or not all(type(v) in (int, float) and 10 <= v <= 86400 for v in timeouts.values())
    ):
        problems.append('timeouts must map phase/observe to 10-86400 seconds')
    schedule = data.get('schedule')
    if type(schedule) is not dict or set(schedule) - {'timezone', 'reset', 'times', 'phases'}:
        problems.append('schedule must have timezone, reset and times (and optional phases)')
    else:
        try:
            ZoneInfo(schedule.get('timezone', 'Asia/Shanghai'))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            problems.append('schedule.timezone must be an IANA time zone')
        times = schedule.get('times')
        try:
            if type(times) is not list or not times:
                raise ValueError
            for value in (*times, schedule.get('reset', '05:00')):
                _clock(value)
        except (ValueError, TypeError, AttributeError):
            problems.append('schedule.times must be a non-empty list of HH:MM (and reset HH:MM)')
        problems.extend(_phase_schedule_problems(schedule.get('phases', {}), phases))
    if type(data.get('run_scope', False)) is not bool:
        problems.append('run_scope must be true or false')
    sections = data.get('sections')
    section_keys = set()
    if type(sections) is not list or not sections:
        problems.append('sections must be a non-empty list')
    else:
        for item in sections:
            if type(item) is not dict or set(item) - {'key', 'title', 'aggregate'}:
                problems.append('each section needs key and title')
                continue
            if (
                type(item.get('key')) is not str
                or not PHASE.fullmatch(item['key'])
                or item['key'] in section_keys
            ):
                problems.append('section keys must be unique [a-z][a-z0-9_]* names')
            if not _text(item.get('title'), 16):
                problems.append(f'section {item.get("key")!r} needs a 1-16 character title')
            section_keys.add(item.get('key'))
    titles = data.get('phase_titles', {})
    if type(titles) is not dict or not all(_text(v, 16) for v in titles.values()):
        problems.append('phase_titles must map phases to 1-16 character titles')
    tasks = data.get('tasks')
    ids = {}
    declared = data.get('count_keys', [])
    declared = {k for k in declared if type(k) is str} if type(declared) is list else set()
    if type(tasks) is not list or not tasks or len(tasks) > protocol.MAX_TASKS:
        problems.append(f'tasks must be a list of 1-{protocol.MAX_TASKS} tasks')
    else:
        for item in tasks:
            if type(item) is not dict or set(item) - TASK_KEYS:
                problems.append(
                    'each task needs id and title (and optional section, kind, parent, seed,'
                    ' demand, endpoint, discovery, done_text, progress_text, phase)'
                )
                continue
            ident = item.get('id')
            if type(ident) is not str or not protocol.TASK_ID.fullmatch(ident) or ident in ids:
                problems.append(f'task id {ident!r} must be unique [a-z][a-z0-9_]{{0,39}}')
                continue
            ids[ident] = item
            if not _text(item.get('title'), 20):
                problems.append(f'task {ident} needs a 1-20 character title')
            kind = item.get('kind', 'goal')
            if kind not in ('goal', 'job', 'step'):
                problems.append(f'task {ident} kind must be goal, job or step')
            if kind == 'step':
                if (
                    item.get('parent') not in ids
                    or ids[item['parent']].get('kind', 'goal') == 'step'
                ):
                    problems.append(f'step {ident} needs an earlier goal or job parent')
            elif item.get('section') not in section_keys:
                problems.append(f'task {ident} needs a known section')
            if type(item.get('seed', True)) is not bool:
                problems.append(f'task {ident} seed must be true or false')
            if item.get('demand', 'daily') not in DEMANDS:
                problems.append(f'task {ident} demand must be one of {", ".join(DEMANDS)}')
            if item.get('endpoint', 'target') not in ENDPOINTS:
                problems.append(f'task {ident} endpoint must be one of {", ".join(ENDPOINTS)}')
            if type(item.get('discovery', False)) is not bool:
                problems.append(f'task {ident} discovery must be true or false')
            elif item.get('discovery') and kind == 'step':
                problems.append(f'discovery task {ident} must be a goal or job')
            if 'phase' in item and (
                kind == 'step' or type(phases) is not list or item['phase'] not in phases
            ):
                problems.append(f'task {ident} phase must be one of phases (not for a step)')
            for field in ('done_text', 'progress_text'):
                if field in item:
                    problems += _text_template_problems(ident, field, item[field], declared)
        if sum(type(item) is dict and item.get('discovery') is True for item in tasks) > 1:
            problems.append('at most one task may be the discovery task')
    reasons = data.get('reasons', {})
    if type(reasons) is not dict or not all(
        type(k) is str and REASON.fullmatch(k) and _text(v, 40) for k, v in reasons.items()
    ):
        problems.append('reasons must map [a-z][a-z0-9_]* codes to 1-40 character texts')
    short = data.get('short_reasons', {})
    if type(short) is not dict or not all(_text(v, 8) for v in short.values()):
        problems.append('short_reasons must map reason codes to 1-8 character labels')
    elif type(reasons) is dict and set(short) - set(reasons) - set(protocol.STANDARD_REASONS):
        problems.append('short_reasons may only use known reason codes')
    own = data.get('schedule', {}).get('phases', {}) if type(data.get('schedule')) is dict else {}
    known = set(protocol.STANDARD_REASONS) | (set(reasons) if type(reasons) is dict else set())
    for name, item in own.items() if type(own) is dict else ():
        idle = item.get('idle') if type(item) is dict else None
        if type(idle) is dict and type(idle.get('reason')) is str and idle['reason'] not in known:
            problems.append(f'schedule.phases.{name}.idle.reason must be a known reason code')
    counts = data.get('count_keys', [])
    if type(counts) is not list or not all(type(k) is str and REASON.fullmatch(k) for k in counts):
        problems.append('count_keys must be a list of [a-z][a-z0-9_]* names')
    units = data.get('units', [])
    if type(units) is not list or not all(_text(u, 4) for u in units):
        problems.append('units must be a list of 1-4 character labels')
    return problems


def _phase_schedule_problems(own, phases) -> list[str]:
    """``schedule.phases``: phases that keep their own times instead of ``schedule.times``."""
    if type(own) is not dict:
        return ['schedule.phases must map phase names to {"times": [...]}']
    problems = []
    for name, item in own.items():
        if name not in phases:
            problems.append(f'schedule.phases.{name} is not a listed phase')
            continue
        texts = item.get('times') if type(item) is dict else None
        try:
            if (
                set(item) - {'times', 'wake', 'until', 'idle'}
                or type(texts) is not list
                or not texts
                or len(set(texts)) != len(texts)
                or type(item.get('wake', True)) is not bool
            ):
                raise ValueError
            for value in texts:
                _clock(value)
            deadlines = item.get('until')
            if deadlines is not None:
                if type(deadlines) is not list or len(deadlines) != len(texts):
                    raise ValueError
                for start, deadline in zip(texts, deadlines):
                    if _clock(deadline) == _clock(start):
                        raise ValueError
            idle = item.get('idle')
            if idle is not None:
                quiet = idle.get('times') if type(idle) is dict else None
                if (
                    set(idle) != {'reason', 'times'}
                    or type(idle['reason']) is not str
                    or not REASON.fullmatch(idle['reason'])
                    or type(quiet) is not list
                    or not quiet
                    or len(set(quiet)) != len(quiet)
                ):
                    raise ValueError
                for value in quiet:
                    _clock(value)
        except (ValueError, TypeError, AttributeError):
            problems.append(
                f'schedule.phases.{name} must be {{"times": [distinct HH:MM], "wake": bool, '
                '"until": [one HH:MM deadline per time], '
                '"idle": {"reason": code, "times": [distinct HH:MM]}}'
            )
    if phases and own and set(phases) <= set(own):
        problems.append('at least one phase must follow schedule.times')
    return problems


MAX_MANIFEST = 256 * 1024


def digest_of(data) -> str:
    """The content digest an admission record is bound to."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def manifest_digest(path) -> str | None:
    """A manifest file's digest as it is now, without validating it; None if unreadable."""
    try:
        raw = Path(path).expanduser().read_bytes()
        return digest_of(json.loads(raw)) if len(raw) <= MAX_MANIFEST else None
    except (OSError, ValueError, UnicodeError):
        return None


def load_manifest(path) -> tuple[dict, str]:
    """Read and validate a manifest; return it with its content digest."""
    path = Path(path).expanduser()
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ManifestError([f'cannot read manifest: {type(error).__name__}']) from None
    if len(raw) > MAX_MANIFEST:
        raise ManifestError(['manifest larger than 256 KiB'])
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ManifestError(['manifest is not valid JSON']) from None
    problems = validate_manifest(data)
    if problems:
        raise ManifestError(problems)
    return data, digest_of(data)


def build_catalog(data) -> Catalog:
    sections = [
        Section(item['key'], item['title'], item.get('aggregate')) for item in data['sections']
    ]
    tasks = [
        TaskSpec(
            item['id'],
            item['title'],
            item.get('section', ''),
            item.get('kind', 'goal'),
            parent=item.get('parent'),
            seed=item.get('seed', item.get('kind', 'goal') != 'step'),
            demand=item.get('demand'),
            endpoint=item.get('endpoint'),
            discovery=item.get('discovery', False),
            done_text=item.get('done_text'),
            progress_text=item.get('progress_text'),
            phase=item.get('phase'),
        )
        for item in data['tasks']
    ]
    section_keys = {section.key for section in sections}
    titles = data.get('phase_titles', {})
    phases = {
        data.get('observe_phase', 'status'): titles.get(data.get('observe_phase', 'status'), '计划读取')
    }
    for phase in data['phases']:
        default = next((s.title for s in sections if s.key == phase), phase)
        phases[phase] = titles.get(phase, default)
    return Catalog(
        project=data['name'],
        display_name=data['display_name'],
        sections=sections,
        tasks=tasks,
        phases=phases,
        phase_sections={phase: phase for phase in data['phases'] if phase in section_keys},
        status_groups=PHASE_STATES,
        reasons={**protocol.STANDARD_REASONS, **data.get('reasons', {}), **protocol.FAILURE_STAGES},
        short_reasons={**protocol.STANDARD_SHORT_REASONS, **data.get('short_reasons', {})},
        request_problems=protocol.REQUEST_PROBLEMS,
        count_keys=data.get('count_keys', ()),
        units=(*protocol.STANDARD_UNITS, *data.get('units', ())),
        plan_kinds=('goal', 'job', 'step'),
        day_done_reasons=protocol.DAY_DONE_REASONS,
        # 每轮收菜: done once this round's own execution reports it complete.
        round_tasks={item['id']: () for item in data['tasks'] if item.get('demand') == 'round'},
    )


def build_schedule(data) -> Schedule:
    config = data['schedule']
    name = config.get('timezone', 'Asia/Shanghai')
    own = config.get('phases', {})
    return Schedule(
        times=tuple(config['times']),
        reset=config.get('reset', '05:00'),
        tz=ZoneInfo(name),
        tz_name=name,
        slots=tuple((phase, tuple(item['times'])) for phase, item in own.items()),
        slot_until=tuple(
            (phase, tuple(item['until'])) for phase, item in own.items() if item.get('until')
        ),
        wake_phases=tuple(phase for phase, item in own.items() if item.get('wake', True)),
        idle=tuple(
            (phase, item['idle']['reason'], tuple(item['idle']['times']))
            for phase, item in own.items()
            if item.get('idle')
        ),
    )


class ManifestProject(Project):
    """A protocol v1 script, configured entirely by its manifest."""

    def __init__(self, manifest, *, workdir=None, private=None, python=None):
        self.manifest_path = Path(manifest).expanduser().resolve()
        self.manifest, self.digest = load_manifest(self.manifest_path)
        data = self.manifest
        base = self.manifest_path.parent
        self.name = data['name']
        self.catalog = build_catalog(data)
        self.schedule = build_schedule(data)
        self.phases = tuple(data['phases'])
        self.commands = self.phases
        self.status_phase = data.get('observe_phase', 'status')
        self.default_workdir = (base / Path(data['workdir']).expanduser()).resolve()
        self.default_private = Path(
            data.get('private') or f'~/.config/autocontrol/{self.name}'
        ).expanduser()
        self.uuid_namespace = f'autocontrol-daily-board/{self.name}'
        timeouts = data.get('timeouts', {})
        self.phase_timeout = float(timeouts.get('phase', 1800))
        self.observe_timeout = float(timeouts.get('observe', 180))
        super().__init__(workdir=workdir, private=private, python=python)

    @property
    def account_lock(self):
        value = self.manifest.get('account_lock')
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.private / path

    @property
    def lark_config(self):
        value = self.manifest.get('lark_config')
        return Path(value).expanduser() if value else self.private / 'lark_app.json'

    def new_run_scope(self):
        return {} if self.manifest.get('run_scope') else None

    # -- invocation ------------------------------------------------------------

    def argv(self, phase, mode, day) -> list[str]:
        values = {'phase': phase, 'service_day': day, 'mode': mode, 'python': self.python}
        return [part.format(**values) for part in self.manifest['command']]

    def invoke(
        self, phase, mode, day, *, on_event=None, run_scope=None, round_id=None, timeout=None
    ):
        """Run the script once; return ``(record, details)`` — never raises for worker faults.

        ``details`` carries the validated events and violations for admission checks.
        """
        timeout = timeout or (self.observe_timeout if mode == 'observe' else self.phase_timeout)
        report = protocol.Violations()
        result_report = protocol.Violations()
        details = {
            'events': [],
            'rejected': 0,
            'total': 0,
            'report': report,
            'result_report': result_report,
        }
        last_seq = {}

        def handle(line):
            details['total'] += 1
            if line is None:
                report.add('event_too_large')
                details['rejected'] += 1
                return
            event = protocol.parse_event_line(line, self.catalog, phase, report)
            if event is None:
                details['rejected'] += 1
                return
            run = event['generation']
            if event['sequence'] <= last_seq.get(run, 0):
                report.add('event_seq_order', event['kind'])
            last_seq[run] = max(last_seq.get(run, 0), event['sequence'])
            if len(details['events']) < 2048:
                details['events'].append(event)
            if on_event is not None:
                on_event(event)

        started = datetime.now(self.schedule.tz)
        clock = time.monotonic()
        env = {
            protocol.ENV_PROTOCOL: str(protocol.VERSION),
            protocol.ENV_PROJECT: self.name,
            protocol.ENV_PHASE: phase,
            protocol.ENV_MODE: mode,
            protocol.ENV_SERVICE_DAY: day,
            protocol.ENV_RUN_ID: round_id or uuid.uuid4().hex,
            protocol.ENV_DEADLINE: str(int(time.time() + max(1.0, timeout - 10))),
        }
        payload = None
        if run_scope is not None:
            payload = json.dumps({'run_scope': run_scope}, separators=(',', ':')).encode()
            if len(payload) > protocol.MAX_SCOPE_BYTES:
                payload = json.dumps({'run_scope': {}}).encode()
                report.add('run_scope_too_large')
        record = {'command': phase, 'mode': mode, 'civil_date': started.date().isoformat(),
                  'started_at': started.isoformat(timespec='seconds')}  # fmt: skip
        result = None
        line_seen = False
        try:
            if not self.workdir.is_dir():
                raise FileNotFoundError(str(self.workdir))
            label = phase if mode == 'execute' else f'{phase}-{mode}'
            child = run_streamed(
                self.argv(phase, mode, day), cwd=self.workdir, on_event=handle,
                progress_env=protocol.ENV_PROGRESS_FD, timeout=timeout,
                input=payload if payload is not None else b'', env=env, raw=True,
                on_stderr=lambda data: storage.keep_stderr(self.private, started, label, data),
            )  # fmt: skip
            code = child.returncode
            raw = last_json_line(child.stdout, protocol.MAX_RESULT_BYTES)
            line_seen = bool(child.stdout.strip())
            if raw is not None:
                result = protocol.parse_result(
                    raw, self.catalog, phase, result_report, allow_scope=run_scope is not None
                )
            elif line_seen:
                result_report.add('result_missing')
        except subprocess.TimeoutExpired as error:
            code = None
            raw = last_json_line(error.stdout or b'', protocol.MAX_RESULT_BYTES)
            if raw is not None:
                # A partial result after a timeout is kept, but not graded.
                result = protocol.parse_result(
                    raw, self.catalog, phase, protocol.Violations(), allow_scope=False
                )
            record.update(execution='timeout', failure_stage='timeout', error_type='TimeoutExpired')
        except OSError as error:
            code = None
            stage = 'project_cwd' if not self.workdir.is_dir() else 'process_start'
            record.update(execution='failed', failure_stage=stage, error_type=_error_name(error))
        record['finished_at'] = datetime.now(self.schedule.tz).isoformat(timespec='seconds')
        record['duration_ms'] = int((time.monotonic() - clock) * 1000)
        record['exit_code'] = code
        if 'execution' not in record:
            if code == protocol.EXIT_BUSY or (result and result['outcome'] == 'busy'):
                record.update(execution='busy', failure_stage='lock')
            elif result is None:
                record['execution'] = 'failed' if code else 'unverified'
                record['failure_stage'] = (
                    'process' if code else ('protocol' if line_seen else 'summary')
                )
            else:
                record['execution'] = result['outcome'] if code == protocol.EXIT_OK else 'failed'
                stage = result.get('failure_stage')
                if code == protocol.EXIT_CONFIG:
                    stage = 'config'
                elif code not in (protocol.EXIT_OK, None) and stage is None:
                    stage = 'process'
                if stage:
                    record['failure_stage'] = stage
        if result is not None:
            if result.get('error_type') and 'error_type' not in record:
                record['error_type'] = result['error_type']
            record.update(game_day=result['day'], tasks=result['tasks'], next_wake=result['next_wake'],
                          metrics=result['metrics'])  # fmt: skip
            if run_scope is not None and 'run_scope' in result:
                run_scope.clear()
                run_scope.update(result['run_scope'])
        record['protocol'] = {
            'version': protocol.VERSION,
            'events': details['total'],
            'events_rejected': details['rejected'],
            'result_valid': result is not None,
            'violations': (report.errors + result_report.errors)[:16],
            'ignored_fields': len(report.ignored) + len(result_report.ignored),
        }
        return record, details

    def observe(self, day, *, on_event, timeout=None, round_id=None):
        record, _ = self.invoke(self.status_phase, 'observe', day, on_event=on_event, round_id=round_id,
                                timeout=timeout or self.observe_timeout)  # fmt: skip
        if record.get('tasks') or record.get('next_wake'):
            # The final result is the authoritative plan of a discovery run.
            on_event({'kind': 'plan', 'phase': self.status_phase, 'observed_at': time.time(), 'plan': {
                'game_day': record.get('game_day', day),
                'full_checklist_summary': {'goals': record.get('tasks', {})},
                'dynamic_candidates': record.get('next_wake', []),
            }})  # fmt: skip
        return self.exit_code(record), record

    def run_phase(self, phase, day, *, run_scope, on_event, round_id=None):
        if phase not in self.phases:
            raise ValueError('invalid phase')
        started = datetime.now(self.schedule.tz)
        record, _ = self.invoke(
            phase, 'execute', day, on_event=on_event, run_scope=run_scope, round_id=round_id
        )
        try:
            storage.archive(self.private, started, phase, record)
        except OSError as error:
            record.update(result_storage='failed', result_storage_error_type=_error_name(error))
        return record

    def result_facts(self, record):
        return {
            'phase': record.get('command'),
            'game_day': record.get('game_day'),
            'goals': None,
            'updates': list((record.get('tasks') or {}).items()),
            'execution': record.get('execution'),
            'reason': record.get('failure_stage'),
            'finished_at': record.get('finished_at'),
            'dynamic_candidates': record.get('next_wake') or [],
        }

    def exit_code(self, record):
        if record.get('execution') == 'busy':
            return protocol.EXIT_BUSY
        if record.get('result_storage') == 'failed':
            return 1
        code = record.get('exit_code')
        if code is None:
            return 1
        if code != 0:
            return code if code > 0 else 128 - code
        if record.get('execution') in ('failed', 'timeout', 'unverified'):
            return 1
        return 0 if record.get('protocol', {}).get('result_valid') else 1


def _error_name(error) -> str:
    name = type(error).__name__
    return name if protocol.ERROR_TYPE.fullmatch(name) else 'OtherError'
