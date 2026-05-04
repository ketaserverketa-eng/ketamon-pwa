"""
Base de données SQLite WAL — thread-safe, 1000+ utilisateurs simultanés.
Gère : plans, abonnements, config paiement.
"""
import sqlite3
import json
import os
import threading
import uuid
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "ketamon.db")
LEGACY_USERS_PATH = os.path.join(DATA_DIR, "users.json")
LEGACY_ROUTERS_PATH = os.path.join(DATA_DIR, "routers.json")
_local  = threading.local()


class DuplicateReferenceError(ValueError):
    """Raised when a payment reference already exists."""

    def __init__(self, reference: str):
        self.reference = reference
        super().__init__(f"Reference already exists: {reference}")


class DuplicateLocalUserError(ValueError):
    """Raised when a local user already exists."""

    def __init__(self, identity: str):
        self.identity = identity
        super().__init__(f"Local user already exists: {identity}")


def _load_json_file(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


_ROUTERS_FERNET = None
try:
    from cryptography.fernet import Fernet
    _rf_key = os.environ.get("KETAMON_ROUTERS_KEY")
    if _rf_key:
        try:
            _ROUTERS_FERNET = Fernet(_rf_key)
        except Exception:
            _ROUTERS_FERNET = None
except Exception:
    _ROUTERS_FERNET = None


def get_conn():
    """Connexion SQLite par thread (thread-local) avec WAL activé."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # lectures non bloquantes
        conn.execute("PRAGMA synchronous=NORMAL") # bon compromis perf/sécurité
        conn.execute("PRAGMA cache_size=-8000")   # 8 Mo de cache
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _decrypt_router_password(value):
    if not value or not _ROUTERS_FERNET:
        return value or ""
    try:
        return _ROUTERS_FERNET.decrypt(str(value).encode()).decode()
    except Exception:
        return value


def _encrypt_router_password(value):
    plain = _decrypt_router_password(value)
    if not plain or not _ROUTERS_FERNET:
        return plain
    try:
        return _ROUTERS_FERNET.encrypt(str(plain).encode()).decode()
    except Exception:
        return plain


def _normalize_port(value):
    try:
        port = int(value or 8728)
    except Exception:
        return 8728
    return port if port > 0 else 8728


def _normalize_router_driver(value):
    driver = str(value or "mikrotik").strip().lower() or "mikrotik"
    try:
        import mikrotik as mk_mod
        if mk_mod.get_driver(driver) is not None:
            return driver
    except Exception:
        pass
    return "mikrotik"


def _normalize_local_user(user):
    username = str(user.get("username") or user.get("email") or "").strip()
    email = str(user.get("email") or (username if "@" in username else "")).strip()
    return {
        "id": str(user.get("id") or uuid.uuid4()),
        "username": username,
        "email": email,
        "password": str(user.get("password") or ""),
        "display_name": str(
            user.get("display_name") or user.get("displayName") or username
        ).strip(),
        "role": str(user.get("role") or "utilisateur").strip() or "utilisateur",
        "created_at": str(user.get("created_at") or datetime.now().isoformat()),
    }


def _normalize_router(router):
    password = _decrypt_router_password(router.get("password") or "")
    return {
        "id": str(router.get("id") or uuid.uuid4()),
        "name": str(router.get("name") or "").strip(),
        "host": str(router.get("host") or "").strip(),
        "port": _normalize_port(router.get("port")),
        "user": str(router.get("user") or "admin").strip() or "admin",
        "password": _encrypt_router_password(password),
        "currency": str(router.get("currency") or "FCFA").strip() or "FCFA",
        "driver": _normalize_router_driver(router.get("driver")),
        "created_at": str(router.get("created_at") or datetime.now().isoformat()),
    }


def _migrate_legacy_local_users(conn):
    if conn.execute("SELECT COUNT(*) FROM local_users").fetchone()[0] > 0:
        return
    users = _load_json_file(LEGACY_USERS_PATH, [])
    if not isinstance(users, list):
        return
    seen = set()
    for user in users:
        normalized = _normalize_local_user(user)
        username = normalized["username"]
        if not username or not normalized["password"]:
            continue
        key = username.casefold()
        if key in seen:
            continue
        conn.execute("""
            INSERT OR IGNORE INTO local_users
            (id, username, email, password, display_name, role, created_at)
            VALUES (:id, :username, :email, :password, :display_name, :role, :created_at)
        """, normalized)
        seen.add(key)


def _migrate_legacy_routers(conn):
    if conn.execute("SELECT COUNT(*) FROM routers").fetchone()[0] > 0:
        return
    routers = _load_json_file(LEGACY_ROUTERS_PATH, [])
    if not isinstance(routers, list):
        return
    for router in routers:
        normalized = _normalize_router(router)
        if not normalized["name"] or not normalized["host"]:
            continue
        conn.execute("""
            INSERT OR IGNORE INTO routers
            (id, name, host, port, user, password, currency, driver, created_at)
            VALUES (:id, :name, :host, :port, :user, :password, :currency, :driver, :created_at)
        """, normalized)


def _migrate_routers_owner_id(conn):
    """Ajoute la colonne owner_id à la table routers si elle n'existe pas encore."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(routers)").fetchall()}
    if "owner_id" not in columns:
        conn.execute("ALTER TABLE routers ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routers_owner ON routers(owner_id)")


def _normalize_duplicate_references(conn):
    """Makes historical duplicate references unique before adding the constraint."""
    duplicates = conn.execute("""
        SELECT reference
        FROM subscriptions
        WHERE TRIM(reference) <> ''
        GROUP BY reference
        HAVING COUNT(*) > 1
    """).fetchall()
    for dup in duplicates:
        reference = dup["reference"]
        rows = conn.execute("""
            SELECT id, fraude_flags, fraude_detail
            FROM subscriptions
            WHERE reference=?
            ORDER BY
                CASE statut WHEN 'actif' THEN 0 WHEN 'en_attente' THEN 1 ELSE 2 END,
                demande_le ASC,
                id ASC
        """, (reference,)).fetchall()
        for row in rows[1:]:
            try:
                flags = json.loads(row["fraude_flags"] or "[]")
            except Exception:
                flags = []
            if "REFERENCE_DOUBLON_HISTORIQUE" not in flags:
                flags.append("REFERENCE_DOUBLON_HISTORIQUE")
            detail = row["fraude_detail"] or "OK"
            if "REFERENCE_DOUBLON_HISTORIQUE" not in detail:
                detail = f"{detail} | REFERENCE_DOUBLON_HISTORIQUE".strip(" |")
            conn.execute("""
                UPDATE subscriptions
                SET reference=?, fraude_flags=?, fraude_detail=?
                WHERE id=?
            """, (
                f"{reference}__DUP__{row['id'][:8]}",
                json.dumps(flags),
                detail,
                row["id"],
            ))


def _ensure_subscription_reference_constraint(conn):
    """Creates a unique index for non-empty references after migrating legacy data."""
    _normalize_duplicate_references(conn)
    conn.execute("DROP INDEX IF EXISTS idx_sub_ref")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_reference_nonempty
        ON subscriptions(reference)
        WHERE TRIM(reference) <> ''
    """)


def _ensure_hotspot_profile_metadata_columns(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hotspot_profile_metadata)").fetchall()}
    if not columns:
        return
    if "time_limit" not in columns:
        conn.execute(
            "ALTER TABLE hotspot_profile_metadata ADD COLUMN time_limit TEXT NOT NULL DEFAULT '0'"
        )


def _migrate_ventes_v1(conn):
    """Migration v1 : supprime les ventes enregistrées à la CRÉATION du ticket.
    Migration v2 : supprime les ventes avec prix=0 (sync sans ticket_pricing).
    Le thread background re-synchronise avec les bons prix depuis ticket_pricing."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.execute("DELETE FROM ventes")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    if version < 2:
        conn.execute("DELETE FROM ventes WHERE prix = 0")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    if version < 3:
        # Re-supprimer les ventes à prix=0 (ré-ajoutées avant le filtre ticket_pricing)
        conn.execute("DELETE FROM ventes WHERE prix = 0")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    if version < 4:
        # Supprimer les doublons (race condition thread background + frontend sync)
        # Garder uniquement la vente avec le min(rowid) pour chaque (router_id, user)
        conn.execute("""
            DELETE FROM ventes WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM ventes GROUP BY router_id, user
            )
        """)
        # Ajouter contrainte unique pour éviter tout doublon futur
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ventes_unique_user
            ON ventes(router_id, user)
        """)
        conn.execute("PRAGMA user_version = 4")
        conn.commit()


def init_db():
    """Crée les tables si elles n'existent pas."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS plans (
        id         TEXT PRIMARY KEY,
        nom        TEXT NOT NULL,
        duree      INTEGER NOT NULL DEFAULT 30,
        prix       REAL NOT NULL DEFAULT 0,
        devise     TEXT NOT NULL DEFAULT 'FCFA',
        actif      INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS subscriptions (
        id              TEXT PRIMARY KEY,
        user_id         TEXT NOT NULL,
        username        TEXT NOT NULL DEFAULT '',
        plan_id         TEXT NOT NULL,
        plan_nom        TEXT NOT NULL DEFAULT '',
        prix_plan       REAL NOT NULL DEFAULT 0,
        devise_plan     TEXT NOT NULL DEFAULT 'FCFA',
        prix_plan_base  REAL NOT NULL DEFAULT 0,
        montant_paye    REAL NOT NULL DEFAULT 0,
        devise_paye     TEXT NOT NULL DEFAULT 'FCFA',
        montant_base    REAL NOT NULL DEFAULT 0,
        devise_base     TEXT NOT NULL DEFAULT 'FCFA',
        duree_jours     INTEGER NOT NULL DEFAULT 30,
        methode         TEXT NOT NULL DEFAULT '',
        reference       TEXT NOT NULL DEFAULT '',
        fraude_flags    TEXT NOT NULL DEFAULT '[]',
        fraude_detail   TEXT NOT NULL DEFAULT 'OK',
        statut          TEXT NOT NULL DEFAULT 'en_attente',
        demande_le      TEXT NOT NULL DEFAULT (datetime('now')),
        active_le       TEXT,
        expire_le       TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_sub_user    ON subscriptions(user_id);
    CREATE INDEX IF NOT EXISTS idx_sub_statut  ON subscriptions(statut);

    CREATE TABLE IF NOT EXISTS local_users (
        id           TEXT PRIMARY KEY,
        username     TEXT NOT NULL,
        email        TEXT NOT NULL DEFAULT '',
        password     TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        role         TEXT NOT NULL DEFAULT 'utilisateur',
        created_at   TEXT DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uq_local_users_username
        ON local_users(username);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_local_users_email_nonempty
        ON local_users(email)
        WHERE TRIM(email) <> '';

    CREATE TABLE IF NOT EXISTS routers (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        host       TEXT NOT NULL,
        port       INTEGER NOT NULL DEFAULT 8728,
        user       TEXT NOT NULL DEFAULT 'admin',
        password   TEXT NOT NULL DEFAULT '',
        currency   TEXT NOT NULL DEFAULT 'FCFA',
        driver     TEXT NOT NULL DEFAULT 'mikrotik',
        owner_id   TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_routers_name    ON routers(name);

    CREATE TABLE IF NOT EXISTS pay_config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS hotspot_profile_metadata (
        router_id    TEXT NOT NULL,
        profile_name TEXT NOT NULL,
        price        TEXT NOT NULL DEFAULT '0',
        currency     TEXT NOT NULL DEFAULT 'FCFA',
        expire_mode  TEXT NOT NULL DEFAULT 'none',
        lock_user    TEXT NOT NULL DEFAULT 'no',
        time_limit   TEXT NOT NULL DEFAULT '0',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (router_id, profile_name)
    );
    CREATE INDEX IF NOT EXISTS idx_hotspot_profile_metadata_router
        ON hotspot_profile_metadata(router_id);

    CREATE TABLE IF NOT EXISTS ventes (
        id          TEXT PRIMARY KEY,
        router_id   TEXT NOT NULL DEFAULT '',
        date        TEXT NOT NULL,
        heure       TEXT NOT NULL,
        user        TEXT NOT NULL,
        profil      TEXT NOT NULL DEFAULT '',
        prix        REAL NOT NULL DEFAULT 0,
        devise      TEXT NOT NULL DEFAULT 'FCFA',
        reseau      TEXT NOT NULL DEFAULT '',
        data_limit  TEXT NOT NULL DEFAULT '0',
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_ventes_router_date ON ventes(router_id, date);

    CREATE TABLE IF NOT EXISTS ticket_pricing (
        router_id  TEXT NOT NULL,
        user       TEXT NOT NULL,
        prix       REAL NOT NULL DEFAULT 0,
        devise     TEXT NOT NULL DEFAULT 'FCFA',
        profil     TEXT NOT NULL DEFAULT '',
        reseau     TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (router_id, user)
    );
    """)
    _ensure_subscription_reference_constraint(conn)
    _ensure_hotspot_profile_metadata_columns(conn)
    _migrate_legacy_local_users(conn)
    _migrate_legacy_routers(conn)
    _migrate_ventes_v1(conn)
    _migrate_routers_owner_id(conn)
    # Ajoute la colonne disabled si absente (migration non destructive)
    try:
        conn.execute("ALTER TABLE local_users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    conn.commit()

    # Plans par défaut
    existing = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    if existing == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO plans(id,nom,duree,prix,devise,actif) VALUES(?,?,?,?,?,1)",
            [
                ("mensuel",  "Mensuel",    30,  5000,  "FCFA"),
                ("trimestr", "Trimestriel",90,  12000, "FCFA"),
                ("annuel",   "Annuel",     365, 40000, "FCFA"),
            ]
        )
        conn.commit()


# ── Plans ─────────────────────────────────────────────────────────────────────

def db_get_local_users():
    conn = get_conn()
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM local_users ORDER BY created_at ASC, username ASC"
        ).fetchall()
    ]


def db_get_local_user(identity: str):
    identity = str(identity or "").strip()
    if not identity:
        return None
    conn = get_conn()
    row = conn.execute("""
        SELECT *
        FROM local_users
        WHERE id=? OR username=? OR email=?
        ORDER BY
            CASE
                WHEN id=? THEN 0
                WHEN username=? THEN 1
                ELSE 2
            END
        LIMIT 1
    """, (identity, identity, identity, identity, identity)).fetchone()
    return dict(row) if row else None


def db_insert_local_user(user: dict):
    record = _normalize_local_user(user)
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO local_users
            (id, username, email, password, display_name, role, created_at)
            VALUES (:id, :username, :email, :password, :display_name, :role, :created_at)
        """, record)
        conn.commit()
        return db_get_local_user(record["id"])
    except sqlite3.IntegrityError as exc:
        if "local_users.username" in str(exc) or "local_users.email" in str(exc):
            raise DuplicateLocalUserError(record["username"] or record["email"]) from exc
        raise


def db_update_local_user_password(identity: str, password_hash: str) -> bool:
    user = db_get_local_user(identity)
    if not user:
        return False
    conn = get_conn()
    conn.execute(
        "UPDATE local_users SET password=? WHERE id=?",
        (str(password_hash or ""), user["id"])
    )
    conn.commit()
    return True


def db_get_routers(owner_id=None):
    """Retourne les routeurs. Si owner_id fourni, filtre sur ce propriétaire."""
    conn = get_conn()
    if owner_id:
        rows = conn.execute(
            "SELECT * FROM routers WHERE owner_id=? ORDER BY created_at ASC, name ASC",
            (str(owner_id),)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM routers ORDER BY created_at ASC, name ASC"
        ).fetchall()
    routers = []
    for row in rows:
        router = dict(row)
        router["password"] = _decrypt_router_password(router.get("password", ""))
        routers.append(router)
    return routers


def db_replace_routers(routers, owner_id=None):
    """Remplace tous les routeurs. Si owner_id fourni, ne touche qu'aux routeurs de ce propriétaire."""
    conn = get_conn()
    normalized = []
    for router in routers or []:
        record = _normalize_router(router)
        if not record["name"] or not record["host"]:
            continue
        record["owner_id"] = str(owner_id or "")
        normalized.append(record)
    with conn:
        if owner_id:
            conn.execute("DELETE FROM routers WHERE owner_id=?", (str(owner_id),))
        else:
            conn.execute("DELETE FROM routers")
        conn.executemany("""
            INSERT INTO routers
            (id, name, host, port, user, password, currency, driver, owner_id, created_at)
            VALUES (:id, :name, :host, :port, :user, :password, :currency, :driver, :owner_id, :created_at)
        """, normalized)


def db_add_router(router_data: dict, owner_id: str) -> dict:
    """Ajoute un routeur appartenant à owner_id et retourne l'enregistrement créé."""
    record = _normalize_router(router_data)
    record["owner_id"] = str(owner_id or "")
    conn = get_conn()
    conn.execute("""
        INSERT INTO routers
        (id, name, host, port, user, password, currency, driver, owner_id, created_at)
        VALUES (:id, :name, :host, :port, :user, :password, :currency, :driver, :owner_id, :created_at)
    """, record)
    conn.commit()
    record["password"] = _decrypt_router_password(record["password"])
    return record


def db_delete_router(router_id: str, owner_id: str | None = None) -> bool:
    """Supprime un routeur par id. Si owner_id fourni, vérifie l'appartenance."""
    conn = get_conn()
    if owner_id:
        cur = conn.execute(
            "DELETE FROM routers WHERE id=? AND owner_id=?",
            (str(router_id), str(owner_id))
        )
    else:
        cur = conn.execute("DELETE FROM routers WHERE id=?", (str(router_id),))
    conn.commit()
    return cur.rowcount > 0


def db_update_router(router_id: str, owner_id: str | None, fields: dict) -> bool:
    """Met à jour les champs d'un routeur. Retourne True si une ligne a été modifiée."""
    allowed = {"name", "host", "port", "user", "password", "currency", "driver"}
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "password":
            updates[k] = _encrypt_router_password(str(v or ""))
        elif k == "port":
            updates[k] = _normalize_port(v)
        elif k == "driver":
            updates[k] = _normalize_router_driver(v)
        else:
            updates[k] = str(v or "").strip()
    if not updates:
        return False
    conn = get_conn()
    set_clause = ", ".join(f"{k}=:{k}" for k in updates)
    params = {**updates, "rid": str(router_id)}
    if owner_id:
        params["oid"] = str(owner_id)
        cur = conn.execute(
            f"UPDATE routers SET {set_clause} WHERE id=:rid AND owner_id=:oid",
            params
        )
    else:
        cur = conn.execute(
            f"UPDATE routers SET {set_clause} WHERE id=:rid",
            params
        )
    conn.commit()
    return cur.rowcount > 0


def db_get_plans(actif_only=False):
    conn = get_conn()
    q = "SELECT * FROM plans"
    if actif_only:
        q += " WHERE actif=1"
    q += " ORDER BY prix ASC"
    return [dict(r) for r in conn.execute(q).fetchall()]


def db_get_hotspot_profile_metadata(router_id: str, profile_name: str | None = None):
    router_id = str(router_id or "").strip()
    if not router_id:
        return None if profile_name else []
    conn = get_conn()
    if profile_name is None:
        rows = conn.execute("""
            SELECT *
            FROM hotspot_profile_metadata
            WHERE router_id=?
            ORDER BY profile_name ASC
        """, (router_id,)).fetchall()
        return [dict(row) for row in rows]

    row = conn.execute("""
        SELECT *
        FROM hotspot_profile_metadata
        WHERE router_id=? AND profile_name=?
        LIMIT 1
    """, (router_id, str(profile_name or "").strip())).fetchone()
    return dict(row) if row else None


def db_upsert_hotspot_profile_metadata(router_id: str, profile_name: str, price="0", currency="FCFA", expire_mode="none", lock_user="no", time_limit="0"):
    router_id = str(router_id or "").strip()
    profile_name = str(profile_name or "").strip()
    if not router_id or not profile_name:
        return None
    record = {
        "router_id": router_id,
        "profile_name": profile_name,
        "price": str(price or "0").strip() or "0",
        "currency": str(currency or "FCFA").strip() or "FCFA",
        "expire_mode": str(expire_mode or "none").strip() or "none",
        "lock_user": str(lock_user or "no").strip() or "no",
        "time_limit": str(time_limit or "0").strip() or "0",
    }
    conn = get_conn()
    conn.execute("""
        INSERT INTO hotspot_profile_metadata(
            router_id, profile_name, price, currency, expire_mode, lock_user, time_limit, created_at, updated_at
        ) VALUES (
            :router_id, :profile_name, :price, :currency, :expire_mode, :lock_user, :time_limit, datetime('now'), datetime('now')
        )
        ON CONFLICT(router_id, profile_name) DO UPDATE SET
            price=excluded.price,
            currency=excluded.currency,
            expire_mode=excluded.expire_mode,
            lock_user=excluded.lock_user,
            time_limit=excluded.time_limit,
            updated_at=datetime('now')
    """, record)
    conn.commit()
    return db_get_hotspot_profile_metadata(router_id, profile_name)


def db_delete_hotspot_profile_metadata(router_id: str, profile_name: str | None = None):
    router_id = str(router_id or "").strip()
    if not router_id:
        return 0
    conn = get_conn()
    if profile_name is None:
        result = conn.execute(
            "DELETE FROM hotspot_profile_metadata WHERE router_id=?",
            (router_id,),
        )
    else:
        result = conn.execute(
            "DELETE FROM hotspot_profile_metadata WHERE router_id=? AND profile_name=?",
            (router_id, str(profile_name or "").strip()),
        )
    conn.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def db_save_plan(plan: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO plans(id,nom,duree,prix,devise,actif)
        VALUES(:id,:nom,:duree,:prix,:devise,:actif)
        ON CONFLICT(id) DO UPDATE SET
            nom=excluded.nom, duree=excluded.duree, prix=excluded.prix,
            devise=excluded.devise, actif=excluded.actif
    """, plan)
    conn.commit()


def db_delete_plan(plan_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    conn.commit()


# ── Abonnements ───────────────────────────────────────────────────────────────

def db_get_subscriptions(user_id=None):
    conn = get_conn()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? ORDER BY demande_le DESC",
            (user_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY demande_le DESC"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["fraude_flags"] = json.loads(d.get("fraude_flags", "[]"))
        except Exception:
            d["fraude_flags"] = []
        result.append(d)
    return result


def db_get_active_sub(user_id: str):
    """Retourne l'abonnement actif de l'utilisateur ou None."""
    now  = datetime.utcnow().isoformat()
    conn = get_conn()
    row  = conn.execute("""
        SELECT * FROM subscriptions
        WHERE user_id=? AND statut='actif' AND expire_le > ?
        ORDER BY expire_le DESC LIMIT 1
    """, (user_id, now)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["fraude_flags"] = json.loads(d.get("fraude_flags", "[]"))
    except Exception:
        d["fraude_flags"] = []
    return d


def db_reference_exists(reference: str) -> bool:
    reference = str(reference or "").strip()
    if not reference:
        return False
    conn = get_conn()
    row  = conn.execute(
        "SELECT id FROM subscriptions WHERE reference=?", (reference,)
    ).fetchone()
    return row is not None


def db_insert_subscription(sub: dict):
    conn = get_conn()
    flags = sub.get("fraude_flags", [])
    reference = str(sub.get("reference") or "").strip()
    try:
        conn.execute("""
            INSERT INTO subscriptions
            (id,user_id,username,plan_id,plan_nom,prix_plan,devise_plan,
             prix_plan_base,montant_paye,devise_paye,montant_base,devise_base,
             duree_jours,methode,reference,fraude_flags,fraude_detail,statut,
             demande_le,active_le,expire_le)
            VALUES
            (:id,:user_id,:username,:plan_id,:plan_nom,:prix_plan,:devise_plan,
             :prix_plan_base,:montant_paye,:devise_paye,:montant_base,:devise_base,
             :duree_jours,:methode,:reference,:fraude_flags,:fraude_detail,:statut,
             :demande_le,:active_le,:expire_le)
        """, {**sub, "reference": reference, "fraude_flags": json.dumps(flags)})
        conn.commit()
    except sqlite3.IntegrityError as exc:
        if reference and "subscriptions.reference" in str(exc):
            raise DuplicateReferenceError(reference) from exc
        raise


def db_update_sub_statut(sub_id: str, statut: str, active_le=None, expire_le=None):
    conn = get_conn()
    conn.execute("""
        UPDATE subscriptions SET statut=?, active_le=?, expire_le=?
        WHERE id=?
    """, (statut, active_le, expire_le, sub_id))
    # Marquer expirés automatiquement
    conn.execute("""
        UPDATE subscriptions SET statut='expire'
        WHERE statut='actif' AND expire_le < ?
    """, (datetime.utcnow().isoformat(),))
    conn.commit()


def db_delete_sub(sub_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
    conn.commit()


# ── Config paiement ───────────────────────────────────────────────────────────

_DEFAULT_PAY_CFG = {
    "devise_base": "FCFA",
    "taux_change": {"USD": 606.0, "EUR": 655.0, "FCFA": 1.0, "XOF": 1.0},
    "tolerance_pct": 0,
    "methodes": [
        {"id": "orange-money", "nom": "Orange Money", "numero": "", "instructions": "Envoyez le montant exact.", "actif": False},
        {"id": "moov-money",   "nom": "Moov Money",   "numero": "", "instructions": "Envoyez le montant exact.", "actif": False},
        {"id": "wave",         "nom": "Wave",          "numero": "", "instructions": "Transfert Wave.", "actif": False},
        {"id": "mtn-money",    "nom": "MTN Money",     "numero": "", "instructions": "Envoyez le montant exact.", "actif": False},
    ],
}


def db_get_pay_config() -> dict:
    conn = get_conn()
    row  = conn.execute("SELECT value FROM pay_config WHERE key='main'").fetchone()
    if row:
        try:
            return json.loads(row["value"])
        except Exception:
            pass
    return dict(_DEFAULT_PAY_CFG)


def db_save_pay_config(cfg: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO pay_config(key,value) VALUES('main',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(cfg),)
    )
    conn.commit()


# ── Ventes locales ─────────────────────────────────────────────────────────────

def db_insert_vente(vente: dict):
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO ventes(id,router_id,date,heure,user,profil,prix,devise,reseau,data_limit)
           VALUES(:id,:router_id,:date,:heure,:user,:profil,:prix,:devise,:reseau,:data_limit)""",
        vente
    )
    conn.commit()


def db_upsert_ticket_pricing(data: dict):
    """Enregistre le prix d'un ticket au moment de sa création (pour récupération lors du sync)."""
    db_batch_upsert_ticket_pricing([data])


def db_batch_upsert_ticket_pricing(data_list: list):
    """Enregistre les prix d'un lot de tickets en une seule transaction."""
    if not data_list:
        return
    conn = get_conn()
    rows = [
        {
            "router_id": d.get("router_id", ""),
            "user":      d.get("user", ""),
            "prix":      float(d.get("prix", 0) or 0),
            "devise":    d.get("devise", "FCFA") or "FCFA",
            "profil":    d.get("profil", "") or "",
            "reseau":    d.get("reseau", "") or "",
        }
        for d in data_list
    ]
    conn.executemany(
        """INSERT INTO ticket_pricing(router_id,user,prix,devise,profil,reseau)
           VALUES(:router_id,:user,:prix,:devise,:profil,:reseau)
           ON CONFLICT(router_id,user) DO UPDATE SET
               prix=excluded.prix, devise=excluded.devise,
               profil=excluded.profil, reseau=excluded.reseau""",
        rows
    )
    conn.commit()


def db_get_ventes(router_id: str, date_from: str = "", date_to: str = "",
                  jour: str = "", mois: str = "", annee: str = "", q: str = "", profil: str = ""):
    conn = get_conn()
    clauses = ["router_id = ?"]
    params  = [router_id]
    # Utilise LIKE prefix quand c'est possible pour que idx_ventes_router_date soit utilisé
    if annee and mois:
        # Range exact mois — utilise l'index sur date
        try:
            y, m = int(annee), int(mois)
            nm, ny = (m + 1, y) if m < 12 else (1, y + 1)
            clauses.append("date >= ? AND date < ?")
            params += [f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"]
        except ValueError:
            clauses.append("substr(date,1,7) = ?")
            params.append(f"{annee}-{mois}")
        if jour:
            clauses.append("substr(date,9,2) = ?"); params.append(jour.zfill(2))
    elif annee:
        clauses.append("date LIKE ?"); params.append(f"{annee}-%")
        if jour:
            clauses.append("substr(date,9,2) = ?"); params.append(jour.zfill(2))
    elif mois:
        clauses.append("substr(date,6,2) = ?"); params.append(mois.zfill(2))
        if jour:
            clauses.append("substr(date,9,2) = ?"); params.append(jour.zfill(2))
    elif jour:
        clauses.append("substr(date,9,2) = ?"); params.append(jour.zfill(2))
    if profil:
        clauses.append("profil = ?"); params.append(profil)
    if q:
        clauses.append("(user LIKE ? OR profil LIKE ? OR reseau LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    sql = "SELECT * FROM ventes WHERE " + " AND ".join(clauses) + " ORDER BY date DESC, heure DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def db_get_ventes_summary(router_id: str, today_str: str, month_str: str):
    conn = get_conn()
    today = conn.execute(
        "SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot, devise FROM ventes WHERE router_id=? AND date=? GROUP BY devise ORDER BY tot DESC LIMIT 1",
        (router_id, today_str)
    ).fetchone()
    month = conn.execute(
        "SELECT COUNT(*) cnt, COALESCE(SUM(prix),0) tot, devise FROM ventes WHERE router_id=? AND date LIKE ? GROUP BY devise ORDER BY tot DESC LIMIT 1",
        (router_id, f"{month_str}-%")
    ).fetchone()
    today = dict(today) if today else {}
    month = dict(month) if month else {}
    cur = today.get("devise") or month.get("devise") or "FCFA"
    return {
        "today_count": today.get("cnt", 0),
        "today_total": today.get("tot", 0.0),
        "month_count": month.get("cnt", 0),
        "month_total": month.get("tot", 0.0),
        "currency":    cur,
    }

def db_delete_vente(vente_id: str, router_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM ventes WHERE id=? AND router_id=?", (vente_id, router_id))
    conn.commit()
    return cur.rowcount > 0

def db_delete_ventes_mois(router_id: str, mois: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM ventes WHERE router_id=? AND date LIKE ?",
        (router_id, f"{mois}-%")
    )
    conn.commit()
    return cur.rowcount
