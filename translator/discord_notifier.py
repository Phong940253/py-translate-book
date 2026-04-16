import json
import logging
from datetime import datetime
from urllib import request, error


class DiscordNotifier:
    _disabled_webhooks: set[str] = set()

    @staticmethod
    def _format_timestamp(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def send_translation_completed(
        webhook_url: str,
        mention_user_id: str | None,
        stats: dict,
    ) -> bool:
        webhook_url = (webhook_url or "").strip()

        started_at = stats.get("started_at")
        finished_at = stats.get("finished_at")
        started_label = (
            DiscordNotifier._format_timestamp(started_at)
            if isinstance(started_at, datetime)
            else "N/A"
        )
        finished_label = (
            DiscordNotifier._format_timestamp(finished_at)
            if isinstance(finished_at, datetime)
            else "N/A"
        )

        mention_line = ""
        if mention_user_id:
            mention_line = f"<@{mention_user_id}>"

        embed = {
            "title": "Dich truyen hoan tat",
            "description": stats.get("summary", "Qua trinh dich da ket thuc thanh cong."),
            "color": 0x3BA55D,
            "fields": [
                {
                    "name": "Input",
                    "value": str(stats.get("input_name", "N/A")),
                    "inline": False,
                },
                {
                    "name": "Output",
                    "value": str(stats.get("output_name", "N/A")),
                    "inline": False,
                },
                {
                    "name": "Engine",
                    "value": str(stats.get("engine", "N/A")),
                    "inline": True,
                },
                {
                    "name": "Chapters",
                    "value": str(stats.get("chapters_label", "N/A")),
                    "inline": True,
                },
                {
                    "name": "Elapsed",
                    "value": str(stats.get("elapsed_label", "N/A")),
                    "inline": True,
                },
                {
                    "name": "Start",
                    "value": started_label,
                    "inline": True,
                },
                {
                    "name": "Finish",
                    "value": finished_label,
                    "inline": True,
                },
                {
                    "name": "Chunk stats",
                    "value": str(stats.get("chunk_stats", "N/A")),
                    "inline": False,
                },
                {
                    "name": "File stats",
                    "value": str(stats.get("file_stats", "N/A")),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "py-translate-book notifier",
            },
            "timestamp": (
                finished_at.isoformat()
                if isinstance(finished_at, datetime)
                else datetime.now().isoformat()
            ),
        }

        payload = {
            "content": mention_line,
            "embeds": [embed],
            "allowed_mentions": {"parse": ["users"]},
        }

        return DiscordNotifier._send_payload(webhook_url, payload)

    @staticmethod
    def send_test_message(
        webhook_url: str,
        mention_user_id: str | None = None,
        note: str | None = None,
    ) -> bool:
        now = datetime.now()
        mention_line = f"<@{mention_user_id}>" if mention_user_id else ""
        embed = {
            "title": "Discord webhook test",
            "description": note or "Webhook test from py-translate-book CLI.",
            "color": 0x2D9CDB,
            "fields": [
                {
                    "name": "Time",
                    "value": DiscordNotifier._format_timestamp(now),
                    "inline": False,
                },
                {
                    "name": "Status",
                    "value": "If you see this message, webhook is working.",
                    "inline": False,
                },
            ],
            "footer": {
                "text": "py-translate-book notifier",
            },
            "timestamp": now.isoformat(),
        }
        payload = {
            "content": mention_line,
            "embeds": [embed],
            "allowed_mentions": {"parse": ["users"]},
        }

        return DiscordNotifier._send_payload(webhook_url, payload)

    @staticmethod
    def _send_payload(webhook_url: str, payload: dict) -> bool:
        webhook_url = (webhook_url or "").strip()
        if not webhook_url:
            logging.info("Discord webhook is empty, skip notification")
            return False

        if webhook_url in DiscordNotifier._disabled_webhooks:
            logging.info("Discord webhook is disabled after previous auth/permission error, skip notification")
            return False

        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "py-translate-book/1.0 (+discord-webhook)",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=15) as response:
                status = getattr(response, "status", None)
                if status is None or 200 <= status < 300:
                    logging.info("Discord notification sent successfully")
                    return True

                logging.warning(f"Discord notification failed with status {status}")
                return False
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""

            details = f"Discord notification HTTP error: {exc.code} {exc.reason}"
            if body:
                details = f"{details} | body={body[:300]}"

            if "1010" in body:
                details = (
                    f"{details}. Cloudflare 1010 indicates this machine/network is blocked "
                    "for direct calls to discord.com (online test sites may still work because "
                    "they send from their own servers)."
                )

            # Auth/permission/not-found failures are persistent for a webhook URL.
            # Disable future attempts in-process to avoid warning spam every chapter.
            if exc.code in (401, 403, 404):
                DiscordNotifier._disabled_webhooks.add(webhook_url)
                logging.warning(
                    f"{details}. Webhook disabled for this run. "
                    "Update discord.webhook_url or set discord.enabled=false to silence notifications."
                )
                return False

            logging.warning(details)
            return False
        except Exception as exc:
            logging.warning(f"Discord notification failed: {exc}")
            return False