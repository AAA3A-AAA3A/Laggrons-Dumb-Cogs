# InstantCommands by retke, aka El Laggron
# Idea by Malarne

import discord
import asyncio
import traceback
import textwrap
import logging
import os
import sys

from typing import Optional
from laggron_utils.logging import close_logger, DisabledConsoleOutput

from redbot.core import commands
from redbot.core import checks
from redbot.core import Config
from redbot.core.bot import Red
from redbot.core.utils.predicates import MessagePredicate, ReactionPredicate
from redbot.core.utils.menus import start_adding_reactions
from redbot.core.utils.chat_formatting import pagify

from .utils import Listener

log = logging.getLogger("red.laggron.instantcmd")
BaseCog = getattr(commands, "Cog", object)

# Red 3.0 backwards compatibility, thanks Sinbad
listener = getattr(commands.Cog, "listener", None)
if listener is None:

    def listener(name=None):
        return lambda x: x


class FakeListener:
    """
    A fake listener used to remove the extra listeners.

    This is needed due to how extra listeners works, and how the cog stores these.
    When adding a listener to the list, we get its ID. Then, when we need to remove\
    the listener, we call this fake class with that ID, so discord.py thinks this is\
    that listener.

    Credit to mikeshardmind for finding this solution. For more info, please look at this issue:
    https://github.com/Rapptz/discord.py/issues/1284
    """

    def __init__(self, idx):
        self.idx = idx

    def __eq__(self, function):
        return self.idx == id(function)


class InstantCommands(BaseCog):
    """
    Generate a new command from a code snippet, without making a new cog.

    Documentation https://laggron.red/instantcommands.html
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.data = Config.get_conf(self, 260)

        def_global = {"commands": {}, "dev_values": {}, "updated_body": False}
        self.data.register_global(**def_global)
        self.listeners = {}

        # resume all commands and listeners
        bot.loop.create_task(self.resume_commands())

    __author__ = ["retke (El Laggron)"]
    __version__ = "1.3.2"

    # def get_config_identifier(self, name):
    # """
    # Get a random ID from a string for Config
    # """

    # random.seed(name)
    # identifier = random.randint(0, 999999)
    # self.env["config"] = Config.get_conf(self, identifier)

    def get_function_from_str(self, command, name=None):
        """
        Execute a string, and try to get a function from it.
        """

        # self.get_config_identifier(name)
        to_compile = "def func():\n%s" % textwrap.indent(command, "  ")
        sys.path.append(os.path.dirname(__file__))
        env = {
            "bot": self.bot,
            "discord": discord,
            "commands": commands,
            "checks": checks,
            "asyncio": asyncio,
        }
        exec(to_compile, env)
        sys.path.remove(os.path.dirname(__file__))
        result = env["func"]()
        if not result:
            raise RuntimeError("Nothing detected. Make sure to return something")
        return result

    def load_command_or_listener(self, function):
        """
        Add a command to discord.py or create a listener
        """

        if isinstance(function, commands.Command):
            self.bot.add_command(function)
            log.debug(f"Added command {function.name}")
        else:
            if not isinstance(function, Listener):
                function = Listener(function, function.__name__)
            self.bot.add_listener(function.func, name=function.name)
            self.listeners[function.func.__name__] = (function.id, function.name)
            if function.name != function.func.__name__:
                log.debug(
                    f"Added listener {function.func.__name__} listening for the "
                    f"event {function.name} (ID: {function.id})"
                )
            else:
                log.debug(f"Added listener {function.name} (ID: {function.id})")

    async def resume_commands(self):
        """
        Load all instant commands made.
        This is executed on load with __init__
        """
        dev_values = await self.data.dev_values()
        for name, code in dev_values.items():
            try:
                function = self.get_function_from_str(code, name)
            except Exception as e:
                log.exception("An exception occurred while trying to resume dev value %s", name)
            else:
                self.bot.add_dev_env_value(name, function)
                log.debug(f"Added dev value %s", name)

        _commands = await self.data.commands()
        for name, command_string in _commands.items():
            try:
                function = self.get_function_from_str(command_string, name)
            except Exception as e:
                log.exception("An exception occurred while trying to resume command %s", name)
            else:
                self.load_command_or_listener(function)

    async def remove_commands(self):
        async with self.data.commands() as _commands:
            for command in _commands:
                if command in self.listeners:
                    # remove a listener
                    listener_id, name = self.listeners[command]
                    self.bot.remove_listener(FakeListener(listener_id), name=name)
                    log.debug(f"Removed listener {command} due to cog unload.")
                else:
                    # remove a command
                    self.bot.remove_command(command)
                    log.debug(f"Removed command {command} due to cog unload.")
        async with self.data.dev_values() as values:
            for name in values:
                self.bot.remove_dev_env_value(name)
                log.debug(f"Removed dev value {name} due to cog unload.")

    # from DEV cog, made by Cog Creators (tekulvw)
    @staticmethod
    def cleanup_code(content):
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith("```") and content.endswith("```"):
            return "\n".join(content.split("\n")[1:-1])

        # remove `foo`
        return content.strip("` \n")

    async def _ask_for_edit(self, ctx: commands.Context, kind: str) -> bool:
        msg = await ctx.send(
            f"That {kind} is already registered with InstantCommands. "
            "Would you like to replace it?"
        )
        pred = ReactionPredicate.yes_or_no(msg, ctx.author)
        start_adding_reactions(msg, ReactionPredicate.YES_OR_NO_EMOJIS)
        try:
            await self.bot.wait_for("reaction_add", check=pred, timeout=30)
        except asyncio.TimeoutError:
            await ctx.send("Cancelled.")
            return False
        if not pred.result:
            await ctx.send("Cancelled.")
            return False
        return True

    async def _read_from_file(self, ctx: commands.Context, msg: discord.Message):
        content = await msg.attachments[0].read()
        try:
            function_string = content.decode()
        except UnicodeDecodeError as e:
            log.error("Failed to decode file for instant command.", exc_info=e)
            await ctx.send(
                ":warning: Failed to decode the file, all invalid characters will be replaced."
            )
            function_string = content.decode(errors="replace")
        finally:
            return self.cleanup_code(function_string)

    async def _extract_code(
        self, ctx: commands.Context, command: Optional[str] = None, is_instantcmd=True
    ):
        if ctx.message.attachments:
            function_string = await self.read_from_file(ctx, ctx.message)
        elif command:
            function_string = self.cleanup_code(command)
        else:
            message = (
                (
                    "You're about to create a new command.\n"
                    "Your next message will be the code of the command.\n\n"
                    "If this is the first time you're adding instant commands, "
                    "please read the wiki:\n"
                    "https://laggron.red/instantcommands.html#usage"
                )
                if is_instantcmd
                else (
                    "You're about to add a new value to the dev environment.\n"
                    "Your next message will be the code returning that value.\n\n"
                    "If this is the first time you're editing the dev environment "
                    "with InstantCommands, please read the wiki:\n"
                    "https://laggron.red/instantcommands.html#usage"
                )
            )
            await ctx.send(message)
            pred = MessagePredicate.same_context(ctx)
            try:
                response: discord.Message = await self.bot.wait_for(
                    "message", timeout=900, check=pred
                )
            except asyncio.TimeoutError:
                await ctx.send("Question timed out.")
                return

            if response.content == "" and response.attachments:
                function_string = await self.read_from_file(ctx, response)
            else:
                function_string = self.cleanup_code(response.content)

        try:
            function = self.get_function_from_str(function_string)
        except Exception as e:
            exception = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            message = (
                f"An exception has occured while compiling your code:\n```py\n{exception}\n```"
            )
            for page in pagify(message):
                await ctx.send(page)
            return
        return function, function_string

    @checks.is_owner()
    @commands.group(aliases=["instacmd", "instantcommand"])
    async def instantcmd(self, ctx):
        """Instant Commands cog management"""
        pass

    @instantcmd.command(aliases=["add"])
    async def create(self, ctx, *, command: str = None):
        """
        Instantly generate a new command from a code snippet.

        If you want to make a listener, give its name instead of the command name.
        You can upload a text file if the command is too long, but you should consider coding a \
cog at this point.
        """
        function = await self._extract_code(ctx, command)
        if function is None:
            return
        function, function_string = function
        # if the user used the command correctly, we should have one async function
        if isinstance(function, commands.Command):
            async with self.data.commands() as _commands:
                if function.name in _commands:
                    response = await self._ask_for_edit(ctx, "command")
                    if response is False:
                        return
                    self.bot.remove_command(function.name)
                    log.debug(f"Removed command {function.name} due to incoming overwrite (edit).")
            try:
                self.bot.add_command(function)
            except Exception as e:
                exception = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                message = (
                    "An expetion has occured while adding the command to discord.py:\n"
                    f"```py\n{exception}\n```"
                )
                for page in pagify(message):
                    await ctx.send(page)
                return
            else:
                async with self.data.commands() as _commands:
                    _commands[function.name] = function_string
                await ctx.send(f"The command `{function.name}` was successfully added.")
                log.debug(f"Added command {function.name}")

        else:
            if not isinstance(function, Listener):
                function = Listener(function, function.__name__)
            async with self.data.commands() as _commands:
                if function.func.__name__ in _commands:
                    response = await self._ask_for_edit(ctx, "listener")
                    if response is False:
                        return
                    listener_id, listener_name = self.listeners[function.func.__name__]
                    self.bot.remove_listener(FakeListener(listener_id), name=listener_name)
                    del listener_id, listener_name
                    log.debug(
                        f"Removed listener {function.name} due to incoming overwrite (edit)."
                    )
            try:
                self.bot.add_listener(function.func, name=function.name)
            except Exception as e:
                exception = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                message = (
                    "An expetion has occured while adding the listener to discord.py:\n"
                    f"```py\n{exception}\n```"
                )
                for page in pagify(message):
                    await ctx.send(page)
                return
            else:
                self.listeners[function.func.__name__] = (function.id, function.name)
                async with self.data.commands() as _commands:
                    _commands[function.func.__name__] = function_string
                if function.name != function.func.__name__:
                    await ctx.send(
                        f"The listener `{function.func.__name__}` listening for the "
                        f"event `{function.name}` was successfully added."
                    )
                    log.debug(
                        f"Added listener {function.func.__name__} listening for the "
                        f"event {function.name} (ID: {function.id})"
                    )
                else:
                    await ctx.send(f"The listener {function.name} was successfully added.")
                    log.debug(f"Added listener {function.name} (ID: {function.id})")

    @instantcmd.command(aliases=["del", "remove"])
    async def delete(self, ctx, command_or_listener: str):
        """
        Remove a command or a listener from the registered instant commands.
        """
        command = command_or_listener
        async with self.data.commands() as _commands:
            if command not in _commands:
                await ctx.send("That instant command doesn't exist")
                return
            if command in self.listeners:
                text = "listener"
                function, name = self.listeners[command]
                self.bot.remove_listener(FakeListener(function), name=name)
            else:
                text = "command"
                self.bot.remove_command(command)
            _commands.pop(command)
        await ctx.send(f"The {text} `{command}` was successfully removed.")

    @instantcmd.command(name="list")
    async def _list(self, ctx):
        """
        List all existing commands made using Instant Commands.
        """
        message = "List of instant commands:\n" "```Diff\n"
        _commands = await self.data.commands()
        for name, command in _commands.items():
            message += f"+ {name}\n"
        message += (
            "```\n"
            "You can show the command source code by typing "
            f"`{ctx.prefix}instacmd source <command>`"
        )
        if _commands == {}:
            await ctx.send("No instant command created.")
            return
        for page in pagify(message):
            await ctx.send(page)

    @instantcmd.command()
    async def source(self, ctx: commands.Context, command: str):
        """
        Show the code of an instantcmd command or listener.
        """
        _commands = await self.data.commands()
        if command not in _commands:
            await ctx.send("Command not found.")
            return
        _function = self.get_function_from_str(_commands[command])
        prefix = ctx.clean_prefix if isinstance(_function, commands.Command) else ""
        await ctx.send(f"Source code for `{prefix}{command}`:")
        await ctx.send_interactive(
            pagify(_commands[command], shorten_by=10), box_lang="py", timeout=60
        )

    @instantcmd.group()
    async def env(self, ctx: commands.Context):
        """
        Manage Red's dev environment

        This allows you to add custom values to the developer's environement used by the \
core dev commands (debug, eval, repl).
        Note that this cannot be used inside instantcommands due to the context requirement. 
        """
        pass

    @env.command(name="add")
    async def env_add(self, ctx: commands.Context, name: str, *, code: str = None):
        """
        Add a new value to Red's dev environement.

        The code is in the form of an eval (like instantcmds) and must return a callable that \
takes the context as its sole parameter.
        """
        function = await self._extract_code(ctx, code, False)
        if function is None:
            return
        function, function_string = function
        # if the user used the command correctly, we should have one async function
        async with self.data.dev_values() as values:
            if name in values:
                response = await self._ask_for_edit(ctx, "dev value")
                if response is False:
                    return
                self.bot.remove_dev_env_value(name)
                log.debug(f"Removed dev value {name} due to incoming overwrite (edit).")
        try:
            self.bot.add_dev_env_value(name, function)
        except Exception as e:
            exception = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            message = (
                "An expetion has occured while adding the value to Red:\n"
                f"```py\n{exception}\n```"
            )
            for page in pagify(message):
                await ctx.send(page)
            return
        else:
            async with self.data.dev_values() as values:
                values[name] = function_string
            await ctx.send(f"The dev value `{name}` was successfully added.")
            log.debug(f"Added dev value {name}")

    @env.command(name="delete", aliases=["del", "remove"])
    async def env_delete(self, ctx: commands.Context, name: str):
        """
        Unload and remove a dev value from the registered ones with instantcmd.
        """
        async with self.data.dev_values() as values:
            if name not in values:
                await ctx.send("That value doesn't exist")
                return
            self.bot.remove_dev_env_value(name)
            values.pop(name)
        await ctx.send(f"The dev env value `{name}` was successfully removed.")

    @env.command(name="list")
    async def env_list(self, ctx: commands.Context):
        """
        List all dev env values registered.
        """
        embed = discord.Embed(name="List of dev env values")
        dev_cog = self.bot.get_cog("Dev")
        values = await self.data.dev_values()
        if not values:
            message = "Nothing set yet."
        else:
            message = "- " + "\n- ".join(values)
            embed.set_footer(
                text=(
                    "You can show the command source code by typing "
                    f"`{ctx.prefix}instacmd env source <name>`"
                )
            )
        embed.add_field(name="Registered with InstantCommands", value=message, inline=False)
        if dev_cog:
            embed.description = "Dev mode is currently enabled"
            other_values = [x for x in dev_cog.env_extensions if x not in values]
            if other_values:
                embed.add_field(
                    name="Other dev env values",
                    value="- " + "\n- ".join(other_values),
                    inline=False,
                )
        else:
            embed.description = "Dev mode is currently disabled"
        embed.colour = await ctx.embed_colour()
        await ctx.send(embed=embed)

    @env.command(name="source")
    async def env_source(self, ctx: commands.Context, name: str):
        """
        Show the code of a dev env value.
        """
        values = await self.data.dev_values()
        if name not in values:
            await ctx.send("Value not found.")
            return
        await ctx.send(f"Source code for `{name}`:")
        await ctx.send_interactive(pagify(values[name], shorten_by=10), box_lang="py", timeout=60)

    @commands.command(hidden=True)
    @checks.is_owner()
    async def instantcmdinfo(self, ctx):
        """
        Get informations about the cog.
        """
        await ctx.send(
            "Laggron's Dumb Cogs V3 - instantcmd\n\n"
            "Version: {0.__version__}\n"
            "Author: {0.__author__}\n"
            "Github repository: https://github.com/retke/Laggrons-Dumb-Cogs/tree/v3\n"
            "Discord server: https://discord.gg/AVzjfpR\n"
            "Documentation: http://laggrons-dumb-cogs.readthedocs.io/\n\n"
            "Support my work on Patreon: https://www.patreon.com/retke"
        ).format(self)

    @listener()
    async def on_command_error(self, ctx, error):
        if not isinstance(error, commands.CommandInvokeError):
            return
        if not ctx.command.cog_name == self.__class__.__name__:
            # That error doesn't belong to the cog
            return
        async with self.data.commands() as _commands:
            if ctx.command.name in _commands:
                log.info(f"Error in instant command {ctx.command.name}.", exc_info=error.original)
                return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "I need the `Add reactions` and `Manage messages` in the "
                "current channel if you want to use this command."
            )
        with DisabledConsoleOutput(log):
            log.error(
                f"Exception in command '{ctx.command.qualified_name}'.\n\n",
                exc_info=error.original,
            )

    # correctly unload the cog
    def __unload(self):
        self.cog_unload()

    def cog_unload(self):
        log.debug("Unloading cog...")

        async def unload():
            # removes commands and listeners
            await self.remove_commands()

            # remove all handlers from the logger, this prevents adding
            # multiple times the same handler if the cog gets reloaded
            close_logger(log)

        # I am forced to put everything in an async function to execute the remove_commands
        # function, and then remove the handlers. Using loop.create_task on remove_commands only
        # executes it after removing the log handlers, while it needs to log...
        self.bot.loop.create_task(unload())
