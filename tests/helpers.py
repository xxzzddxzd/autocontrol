import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / 'examples' / 'demo_worker'
DEMO_MANIFEST = DEMO_DIR / 'autocontrol.json'
SDK = ROOT / 'sdk' / 'python' / 'autocontrol_worker.py'


def demo_copy(tmp_path, *, worker_source=None, **manifest_changes) -> Path:
    """A private copy of the demo worker (with the SDK vendored) and its manifest."""
    target = tmp_path / 'demo'
    shutil.copytree(DEMO_DIR, target, ignore=shutil.ignore_patterns('state.json', '__pycache__'))
    shutil.copy(SDK, target / SDK.name)
    if worker_source is not None:
        (target / 'worker.py').write_text(worker_source)
    manifest = json.loads(DEMO_MANIFEST.read_text())
    manifest.update(private=str(tmp_path / 'private'), **manifest_changes)
    (target / 'autocontrol.json').write_text(json.dumps(manifest, ensure_ascii=False))
    return target / 'autocontrol.json'
