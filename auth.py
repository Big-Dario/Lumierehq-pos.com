"""
auth.py — authentication + authorization for the Lumière POS backend.

Standard library only.

  Authentication : username + password -> opaque session token (Bearer).
                   Passwords: PBKDF2-HMAC-SHA256. Tokens: stored hashed.
  Authorization  : role -> permission set, plus branch scoping.

Roles
  admin    owner. Everything, all branches, user management.
  manager  bound to ONE branch. Stock, sales, analytics, shifts for that branch.
  cashier  bound to ONE branch. Sell, open/close own shift.

CLI (run on the server):
  python3 auth.py create-user <username> <role> [branch_id]
  python3 auth.py reset-password <username>
"""
import hashlib
import hmac
import os
import secrets
import sys
import time
import getpass

PBKDF2_ITER = 600_000
SESSION_TTL = 12 * 3600          # a long shift; re-login after this
MAX_FAILED = 5                   # failed logins before lockout
LOCK_SECONDS = 15 * 60
MIN_PASSWORD_LEN = 10

ROLES = ("admin", "manager", "cashier")

PERMISSIONS = {
    "cashier": {"catalog.read", "checkout", "discount.validate",
                "shift.open", "shift.close_own", "shift.read"},
    "manager": {"catalog.read", "checkout", "discount.validate",
                "shift.open", "shift.close_own", "shift.read",
                "shift.close_any", "stock.read", "stock.add",
                "sales.read", "analytics.read"},
}
PERMISSIONS["admin"] = PERMISSIONS["manager"] | {"users.manage"}

# Highest discount % a role may apply at checkout
MAX_DISCOUNT = {"cashier": 20, "manager": 50, "admin": 100}


class AuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ── passwords ────────────────────────────────────────────────────────────
def hash_password(password, salt=None, iterations=PBKDF2_ITER):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def check_password_strength(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise AuthError(400, f"Password must be at least {MIN_PASSWORD_LEN} characters")


# ── sessions ─────────────────────────────────────────────────────────────
def _tok_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login(conn, username, password, ip=""):
    username = (username or "").strip().lower()
    password = password if isinstance(password, str) else ""
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    now = int(time.time())

    if user is None:
        verify_password(password, _DUMMY_HASH)   # equalise timing
        raise AuthError(401, "Invalid username or password")
    if user["locked_until"] and user["locked_until"] > now:
        raise AuthError(423, "Account temporarily locked. Try again later.")
    if not verify_password(password, user["password_hash"]):
        failed = user["failed_attempts"] + 1
        locked = now + LOCK_SECONDS if failed >= MAX_FAILED else None
        conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                     (0 if locked else failed, locked, user["id"]))
        conn.commit()
        raise AuthError(401, "Invalid username or password")
    if not user["active"]:
        raise AuthError(403, "Account is disabled")

    conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login = ? "
                 "WHERE id = ?", (now, user["id"]))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))   # housekeeping
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, ip) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (_tok_hash(token), user["id"], now, now + SESSION_TTL, ip))
    conn.commit()
    return token, now + SESSION_TTL, user


def authenticate(conn, auth_header):
    """Return the user row for a valid Bearer token, else raise 401."""
    if not auth_header or not auth_header.startswith("Bearer "):
        raise AuthError(401, "Authentication required")
    token = auth_header[7:].strip()
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (_tok_hash(token), int(time.time())),
    ).fetchone()
    if row is None:
        raise AuthError(401, "Session expired or invalid")
    if not row["active"]:
        raise AuthError(403, "Account is disabled")
    return row


def logout(conn, auth_header):
    if auth_header and auth_header.startswith("Bearer "):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                     (_tok_hash(auth_header[7:].strip()),))
        conn.commit()


def revoke_user_sessions(conn, user_id, keep_header=None):
    keep = _tok_hash(keep_header[7:].strip()) if keep_header else ""
    conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep))


# ── authorization ────────────────────────────────────────────────────────
def require(user, permission):
    if permission not in PERMISSIONS.get(user["role"], ()):
        raise AuthError(403, "You don't have permission to do that")


def scoped_branch(user, requested=None):
    """Admins may use any branch. Everyone else is pinned to their own branch;
    asking for a different one is a 403. Returns the branch id to use (None = all)."""
    if user["role"] == "admin":
        return requested
    if requested and requested != user["branch_id"]:
        raise AuthError(403, "You can only access your own branch")
    return user["branch_id"]


def public_user(u):
    return {"id": u["id"], "username": u["username"], "fullName": u["full_name"],
            "role": u["role"], "branchId": u["branch_id"], "active": bool(u["active"]),
            "mustChangePassword": bool(u["must_change_password"]),
            "permissions": sorted(PERMISSIONS.get(u["role"], ()))}


# ── bootstrap + CLI ──────────────────────────────────────────────────────
def create_user(conn, username, password, role, branch_id=None, full_name=None,
                must_change=True):
    username = (username or "").strip().lower()
    if not username or role not in ROLES:
        raise AuthError(400, "username and a valid role are required")
    if role != "admin":
        if not branch_id or not conn.execute("SELECT 1 FROM branches WHERE id = ?",
                                             (branch_id,)).fetchone():
            raise AuthError(400, "Managers and cashiers need a valid branchId")
    else:
        branch_id = None
    check_password_strength(password)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError(409, "Username already exists")
    cur = conn.execute(
        "INSERT INTO users (username, full_name, password_hash, role, branch_id, "
        "must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, full_name or username, hash_password(password), role, branch_id,
         1 if must_change else 0, int(time.time())))
    conn.commit()
    return cur.lastrowid


def bootstrap_admin(conn):
    """First run only: create 'admin'. Password from LUMIERE_ADMIN_PASSWORD, else random
    (printed once). Must be changed at first login."""
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]:
        return
    pw = os.environ.get("LUMIERE_ADMIN_PASSWORD")
    generated = not pw
    if generated:
        pw = secrets.token_urlsafe(12)
    create_user(conn, "admin", pw, "admin", full_name="Administrator")
    print("=" * 60)
    print("First run: created user 'admin'")
    if generated:
        print(f"Temporary password: {pw}   (shown once — change it at first login)")
    print("=" * 60)


def _cli():
    import db
    conn = db.get_conn()
    args = sys.argv[1:]
    try:
        if args[:1] == ["create-user"] and len(args) >= 3:
            branch = args[3] if len(args) > 3 else None
            pw = getpass.getpass("Temporary password: ")
            uid = create_user(conn, args[1], pw, args[2], branch)
            print(f"Created user #{uid} ({args[1]}, {args[2]}). Password must be changed at first login.")
        elif args[:1] == ["reset-password"] and len(args) == 2:
            pw = getpass.getpass("New temporary password: ")
            check_password_strength(pw)
            cur = conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1, "
                               "failed_attempts = 0, locked_until = NULL WHERE username = ?",
                               (hash_password(pw), args[1].lower()))
            conn.execute("DELETE FROM sessions WHERE user_id IN "
                         "(SELECT id FROM users WHERE username = ?)", (args[1].lower(),))
            conn.commit()
            print("Password reset." if cur.rowcount else "No such user.")
        else:
            print(__doc__)
    except AuthError as e:
        print("Error:", e.message)
    finally:
        conn.close()


if __name__ == "__main__":
    _cli()
