# Review Report: valerie-assistant (email-imap)

## Task Summary
Added IMAP & SMTP email connector (`app/email_connect.py`) without Google API keys, along with configuration handling, example config (`email.example.json`), tests (`tests/test_email_connect.py`), and setup docs (`docs/EMAIL.md`).

## Verification Output
```
....
----------------------------------------------------------------------
Ran 4 tests in 0.031s

OK
```
All unit tests for configuration loading, placeholder rejection, unread listing, and email sending (mocked) passed successfully.
