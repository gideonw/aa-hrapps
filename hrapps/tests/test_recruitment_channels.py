from django.test import TestCase
from django.utils import timezone

from hrapps import recruitment_channels as rc
from hrapps.models import RecruitmentChannel


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
