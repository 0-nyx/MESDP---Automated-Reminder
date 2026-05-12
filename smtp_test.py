import smtplib
import ssl
import sys
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr
from config import CONFIG

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

# Get email config
email_config = CONFIG['email']
smtp_server = email_config['smtp_server']
smtp_port = email_config['smtp_port']
sender = email_config['sender']
password = email_config['password']
recipients = []
for addr in email_config.get('recipients', []):
    _, email_addr = parseaddr(str(addr))
    if "@" in email_addr:
        recipients.append(email_addr)

if not recipients:
    recipients = [sender]

print(f"Testing SMTP connection to {smtp_server}:{smtp_port}")
print(f"Sender: {sender}")
print(f"Recipients: {', '.join(recipients)}")
print(f"Password length: {len(password)}")

try:
    # Create SSL context
    context = ssl.create_default_context()

    # Connect to server
    print("Connecting to SMTP server...")
    server = smtplib.SMTP(smtp_server, smtp_port)
    server.ehlo()

    # Start TLS
    print("Starting TLS...")
    server.starttls(context=context)
    server.ehlo()

    # Login
    print("Attempting login...")
    server.login(sender, password)
    print("✅ Login successful!")

    # Send test email
    print("Sending test email...")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"MESDP SMTP Test - {now}"
    body = (
        "This is a test email from MESDP Worklog Reminder.\n\n"
        f"Sent at: {now}\n"
        f"SMTP server: {smtp_server}:{smtp_port}\n"
    )
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1])

    server.sendmail(sender, recipients, message.as_string())
    print("✅ Test email sent successfully!")

    server.quit()
    print("✅ SMTP test completed successfully!")

except Exception as e:
    print(f"❌ SMTP Error: {e}")
    print(f"Error type: {type(e).__name__}")
