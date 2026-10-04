"""Private recruitment channels.

Channel operations live here rather than in the cog because the cog is already
the largest file in the package and would otherwise own Discord API calls, ORM
access and mode dispatch at once. The cog keeps only dispatch.

The sync/async split follows the convention the async-ORM patch established:
every function that touches the database is sync and is called through
sync_to_async, because each attribute hop on a Django model can be its own
query and none of them may run on the gateway's event loop.
"""

import discord
from asgiref.sync import sync_to_async
from django.utils import timezone

from allianceauth.services.hooks import get_extension_logger
from aadiscordbot.cogs.utils.exceptions import NotAuthenticated
from aadiscordbot.utils.auth import get_discord_user_id

from hrapps.models import FormResponse, RecruitmentChannel

logger = get_extension_logger(__name__)


# --- ORM helpers. Sync on purpose; always called through sync_to_async. ---

def get_open_channel_id(discord_user_id):
    """Channel ID of this user's open recruitment channel, or None."""
    row = RecruitmentChannel.objects.filter(
        discord_user_id=discord_user_id, archived__isnull=True
    ).first()
    return row.channel_id if row else None


def record_channel(discord_user_id, channel_id):
    RecruitmentChannel.objects.create(
        discord_user_id=discord_user_id, channel_id=channel_id
    )


def mark_archived(channel_id):
    """Stamp `archived`, which also clears `open_for_user` via save()."""
    row = RecruitmentChannel.objects.filter(channel_id=channel_id).first()
    if row is None:
        return
    row.archived = timezone.now()
    row.save()


def closed_application_discord_id(app_pk):
    """Applicant's Discord ID, but only if the application is now closed.

    Sync on purpose: loading the FormResponse, walking .user, and
    get_discord_user_id's own lookup are three separate queries.

    Returns None when the application is not closed -- the
    "application status changed" message also fires for pending to
    under_review, which must not archive anything -- or when the applicant has
    no linked Discord account.
    """
    application = FormResponse.objects.get(pk=app_pk)
    if not application.is_closed:
        return None
    try:
        return get_discord_user_id(application.user)
    except NotAuthenticated:
        logger.warning(
            f"Application {app_pk} has no linked Discord account; nothing to archive."
        )
        return None
