#!/usr/bin/env python3
"""hotmem.py: hot search over the last N days of ALL your agent sessions (Claude Code, Codex, Gemini, any JSONL).

One file, Python stdlib only, SQLite FTS5. No LLM, no network, no server.

  python3 hotmem.py ingest              # (re)index sessions modified in the last 14 days (incremental)
  python3 hotmem.py search "my query"   # best sessions first: path, date, snippet
  python3 hotmem.py hook                # Claude Code UserPromptSubmit hook: injects top hits on first prompt
  python3 hotmem.py stats               # what is indexed and how fresh

Where it looks (edit with env HOTMEM_ROOTS, separated by ':' on mac/linux, ';' on Windows):
  ~/.claude/projects   (Claude Code, also Claude Desktop Code tab)
  ~/.codex/sessions    (Codex CLI / app)
  ~/.gemini/tmp        (Gemini CLI chats)
  + any folder you add, e.g. a Syncthing folder where other machines drop their sessions.

Why no distillation is required: the window is short (14 days), so raw sessions are still searchable
"by hand". Distillation (a few clean sentences per session) improves ranking when you have thousands of
sessions; it is an optional upgrade, not a precondition.

Env: HOTMEM_DB (default ~/.hotmem/hotmem.db), HOTMEM_DAYS (default 14), HOTMEM_ROOTS (extra roots).
License MIT. github.com/tonydzi
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path.home()
DB = Path(os.environ.get('HOTMEM_DB', HOME / '.hotmem' / 'hotmem.db'))
DAYS = float(os.environ.get('HOTMEM_DAYS', '14'))
DEFAULT_ROOTS = [
    ('claude', HOME / '.claude' / 'projects'),
    ('codex', HOME / '.codex' / 'sessions'),
    ('gemini', HOME / '.gemini' / 'tmp'),
]
MAX_TEXT = 4000
SECRET_RE = re.compile(r'(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}'
                       r'|xox[bp]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})')
SKIP_ROLES = {'system', 'developer', 'tool'}
NOISE_PREFIXES = ('# AGENTS.md instructions', '<environment_context', '<user_instructions', '<permissions',
                  '<system-reminder', '<command-name>', '<local-command', 'Caveat: The messages below')


def roots():
    out = [(n, p) for n, p in DEFAULT_ROOTS if p.is_dir()]
    for raw in os.environ.get('HOTMEM_ROOTS', '').split(os.pathsep):
        if raw.strip() and Path(raw).expanduser().is_dir():
            out.append(('extra', Path(raw).expanduser()))
    return out


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INT, source TEXT);
    CREATE TABLE IF NOT EXISTS rows(rid INTEGER PRIMARY KEY, path TEXT, source TEXT, session TEXT,
                                    role TEXT, ts TEXT, title TEXT);
    CREATE INDEX IF NOT EXISTS ix_rows_path ON rows(path);
    CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, tokenize="unicode61 remove_diacritics 2");
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    ''')
    return con


# ---------- parsing: pull (role, text, ts, session) out of any session format ----------

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for it in content:
            if isinstance(it, str):
                parts.append(it)
            elif isinstance(it, dict) and it.get('type') in (None, 'text', 'input_text', 'output_text'):
                parts.append(it.get('text') or '')
        return '\n'.join(p for p in parts if p)
    if isinstance(content, dict):
        return content.get('text') or ''
    return ''


def _msg(o):
    """Return (role, text, ts, session) or None. Knows Claude Code, Codex, Gemini, generic chat JSON."""
    if not isinstance(o, dict):
        return None
    ts = o.get('timestamp') or o.get('ts') or ''
    sid = o.get('sessionId') or o.get('session_id') or ''
    if o.get('type') in ('user', 'assistant') and isinstance(o.get('message'), dict):      # Claude Code
        m = o['message']
        return m.get('role') or o['type'], _text_of(m.get('content')), ts, sid
    if o.get('type') == 'response_item' and isinstance(o.get('payload'), dict):            # Codex rollout
        p = o['payload']
        if p.get('type') == 'message':
            return p.get('role', ''), _text_of(p.get('content')), ts, sid
        return None
    if o.get('type') == 'message' and 'role' in o:                                          # old Codex
        return o.get('role', ''), _text_of(o.get('content')), ts, sid
    if o.get('type') in ('user', 'gemini', 'model') and 'content' in o:                     # Gemini CLI
        return ('user' if o['type'] == 'user' else 'assistant'), _text_of(o.get('content')), ts, sid
    if o.get('role') in ('user', 'assistant', 'model') and ('content' in o or 'text' in o):  # generic
        return o['role'], _text_of(o.get('content', o.get('text'))), ts, sid
    return None


def _iter_objects(path):
    if path.suffix == '.jsonl':
        with path.open('r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('{'):
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
    else:                                                   # .json: a chat file with a list of messages
        try:
            data = json.loads(path.read_text(encoding='utf-8', errors='replace'))
        except ValueError:
            return
        msgs = data.get('messages') if isinstance(data, dict) else data
        if isinstance(msgs, list):
            sid = data.get('sessionId', '') if isinstance(data, dict) else ''
            for m in msgs:
                if isinstance(m, dict):
                    m.setdefault('sessionId', sid)
                    yield m


def parse(path):
    out, title, session = [], '', ''
    for o in _iter_objects(path):
        if isinstance(o, dict) and o.get('type') == 'session_meta':                          # Codex header
            session = (o.get('payload') or {}).get('id', '') or session
        m = _msg(o)
        if not m:
            continue
        role, text, ts, sid = m
        session = session or sid
        if role in SKIP_ROLES or not text or len(text.strip()) < 3:
            continue
        if text.lstrip().startswith(NOISE_PREFIXES):          # injected instructions, not the human
            continue
        text = SECRET_RE.sub('[secret]', text.strip())[:MAX_TEXT]
        if role == 'user' and not title and not text.startswith('<'):
            title = text.splitlines()[0][:120]
        out.append((role, text, ts))
    return session or path.stem, title, out


# ---------- ingest ----------

def ingest(quiet=False):
    lock = Path(str(DB) + '.lock')
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 1800:
            print('another ingest is running, skip')
            return 0
        lock.unlink()
        return ingest(quiet)
    t0, cutoff = time.time(), time.time() - DAYS * 86400
    con = connect()
    try:
        known = {p: (m, s) for p, m, s in con.execute('SELECT path, mtime, size FROM files')}
        seen, changed = set(), 0
        for source, root in roots():
            for path in root.rglob('*'):
                if path.suffix not in ('.jsonl', '.json') or not path.is_file():
                    continue
                if source == 'extra' and path.suffix == '.json' and 'chat' not in str(path).lower():
                    continue
                st = path.stat()
                if st.st_mtime < cutoff:
                    continue
                key = str(path)
                seen.add(key)
                if known.get(key) == (st.st_mtime, st.st_size):
                    continue
                session, title, msgs = parse(path)
                _drop(con, key)
                for role, text, ts in msgs:
                    cur = con.execute('INSERT INTO rows(path, source, session, role, ts, title) VALUES(?,?,?,?,?,?)',
                                      (key, source, session, role, ts, title))
                    con.execute('INSERT INTO fts(rowid, text) VALUES(?,?)', (cur.lastrowid, text))
                con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?)', (key, st.st_mtime, st.st_size, source))
                changed += 1
        for key in set(known) - seen:                       # aged out of the window or deleted
            _drop(con, key)
            con.execute('DELETE FROM files WHERE path=?', (key,))
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_ingest', ?)", (str(time.time()),))
        con.commit()
        if not quiet:
            print('ok: %d files in window, %d re-indexed, %.1fs' % (len(seen), changed, time.time() - t0))
        return 0
    finally:
        con.close()
        lock.unlink(missing_ok=True)


def _drop(con, key):
    rids = [r for (r,) in con.execute('SELECT rid FROM rows WHERE path=?', (key,))]
    con.executemany('DELETE FROM fts WHERE rowid=?', [(r,) for r in rids])
    con.execute('DELETE FROM rows WHERE path=?', (key,))


# ---------- search ----------

def _fts_query(q):
    """Words -> prefix terms. Long words are cut to 5 letters: a crude stemmer that catches word forms
    in Russian and other inflected languages (памяти/память, sessions/session)."""
    terms = []
    for w in re.findall(r'\w+', q.lower()):
        if len(w) < 3:
            continue
        terms.append('"%s"*' % (w[:5] if len(w) > 6 else w))
    return ' OR '.join(terms)


def search(q, n=8):
    if not DB.exists():
        return [], None
    con = connect()
    try:
        last = con.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone()
        fq = _fts_query(q)
        if not fq:
            return [], last and float(last[0])
        rows = con.execute('''SELECT r.path, r.source, r.session, r.ts, r.title, bm25(fts) AS s,
                                     snippet(fts, 0, '«', '»', '…', 14)
                              FROM fts JOIN rows r ON r.rid = fts.rowid
                              WHERE fts MATCH ? ORDER BY s LIMIT 400''', (fq,)).fetchall()
    finally:
        con.close()
    best, now = {}, time.time()
    for path, source, session, ts, title, s, snip in rows:
        age = _age_days(ts, path, now)
        score = -s / (1 + age / 7.0)                       # relevance, gently decayed by age
        b = best.get(session)
        if b is None:
            best[session] = {'score': score, 'hits': 1, 'path': path, 'source': source, 'ts': ts,
                             'title': title, 'snippet': snip}
        else:
            b['hits'] += 1
            if score > b['score']:
                b.update(score=score, snippet=snip, ts=ts)
    ranked = sorted(best.values(), key=lambda b: b['score'] * (1 + 0.1 * min(b['hits'], 10)), reverse=True)
    return ranked[:n], last and float(last[0])


def _age_days(ts, path, now):
    try:
        return max(0.0, (now - time.mktime(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S'))) / 86400)
    except (ValueError, TypeError):
        try:
            return (now - os.path.getmtime(path)) / 86400
        except OSError:
            return DAYS


def _fmt(hits, last):
    age = '%d min ago' % ((time.time() - last) / 60) if last else 'never'
    lines = ['hot search (last %d days, index built %s):' % (DAYS, age)]
    for h in hits:
        lines.append('- [%s · %s] %s | %s | %s' % (h['source'], (h['ts'] or '')[:16], h['title'][:80],
                                                    h['snippet'].replace('\n', ' '), h['path']))
    return '\n'.join(lines)


# ---------- Claude Code hook ----------

def hook():
    """UserPromptSubmit hook. Searches once per session (first real prompt). Never breaks the prompt."""
    try:
        data = json.load(sys.stdin)
        prompt, sid = (data.get('prompt') or '').strip(), data.get('session_id') or 'x'
        mark = DB.parent / 'seen' / re.sub(r'[^\w-]', '_', sid)
        if len(prompt) < 12 or prompt.startswith('/') or mark.exists():
            return 0
        hits, last = search(prompt, n=3)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(str(time.time()))
        if hits:
            ctx = _fmt(hits, last) + '\nMaybe this was already done: check before starting.'
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': ctx}}))
    except Exception:
        pass
    return 0


def stats():
    if not DB.exists():
        print('no index yet: run  python3 hotmem.py ingest')
        return 1
    con = connect()
    for source, files, rows in con.execute('''SELECT f.source, COUNT(DISTINCT f.path), COUNT(r.rid)
                                               FROM files f LEFT JOIN rows r ON r.path=f.path GROUP BY f.source'''):
        print('%-7s files=%-6d messages=%d' % (source, files, rows))
    last = con.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone()
    print('last ingest: %s' % (time.strftime('%Y-%m-%d %H:%M', time.localtime(float(last[0]))) if last else 'never'))
    print('roots: ' + ', '.join(str(p) for _, p in roots()))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'help'
    if cmd == 'ingest':
        return ingest()
    if cmd == 'search' and len(argv) > 2:
        hits, last = search(' '.join(argv[2:]))
        print(_fmt(hits, last) if hits else 'nothing found in the last %d days' % DAYS)
        return 0
    if cmd == 'hook':
        return hook()
    if cmd == 'stats':
        return stats()
    print(__doc__)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
