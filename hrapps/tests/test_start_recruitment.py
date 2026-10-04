import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase

from hrapps.models import RecruitmentMode

# The module name is not a valid Python identifier, so it cannot be imported
# with `from ... import ...`.
cog = importlib.import_module("hrapps.cogs.aa-hrapps")


class StartRecruitmentModeDispatchTests(SimpleTestCase):
    """Covers start_recruitment's three-way dispatch on recruitment_mode.

    Patches are applied to the names as bound in the cog module, not in
    hrapps.recruitment_channels, since that is what start_recruitment
    actually calls.
    """

    async def test_channels_mode_calls_ensure_and_not_thread_path(self):
        settings = MagicMock(recruitment_mode=RecruitmentMode.CHANNELS)
        guild = MagicMock()
        member = MagicMock()
        member.name = "applicant"
        fake_channel = MagicMock(name="fake_channel")

        with patch.object(
            cog, "ensure_recruitment_channel", new=AsyncMock(return_value=fake_channel)
        ) as ensure_mock, patch.object(
            cog, "create_recruitment_thread", new=AsyncMock()
        ) as thread_mock:
            result = await cog.start_recruitment(guild, member, settings)

        ensure_mock.assert_awaited_once_with(guild, member, settings)
        thread_mock.assert_not_awaited()
        self.assertIs(result, True)

    async def test_channels_mode_returns_false_when_ensure_returns_none(self):
        settings = MagicMock(recruitment_mode=RecruitmentMode.CHANNELS)
        guild = MagicMock()
        member = MagicMock()
        member.name = "applicant"

        with patch.object(
            cog, "ensure_recruitment_channel", new=AsyncMock(return_value=None)
        ):
            result = await cog.start_recruitment(guild, member, settings)

        self.assertIs(result, False)

    async def test_channels_mode_returns_false_when_ensure_raises(self):
        settings = MagicMock(recruitment_mode=RecruitmentMode.CHANNELS)
        guild = MagicMock()
        member = MagicMock()
        member.name = "applicant"

        with patch.object(
            cog,
            "ensure_recruitment_channel",
            new=AsyncMock(side_effect=RuntimeError("registry write failed")),
        ):
            result = await cog.start_recruitment(guild, member, settings)

        self.assertIs(result, False)

    async def test_threads_mode_calls_thread_path_and_not_ensure(self):
        settings = MagicMock(
            recruitment_mode=RecruitmentMode.THREADS,
            recruitment_thread_channel=555,
            recruiter_role=777,
        )
        guild = MagicMock()
        member = MagicMock()
        member.name = "applicant"

        with patch.object(
            cog, "check_active_threads", new=AsyncMock(return_value=None)
        ) as check_mock, patch.object(
            cog, "create_recruitment_thread", new=AsyncMock()
        ) as thread_mock, patch.object(
            cog, "ensure_recruitment_channel", new=AsyncMock()
        ) as ensure_mock:
            result = await cog.start_recruitment(guild, member, settings)

        check_mock.assert_awaited_once_with(member, guild, 555)
        thread_mock.assert_awaited_once_with(member, guild, 555, 777)
        ensure_mock.assert_not_awaited()
        self.assertIs(result, True)

    async def test_off_mode_calls_neither_path_and_returns_false(self):
        settings = MagicMock(recruitment_mode=RecruitmentMode.OFF)
        guild = MagicMock()
        member = MagicMock()
        member.name = "applicant"

        with patch.object(
            cog, "ensure_recruitment_channel", new=AsyncMock()
        ) as ensure_mock, patch.object(
            cog, "check_active_threads", new=AsyncMock()
        ) as check_mock, patch.object(
            cog, "create_recruitment_thread", new=AsyncMock()
        ) as thread_mock:
            result = await cog.start_recruitment(guild, member, settings)

        ensure_mock.assert_not_awaited()
        check_mock.assert_not_awaited()
        thread_mock.assert_not_awaited()
        self.assertIs(result, False)
