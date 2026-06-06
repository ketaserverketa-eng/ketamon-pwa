"""
Couche MikroTik — librouteros 4.x avec wrapper routeros-api compatible.
Toutes les routes app.py utilisent api.get_resource() sans modification.
"""
import socket as _socket
import re as _re
import threading as _threading
import librouteros
from librouteros import connect as _lrt_connect
from librouteros.exceptions import ConnectionClosed, FatalError
from librouteros.query import And as _QueryAnd, Key as _QueryKey


_CONNECT_LOCKS = {}
_CONNECT_LOCKS_GUARD = _threading.Lock()


def _connection_key(host, port):
    return f"{str(host or '').strip().lower()}:{int(port or 8728)}"


def _get_connection_lock(host, port):
    key = _connection_key(host, port)
    with _CONNECT_LOCKS_GUARD:
        lock = _CONNECT_LOCKS.get(key)
        if lock is None:
            lock = _threading.Lock()
            _CONNECT_LOCKS[key] = lock
        return lock


# ── Wrapper de ressource (émule routeros-api Resource) ────────────────────────

class _Resource:
    def __init__(self, api_obj, path):
        self._api  = api_obj
        self._path = path.rstrip("/")

    def get(self, **filters):
        """Retourne une liste de dicts, avec filtres optionnels."""
        rows = []
        resource = self._api.path(self._path)
        if filters:
            conditions = [
                _QueryKey(str(key)) == str(value)
                for key, value in filters.items()
            ]
            if len(conditions) == 1:
                raw_rows = resource.select().where(conditions[0])
            else:
                raw_rows = resource.select().where(_QueryAnd(conditions[0], conditions[1], *conditions[2:]))
        else:
            raw_rows = resource
        for raw in raw_rows:
            row = dict(raw)
            if ".id" in row and "id" not in row:
                row["id"] = row[".id"]
            rows.append(row)
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
    def __init__(self, lrt_api, release_lock=None):
        self._lrt = lrt_api
        self._release_lock = release_lock
        self._closed = False

    def get_resource(self, path):
        return _Resource(self._lrt, path)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._lrt.close()
        except Exception:
            pass
        try:
            protocol = getattr(self._lrt, "protocol", None)
            transport = getattr(protocol, "transport", None)
            sock = getattr(transport, "sock", None)
            if sock:
                try:
                    sock.shutdown(_socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
            close_transport = getattr(transport, "close", None)
            if callable(close_transport):
                close_transport()
        except Exception:
            pass
        finally:
            if self._release_lock:
                try:
                    self._release_lock()
                except Exception:
                    pass
                self._release_lock = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ── Connexion publique ────────────────────────────────────────────────────────

def _mikrotik_connect(host, user, password, port=8728, timeout=10):
    """
    Retourne un objet _Api (compatible get_resource) ou lève une exception.
    Utilise librouteros avec login plain (RouterOS 6 et 7).
    """
    lock = _get_connection_lock(host, port)
    if not lock.acquire(timeout=max(1, int(timeout or 10))):
        raise TimeoutError(f"Connexion API deja occupee vers {host}:{port}")
    old = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(timeout)
    success = False
    try:
        lrt = _lrt_connect(
            host,
            username=user,
            password=password,
            port=int(port),
            login_method=librouteros.login.plain,
        )
        try:
            sock = getattr(getattr(getattr(lrt, "protocol", None), "transport", None), "sock", None)
            if sock:
                sock.settimeout(timeout)
        except Exception:
            pass
        success = True
        return _Api(lrt, release_lock=lock.release)
    finally:
        _socket.setdefaulttimeout(old)
        if not success:
            try:
                lock.release()
            except Exception:
                pass


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


_HOST_SPLIT_RE = _re.compile(r"[|,;]")


def _iter_connection_hosts(host, fallback_host=None):
    seen = set()
    for raw in (host, fallback_host):
        for chunk in _HOST_SPLIT_RE.split(str(raw or "")):
            candidate = chunk.strip()
            if not candidate:
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            yield candidate


def safe_connect_router(router: dict, timeout=10):
    router = dict(router or {})
    driver = router.get("driver", DEFAULT_DRIVER) or DEFAULT_DRIVER
    user = router.get("user") or router.get("username") or "admin"
    return safe_connect(
        router.get("host", ""),
        user,
        router.get("password", ""),
        router.get("port", 8728),
        timeout=timeout,
        driver=driver,
        fallback_host=router.get("fallback_host", ""),
    )


def connect(host, user, password, port=8728, timeout=10, driver=DEFAULT_DRIVER, fallback_host=None):
    d = get_driver(driver)
    if not d:
        raise RuntimeError(f"Driver {driver} not found")
    last_error = None
    for candidate in _iter_connection_hosts(host, fallback_host):
        try:
            return d.connect(candidate, user, password, port, timeout)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("Host MikroTik manquant")


def safe_connect(host, user, password, port=8728, timeout=10, driver=DEFAULT_DRIVER, fallback_host=None):
    d = get_driver(driver)
    if not d:
        return None, f"Driver {driver} not found"
    errors = []
    targets = list(_iter_connection_hosts(host, fallback_host))
    if not targets:
        return None, "Host MikroTik manquant"
    for candidate in targets:
        api, err = d.safe_connect(candidate, user, password, port, timeout)
        if api and not err:
            return api, None
        errors.append(str(err or f"Connexion echouee vers {candidate}:{port}"))
    return None, " ; ".join(errors)
