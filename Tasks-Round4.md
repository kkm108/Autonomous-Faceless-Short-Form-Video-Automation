# Tasks-Round4.md — Post-verification addendum

**Context:** Rounds 1–3 from `Tasks.md` are genuinely, independently verified as done — see the verification note in chat for how. This file covers what a fresh, from-scratch review (fresh `git clone`, not just the file tree) turned up on top of that: one urgent item that predates all three rounds, plus a handful of small residual findings from the R1–R3 work itself.

---

## R4-W0 — CRITICAL, OUT-OF-BAND: live session data is exposed in Git history (pre-existing, not caused by Rounds 1–3)

Four zip files (`backups/automato_backup_2026....zip`) were committed in this repo's first two commits and "removed" two commits later. Deleting a file in a later commit does **not** remove it from history — it is still fully present and downloadable by anyone who clones the repo, exactly as a plain `git clone` just retrieved it for this review. One archive was inspected (file listing only, no credential contents opened) and confirmed to be a genuine ~35MB bundle of all four Chromium profiles (`ai_studio`, `perchance`, `tts`, `youtube`), including their `Network/Cookies` and `Login Data` SQLite files, browsing `History`, and session-restore data — plus a Gmail-linked session (`mail.google.com`) reachable from the same profile as AI Studio. This repo is public, so this has been openly retrievable for as long as those commits have existed.

This is not a code-quality item and not something the coding agent should just fix as a normal task — do the first step yourself, right now, before anything else in this file:

- [x] **Rotate every credential that was ever live in any of these four browser profiles.** At minimum: change the password on the Google account(s) involved, use the account's "sign out of all sessions/devices" option, and check recent account activity for anything unfamiliar. Do this *before* the history rewrite below — the exposure already happened regardless of what gets cleaned up in Git, and automated secret-scanners generally don't flag this (a Chromium cookie/login database isn't a recognizable API-key pattern), so don't assume you'd have been alerted.
- [x] **Purge the backup files from Git history** (a normal `git rm` commit does not do this). Tested against a copy of this exact repo and confirmed working:
  ```bash
  # Use a fresh clone dedicated to this operation, not your normal working copy
  git clone https://github.com/kkm108/Autonomous-Faceless-Short-Form-Video-Automation.git purge-clone
  cd purge-clone
  pip install git-filter-repo
  git filter-repo --path backups --invert-paths
  git remote add origin https://github.com/kkm108/Autonomous-Faceless-Short-Form-Video-Automation.git
  git push origin --force --all
  git push origin --force --tags
  ```
  `git filter-repo` deliberately drops the `origin` remote as a safety measure — that's why it's re-added before pushing. This rewrites every commit hash on `main`, so any other clone (including the one used for this review) is now stale and would need to be re-cloned. If this repo was ever forked, the fork keeps the old history independently — this only cleans this copy.
- [x] Confirm the purge worked: `git log --all --diff-filter=A --name-only -- 'backups/*.zip'` should return nothing on the fresh push.
- [ ] Your current `.gitignore` already excludes `backups/`, `profiles/`, `output/`, and `*.zip` correctly — good. See R4-F1 below for a technical control so a `--force`-added exception can't slip through again.

---

## Weaknesses found while verifying Rounds 1–3 (ordered by impact)

- [x] **R4-W1 — Medium: the claimed "107 tests pass" doesn't hold on Linux, which undermines the very CI Round 2 built.** I installed the pinned `requirements.txt` fresh and ran the suite myself: 106 pass, 1 fails — `tests/test_backup.py::test_rebind_replaces_root` asserts the rebound path contains a literal backslash (`D:\new\engine`), but `_rebind()` in `backup.py` correctly converts separators to the *current* platform's convention, so on Linux it produces `D:/new/engine` and the assertion fails. `.github/workflows/ci.yml` runs on `ubuntu-latest`, so this test very likely fails there too right now — meaning the cross-platform CI added specifically to catch this class of issue (R2-W1/R2-F1) has a red check of its own that testing only on a Windows machine wouldn't surface. Fix: assert against `os.sep`-correct output (or parametrize/mock `os.sep` to check both branches), then confirm the Actions run on `main` is actually green.
- [x] **R4-W2 — Low: `pyproject.toml` disagrees with the newly-unified Python-version statement.** README.md and ARCHITECTURE.md now consistently say "Python 3.10+ minimum" (good — that inconsistency from the original review is resolved) but `pyproject.toml` sets `requires-python = ">=3.12"`, which is what's actually enforced if anyone installs this as a package. Pick one real minimum and make all three agree.
- [x] **R4-W3 — Low: two stray, unrelated files got swept into the repo root.** `Autonomous-Faceless-Short-Form-Video-Automation.txt` (a one-line file containing this repo's own URL) and `Hybrid-cloud-local-multi-agent-orchestrator.txt` (a one-line file containing the URL of a different, unrelated repo) were added in the same commit as a large batch of legitimate R2/R3 work. Harmless, but worth a `git rm` — and worth a glance at whatever produced them, since it's the kind of thing that could just as easily have been something less harmless.
- [x] **R4-W4 — Low: one dead line, and one partially-unified fix.** In `automato/llm/chat.py`'s new `wait_for_completion()`, `if unchanged >= 6: break` is unreachable — the `if unchanged >= 2: break` immediately above it always fires first. Separately, R2-W6's fix is correct in both places it needed to be (the `"END"`-substring bug is genuinely gone from both `generic_llm.py` and the shared helper), but `generic_llm.py`'s AI Studio polling loop still doesn't call the new shared `wait_for_completion()` — it kept its own separate (now also-correct) loop. Not a bug, just not the full one-implementation consolidation originally asked for; worth finishing if a similar heuristic bug is worth guarding against long-term.

## Future-proof additions

- [x] **R4-F1 — A technical control against re-committing profile/session data, not just `.gitignore`.** `.gitignore` only stops an *unthinking* `git add .`; it doesn't stop a `git add -f`. Add a pre-commit hook (or a CI job that fails the build) that blocks any staged path matching `backups/`, `profiles/`, `*.zip`, or anything over a few MB, so this specific mistake is structurally harder to repeat regardless of how it happens next time.
- [x] **R4-F2 — A `SECURITY.md` with exactly one paragraph:** what to do if a credential or backup archive is ever accidentally committed (rotate first, rewrite history second, in that order) — so the runbook exists before it's needed instead of being reconstructed under pressure.

---

