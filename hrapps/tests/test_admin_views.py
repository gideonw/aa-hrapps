from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from allianceauth.tests.auth_utils import AuthUtils

from hrapps.models import HRAppDiscordSettings, RecruitmentMode


class RecruitmentSettingsViewTests(TestCase):
    """follow=False throughout: the dashboard this view redirects to reads
    request.user.profile.main_character.corporation_id, which is None for a
    user with no main character, so following the redirect would raise
    AttributeError instead of showing the message under test.

    The user still needs a main character to reach update_discord at all:
    every hradmin: URL is wrapped by AllianceAuth's UrlHook in
    main_character_required (allianceauth/urls.py), which redirects to
    authentication:dashboard with its own message before the view runs for
    a user with none. A bare create_superuser() user has no main character,
    so it is added explicitly here."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "password"
        )
        AuthUtils.add_main_character(
            self.user,
            "Admin Character",
            "1",
            corp_id=1,
            corp_name="Test Corp",
            corp_ticker="TEST",
        )
        self.user.refresh_from_db()
        self.client.force_login(self.user)

    def _post(self, **overrides):
        data = {"recruitment": "true", "mode": RecruitmentMode.OFF}
        data.update(overrides)
        # update_discord saves the HRAppDiscordSettings singleton, whose
        # post_save signal (hrapps.signals.announce_update) publishes to
        # Redis unconditionally. Patch the client it uses so these POSTs
        # never open a real Redis connection.
        with patch("hrapps.signals.get_redis_client"):
            return self.client.post(reverse("hradmin:update_discord"), data)

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_channels_mode_without_a_category_names_the_missing_field(self):
        response = self._post(
            mode=RecruitmentMode.CHANNELS, role="1", rrole="2", archive_category="3"
        )
        self.assertIs(
            any("recruitment category" in m for m in self._messages(response)),
            True,
            self._messages(response),
        )

    def test_channels_mode_without_a_recruiter_role_names_it(self):
        response = self._post(
            mode=RecruitmentMode.CHANNELS, category="1", archive_category="2", rrole="3"
        )
        self.assertIs(
            any("recruiter role" in m for m in self._messages(response)),
            True,
            self._messages(response),
        )

    def test_threads_mode_without_a_thread_channel_is_rejected(self):
        response = self._post(mode=RecruitmentMode.THREADS, role="1")
        self.assertIs(
            any("thread channel" in m for m in self._messages(response)),
            True,
            self._messages(response),
        )

    def test_unknown_mode_is_rejected(self):
        response = self._post(mode="nonsense")
        self.assertIs(
            any("Unknown recruitment mode" in m for m in self._messages(response)),
            True,
            self._messages(response),
        )

    def test_fully_configured_channels_mode_saves(self):
        self._post(
            mode=RecruitmentMode.CHANNELS,
            category="100",
            archive_category="200",
            role="300",
            rrole="400",
        )
        saved = HRAppDiscordSettings.get_solo()
        self.assertEqual(saved.recruitment_mode, RecruitmentMode.CHANNELS)
        self.assertEqual(saved.recruitment_category, 100)
        self.assertEqual(saved.recruitment_archive_category, 200)
        self.assertEqual(saved.recruiter_role, 300)

    def test_recruit_role_is_not_overwritten_by_the_recruiter_role(self):
        """The bug fixed in fd1965c, pinned here: these two fields are adjacent
        and easy to cross-wire again."""
        self._post(
            mode=RecruitmentMode.CHANNELS,
            category="100",
            archive_category="200",
            role="300",
            rrole="400",
        )
        saved = HRAppDiscordSettings.get_solo()
        self.assertEqual(saved.recruit_role, 400)
        self.assertNotEqual(saved.recruit_role, saved.recruiter_role)
