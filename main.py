import discord
from discord import app_commands
from discord.ext import tasks
from motor.motor_asyncio import AsyncIOMotorClient
from google import genai
import hashlib
import os
import io
import json
import PIL.Image
from datetime import datetime, timedelta, timezone
import asyncio
from dotenv import load_dotenv

load_dotenv()

# --- 設定 ---
TARGET_ROLE_NAME = "ROLE NAME" # ロール名
TARGET_CHANNEL = "CHANNEL NAME" # Youtubeのチャンネル名
GUILD_ID = 1000000000 # DiscordのサーバーID
# ---ここから変更しない！！！
MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = "hololive_membership"
COLLECTION_NAME = "verified_members"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

gen_client = genai.Client(api_key=GEMINI_API_KEY)

class IdentityClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True 
        super().__init__(intents=intents)
        
        self.tree = app_commands.CommandTree(self)
        self.mongo_client = AsyncIOMotorClient(MONGO_URL)
        self.db = self.mongo_client[DB_NAME]
        self.collection = self.db[COLLECTION_NAME]

    async def setup_hook(self):
        await self.collection.create_index("payment_hash", unique=True)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.check_expiration.start()

    @tasks.loop(hours=1)
    async def check_expiration(self):
        now_utc = datetime.now(timezone.utc)
        print(f"[{now_utc}] Running smart expiration check...")
        
        guild = self.get_guild(GUILD_ID)
        if not guild: return
        role = discord.utils.get(guild.roles, name=TARGET_ROLE_NAME)
        if not role: return

        # 1. 【期限切れ当日】の処理
        expired_cursor = self.collection.find({"expire_at": {"$lt": now_utc}})
        async for entry in expired_cursor:
            member = guild.get_member(entry["user_id"])
            if member:
                try:
                    await member.remove_roles(role, reason="Membership expired")
                    await member.send(f"【{guild.name}】メンバーシップの有効期限が切れたため、ロールを剥奪しました。継続される場合は再度認証してください。")
                except Exception as e: print(f"Removal error: {e}")
            await self.collection.delete_one({"_id": entry["_id"]})

        # 2. 【前日通知】の処理
        notice_threshold = now_utc + timedelta(days=1)
        notify_cursor = self.collection.find({
            "expire_at": {"$lt": notice_threshold},
            "notified_expiration": {"$ne": True}
        })
        async for entry in notify_cursor:
            member = guild.get_member(entry["user_id"])
            if member:
                try:
                    exp_date = entry["expire_at"].strftime('%Y/%m/%d')
                    await member.send(f"【{guild.name}】メンバーシップの有効期限が近づいています。（終了予定: {exp_date}）\n期限が切れるとロールは剥奪されます。")
                    await self.collection.update_one({"_id": entry["_id"]}, {"$set": {"notified_expiration": True}})
                except Exception: pass

    @check_expiration.before_loop
    async def before_check(self): await self.wait_until_ready()

    async def get_or_create_role(self, guild: discord.Guild):
        role = discord.utils.get(guild.roles, name=TARGET_ROLE_NAME)
        if role is None:
            try:
                role = await guild.create_role(name=TARGET_ROLE_NAME, colour=discord.Colour.from_rgb(33, 197, 243))
            except discord.Forbidden: return None
        return role

client = IdentityClient()

@client.tree.command(name="verify", description="メンバーシップの認証をします")
@app_commands.describe(image="お支払い方法と有効期限がわかるスクリーンショットを添付してください")
@app_commands.rename(image="スクリーンショット")
async def verify(interaction: discord.Interaction, image: discord.Attachment):
    if not image.content_type or not image.content_type.startswith('image/'):
        await interaction.response.send_message("画像を添付してください。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        target_role = await client.get_or_create_role(interaction.guild)
        image_bytes = await image.read()
        img = PIL.Image.open(io.BytesIO(image_bytes))

        # 現在のUTC時刻を取得
        now_utc = datetime.now(timezone.utc)

        # プロンプトに現在時刻を明記して、古いスクショを判定させる
        prompt = f"""
        YouTubeメンバーシップのスクリーンショットを解析し、以下のルールでJSONを返してください。
        
        【現在時刻 (UTC)】: {now_utc.strftime('%Y-%m-%d')}
        
        【厳格な判定ルール】
        1. チャンネル名が「{TARGET_CHANNEL}」であるか確認してください。
        2. 画像内の「次回請求日」や「有効期限」が、上記【現在時刻】より過去である場合は、is_memberをfalseにしてください。
        3. 他の配信者のチャンネル（例：さくらみこ等）は全て「is_member: false」にしてください。

        【出力フォーマット】
        {{
          "is_member": boolean,
          "payment_method": "string",
          "channel_name": "string",
          "expiration_date": "YYYY-MM-DD or null",
          "reason": "判定の根拠（例：期限が切れています、チャンネル名が違います等）"
        }}
        """

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: gen_client.models.generate_content(
            model='gemini-3.1-flash-lite-preview', contents=[prompt, img]
        ))
        
        res_text = response.text.strip().removeprefix("```json").removesuffix("```").strip()
        result = json.loads(res_text)
        
        fail_reason = result.get("reason", "不明なエラー")
        detected_ch = result.get("channel_name", "取得失敗")

        # --- 厳格なチェック ---
        is_ai_valid = result.get("is_member")
        is_ch_correct = TARGET_CHANNEL.lower() in detected_ch.lower()
        
        # AIが読み取った日付をパース
        expire_dt = None
        is_past = False
        if result.get("expiration_date"):
            try:
                expire_dt = datetime.strptime(result["expiration_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                # 読み取った日付が現在時刻より前なら「過去」
                if expire_dt < now_utc:
                    is_past = True
            except ValueError:
                pass

        # 1. AI判定がTrue 2. チャンネル一致 3. 期限が過去でない
        if is_ai_valid and is_ch_correct and not is_past:
            
            # 日付が取れなかった場合のフォールバック（ここに来る＝AIがTrueと言っている）
            if not expire_dt:
                expire_dt = now_utc + timedelta(days=31)
                display_date = expire_dt.strftime("%Y-%m-%d") + " (推定)"
            else:
                display_date = expire_dt.strftime("%Y-%m-%d")

            raw_id = f"{detected_ch}_{result.get('payment_method')}"
            payment_hash = hashlib.sha256(raw_id.encode()).hexdigest()

            # 他の人の重複チェック
            existing = await client.collection.find_one({"payment_hash": payment_hash})
            if existing and existing["user_id"] != interaction.user.id:
                await interaction.followup.send("⚠️ この決済情報は既に他のユーザーが使用しています。", ephemeral=True)
                return

            await client.collection.update_one(
                {"payment_hash": payment_hash},
                {"$set": {
                    "user_id": interaction.user.id,
                    "expire_at": expire_dt,
                    "notified_expiration": False,
                    "verified_at": now_utc
                }},
                upsert=True
            )

            await interaction.user.add_roles(target_role)
            await interaction.followup.send(f"✅ 認証成功！\nチャンネル: **{detected_ch}**\n有効期限: **{display_date}**", ephemeral=True)
        
        else:
            # 失敗理由の構成
            if is_past:
                msg = f"❌ 期限切れのスクリーンショットです。\n**期限:** {result.get('expiration_date')}\n**現在:** {now_utc.strftime('%Y-%m-%d')}"
            elif not is_ch_correct:
                msg = f"❌ チャンネルが異なります。\n**検出:** {detected_ch}\n**対象:** {TARGET_CHANNEL}"
            else:
                msg = f"❌ 認証できませんでした。\n**理由:** {fail_reason}"
            
            await interaction.followup.send(msg, ephemeral=True)

    except Exception as e:
        print(f"Error: {e}")
        await interaction.followup.send("解析中にエラーが発生しました。スクショを撮り直してください。", ephemeral=True)

client.run(DISCORD_TOKEN)