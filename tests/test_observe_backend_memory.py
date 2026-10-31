import json
import subprocess
import sys


def test_observer_preserves_target_and_emits_bounded_metadata(tmp_path):
    target = tmp_path / 'target.py'
    target.write_text("import sys\nassert sys.argv[1:] == ['personal', '--require-auth']\nimport time\nsecret = 'PRIVATE_PROMPT_SENTINEL'\nvalues = [bytearray(1024) for _ in range(100)]\ntime.sleep(6)\nprint('target-completed')\n")
    report = tmp_path / 'memory.jsonl'
    result = subprocess.run([
        sys.executable, 'scripts/observe_backend_memory.py', '--target', str(target),
        '--report', str(report), '--interval', '5', '--duration', '5', '--', 'personal', '--require-auth',
    ], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert 'target-completed' in result.stdout
    text = report.read_text()
    assert 'PRIVATE_PROMPT_SENTINEL' not in text
    row = json.loads(text.splitlines()[0])
    assert row['python_bytes'] > 0
    assert len(row['allocation_top']) <= 15
    assert report.stat().st_mode & 0o777 == 0o600
    assert all(set(item) == {'file', 'line', 'bytes', 'blocks'} for item in row['allocation_top'])
