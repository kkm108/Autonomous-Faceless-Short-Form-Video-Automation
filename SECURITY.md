# Security

If credential material or a backup archive is ever accidentally committed to this
repository, do these two things **in this order**: (1) **rotate/revoke first** —
assume the exposed data is already public (Git history is immutable once pushed
and may have been cloned), so change passwords, sign out of all sessions, and
check for unfamiliar activity before cleaning anything; (2) **rewrite history
second** — purge the files with `git filter-repo` (see `Tasks-Round4.md` R4-W0
for the verified procedure), force-push `--all` and `--tags`, and confirm with
`git log --all --diff-filter=A --name-only -- '<path>'` that nothing remains.
Never commit `backups/`, `profiles/`, or `*.zip` files; the pre-commit hook
(`git config core.hooksPath .githooks`) and the CI guard step enforce this.