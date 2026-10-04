import importlib
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from hrapps.models import RecruitmentMode

# The module name is not a valid Python identifier, so it cannot be imported
# with `from ... import ...`.
cog = importlib.import_module("hrapps.cogs.aa-hrapps")


class WantedSubscriptionTests(SimpleTestCase):
    def test_channels_mode_subscribes_even_with_notifications_off(self):
        """Review Focus 4. This is the defect that would make archiving
        silently never run."""
        settings = MagicMock(
            enable_application_notifications=False,
            recruitment_mode=RecruitmentMode.CHANNELS,
        )
        self.assertIn(
            "hrapp_application_notifications", cog.wanted_subscriptions(settings)
        )

    def test_threads_mode_with_notifications_off_subscribes_to_settings_only(self):
        settings = MagicMock(
            enable_application_notifications=False,
            recruitment_mode=RecruitmentMode.THREADS,
        )
        self.assertEqual(
            cog.wanted_subscriptions(settings), {"hrapp_discord_settings"}
        )

    def test_notifications_on_subscribes_to_comments_too(self):
        settings = MagicMock(
            enable_application_notifications=True,
            recruitment_mode=RecruitmentMode.OFF,
        )
        self.assertEqual(
            cog.wanted_subscriptions(settings),
            {
                "hrapp_discord_settings",
                "hrapp_application_notifications",
                "hrapp_comment_notifications",
            },
        )

    def test_settings_channel_is_always_wanted(self):
        settings = MagicMock(
            enable_application_notifications=False,
            recruitment_mode=RecruitmentMode.OFF,
        )
        self.assertEqual(
            cog.wanted_subscriptions(settings), {"hrapp_discord_settings"}
        )
