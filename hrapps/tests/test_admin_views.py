from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from allianceauth.eveonline.models import EveCorporationInfo
from allianceauth.tests.auth_utils import AuthUtils

from hrapps.models import Form, FormResponse, HRAppDiscordSettings, RecruitmentMode


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

class WelcomeSettingsViewTests(TestCase):
    """Round-trips the welcome card's settings. follow=False and the explicit
    main character are needed for the same reasons documented on
    RecruitmentSettingsViewTests above."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            "welcomeadmin", "welcomeadmin@example.com", "password"
        )
        AuthUtils.add_main_character(
            self.user,
            "Welcome Admin",
            "2",
            corp_id=1,
            corp_name="Test Corp",
            corp_ticker="TEST",
        )
        self.user.refresh_from_db()
        self.client.force_login(self.user)

    def _post(self, **overrides):
        data = {"welcome": "true", "channel": "500", "message": "hi {user_mention}"}
        data.update(overrides)
        with patch("hrapps.signals.get_redis_client"):
            return self.client.post(reverse("hradmin:update_discord"), data)

    def test_recruit_button_toggle_saves_when_ticked(self):
        self._post(enabled="on", include_recruit_button="on")
        saved = HRAppDiscordSettings.get_solo()
        self.assertIs(saved.welcome_include_recruit_button, True)

    def test_recruit_button_toggle_saves_when_unticked(self):
        """An unchecked HTML checkbox posts no key at all, so the handler must
        treat absence as False rather than leaving the previous value."""
        self._post(enabled="on", include_recruit_button="on")
        self._post(enabled="on")
        saved = HRAppDiscordSettings.get_solo()
        self.assertIs(saved.welcome_include_recruit_button, False)

    def test_welcome_enabled_round_trips(self):
        self._post(enabled="on")
        self.assertIs(HRAppDiscordSettings.get_solo().enable_welcome_messages, True)

    def test_welcome_message_and_channel_round_trip(self):
        self._post(enabled="on", channel="12345", message="welcome {user_mention}")
        saved = HRAppDiscordSettings.get_solo()
        self.assertEqual(saved.welcome_channel, 12345)
        self.assertEqual(saved.welcome_message, "welcome {user_mention}")


class _TableRows(HTMLParser):
    """Collects the cell count of every <tr> inside one table's <tbody>.

    Counts explicit tags only. A browser opens an implied <tr> for a stray
    <td>, which is exactly how the missing rows went unnoticed: every
    application rendered, just all in one row."""

    def __init__(self, table_id):
        super().__init__()
        self.table_id = table_id
        self.in_table = self.in_body = False
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "table" and dict(attrs).get("id") == self.table_id:
            self.in_table = True
        elif self.in_table and tag == "tbody":
            self.in_body = True
        elif self.in_body and tag == "tr":
            self.rows.append(0)
        elif self.in_body and tag == "td":
            if not self.rows:
                self.rows.append(0)
            self.rows[-1] += 1

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_body = False
        elif tag == "table":
            self.in_table = False


# The page renders the full AllianceAuth base template, whose {% static %}
# tags need a manifest that only collectstatic builds. Plain storage resolves
# them without one.
@override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
class ActiveApplicationsTableTests(TestCase):
    """Each active application must be its own row. DataTables rejects the
    table outright ("Incorrect column count") when the cells of every
    application share one row."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            "tableadmin", "tableadmin@example.com", "password"
        )
        AuthUtils.add_main_character(
            self.user,
            "Table Admin",
            "3",
            corp_id=1,
            corp_name="Test Corp",
            corp_ticker="TEST",
        )
        self.user.refresh_from_db()
        self.client.force_login(self.user)

        corp = EveCorporationInfo.objects.create(
            corporation_id=1,
            corporation_name="Test Corp",
            corporation_ticker="TEST",
            member_count=1,
        )
        form = Form.objects.create(corporation=corp, name="Apply", fields=[])
        # FormResponse's post_save signals read the applicant's main character
        # and publish the new application to Redis.
        for character_id in ("10", "11"):
            applicant = AuthUtils.create_user(f"applicant{character_id}")
            AuthUtils.add_main_character(
                applicant, f"Applicant {character_id}", character_id, corp_id=1
            )
            applicant.refresh_from_db()
            with patch("hrapps.signals.get_redis_client"):
                FormResponse.objects.create(user=applicant, form=form, response={})

    def test_each_application_is_its_own_row(self):
        with patch("hrapps.signals.get_redis_client"):
            response = self.client.get(reverse("hradmin:dashboard"))
        self.assertEqual(response.status_code, 200)
        parser = _TableRows("applications-table")
        parser.feed(response.content.decode())
        self.assertEqual(parser.rows, [6, 6])
