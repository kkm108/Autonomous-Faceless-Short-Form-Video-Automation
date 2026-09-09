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
from .settings import RunSettings

# Exit-code semantics (R1-F5): distinct codes let a scheduler react.
_EXIT_OK = 0
_EXIT_ERROR = 1          # expected/handled failure (auth, challenge, user error)
_EXIT_INTERNAL = 2       # unexpected crash / programming error
_EXIT_USAGE = 3          # CLI usage error (bad args)


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
    settings = RunSettings.from_args(args)
    tts_provider = settings.tts_provider

    missing = config.validate_environment(tts_provider)
    if missing:
        print(f"ERROR: TTS provider '{tts_provider}' needs missing "
              f"packages: {', '.join(missing)}. Install with "
              f"'pip install -r requirements.txt'.")
        return _EXIT_INTERNAL

    if not args.topic and not settings.topic_ideation_enabled:
        print("ERROR: no seed topic given and the Ask Studio topic-ideation "
              "pre-stage is disabled. Pass a topic, re-enable ideation "
              "(--ideate / AUTOMATO_TOPIC_IDEATION_ENABLED=1), or drop "
              "--no-ideate.")
        return _EXIT_USAGE

    seed = {}
    if args.topic:
        seed["topic"] = args.topic
    from .orchestrator import run_workflow
    state = run_workflow(args.workflow, seed, resume_run_id=args.resume,
                         settings=settings)
    print(f"\nRun {state.run_id} finished -> {state.run_dir}")
    for sid, outputs in state.completed.items():
        print(f"  {sid}: {outputs}")
    return 0


def cmd_login(args) -> int:
    from .browser.factory import PersistentBrowser
    from .browser.session import profile_dir_for

    profile = {
        "youtube": "https://studio.youtube.com/",
        "ai_studio": "https://aistudio.google.com/prompts/new_chat",
        "perchance": "https://perchance.org/ai-text-to-image-generator",
    }.get(args.provider)
    if profile is None:
        # TTS, and similarly non-login providers, are intentionally absent (R3-W6):
        # the README documents that they need no login.
        print(f"Unknown provider '{args.provider}'. Known: " + ", ".join(
            ["youtube", "ai_studio", "perchance"]))
        return 1

    # login always runs headed so the human can sign in; browser choice still
    # honors the run's settings (R2-W2/R2-F2).
    settings = RunSettings.from_args(args)
    settings.headless_mode = "headed"

    print(f"Opening visible browser for '{args.provider}' at {profile}")
    print("Sign in as needed, then close the browser window to finish.")
    browser = PersistentBrowser(profile_dir_for(args.provider), headless=False,
                                browser_choice=settings.browser_choice,
                                headless_mode=settings.headless_mode)
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
    out = import_replay(args.file, provider=args.provider, out=args.out,
                        force=args.force)
    print(out)
    return 0


def cmd_backup(args) -> int:
    config.ensure_dirs()
    from .backup import build_archive
    print("Backing up profiles, workflows, config and run outputs...")
    if args.no_encrypt:
        print("WARNING: --no-encrypt means the archive contains live signed-in "
              "sessions in plaintext. Protect it yourself (encrypt/rotate).")
    else:
        print("Backup will be AES-encrypted (set AUTOMATO_BACKUP_PASSPHRASE to "
              "use your own passphrase, or record the generated one).")
    path = build_archive(out_path=args.out, include_outputs=not args.no_outputs,
                         encrypt=not args.no_encrypt, passphrase=args.passphrase)
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
    if args.passphrase:
        print("Using the provided --passphrase to decrypt the archive.")
    else:
        print("If the archive is encrypted I'll read the passphrase from "
              "the AUTOMATO_BACKUP_PASSPHRASE env var.")
    print("WARNING: restoring signed-in sessions onto this machine will let the "
          "engine act as those accounts. Make sure this is the machine you want.")
    try:
        root = restore_archive(Path(args.archive), dest_root=dest,
                               force=args.force, apply_config=args.apply_config,
                               passphrase=args.passphrase)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return 1
    print(f"\nRestore complete -> {root}")
    return 0


def cmd_health_check(args) -> int:
    """R3-F1: re-validate static locators against the live sites."""
    from .healthcheck import run as healthcheck_run
    print("Selector health check (static locators, no learned overlay). "
          "Login may be required for some sites; check them first with "
          "'python -m automato login <provider>'.")
    results = healthcheck_run(
        provider=args.provider,
        headless=args.headless,
        browser_choice=args.browser,
        timeout_ms=args.timeout,
    )
    print()
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"{mark} [{r['group']}] {r['provider']}: {r['detail']}")
    ok = sum(1 for r in results if r["ok"])
    print(f"\n{ok}/{len(results)} locator groups resolved.")
    return 0 if ok == len(results) else 1


def cmd_trend(args) -> int:
    """R3-W1: surface the recovery-attempt trend (the drift early-warning)."""
    from .resilience import recovery as recovery_mod
    trend = recovery_mod.recovery_trend(provider=args.provider, days=args.days)
    totals = trend.pop("__totals__", {"attempts": 0, "succeeded": 0})
    print(f"LLM-recovery attempts over the last {args.days} days "
          f"({totals['attempts']} attempts, {totals['succeeded']} succeeded):")
    rows = sorted(trend.items())
    for prov, agg in rows:
        rate = agg.get("success_rate")
        rate_s = "n/a" if rate is None else f"{rate * 100:.0f}%"
        print(f"  {prov:<12} attempts={agg['attempts']:<4} "
              f"succeeded={agg['succeeded']:<4} success={rate_s}")
    if not rows:
        print("  (no recovery activity recorded yet)")
    alerts = recovery_mod.trend_alert(provider=args.provider)
    print()
    if not alerts:
        print("No upward recovery trend detected.")
    else:
        print(f"ATTENTION: {len(alerts)} provider(s) show a climbing recovery rate:")
        for a in alerts:
            print(f"  {a['provider']}: {a['fold_increase']}x the preceding week "
                  f"({a['recent_attempts']} vs {a['baseline_attempts']} attempts). "
                  f"{a['note']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="automato",
                                     description="Autonomous faceless short-form video automation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full pipeline. With a topic, that "
                                       "topic is scripted; without one, the Ask "
                                       "Studio pre-stage derives a topic from the "
                                       "channel's performance.")
    p_run.add_argument("topic", nargs="?", default=None,
                       help="Seed topic for the video (omit to let the Ask "
                            "Studio ideation pre-stage derive one)")
    p_run.add_argument("--ideate", action="store_true",
                       help="Force the Ask Studio topic-ideation pre-stage even "
                            "when a topic was given")
    p_run.add_argument("--no-ideate", action="store_true",
                       help="Disable the Ask Studio topic-ideation pre-stage")
    p_run.add_argument("--channel", default=None,
                       help="Target brand channel for ideation + publish "
                            "(allowlist names; default resolves from the topic "
                            "or config.CHANNEL_DEFAULT)")
    p_run.add_argument("--yes", action="store_true",
                       help="Skip the interactive confirm-before-publish prompt")
    p_run.add_argument("--workflow", default=config.DEFAULT_WORKFLOW,
                       help="Workflow manifest name")
    p_run.add_argument("--visibility", choices=["public", "unlisted", "private"],
                       default=None,
                       help="public | unlisted | private (default: AUTOMATO_VISIBILITY "
                            "env var, else config default = unlisted)")
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
    p_login.add_argument("provider", choices=["youtube", "ai_studio", "perchance"])
    p_login.add_argument("--browser", choices=["edge", "chrome", "brave", "chromium"],
                         default=None, help="Browser engine to use")
    p_login.add_argument("-v", "--verbose", action="store_true")
    p_login.set_defaults(func=cmd_login)

    p_hc = sub.add_parser("health-check",
                          help="Re-validate each provider's static locators against "
                               "the live site (monthly drift check)")
    p_hc.add_argument("--provider", default=None,
                      help="Check a single provider: youtube | ai_studio | perchance | tts")
    p_hc.add_argument("--headless", action="store_true",
                      help="Run browsers hidden")
    p_hc.add_argument("--browser", choices=["edge", "chrome", "brave", "chromium"],
                      default=None, help="Browser engine to use")
    p_hc.add_argument("--timeout", type=int, default=8000, metavar="MS",
                      help="Per-locator-group timeout in milliseconds (default 8000)")
    p_hc.add_argument("-v", "--verbose", action="store_true")
    p_hc.set_defaults(func=cmd_health_check)

    p_trend = sub.add_parser("trend",
                             help="Show the LLM-recovery trend (drift early-warning)")
    p_trend.add_argument("--provider", default=None,
                         help="Filter the trend to one provider")
    p_trend.add_argument("--days", type=int, default=14, metavar="N",
                         help="How many days of history to summarize (default 14)")
    p_trend.add_argument("-v", "--verbose", action="store_true")
    p_trend.set_defaults(func=cmd_trend)

    p_ri = sub.add_parser("replay-import",
                          help="Import a @puppeteer/replay JSON (DevTools Recorder) "
                               "export to seed provider locators")
    p_ri.add_argument("file", help="Path to the recorded replay JSON")
    p_ri.add_argument("--provider", default=None,
                      help="Provider name to associate/locate the overlay")
    p_ri.add_argument("--out", default=None, help="Optional output JSON path")
    p_ri.add_argument("--force", action="store_true",
                      help="Overwrite (replace) an existing non-empty target file; "
                           "by default new selectors merge into it")
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
    p_bk.add_argument("--no-encrypt", action="store_true",
                      help="Write an unencrypted archive (default: AES-encrypted, "
                           "since backups carry live sessions)")
    p_bk.add_argument("--passphrase", default=None,
                      help="Passphrase for the AES archive (default: "
                           "AUTOMATO_BACKUP_PASSPHRASE env var, else a randomly "
                           "generated one that is printed)")
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
    p_rs.add_argument("--passphrase", default=None,
                      help="Passphrase to decrypt an encrypted archive (default: "
                           "AUTOMATO_BACKUP_PASSPHRASE env var)")
    p_rs.add_argument("-v", "--verbose", action="store_true")
    p_rs.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001
        import traceback

        # Expected, domain-level failures -> distinct code + one clear line.
        from .adapters.base import ExecutorError
        from .backup import RestoreError
        from .browser.session import AuthRequiredError
        from .manifest import WorkflowError
        from .run_guard import RunGuardError
        from .settings import SettingsError
        if isinstance(exc, (ExecutorError, WorkflowError, RestoreError,
                            AuthRequiredError, RunGuardError, SettingsError)):
            log = logging.getLogger("automato.cli")
            log.error("Command failed: %s", exc)
            sys.stderr.write(f"ERROR: {exc}\n")
            if "--verbose" in sys.argv or isinstance(exc, ExecutorError):
                sys.stderr.write("See the verbose log below for the full traceback.\n")
            return _EXIT_ERROR
        # Unexpected crash -> internal error code + traceback (DEBUG level) +
        # a short actionable pointer.
        log = logging.getLogger("automato.cli")
        sys.stderr.write(f"ERROR: unexpected crash: {exc}\n")
        sys.stderr.write("Log: use -v/--verbose for the full traceback.\n")
        log.debug("Unexpected crash", exc_info=True)
        traceback.print_exc(file=sys.stderr)
        return _EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
