"""One worker process can run a whole round (``main.py run``) on shared logins."""

import json
from pathlib import Path
import tempfile
import unittest

from autocontrol.projects.yslzm import phases as scheduled

DAY = '2026-11-11'
FAKE = '''
import json, os, sys, time
args = sys.argv[1:]
if args[:1] != ['run']:
    sys.exit(2)
phases = args[2].split(',')
assert args[3:6] == ['--not-before-game-day', DAY, '--scheduled-result'], args
behaviour = BEHAVIOUR
fd = int(os.environ['YSLZM_PROGRESS_FD'])
for phase in phases:
    os.write(fd, (json.dumps({'kind': 'task_started', 'phase': phase, 'task_id': 'arena'}) + '\\n').encode())
    action = behaviour.get(phase, 'ok')
    if action == 'hang':
        time.sleep(30)
    if action == 'crash':
        sys.exit(1)
    result = {'game_day': DAY, 'execution': 'complete', 'policy_jobs': {},
              'run_scope': {'uncertain_targets': [[phase, 1]]}}
    print(json.dumps({'phase': phase, 'exit_code': 0, 'logins': int(phase == phases[0]),
                      'result': result}), flush=True)
'''


class RunRoundTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.project = root / 'workspace'
        self.project.mkdir()
        self.private = root / 'private'
        self.private.mkdir(mode=0o700)

    def fake(self, behaviour=None, *, source=None):
        text = (
            source
            if source is not None
            else FAKE.replace('DAY', repr(DAY)).replace('BEHAVIOUR', repr(behaviour or {}))
        )
        (self.project / 'main.py').write_text(text)

    def run_round(self, phases, *, timeout=20, scope=None):
        events, finished = [], []
        reported = scheduled.run_round(
            phases, DAY, project=self.project, private=self.private, timeout=timeout,
            run_scope=scope, on_event=events.append,
            on_phase=lambda name, record: finished.append((name, record)),
        )  # fmt: skip
        return reported, finished, events

    def test_each_phase_is_reported_in_order_and_archived(self):
        self.fake()
        scope = {'uncertain_targets': []}
        reported, finished, events = self.run_round(('cardbook', 'auto', 'craft'), scope=scope)
        self.assertEqual(reported, ('cardbook', 'auto', 'craft'))
        self.assertEqual([name for name, _ in finished], ['cardbook', 'auto', 'craft'])
        for name, record in finished:
            self.assertEqual(record['command'], name)
            self.assertEqual(record['exit_code'], 0)
            self.assertIn('started_at', record)
            self.assertIn('finished_at', record)
        self.assertEqual([event['phase'] for event in events], ['cardbook', 'auto', 'craft'])
        self.assertEqual(
            sorted(scope['uncertain_targets']), [['auto', 1], ['cardbook', 1], ['craft', 1]]
        )
        self.assertEqual([record['session_logins'] for _, record in finished], [1, 0, 0])
        from autocontrol.projects.yslzm import YslzmProject

        project = YslzmProject(workdir=self.project, private=self.private)
        self.assertEqual(project.phase_metrics(finished[0][1])['sessions'], 1)
        saved = list((self.private / 'results').rglob('*.json'))
        self.assertEqual(len(saved), 3)

    def test_a_hanging_phase_times_out_and_later_phases_are_left_to_the_caller(self):
        self.fake({'auto': 'hang'})
        reported, finished, _ = self.run_round(('cardbook', 'auto', 'craft'), timeout=1.5)
        self.assertEqual(reported, ('cardbook', 'auto'))
        self.assertEqual(finished[1][1]['execution'], 'timeout')

    def test_a_crash_reports_the_phase_in_flight_only(self):
        self.fake({'auto': 'crash'})
        reported, finished, _ = self.run_round(('cardbook', 'auto', 'craft'))
        self.assertEqual(reported, ('cardbook', 'auto'))
        self.assertNotEqual(finished[1][1]['execution'], 'complete')
        self.assertIsNone(finished[1][1].get('game_day'))

    def test_a_worker_without_run_starts_nothing(self):
        self.fake(source='import sys\nsys.exit(2)\n')  # argparse refuses ``run``
        reported, finished, _ = self.run_round(('cardbook', 'auto'))
        self.assertEqual((reported, finished), ((), []))

    def test_missing_checkout_starts_nothing(self):
        reported = scheduled.run_round(
            ('cardbook',), DAY, project=self.project / 'missing', private=self.private, timeout=5
        )
        self.assertEqual(reported, ())

    def test_invalid_arguments_are_rejected(self):
        for phases in ((), ('auto', 'auto'), ('status',)):
            with self.subTest(phases=phases), self.assertRaises(ValueError):
                scheduled.run_round(phases, DAY, project=self.project, private=self.private)


class SharedSessionSettingTests(unittest.TestCase):
    def test_shared_session_is_opt_in_through_autocontrol_json(self):
        from autocontrol.projects.yslzm import YslzmProject

        with tempfile.TemporaryDirectory() as private:
            project = YslzmProject(workdir=private, private=private)
            self.assertFalse(project.shares_session())
            path = Path(private) / 'autocontrol.json'
            path.write_text(json.dumps({'shared_session': True}))
            self.assertTrue(project.shares_session())
            path.write_text('{not json')
            self.assertFalse(project.shares_session())
            path.write_text(json.dumps({'shared_session': 'yes'}))
            self.assertFalse(project.shares_session())


if __name__ == '__main__':
    unittest.main()
