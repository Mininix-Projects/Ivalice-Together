import unittest

import launcher_tray


class LauncherTrayTests(unittest.TestCase):
    def test_exit_request_is_consumed_once(self):
        tray = launcher_tray.WindowsTrayIcon("unused.ico", "Ivalice Together")
        tray._exit_requested.set()

        self.assertTrue(tray.consume_exit_request())
        self.assertFalse(tray.consume_exit_request())

    def test_menu_labels_can_follow_launcher_language(self):
        tray = launcher_tray.WindowsTrayIcon("unused.ico", "Ivalice Together")
        tray.set_menu_labels("Abrir launcher", "Fechar completamente")

        self.assertEqual(tray._restore_label, "Abrir launcher")
        self.assertEqual(tray._exit_label, "Fechar completamente")


if __name__ == "__main__":
    unittest.main()
