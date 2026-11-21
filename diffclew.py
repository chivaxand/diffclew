#!/usr/bin/env python3
"""DiffClew: a git client in the browser (history graph, working copy, branches). Stdlib only, Python 3.8+, needs git in PATH.

    python3 diffclew.py [REPO ...] [--port 8222]     then open http://127.0.0.1:8222/
    python3 diffclew.py --test                       self-tests of the parsers

The repositories given on the command line open as tabs; more via File -> Open repository (kept in the browser).
The UI is index.html; this file is the git backend on server.py (a small stdlib HTTP server).

API: POST /api/<name>, JSON body {repo: path, ...} -> JSON; a failure -> 400 {error}.
  read:  initial, open, ls, refs, log, search, commit, diff, status; choose = the OS folder dialog -> {path}
  write: action {repo, action, ...} -> {ok, output, command}; ACTIONS and LOGGED below list them. A failed git command is
         ok: false with its output (not an HTTP error), so the page can show what git said.
Safety: listens on 127.0.0.1 only, because it runs git (commit, reset --hard, clean...) in any folder a request names.
Every /api request needs the X-DiffClew header (a cross-site page cannot send it without a CORS preflight, which is
never allowed) and a localhost Host header (DNS rebinding). Arguments go to git as a list (no shell); refs may not
start with '-' (option injection); paths always follow '--'.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.parse
from collections import defaultdict

sys.dont_write_bytecode = True  # no __pycache__ next to the script
from server import HttpError, Request, Response, Server

ROOT = os.path.dirname(os.path.realpath(__file__))
HOST = '127.0.0.1'
PORT = 8222
LOG_MAX = 2000                # commits per log request
SEARCH_MAX = 500              # commits per search
LIST_MAX = 5000               # files per status list / commit
DIFF_MAX = 2 * 1024 * 1024    # bytes of diff text; the rest is cut (truncated: true)
FS, RS = '\x1f', '\x1e'       # field / record separators in git --format output
CONFLICTS = {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'}

# LC_ALL=C: English "ahead 1, behind 2" from for-each-ref. No prompts: a command that needs a password or an editor
# fails instead of hanging the request. GIT_OPTIONAL_LOCKS=0: status does not take index.lock while the user works.
ENV = {**os.environ, 'LC_ALL': 'C', 'GIT_TERMINAL_PROMPT': '0', 'GIT_EDITOR': 'true', 'GIT_MERGE_AUTOEDIT': 'no',
       'GIT_OPTIONAL_LOCKS': '0'}
ENV.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')


class ApiError(Exception):
    """Expected failure with a message for the user (bad input, not a repository, git refused)."""


# **************************************** Running git ****************************************

def command_text(args):
    return ' '.join(shlex.quote(a) for a in ['git', *args])


def run_git(repo, args, input=None, timeout=60, env=None):
    """-> (exit code, stdout bytes, stderr bytes). Never raises for a non-zero exit. env: variables added to ENV."""
    cmd = ['git', '-c', 'core.quotepath=off', '-c', 'color.ui=false', '-c', 'log.showSignature=false', *args]
    try:
        p = subprocess.run(cmd, cwd=repo, env={**ENV, **env} if env else ENV, capture_output=True, timeout=timeout,
                           **({'input': input.encode('utf-8')} if input is not None else {'stdin': subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        raise ApiError(f'{command_text(args)}: timed out after {timeout} s')
    except FileNotFoundError:
        raise ApiError('git is not installed or not in PATH')
    return p.returncode, p.stdout, p.stderr


def git(repo, *args, ok=(0,), timeout=60, raw=False):
    """stdout of a git command (str, or bytes with raw=True); an exit code outside `ok` -> ApiError with stderr."""
    code, out, err = run_git(repo, args, timeout=timeout)
    if code not in ok:
        raise ApiError((err or out).decode('utf-8', 'replace').strip() or f'{command_text(args)} failed ({code})')
    return out if raw else out.decode('utf-8', 'replace')


_toplevels = {}
_locks = defaultdict(threading.Lock)  # one write action at a time per repository


def repo_of(body):
    """Top level folder of the repository named by body['repo'] (cached)."""
    path = body.get('repo')
    if not isinstance(path, str) or not path:
        raise ApiError('No repository')
    if path not in _toplevels:
        if not os.path.isdir(path):
            raise ApiError(f'Folder not found: {path}')
        _toplevels[path] = git(path, 'rev-parse', '--show-toplevel').strip()
    return _toplevels[path]


def has_head(repo):
    return git(repo, 'rev-parse', '-q', '--verify', 'HEAD', ok=(0, 1)).strip() != ''


def git_dir(repo):
    return git(repo, 'rev-parse', '--absolute-git-dir').strip()


def repo_state(repo):
    """'merge' | 'rebase' | 'cherry-pick' | 'revert' | 'squash' | '' - an operation waiting for the user.
    'squash' = `merge --squash` result not committed yet: git leaves no MERGE_HEAD, only SQUASH_MSG (commit and
    `reset --merge` delete it). A SQUASH_MSG without any tracked change is stale (the user threw the changes away):
    it is deleted, or git commit and the commit box would pick it up for unrelated changes later."""
    d = git_dir(repo)
    if os.path.exists(os.path.join(d, 'rebase-merge')) or os.path.exists(os.path.join(d, 'rebase-apply')):
        return 'rebase'
    for name, state in (('MERGE_HEAD', 'merge'), ('CHERRY_PICK_HEAD', 'cherry-pick'), ('REVERT_HEAD', 'revert')):
        if os.path.exists(os.path.join(d, name)):
            return state
    squash = os.path.join(d, 'SQUASH_MSG')
    if os.path.exists(squash):
        if git(repo, 'status', '--porcelain', '--untracked-files=no').strip():
            return 'squash'
        try:
            os.remove(squash)
        except OSError:
            pass
    return ''


def merge_heads(repo):
    """Commits being merged while a merge waits for the user (MERGE_HEAD, one line per head; several for an octopus)."""
    try:
        with open(os.path.join(git_dir(repo), 'MERGE_HEAD')) as f:
            return [h for h in f.read().split() if re.fullmatch(r'[0-9a-f]{40,64}', h)]
    except OSError:
        return []


_ssh_hosts = {}


def ssh_host(alias):
    """Real host name of an ssh host alias (~/.ssh/config Host ... HostName ...) by `ssh -G`, which only prints the
    resolved config and connects nowhere. Cached; the alias itself when ssh is missing or fails."""
    if alias not in _ssh_hosts:
        host = alias
        if not alias.startswith('-'):
            try:
                out = subprocess.run(['ssh', '-G', alias], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
                host = next((l.split(None, 1)[1].strip() for l in out.stdout.decode('utf-8', 'replace').splitlines()
                             if l.startswith('hostname ')), alias) or alias
            except (OSError, subprocess.SubprocessError):
                pass
        _ssh_hosts[alias] = host
    return _ssh_hosts[alias]


def web_url(url, resolve=lambda host: host):
    """Remote URL -> web page of the repository ('' for a local path or an unknown scheme). Login and token are
    dropped (never sent to the page); ssh host aliases go through resolve(); ssh ports are dropped."""
    url = url.strip()
    if re.match(r'^[A-Za-z]:[\\/]', url):                      # Windows drive path
        return ''
    m = re.match(r'^(?P<scheme>[A-Za-z][\w+.-]*)://(?:[^/@]*@)?(?P<host>[^/:]+)(?::(?P<port>\d+))?(?P<path>/.*)?$', url)
    if m:
        scheme, host, path = m['scheme'].lower(), m['host'], m['path'] or ''
        if scheme in ('http', 'https'):
            base = f'{scheme}://{host}' + (f':{m["port"]}' if m['port'] else '')
        elif scheme in ('ssh', 'git+ssh', 'ssh+git', 'git'):
            base = 'https://' + (host if scheme == 'git' else resolve(host))
        else:
            return ''
    else:
        m = re.match(r'^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>.+)$', url)  # scp-like: [user@]host:path
        if not m or '://' in url:
            return ''
        host = resolve(m['host'])
        base, path = 'https://' + host, '/' + m['path'].lstrip('/')
    path = re.sub(r'\.git$', '', path.rstrip('/')).rstrip('/')
    if not path.strip('/'):
        return ''
    parts = path.split('/')
    if base == 'https://ssh.dev.azure.com' and len(parts) == 5 and parts[1] == 'v3':   # /v3/org/project/repo
        return f'https://dev.azure.com/{parts[2]}/{parts[3]}/_git/{parts[4]}'
    return base + path


def remote_web_urls(repo):
    """{remote: [web page, ...]} from `git remote -v` (insteadOf applied; every fetch and push URL), without duplicates."""
    pages = {}
    for line in git(repo, 'remote', '-v', ok=(0, 1)).splitlines():
        f = line.split('\t', 1)
        if len(f) < 2:
            continue
        name, url = f[0], f[1].rsplit(' (', 1)[0]
        page = web_url(url, ssh_host)
        urls = pages.setdefault(name, [])
        if page and page not in urls:
            urls.append(page)
    return pages


def user_path(text):
    """A folder path as people paste it: quoted ("Copy as path" on Windows), a file:// URL (Linux file managers),
    backslash-escaped spaces (macOS Finder -> Terminal), ~. '' stays '' (the caller's default)."""
    path = str(text or '').strip()
    if len(path) > 1 and path[0] == path[-1] and path[0] in '"\'':
        path = path[1:-1].strip()
    if path.lower().startswith('file://'):
        path = urllib.parse.unquote(urllib.parse.urlparse(path).path)
        if re.match(r'^/[A-Za-z]:', path):                        # file:///C:/x
            path = path[1:]
    elif os.sep == '/' and '\\' in path and not os.path.exists(os.path.expanduser(path)):
        path = re.sub(r'\\(.)', r'\1', path)
    return os.path.expanduser(path)


# **************************************** Arguments ****************************************

def int_arg(body, name, default, low, high):
    try:
        return max(low, min(high, int(body.get(name, default))))
    except (TypeError, ValueError):
        return default


def ref_arg(value, name='ref'):
    """Branch, tag, hash or rev expression. Never an option (leading '-'), no whitespace."""
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f'No {name}')
    value = value.strip()
    if value.startswith('-') or len(value) > 300 or re.search(r'[\s\x00-\x1f]', value):
        raise ApiError(f'Bad {name}: {value}')
    return value


def branch_name_arg(repo, value):
    name = ref_arg(value, 'branch name')
    if run_git(repo, ['check-ref-format', '--branch', name])[0] != 0:
        raise ApiError(f'Invalid branch name: {name}')
    return name


def tag_name_arg(repo, value):
    name = ref_arg(value, 'tag name')
    if run_git(repo, ['check-ref-format', 'refs/tags/' + name])[0] != 0:
        raise ApiError(f'Invalid tag name: {name}')
    return name


def remote_arg(repo, value):
    remote = ref_arg(value, 'remote')
    if remote not in git(repo, 'remote').split():
        raise ApiError(f'Unknown remote: {remote}')
    return remote


def stash_arg(value):
    if not isinstance(value, str) or not re.fullmatch(r'stash@\{\d+\}', value):
        raise ApiError(f'Bad stash: {value}')
    return value


def paths_arg(value, name='paths'):
    if not isinstance(value, list) or not all(isinstance(p, str) and p and '\0' not in p for p in value):
        raise ApiError(f'Bad {name}')
    return value


def text_arg(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f'{name} is empty')
    return value


# **************************************** Parsers (tested by --test) ****************************************

LOG_FORMAT = RS + FS.join(['%H', '%P', '%an', '%ae', '%at', '%s'])


def parse_log(out):
    """LOG_FORMAT records. With --name-only (file history) the file's path in that commit follows the subject
    after a blank line: it goes to 'path' (it differs from today's path before a rename)."""
    commits = []
    for record in out.split(RS)[1:]:
        f = record.rstrip('\n').split(FS)
        if len(f) >= 6:
            subject, _, names = f[5].partition('\n')
            commit = {'hash': f[0], 'parents': f[1].split(), 'author': f[2], 'email': f[3], 'date': int(f[4] or 0), 'subject': subject}
            paths = [line for line in names.splitlines() if line.strip()]
            if paths:
                commit['path'] = paths[-1]
            commits.append(commit)
    return commits


def parse_name_status(out):
    """`diff --name-status -z` -> [{status, path, oldPath?}] sorted by path."""
    parts, files, i = out.split('\0'), [], 0
    while i < len(parts) and parts[i]:
        code = parts[i][0]
        if code in 'RC' and i + 2 < len(parts):
            files.append({'status': code, 'oldPath': parts[i + 1], 'path': parts[i + 2]})
            i += 3
        elif i + 1 < len(parts):
            files.append({'status': code, 'path': parts[i + 1]})
            i += 2
        else:
            break
    return sorted(files, key=lambda f: f['path'])


def parse_status(out):
    """`status --porcelain=v1 -z` -> (staged, unstaged). Untracked: status '?', conflicts: status 'C' in unstaged."""
    parts, staged, unstaged, i = out.split('\0'), [], [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path, old = entry[:2], entry[3:], None
        if 'R' in xy or 'C' in xy:  # the source path follows as its own field
            old = parts[i] if i < len(parts) else None
            i += 1
        if xy == '??':
            unstaged.append({'status': '?', 'path': path})
        elif xy == '!!':
            continue
        elif xy in CONFLICTS:
            unstaged.append({'status': 'C', 'path': path, 'conflict': xy})
        else:
            if xy[0] != ' ':
                staged.append({'status': xy[0], 'path': path, **({'oldPath': old} if old and xy[0] in 'RC' else {})})
            if xy[1] != ' ':
                unstaged.append({'status': xy[1], 'path': path, **({'oldPath': old} if old and xy[1] in 'RC' else {})})
    key = lambda f: f['path']
    return sorted(staged, key=key), sorted(unstaged, key=key)


def parse_track(track):
    """'ahead 1, behind 2' -> (1, 2)"""
    ahead, behind = re.search(r'ahead (\d+)', track), re.search(r'behind (\d+)', track)
    return int(ahead.group(1)) if ahead else 0, int(behind.group(1)) if behind else 0


# **************************************** Read endpoints ****************************************

def api_initial(body):
    repos = []
    for path in ARGS.repos:
        try:
            repos.append(api_open({'path': path}))
        except ApiError as e:
            print('Skipped:', e)
    return {'repos': repos, 'home': os.path.expanduser('~')}


def api_open(body):
    path = os.path.realpath(user_path(body.get('path')) or '.')
    if os.path.isfile(path):          # a file of the repository was pasted
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        raise ApiError(f'Folder not found: {path}')
    if run_git(path, ['rev-parse', '--show-toplevel'])[0] != 0:
        raise ApiError(f'Not a git repository: {path}')
    top = git(path, 'rev-parse', '--show-toplevel').strip()
    return {'path': top, 'name': os.path.basename(top) or top}


def api_ls(body):
    """Sub-folders of a folder for the Open dialog; repo: true for git repositories."""
    path = os.path.realpath(user_path(body.get('path')) or os.path.expanduser('~'))
    if not os.path.isdir(path):
        raise ApiError(f'Folder not found: {path}')
    dirs = []
    try:
        with os.scandir(path) as entries:
            for e in entries:
                if not e.name.startswith('.') and e.is_dir():
                    dirs.append({'name': e.name, 'path': e.path, 'repo': os.path.exists(os.path.join(e.path, '.git'))})
    except OSError as e:
        raise ApiError(f'Cannot read {path}: {e.strerror}')
    dirs.sort(key=lambda d: d['name'].lower())
    return {'path': path, 'parent': os.path.dirname(path), 'repo': os.path.exists(os.path.join(path, '.git')),
            'dirs': dirs[:1000]}


def api_refs(body):
    repo = repo_of(body)
    head_hash = git(repo, 'rev-parse', '-q', '--verify', 'HEAD', ok=(0, 1)).strip()
    branch = git(repo, 'symbolic-ref', '-q', '--short', 'HEAD', ok=(0, 1)).strip()
    fmt = FS.join(['%(refname)', '%(objectname)', '%(*objectname)', '%(upstream:short)',
                   '%(upstream:track,nobracket)', '%(HEAD)', '%(symref)'])
    branches, remotes, tags, remote_heads = [], [], [], {}
    for line in git(repo, 'for-each-ref', '--format=' + fmt, 'refs/heads', 'refs/remotes', 'refs/tags').splitlines():
        f = line.split(FS)
        if len(f) < 7:
            continue
        ref, commit = f[0], f[2] or f[1]  # annotated tags: the commit, not the tag object
        if ref.startswith('refs/heads/'):
            ahead, behind = parse_track(f[4])
            branches.append({'name': ref[11:], 'hash': commit, 'upstream': f[3], 'ahead': ahead, 'behind': behind,
                             'gone': f[4] == 'gone', 'current': f[5] == '*'})
        elif ref.startswith('refs/remotes/'):
            if not ref.endswith('/HEAD'):
                remotes.append({'name': ref[13:], 'hash': commit})
            elif f[6].startswith(ref[:-4]):         # refs/remotes/<remote>/HEAD -> the remote's default branch
                remote_heads[ref[13:-5]] = f[6][len(ref) - 4:]
        elif ref.startswith('refs/tags/'):
            tags.append({'name': ref[10:], 'hash': commit})
    stashes = []
    if head_hash:
        for line in git(repo, 'stash', 'list', f'--format=%gd{FS}%H{FS}%gs', ok=(0, 1)).splitlines():
            f = line.split(FS)
            if len(f) == 3:
                stashes.append({'ref': f[0], 'hash': f[1], 'message': f[2]})
    submodules = []
    if os.path.isfile(os.path.join(repo, '.gitmodules')):
        out = git(repo, 'config', '-f', '.gitmodules', '--get-regexp', r'^submodule\..*\.path$', ok=(0, 1))
        for line in out.splitlines():
            sub = line.split(' ', 1)[-1]
            submodules.append({'path': sub, 'abs': os.path.join(repo, sub)})
    return {'head': {'branch': branch, 'hash': head_hash, 'detached': bool(head_hash and not branch)},
            'branches': branches, 'remotes': remotes, 'tags': tags, 'stashes': stashes, 'submodules': submodules,
            'remoteNames': git(repo, 'remote').split(), 'state': repo_state(repo), 'mergeHeads': merge_heads(repo),
            'remoteHeads': remote_heads,
            'webUrls': remote_web_urls(repo)}


def log_revs(repo, scope, remotes):
    """scope: 'all' branches and tags, 'current' = HEAD with everything merged into it,
    'first' = HEAD's own line only (--first-parent: its commits and merge commits, not the merged branches' commits).
    A merge in progress adds the commits being merged (not to 'first'): the "Uncommitted changes" row joins them."""
    head = has_head(repo)
    if not head:
        return [] if scope in ('current', 'first') else ['--branches', '--tags'] + (['--remotes'] if remotes else [])
    if scope == 'first':
        return ['--first-parent', 'HEAD']
    merging = merge_heads(repo)
    if scope == 'current':
        return ['HEAD', *merging]
    return ['--branches', '--tags'] + (['--remotes'] if remotes else []) + ['HEAD', *merging]


def api_log(body):
    repo = repo_of(body)
    skip, limit = int_arg(body, 'skip', 0, 0, 10 ** 8), int_arg(body, 'limit', 500, 1, LOG_MAX)
    revs = log_revs(repo, body.get('scope'), bool(body.get('remotes')))
    if not revs:
        return {'commits': [], 'more': False}
    order = '--topo-order' if body.get('order') == 'topo' else '--date-order'
    out = git(repo, 'log', order, f'--skip={skip}', f'--max-count={limit + 1}', '--format=' + LOG_FORMAT, *revs, '--',
              ok=(0, 128))  # 128: no branches yet
    commits = parse_log(out)
    return {'commits': commits[:limit], 'more': len(commits) > limit}


def api_search(body):
    repo = repo_of(body)
    text, mode = str(body.get('text') or '').strip(), body.get('mode')
    if not text:
        return {'commits': []}
    if '\0' in text:
        raise ApiError('Bad search text')
    paths = []
    if mode == 'author':
        args = ['--author=' + text, '-i', '-F']
    elif mode == 'code':
        args = ['-S' + text]
    elif mode == 'file':
        args, paths = [], [':(glob,icase)**/*' + text + '*']
    elif mode == 'history':  # one file's history, exact path from the repository root, across renames
        args, paths = ['--follow', '--name-only'], [':(literal)' + text]
    else:
        args = ['--grep=' + text, '-i', '-F']
    revs = log_revs(repo, 'all', True)
    if not revs:
        return {'commits': []}
    out = git(repo, 'log', '--date-order', f'--max-count={SEARCH_MAX}', '--format=' + LOG_FORMAT, *args, *revs,
              '--', *paths, timeout=180)
    return {'commits': parse_log(out)}


def api_commit(body):
    repo = repo_of(body)
    rev = ref_arg(body.get('hash'), 'commit')
    fmt = FS.join(['%H', '%P', '%an', '%ae', '%at', '%cn', '%ce', '%ct', '%B'])
    f = git(repo, 'show', '-s', '--format=' + fmt, rev + '^{commit}', '--').split(FS)
    if len(f) < 9:
        raise ApiError(f'Not a commit: {rev}')
    commit_hash, parents = f[0], f[1].split()
    if parents:
        out = git(repo, 'diff', '--name-status', '-z', '-M', parents[0], commit_hash, '--')
    else:
        out = git(repo, 'diff-tree', '--root', '--no-commit-id', '-r', '--name-status', '-z', '-M', commit_hash, '--')
    files = parse_name_status(out)
    return {'hash': commit_hash, 'parents': parents, 'author': f[2], 'email': f[3], 'date': int(f[4] or 0),
            'committer': f[5], 'committerEmail': f[6], 'committerDate': int(f[7] or 0), 'message': f[8].strip(),
            'files': files[:LIST_MAX], 'more': len(files) > LIST_MAX}


def api_diff(body):
    """mode: commit (hash, parent: '' for a root commit) | staged | unstaged | untracked."""
    repo = repo_of(body)
    path = paths_arg([body.get('path')], 'path')[0]
    old = body.get('oldPath')
    paths = [path] + ([old] if isinstance(old, str) and old and old != path and '\0' not in old else [])
    opts = ['--no-color', '--no-ext-diff', '-M', f"-U{int_arg(body, 'context', 3, 0, 1000)}"]
    if body.get('ignoreWs'):
        opts.append('-w')
    mode, ok = body.get('mode'), (0,)
    if mode == 'staged':
        args = ['diff', '--cached', *opts, '--', *paths]
    elif mode == 'unstaged':
        args = ['diff', *opts, '--', *paths]
    elif mode == 'untracked':
        args, ok = ['diff', '--no-index', *opts, '--', os.devnull, path], (0, 1)
    elif mode == 'commit':
        rev, parent = ref_arg(body.get('hash'), 'commit'), body.get('parent')
        if parent:
            args = ['diff', *opts, ref_arg(parent, 'parent'), rev, '--', *paths]
        else:
            args = ['show', '--format=', *opts, rev, '--', *paths]
    else:
        raise ApiError(f'Bad diff mode: {mode}')
    out = git(repo, *args, ok=ok, raw=True)
    return {'text': out[:DIFF_MAX].decode('utf-8', 'replace'), 'truncated': len(out) > DIFF_MAX}


def api_status(body):
    repo = repo_of(body)
    staged, unstaged = parse_status(git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
    state, message = repo_state(repo), ''
    # MERGE_MSG: the message of a merge, revert or cherry-pick prepared without a commit. Read it whenever it exists:
    # cherry-pick --no-commit leaves no CHERRY_PICK_HEAD; git deletes the file on commit / abort.
    # A squash merge has SQUASH_MSG (MERGE_MSG only on conflicts, and then only '# Conflicts' comments).
    for name in ('MERGE_MSG',) + (('SQUASH_MSG',) if state == 'squash' else ()):
        try:
            with open(os.path.join(git_dir(repo), name), encoding='utf-8', errors='replace') as f:
                message = '\n'.join(l for l in f.read().splitlines() if not l.startswith('#')).strip()
        except OSError:
            pass
        if message:
            break
    return {'staged': staged[:LIST_MAX], 'unstaged': unstaged[:LIST_MAX],
            'more': len(staged) > LIST_MAX or len(unstaged) > LIST_MAX, 'state': state, 'message': message}


# **************************************** Write actions ****************************************
# Each builder validates the body and returns the git commands to run in order: [(args, stdin text or None)].

def act_unstage(repo, b):
    paths = paths_arg(b.get('paths'))
    if has_head(repo):
        return [(['restore', '--staged', '--', *paths], None)]
    return [(['rm', '--cached', '-r', '-q', '--', *paths], None)]  # no commits yet: nothing to restore from


def act_discard(repo, b):
    """Working copy changes of tracked files (the index stays), and untracked files are deleted."""
    tracked, untracked = paths_arg(b.get('paths') or [], 'paths'), paths_arg(b.get('untracked') or [], 'untracked')
    steps = [(['restore', '--worktree', '--', *tracked], None)] if tracked else []
    return steps + ([(['clean', '-f', '-q', '--', *untracked], None)] if untracked else [])


def act_commit(repo, b):
    return [(['commit', '-F', '-'] + (['--amend'] if b.get('amend') else []), text_arg(b.get('message'), 'Commit message'))]


def act_fetch(repo, b):
    target = [remote_arg(repo, b['remote'])] if b.get('remote') else ['--all']
    return [(['fetch', *target] + (['--prune'] if b.get('prune') else []) + (['--tags'] if b.get('tags') else []), None)]


def act_pull(repo, b):
    args = ['pull', '--no-edit', '--rebase' if b.get('rebase') else '--no-rebase']
    if b.get('remote'):
        args += [remote_arg(repo, b['remote']), ref_arg(b.get('branch'), 'branch')]
    return [(args, None)]


def act_push(repo, b):
    args = ['push'] + (['-u'] if b.get('upstream') else []) + (['--force-with-lease'] if b.get('force') else [])
    args += (['--tags'] if b.get('tags') else [])
    return [(args + [remote_arg(repo, b.get('remote')), ref_arg(b.get('branch'), 'branch')], None)]


def act_branch(repo, b):
    name, start = branch_name_arg(repo, b.get('name')), ref_arg(b.get('start') or 'HEAD', 'start point')
    return [(['switch', '-c', name, start] if b.get('checkout') else ['branch', name, start], None)]


def act_checkout(repo, b):
    """mode: branch (default) | track (new local branch from a remote one) | detach.
    localChanges - what happens to uncommitted changes first: 'keep' (default: git carries them over, or refuses),
    'stash' (stash push --include-untracked, so new files go too), 'discard' (reset --hard + clean -fd: tracked changes
    and untracked files are gone; ignored files stay). The steps stop at the first failure, so a failed switch after
    a stash leaves the changes in the stash."""
    ref, mode, local_changes = ref_arg(b.get('ref')), b.get('mode'), b.get('localChanges') or 'keep'
    if mode == 'track':
        switch = ['switch', '--track', '-c', branch_name_arg(repo, b.get('local')), ref]
    elif mode == 'detach':
        switch = ['switch', '--detach', ref]
    else:
        switch = ['switch', ref]
    if local_changes == 'stash':
        before = [['stash', 'push', '--include-untracked', '-m', f'Before checkout of {ref}']]
    elif local_changes == 'discard':
        before = [['reset', '--hard', '-q', 'HEAD'], ['clean', '-fd', '-q']]
    elif local_changes == 'keep':
        before = []
    else:
        raise ApiError(f'Bad localChanges: {local_changes}')
    return [(args, None) for args in before + [switch]]


SQUASH_LIST = 50              # commit subjects listed in a squash merge message
PROTECTED_BRANCHES = {'main', 'master', 'develop', 'development', 'dev', 'trunk', 'release', 'staging', 'production', 'prod'}


def squash_message(repo, ref, target=''):
    """"Squash merge branch 'x' [into y]" + the subjects of the commits it brings (oldest first). git's own SQUASH_MSG
    is a full `git log` of them (hashes, authors, dates), too long for a commit message."""
    what = f"commit '{ref[:12] if re.fullmatch(r'[0-9a-f]{40,64}', ref) else ref}'"
    for prefix, kind in (('refs/heads/', 'branch'), ('refs/remotes/', 'remote-tracking branch'), ('refs/tags/', 'tag')):
        if run_git(repo, ['show-ref', '--verify', '-q', prefix + ref])[0] == 0:
            what = f"{kind} '{ref}'"
            break
    count = int(git(repo, 'rev-list', '--count', 'HEAD..' + ref, ok=(0, 128)).strip() or 0)
    subjects = git(repo, 'log', '--format=%s', f'--max-count={SQUASH_LIST}', 'HEAD..' + ref, ok=(0, 128)).splitlines()[::-1]
    lines = ([f'- ...and {count - len(subjects)} earlier commits'] if count > len(subjects) else []) + [f'- {s}' for s in subjects]
    return f'Squash merge {what}' + (f' into {target}' if target else '') + ('\n\n' + '\n'.join(lines) if lines else '') + '\n'


def write_squash_message(repo, message):
    """Replaces the SQUASH_MSG git wrote after `merge --squash` (none: nothing to commit, nothing written)."""
    path = os.path.join(git_dir(repo), 'SQUASH_MSG')
    if os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(message)


def merge(repo, b, run):
    """Prepares a merge without committing it: the user reviews the result in File status and commits it, or aborts.
    Default: a merge commit (--no-ff --no-commit); ff: true allows a fast-forward when one is possible (that moves the
    branch at once). squash: true = `merge --squash`: only the changes, committed later as an ordinary one-parent
    commit (state 'squash'; the merged branch does not count as merged for git)."""
    ref = ref_arg(b.get('ref'))
    if repo_state(repo):
        raise ApiError(f'A {repo_state(repo)} is in progress: finish or abort it first')
    if not b.get('squash'):
        run(['merge', '--no-edit', '--no-commit', '--ff' if b.get('ff') else '--no-ff', ref])
        return
    message = squash_message(repo, ref)
    run(['merge', '--squash', ref])
    write_squash_message(repo, message)


def merge_to(repo, b, run):
    """Merges the current branch into another local branch `target` without the user switching by hand: switch to it,
    merge; with commit: true and no conflicts the merge is committed, push: true pushes target to its upstream, then
    the source branch is checked out again. Conflicts (or commit: false) stay on target with the merge waiting in
    File status. squash: true = `merge --squash` + an ordinary commit (no merge commit). Runs only when the working
    copy is clean (untracked files too): nothing of the user's is moved."""
    source = git(repo, 'symbolic-ref', '-q', '--short', 'HEAD', ok=(0, 1)).strip()
    target, commit, push = ref_arg(b.get('target'), 'target branch'), bool(b.get('commit')), bool(b.get('push'))
    if not source:
        raise ApiError('HEAD is detached: check out the branch to merge first')
    if target == source or run_git(repo, ['show-ref', '--verify', '-q', 'refs/heads/' + target])[0] != 0:
        raise ApiError(f'Not another local branch: {target}')
    if repo_state(repo):
        raise ApiError(f'A {repo_state(repo)} is in progress')
    if git(repo, 'status', '--porcelain', '--untracked-files=all').strip():
        raise ApiError('There are uncommitted changes: commit, stash or discard them first')
    remote = git(repo, 'config', f'branch.{target}.remote', ok=(0, 1)).strip()
    upstream = git(repo, 'config', f'branch.{target}.merge', ok=(0, 1)).strip()
    if push and (not commit or remote in ('', '.') or not upstream):
        raise ApiError(f'Push needs the commit option and an upstream branch of {target}')
    if run(['switch', target], 120)[0]:
        return
    if b.get('squash'):
        message = squash_message(repo, source, target)
        code = run(['merge', '--squash', source], 120)[0]
        write_squash_message(repo, message)
        if code == 0 and commit and repo_state(repo) == 'squash':
            code = run(['commit', '-F', '-'], 120, input=message)[0]
    else:
        # explicit message: git's own drops "into <target>" for main / master (merge.suppressDest)
        merge = ['merge', '--no-edit', '--ff' if b.get('ff') else '--no-ff', '-m', f"Merge branch '{source}' into {target}"]
        code = run(merge + ([] if commit else ['--no-commit']) + [source], 120)[0]
    if repo_state(repo):
        return                  # conflicts, or commit: false - the merge waits on target for the user
    if code == 0 and push:
        run(['push', remote_arg(repo, remote), f'refs/heads/{target}:{upstream}'], 600)
    run(['switch', source], 120)


def remote_default(repo, remote):
    """The remote's default branch as last fetched / cloned (refs/remotes/<remote>/HEAD), or ''."""
    out = git(repo, 'symbolic-ref', '-q', f'refs/remotes/{remote}/HEAD', ok=(0, 1)).strip()
    return out[len(f'refs/remotes/{remote}/'):] if out else ''


def rename_branch(repo, b, run):
    """mode 'local': git branch -m name newName (the upstream stays).
    mode 'remote': the branch `branch` on `remote` gets the name newName: fetch it, push its commit as newName
    (lease: newName must not exist there), delete the old one (lease: it must still be that commit), then local
    branches that tracked it track the new one. Nothing local is pushed: only the remote's own commit moves.
    mode 'both': the local branch `name` and its upstream branch (mode 'remote' first, then 'local').
    Deleting a protected remote branch (PROTECTED_BRANCHES or the remote's default) needs confirm: 'delete'.
    A step that fails stops the rest: a failed delete leaves both names on the remote, the local branch untouched."""
    mode, new = b.get('mode'), branch_name_arg(repo, b.get('newName'))
    if mode not in ('local', 'remote', 'both'):
        raise ApiError(f'Bad rename mode: {mode}')
    has = lambda ref: run_git(repo, ['show-ref', '--verify', '-q', ref])[0] == 0
    old = remote = theirs = ''
    if mode in ('local', 'both'):
        old = ref_arg(b.get('name'), 'branch')
        if not has('refs/heads/' + old):
            raise ApiError(f'No local branch {old}')
        if new != old and has('refs/heads/' + new):
            raise ApiError(f'A local branch {new} already exists')
    if mode == 'both':
        remote = git(repo, 'config', f'branch.{old}.remote', ok=(0, 1)).strip()
        merge_ref = git(repo, 'config', f'branch.{old}.merge', ok=(0, 1)).strip()
        if remote in ('', '.') or not merge_ref.startswith('refs/heads/'):
            raise ApiError(f'{old} has no upstream branch on a remote')
        remote, theirs = remote_arg(repo, remote), merge_ref[len('refs/heads/'):]
    elif mode == 'remote':
        remote, theirs = remote_arg(repo, b.get('remote')), ref_arg(b.get('branch'), 'branch')
    if {'local': new == old, 'remote': new == theirs, 'both': new == old == theirs}[mode]:
        raise ApiError(f'The branch is already called {new}')
    if mode != 'local' and theirs != new:
        if (theirs in PROTECTED_BRANCHES or theirs == remote_default(repo, remote)) and b.get('confirm') != 'delete':
            raise ApiError(f'Deleting {remote}/{theirs} needs the confirmation word "delete"')
        tracking = f'refs/remotes/{remote}/{theirs}'
        followers = [line.split(FS)[0] for line in git(repo, 'for-each-ref', f'--format=%(refname:short){FS}%(upstream)',
                                                         'refs/heads').splitlines() if line.split(FS)[-1] == tracking]
        # the lease alone lets it through when the new name already holds the same commit ("up-to-date")
        if git(repo, 'ls-remote', '--heads', remote, 'refs/heads/' + new, timeout=600).strip():
            raise ApiError(f'{remote} already has a branch {new}')
        if run(['fetch', remote, f'+refs/heads/{theirs}:{tracking}'], 600)[0]:
            return
        commit = git(repo, 'rev-parse', '--verify', tracking).strip()
        if run(['push', f'--force-with-lease=refs/heads/{new}:', remote, f'{commit}:refs/heads/{new}'], 600)[0]:
            return
        if run(['push', f'--force-with-lease=refs/heads/{theirs}:{commit}', remote, f':refs/heads/{theirs}'], 600)[0]:
            return
        for name in followers:                  # 'both': the branch itself too; branch -m below takes its config along
            run(['branch', f'--set-upstream-to={remote}/{new}', name])
    if mode != 'remote' and new != old:
        run(['branch', '-m', old, new])


def renamed_tag(repo, obj, new, run):
    """The object a tag named `new` points to: a commit (lightweight tag) as is; an annotated tag object is written
    again by `git mktag` with the line `tag <new>` (message, tagger and date kept; a signature is dropped: it signed
    the old name). A plain `git tag new old` would point to the old object, which still says `tag old` inside."""
    if git(repo, 'cat-file', '-t', obj).strip() != 'tag':
        return obj
    head, _, message = git(repo, 'cat-file', 'tag', obj).partition('\n\n')
    lines, signature = [], False
    for line in head.split('\n'):
        signature = line.startswith('gpgsig') or (signature and line.startswith(' '))
        if not signature:
            lines.append('tag ' + new if line.startswith('tag ') else line)
    message = re.split(r'^-----BEGIN (?:PGP SIGNATURE|SSH SIGNATURE|SIGNED MESSAGE)-----', message, maxsplit=1, flags=re.M)[0]
    code, out = run(['mktag'], input='\n'.join(lines) + '\n\n' + message)
    return out if code == 0 and re.fullmatch(r'[0-9a-f]{40,64}', out) else ''


def rename_tag(repo, b, run):
    """mode 'local': refs/tags/<name> -> refs/tags/<newName> (renamed_tag object; created only if the new name is
    free, the old one deleted only if unchanged). mode 'remote': the tag on `remote` (fetched first, so it is the
    remote's own object): push the renamed object (lease: the new name must not exist there), delete the old name
    (lease: still that object); local tags stay. mode 'both': 'remote' first, then 'local'; the local tag must be the
    same object as the remote one. A failed step stops the rest."""
    mode, old, new = b.get('mode'), ref_arg(b.get('name'), 'tag'), tag_name_arg(repo, b.get('newName'))
    if mode not in ('local', 'remote', 'both'):
        raise ApiError(f'Bad rename mode: {mode}')
    if new == old:
        raise ApiError(f'The tag is already called {new}')
    mine = ''
    if mode != 'remote':
        mine = git(repo, 'rev-parse', '-q', '--verify', 'refs/tags/' + old, ok=(0, 1)).strip()
        if not mine:
            raise ApiError(f'No local tag {old}')
        if run_git(repo, ['show-ref', '--verify', '-q', 'refs/tags/' + new])[0] == 0:
            raise ApiError(f'A local tag {new} already exists')
    if mode != 'local':
        remote = remote_arg(repo, b.get('remote'))
        # the lease alone lets it through when the new name already holds the same object ("up-to-date")
        if git(repo, 'ls-remote', '--tags', remote, 'refs/tags/' + new, timeout=600).strip():
            raise ApiError(f'{remote} already has a tag {new}')
        if run(['fetch', '--no-tags', remote, 'refs/tags/' + old], 600)[0]:
            return
        theirs = git(repo, 'rev-parse', 'FETCH_HEAD').strip()
        if mode == 'both' and theirs != mine:
            raise ApiError(f'Tag {old} on {remote} is another object than the local one: rename them one by one')
        renamed = renamed_tag(repo, theirs, new, run)
        if not renamed:
            return
        if run(['push', f'--force-with-lease=refs/tags/{new}:', remote, f'{renamed}:refs/tags/{new}'], 600)[0]:
            return
        if run(['push', f'--force-with-lease=refs/tags/{old}:{theirs}', remote, f':refs/tags/{old}'], 600)[0]:
            return
    if mode != 'remote':
        renamed = renamed_tag(repo, mine, new, run)
        if renamed and run(['update-ref', 'refs/tags/' + new, renamed, ''])[0] == 0:
            run(['update-ref', '-d', 'refs/tags/' + old, mine])


# **************************************** Rewriting history ****************************************
# Combine / reword make new commits with commit-tree and move the branch with update-ref (old value checked): the
# branch's last tree stays the same, so the working copy and the index are never touched (uncommitted changes stay,
# a branch that is not checked out works too, no conflicts are possible). The commits after the rewritten ones are
# made again with their own trees on top. Interactive rebase (a todo the user edited) runs `git rebase -i`.

CHAIN_MAX = 10000             # commits walked down a first-parent line
REWRITE_MAX = 1000            # commits combined / listed for a rebase


def commit_parts(repo, rev):
    """-> (author environment, raw message) of a commit (`cat-file commit`)."""
    head, _, message = git(repo, 'cat-file', 'commit', rev).partition('\n\n')
    for line in head.split('\n'):
        m = re.match(r'author (.*) <(.*)> (\d+ [+-]\d{4})$', line)
        if m:
            return {'GIT_AUTHOR_NAME': m.group(1), 'GIT_AUTHOR_EMAIL': m.group(2), 'GIT_AUTHOR_DATE': '@' + m.group(3)}, message
    return {}, message


def commits_arg(repo, value):
    """List of commit revisions -> full hashes, no duplicates."""
    if not isinstance(value, list) or not 0 < len(value) <= REWRITE_MAX:
        raise ApiError('No commits')
    hashes = []
    for rev in value:
        h = git(repo, 'rev-parse', '-q', '--verify', ref_arg(rev, 'commit') + '^{commit}', ok=(0, 1)).strip()
        if not h:
            raise ApiError(f'Not a commit: {rev}')
        if h not in hashes:
            hashes.append(h)
    return hashes


def first_parent_chain(repo, tip):
    """[(hash, [parents])] from tip down its first parents, newest first (up to CHAIN_MAX)."""
    out = git(repo, 'rev-list', '--first-parent', '--parents', f'--max-count={CHAIN_MAX}', tip, '--')
    return [(f[0], f[1:]) for f in (line.split() for line in out.splitlines()) if f]


def is_ancestor(repo, a, b):
    return run_git(repo, ['merge-base', '--is-ancestor', a, b])[0] == 0


def rewrite_targets(repo, hashes, mode):
    """Refs that can be rewritten: local branches (or a detached HEAD) whose first-parent line holds `hashes` as
    consecutive commits. -> [{ref, name, current, upstream, pushed, after, keep, chain, start}], chain[start:start +
    len(hashes)] = the commits (newest first); after = newer commits made again; keep = other refs that keep the old ones.
    combine: no merge commit among them (its other parents would be lost)."""
    fmt = FS.join(['%(refname)', '%(upstream:short)', '%(HEAD)'])
    refs = [line.split(FS) for line in git(repo, 'for-each-ref', '--contains', hashes[0], '--format=' + fmt, 'refs/heads').splitlines()]
    head = git(repo, 'rev-parse', '-q', '--verify', 'HEAD', ok=(0, 1)).strip()
    if head and not git(repo, 'symbolic-ref', '-q', 'HEAD', ok=(0, 1)).strip() and is_ancestor(repo, hashes[0], head):
        refs.append(['HEAD', '', '*'])
    targets = []
    for ref, upstream, current in refs[:50]:
        chain = first_parent_chain(repo, ref)
        index = {h: i for i, (h, _) in enumerate(chain)}
        pos = sorted(index[h] for h in hashes if h in index)
        if len(pos) != len(hashes) or pos[-1] - pos[0] + 1 != len(hashes):
            continue
        if mode == 'combine' and any(len(p) > 1 for _, p in chain[pos[0]:pos[-1] + 1]):
            raise ApiError('A merge commit cannot be combined: its merged branch would be lost')
        oldest = chain[pos[-1]][0]
        keep = [r for r in git(repo, 'for-each-ref', '--contains', oldest, '--format=%(refname)',
                               'refs/heads', 'refs/remotes', 'refs/tags').splitlines() if r != ref and not r.endswith('/HEAD')]
        targets.append({'ref': ref, 'name': ref[11:] if ref.startswith('refs/heads/') else 'HEAD (detached)',
                        'current': current == '*', 'upstream': upstream, 'after': pos[0], 'chain': chain, 'start': pos[0],
                        'pushed': bool(upstream) and is_ancestor(repo, oldest, upstream),
                        'keep': [re.sub(r'^refs/(heads|remotes|tags)/', '', r) for r in keep]})
    targets.sort(key=lambda t: not t['current'])
    return targets


def api_rewrite_plan(body):
    """What combine / reword of `hashes` would do: {targets (see rewrite_targets, without chain), message}.
    message: the commit's message (reword), or the messages oldest first (combine)."""
    repo, mode = repo_of(body), body.get('mode')
    if mode not in ('combine', 'reword'):
        raise ApiError(f'Bad mode: {mode}')
    hashes = commits_arg(repo, body.get('hashes'))
    if mode == 'combine' and len(hashes) < 2:
        raise ApiError('Pick at least two commits to combine')
    if mode == 'reword' and len(hashes) != 1:
        raise ApiError('Pick one commit to reword')
    targets = rewrite_targets(repo, hashes, mode)
    if not targets:
        raise ApiError('These are not consecutive commits of one local branch' if mode == 'combine'
                       else 'The commit is not on the line of a local branch')
    chain, start = targets[0]['chain'], targets[0]['start']
    messages = [commit_parts(repo, h)[1].strip() for h, _ in reversed(chain[start:start + len(hashes)])]
    return {'targets': [{k: v for k, v in t.items() if k not in ('chain', 'start')} for t in targets],
            'message': '\n\n'.join(m for m in messages if m), 'count': len(hashes)}


def rewrite(repo, b, run):
    """Combine (several consecutive commits -> one: the last one's tree, the oldest one's author and parent) or reword
    (one commit, new message) on b['ref']; the newer commits are made again. -> {ref, old, new, created}."""
    mode = b.get('mode')
    if mode not in ('combine', 'reword'):
        raise ApiError(f'Bad mode: {mode}')
    if repo_state(repo):
        raise ApiError(f'A {repo_state(repo)} is in progress: finish or abort it first')
    hashes, message = commits_arg(repo, b.get('hashes')), text_arg(b.get('message'), 'Commit message').strip() + '\n'
    target = next((t for t in rewrite_targets(repo, hashes, mode) if t['ref'] == b.get('ref')), None)
    if not target:
        raise ApiError('The commits are not on that branch any more: refresh and try again')
    chain, start = target['chain'], target['start']
    commits = chain[start:start + len(hashes)]                  # newest first
    author, _ = commit_parts(repo, commits[-1][0])
    created = new = None
    for i, (h, parents) in enumerate([(commits[0][0], commits[-1][1])] + chain[:start][::-1]):
        env, text = (author, message) if i == 0 else commit_parts(repo, h)
        code, out = run(['commit-tree', h + '^{tree}'] + [a for p in ([new] if new else []) + parents[i > 0:] for a in ('-p', p)],
                        input=text, env=env)
        if code:
            return {}
        new = out
        created = created or new
    old = chain[0][0]
    deref = ['--no-deref'] if target['ref'] == 'HEAD' else []
    run(['update-ref', *deref, '-m', f'diffclew: {mode}', target['ref'], new, old])
    return {'ref': target['ref'], 'old': old, 'new': new, 'created': created}


def act_rewrite_undo(repo, b):
    """Puts ref back to `old` after a combine / reword / rebase, if it is still at `new`. Same tree (combine, reword):
    update-ref only, the index and working copy stay as they are. Another tree on the checked-out branch (rebase that
    dropped or changed something): reset --keep (git refuses when a local change is in a file that differs; staged
    changes become unstaged)."""
    ref, old, new = ref_arg(b.get('ref')), ref_arg(b.get('old'), 'old'), ref_arg(b.get('new'), 'new')
    if ref != 'HEAD' and not ref.startswith('refs/heads/'):
        raise ApiError(f'Bad ref: {ref}')
    if repo_state(repo):
        raise ApiError(f'A {repo_state(repo)} is in progress: finish or abort it first')
    if git(repo, 'rev-parse', '-q', '--verify', ref, ok=(0, 1)).strip() != new:
        raise ApiError('The branch has changed since: nothing to undo')
    checked_out = ref == (git(repo, 'symbolic-ref', '-q', 'HEAD', ok=(0, 1)).strip() or 'HEAD')
    trees = git(repo, 'rev-parse', old + '^{tree}', new + '^{tree}').split()
    if checked_out and trees[0] != trees[1]:
        return [(['reset', '--keep', old], None)]
    return [(['update-ref', *(['--no-deref'] if ref == 'HEAD' else []), '-m', 'diffclew: undo', ref, old, new], None)]


def api_rebase_plan(body):
    """Commits from `start` up to HEAD (oldest first) for the interactive rebase window: {base ('' = root), branch,
    upstream, pushed, dirty, commits: [{hash, subject, message, author, date}]}. Refuses merge commits in the range."""
    repo = repo_of(body)
    start = commits_arg(repo, [body.get('start')])[0]
    if repo_state(repo):
        raise ApiError(f'A {repo_state(repo)} is in progress: finish or abort it first')
    if not is_ancestor(repo, start, 'HEAD'):
        raise ApiError('The commit is not in the current branch')
    parents = git(repo, 'rev-list', '--parents', '-1', start).split()[1:]
    if len(parents) > 1:
        raise ApiError('Rebase from a merge commit is not supported: pick a commit after it')
    base = parents[0] if parents else ''
    out = git(repo, 'log', '--reverse', f'--max-count={REWRITE_MAX + 1}', '--format=' + RS + FS.join(['%H', '%P', '%an', '%at', '%B']),
              *([f'{base}..HEAD'] if base else ['HEAD']), '--')
    commits = []
    for record in out.split(RS)[1:]:
        f = record.split(FS)
        if len(f.pop(1).split()) > 1:
            raise ApiError('There are merge commits after this one: interactive rebase works on a straight line of commits')
        message = f[3].strip()
        commits.append({'hash': f[0], 'author': f[1], 'date': int(f[2] or 0), 'message': message, 'subject': message.split('\n')[0]})
    if len(commits) > REWRITE_MAX:
        raise ApiError(f'More than {REWRITE_MAX} commits: pick a newer commit')
    branch = git(repo, 'symbolic-ref', '-q', '--short', 'HEAD', ok=(0, 1)).strip()
    upstream = git(repo, 'for-each-ref', '--format=%(upstream:short)', 'refs/heads/' + branch).strip() if branch else ''
    dirty = bool(git(repo, 'status', '--porcelain', '--untracked-files=no').strip())
    return {'base': base, 'branch': branch, 'upstream': upstream, 'pushed': bool(upstream) and is_ancestor(repo, start, upstream),
            'dirty': dirty, 'commits': commits}


def rebase_todo(plan, todo, folder):
    """The user's list (oldest first: {hash, action: pick | squash | fixup | drop, message?}) -> git-rebase-todo text.
    squash / fixup join the last picked commit as `fixup`; the group's message (the picked item's message, or for a
    group with squash items: all their messages joined) is set by `exec git commit --amend -F <file in folder>`."""
    if not isinstance(todo, list) or not all(isinstance(t, dict) for t in todo) or \
            sorted(str(t.get('hash')) for t in todo) != sorted(c['hash'] for c in plan):
        raise ApiError('The list does not match the commits of the branch any more: refresh and try again')
    messages = {c['hash']: c['message'] for c in plan}
    lines, group = [], None              # group: {message: the user's or None, joined: [messages of a squash group]}

    def close():
        if group and (group['message'] is not None or len(group['joined']) > 1):
            path = os.path.join(folder, f'message-{len(lines)}.txt')
            with open(path, 'w', encoding='utf-8') as f:
                f.write((group['message'] if group['message'] is not None else '\n\n'.join(group['joined'])).strip() + '\n')
            lines.append('exec git commit --amend --no-verify -q -F ' + shlex.quote(path.replace(os.sep, '/')))

    for t in todo:
        h, action, message = t['hash'], t.get('action'), t.get('message')
        if action not in ('pick', 'squash', 'fixup', 'drop'):
            raise ApiError(f'Bad action: {action}')
        if message is not None:
            text_arg(message, 'Commit message')
        if action == 'pick':
            close()
            group = {'message': message, 'joined': [messages[h]]}
        elif action in ('squash', 'fixup'):
            if not group:
                raise ApiError('The oldest kept commit cannot be squashed: there is no commit before it to join')
            if action == 'squash':
                group['joined'].append(messages[h])
        lines.append(f"{'fixup' if action == 'squash' else action} {h}")
    close()
    if not any(line.startswith('pick ') for line in lines):
        raise ApiError('Every commit is dropped: use Reset instead')
    return '\n'.join(lines) + '\n'


def rebase(repo, b, run):
    """Interactive rebase of the commits after b['base'] ('' = from the root) with the user's todo (see rebase_todo).
    git takes the todo through GIT_SEQUENCE_EDITOR (a copy of our file); conflicts stop it with the state 'rebase'
    (continue / skip / abort). autostash: uncommitted changes are stashed during it. -> {ref, old, new}."""
    base = b.get('base') or ''
    plan = api_rebase_plan({'repo': repo, 'start': b.get('start')})
    if plan['base'] != base:
        raise ApiError('The branch has changed: refresh and try again')
    folder = os.path.join(git_dir(repo), 'diffclew-rebase')
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    todo_path = os.path.join(folder, 'todo')
    with open(todo_path, 'w', encoding='utf-8') as f:
        f.write(rebase_todo(plan['commits'], b.get('todo'), folder))
    ref = git(repo, 'symbolic-ref', '-q', 'HEAD', ok=(0, 1)).strip() or 'HEAD'
    old = git(repo, 'rev-parse', 'HEAD').strip()
    run(['rebase', '-i', '--autostash' if b.get('autostash') else '--no-autostash', base or '--root'], timeout=600,
        env={'GIT_SEQUENCE_EDITOR': 'cp ' + shlex.quote(todo_path.replace(os.sep, '/'))})
    return {'ref': ref, 'old': old, 'new': git(repo, 'rev-parse', 'HEAD').strip()}


def act_abort(repo, b):
    state = repo_state(repo)
    if not state:
        raise ApiError('No merge, rebase, cherry-pick or revert in progress')
    # a squash merge has no `--abort` (no MERGE_HEAD): reset --merge is what merge --abort runs, and it deletes SQUASH_MSG
    return [(['reset', '--merge'] if state == 'squash' else [state, '--abort'], None)]


def act_rebase_step(step):
    """rebase --continue (with the resolved files staged; GIT_EDITOR=true keeps the message) or --skip (drops the
    commit that stopped)."""
    def build(repo, b):
        if repo_state(repo) != 'rebase':
            raise ApiError('No rebase in progress')
        return [(['rebase', step], None)]
    return build


def act_stash(repo, b):
    args = ['stash', 'push'] + (['-m', b['message']] if isinstance(b.get('message'), str) and b['message'].strip() else [])
    return [(args + (['--keep-index'] if b.get('keepIndex') else []) + (['--include-untracked'] if b.get('untracked') else []), None)]


def act_tag(repo, b):
    name, ref, message = tag_name_arg(repo, b.get('name')), ref_arg(b.get('ref') or 'HEAD'), b.get('message')
    annotated = ['-a', '-m', message] if isinstance(message, str) and message.strip() else []
    return [(['tag', *annotated, name, ref], None)]


def act_force_push(repo, b):
    """git push <remote> HEAD --force-with-lease. The page asks the user to type 'force'; the server checks it too."""
    if b.get('confirm') != 'force':
        raise ApiError('Force push needs the confirmation word "force"')
    return [(['push', remote_arg(repo, b.get('remote')), 'HEAD', '--force-with-lease'], None)]


def act_reset(repo, b):
    mode = b.get('mode')
    if mode not in ('soft', 'mixed', 'hard'):
        raise ApiError(f'Bad reset mode: {mode}')
    return [(['reset', '--' + mode, ref_arg(b.get('ref'))], None)]


def act_apply_patch(repo, b):
    """One hunk: stage (cached), unstage (cached + reverse) or discard (reverse) it."""
    patch = text_arg(b.get('patch'), 'Patch')
    args = ['apply', '--whitespace=nowarn'] + (['--cached'] if b.get('cached') else []) + (['-R'] if b.get('reverse') else [])
    return [(args + (['--unidiff-zero'] if b.get('zero') else []) + ['-'], patch if patch.endswith('\n') else patch + '\n')]


ACTIONS = {
    'stage':         lambda repo, b: [(['add', '-A', '--', *paths_arg(b.get('paths'))], None)],
    'unstage':       act_unstage,
    'discard':       act_discard,
    'commit':        act_commit,
    'fetch':         act_fetch,
    'pull':          act_pull,
    'push':          act_push,
    'branch':        act_branch,
    'checkout':      act_checkout,
    'abort':         act_abort,
    'rebase_continue': act_rebase_step('--continue'),
    'rebase_skip':   act_rebase_step('--skip'),
    'rewrite_undo':  act_rewrite_undo,
    'stash':         act_stash,
    'stash_apply':   lambda repo, b: [(['stash', 'apply', stash_arg(b.get('ref'))], None)],
    'stash_pop':     lambda repo, b: [(['stash', 'pop', stash_arg(b.get('ref'))], None)],
    'stash_drop':    lambda repo, b: [(['stash', 'drop', stash_arg(b.get('ref'))], None)],
    'delete_branch': lambda repo, b: [(['branch', '-D' if b.get('force') else '-d', ref_arg(b.get('name'), 'branch')], None)],
    'delete_tag':    lambda repo, b: [(['tag', '-d', ref_arg(b.get('name'), 'tag')], None)],
    'tag':           act_tag,
    # cherry-pick / revert only prepare the change (--no-commit): the user reviews it in File status and commits
    'cherry_pick':   lambda repo, b: [(['cherry-pick', '--no-commit'] + (['-m', '1'] if b.get('mainline') else []) + [ref_arg(b.get('ref'))], None)],
    'revert':        lambda repo, b: [(['revert', '--no-commit'] + (['-m', '1'] if b.get('mainline') else []) + [ref_arg(b.get('ref'))], None)],
    'undo_commit':   lambda repo, b: [(['reset', '--soft', 'HEAD~1'], None)],   # the commit's changes stay staged
    'force_push':    act_force_push,
    'reset':         act_reset,
    'apply_patch':   act_apply_patch,
}
NETWORK = {'fetch', 'pull', 'push', 'force_push'}


def open_folder(repo, terminal):
    """Show in Finder / Explorer, or a terminal in the repository (detached, not waited for)."""
    if sys.platform == 'darwin':
        cmd = ['open', '-a', 'Terminal', repo] if terminal else ['open', repo]
    elif os.name == 'nt':
        cmd = ['cmd', '/c', 'start', 'cmd', '/K', 'cd', '/d', repo] if terminal else ['explorer', repo]
    else:
        cmd = ['x-terminal-emulator', '--working-directory', repo] if terminal else ['xdg-open', repo]
    try:
        subprocess.Popen(cmd, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ApiError(f'Cannot run {cmd[0]}: {e.strerror}')
    return {'ok': True, 'output': '', 'command': ' '.join(cmd)}


def repo_file(repo, path):
    """Absolute path of a repository-relative path; its folder must stay inside the repository (no '..', no symlinked
    folder out). Not on disk (deleted file, file of an old commit) -> the nearest existing folder above it."""
    if not isinstance(path, str) or not path or '\0' in path or os.path.isabs(path) or '..' in re.split(r'[\\/]', path):
        raise ApiError('Bad path')
    root = os.path.realpath(repo)
    folder = os.path.realpath(os.path.join(root, os.path.dirname(path)))
    if folder != root and not folder.startswith(root + os.sep):
        raise ApiError('The path is outside the repository')
    target = os.path.join(folder, os.path.basename(path))
    while not os.path.lexists(target) and target != root:
        target = os.path.dirname(target)
    return target


def reveal_file(repo, path):
    """Shows a file of the repository in Finder / Explorer / the file manager, selected. Never opens or runs the file."""
    target = repo_file(repo, path)
    if sys.platform == 'darwin':
        cmd = ['open', '-R', target]
    elif os.name == 'nt':
        cmd = f'explorer /select,"{target}"'       # a Windows file name cannot contain '"'
    else:
        uri = 'file://' + urllib.parse.quote(target)
        cmd = ['dbus-send', '--session', '--print-reply', '--dest=org.freedesktop.FileManager1', '--type=method_call',
               '/org/freedesktop/FileManager1', 'org.freedesktop.FileManager1.ShowItems', 'array:string:' + uri, 'string:']
        try:
            if subprocess.run(cmd, capture_output=True, timeout=5, stdin=subprocess.DEVNULL).returncode == 0:
                return {'ok': True, 'output': '', 'command': ' '.join(cmd)}
        except (OSError, subprocess.SubprocessError):
            pass
        # no FileManager1 service: open the folder (xdg-open on the file itself would open / run it)
        cmd = ['xdg-open', target if os.path.isdir(target) else os.path.dirname(target)]
    try:
        subprocess.Popen(cmd, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ApiError(f'Cannot show the file: {e.strerror}')
    return {'ok': True, 'output': '', 'command': cmd if isinstance(cmd, str) else ' '.join(cmd)}


_choosing = threading.Lock()


def api_choose(body):
    """The OS's own folder dialog (the server runs on the user's machine) -> {path}, '' when cancelled.
    The start folder goes to the dialog as an argument / environment variable, never inside the script text."""
    start = os.path.realpath(user_path(body.get('path')) or os.path.expanduser('~'))
    if not os.path.isdir(start):
        start = os.path.expanduser('~')
    env = None
    if sys.platform == 'darwin':
        cmd = ['osascript', '-l', 'JavaScript', '-e',
               'function run(argv) { const app = Application.currentApplication(); app.includeStandardAdditions = true; '
               'app.activate(); return app.chooseFolder({ withPrompt: "Open repository", defaultLocation: Path(argv[0]) }).toString(); }',
               start]
    elif os.name == 'nt':
        cmd = ['powershell', '-NoProfile', '-STA', '-Command',
               'Add-Type -AssemblyName System.Windows.Forms; '
               '$owner = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; '
               '$d = New-Object System.Windows.Forms.FolderBrowserDialog; '
               "$d.Description = 'Open repository'; $d.SelectedPath = $env:DIFFCLEW_START; "
               "if ($d.ShowDialog($owner) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $d.SelectedPath }"]
        env = {**os.environ, 'DIFFCLEW_START': start}
    elif shutil.which('zenity'):
        cmd = ['zenity', '--file-selection', '--directory', '--title=Open repository', '--filename=' + os.path.join(start, '')]
    elif shutil.which('kdialog'):
        cmd = ['kdialog', '--title', 'Open repository', '--getexistingdirectory', start]
    else:
        raise ApiError('No folder dialog here (zenity or kdialog is needed): paste the path instead')
    if not _choosing.acquire(blocking=False):
        raise ApiError('The folder dialog is already open')
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=3600, stdin=subprocess.DEVNULL, env=env)
    except OSError as e:
        raise ApiError(f'Cannot run {cmd[0]}: {e.strerror}')
    except subprocess.TimeoutExpired:
        return {'path': ''}
    finally:
        _choosing.release()
    err = out.stderr.decode('utf-8', 'replace').strip()
    if out.returncode != 0:
        if sys.platform == 'darwin' and '-128' not in err:          # -128: the user cancelled
            raise ApiError('Folder dialog failed: ' + (err.splitlines() or ['exit code %d' % out.returncode])[-1])
        return {'path': ''}
    return {'path': out.stdout.decode('utf-8', 'replace').strip()}


def api_action(body):
    repo, name = repo_of(body), body.get('action')
    if name == 'reveal' and body.get('path'):
        return reveal_file(repo, body['path'])
    if name in ('reveal', 'terminal'):
        return open_folder(repo, name == 'terminal')
    if name in LOGGED:
        return run_logged(repo, body, LOGGED[name])
    if name not in ACTIONS:
        raise ApiError(f'Unknown action: {name}')
    steps = ACTIONS[name](repo, body)
    timeout, outputs, text = (600 if name in NETWORK else 120), [], ''
    with _locks[repo]:
        for args, stdin in steps:
            text = command_text(args)
            code, out, err = run_git(repo, args, input=stdin, timeout=timeout)
            output = (out + err).decode('utf-8', 'replace').strip()
            outputs.append(f'$ {text}' + ('\n' + output if output else ''))
            if code != 0:
                return {'ok': False, 'output': '\n'.join(outputs), 'command': text}
    return {'ok': True, 'output': '\n'.join(outputs), 'command': text}


def run_logged(repo, body, work):
    """A write action whose steps depend on results (merge_to, rewrite, rebase): work(repo, body, run) under the
    repository lock -> {ok, output, command} + what work returns; ok: false when any git command failed.
    run(args, timeout=120, input=None, env=None) runs and logs one git command -> (exit code, stdout stripped)."""
    outputs, failed = [], []

    def run(args, timeout=120, input=None, env=None):
        code, out, err = run_git(repo, args, input=input, timeout=timeout, env=env)
        output = (out + err).decode('utf-8', 'replace').strip()
        outputs.append(f'$ {command_text(args)}' + ('\n' + output if output else ''))
        if code != 0 and not failed:
            failed.append(command_text(args))
        return code, out.decode('utf-8', 'replace').strip()

    with _locks[repo]:
        extra = work(repo, body, run) or {}
    return {'ok': not failed, 'output': '\n'.join(outputs), 'command': failed[0] if failed else '', **extra}


LOGGED = {'merge': merge, 'merge_to': merge_to, 'rename_branch': rename_branch, 'rename_tag': rename_tag, 'rewrite': rewrite, 'rebase': rebase}


API = {'initial': api_initial, 'open': api_open, 'ls': api_ls, 'refs': api_refs, 'log': api_log, 'search': api_search,
       'commit': api_commit, 'diff': api_diff, 'status': api_status, 'action': api_action, 'choose': api_choose,
       'rewrite_plan': api_rewrite_plan, 'rebase_plan': api_rebase_plan}


# **************************************** HTTP ****************************************

app = Server(HOST, PORT, root=ROOT)


@app.route('GET', '/')
def index_page(req: Request, resp):
    return Response.withFile('index.html')


@app.route('POST', pattern=r'/api/(\w+)')
def api(req: Request, resp):
    if req.headers.get('X-DiffClew') != '1':
        raise HttpError(403, 'Missing X-DiffClew header')
    host = re.sub(r':\d+$', '', req.headers.get('Host') or '')
    if host not in ('127.0.0.1', 'localhost', '[::1]'):
        raise HttpError(403, 'Bad Host header')
    handler = API.get(req.match.group(1))
    if not handler:
        return Response.withJson({'error': 'Unknown API: ' + req.match.group(1)}, 404)
    body = req.json() or {}
    if not isinstance(body, dict):
        raise HttpError(400, 'Body must be a JSON object')
    try:
        return Response.withJson(handler(body))
    except ApiError as e:
        return Response.withJson({'error': str(e)}, 400)


# **************************************** Tests ****************************************

def run_tests():
    def check(name, actual, expected):
        assert actual == expected, f'FAIL {name}: expected {expected!r}, got {actual!r}'
        print('PASS', name)

    log = f'{RS}a1{FS}p1 p2{FS}Ann{FS}a@x{FS}100{FS}Merge x\n{RS}p1{FS}{FS}Bob{FS}b@x{FS}50{FS}Root\n'
    check('log', parse_log(log), [
        {'hash': 'a1', 'parents': ['p1', 'p2'], 'author': 'Ann', 'email': 'a@x', 'date': 100, 'subject': 'Merge x'},
        {'hash': 'p1', 'parents': [], 'author': 'Bob', 'email': 'b@x', 'date': 50, 'subject': 'Root'}])
    check('name-status', parse_name_status('M\0b.txt\0R087\0old.txt\0a.txt\0D\0c d.txt\0'), [
        {'status': 'R', 'oldPath': 'old.txt', 'path': 'a.txt'}, {'status': 'M', 'path': 'b.txt'},
        {'status': 'D', 'path': 'c d.txt'}])
    staged, unstaged = parse_status('M  a.txt\0 M b.txt\0MM c.txt\0R  new.txt\0old.txt\0?? n.txt\0UU x.txt\0')
    check('status staged', staged, [{'status': 'M', 'path': 'a.txt'}, {'status': 'M', 'path': 'c.txt'},
                                    {'status': 'R', 'path': 'new.txt', 'oldPath': 'old.txt'}])
    check('status unstaged', unstaged, [{'status': 'M', 'path': 'b.txt'}, {'status': 'M', 'path': 'c.txt'},
                                        {'status': '?', 'path': 'n.txt'}, {'status': 'C', 'path': 'x.txt', 'conflict': 'UU'}])
    check('log with file paths', parse_log(f'{RS}a1{FS}p1{FS}Ann{FS}a@x{FS}100{FS}Rename\n\nnew/name.txt\n{RS}p1{FS}{FS}Bob{FS}b@x{FS}50{FS}Add\n\nold.txt\n'),
          [{'hash': 'a1', 'parents': ['p1'], 'author': 'Ann', 'email': 'a@x', 'date': 100, 'subject': 'Rename', 'path': 'new/name.txt'},
           {'hash': 'p1', 'parents': [], 'author': 'Bob', 'email': 'b@x', 'date': 50, 'subject': 'Add', 'path': 'old.txt'}])
    check('track', parse_track('ahead 1, behind 21'), (1, 21))
    check('track gone', parse_track('gone'), (0, 0))
    for bad in ('-x', '--upload-pack=x', 'a b', ''):
        try:
            ref_arg(bad)
            raise AssertionError(f'FAIL ref_arg accepted {bad!r}')
        except ApiError:
            pass
    print('PASS ref_arg rejects options')
    check('stash ref', stash_arg('stash@{2}'), 'stash@{2}')
    try:
        act_force_push(None, {'remote': 'origin'})
        raise AssertionError('FAIL force push ran without the confirmation word')
    except ApiError:
        print('PASS force push needs "force"')
    check('cherry-pick prepares only', ACTIONS['cherry_pick'](None, {'ref': 'abc1234'})[0][0], ['cherry-pick', '--no-commit', 'abc1234'])
    check('revert of a merge prepares only', ACTIONS['revert'](None, {'ref': 'abc1234', 'mainline': True})[0][0],
          ['revert', '--no-commit', '-m', '1', 'abc1234'])
    check('undo commit keeps changes staged', ACTIONS['undo_commit'](None, {})[0][0], ['reset', '--soft', 'HEAD~1'])
    check('checkout keeps changes', [s[0] for s in act_checkout(None, {'ref': 'dev'})], [['switch', 'dev']])
    check('checkout with stash', [s[0] for s in act_checkout(None, {'ref': 'dev', 'localChanges': 'stash'})],
          [['stash', 'push', '--include-untracked', '-m', 'Before checkout of dev'], ['switch', 'dev']])
    check('checkout with discard', [s[0] for s in act_checkout(None, {'ref': 'dev', 'localChanges': 'discard', 'mode': 'detach'})],
          [['reset', '--hard', '-q', 'HEAD'], ['clean', '-fd', '-q'], ['switch', '--detach', 'dev']])
    no_alias = lambda host: host
    for url, page in [('git@github.com:owner/repo.git', 'https://github.com/owner/repo'),
                      ('https://user:token@gitlab.com/group/sub/repo.git', 'https://gitlab.com/group/sub/repo'),
                      ('ssh://git@git.example.com:2222/team/repo.git/', 'https://git.example.com/team/repo'),
                      ('http://host:8080/a/b', 'http://host:8080/a/b'),
                      ('git@ssh.dev.azure.com:v3/org/proj/repo', 'https://dev.azure.com/org/proj/_git/repo'),
                      ('https://org@dev.azure.com/org/proj/_git/repo', 'https://dev.azure.com/org/proj/_git/repo'),
                      ('/srv/git/repo.git', ''), ('../other', ''), ('file:///srv/repo', ''), ('C:\\repos\\x', ''), ('git@host:', '')]:
        check('web url ' + url, web_url(url, no_alias), page)
    check('web url ssh alias', web_url('work:owner/repo.git', lambda h: 'github.com' if h == 'work' else h), 'https://github.com/owner/repo')
    check('pasted quoted path', user_path('"/tmp/a b"'), '/tmp/a b')
    check('pasted file url', user_path('file:///tmp/a%20b/'), '/tmp/a b/')
    if os.sep == '/':
        check('pasted escaped path', user_path('/tmp/no\\ such\\ dir'), '/tmp/no such dir')
    check('pasted empty path', user_path('  '), '')
    for bad in ('../x', 'a/../../x', '/etc/passwd', ''):
        try:
            repo_file(ROOT, bad)
            raise AssertionError(f'FAIL repo_file accepted {bad!r}')
        except ApiError:
            pass
    print('PASS repo_file stays inside the repository')
    check('repo file', repo_file(ROOT, 'diffclew.py'), os.path.join(os.path.realpath(ROOT), 'diffclew.py'))
    check('repo file not on disk -> folder', repo_file(ROOT, 'no-such-dir/x.txt'), os.path.realpath(ROOT))
    test_merge_to(check)
    test_squash_rename(check)
    test_rename_tag(check)
    test_rewrite(check)
    print('All tests passed')


def test_merge_to(check):
    """merge_to on throwaway repositories (a temp folder with a bare 'origin')."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        origin, repo = os.path.join(tmp, 'origin.git'), os.path.join(tmp, 'repo')
        g = lambda *args, ok=(0,): git(repo, *args, ok=ok).strip()
        def write(name, text):
            with open(os.path.join(repo, name), 'w') as f:
                f.write(text)
        git(tmp, 'init', '-q', '--bare', '-b', 'develop', origin)
        git(tmp, 'init', '-q', '-b', 'develop', repo)
        for key, value in (('user.name', 'T'), ('user.email', 't@t'), ('commit.gpgsign', 'false')):
            g('config', key, value)
        write('a.txt', 'a\n'), write('b.txt', 'b\n'), g('add', '.'), g('commit', '-qm', 'init')
        g('remote', 'add', 'origin', origin), g('push', '-qu', 'origin', 'develop')
        g('switch', '-qc', 'feature'), write('a.txt', 'a\nfeature\n'), g('commit', '-qam', 'feature')
        g('switch', '-q', 'develop'), write('b.txt', 'b\ndevelop\n'), g('commit', '-qam', 'develop'), g('switch', '-q', 'feature')
        call = lambda **b: run_logged(repo, {'target': 'develop', **b}, merge_to)
        for name, change in (('untracked', lambda: write('new.txt', 'x')), ('staged', lambda: (write('b.txt', 'local\n'), g('add', 'b.txt')))):
            change()
            try:
                call(commit=True)
                raise AssertionError(f'FAIL merge_to ran with {name} files')
            except ApiError:
                pass
            check(f'merge_to with {name} files does nothing', (g('branch', '--show-current'), bool(g('status', '--porcelain'))), ('feature', True))
            g('reset', '-q', '--hard'), g('clean', '-qfd')
        result = call(commit=True, push=True)
        check('merge_to commit + push', (result['ok'], g('branch', '--show-current'), len(g('rev-list', '--parents', '-1', 'develop').split()),
                                          g('rev-parse', 'develop') == g('rev-parse', 'origin/develop')), (True, 'feature', 3, True))
        check('merge_to message', g('log', '-1', '--format=%s', 'develop'), "Merge branch 'feature' into develop")
        write('a.txt', 'a\nfeature 2\n'), g('commit', '-qam', 'feature 2')
        result = call(commit=False)
        check('merge_to without commit waits on target', (result['ok'], g('branch', '--show-current'), repo_state(repo)), (True, 'develop', 'merge'))
        check('merge_to message waits in File status', api_status({'repo': repo})['message'], "Merge branch 'feature' into develop")
        g('merge', '--abort'), write('a.txt', 'a\nconflict\n'), g('commit', '-qam', 'conflict'), g('switch', '-q', 'feature')
        result = call(commit=True, push=True)
        check('merge_to conflict waits on target', (result['ok'], g('branch', '--show-current'), repo_state(repo),
                                                     g('rev-parse', 'develop~1') == g('rev-parse', 'origin/develop')), (False, 'develop', 'merge', True))


def test_squash_rename(check):
    """Squash merge (here and into another branch), its abort, and rename_branch (local / remote / both)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        origin, repo = os.path.join(tmp, 'origin.git'), os.path.join(tmp, 'repo')
        g = lambda *args, ok=(0,): git(repo, *args, ok=ok).strip()
        def write(name, text):
            with open(os.path.join(repo, name), 'w') as f:
                f.write(text)
        git(tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        git(tmp, 'init', '-q', '-b', 'main', repo)
        for key, value in (('user.name', 'T'), ('user.email', 't@t'), ('commit.gpgsign', 'false')):
            g('config', key, value)
        write('a.txt', 'a\n'), g('add', '.'), g('commit', '-qm', 'init')
        g('remote', 'add', 'origin', origin), g('push', '-qu', 'origin', 'main'), g('remote', 'set-head', 'origin', 'main')
        g('switch', '-qc', 'feature'), write('a.txt', 'a\none\n'), g('commit', '-qam', 'one')
        write('b.txt', 'b\n'), g('add', '.'), g('commit', '-qm', 'two'), g('switch', '-q', 'main')
        result = run_logged(repo, {'ref': 'feature', 'squash': True}, merge)
        status = api_status({'repo': repo})
        check('squash merge waits', (result['ok'], repo_state(repo), merge_heads(repo), len(status['staged'])), (True, 'squash', [], 2))
        check('squash message', status['message'], "Squash merge branch 'feature'\n\n- one\n- two")
        try:
            run_logged(repo, {'ref': 'feature'}, merge)
            raise AssertionError('FAIL merge ran during a squash merge')
        except ApiError:
            pass
        api_action({'repo': repo, 'action': 'abort'})
        check('squash abort', (repo_state(repo), g('status', '--porcelain'), os.path.exists(os.path.join(git_dir(repo), 'SQUASH_MSG'))), ('', '', False))
        run_logged(repo, {'ref': 'feature', 'squash': True}, merge)
        g('reset', '-q', '--hard')                                           # thrown away by hand: SQUASH_MSG is stale
        check('stale SQUASH_MSG is dropped', (repo_state(repo), os.path.exists(os.path.join(git_dir(repo), 'SQUASH_MSG'))), ('', False))
        g('switch', '-q', 'feature')
        result = run_logged(repo, {'target': 'main', 'squash': True, 'commit': True, 'push': True}, merge_to)
        check('merge_to squash + push', (result['ok'], g('branch', '--show-current'), len(g('rev-list', '--parents', '-1', 'main').split()),
                                          g('rev-parse', 'main') == g('rev-parse', 'origin/main'), g('log', '-1', '--format=%s', 'main')),
              (True, 'feature', 2, True, "Squash merge branch 'feature' into main"))
        check('merge_to squash tree', g('rev-parse', 'main^{tree}') == g('rev-parse', 'feature^{tree}'), True)

        rename = lambda **b: run_logged(repo, b, rename_branch)
        rename(mode='local', name='feature', newName='feat')
        check('rename local', (g('branch', '--show-current'), g('show-ref', '--verify', 'refs/heads/feature', ok=(0, 1, 128))), ('feat', ''))
        g('push', '-qu', 'origin', 'feat'), g('switch', '-q', 'main')
        result = rename(mode='both', name='feat', newName='feature-x')
        check('rename both', (result['ok'], g('ls-remote', '--heads', 'origin').count('refs/heads/feat\n'),
                              'refs/heads/feature-x' in g('ls-remote', '--heads', 'origin'), g('rev-parse', '--abbrev-ref', 'feature-x@{u}')),
              (True, 0, True, 'origin/feature-x'))
        g('switch', '-q', 'feature-x'), write('a.txt', 'local only\n'), g('commit', '-qam', 'local only'), g('switch', '-q', 'main')
        result = rename(mode='remote', remote='origin', branch='feature-x', newName='feature-y')
        check('rename remote: the remote commit, not the local one', (result['ok'], g('rev-parse', 'origin/feature-y') == g('rev-parse', 'feature-x~1'),
                                                                       g('rev-parse', '--abbrev-ref', 'feature-x@{u}')), (True, True, 'origin/feature-y'))
        g('push', '-q', 'origin', 'origin/feature-y:refs/heads/taken')                 # the same commit: the lease would pass
        try:
            rename(mode='remote', remote='origin', branch='feature-y', newName='taken')
            raise AssertionError('FAIL rename onto an existing remote branch ran')
        except ApiError:
            pass
        check('rename remote onto an existing branch does nothing', 'refs/heads/feature-y' in g('ls-remote', '--heads', 'origin'), True)
        for branch in ('main', 'develop'):
            try:
                rename(mode='remote', remote='origin', branch=branch, newName='other')
                raise AssertionError(f'FAIL rename of {branch} ran without the confirmation')
            except ApiError:
                pass
        check('protected rename needs the word', 'refs/heads/main' in g('ls-remote', '--heads', 'origin'), True)


def test_rename_tag(check):
    """rename_tag: lightweight / annotated, local / both / remote, refusals."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        origin, repo = os.path.join(tmp, 'origin.git'), os.path.join(tmp, 'repo')
        g = lambda *args, ok=(0,): git(repo, *args, ok=ok).strip()
        git(tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        git(tmp, 'init', '-q', '-b', 'main', repo)
        for key, value in (('user.name', 'T'), ('user.email', 't@t'), ('commit.gpgsign', 'false'), ('tag.gpgsign', 'false')):
            g('config', key, value)
        with open(os.path.join(repo, 'a.txt'), 'w') as f:
            f.write('a\n')
        g('add', '.'), g('commit', '-qm', 'init'), g('tag', 'light'), g('tag', '-a', 'v1', '-m', 'Release one\n\nNotes')
        g('remote', 'add', 'origin', origin), g('push', '-q', 'origin', 'main', '--tags')
        rename = lambda **b: run_logged(repo, b, rename_tag)
        remote_tags = lambda: [line.split()[1] for line in g('ls-remote', '--tags', 'origin').splitlines()]
        tagger = g('for-each-ref', '--format=%(taggername) %(taggerdate:raw)', 'refs/tags/v1')
        rename(mode='local', name='light', newName='light2')
        check('rename lightweight tag', (g('rev-parse', 'light2'), g('tag', '-l', 'light')), (g('rev-parse', 'HEAD'), ''))
        result = rename(mode='both', name='v1', newName='v1.0', remote='origin')
        check('rename annotated tag local + remote', (result['ok'], g('tag', '-l', 'v1'), g('cat-file', 'tag', 'v1.0').split('\n')[2],
                                                       g('for-each-ref', '--format=%(contents)', 'refs/tags/v1.0'),
                                                       g('for-each-ref', '--format=%(taggername) %(taggerdate:raw)', 'refs/tags/v1.0') == tagger,
                                                       g('rev-parse', 'v1.0^{commit}') == g('rev-parse', 'HEAD'),
                                                       sorted(remote_tags()), g('rev-parse', 'v1.0') in g('ls-remote', '--tags', 'origin')),
              (True, '', 'tag v1.0', 'Release one\n\nNotes', True, True, ['refs/tags/light', 'refs/tags/v1.0', 'refs/tags/v1.0^{}'], True))
        result = rename(mode='remote', name='light', newName='base', remote='origin')
        check('rename remote tag only', (result['ok'], 'refs/tags/base' in remote_tags(), 'refs/tags/light' in remote_tags(), g('tag', '-l', 'light2')),
              (True, True, False, 'light2'))
        g('tag', 'taken'), g('tag', 'other', 'HEAD')
        for name, body in (('existing local name', {'mode': 'local', 'name': 'other', 'newName': 'taken'}),
                           ('same name', {'mode': 'local', 'name': 'other', 'newName': 'other'}),
                           ('another object on the remote', {'mode': 'both', 'name': 'v1.0', 'newName': 'v2', 'remote': 'origin'})):
            g('tag', '-f', '-a', 'v1.0', '-m', 'moved') if name.startswith('another') else None
            try:
                rename(**body)
                raise AssertionError(f'FAIL tag rename with {name} ran')
            except ApiError:
                pass
        check('refused tag renames change nothing', ('refs/tags/v2' in remote_tags(), g('tag', '-l', 'v2')), (False, ''))
        g('push', '-q', 'origin', 'main:refs/tags/clash')
        try:
            rename(mode='remote', name='base', newName='clash', remote='origin')
            raise AssertionError('FAIL remote tag rename onto an existing tag ran')
        except ApiError:
            pass
        check('remote tag rename onto an existing tag does nothing', 'refs/tags/base' in remote_tags(), True)


def test_rewrite(check):
    """Combine / reword / undo / interactive rebase on a throwaway repository."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, 'repo')
        g = lambda *args: git(repo, *args).strip()
        def write(name, text):
            with open(os.path.join(repo, name), 'w') as f:
                f.write(text)
        git(tmp, 'init', '-q', '-b', 'main', repo)
        for key, value in (('user.name', 'T'), ('user.email', 't@t'), ('commit.gpgsign', 'false')):
            g('config', key, value)
        for name in 'abcde':
            write(name + '.txt', name), g('add', '.'), g('commit', '-qm', 'commit ' + name)
        h = {s.split()[2]: s.split()[0] for s in g('log', '--format=%H %s').splitlines()}
        g('branch', 'other', h['c'])
        write('a.txt', 'staged'), g('add', 'a.txt'), write('b.txt', 'unstaged'), write('new.txt', 'untracked')
        status, tree = g('status', '--porcelain'), g('rev-parse', 'main^{tree}')
        plan = api_rewrite_plan({'repo': repo, 'mode': 'combine', 'hashes': [h['d'], h['b'], h['c']]})
        check('combine plan', [(t['name'], t['current'], t['after'], t['keep']) for t in plan['targets']], [('main', True, 1, ['other'])])
        check('combine message oldest first', plan['message'], 'commit b\n\ncommit c\n\ncommit d')
        for bad in ([h['b'], h['d']], [h['b']]):
            try:
                api_rewrite_plan({'repo': repo, 'mode': 'combine', 'hashes': bad})
                raise AssertionError(f'FAIL combine accepted {bad}')
            except ApiError:
                pass
        print('PASS combine needs consecutive commits')
        result = run_logged(repo, {'mode': 'combine', 'hashes': [h['b'], h['c'], h['d']], 'ref': 'refs/heads/main', 'message': 'b to d'}, rewrite)
        check('combine', (result['ok'], g('log', '--format=%s', 'main').splitlines(), g('rev-parse', 'main^{tree}') == tree,
                          g('status', '--porcelain') == status, g('rev-parse', 'other') == h['c']),
              (True, ['commit e', 'b to d', 'commit a'], True, True, True))
        check('combine keeps the oldest author date', g('log', '-1', '--format=%at', result['created']), g('log', '-1', '--format=%at', h['b']))
        undo = act_rewrite_undo(repo, result)
        check('undo of the same tree moves the ref only', undo, [(['update-ref', '-m', 'diffclew: undo', 'refs/heads/main', h['e'], result['new']], None)])
        run_git(repo, undo[0][0])
        check('undo keeps local changes', (g('rev-parse', 'main'), g('status', '--porcelain') == status), (h['e'], True))
        result = run_logged(repo, {'mode': 'reword', 'hashes': [h['b']], 'ref': 'refs/heads/other', 'message': 'B!'}, rewrite)
        check('reword another branch', (result['ok'], g('log', '--format=%s', 'other').splitlines(), g('rev-parse', 'main')),
              (True, ['commit c', 'B!', 'commit a'], h['e']))
        check('undo of another branch', act_rewrite_undo(repo, result), [(['update-ref', '-m', 'diffclew: undo', 'refs/heads/other', h['c'], result['new']], None)])
        g('reset', '-q', '--hard'), g('clean', '-qfd')
        plan = api_rebase_plan({'repo': repo, 'start': h['b']})
        check('rebase plan', ([c['subject'] for c in plan['commits']], plan['base'], plan['dirty']), (['commit b', 'commit c', 'commit d', 'commit e'], h['a'], False))
        todo = [{'hash': h['b'], 'action': 'pick'}, {'hash': h['c'], 'action': 'squash'}, {'hash': h['d'], 'action': 'drop'},
                {'hash': h['e'], 'action': 'pick', 'message': 'E reworded'}]
        result = run_logged(repo, {'start': h['b'], 'base': h['a'], 'todo': todo}, rebase)
        check('rebase squash + drop + reword', (result['ok'], repo_state(repo), g('log', '--format=%B', '-3').split('\n\n')[:3],
                                                sorted(os.listdir(repo))), (True, '', ['E reworded', 'commit b', 'commit c'], ['.git', 'a.txt', 'b.txt', 'c.txt', 'e.txt']))
        check('undo of a rebase resets', act_rewrite_undo(repo, result), [(['reset', '--keep', h['e']], None)])
        g('reset', '-q', '--hard', h['e'])
        for text in ('1', '2', '3'):
            write('x.txt', text), g('add', '.'), g('commit', '-qm', 'x' + text)
        x = g('log', '--format=%H', '-3').split()[::-1]
        todo = [{'hash': x[0], 'action': 'pick'}, {'hash': x[1], 'action': 'drop'}, {'hash': x[2], 'action': 'pick'}]
        result = run_logged(repo, {'start': x[0], 'base': h['e'], 'todo': todo}, rebase)
        check('rebase conflict stops', (result['ok'], repo_state(repo)), (False, 'rebase'))
        write('x.txt', '3'), g('add', 'x.txt')
        code = run_git(repo, ACTIONS['rebase_continue'](repo, {})[0][0])[0]
        check('rebase continue', (code, repo_state(repo), g('log', '--format=%s', '-2').split()), (0, '', ['x3', 'x1']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Git client in the browser')
    parser.add_argument('repos', nargs='*', help='repositories to open as tabs')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--test', action='store_true', help='run the parser self-tests and exit')
    ARGS = parser.parse_args()
    if ARGS.test:
        run_tests()
    else:
        app.port = ARGS.port
        app.start_and_wait()
