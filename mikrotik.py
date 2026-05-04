"""
Couche MikroTik — librouteros 4.x avec wrapper routeros-api compatible.
Toutes les routes app.py utilisent api.get_resource() sans modification.
"""
import socket as _socket
import librouteros
from librouteros import connect as _lrt_connect
from librouteros.exceptions import ConnectionClosed, FatalError


# ── Wrapper de ressource (émule routeros-api Resource) ────────────────────────

class _Resource:
    def __init__(self, api_obj, path):
        self._api  = api_obj
        self._path = path.rstrip("/")

    def get(self, **filters):
        """Retourne une liste de dicts, avec filtres optionnels."""
        rows = []
        for raw in self._api.path(self._path):
            row = dict(raw)
            if ".id" in row and "id" not in row:
                row["id"] = row[".id"]
            rows.append(row)
        if filters:
            rows = [
                r for r in rows
                if all(str(r.get(k, "")) == str(v) for k, v in filters.items())
            ]
        return rows

    def add(self, **params):
        """Ajoute un enregistrement et retourne son id."""
        resource = self._api.path(self._path)
        return resource.add(**params)

    def set(self, **params):
        """Modifie un enregistrement. Accepte id= ou .id="""
        if "id" in params:
            params[".id"] = params.pop("id")
        resource = self._api.path(self._path)
        resource.update(**params)

    def remove(self, id):
        """Supprime un enregistrement par son .id."""
        resource = self._api.path(self._path)
        resource.remove(id)

    def call(self, command, extra_params=None):
        """Exécute une sous-commande (ex: 'print' avec count-only)."""
        cmd = f"{self._path}/{command}"
        kwargs = {k.replace("-", "_"): v for k, v in (extra_params or {}).items()}
        try:
            return list(self._api(cmd, **kwargs))
        except Exception:
            return []


# ── Wrapper de connexion (émule routeros-api Api) ─────────────────────────────

class _Api:
    def __init__(self, lrt_api):
        self._lrt = lrt_api

    def get_resource(self, path):
        return _Resource(self._lrt, path)

    def close(self):
        try:
            self._lrt.close()
        except Exception:
            pass


# ── Connexion publique ────────────────────────────────────────────────────────

def _mikrotik_connect(host, user, password, port=8728, timeout=10):
    """
    Retourne un objet _Api (compatible get_resource) ou lève une exception.
    Utilise librouteros avec login plain (RouterOS 6 et 7).
    """
    old = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(timeout)
    try:
        lrt = _lrt_connect(
            host,
            username=user,
            password=password,
            port=int(port),
            login_method=librouteros.login.plain,
        )
        return _Api(lrt)
    finally:
        _socket.setdefaulttimeout(old)


def _mikrotik_safe_connect(host, user, password, port=8728, timeout=10):
    """Retourne (api, None) ou (None, message_erreur)."""
    try:
        api = _mikrotik_connect(host, user, password, port, timeout)
        return api, None
    except FatalError as e:
        return None, f"Erreur RouterOS : {e}"
    except (ConnectionClosed, OSError, _socket.timeout, TimeoutError) as e:
        return None, f"Impossible de se connecter à {host}:{port} — {e}"
    except Exception as e:
        return None, str(e)


# ── Utilitaires ───────────────────────────────────────────────────────────────

def format_bytes(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ["B", "Ko", "Mo", "Go", "To"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} Po"


def format_uptime(uptime_str):
    return uptime_str or "0s"


# ---------------- Driver registry ----------------
from abc import ABC, abstractmethod


class BaseDriver(ABC):
    """Abstract driver interface."""
    @abstractmethod
    def connect(self, host, user, password, port=8728, timeout=10):
        pass

    @abstractmethod
    def safe_connect(self, host, user, password, port=8728, timeout=10):
        pass


class MikroTikDriver(BaseDriver):
    def connect(self, host, user, password, port=8728, timeout=10):
        return _mikrotik_connect(host, user, password, port, timeout)

    def safe_connect(self, host, user, password, port=8728, timeout=10):
        return _mikrotik_safe_connect(host, user, password, port, timeout)


# Registry and helpers
_DRIVERS = {}
DEFAULT_DRIVER = 'mikrotik'


def register_driver(name, driver_obj):
    _DRIVERS[name] = driver_obj


def get_driver(name):
    return _DRIVERS.get(name)


# Register built-in mikrotik driver
register_driver('mikrotik', MikroTikDriver())


def connect(host, user, password, port=8728, timeout=10, driver=DEFAULT_DRIVER):
    d = get_driver(driver)
    if not d:
        raise RuntimeError(f"Driver {driver} not found")
    return d.connect(host, user, password, port, timeout)


def safe_connect(host, user, password, port=8728, timeout=10, driver=DEFAULT_DRIVER):
    d = get_driver(driver)
    if not d:
        return None, f"Driver {driver} not found"
    return d.safe_connect(host, user, password, port, timeout)
