"""Covers db_sync_to_async, the cog's only route to the database.

The bot is a long-lived process that never fires request_started or
request_finished, so nothing else ever runs close_old_connections in it. A
connection the MySQL server drops for idling (wait_timeout) then stays in place
and every later query fails with 2006 "Server has gone away" until restart.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, call, patch

from django.db import OperationalError
from django.test import SimpleTestCase

from hrapps import async_db

cog = importlib.import_module("hrapps.cogs.aa-hrapps")


class DbSyncToAsyncTests(SimpleTestCase):
    async def test_closes_old_connections_before_and_after(self):
        events = []

        def work(value):
            events.append(("work", value))
            return value * 2

        with patch.object(
            async_db, "close_old_connections",
            side_effect=lambda: events.append("close"),
        ):
            result = await async_db.db_sync_to_async(work)(21)

        self.assertEqual(result, 42)
        self.assertEqual(events, ["close", ("work", 21), "close"])

    async def test_closes_after_even_when_the_query_raises(self):
        def work():
            raise OperationalError(2006, "Server has gone away")

        with patch.object(async_db, "close_old_connections") as close:
            with self.assertRaises(OperationalError):
                await async_db.db_sync_to_async(work)()

        self.assertEqual(close.call_args_list, [call(), call()])


class UpdateSettingsTests(SimpleTestCase):
    """update_settings is the call in the reported traceback."""

    async def test_update_settings_refreshes_the_connection(self):
        instance = cog.HRApps.__new__(cog.HRApps)
        instance._reconcile_subscriptions = AsyncMock()
        events = []

        with patch.object(
            async_db, "close_old_connections",
            side_effect=lambda: events.append("close"),
        ), patch.object(
            cog.HRAppDiscordSettings, "get_solo",
            side_effect=lambda: events.append("get_solo") or MagicMock(),
        ):
            await instance.update_settings()

        self.assertEqual(events, ["close", "get_solo", "close"])
