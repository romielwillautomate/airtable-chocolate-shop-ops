"""Test entry point for automation 06, so every automation folder can be tested the same way.

Automation 06 has no n8n workflow: its four scripts run inside Airtable's own automations. Its real
tests are two Node scripts next to this file:

    run-tests.mjs       runs the four scripts, unchanged, inside a strict stand-in for the Airtable
                        automation scripting environment, loaded with base/schema.json and the seed
    mutation-check.mjs  breaks the scripts on purpose, one realistic mistake at a time, and checks
                        that run-tests.mjs fails for every one of them

This wrapper runs both and exits with 0 only when both pass. It needs Node 18 or later on PATH. It
does not use n8n, the mock servers or the network, so N8N_URL and N8N_API_KEY are not needed.

Usage: python automations/06-airtable-native-scripts/tests/e2e_mock_test.py [--quick]
       --quick runs run-tests.mjs only (the mutation check takes about four minutes).
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def main() -> int:
    node = shutil.which('node')
    if not node:
        print('Node 18 or later is needed for these tests and was not found on PATH.')
        return 2
    steps = ['run-tests.mjs'] if '--quick' in sys.argv[1:] else ['run-tests.mjs', 'mutation-check.mjs']
    failed = []
    for name in steps:
        print(f'\n=== node {name}', flush=True)
        result = subprocess.run([node, str(HERE / name)], cwd=ROOT)
        if result.returncode != 0:
            failed.append(name)
    print('\nAll passed.' if not failed else f"\nFailed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
