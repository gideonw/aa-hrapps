"""sync_to_async for the bot's database access.

The web process gets close_old_connections for free from Django's
request_started and request_finished signals. The Discord bot is one
long-lived process that fires neither, so nothing ever retires its connection.
Once the MySQL server drops that connection for idling (wait_timeout), every
query fails with OperationalError 2006 "Server has gone away", and keeps
failing, because Django only replaces an errored connection from inside
close_old_connections. The cog stays up but goes deaf until the bot restarts.

db_sync_to_async runs close_old_connections on both sides of the call, in the
thread that owns the connection, the same way channels' database_sync_to_async
does. With the default CONN_MAX_AGE of 0 that means each call opens and closes
its own connection, as a web request would.
"""

import functools

from asgiref.sync import sync_to_async
from django.db import close_old_connections


def db_sync_to_async(func):
    @functools.wraps(func)
    def inner(*args, **kwargs):
        close_old_connections()
        try:
            return func(*args, **kwargs)
        finally:
            close_old_connections()

    return sync_to_async(inner)
