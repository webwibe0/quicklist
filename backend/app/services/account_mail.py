"""TLS SMTP only. Test transport is supplied by dependency override; no token logs/files."""
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import get_settings


class MailUnavailable(RuntimeError):
    pass


class SMTPMail:
    def send(self, recipient, subject, body):
        settings = get_settings()
        if not settings.mail_host or not settings.mail_from or any(c in settings.mail_from for c in '\r\n'):
            raise MailUnavailable('MAIL_NOT_CONFIGURED')
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = settings.mail_from, recipient, subject
        message.set_content(body)
        context = ssl.create_default_context(cafile=settings.mail_ca_file or None)
        try:
            if settings.mail_ssl:
                client = smtplib.SMTP_SSL(settings.mail_host, settings.mail_port, timeout=5,
                                          context=context)
            else:
                client = smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=5)
            with client:
                if not settings.mail_ssl:
                    client.starttls(context=context)
                if settings.mail_username:
                    client.login(settings.mail_username, settings.mail_password.get_secret_value())
                client.send_message(message)
        except (OSError, smtplib.SMTPException):
            raise MailUnavailable('MAIL_DELIVERY_UNCONFIRMED') from None


def get_mail():
    return SMTPMail()
