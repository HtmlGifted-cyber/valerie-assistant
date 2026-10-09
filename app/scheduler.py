"""
scheduler.py - Valerie's local scheduled-task runner.

The desktop analogue of a cloud scheduled task: reads cfg["scheduled_tasks"]
from config.json (name, cron, prompt, provider), and at each cron firing
runs that prompt through brain.run_agent_loop with tools enabled, headless
(no GUI needed), logging the result. Install it as a systemd --user timer
or a crontab entry (see docs/README.md) so it keeps running even when the
Valerie GUI itself isn't open.

This intentionally does NOT run arbitrary code - a scheduled task is just
a prompt string, handled by the exact same sandboxed tool allow-list the
interactive GUI uses.
"""

import sys
import time

import config as cfg_mod
from brain import run_agent_loop

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:
    BlockingScheduler = None
    CronTrigger = None


def _run_task(task):
    cfg = cfg_mod.load_config()
    name = task.get("name", "unnamed task")
    provider = task.get("provider") or cfg.get("active_provider", "ollama")
    prompt = task.get("prompt", "")
    print(f"[valerie-scheduler] running '{name}' via {provider}")
    result = run_agent_loop(provider, prompt, cfg)
    cfg_mod.log_activity(f"scheduled:{name}", result[:500])
    print(f"[valerie-scheduler] '{name}' -> {result[:200]}")


def run_forever():
    if BlockingScheduler is None:
        print(
            "APScheduler isn't installed in this venv. Install it with:\n"
            "  pip install apscheduler\n"
            "or just invoke 'python3 scheduler.py --once' from cron/systemd instead."
        )
        sys.exit(1)

    cfg = cfg_mod.load_config()
    tasks = [t for t in cfg.get("scheduled_tasks", []) if t.get("enabled", True)]
    if not tasks:
        print("[valerie-scheduler] no enabled scheduled_tasks in config.json - nothing to do.")
        return

    sched = BlockingScheduler()
    for task in tasks:
        cron = task.get("cron")
        if not cron:
            continue
        trigger = CronTrigger.from_crontab(cron)
        sched.add_job(_run_task, trigger, args=[task], name=task.get("name"))
        print(f"[valerie-scheduler] scheduled '{task.get('name')}' at '{cron}'")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


def run_once():
    """Run every enabled task a single time right now - used by a plain
    cron/systemd-timer entry that doesn't want a long-lived process."""
    cfg = cfg_mod.load_config()
    for task in cfg.get("scheduled_tasks", []):
        if task.get("enabled", True):
            _run_task(task)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
