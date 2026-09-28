"""Database-backed authentication; no credentials are logged or put in URLs."""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import closing
from uuid import uuid4

from flask import abort, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

COOKIE = 'pds_session'
SESSION_SECONDS = 8 * 60 * 60
ANONYMOUS_SECONDS = 30 * 60


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def migrate(db, path):
    """Back up a consistent snapshot before changing an existing legacy schema.

    Run startup/migration with other writers stopped. NULL owners are quarantined.
    """
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plans'").fetchone()
    if exists and 'owner_id' not in {r[1] for r in db.execute('PRAGMA table_info(plans)')}:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        populated = any(db.execute('SELECT 1 FROM "' + t.replace('"', '""') + '" LIMIT 1').fetchone() for t in tables)
        if populated:
            folder = path.parent / 'backups'
            folder.mkdir(exist_ok=True)
            backup = folder / (path.name + '.' + uuid4().hex + '.bak')
            with closing(sqlite3.connect(backup)) as target:
                db.backup(target)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY NOT NULL,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY NOT NULL,
            user_id TEXT REFERENCES users(id),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_user ON auth_sessions(user_id);
    """)
    if exists and 'owner_id' not in {r[1] for r in db.execute('PRAGMA table_info(plans)')}:
        with db:
            db.execute('ALTER TABLE plans ADD COLUMN owner_id TEXT REFERENCES users(id)')


def register_auth(app, get_db):
    # Equal-cost verification also for unknown usernames; never a usable account.
    dummy_hash = generate_password_hash(secrets.token_urlsafe(32), method='scrypt')

    def issue(user_id=None):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        lifetime = SESSION_SECONDS if user_id else ANONYMOUS_SECONDS
        db = get_db()
        with db:
            if getattr(g, 'session_hash', None):
                db.execute('DELETE FROM auth_sessions WHERE token_hash = ?', (g.session_hash,))
            db.execute('DELETE FROM auth_sessions WHERE expires_at <= ?', (now,))
            db.execute('INSERT INTO auth_sessions VALUES (?, ?, ?, ?)', (digest(token), user_id, now, now + lifetime))
        g.new_token = token
        g.session_hash = digest(token)
        g.csrf_token = digest('csrf:' + token)
        g.cookie_lifetime = lifetime

    @app.before_request
    def authenticate():
        g.user = None
        g.csrf_token = ''
        g.session_hash = None
        if request.endpoint == 'static':
            return
        token = request.cookies.get(COOKIE, '')
        if token and len(token) <= 128:
            row = get_db().execute('SELECT * FROM auth_sessions WHERE token_hash = ? AND expires_at > ?',
                                   (digest(token), int(time.time()))).fetchone()
            if row:
                g.session_hash = row['token_hash']
                g.csrf_token = digest('csrf:' + token)
                if row['user_id']:
                    g.user = get_db().execute('SELECT id, username FROM users WHERE id = ?', (row['user_id'],)).fetchone()
        public = request.endpoint in ('login', 'signup')
        if not public and not g.user:
            return redirect(url_for('login'), code=303)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
            if not g.session_hash or not hmac.compare_digest(g.csrf_token.encode(), supplied.encode()):
                return render_template('error.html', message='요청을 확인할 수 없습니다. 화면을 새로고침해 다시 시도해 주세요.'), 403
        elif public and not g.session_hash:
            issue()
        if g.user:
            # Validate all resource references, including body/query IDs, before
            # any endpoint can read, replay an idempotency receipt, or write.
            checks = {
                'plan_id': 'SELECT owner_id FROM plans WHERE id = ?',
                'task_id': 'SELECT p.owner_id FROM tasks t JOIN plans p ON p.id=t.plan_id WHERE t.id = ?',
                'record_id': 'SELECT p.owner_id FROM execution_records e JOIN tasks t ON t.id=e.task_id JOIN plans p ON p.id=t.plan_id WHERE e.id = ?',
                'source_improvement': 'SELECT p.owner_id FROM review_improvements i JOIN plans p ON p.id=i.plan_id WHERE i.id = ?',
                'rule_change_id': 'SELECT p.owner_id FROM review_rule_changes r JOIN plans p ON p.id=r.plan_id WHERE r.id = ?',
                'request_id': None,
            }
            for key, sql in checks.items():
                values = request.args.getlist(key) + request.form.getlist(key)
                if request.view_args and key in request.view_args:
                    values.append(request.view_args[key])
                for value in values:
                    # Forms normalize identifiers before using them. Apply the
                    # same normalization here so padded IDs cannot bypass checks.
                    value = value.strip()
                    if not value:
                        continue
                    if sql:
                        owner = get_db().execute(sql, (value,)).fetchone()
                        if owner and owner['owner_id'] != g.user['id']:
                            abort(404)
                    else:
                        for table in ('task_state_requests', 'execution_records'):
                            owner = get_db().execute('SELECT p.owner_id FROM ' + table + ' r JOIN tasks t ON t.id=r.task_id JOIN plans p ON p.id=t.plan_id WHERE r.request_id=?', (value,)).fetchone()
                            if owner and owner['owner_id'] != g.user['id']:
                                abort(404)

    @app.after_request
    def session_cookie(response):
        if getattr(g, 'clear_cookie', False):
            response.delete_cookie(COOKIE, path='/', secure=app.config['SESSION_COOKIE_SECURE'] or request.is_secure, httponly=True, samesite='Lax')
        elif getattr(g, 'new_token', None):
            response.set_cookie(COOKIE, g.new_token, max_age=g.cookie_lifetime, path='/',
                                secure=app.config['SESSION_COOKIE_SECURE'] or request.is_secure, httponly=True, samesite='Lax')
        return response

    @app.context_processor
    def auth_context():
        return {'current_user': getattr(g, 'user', None), 'csrf_token': getattr(g, 'csrf_token', '')}

    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip().lower()
            password = request.form.get('password', '')
            if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
                error = '아이디는 영문 소문자·숫자·밑줄 3~32자로 입력해 주세요.'
            elif not 12 <= len(password) <= 128:
                error = '비밀번호는 12~128자로 입력해 주세요.'
            else:
                hashed = generate_password_hash(password, method='scrypt')
                try:
                    with get_db() as db:
                        db.execute('INSERT INTO users VALUES (?, ?, ?, ?)', (str(uuid4()), username, hashed, int(time.time())))
                except sqlite3.IntegrityError:
                    error = '사용할 수 없는 아이디입니다.'
                else:
                    return redirect(url_for('login'), code=303)
        return render_template('auth.html', mode='signup', error=error), 400 if error else 200

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip().lower()
            password = request.form.get('password', '')
            db = get_db()
            # Serialize credential verification with password changes, so a stale
            # credential cannot mint a session after its password was replaced.
            with db:
                db.execute('BEGIN IMMEDIATE')
                user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
                valid = check_password_hash(user['password_hash'] if user else dummy_hash, password[:129])
                if valid and user and len(password) <= 128:
                    issue(user['id'])
                    return redirect(url_for('index'), code=303)
            error = '아이디 또는 비밀번호가 올바르지 않습니다.'
        return render_template('auth.html', mode='login', error=error), 400 if error else 200

    @app.post('/logout')
    def logout():
        with get_db() as db:
            db.execute('DELETE FROM auth_sessions WHERE token_hash = ?', (g.session_hash,))
        g.clear_cookie = True
        return redirect(url_for('login'), code=303)

    @app.route('/account/password', methods=['GET', 'POST'])
    def change_password():
        error = None
        if request.method == 'POST':
            old = request.form.get('current_password', '')
            new = request.form.get('password', '')
            db = get_db()
            with db:
                db.execute('BEGIN IMMEDIATE')
                user = db.execute('SELECT * FROM users WHERE id = ?', (g.user['id'],)).fetchone()
                if len(old) > 128 or not check_password_hash(user['password_hash'], old):
                    error = '현재 비밀번호가 올바르지 않습니다.'
                elif not 12 <= len(new) <= 128:
                    error = '새 비밀번호는 12~128자로 입력해 주세요.'
                else:
                    db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(new, method='scrypt'), g.user['id']))
                    db.execute('DELETE FROM auth_sessions WHERE user_id = ?', (g.user['id'],))
                    g.clear_cookie = True
                    return redirect(url_for('login'), code=303)
        return render_template('auth.html', mode='password', error=error), 400 if error else 200


    @app.route('/account/delete', methods=['GET', 'POST'])
    def delete_account():
        error = None
        status = 200
        if request.method == 'POST':
            password = request.form.get('current_password', '')
            db = get_db()
            try:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    user = db.execute('SELECT * FROM users WHERE id = ?', (g.user['id'],)).fetchone()
                    if not user or len(password) > 128 or not check_password_hash(user['password_hash'], password):
                        error = '현재 비밀번호가 올바르지 않습니다.'
                    elif request.form.get('confirm_delete') != 'yes':
                        error = '삭제 범위와 복구 불가 안내에 동의해 주세요.'
                    else:
                        # Only the authenticated identity determines the target.
                        # RESTRICT foreign keys fail closed on inconsistent cross-owner links.
                        owner = (g.user['id'],)
                        plans = 'SELECT id FROM plans WHERE owner_id = ?'
                        tasks = 'SELECT id FROM tasks WHERE plan_id IN (' + plans + ')'
                        rules = 'SELECT id FROM review_rule_changes WHERE plan_id IN (' + plans + ')'
                        db.execute('DELETE FROM review_rule_evidence WHERE change_id IN (' + rules + ')', owner)
                        db.execute('DELETE FROM review_rule_changes WHERE plan_id IN (' + plans + ')', owner)
                        db.execute('DELETE FROM next_plan_links WHERE next_plan_id IN (' + plans + ') AND previous_plan_id IN (' + plans + ')', owner + owner)
                        db.execute('DELETE FROM review_improvements WHERE plan_id IN (' + plans + ')', owner)
                        for table in ('task_completion_events', 'task_state_requests', 'task_tags', 'execution_records'):
                            db.execute('DELETE FROM ' + table + ' WHERE task_id IN (' + tasks + ')', owner)
                        db.execute('DELETE FROM tasks WHERE plan_id IN (' + plans + ')', owner)
                        db.execute('DELETE FROM plan_versions WHERE plan_id IN (' + plans + ')', owner)
                        db.execute('DELETE FROM plans WHERE owner_id = ?', owner)
                        db.execute('DELETE FROM auth_sessions WHERE user_id = ?', owner)
                        db.execute('DELETE FROM users WHERE id = ?', owner)
                if not error:
                    g.clear_cookie = True
                    return redirect(url_for('login'), code=303)
                status = 400
            except sqlite3.DatabaseError:
                # The context manager rolled back every deletion, including sessions.
                error = '탈퇴를 완료하지 못했습니다. 계정과 자료는 삭제되지 않았습니다. 잠시 후 다시 시도해 주세요.'
                status = 503
        return render_template('account_delete.html', error=error), status
