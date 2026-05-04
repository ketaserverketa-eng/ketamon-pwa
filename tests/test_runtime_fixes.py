import os
import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import session, url_for

import app as app_module
import database as db_module
from tests._support import cleanup_temp_workspace, make_temp_workspace


class DummyResource:
    def __init__(self, path, calls):
        self.path = path
        self.calls = calls

    def call(self, command, extra_params=None):
        self.calls.append((self.path, command, extra_params))
        return []


class DummyApi:
    def __init__(self):
        self.calls = []

    def get_resource(self, path):
        return DummyResource(path, self.calls)


class RecordingResource:
    def __init__(self, path, store, calls):
        self.path = path
        self.store = store
        self.calls = calls

    def get(self, **filters):
        rows = [dict(item) for item in self.store.get(self.path, [])]
        if filters:
            rows = [
                row for row in rows
                if all(str(row.get(key, "")) == str(value) for key, value in filters.items())
            ]
        return rows

    def set(self, **params):
        self.calls.append((self.path, "set", params))
        row_id = params.get("id") or params.get(".id")
        if not row_id:
            return
        for row in self.store.get(self.path, []):
            current_id = row.get("id") or row.get(".id")
            if str(current_id) == str(row_id):
                for key, value in params.items():
                    if key not in {"id", ".id"}:
                        row[key] = value
                break

    def remove(self, id):
        self.calls.append((self.path, "remove", id))
        self.store[self.path] = [
            row for row in self.store.get(self.path, [])
            if str(row.get("id") or row.get(".id") or "") != str(id)
        ]

    def add(self, **params):
        self.calls.append((self.path, "add", params))
        next_id = f"*GEN{len(self.store.get(self.path, [])) + 1}"
        row = {"id": next_id}
        row.update(params)
        self.store.setdefault(self.path, []).append(row)
        return next_id


class RecordingApi:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def get_resource(self, path):
        return RecordingResource(path, self.store, self.calls)


class RuntimeFixTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = make_temp_workspace("runtime-fixes")
        self.tmpdir_path = str(self.tmpdir)
        self.originals = {
            "DATA_DIR": app_module.DATA_DIR,
            "USERS_F": app_module.USERS_F,
            "ROUTERS_F": app_module.ROUTERS_F,
            "LOGOS_DIR": app_module.LOGOS_DIR,
            "DB_DATA_DIR": db_module.DATA_DIR,
            "DB_PATH": db_module.DB_PATH,
            "LEGACY_USERS_PATH": db_module.LEGACY_USERS_PATH,
            "LEGACY_ROUTERS_PATH": db_module.LEGACY_ROUTERS_PATH,
            "CONCEPTEUR_FILE": os.environ.get("KETASERVER_CONCEPTEUR_FILE"),
        }
        app_module.DATA_DIR = self.tmpdir_path
        app_module.USERS_F = os.path.join(self.tmpdir_path, "users.json")
        app_module.ROUTERS_F = os.path.join(self.tmpdir_path, "routers.json")
        app_module.LOGOS_DIR = os.path.join(self.tmpdir_path, "logos")
        db_module.DATA_DIR = self.tmpdir_path
        db_module.DB_PATH = os.path.join(self.tmpdir_path, "ketamon.db")
        db_module.LEGACY_USERS_PATH = app_module.USERS_F
        db_module.LEGACY_ROUTERS_PATH = app_module.ROUTERS_F
        self._reset_conn()
        db_module.init_db()
        os.makedirs(app_module.LOGOS_DIR, exist_ok=True)

    def tearDown(self):
        self._reset_conn()
        for name, value in self.originals.items():
            if name in {"DATA_DIR", "USERS_F", "ROUTERS_F", "LOGOS_DIR"}:
                setattr(app_module, name, value)
            elif name in {"DB_DATA_DIR", "DB_PATH", "LEGACY_USERS_PATH", "LEGACY_ROUTERS_PATH"}:
                mapped = {
                    "DB_DATA_DIR": "DATA_DIR",
                    "DB_PATH": "DB_PATH",
                    "LEGACY_USERS_PATH": "LEGACY_USERS_PATH",
                    "LEGACY_ROUTERS_PATH": "LEGACY_ROUTERS_PATH",
                }[name]
                setattr(db_module, mapped, value)
            elif name == "CONCEPTEUR_FILE":
                if value is None:
                    os.environ.pop("KETASERVER_CONCEPTEUR_FILE", None)
                else:
                    os.environ["KETASERVER_CONCEPTEUR_FILE"] = value
        cleanup_temp_workspace(self.tmpdir)

    def _reset_conn(self):
        conn = getattr(db_module._local, "conn", None)
        if conn is not None:
            conn.close()
            db_module._local.conn = None

    def test_login_endpoint_registered(self):
        with app_module.app.test_request_context("/"):
            self.assertEqual(url_for("login"), "/login")

    def test_dashboard_uses_real_resource_counts(self):
        class CountResource:
            def __init__(self, path):
                self.path = path

            def get(self, **kwargs):
                if self.path == "/system/resource":
                    return [{"total-memory": 10, "free-memory": 5, "total-hdd-space": 100, "free-hdd-space": 50, "cpu-load": "7", "version": "7.16", "uptime": "1d"}]
                if self.path == "/system/identity":
                    return [{"name": "router-demo"}]
                if self.path == "/system/clock":
                    return [{"time": "12:00:00", "date": "apr/23/2026"}]
                if self.path == "/system/routerboard":
                    return [{"model": "RB5009"}]
                if self.path == "/ip/hotspot/user":
                    return [{"name": "u1"}, {"name": "u2"}, {"name": "u3"}]
                if self.path == "/ip/hotspot/active":
                    return [{"user": "u1"}]
                if self.path == "/ip/hotspot":
                    return [{"name": "hotspot1"}]
                return []

            def call(self, command, extra_params=None):
                return []

        class CountApi:
            def get_resource(self, path):
                return CountResource(path)

        with app_module.app.test_request_context("/tableau-de-bord"):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            with patch.object(app_module, "get_api", return_value=(CountApi(), None)):
                with patch.object(app_module, "render_template", side_effect=lambda template, **ctx: ctx):
                    context = app_module.dashboard()
        self.assertEqual(context["data"]["hs_users"], 3)
        self.assertEqual(context["data"]["hs_active"], 1)
        self.assertEqual(context["data"]["hs_servers"], ["hotspot1"])

    def test_dashboard_template_uses_working_hotspot_aliases(self):
        with app_module.app.test_request_context("/tableau-de-bord"):
            session["logged_in"] = True
            session["username"] = "Demo"
            html = app_module.render_template(
                "dashboard.html",
                data={
                    "identity": "router",
                    "board": "rb",
                    "version": "7.x",
                    "uptime": "1d",
                    "cpu_load": 1,
                    "free_mem": "1 MiB",
                    "mem_pct": 10,
                    "total_mem": "2 MiB",
                    "free_hdd": "10 MiB",
                    "total_hdd": "20 MiB",
                    "time": "12:00:00",
                    "date": "apr/21/2026",
                    "hs_users": 1,
                    "hs_active": 1,
                    "hs_servers": ["hs1"],
                },
            )
        self.assertIn("/hotspot/utilisateurs", html)
        self.assertIn("/hotspot/utilisateurs/ajouter", html)

    def test_ks_post_handles_request_failures_without_unboundlocal(self):
        with patch.object(app_module.http_req, "post", side_effect=RuntimeError("boom")):
            app_module.KS_ENABLED = True
            resp, err = app_module.ks_post("/api/test", {})
        self.assertIsNone(resp)
        self.assertIn("boom", err)
        self.assertFalse(app_module.KS_ENABLED)

    def test_settings_account_matches_local_user_by_email_session(self):
        email = "foo@example.com"
        app_module.local_register(email, "secret123", "Foo")
        with app_module.app.test_request_context(
            "/parametres/compte", method="POST", data={"new_password": "newsecret456"}
        ):
            session["logged_in"] = True
            session["user_id"] = email
            session["username"] = "Foo"
            app_module.settings_account()
        self.assertIsNotNone(app_module.authenticate_local_user(email, "newsecret456"))

    def test_ticket_logo_defaults_to_builtin_asset(self):
        with app_module.app.test_request_context("/bons"):
            logo = app_module.get_active_ticket_logo()
        self.assertFalse(logo["is_custom"])
        self.assertEqual(logo["name"], app_module.DEFAULT_TICKET_LOGO_NAME)
        self.assertTrue(logo["url"].endswith("/static/img/default-ticket-logo.png"))

    def test_ticket_logo_prefers_latest_uploaded_logo(self):
        first_logo = Path(app_module.LOGOS_DIR) / "old-logo.png"
        second_logo = Path(app_module.LOGOS_DIR) / "new-logo.png"
        first_logo.write_bytes(b"old")
        second_logo.write_bytes(b"new")
        now = time.time()
        os.utime(first_logo, (now - 10, now - 10))
        os.utime(second_logo, (now, now))

        with app_module.app.test_request_context("/bons"):
            logo = app_module.get_active_ticket_logo()

        self.assertTrue(logo["is_custom"])
        self.assertEqual(logo["name"], "new-logo.png")
        self.assertTrue(logo["url"].endswith("/logos/new-logo.png"))

    def test_system_reboot_uses_system_resource_call(self):
        dummy_api = DummyApi()
        with app_module.app.test_request_context(
            "/systeme/redemarrer", method="POST", data={"csrf_token": "token"}
        ):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(dummy_api, None)):
                response = app_module.system_reboot()
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(dummy_api.calls, [("/system", "reboot", None)])

    def test_hotspot_add_profile_installs_router_runtime_and_stores_metadata(self):
        api = RecordingApi({
            "/ip/pool": [{"name": "pool-demo"}],
            "/ip/hotspot/user/profile": [],
            "/system/script": [],
            "/system/scheduler": [],
        })
        with app_module.app.test_request_context(
            "/hotspot/profils/ajouter",
            method="POST",
            data={
                "name": "Pack 1H",
                "shared_users": "1",
                "rate_limit": "2M/1M",
                "time_limit": "1h",
                "expire_behavior": "remove",
                "addr_pool": "pool-demo",
                "lock_policy": "yes",
                "price": "500",
                "currency": "FCFA",
                "csrf_token": "token",
            },
        ):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                response = app_module.hotspot_add_profile()

        self.assertEqual(response.status_code, 302)
        add_calls = [call for call in api.calls if call[0] == "/ip/hotspot/user/profile" and call[1] == "add"]
        self.assertEqual(len(add_calls), 1)
        params = add_calls[0][2]
        self.assertEqual(params["name"], "Pack-1H")
        self.assertEqual(params["shared-users"], "1")
        self.assertEqual(params["rate-limit"], "2M/1M")
        self.assertEqual(params["address-pool"], "pool-demo")
        self.assertNotIn("comment", params)
        self.assertNotIn("on-login", params)

        profile_sets = [call for call in api.calls if call[0] == "/ip/hotspot/user/profile" and call[1] == "set"]
        self.assertEqual(len(profile_sets), 1)
        self.assertEqual(profile_sets[0][2]["on-login"], app_module.KETAMON_TICKET_LOGIN_SCRIPT)

        script_adds = [call for call in api.calls if call[0] == "/system/script" and call[1] == "add"]
        self.assertEqual({call[2]["name"] for call in script_adds}, {
            app_module.KETAMON_TICKET_LOGIN_SCRIPT,
            app_module.KETAMON_TICKET_EXPIRY_SCRIPT,
        })
        scheduler_adds = [call for call in api.calls if call[0] == "/system/scheduler" and call[1] == "add"]
        self.assertEqual(len(scheduler_adds), 1)
        self.assertEqual(scheduler_adds[0][2]["name"], app_module.KETAMON_TICKET_EXPIRY_SCHEDULER)

        meta = db_module.db_get_hotspot_profile_metadata("router-1", "Pack-1H")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["expire_mode"], "remove")
        self.assertEqual(meta["lock_user"], "yes")
        self.assertEqual(meta["time_limit"], "1h")
        self.assertEqual(meta["price"], "500")
        self.assertEqual(meta["currency"], "FCFA")

    def test_hotspot_profiles_merge_local_ketamon_metadata(self):
        db_module.db_upsert_hotspot_profile_metadata(
            "router-1",
            "Pack-1H",
            price="500",
            currency="FCFA",
            expire_mode="remove",
            lock_user="yes",
            time_limit="1h",
        )
        api = RecordingApi(
            {
                "/ip/hotspot/user/profile": [
                    {"id": "*P1", "name": "Pack-1H", "shared-users": "1", "rate-limit": "2M/1M"}
                ]
            }
        )
        with app_module.app.test_request_context("/hotspot/profils"):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                with patch.object(app_module, "render_template", side_effect=lambda template, **ctx: ctx):
                    context = app_module.hotspot_profiles()

        profile = context["profiles"][0]
        self.assertEqual(profile["expire-mode"], "remove")
        self.assertEqual(profile["add-mac-cookie"], "yes")
        self.assertEqual(profile["price"], "500")
        self.assertEqual(profile["currency"], "FCFA")
        self.assertEqual(profile["time-limit"], "1h")
        self.assertTrue(profile["_ketamon_meta"])

    def test_add_user_inherits_profile_time_and_installs_runtime(self):
        db_module.db_upsert_hotspot_profile_metadata(
            "router-1",
            "Pack-1H",
            price="500",
            currency="FCFA",
            expire_mode="remove",
            lock_user="yes",
            time_limit="1h",
        )
        api = RecordingApi(
            {
                "/ip/hotspot/user/profile": [{"id": "*P1", "name": "Pack-1H"}],
                "/ip/hotspot": [{"name": "hotspot1"}],
                "/ip/hotspot/user": [],
                "/system/script": [],
                "/system/scheduler": [],
            }
        )
        with app_module.app.test_request_context(
            "/reseau/clients/ajouter",
            method="POST",
            data={
                "name": "ticket-001",
                "password": "",
                "profile": "Pack-1H",
                "server": "hotspot1",
                "time_limit": "",
                "data_limit": "0",
                "comment": "demo",
                "csrf_token": "token",
            },
        ):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                response = app_module.reseau_ajouter_client()

        self.assertEqual(response.status_code, 302)
        user_adds = [call for call in api.calls if call[0] == "/ip/hotspot/user" and call[1] == "add"]
        self.assertEqual(len(user_adds), 1)
        params = user_adds[0][2]
        self.assertEqual(params["limit-uptime"], "1h")
        self.assertEqual(params["comment"], "vc-demo")
        self.assertEqual(params["server"], "hotspot1")

        profile_sets = [call for call in api.calls if call[0] == "/ip/hotspot/user/profile" and call[1] == "set"]
        self.assertEqual(len(profile_sets), 1)
        self.assertEqual(profile_sets[0][2]["on-login"], app_module.KETAMON_TICKET_LOGIN_SCRIPT)

        script_adds = [call for call in api.calls if call[0] == "/system/script" and call[1] == "add"]
        self.assertEqual({call[2]["name"] for call in script_adds}, {
            app_module.KETAMON_TICKET_LOGIN_SCRIPT,
            app_module.KETAMON_TICKET_EXPIRY_SCRIPT,
        })
        scheduler_adds = [call for call in api.calls if call[0] == "/system/scheduler" and call[1] == "add"]
        self.assertEqual(len(scheduler_adds), 1)
        self.assertEqual(scheduler_adds[0][2]["name"], app_module.KETAMON_TICKET_EXPIRY_SCHEDULER)

    def test_hotspot_reset_user_clears_runtime_marker_and_mac_lock(self):
        api = RecordingApi(
            {
                "/ip/hotspot/user": [
                    {
                        "id": "*A",
                        "name": "ticket-001",
                        "comment": "vc-demo ##KETAMON## exp=10w1d00:00:00",
                        "mac-address": "AA:BB:CC:DD:EE:01",
                        "disabled": "yes",
                    }
                ],
                "/ip/hotspot/active": [
                    {"id": "*1", "user": "ticket-001", "address": "10.0.0.8", "mac-address": "AA:BB:CC:DD:EE:01"}
                ],
                "/ip/hotspot/cookie": [{"id": "*C", "user": "ticket-001"}],
                "/ip/hotspot/host": [{"id": "*H", "address": "10.0.0.8", "mac-address": "AA:BB:CC:DD:EE:01"}],
            }
        )
        with app_module.app.test_request_context(
            "/hotspot/utilisateurs/reinitialiser",
            method="POST",
            json={"id": "*A"},
            headers={"X-CSRF-Token": "token"},
        ):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                response = app_module.hotspot_reset_user()

        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["disconnected"]["active_sessions"], 1)
        self.assertEqual(payload["disconnected"]["cookies"], 1)
        self.assertEqual(payload["disconnected"]["hosts"], 1)

        set_calls = [call for call in api.calls if call[0] == "/ip/hotspot/user" and call[1] == "set"]
        self.assertEqual(len(set_calls), 1)
        set_params = set_calls[0][2]
        self.assertEqual(set_params["comment"], "vc-demo")
        self.assertEqual(set_params["mac-address"], "00:00:00:00:00:00")
        self.assertEqual(set_params["disabled"], "no")

    def test_reseau_clients_marks_live_router_state(self):
        api = RecordingApi(
            {
                "/ip/hotspot/user/profile": [{"name": "1H"}],
                "/ip/hotspot": [{"name": "hotspot1"}],
                "/ip/hotspot/user": [
                    {"id": "*A", "name": "ticket-001", "disabled": "no"},
                    {"id": "*B", "name": "ticket-002", "disabled": "yes"},
                    {"id": "*C", "name": "ticket-003", "disabled": "no"},
                ],
                "/ip/hotspot/active": [
                    {"id": "*1", "user": "ticket-001", "address": "10.0.0.8", "mac-address": "AA:BB:CC:DD:EE:01"}
                ],
            }
        )

        with app_module.app.test_request_context("/hotspot/utilisateurs"):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                with patch.object(app_module, "render_template", side_effect=lambda template, **ctx: ctx):
                    context = app_module.reseau_clients()

        users = {row["name"]: row for row in context["users"]}
        self.assertEqual(users["ticket-001"]["_live_state"], "connected")
        self.assertEqual(users["ticket-001"]["_active_sessions"], 1)
        self.assertEqual(users["ticket-002"]["_live_state"], "disabled")
        self.assertEqual(users["ticket-003"]["_live_state"], "offline")

    def test_hotspot_toggle_user_disconnects_live_session_and_cookie(self):
        api = RecordingApi(
            {
                "/ip/hotspot/user": [{"id": "*A", "name": "ticket-001", "disabled": "no"}],
                "/ip/hotspot/active": [{"id": "*1", "user": "ticket-001", "address": "10.0.0.8", "mac-address": "AA:BB:CC:DD:EE:01"}],
                "/ip/hotspot/cookie": [{"id": "*C", "user": "ticket-001"}],
                "/ip/hotspot/host": [{"id": "*H", "address": "10.0.0.8", "mac-address": "AA:BB:CC:DD:EE:01"}],
            }
        )
        with app_module.app.test_request_context(
            "/hotspot/utilisateurs/basculer",
            method="POST",
            json={"id": "*A", "disabled": "yes"},
            headers={"X-CSRF-Token": "token"},
        ):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                response = app_module.hotspot_toggle_user()

        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["disconnected"]["active_sessions"], 1)
        self.assertEqual(payload["disconnected"]["cookies"], 1)
        self.assertEqual(payload["disconnected"]["hosts"], 1)
        self.assertIn(("/ip/hotspot/user", "set", {"id": "*A", "disabled": "yes"}), api.calls)
        self.assertIn(("/ip/hotspot/active", "remove", "*1"), api.calls)
        self.assertIn(("/ip/hotspot/cookie", "remove", "*C"), api.calls)
        self.assertIn(("/ip/hotspot/host", "remove", "*H"), api.calls)

    def test_hotspot_remove_active_requires_real_session_id(self):
        api = RecordingApi({"/ip/hotspot/active": []})
        with app_module.app.test_request_context(
            "/hotspot/actifs/supprimer",
            method="POST",
            json={"id": ""},
            headers={"X-CSRF-Token": "token"},
        ):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                response, status_code = app_module.hotspot_remove_active()

        self.assertEqual(status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("session", response.get_json()["msg"])

    def test_hotspot_remove_active_clears_session_cookie_and_host(self):
        api = RecordingApi(
            {
                "/ip/hotspot/active": [{"id": "*1", "user": "ticket-009", "address": "10.0.0.9", "mac-address": "AA:BB:CC:DD:EE:09"}],
                "/ip/hotspot/cookie": [{"id": "*C", "user": "ticket-009", "mac-address": "AA:BB:CC:DD:EE:09"}],
                "/ip/hotspot/host": [{"id": "*H", "address": "10.0.0.9", "mac-address": "AA:BB:CC:DD:EE:09"}],
            }
        )
        with app_module.app.test_request_context(
            "/hotspot/actifs/supprimer",
            method="POST",
            json={"id": "*1"},
            headers={"X-CSRF-Token": "token"},
        ):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            session["csrf_token"] = "token"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                response = app_module.hotspot_remove_active()

        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["disconnected"]["active_sessions"], 1)
        self.assertEqual(payload["disconnected"]["cookies"], 1)
        self.assertEqual(payload["disconnected"]["hosts"], 1)

    def test_hotspot_active_adds_ticket_time_left_to_context(self):
        api = RecordingApi(
            {
                "/ip/hotspot": [{"name": "hotspot1"}],
                "/ip/hotspot/active": [{"id": "*1", "user": "ticket-050", "server": "hotspot1", "uptime": "10m", "address": "10.0.0.50", "mac-address": "AA:BB:CC:DD:EE:50"}],
                "/ip/hotspot/user": [{"id": "*U", "name": "ticket-050", "limit-uptime": "1h", "uptime-used": "10m"}],
            }
        )
        with app_module.app.test_request_context("/hotspot/actifs"):
            session["logged_in"] = True
            session["router_id"] = "router-1"
            with patch.object(app_module, "get_api", return_value=(api, None)):
                with patch.object(app_module, "render_template", side_effect=lambda template, **ctx: ctx):
                    context = app_module.hotspot_active()

        self.assertEqual(context["actifs"][0]["id"], "*1")
        self.assertEqual(context["actifs"][0]["temps-restant"], "50m")

    def test_concepteur_fallback_requires_hashed_password(self):
        creds_file = os.path.join(self.tmpdir_path, "concepteur.json")
        with open(creds_file, "w", encoding="utf-8") as fh:
            fh.write('{"username":"admin","password":"plain-secret","displayName":"Boss"}')
        os.environ["KETASERVER_CONCEPTEUR_FILE"] = creds_file
        with app_module.app.test_request_context(
            "/login",
            method="POST",
            data={"mode": "username", "submode": "login", "username": "admin", "password": "plain-secret"},
        ):
            with patch.object(app_module, "ks_post", return_value=(None, "KetaServer indisponible")):
                app_module.login()
            self.assertFalse(session.get("logged_in", False))

    def test_concepteur_fallback_accepts_werkzeug_hash(self):
        creds_file = os.path.join(self.tmpdir_path, "concepteur.json")
        hashed = app_module.generate_password_hash("secret123")
        with open(creds_file, "w", encoding="utf-8") as fh:
            fh.write(
                '{"username":"admin","password":"%s","displayName":"Boss"}' % hashed.replace("\\", "\\\\")
            )
        os.environ["KETASERVER_CONCEPTEUR_FILE"] = creds_file
        with app_module.app.test_request_context(
            "/login",
            method="POST",
            data={"mode": "username", "submode": "login", "username": "admin", "password": "secret123"},
        ):
            with patch.object(app_module, "ks_post", return_value=(None, "KetaServer indisponible")):
                response = app_module.login()
            self.assertTrue(session.get("logged_in", False))
            self.assertEqual(session.get("role"), "concepteur")
            self.assertEqual(response.status_code, 302)

    def test_app_has_single_main_block(self):
        text = Path(app_module.__file__).read_text(encoding="utf-8", errors="ignore")
        self.assertEqual(text.count('if __name__ == "__main__":'), 1)

    def test_all_template_endpoints_resolve(self):
        templates_dir = Path(app_module.__file__).with_name("templates")
        endpoints = set(app_module.app.view_functions)
        missing = set()
        for template in templates_dir.rglob("*.html"):
            text = template.read_text(encoding="utf-8", errors="ignore")
            for endpoint in re.findall(r"url_for\(['\"]([^'\"]+)['\"]", text):
                if endpoint not in endpoints and endpoint != "static":
                    missing.add(endpoint)
        self.assertEqual(set(), missing)

    def test_legacy_concepteur_mutation_routes_are_blocked(self):
        client = app_module.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["logged_in"] = True
            flask_session["role"] = "concepteur"
            flask_session["csrf_token"] = "legacy-token"

        blocked_page = client.get("/concepteur/tickets", follow_redirects=False)
        self.assertEqual(blocked_page.status_code, 302)
        self.assertTrue(blocked_page.headers["Location"].endswith("/concepteur"))

        blocked_action = client.post(
            "/concepteur/tickets/demo/supprimer",
            data={"csrf_token": "legacy-token"},
        )
        self.assertEqual(blocked_action.status_code, 403)
        payload = blocked_action.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("mode legacy", payload["message"])


if __name__ == "__main__":
    unittest.main()
