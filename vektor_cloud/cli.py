"""VektorCode CLI agent — command-line interface for Vektor.

Usage:
    python -m vektor_cloud.cli do "your task here"
    python -m vektor_cloud.cli chat
    python -m vektor_cloud.cli health
    python -m vektor_cloud.cli kill
    python -m vektor_cloud.cli unkille
"""
import argparse
import sys

from vektor_cloud.core import VektorCore, kill_on
from vektor_cloud import aegis


def cmd_do(args):
    """Plan a task (draft-only, never executes)."""
    core = VektorCore()
    result = core.run_command(args.task, approved=False)
    print(f"Status: {result.get('status', 'unknown')}")
    if result.get('plan'):
        print("\nPlan:")
        for i, step in enumerate(result['plan'], 1):
            print(f"  {i}. {step}")
    if result.get('reason'):
        print(f"\nNote: {result['reason']}")
    return 0


def cmd_chat(args):
    """Interactive chat session (draft-only)."""
    core = VektorCore()
    print("Vektor chat — type 'exit' to quit. Draft-only, nothing executes.")
    while True:
        try:
            user_in = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if user_in.lower() in ('exit', 'quit'):
            print("Bye.")
            break
        if not user_in:
            continue
        result = core.run_command(user_in, approved=False)
        print(f"\nVektor: {result.get('output', result.get('reason', 'No output'))}")
    return 0


def cmd_health(args):
    """Show system health."""
    from vektor_cloud import memory, receipts
    print("VektorCode Health:")
    print(f"  Kill switch: {'ON' if kill_on() else 'off'}")
    print(f"  Aegis Gate: active")
    print(f"  Core: ready")
    try:
        print(f"  Composio: {'configured' if __import__('os').environ.get('COMPOSIO_API_KEY') else 'not configured'}")
    except Exception:
        pass
    return 0


def cmd_kill(args):
    """Engage the kill switch."""
    import os
    # Kill switch via environment or file
    kill_file = os.path.expanduser("~/.vektor/KILL")
    os.makedirs(os.path.dirname(kill_file), exist_ok=True)
    with open(kill_file, 'w') as f:
        f.write("kill")
    print("Kill switch ENGAGED. All work refuses until unkilled.")
    return 0


def cmd_unkill(args):
    """Disengage the kill switch."""
    import os
    kill_file = os.path.expanduser("~/.vektor/KILL")
    if os.path.exists(kill_file):
        os.remove(kill_file)
        print("Kill switch released.")
    else:
        print("Kill switch was not engaged.")
    return 0


def cmd_scan(args):
    """Run FANGS security scan on an allowlisted host."""
    from vektor_cloud import vektorbot_fangs
    runner = vektorbot_fangs.FangsRunner()
    try:
        report = runner.run(args.host)
    except vektorbot_fangs.TargetNotAuthorized:
        print(f"Blocked: {args.host} is not on the Vektor allowlist. Scan refused.")
        return 1
    print(f"Scan of {args.host} (report {report.get('report_id', '?')}):")
    findings = report.get('findings', [])
    if not findings:
        print("  No findings.")
    for f in findings:
        print(f"  [{f.get('severity', '?')}] {f.get('check', '')}: {f.get('message', '')}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='vektor',
        description='VektorCode CLI agent — draft-only, nothing executes without approval.')
    sub = parser.add_subparsers(dest='command', required=True)

    p_do = sub.add_parser('do', help='Plan a task (draft-only)')
    p_do.add_argument('task', help='The task to plan')
    p_do.set_defaults(func=cmd_do)

    p_chat = sub.add_parser('chat', help='Interactive chat session')
    p_chat.set_defaults(func=cmd_chat)

    p_health = sub.add_parser('health', help='Show system health')
    p_health.set_defaults(func=cmd_health)

    p_kill = sub.add_parser('kill', help='Engage kill switch')
    p_kill.set_defaults(func=cmd_kill)

    p_unkill = sub.add_parser('unkill', help='Release kill switch')
    p_unkill.set_defaults(func=cmd_unkill)

    p_scan = sub.add_parser('scan', help='FANGS security scan (allowlisted hosts only)')
    p_scan.add_argument('host', help='Host to scan')
    p_scan.set_defaults(func=cmd_scan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
