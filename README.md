# Command Center

A local, zero-dependency dashboard that puts every project you work on in one
place: **Jira epics → tasks → merge requests** for tracked projects, and
**repo → open MRs → local git worktrees** for everything else.

Each project is its own workspace with its own Jira key. Add a project by
picking its folder; the dashboard finds the git repos inside and wires up the
view. Runs entirely on `127.0.0.1` — your tokens never leave your machine.

> Pure Python standard library. No `pip install`, no `npm`, no build step.
> If you have `python3` and `git`, it runs.

## Features

- **Multi-project switcher** — one dashboard for all your work; pick projects from a dropdown.
- **Jira view** — epics with your tasks first, others collapsed; each task shows its linked MRs and live statuses.
- **VCS view** (no Jira) — per repo: open merge requests + local git worktrees.
- **Per-project workspace** — each project has its own Jira site/key/token. No shared global key.
- **Add by folder** — native macOS folder picker (or paste a path on other OSes); the repos inside are auto-detected.
- **Worktree cleanup** — one click removes worktrees whose MR is already merged and whose tree is clean.
- **Fast** — per-project caching with background pre-warm; instant switching once warm.
- **Async UI** — loading spinner + skeletons, no full-page reloads.

## Requirements

- `python3` (ships with macOS)
- `git`
- Optional: [`glab`](https://gitlab.com/gitlab-org/cli) (GitLab) and/or [`gh`](https://cli.github.com/) (GitHub), authenticated, for MR/PR data
- Optional: a [Jira API token](https://id.atlassian.com/manage-profile/security/api-tokens) for Jira projects

## Install

One line — clones, sets up, adds a `dash` alias, and opens the dashboard:

```bash
curl -fsSL https://raw.githubusercontent.com/freekos/command-center/main/install.sh | bash
```

Then, from any terminal:

```bash
dash            # starts the local server (if needed) and opens it in your browser
```

Prefer to clone yourself? Same result:

```bash
git clone https://github.com/freekos/command-center.git ~/command-center
cd ~/command-center && ./install.sh
```

`dash` (and `./cc`) are idempotent — if the server is already up they just open
the dashboard. The installer only appends an alias to your shell rc if one isn't
already there; nothing else about your environment is touched.

## Updating

The install lives in `~/.command-center` as a git clone, so updating is one command:

```bash
dash update      # git pull + restart the server + reopen
```

Re-running the install one-liner does the same (it fast-forwards an existing
clone). Other commands: `dash restart`, `dash stop`, `dash status`.

## Usage

1. Open the dashboard, click **＋ Проект**.
2. **Choose a folder** (native picker on macOS) or paste a path. The dashboard
   lists the git repos it found inside.
3. Optionally tick **Jira** and enter your email + API token (the project key,
   e.g. `IK`, is optional and scopes the view; the ticket pattern is derived
   from it). Without Jira, the project shows the VCS view.
4. Switch projects from the dropdown; use **⚙** to edit a project's Jira key or
   remove it (removing only forgets it — your files are never touched).

## Configuration

State lives in `config.json` next to `server.py` (created from
`config.example.json` on first install). It is **git-ignored** because it holds
tokens.

```jsonc
{
  // optional: pre-fills the Jira site field in the "add project" form
  "default_jira_site": "https://your-org.atlassian.net",

  "projects": {
    "my-app": {
      "path": "~/code/my-app",
      "tracker": "jira",            // "jira" | "none"
      "jira": {
        "site": "https://your-org.atlassian.net",
        "project": "ABC",           // optional; derives the ticket regex (ABC-\d+)
        "email": "you@example.com",
        "token": "<jira-api-token>"
      }
    },
    "side-project": {
      "path": "~/code/side-project",
      "tracker": "none"
    }
  }
}
```

You normally never edit this by hand — the UI manages it.

## Security

- Everything binds to `127.0.0.1` only.
- Jira tokens live in `config.json` with `600` permissions and are **never**
  sent to the browser (the settings form shows a blank token field; leave it
  empty to keep the existing one).
- `config.json` and `.env` are in `.gitignore` — they will not be committed.

## Platform support

- **macOS** — native folder picker via `osascript`.
- **Linux / Windows** — paste the project path manually in the add dialog
  (everything else works the same).

## How it works

A single-file `http.server` on `127.0.0.1:8787`. Jira is read over the REST API
(basic auth, email + token); merge requests come from `glab`/`gh`; worktrees and
prune use plain `git`. Results are cached per project with a small background
pre-warm thread, so switching between warmed projects is instant.

## License

MIT — see [LICENSE](LICENSE).

---

The UI strings are currently in Russian; localization PRs are welcome.
