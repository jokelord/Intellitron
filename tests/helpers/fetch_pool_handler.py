import asyncio
import os
import resource
import subprocess
import sys
import time


async def handle(operation, payload):
    if operation in {'child', 'detached_child'}:
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 start_new_session=operation == 'detached_child')
        return {'child_pid': child.pid}
    if operation == 'cleanup':
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(.08)
            with open(payload['marker'], 'w') as target:
                target.write('cleaned')
    if operation == 'crash':
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.abort()
    if operation == 'hang':
        time.sleep(60)
    if operation == 'noisy':
        print('x' * 1000000, flush=True)
    await asyncio.sleep(payload.get('delay', 0))
    return {'pid': os.getpid(), 'value': payload.get('value')}


async def shutdown():
    pass
