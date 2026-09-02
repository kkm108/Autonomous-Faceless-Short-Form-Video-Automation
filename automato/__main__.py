"""CLI entry: python -m automato run "topic" [--visibility X] [--headless]
              python -m automato login <provider>
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config
from .providers import register_all


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def cmd_run(args) -> int:
    register_all()
    config.ensure_dirs()
    config.DEFAULT_VISIBILITY = args.visibility
    _apply_browser_args(args)
    if getattr(args, "tts", None):
        config.TTS_PROVIDER = args.tts

    seed = {"topic": args.topic}
    from .orchestrator import run_workflow
    state = run_workflow(args.workflow, seed, resume_run_id=args.resume)
    print(f"\nRun {state.run_id} finished -> {state.run_dir}")
    for sid, outputs in state.completed.items():
        print(f"  {sid}: {outputs}")
    return 0


def _apply_browser_args(args) -> None:
    """Apply browser selection + headless mode to config before any session opens."""
    if getattr(args, "browser", None):
        config.BROWSER_CHOICE = args.browser
    if getattr(args, "headless_mode", None):
        config.HEADLESS_MODE = args.headless_mode
    # backwards-compatible flag: --headless forces full headless
    if getattr(args, "headless", False):
        config.HEADLESS_MODE = "full"


def cmd_login(args) -> int:
    from .browser.session import profile_dir_for
    from .browser.factory import PersistentBrowser

    profile = {
        "youtube": "https://studio.youtube.com/",
        "ai_studio": "https://aistudio.google.com/prompts/new_chat",
        "perchance": "https://perchance.org/ai-text-to-image-generator",
        "tts": "https://soundtools.io/text-to-speech/",
    }.get(args.provider)
    if profile is None:
        print(f"Unknown provider '{args.provider}'. Known: " + ", ".join(
            ["youtube", "ai_studio", "perchance", "tts"]))
        return 1

    _apply_browser_args(args)
    # login always runs headed so the human can sign in
    config.HEADLESS_MODE = "headed"

    print(f"Opening visible browser for '{args.provider}' at {profile}")
    print("Sign in as needed, then close the browser window to finish.")
    browser = PersistentBrowser(profile_dir_for(args.provider), headless=False)
    with browser:
        page = browser.first_page()
        page.goto(profile, wait_until="domcontentloaded", timeout=60000)
        print("Press Enter when you have finished signing in...")
        input()
        print("Saving session...")
    print("Session saved. You can now run the workflow.")
    return 0


def cmd_replay_import(args) -> int:
    register_all()
    config.ensure_dirs()
    from .tools.replay_import import import_replay
    out = import_replay(args.file, provider=args.provider, out=args.out)
    print(out)
    return 0


def cmd_backup(args) -> int:
    config.ensure_dirs()
    from .backup import build_archive
    print("Backing up profiles, workflows, config and run outputs...")
    print("WARNING: the archive contains live signed-in sessions. Treat it as a "
          "secret (encrypt/rotate accordingly).")
    path = build_archive(out_path=args.out, include_outputs=not args.no_outputs)
    print(f"\nBackup written -> {path}")
    if args.keep > 0:
        from .backup import prune_old_backups
        deleted = prune_old_backups(args.keep)
        if deleted:
            print(f"Retention: pruned {len(deleted)} old backup(s) -> "
                  + ", ".join(str(p) for p in deleted))
    return 0


def cmd_restore(args) -> int:
    config.ensure_dirs()
    from .backup import restore_archive
    dest = args.dir.resolve() if args.dir else None
    print(f"Restoring {args.archive} into {dest or '<current root>'}")
    print("WARNING: restoring signed-in sessions onto this machine will let the "
          "engine act as those accounts. Make sure this is the machine you want.")
    try:
        root = restore_archive(Path(args.archive), dest_root=dest,
                               force=args.force, apply_config=args.apply_config)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return 1
    print(f"\nRestore complete -> {root}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="automato",
                                     description="Autonomous faceless short-form video automation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full pipeline from a seed topic")
    p_run.add_argument("topic", help="Seed topic for the video")
    p_run.add_argument("--workflow", default=config.DEFAULT_WORKFLOW,
                       help="Workflow manifest name")
    p_run.add_argument("--visibility", choices=["public", "unlisted", "private"],
                       default=config.DEFAULT_VISIBILITY)
    p_run.add_argument("--resume", default=None,
                       help="Resume an existing run by its run id")
    p_run.add_argument("--headless", action="store_true",
                       help="Run browsers hidden (full headless)")
    p_run.add_argument("--browser", choices=["edge", "chrome", "brave", "chromium"],
                       default=None, help="Browser engine to use")
    p_run.add_argument("--headless-mode", choices=["headed", "new", "full"],
                       default=None,
                       help="headed, new (headless=new), or full (classic headless)")
    p_run.add_argument("--tts", choices=["auto", "soundtools", "edge_tts", "pyttsx3"],
                       default=None, help="TTS provider (default: auto fallback chain)")
    p_run.add_argument("-v", "--verbose", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_login = sub.add_parser("login", help="One-time visible login for a provider")
    p_login.add_argument("provider", choices=["youtube", "ai_studio", "perchance", "tts"])
    p_login.add_argument("--browser", choices=["edge", "chrome", "brave", "chromium"],
                         default=None, help="Browser engine to use")
    p_login.add_argument("-v", "--verbose", action="store_true")
    p_login.set_defaults(func=cmd_login)

    p_ri = sub.add_parser("replay-import",
                          help="Import a @puppeteer/replay JSON (DevTools Recorder) "
                               "export to seed provider locators")
    p_ri.add_argument("file", help="Path to the recorded replay JSON")
    p_ri.add_argument("--provider", default=None,
                      help="Provider name to associate/locate the overlay")
    p_ri.add_argument("--out", default=None, help="Optional output JSON path")
    p_ri.add_argument("-v", "--verbose", action="store_true")
    p_ri.set_defaults(func=cmd_replay_import)

    p_bk = sub.add_parser("backup",
                          help="Bundle profiles, workflows, config and run outputs "
                               "into a portable, relocatable .zip archive")
    p_bk.add_argument("--out", default=None,
                      help="Destination .zip path (default: backups/automato_backup_<ts>.zip)")
    p_bk.add_argument("--no-outputs", action="store_true",
                      help="Exclude run outputs (keep only sessions, learned locators, "
                           "workflows and config)")
    p_bk.add_argument("--keep", type=int, default=0, metavar="N",
                      help="Keep only the newest N backups, pruning older ones "
                           "(0 = keep all)")
    p_bk.add_argument("-v", "--verbose", action="store_true")
    p_bk.set_defaults(func=cmd_backup)

    p_rs = sub.add_parser("restore",
                          help="Restore a backup archive onto this engine root, "
                               "rebinding absolute paths for portability")
    p_rs.add_argument("archive", help="Path to the backup .zip produced by 'backup'")
    p_rs.add_argument("--dir", type=Path, default=None,
                      help="Engine root to restore into (default: current install)")
    p_rs.add_argument("--force", action="store_true",
                      help="Allow overwriting an already-populated destination")
    p_rs.add_argument("--apply-config", action="store_true",
                      help="Re-apply the source machine's config values to the "
                           "destination config.py (idempotent override block)")
    p_rs.add_argument("-v", "--verbose", action="store_true")
    p_rs.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
