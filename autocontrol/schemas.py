"""JSON Schemas (draft 2020-12) of worker protocol v1, generated from the protocol constants.

Exported to ``docs/schemas/`` with ``autocontrol schema --out docs/schemas`` so
scripts in any language can validate their manifest, events and result.
The console's own validators in :mod:`autocontrol.protocol` remain the
authority; a test keeps the exported files in sync.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import protocol
from .board.catalog import DEMANDS, ENDPOINTS
from .board.catalog import STATUSES

DRAFT = 'https://json-schema.org/draft/2020-12/schema'
TASK_ID = {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,39}$'}
EPOCH = {'type': 'number', 'exclusiveMinimum': 0, 'description': 'Unix 时间戳（秒）'}
DAY = {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$', 'description': '服务日 YYYY-MM-DD'}
WAKES = {
    'type': 'array',
    'maxItems': protocol.MAX_WAKES,
    'items': EPOCH,
    'description': '未来需要再次检查的时间（服务器计时到期、窗口开放等）',
}


def _task_state() -> dict:
    return {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'status': {'enum': list(STATUSES), 'description': '十种标准状态之一'},
            'reason': {
                'type': 'string',
                'pattern': '^[a-z][a-z0-9_]{0,47}$',
                'description': '标准原因码或清单 reasons 中声明的原因码',
            },
            'current': {'type': 'integer', 'minimum': 0},
            'total': {'type': 'integer', 'minimum': 1, 'description': '只有真实分母时才上报'},
            'unit': {'type': 'string', 'maxLength': 4},
            'next_run_at': EPOCH,
            'counts': {
                'type': 'object',
                'additionalProperties': {'type': 'integer', 'minimum': 0},
                'description': '清单 count_keys 中声明的计数',
            },
        },
    }


def event_schema() -> dict:
    task_kinds = [kind for kind in protocol.EVENT_KINDS if kind != 'plan']
    state = _task_state()
    return {
        '$schema': DRAFT,
        'title': 'autocontrol 进度事件 v1（进度管道中的一行）',
        'type': 'object',
        'required': ['v', 'kind', 'seq', 'ts'],
        'properties': {
            'v': {'const': protocol.VERSION},
            'kind': {'enum': list(protocol.EVENT_KINDS)},
            'seq': {'type': 'integer', 'minimum': 1, 'description': '同一进程内严格递增'},
            'ts': EPOCH,
            'run': {'type': 'string', 'minLength': 1, 'maxLength': 64, 'description': '进程实例标识'},
            'day': DAY,
            'tasks': {'type': 'object', 'propertyNames': TASK_ID, 'additionalProperties': state},
            'next_wake': WAKES,
            'task': TASK_ID,
            **state['properties'],
        },
        'allOf': [
            {'if': {'properties': {'kind': {'const': 'plan'}}}, 'then': {'required': ['tasks']}},
            {'if': {'properties': {'kind': {'enum': task_kinds}}}, 'then': {'required': ['task']}},
            {
                'if': {'properties': {'kind': {'const': 'task_finished'}}},
                'then': {'required': ['status']},
            },
        ],
        'description': f'UTF-8 JSON，一行一个事件，含换行不超过 {protocol.MAX_EVENT_BYTES} 字节',
    }


def result_schema() -> dict:
    return {
        '$schema': DRAFT,
        'title': 'autocontrol 最终结果 v1（stdout 最后一行）',
        'type': 'object',
        'required': ['v', 'phase', 'day', 'outcome'],
        'additionalProperties': False,
        'properties': {
            'v': {'const': protocol.VERSION},
            'phase': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'},
            'day': DAY,
            'outcome': {'enum': list(protocol.OUTCOMES)},
            'failure_stage': {'enum': list(protocol.WORKER_FAILURE_STAGES)},
            'error_type': {'type': 'string', 'pattern': '^[A-Z][A-Za-z0-9]{0,63}$'},
            'tasks': {
                'type': 'object',
                'propertyNames': TASK_ID,
                'additionalProperties': _task_state(),
            },
            'next_wake': WAKES,
            'metrics': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    key: {'type': 'integer', 'minimum': 0, 'description': text}
                    for key, text in protocol.METRIC_KEYS.items()
                },
            },
            'run_scope': {'type': 'object', 'description': '本轮下一阶段原样收到（仅清单 run_scope=true）'},
        },
    }


def manifest_schema() -> dict:
    name = {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'}
    title = lambda limit: {'type': 'string', 'minLength': 1, 'maxLength': limit}  # noqa: E731
    clock = {'type': 'string', 'pattern': '^([01][0-9]|2[0-3]):[0-5][0-9]$'}
    return {
        '$schema': DRAFT,
        'title': 'autocontrol 接入清单 v1（autocontrol.json）',
        'type': 'object',
        'additionalProperties': False,
        'required': [
            'protocol',
            'name',
            'display_name',
            'workdir',
            'command',
            'phases',
            'schedule',
            'sections',
            'tasks',
        ],
        'properties': {
            'protocol': {'const': protocol.VERSION},
            'name': {'type': 'string', 'pattern': '^[a-z][a-z0-9_-]{0,31}$'},
            'display_name': title(32),
            'workdir': {'type': 'string', 'minLength': 1, 'description': '相对清单文件所在目录'},
            'private': {'type': ['string', 'null']},
            'lark_config': {'type': ['string', 'null']},
            'command': {
                'type': 'array',
                'minItems': 1,
                'items': {'type': 'string', 'minLength': 1},
                'description': '占位符：{phase} {service_day} {mode} {python}；必须包含 {phase}',
            },
            'phases': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': name},
            'observe_phase': name,
            'timeouts': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'phase': {'type': 'number', 'minimum': 10, 'maximum': 86400},
                    'observe': {'type': 'number', 'minimum': 10, 'maximum': 86400},
                },
            },
            'schedule': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['times'],
                'properties': {
                    'timezone': {'type': 'string'},
                    'reset': clock,
                    'times': {'type': 'array', 'minItems': 1, 'items': clock},
                },
            },
            'account_lock': {'type': ['string', 'null']},
            'run_scope': {'type': 'boolean'},
            'sections': {
                'type': 'array',
                'minItems': 1,
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'required': ['key', 'title'],
                    'properties': {'key': name, 'title': title(16), 'aggregate': TASK_ID},
                },
            },
            'phase_titles': {'type': 'object', 'additionalProperties': title(16)},
            'tasks': {
                'type': 'array',
                'minItems': 1,
                'maxItems': protocol.MAX_TASKS,
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'required': ['id', 'title'],
                    'properties': {
                        'id': TASK_ID,
                        'title': title(20),
                        'section': name,
                        'kind': {'enum': ['goal', 'job', 'step']},
                        'parent': TASK_ID,
                        'seed': {'type': 'boolean'},
                        'demand': {
                            'enum': list(DEMANDS),
                            'description': '需求方：daily 每日、weekly 每周、window 服务器当天开放才有',
                        },
                        'endpoint': {
                            'enum': list(ENDPOINTS),
                            'description': '今日终点：target 达到目标、once 完成一次、claim 领完奖励',
                        },
                        'discovery': {
                            'type': 'boolean',
                            'description': '「开放活动」发现任务：上报当天开放但无模块处理的活动数',
                        },
                    },
                },
            },
            'reasons': {
                'type': 'object',
                'propertyNames': {'pattern': '^[a-z][a-z0-9_]{0,47}$'},
                'additionalProperties': title(40),
            },
            'short_reasons': {'type': 'object', 'additionalProperties': title(8)},
            'count_keys': {
                'type': 'array',
                'items': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,47}$'},
            },
            'units': {'type': 'array', 'items': title(4)},
        },
    }


SCHEMAS = {
    'manifest.schema.json': manifest_schema,
    'event.schema.json': event_schema,
    'result.schema.json': result_schema,
}


def render(name: str) -> str:
    return json.dumps(SCHEMAS[name](), ensure_ascii=False, indent=2) + '\n'


def export(directory) -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in SCHEMAS:
        path = directory / name
        path.write_text(render(name), encoding='utf-8')
        paths.append(path)
    return paths
