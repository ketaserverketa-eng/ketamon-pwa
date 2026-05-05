import base64
import json
import os
import re
import uuid
import secrets
import random
import string
import sqlite3
import threading
import time
from datetime import datetime
from functools import wraps

import requests as http_req

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_from_directory, abort, g
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import mikrotik as mk
import database as db_mod

# KetaServer endpoint configurable
KS_API = os.environ.get("KETASERVER_API_URL", "http://127.0.0.1:5000")
STANDALONE_MODE = os.environ.get("STANDALONE_MODE", "0") == "1"
KS_ENABLED = STANDALONE_MODE
PROFILE_META_PREFIX = "ketamon-profile:"

# ─── Config AdMob locale ─────────────────────────────────────────────────────
AD_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "data", "ad_config.json")
AD_VIEWS_FILE  = os.path.join(os.path.dirname(__file__), "data", "ad_views.json")

_ad_config_cache: dict = {"data": {}, "ts": 0.0}
_AD_CONFIG_TTL = 60  # secondes

def load_ad_config() -> dict:
    now = time.time()
    if now - _ad_config_cache["ts"] < _AD_CONFIG_TTL:
        return _ad_config_cache["data"]
    try:
        with open(AD_CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    _ad_config_cache["data"] = data
    _ad_config_cache["ts"] = now
    return data

def save_ad_config(data: dict) -> None:
    try:
        with open(AD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _ad_config_cache["data"] = data
        _ad_config_cache["ts"] = time.time()
    except Exception:
        pass

def load_ad_views() -> dict:
    try:
        with open(AD_VIEWS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_ad_views(data: dict) -> None:
    try:
        with open(AD_VIEWS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
KETAMON_TICKET_COMMENT_MARKER = " ##KETAMON## exp="
KETAMON_TICKET_LOGIN_SCRIPT = "ketamon-ticket-login"
KETAMON_TICKET_EXPIRY_SCRIPT = "ketamon-ticket-expiry"
KETAMON_TICKET_EXPIRY_SCHEDULER = "ketamon-ticket-expiry-runner"

def _ks_ping_once(timeout=1):
    try:
        r = http_req.get(KS_API if KS_API.endswith('/') else KS_API + '/', timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False

def _ks_background_ping(interval=15):
    import time
    global KS_ENABLED
    while True:
        KS_ENABLED = _ks_ping_once(timeout=2)
        time.sleep(interval)

if not STANDALONE_MODE:
    try:
        t = threading.Thread(target=_ks_background_ping, args=(15,), daemon=True)
        t.start()
    except Exception:
        KS_ENABLED = _ks_ping_once()

def ks_post(path, data, token=None):
    """POST vers KetaServer API. Retourne (dict, None) ou (None, erreur_str)."""
    global KS_ENABLED
    if not KS_ENABLED:
        return None, "KetaServer indisponible"
    try:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = http_req.post(f"{KS_API}{path}", json=data, headers=headers, timeout=8)
        try:
            return r.json(), None
        except Exception:
            return None, f"KetaServer repondu code {r.status_code}"
    except Exception as e:
        KS_ENABLED = False
        return None, str(e)

def ks_get(path, token, params=None):
    """GET vers KetaServer API avec Bearer token."""
    global KS_ENABLED
    if not KS_ENABLED:
        return None, "KetaServer indisponible"
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = http_req.get(f"{KS_API}{path}", headers=headers, params=params or {}, timeout=8)
        try:
            return r.json(), None
        except Exception:
            return None, f"KetaServer repondu code {r.status_code}"
    except Exception as e:
        KS_ENABLED = False
        return None, str(e)

def ks_delete(path, token):
    """DELETE vers KetaServer API."""
    global KS_ENABLED
    if not KS_ENABLED:
        return None, "KetaServer indisponible"
    try:
        r = http_req.delete(f"{KS_API}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=8)
        try:
            return r.json(), None
        except Exception:
            return None, f"KetaServer repondu code {r.status_code}"
    except Exception as e:
        KS_ENABLED = False
        return None, str(e)

def ks_patch(path, token, data=None):
    """PATCH vers KetaServer API."""
    global KS_ENABLED
    if not KS_ENABLED:
        return None, "KetaServer indisponible"
    try:
        r = http_req.patch(f"{KS_API}{path}", json=data or {}, headers={"Authorization": f"Bearer {token}"}, timeout=8)
        try:
            return r.json(), None
        except Exception:
            return None, f"KetaServer repondu code {r.status_code}"
    except Exception as e:
        KS_ENABLED = False
        return None, str(e)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, root_path=APP_DIR, template_folder="templates", static_folder="static")
KETAMON_ENV = (os.environ.get("KETAMON_ENV") or "development").strip().lower()
SECRET_KEY = os.environ.get("KETAMON_SECRET_KEY")
if not SECRET_KEY:
    # Clé persistante stockée dans data/secret.key — générée une seule fois
    _sk_path = os.path.join(os.path.dirname(__file__), "data", "secret.key")
    try:
        if os.path.exists(_sk_path):
            with open(_sk_path, "r") as _f:
                SECRET_KEY = _f.read().strip() or None
        if not SECRET_KEY:
            SECRET_KEY = secrets.token_hex(32)
            os.makedirs(os.path.dirname(_sk_path), exist_ok=True)
            with open(_sk_path, "w") as _f:
                _f.write(SECRET_KEY)
            print("INFO: Clé secrète générée et sauvegardée dans data/secret.key")
        else:
            print("INFO: Clé secrète chargée depuis data/secret.key — sessions persistantes.")
    except Exception as _e:
        SECRET_KEY = secrets.token_hex(32)
        print(f"WARNING: Impossible de lire/écrire data/secret.key ({_e}). Clé éphémère utilisée.")
if KETAMON_ENV in {"prod", "production"} and not os.environ.get("KETAMON_SECRET_KEY"):
    print("WARNING: En production, définissez KETAMON_SECRET_KEY comme variable d'environnement.")
app.secret_key = SECRET_KEY

# ── Sécurité cookies de session ───────────────────────────────────────────────
app.config["SESSION_COOKIE_HTTPONLY"] = True   # Inaccessible au JS (anti XSS)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # Anti CSRF cross-origin
app.config["SESSION_COOKIE_SECURE"]   = KETAMON_ENV in {"prod", "production"}  # HTTPS only en prod

# Upload limits and allowed logo extensions
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB
ALLOWED_LOGO_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg"}

# ── Protection brute-force login (en mémoire, par IP) ─────────────────────────
_login_attempts: dict = {}   # {ip: [timestamps]}
_LOGIN_MAX_ATTEMPTS = 10     # tentatives max
_LOGIN_WINDOW_SEC   = 300    # sur 5 minutes
_LOGIN_LOCKOUT_SEC  = 600    # blocage 10 minutes

def _check_login_rate_limit() -> bool:
    """Retourne True si l'IP est bloquée."""
    ip = request.remote_addr or "unknown"
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Nettoyer les anciennes tentatives
    attempts = [t for t in attempts if now - t < _LOGIN_LOCKOUT_SEC]
    _login_attempts[ip] = attempts
    recent = [t for t in attempts if now - t < _LOGIN_WINDOW_SEC]
    return len(recent) >= _LOGIN_MAX_ATTEMPTS

def _record_login_failure():
    ip = request.remote_addr or "unknown"
    _login_attempts.setdefault(ip, []).append(time.time())

def _clear_login_failures():
    ip = request.remote_addr or "unknown"
    _login_attempts.pop(ip, None)

# ── CSRF : génération + protection ────────────────────────────────────────────
@app.before_request
def _ensure_csrf_and_protect():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    if request.method == "POST":
        # Exclure les webhooks internes et les endpoints appelés par l'app Android sans session
        _csrf_exempt_prefixes = ("/api/internal/", "/api/ads/report", "/api/vouchers", "/api/hotspot/")
        if any(request.path.startswith(p) for p in _csrf_exempt_prefixes):
            return
        token = session.get("csrf_token")
        header = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not token or not header or not secrets.compare_digest(token, header):
            if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                return jsonify({"ok": False, "msg": "Token de sécurité invalide"}), 400
            abort(400)

# ── Headers de sécurité HTTP ──────────────────────────────────────────────────
@app.after_request
def _add_security_headers(response):
    response.headers["X-Frame-Options"]        = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"]       = "1; mode=block"
    # CSP permissif mais fonctionnel (CDN font-awesome + data: URI pour canvas)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'self';"
    )
    return response

@app.context_processor
def inject_csrf_token():
    logo = get_active_ticket_logo()
    return dict(
        csrf_token=session.get("csrf_token", ""),
        ks_enabled=KS_ENABLED,
        # ks_api intentionnellement retiré — URL interne non exposée aux templates
        ticket_logo_url=logo["url"],
        ticket_logo_name=logo["name"],
        ticket_logo_is_custom=logo["is_custom"],
    )

# Initialiser SQLite au démarrage

DATA_DIR         = os.path.join(os.path.dirname(__file__), "data")
ROUTERS_F        = os.path.join(DATA_DIR, "routers.json")
USERS_F          = os.path.join(DATA_DIR, "users.json")
PLANS_F          = os.path.join(DATA_DIR, "plans.json")
SUBSCRIPTIONS_F  = os.path.join(DATA_DIR, "subscriptions.json")
PAY_CONFIG_F     = os.path.join(DATA_DIR, "paiement_config.json")
LOGOS_DIR        = os.path.join(DATA_DIR, "logos")
STATIC_IMG_DIR   = os.path.join(APP_DIR, "static", "img")
DEFAULT_TICKET_LOGO_NAME = "default-ticket-logo.png"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGOS_DIR, exist_ok=True)
os.makedirs(STATIC_IMG_DIR, exist_ok=True)

# Initialiser SQLite au dÃ©marrage
db_mod.DATA_DIR = DATA_DIR
db_mod.DB_PATH = os.path.join(DATA_DIR, "ketamon.db")
db_mod.LEGACY_USERS_PATH = USERS_F
db_mod.LEGACY_ROUTERS_PATH = ROUTERS_F
db_mod.init_db()


def list_uploaded_logos():
    logos = []
    if not os.path.isdir(LOGOS_DIR):
        return logos
    for filename in os.listdir(LOGOS_DIR):
        if filename.startswith("."):
            continue
        full_path = os.path.join(LOGOS_DIR, filename)
        ext = os.path.splitext(filename)[1].lower()
        if not os.path.isfile(full_path) or ext not in ALLOWED_LOGO_EXT:
            continue
        logos.append({
            "filename": filename,
            "mtime": os.path.getmtime(full_path),
        })
    logos.sort(key=lambda row: (-row["mtime"], row["filename"].lower()))
    return logos


def get_active_ticket_logo():
    uploaded = list_uploaded_logos()
    if uploaded:
        active = uploaded[0]
        return {
            "url": url_for("serve_logo", filename=active["filename"]),
            "name": active["filename"],
            "is_custom": True,
        }
    return {
        "url": url_for("static", filename=f"img/{DEFAULT_TICKET_LOGO_NAME}"),
        "name": DEFAULT_TICKET_LOGO_NAME,
        "is_custom": False,
    }

DEFAULT_PLANS = [
    {"id": "mensuel",  "nom": "Mensuel",    "duree": 30,  "prix": 5000,  "devise": "FCFA", "actif": True},
    {"id": "trimestr", "nom": "Trimestriel","duree": 90,  "prix": 12000, "devise": "FCFA", "actif": True},
    {"id": "annuel",   "nom": "Annuel",     "duree": 365, "prix": 40000, "devise": "FCFA", "actif": True},
]

DEFAULT_PAY_CONFIG = {
    "devise_base": "FCFA",
    "taux_change": {
        "USD": 606.0,   # 1 USD = 606 FCFA
        "EUR": 655.0,   # 1 EUR = 655 FCFA
        "FCFA": 1.0,
        "XOF": 1.0,
    },
    "tolerance_pct": 0,
    "methodes": [
        {"id": "orange-money", "nom": "Orange Money", "numero": "",  "instructions": "Envoyez le montant exact au numéro ci-dessus. Mentionnez votre email en commentaire.", "actif": False},
        {"id": "moov-money",   "nom": "Moov Money",   "numero": "",  "instructions": "Envoyez le montant exact au numéro ci-dessus. Mentionnez votre email en commentaire.", "actif": False},
        {"id": "wave",         "nom": "Wave",          "numero": "",  "instructions": "Transfert Wave au numéro ci-dessus. Email en commentaire.", "actif": False},
        {"id": "mtn-money",    "nom": "MTN Money",     "numero": "",  "instructions": "Envoyez le montant exact. Email en commentaire.", "actif": False},
    ],
}

# ── Wrappers SQLite (remplacent les JSON pour thread-safety 1000+ users) ──────

def get_plans():              return db_mod.db_get_plans()
def get_plans_actifs():       return db_mod.db_get_plans(actif_only=True)
def get_pay_config():         return db_mod.db_get_pay_config()
def save_pay_config(cfg):     db_mod.db_save_pay_config(cfg)
def get_user_subscription(u): return db_mod.db_get_active_sub(u)

def to_base(montant, devise, cfg):
    taux = cfg.get("taux_change", {})
    return float(montant or 0) * float(taux.get(devise, 1.0))

def verifier_paiement_antifraude(plan, montant_paye, devise_paye, reference, methode, cfg):
    """Retourne (ok, flags, detail). Vérifie fraude avant insertion."""
    flags = []
    try:
        m = float(montant_paye)
    except (ValueError, TypeError):
        m = 0.0

    if m <= 0:
        flags.append("MONTANT_ZERO")
    ref = str(reference or "").strip()
    if not ref:
        flags.append("REFERENCE_VIDE")
    # Doublon vérifié directement dans SQLite (index sur reference)
    if ref and db_mod.db_reference_exists(ref):
        flags.append("REFERENCE_DOUBLON")
    methodes_actives = {mt["id"] for mt in cfg.get("methodes", []) if mt.get("actif")}
    if methode not in methodes_actives:
        flags.append("METHODE_INVALIDE_OU_INACTIVE")
    if m > 0:
        prix_base = to_base(plan["prix"], plan["devise"], cfg)
        paye_base = to_base(m, devise_paye, cfg)
        tolerance = float(cfg.get("tolerance_pct", 0)) / 100.0
        if paye_base < prix_base * (1 - tolerance):
            flags.append(f"MONTANT_INSUFFISANT({paye_base:.0f}<{prix_base:.0f} {cfg.get('devise_base','FCFA')})")
        if paye_base > prix_base * 10:
            flags.append("MONTANT_SUSPECT_ELEVE")

    _FLAG_MSGS = {
        "MONTANT_ZERO":              "Le montant payé doit être supérieur à zéro.",
        "REFERENCE_VIDE":            "La référence de paiement est obligatoire.",
        "REFERENCE_DOUBLON":         "Cette référence de paiement est déjà utilisée.",
        "METHODE_INVALIDE_OU_INACTIVE": "La méthode de paiement sélectionnée n'est pas disponible.",
        "MONTANT_SUSPECT_ELEVE":     "Le montant payé est anormalement élevé.",
    }
    def _humanize(f):
        for key, msg in _FLAG_MSGS.items():
            if f.startswith(key):
                return msg
        return "Paiement non valide."
    detail = _humanize(flags[0]) if flags else "OK"
    return len(flags) == 0, flags, detail

# ─── Persistence helpers ────────────────────────────────────────────────────

def _current_owner_id():
    """Retourne l'owner_id de l'utilisateur connecté, ou None pour le concepteur (voit tout)."""
    # Contexte session (web)
    if session.get("role") == "concepteur":
        return None
    if session.get("user_id"):
        return session["user_id"]
    # Contexte API Basic Auth (mobile)
    basic_user = getattr(g, "basic_auth_user", None)
    if basic_user:
        if basic_user.get("role") == "concepteur":
            return None
        return basic_user.get("email") or basic_user.get("username") or None
    return None

def get_routers():
    return db_mod.db_get_routers(owner_id=_current_owner_id())

def save_routers(routers):
    db_mod.db_replace_routers(routers, owner_id=_current_owner_id())

def get_app_users():
    """Utilisateurs locaux (fallback si KetaServer indispo)."""
    users = db_mod.db_get_local_users()
    if not users:
        initial_pwd = secrets.token_urlsafe(12)
        try:
            db_mod.db_insert_local_user({
                "id": str(uuid.uuid4()),
                "username": "admin",
                "email": "",
                "password": generate_password_hash(initial_pwd),
                "display_name": "admin",
                "role": "admin",
            })
        except db_mod.DuplicateLocalUserError:
            pass
        users = db_mod.db_get_local_users()
        if users:
            print("IMPORTANT: initial admin account created.")
            print(f"  username=admin password={initial_pwd}")
            print("Store this password securely and change it on first login.")
    return users

# ─── Auth helpers ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def router_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("router_id"):
            # Auto-sélection si un seul routeur enregistré
            routers = get_routers()
            if len(routers) == 1:
                session["router_id"] = routers[0]["id"]
            else:
                flash("Veuillez d'abord sélectionner un routeur.", "warning")
                return redirect(url_for("sessions_list"))
        return f(*args, **kwargs)
    return decorated

def get_active_router():
    rid = session.get("router_id")
    if not rid:
        return None
    for r in get_routers():
        if r["id"] == rid:
            return r
    return None

def local_register(email, password, display_name=None):
    """Enregistre un utilisateur local dans SQLite. Retourne user dict."""
    try:
        user = db_mod.db_insert_local_user({
            "id": str(uuid.uuid4()),
            "username": email,
            "email": email,
            "password": generate_password_hash(password),
            "display_name": display_name or email,
            "role": "utilisateur",
        })
        if user:
            user["displayName"] = user.get("display_name") or user.get("displayName") or email
        return user
    except db_mod.DuplicateLocalUserError:
        return None
    except Exception:
        return None


def authenticate_local_user(email, password):
    """Verifie un utilisateur local en SQLite. Retourne (user_dict|None)."""
    try:
        user = db_mod.db_get_local_user(email)
        if user:
            stored = user.get("password")
            if stored and check_password_hash(stored, password):
                user["displayName"] = user.get("display_name") or user.get("displayName") or email
                return user
    except Exception:
        pass
    return None


def _check_basic_auth():
    """Vérifie l'en-tête Authorization: Basic pour les endpoints API Android.
    Retourne (ok: bool, user_dict|None). Stocke l'user dans g.basic_auth_user."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False, None
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
        user = authenticate_local_user(username, password)
        if user:
            g.basic_auth_user = user
        return (user is not None), user
    except Exception:
        return False, None


def build_remote_user_session(resp, fallback_identity, fallback_role="utilisateur"):
    """Normalise les reponses d'auth KetaServer legacy et FastAPI SaaS."""
    if not isinstance(resp, dict):
        return None
    token = resp.get("token") or resp.get("access_token")
    if not token:
        return None
    if resp.get("ok") is False and not resp.get("access_token"):
        return None
    display_name = (
        resp.get("displayName")
        or resp.get("display_name")
        or resp.get("name")
        or fallback_identity
    )
    return {
        "logged_in": True,
        "ks_token": token,
        "ks_refresh_token": resp.get("refresh_token"),
        "username": display_name,
        "role": resp.get("role", fallback_role),
        "user_id": resp.get("user_id") or resp.get("email") or fallback_identity,
        "auth_source": "remote",
    }


def request_payload():
    return request.get_json(silent=True) or request.form

def safe_int(value, default=0, min_val=None, max_val=None):
    """Conversion int sécurisée — jamais de crash sur entrée utilisateur."""
    try:
        v = int(str(value).strip())
        if min_val is not None:
            v = max(min_val, v)
        if max_val is not None:
            v = min(max_val, v)
        return v
    except (ValueError, TypeError):
        return default

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(str(email or "").strip()))

import logging as _logging
_logger = _logging.getLogger("ketamon")

def _flash_err(msg: str, e: Exception = None, level: str = "danger") -> None:
    """Flash un message générique et logue le vrai détail sans l'exposer à l'utilisateur."""
    if e is not None:
        _logger.error("[ketamon] %s — %s: %s", msg, type(e).__name__, e)
    flash(msg, level)


def router_item_id(item):
    if not isinstance(item, dict):
        return ""
    return str(item.get("id") or item.get(".id") or "").strip()


def parse_routeros_duration(value):
    text = str(value or "").strip().lower()
    if not text or text in {"0", "0s", "none", "unlimited", "infinite", "inf", "?"}:
        return None

    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([wdhms])", text):
        amount = int(amount)
        if unit == "w":
            total += amount * 7 * 24 * 3600
        elif unit == "d":
            total += amount * 24 * 3600
        elif unit == "h":
            total += amount * 3600
        elif unit == "m":
            total += amount * 60
        elif unit == "s":
            total += amount
    text = re.sub(r"(\d+)\s*[wdhms]", "", text).strip()

    if text:
        parts = [p for p in text.split(":") if p != ""]
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            hours, minutes, seconds = map(int, parts)
            total += hours * 3600 + minutes * 60 + seconds
        elif len(parts) == 2 and all(part.isdigit() for part in parts):
            minutes, seconds = map(int, parts)
            total += minutes * 60 + seconds
        elif text.isdigit():
            total += int(text)
        else:
            return None

    return total if total > 0 else None


def format_duration_compact(seconds):
    if seconds is None:
        return "∞"
    seconds = max(0, int(seconds))
    if seconds == 0:
        return "0s"

    chunks = []
    for suffix, unit in (("w", 7 * 24 * 3600), ("d", 24 * 3600), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds < unit:
            continue
        value, seconds = divmod(seconds, unit)
        if value:
            chunks.append(f"{value}{suffix}")
        if len(chunks) == 2:
            break
    return " ".join(chunks) or "0s"


def build_active_disconnect_targets(active_rows):
    usernames = []
    addresses = []
    mac_addresses = []
    active_ids = []
    for active in active_rows or []:
        username = str(active.get("user") or "").strip()
        address = str(active.get("address") or "").strip()
        mac_address = str(active.get("mac-address") or "").strip()
        active_id = router_item_id(active)
        if username:
            usernames.append(username)
        if address:
            addresses.append(address)
        if mac_address:
            mac_addresses.append(mac_address)
        if active_id:
            active_ids.append(active_id)
    return usernames, addresses, mac_addresses, active_ids


def find_matching_hotspot_active_rows(api, usernames=None, addresses=None, mac_addresses=None):
    usernames = {str(name or '').strip() for name in (usernames or []) if str(name or '').strip()}
    addresses = {str(address or '').strip() for address in (addresses or []) if str(address or '').strip()}
    mac_addresses = {str(mac or '').strip().lower() for mac in (mac_addresses or []) if str(mac or '').strip()}
    try:
        rows = []
        for row in api.get_resource('/ip/hotspot/active').get():
            username = str(row.get('user') or '').strip()
            address = str(row.get('address') or '').strip()
            mac_address = str(row.get('mac-address') or '').strip().lower()
            if not usernames and not addresses and not mac_addresses:
                rows.append(row)
                continue
            if (username and username in usernames) or (address and address in addresses) or (mac_address and mac_address in mac_addresses):
                rows.append(row)
        return rows
    except Exception:
        return []


def disconnect_hotspot_entities(api, usernames=None, addresses=None, mac_addresses=None, active_ids=None):
    usernames = {str(name or "").strip() for name in (usernames or []) if str(name or "").strip()}
    addresses = {str(address or "").strip() for address in (addresses or []) if str(address or "").strip()}
    mac_addresses = {str(mac or "").strip().lower() for mac in (mac_addresses or []) if str(mac or "").strip()}
    active_ids = {str(active_id or "").strip() for active_id in (active_ids or []) if str(active_id or "").strip()}
    removed = {"active_sessions": 0, "cookies": 0, "hosts": 0}

    try:
        active_resource = api.get_resource("/ip/hotspot/active")
        for active in active_resource.get():
            active_id = router_item_id(active)
            username = str(active.get("user") or "").strip()
            address = str(active.get("address") or "").strip()
            mac_address = str(active.get("mac-address") or "").strip().lower()
            if (
                active_id in active_ids
                or username in usernames
                or address in addresses
                or (mac_address and mac_address in mac_addresses)
            ):
                if active_id:
                    active_resource.remove(id=active_id)
                    removed["active_sessions"] += 1
    except Exception:
        pass

    try:
        cookie_resource = api.get_resource("/ip/hotspot/cookie")
        for cookie in cookie_resource.get():
            cookie_id = router_item_id(cookie)
            username = str(cookie.get("user") or "").strip()
            mac_address = str(cookie.get("mac-address") or "").strip().lower()
            if username in usernames or (mac_address and mac_address in mac_addresses):
                if cookie_id:
                    cookie_resource.remove(id=cookie_id)
                    removed["cookies"] += 1
    except Exception:
        pass

    try:
        host_resource = api.get_resource("/ip/hotspot/host")
        for host in host_resource.get():
            host_id = router_item_id(host)
            address = str(host.get("address") or "").strip()
            mac_address = str(host.get("mac-address") or "").strip().lower()
            if address in addresses or (mac_address and mac_address in mac_addresses):
                if host_id:
                    host_resource.remove(id=host_id)
                    removed["hosts"] += 1
    except Exception:
        pass

    return removed


def compute_active_time_left(active_row, user_row):
    direct_left = parse_routeros_duration(active_row.get("session-time-left"))
    if direct_left is not None:
        return format_duration_compact(direct_left)

    limit_seconds = parse_routeros_duration(user_row.get("limit-uptime"))
    if limit_seconds is None:
        return "∞"

    used_seconds = parse_routeros_duration(user_row.get("uptime-used"))
    if used_seconds is None:
        used_seconds = parse_routeros_duration(active_row.get("uptime")) or 0
    return format_duration_compact(limit_seconds - used_seconds)


def _fmt_bytes(val):
    """Formate un nombre d'octets en KB / MB / GB lisible."""
    try:
        b = int(val or 0)
    except Exception:
        return "-"
    if b == 0:
        return "0"
    if b < 1024 * 1024:
        return f"{b/1024:.1f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b/1024/1024:.1f} MB"
    return f"{b/1024/1024/1024:.2f} GB"


def normalize_active_sessions(api, active_rows, users_map=None):
    if users_map is None:
        try:
            users_map = {
                str(user.get("name") or "").strip(): dict(user)
                for user in api.get_resource("/ip/hotspot/user").get()
            }
        except Exception:
            users_map = {}

    normalized = []
    for active in active_rows or []:
        row = dict(active)
        row["id"] = router_item_id(active)
        user_row = users_map.get(str(row.get("user") or "").strip(), {})
        row["temps-restant"] = compute_active_time_left(row, user_row)
        row["debit-down"] = _fmt_bytes(row.get("bytes-in",  0))
        row["debit-up"]   = _fmt_bytes(row.get("bytes-out", 0))
        row["user_hotspot_id"] = router_item_id(user_row) if user_row else ""
        row["profile"] = str(user_row.get("profile") or "-")
        row["limit-uptime"] = str(user_row.get("limit-uptime") or "0")
        row["bytes-in-total"]  = str(user_row.get("bytes-in",  row.get("bytes-in",  0)))
        row["bytes-out-total"] = str(user_row.get("bytes-out", row.get("bytes-out", 0)))
        row["user_disabled"] = str(user_row.get("disabled", "no")).strip().lower() == "yes"
        normalized.append(row)
    return normalized


def normalize_ticket_time_limit(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() in {"0", "0s", "none", "illimite", "unlimited", "infinite", "inf"}:
        return "0"
    return text


def build_profile_comment_metadata(price, currency, expire_mode, lock_user, time_limit="0"):
    payload = {
        "price": str(price or "0"),
        "currency": str(currency or "FCFA"),
        "expire_mode": str(expire_mode or "none"),
        "lock_user": str(lock_user or "yes"),
        "time_limit": normalize_ticket_time_limit(time_limit) or "0",
    }
    return PROFILE_META_PREFIX + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def parse_profile_comment_metadata(profile):
    comment = str((profile or {}).get("comment") or "").strip()
    if not comment.startswith(PROFILE_META_PREFIX):
        return {}
    raw = comment[len(PROFILE_META_PREFIX):].strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_hotspot_profile_metadata_map(router_id):
    metadata_rows = db_mod.db_get_hotspot_profile_metadata(router_id) or []
    mapping = {}
    for row in metadata_rows:
        profile_name = str(row.get("profile_name") or "").strip()
        if profile_name:
            mapping[profile_name] = row
    return mapping


def _sync_ventes_for_router(router_info, timeout=8):
    """Sync tickets UTILISÉS (bytes-in>0) pour un routeur → SQLite.
    Chaque ticket compté exactement 1 fois, jamais re-compté."""
    host      = router_info.get("host", "")
    ruser     = router_info.get("user") or router_info.get("username") or "admin"
    password  = router_info.get("password", "")
    port      = int(router_info.get("port") or 8728)
    router_id = router_info.get("id") or host

    api, err = mk.safe_connect(host, ruser, password, port, timeout=timeout)
    if err or not api:
        return 0

    try:
        profiles_meta = get_hotspot_profile_metadata_map(router_id)
        conn = db_mod.get_conn()

        existing = set(
            r[0] for r in conn.execute(
                "SELECT user FROM ventes WHERE router_id=?", (router_id,)
            ).fetchall()
        )

        all_users = api.get_resource("/ip/hotspot/user").get()
        now_dt   = datetime.now()
        date_str = now_dt.strftime("%Y-%m-%d")
        time_str = now_dt.strftime("%H:%M:%S")
        new_count = 0

        import uuid as _uuid
        for u in all_users:
            try:
                bytes_in = int(u.get("bytes-in", 0) or 0)
            except (ValueError, TypeError):
                bytes_in = 0
            if bytes_in == 0:
                continue  # jamais connecté → ne pas compter
            username = str(u.get("name", "") or "").strip()
            if not username or username in existing:
                continue  # déjà enregistré → pas de doublon

            profile = str(u.get("profile", "default") or "default")

            # Priorité 1 : prix enregistré à la création du ticket dans KetaMon
            pricing = conn.execute(
                "SELECT prix, devise, profil, reseau FROM ticket_pricing WHERE router_id=? AND user=?",
                (router_id, username)
            ).fetchone()
            if pricing:
                price    = float(pricing[0] or 0)
                currency = pricing[1] or "FCFA"
                profile  = pricing[2] or profile
                reseau   = pricing[3] or ""
            elif profile in profiles_meta:
                # Priorité 2 : profil configuré dans KetaMon (métadonnées)
                meta     = profiles_meta[profile]
                price    = float(meta.get("price", "0") or "0")
                currency = meta.get("currency", "FCFA") or "FCFA"
                reseau   = ""
            else:
                # Ticket inconnu de KetaMon → ignorer pour ne pas polluer les revenus
                continue

            db_mod.db_insert_vente({
                "id":         _uuid.uuid4().hex,
                "router_id":  router_id,
                "date":       date_str,
                "heure":      time_str,
                "user":       username,
                "profil":     profile,
                "prix":       price,
                "devise":     currency,
                "reseau":     reseau,
                "data_limit": "0",
            })
            existing.add(username)
            new_count += 1

        return new_count
    except Exception as _e:
        import traceback
        print(f"[SYNC] ERREUR router {router_info.get('host','?')}: {_e}")
        traceback.print_exc()
        return 0


# Suivi temps réel du statut de sync par routeur
_sync_stats = {}  # {router_id: {"last_sync": "ISO", "new": N, "ok": bool, "error": str, "host": str}}


def _bg_ventes_loop():
    """Thread daemon : sync toutes les 5s, timeout 5s/routeur — capture rapide des tickets."""
    time.sleep(3)
    while True:
        try:
            for router in db_mod.db_get_routers():
                rid = router.get("id") or router.get("host", "")
                try:
                    n = _sync_ventes_for_router(router, timeout=5)
                    _sync_stats[rid] = {
                        "last_sync": datetime.now().isoformat(timespec="seconds"),
                        "new":       n,
                        "ok":        True,
                        "error":     "",
                        "host":      router.get("host", ""),
                        "name":      router.get("name", ""),
                    }
                    if n:
                        print(f"[SYNC] {router.get('host','?')} : {n} nouvelle(s) vente(s)")
                except Exception as _e:
                    _sync_stats[rid] = {
                        "last_sync": datetime.now().isoformat(timespec="seconds"),
                        "new":       0,
                        "ok":        False,
                        "error":     str(_e),
                        "host":      router.get("host", ""),
                        "name":      router.get("name", ""),
                    }
        except Exception as _e:
            print(f"[SYNC] boucle erreur: {_e}")
        time.sleep(5)  # cycle complet toutes les 5 secondes


threading.Thread(target=_bg_ventes_loop, daemon=True).start()


def get_profile_time_limit(router_id, profile_name):
    metadata = db_mod.db_get_hotspot_profile_metadata(router_id, profile_name) or {}
    return normalize_ticket_time_limit(metadata.get("time_limit"))


def resolve_ticket_time_limit(router_id, profile_name, requested_time_limit):
    normalized_requested = normalize_ticket_time_limit(requested_time_limit)
    if normalized_requested != "":
        return normalized_requested
    profile_time_limit = get_profile_time_limit(router_id, profile_name)
    return profile_time_limit or "0"


def strip_ticket_runtime_comment(comment):
    raw = str(comment or "").strip()
    marker_pos = raw.find(KETAMON_TICKET_COMMENT_MARKER)
    if marker_pos != -1:
        raw = raw[:marker_pos].rstrip()
    return raw


def build_hotspot_user_comment(user_comment, mode="vc-"):
    base_comment = strip_ticket_runtime_comment(user_comment)
    return f"{mode}{base_comment}" if base_comment else mode


def sanitize_router_script_name(prefix, value, max_length=63):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-") or "default"
    return f"{prefix}-{slug}"[:max_length]


def build_ticket_login_wrapper_script(existing_script_name):
    safe_name = str(existing_script_name or "").replace("\\", "\\\\").replace('"', '\\"')
    return "\n".join([
        f':local legacy [/system script find where name="{safe_name}"];',
        ':if ([:len $legacy] > 0) do={ /system script run $legacy; }',
        f':local ketamon [/system script find where name="{KETAMON_TICKET_LOGIN_SCRIPT}"];',
        ':if ([:len $ketamon] > 0) do={ /system script run $ketamon; }',
    ])


def build_ketamon_ticket_login_script_source():
    return "\n".join([
        ':local uname $user;',
        ':local loginMac $"mac-address";',
        ':if ([:len $uname] = 0) do={ :return; }',
        ':local userId [/ip hotspot user find where name=$uname];',
        ':if ([:len $userId] = 0) do={ :return; }',
        ':local storedMac [:tostr [/ip hotspot user get $userId mac-address]];',
        ':if (([:len $loginMac] > 0) && (($storedMac = "") || ($storedMac = "00:00:00:00:00:00"))) do={ /ip hotspot user set $userId mac-address=$loginMac; }',
        ':local limitVal [:tostr [/ip hotspot user get $userId limit-uptime]];',
        ':if (([:len $limitVal] = 0) || ($limitVal = "0") || ($limitVal = "0s") || ($limitVal = "none")) do={ :return; }',
        ':local currentComment [:tostr [/ip hotspot user get $userId comment]];',
        f':local marker "{KETAMON_TICKET_COMMENT_MARKER}";',
        ':local markerPos [:find $currentComment $marker];',
        ':if ($markerPos = nil) do={',
        '    :local expireAt ([:timestamp] + [:totime $limitVal]);',
        '    :local baseComment $currentComment;',
        '    :local oldMarkerPos [:find $baseComment " ##KETAMON## "];',
        '    :if ($oldMarkerPos != nil) do={ :set baseComment [:pick $baseComment 0 $oldMarkerPos]; }',
        '    /ip hotspot user set $userId comment=($baseComment . $marker . $expireAt) limit-uptime=0;',
        '}',
    ])


def build_ketamon_ticket_expiry_script_source():
    # Seuls les tickets UTILISÉS ont le marqueur ##KETAMON## exp=...
    # Les tickets non utilisés (bytes-in=0, pas de marqueur) ne sont JAMAIS touchés.
    # Quand un ticket expiré est détecté : sessions/cookies/hosts coupés + ticket SUPPRIMÉ.
    return "\n".join([
        f':local marker "{KETAMON_TICKET_COMMENT_MARKER}";',
        ':local nowNs [:tonsec value=[:timestamp]];',
        ':local expiredIds [:toarray ""];',
        '/ip hotspot user',
        ':foreach userId in=[find] do={',
        '    :local comment [:tostr [get $userId comment]];',
        '    :local markerPos [:find $comment $marker];',
        '    :if ($markerPos = nil) do={ :continue; }',
        '    :local startPos ($markerPos + [:len $marker]);',
        '    :local expireRaw [:pick $comment $startPos [:len $comment]];',
        '    :if ([:len $expireRaw] = 0) do={ :continue; }',
        '    :local expireTime [:totime $expireRaw];',
        '    :if ([:typeof $expireTime] = "nil") do={ :continue; }',
        '    :local expireNs [:tonsec value=$expireTime];',
        '    :if ($nowNs >= $expireNs) do={',
        '        :local uname [:tostr [get $userId name]];',
        '        :local lockedMac [:tostr [get $userId mac-address]];',
        '        /ip hotspot active remove [find where user=$uname];',
        '        /ip hotspot cookie remove [find where user=$uname];',
        '        :if (([:len $lockedMac] > 0) && ($lockedMac != "00:00:00:00:00:00")) do={ /ip hotspot host remove [find where mac-address=$lockedMac]; }',
        '        set $expiredIds ($expiredIds, $userId);',
        '    }',
        '}',
        ':foreach eid in=$expiredIds do={ /ip hotspot user remove $eid; }',
    ])


def upsert_router_script(api, name, source):
    resource = api.get_resource("/system/script")
    params = {
        "name": name,
        "source": source,
        "policy": "ftp,reboot,read,write,policy,test,password,sniff,sensitive,romon",
    }
    rows = resource.get(name=name)
    if rows:
        resource.set(id=router_item_id(rows[0]), **params)
    else:
        resource.add(**params)


def upsert_router_scheduler(api, name, on_event, interval="30s", start_time="00:00:00"):
    resource = api.get_resource("/system/scheduler")
    params = {
        "name": name,
        "interval": interval,
        "on-event": on_event,
        "start-time": start_time,
        "disabled": "no",
        "policy": "ftp,reboot,read,write,policy,test,password,sniff,sensitive,romon",
    }
    rows = resource.get(name=name)
    if rows:
        resource.set(id=router_item_id(rows[0]), **params)
    else:
        resource.add(**params)


def ensure_ticket_runtime_support(api, profile_name=None):
    upsert_router_script(api, KETAMON_TICKET_LOGIN_SCRIPT, build_ketamon_ticket_login_script_source())
    upsert_router_script(api, KETAMON_TICKET_EXPIRY_SCRIPT, build_ketamon_ticket_expiry_script_source())
    upsert_router_scheduler(api, KETAMON_TICKET_EXPIRY_SCHEDULER, KETAMON_TICKET_EXPIRY_SCRIPT)

    profile_name = str(profile_name or "").strip()
    if not profile_name:
        return

    profile_resource = api.get_resource("/ip/hotspot/user/profile")
    profiles = profile_resource.get(name=profile_name)
    if not profiles:
        return

    profile_row = profiles[0]
    current_on_login = str(profile_row.get("on-login") or "").strip()
    wrapper_name = sanitize_router_script_name("ketamon-login", profile_name)
    desired_on_login = KETAMON_TICKET_LOGIN_SCRIPT

    if current_on_login and current_on_login not in {KETAMON_TICKET_LOGIN_SCRIPT, wrapper_name}:
        upsert_router_script(api, wrapper_name, build_ticket_login_wrapper_script(current_on_login))
        desired_on_login = wrapper_name
    elif current_on_login == wrapper_name:
        desired_on_login = wrapper_name

    if current_on_login != desired_on_login:
        profile_resource.set(id=router_item_id(profile_row), **{"on-login": desired_on_login})


def normalize_hotspot_profile(profile, metadata=None):
    row = dict(profile or {})
    meta = dict(metadata or {})
    if not meta:
        meta = parse_profile_comment_metadata(row)
    if meta.get("expire_mode"):
        row["expire-mode"] = meta.get("expire_mode")
    if meta.get("lock_user"):
        row["add-mac-cookie"] = meta.get("lock_user")
    if meta.get("price") is not None:
        row["price"] = meta.get("price")
    if meta.get("currency"):
        row["currency"] = meta.get("currency")
    row["time-limit"] = normalize_ticket_time_limit(meta.get("time_limit")) or "0"
    row["_ketamon_meta"] = bool(meta)
    return row


def get_api():
    r = get_active_router()
    if not r:
        return None, "Aucun routeur actif"
    driver = r.get("driver", "mikrotik")
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728), driver=driver)
    return api, err


def _normalize_router_count(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except Exception:
            return None
    if isinstance(value, dict):
        return _normalize_router_count(value.get("ret") or value.get("count"))
    if isinstance(value, (list, tuple)):
        if not value:
            return 0
        if len(value) == 1:
            parsed = _normalize_router_count(value[0])
            if parsed is not None:
                return parsed
        return len(value)
    return None


def resource_count(api, path):
    resource = api.get_resource(path)
    try:
        raw_count = resource.call("print", {"count-only": ""})
        if raw_count not in (None, [], (), ""):
            direct_count = _normalize_router_count(raw_count)
            if direct_count is not None:
                return direct_count
    except Exception:
        pass
    try:
        items = resource.get()
        return len(list(items or []))
    except Exception:
        return 0

# ─── Context processor ───────────────────────────────────────────────────────
@app.context_processor
def inject_layout_context():
    logged_in = bool(session.get("logged_in"))
    _ad = load_ad_config()
    _css_path = os.path.join(os.path.dirname(__file__), "static", "css", "style.css")
    try:
        _css_ver = int(os.path.getmtime(_css_path))
    except Exception:
        _css_ver = 1
    return {
        "routers": get_routers() if logged_in else [],
        "active_router": get_active_router() if logged_in else None,
        "current_page": request.endpoint or "",
        "adsense_pub_id":      _ad.get("adsensePubId", "") or os.environ.get("ADSENSE_PUB_ID", ""),
        "adsense_banner_slot": _ad.get("adsenseBannerSlot", ""),
        "adsense_inter_slot":  _ad.get("adsenseInterSlot", ""),
        "_css_ver": _css_ver,
    }

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    # Variables pour re-afficher la page avec le bon mode/sous-mode
    tpl_vars = {"mode": "utilisateur", "submode": "login",
                "prefill_email": "", "reset_token": None}

    if request.method == "POST":
        # ── Protection brute-force ─────────────────────────────────────────
        if _check_login_rate_limit():
            flash("Trop de tentatives de connexion. Réessayez dans 10 minutes.", "danger")
            return render_template("login.html", **tpl_vars)

        mode    = request.form.get("mode", "email")      # "username" | "email"
        submode = request.form.get("submode", "login")   # "login"|"register"|"forgot"|"reset"
        tpl_vars["mode"]    = "concepteur" if mode == "username" else "utilisateur"
        tpl_vars["submode"] = submode

        # ── CONCEPTEUR (username + password) ──────────────────────────────
        if mode == "username":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            tpl_vars["mode"] = "concepteur"
            resp, err = ks_post("/api/auth/login", {"username": username, "password": password})
            if resp and resp.get("ok"):
                _clear_login_failures()
                session.update({
                    "logged_in": True, "ks_token": resp.get("token"),
                    "username": resp.get("displayName") or username,
                    "role": "concepteur", "user_id": username,
                })
                return redirect(url_for("index"))
            else:
                # Fallback : fichier concepteur local
                creds_ok = False
                creds_file = os.environ.get("KETASERVER_CONCEPTEUR_FILE") or os.path.join(DATA_DIR, "concepteur.json")
                try:
                    with open(creds_file, encoding="utf-8") as f:
                        creds = json.load(f)
                    stored = creds.get("password", "")
                    if stored and stored.startswith(("pbkdf2:", "scrypt:")):
                        if check_password_hash(stored, password) and creds.get("username") == username:
                            creds_ok = True
                            display = creds.get("displayName", username)
                    else:
                        print("WARNING: plaintext concepteur password ignored; use a Werkzeug hash in concepteur.json.")
                except Exception:
                    pass
                if creds_ok:
                    _clear_login_failures()
                    session.update({
                        "logged_in": True, "ks_token": None,
                        "username": display,
                        "role": "concepteur", "user_id": username,
                    })
                    return redirect(url_for("index"))
                _record_login_failure()
                flash("Identifiants concepteur incorrects.", "danger")

        # ── UTILISATEUR EMAIL — CONNEXION ──────────────────────────────────
        elif submode == "login":
            email    = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            tpl_vars["prefill_email"] = email
            resp, err = ks_post("/api/auth/login", {"email": email, "password": password})
            remote_session = build_remote_user_session(resp, email)
            if err:
                # Try local fallback
                user = authenticate_local_user(email, password)
                if user:
                    _clear_login_failures()
                    session.update({
                        "logged_in": True, "ks_token": None,
                        "ks_refresh_token": None,
                        "username": user.get("displayName") or email,
                        "role": user.get("role", "utilisateur"), "user_id": email,
                        "auth_source": "local",
                    })
                    return redirect(url_for("index"))
                _record_login_failure()
                flash("KetaServer indisponible ou identifiants incorrects.", "danger")
            elif remote_session:
                _clear_login_failures()
                session.update(remote_session)
                return redirect(url_for("index"))
            else:
                _record_login_failure()
                flash("Email ou mot de passe incorrect.", "danger")

        # ── INSCRIPTION ────────────────────────────────────────────────────
        elif submode == "register":
            email    = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirm  = request.form.get("confirm_password", "")
            display  = request.form.get("display_name", "").strip()
            tpl_vars["prefill_email"] = email
            if not is_valid_email(email):
                flash("Adresse email invalide.", "danger")
            elif password != confirm:
                flash("Les mots de passe ne correspondent pas.", "danger")
            elif len(password) < 8:
                flash("Mot de passe min. 8 caracteres.", "danger")
            else:
                resp, err = ks_post("/api/auth/register",
                                    {"email": email, "password": password, "displayName": display})
                remote_session = build_remote_user_session(resp, display or email)
                if err:
                    u = local_register(email, password, display)
                    if u:
                        session.update({
                            "logged_in": True, "ks_token": None,
                            "ks_refresh_token": None,
                            "username": u.get("displayName") or email,
                            "role": u.get("role", "utilisateur"), "user_id": email,
                            "auth_source": "local",
                        })
                        flash("Compte créé avec succès.", "success")
                        return redirect(url_for("index"))
                    flash("Inscription impossible. Réessayez.", "danger")
                elif remote_session:
                    session.update(remote_session)
                    return redirect(url_for("index"))
                else:
                    flash(resp.get("message", "Erreur inscription.") if resp else "Erreur.", "danger")

        # ── MOT DE PASSE OUBLIÉ ────────────────────────────────────────────
        elif submode == "forgot":
            email = request.form.get("email", "").strip()
            tpl_vars["prefill_email"] = email
            resp, err = ks_post("/api/auth/forgot-password", {"email": email})
            if err:
                flash("KetaServer indisponible — réinitialisation non disponible.", "danger")
            elif resp and resp.get("ok"):
                raw_token = (resp.get("resetUrl") or "").split("token=")[-1] or ""
                if raw_token:
                    # Stocker le token en session (jamais dans le HTML)
                    session["_pwd_reset_token"] = raw_token
                    tpl_vars["submode"] = "reset"
                    flash("Entrez votre nouveau mot de passe ci-dessous.", "success")
                else:
                    flash("Lien envoyé si l'email existe.", "success")
            else:
                flash(resp.get("message", "Erreur.") if resp else "Erreur.", "danger")

        # ── RESET MOT DE PASSE ─────────────────────────────────────────────
        elif submode == "reset":
            # Token lu depuis la session (jamais depuis le formulaire HTML)
            token    = session.get("_pwd_reset_token", "")
            new_pass = request.form.get("new_password", "")
            if not token:
                flash("Session expirée. Recommencez la demande de réinitialisation.", "danger")
                tpl_vars["submode"] = "forgot"
                return render_template("login.html", **tpl_vars)
            if len(new_pass) < 6:
                tpl_vars["submode"] = "reset"
                flash("Mot de passe min. 6 caractères.", "danger")
            else:
                resp, err = ks_post("/api/auth/reset-password",
                                    {"token": token, "newPassword": new_pass})
                if err:
                    flash("KetaServer indisponible.", "danger")
                elif resp and resp.get("ok"):
                    session.pop("_pwd_reset_token", None)  # Consommer le token
                    flash("Mot de passe modifié. Connectez-vous.", "success")
                    tpl_vars["submode"] = "login"
                else:
                    tpl_vars["submode"] = "reset"
                    flash("Erreur lors de la réinitialisation. Recommencez.", "danger")

    return render_template("login.html", **tpl_vars)

@app.route("/logout")
def logout():
    session.clear()
    flash("Vous avez ete deconnecte.", "info")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    if session.get("router_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("sessions_list"))

# ─── Tableau de bord ─────────────────────────────────────────────────────────

@app.route("/tableau-de-bord")
@login_required
@router_required
def dashboard():
    api, err = get_api()
    data = {}
    if err:
        flash(err, "danger")
    else:
        try:
            res   = api.get_resource("/system/resource").get()[0]
            ident = api.get_resource("/system/identity").get()[0]
            clock = api.get_resource("/system/clock").get()[0]
            rb    = api.get_resource("/system/routerboard").get()[0]
            hs_users    = resource_count(api, "/ip/hotspot/user")
            hs_active   = resource_count(api, "/ip/hotspot/active")
            hs_profiles = resource_count(api, "/ip/hotspot/user/profile")
            hs_list     = api.get_resource("/ip/hotspot").get()

            total_mem  = int(res.get("total-memory", 0))
            free_mem   = int(res.get("free-memory", 0))
            total_hdd  = int(res.get("total-hdd-space", 0))
            free_hdd   = int(res.get("free-hdd-space", 0))
            mem_used   = total_mem - free_mem
            mem_pct    = round(mem_used / total_mem * 100) if total_mem else 0

            router = get_active_router() or {}
            data = {
                "identity":     ident.get("name", "MikroTik"),
                "board":        rb.get("model", res.get("board-name", "?")),
                "version":      res.get("version", "?"),
                "uptime":       res.get("uptime", "?"),
                "cpu_load":     res.get("cpu-load", "0"),
                "total_mem":    mk.format_bytes(total_mem),
                "free_mem":     mk.format_bytes(free_mem),
                "mem_pct":      mem_pct,
                "total_hdd":    mk.format_bytes(total_hdd),
                "free_hdd":     mk.format_bytes(free_hdd),
                "time":         clock.get("time", ""),
                "date":         clock.get("date", ""),
                "hs_users":     hs_users,
                "hs_active":    hs_active,
                "hs_profiles":  hs_profiles,
                "hs_servers":   [h.get("name", "") for h in hs_list],
                "router_host":  router.get("host", ""),
                "router_port":  str(router.get("port", "8728")),
            }
        except Exception as e:
            _flash_err("Erreur de communication MikroTik.", e)
    return render_template("dashboard.html", data=data)

# ─── Hotspot : Utilisateurs ──────────────────────────────────────────────────

@app.route("/reseau/clients")
@app.route("/hotspot/utilisateurs", endpoint="hotspot_users")
@login_required
@router_required
def reseau_clients():
    api, err = get_api()
    users, profiles, servers = [], [], []
    if err:
        flash(err, "danger")
    else:
        try:
            prof_filter = request.args.get("profil", "tous")
            comm_filter = request.args.get("commentaire", "")
            exp_filter  = request.args.get("expire", "")
            profiles = api.get_resource("/ip/hotspot/user/profile").get()
            servers  = api.get_resource("/ip/hotspot").get()
            if comm_filter:
                users = api.get_resource("/ip/hotspot/user").get(**{"comment": comm_filter})
            elif exp_filter:
                users = api.get_resource("/ip/hotspot/user").get(**{"limit-uptime": "1s"})
            elif prof_filter != "tous":
                users = api.get_resource("/ip/hotspot/user").get(**{"profile": prof_filter})
            else:
                users = api.get_resource("/ip/hotspot/user").get()

            active_by_user = {}
            for active in find_matching_hotspot_active_rows(api):
                username = str(active.get("user") or "").strip()
                if username:
                    active_by_user.setdefault(username, []).append(active)

            normalized_users = []
            for user in users:
                row = dict(user)
                username = str(row.get("name") or "").strip()
                is_disabled = str(row.get("disabled", "no")).strip().lower() == "yes"
                active_rows = active_by_user.get(username, [])
                row["_active_sessions"] = len(active_rows)
                row["_is_connected"] = bool(active_rows)
                if is_disabled:
                    row["_live_state"] = "disabled"
                    row["_live_state_label"] = "Desactive"
                    row["_live_state_badge"] = "badge-red"
                elif active_rows:
                    row["_live_state"] = "connected"
                    row["_live_state_label"] = "Connecte"
                    row["_live_state_badge"] = "badge-green"
                else:
                    row["_live_state"] = "offline"
                    row["_live_state_label"] = "Hors ligne"
                    row["_live_state_badge"] = "badge-orange"
                normalized_users.append(row)
            users = normalized_users
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("reseau/clients.html",
        users=users, profiles=profiles, servers=servers,
        prof_filter=request.args.get("profil","tous"))

@app.route("/reseau/clients/ajouter", methods=["GET", "POST"])
@app.route("/hotspot/utilisateurs/ajouter", methods=["GET", "POST"], endpoint="hotspot_add_user")
@login_required
@router_required
def reseau_ajouter_client():
    api, err = get_api()
    profiles, servers = [], []
    router_id = session.get("router_id", "")
    if err:
        flash(err, "danger")
        return redirect(url_for("reseau_clients"))
    try:
        profiles = api.get_resource("/ip/hotspot/user/profile").get()
        servers  = api.get_resource("/ip/hotspot").get()
    except Exception as e:
        _flash_err("Une erreur est survenue.", e)

    if request.method == "POST":
        try:
            name    = request.form["name"].strip()
            passwd  = request.form.get("password", name).strip() or name
            profile = (request.form.get("profile", "default") or "default").strip() or "default"
            server  = (request.form.get("server", "") or "").strip()
            tlimit  = resolve_ticket_time_limit(router_id, profile, request.form.get("time_limit", ""))
            dlimit  = (request.form.get("data_limit", "0") or "0").strip() or "0"
            comment = request.form.get("comment", "")
            mode    = "vc-" if name == passwd else "up-"

            ensure_ticket_runtime_support(api, profile)

            params = {
                "name": name, "password": passwd, "profile": profile,
                "disabled": "no", "limit-uptime": tlimit or "0",
                "limit-bytes-total": str(int(dlimit) * 1048576) if dlimit != "0" else "0",
                "comment": build_hotspot_user_comment(comment, mode),
            }
            if server:
                params["server"] = server
            api.get_resource("/ip/hotspot/user").add(**params)
            flash(f'Client "{name}" cree. 1 ticket = 1 appareil, compteur absolu a la premiere connexion.', "success")
            return redirect(url_for("reseau_clients"))
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("reseau/ajouter_client.html", profiles=profiles, servers=servers)

@app.route("/hotspot/utilisateurs/modifier/<uid>", methods=["GET", "POST"])
@app.route("/reseau/clients/modifier/<uid>", methods=["GET", "POST"], endpoint="reseau_modifier_client")
@login_required
@router_required
def hotspot_edit_user(uid):
    api, err = get_api()
    if err:
        flash(err, "danger")
        return redirect(url_for("hotspot_users"))
    try:
        users    = api.get_resource("/ip/hotspot/user").get(id=uid)
        profiles = api.get_resource("/ip/hotspot/user/profile").get()
        servers  = api.get_resource("/ip/hotspot").get()
        user = users[0] if users else {}
    except Exception as e:
        _flash_err("Une erreur est survenue.", e)
        return redirect(url_for("hotspot_users"))

    if request.method == "POST":
        try:
            name     = request.form["name"]
            password = request.form.get("password", "") or name
            profile  = request.form.get("profile", "default")
            comment  = request.form.get("comment", "")
            server   = (request.form.get("server", "") or "").strip()

            # Normaliser la limite de temps (accepte "1h30m", "3600s", "0", etc.)
            time_limit_raw = request.form.get("time_limit", "0") or "0"
            time_limit = normalize_ticket_time_limit(time_limit_raw) or "0"

            # Convertir Mo → octets (comme à l'ajout)
            data_limit_mo = request.form.get("data_limit", "0") or "0"
            try:
                dl_val = float(data_limit_mo)
                data_limit_bytes = str(int(dl_val * 1024 * 1024)) if dl_val > 0 else "0"
            except (ValueError, TypeError):
                data_limit_bytes = "0"

            # Préserver le préfixe de commentaire interne
            existing_comment = str(user.get("comment", "") or "")
            if existing_comment.startswith("vc-") or existing_comment.startswith("up-"):
                mode = existing_comment[:3]
            else:
                mode = "up-"
            new_comment = build_hotspot_user_comment(comment, mode)

            params = {
                "id":              uid,
                "name":            name,
                "password":        password,
                "profile":         profile,
                "comment":         new_comment,
                "limit-uptime":    time_limit,
                "limit-bytes-total": data_limit_bytes,
            }
            if server:
                params["server"] = server
            api.get_resource("/ip/hotspot/user").set(**params)
            flash("Utilisateur modifié.", "success")
            return redirect(url_for("reseau_clients"))
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("reseau/modifier_client.html", user=user, profiles=profiles, servers=servers)

@app.route("/hotspot/utilisateurs/supprimer", methods=["POST"])
@app.route("/reseau/clients/supprimer", methods=["POST"], endpoint="reseau_supprimer_client")
@login_required
@router_required
def hotspot_delete_user():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        payload = request_payload()
        uid = (payload.get("id") or "").strip()
        if not uid:
            return jsonify({"ok": False, "msg": "Identifiant utilisateur introuvable."}), 400
        users = api.get_resource("/ip/hotspot/user").get(id=uid)
        username = str(users[0].get("name") or "") if users else ""
        active_rows = find_matching_hotspot_active_rows(api, usernames=[username])
        usernames, addresses, mac_addresses, active_ids = build_active_disconnect_targets(active_rows)
        api.get_resource("/ip/hotspot/user").remove(id=uid)
        disconnected = disconnect_hotspot_entities(
            api,
            usernames=usernames or [username],
            addresses=addresses,
            mac_addresses=mac_addresses,
            active_ids=active_ids,
        )
        return jsonify({"ok": True, "disconnected": disconnected})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/hotspot/utilisateurs/basculer", methods=["POST"])
@app.route("/reseau/clients/basculer", methods=["POST"], endpoint="reseau_basculer_client")
@login_required
@router_required
def hotspot_toggle_user():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        payload = request_payload()
        uid = (payload.get("id") or "").strip()
        disabled = str(payload.get("disabled", "yes")).strip().lower() or "yes"
        if not uid:
            return jsonify({"ok": False, "msg": "Identifiant utilisateur introuvable."}), 400

        users = api.get_resource("/ip/hotspot/user").get(id=uid)
        if not users:
            return jsonify({"ok": False, "msg": "Utilisateur hotspot introuvable sur le routeur."}), 404

        username = str(users[0].get("name") or "").strip()
        active_rows = find_matching_hotspot_active_rows(api, usernames=[username])
        usernames, addresses, mac_addresses, active_ids = build_active_disconnect_targets(active_rows)

        api.get_resource("/ip/hotspot/user").set(id=uid, disabled=disabled)

        disconnected = {"active_sessions": 0, "cookies": 0, "hosts": 0}
        if disabled == "yes":
            remaining_active = active_rows
            for _ in range(3):
                batch = disconnect_hotspot_entities(
                    api,
                    usernames=usernames or [username],
                    addresses=addresses,
                    mac_addresses=mac_addresses,
                    active_ids=active_ids,
                )
                for key, value in batch.items():
                    disconnected[key] += int(value or 0)

                remaining_active = find_matching_hotspot_active_rows(
                    api,
                    usernames=usernames or [username],
                    addresses=addresses,
                    mac_addresses=mac_addresses,
                )
                if not remaining_active:
                    return jsonify({
                        "ok": True,
                        "msg": "Utilisateur desactive et acces Internet coupe.",
                        "disconnected": disconnected,
                    })

                usernames, addresses, mac_addresses, active_ids = build_active_disconnect_targets(remaining_active)
                time.sleep(0.2)

            return jsonify({
                "ok": False,
                "msg": "Utilisateur desactive, mais la session Internet est encore active sur le routeur.",
                "disconnected": disconnected,
            }), 409

        return jsonify({
            "ok": True,
            "msg": "Utilisateur active. Il peut se reconnecter.",
            "disconnected": disconnected,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/hotspot/utilisateurs/reinitialiser", methods=["POST"])
@login_required
@router_required
def hotspot_reset_user():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        payload = request_payload()
        uid = (payload.get("id") or "").strip()
        if not uid:
            return jsonify({"ok": False, "msg": "Identifiant utilisateur introuvable."}), 400

        users = api.get_resource("/ip/hotspot/user").get(id=uid)
        if not users:
            return jsonify({"ok": False, "msg": "Utilisateur hotspot introuvable sur le routeur."}), 404

        user_row = users[0]
        username = str(user_row.get("name") or "").strip()
        locked_mac = str(user_row.get("mac-address") or "").strip()
        active_rows = find_matching_hotspot_active_rows(
            api,
            usernames=[username],
            mac_addresses=[locked_mac] if locked_mac else None,
        )
        usernames, addresses, mac_addresses, active_ids = build_active_disconnect_targets(active_rows)
        disconnected = disconnect_hotspot_entities(
            api,
            usernames=usernames or [username],
            addresses=addresses,
            mac_addresses=mac_addresses or ([locked_mac] if locked_mac else []),
            active_ids=active_ids,
        )

        clean_comment = strip_ticket_runtime_comment(user_row.get("comment", ""))
        api.get_resource("/ip/hotspot/user").set(
            id=uid,
            disabled="no",
            **{
                "uptime": "0s",
                "bytes-in": "0",
                "bytes-out": "0",
                "mac-address": "00:00:00:00:00:00",
                "comment": clean_comment,
            },
        )
        return jsonify({
            "ok": True,
            "msg": "Ticket reinitialise. Il sera relie au premier appareil qui se reconnecte.",
            "disconnected": disconnected,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Hotspot : Generer utilisateurs ──────────────────────────────────────────

@app.route("/hotspot/generer", methods=["GET", "POST"])
@app.route("/reseau/clients/creer-comptes", methods=["GET", "POST"], endpoint="reseau_creer_comptes")
@login_required
@router_required
def hotspot_generate():
    api, err = get_api()
    profiles, servers = [], []
    generated = []
    router_id = session.get("router_id", "")
    if err:
        flash(err, "danger")
    else:
        try:
            profiles = api.get_resource("/ip/hotspot/user/profile").get()
            servers  = api.get_resource("/ip/hotspot").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)

    # Charger les métadonnées (prix) de chaque profil depuis SQLite
    profiles_meta = get_hotspot_profile_metadata_map(router_id)

    if request.method == "POST" and not err:
        try:
            qty          = safe_int(request.form.get("qty", 1), default=1, min_val=1, max_val=200)
            profile      = (request.form.get("profile", "default") or "default").strip() or "default"
            server       = (request.form.get("server", "") or "").strip()
            mode          = request.form.get("mode", "aleatoire")
            password_mode = request.form.get("password_mode", "identique")
            prefix        = (request.form.get("prefix", "") or "").strip()
            length        = safe_int(request.form.get("length", 8), default=8, min_val=4, max_val=32)
            comment      = request.form.get("comment", "")
            network_name = (request.form.get("network_name", "") or "").strip()
            data_limit   = (request.form.get("data_limit", "0") or "0").strip()
            time_limit_override = (request.form.get("time_limit_override", "") or "").strip()

            ticket_time_limit = time_limit_override if time_limit_override else (get_profile_time_limit(router_id, profile) or "0")

            # ensure_ticket_runtime_support : exécuter une seule fois par session router
            _erts_key = f"erts_done_{router_id}"
            if not session.get(_erts_key):
                try:
                    ensure_ticket_runtime_support(api, profile)
                    session[_erts_key] = True
                except Exception:
                    pass

            meta     = profiles_meta.get(profile, {})
            price    = meta.get("price", "0") or "0"
            currency = meta.get("currency", "FCFA") or "FCFA"

            if mode == "chiffres":
                charset = string.digits
            elif mode == "lettres":
                charset = string.ascii_lowercase
            else:
                charset = string.ascii_lowercase + string.digits

            now_dt   = datetime.now()
            date_str = now_dt.strftime("%Y-%m-%d")

            hotspot_resource = api.get_resource("/ip/hotspot/user")
            pricing_batch = []
            for _ in range(qty):
                rand = "".join(random.choices(charset, k=length))
                name = prefix + rand
                if password_mode == "different":
                    password = "".join(random.choices(charset, k=length))
                else:
                    password = name
                params = {
                    "name": name, "password": password, "profile": profile,
                    "disabled": "no", "comment": build_hotspot_user_comment(comment, "vc-"),
                    "limit-uptime": ticket_time_limit,
                }
                if data_limit and data_limit != "0":
                    params["limit-bytes-total"] = str(int(float(data_limit) * 1024 * 1024))
                if server:
                    params["server"] = server
                try:
                    hotspot_resource.add(**params)
                    generated.append({
                        "name":       name,
                        "password":   password,
                        "profile":    profile,
                        "price":      price,
                        "currency":   currency,
                        "network":    network_name,
                        "date":       date_str,
                        "data_limit": data_limit,
                        "time_limit": ticket_time_limit,
                    })
                    pricing_batch.append({
                        "router_id": router_id,
                        "user":      name,
                        "prix":      float(price) if price and price != "0" else 0.0,
                        "devise":    currency,
                        "profil":    profile,
                        "reseau":    network_name,
                    })
                except Exception as ex:
                    _flash_err("Erreur lors de la création du ticket.", ex, "warning")
            if pricing_batch:
                db_mod.db_batch_upsert_ticket_pricing(pricing_batch)

            flash(f"{len(generated)} ticket(s) créé(s).", "success")
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)

    return render_template("reseau/creer_comptes.html",
        profiles=profiles, servers=servers, generated=generated,
        profiles_meta=profiles_meta)

# ─── Hotspot : Profils ───────────────────────────────────────────────────────

@app.route("/hotspot/profils")
@app.route("/reseau/profils", endpoint="reseau_profils")
@login_required
@router_required
def hotspot_profiles():
    api, err = get_api()
    profiles = []
    if err:
        flash(err, "danger")
    else:
        try:
            router_id = session.get("router_id", "")
            metadata_map = get_hotspot_profile_metadata_map(router_id)
            profiles = [
                normalize_hotspot_profile(
                    profile,
                    metadata_map.get(str(profile.get("name") or "").strip())
                )
                for profile in api.get_resource("/ip/hotspot/user/profile").get()
            ]
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("hotspot/profiles.html", profiles=profiles)

@app.route("/hotspot/profils/ajouter", methods=["GET", "POST"])
@app.route("/reseau/profils/ajouter", methods=["GET", "POST"], endpoint="reseau_ajouter_profil")
@login_required
@router_required
def hotspot_add_profile():
    api, err = get_api()
    pools = []
    router_id = session.get("router_id", "")
    if err:
        flash(err, "danger")
        return redirect(url_for("hotspot_profiles"))
    try:
        pools = api.get_resource("/ip/pool").get()
    except Exception:
        pass

    if request.method == "POST":
        try:
            name         = request.form["name"].replace(" ", "-")
            shared_users = "1"
            rate_limit   = request.form.get("rate_limit", "")
            expire_mode  = request.form.get("expire_behavior") or request.form.get("expire_mode", "none")
            addr_pool    = request.form.get("addr_pool", "")
            lock_user    = "yes"
            time_limit   = normalize_ticket_time_limit(request.form.get("time_limit", "")) or "0"
            price        = request.form.get("price", "0")
            currency     = request.form.get("currency", "FCFA")
            router_params = {
                "name": name,
                "shared-users": shared_users,
            }
            if rate_limit:
                router_params["rate-limit"] = rate_limit
            if addr_pool:
                router_params["address-pool"] = addr_pool

            profile_resource = api.get_resource("/ip/hotspot/user/profile")
            profile_resource.add(**router_params)

            metadata_warning = None
            runtime_warning = None
            try:
                db_mod.db_upsert_hotspot_profile_metadata(
                    router_id,
                    name,
                    price=price,
                    currency=currency,
                    expire_mode=expire_mode,
                    lock_user=lock_user,
                    time_limit=time_limit,
                )
            except Exception as meta_exc:
                metadata_warning = str(meta_exc)

            try:
                ensure_ticket_runtime_support(api, name)
            except Exception as runtime_exc:
                runtime_warning = str(runtime_exc)

            flash(f'Profil "{name}" cree. Duree par defaut: {time_limit or "0"}.', "success")
            if metadata_warning:
                flash(
                    "Profil cree, mais les options KetaMon locales n'ont pas pu etre sauvegardees : "
                    f"{metadata_warning}",
                    "warning",
                )
            if runtime_warning:
                flash(
                    "Profil cree, mais le verrou 1 ticket = 1 appareil n'a pas pu etre installe sur le routeur : "
                    f"{runtime_warning}",
                    "warning",
                )
            return redirect(url_for("hotspot_profiles"))
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)

    return render_template("hotspot/add_profile.html", pools=pools)

@app.route("/hotspot/profils/supprimer", methods=["POST"])
@app.route("/reseau/profils/modifier-meta", methods=["POST"], endpoint="reseau_modifier_profil_meta")
@login_required
@router_required
def hotspot_update_profile_meta():
    """Met à jour les métadonnées KetaMon d'un profil (prix, devise, durée) sans toucher MikroTik."""
    router_id = session.get("router_id", "")
    payload   = request_payload()
    name      = str(payload.get("name", "")).strip()
    price     = str(payload.get("price", "0")).strip()
    currency  = str(payload.get("currency", "FCFA")).strip()
    time_limit = str(payload.get("time_limit", "0")).strip()
    expire_mode= str(payload.get("expire_mode", "none")).strip()
    if not name:
        return jsonify({"ok": False, "msg": "Nom de profil manquant."})
    try:
        db_mod.db_upsert_hotspot_profile_metadata(
            router_id, name,
            price=price, currency=currency,
            expire_mode=expire_mode, lock_user="yes", time_limit=time_limit
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/reseau/profils/supprimer", methods=["POST"], endpoint="reseau_supprimer_profil")
@login_required
@router_required
def hotspot_delete_profile():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        payload = request_payload()
        pid = (payload.get("id") or "").strip()
        if not pid:
            return jsonify({"ok": False, "msg": "Identifiant profil introuvable."}), 400
        profile_resource = api.get_resource("/ip/hotspot/user/profile")
        existing = profile_resource.get(id=pid)
        profile_name = str(existing[0].get("name") or "") if existing else ""
        profile_resource.remove(id=pid)
        if profile_name:
            db_mod.db_delete_hotspot_profile_metadata(session.get("router_id", ""), profile_name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Hotspot : Actifs ────────────────────────────────────────────────────────

@app.route("/hotspot/actifs")
@app.route("/reseau/sessions", endpoint="reseau_sessions")
@login_required
@router_required
def hotspot_active():
    api, err = get_api()
    actifs, servers = [], []
    if err:
        flash(err, "danger")
    else:
        try:
            server_filter = request.args.get("serveur", "")
            servers = api.get_resource("/ip/hotspot").get()
            if server_filter:
                actifs = api.get_resource("/ip/hotspot/active").get(**{"server": server_filter})
            else:
                actifs = api.get_resource("/ip/hotspot/active").get()
            actifs = normalize_active_sessions(api, actifs)
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("hotspot/active.html", actifs=actifs, servers=servers,
        server_filter=request.args.get("serveur",""))

@app.route("/hotspot/scripts/reinstaller", methods=["POST"])
@login_required
def hotspot_reinstall_scripts():
    """Réinstalle les scripts MikroTik (expiration + login) sur tous les routeurs."""
    routers = db_mod.db_get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré."})
    ok_count = 0
    errors = []
    for router_info in routers:
        try:
            rhost = router_info.get("host","")
            ruser = router_info.get("user") or "admin"
            rpwd  = router_info.get("password","")
            rport = int(router_info.get("port") or 8728)
            api2, err2 = mk.safe_connect(rhost, ruser, rpwd, rport)
            if err2:
                errors.append(f"{rhost}: {err2}")
                continue
            ensure_ticket_runtime_support(api2)
            ok_count += 1
        except Exception as e:
            errors.append(f"{router_info.get('host','?')}: {e}")
    if ok_count:
        msg = f"Scripts mis à jour sur {ok_count} routeur(s)."
        if errors:
            msg += f" Erreurs: {'; '.join(errors)}"
        return jsonify({"ok": True, "msg": msg})
    return jsonify({"ok": False, "msg": "; ".join(errors) or "Echec."})


@app.route("/hotspot/actifs/supprimer", methods=["POST"])
@app.route("/reseau/sessions/supprimer", methods=["POST"], endpoint="reseau_supprimer_session")
@login_required
@router_required
def hotspot_remove_active():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        payload = request_payload()
        aid  = str(payload.get("id")   or "").strip()
        user = str(payload.get("user") or "").strip()
        mac  = str(payload.get("mac")  or "").strip()

        removed_total = 0
        active_resource = api.get_resource("/ip/hotspot/active")

        if aid:
            # Suppression directe par ID — pas besoin de charger toutes les sessions
            targets = [{"id": aid, "user": user, "mac": mac.lower(), "ip": ""}]
        else:
            # Filtre par user ou mac : charge toutes les sessions une seule fois
            all_active = active_resource.get()
            targets = []
            for a in all_active:
                row_id  = router_item_id(a)
                row_usr = str(a.get("user") or "").strip()
                row_mac = str(a.get("mac-address") or "").strip().lower()
                row_ip  = str(a.get("address") or "").strip()
                if (
                    (user and row_usr == user) or
                    (mac  and row_mac == mac.lower())
                ):
                    targets.append({"id": row_id, "user": row_usr, "mac": row_mac, "ip": row_ip})

        if not targets:
            return jsonify({"ok": False, "msg": "Session introuvable ou deja fermee."})

        last_err = ""
        for t in targets:
            # 1. Supprimer session active
            removed_this = False
            if t["id"]:
                try:
                    # librouteros 4.x : path.remove(*ids) envoie .id=<id>
                    api._lrt.path("/ip/hotspot/active").remove(t["id"])
                    removed_this = True
                except Exception as e:
                    last_err = str(e)
            if removed_this:
                removed_total += 1
            # 2. Supprimer cookie (empêche reconnexion automatique)
            try:
                cookie_res = api.get_resource("/ip/hotspot/cookie")
                for c in cookie_res.get():
                    c_usr = str(c.get("user","")).strip()
                    c_mac = str(c.get("mac-address","")).strip().lower()
                    if (t["user"] and c_usr == t["user"]) or \
                       (t["mac"]  and c_mac == t["mac"]):
                        cid = c.get(".id") or c.get("id")
                        if cid:
                            try:
                                cookie_res.remove(id=cid)
                            except Exception:
                                pass
            except Exception:
                pass
            # 3. Supprimer hôte (force reconnexion via portail)
            try:
                host_res = api.get_resource("/ip/hotspot/host")
                for h in host_res.get():
                    h_ip  = str(h.get("address","")).strip()
                    h_mac = str(h.get("mac-address","")).strip().lower()
                    if (t["ip"]  and h_ip  == t["ip"]) or \
                       (t["mac"] and h_mac == t["mac"]):
                        hid = h.get(".id") or h.get("id")
                        if hid:
                            try:
                                host_res.remove(id=hid)
                            except Exception:
                                pass
            except Exception:
                pass

        if removed_total == 0:
            msg = last_err if last_err else "Session introuvable ou deja fermee."
            return jsonify({"ok": False, "msg": msg})
        return jsonify({"ok": True, "removed": removed_total})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Hotspot : Hotes ─────────────────────────────────────────────────────────

@app.route("/hotspot/hotes")
@app.route("/reseau/appareils", endpoint="reseau_appareils")
@login_required
@router_required
def hotspot_hosts():
    api, err = get_api()
    hosts = []
    if err:
        flash(err, "danger")
    else:
        try:
            hosts = api.get_resource("/ip/hotspot/host").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("hotspot/hosts.html", hosts=hosts)

@app.route("/hotspot/hotes/supprimer", methods=["POST"])
@app.route("/reseau/appareils/supprimer", methods=["POST"], endpoint="reseau_supprimer_appareil")
@login_required
@router_required
def hotspot_remove_host():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        hid = request.json.get("id")
        api.get_resource("/ip/hotspot/host").remove(id=hid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Hotspot : Liaisons IP ───────────────────────────────────────────────────

@app.route("/hotspot/liaisons-ip")
@app.route("/reseau/reservations-ip", endpoint="reseau_reservations_ip")
@login_required
@router_required
def hotspot_ip_bindings():
    api, err = get_api()
    bindings = []
    if err:
        flash(err, "danger")
    else:
        try:
            bindings = api.get_resource("/ip/hotspot/ip-binding").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("hotspot/ip_bindings.html", bindings=bindings)

@app.route("/hotspot/liaisons-ip/ajouter", methods=["POST"])
@app.route("/reseau/reservations-ip/ajouter", methods=["POST"], endpoint="reseau_ajouter_reservation")
@login_required
@router_required
def hotspot_add_binding():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        data = request.json
        params = {"type": data.get("type", "regular")}
        if data.get("mac"):
            params["mac-address"] = data["mac"]
        if data.get("address"):
            params["address"] = data["address"]
        if data.get("to_address"):
            params["to-address"] = data["to_address"]
        if data.get("server"):
            params["server"] = data["server"]
        api.get_resource("/ip/hotspot/ip-binding").add(**params)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/hotspot/liaisons-ip/supprimer", methods=["POST"])
@app.route("/reseau/reservations-ip/supprimer", methods=["POST"], endpoint="reseau_supprimer_reservation")
@login_required
@router_required
def hotspot_remove_binding():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        bid = request.json.get("id")
        api.get_resource("/ip/hotspot/ip-binding").remove(id=bid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/hotspot/liaisons-ip/basculer", methods=["POST"])
@app.route("/reseau/reservations-ip/basculer", methods=["POST"], endpoint="reseau_basculer_reservation")
@login_required
@router_required
def hotspot_toggle_binding():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        bid      = request.json.get("id")
        disabled = request.json.get("disabled", "yes")
        api.get_resource("/ip/hotspot/ip-binding").set(id=bid, disabled=disabled)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Hotspot : Cookies ───────────────────────────────────────────────────────

@app.route("/hotspot/cookies")
@app.route("/reseau/jetons", endpoint="reseau_jetons")
@login_required
@router_required
def hotspot_cookies():
    api, err = get_api()
    cookies = []
    if err:
        flash(err, "danger")
    else:
        try:
            cookies = api.get_resource("/ip/hotspot/cookie").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("hotspot/cookies.html", cookies=cookies)

@app.route("/hotspot/cookies/supprimer", methods=["POST"])
@app.route("/reseau/jetons/supprimer", methods=["POST"], endpoint="reseau_supprimer_jeton")
@login_required
@router_required
def hotspot_remove_cookie():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        cid = request.json.get("id")
        api.get_resource("/ip/hotspot/cookie").remove(id=cid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Impression Rapide ───────────────────────────────────────────────────────

@app.route("/impression-rapide")
@login_required
@router_required
def quick_print():
    api, err = get_api()
    quick_items, profiles, servers = [], [], []
    router_id = session.get("router_id", "")
    profiles_meta = {}
    if err:
        flash(err, "danger")
    else:
        try:
            scripts = api.get_resource("/system/script").get(**{"comment": "KetaMonQuickPrint"})
            for s in scripts:
                src = s.get("source", "")
                parts = src.split("#")
                quick_items.append({
                    "id":      s.get("id", ""),
                    "name":    s.get("name", ""),
                    "profile": parts[1] if len(parts) > 1 else "",
                    "server":  parts[2] if len(parts) > 2 else "",
                    "mode":    parts[3] if len(parts) > 3 else "",
                    "length":  parts[4] if len(parts) > 4 else "",
                    "prefix":  parts[5] if len(parts) > 5 else "",
                    "qty":     parts[6] if len(parts) > 6 else "1",
                    "price":   parts[7] if len(parts) > 7 else "0",
                    "network": parts[8] if len(parts) > 8 else "",
                })
            profiles = api.get_resource("/ip/hotspot/user/profile").get()
            servers  = api.get_resource("/ip/hotspot").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
        profiles_meta = get_hotspot_profile_metadata_map(router_id)
    return render_template("quick_print.html", quick_items=quick_items,
                           profiles=profiles, servers=servers, profiles_meta=profiles_meta)


@app.route("/impression-rapide/generer", methods=["POST"], endpoint="quick_print_generer")
@login_required
@router_required
def quick_print_generer():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    data = request.get_json(silent=True) or {}
    script_id = data.get("id", "")
    try:
        scripts = api.get_resource("/system/script").get(id=script_id)
        if not scripts:
            return jsonify({"ok": False, "msg": "Modèle introuvable."})
        s = scripts[0]
        src = s.get("source", "")
        parts = src.split("#")
        profile = parts[1] if len(parts) > 1 else "default"
        server  = parts[2] if len(parts) > 2 else ""
        mode    = parts[3] if len(parts) > 3 else "aleatoire"
        length  = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 8
        prefix  = parts[5] if len(parts) > 5 else ""
        qty     = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 1
        price   = parts[7] if len(parts) > 7 else "0"
        network = parts[8] if len(parts) > 8 else ""
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Erreur lecture modèle: {e}"})
    router_id = session.get("router_id", "")
    if mode == "chiffres":
        charset = string.digits
    elif mode == "lettres":
        charset = string.ascii_lowercase
    else:
        charset = string.ascii_lowercase + string.digits
    ticket_time_limit = get_profile_time_limit(router_id, profile) or "0"
    profiles_meta = get_hotspot_profile_metadata_map(router_id)
    meta = profiles_meta.get(profile, {})
    currency = meta.get("currency", "FCFA") or "FCFA"
    if not price or price == "0":
        price = meta.get("price", "0") or "0"
    date_str = datetime.now().strftime("%Y-%m-%d")
    generated = []
    pricing_batch = []
    hotspot_resource = api.get_resource("/ip/hotspot/user")
    for _ in range(qty):
        rand = "".join(random.choices(charset, k=length))
        name = prefix + rand
        password = name
        params = {
            "name": name, "password": password, "profile": profile,
            "disabled": "no", "comment": "vc-",
            "limit-uptime": ticket_time_limit,
        }
        if server:
            params["server"] = server
        try:
            hotspot_resource.add(**params)
            generated.append({
                "name": name, "password": password, "profile": profile,
                "price": price, "currency": currency, "network": network,
                "date": date_str, "time_limit": ticket_time_limit,
            })
            pricing_batch.append({
                "router_id": router_id, "user": name,
                "prix": float(price) if price and price != "0" else 0.0,
                "devise": currency, "profil": profile, "reseau": network,
            })
        except Exception:
            pass
    if pricing_batch:
        db_mod.db_batch_upsert_ticket_pricing(pricing_batch)
    return jsonify({"ok": True, "tickets": generated, "count": len(generated)})


@app.route("/impression-rapide/creer", methods=["POST"], endpoint="quick_print_creer")
@login_required
@router_required
def quick_print_creer():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    data = request.get_json(silent=True) or {}
    name    = (data.get("name", "") or "").strip()
    profile = (data.get("profile", "default") or "default").strip()
    server  = (data.get("server", "") or "").strip()
    mode    = data.get("mode", "aleatoire")
    length  = int(data.get("length", 8))
    prefix  = (data.get("prefix", "") or "").strip()
    qty     = int(data.get("qty", 1))
    price   = (data.get("price", "0") or "0").strip()
    network = (data.get("network", "") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Le nom est requis."})
    source = f"#{profile}#{server}#{mode}#{length}#{prefix}#{qty}#{price}#{network}"
    try:
        api.get_resource("/system/script").add(name=name, source=source, comment="KetaMonQuickPrint")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/impression-rapide/supprimer", methods=["POST"], endpoint="quick_print_supprimer")
@login_required
@router_required
def quick_print_supprimer():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    data = request.get_json(silent=True) or {}
    sid = data.get("id", "")
    try:
        api.get_resource("/system/script").remove(id=sid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Bons de connexion ───────────────────────────────────────────────────────

@app.route("/bons")
@login_required
@router_required
def vouchers():
    api, err = get_api()
    profiles, vouchers_data = [], {}
    router_id = session.get("router_id", "")
    profiles_meta = {}
    if err:
        flash(err, "danger")
    else:
        try:
            profiles = api.get_resource("/ip/hotspot/user/profile").get()
        except Exception as e:
            _flash_err("Erreur lors du chargement des profils.", e)
        try:
            all_users = api.get_resource("/ip/hotspot/user").get()
            for u in all_users:
                pname = u.get("profile", "default")
                vouchers_data.setdefault(pname, []).append(u)
        except Exception as e:
            _flash_err("Erreur lors du chargement des utilisateurs.", e)
        if not profiles and vouchers_data:
            profiles = [{"name": k} for k in vouchers_data]
        profiles_meta = get_hotspot_profile_metadata_map(router_id)
    return render_template("vouchers.html", profiles=profiles, vouchers_data=vouchers_data,
                           profiles_meta=profiles_meta)



# ─── Journaux ────────────────────────────────────────────────────────────────

@app.route("/journaux/hotspot")
@login_required
@router_required
def log_hotspot():
    api, err = get_api()
    logs = []
    if err:
        flash(err, "danger")
    else:
        try:
            all_logs = api.get_resource("/log").get()
            logs = [l for l in all_logs if "hotspot" in l.get("topics", "")]
            logs = list(reversed(logs))[:200]
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("logs/hotspot_log.html", logs=logs)

@app.route("/journaux/utilisateurs")
@login_required
@router_required
def log_users():
    api, err = get_api()
    logs = []
    if err:
        flash(err, "danger")
    else:
        try:
            all_logs = api.get_resource("/log").get()
            relevant = ("account", "hotspot", "system", "wireless", "manager")
            logs = [l for l in all_logs if any(t in l.get("topics", "") for t in relevant)]
            logs = list(reversed(logs))[:300]
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("logs/user_log.html", logs=logs)

# ─── Baux DHCP ───────────────────────────────────────────────────────────────

@app.route("/baux-dhcp")
@login_required
@router_required
def dhcp_leases():
    api, err = get_api()
    leases = []
    if err:
        flash(err, "danger")
    else:
        try:
            leases = api.get_resource("/ip/dhcp-server/lease").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("dhcp.html", leases=leases)


@app.route("/baux-dhcp/liberer", methods=["POST"], endpoint="dhcp_liberer")
@login_required
@router_required
def dhcp_liberer():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    data = request.get_json(silent=True) or {}
    lid = data.get("id", "")
    try:
        api.get_resource("/ip/dhcp-server/lease").remove(id=lid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/baux-dhcp/rendre-statique", methods=["POST"], endpoint="dhcp_rendre_statique")
@login_required
@router_required
def dhcp_rendre_statique():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    data = request.get_json(silent=True) or {}
    lid = data.get("id", "")
    try:
        api.get_resource("/ip/dhcp-server/lease").call("make-static", {".id": lid})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Moniteur Trafic ─────────────────────────────────────────────────────────

@app.route("/moniteur-trafic")
@login_required
@router_required
def traffic_monitor():
    api, err = get_api()
    interfaces = []
    if err:
        flash(err, "danger")
    else:
        try:
            interfaces = api.get_resource("/interface").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("traffic.html", interfaces=interfaces)

@app.route("/api/interfaces")
@login_required
@router_required
def api_interfaces():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "interfaces": []})
    try:
        ifaces = api.get_resource("/interface").get()
        result = []
        for i in ifaces:
            name = str(i.get("name", ""))
            if name:
                result.append({
                    "name": name,
                    "type": str(i.get("type", "")),
                    "running": str(i.get("running", "false")).lower() == "true",
                })
        return jsonify({"ok": True, "interfaces": result})
    except Exception as e:
        return jsonify({"ok": False, "interfaces": [], "msg": str(e)})

# Stockage du dernier état d'interface pour calcul différentiel du débit
_last_iface_bytes = {}  # clé: "router_id:iface" → (rx_bytes, tx_bytes, timestamp)


@app.route("/api/trafic")
@login_required
@router_required
def api_traffic():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        iface     = request.args.get("interface", "ether1")
        router_id = session.get("router_id", "")
        data = api.get_resource("/interface").get(**{"name": iface})
        if not data:
            return jsonify({"ok": False, "msg": "Interface introuvable"})
        d = data[0]
        rx_bytes = int(d.get("rx-byte", 0) or 0)
        tx_bytes = int(d.get("tx-byte", 0) or 0)
        now      = time.time()
        key      = f"{router_id}:{iface}"
        last     = _last_iface_bytes.get(key)
        if last:
            prev_rx, prev_tx, prev_t = last
            elapsed = now - prev_t
            rx_rate = int(max(0, (rx_bytes - prev_rx) / elapsed)) if elapsed > 0 else 0
            tx_rate = int(max(0, (tx_bytes - prev_tx) / elapsed)) if elapsed > 0 else 0
        else:
            rx_rate = tx_rate = 0
        _last_iface_bytes[key] = (rx_bytes, tx_bytes, now)
        return jsonify({
            "ok": True,
            "rx": rx_bytes, "tx": tx_bytes,
            "rx_rate": rx_rate, "tx_rate": tx_rate,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── API Revenus (réels, MikroTik) ──────────────────────────────────────────

def _parse_vente_source(src):
    """Parse 'date=X heure=X user=X profil=X prix=X devise=X reseau=X' → dict."""
    out = {}
    for part in str(src or "").split():
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out

@app.route("/api/sync-ventes", methods=["POST"])
@login_required
@router_required
def api_sync_ventes():
    """Sync manuelle du routeur actif — retourne immédiatement le nombre de nouveaux tickets."""
    try:
        router = get_active_router()
        if not router:
            return jsonify({"ok": False, "msg": "Aucun routeur actif", "new": 0})
        new_count = _sync_ventes_for_router(router, timeout=8)
        return jsonify({"ok": True, "new": new_count})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "new": 0})


@app.route("/api/sync-etat")
@login_required
def api_sync_etat():
    """Retourne le statut de la dernière sync pour le routeur actif."""
    router_id = session.get("router_id", "")
    stats = _sync_stats.get(router_id, {})
    return jsonify({"ok": True, "stats": stats})


@app.route("/api/revenus")
@login_required
@router_required
def api_revenus():
    try:
        router_id = session.get("router_id", "")
        today_str = datetime.now().strftime("%Y-%m-%d")
        month_str = datetime.now().strftime("%Y-%m")
        s = db_mod.db_get_ventes_summary(router_id, today_str, month_str)
        return jsonify({
            "ok": True,
            "today_count": s["today_count"], "today_total": round(s["today_total"], 0),
            "month_count": s["month_count"], "month_total": round(s["month_total"], 0),
            "currency":    s["currency"],
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e),
                        "today_count": 0, "today_total": 0.0,
                        "month_count": 0, "month_total": 0.0,
                        "currency": "FCFA"})

# ─── Rapport de ventes ───────────────────────────────────────────────────────

@app.route("/rapport")
@login_required
@router_required
def report():
    router_id    = session.get("router_id", "")
    filtre_jour   = request.args.get("jour", "")
    filtre_mois   = request.args.get("mois", "")
    filtre_annee  = request.args.get("annee", "")
    filtre_q      = request.args.get("q", "")
    filtre_profil = request.args.get("profil", "")

    today_str = datetime.now().strftime("%Y-%m-%d")
    month_str = datetime.now().strftime("%Y-%m")

    # Mois cible pour suppression : mois filtré ou mois courant
    if filtre_annee and filtre_mois:
        mois_filtre = f"{filtre_annee}-{filtre_mois.zfill(2)}"
    elif filtre_mois:
        mois_filtre = f"{datetime.now().year}-{filtre_mois.zfill(2)}"
    elif filtre_annee:
        mois_filtre = f"{filtre_annee}-{datetime.now().strftime('%m')}"
    else:
        mois_filtre = month_str

    summ = db_mod.db_get_ventes_summary(router_id, today_str, month_str)
    rows = db_mod.db_get_ventes(router_id, jour=filtre_jour.zfill(2) if filtre_jour else "",
                                mois=filtre_mois.zfill(2) if filtre_mois else "",
                                annee=filtre_annee, q=filtre_q, profil=filtre_profil)

    # Total global (toutes périodes confondues)
    conn = db_mod.get_conn()
    total_row = conn.execute(
        "SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes WHERE router_id=?",
        (router_id,)
    ).fetchone()
    total_global_count = total_row[0] if total_row else 0
    total_global       = float(total_row[1]) if total_row else 0.0

    # Profils distincts pour le filtre
    profils_rows = conn.execute(
        "SELECT DISTINCT profil FROM ventes WHERE router_id=? ORDER BY profil", (router_id,)
    ).fetchall()
    profils_disponibles = [r[0] for r in profils_rows if r[0]]

    sales = [{
        "id":    r["id"],
        "date":  r["date"],
        "heure": r["heure"],
        "user":  r["user"],
        "profil":r["profil"],
        "prix":  r["prix"],
        "devise":r["devise"],
        "reseau":r["reseau"],
    } for r in rows]

    return render_template("report.html", sales=sales, total=len(sales),
                           today_count=summ["today_count"], today_total=summ["today_total"],
                           month_count=summ["month_count"], month_total=summ["month_total"],
                           month_str=month_str,
                           currency=summ["currency"],
                           total_global=total_global,
                           total_global_count=total_global_count,
                           mois_filtre=mois_filtre,
                           profils_disponibles=profils_disponibles)

@app.route("/rapport/supprimer-mois", methods=["POST"])
@login_required
@router_required
def report_supprimer_mois():
    try:
        router_id = session.get("router_id", "")
        mois = (request.json or {}).get("mois", "")
        if not mois:
            return jsonify({"ok": False, "msg": "Mois requis"})
        deleted = db_mod.db_delete_ventes_mois(router_id, mois)
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/rapport/supprimer-vente", methods=["POST"])
@login_required
@router_required
def report_supprimer_vente():
    try:
        router_id = session.get("router_id", "")
        vid = (request.json or {}).get("id", "")
        if not vid:
            return jsonify({"ok": False, "msg": "ID requis"})
        ok = db_mod.db_delete_vente(vid, router_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Systeme ─────────────────────────────────────────────────────────────────

@app.route("/systeme/planificateur")
@login_required
@router_required
def system_scheduler():
    api, err = get_api()
    schedulers = []
    if err:
        flash(err, "danger")
    else:
        try:
            schedulers = api.get_resource("/system/scheduler").get()
        except Exception as e:
            _flash_err("Une erreur est survenue.", e)
    return render_template("system/scheduler.html", schedulers=schedulers)

@app.route("/systeme/planificateur/basculer", methods=["POST"])
@login_required
@router_required
def system_scheduler_toggle():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        sid      = request.json.get("id")
        disabled = request.json.get("disabled", "yes")
        api.get_resource("/system/scheduler").set(id=sid, disabled=disabled)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/systeme/planificateur/supprimer", methods=["POST"])
@login_required
@router_required
def system_scheduler_remove():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        sid = request.json.get("id")
        api.get_resource("/system/scheduler").remove(id=sid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/systeme/redemarrer", methods=["POST"])
@login_required
@router_required
def system_reboot():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        api.get_resource("/system").call("reboot")
    except Exception:
        pass  # MikroTik coupe la connexion immédiatement au reboot — c'est attendu
    return jsonify({"ok": True})

@app.route("/systeme/eteindre", methods=["POST"])
@login_required
@router_required
def system_shutdown():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        api.get_resource("/system").call("shutdown")
    except Exception:
        pass  # MikroTik coupe la connexion immédiatement
    return jsonify({"ok": True})

# ─── Parametres : Sessions / Routeurs ────────────────────────────────────────

@app.route("/parametres/routeurs")
@login_required
def sessions_list():
    routers = get_routers()
    return render_template("settings/routers.html", routers=routers)

@app.route("/parametres/routeurs/ajouter", methods=["GET", "POST"])
@login_required
def settings_add_router():
    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        host     = request.form.get("host", "").strip()
        port     = db_mod._normalize_port(request.form.get("port", 8728))
        user     = request.form.get("user", "admin").strip() or "admin"
        password = request.form.get("password", "")
        currency = request.form.get("currency", "FCFA").strip()

        if not name or not host:
            flash("Nom et hote sont obligatoires.", "danger")
        else:
            owner_id = _current_owner_id() or ""
            db_mod.db_add_router({
                "id": str(uuid.uuid4()),
                "name": name,
                "host": host,
                "port": port,
                "user": user,
                "password": password,
                "currency": currency,
                "created_at": datetime.now().isoformat(),
            }, owner_id=owner_id)
            flash(f"Routeur \"{name}\" enregistre. Cliquez sur Connecter pour tester.", "success")
            return redirect(url_for("sessions_list"))
    return render_template("settings/add_router.html")

@app.route("/parametres/routeurs/<rid>/connecter")
@login_required
def connect_router(rid):
    routers = get_routers()
    for r in routers:
        if r["id"] == rid:
            router_user = r.get("user") or r.get("username") or "admin"
            api_test, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728), driver=r.get("driver", "mikrotik"))
            if err:
                _flash_err("Connexion au routeur echouee. Verifiez les parametres (hote, port, identifiants).", err)
                return redirect(url_for("sessions_list"))
            session["router_id"]   = r["id"]
            session["router_name"] = r["name"]
            flash(f"Connecte a \"{r['name']}\".", "success")
            return redirect(url_for("dashboard"))
    flash("Routeur introuvable.", "danger")
    return redirect(url_for("sessions_list"))

@app.route("/api/test-connexion", methods=["POST"])
@login_required
def api_test_connexion():
    """Teste la connexion à un routeur MikroTik sans le sélectionner."""
    data = request.get_json(silent=True) or {}
    host = str(data.get("host", "")).strip()
    user = str(data.get("user", "admin")).strip() or "admin"
    pwd  = str(data.get("password", ""))
    port = int(data.get("port", 8728) or 8728)
    if not host:
        return jsonify({"ok": False, "msg": "Adresse IP manquante."})
    try:
        api_t, err = mk.safe_connect(host, user, pwd, port, timeout=8)
        if err:
            return jsonify({"ok": False, "msg": f"Connexion echouee : {err}"})
        identity = api_t.get_resource("/system/identity").get()
        name = identity[0].get("name", host) if identity else host
        return jsonify({"ok": True, "msg": f"Connexion reussie — routeur : {name}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/parametres/routeurs/<rid>/supprimer", methods=["POST"])
@login_required
def delete_router(rid):
    owner_id = _current_owner_id()
    deleted = db_mod.db_delete_router(rid, owner_id=owner_id)
    if not deleted:
        flash("Routeur introuvable ou non autorise.", "danger")
        return redirect(url_for("sessions_list"))
    if session.get("router_id") == rid:
        session.pop("router_id", None)
        session.pop("router_name", None)
    flash("Routeur supprime.", "success")
    return redirect(url_for("sessions_list"))

@app.route("/parametres/routeurs/<rid>/modifier", methods=["GET", "POST"])
@login_required
def edit_router(rid):
    routers = get_routers()
    router  = next((r for r in routers if r["id"] == rid), None)
    if not router:
        flash("Routeur introuvable.", "danger")
        return redirect(url_for("sessions_list"))
    if request.method == "POST":
        fields = {
            "name":     request.form.get("name", router["name"]).strip(),
            "host":     request.form.get("host", router["host"]).strip(),
            "port":     request.form.get("port", router.get("port", 8728)),
            "user":     (request.form.get("user", router.get("user") or "admin").strip() or "admin"),
            "currency": request.form.get("currency", router.get("currency", "FCFA")).strip(),
        }
        pwd = request.form.get("password", "")
        if pwd:
            fields["password"] = pwd
        db_mod.db_update_router(rid, _current_owner_id(), fields)
        flash("Routeur modifie.", "success")
        return redirect(url_for("sessions_list"))
    return render_template("settings/edit_router.html", router=router)

@app.route("/parametres/deconnecter-routeur")
@login_required
def disconnect_router():
    session.pop("router_id", None)
    session.pop("router_name", None)
    flash("Routeur deconnecte.", "info")
    return redirect(url_for("sessions_list"))

# ─── Parametres : Compte ─────────────────────────────────────────────────────

@app.route("/parametres/compte", methods=["GET", "POST"])
@login_required
def settings_account():
    if request.method == "POST":
        uid          = session.get("user_id", "")
        current_pass = request.form.get("current_password", "")
        new_pass     = request.form.get("new_password", "")
        confirm_pass = request.form.get("confirm_password", "")
        if not current_pass or not new_pass:
            flash("Tous les champs sont obligatoires.", "danger")
        elif new_pass != confirm_pass:
            flash("Les deux nouveaux mots de passe ne correspondent pas.", "danger")
        else:
            user_row = db_mod.db_get_local_user(uid)
            if not user_row:
                flash("Compte introuvable.", "danger")
            elif not check_password_hash(user_row.get("password", ""), current_pass):
                flash("Mot de passe actuel incorrect.", "danger")
            elif db_mod.db_update_local_user_password(uid, generate_password_hash(new_pass)):
                flash("Mot de passe modifie avec succes.", "success")
            else:
                flash("Erreur lors de la mise a jour.", "warning")
    return render_template("settings/account.html")

# ─── Upload Logo ─────────────────────────────────────────────────────────────

@app.route("/parametres/logo", methods=["GET", "POST"])
@login_required
def upload_logo():
    if request.method == "POST":
        f = request.files.get("logo")
        if f and f.filename:
            fname = secure_filename(f.filename)
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED_LOGO_EXT:
                flash("Type de fichier non autorise.", "danger")
            else:
                try:
                    f.save(os.path.join(LOGOS_DIR, fname))
                    flash(f"Logo \"{fname}\" uploade et applique aux tickets.", "success")
                except Exception as e:
                    _flash_err("Erreur lors de l upload du fichier.", e)
    logos = list_uploaded_logos()
    active_logo = get_active_ticket_logo()
    return render_template("settings/upload_logo.html", logos=logos, active_logo=active_logo)

@app.route("/logos/<filename>")
def serve_logo(filename):
    return send_from_directory(LOGOS_DIR, filename)

# ─── A propos ────────────────────────────────────────────────────────────────

@app.route("/a-propos")
@login_required
def about():
    import sys
    try:
        version = get_app_version()
    except Exception:
        version = "1.0.0"
    import flask as _fl
    return render_template("about.html",
        version=version,
        python_version=sys.version.split()[0],
        flask_version=_fl.__version__,
        active_router=get_active_router()
    )

@app.route("/contact")
def contact():
    return render_template("contact.html", active_router=None)

@app.route("/politique-de-confidentialite")
def privacy():
    return render_template("privacy.html", active_router=None)

@app.route("/sw.js")
def service_worker():
    from flask import send_from_directory
    return send_from_directory(app.static_folder, "sw.js",
                               mimetype="application/javascript")

# ─── API Log Hotspot (live dashboard) ────────────────────────────────────────

@app.route("/api/log-hotspot")
@login_required
@router_required
def api_log_hotspot():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err, "logs": []})
    try:
        all_logs = api.get_resource("/log").get()
        hs_logs = [l for l in all_logs if "hotspot" in l.get("topics", "")]
        hs_logs = list(reversed(hs_logs))[:20]
        out = []
        for l in hs_logs:
            msg = l.get("message", "")
            user = ""; ip = ""
            # Extraire user et IP du message type "user BXFBZ9455 logged in from 10.10.10.53"
            import re as _re
            m = _re.search(r"user\s+(\S+)", msg, _re.IGNORECASE)
            if m: user = m.group(1)
            m2 = _re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", msg)
            if m2: ip = m2.group(1)
            out.append({"time": l.get("time",""), "message": msg, "user": user, "ip": ip})
        return jsonify({"ok": True, "logs": out})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "logs": []})

# ─── API Systeme Ressources (live) ────────────────────────────────────────────

@app.route("/api/ressources")
@login_required
@router_required
def api_resources():
    api, err = get_api()
    if err:
        return jsonify({"ok": False, "msg": err})
    try:
        res       = api.get_resource("/system/resource").get()[0]
        total_mem = int(res.get("total-memory", 1) or 1)
        free_mem  = int(res.get("free-memory", 0)  or 0)
        total_hdd = int(res.get("total-hdd-space", 0) or 0)
        free_hdd  = int(res.get("free-hdd-space",  0) or 0)
        try:
            rb    = api.get_resource("/system/routerboard").get()[0]
            board = rb.get("model", res.get("board-name", "—"))
        except Exception:
            board = res.get("board-name", "—")
        return jsonify({
            "ok":       True,
            "cpu":      res.get("cpu-load", "0"),
            "uptime":   res.get("uptime", ""),
            "version":  res.get("version", ""),
            "board":    board,
            "free_mem": mk.format_bytes(free_mem),
            "mem_pct":  round((total_mem - free_mem) / total_mem * 100) if total_mem else 0,
            "free_hdd": mk.format_bytes(free_hdd) if total_hdd else "—",
            "hdd_pct":  round((total_hdd - free_hdd) / total_hdd * 100) if total_hdd else 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── Concepteur : Tableau de bord analytique ─────────────────────────────────

def concepteur_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if session.get("role") != "concepteur":
            flash("Acces reserve au concepteur.", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def legacy_platform_boundary_response(json_mode=False):
    message = "Fonction desactivee dans le mode legacy. Utilisez le backend FastAPI SaaS officiel."
    if json_mode:
        return jsonify({"ok": False, "message": message}), 403
    flash(message, "warning")
    return redirect(url_for("concepteur_dashboard"))

@app.route("/concepteur")
@concepteur_required
def concepteur_dashboard():
    token = session.get("ks_token")
    stats, users_data, analytics = {}, [], {}

    if token:
        s, _ = ks_get("/api/admin/stats", token)
        if s and s.get("ok"):
            stats = s
        u, _ = ks_get("/api/admin/users", token)
        if u and u.get("ok"):
            users_data = u.get("users", [])
        ana, _ = ks_get("/api/admin/analytics/summary", token)
        if ana and ana.get("ok"):
            analytics = {"logs": ana.get("logs", [])}
        # Stats publicités
        ads_s, _ = ks_get("/api/ads/stats", token)
        if ads_s and isinstance(ads_s, dict):
            analytics["ads"] = {
                "today":        ads_s.get("today", {}).get("views", 0),
                "revenueToday": ads_s.get("today", {}).get("revenue", 0),
            }

    # Revenus réels depuis SQLite (toutes les sessions)
    today_str = datetime.now().strftime("%Y-%m-%d")
    month_str = datetime.now().strftime("%Y-%m")
    conn = db_mod.get_conn()
    day_row   = dict(conn.execute("SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes WHERE date=?", (today_str,)).fetchone())
    month_row = dict(conn.execute("SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes WHERE substr(date,1,7)=?", (month_str,)).fetchone())
    total_row = dict(conn.execute("SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes").fetchone())
    cur_row   = conn.execute("SELECT devise FROM ventes ORDER BY created_at DESC LIMIT 1").fetchone()
    cur = dict(cur_row)["devise"] if cur_row else "FCFA"

    # Revenus par routeur
    by_router_rows = conn.execute(
        "SELECT router_id, COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes GROUP BY router_id"
    ).fetchall()
    routers_map = {r["id"]: r["name"] for r in db_mod.db_get_routers()}
    by_router = [
        {"_id": routers_map.get(r["router_id"], r["router_id"]),
         "count": r["cnt"], "used": r["cnt"], "total": int(r["tot"])}
        for r in by_router_rows
    ]

    # Statuts tickets (dans SQLite, tous sont "used" ; on montre aussi les non-utilisés sur MikroTik)
    total_mk_users = 0
    active_router_info = get_active_router()
    if active_router_info:
        try:
            host = active_router_info.get("host","")
            ruser = active_router_info.get("user") or "admin"
            pwd = active_router_info.get("password","")
            port = int(active_router_info.get("port") or 8728)
            api2, err2 = mk.safe_connect(host, ruser, pwd, port)
            if not err2:
                all_u = api2.get_resource("/ip/hotspot/user").get()
                total_mk_users = len(all_u)
        except Exception:
            pass
    used_count = total_row["cnt"]
    unused_count = max(0, total_mk_users - used_count)
    status = {
        "used":    used_count,
        "unused":  unused_count,
        "new":     0,
        "expired": 0,
    }

    revenue = {
        "day":      {"count": day_row["cnt"],   "value": int(day_row["tot"])},
        "month":    {"count": month_row["cnt"], "value": int(month_row["tot"])},
        "total":    {"count": total_row["cnt"], "value": int(total_row["tot"])},
        "currency": cur,
        "status":   status,
        "byRouter": by_router,
    }
    return render_template("concepteur/dashboard.html",
        stats=stats, users_data=users_data, revenue=revenue, analytics=analytics)

@app.route("/concepteur/utilisateurs")
@concepteur_required
def concepteur_users():
    token = session.get("ks_token")
    users_data = []
    is_local = False
    if token:
        u, _ = ks_get("/api/admin/users", token)
        if u and u.get("ok"):
            users_data = u.get("users", [])
    if not users_data:
        is_local = True
        for lu in db_mod.db_get_local_users():
            users_data.append({
                "id":          lu.get("id", ""),
                "email":       lu.get("email") or lu.get("username", ""),
                "displayName": lu.get("display_name") or lu.get("username", ""),
                "role":        lu.get("role", "utilisateur"),
                "routerCount": 0,
                "loginCount":  0,
                "lastLoginAt": None,
                "createdAt":   lu.get("created_at", ""),
                "active":      int(lu.get("disabled", 0)) == 0,
            })
    return render_template("concepteur/users.html", users_data=users_data, is_local=is_local)

@app.route("/concepteur/utilisateurs/<uid>/supprimer", methods=["POST"])
@concepteur_required
def concepteur_delete_user(uid):
    token = session.get("ks_token")
    if token:
        resp, err = ks_delete(f"/api/admin/users/{uid}", token)
        if err:
            return jsonify({"ok": False, "msg": err})
        return jsonify(resp or {"ok": False})
    # Fallback : supprimer l'utilisateur local
    try:
        conn = db_mod.get_conn()
        rows = conn.execute("DELETE FROM local_users WHERE id=?", (uid,)).rowcount
        conn.commit()
        if rows:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "msg": "Utilisateur introuvable."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/concepteur/utilisateurs/<uid>/basculer", methods=["POST"])
@concepteur_required
def concepteur_toggle_user(uid):
    token = session.get("ks_token")
    if token:
        resp, err = ks_patch(f"/api/admin/users/{uid}/toggle", token)
        if err:
            return jsonify({"ok": False, "msg": err})
        return jsonify(resp or {"ok": False})
    # Fallback : basculer disabled dans la base locale
    try:
        conn = db_mod.get_conn()
        row = conn.execute("SELECT disabled FROM local_users WHERE id=?", (uid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "msg": "Utilisateur introuvable."})
        new_val = 0 if int(row["disabled"] or 0) else 1
        conn.execute("UPDATE local_users SET disabled=? WHERE id=?", (new_val, uid))
        conn.commit()
        return jsonify({"ok": True, "active": new_val == 0})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/concepteur/services")
@concepteur_required
def concepteur_services():
    token = session.get("ks_token")
    gateway_status, cloud_status = "unknown", "running"
    if token:
        g, _ = ks_get("/api/admin/services/gateway/status", token)
        if g:
            gateway_status = g.get("state", "unknown")
        c, _ = ks_get("/api/admin/services/cloud/status", token)
        if c:
            cloud_status = c.get("state", "running")
    return render_template("concepteur/services.html",
        gateway_status=gateway_status, cloud_status=cloud_status)

@app.route("/concepteur/services/<name>/action", methods=["POST"])
@concepteur_required
def concepteur_service_action(name):
    token = session.get("ks_token")
    if not token:
        return jsonify({"ok": False, "msg": "Non authentifie"})
    action = request.json.get("action", "restart")
    resp, err = ks_post(f"/api/admin/services/{name}/action", {"action": action}, token)
    if err:
        return jsonify({"ok": False, "msg": err})
    return jsonify(resp or {"ok": False})

@app.route("/concepteur/backup")
@concepteur_required
def concepteur_backup():
    token = session.get("ks_token")
    backups = []
    is_local = False
    if token:
        b, _ = ks_get("/api/admin/backup/list", token)
        if b and b.get("ok"):
            backups = b.get("backups", [])
    if not backups:
        is_local = True
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        try:
            for fname in os.listdir(data_dir):
                fpath = os.path.join(data_dir, fname)
                if os.path.isfile(fpath):
                    fsize = os.path.getsize(fpath)
                    fsize_str = f"{fsize // 1024} KB" if fsize >= 1024 else f"{fsize} B"
                    fmod = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M")
                    backups.append({"name": fname, "createdAt": fmod, "size": fsize_str})
        except Exception:
            pass
    return render_template("concepteur/backup.html", backups=backups, is_local=is_local)

@app.route("/concepteur/backup/lancer", methods=["POST"])
@concepteur_required
def concepteur_run_backup():
    token = session.get("ks_token")
    if not token:
        return jsonify({"ok": False, "msg": "Non authentifie"})
    resp, err = ks_post("/api/admin/backup/run", {}, token)
    if err:
        return jsonify({"ok": False, "msg": err})
    return jsonify(resp or {"ok": False})

@app.route("/concepteur/credentials", methods=["GET", "POST"])
@concepteur_required
def concepteur_credentials():
    token = session.get("ks_token")
    msg = None
    if request.method == "POST" and token:
        data = {
            "currentPassword": request.form.get("current_password", ""),
            "newUsername":     request.form.get("new_username", ""),
            "newPassword":     request.form.get("new_password", ""),
            "displayName":     request.form.get("display_name", ""),
        }
        resp, err = ks_patch("/api/admin/concepteur/credentials", token, data)
        if err:
            _flash_err("Erreur de communication avec KetaServer.")
        elif resp and resp.get("ok"):
            flash("Identifiants mis a jour.", "success")
        else:
            flash(resp.get("message", "Erreur.") if resp else "Erreur.", "danger")
    return render_template("concepteur/credentials.html")

# ─── Abonnement Premium (utilisateurs) ───────────────────────────────────────

@app.route("/abonnement")
@login_required
def abonnement():
    uid    = session.get("user_id", "")
    sub    = get_user_subscription(uid)
    # Demande en attente de validation (si pas d'abo actif)
    pending_sub = None
    if not sub:
        all_user_subs = db_mod.db_get_subscriptions(user_id=uid)
        pending_sub = next((s for s in all_user_subs if s.get("statut") == "en_attente"), None)
    plans  = [p for p in get_plans() if p.get("actif")]
    cfg    = get_pay_config()
    methodes_actives = [m for m in cfg.get("methodes", []) if m.get("actif") and m.get("numero")]
    return render_template("abonnement.html", plans=plans, sub=sub, pending_sub=pending_sub,
                           methodes=methodes_actives, cfg=cfg)

@app.route("/abonnement/souscrire", methods=["POST"])
@login_required
def abonnement_souscrire():
    uid          = session.get("user_id", "")
    plan_id      = request.form.get("plan_id", "")
    ref_pay      = request.form.get("reference_paiement", "").strip().upper()
    methode      = request.form.get("methode", "")
    montant_paye = request.form.get("montant_paye", "0").strip()
    devise_paye  = request.form.get("devise_paye", "FCFA").strip().upper()

    cfg   = get_pay_config()
    plans = get_plans()
    plan  = next((p for p in plans if p["id"] == plan_id and p.get("actif")), None)

    if not plan:
        flash("Plan invalide.", "danger")
        return redirect(url_for("abonnement"))

    # ── Vérification anti-fraude ──────────────────────────────────────────────
    ok, flags, detail = verifier_paiement_antifraude(
        plan, montant_paye, devise_paye, ref_pay, methode, cfg
    )

    if "REFERENCE_DOUBLON" in flags:
        flash("Cette reference de paiement est deja utilisee.", "danger")
        return redirect(url_for("abonnement"))

    prix_base  = to_base(plan["prix"], plan["devise"], cfg)
    paye_base  = to_base(float(montant_paye or 0), devise_paye, cfg)

    new_sub = {
        "id":           str(uuid.uuid4()),
        "user_id":      uid,
        "username":     session.get("username", uid),
        "plan_id":      plan_id,
        "plan_nom":     plan["nom"],
        "prix_plan":    plan["prix"],
        "devise_plan":  plan["devise"],
        "prix_plan_base": round(prix_base, 2),
        "montant_paye": float(montant_paye or 0),
        "devise_paye":  devise_paye,
        "montant_base": round(paye_base, 2),
        "devise_base":  cfg.get("devise_base", "FCFA"),
        "duree_jours":  plan["duree"],
        "methode":      methode,
        "reference":    ref_pay,
        "fraude_flags": flags,
        "fraude_detail":detail,
        "statut":       "rejete" if not ok else "en_attente",
        "demande_le":   datetime.utcnow().isoformat(),
        "active_le":    None,
        "expire_le":    None,
    }

    # Insertion thread-safe dans SQLite
    try:
        db_mod.db_insert_subscription(new_sub)
    except db_mod.DuplicateReferenceError:
        flash("Cette reference de paiement est deja utilisee.", "danger")
        return redirect(url_for("abonnement"))

    # Enregistrer dans MongoDB via KetaServer si token dispo
    token = session.get("ks_token")
    if token:
        ks_post("/api/payments/mobile-money/checkout", {
            "planId": plan_id, "montant": float(montant_paye or 0),
            "utilisateur": uid, "reference": ref_pay, "methode": methode,
        }, token)

    if not ok:
        flash(f"Paiement rejeté : {detail}", "danger")
    else:
        flash("Demande envoyée. Le concepteur va vérifier et valider votre paiement.", "success")
    return redirect(url_for("abonnement"))

# ─── Concepteur : Abonnements ─────────────────────────────────────────────────

@app.route("/concepteur/abonnements")
@concepteur_required
def concepteur_abonnements():
    subs  = db_mod.db_get_subscriptions()
    plans = get_plans()
    return render_template("concepteur/abonnements.html", subs=subs, plans=plans)

@app.route("/concepteur/abonnements/<sid>/valider", methods=["POST"])
@concepteur_required
def concepteur_valider_abo(sid):
    from datetime import timedelta
    subs = db_mod.db_get_subscriptions()
    sub  = next((s for s in subs if s["id"] == sid), None)
    if not sub:
        return jsonify({"ok": False, "msg": "Introuvable"})
    plans = get_plans()
    plan  = next((p for p in plans if p["id"] == sub["plan_id"]), None)
    duree = plan["duree"] if plan else sub.get("duree_jours", 30)
    now   = datetime.utcnow()
    exp   = (now + timedelta(days=int(duree))).isoformat()
    db_mod.db_update_sub_statut(sid, "actif", now.isoformat(), exp)
    return jsonify({"ok": True, "expire_le": exp})

@app.route("/concepteur/abonnements/<sid>/revoquer", methods=["POST"])
@concepteur_required
def concepteur_revoquer_abo(sid):
    db_mod.db_update_sub_statut(sid, "revoque")
    return jsonify({"ok": True})

@app.route("/concepteur/abonnements/<sid>/supprimer", methods=["POST"])
@concepteur_required
def concepteur_supprimer_abo(sid):
    db_mod.db_delete_sub(sid)
    return jsonify({"ok": True})

@app.route("/concepteur/plans", methods=["GET", "POST"])
@concepteur_required
def concepteur_plans():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "ajouter":
            db_mod.db_save_plan({
                "id":    str(uuid.uuid4())[:8],
                "nom":   request.form.get("nom", "").strip(),
                "duree": safe_int(request.form.get("duree", 30), default=30, min_val=1),
                "prix":  float(request.form.get("prix", 0) or 0),
                "devise":request.form.get("devise", "FCFA"),
                "actif": 1,
            })
            flash("Plan ajouté.", "success")
        elif action == "modifier":
            pid = request.form.get("plan_id")
            plans = get_plans()
            p = next((x for x in plans if x["id"] == pid), None)
            if p:
                db_mod.db_save_plan({
                    "id":    pid,
                    "nom":   request.form.get("nom", p["nom"]).strip(),
                    "duree": safe_int(request.form.get("duree", p["duree"]), default=safe_int(p["duree"], 30), min_val=1),
                    "prix":  float(request.form.get("prix", p["prix"]) or p["prix"]),
                    "devise":request.form.get("devise", p["devise"]),
                    "actif": 1 if request.form.get("actif") == "1" else 0,
                })
                flash("Plan mis à jour.", "success")
        elif action == "supprimer":
            db_mod.db_delete_plan(request.form.get("plan_id", ""))
            flash("Plan supprimé.", "success")
        return redirect(url_for("concepteur_plans"))

    return render_template("concepteur/plans.html", plans=get_plans())

# ─── Concepteur : Config paiements ──────────────────────────────────────────

@app.route("/concepteur/paiements-config", methods=["GET", "POST"])
@concepteur_required
def concepteur_paiements_config():
    cfg = get_pay_config()
    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "taux":
            taux = cfg.get("taux_change", {})
            for devise in ["USD", "EUR", "XOF"]:
                val = request.form.get(f"taux_{devise}", "")
                try:
                    taux[devise] = float(val)
                except ValueError:
                    pass
            cfg["taux_change"]  = taux
            cfg["tolerance_pct"]= float(request.form.get("tolerance_pct", 0) or 0)
            cfg["devise_base"]  = request.form.get("devise_base", "FCFA")
            flash("Taux de change mis à jour.", "success")

        elif action == "methode":
            mid    = request.form.get("methode_id", "")
            numero = request.form.get("numero", "").strip()
            instru = request.form.get("instructions", "").strip()
            actif  = request.form.get("actif") == "1"
            methodes = cfg.get("methodes", [])
            found = False
            for m in methodes:
                if m["id"] == mid:
                    m["numero"]       = numero
                    m["instructions"] = instru
                    m["actif"]        = actif
                    found = True
                    break
            if not found:
                methodes.append({"id": mid, "nom": mid, "numero": numero,
                                 "instructions": instru, "actif": actif})
            cfg["methodes"] = methodes
            flash(f"Méthode '{mid}' mise à jour.", "success")

        save_pay_config(cfg)
        return redirect(url_for("concepteur_paiements_config"))

    return render_template("concepteur/paiements_config.html", cfg=cfg)

# ─── Concepteur : Tickets ────────────────────────────────────────────────────

@app.route("/concepteur/tickets")
@concepteur_required
def concepteur_tickets():
    today_str = datetime.now().strftime("%Y-%m-%d")
    month_str = datetime.now().strftime("%Y-%m")
    conn = db_mod.get_conn()
    day_r   = dict(conn.execute("SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes WHERE date=?", (today_str,)).fetchone())
    mon_r   = dict(conn.execute("SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes WHERE substr(date,1,7)=?", (month_str,)).fetchone())
    total_r = dict(conn.execute("SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot FROM ventes").fetchone())
    summary = {
        "day":   {"count": day_r["cnt"],   "generatedValue": int(day_r["tot"])},
        "month": {"count": mon_r["cnt"],   "generatedValue": int(mon_r["tot"])},
        "total": {"count": total_r["cnt"], "generatedValue": int(total_r["tot"])},
    }
    # Tickets utilisés (revenus comptés)
    rows = conn.execute("SELECT * FROM ventes ORDER BY date DESC, heure DESC LIMIT 500").fetchall()
    tickets_used = [{
        "_id":        r["id"],
        "code":       r["user"],
        "routerHost": r["router_id"],
        "status":     "used",
        "price":      r["prix"],
        "currency":   r["devise"],
        "plan":       r["profil"],
        "soldAt":     r["date"] + "T" + r["heure"],
        "reseau":     r["reseau"],
    } for r in rows]

    # Tickets non utilisés (stock sur MikroTik : bytes-in = 0)
    stock = []
    used_names = set(r["user"] for r in rows)
    routers_map = {r["id"]: r for r in db_mod.db_get_routers()}
    for router_info in db_mod.db_get_routers():
        try:
            rhost = router_info.get("host","")
            ruser = router_info.get("user") or "admin"
            rpwd  = router_info.get("password","")
            rport = int(router_info.get("port") or 8728)
            rid   = router_info.get("id") or rhost
            api2, err2 = mk.safe_connect(rhost, ruser, rpwd, rport)
            if err2:
                continue
            mk_users = api2.get_resource("/ip/hotspot/user").get()
            for u in mk_users:
                try:
                    bi = int(u.get("bytes-in", 0) or 0)
                except Exception:
                    bi = 0
                name = str(u.get("name","") or "").strip()
                if not name or bi > 0 or name in used_names:
                    continue
                disabled = str(u.get("disabled","false")).lower() in ("true","yes","1")
                if disabled:
                    continue
                profile = str(u.get("profile","") or "")
                # Chercher le prix dans ticket_pricing
                tp = conn.execute(
                    "SELECT prix, devise FROM ticket_pricing WHERE router_id=? AND user=?",
                    (rid, name)
                ).fetchone()
                price = float(tp[0]) if tp else 0.0
                currency = tp[1] if tp else "FCFA"
                stock.append({
                    "code":    name,
                    "router":  router_info.get("name", rhost),
                    "profile": profile,
                    "price":   int(price),
                    "currency":currency,
                })
        except Exception:
            continue

    return render_template("concepteur/tickets.html",
        summary=summary, tickets=tickets_used, stock=stock)

@app.route("/concepteur/tickets/<tid>/supprimer", methods=["POST"])
@concepteur_required
def concepteur_delete_ticket(tid):
    conn = db_mod.get_conn()
    conn.execute("DELETE FROM ventes WHERE id=?", (tid,))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/concepteur/tickets/<tid>/basculer", methods=["POST"])
@concepteur_required
def concepteur_toggle_ticket(tid):
    return jsonify({"ok": True})

@app.route("/concepteur/tickets/sync", methods=["POST"])
@concepteur_required
def concepteur_sync_tickets():
    total = db_mod.get_conn().execute("SELECT COUNT(*) FROM ventes").fetchone()[0]
    return jsonify({"ok": True, "msg": f"{total} ventes en base SQLite."})

# ─── Concepteur : Paiements ───────────────────────────────────────────────────

@app.route("/concepteur/paiements")
@concepteur_required
def concepteur_paiements():
    subs = db_mod.db_get_subscriptions()
    today_str = datetime.now().strftime("%Y-%m-%d")
    month_str = datetime.now().strftime("%Y-%m")
    plans = {p["id"]: p for p in get_plans()}
    history = []
    today_amt = 0.0; month_amt = 0.0; total_amt = 0.0
    for s in subs:
        plan = plans.get(s.get("plan_id", ""), {})
        prix = float(plan.get("prix", 0))
        devise = plan.get("devise", "FCFA")
        date_dem = (s.get("demande_le") or "")[:10]
        total_amt += prix
        if date_dem.startswith(month_str): month_amt += prix
        if date_dem == today_str: today_amt += prix
        history.append({
            "reference": s.get("id","")[:8],
            "amount":    int(prix),
            "currency":  devise,
            "provider":  s.get("methode_paiement","—"),
            "plan":      plan.get("nom", s.get("plan_id","—")),
            "status":    s.get("statut","—"),
            "username":  s.get("user_id","—"),
            "createdAt": s.get("demande_le",""),
        })
    summary = {
        "total": {"count": len(subs), "amount": int(total_amt)},
        "month": {"amount": int(month_amt)},
        "day":   {"amount": int(today_amt)},
    }
    return render_template("concepteur/paiements.html", summary=summary, history=history)

# ─── Concepteur : Publicités ──────────────────────────────────────────────────

@app.route("/concepteur/pubs")
@concepteur_required
def concepteur_pubs():
    token = session.get("ks_token")
    stats, config = {}, {}
    if token:
        s, _ = ks_get("/api/ads/stats", token)
        stats = s or {}
        c, _ = ks_get("/api/ads/config", token)
        config = c or {}
    return render_template("concepteur/pubs.html", stats=stats, config=config)

@app.route("/concepteur/pubs/config", methods=["POST"])
@concepteur_required
def concepteur_pubs_config():
    token = session.get("ks_token")
    existing = load_ad_config()
    data = {
        "bannerUnitId":         request.form.get("banner_unit_id", "").strip(),
        "interstitialUnitId":   request.form.get("interstitial_unit_id", "").strip(),
        "eCpmEstimate":         safe_int(request.form.get("ecpm_estimate", 0), default=0, min_val=0),
        # AdSense Web
        "adsensePubId":         request.form.get("adsense_pub_id", "").strip(),
        "adsenseBannerSlot":    request.form.get("adsense_banner_slot", "").strip(),
        "adsenseInterSlot":     request.form.get("adsense_inter_slot", "").strip(),
    }
    existing.update(data)
    save_ad_config(existing)
    if token:
        resp, err = ks_patch("/api/ads/config", token, data)
        if err:
            _flash_err("Erreur de communication avec KetaServer.")
        elif resp and resp.get("ok"):
            flash("Configuration publicités mise à jour.", "success")
        else:
            flash((resp.get("msg") or resp.get("message") or "Erreur.") if resp else "Erreur.", "danger")
    else:
        flash("Configuration sauvegardée localement.", "success")
    return redirect(url_for("concepteur_pubs"))


# ─── API publique pour l'app Android ─────────────────────────────────────────

@app.route("/api/ads/units", methods=["GET"])
def api_ads_units():
    """Retourne les Unit IDs AdMob configurés — appelé par l'app Android au démarrage."""
    cfg = load_ad_config()
    return jsonify({
        "bannerUnitId":       cfg.get("bannerUnitId", ""),
        "interstitialUnitId": cfg.get("interstitialUnitId", ""),
    })

@app.route("/api/ads/report", methods=["POST"])
def api_ads_report():
    """Enregistre une vue publicitaire depuis l'app Android."""
    data = request.get_json(silent=True) or {}
    ad_type = data.get("type", "banner") if data.get("type") in ("banner", "interstitial") else "banner"
    today   = datetime.now().strftime("%Y-%m-%d")

    views       = load_ad_views()
    day_data    = views.get(today, {"banner": 0, "interstitial": 0, "total": 0})
    day_data[ad_type] = day_data.get(ad_type, 0) + 1
    day_data["total"] = day_data.get("total", 0) + 1
    views[today] = day_data

    # Garder seulement les 90 derniers jours
    all_days = sorted(views.keys(), reverse=True)
    views = {d: views[d] for d in all_days[:90]}
    save_ad_views(views)

    # Transmission best-effort vers KetaServer cloud si token concepteur disponible
    token = session.get("ks_token")
    if token:
        ks_post("/api/ads/view", {"type": ad_type, "routerHost": data.get("routerHost", "")}, token)

    return jsonify({"ok": True})

@app.route("/api/ads/local-stats", methods=["GET"])
@login_required
def api_ads_local_stats():
    """Stats de vues pub locales — pour le dashboard concepteur."""
    views = load_ad_views()
    today = datetime.now().strftime("%Y-%m-%d")
    cfg   = load_ad_config()
    ecpm  = safe_int(cfg.get("eCpmEstimate", 0), default=0, min_val=0)

    def rev(v): return round(v * ecpm / 1000, 0)

    today_total = views.get(today, {}).get("total", 0)
    month_prefix = datetime.now().strftime("%Y-%m")
    month_total = sum(v.get("total", 0) for k, v in views.items() if k.startswith(month_prefix))
    total = sum(v.get("total", 0) for v in views.values())

    return jsonify({
        "today":  {"views": today_total,  "revenue": rev(today_total)},
        "month":  {"views": month_total,  "revenue": rev(month_total)},
        "total":  {"views": total,        "revenue": rev(total)},
        "currency": "FCFA",
    })


# ─── API Android : tickets hotspot ───────────────────────────────────────────

@app.route("/api/ping")
def api_ping():
    """Test de connectivité + validation des identifiants pour l'app Android."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    return jsonify({"ok": True, "service": "ketamon"})


@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    """Stats MikroTik en temps réel pour l'app Android (auth HTTP Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401

    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503

    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503

    try:
        res   = api.get_resource("/system/resource").get()[0]
        ident = api.get_resource("/system/identity").get()[0]
        hs_active  = resource_count(api, "/ip/hotspot/active")
        hs_tickets = resource_count(api, "/ip/hotspot/user")
        total_mem  = int(res.get("total-memory", 0))
        free_mem   = int(res.get("free-memory", 0))
        mem_pct    = round((total_mem - free_mem) / total_mem * 100) if total_mem else 0
        return jsonify({
            "ok":        True,
            "identity":  ident.get("name", "MikroTik"),
            "version":   res.get("version", "?"),
            "uptime":    res.get("uptime", "?"),
            "cpu_load":  str(res.get("cpu-load", "0")),
            "mem_pct":   mem_pct,
            "hs_active": hs_active,
            "hs_tickets": hs_tickets,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/vouchers", methods=["POST"])
def api_create_voucher():
    """Crée un ticket hotspot depuis l'app Android (auth HTTP Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401

    data    = request.get_json(silent=True) or {}
    code    = str(data.get("code", "")).strip()
    profile = str(data.get("profile", "default")).strip() or "default"
    uptime  = str(data.get("uptime", "1h")).strip() or "1h"

    if not code or not re.match(r"^[A-Za-z0-9\-_]{1,64}$", code):
        return jsonify({"ok": False, "msg": "Code invalide"}), 400
    if not re.match(r"^\d+[smhd]$", uptime):
        return jsonify({"ok": False, "msg": "Format durée invalide (ex: 1h, 30m)"}), 400
    if not re.match(r"^[\w\-]{1,64}$", profile):
        return jsonify({"ok": False, "msg": "Profil invalide"}), 400

    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503

    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503

    try:
        try:
            ensure_ticket_runtime_support(api, profile)
        except Exception:
            pass  # Ne pas bloquer la création si les scripts échouent
        api.get_resource("/ip/hotspot/user").add(
            name=code,
            password=code,
            profile=profile,
            disabled="no",
            comment=f"vc-{datetime.now().strftime('%d/%m %H:%M')}",
            **{"limit-uptime": uptime}
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/vouchers/summary", methods=["GET"])
def api_hotspot_vouchers_summary():
    """Résumé des bons de connexion par profil (nombre de tickets + métadonnées)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503
    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503
    try:
        users    = api.get_resource("/ip/hotspot/user").get()
        profiles = api.get_resource("/ip/hotspot/user/profile").get()
        router_id     = r.get("id", "") or r.get("host", "")
        profiles_meta = get_hotspot_profile_metadata_map(router_id)
        count_by_profil = {}
        for u in users:
            p = str(u.get("profile", "default")).strip()
            count_by_profil[p] = count_by_profil.get(p, 0) + 1
        result = [{"nom": "all", "count": len(users), "prix": "", "duree": ""}]
        for p in profiles:
            nom  = str(p.get("name", "")).strip()
            meta = profiles_meta.get(nom, {})
            prix_val  = str(meta.get("price", "0") or "0")
            devise    = str(meta.get("currency", "FCFA") or "FCFA")
            duree_val = normalize_ticket_time_limit(str(meta.get("time_limit", "0") or "0")) or "0"
            result.append({
                "nom":   nom,
                "count": count_by_profil.get(nom, 0),
                "prix":  f"{prix_val} {devise}" if prix_val != "0" else "",
                "duree": duree_val if duree_val != "0" else "",
            })
        return jsonify({"ok": True, "profiles": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/vouchers/generate", methods=["POST"])
def api_hotspot_vouchers_generate():
    """Génère N bons de connexion pour un profil depuis l'app Android."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data   = request.get_json(silent=True) or {}
    profil = str(data.get("profil", "default")).strip() or "default"
    qty    = safe_int(data.get("qty", 1), default=1, min_val=1, max_val=50)
    if not re.match(r"^[\w\-]{1,64}$", profil):
        return jsonify({"ok": False, "msg": "Profil invalide"}), 400
    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503
    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503
    try:
        router_id         = r.get("id", "") or r.get("host", "")
        ticket_time_limit = get_profile_time_limit(router_id, profil) or "0"
        try:
            ensure_ticket_runtime_support(api, profil)
        except Exception:
            pass
        resource  = api.get_resource("/ip/hotspot/user")
        generated = []
        for _ in range(qty):
            code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=8))
            try:
                resource.add(
                    name=code, password=code, profile=profil,
                    disabled="no",
                    comment=f"vc-{datetime.now().strftime('%d/%m %H:%M')}",
                    **{"limit-uptime": ticket_time_limit}
                )
                generated.append(code)
            except Exception:
                pass  # Ignorer les doublons éventuels
        return jsonify({"ok": True, "codes": generated, "count": len(generated)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/users", methods=["GET"])
def api_hotspot_users():
    """Liste les utilisateurs hotspot (tickets + sessions actives) depuis MikroTik."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503
    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503
    try:
        users    = api.get_resource("/ip/hotspot/user").get()
        sessions = api.get_resource("/ip/hotspot/active").get()
        active_by_user = {}
        for s in sessions:
            uname = str(s.get("user", "")).strip()
            if uname and uname not in active_by_user:
                active_by_user[uname] = s
        result = []
        for u in users:
            nom  = str(u.get("name", "")).strip()
            sess = active_by_user.get(nom)
            is_disabled = str(u.get("disabled", "no")).strip().lower() == "yes"
            if is_disabled:
                etat = "desactive"
            elif sess:
                etat = "actif"
            else:
                etat = "hors_ligne"
            mac = str(u.get("mac-address", "") or (sess.get("mac-address", "") if sess else ""))
            result.append({
                "id":        str(u.get(".id", "")),
                "nom":       nom,
                "profil":    str(u.get("profile", "default")),
                "etat":      etat,
                "ip":        str(sess.get("address", "") if sess else ""),
                "mac":       mac,
                "bytesIn":   str(sess.get("bytes-in", "0") if sess else "0"),
                "bytesOut":  str(sess.get("bytes-out", "0") if sess else "0"),
                "uptime":    str(sess.get("uptime", "") if sess else ""),
                "sessionId": str(sess.get(".id", "") if sess else ""),
            })
        return jsonify({"ok": True, "users": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/users/disconnect", methods=["POST"])
def api_hotspot_disconnect():
    """Déconnecte un utilisateur actif du hotspot."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data = request.get_json(silent=True) or {}
    nom  = str(data.get("nom", "")).strip()
    if not nom or not re.match(r"^[\w\-\.@]{1,64}$", nom):
        return jsonify({"ok": False, "msg": "Nom utilisateur invalide"}), 400
    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503
    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503
    try:
        active_rows = find_matching_hotspot_active_rows(api, usernames=[nom])
        usernames, addresses, mac_addresses, active_ids = build_active_disconnect_targets(active_rows)
        disconnected = disconnect_hotspot_entities(
            api, usernames=usernames or [nom],
            addresses=addresses, mac_addresses=mac_addresses, active_ids=active_ids,
        )
        return jsonify({"ok": True, "disconnected": disconnected})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/profiles", methods=["GET"])
def api_hotspot_profiles():
    """Liste les profils hotspot depuis MikroTik avec leurs métadonnées prix/durée."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503
    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503
    try:
        profiles      = api.get_resource("/ip/hotspot/user/profile").get()
        router_id     = r.get("id", "") or r.get("host", "")
        profiles_meta = get_hotspot_profile_metadata_map(router_id)
        result = []
        for p in profiles:
            nom  = str(p.get("name", "")).strip()
            meta = profiles_meta.get(nom, {})
            prix_val  = str(meta.get("price", "0") or "0")
            devise    = str(meta.get("currency", "FCFA") or "FCFA")
            duree_val = normalize_ticket_time_limit(str(meta.get("time_limit", "0") or "0")) or "0"
            result.append({
                "nom":   nom,
                "debit": str(p.get("rate-limit", "") or "illimité"),
                "prix":  f"{prix_val} {devise}" if prix_val != "0" else "",
                "duree": duree_val if duree_val != "0" else "",
            })
        return jsonify({"ok": True, "profiles": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/tickets/list", methods=["GET"])
def api_list_tickets():
    """Liste les tickets hotspot réels depuis MikroTik (auth HTTP Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401

    routers = get_routers()
    if not routers:
        return jsonify({"ok": False, "msg": "Aucun routeur configuré"}), 503

    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    if err:
        return jsonify({"ok": False, "msg": "Routeur inaccessible : " + err}), 503

    try:
        users = api.get_resource("/ip/hotspot/user").get()
        tickets = []
        now = datetime.now()
        marker = KETAMON_TICKET_COMMENT_MARKER

        for u in users:
            comment  = str(u.get("comment", ""))
            duree_raw = str(u.get("limit-uptime", "0") or "0")
            statut   = "actif"
            expire_at = ""

            marker_pos = comment.find(marker)
            if marker_pos != -1:
                expire_raw = comment[marker_pos + len(marker):].strip()
                try:
                    # Format RouterOS : "jan/27/2026 15:30:00"
                    expire_dt = datetime.strptime(expire_raw[:19], "%b/%d/%Y %H:%M:%S")
                    expire_at = expire_dt.strftime("%d/%m %H:%M")
                    statut = "expire" if now >= expire_dt else "utilise"
                except Exception:
                    statut = "utilise"

            clean_comment = strip_ticket_runtime_comment(comment)
            # Extraire la date de création du préfixe "vc-DD/MM HH:MM"
            if clean_comment.startswith("vc-"):
                cree_le = clean_comment[3:].strip()
            else:
                cree_le = clean_comment
            tickets.append({
                "code":      u.get("name", ""),
                "profil":    u.get("profile", "default"),
                "duree":     duree_raw,
                "cree_le":   cree_le,
                "statut":    statut,
                "expire_at": expire_at,
            })

        return jsonify({"ok": True, "tickets": tickets})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ─── Concepteur : Mise à jour de l'application ───────────────────────────────

VERSION_FILE = os.path.join(os.path.dirname(__file__), "data", "version.txt")

def get_app_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "1.0.0"

def set_app_version(v):
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(str(v).strip())

@app.route("/api/version")
def api_version():
    """Endpoint public — retourne la version actuelle (pour auto-check dans le navigateur)."""
    return jsonify({"version": get_app_version(), "ok": True})


# ─── API Android : journaux, système, DHCP, interfaces, rapport ───────────────

def _mk_connect_first_router():
    """Connecte au premier routeur configuré. Retourne (api, erreur_str|None)."""
    routers = get_routers()
    if not routers:
        return None, "Aucun routeur configuré"
    r = routers[0]
    router_user = r.get("user") or r.get("username") or "admin"
    api, err = mk.safe_connect(r["host"], router_user, r.get("password", ""), r.get("port", 8728))
    return (api, err) if not err else (None, err)

def _fmt_bytes(b):
    b = int(b or 0)
    if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} Go"
    if b >= 1_048_576:     return f"{b/1_048_576:.0f} Mo"
    if b >= 1_024:         return f"{b/1_024:.0f} Ko"
    return f"{b} o"


@app.route("/api/network/traffic", methods=["GET"])
def api_network_traffic():
    """Débit réseau en temps réel — 2 mesures à 1s d'écart (auth Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        ifaces1 = {i.get("name", ""): i for i in api.get_resource("/interface").get()}
        time.sleep(1)
        ifaces2 = {i.get("name", ""): i for i in api.get_resource("/interface").get()}
        result = []
        for nom, i2 in ifaces2.items():
            disabled = str(i2.get("disabled", "no")).lower() == "yes"
            running  = str(i2.get("running",  "no")).lower() == "yes"
            i1       = ifaces1.get(nom, {})
            rx1 = int(i1.get("rx-byte", 0) or 0)
            rx2 = int(i2.get("rx-byte", 0) or 0)
            tx1 = int(i1.get("tx-byte", 0) or 0)
            tx2 = int(i2.get("tx-byte", 0) or 0)
            rx_bps = max(0, rx2 - rx1)
            tx_bps = max(0, tx2 - tx1)
            result.append({
                "nom":      nom,
                "actif":    running and not disabled,
                "desactive": disabled,
                "rxBps":    (_fmt_bytes(rx_bps) + "/s") if rx_bps > 0 else "0 o/s",
                "txBps":    (_fmt_bytes(tx_bps) + "/s") if tx_bps > 0 else "0 o/s",
                "_rx":      rx_bps,
                "_tx":      tx_bps,
            })
        max_bps = max((max(r["_rx"], r["_tx"]) for r in result), default=1) or 1
        for r in result:
            r["rxPct"] = min(100, round(r["_rx"] / max_bps * 100))
            r["txPct"] = min(100, round(r["_tx"] / max_bps * 100))
            del r["_rx"]; del r["_tx"]
        return jsonify({"ok": True, "interfaces": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/network/ping", methods=["GET"])
def api_network_ping():
    """Ping depuis MikroTik vers une cible (auth Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    hote  = request.args.get("hote", "").strip()
    count = min(safe_int(request.args.get("count", 4), default=4, min_val=1, max_val=10), 10)
    if not hote:
        return jsonify({"ok": False, "msg": "Hôte obligatoire"}), 400
    if not re.match(r'^[\w.\-:]+$', hote):
        return jsonify({"ok": False, "msg": "Cible invalide"}), 400
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        # api.get_resource("") -> _Resource(lrt, "") -> call("ping") -> /ping
        raw = api.get_resource("").call("ping", {"address": hote, "count": str(count)})
        resultats = []
        resume    = {}
        seq = 1
        for r in raw:
            if "time" in r or "status" in r:
                tmo = str(r.get("status", "")).lower() == "timeout" or not r.get("time")
                resultats.append({
                    "seq":    seq,
                    "delai":  str(r.get("time", "timeout")) if not tmo else "timeout",
                    "statut": "timeout" if tmo else "ok",
                    "ttl":    str(r.get("ttl", "")),
                    "taille": str(r.get("size", "64"))
                })
                seq += 1
            elif "sent" in r or "received" in r:
                resume = {
                    "envoyes": str(r.get("sent", count)),
                    "recus":   str(r.get("received", 0)),
                    "pertes":  str(r.get("packet-loss", "?")),
                    "minRtt":  str(r.get("min-rtt", "")),
                    "avgRtt":  str(r.get("avg-rtt", "")),
                    "maxRtt":  str(r.get("max-rtt", ""))
                }
        if not resume and resultats:
            recus = sum(1 for r in resultats if r["statut"] == "ok")
            resume = {
                "envoyes": str(len(resultats)),
                "recus":   str(recus),
                "pertes":  f"{(len(resultats) - recus) * 100 // len(resultats)}%" if resultats else "?",
                "minRtt": "", "avgRtt": "", "maxRtt": ""
            }
        return jsonify({"ok": True, "hote": hote, "resultats": resultats, "resume": resume})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Journal MikroTik temps réel (auth Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    limit = min(safe_int(request.args.get("limit", 150), default=150, min_val=1, max_val=500), 500)
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        entries = api.get_resource("/log").get()
        result = []
        for e in reversed(entries[-limit:]):
            topics = str(e.get("topics", ""))
            if "error" in topics or "critical" in topics:
                type_ = "erreur"
            elif "warning" in topics:
                type_ = "avertissement"
            else:
                type_ = "info"
            result.append({
                "temps":   str(e.get("time", "")),
                "message": str(e.get("message", "")),
                "topics":  topics,
                "type":    type_
            })
        return jsonify({"ok": True, "entries": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/system/info", methods=["GET"])
def api_system_info():
    """Informations système détaillées MikroTik (auth Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        res   = api.get_resource("/system/resource").get()[0]
        ident = api.get_resource("/system/identity").get()[0]
        total_mem = int(res.get("total-memory", 0) or 0)
        free_mem  = int(res.get("free-memory",  0) or 0)
        total_hdd = int(res.get("total-hdd-space", 0) or 0)
        free_hdd  = int(res.get("free-hdd-space",  0) or 0)
        ram_pct   = round((total_mem - free_mem) / total_mem * 100) if total_mem else 0
        modele = str(res.get("board-name", ""))
        try:
            rb = api.get_resource("/system/routerboard").get()[0]
            modele = str(rb.get("model", modele))
        except Exception:
            pass
        return jsonify({
            "ok":            True,
            "identite":      str(ident.get("name", "MikroTik")),
            "version":       str(res.get("version", "?")),
            "uptime":        str(res.get("uptime", "?")),
            "architecture":  str(res.get("architecture-name", "?")),
            "modele":        modele,
            "cpuLoad":       str(res.get("cpu-load", "0")),
            "ramTotal":      _fmt_bytes(total_mem),
            "ramLibre":      _fmt_bytes(free_mem),
            "ramPct":        ram_pct,
            "stockageTotal": _fmt_bytes(total_hdd) if total_hdd else "",
            "stockageLibre": _fmt_bytes(free_hdd)  if total_hdd else "",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/dhcp/leases", methods=["GET"])
def api_dhcp_leases():
    """Baux DHCP actifs depuis MikroTik (auth Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        leases = api.get_resource("/ip/dhcp-server/lease").get()
        result = []
        for l in leases:
            result.append({
                "id":      str(l.get(".id", "")),
                "ip":      str(l.get("active-address", "") or l.get("address", "")),
                "mac":     str(l.get("mac-address", "")),
                "nomHote": str(l.get("host-name", "") or l.get("active-host-name", "") or "—"),
                "statut":  str(l.get("status", "waiting")),
                "expireAt":str(l.get("expires-after", "")),
                "serveur": str(l.get("server", ""))
            })
        return jsonify({"ok": True, "leases": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/network/interfaces", methods=["GET"])
def api_network_interfaces():
    """Interfaces réseau MikroTik avec compteurs trafic (auth Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        interfaces = api.get_resource("/interface").get()
        result = []
        for iface in interfaces:
            disabled = str(iface.get("disabled", "no")).lower() == "yes"
            running  = str(iface.get("running",  "no")).lower() == "yes"
            result.append({
                "nom":        str(iface.get("name", "")),
                "type":       str(iface.get("type", "")),
                "actif":      running and not disabled,
                "desactive":  disabled,
                "rxBytes":    str(iface.get("rx-byte", "0") or "0"),
                "txBytes":    str(iface.get("tx-byte", "0") or "0"),
                "mac":        str(iface.get("mac-address", "")),
                "commentaire":str(iface.get("comment", ""))
            })
        return jsonify({"ok": True, "interfaces": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/rapport", methods=["GET"])
def api_rapport():
    """Rapport agrégé hotspot depuis MikroTik (auth Basic requise)."""
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        users    = api.get_resource("/ip/hotspot/user").get()
        profiles = api.get_resource("/ip/hotspot/user/profile").get()
        active   = api.get_resource("/ip/hotspot/active").get()
        marker   = KETAMON_TICKET_COMMENT_MARKER
        now      = datetime.now()
        actifs = utilises = expires = 0
        count_by_profil = {}
        for u in users:
            comment = str(u.get("comment", ""))
            profil  = str(u.get("profile", "default"))
            count_by_profil[profil] = count_by_profil.get(profil, 0) + 1
            if marker in comment:
                expire_raw = comment[comment.find(marker) + len(marker):].strip()
                try:
                    expire_dt = datetime.strptime(expire_raw[:19], "%b/%d/%Y %H:%M:%S")
                    if now >= expire_dt: expires += 1
                    else:                utilises += 1
                except Exception:
                    utilises += 1
            else:
                actifs += 1
        routers = get_routers()
        router_id     = (routers[0].get("id", "") or routers[0].get("host", "")) if routers else ""
        profiles_meta = get_hotspot_profile_metadata_map(router_id)
        par_profil = []
        for p in profiles:
            nom  = str(p.get("name", "")).strip()
            meta = profiles_meta.get(nom, {})
            prix_val  = str(meta.get("price", "0") or "0")
            devise    = str(meta.get("currency", "FCFA") or "FCFA")
            duree_val = normalize_ticket_time_limit(str(meta.get("time_limit", "0") or "0")) or "0"
            par_profil.append({
                "nom":   nom,
                "count": count_by_profil.get(nom, 0),
                "prix":  f"{prix_val} {devise}" if prix_val != "0" else "",
                "duree": duree_val if duree_val != "0" else ""
            })
        return jsonify({
            "ok":              True,
            "ticketsActifs":   actifs,
            "ticketsUtilises": utilises,
            "ticketsExpires":  expires,
            "ticketsTotal":    len(users),
            "usersActifs":     len(active),
            "parProfil":       par_profil
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ─── API Android : endpoints supplémentaires ─────────────────────────────────

@app.route("/api/hotspot/active", methods=["GET"])
def api_hotspot_active():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        sessions = api.get_resource("/ip/hotspot/active").get()
        result = []
        for s in sessions:
            result.append({
                "id":       str(s.get(".id", "")),
                "nom":      str(s.get("user", "")),
                "mac":      str(s.get("mac-address", "")),
                "ip":       str(s.get("address", "")),
                "serveur":  str(s.get("server", "")),
                "uptime":   str(s.get("uptime", "")),
                "bytesIn":  str(s.get("bytes-in", "0")),
                "bytesOut": str(s.get("bytes-out", "0")),
                "loginBy":  str(s.get("login-by", "")),
            })
        return jsonify({"ok": True, "sessions": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/hosts", methods=["GET"])
def api_hotspot_hosts():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        hosts = api.get_resource("/ip/hotspot/host").get()
        result = []
        for h in hosts:
            result.append({
                "id":      str(h.get(".id", "")),
                "mac":     str(h.get("mac-address", "")),
                "ip":      str(h.get("address", "")),
                "serveur": str(h.get("server", "")),
                "bypass":  str(h.get("to-address", "")),
            })
        return jsonify({"ok": True, "hosts": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/ip-bindings", methods=["GET"])
def api_hotspot_ip_bindings():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        bindings = api.get_resource("/ip/hotspot/ip-binding").get()
        result = []
        for b in bindings:
            result.append({
                "id":     str(b.get(".id", "")),
                "mac":    str(b.get("mac-address", "")),
                "ip":     str(b.get("address", "")),
                "toIp":   str(b.get("to-address", "")),
                "server": str(b.get("server", "")),
                "type":   str(b.get("type", "")),
                "actif":  str(b.get("disabled", "no")).lower() != "yes",
            })
        return jsonify({"ok": True, "bindings": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/cookies", methods=["GET"])
def api_hotspot_cookies():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        cookies = api.get_resource("/ip/hotspot/cookie").get()
        result = []
        for c in cookies:
            result.append({
                "id":     str(c.get(".id", "")),
                "nom":    str(c.get("user", "")),
                "mac":    str(c.get("mac-address", "")),
                "cookie": str(c.get("cookie", "")),
                "expiry": str(c.get("expires-in", "")),
            })
        return jsonify({"ok": True, "cookies": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/system/scheduler", methods=["GET"])
def api_system_scheduler():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        tasks = api.get_resource("/system/scheduler").get()
        result = []
        for t in tasks:
            result.append({
                "id":        str(t.get(".id", "")),
                "nom":       str(t.get("name", "")),
                "startTime": str(t.get("start-time", "")),
                "startDate": str(t.get("start-date", "")),
                "interval":  str(t.get("interval", "")),
                "actif":     str(t.get("disabled", "no")).lower() != "yes",
                "script":    str(t.get("on-event", ""))[:200],
            })
        return jsonify({"ok": True, "taches": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/users/add", methods=["POST"])
def api_hotspot_users_add():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data = request.get_json(silent=True) or {}
    nom = str(data.get("name", "") or data.get("nom", "")).strip()
    password = str(data.get("userPassword", "") or data.get("password", "")).strip()
    profil = str(data.get("profile", "") or data.get("profil", "default")).strip() or "default"
    server = str(data.get("server", "") or data.get("serveur", "")).strip()
    comment = str(data.get("comment", "") or data.get("commentaire", "")).strip()
    limit_uptime = str(data.get("limitUptime", "") or data.get("limit-uptime", "")).strip()
    if not nom or not re.match(r"^[\w\-\.@]{1,64}$", nom):
        return jsonify({"ok": False, "msg": "Nom invalide"}), 400
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        res = api.get_resource("/ip/hotspot/user")
        params = {"name": nom, "profile": profil, "password": password or nom, "disabled": "no", "limit-uptime": "0"}
        if server:
            params["server"] = server
        if comment:
            params["comment"] = comment
        if limit_uptime:
            params["limit-uptime"] = limit_uptime
        res.add(**params)
        return jsonify({"ok": True, "msg": f"Utilisateur {nom} ajouté"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/users/delete", methods=["POST"])
def api_hotspot_users_delete():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data = request.get_json(silent=True) or {}
    nom = str(data.get("name", "") or data.get("nom", "")).strip()
    if not nom:
        return jsonify({"ok": False, "msg": "Nom requis"}), 400
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        res = api.get_resource("/ip/hotspot/user")
        rows = res.get(**{"name": nom})
        if not rows:
            return jsonify({"ok": False, "msg": "Utilisateur introuvable"}), 404
        res.remove(id=rows[0][".id"])
        return jsonify({"ok": True, "msg": f"Utilisateur {nom} supprimé"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/users/toggle", methods=["POST"])
def api_hotspot_users_toggle():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data = request.get_json(silent=True) or {}
    nom = str(data.get("name", "") or data.get("nom", "")).strip()
    if not nom:
        return jsonify({"ok": False, "msg": "Nom requis"}), 400
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        res = api.get_resource("/ip/hotspot/user")
        rows = res.get(**{"name": nom})
        if not rows:
            return jsonify({"ok": False, "msg": "Utilisateur introuvable"}), 404
        uid = rows[0][".id"]
        disable_req = data.get("disabled", data.get("disable", None))
        if disable_req is None:
            disable_req = str(rows[0].get("disabled", "no")).lower() != "yes"
        elif isinstance(disable_req, str):
            disable_req = disable_req.lower() in ("yes", "true", "1")
        res.set(id=uid, disabled="yes" if disable_req else "no")
        state = "désactivé" if disable_req else "activé"
        return jsonify({"ok": True, "msg": f"Utilisateur {nom} {state}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/users/edit", methods=["POST"])
def api_hotspot_users_edit():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data = request.get_json(silent=True) or {}
    nom = str(data.get("name", "") or data.get("nom", "")).strip()
    if not nom:
        return jsonify({"ok": False, "msg": "Nom requis"}), 400
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        res = api.get_resource("/ip/hotspot/user")
        rows = res.get(**{"name": nom})
        if not rows:
            return jsonify({"ok": False, "msg": "Utilisateur introuvable"}), 404
        uid = rows[0][".id"]
        params = {"id": uid}
        new_name = str(data.get("newName", "")).strip()
        if new_name and new_name != nom:
            params["name"] = new_name
        pwd = str(data.get("userPassword", "") or data.get("password", "")).strip()
        if pwd:
            params["password"] = pwd
        profil = str(data.get("profile", "") or data.get("profil", "")).strip()
        if profil:
            params["profile"] = profil
        comment = str(data.get("comment", "") or data.get("commentaire", "")).strip()
        if comment:
            params["comment"] = comment
        tlimit = str(data.get("limitUptime", "") or data.get("limit-uptime", "")).strip()
        if tlimit:
            params["limit-uptime"] = tlimit
        res.set(**params)
        return jsonify({"ok": True, "msg": f"Utilisateur {nom} modifié"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/profiles/add", methods=["POST"])
def api_hotspot_profiles_add():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data = request.get_json(silent=True) or {}
    nom = str(data.get("profileName", "") or data.get("name", "") or data.get("nom", "")).strip()
    if not nom:
        return jsonify({"ok": False, "msg": "Nom requis"}), 400
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        res = api.get_resource("/ip/hotspot/user/profile")
        params = {"name": nom, "shared-users": str(data.get("sharedUsers", "1") or "1")}
        if data.get("rateLimit"):
            params["rate-limit"] = str(data["rateLimit"])
        if data.get("addressPool"):
            params["address-pool"] = str(data["addressPool"])
        res.add(**params)
        routers = get_routers()
        if routers:
            rid = routers[0].get("id", "") or routers[0].get("host", "")
            try:
                db_mod.db_upsert_hotspot_profile_metadata(
                    rid, nom,
                    price=str(data.get("priceCfa", "0") or "0"),
                    currency=str(data.get("currency", "FCFA") or "FCFA"),
                    expire_mode=str(data.get("expiredMode", "none") or "none"),
                    lock_user="yes",
                    time_limit="0",
                )
            except Exception:
                pass
        return jsonify({"ok": True, "msg": f"Profil {nom} ajouté"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hotspot/profiles/delete", methods=["POST"])
def api_hotspot_profiles_delete():
    auth_ok, _ = _check_basic_auth()
    if not auth_ok:
        return jsonify({"ok": False, "msg": "Authentification requise"}), 401
    data = request.get_json(silent=True) or {}
    nom = str(data.get("name", "") or data.get("nom", "")).strip()
    if not nom:
        return jsonify({"ok": False, "msg": "Nom requis"}), 400
    api, err = _mk_connect_first_router()
    if err:
        return jsonify({"ok": False, "msg": err}), 503
    try:
        res = api.get_resource("/ip/hotspot/user/profile")
        rows = res.get(**{"name": nom})
        if not rows:
            return jsonify({"ok": False, "msg": "Profil introuvable"}), 404
        res.remove(id=rows[0][".id"])
        return jsonify({"ok": True, "msg": f"Profil {nom} supprimé"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/concepteur/mise-a-jour")
@concepteur_required
def concepteur_mise_a_jour():
    version = get_app_version()
    git_branch = None
    git_last_commit = None
    try:
        import subprocess
        _base = os.path.dirname(__file__)
        r1 = subprocess.run(["git", "-C", _base, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5)
        git_branch = r1.stdout.strip() or None
        r2 = subprocess.run(["git", "-C", _base, "log", "--oneline", "-1"],
                            capture_output=True, text=True, timeout=5)
        git_last_commit = r2.stdout.strip() or None
    except Exception:
        pass
    return render_template("concepteur/mise_a_jour.html",
                           version=version,
                           git_branch=git_branch,
                           git_last_commit=git_last_commit)

@app.route("/concepteur/mise-a-jour/git-status")
@concepteur_required
def concepteur_git_status():
    try:
        import subprocess
        _base = os.path.dirname(__file__)
        r1 = subprocess.run(["git", "-C", _base, "status"],
                            capture_output=True, text=True, timeout=5)
        r2 = subprocess.run(["git", "-C", _base, "log", "--oneline", "-10"],
                            capture_output=True, text=True, timeout=5)
        output = "=== git status ===\n" + r1.stdout.strip()
        output += "\n\n=== git log (10 derniers) ===\n" + r2.stdout.strip()
        return jsonify({"ok": True, "output": output})
    except FileNotFoundError:
        return jsonify({"ok": False, "output": "Git non disponible sur ce serveur."})
    except Exception as e:
        return jsonify({"ok": False, "output": str(e)})

@app.route("/concepteur/mise-a-jour/git-pull", methods=["POST"])
@concepteur_required
def concepteur_git_pull():
    try:
        import subprocess
        r = subprocess.run(
            ["git", "-C", os.path.dirname(__file__), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=30
        )
        output = (r.stdout + r.stderr).strip()
        ok = r.returncode == 0
        return jsonify({"ok": ok, "output": output})
    except FileNotFoundError:
        return jsonify({"ok": False, "output": "Git non disponible sur ce serveur."})
    except Exception as e:
        return jsonify({"ok": False, "output": str(e)})

@app.route("/concepteur/mise-a-jour/version", methods=["POST"])
@concepteur_required
def concepteur_set_version():
    v = (request.json or {}).get("version", "").strip()
    if not v:
        return jsonify({"ok": False, "msg": "Version vide"})
    set_app_version(v)
    return jsonify({"ok": True, "version": v})

@app.route("/concepteur/mise-a-jour/redemarrer", methods=["POST"])
@concepteur_required
def concepteur_redemarrer():
    """Redémarre l'application après un délai (les connexions actives finissent)."""
    import threading, sys
    def do_restart():
        import time, os
        time.sleep(3)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    t = threading.Thread(target=do_restart, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Redémarrage dans 3 secondes…"})

# ─── Concepteur : Routeurs MongoDB ───────────────────────────────────────────

@app.route("/concepteur/routeurs-cloud")
@concepteur_required
def concepteur_routeurs_cloud():
    routers = db_mod.db_get_routers()
    profiles = [{
        "_id":              r.get("id", ""),
        "name":             r.get("name", "—"),
        "host":             r.get("host", "—"),
        "port":             r.get("port", 8728),
        "hotspotName":      r.get("name", "—"),
        "currency":         r.get("currency", "FCFA"),
        "trafficInterface": "ether1",
    } for r in routers]
    return render_template("concepteur/routeurs_cloud.html", profiles=profiles)

@app.route("/concepteur/routeurs-cloud/ajouter", methods=["GET", "POST"])
@concepteur_required
def concepteur_add_routeur_cloud():
    flash("Pour ajouter un routeur, utilisez Paramètres → Ajouter un routeur.", "info")
    return redirect(url_for("settings_add_router"))

@app.route("/concepteur/routeurs-cloud/<rid>/supprimer", methods=["POST"])
@concepteur_required
def concepteur_delete_routeur_cloud(rid):
    db_mod.db_delete_router(rid, owner_id=None)
    return jsonify({"ok": True})

# ─── Concepteur : Logs & Sécurité ────────────────────────────────────────────

@app.route("/concepteur/logs")
@concepteur_required
def concepteur_logs():
    token = session.get("ks_token")
    logs, security, resets = [], [], []
    if token:
        l, _ = ks_get("/api/admin/logs", token, {"limit": 100})
        logs = l.get("logs", l) if isinstance(l, dict) else (l or [])
        s, _ = ks_get("/api/admin/security", token)
        security = s.get("events", s) if isinstance(s, dict) else (s or [])
        r, _ = ks_get("/api/admin/reset-requests", token)
        resets = r.get("requests", r) if isinstance(r, dict) else (r or [])
    # Fallback : lire les logs MikroTik locaux si pas de KetaServer token
    if not logs:
        try:
            api, err = get_api()
            if not err:
                mk_logs = api.get_resource("/log").get()
                relevant = ("account", "hotspot", "system", "manager", "critical")
                mk_logs = [l for l in mk_logs if any(t in l.get("topics","") for t in relevant)]
                mk_logs = list(reversed(mk_logs))[:50]
                logs = [{
                    "action":    l.get("message",""),
                    "username":  "mikrotik",
                    "routerHost": session.get("router_name",""),
                    "level":     "error" if "error" in l.get("topics","") or "critical" in l.get("topics","") else "info",
                    "createdAt": l.get("time",""),
                } for l in mk_logs]
        except Exception:
            pass
    return render_template("concepteur/logs.html", logs=logs, security=security, resets=resets)

# ─── Concepteur : Base de données ────────────────────────────────────────────

@app.route("/concepteur/base-de-donnees")
@concepteur_required
def concepteur_database():
    token = session.get("ks_token")
    status, stats = {}, {}
    is_local = False
    if token:
        s, _ = ks_get("/api/database/status", token)
        status = s or {}
        st, _ = ks_get("/api/database/stats", token)
        stats = st or {}
    if not status:
        is_local = True
        db_path = os.path.join(os.path.dirname(__file__), "data", "ketamon.db")
        db_size = 0
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
        status = {
            "connected": True,
            "uri": f"SQLite local — {db_path}",
            "dbName": "ketamon.db",
            "pingMs": 0,
        }
        try:
            conn = db_mod.get_conn()
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            collections = []
            for (tname,) in tables:
                cnt = conn.execute(f"SELECT COUNT(*) FROM \"{tname}\"").fetchone()[0]
                collections.append({"name": tname, "count": cnt, "size": "—"})
            size_str = f"{db_size // 1024} KB" if db_size < 1024*1024 else f"{db_size // (1024*1024)} MB"
            stats = {"collections": collections, "totalSize": size_str}
        except Exception:
            stats = {}
    return render_template("concepteur/base_de_donnees.html", status=status, stats=stats, is_local=is_local)

@app.route("/concepteur/base-de-donnees/bootstrap", methods=["POST"])
@concepteur_required
def concepteur_database_bootstrap():
    token = session.get("ks_token")
    if token:
        resp, err = ks_post("/api/database/bootstrap", {}, token)
        if err: return jsonify({"ok": False, "message": err})
        return jsonify(resp or {"ok": True})
    return jsonify({"ok": False, "message": "Non authentifie"})

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", 5001))
    dev_mode = "--dev" in sys.argv or os.environ.get("KETAMON_DEV", "") == "1"
    sys.stdout.reconfigure(encoding="utf-8")
    print("KetaMon -- Gestionnaire MikroTik")
    print(f"http://localhost:{port}")
    if dev_mode:
        print("Mode DEV — rechargement automatique activé")
        app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
        app.run(host="0.0.0.0", port=port, debug=True, use_reloader=True)
    else:
        try:
            from waitress import serve
            print("Serveur Waitress (production) — 1000+ utilisateurs")
            serve(app, host="0.0.0.0", port=port, threads=16,
                  connection_limit=1000, channel_timeout=60)
        except ImportError:
            print("Waitress absent — mode dev (1 utilisateur)")
            app.run(host="0.0.0.0", port=port, debug=False)
