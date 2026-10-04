"""Covers listen_to_mq's per-message dispatch.

No cog is constructed: HRApps.__init__ opens a Redis connection and spawns a
task, so the instance is built with __new__ and handed a fake pubsub. Nothing
here touches Redis or the database.
"""

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase

from hrapps.models import RecruitmentMode

# The module name is not a valid Python identifier, so it cannot be imported
# with `from ... import ...`.
cog = importlib.import_module("hrapps.cogs.aa-hrapps")


def _pubsub(*actions):
    """A pubsub whose listen() yields one message per action, then stops.

    The loop ending naturally is what lets listen_to_mq return, so each test is
    a single await with no cancellation dance.
    """
    messages = [
        {"type": "message", "data": json.dumps(action)} for action in actions
    ]

    pubsub = MagicMock()
    # _reconcile_subscriptions reads this as a dict and awaits the two calls.
    pubsub.channels = {}
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()

    async def listen():
        for message in messages:
            yield message

    pubsub.listen = listen
    return pubsub


def _cog(settings, pubsub):
    instance = cog.HRApps.__new__(cog.HRApps)
    instance.settings = settings
    instance.bot = MagicMock()
    instance.pubsub = pubsub
    instance.send_new_app_notification = AsyncMock()
    instance.send_new_comment_notification = AsyncMock()
    instance.send_claim_notification = AsyncMock()
    instance.send_status_notification = AsyncMock()
    instance.update_settings = AsyncMock()
    return instance


def _channels_notifications_off():
    return MagicMock(
        enable_application_notifications=False,
        recruitment_mode=RecruitmentMode.CHANNELS,
    )


class NotificationGuardTests(SimpleTestCase):
    """Only "application status changed" was guarded on the notification flag.

    Channels mode subscribes to hrapp_application_notifications even with
    notifications off, because archiving rides that channel. In that
    configuration application_notification_channel is NULL,
    bot.get_channel(None) returns None and channel.send raises AttributeError --
    and since the outer handler sits outside the `async for`, the first
    application submitted ended the subscription task for good: no settings
    reloads, no notifications, and no archiving of any recruitment channel until
    the bot restarted.
    """

    async def test_new_application_sends_nothing_with_notifications_off(self):
        instance = _cog(
            _channels_notifications_off(), _pubsub({"action": "new application", "app_pk": 1})
        )
        await instance.listen_to_mq()
        instance.send_new_app_notification.assert_not_awaited()

    async def test_new_comment_sends_nothing_with_notifications_off(self):
        instance = _cog(
            _channels_notifications_off(),
            _pubsub({"action": "new comment", "comment_pk": 1}),
        )
        await instance.listen_to_mq()
        instance.send_new_comment_notification.assert_not_awaited()

    async def test_application_claimed_sends_nothing_with_notifications_off(self):
        instance = _cog(
            _channels_notifications_off(),
            _pubsub({"action": "application claimed", "app_pk": 1}),
        )
        await instance.listen_to_mq()
        instance.send_claim_notification.assert_not_awaited()

    async def test_notifications_on_still_sends_each_one(self):
        """Pins the guards as guards, not as a removal of the feature."""
        settings = MagicMock(
            enable_application_notifications=True,
            recruitment_mode=RecruitmentMode.OFF,
        )
        instance = _cog(
            settings,
            _pubsub(
                {"action": "new application", "app_pk": 1},
                {"action": "new comment", "comment_pk": 2},
                {"action": "application claimed", "app_pk": 3},
                {"action": "application status changed", "app_pk": 4, "old": "pending"},
            ),
        )
        await instance.listen_to_mq()
        instance.send_new_app_notification.assert_awaited_once_with(1)
        instance.send_new_comment_notification.assert_awaited_once_with(2)
        instance.send_claim_notification.assert_awaited_once_with(3, True)
        instance.send_status_notification.assert_awaited_once_with(4, "pending")

    async def test_archiving_runs_with_notifications_off(self):
        """The archive dispatch must stay ungated by the notification flag:
        channels mode subscribes precisely so this can run with them off."""
        instance = _cog(
            _channels_notifications_off(),
            _pubsub({"action": "application status changed", "app_pk": 7, "old": "pending"}),
        )
        with patch.object(
            cog, "archive_recruitment_channel", new=AsyncMock()
        ) as archive_mock:
            await instance.listen_to_mq()
        instance.send_status_notification.assert_not_awaited()
        archive_mock.assert_awaited_once_with(instance.bot, 7, instance.settings)


class PerMessageIsolationTests(SimpleTestCase):
    """The outer except sits outside the `async for`, so one raising handler
    used to end the subscription. FormResponse.objects.get raising DoesNotExist
    for an application deleted between publish and receipt is enough to do it.
    """

    async def test_one_failing_message_does_not_stop_the_bus(self):
        settings = MagicMock(
            enable_application_notifications=True,
            recruitment_mode=RecruitmentMode.OFF,
        )
        instance = _cog(
            settings,
            _pubsub(
                {"action": "new application", "app_pk": 1},
                {"action": "new comment", "comment_pk": 2},
            ),
        )
        instance.send_new_app_notification.side_effect = RuntimeError("app deleted")

        await instance.listen_to_mq()

        instance.send_new_app_notification.assert_awaited_once()
        instance.send_new_comment_notification.assert_awaited_once_with(2)

    async def test_undecodable_payload_does_not_stop_the_bus(self):
        settings = MagicMock(
            enable_application_notifications=True,
            recruitment_mode=RecruitmentMode.OFF,
        )
        pubsub = _pubsub({"action": "new comment", "comment_pk": 5})
        good = pubsub.listen

        async def listen():
            yield {"type": "message", "data": "not json"}
            async for message in good():
                yield message

        pubsub.listen = listen
        instance = _cog(settings, pubsub)

        await instance.listen_to_mq()

        instance.send_new_comment_notification.assert_awaited_once_with(5)
