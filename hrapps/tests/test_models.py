from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from hrapps.models import HRAppDiscordSettings, RecruitmentChannel, RecruitmentMode


class RecruitmentChannelTests(TestCase):
    def test_open_row_sets_open_for_user(self):
        row = RecruitmentChannel.objects.create(discord_user_id=111, channel_id=222)
        self.assertEqual(row.open_for_user, 111)

    def test_archiving_clears_open_for_user(self):
        row = RecruitmentChannel.objects.create(discord_user_id=111, channel_id=222)
        row.archived = timezone.now()
        row.save()
        row.refresh_from_db()
        self.assertIsNone(row.open_for_user)

    def test_second_open_channel_for_same_user_is_rejected(self):
        """The whole point of open_for_user. A conditional UniqueConstraint
        would pass this test on SQLite and silently enforce nothing on MySQL,
        which is what this column exists to avoid."""
        RecruitmentChannel.objects.create(discord_user_id=111, channel_id=222)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecruitmentChannel.objects.create(discord_user_id=111, channel_id=333)

    def test_many_archived_channels_for_same_user_are_allowed(self):
        first = RecruitmentChannel.objects.create(discord_user_id=111, channel_id=222)
        first.archived = timezone.now()
        first.save()
        second = RecruitmentChannel.objects.create(discord_user_id=111, channel_id=333)
        second.archived = timezone.now()
        second.save()
        self.assertEqual(
            RecruitmentChannel.objects.filter(discord_user_id=111).count(), 2
        )


class RecruitmentSettingsConstraintTests(TestCase):
    """IntegrityError marks the transaction broken, so each failing save is
    wrapped in its own atomic block or the next query raises
    TransactionManagementError instead of what the test is about.

    HRAppDiscordSettings' post_save signal (hrapps.signals.announce_update)
    publishes to Redis unconditionally. get_solo() itself saves (via
    get_or_create) the first time it runs in a given test -- here, inside
    _settings(), before any of the explicit instance.save() calls below --
    so the patch is started for the whole test in setUp rather than wrapped
    around individual saves. That way no call path to get_redis_client can
    slip through unpatched."""

    def setUp(self):
        patcher = patch("hrapps.signals.get_redis_client")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _settings(self, **kwargs):
        instance = HRAppDiscordSettings.get_solo()
        for field, value in kwargs.items():
            setattr(instance, field, value)
        return instance

    def test_threads_mode_requires_a_thread_channel(self):
        instance = self._settings(
            recruitment_mode=RecruitmentMode.THREADS, recruitment_thread_channel=None
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            instance.save()

    def test_channels_mode_requires_categories_and_recruiter_role(self):
        instance = self._settings(
            recruitment_mode=RecruitmentMode.CHANNELS,
            recruitment_category=None,
            recruitment_archive_category=None,
            recruiter_role=None,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            instance.save()

    def test_channels_mode_requires_the_recruiter_role_specifically(self):
        """A null recruiter_role would produce a channel only the applicant can
        see -- a silent privacy inversion, not a missing mention."""
        instance = self._settings(
            recruitment_mode=RecruitmentMode.CHANNELS,
            recruitment_category=1,
            recruitment_archive_category=2,
            recruiter_role=None,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            instance.save()

    def test_channels_mode_saves_when_fully_configured(self):
        instance = self._settings(
            recruitment_mode=RecruitmentMode.CHANNELS,
            recruitment_category=1,
            recruitment_archive_category=2,
            recruiter_role=3,
        )
        instance.save()
        self.assertEqual(
            HRAppDiscordSettings.get_solo().recruitment_mode, RecruitmentMode.CHANNELS
        )

    def test_off_mode_needs_nothing(self):
        instance = self._settings(
            recruitment_mode=RecruitmentMode.OFF,
            recruitment_thread_channel=None,
            recruitment_category=None,
        )
        instance.save()
