"""
notifications.py — Multi-channel notification dispatcher.

Priority order: SMS (Twilio) → Email (SMTP) → Discord webhook.
Each channel is tried in order; the first success stops the chain.

Required env vars per channel
──────────────────────────────
SMS:     TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER
Email:   SMTP_HOST, SMTP_USER, SMTP_PASS, NOTIFY_EMAIL_TO
         (optional: SMTP_PORT defaults to 587, NOTIFY_EMAIL_FROM defaults to SMTP_USER)
Discord: DISCORD_WEBHOOK_URL
"""

import asyncio
import base64
import logging
import os
import smtplib
from email.mime.text import MIMEText
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


async def send_notification(message: str) -> bool:
    """
    Send *message* via the first configured and successful channel.
    Returns True if at least one channel succeeded.
    """
    for channel in (_send_ntfy, _send_sms, _send_email, _send_discord):
        try:
            if await channel(message):
                return True
        except Exception as exc:
            logger.error("Notification channel %s raised: %s", channel.__name__, exc)
    logger.warning("All notification channels failed or unconfigured")
    return False


async def _send_ntfy(message: str) -> bool:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        logger.debug("ntfy channel not configured (set NTFY_TOPIC)")
        return False

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode(),
            headers={"Title": "Claude Agent", "Priority": "high"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.info("ntfy notification sent to topic %s", topic)
                return True
            body = await resp.text()
            logger.warning("ntfy failed (%s): %s", resp.status, body[:200])
            return False


async def _send_sms(message: str) -> bool:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token  = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number   = os.getenv("TWILIO_TO_NUMBER")

    if not all([account_sid, auth_token, from_number, to_number]):
        logger.debug("SMS channel not configured")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    creds = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={"Authorization": f"Basic {creds}"},
            data={"From": from_number, "To": to_number, "Body": message[:1600]},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status in (200, 201):
                logger.info("SMS notification sent via Twilio to %s", to_number)
                return True
            body = await resp.text()
            logger.warning("Twilio SMS failed (%s): %s", resp.status, body[:300])
            return False


async def _send_email(message: str) -> bool:
    smtp_host   = os.getenv("SMTP_HOST")
    smtp_port   = int(os.getenv("SMTP_PORT", "587"))
    smtp_user   = os.getenv("SMTP_USER")
    smtp_pass   = os.getenv("SMTP_PASS")
    to_email    = os.getenv("NOTIFY_EMAIL_TO")
    from_email  = os.getenv("NOTIFY_EMAIL_FROM", smtp_user)

    if not all([smtp_host, smtp_user, smtp_pass, to_email]):
        logger.debug("Email channel not configured")
        return False

    msg = MIMEText(message)
    msg["Subject"] = "Claude Agent: Usage Limit Reset"
    msg["From"]    = from_email
    msg["To"]      = to_email

    def _blocking_send() -> None:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(smtp_user, smtp_pass)
            smtp.send_message(msg)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _blocking_send)
    logger.info("Email notification sent to %s", to_email)
    return True


async def _send_discord(message: str) -> bool:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("Discord channel not configured")
        return False

    async with aiohttp.ClientSession() as session:
        async with session.post(
            webhook_url,
            json={"content": message[:2000]},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status in (200, 204):
                logger.info("Discord notification sent")
                return True
            body = await resp.text()
            logger.warning("Discord webhook failed (%s): %s", resp.status, body[:300])
            return False
