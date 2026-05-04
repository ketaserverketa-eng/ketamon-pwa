import os
import sqlite3
import unittest

import database as db_module
from tests._support import cleanup_temp_workspace, make_temp_workspace


class PaymentReferenceUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = make_temp_workspace("db-reference")
        self.tmpdir_path = str(self.tmpdir)
        self.originals = {
            "DATA_DIR": db_module.DATA_DIR,
            "DB_PATH": db_module.DB_PATH,
            "LEGACY_USERS_PATH": db_module.LEGACY_USERS_PATH,
            "LEGACY_ROUTERS_PATH": db_module.LEGACY_ROUTERS_PATH,
        }
        self._reset_conn()
        db_module.DATA_DIR = self.tmpdir_path
        db_module.DB_PATH = os.path.join(self.tmpdir_path, "ketamon.db")
        db_module.LEGACY_USERS_PATH = os.path.join(self.tmpdir_path, "users.json")
        db_module.LEGACY_ROUTERS_PATH = os.path.join(self.tmpdir_path, "routers.json")

    def tearDown(self):
        self._reset_conn()
        for name, value in self.originals.items():
            setattr(db_module, name, value)
        cleanup_temp_workspace(self.tmpdir)

    def _reset_conn(self):
        conn = getattr(db_module._local, "conn", None)
        if conn is not None:
            conn.close()
            db_module._local.conn = None

    def _subscription(self, sub_id, reference):
        return {
            "id": sub_id,
            "user_id": "user-1",
            "username": "demo",
            "plan_id": "mensuel",
            "plan_nom": "Mensuel",
            "prix_plan": 5000,
            "devise_plan": "FCFA",
            "prix_plan_base": 5000,
            "montant_paye": 5000,
            "devise_paye": "FCFA",
            "montant_base": 5000,
            "devise_base": "FCFA",
            "duree_jours": 30,
            "methode": "orange-money",
            "reference": reference,
            "fraude_flags": [],
            "fraude_detail": "OK",
            "statut": "en_attente",
            "demande_le": "2026-04-21T12:00:00",
            "active_le": None,
            "expire_le": None,
        }

    def test_duplicate_non_empty_reference_is_rejected(self):
        db_module.init_db()
        db_module.db_insert_subscription(self._subscription("sub-1", "PAY-001"))
        with self.assertRaises(db_module.DuplicateReferenceError):
            db_module.db_insert_subscription(self._subscription("sub-2", "PAY-001"))

    def test_blank_reference_can_repeat(self):
        db_module.init_db()
        db_module.db_insert_subscription(self._subscription("sub-1", ""))
        db_module.db_insert_subscription(self._subscription("sub-2", ""))
        conn = db_module.get_conn()
        count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        self.assertEqual(count, 2)

    def test_init_db_normalizes_legacy_duplicates_before_unique_index(self):
        conn = sqlite3.connect(db_module.DB_PATH)
        conn.executescript("""
        CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            plan_id TEXT NOT NULL,
            plan_nom TEXT NOT NULL DEFAULT '',
            prix_plan REAL NOT NULL DEFAULT 0,
            devise_plan TEXT NOT NULL DEFAULT 'FCFA',
            prix_plan_base REAL NOT NULL DEFAULT 0,
            montant_paye REAL NOT NULL DEFAULT 0,
            devise_paye TEXT NOT NULL DEFAULT 'FCFA',
            montant_base REAL NOT NULL DEFAULT 0,
            devise_base TEXT NOT NULL DEFAULT 'FCFA',
            duree_jours INTEGER NOT NULL DEFAULT 30,
            methode TEXT NOT NULL DEFAULT '',
            reference TEXT NOT NULL DEFAULT '',
            fraude_flags TEXT NOT NULL DEFAULT '[]',
            fraude_detail TEXT NOT NULL DEFAULT 'OK',
            statut TEXT NOT NULL DEFAULT 'en_attente',
            demande_le TEXT NOT NULL DEFAULT (datetime('now')),
            active_le TEXT,
            expire_le TEXT
        );
        CREATE INDEX idx_sub_ref ON subscriptions(reference);
        """)
        conn.execute("""
            INSERT INTO subscriptions(
                id,user_id,username,plan_id,plan_nom,prix_plan,devise_plan,
                prix_plan_base,montant_paye,devise_paye,montant_base,devise_base,
                duree_jours,methode,reference,fraude_flags,fraude_detail,statut,
                demande_le,active_le,expire_le
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            "sub-1", "user-1", "demo", "mensuel", "Mensuel", 5000, "FCFA",
            5000, 5000, "FCFA", 5000, "FCFA", 30, "orange-money", "PAY-LEGACY",
            "[]", "OK", "actif", "2026-04-20T10:00:00", None, None
        ))
        conn.execute("""
            INSERT INTO subscriptions(
                id,user_id,username,plan_id,plan_nom,prix_plan,devise_plan,
                prix_plan_base,montant_paye,devise_paye,montant_base,devise_base,
                duree_jours,methode,reference,fraude_flags,fraude_detail,statut,
                demande_le,active_le,expire_le
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            "sub-2", "user-2", "demo2", "mensuel", "Mensuel", 5000, "FCFA",
            5000, 5000, "FCFA", 5000, "FCFA", 30, "orange-money", "PAY-LEGACY",
            "[]", "OK", "en_attente", "2026-04-21T10:00:00", None, None
        ))
        conn.commit()
        conn.close()

        db_module.init_db()
        migrated = db_module.get_conn().execute("""
            SELECT id, reference, fraude_flags, fraude_detail
            FROM subscriptions
            ORDER BY id
        """).fetchall()
        self.assertEqual(migrated[0]["reference"], "PAY-LEGACY")
        self.assertTrue(migrated[1]["reference"].startswith("PAY-LEGACY__DUP__"))
        self.assertIn("REFERENCE_DOUBLON_HISTORIQUE", migrated[1]["fraude_flags"])
        self.assertIn("REFERENCE_DOUBLON_HISTORIQUE", migrated[1]["fraude_detail"])


if __name__ == "__main__":
    unittest.main()
