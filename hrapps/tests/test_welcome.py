import importlib
from unittest.mock import AsyncMock, MagicMock

from django.test import SimpleTestCase

from hrapps.models import RecruitmentMode

# The module name is not a valid Python identifier, so it cannot be imported
# with `from ... import ...`.
cog = importlib.import_module("hrapps.cogs.aa-hrapps")


def _settings(mode=RecruitmentMode.CHANNELS, include_button=True, welcome_enabled=True):
    return MagicMock(
        recruitment_mode=mode,
        welcome_include_recruit_button=include_button,
        enable_welcome_messages=welcome_enabled,
    )


class WelcomeRecruitButtonTests(SimpleTestCase):
    """The button decision is a free function so it can be tested without
    constructing a member-join event: the cog's __init__ opens Redis and
    spawns a task, and on_member_join needs a full member/guild graph."""

    def test_shown_in_channels_mode_when_toggle_on(self):
        self.assertIs(
            cog.welcome_recruit_button_enabled(
                _settings(RecruitmentMode.CHANNELS, include_button=True)
            ),
            True,
        )

    def test_shown_in_threads_mode_when_toggle_on(self):
        self.assertIs(
            cog.welcome_recruit_button_enabled(
                _settings(RecruitmentMode.THREADS, include_button=True)
            ),
            True,
        )

    def test_hidden_when_toggle_off(self):
        self.assertIs(
            cog.welcome_recruit_button_enabled(
                _settings(RecruitmentMode.CHANNELS, include_button=False)
            ),
            False,
        )

    def test_hidden_in_off_mode_even_when_toggle_on(self):
        """In off mode the button can only reply "Recruitment is not currently
        open", so the mode wins over the toggle."""
        self.assertIs(
            cog.welcome_recruit_button_enabled(
                _settings(RecruitmentMode.OFF, include_button=True)
            ),
            False,
        )


class ShouldSendWelcomeTests(SimpleTestCase):
    """enable_welcome_messages was saved, constrained and rendered but never
    read by the cog, so the admin checkbox did nothing. These pin the guard."""

    def test_welcome_sent_when_enabled(self):
        self.assertIs(
            cog.should_send_welcome(_settings(welcome_enabled=True)), True
        )

    def test_welcome_suppressed_when_disabled(self):
        self.assertIs(
            cog.should_send_welcome(_settings(welcome_enabled=False)), False
        )

class OnMemberJoinWiringTests(SimpleTestCase):
    """The free functions above are only useful if on_member_join actually
    consults them, and a toggle that is read but ignored is the exact bug this
    change fixes -- so these call the real handler.

    HRApps.on_member_join is reached as an unbound function with a mock self:
    py-cord's Cog.listener() only tags the function, it does not wrap it, and
    constructing a real cog would open Redis and spawn a task.

    _is_ignored_state.return_value is forced to False deliberately. Left as a
    bare MagicMock it is truthy, so the handler would return at the
    ignored-state check and these tests would pass whether or not the welcome
    guard exists -- teeth, not coverage theatre.
    """

    def _instance(self, settings):
        instance = MagicMock()
        instance.settings = settings
        instance._is_ignored_state.return_value = False
        channel = MagicMock()
        channel.send = AsyncMock()
        instance.bot.get_channel.return_value = channel
        return instance, channel

    async def test_disabled_welcome_sends_nothing(self):
        settings = _settings(welcome_enabled=False)
        settings.welcome_message = "hi {user_mention}"
        instance, channel = self._instance(settings)
        await cog.HRApps.on_member_join(instance, MagicMock())
        channel.send.assert_not_awaited()

    async def test_enabled_welcome_sends_the_message(self):
        settings = _settings(welcome_enabled=True)
        settings.welcome_message = "hi {user_mention}"
        instance, channel = self._instance(settings)
        await cog.HRApps.on_member_join(instance, MagicMock())
        channel.send.assert_awaited_once()

    async def test_button_omitted_when_toggle_off(self):
        settings = _settings(include_button=False)
        settings.welcome_message = "hi"
        instance, channel = self._instance(settings)
        await cog.HRApps.on_member_join(instance, MagicMock())
        self.assertIsNone(channel.send.call_args.kwargs["view"])

    async def test_button_attached_when_toggle_on(self):
        settings = _settings(include_button=True)
        settings.welcome_message = "hi"
        instance, channel = self._instance(settings)
        await cog.HRApps.on_member_join(instance, MagicMock())
        self.assertIsNotNone(channel.send.call_args.kwargs["view"])
