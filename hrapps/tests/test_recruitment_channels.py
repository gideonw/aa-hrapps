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


from unittest.mock import AsyncMock, MagicMock, patch

import discord
from django.test import SimpleTestCase


def _guild_and_category():
    guild = MagicMock()
    guild.default_role = MagicMock()
    guild.me = MagicMock()
    guild.get_role.return_value = MagicMock()

    channel = MagicMock()
    channel.id = 999
    channel.send = AsyncMock()
    channel.delete = AsyncMock()

    # spec= makes isinstance(category, discord.CategoryChannel) true, which the
    # implementation checks before using it.
    category = MagicMock(spec=discord.CategoryChannel)
    category.create_text_channel = AsyncMock(return_value=channel)

    guild.get_channel.return_value = category
    return guild, category, channel


def _member(user_id=111, username="testuser"):
    member = MagicMock()
    member.id = user_id
    # `name` is assigned after construction on purpose: MagicMock(name="x") sets
    # the mock's own repr name, not an attribute, so .name would not be a str.
    member.name = username
    member.mention = f"<@{user_id}>"
    return member


def _channels_settings():
    return MagicMock(recruitment_category=1, recruiter_role=2, recruitment_archive_category=3)


class CreateChannelTests(SimpleTestCase):
    async def test_everyone_is_denied_view_channel(self):
        """Review Focus 1. If this regresses, every applicant conversation is
        world-readable and nothing in the product says so."""
        guild, category, _ = _guild_and_category()
        with patch.object(rc, "record_channel"):
            await rc._create_recruitment_channel(guild, _member(), _channels_settings())
        overwrites = category.create_text_channel.call_args.kwargs["overwrites"]
        self.assertIs(overwrites[guild.default_role].view_channel, False)

    async def test_recruiter_role_can_view_the_channel(self):
        guild, category, _ = _guild_and_category()
        role = guild.get_role.return_value
        with patch.object(rc, "record_channel"):
            await rc._create_recruitment_channel(guild, _member(), _channels_settings())
        overwrites = category.create_text_channel.call_args.kwargs["overwrites"]
        self.assertIs(overwrites[role].view_channel, True)

    async def test_applicant_can_view_the_channel(self):
        guild, category, _ = _guild_and_category()
        member = _member()
        with patch.object(rc, "record_channel"):
            await rc._create_recruitment_channel(guild, member, _channels_settings())
        overwrites = category.create_text_channel.call_args.kwargs["overwrites"]
        self.assertIs(overwrites[member].view_channel, True)

    async def test_failed_registry_write_deletes_the_channel(self):
        """An unrecorded channel is invisible to every later lookup, so it would
        leak and hand the member a second one next time."""
        guild, _, channel = _guild_and_category()
        with patch.object(rc, "record_channel", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await rc._create_recruitment_channel(guild, _member(), _channels_settings())
        channel.delete.assert_awaited()

    async def test_forbidden_returns_none_rather_than_raising(self):
        guild, category, _ = _guild_and_category()
        category.create_text_channel.side_effect = discord.Forbidden(MagicMock(), "nope")
        result = await rc._create_recruitment_channel(guild, _member(), _channels_settings())
        self.assertIsNone(result)

    async def test_full_category_returns_none_rather_than_raising(self):
        guild, category, _ = _guild_and_category()
        category.create_text_channel.side_effect = discord.HTTPException(MagicMock(), "full")
        result = await rc._create_recruitment_channel(guild, _member(), _channels_settings())
        self.assertIsNone(result)

    async def test_missing_recruiter_role_creates_nothing(self):
        """Creating it anyway would make a channel only the applicant can see."""
        guild, category, _ = _guild_and_category()
        guild.get_role.return_value = None
        result = await rc._create_recruitment_channel(guild, _member(), _channels_settings())
        self.assertIsNone(result)
        category.create_text_channel.assert_not_awaited()

    async def test_non_category_id_creates_nothing(self):
        guild, _, _ = _guild_and_category()
        guild.get_channel.return_value = MagicMock()  # not a CategoryChannel
        result = await rc._create_recruitment_channel(guild, _member(), _channels_settings())
        self.assertIsNone(result)


class EnsureChannelTests(SimpleTestCase):
    async def test_existing_channel_is_reused(self):
        guild, _, _ = _guild_and_category()
        existing = MagicMock()
        existing.send = AsyncMock()
        guild.get_channel.return_value = existing
        with patch.object(rc, "get_open_channel_id", return_value=555):
            result = await rc.ensure_recruitment_channel(guild, _member(), _channels_settings())
        self.assertIs(result, existing)
        existing.send.assert_awaited()

    async def test_stale_row_is_retired_and_a_new_channel_created(self):
        """Review Focus 2. Without retiring the row, the unique index on
        open_for_user blocks this member from ever starting again."""
        guild, category, channel = _guild_and_category()
        # First lookup is the missing channel; second is the category.
        guild.get_channel.side_effect = [None, category]
        with patch.object(rc, "get_open_channel_id", return_value=555), \
             patch.object(rc, "mark_archived") as marked, \
             patch.object(rc, "record_channel"):
            result = await rc.ensure_recruitment_channel(guild, _member(), _channels_settings())
        marked.assert_called_once_with(555)
        self.assertIs(result, channel)

    async def test_no_existing_row_creates_a_channel(self):
        guild, category, channel = _guild_and_category()
        with patch.object(rc, "get_open_channel_id", return_value=None), \
             patch.object(rc, "record_channel"):
            result = await rc.ensure_recruitment_channel(guild, _member(), _channels_settings())
        self.assertIs(result, channel)
        category.create_text_channel.assert_awaited_once()
        channel.send.assert_awaited_once()


def _archive_guild():
    guild, _, _ = _guild_and_category()
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.edit = AsyncMock()
    channel.set_permissions = AsyncMock()

    archive = MagicMock(spec=discord.CategoryChannel)
    member = _member()

    # First lookup is the channel being archived; second is the archive category.
    guild.get_channel.side_effect = [channel, archive]
    guild.get_member.return_value = member
    return guild, channel, archive, member


class ArchiveChannelTests(SimpleTestCase):
    async def test_non_closed_application_archives_nothing(self):
        """Review Focus 3. The status-changed message also fires for
        pending to under_review."""
        guild = MagicMock()
        with patch.object(rc, "closed_application_discord_id", return_value=None), \
             patch.object(rc, "mark_archived") as marked:
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        marked.assert_not_called()

    async def test_applicant_without_a_channel_is_a_noop(self):
        guild = MagicMock()
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=None), \
             patch.object(rc, "mark_archived") as marked:
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        marked.assert_not_called()

    async def test_archive_moves_channel_without_syncing_permissions(self):
        """Review Focus 5. Syncing would replace the channel's overwrites with
        the archive category's and could re-expose the closed conversation."""
        guild, channel, archive, _ = _archive_guild()
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=222), \
             patch.object(rc, "mark_archived"):
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        channel.edit.assert_awaited_once()
        self.assertIs(channel.edit.call_args.kwargs["sync_permissions"], False)
        self.assertIs(channel.edit.call_args.kwargs["category"], archive)

    async def test_applicant_overwrite_is_removed(self):
        guild, channel, _, member = _archive_guild()
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=222), \
             patch.object(rc, "mark_archived"):
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        channel.set_permissions.assert_awaited_once_with(member, overwrite=None)

    async def test_departed_member_still_gets_the_channel_moved(self):
        guild, channel, _, _ = _archive_guild()
        guild.get_member.return_value = None
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=222), \
             patch.object(rc, "mark_archived"):
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        channel.set_permissions.assert_not_awaited()
        channel.edit.assert_awaited_once()

    async def test_row_is_retired_when_the_channel_is_already_gone(self):
        guild = MagicMock()
        guild.get_channel.return_value = None
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=222), \
             patch.object(rc, "mark_archived") as marked:
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        marked.assert_called_once_with(222)

    async def test_missing_archive_category_leaves_the_channel_in_place(self):
        guild, channel, _, _ = _archive_guild()
        guild.get_channel.side_effect = [channel, MagicMock()]  # not a category
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=222), \
             patch.object(rc, "mark_archived") as marked:
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        channel.edit.assert_not_awaited()
        marked.assert_not_called()

    async def test_forbidden_removing_access_leaves_the_channel_untouched(self):
        """Nothing has changed yet, so the row must stay open: an operator can
        fix permissions and the next status change retries from scratch."""
        guild, channel, _, _ = _archive_guild()
        channel.set_permissions.side_effect = discord.Forbidden(MagicMock(), "nope")
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=222), \
             patch.object(rc, "mark_archived") as marked:
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        channel.edit.assert_not_awaited()
        marked.assert_not_called()

    async def test_forbidden_moving_channel_still_stamps_the_row(self):
        """Regression guard for the wedge: the applicant's overwrite is
        already gone, so the row must still be stamped or they come back to a
        channel they can no longer see."""
        guild, channel, _, _ = _archive_guild()
        channel.edit.side_effect = discord.Forbidden(MagicMock(), "nope")
        with patch.object(rc, "closed_application_discord_id", return_value=111), \
             patch.object(rc, "get_open_channel_id", return_value=222), \
             patch.object(rc, "mark_archived") as marked:
            await rc.archive_recruitment_channel(guild, 1, _channels_settings())
        marked.assert_called_once_with(222)
