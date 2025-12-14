from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Iterable, List, Optional

from discord_webhook import DiscordWebhook, DiscordEmbed
from config import BASE_DIR

# Define default carrier stage list and maintenance stage list
CSL = [
    "Waiting...",
    "Jump locked",
    "Lockdown protocol active",
    "Powering FSD",
    "Initiating FSD",
    "Entering hyperspace portal",
    "Traversing hyperspace",
    "Exiting hyperspace portal",
    "FSD cooling down",
    "Jump complete"
]
MSL = [
    "Waiting",
    "Preparing carrier for hyperspace",
    "Services taken down",
    "Landing pads retracting",
    "Bulkheads closing",
    "Airlocks sealing",
    "Task confirmation",
    "Waiting",
    "Restocking Tritium",
    "Done"
]

class DiscordHandler:
    __slots__ = ["lastHook", "lastEmbed", "photo_list", "single_message"]

    def __init__(self, *, single_message: bool = False, photos: Optional[Iterable[str]] = None) -> None:
        self.lastHook = None
        self.lastEmbed = None
        self.single_message = single_message

        if photos is not None:
            self.photo_list: List[str] = list(photos)
        else:
            photos_path = BASE_DIR / "photos.txt"
            try:
                with photos_path.open("r", encoding="utf-8") as photosFile:
                    self.photo_list = photosFile.read().split()
            except Exception as e:
                print("Failed to get image URLs in photos.txt with error: ", e)
                print("Using fallback URL...")
                self.photo_list = ["https://upload.wikimedia.org/wikipedia/en/e/e5/Elite_Dangerous.png"]


    def post_to_discord(self, subject: str, webhook_url: str, routeName: str, *message: str):
        """Send a simple message to a Discord webhook."""
        if webhook_url == "":
            return
        try:
            photo = random.choice(self.photo_list)
            self._prepare_hook(webhook_url, subject, routeName, message, photo)
            self._send_or_edit()
        except Exception as e:
            print("Discord webhook failed with error: ", e)
            print("Double-check that the webhook is set up")


    def post_with_fields(self, subject: str, webhook_url: str, routeName: str, *message: str):
        """Send a message to a Discord webhook with status fields attached."""
        if webhook_url == "":
            return
        try:
            photo = random.choice(self.photo_list)
            self._prepare_hook(webhook_url, subject, routeName, message, photo)
            self._reset_fields()
            self._add_fields(("Jump stage", "Wait..."), ("Maintenance stage", "Wait..."))
            self._send_or_edit()
        except Exception as e:
            print("Discord webhook failed with error: ", e)
            print("Double-check that the webhook is set up")


    def update_fields(self, carrierStage: int, maintenanceStage: int):
        if not self.lastHook:
            return
        try:
            cur_CSL, cur_MSL = [], []  # Define carrier stage list and maintenance stage list for the current field update

            # Add strikethru to every carrier stage before current
            for c_stage_name in CSL[:carrierStage]:
                cur_CSL.append(f"~~{c_stage_name}~~")
            # Bold current stage
            cur_CSL.append(f"**{CSL[carrierStage]}**")
            # Add remaining stages as normal text
            cur_CSL += CSL[carrierStage+1:]
            
            # Add strikethru & "...DONE" signifier to every maintenance stage before current
            for m_stage_name in MSL[:maintenanceStage]:
                cur_MSL.append(f"~~{m_stage_name}...DONE~~")
            # Bold current stage and add ellipsis
            cur_MSL.append(f"**{MSL[maintenanceStage]}...**")
            # Add remaining stages as normal text
            cur_MSL += MSL[maintenanceStage+1:]

            # Once the jump is finished, replace all countdowns with a static text blurb
            if maintenanceStage == 9 and self.lastEmbed.description:
                while re.search(r"<t:\d*:R>", self.lastEmbed.description):
                    self.lastEmbed.description = str(re.sub(r"<t:\d*:R>", "Countdown Expired", self.lastEmbed.description))
        
        
            self._reset_fields()
            self._add_fields(
                ("Jump stage", "\n".join(cur_CSL)),
                ("Maintenance stage", "\n".join(cur_MSL)),
            )
            self.lastHook.edit()
        except Exception as e:
            print("Discord webhook failed with error: ", e)
            print("Double-check that the webhook is set up")
            # print(f"DEBUG DATA: lastHook: {self.lastHook}, lastEmbed: {self.lastEmbed}")

    def _prepare_hook(
        self,
        webhook_url: str,
        subject: str,
        route_name: str,
        message: tuple[str, ...],
        photo: str,
    ) -> None:
        if self.single_message and self.lastHook is not None and self.lastEmbed is not None:
            self.lastHook.remove_embeds()
            embed = self.lastEmbed
            embed.title = subject
            embed.description = "\n".join(message)
        else:
            self.lastHook = DiscordWebhook(url=webhook_url, rate_limit_retry=True)
            embed = DiscordEmbed(title=subject, description="\n".join(message))
            self.lastEmbed = embed

        embed.set_image(url=photo)
        embed.set_author(name=route_name)
        embed.set_footer(text="Carrier Administration and Traversal System")

    def _reset_fields(self) -> None:
        if not self.lastHook or not self.lastEmbed:
            return

        self.lastHook.remove_embeds()
        try:
            self.lastEmbed.delete_embed_field(0)
            self.lastEmbed.delete_embed_field(0)
        except Exception:
            pass

    def _add_fields(self, *fields: tuple[str, str]) -> None:
        if not self.lastEmbed:
            return

        for name, value in fields:
            self.lastEmbed.add_embed_field(name=name, value=value)

        if self.lastHook:
            self.lastHook.add_embed(self.lastEmbed)

    def _send_or_edit(self) -> None:
        if not self.lastHook:
            return

        if self.single_message and self.lastHook is not None and self.lastEmbed is not None:
            self.lastHook.edit()
        else:
            self.lastHook.execute()
