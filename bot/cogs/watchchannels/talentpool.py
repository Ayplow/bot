import logging
import textwrap
from collections import ChainMap
from typing import Union

from discord import Color, Embed, Member, User
from discord.ext.commands import Bot, Cog, Context, group

from bot.api import ResponseCodeError
from bot.constants import Channels, Guild, Roles, Webhooks
from bot.decorators import with_role
from bot.pagination import LinePaginator
from bot.utils import time
from .watchchannel import WatchChannel, proxy_user

log = logging.getLogger(__name__)
STAFF_ROLES = Roles.owner, Roles.admin, Roles.moderator, Roles.helpers    # <- In constants after the merge?


class TalentPool(WatchChannel, Cog, name="Talentpool"):
    """Relays messages of helper candidates to a watch channel to observe them."""

    def __init__(self, bot: Bot) -> None:
        super().__init__(
            bot,
            destination=Channels.talent_pool,
            webhook_id=Webhooks.talent_pool,
            api_endpoint='bot/nominations',
            api_default_params={'active': 'true', 'ordering': '-inserted_at'},
            logger=log,
        )

    @group(name='talentpool', aliases=('tp', 'talent', 'nomination', 'n'), invoke_without_command=True)
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def nomination_group(self, ctx: Context) -> None:
        """Highlights the activity of helper nominees by relaying their messages to the talent pool channel."""
        await ctx.invoke(self.bot.get_command("help"), "talentpool")

    @nomination_group.command(name='watched', aliases=('all', 'list'))
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def watched_command(self, ctx: Context, update_cache: bool = True) -> None:
        """
        Shows the users that are currently being monitored in the talent pool.

        The optional kwarg `update_cache` can be used to update the user
        cache using the API before listing the users.
        """
        await self.list_watched_users(ctx, update_cache)

    @nomination_group.command(name='watch', aliases=('w', 'add', 'a'))
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def watch_command(self, ctx: Context, user: Union[Member, User, proxy_user], *, reason: str) -> None:
        """
        Relay messages sent by the given `user` to the `#talent-pool` channel.

        A `reason` for adding the user to the talent pool is required and will be displayed
        in the header when relaying messages of this user to the channel.
        """
        if user.bot:
            await ctx.send(f":x: I'm sorry {ctx.author}, I'm afraid I can't do that. I only watch humans.")
            return

        if isinstance(user, Member) and any(role.id in STAFF_ROLES for role in user.roles):
            await ctx.send(f":x: Nominating staff members, eh? Here's a cookie :cookie:")
            return

        if not await self.fetch_user_cache():
            await ctx.send(f":x: Failed to update the user cache; can't add {user}")
            return

        if user.id in self.watched_users:
            await ctx.send(":x: The specified user is already being watched in the talent pool")
            return

        # Manual request with `raise_for_status` as False because we want the actual response
        session = self.bot.api_client.session
        url = self.bot.api_client._url_for(self.api_endpoint)
        kwargs = {
            'json': {
                'actor': ctx.author.id,
                'reason': reason,
                'user': user.id
            },
            'raise_for_status': False,
        }
        async with session.post(url, **kwargs) as resp:
            response_data = await resp.json()

            if resp.status == 400 and response_data.get('user', False):
                await ctx.send(":x: The specified user can't be found in the database tables")
                return
            else:
                resp.raise_for_status()

        self.watched_users[user.id] = response_data
        await ctx.send(f":white_check_mark: Messages sent by {user} will now be relayed to the talent pool channel")

    @nomination_group.command(name='history', aliases=('info', 'search'))
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def history_command(self, ctx: Context, user: Union[User, proxy_user]) -> None:
        """Shows the specified user's nomination history."""
        result = await self.bot.api_client.get(
            self.api_endpoint,
            params={
                'user__id': str(user.id),
                'ordering': "-active,-inserted_at"
            }
        )
        if not result:
            await ctx.send(":warning: This user has never been nominated")
            return

        embed = Embed(
            title=f"Nominations for {user.display_name} `({user.id})`",
            color=Color.blue()
        )
        lines = [self._nomination_to_string(nomination) for nomination in result]
        await LinePaginator.paginate(
            lines,
            ctx=ctx,
            embed=embed,
            empty=True,
            max_lines=3,
            max_size=1000
        )

    @nomination_group.command(name='unwatch', aliases=('end', ))
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def unwatch_command(self, ctx: Context, user: Union[User, proxy_user], *, reason: str) -> None:
        """
        Ends the active nomination of the specified user with the given reason.

        Providing a `reason` is required.
        """
        active_nomination = await self.bot.api_client.get(
            self.api_endpoint,
            params=ChainMap(
                self.api_default_params,
                {"user__id": str(user.id)}
            )
        )

        if not active_nomination:
            await ctx.send(":x: The specified user does not have an active nomination")
            return

        [nomination] = active_nomination
        await self.bot.api_client.patch(
            f"{self.api_endpoint}/{nomination['id']}",
            json={'end_reason': reason, 'active': False}
        )
        await ctx.send(f":white_check_mark: Messages sent by {user} will no longer be relayed")
        self._remove_user(user.id)

    @nomination_group.group(name='edit', aliases=('e',), invoke_without_command=True)
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def nomination_edit_group(self, ctx: Context) -> None:
        """Commands to edit nominations."""
        await ctx.invoke(self.bot.get_command("help"), "talentpool", "edit")

    @nomination_edit_group.command(name='reason')
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def edit_reason_command(self, ctx: Context, nomination_id: int, *, reason: str) -> None:
        """
        Edits the reason/unnominate reason for the nomination with the given `id` depending on the status.

        If the nomination is active, the reason for nominating the user will be edited;
        If the nomination is no longer active, the reason for ending the nomination will be edited instead.
        """
        try:
            nomination = await self.bot.api_client.get(f"{self.api_endpoint}/{nomination_id}")
        except ResponseCodeError as e:
            if e.response.status == 404:
                self.log.trace(f"Nomination API 404: Can't nomination with id {nomination_id}")
                await ctx.send(f":x: Can't find a nomination with id `{nomination_id}`")
                return
            else:
                raise

        field = "reason" if nomination["active"] else "end_reason"

        self.log.trace(f"Changing {field} for nomination with id {nomination_id} to {reason}")

        await self.bot.api_client.patch(
            f"{self.api_endpoint}/{nomination_id}",
            json={field: reason}
        )

        await ctx.send(f":white_check_mark: Updated the {field} of the nomination!")

    def _nomination_to_string(self, nomination_object: dict) -> str:
        """Creates a string representation of a nomination."""
        guild = self.bot.get_guild(Guild.id)

        actor_id = nomination_object["actor"]
        actor = guild.get_member(actor_id)

        active = nomination_object["active"]
        log.debug(active)
        log.debug(type(nomination_object["inserted_at"]))

        start_date = time.format_infraction(nomination_object["inserted_at"])
        if active:
            lines = textwrap.dedent(
                f"""
                ===============
                Status: **Active**
                Date: {start_date}
                Actor: {actor.mention if actor else actor_id}
                Reason: {nomination_object["reason"]}
                Nomination ID: `{nomination_object["id"]}`
                ===============
                """
            )
        else:
            end_date = time.format_infraction(nomination_object["ended_at"])
            lines = textwrap.dedent(
                f"""
                ===============
                Status: Inactive
                Date: {start_date}
                Actor: {actor.mention if actor else actor_id}
                Reason: {nomination_object["reason"]}

                End date: {end_date}
                Unwatch reason: {nomination_object["end_reason"]}
                Nomination ID: `{nomination_object["id"]}`
                ===============
                """
            )

        return lines.strip()
