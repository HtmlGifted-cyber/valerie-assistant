"""
Tests for email_connect.py using mocks (no network required).
"""

import os
import json
import unittest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.email_connect import load_email_config, list_unread, send_mail, EmailError


class TestEmailConnect(unittest.TestCase):

    @patch("app.email_connect.EMAIL_CONFIG_PATH", "/nonexistent/path/email.json")
    def test_config_missing(self):
        with self.assertRaises(EmailError) as ctx:
            load_email_config()
        self.assertIn("Email config not found", str(ctx.exception))

    @patch("app.email_connect.EMAIL_CONFIG_PATH")
    def test_config_placeholders_refused(self, mock_path):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tf:
            json.dump({
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 465,
                "address": "ENTER_YOUR_EMAIL_ADDRESS_HERE",
                "app_password": "ENTER_YOUR_APP_PASSWORD_HERE"
            }, tf)
            tf_name = tf.name

        mock_path.__str__.return_value = tf_name
        # Also patch os.path.exists and open if needed, but load_email_config uses EMAIL_CONFIG_PATH directly.
        with patch("app.email_connect.os.path.exists", return_value=True):
            with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps({
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 465,
                "address": "ENTER_YOUR_EMAIL_ADDRESS_HERE",
                "app_password": "ENTER_YOUR_APP_PASSWORD_HERE"
            }))):
                with self.assertRaises(EmailError) as ctx:
                    load_email_config()
                self.assertIn("placeholder or empty value", str(ctx.exception))
        try:
            os.unlink(tf_name)
        except OSError:
            pass

    @patch("app.email_connect.load_email_config")
    @patch("imaplib.IMAP4_SSL")
    def test_list_unread_parsed(self, mock_imap_ssl, mock_load_config):
        mock_load_config.return_value = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "smtp_host": "smtp.test.com",
            "smtp_port": 465,
            "address": "test@test.com",
            "app_password": "secretpassword"
        }

        mock_server = MagicMock()
        mock_imap_ssl.return_value = mock_server
        mock_server.search.return_value = ('OK', [b'101 102'])
        mock_server.fetch.return_value = ('OK', [(b'1 (BODY[...] {100}', b'Subject: Test Subject\r\nFrom: sender@test.com\r\nDate: Mon, 30 Oct 2026 12:00:00 +0000\r\n\r\n')])

        unread = list_unread(n=10)
        self.assertEqual(len(unread), 2)
        self.assertEqual(unread[0]["subject"], "Test Subject")
        self.assertEqual(unread[0]["from"], "sender@test.com")
        self.assertEqual(unread[0]["id"], "101")
        mock_server.logout.assert_called_once()

    @patch("app.email_connect.load_email_config")
    @patch("smtplib.SMTP_SSL")
    def test_send_mail_ssl(self, mock_smtp_ssl, mock_load_config):
        mock_load_config.return_value = {
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "smtp_host": "smtp.test.com",
            "smtp_port": 465,
            "address": "test@test.com",
            "app_password": "secretpassword"
        }

        mock_server = MagicMock()
        mock_smtp_ssl.return_value = mock_server

        success = send_mail("recipient@test.com", "Hello", "Test body")
        self.assertTrue(success)
        mock_server.login.assert_called_once_with("test@test.com", "secretpassword")
        mock_server.send_message.assert_called_once()
        mock_server.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
