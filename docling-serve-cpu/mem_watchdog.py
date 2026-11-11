#!/usr/bin/env python3
"""Memory watchdog for docling-serve container.

If cgroup memory stays above THRESHOLD bytes while idle (usage unchanged
between checks), kill PID 1 so the container exits and docker's
restart policy brings it back with clean memory.
"""
import os
import signal
import time

THRESHOLD_GB = float(os.environ.get("WATCHDOG_MEM_LIMIT_GB", "7.0"))
CHECK_SECS = 15


def read_usage() -> int:
    for p in ("/sys/fs/cgroup/memory.current",
              "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            return int(open(p).read().strip())
        except (OSError, ValueError):
            continue
    return 0


def main() -> None:
    threshold = int(THRESHOLD_GB * 1024 ** 3)
    prev = -1
    stable_over = 0
    while True:
        time.sleep(CHECK_SECS)
        cur = read_usage()
        if cur > threshold and cur == prev:
            stable_over += 1
            # two identical readings above threshold => idle, safe to recycle
            if stable_over >= 2:
                print(f"[watchdog] idle mem {cur / 1024**2:.0f}MB > "
                      f"{THRESHOLD_GB}GB, recycling container", flush=True)
                os.kill(1, signal.SIGTERM)
                return
        else:
            stable_over = 0
        prev = cur


if __name__ == "__main__":
    main()
