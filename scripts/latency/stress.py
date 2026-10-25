"""Diagnostic only: retain one fixture while stressing CLI process lifetimes."""
import asyncio
import json
import os
import subprocess
import sys
import sample

os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])


def click(python, point, env):
    command = [python, '-m', 'a11y_computer_use', 'run-once',
               json.dumps(dict(tool='click', x=int(point.x), y=int(point.y)))]
    for i in range(200):
        result = subprocess.run(command, env=dict(env, PYTHONFAULTHANDLER='1'),
                                capture_output=True, text=True, cwd="/results")
        print(f'CLI {i}: {result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}',
              file=sys.stderr, flush=True)
        if result.returncode:
            subprocess.run(['gdb', '--batch', '-ex', 'thread apply all bt', python, '/results/core'])
            raise RuntimeError('CLI failed; diagnostics above')
    return result


sample.run_once_click = click
asyncio.run(sample.sample('R2', False))
