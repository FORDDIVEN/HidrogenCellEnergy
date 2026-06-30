import os
import tempfile
import unittest
import importlib.util
import re

import bcrypt

import controller
import database

if importlib.util.find_spec("flask_login") is not None:
    import app as web_app
else:
    web_app = None


class DatabaseSecurityTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_name = database.DB_NAME
        database.DB_NAME = os.path.join(self.tmp.name, "horno.db")
        self.old_bootstrap = os.environ.pop(
            "SCADA_BOOTSTRAP_ADMIN_PASSWORD",
            None
        )
        self.old_bootstrap_username = os.environ.pop(
            "SCADA_BOOTSTRAP_ADMIN_USERNAME",
            None
        )

    def tearDown(self):
        database.DB_NAME = self.old_db_name

        if self.old_bootstrap is not None:
            os.environ["SCADA_BOOTSTRAP_ADMIN_PASSWORD"] = self.old_bootstrap
        else:
            os.environ.pop("SCADA_BOOTSTRAP_ADMIN_PASSWORD", None)

        if self.old_bootstrap_username is not None:
            os.environ["SCADA_BOOTSTRAP_ADMIN_USERNAME"] = self.old_bootstrap_username
        else:
            os.environ.pop("SCADA_BOOTSTRAP_ADMIN_USERNAME", None)

        self.tmp.cleanup()

    def test_init_db_does_not_create_default_admin_without_env_password(self):
        database.init_db()

        self.assertFalse(database.has_users())

    def test_init_db_creates_admin_from_bootstrap_env_credentials(self):
        os.environ["SCADA_BOOTSTRAP_ADMIN_USERNAME"] = "first-admin"
        os.environ["SCADA_BOOTSTRAP_ADMIN_PASSWORD"] = "strong-test-password"

        database.init_db()
        user = database.get_user("first-admin")

        self.assertIsNotNone(user)
        self.assertEqual(user[3], "admin")
        self.assertTrue(
            bcrypt.checkpw(
                b"strong-test-password",
                user[2].encode()
            )
        )

    def test_acknowledge_alarm_requires_active_alarm(self):
        database.init_db()

        self.assertFalse(database.acknowledge_alarm(999))

        created, alarm_id = database.raise_alarm(
            "TEST_ALARM",
            "critical",
            "test message",
            run_id=42
        )

        self.assertTrue(created)
        self.assertTrue(database.acknowledge_alarm(alarm_id))

        active = database.get_active_alarms()
        self.assertEqual(active[0][9], 42)

        database.clear_alarm("TEST_ALARM")
        self.assertFalse(database.acknowledge_alarm(alarm_id))

    def test_update_user_keeps_password_when_new_password_is_empty(self):
        database.init_db()
        database.create_user("operator", "old-password", "operator")

        old_user = database.get_user("operator")

        database.update_user(
            old_user[0],
            "operator-renamed",
            "",
            "viewer"
        )

        self.assertIsNone(database.get_user("operator"))

        updated_user = database.get_user("operator-renamed")

        self.assertEqual(updated_user[3], "viewer")
        self.assertEqual(updated_user[2], old_user[2])
        self.assertTrue(
            bcrypt.checkpw(
                b"old-password",
                updated_user[2].encode()
            )
        )


class LoginRateLimitTests(unittest.TestCase):

    def tearDown(self):
        if web_app is None:
            return

        web_app.failed_logins.clear()
        web_app.LOGIN_MAX_FAILED_ATTEMPTS = 5
        web_app.LOGIN_LOCKOUT_SECONDS = 15 * 60

    @unittest.skipIf(web_app is None, "Flask-Login no está instalado")
    def test_failed_login_lock_expires(self):
        web_app.failed_logins.clear()
        web_app.LOGIN_MAX_FAILED_ATTEMPTS = 2
        web_app.LOGIN_LOCKOUT_SECONDS = 60

        web_app.register_failed_login("operator", "127.0.0.1")
        attempt = web_app.register_failed_login("operator", "127.0.0.1")

        self.assertTrue(web_app.is_login_locked(attempt))

        key = web_app.login_attempt_key("operator", "127.0.0.1")
        web_app.failed_logins[key]["locked_until"] = web_app.time.time() - 1

        _, expired_attempt = web_app.get_login_attempt("operator", "127.0.0.1")

        self.assertFalse(web_app.is_login_locked(expired_attempt))
        self.assertEqual(expired_attempt["count"], 0)


class SetupAdminTests(unittest.TestCase):

    def setUp(self):
        if web_app is None:
            self.skipTest("Flask-Login no está instalado")

        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_name = database.DB_NAME
        self.old_bootstrap = os.environ.pop(
            "SCADA_BOOTSTRAP_ADMIN_PASSWORD",
            None
        )
        self.old_bootstrap_username = os.environ.pop(
            "SCADA_BOOTSTRAP_ADMIN_USERNAME",
            None
        )
        database.DB_NAME = os.path.join(self.tmp.name, "horno.db")
        web_app.app_initialized = False
        web_app.app.config["TESTING"] = True
        self.client = web_app.app.test_client()

    def tearDown(self):
        if web_app is None:
            return

        database.DB_NAME = self.old_db_name
        web_app.app_initialized = False

        if self.old_bootstrap is not None:
            os.environ["SCADA_BOOTSTRAP_ADMIN_PASSWORD"] = self.old_bootstrap
        else:
            os.environ.pop("SCADA_BOOTSTRAP_ADMIN_PASSWORD", None)

        if self.old_bootstrap_username is not None:
            os.environ["SCADA_BOOTSTRAP_ADMIN_USERNAME"] = self.old_bootstrap_username
        else:
            os.environ.pop("SCADA_BOOTSTRAP_ADMIN_USERNAME", None)

        self.tmp.cleanup()

    def test_setup_creates_first_admin_and_login_redirects_there(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/setup", response.headers["Location"])

        response = self.client.get("/setup")
        token = re.search(
            rb'name="csrf_token" value="([^"]+)"',
            response.data
        ).group(1).decode()

        response = self.client.post("/setup", data={
            "csrf_token": token,
            "username": "thesis-admin",
            "password": "strong-pass",
            "confirm_password": "strong-pass"
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        self.assertIsNotNone(database.get_user("thesis-admin"))


class ControllerStateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_name = database.DB_NAME
        self.old_bootstrap = os.environ.get("SCADA_BOOTSTRAP_ADMIN_PASSWORD")
        self.old_bootstrap_username = os.environ.get("SCADA_BOOTSTRAP_ADMIN_USERNAME")
        database.DB_NAME = os.path.join(self.tmp.name, "horno.db")
        os.environ["SCADA_BOOTSTRAP_ADMIN_USERNAME"] = "state-admin"
        os.environ["SCADA_BOOTSTRAP_ADMIN_PASSWORD"] = "strong-test-password"
        database.init_db()

    def tearDown(self):
        controller.stop_controller()
        database.DB_NAME = self.old_db_name

        if self.old_bootstrap is not None:
            os.environ["SCADA_BOOTSTRAP_ADMIN_PASSWORD"] = self.old_bootstrap
        else:
            os.environ.pop("SCADA_BOOTSTRAP_ADMIN_PASSWORD", None)

        if self.old_bootstrap_username is not None:
            os.environ["SCADA_BOOTSTRAP_ADMIN_USERNAME"] = self.old_bootstrap_username
        else:
            os.environ.pop("SCADA_BOOTSTRAP_ADMIN_USERNAME", None)

        self.tmp.cleanup()

    def test_start_and_stop_reset_pause_and_next_step_state(self):
        controller.controller_paused = True
        controller.next_step_requested = True

        controller.start_controller(run_id=123)

        self.assertTrue(controller.controller_running)
        self.assertFalse(controller.controller_paused)
        self.assertFalse(controller.next_step_requested)

        controller.pause_controller()
        controller.request_next_step()
        controller.stop_controller()

        self.assertFalse(controller.controller_running)
        self.assertFalse(controller.controller_paused)
        self.assertFalse(controller.next_step_requested)

    def test_boot_recovery_closes_stale_running_run(self):
        run_id = database.create_startup_run("tester", True)
        database.save_controller_state(
            "RUNNING",
            current_step=2,
            step_started_at=123.0,
            current_run_id=run_id,
            logging_interval_s=1
        )

        controller.recover_controller_state_on_boot()

        state = database.get_controller_state()
        run = database.get_startup_run(run_id)

        self.assertEqual(state["status"], "STOPPED")
        self.assertIsNone(state["current_run_id"])
        self.assertEqual(run[3], "STOPPED")
        self.assertEqual(run[6], "APP_BOOT_RECOVERY")


if __name__ == "__main__":
    unittest.main()
