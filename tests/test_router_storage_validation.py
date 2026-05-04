import unittest

import database


class RouterStorageValidationTests(unittest.TestCase):
    def test_unknown_driver_falls_back_to_mikrotik(self):
        router = database._normalize_router({"name": "R1", "host": "1.1.1.1", "driver": "unknown"})
        self.assertEqual(router["driver"], "mikrotik")

    def test_invalid_port_falls_back_to_default(self):
        router = database._normalize_router({"name": "R1", "host": "1.1.1.1", "port": -5})
        self.assertEqual(router["port"], 8728)


if __name__ == "__main__":
    unittest.main()
