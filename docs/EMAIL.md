# Email Access Setup (IMAP / SMTP)

Valerie includes an IMAP and SMTP email connector (`app/email_connect.py`) that operates without Google API keys or OAuth setup.

## 1. Configuration File

Create the configuration file at `~/.config/valerie-assistant/email.json`:

```json
{
  "imap_host": "imap.gmail.com",
  "imap_port": 993,
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 465,
  "address": "ENTER_YOUR_EMAIL_ADDRESS_HERE",
  "app_password": "your-app-password"
}
```

An example template is provided in the repository root as `email.example.json`.

---

## 2. Gmail Setup (App Password)

If you use Gmail, Google requires an **App Password** rather than your main account password when connecting via IMAP/SMTP:

1. Enable **2-Step Verification** on your Google Account (`Google Account > Security > 2-Step Verification`).
2. Go back to `Security` and search for **App passwords**.
3. Create a new app password (e.g. name it "Valerie Assistant").
4. Copy the 16-character password into `app_password` in `email.json`.

---

## 3. Other Providers

For other providers (Fastmail, Outlook, custom domains):
- Set `imap_host` and `imap_port` (typically `993` for SSL).
- Set `smtp_host` and `smtp_port` (`465` for SSL or `587` for STARTTLS).
- Use your standard email credentials or provider-specific app passwords.
