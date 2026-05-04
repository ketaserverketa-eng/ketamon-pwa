import json
import os
import unittest

import app as app_module
import database as db_module
from tests._support import cleanup_temp_workspace, make_temp_workspace


class LocalStorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = make_temp_workspace("local-storage")
        self.tmpdir_path = str(self.tmpdir)
        self.originals = {
            "app_data_dir": app_module.DATA_DIR,
            "app_users_f": app_module.USERS_F,
            "app_routers_f": app_module.ROUTERS_F,
            "db_data_dir": db_module.DATA_DIR,
            "db_path": db_module.DB_PATH,
            "db_legacy_users": db_module.LEGACY_USERS_PATH,
            "db_legacy_routers": db_module.LEGACY_ROUTERS_PATH,
        }
        app_module.DATA_DIR = self.tmpdir_path
        app_module.USERS_F = os.path.join(self.tmpdir_path, "users.json")
        app_module.ROUTERS_F = os.path.join(self.tmpdir_path, "routers.json")
        db_module.DATA_DIR = self.tmpdir_path
        db_module.DB_PATH = os.path.join(self.tmpdir_path, "ketamon.db")
        db_module.LEGACY_USERS_PATH = app_module.USERS_F
        db_module.LEGACY_ROUTERS_PATH = app_module.ROUTERS_F
        self._reset_conn()

    def tearDown(self):
        self._reset_conn()
        app_module.DATA_DIR = self.originals["app_data_dir"]
        app_module.USERS_F = self.originals["app_users_f"]
        app_module.ROUTERS_F = self.originals["app_routers_f"]
        db_module.DATA_DIR = self.originals["db_data_dir"]
        db_module.DB_PATH = self.originals["db_path"]
        db_module.LEGACY_USERS_PATH = self.originals["db_legacy_users"]
        db_module.LEGACY_ROUTERS_PATH = self.originals["db_legacy_routers"]
        cleanup_temp_workspace(self.tmpdir)

    def _reset_conn(self):
        conn = getattr(db_module._local, "conn", None)
        if conn is not None:
            conn.close()
            db_module._local.conn = None

    def test_init_db_migrates_legacy_users_and_routers(self):
        with open(app_module.USERS_F, "w", encoding="utf-8") as fh:
            json.dump([
                {
                    "id": "user-1",
                    "username": "legacy@example.com",
                    "email": "legacy@example.com",
                    "password": app_module.generate_password_hash("secret123"),
                    "displayName": "Legacy",
                    "role": "utilisateur",
                }
            ], fh)
        with open(app_module.ROUTERS_F, "w", encoding="utf-8") as fh:
            json.dump([
                {
                    "id": "router-1",
                    "name": "Router A",
                    "host": "10.0.0.1",
                    "port": 8728,
                    "user": "admin",
                    "password": "router-pass",
                    "currency": "FCFA",
                    "driver": "mikrotik",
                }
            ], fh)

        db_module.init_db()

        user = db_module.db_get_local_user("legacy@example.com")
        routers = db_module.db_get_routers()
        self.assertIsNotNone(user)
        self.assertEqual(user["display_name"], "Legacy")
        self.assertEqual(len(routers), 1)
        self.assertEqual(routers[0]["id"], "router-1")
        self.assertEqual(routers[0]["password"], "router-pass")

    def test_get_app_users_seeds_admin_into_sqlite(self):
        db_module.init_db()
        users = app_module.get_app_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "admin")
        self.assertTrue(db_module.db_get_local_user("admin"))


if __name__ == "__main__":
    unittest.main()
