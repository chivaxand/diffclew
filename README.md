# DiffClew

A git client that runs in your browser: a small local Python server plus a single HTML page.
History graph, diffs, staging by hunk, branches, merges and stashes, without installing anything
beyond Python and git.

- **No dependencies.** Python standard library and plain JavaScript; no build step, no `npm install`.
- **Local only.** The server listens on `127.0.0.1` and runs the `git` you already have.
- **Review before commit.** Merge, cherry-pick and revert only prepare the change; you review it and commit yourself.

## Requirements

- Python 3.8 or newer
- git in `PATH`
- A current browser (Chrome, Edge, Firefox, Safari)

## Run

```bash
git clone https://github.com/chivaxand/diffclew.git
cd diffclew
python3 diffclew.py ~/projects/my-app ~/projects/other-app
```

Then open <http://127.0.0.1:8222/>. Each repository given on the command line opens as a tab; more can be added
with **File → Open repository** (or the **+** tab). The open tabs, layout and settings are remembered by the browser.

Options:

```text
python3 diffclew.py [REPO ...] [--port 8222]
python3 diffclew.py --test        # self-tests of the server parsers
```

The page runs its own tests on every load; the results are in the browser console.

## Features

**History**
- Commit graph with coloured lanes and branch / tag labels; the history loads more as you scroll.
- Filters: all branches, current branch, *current branch only* (`--first-parent`: its own commits and merges,
  without the commits of merged branches), remote branches on / off, date or ancestor order.
- "Jump to" a hash, branch or tag; commit details with clickable parents; full-path file list; diffs with old and new line numbers.
- Search by commit message, author, file path or changed code (`git log -S`); double click (or `Enter`) on a result
  opens it in the History.
- File history: right click a file (in a commit or in the working copy) → *Show file history* lists the commits that
  changed it, following renames; a click shows that file's diff in the commit, a double click opens the commit in the History.

**Working copy (File status)**
- Staged and unstaged lists, one above the other (drag the border between them); stage, unstage or discard a whole
  file, several files or a single hunk.
- File lists show full paths (scroll both ways) and pick several files: the checkbox on the left or `Ctrl`/`⌘` + click
  toggles, `Shift` + click picks a range, the header checkbox or `Ctrl`/`⌘` + `A` all. The `+` / `−` button on a row and
  *Stage selected* / *Unstage selected* move them; right click or `Ctrl`/`⌘` + `C` copies their paths.
- Commit box with amend and "push after commit"; `⌘/Ctrl + Enter` commits.
- Conflicts are marked; a banner offers to abort a merge, cherry-pick, revert or rebase in progress (a rebase also: Continue, Skip).

**Branches and remotes**
- Sidebar with local branches (folders by `/`, ahead / behind counts), tags, remotes, stashes, submodules.
- Checkout (double click), new branch, merge, rename, delete, tag, stash apply / pop / drop.
- **Rename branch**: only the local one, the local one and its remote branch, or only the remote one (deleting
  `main`, `master`, `develop` or the remote's default branch asks you to type *delete*).
- **Rename tag** the same way (local, remote or both); an annotated tag keeps its message, author and date.
- Checkout with uncommitted changes asks first: stash them (new files too) and switch, discard them and switch,
  switch and take them along, or cancel.
- Fetch, pull (merge or rebase), push with upstream tracking.

**Changes are never committed behind your back**
- **Merge** runs `git merge --no-commit --no-ff`: the result opens in File status with the merge message filled in;
  Commit creates the merge commit, Abort cancels it. A fast-forward is used only if you tick it in the dialog.
  **Squash** brings only the changes: they are committed as one ordinary commit, without a merge in the history.
- **Merge current branch into…** (e.g. a feature into `develop` without checking it out by hand): switches to the
  target, merges, and only if you tick it commits (when there are no conflicts) and pushes, then switches back.
  On conflicts you stay on the target to resolve them. Runs only with no uncommitted files, so nothing of yours is moved.
- **Cherry-pick** and **Revert** run with `--no-commit` the same way.
- **Undo last commit** runs `git reset --soft HEAD~1`: the commit disappears, its changes stay staged.
- **Combine commits / Reword**: pick several consecutive commits (`⌘`/`Ctrl` + click, `Shift` + click), right click →
  *Combine N commits…*, or *Reword…* on one: new commits with the same files, so the working copy and uncommitted
  changes are untouched, also on a branch that is not checked out. The dialog warns about pushed commits and other
  branches that keep the old ones; **Undo** puts the branch back.
- **Interactive rebase**: right click → *Interactive rebase from here…*: pick, squash, fixup or drop each commit, drag to
  reorder, edit messages. Conflicts stop in File status with Continue / Skip / Abort; Undo afterwards.
- **Force push**: `Ctrl + click` on Push (`⌘` or `⌃ + click` on macOS) runs `git push <remote> HEAD --force-with-lease`
  only after you type the word `force`; the server checks the word too.

Every write command is listed with its full output in **View → Command log**.

## Keyboard

| Action | macOS | Windows / Linux |
|---|---|---|
| Open repository | `⌘O` | `Ctrl+O` |
| Refresh | `⌘R` | `Ctrl+R` |
| File status / History / Search | `⌥1` `⌥2` `⌥3` | `Alt+1` `Alt+2` `Alt+3` |
| Next / previous tab | `F6` / `⇧F6` | `F6` / `Shift+F6` |
| Commit (in the message box) | `⌘↩` | `Ctrl+Enter` |
| Pick files: toggle / range / all | `⌘`-click / `⇧`-click / `⌘A` | `Ctrl`-click / `Shift`-click / `Ctrl+A` |
| Pick commits: toggle / range | `⌘`-click / `⇧`-click | `Ctrl`-click / `Shift`-click |
| Copy picked file paths | `⌘C` | `Ctrl+C` |
| Settings | `⌘,` | `Ctrl+,` |
| All shortcuts | `F1` | `F1` |

Arrow keys move through commits and files. Right click opens menus for commits, files, branches, tags and stashes.

## Security

DiffClew runs git commands, including destructive ones (`reset --hard`, `clean`, force push), in any folder
the page asks for. It is meant for your own machine only:

- The server binds to `127.0.0.1`. **Do not change `HOST` to `0.0.0.0`**: anyone on your network could then run
  git in your folders.
- Every API request must carry the `X-DiffClew` header and a localhost `Host` header. Other web sites cannot send
  that header without a CORS preflight, which the server never allows, and DNS rebinding is refused.
- git runs with an argument list (no shell); refs starting with `-` are rejected, paths always follow `--`.
- The page builds all content with `textContent`, so commit messages and branch names cannot inject HTML.
- Like any git client, opening a repository you do not trust is risky: git itself may run its hooks or
  `core.fsmonitor` command from that repository's configuration.

## How it is built

```text
diffclew.py   git backend: POST /api/<name> with JSON. Read: initial, open, ls, refs, log, search, commit,
              diff, status. Write: action {action: stage, commit, merge, push, ...} -> {ok, output, command}.
server.py     small threaded HTTP server (stdlib only): routes, JSON, static files, streaming.
index.html    the whole UI: generic widgets (UI), pure git logic (Git: graph lanes, diff parser, trees),
              views, the server API, and App - state and commands, refreshed once per frame.
```

The API and the UI are documented in the comments at the top of `diffclew.py` and `index.html`.

## Development

- `python3 diffclew.py --test` runs the server self-tests; the page runs its own tests on every load (browser console).
- CI (`.github/workflows/test.yml`) runs the self-tests and an API smoke test on every push and pull request.
- Try write actions (commit, merge, reset...) on a throwaway repository, not on real work.

## Trademarks

Git and the Git logo are trademarks of the Software Freedom Conservancy. DiffClew is an independent project and is
not affiliated with or endorsed by the Git project.
