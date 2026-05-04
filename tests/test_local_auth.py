import os
import unittest

import app as app_module
import database as db_module
from tests._support import cleanup_temp_workspace, make_temp_workspace

class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = make_temp_workspace("local-auth")
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
        app_module.USERS_F = os.path.join(self.tmpdir_path, 'users.json')
        app_module.ROUTERS_F = os.path.join(self.tmpdir_path, 'routers.json')
        db_module.DATA_DIR = self.tmpdir_path
        db_module.DB_PATH = os.path.join(self.tmpdir_path, 'ketamon.db')
        db_module.LEGACY_USERS_PATH = app_module.USERS_F
        db_module.LEGACY_ROUTERS_PATH = app_module.ROUTERS_F
        conn = getattr(db_module._local, "conn", None)
        if conn is not None:
            conn.close()
            db_module._local.conn = None
        db_module.init_db()

    def tearDown(self):
        conn = getattr(db_module._local, "conn", None)
        if conn is not None:
            conn.close()
            db_module._local.conn = None
        app_module.DATA_DIR = self.originals["app_data_dir"]
        app_module.USERS_F = self.originals["app_users_f"]
        app_module.ROUTERS_F = self.originals["app_routers_f"]
        db_module.DATA_DIR = self.originals["db_data_dir"]
        db_module.DB_PATH = self.originals["db_path"]
        db_module.LEGACY_USERS_PATH = self.originals["db_legacy_users"]
        db_module.LEGACY_ROUTERS_PATH = self.originals["db_legacy_routers"]
        cleanup_temp_workspace(self.tmpdir)

    def test_build_remote_user_session_supports_fastapi_tokens(self):
        session_data = app_module.build_remote_user_session(
            {
                "access_token": "access-123",
                "refresh_token": "refresh-123",
                "token_type": "bearer",
            },
            "foo@example.com",
        )
        self.assertIsNotNone(session_data)
        self.assertEqual(session_data["ks_token"], "access-123")
        self.assertEqual(session_data["ks_refresh_token"], "refresh-123")
        self.assertEqual(session_data["username"], "foo@example.com")
        self.assertEqual(session_data["auth_source"], "remote")

    def test_local_register_and_authenticate(self):
        email = 'foo@example.com'
        pwd = 'secret123'
        u = app_module.local_register(email, pwd, 'Foo')
        self.assertIsNotNone(u)
        # second register should return None (duplicate)
        self.assertIsNone(app_module.local_register(email, pwd))
        # authenticate
        found = app_module.authenticate_local_user(email, pwd)
        self.assertIsNotNone(found)
        self.assertEqual(found.get('username'), email)

if __name__ == '__main__':
    unittest.main()
