"""Single-process ownership for the local media store.

Copyright (c) 2026 TheVibeFounder.
"""
import os
from pathlib import Path


def acquire_service_lock(downloads):
    handle = (Path(downloads) / '.service.lock').open('a+')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            handle.write('0')
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another FrameGrab service owns this Downloads folder. Stop it before starting a second service.') from None
    return handle
