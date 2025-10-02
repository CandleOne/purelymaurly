import os, sqlite3, uuid, secrets, datetime as dt, traceback, hashlib, hmac
import json
try:
    import stripe
except ImportError:
    stripe = None
from flask import Flask, g, request, jsonify, make_response, render_template, send_from_directory, abort, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from pathlib import Path
from functools import wraps

BASE_DIR = Path(__file__).resolve().parent
# Allow overriding DB path in production (e.g., mounted disk). Defaults to backend/app.db for local dev.
DB_PATH = Path(os.environ.get('DATABASE_PATH', str(BASE_DIR / 'app.db')))
SCHEMA_PATH = BASE_DIR / 'schema.sql'
FRONTEND_DIR = BASE_DIR.parent  # project root containing index.html & assets

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'dev-secret-key')
# Simple admin password (fallback) – set ENV ADMIN_PANEL_PASSWORD to override
ADMIN_PANEL_PASSWORD = os.environ.get('ADMIN_PANEL_PASSWORD', 'changeme')
app.config['JSON_SORT_KEYS'] = False
app.config['SESSION_COOKIE_NAME'] = 'pm_session'
SESSION_TTL_HOURS = 24
def _load_dotenv():
    """Lightweight .env loader (KEY=VALUE lines). Does not override existing env vars."""
    env_path = BASE_DIR.parent / '.env'
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding='utf-8').splitlines():
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k,v = line.split('=',1)
            k=k.strip(); v=v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k]=v
    except Exception:
        print('[env] Failed to parse .env file')

_load_dotenv()
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')  # expected in environment or .env
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    # Warn once at startup if missing
    print('[checkout] WARNING: STRIPE_SECRET_KEY not set; /api/checkout/session will return an error until configured.')

# Optional Stripe automatic tax (disabled by default because Stripe requires a configured origin
# address in the dashboard test settings. Enable by setting ENABLE_AUTOMATIC_TAX=1 in your env.)
ENABLE_AUTOMATIC_TAX = os.environ.get('ENABLE_AUTOMATIC_TAX','0').lower() in {'1','true','yes','on'}

# ---------- Database Helpers ----------

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        # Ensure foreign key cascades are enforced (needed for deleting users)
        g.db.execute('PRAGMA foreign_keys = ON')
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
        db.executescript(f.read())
    db.commit()
    ensure_is_admin_column(db)

# Run initialization on first start
if not DB_PATH.exists():
    with app.app_context():
        init_db()

# ---------- Utility ----------

def now():
    return dt.datetime.utcnow()

def hash_password(password: str) -> str:
    return generate_password_hash(password, method='pbkdf2:sha256', salt_length=16)

def verify_password(password: str, hashed: str) -> bool:
    try:
        return check_password_hash(hashed, password)
    except Exception:
        return False

def create_session(user_id: int, user_agent: str, ip: str):
    token = secrets.token_urlsafe(40)
    expires = now() + dt.timedelta(hours=SESSION_TTL_HOURS)
    db = get_db()
    db.execute("INSERT INTO sessions (user_id, token, user_agent, ip_address, expires_at) VALUES (?,?,?,?,?)",
               (user_id, token, user_agent[:180], ip, expires.isoformat()))
    db.commit()
    return token, expires

def get_session(token: str):
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    # expire check
    if dt.datetime.fromisoformat(row['expires_at']) < now():
        db.execute("DELETE FROM sessions WHERE id=?", (row['id'],))
        db.commit()
        return None
    return row

def session_user(token: str):
    s = get_session(token)
    if not s: return None
    db = get_db()
    # Try include is_admin if present
    try:
        return db.execute("SELECT id, uuid, email, name, created_at, is_admin FROM users WHERE id=?", (s['user_id'],)).fetchone()
    except Exception:
        return db.execute("SELECT id, uuid, email, name, created_at FROM users WHERE id=?", (s['user_id'],)).fetchone()

# ---------- Error / Response Helpers ----------

def api_error(message, status=400):
    return jsonify({ 'ok': False, 'error': message }), status

def api_ok(**data):
    resp = { 'ok': True }
    resp.update(data)
    return jsonify(resp)

# ---------- Middleware-like ----------

def current_user():
    token = request.cookies.get('pm_session') or request.headers.get('Authorization','').replace('Bearer ','')
    if not token:
        return None
    return session_user(token)

def admin_required(view):
    @wraps(view)
    def inner(*args, **kwargs):
        # Simple cookie flag check
        if request.cookies.get('admin_auth') == '1':
            return view(*args, **kwargs)
        # Allow override via header for scripting (not recommended for prod)
        if request.headers.get('X-Admin-Password') == ADMIN_PANEL_PASSWORD:
            resp = make_response(view(*args, **kwargs))
            resp.set_cookie('admin_auth', '1', httponly=True, samesite='Lax')
            return resp
        # API vs HTML response handling
        if request.path.startswith('/api/'):
            return api_error('Admin auth required', 403)
        return redirect(url_for('admin_login', next=request.path))
    return inner

def ensure_is_admin_column(db=None):
    db = db or get_db()
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
        if 'is_admin' not in cols:
            db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception:
        traceback.print_exc()

def ensure_coupons_table(db=None):
    """Create per-user coupons table if missing (idempotent)."""
    db = db or get_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS coupons (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT NOT NULL UNIQUE,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              discount_type TEXT NOT NULL CHECK(discount_type IN ('percent','fixed')),
              value REAL NOT NULL,
              max_uses INTEGER NOT NULL DEFAULT 1,
              uses INTEGER NOT NULL DEFAULT 0,
              expires_at DATETIME,
              active INTEGER NOT NULL DEFAULT 1,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_coupons_user ON coupons(user_id)")
        db.commit()
    except Exception:
        traceback.print_exc()

# ---------- Routes ----------

@app.route('/api/health')
def health():
    return api_ok(status='healthy')

@app.route('/api/checkout/config')
def checkout_config():
    """Expose publishable key so frontend can use Stripe.js redirect (improves redirect reliability)."""
    if not stripe or not STRIPE_PUBLISHABLE_KEY:
        return api_ok(enabled=False)
    return api_ok(enabled=True, publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not name or not email or not password:
        return api_error('Missing required fields')
    if len(password) < 6:
        return api_error('Password too short (min 6)')
    db = get_db()
    if db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
        return api_error('Email already registered')
    user_uuid = str(uuid.uuid4())
    ensure_is_admin_column()
    db.execute('INSERT INTO users (uuid, email, password_hash, name) VALUES (?,?,?,?)',
               (user_uuid, email, hash_password(password), name))
    db.commit()
    user_id = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()['id']
    total = db.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
    if total == 1:
        db.execute('UPDATE users SET is_admin=1 WHERE id=?', (user_id,))
        db.commit()
    token, expires = create_session(user_id, request.headers.get('User-Agent',''), request.remote_addr or '')
    # Include is_admin if column present
    try:
        is_admin = db.execute('SELECT is_admin FROM users WHERE id=?', (user_id,)).fetchone()['is_admin']
    except Exception:
        is_admin = 0
    resp = make_response(api_ok(user={ 'uuid': user_uuid, 'email': email, 'name': name, 'is_admin': bool(is_admin) }))
    resp.set_cookie('pm_session', token, httponly=True, secure=False, samesite='Lax', expires=expires)
    return resp

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password:
        return api_error('Missing credentials')
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    if not user or not verify_password(password, user['password_hash']):
        return api_error('Invalid email or password', 401)
    token, expires = create_session(user['id'], request.headers.get('User-Agent',''), request.remote_addr or '')
    # Provide is_admin flag if present
    is_admin = user['is_admin'] if 'is_admin' in user.keys() else 0
    resp = make_response(api_ok(user={ 'uuid': user['uuid'], 'email': user['email'], 'name': user['name'], 'is_admin': bool(is_admin) }))
    resp.set_cookie('pm_session', token, httponly=True, secure=False, samesite='Lax', expires=expires)
    return resp

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    token = request.cookies.get('pm_session')
    if token:
        db = get_db()
        db.execute('DELETE FROM sessions WHERE token=?', (token,))
        db.commit()
    resp = make_response(api_ok(message='Logged out'))
    resp.set_cookie('pm_session','', expires=0)
    return resp

@app.route('/api/auth/me')
def me():
    user = current_user()
    if not user: return api_error('Not authenticated', 401)
    u = dict(user)
    # Normalize possible absence of is_admin
    if 'is_admin' not in u: u['is_admin'] = 0
    return api_ok(user=u)

# Example protected route
@app.route('/api/account/preferences', methods=['GET','PATCH'])
def preferences():
    user = current_user()
    if not user: return api_error('Not authenticated', 401)
    db = get_db()
    if request.method == 'GET':
        prof = db.execute('SELECT * FROM user_profiles WHERE user_id=?', (user['id'],)).fetchone()
        return api_ok(preferences=dict(prof) if prof else {})
    data = request.get_json(force=True, silent=True) or {}
    allowed = { 'display_name','avatar_url','bio','marketing_opt_in','theme' }
    updates = { k:v for k,v in data.items() if k in allowed }
    if not updates:
        return api_error('No valid fields to update')
    placeholders = ', '.join(f"{k}=?" for k in updates)
    values = list(updates.values())
    values.append(user['id'])
    # Upsert style
    existing = db.execute('SELECT 1 FROM user_profiles WHERE user_id=?',(user['id'],)).fetchone()
    if existing:
        db.execute(f'UPDATE user_profiles SET {placeholders}, updated_at=CURRENT_TIMESTAMP WHERE user_id=?', values)
    else:
        base_cols = ', '.join(updates.keys())
        q_marks = ', '.join('?' for _ in updates)
        db.execute(f'INSERT INTO user_profiles (user_id, {base_cols}) VALUES (?, {q_marks})', [user['id'], *updates.values()])
    db.commit()
    return api_ok(updated=True)

@app.route('/api/account/password', methods=['POST'])
def change_password():
    """Authenticated password change: verifies current password, enforces basic policy, rotates session, logs audit."""
    user = current_user()
    if not user:
        return api_error('Not authenticated', 401)
    data = request.get_json(force=True, silent=True) or {}
    current_pwd = data.get('current_password') or ''
    new_pwd = data.get('new_password') or ''
    if not current_pwd or not new_pwd:
        return api_error('Missing required fields')
    if len(new_pwd) < 6:
        return api_error('New password too short (min 6)')
    db = get_db()
    row = db.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
    if not row or not verify_password(current_pwd, row['password_hash']):
        return api_error('Current password incorrect', 400)
    # Prevent re-use of same password
    if verify_password(new_pwd, row['password_hash']):
        return api_error('New password must be different from current one')
    # Update password
    db.execute('UPDATE users SET password_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (hash_password(new_pwd), user['id']))
    # Invalidate all prior sessions for this user
    db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
    # Audit log
    try:
        db.execute('INSERT INTO audit_log (user_id, action, details, ip_address) VALUES (?,?,?,?)', (user['id'], 'password_change', 'Password updated', request.remote_addr or ''))
    except Exception:
        pass
    db.commit()
    # New session
    new_token, expires = create_session(user['id'], request.headers.get('User-Agent',''), request.remote_addr or '')
    resp = make_response(api_ok(changed=True))
    resp.set_cookie('pm_session', new_token, httponly=True, secure=False, samesite='Lax', expires=expires)
    return resp

# ---------- Coupons (Per-User) ----------

def _gen_coupon_code(db, length=8, attempts=10):
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(attempts):
        code = ''.join(secrets.choice(alphabet) for _ in range(length))
        if not db.execute('SELECT 1 FROM coupons WHERE code=?', (code,)).fetchone():
            return code
    raise RuntimeError('Could not generate unique coupon code')

def _consume_coupon_by_user_id(code: str, user_id: int) -> bool:
    """Increment uses for a coupon for a given user; persist the row and mark inactive when exhausted.
    Returns True if a coupon row was found and updated.
    """
    if not code or not user_id:
        return False
    db = get_db(); ensure_coupons_table(db)
    row = db.execute('SELECT id, uses, max_uses FROM coupons WHERE code=? AND user_id=?', (code.upper(), user_id)).fetchone()
    if not row:
        return False
    new_uses = int(row['uses'] or 0) + 1
    # Persist coupon record; when reaching max uses, set active=0 (but keep for history)
    if new_uses >= int(row['max_uses'] or 1):
        db.execute('UPDATE coupons SET uses=?, active=0 WHERE id=?', (new_uses, row['id']))
    else:
        db.execute('UPDATE coupons SET uses=? WHERE id=?', (new_uses, row['id']))
    db.commit()
    return True

def _consume_coupon_by_uuid(code: str, user_uuid: str) -> bool:
    if not code or not user_uuid:
        return False
    db = get_db(); ensure_coupons_table(db)
    u = db.execute('SELECT id FROM users WHERE uuid=?', (user_uuid,)).fetchone()
    if not u:
        return False
    return _consume_coupon_by_user_id(code, u['id'])

@app.route('/api/account/coupons')
def my_coupons():
    user = current_user()
    if not user:
        return api_error('Not authenticated', 401)
    db = get_db(); ensure_coupons_table(db)
    # Return all coupons for the user (active, used, or expired) so they persist in account history
    rows = db.execute(
        """SELECT code, discount_type, value, max_uses, uses, expires_at, active
             FROM coupons
             WHERE user_id=?
             ORDER BY id DESC
             LIMIT 100""",
        (user['id'],)
    ).fetchall()
    coupons = [dict(r) for r in rows]
    return api_ok(coupons=coupons)

@app.route('/api/admin/coupons', methods=['POST','GET'])
@admin_required
def admin_coupons():
    db = get_db(); ensure_coupons_table(db)
    if request.method == 'GET':
        # optional filter by user (email or uuid)
        user_filter = request.args.get('user')
        q = ("SELECT c.id, c.code, c.discount_type, c.value, c.max_uses, c.uses, c.expires_at, c.active, u.email, u.uuid"
             " FROM coupons c JOIN users u ON c.user_id=u.id")
        params = []
        if user_filter:
            q += " WHERE u.email=? OR u.uuid=?"
            params.extend([user_filter, user_filter])
        q += " ORDER BY c.id DESC LIMIT 200"
        rows = [dict(r) for r in db.execute(q, params).fetchall()]
        return api_ok(coupons=rows)
    data = request.get_json(force=True, silent=True) or {}
    identifier = (data.get('user') or '').strip()
    discount_type = (data.get('discount_type') or 'percent').lower()
    value = data.get('value')
    max_uses = int(data.get('max_uses') or 1)
    expires_at = data.get('expires_at') or None
    code = (data.get('code') or '').strip().upper()
    if discount_type not in {'percent','fixed'}:
        return api_error('discount_type must be percent or fixed')
    try:
        value = float(value)
    except Exception:
        return api_error('Invalid value')
    if value <= 0:
        return api_error('Value must be > 0')
    dbu = None
    if identifier:
        dbu = db.execute('SELECT id FROM users WHERE email=? COLLATE NOCASE', (identifier,)).fetchone()
        if not dbu:
            dbu = db.execute('SELECT id FROM users WHERE uuid=?', (identifier,)).fetchone()
    if not dbu:
        return api_error('User not found')
    user_id = dbu['id']
    if not code:
        code = _gen_coupon_code(db)
    else:
        # ensure uniqueness
        if db.execute('SELECT 1 FROM coupons WHERE code=?', (code,)).fetchone():
            return api_error('Code already exists')
    # Basic value limits
    if discount_type == 'percent' and value > 100:
        return api_error('Percent too high (max 90)')
    if discount_type == 'fixed' and value > 1000:
        return api_error('Fixed value too high')
    # Insert
    db.execute('INSERT INTO coupons (code, user_id, discount_type, value, max_uses, expires_at) VALUES (?,?,?,?,?,?)',
               (code, user_id, discount_type, value, max_uses, expires_at))
    try:
        db.execute('INSERT INTO audit_log (user_id, action, details, ip_address) VALUES (?,?,?,?)', (user_id, 'coupon_assign', f'Code {code}', request.remote_addr or ''))
    except Exception:
        pass
    db.commit()
    return api_ok(created=True, code=code)

@app.route('/admin/coupons')
@admin_required
def admin_coupons_page():
    # Lightweight HTML tool for testing coupon assignment
    ensure_coupons_table()
    return render_template('coupons.html')

# ---------- Surveys (award coupon on completion) ----------

@app.route('/api/survey/essay', methods=['POST'])
def survey_essay_submit():
    """Accept an essay response from an authenticated user and award a one-time 10% off coupon.
    Safeguards:
      - Requires login
      - Minimum essay length check
      - One reward per user (returns existing code if already created)
    """
    user = current_user()
    if not user:
        return api_error('Not authenticated', 401)
    data = request.get_json(silent=True) or {}
    essay = (data.get('essay') or '').strip()
    if len(essay) < 200:  # simple content-length guard
        return api_error('Essay is too short (min 200 characters)')

    db = get_db(); ensure_coupons_table(db)
    # If user already has an essay reward coupon, return it instead of creating a new one
    existing = db.execute(
        """
        SELECT code FROM coupons
         WHERE user_id=? AND active=1 AND max_uses=1 AND uses<1
           AND discount_type='percent' AND value=10
           AND code LIKE 'ESSAY10-%'
         ORDER BY id DESC LIMIT 1
        """,
        (user['id'],)
    ).fetchone()
    if existing:
        return api_ok(already_awarded=True, code=existing['code'])

    # Create a unique code with a recognizable prefix
    try:
        prefix = 'ESSAY10-'
        # Attempt to generate a unique code with prefix
        for _ in range(20):
            suffix = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
            code = prefix + suffix
            clash = db.execute('SELECT 1 FROM coupons WHERE code=?', (code,)).fetchone()
            if not clash:
                break
        else:
            # Fallback to generic generator if prefix collisions (very unlikely)
            code = _gen_coupon_code(db)
        # Optional expiry (e.g., 60 days from now). Comment out to have no expiry.
        expires_at = (now() + dt.timedelta(days=60)).isoformat()
        db.execute(
            'INSERT INTO coupons (code, user_id, discount_type, value, max_uses, expires_at) VALUES (?,?,?,?,?,?)',
            (code, user['id'], 'percent', 10.0, 1, expires_at)
        )
        try:
            db.execute(
                'INSERT INTO audit_log (user_id, action, details, ip_address) VALUES (?,?,?,?)',
                (user['id'], 'survey_essay_reward', json.dumps({'code': code}), request.remote_addr or '')
            )
        except Exception:
            pass
        db.commit()
        return api_ok(awarded=True, code=code, expires_at=expires_at)
    except Exception as e:
        traceback.print_exc()
        return api_error('Failed to award coupon')

# ---------- Checkout (Stripe) ----------

def _price_from_cart_item(item):
    # Expect {name, price, qty}
    unit_amount = int(round(float(item['price']) * 100))
    if unit_amount < 50:
        unit_amount = 50  # Stripe minimum 50 cents in many regions
    return {
        'price_data': {
            'currency': 'usd',
            'product_data': { 'name': item.get('name','Item')[:120] },
            'unit_amount': unit_amount,
        },
        'quantity': int(item.get('qty') or 1)
    }

def _validate_coupon(code: str, user, cart_subtotal_cents: int):
    """Validate a coupon for the current user.
    Returns dict with keys (code, discount_type, value, discount_cents) or None if invalid.
    Does NOT increment uses – that should happen when an order completes (webhook).
    """
    if not code or not user:
        return None
    db = get_db(); ensure_coupons_table(db)
    now_iso = now().isoformat()
    row = db.execute(
        """SELECT code, discount_type, value, max_uses, uses, expires_at, active
               FROM coupons
              WHERE code=? AND user_id=? AND active=1
                AND (expires_at IS NULL OR expires_at > ?) AND uses < max_uses""",
        (code.upper(), user['id'], now_iso)
    ).fetchone()
    if not row:
        return None
    discount_type = row['discount_type']
    value = float(row['value'])
    if cart_subtotal_cents <= 0:
        return None
    if discount_type == 'percent':
        discount_cents = int(round(cart_subtotal_cents * (value/100.0)))
    else:  # fixed
        discount_cents = int(round(value * 100))
    # Cap discount so it never exceeds the subtotal (allow full 100% off for test/dev scenarios).
    # NOTE: If using Stripe Checkout, a 0 total might require a different fulfillment flow.
    # For now we allow it so percent=100 or fixed==subtotal becomes a valid free checkout scenario.
    discount_cents = min(discount_cents, cart_subtotal_cents)
    if discount_cents <= 0:
        return None
    return {
        'code': row['code'],
        'discount_type': discount_type,
        'value': value,
        'discount_cents': discount_cents
    }

@app.route('/api/checkout/validate-coupon', methods=['POST'])
def validate_coupon():
    user = current_user()
    if not user:
        return api_error('Not authenticated', 401)
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip().upper()
    cart = data.get('cart') or []
    # Compute subtotal from provided cart for context (client authoritative here; final guard happens at session creation)
    subtotal_cents = 0
    for c in cart:
        try:
            price = float(c.get('price'))
            qty = int(c.get('qty') or 1)
            if price > 0 and qty > 0:
                subtotal_cents += int(round(price * 100)) * qty
        except Exception:
            continue
    if subtotal_cents <= 0:
        return api_error('Cart empty')
    coup = _validate_coupon(code, user, subtotal_cents)
    if not coup:
        return api_error('Invalid or expired coupon', 404)
    return api_ok(coupon={
        'code': coup['code'],
        'discount_type': coup['discount_type'],
        'value': coup['value']
    })

@app.route('/api/checkout/session', methods=['POST'])
def create_checkout_session():
    # Allow a lightweight dev fallback when Stripe isn't configured
    allow_fake = os.environ.get('ALLOW_FAKE_CHECKOUT') in {'1','true','yes','on'}
    if not stripe:
        if allow_fake:
            # proceed to fake session below
            pass
        else:
            return api_error('Stripe library not installed', 500)
    if not STRIPE_SECRET_KEY:
        if allow_fake:
            # proceed to fake session below
            pass
        else:
            return api_error('Stripe secret missing (set STRIPE_SECRET_KEY env var or .env)', 500)
    data = request.get_json(silent=True) or {}
    cart = data.get('cart') or []
    if not cart:
        return api_error('Cart empty')
    # basic validation
    safe_items = []
    for c in cart:
        try:
            name = str(c.get('name') or '')[:120]
            price = float(c.get('price'))
            qty = int(c.get('qty') or 1)
        except Exception:
            continue
        if price <= 0 or qty <= 0: continue
        safe_items.append({ 'name': name, 'price': price, 'qty': qty })
    if not safe_items:
        return api_error('No valid items')
    line_items = [_price_from_cart_item(i) for i in safe_items]
    # Determine user & prepare metadata now (needed for coupon logic)
    user = current_user()
    metadata = { 'user_uuid': user['uuid'] if user else 'anon' }
    coupon_code = (data.get('coupon') or '').strip().upper()
    if coupon_code:
        metadata['coupon_attempt'] = coupon_code
    original_subtotal = sum(li['price_data']['unit_amount'] * li['quantity'] for li in line_items)
    applied_coupon = None
    if coupon_code and user:
        coup = _validate_coupon(coupon_code, user, original_subtotal)
        if coup:
            metadata['coupon_code'] = coup['code']
            metadata['coupon_type'] = coup['discount_type']
            metadata['coupon_value'] = str(coup['value'])
            # Adjust line items to reflect discount so Stripe total matches (best-effort; respects min 50 cents)
            discount_remaining = coup['discount_cents']
            if coup['discount_type'] == 'percent':
                # Apply percentage to each item
                for li in line_items:
                    old = li['price_data']['unit_amount']
                    new_amt = int(round(old * (1 - coup['value']/100.0)))
                    if new_amt < 50: new_amt = 50
                    li['price_data']['unit_amount'] = new_amt
                # Due to rounding, recompute effective discount
                new_sub = sum(li['price_data']['unit_amount'] * li['quantity'] for li in line_items)
                metadata['coupon_discount_cents'] = str(original_subtotal - new_sub)
            else:  # fixed
                # Reduce from last item backwards keeping >=50 per unit
                for li in reversed(line_items):
                    if discount_remaining <= 0: break
                    unit = li['price_data']['unit_amount']
                    qty = li['quantity']
                    # Max reducible per unit keeping >=50
                    max_reducible_per_unit = max(0, unit - 50)
                    if max_reducible_per_unit <= 0:
                        continue
                    # Desired per-unit reduction to consume remaining discount evenly
                    total_item_reducible = max_reducible_per_unit * qty
                    take = min(discount_remaining, total_item_reducible)
                    per_unit = take // qty
                    if per_unit <= 0:
                        continue
                    new_unit = unit - per_unit
                    if new_unit < 50: new_unit = 50
                    li['price_data']['unit_amount'] = new_unit
                    discount_remaining -= per_unit * qty
                new_sub = sum(li['price_data']['unit_amount'] * li['quantity'] for li in line_items)
                metadata['coupon_discount_cents'] = str(original_subtotal - new_sub)
            applied_coupon = coup['code']
        else:
            # If invalid coupon was supplied, return error so client can show message
            return api_error('Invalid or expired coupon', 400)
    if applied_coupon is None and coupon_code:
        # No logged in user but coupon provided
        return api_error('Login required to use coupon', 401)
    # Only enable automatic tax if explicitly allowed; otherwise Stripe test mode
    # may reject the request if a tax origin address isn't configured.
    automatic_tax = { 'enabled': ENABLE_AUTOMATIC_TAX }
    # Success / cancel URLs (support overriding from client, else derive)
    # Default success now points to richer confirmation page (can override from client)
    # Use standard session_id param placeholder so Stripe reliably redirects
    success_url = data.get('success_url') or request.host_url.rstrip('/') + '/order-confirmation.html?session_id={CHECKOUT_SESSION_ID}'
    cancel_url = data.get('cancel_url') or request.host_url.rstrip('/') + '/checkout-cancel.html'

    if allow_fake and (not stripe or not STRIPE_SECRET_KEY):
        # Simulate a Stripe session for local development without credentials.
        fake_id = 'cs_test_fake_' + secrets.token_hex(8)
        fake_url = success_url.replace('{CHECKOUT_SESSION_ID}', fake_id)
        # In fake mode, treat as immediately completed and consume coupon now
        try:
            if applied_coupon and user:
                _consume_coupon_by_user_id(applied_coupon, user['id'])
                try:
                    db = get_db()
                    db.execute('INSERT INTO audit_log (user_id, action, details, ip_address) VALUES (?,?,?,?)', (user['id'], 'order_complete_fake', json.dumps({'coupon_used': applied_coupon}), request.remote_addr or ''))
                    db.commit()
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()
        return api_ok(id=fake_id, url=fake_url, simulated=True)

    try:
        session = stripe.checkout.Session.create(
            mode='payment',
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            automatic_tax=automatic_tax,
            allow_promotion_codes=False
        )
        try:
            print(f"[checkout] Created session {session.id} status={session.status} success_url={success_url}")
        except Exception:
            pass
        return api_ok(id=session.id, url=session.url, success_url=success_url)
    except Exception as e:
        # Log full traceback server-side
        traceback.print_exc()
        msg = str(e)
        # Provide a friendlier hint for common mistakes
        if 'No such price' in msg:
            msg = 'Stripe price error – ensure amounts are >= $0.50 and test key is correct.'
        elif 'Invalid API Key' in msg:
            msg = 'Invalid Stripe API key – verify STRIPE_SECRET_KEY.'
        elif 'connection' in msg.lower():
            msg = 'Network issue reaching Stripe – check internet connection.'
        return api_error('Checkout failed: ' + msg, 500)

@app.route('/api/checkout/session/<session_id>')
def get_checkout_session(session_id):
    """Return basic details about a Stripe Checkout Session.
    Adds metadata & (optionally) line items so the confirmation page can show more context.
    """
    if not stripe or not STRIPE_SECRET_KEY:
        return api_error('Stripe not configured', 500)
    try:
        # Try expand line_items; if it fails fall back to basic retrieve
        try:
            session = stripe.checkout.Session.retrieve(session_id, expand=['line_items'])
        except Exception:
            session = stripe.checkout.Session.retrieve(session_id)
        resp = {
            'status': session.status,
            'amount_total': session.amount_total,
            'currency': session.currency,
            'metadata': dict(getattr(session, 'metadata', {}) or {})
        }
        # Attach minimal line_items info if present
        try:
            li = getattr(session, 'line_items', None)
            if li and hasattr(li, 'data'):
                resp['items'] = [
                    {
                        'description': (item.get('description') if isinstance(item, dict) else getattr(item, 'description', '')),
                        'amount_subtotal': (item.get('amount_subtotal') if isinstance(item, dict) else getattr(item, 'amount_subtotal', None)),
                        'quantity': (item.get('quantity') if isinstance(item, dict) else getattr(item, 'quantity', None)),
                    } for item in li.data
                ]
        except Exception:
            pass
        # Opportunistically consume coupon here as well (covers cases without webhook)
        try:
            md = dict(getattr(session, 'metadata', {}) or {})
            status_str = str(getattr(session, 'status', '')).lower()
            pay_status = str(getattr(session, 'payment_status', '')).lower()
            if md.get('coupon_code') and md.get('user_uuid') and (status_str == 'complete' or pay_status == 'paid'):
                _consume_coupon_by_uuid(md.get('coupon_code'), md.get('user_uuid'))
        except Exception:
            traceback.print_exc()
        return api_ok(**resp)
    except Exception as e:
        return api_error(str(e), 404)

@app.route('/api/checkout/webhook', methods=['POST'])
def stripe_webhook():
    if not stripe or not STRIPE_WEBHOOK_SECRET:
        return api_error('Webhook not configured', 500)
    payload = request.data
    sig = request.headers.get('Stripe-Signature','')
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return api_error('Invalid payload: '+str(e), 400)
    # Handle events (minimal)
    if event['type'] == 'checkout.session.completed':
        data = event['data']['object']
        # event payload here is a plain dict; use .get rather than getattr
        meta = dict((data.get('metadata') or {}))
        # Mark coupon as used (and remove if max uses reached)
        try:
            db = get_db(); ensure_coupons_table(db)
            code = (meta.get('coupon_code') or '').strip().upper()
            user_uuid = (meta.get('user_uuid') or '').strip()
            if code and user_uuid:
                _consume_coupon_by_uuid(code, user_uuid)
            # Audit
            try:
                db.execute(
                    'INSERT INTO audit_log (user_id, action, details, ip_address) VALUES (?,?,?,?)',
                    (None, 'order_complete', json.dumps({'session': data.get('id'), 'coupon_used': bool(meta.get('coupon_code'))}), request.remote_addr or '')
                )
                db.commit()
            except Exception:
                pass
        except Exception:
            traceback.print_exc()
    return api_ok(received=True)

# Admin (simple) users table view
@app.route('/admin/users')
@admin_required
def admin_users():
    db = get_db()
    ensure_is_admin_column(db)
    rows = db.execute('SELECT id, uuid, email, name, created_at, updated_at, is_admin FROM users ORDER BY id DESC LIMIT 500').fetchall()
    return render_template('users.html', users=rows)

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    # Placeholder data; replace with real queries later.
    sample_supplies = [
        { 'item': 'Soap Base (lbs)', 'in_stock': 42, 'reorder_level': 20 },
        { 'item': 'Lavender Oil (ml)', 'in_stock': 180, 'reorder_level': 150 },
        { 'item': 'Glass Jars (units)', 'in_stock': 320, 'reorder_level': 200 },
        { 'item': 'Labels (rolls)', 'in_stock': 5, 'reorder_level': 4 },
    ]
    sample_orders = [
        { 'id': 'PO-1005', 'status': 'Pending', 'total': 58.90, 'created': '2025-09-05' },
        { 'id': 'PO-1004', 'status': 'Shipped', 'total': 132.40, 'created': '2025-09-04' },
        { 'id': 'PO-1003', 'status': 'Delivered', 'total': 24.00, 'created': '2025-09-03' },
    ]
    return render_template('dashboard.html', supplies=sample_supplies, orders=sample_orders)

@app.route('/admin')
@admin_required
def admin_root():
    return render_template('redirect.html', target='/admin/dashboard')

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def admin_delete_user(user_id):
    # Simple admin endpoint (no auth layer yet). In production secure this route.
    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if not existing:
        return api_error('User not found', 404)
    db.execute('DELETE FROM users WHERE id=?', (user_id,))
    db.commit()
    return api_ok(deleted=user_id)

@app.route('/api/admin/users/<int:user_id>/admin', methods=['PATCH'])
@admin_required
def admin_toggle_user_admin(user_id):
    db = get_db()
    ensure_is_admin_column(db)
    data = request.get_json(silent=True) or {}
    if 'is_admin' not in data:
        return api_error('Missing is_admin')
    val = 1 if str(data['is_admin']).lower() in {'1','true','yes','on'} else 0
    # Prevent removal of last admin
    if val == 0:
        admins = db.execute('SELECT COUNT(*) c FROM users WHERE is_admin=1').fetchone()['c']
        current = db.execute('SELECT is_admin FROM users WHERE id=?', (user_id,)).fetchone()
        if current and current['is_admin'] == 1 and admins <= 1:
            return api_error('Cannot remove last admin')
    db.execute('UPDATE users SET is_admin=? WHERE id=?', (val, user_id))
    db.commit()
    return api_ok(user_id=user_id, is_admin=bool(val))

## Removed bootstrap promotion in favor of static password auth

@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        pwd = data.get('password') or ''
        if pwd != ADMIN_PANEL_PASSWORD:
            return api_error('Invalid password', 401)
        resp = api_ok(authenticated=True)
        resp.set_cookie('admin_auth','1', httponly=True, samesite='Lax')
        return resp
    # HTML form
    html = (
        '<!DOCTYPE html><html><head><title>Admin Login</title><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>'
        'body{font-family:system-ui;background:#0f172a;color:#fff;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh}'
        '.box{background:#1e293b;padding:34px 40px;border-radius:18px;width:340px;box-shadow:0 10px 38px -15px rgba(0,0,0,.55)}'
        'h1{margin:0 0 18px;font-size:1.25rem;text-align:center;letter-spacing:.06em}'
        'input{width:100%;padding:12px 14px;margin:0 0 14px;border:1px solid #334155;background:#0f172a;border-radius:10px;color:#fff;font:inherit}'
        'button{width:100%;padding:12px 14px;border:0;background:#6366f1;color:#fff;font-weight:600;border-radius:10px;cursor:pointer;font:inherit;letter-spacing:.05em}'
        '.msg{min-height:18px;font-size:.75rem;text-align:center;margin-top:6px}'
        'a{color:#818cf8;text-decoration:none;font-size:.75rem}'
        '</style></head><body><div class="box"><h1>Admin Login</h1>'
        '<input type="password" id="pw" placeholder="Password" autofocus />'
        '<button id="go">Enter</button><div class="msg" id="msg"></div>'
        '<div style="text-align:center;margin-top:14px"><a href="/">Return Home</a></div>'
        '<script>'
        'const b=document.getElementById("go"),pw=document.getElementById("pw"),msg=document.getElementById("msg");'
        'pw.addEventListener("keydown",e=>{if(e.key==="Enter")b.click();});'
        'b.onclick=async()=>{const v=pw.value;if(!v){msg.textContent="Enter password";return;}'
        'msg.textContent="Authenticating...";'
        'const r=await fetch("/admin/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:v})});'
        'let d={};try{d=await r.json()}catch{};' 
        'if(r.ok&&d.ok){msg.textContent="Success";setTimeout(()=>{location=(new URLSearchParams(location.search).get("next"))||"/admin/dashboard";},400);}'
        'else{msg.textContent=d.error||"Error";}'
        '};'
        '</script>'
        '</div></body></html>'
    )
    return html

@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    resp = api_ok(logged_out=True)
    resp.set_cookie('admin_auth','',expires=0)
    return resp

# -------- Frontend (serve static files for same-origin dev) --------
@app.route('/')
def serve_index():
    return send_from_directory(FRONTEND_DIR, 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    # Do not hijack API routes
    if filename.startswith('api/'):
        abort(404)
    target = FRONTEND_DIR / filename
    if target.is_file():
        # Allow serving everything except the backend folder itself
        if BASE_DIR.name in target.parts:
            abort(404)
        return send_from_directory(FRONTEND_DIR, filename)
    abort(404)

# -------- Error Handling (return JSON for API routes) --------
@app.errorhandler(404)
def _404(e):
    if request.path.startswith('/api/'):
        return api_error('Not found', 404)
    return e, 404

@app.errorhandler(405)
def _405(e):
    if request.path.startswith('/api/'):
        return api_error('Method not allowed', 405)
    return e, 405

@app.errorhandler(Exception)
def _500(e):
    # Log traceback to stderr
    traceback.print_exc()
    if request.path.startswith('/api/'):
        return api_error('Server error', 500)
    return e, 500

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
