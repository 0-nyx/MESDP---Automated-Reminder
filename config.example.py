# config.example.py — commit-safe template
# Reference only. Real values live in %APPDATA%\CLL MESDP\app_config.json
# (same keys, JSON), written by the Scheduler Control GUI. See config.py.

CONFIG = {
    "mesdp": {
        "base_url": "https://servicedesk.example.com:8443",
        "auth_token": "REPLACE_WITH_MESDP_AUTH_TOKEN",
    },
    "email": {
        "smtp_server": "smtp.office365.com",
        "smtp_port": 587,
        "use_starttls": True,
        "sender": "sender@example.com",
        "password": "REPLACE_WITH_EMAIL_PASSWORD_OR_APP_PASSWORD",
        "recipients": [
            "recipient1@example.com",
            "recipient2@example.com",
        ],
    },
    "teams": {
        "webhook_url": "https://outlook.office.com/webhook/REPLACE_WITH_WEBHOOK",
    },
}
