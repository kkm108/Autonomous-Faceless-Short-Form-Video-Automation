# Customizing the Engine & Using Backup/Restore for New Objectives

This guide is for **modifying** the automation engine and for **forking it into a
tailored configuration** targeting a specific objective (a different niche, content
style, provider setup, or one-off experiment), without disturbing your working
installation.

It complements the base `README.md` (which covers first-time login and the stock
run). Here we focus on:

1. Choosing the right **modification strategy** — edit in place vs. a sandboxed copy.
2. What to change for a new objective: **workflow manifest → config → adapters → prompts**.
3. Using the **`backup` / `restore` portability layer** to snapshot and relocate an altered configuration onto another machine or a fresh instance.

---

## 1. Two strategies: edit vs. fork

| Strategy | Command | Use when |
|---|---|---|
| **Edit in place** | edit files, then `python -m automato run "..."` | Small tweaks; you own the working copy and don't need the old behavior preserved. |
| **Fork via backup/restore** | `backup` → edit the copy → `restore` | You want a separate experiment; you must keep the proven install intact; you'll move to another machine. |

**Rule of thumb:** if the change is experimental, is a new niche you may want to
abandon, or might break the stock pipeline, **fork it**. Keep your canonical,
verified configuration in the current install and treat it as the source of truth.

---

## 2. What "a configuration" is made of

Everything that defines an objective lives in a handful of files. The `backup`
command groups exactly these:

| Area | Path | Role |
|---|---|---|
| Workflow | `workflows/<name>.json` | Ordered stages, input/output bindings. The single most important file for a new objective. |
| Config | `automato/config.py` | Provider choices, TTS strategy, visibility, browser mode, resilience timing. |
| Scripting prompt | `automato/llm/script_prompts.py` | The exact instructions given to the LLM; controls output format for your niche. |
| Adapters | `automato/adapters/<category>/<name>.py` | Actual browser/Ux automation per stage. |
| Sessions | `profiles/<provider>/` | Persistent signed-in sessions (kept by `backup`, never committed). |
| Run history | `output/<run_id>/` | Scripts, voiceovers, final videos, publish links. |

`backup` also writes a `manifest.json` (source root, format version, counts) and a
`config-snapshot.json` (the 16 most relevant config fields), so you can diff what a
given objective's config was.

---

## 3. Tailoring to a new objective (walkthrough)

Say you want a new niche: *"high-protein meal prep"* instead of the stock science
topic. Each objective is a combination of the items below.

### 3.1 Define the workflow

The stock workflow is `workflows/faceless_short.json`. Copy it to a new name for the
niche:

```powershell
Copy-Item workflows/faceless_short.json workflows/meal_prep.json
```

A manifest lists stages in strict order. Each stage names an **adapter** (dotted
path under `automato.adapters.`), its **inputs** (which prior-output artifact feeds
it) and its **output** binding.

The binding rules are important:

- A **dotted value** like `"voiceover.audio"` means *"the artifact named `audio` produced by the `voiceover` stage"*.
- A **non-dotted value** like `"script.json"` or `"assets"` means *"the file/dir `run_dir/<value>`"*.
- So always refer to a prior stage's output by its **dotted output binding**, e.g. `"assemble": { "inputs": { "audio": "voiceover.audio", "video": "final.mp4" } }`.

Run a specific workflow with `--workflow`:

```powershell
python -m automato run "5 high-protein meal-prep ideas" --workflow meal_prep --visibility unlisted
```

> `--workflow <name>` loads `workflows/<name>.json`.
> `DEFAULT_WORKFLOW` in `config.py` changes the default for `run` with no flag.

### 3.2 Tune the config (`automato/config.py`)

Common knobs per objective:

```python
LLM_PROVIDER = "ai_studio"          # "ai_studio" (needs login) | "duckai" (no login)
TTS_PROVIDER  = "auto"              # auto | soundtools | edge_tts | pyttsx3
EDGE_TTS_VOICE = "en-US-ChristopherNeural"   # pick a voice matching the niche's tone
DEFAULT_VISIBILITY = "unlisted"     # unlisted | private | public
HEADLESS_MODE = "headed"            # headed | new | full  (headed is easiest to debug)
```

You can also override `TTS_PROVIDER` per-run on the command line:

```powershell
python -m automato run "topic" --tts edge_tts
```

(`--tts choices: auto|soundtools|edge_tts|pyttsx3`)

### 3.3 Rewrite the scripting prompt (`automato/llm/script_prompts.py`)

The LLM is told exactly what to emit. `generic_llm.py` parses a **delimited**
plain-text format (`TITLE` / `NARRATION` / `CAPTION` / `IMAGE` lines terminated by
`END`). To change the niche **and/or the format**, edit the system preamble and the
user prompt builder here. Keep the same `KEY | value` line grammar unless you also
update `_parse_script` in `generic_llm.py`.

### 3.4 (If needed) add or swap an adapter

Adapters live under `automato/adapters/<category>/<name>.py` and expose:

```python
def run(ctx, inputs, run_dir, session=None) -> dict:
    ...
    return {"artifact_name": str(path)}   # consumed by later stages via dotted binding
```

To register a new one:

1. Add the file under the right category, e.g. `automato/adapters/scripting/my_llm.py` exposing `run(...)`.
2. Register its provider mapping in `automato/orchestrator.py` (`_ADAPTER_PROVIDER`) and an optional auth check in `automato/providers.py`.
3. Point a stage of your workflow manifest at it, e.g. `"scripting": { "adapter": "scripting.my_llm", ... }`.

`manifest.py` resolves dotted adapter paths, so a fully-qualified override
(`"my.pkg.adapter"`) is also supported without editing the orchestrator.

---

## 4. Snapshoting and relocating a tailored config (backup/restore)

### 4.1 Take a backup

```powershell
python -m automato backup
```

- Writes `backups/automato_backup_<timestamp>.zip` (default).
- Bundles profiles, workflows, run outputs, and the config snapshot.
- **Excludes** volatile browser caches (GPU/Cache/Shader/Dawn/Crashpad, *.pma logs,
  session LOCK files, etc.) while **keeping** the auth-relevant data you need to stay
  logged in (Cookies, Login Data, Preferences, Local/Session Storage).

Useful variants:

```powershell
python -m automato backup --out "C:\exp\meal_prep_v2.zip"   # explicit destination
python -m automato backup --no-outputs                       # sessions + config only, no run outputs
python -m automato backup --keep 5                            # prune to newest 5 backups after writing
```

`--keep N` trims `backups/automato_backup_*.zip` down to the newest `N` after the
new backup is written (newest-first by filename timestamp; `0`/omitted keeps all).
Use it to bound disk growth on a machine that backs up frequently.

The archive is generated by `automato/backup.py` (`build_archive`); restoring is
`restore_archive`.

### 4.2 Restore onto the same or another machine

```powershell
# Restore into a fresh engine root; absolute paths rebind to the new root.
python -m automato restore backups/automato_backup_<timestamp>.zip --dir C:\Users\Me\engines\meal_prep

# Restoring over an already-populated destination requires --force (restore is a
# destructive overwrite by design, so this is opt-in to avoid accidental clobber).
python -m automato restore backups/automato_backup_<timestamp>.zip --dir C:\Users\Me\engines\meal_prep --force

# Reproduce the source machine's config values (idempotent override block).
python -m automato restore backups/automato_backup_<timestamp>.zip --dir C:\Users\Me\engines\meal_prep --force --apply-config
```

The restore does **portability rebinding**: any absolute engine path recorded in
your own artifacts (run state, config snapshots) is rewritten from the original
`source_root` to the new destination root, because `run_state.json` stores paths
JSON-escaped. Only authored text/JSON files are rewritten; browser database binaries
are intentionally left byte-identical to avoid corruption.

So a backup taken on machine A (`C:\Dev\automation system`) restores cleanly onto
machine B at (`C:\Users\Me\engines\meal_prep`) with all `run_dir` values pointing at
the new location — this is what makes the engine **portable**.

### 4.3 A portable, reproducible objective recipe

1. Land the stock install and one-time logins (per `README.md`).
2. `python -m automato backup --out base.zip` → your verified baseline.
3. Edit workflow/config/prompts for the new objective **in a copy** (or restore `base.zip --dir new_root` first, then edit there so the baseline stays untouched).
4. `python -m automato backup --out meal_prep_v2.zip` inside that sandbox → captures the tailored config and any sessions it added.
5. Ship `meal_prep_v2.zip` to any machine and `restore` it; the paths rebind automatically and logged-in sessions travel with it.

---

## 5. Reproducing / diffing a configuration

- `backup-manifest.json` inside each archive records `source_root`, `format_version`,
  `engine_version`, **per-file SHA-256 hashes** (every file is verified on restore,
  so a corrupt or tampered archive is refused), and per-area file counts —
  useful for confirming what a backup contains and that it matches expectations.
- `config-snapshot.json` holds every portable (non-path) config field (auto-reflected,
  not a hand-curated list), so you can compare two objective configs at a glance and
  re-apply them with `restore --apply-config`.

---

## 6. Tips

- **Never commit `profiles/` or `backups/`** to any repo — they contain live
  sessions. Treat a backup archive as a secret (the CLI prints this warning).
- **Verify a fork in isolation** with `restore --dir <sandbox>` before relying on it,
  then run a throwaway `--visibility private` run against it. Restoring over an
  already-populated install requires `--force`, so this is safe by default.
- **Reproduce the altered config** with `restore --apply-config`; without it only
  the *data* is restored, the destination keeps its own `config.py`.
- Keep the **workflow manifest binding rules** in mind (dotted = prior-stage artifact,
  non-dotted = `run_dir/<value>`). A dotted reference to a missing stage/key or a
  missing stage now raises a clear error instead of silently falling through — so a
  mistyped binding surfaces immediately at start-up.
- The LLM output is parsed locally as delimited text, not JSON — if a model starts
  ignoring the format, loosen the preamble or widen `_parse_script` in
  `generic_llm.py`, don't demand strict JSON from small web models.
