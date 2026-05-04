import unittest

import mikrotik

class DummyDriver:
    def __init__(self):
        self.called = False

    def connect(self, host, user, password, port=8728, timeout=10):
        self.called = True
        return object()

    def safe_connect(self, host, user, password, port=8728, timeout=10):
        self.called = True
        return (object(), None)

class DriverRegistryTests(unittest.TestCase):
    def test_builtin_driver_registered(self):
        d = mikrotik.get_driver('mikrotik')
        self.assertIsNotNone(d, "Default 'mikrotik' driver should be registered")

    def test_register_and_get_driver(self):
        dummy = DummyDriver()
        mikrotik.register_driver('dummy', dummy)
        got = mikrotik.get_driver('dummy')
        self.assertIs(got, dummy)

    def test_unknown_driver_behavior(self):
        # safe_connect should return (None, error) for missing driver
        api, err = mikrotik.safe_connect('1.2.3.4', 'u', 'p', driver='no-such')
        self.assertIsNone(api)
        self.assertIsNotNone(err)
        # connect should raise RuntimeError for missing driver
        with self.assertRaises(RuntimeError):
            mikrotik.connect('1.2.3.4', 'u', 'p', driver='no-such')

    def test_dummy_driver_calls(self):
        dummy = DummyDriver()
        mikrotik.register_driver('dummy2', dummy)
        d = mikrotik.get_driver('dummy2')
        # call methods and verify they return expected shapes
        api = d.connect('h', 'u', 'p')
        self.assertIsNotNone(api)
        api2, err = d.safe_connect('h', 'u', 'p')
        self.assertIsNone(err)
        self.assertIsNotNone(api2)

    def test_resource_get_exposes_routeros_dot_id_as_id(self):
        class FakeApi:
            def path(self, path):
                self.last_path = path
                return [
                    {".id": "*A", "name": "ticket-001", "disabled": "no"},
                    {"id": "*B", "name": "ticket-002", "disabled": "yes"},
                ]

        resource = mikrotik._Resource(FakeApi(), "/ip/hotspot/user")
        rows = resource.get()
        self.assertEqual(rows[0]["id"], "*A")
        self.assertEqual(rows[0][".id"], "*A")
        self.assertEqual(rows[1]["id"], "*B")

if __name__ == '__main__':
    unittest.main()
