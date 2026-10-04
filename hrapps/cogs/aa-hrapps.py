import asyncio
import json
from collections import defaultdict

import discord.ui
import redis.asyncio as aioredis
from django.conf import settings
from allianceauth.utils.cache import get_redis_client
from aadiscordbot.utils.auth import get_auth_user
from allianceauth.services.hooks import get_extension_logger
from aadiscordbot.app_settings import get_all_servers, get_site_url
from aadiscordbot.cogs.utils.exceptions import NotAuthenticated
from asgiref.sync import sync_to_async
from discord.ext import commands
from hrapps.models import HRAppDiscordSettings, FormResponse, ResponseComment, RecruitmentMode
from hrapps.recruitment_channels import (
    archive_recruitment_channel,
    ensure_recruitment_channel,
)
from allianceauth.eveonline.evelinks import eveimageserver

logger = get_extension_logger(__name__)


async def add_recruit_role(member, guild, role_id):
    logger.debug(f"Adding recruit role to {member.name}")
    role = guild.get_role(role_id)
    await member.add_roles(role)


async def check_active_threads(member, guild, channel_id):
    logger.debug(f"Checking active threads for {member.name}")
    channel = guild.get_channel(channel_id)
    thread = discord.utils.find(lambda t: member.name in t.name, channel.threads)
    if thread:
        return thread.id
    return None


async def create_recruitment_thread(member, guild, channel_id, recruiter_role_id):
    logger.debug(f"Creating recruitment thread for {member.name}")
    channel = guild.get_channel(channel_id)
    recruiter_role = guild.get_role(recruiter_role_id)
    thread = await channel.create_thread(
        name=f"Recruitment: {member.name}",
        type=discord.ChannelType.public_thread
    )
    logger.debug(f"Sending recruiter notification message in thread for {member.name}")
    await thread.send(f"*ATTN: {recruiter_role.mention}*\n\n{member.mention} has indicated they are interested in joining.")


async def start_recruitment(guild, member, settings):
    """Open or re-point the member's recruitment space for the configured mode.

    Returns True when the member has somewhere to talk, False when channel
    creation failed and the caller should say so rather than imply success.
    """
    if settings.recruitment_mode == RecruitmentMode.CHANNELS:
        try:
            channel = await ensure_recruitment_channel(guild, member, settings)
        except Exception:
            logger.exception(
                f"Failed to open a recruitment channel for {member.name}."
            )
            return False
        return channel is not None

    if settings.recruitment_mode == RecruitmentMode.THREADS:
        existing_thread = await check_active_threads(
            member, guild, settings.recruitment_thread_channel
        )
        if existing_thread:
            channel = guild.get_channel(existing_thread)
            await channel.send(
                f"{member.mention} here is your existing recruitment thread!"
            )
            return True
        await create_recruitment_thread(
            member, guild, settings.recruitment_thread_channel, settings.recruiter_role
        )
        return True

    logger.debug("Recruitment is disabled; not opening a channel or thread.")
    return False


def wanted_subscriptions(settings):
    """Redis channels the cog needs given these settings.

    hrapp_application_notifications carries both the notification embeds and the
    "application status changed" message that archives a recruitment channel, so
    channels mode needs it even with notifications off.

    A free function rather than a method so it can be tested without building a
    cog, whose __init__ opens a Redis connection and spawns a task.
    """
    wanted = {"hrapp_discord_settings"}
    if settings.enable_application_notifications:
        wanted.add("hrapp_application_notifications")
        wanted.add("hrapp_comment_notifications")
    if settings.recruitment_mode == RecruitmentMode.CHANNELS:
        wanted.add("hrapp_application_notifications")
    return wanted


class RecruitButtonView(discord.ui.View):
    def __init__(self, bot, member=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.member = member

    @discord.ui.button(label="Recruit Me", style=discord.ButtonStyle.green, custom_id="hrapps_recruit_button")
    async def recruit_button(self, button, interaction):
        if self.member is None:
            # If the bot is restarted then existing views will lose the member attribute. In this case we don't have a
            # reliable way to know who the user is that the welcome message was targeting as the user mention may not
            # be the first one.
            command = self.bot.get_application_command("recruit_me")
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(
                f"Something went wrong processing your request. "
                f"Please use the </recruit_me:{command.id}> command instead.",
                ephemeral=True
            )
            return
        if interaction.user != self.member:
            await interaction.response.send_message("You can not make this decision for others.", ephemeral=True)
            return
        settings = await sync_to_async(HRAppDiscordSettings.get_solo)()

        await add_recruit_role(self.member, interaction.guild, settings.recruit_role)
        started = await start_recruitment(interaction.guild, self.member, settings)

        await interaction.response.edit_message(view=None)
        if not started:
            await interaction.followup.send(
                "Your recruitment channel could not be created. "
                "Please contact a recruiter.",
                ephemeral=True,
            )


    @discord.ui.button(label="No Thanks", custom_id="hrapps_cancel_button")
    async def cancel_button(self, button, interaction):
        if interaction.user != self.member:
            await interaction.response.send_message("You can not make this decision for others.", ephemeral=True)
            return
        command = self.bot.get_application_command("recruit_me")
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(
            f"If you change your mind later, simply run the </recruit_me:{command.id}> command.",
            ephemeral=True
        )


class HRApps(commands.Cog):
    """
    A cog to integrate with the HRApps app.
    """

    def __init__(self, bot):
        self.settings = HRAppDiscordSettings.get_solo()
        self.bot = bot

        redis_client = get_redis_client()
        rkwargs = redis_client.connection_pool.connection_kwargs

        askwargs = {
            "decode_responses": True,
            'host': rkwargs.get('host', 'localhost'),
            'port': rkwargs.get('port', 6379),
            'db': rkwargs.get('db', 0),
            'password': rkwargs.get('password', None),
        }

        self.redis_client = aioredis.Redis(**askwargs)
        self.pubsub = self.redis_client.pubsub()
        self.listener_task = self.bot.loop.create_task(self.listen_to_mq())
        logger.debug("Initialized HRApp cog.")

    async def _reconcile_subscriptions(self):
        """Subscribe and unsubscribe so the connection matches the settings.

        Called on startup and after every settings update. Without the second
        call, switching to channels mode in the admin UI appears to work and
        silently archives nothing until someone restarts the bot.

        Must only be called from inside the pubsub listen loop: redis-py's
        unsubscribe only moves a channel to pending_unsubscribe_channels, and it
        is popped from self.channels when listen() later reads the server's
        confirmation -- so off the loop, self.channels never converges and every
        reconcile re-issues the same unsubscribe.
        """
        wanted = wanted_subscriptions(self.settings)
        current = set(self.pubsub.channels.keys())
        for channel in wanted - current:
            await self.pubsub.subscribe(channel)
            logger.debug(f"Subscribed to {channel}")
        for channel in current - wanted:
            await self.pubsub.unsubscribe(channel)
            logger.debug(f"Unsubscribed from {channel}")

    async def listen_to_mq(self):
        await self._reconcile_subscriptions()
        logger.debug("Listening for HRApp settings updates.")

        try:
            async for message in self.pubsub.listen():
                if message["type"] != "message":
                    continue
                # Per-message isolation. The handlers below reach the database
                # and the Discord API, so any one of them can raise: an
                # application deleted between publish and receipt makes
                # FormResponse.objects.get raise DoesNotExist, for instance.
                # Without this try the exception leaves the `async for` -- the
                # outer handler sits outside the loop -- and the subscription
                # task ends for good: no settings reloads, no notifications and
                # no archiving until the bot restarts. asyncio.CancelledError is
                # a BaseException, so `except Exception` already lets
                # cancellation propagate to the outer handler.
                try:
                    logger.debug("Received redis message")
                    data = json.loads(message["data"])
                    action = data.get("action")
                    logger.debug(f"Action: {action}")

                    if action == "settings_updated":
                        logger.debug("HRApp settings updated, updating local settings.")
                        await self.update_settings()
                    # Every notification below is gated on the notification
                    # flag. Channels mode subscribes to
                    # hrapp_application_notifications even with notifications
                    # off, because archiving rides that channel; in that
                    # configuration application_notification_channel is NULL,
                    # bot.get_channel(None) returns None and channel.send raises
                    # AttributeError. Ungated, the first application submitted
                    # killed the listener and with it all archiving.
                    if action == "new application":
                        logger.debug("New HRApp application detected, sending notification.")
                        if self.settings.enable_application_notifications:
                            await self.send_new_app_notification(data.get("app_pk"))
                    if action == "new comment":
                        logger.debug("New HRApp comment detected, sending notification.")
                        if self.settings.enable_application_notifications:
                            await self.send_new_comment_notification(data.get("comment_pk"))
                    if action == "application claimed":
                        logger.debug("HRApp application claimed, sending notification.")
                        if self.settings.enable_application_notifications:
                            await self.send_claim_notification(data.get("app_pk"), data.get("recruiter", True))
                    if action == "application status changed":
                        logger.debug("HRApp application status changed, sending notification.")
                        if self.settings.enable_application_notifications:
                            await self.send_status_notification(
                                data.get("app_pk"), data.get("old")
                            )
                        # Deliberately not gated on the notification flag:
                        # archiving must run with notifications off.
                        if self.settings.recruitment_mode == RecruitmentMode.CHANNELS:
                            await archive_recruitment_channel(
                                self.bot, data.get("app_pk"), self.settings
                            )
                except Exception:
                    logger.exception(
                        "Error handling an HRApp redis message; still listening."
                    )
        except asyncio.CancelledError:
            logger.debug("Cancelled listening for HRApp settings updates.")
        except Exception as e:
            logger.error(f"Error listening for HRApp settings updates: {e}")
            logger.exception(e)

    async def send_new_app_notification(self, app_pk):
        channel = self.bot.get_channel(self.settings.application_notification_channel)
        embed = await sync_to_async(self._new_app_embed)(app_pk)
        await channel.send(embed=embed)

    def _new_app_embed(self, app_pk):
        """Build the embed. Sync on purpose: every attribute walked below
        (user.profile.main_character, form.corporation) is a lazy FK that
        queries, so the whole build has to run off the event loop."""
        application = FormResponse.objects.get(pk=app_pk)
        embed = discord.Embed(
            title="New Application Submitted",
            description=f"{application.user.profile.main_character.character_name} "
                        f"has applied to join {application.form.corporation.corporation_name}",
            colour=discord.Colour.green()
        )
        embed.set_author(name=application.user.profile.main_character.character_name,
                         icon_url=eveimageserver.character_portrait_url(application.user.profile.main_character.character_id))
        embed.set_thumbnail(url=eveimageserver.corporation_logo_url(application.form.corporation.corporation_id, 128))
        embed.set_footer(text=f"{self.bot.user.name} via AllianceAuth HRApps", icon_url=self.bot.user.display_avatar.url)
        embed.add_field(name=" ", value="⠀")
        embed.add_field(name="App Link", value=f"[Click Here]({settings.SITE_URL}/hradmin/resp/{app_pk})", inline=False)


        return embed
    async def send_new_comment_notification(self, comment_pk):
        channel = self.bot.get_channel(self.settings.application_notification_channel)
        embed = await sync_to_async(self._new_comment_embed)(comment_pk)
        await channel.send(embed=embed)

    def _new_comment_embed(self, comment_pk):
        """Build the embed. Sync on purpose: every attribute walked below
        (user.profile.main_character, form.corporation) is a lazy FK that
        queries, so the whole build has to run off the event loop."""
        comment = ResponseComment.objects.get(pk=comment_pk)
        application = comment.response
        embed = discord.Embed(
            title=f"New Comment",
            description=f"{comment.user.profile.main_character.character_name} commented on "
                        f"{application.user.profile.main_character.character_name}'s application.",
            colour=discord.Colour.blurple()
        )
        embed.set_author(name=comment.user.profile.main_character.character_name,
                         icon_url=eveimageserver.character_portrait_url(comment.user.profile.main_character.character_id))
        embed.set_footer(text=f"{self.bot.user.name} via AllianceAuth HRApps", icon_url=self.bot.user.display_avatar.url)
        embed.add_field(name="Private Comment?", value=f"{comment.private}", inline=False)
        embed.add_field(name="Comment", value=f"{comment.content}", inline=False)
        embed.add_field(name=" ", value="⠀", inline=False)

        embed.add_field(name="App Link", value=f"[Click Here]({settings.SITE_URL}/hradmin/resp/{application.pk})", inline=False)


        return embed
    async def send_claim_notification(self, app_pk, recruiter=True):
        channel = self.bot.get_channel(self.settings.application_notification_channel)
        embed = await sync_to_async(self._claim_embed)(app_pk, recruiter)
        await channel.send(embed=embed)

    def _claim_embed(self, app_pk, recruiter=True):
        """Build the embed. Sync on purpose: every attribute walked below
        (user.profile.main_character, form.corporation) is a lazy FK that
        queries, so the whole build has to run off the event loop."""
        application = FormResponse.objects.get(pk=app_pk)
        claimer = application.recruiter if recruiter else application.reviewer
        title = ""
        description = ""
        if claimer is None:
            title = f"{'Recruiter' if recruiter else 'Reviewer'} Claim Reset by Admin"
            description = (f"The {'recruiter' if recruiter else 'reviewer'} claim on "
                           f"{application.user.profile.main_character.character_name}'s application to join "
                           f"{application.form.corporation.corporation_name} has been cleared by a site administrator.")
        else:
            title = f"Application claimed by {'recruiter' if recruiter else 'reviewer'}"
            description = (f"{claimer.profile.main_character.character_name} claimed "
                        f"{application.user.profile.main_character.character_name}'s application to join "
                        f"{application.form.corporation.corporation_name} as the {'recruiter' if recruiter else 'reviewer'}.")
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Colour.orange()
        )
        embed.set_author(name=claimer.profile.main_character.character_name,
                         icon_url=eveimageserver.character_portrait_url(claimer.profile.main_character.character_id))
        embed.set_footer(text=f"{self.bot.user.name} vial AllianceAuth HRApps", icon_url=self.bot.user.display_avatar.url)
        embed.add_field(name=" ", value="⠀", inline=False)

        embed.add_field(name="App Link", value=f"[Click Here]({settings.SITE_URL}/hradmin/resp/{application.pk})",
                        inline=False)


        return embed
    async def send_status_notification(self, app_pk, old_status):
        channel = self.bot.get_channel(self.settings.application_notification_channel)
        embed = await sync_to_async(self._status_embed)(app_pk, old_status)
        await channel.send(embed=embed)

    def _status_embed(self, app_pk, old_status):
        """Build the embed. Sync on purpose: every attribute walked below
        (user.profile.main_character, form.corporation) is a lazy FK that
        queries, so the whole build has to run off the event loop."""
        application = FormResponse.objects.get(pk=app_pk)
        title = "Application Status Changed"
        description = (f"{application.user.profile.main_character.character_name}'s application to join "
                        f"{application.form.corporation.corporation_name} has been updated.")
        color = discord.Colour.orange()
        if application.status == "approved":
            title = "Application Approved"
            description = (f"{application.user.profile.main_character.character_name}'s application to join "
                        f"{application.form.corporation.corporation_name} has been approved.")
            color = discord.Colour.green()
        elif application.status == "rejected":
            title = "Application Rejected"
            description = (f"{application.user.profile.main_character.character_name}'s application to join "
                        f"{application.form.corporation.corporation_name} has been rejected.")
            color = discord.Colour.red()

        embed = discord.Embed(
            title=title,
            description=description,
            colour=color
        )
        embed.set_author(name=application.user.profile.main_character.character_name,
                         icon_url=eveimageserver.character_portrait_url(
                             application.user.profile.main_character.character_id)
                         )
        embed.set_footer(text=f"{self.bot.user.name} vial AllianceAuth HRApps",
                         icon_url=self.bot.user.display_avatar.url)
        if application.status not in ("approved", "rejected"):
            embed.add_field(name="New Status", value=f"{application.status}")
            embed.add_field(name="Old Status", value=f"{old_status}")
        embed.add_field(name=" ", value="⠀", inline=False)

        embed.add_field(name="App Link", value=f"[Click Here]({settings.SITE_URL}/hradmin/resp/{application.pk})",
                        inline=False)

        return embed
    async def update_settings(self):
        self.settings = await sync_to_async(HRAppDiscordSettings.get_solo)()
        await self._reconcile_subscriptions()

    def _is_ignored_state(self, discord_user, guild):
        """Resolve the auth user and test their state against ignored_states.

        Sync on purpose, and called through sync_to_async: get_auth_user queries,
        and `user.profile.state` plus `ignored_states.all()` are two more queries.
        Raises NotAuthenticated when the Discord user has no linked auth account,
        which both callers already handle.
        """
        user = get_auth_user(discord_user, guild)
        return user.profile.state in self.settings.ignored_states.all()

    @commands.Cog.listener()
    async def on_member_join(self, member):
        logger.debug(f"Member joined the server.")
        # Check if the user is part of an ignored state, if so we can return, no need to welcome.
        try:
            if await sync_to_async(self._is_ignored_state)(member._user, member.guild):
                logger.debug(f"User is in ignored state, no need to welcome.")
                return
        except NotAuthenticated:
            # User is not found, continue
            logger.debug(f"User not found, continuing.")
            pass
        except Exception as e:
            logger.error(f"Error checking if user is in ignored state: {e}")
            return

        # Welcome the user
        logger.debug(f"Welcoming user {member.name}")
        logger.debug(f"Recruitment mode: {self.settings.recruitment_mode}")
        welcome_channel = self.bot.get_channel(self.settings.welcome_channel)
        recruit_view = (
            RecruitButtonView(self.bot, member)
            if self.settings.recruitment_mode != RecruitmentMode.OFF
            else None
        )
        await welcome_channel.send(
            self.settings.welcome_message.format_map(
                defaultdict(
                    lambda:"",
                    user_mention=member.mention,
                    auth_url=get_site_url()
                )
            ),
            view=recruit_view
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only threads announce themselves with a system message to delete.
        if self.settings.recruitment_mode != RecruitmentMode.THREADS:
            return

        logger.debug(f"Message type: {message.type}")
        if message.type == discord.MessageType.thread_created and message.channel == message.guild.get_channel(self.settings.recruitment_thread_channel):
            logger.debug(f"Found recruitment thread, deleting creation message for privacy. {message.id}")
            await message.delete()
            return

    @commands.Cog.listener()
    async def on_ready(self):
        logger.debug("HRApp cog is ready.")
        self.bot.add_view(RecruitButtonView(self.bot))

    @commands.slash_command(name="recruit_me", description="Begin the recruitment process.", guild_ids=get_all_servers())
    async def recruit_me(self, ctx):
        try:
            if await sync_to_async(self._is_ignored_state)(ctx.author, ctx.guild):
                return await ctx.respond("You are not eligible for recruitment.", ephemeral=True)
        except NotAuthenticated:
            pass

        await add_recruit_role(ctx.author, ctx.guild, self.settings.recruit_role)
        started = await start_recruitment(ctx.guild, ctx.author, self.settings)
        if not started:
            return await ctx.respond(
                "Your recruitment channel could not be created. "
                "Please contact a recruiter.",
                ephemeral=True,
            )
        return await ctx.respond("Your recruitment channel is ready.", ephemeral=True)


def setup(bot):
    bot.add_cog(HRApps(bot))