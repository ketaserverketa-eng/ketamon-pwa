"""
KetaMon Agent — Surveillance + expiration absolue + revenus.

Architecture :
  - MOTEUR EXPIRATION  : tourne toutes les 60s sur TOUS les routers de TOUS
    les utilisateurs web. Lit l'heure locale du routeur MikroTik (pas le serveur)
    pour calculer l'expiration absolue. Totalement autonome.
  - MOTEUR MONITEUR    : tourne toutes les 90s. Detecte pannes, erreurs,
    syncs en retard, DB corrompue. Cree des incidents pour le concepteur.
  - MOTEUR REVENUS     : integre les ventes (revenus) dans la boucle agent.

Seul le concepteur voit et controle l'agent (interface /concepteur/incidents).
Les utilisateurs web ne voient rien mais l'agent agit directement sur leurs
routers en temps reel.
"""

import threading
import time
import re
import socket
import os
import sys
import requests
from datetime import datetime, timedelta

_DIR = os.path.dirname(__file__)
sys.path.insert(0, _DIR)

import database as db_mod
import mikrotik as mk

# ── Constantes (modifiables par le concepteur via set_config) ─────────────────
KETAMON_TICKET_COMMENT_MARKER = " ##KETAMON## exp="
KETAMON_TICKET_COMMENT_MARKERS = (
    KETAMON_TICKET_COMMENT_MARKER,
    KETAMON_TICKET_COMMENT_MARKER.strip(),
)
APP_PORT             = int(os.environ.get("KETAMON_PORT", 5001))
ROUTER_TIMEOUT       = 8    # secondes connexion MikroTik

def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    if minimum is not None:
        return max(minimum, value)
    return value

_config = {
    "expiry_interval":   _env_int("KETAMON_EXPIRY_INTERVAL", 60, minimum=30),
    "monitor_interval":  180,  # secondes entre chaque cycle moniteur
    "revenue_interval":  300,  # secondes entre chaque cycle revenus
    "sync_stale_min":    10,   # alerte si sync en retard > X minutes
    # Secours universel: si le script MikroTik rate, Python coupe les tickets
    # deja marques exp=... et nettoie les sessions/cookies orphelins.
    "python_expiry_fallback": True,
    "orphan_cleanup": True,
    "paused":            False,
}
_config_lock = threading.Lock()

def get_config() -> dict:
    with _config_lock:
        return dict(_config)

def set_config(key: str, value) -> bool:
    with _config_lock:
        if key in _config:
            _config[key] = value
            return True
    return False


# ── Etat temps-reel de l'agent (lu par /api/agent/status) ────────────────────
_status = {
    "running":         True,
    "started_at":      datetime.now().isoformat(timespec="seconds"),
    "last_expiry_run": None,
    "last_monitor_run":None,
    "last_revenue_run":None,
    "expiry_cycles":   0,
    "monitor_cycles":  0,
    "revenue_cycles":  0,
    "tickets_expired": 0,
    "epochs_written":  0,
    "ventes_synced":   0,
    "last_incident_at":None,
    "errors":          [],
}
_status_lock = threading.Lock()

def get_status() -> dict:
    with _status_lock:
        return dict(_status)

def _upd_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)

def _add_error(msg: str):
    with _status_lock:
        _status["errors"] = ([msg] + _status["errors"])[:20]


# ── Sync stats (injecte depuis app.py apres chaque sync) ─────────────────────
_sync_stats: dict = {}
_sync_stats_lock  = threading.Lock()

def set_sync_stats(stats: dict):
    with _sync_stats_lock:
        _sync_stats.update(stats)

def get_sync_stats() -> dict:
    with _sync_stats_lock:
        return dict(_sync_stats)


# ── Force-run (concepteur peut declencher un cycle immediat) ──────────────────
_force_run_event = threading.Event()

def force_run():
    _force_run_event.set()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _parse_duration(s: str) -> int:
    """Convertit une duree RouterOS (1d, 2h, 03:00:00, 7d00:00:00) en secondes."""
    if not s or s in ("0", "0s", "none"):
        return 0
    s = s.strip()
    total = 0
    try:
        m = re.match(r'^(\d+)d(?:(\d+):(\d+):(\d+))?$', s)
        if m:
            total += int(m.group(1)) * 86400
            if m.group(2):
                total += int(m.group(2))*3600 + int(m.group(3))*60 + int(m.group(4))
            return total
        m = re.match(r'^(\d+):(\d+):(\d+)$', s)
        if m:
            return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
        for unit, sec in (('w',604800),('d',86400),('h',3600),('m',60),('s',1)):
            m2 = re.search(r'(\d+)' + unit, s)
            if m2:
                total += int(m2.group(1)) * sec
        return total
    except Exception:
        return 0


def _router_item_id(row: dict) -> str:
    return str(row.get(".id") or row.get("id") or "")


def _split_comment_and_marker(comment: str):
    raw_comment = str(comment or "").strip()
    for marker in KETAMON_TICKET_COMMENT_MARKERS:
        pos = raw_comment.find(marker)
        if pos != -1:
            return raw_comment[:pos].rstrip(), raw_comment[pos + len(marker):].strip()
    return raw_comment, None


def _get_router_local_epoch(api) -> int:
    """
    Lit l'heure locale du routeur MikroTik via /system/clock et calcule
    l'epoch en utilisant LA MEME methode que le script on-login MikroTik
    (heure locale traitee comme UTC pour rester coherent avec les epochs
    stockes dans les commentaires des tickets).
    """
    try:
        clock_rows = api.get_resource("/system/clock").get()
        if not clock_rows:
            return int(datetime.now().timestamp())
        clk  = clock_rows[0]
        date_str = str(clk.get("date", "") or "")
        time_str = str(clk.get("time", "") or "")

        dt = None
        # RouterOS 7 : "2026-05-14"
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            dt = datetime.strptime(f"{date_str} {time_str[:8]}", "%Y-%m-%d %H:%M:%S")
        # RouterOS 6 : "may/14/2026"
        elif "/" in date_str:
            dt = datetime.strptime(f"{date_str} {time_str[:8]}", "%b/%d/%Y %H:%M:%S")

        if dt:
            # Meme calcul que le script MikroTik on-login : jours * 86400 + heure locale
            y = dt.year - 1970
            month_offsets = [0,31,59,90,120,151,181,212,243,273,304,334]
            moff = month_offsets[dt.month - 1]
            leaps = (y+1)//4 - (y+69)//100 + (y+369)//400
            dse   = y*365 + leaps + moff + (dt.day - 1)
            if dt.month > 2:
                if dt.year % 4 == 0:   dse += 1
                if dt.year % 100 == 0: dse -= 1
                if dt.year % 400 == 0: dse += 1
            return dse*86400 + dt.hour*3600 + dt.minute*60 + dt.second
    except Exception as e:
        _add_error(f"clock read: {e}")
    return int(datetime.now().timestamp())


def _datetime_to_router_local_epoch(dt: datetime) -> int:
    y = dt.year - 1970
    month_offsets = [0,31,59,90,120,151,181,212,243,273,304,334]
    moff = month_offsets[dt.month - 1]
    leaps = (y+1)//4 - (y+69)//100 + (y+369)//400
    dse   = y*365 + leaps + moff + (dt.day - 1)
    if dt.month > 2:
        if dt.year % 4 == 0:
            dse += 1
        if dt.year % 100 == 0:
            dse -= 1
        if dt.year % 400 == 0:
            dse += 1
    return dse*86400 + dt.hour*3600 + dt.minute*60 + dt.second


def _extract_expire_epoch(comment: str):
    _base_comment, expire_raw = _split_comment_and_marker(comment)
    if expire_raw is None:
        return None
    if not expire_raw:
        return None
    if expire_raw.isdigit() and len(expire_raw) >= 9:
        return int(expire_raw)
    try:
        dt = datetime.strptime(expire_raw[:19], "%b/%d/%Y %H:%M:%S")
        return _datetime_to_router_local_epoch(dt)
    except Exception:
        return None


def _compose_comment_with_epoch(comment: str, expire_epoch: int) -> str:
    base_comment, _expire_raw = _split_comment_and_marker(comment)
    if base_comment:
        return f"{base_comment}{KETAMON_TICKET_COMMENT_MARKER}{int(expire_epoch)}"
    return f"{KETAMON_TICKET_COMMENT_MARKER.strip()}{int(expire_epoch)}"


def _disconnect_hotspot_entities(api, usernames=None, addresses=None, mac_addresses=None):
    usernames = {str(name or "").strip() for name in (usernames or []) if str(name or "").strip()}
    addresses = {str(address or "").strip() for address in (addresses or []) if str(address or "").strip()}
    mac_addresses = {str(mac or "").strip().lower() for mac in (mac_addresses or []) if str(mac or "").strip()}
    removed = {"active_sessions": 0, "cookies": 0, "hosts": 0}

    try:
        active_resource = api.get_resource("/ip/hotspot/active")
        for active in active_resource.get():
            active_id = _router_item_id(active)
            username = str(active.get("user") or "").strip()
            address = str(active.get("address") or "").strip()
            mac_address = str(active.get("mac-address") or "").strip().lower()
            if (
                username in usernames
                or address in addresses
                or (mac_address and mac_address in mac_addresses)
            ):
                if address:
                    addresses.add(address)
                if mac_address:
                    mac_addresses.add(mac_address)
                if active_id:
                    active_resource.remove(id=active_id)
                    removed["active_sessions"] += 1
    except Exception:
        pass

    try:
        cookie_resource = api.get_resource("/ip/hotspot/cookie")
        for cookie in cookie_resource.get():
            cookie_id = _router_item_id(cookie)
            username = str(cookie.get("user") or "").strip()
            mac_address = str(cookie.get("mac-address") or "").strip().lower()
            if username in usernames or (mac_address and mac_address in mac_addresses):
                if mac_address:
                    mac_addresses.add(mac_address)
                if cookie_id:
                    cookie_resource.remove(id=cookie_id)
                    removed["cookies"] += 1
    except Exception:
        pass

    try:
        host_resource = api.get_resource("/ip/hotspot/host")
        for host in host_resource.get():
            host_id = _router_item_id(host)
            address = str(host.get("address") or "").strip()
            mac_address = str(host.get("mac-address") or "").strip().lower()
            if address in addresses or (mac_address and mac_address in mac_addresses):
                if host_id:
                    host_resource.remove(id=host_id)
                    removed["hosts"] += 1
    except Exception:
        pass

    return removed


def _is_service_reachable(http_timeout=4, socket_timeout=3) -> bool:
    try:
        response = requests.get(f"http://127.0.0.1:{APP_PORT}/health", timeout=http_timeout)
        if response.status_code == 200:
            return True
    except Exception:
        pass

    try:
        sock = socket.create_connection(("127.0.0.1", APP_PORT), timeout=socket_timeout)
        sock.close()
        return True
    except Exception:
        return False


def _new_incident(level, category, title, description="",
                  router_id="", router_name="", fix_action="", auto_fixed=False):
    if db_mod.db_agent_incident_exists(category, router_id=router_id, title=title):
        return None
    inc_id = db_mod.db_agent_create_incident(
        level=level, category=category, title=title,
        description=description, router_id=router_id,
        router_name=router_name, fix_action=fix_action,
        auto_fixed=auto_fixed
    )
    _upd_status(last_incident_at=datetime.now().isoformat(timespec="seconds"))
    return inc_id


# ══════════════════════════════════════════════════════════════════════════════
# MOTEUR 1 : Expiration absolue des tickets (heure locale du routeur)
# ══════════════════════════════════════════════════════════════════════════════

def _write_epochs_for_router(api, router_id: str, host: str = "?") -> int:
    """
    Secours Python uniquement pour les tickets ACTIFS sans epoch absolu.
    Ne touche jamais aux tickets non utilises.
    """
    try:
        active_rows = api.get_resource("/ip/hotspot/active").get()
        if not active_rows:
            return 0

        users_map = {
            str(user.get("name") or "").strip(): dict(user)
            for user in api.get_resource("/ip/hotspot/user").get()
            if str(user.get("name") or "").strip()
        }
        if not users_map:
            return 0

        router_now_epoch = _get_router_local_epoch(api)
        user_resource = api.get_resource("/ip/hotspot/user")
        written = 0

        for active in active_rows:
            username = str(active.get("user") or "").strip()
            if not username:
                continue
            user_row = users_map.get(username)
            if not user_row:
                continue

            current_comment = str(user_row.get("comment") or "").strip()
            if _extract_expire_epoch(current_comment) is not None:
                continue

            limit_seconds = _parse_duration(str(user_row.get("limit-uptime") or "0"))
            if limit_seconds <= 0:
                continue

            used_seconds = _parse_duration(str(user_row.get("uptime-used") or "0"))
            if used_seconds <= 0:
                used_seconds = _parse_duration(str(active.get("uptime") or "0"))
            remaining = max(limit_seconds - max(used_seconds, 0), 0)

            user_id = _router_item_id(user_row)
            if not user_id:
                continue

            new_comment = _compose_comment_with_epoch(current_comment, router_now_epoch + remaining)
            try:
                user_resource.set(id=user_id, comment=new_comment, **{"limit-uptime": "0"})
                written += 1
            except Exception as e:
                _add_error(f"epoch write {host}/{username}: {e}")
        return written
    except Exception as e:
        _add_error(f"epoch fallback {host}: {e}")
        return 0


def _expire_tickets_for_router(api, router_id: str = "", host: str = "?") -> int:
    """
    Secours Python pour supprimer les tickets deja marques exp=...
    Sans marqueur, aucun ticket n'est touche.
    """
    try:
        users = api.get_resource("/ip/hotspot/user").get()
        if not users:
            return 0

        now_epoch = _get_router_local_epoch(api)
        current_usernames = {
            str(user.get("name") or "").strip()
            for user in users
            if str(user.get("name") or "").strip()
        }
        expired_rows = []
        expired_usernames = set()
        expired_macs = set()

        for user in users:
            comment = str(user.get("comment") or "").strip()
            expire_epoch = _extract_expire_epoch(comment)
            if not expire_epoch or expire_epoch < 1000000000:
                continue
            if now_epoch < expire_epoch:
                continue

            user_id = _router_item_id(user)
            username = str(user.get("name") or "").strip()
            if user_id:
                expired_rows.append((user_id, username))
            if username:
                expired_usernames.add(username)
            mac = str(user.get("mac-address") or "").strip().lower()
            if mac and mac != "00:00:00:00:00:00":
                expired_macs.add(mac)

        if not expired_rows:
            if router_id:
                try:
                    db_mod.db_prune_missing_ticket_pricing(router_id, current_usernames)
                except Exception as e:
                    _add_error(f"db ticket prune {host}: {e}")
            return 0

        _disconnect_hotspot_entities(api, usernames=expired_usernames, mac_addresses=expired_macs)

        user_resource = api.get_resource("/ip/hotspot/user")
        removed = 0
        removed_usernames = set()
        for user_id, username in expired_rows:
            try:
                user_resource.remove(id=user_id)
                removed += 1
                if username:
                    removed_usernames.add(username)
                    current_usernames.discard(username)
            except Exception as e:
                _add_error(f"expire remove {host}/{user_id}: {e}")

        if router_id:
            try:
                db_deleted = db_mod.db_delete_ticket_pricing(router_id, removed_usernames)
                db_deleted += db_mod.db_prune_missing_ticket_pricing(router_id, current_usernames)
                if db_deleted:
                    print(f"[AGENT-EXPIRY] {host}: {db_deleted} ticket(s) nettoyes dans la DB")
            except Exception as e:
                _add_error(f"db ticket cleanup {host}: {e}")
        return removed
    except Exception as e:
        _add_error(f"expiry fallback {host}: {e}")
        return 0


def _cleanup_orphan_hotspot_access(api, host: str = "?") -> int:
    """
    Nettoie les acces hotspot qui n'ont plus de ticket correspondant:
    cookies/hosts/sessions restes apres suppression d'un user.
    """
    try:
        user_names = {
            str(user.get("name") or "").strip()
            for user in api.get_resource("/ip/hotspot/user").get()
            if str(user.get("name") or "").strip()
        }

        stale_usernames = set()
        stale_addresses = set()
        stale_macs = set()

        try:
            for active in api.get_resource("/ip/hotspot/active").get():
                username = str(active.get("user") or "").strip()
                if not username or username in user_names:
                    continue
                stale_usernames.add(username)
                address = str(active.get("address") or "").strip()
                if address:
                    stale_addresses.add(address)
                mac = str(active.get("mac-address") or "").strip().lower()
                if mac and mac != "00:00:00:00:00:00":
                    stale_macs.add(mac)
        except Exception:
            pass

        try:
            for cookie in api.get_resource("/ip/hotspot/cookie").get():
                username = str(cookie.get("user") or "").strip()
                if not username or username in user_names:
                    continue
                stale_usernames.add(username)
                mac = str(cookie.get("mac-address") or "").strip().lower()
                if mac and mac != "00:00:00:00:00:00":
                    stale_macs.add(mac)
        except Exception:
            pass

        if not stale_usernames and not stale_addresses and not stale_macs:
            return 0

        removed = _disconnect_hotspot_entities(
            api,
            usernames=stale_usernames,
            addresses=stale_addresses,
            mac_addresses=stale_macs,
        )
        return int(removed.get("active_sessions") or 0) + int(removed.get("cookies") or 0) + int(removed.get("hosts") or 0)
    except Exception as e:
        _add_error(f"orphan cleanup {host}: {e}")
        return 0


def _expire_and_write_for_router(api, router_id: str, host: str = "?", cfg: dict | None = None) -> dict:
    cfg = cfg or get_config()
    written = _write_epochs_for_router(api, router_id, host=host)
    expired = _expire_tickets_for_router(api, router_id=router_id, host=host) if cfg.get("python_expiry_fallback") else 0
    orphaned = _cleanup_orphan_hotspot_access(api, host=host) if cfg.get("orphan_cleanup") else 0
    return {"written": written, "expired": expired, "orphaned": orphaned}


def _run_expiry_engine():
    time.sleep(180)
    while True:
        cfg = get_config()
        total_written = 0
        total_expired = 0
        total_orphaned = 0

        if not cfg["paused"]:
            try:
                routers = db_mod.db_get_routers(owner_id=None)
                for router in routers:
                    host   = router.get("host", "?")
                    rid    = router.get("id") or host
                    rname  = router.get("name") or host
                    api    = None
                    try:
                        api, err = mk.safe_connect_router(router, timeout=ROUTER_TIMEOUT)
                        if err or not api:
                            raise RuntimeError(err or "connexion impossible")
                        result = _expire_and_write_for_router(api, rid, host=host, cfg=cfg)
                        total_written += int(result.get("written") or 0)
                        total_expired += int(result.get("expired") or 0)
                        total_orphaned += int(result.get("orphaned") or 0)
                        if result.get("written"):
                            print(f"[AGENT-EXPIRY] {rname}: {result['written']} epoch(s) repares")
                        if result.get("expired"):
                            print(f"[AGENT-EXPIRY] {rname}: {result['expired']} ticket(s) expires via fallback Python")
                        if result.get("orphaned"):
                            print(f"[AGENT-EXPIRY] {rname}: {result['orphaned']} acces orphelin(s) nettoyes")
                    except Exception as e:
                        _add_error(f"expiry loop {host}: {e}")
                    finally:
                        try:
                            if api:
                                api.close()
                        except Exception:
                            pass
            except Exception as e:
                _add_error(f"expiry routers: {e}")

        with _status_lock:
            _status["last_expiry_run"] = datetime.now().isoformat(timespec="seconds")
            _status["expiry_cycles"]  += 1
            _status["epochs_written"] += total_written
            _status["tickets_expired"] += total_expired

        triggered = _force_run_event.wait(cfg["expiry_interval"])
        if triggered:
            _force_run_event.clear()


# ══════════════════════════════════════════════════════════════════════════════
# MOTEUR 2 : Revenus (ventes) — sync pour tous les utilisateurs web
# ══════════════════════════════════════════════════════════════════════════════

def _sync_revenues_for_router(router: dict) -> int:
    """
    Desactive — la sync des revenus est geree par _bg_ventes_loop dans app.py
    (toutes les 30s) avec deduplication via ticket_key. Garder ici creerait
    des doublons car le format d'insertion differ (pas de ticket_key).
    """
    return 0


def _run_revenue_engine():
    time.sleep(15)
    while True:
        cfg = get_config()
        if not cfg["paused"]:
            total = 0
            try:
                routers = db_mod.db_get_routers(owner_id=None)
                for router in routers:
                    total += _sync_revenues_for_router(router)
            except Exception as e:
                _add_error(f"revenue loop: {e}")
            with _status_lock:
                _status["last_revenue_run"]  = datetime.now().isoformat(timespec="seconds")
                _status["revenue_cycles"]   += 1
                _status["ventes_synced"]    += total
        time.sleep(cfg["revenue_interval"])


# ══════════════════════════════════════════════════════════════════════════════
# MOTEUR 3 : Moniteur web (incidents pour le concepteur)
# ══════════════════════════════════════════════════════════════════════════════

def _check_service():
    if _is_service_reachable():
        db_mod.db_agent_resolve_by_category("service")
        return
    _new_incident("critical", "service",
                  "Serveur KetaMon ne repond pas",
                  f"Port {APP_PORT} inaccessible. Serveur peut-etre arrete.",
                  fix_action="restart_service")


def _check_routers():
    try:
        routers = db_mod.db_get_routers(owner_id=None)
    except Exception:
        return
    for router in routers:
        rid   = router.get("id") or router.get("host","")
        host  = router.get("host","?")
        rname = router.get("name") or host
        api = None
        try:
            api, err = mk.safe_connect_router(router, timeout=ROUTER_TIMEOUT)
            if err or not api:
                _new_incident("warning", "router",
                              f"Router '{rname}' inaccessible",
                              f"Connexion a {host} echouee : {err or 'connexion impossible'}",
                              router_id=rid, router_name=rname,
                              fix_action="check_router_connection")
            else:
                db_mod.db_agent_resolve_by_category("router", router_id=rid)
        except Exception as e:
            _new_incident("warning", "router",
                          f"Router '{rname}' inaccessible",
                          f"Exception : {e}",
                          router_id=rid, router_name=rname,
                          fix_action="check_router_connection")
        finally:
            try:
                if api:
                    api.close()
            except Exception:
                pass


def _check_sync():
    stats = get_sync_stats()
    now   = datetime.now()
    try:
        routers = db_mod.db_get_routers(owner_id=None)
    except Exception:
        return
    stale_min = get_config()["sync_stale_min"]
    for router in routers:
        rid   = router.get("id") or router.get("host","")
        rname = router.get("name") or router.get("host","?")
        stat  = stats.get(rid, {})
        last_ok  = stat.get("last_ok","")
        last_err = stat.get("last_error","")
        if last_ok:
            try:
                diff = (now - datetime.fromisoformat(last_ok)).total_seconds() / 60
                if diff > stale_min:
                    _new_incident("warning", "sync",
                                  f"Sync en retard pour '{rname}'",
                                  f"Derniere sync reussie il y a {int(diff)}min (seuil={stale_min}min).",
                                  router_id=rid, router_name=rname,
                                  fix_action="force_sync")
                else:
                    db_mod.db_agent_resolve_by_category("sync", router_id=rid)
            except Exception:
                pass
        elif last_err:
            _new_incident("warning", "sync",
                          f"Sync echouee pour '{rname}'",
                          f"Erreur : {last_err}",
                          router_id=rid, router_name=rname,
                          fix_action="force_sync")


def _check_db():
    try:
        import sqlite3
        from database import DB_PATH
        conn = sqlite3.connect(DB_PATH, timeout=5)
        res  = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if res and res[0] == "ok":
            db_mod.db_agent_resolve_by_category("database")
        else:
            _new_incident("critical", "database",
                          "Base de donnees corrompue",
                          f"integrity_check: {res[0] if res else 'inconnu'}",
                          fix_action="db_integrity_repair")
    except Exception as e:
        _new_incident("critical", "database",
                      "Base de donnees inaccessible",
                      f"Erreur: {e}", fix_action="db_integrity_repair")


def _check_flask_errors():
    log_path = os.path.join(_DIR, "data", "flask_errors.log")
    if not os.path.exists(log_path):
        return
    try:
        size = os.path.getsize(log_path)
        if size == 0:
            db_mod.db_agent_resolve_by_category("flask_error")
            return
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(max(0, size - 4096))
            recent = f.read()
        if any(k in recent for k in ("500", "Traceback", "Error", "Exception")):
            lines     = [l for l in recent.splitlines() if l.strip()]
            last_part = "\n".join(lines[-8:])
            _new_incident("warning", "flask_error",
                          "Erreur detectee dans les logs Flask",
                          last_part[:600], fix_action="view_logs")
        else:
            db_mod.db_agent_resolve_by_category("flask_error")
    except Exception:
        pass


def _execute_approved_fixes():
    try:
        for inc in db_mod.db_agent_get_incidents(resolved=False):
            if inc.get("fix_status") != "approved":
                continue
            inc_id = inc["id"]
            action = inc.get("fix_action","")
            rid    = inc.get("router_id","")

            if action == "restart_service":
                ok, result = _fix_restart_service()
            elif action == "force_sync":
                ok, result = _fix_force_sync(rid)
            elif action == "check_router_connection":
                ok, result = _fix_check_router_connection(rid)
            elif action == "db_integrity_repair":
                ok, result = _fix_db_repair()
            elif action == "view_logs":
                ok, result = False, "Consultation manuelle requise pour les logs Flask."
            else:
                ok, result = False, f"Action '{action}' non reconnue."

            if ok:
                db_mod.db_agent_resolve_incident(inc_id, fix_result=result)
                print(f"[AGENT-FIX] {inc_id[:8]} resolu : {result[:80]}")
            else:
                db_mod.db_agent_requeue_incident(inc_id, fix_result=result)
                _add_error(f"fix {action or 'unknown'} {inc_id[:8]}: {result}")
                print(f"[AGENT-FIX] {inc_id[:8]} echec : {result[:80]}")
    except Exception as e:
        _add_error(f"fixes: {e}")


def _fix_restart_service():
    try:
        import subprocess
        if _is_service_reachable(http_timeout=2, socket_timeout=2):
            return True, "Service deja operationnel."

        app_path = os.path.join(_DIR, "app.py")
        if not os.path.exists(app_path):
            return False, f"Fichier introuvable: {app_path}"

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            [sys.executable, app_path],
            cwd=_DIR,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 20
        while time.time() < deadline:
            if _is_service_reachable(http_timeout=2, socket_timeout=2):
                return True, "Redemarrage confirme par /health."
            if proc.poll() is not None:
                return False, f"Echec: le processus s'est termine avec le code {proc.returncode}."
            time.sleep(1)

        return False, "Echec: service non joignable 20s apres le lancement."
    except Exception as e:
        return False, f"Echec: {e}"


def _fix_check_router_connection(router_id: str):
    try:
        routers = db_mod.db_get_routers(owner_id=None)
        router  = next((r for r in routers
                        if (r.get("id") or r.get("host","")) == router_id), None)
        if not router:
            return False, "Router introuvable."

        host  = router.get("host","")
        api = None
        try:
            api, err = mk.safe_connect_router(router, timeout=ROUTER_TIMEOUT)
            if err or not api:
                return False, f"Connexion echouee: {err or 'connexion impossible'}"
            return True, f"Connexion a {host} retablie."
        finally:
            try:
                if api:
                    api.close()
            except Exception:
                pass
    except Exception as e:
        return False, f"Erreur: {e}"


def _fix_force_sync(router_id: str):
    try:
        routers = db_mod.db_get_routers(owner_id=None)
        router  = next((r for r in routers
                        if (r.get("id") or r.get("host","")) == router_id), None)
        if not router:
            return False, "Router introuvable."
        host  = router.get("host","")
        api = None
        try:
            api, err = mk.safe_connect_router(router, timeout=ROUTER_TIMEOUT)
            if err or not api:
                return False, f"Connexion echouee: {err or 'connexion impossible'}"
            result = _expire_and_write_for_router(api, router_id, host=host, cfg=get_config())
            return True, f"Sync forcee sur {host} - epochs repares={result.get('written', 0)}, expires={result.get('expired', 0)}."
        finally:
            try:
                if api:
                    api.close()
            except Exception:
                pass
    except Exception as e:
        return False, f"Erreur: {e}"


def _fix_db_repair():
    try:
        import sqlite3
        from database import DB_PATH
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            before = conn.execute("PRAGMA integrity_check").fetchone()
            if before and before[0] == "ok":
                return True, "Base deja integre."

            conn.execute("VACUUM")
            after = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()

        if after and after[0] == "ok":
            return True, "VACUUM execute et integrity_check valide."
        return False, f"integrity_check apres reparation: {after[0] if after else 'inconnu'}"
    except Exception as e:
        return False, f"Erreur: {e}"


def _run_monitor_engine():
    time.sleep(60)
    while True:
        cfg = get_config()
        if not cfg["paused"]:
            for fn in [_check_service, _check_routers, _check_sync,
                       _check_db, _check_flask_errors, _execute_approved_fixes]:
                try:
                    fn()
                except Exception as e:
                    _add_error(f"monitor {fn.__name__}: {e}")

            with _status_lock:
                _status["last_monitor_run"]  = datetime.now().isoformat(timespec="seconds")
                _status["monitor_cycles"]   += 1
        time.sleep(cfg["monitor_interval"])


# ── Point d'entree ─────────────────────────────────────────────────────────────
def start():
    for name, target in [
        ("agent-expiry",  _run_expiry_engine),
        ("agent-revenue", _run_revenue_engine),
        ("agent-monitor", _run_monitor_engine),
    ]:
        threading.Thread(target=target, daemon=True, name=name).start()
    print("[AGENT] 3 moteurs demarres : expiration | revenus | moniteur")
