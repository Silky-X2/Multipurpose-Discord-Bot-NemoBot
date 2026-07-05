import discord
from discord.ext import commands

#ID des Erstellungskanals
CREATOR_CHANNEL_ID = 1523320366959820851

# In-memory map: voice_channel_id owner_id
_temp_channels: dict[int, int] = {}


class ChannelNameModal(discord.ui.Modal):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(title="Kanal benennen")
        self.channel = channel
        self.add_item(
            discord.ui.InputText(
                label="Kanalname",
                placeholder="Gib deinem Kanal einen Namen ...",
                max_length=100,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        name = self.children[0].value.strip() or self.channel.name
        try:
            await self.channel.edit(name=name)
            await interaction.response.send_message(
                f"Dein Kanal wurde in **{name}** umbenannt.", ephemeral=True
            )
        except discord.NotFound:
            await interaction.response.send_message(
                "Dein Kanal existiert nicht mehr.", ephemeral=True
            )


class ChannelNameView(discord.ui.View):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.button(label="Kanal benennen", style=discord.ButtonStyle.primary)
    async def rename_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(ChannelNameModal(self.channel))
        self.stop()


class TempVoice(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        # User joins the creator channel create a temp channel
        if after.channel and after.channel.id == CREATOR_CHANNEL_ID:
            category = after.channel.category
            channel_name = f"{member.display_name}'s Kanal"

            new_channel = await member.guild.create_voice_channel(
                name=channel_name,
                category=category,
                reason=f"TempVoice für {member}",
            )

            _temp_channels[new_channel.id] = member.id

            try:
                await member.move_to(new_channel)
            except discord.HTTPException:
                await new_channel.delete(reason="TempVoice: Nutzer nicht mehr verfügbar")
                _temp_channels.pop(new_channel.id, None)
                return

            # Send DM with button to name the channel
            try:
                await member.send(
                    f"Du hast einen temporären Sprachkanal erstellt. Klicke den Button um ihm einen Namen zu geben.",
                    view=ChannelNameView(new_channel),
                )
            except discord.Forbidden:
                pass  # DMs deaktiviert Standardname bleibt bestehen

        # User left a temp channel → delete it if empty
        if before.channel and before.channel.id in _temp_channels:
            channel = before.channel
            if len(channel.members) == 0:
                _temp_channels.pop(channel.id, None)
                try:
                    await channel.delete(reason="TempVoice: Kanal leer")
                except discord.NotFound:
                    pass


def setup(bot: discord.Bot):
    bot.add_cog(TempVoice(bot))
