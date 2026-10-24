"""Exercise the actual launcher with private stub processes; never touch real Docker."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def executable(path: Path, text: str):
    path.write_text(text)
    path.chmod(0o755)


def launcher_project(tmp_path: Path, scenario: str):
    project = tmp_path / 'project'
    for directory in ['scripts', 'config', '.sglang/bin', 'fakebin', 'deploy/searxng/core-config']:
        (project / directory).mkdir(parents=True)
    for name in ['4_start_backend.sh', '_workspace_paths.sh']:
        shutil.copy(ROOT / 'scripts' / name, project / 'scripts' / name)
    executable(project / '.sglang/bin/python', f'''#!/bin/sh
if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "backend.tools.backend_startup_support" ] && [ "${{3:-}}" = "init" ] && [ -n "${{STUB_NONCE:-}}" ]; then
    {sys.executable} "$@" >/dev/null || exit $?
    printf "STARTUP_NONCE='%s'\\n" "$STUB_NONCE"
    exit 0
fi
exec {sys.executable} "$@"
''')
    fakebin = project / 'fakebin'
    (project / '.sglang/bin/activate').write_text(f'export PATH="{fakebin}:$PATH"\n')
    (project / 'config/config-overall.yaml').write_text('server: config/server.yaml\ninfer: config/infer.yaml\nasr: config/asr.yaml\nlocal_memory: config/memory.yaml\n')
    (project / 'config/server.yaml').write_text('server: {host: 127.0.0.1, port: 18090, workers: 1}\nocr: {enabled: false}\n')
    (project / 'config/infer.yaml').write_text('{}')
    asr = yaml.safe_load((ROOT / 'config/config-asr.yaml').read_text())
    asr['enabled'] = False
    asr['browser_security']['https_enabled'] = False
    (project / 'config/asr.yaml').write_text(yaml.safe_dump(asr))
    (project / 'config/memory.yaml').write_text('local_memory:\n  startup:\n    gate_timeout_seconds: 3\n    searxng_attempt_timeout_seconds: 2\n')
    deploy = project / 'deploy/searxng'
    (deploy / 'docker-compose.yml').write_text('services: {}')
    (deploy / '.env.example').write_text('')
    (deploy / 'core-config/settings.yml.example').write_text('')
    log = project / 'operations.jsonl'
    executable(fakebin / 'lsof', '#!/bin/sh\nexit 1\n')
    executable(fakebin / 'python3', f'''#!{sys.executable}
import os, time, json
from pathlib import Path
root=Path(os.environ['STUB_PROJECT'])
gate=os.environ.get('LOCAL_MEMORY_STARTUP_GATE')
nonce=os.environ.get('LOCAL_MEMORY_STARTUP_NONCE')
(root/'backend-started.json').write_text(json.dumps({{'gate':gate,'nonce':nonce}}))
if gate:
 p=Path(gate)
 while not p.exists(): time.sleep(.01)
 receipt=p.parent/'searxng.json'
 (root/'memory-released.json').write_text(json.dumps({{'nonce':p.read_text().strip(),'expected':nonce,'private':oct(p.parent.stat().st_mode & 0o777),'receipt':receipt.exists() and (p.parent/'worker.pid').exists()}}))
while True: time.sleep(.1)
''')
    executable(fakebin / 'curl', f'''#!{sys.executable}
import os,sys,time,json
from pathlib import Path
url=sys.argv[-1]
with open(os.environ['STUB_LOG'],'a') as f: f.write(json.dumps({{'op':'curl','url':url,'gate':os.environ.get('LOCAL_MEMORY_STARTUP_GATE')}})+'\\n')
if url.endswith('/health'): sys.exit(0)
if '/search?' in url:
 if os.environ['STUB_SCENARIO']=='external': sys.exit(0)
 if os.environ['STUB_SCENARIO']=='hang_health' and (Path(os.environ['STUB_PROJECT'])/'compose-up').exists():
  while True: time.sleep(.1)
sys.exit(1)
''')
    executable(fakebin / 'docker', f'''#!{sys.executable}
import os,sys,time,json
args=sys.argv[1:]
with open(os.environ['STUB_LOG'],'a') as f: f.write(json.dumps({{'op':'docker','args':args,'gate':os.environ.get('LOCAL_MEMORY_STARTUP_GATE')}})+'\\n')
scenario=os.environ['STUB_SCENARIO']
if (scenario=='hang_info' and args==['info']) or (scenario=='hang_compose' and 'up' in args) or 'down' in args:
 while True: time.sleep(.1)
if scenario=='fail' and args==['info']: sys.exit(1)
if 'up' in args: open(os.path.join(os.environ['STUB_PROJECT'],'compose-up'),'w').write('yes')
if 'ps' in args and scenario=='existing_containers': print('externally-owned-container')
sys.exit(0)
''')
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'STUB_PROJECT':str(project), 'STUB_SCENARIO':scenario, 'STUB_LOG':str(log), 'SEARXNG_AUTOSTART': '0' if scenario in {'disabled', 'leading_dash_nonce'} else '1', 'STUB_NONCE': '-deterministic_launch_nonce' if scenario=='leading_dash_nonce' else ''}
    return project, env


@pytest.mark.parametrize('scenario', ['hang_info','hang_compose','hang_health','fail','external','disabled','existing_containers','leading_dash_nonce'])
def test_actual_launcher_releases_nonce_gate_without_waiting_for_searxng(tmp_path, scenario):
    project, env = launcher_project(tmp_path, scenario)
    process = subprocess.Popen(['bash', str(project/'scripts/4_start_backend.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    output = ''
    try:
        deadline = time.monotonic()+5
        marker = project/'memory-released.json'
        while not marker.exists() and process.poll() is None and time.monotonic()<deadline: time.sleep(.02)
        if not marker.exists():
            if process.poll() is None: os.killpg(process.pid, signal.SIGKILL)
            output = process.communicate(timeout=4)[0]
        assert marker.exists(), output
        result = json.loads(marker.read_text())
        assert result['nonce'] == result['expected']
        if scenario == 'leading_dash_nonce':
            assert result['nonce'] == '-deterministic_launch_nonce'
        assert result['private'] == '0o700'
        assert result['receipt'], 'dispatch receipt must exist before memory is released'
        # Let the bounded worker reach its outcome (including infinite Docker stubs).
        time.sleep(2.4)
        process.terminate()
        output = process.communicate(timeout=5)[0]
        operations = [json.loads(line) for line in (project/'operations.jsonl').read_text().splitlines()]
        assert all(op['gate'] is None for op in operations), 'gate env is backend-only'
        downs = [op for op in operations if 'down' in op.get('args',[])]
        if scenario in {'external','disabled','existing_containers','hang_info','fail','leading_dash_nonce'}:
            assert not downs
        if scenario in {'hang_compose','hang_health'}:
            assert len(downs) == 1, operations
        assert process.returncode == 0, output
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)


def test_actual_gate_release_cli_roundtrips_leading_dash_nonce(tmp_path):
    directory = tmp_path / 'private-launch'
    directory.mkdir(mode=0o700)
    nonce = '-deterministic_cli_nonce'
    subprocess.run([sys.executable, '-m', 'backend.tools.backend_startup_support',
                    'release', '--directory', str(directory), f'--nonce={nonce}'],
                   cwd=ROOT, check=True, capture_output=True, text=True)
    assert (directory / 'memory.gate').read_text() == nonce + '\n'
