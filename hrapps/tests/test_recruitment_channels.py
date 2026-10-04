from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from allianceauth.eveonline.models import EveCorporationInfo
from allianceauth.tests.auth_utils import AuthUtils

from aadiscordbot.cogs.utils.exceptions import NotAuthenticated

from hrapps import recruitment_channels as rc
from hrapps.models import Form, FormResponse, RecruitmentChannel, StatusChoices


class OrmHelperTests(TestCase):
    def test_get_open_channel_id_returns_none_when_absent(self):
        self.assertIsNone(rc.get_open_channel_id(111))

    def test_get_open_channel_id_finds_the_open_row(self):
        RecruitmentChannel.objects.create(discord_user_id=111, channel_id=222)
        self.assertEqual(rc.get_open_channel_id(111), 222)

    def test_get_open_channel_id_ignores_archived_rows(self):
        row = RecruitmentChannel.objects.create(discord_user_id=111, channel_id=222)
        row.archived = timezone.now()
        row.save()
        self.assertIsNone(rc.get_open_channel_id(111))

    def test_record_channel_creates_an_open_row(self):
        rc.record_channel(111, 222)
        row = RecruitmentChannel.objects.get(channel_id=222)
        self.assertEqual(row.open_for_user, 111)
        self.assertIsNone(row.archived)

    def test_mark_archived_releases_the_user_for_a_new_channel(self):
        rc.record_channel(111, 222)
        rc.mark_archived(222)
        row = RecruitmentChannel.objects.get(channel_id=222)
        self.assertIsNotNone(row.archived)
        self.assertIsNone(row.open_for_user)
        # Must not raise: the unique index no longer sees the archived row.
        rc.record_channel(111, 333)

    def test_mark_archived_is_a_noop_for_unknown_channels(self):
        rc.mark_archived(999)


class ClosedApplicationDiscordIdTests(TestCase):
    def _application(self, status):
        corporation = EveCorporationInfo.objects.create(
            corporation_id=1,
            corporation_name="Test Corp",
            corporation_ticker="TEST",
            member_count=1,
        )
        form = Form.objects.create(corporation=corporation, name="Application", fields={})
        user = AuthUtils.create_user("applicant")
        AuthUtils.add_main_character(
            user,
            "Test Character",
            "1",
            corp_id=1,
            corp_name="Test Corp",
            corp_ticker="TEST",
        )
        # add_main_character sets main_character via a second UserProfile
        # instance; without this, the `user` object's cached reverse `.profile`
        # relation (set by get_or_create's own UserProfile creation during
        # AuthUtils.create_user) is stale and still shows main_character=None.
        user.refresh_from_db()
        # FormResponse's post_save signal (hrapps.signals.announce_app_changes)
        # publishes to Redis when aadiscordbot is installed. Patch the client
        # it uses so creating the fixture never opens a real Redis connection.
        with patch("hrapps.signals.get_redis_client"):
            return FormResponse.objects.create(
                user=user, form=form, response={}, status=status
            )

    def test_returns_none_and_skips_lookup_when_not_closed(self):
        application = self._application(StatusChoices.PENDING)
        with patch("hrapps.recruitment_channels.get_discord_user_id") as mock_lookup:
            result = rc.closed_application_discord_id(application.pk)
        self.assertIsNone(result)
        mock_lookup.assert_not_called()

    def test_returns_none_when_closed_but_not_linked_to_discord(self):
        application = self._application(StatusChoices.APPROVED)
        with patch(
            "hrapps.recruitment_channels.get_discord_user_id",
            side_effect=NotAuthenticated,
        ):
            result = rc.closed_application_discord_id(application.pk)
        self.assertIsNone(result)

    def test_returns_discord_id_when_closed_and_linked(self):
        application = self._application(StatusChoices.APPROVED)
        with patch(
            "hrapps.recruitment_channels.get_discord_user_id", return_value=444
        ):
            result = rc.closed_application_discord_id(application.pk)
        self.assertEqual(result, 444)
