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


async def ensure_recruitment_channel(guild, member, settings):
    """Point `member` at their recruitment channel, creating it if needed.

    Returns the channel, or None for an operator-misconfiguration failure
    (already logged). A registry-write failure during creation is not caught
    here -- it propagates so the caller sees it rather than silently losing
    the channel.
    """
    channel_id = await sync_to_async(get_open_channel_id)(member.id)
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if channel is not None:
            await channel.send(
                f"{member.mention} here is your existing recruitment channel!"
            )
            return channel
        # The channel was deleted outside the app. Retire the stale row or the
        # unique index on open_for_user rejects every future attempt by this
        # member, wedging them permanently with no route out but a DBA.
        logger.info(
            f"Recruitment channel {channel_id} for {member.name} is gone; "
            f"retiring the stale registry row."
        )
        await sync_to_async(mark_archived)(channel_id)

    return await _create_recruitment_channel(guild, member, settings)


async def _create_recruitment_channel(guild, member, settings):
    category = guild.get_channel(settings.recruitment_category)
    if not isinstance(category, discord.CategoryChannel):
        logger.error(
            f"recruitment_category {settings.recruitment_category} is missing or is "
            f"not a category; cannot create a channel for {member.name}."
        )
        return None

    recruiter_role = guild.get_role(settings.recruiter_role)
    if recruiter_role is None:
        logger.error(
            f"recruiter_role {settings.recruiter_role} not found in {guild.name}; "
            f"refusing to create a channel that only {member.name} could see."
        )
        return None

    # The @everyone deny is the entire privacy mechanism. The explicit guild.me
    # entry keeps the bot able to edit the channel later even though the category
    # hides it by default.
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        recruiter_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }

    try:
        channel = await category.create_text_channel(
            name=f"recruit-{member.name}", overwrites=overwrites
        )
    except discord.Forbidden:
        logger.error(
            f"Missing Manage Channels or Manage Roles in category "
            f"{settings.recruitment_category}; cannot create a channel for {member.name}."
        )
        return None
    except discord.HTTPException as e:
        logger.error(
            f"Discord refused a channel for {member.name} in category "
            f"{settings.recruitment_category}: {e}. A category holds at most 50 "
            f"channels -- check whether it is full."
        )
        return None

    try:
        await sync_to_async(record_channel)(member.id, channel.id)
    except Exception:
        # An unrecorded channel is invisible to every later lookup: it would leak
        # and the member would be handed a second one next time. Deleting it is
        # the only ordering that leaves Discord and the registry agreeing.
        logger.exception(
            f"Failed to record channel {channel.id} for {member.name}; deleting it."
        )
        await channel.delete(reason="hrapps: registry write failed")
        raise

    await channel.send(
        f"*ATTN: {recruiter_role.mention}*\n\n"
        f"{member.mention} has indicated they are interested in joining."
    )
    return channel


async def archive_recruitment_channel(guild, app_pk, settings):
    """Drop the applicant's access and move their channel to the archive.

    Called on the "application status changed" message, which also fires for
    transitions that are not closures; closed_application_discord_id returns
    None for those, so this is a no-op unless the application really closed.
    """
    discord_user_id = await sync_to_async(closed_application_discord_id)(app_pk)
    if discord_user_id is None:
        return

    channel_id = await sync_to_async(get_open_channel_id)(discord_user_id)
    if channel_id is None:
        # They never ran /recruit_me, or it was archived already.
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        # Deleted by hand. Nothing to move, but retire the row so the member is
        # not blocked if they ever apply again.
        await sync_to_async(mark_archived)(channel_id)
        return

    archive = guild.get_channel(settings.recruitment_archive_category)
    if not isinstance(archive, discord.CategoryChannel):
        logger.error(
            f"recruitment_archive_category {settings.recruitment_archive_category} is "
            f"missing or is not a category; leaving channel {channel_id} in place."
        )
        return

    member = guild.get_member(discord_user_id)
    if member is not None:
        try:
            await channel.set_permissions(member, overwrite=None)
        except discord.Forbidden:
            # Nothing has changed yet: the applicant still has access and the
            # row stays open, so an operator can fix permissions and the next
            # status change will retry this from scratch.
            logger.error(
                f"Missing permissions to remove access from channel {channel_id}; "
                f"leaving it in place."
            )
            return

    try:
        # sync_permissions=False is load-bearing: syncing would replace this
        # channel's overwrites with the archive category's, undoing the
        # access removal above and potentially re-exposing the conversation
        # just closed.
        await channel.edit(category=archive, sync_permissions=False)
    except discord.Forbidden:
        # The applicant's overwrite is already gone, so this channel is no
        # longer theirs regardless of whether the category move succeeded.
        # Stamp it anyway: not stamping here would wedge the applicant the
        # same way an unstamped row wedges channel creation -- they would be
        # handed back a channel they can no longer see on their next
        # application.
        logger.error(
            f"Applicant access to channel {channel_id} was removed, but missing "
            f"permissions to move it to the archive category; file it by hand."
        )

    await sync_to_async(mark_archived)(channel_id)
    await channel.send("This application is closed. This channel has been archived.")
