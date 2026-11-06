import asyncio
import json
import os
import time
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands

# ======================================================================
#  AYARLAR
# ======================================================================
TOKEN = os.getenv("DISCORD_TOKEN", "BURAYA_TOKEN")
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
DATA_FILE = DATA_DIR / "botty_data.json"
MONGODB_URI = os.getenv("MONGODB_URI")  # doluysa veri MongoDB'de tutulur (Render için önerilen)
EMPTY_DELETE_SECONDS = 2        # oda boşalınca kaç saniye sonra silinsin
NEW_ROOM_GRACE_SECONDS = 20     # oluşturan kişi sesli kanalda değilse bekleme süresi

# Oda sahibi / moderatörlerin sahip olduğu izinler
STAFF = dict(connect=True, speak=True, stream=True, use_embedded_activities=True)
STAFF_RESET = {k: None for k in STAFF}

# Developer Portal > Emojis bölümüne yüklenen uygulama emojileri (ad -> emoji)
ICONS = {}


def apply_icon(btn, key, text):
    """İkon yüklüyse sadece ikonu göster, yoksa yazıyı göster."""
    e = ICONS.get(key)
    if e:
        btn.emoji, btn.label = e, None
    else:
        btn.emoji, btn.label = None, text


# ======================================================================
#  VERİ (JSON)
# ======================================================================
db = {"guilds": {}, "rooms": {}}
_mongo_coll = None
_save_lock = asyncio.Lock()
_bg_tasks = set()


async def load_db():
    """Açılışta veriyi MongoDB'den (ya da dosyadan) yükler."""
    global _mongo_coll
    data = {}
    if MONGODB_URI:
        from pymongo import AsyncMongoClient
        client = AsyncMongoClient(MONGODB_URI)
        _mongo_coll = client["botty_bot"]["state"]
        doc = await _mongo_coll.find_one({"_id": "main"})
        if doc:
            data = json.loads(doc["data"])
        print("Veri kaynağı: MongoDB")
    else:
        if DATA_FILE.exists():
            try:
                data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        print(f"Veri kaynağı: dosya ({DATA_FILE})")
    data.setdefault("guilds", {})
    data.setdefault("rooms", {})
    db.clear()
    db.update(data)


async def _persist(snapshot):
    async with _save_lock:
        try:
            if _mongo_coll is not None:
                await _mongo_coll.replace_one(
                    {"_id": "main"}, {"_id": "main", "data": snapshot}, upsert=True)
            else:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(DATA_FILE.write_text, snapshot, "utf-8")
        except Exception as e:
            print("Kayıt hatası:", e)


def save():
    snapshot = json.dumps(db, ensure_ascii=False)
    task = asyncio.get_running_loop().create_task(_persist(snapshot))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def new_room(guild_id, owner_id):
    return {
        "guild": guild_id, "owner": owner_id,
        "mods": [], "whitelist": [], "banned": [],
        "locked": False, "mic": True, "cam": False, "act": False,
        "panel_id": None,
    }


def room_of_owner(uid, guild_id):
    for cid, r in db["rooms"].items():
        if r["owner"] == uid and r["guild"] == guild_id:
            return cid
    return None


def can_manage(user, room, owner_only=False):
    if user.guild_permissions.administrator or user.id == room["owner"]:
        return True
    return (not owner_only) and user.id in room["mods"]


def owner_level(user, room):
    return user.guild_permissions.administrator or user.id == room["owner"]


# ======================================================================
#  BOT
# ======================================================================
async def start_web():
    """Render 'Web Service' için küçük bir sağlık-kontrol sunucusu (PORT varsa açılır)."""
    port = os.getenv("PORT")
    if not port:
        return
    from aiohttp import web

    async def ok(request):
        return web.Response(text="Botty calisiyor")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    print(f"Web sunucusu {port} portunda açıldı")


class BottyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await start_web()
        await load_db()
        try:
            for em in await self.fetch_application_emojis():
                ICONS[em.name] = em
            print(f"{len(ICONS)} uygulama emojisi yüklendi")
        except Exception as e:
            print("Uygulama emojileri yüklenemedi:", e)
        self.add_view(SetupView())
        self.add_view(PanelView())
        await self.tree.sync()


bot = BottyBot()
tree = bot.tree
pending = {}      # kanal_id -> silme görevi
creating = set()  # aynı anda iki oda açılmasını engeller


# ======================================================================
#  YARDIMCI FONKSİYONLAR
# ======================================================================
async def set_ow(ch, target, **perms):
    """Kanal izin ayarını günceller; boş kalırsa overwrite'ı siler."""
    ow = ch.overwrites_for(target)
    ow.update(**perms)
    await ch.set_permissions(target, overwrite=None if ow.is_empty() else ow)


async def resolve_user(guild, uid):
    m = guild.get_member(uid)
    if m:
        return m
    try:
        return await bot.fetch_user(uid)
    except discord.HTTPException:
        return None


async def toggle_user(ch, target, attr, default):
    cur = getattr(ch.overwrites_for(target), attr)
    if cur is None:
        cur = default
    await set_ow(ch, target, **{attr: not cur})


def eff(ch, target, attr, default):
    v = getattr(ch.overwrites_for(target), attr)
    return default if v is None else v


def panel_embed(ch, room):
    def dur(v):
        return "Açık" if v else "Kapalı"

    e = discord.Embed(
        title=f"{ch.name} • Oda Paneli",
        description=("Bu paneli yalnızca oda sahibi ve oda moderatörleri kullanabilir.\n"
                     f"Son güncelleme: <t:{int(time.time())}:R>"),
        color=discord.Color.blurple(),
    )
    e.add_field(name="Sahip", value=f"<@{room['owner']}>")
    e.add_field(name="Limit", value=str(ch.user_limit) if ch.user_limit else "Sınırsız")
    e.add_field(name="Durum", value="Kilitli" if room["locked"] else "Açık")
    e.add_field(name="Oda ayarları",
                value=(f"Mikrofon: {dur(room['mic'])}\n"
                       f"Kamera/Yayın: {dur(room['cam'])}\n"
                       f"Etkinlik: {dur(room['act'])}"),
                inline=False)
    mods = ", ".join(f"<@{i}>" for i in room["mods"][:20]) or "Yok"
    e.add_field(name="Moderatörler", value=mods, inline=False)
    e.add_field(name="Whitelist", value=f"{len(room['whitelist'])} kişi")
    e.add_field(name="Banlı", value=f"{len(room['banned'])} kişi")
    return e


async def refresh_panel(ch, room):
    pid = room.get("panel_id")
    if not pid:
        return
    try:
        await ch.get_partial_message(pid).edit(embed=panel_embed(ch, room), view=PanelView(room))
    except discord.HTTPException:
        pass


async def authorize(interaction, owner_only=False):
    room = db["rooms"].get(str(interaction.channel_id))
    ch = interaction.guild.get_channel(interaction.channel_id) if interaction.guild else None
    if room is None or ch is None:
        await interaction.response.send_message("Bu oda artık kayıtlı değil.", ephemeral=True)
        return None
    if not can_manage(interaction.user, room, owner_only):
        await interaction.response.send_message(
            "Bu butonu yalnızca oda sahibi ve oda moderatörleri kullanabilir.", ephemeral=True)
        return None
    return ch, room


async def fail(interaction, e):
    msg = "İşlem başarısız oldu. Botun yetkilerini kontrol et (Kanalları Yönet, Rolleri Yönet, Üyeleri Taşı)."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass
    print("Hata:", e)


# ---------------- oda silme ----------------
def cancel_delete(cid):
    t = pending.pop(cid, None)
    if t:
        t.cancel()


async def delete_room(ch):
    db["rooms"].pop(str(ch.id), None)
    save()
    try:
        await ch.delete(reason="Geçici oda boş kaldı")
    except discord.HTTPException:
        pass


def schedule_delete(ch, delay=EMPTY_DELETE_SECONDS):
    cancel_delete(ch.id)

    async def job():
        try:
            await asyncio.sleep(delay)
            current = bot.get_channel(ch.id)
            if current is not None and not current.members:
                await delete_room(current)
        finally:
            if pending.get(ch.id) is asyncio.current_task():
                pending.pop(ch.id, None)

    pending[ch.id] = asyncio.create_task(job())


# ======================================================================
#  ODA OLUŞTURMA (kurulum mesajı + modal)
# ======================================================================
class CreateModal(discord.ui.Modal, title="Geçici Oda Oluştur"):
    name = discord.ui.TextInput(label="Oda adı ne olsun?", min_length=1, max_length=50,
                                placeholder="Örn: Sohbet Odası")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user, guild = interaction.user, interaction.guild

        existing = room_of_owner(user.id, guild.id)
        if existing:
            await interaction.followup.send(f"Zaten bir odan var: <#{existing}>", ephemeral=True)
            return
        if user.id in creating:
            await interaction.followup.send("Odan zaten oluşturuluyor, lütfen bekle.", ephemeral=True)
            return

        creating.add(user.id)
        try:
            cfg = db["guilds"].get(str(guild.id), {})
            category = guild.get_channel(cfg["category_id"]) if cfg.get("category_id") else None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    connect=True, speak=True, stream=False, use_embedded_activities=False),
                user: discord.PermissionOverwrite(**STAFF),
            }
            ch = await guild.create_voice_channel(
                name=str(self.name).strip(), category=category, overwrites=overwrites,
                reason=f"{user} geçici oda oluşturdu")
            room = new_room(guild.id, user.id)
            db["rooms"][str(ch.id)] = room
            save()

            panel = await ch.send(embed=panel_embed(ch, room), view=PanelView(room))
            room["panel_id"] = panel.id
            save()

            moved = False
            if user.voice and user.voice.channel:
                try:
                    await user.move_to(ch)
                    moved = True
                except discord.HTTPException:
                    pass
            if not moved:
                schedule_delete(ch, NEW_ROOM_GRACE_SECONDS)
                await interaction.followup.send(
                    f"{ch.mention} oluşturuldu! {NEW_ROOM_GRACE_SECONDS} saniye içinde katılmazsan oda silinecek. "
                    f"Oda paneli, odanın yazı sohbetinde bulunuyor.", ephemeral=True)
            else:
                await interaction.followup.send(f"{ch.mention} oluşturuldu ve seni taşıdım!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "Oda oluşturamadım, botun **Kanalları Yönet** ve **Rolleri Yönet** yetkisi olmalı.",
                ephemeral=True)
        finally:
            creating.discard(user.id)


class SetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Oda Oluştur", style=discord.ButtonStyle.primary,
                       custom_id="botty:create")
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = room_of_owner(interaction.user.id, interaction.guild_id)
        if existing:
            await interaction.response.send_message(f"Zaten bir odan var: <#{existing}>", ephemeral=True)
            return
        await interaction.response.send_modal(CreateModal())


# ======================================================================
#  ODA PANELİ
# ======================================================================
def state_btn(label, on):
    return f"{label}: {'Açık' if on else 'Kapalı'}", (
        discord.ButtonStyle.success if on else discord.ButtonStyle.danger)


class RenameModal(discord.ui.Modal, title="Oda İsmini Değiştir"):
    name = discord.ui.TextInput(label="Yeni oda adı", min_length=1, max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        ctx = await authorize(interaction)
        if not ctx:
            return
        ch, room = ctx
        await interaction.response.defer()
        try:
            new = await asyncio.wait_for(ch.edit(name=str(self.name).strip()), timeout=8)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Discord, kanal adının 10 dakikada en fazla 2 kez değiştirilmesine izin veriyor. Lütfen biraz sonra tekrar dene.",
                ephemeral=True)
            return
        except discord.HTTPException as e:
            await fail(interaction, e)
            return
        await refresh_panel(new or ch, room)


class LimitModal(discord.ui.Modal, title="Oda Limitini Değiştir"):
    limit = discord.ui.TextInput(label="Kullanıcı limiti (0 = sınırsız, en fazla 99)",
                                 min_length=1, max_length=2, placeholder="0")

    async def on_submit(self, interaction: discord.Interaction):
        ctx = await authorize(interaction)
        if not ctx:
            return
        ch, room = ctx
        try:
            n = int(str(self.limit).strip())
            if not 0 <= n <= 99:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("0 ile 99 arasında bir sayı gir.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            new = await ch.edit(user_limit=n)
        except discord.HTTPException as e:
            await fail(interaction, e)
            return
        await refresh_panel(new or ch, room)


HELP_TEXT = (
    "**Oda paneli nasıl kullanılır?**\n"
    "Kilit: Odaya yeni üyelerin girişini açar veya kapatır.\n"
    "Mikrofon: Herkesin konuşma iznini açar veya kapatır.\n"
    "Kamera/Yayın: Herkesin kamera ve yayın iznini açar veya kapatır.\n"
    "Etkinlik: Herkesin etkinlik başlatma iznini açar veya kapatır.\n"
    "İsim ve Limit: Odanın adını ve kullanıcı sınırını değiştirir.\n"
    "Üye Yönet: Seçtiğin üyenin izinlerini, yasak ve beyaz liste durumunu, "
    "moderatörlüğünü ve oda sahipliğini düzenler.\n"
    "Banlananlar: Odadan yasaklanan üyeleri gösterir ve yasağı kaldırır.\n"
    "Yeşil buton açık, kırmızı buton kapalı anlamına gelir."
)


class PanelView(discord.ui.View):
    def __init__(self, room=None):
        super().__init__(timeout=None)
        r = room or {"mic": True, "cam": False, "act": False, "locked": False}
        locked = r["locked"]
        self.lock_btn.style = discord.ButtonStyle.danger if locked else discord.ButtonStyle.success
        apply_icon(self.lock_btn, "lock_closed" if locked else "lock_open",
                   "Oda Kilitli" if locked else "Oda Açık")
        for btn, key, icon, text in (
            (self.mic_btn, "mic", "mic", "Mikrofon"),
            (self.cam_btn, "cam", "camera", "Kamera/Yayın"),
            (self.act_btn, "act", "rocket", "Etkinlik"),
        ):
            btn.style = discord.ButtonStyle.success if r[key] else discord.ButtonStyle.danger
            apply_icon(btn, icon, f"{text}: {'Açık' if r[key] else 'Kapalı'}")
        apply_icon(self.rename_btn, "edit", "İsim")
        apply_icon(self.limit_btn, "limit", "Limit")
        apply_icon(self.members_btn, "members", "Üye Yönet")
        apply_icon(self.banlist_btn, "banlist", "Banlananlar")
        apply_icon(self.info_btn, "info", "Bilgi")

    async def _toggle(self, interaction, key, attr):
        ctx = await authorize(interaction)
        if not ctx:
            return
        ch, room = ctx
        await interaction.response.defer()
        room[key] = not room[key]
        try:
            await set_ow(ch, interaction.guild.default_role, **{attr: room[key]})
        except discord.HTTPException as e:
            room[key] = not room[key]
            await fail(interaction, e)
            return
        save()
        await refresh_panel(ch, room)

    @discord.ui.button(label="Oda Açık", style=discord.ButtonStyle.success,
                       custom_id="botty:lock", row=0)
    async def lock_btn(self, interaction, button):
        ctx = await authorize(interaction)
        if not ctx:
            return
        ch, room = ctx
        await interaction.response.defer()
        room["locked"] = not room["locked"]
        try:
            await set_ow(ch, interaction.guild.default_role, connect=not room["locked"])
        except discord.HTTPException as e:
            room["locked"] = not room["locked"]
            await fail(interaction, e)
            return
        save()
        await refresh_panel(ch, room)

    @discord.ui.button(label="Mikrofon", style=discord.ButtonStyle.success,
                       custom_id="botty:mic", row=0)
    async def mic_btn(self, interaction, button):
        await self._toggle(interaction, "mic", "speak")

    @discord.ui.button(label="Kamera/Yayın", style=discord.ButtonStyle.danger,
                       custom_id="botty:cam", row=0)
    async def cam_btn(self, interaction, button):
        await self._toggle(interaction, "cam", "stream")

    @discord.ui.button(label="Etkinlik", style=discord.ButtonStyle.danger,
                       custom_id="botty:act", row=0)
    async def act_btn(self, interaction, button):
        await self._toggle(interaction, "act", "use_embedded_activities")

    @discord.ui.button(label="İsim", style=discord.ButtonStyle.secondary,
                       custom_id="botty:rename", row=1)
    async def rename_btn(self, interaction, button):
        if await authorize(interaction):
            await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="Limit", style=discord.ButtonStyle.secondary,
                       custom_id="botty:limit", row=1)
    async def limit_btn(self, interaction, button):
        if await authorize(interaction):
            await interaction.response.send_modal(LimitModal())

    @discord.ui.button(label="Üye Yönet", style=discord.ButtonStyle.primary,
                       custom_id="botty:members", row=1)
    async def members_btn(self, interaction, button):
        ctx = await authorize(interaction)
        if not ctx:
            return
        ch, room = ctx
        await interaction.response.send_message(
            "Yönetmek istediğin üyeyi seç:", view=MemberPicker(ch, room, interaction.user), ephemeral=True)

    @discord.ui.button(label="Banlananlar", style=discord.ButtonStyle.secondary,
                       custom_id="botty:banlist", row=1)
    async def banlist_btn(self, interaction, button):
        ctx = await authorize(interaction)
        if not ctx:
            return
        ch, room = ctx
        content, view = await build_banlist(interaction.guild, ch, room)
        kw = {"view": view} if view else {}
        await interaction.response.send_message(content, ephemeral=True, **kw)

    @discord.ui.button(label="Bilgi", style=discord.ButtonStyle.secondary,
                       custom_id="botty:info", row=1)
    async def info_btn(self, interaction, button):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)


# ======================================================================
#  ÜYE SEÇİMİ + ÜYE İŞLEMLERİ
# ======================================================================
class MemberPicker(discord.ui.View):
    def __init__(self, ch, room, actor):
        super().__init__(timeout=120)
        self.ch_id = ch.id
        self.actor_id = actor.id

        options = [discord.SelectOption(label=m.display_name[:100], value=str(m.id))
                   for m in ch.members if m.id not in (actor.id, room["owner"])][:25]
        if options:
            sel = discord.ui.Select(placeholder="Odadaki bir üyeyi seç", options=options, row=0)

            async def sel_cb(interaction, sel=sel):
                target = interaction.guild.get_member(int(sel.values[0]))
                await self.open_target(interaction, target)

            sel.callback = sel_cb
            self.add_item(sel)

        usel = discord.ui.UserSelect(placeholder="Odada olmayan birini seç (whitelist / ban için)", row=1)

        async def usel_cb(interaction, usel=usel):
            await self.open_target(interaction, usel.values[0])

        usel.callback = usel_cb
        self.add_item(usel)

    async def open_target(self, interaction, target):
        room = db["rooms"].get(str(self.ch_id))
        if room is None or target is None:
            await interaction.response.edit_message(content="Üye veya oda bulunamadı.", view=None)
            return
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Bu menü sana ait değil.", ephemeral=True)
            return
        if target.id == room["owner"]:
            await interaction.response.send_message("Oda sahibine işlem yapılamaz.", ephemeral=True)
            return
        if target.id == interaction.user.id:
            await interaction.response.send_message("Kendine işlem yapamazsın.", ephemeral=True)
            return
        if getattr(target, "bot", False):
            await interaction.response.send_message("Botlara işlem yapılamaz.", ephemeral=True)
            return
        if target.id in room["mods"] and not owner_level(interaction.user, room):
            await interaction.response.send_message(
                "Moderatörlere sadece oda sahibi işlem yapabilir.", ephemeral=True)
            return
        view = MemberView(interaction.guild, self.ch_id, target, interaction.user)
        await interaction.response.edit_message(content=view.header(), view=view)


class MemberView(discord.ui.View):
    def __init__(self, guild, ch_id, target, actor):
        super().__init__(timeout=180)
        self.guild, self.ch_id, self.target, self.actor = guild, ch_id, target, actor
        self.build()

    def header(self):
        return f"**{self.target.display_name}** için işlem seç:"

    def _add(self, key, label, style, row, disabled=False):
        b = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)

        async def cb(interaction, key=key):
            await self.on_click(key, interaction)

        b.callback = cb
        self.add_item(b)

    def build(self):
        self.clear_items()
        ch = self.guild.get_channel(self.ch_id)
        room = db["rooms"].get(str(self.ch_id))
        if ch is None or room is None:
            return
        t, tid = self.target, self.target.id
        banned = tid in room["banned"]
        wl = tid in room["whitelist"]
        is_mod = tid in room["mods"]
        top = owner_level(self.actor, room)

        for key, label, attr, default in (
            ("mic", "Mikrofon", "speak", room["mic"]),
            ("cam", "Kamera/Yayın", "stream", room["cam"]),
            ("act", "Etkinlik", "use_embedded_activities", room["act"]),
        ):
            text, style = state_btn(label, eff(ch, t, attr, default))
            self._add(key, text, style, 0, disabled=banned)

        self._add("ban", "Banla", discord.ButtonStyle.danger, 1, disabled=banned)
        self._add("kick", "Odadan At", discord.ButtonStyle.secondary, 1)
        self._add("wl", "Whitelist'ten Çıkar" if wl else "Whitelist'e Ekle",
                  discord.ButtonStyle.success if wl else discord.ButtonStyle.secondary, 1, disabled=banned)
        self._add("mod", "Moderatörlüğü Al" if is_mod else "Moderatör Yap",
                  discord.ButtonStyle.success if is_mod else discord.ButtonStyle.primary, 2,
                  disabled=(not top) or banned)
        self._add("transfer", "Odayı Devret", discord.ButtonStyle.danger, 2,
                  disabled=(not top) or banned)

    async def on_click(self, key, interaction):
        if interaction.user.id != self.actor.id:
            await interaction.response.send_message("Bu menü sana ait değil.", ephemeral=True)
            return
        ch = self.guild.get_channel(self.ch_id)
        room = db["rooms"].get(str(self.ch_id))
        if ch is None or room is None:
            await interaction.response.edit_message(content="Oda artık yok.", view=None)
            return
        if not can_manage(interaction.user, room):
            await interaction.response.edit_message(content="Artık yetkin yok.", view=None)
            return

        await interaction.response.defer()
        t, tid = self.target, self.target.id
        top = owner_level(interaction.user, room)
        note, close = None, False

        try:
            if key == "mic":
                await toggle_user(ch, t, "speak", room["mic"])
            elif key == "cam":
                await toggle_user(ch, t, "stream", room["cam"])
            elif key == "act":
                await toggle_user(ch, t, "use_embedded_activities", room["act"])

            elif key == "ban":
                for lst in ("mods", "whitelist"):
                    if tid in room[lst]:
                        room[lst].remove(tid)
                if tid not in room["banned"]:
                    room["banned"].append(tid)
                await set_ow(ch, t, connect=False, speak=None, stream=None, use_embedded_activities=None)
                m = self.guild.get_member(tid)
                if m and m in ch.members:
                    await m.move_to(None)
                note, close = f"**{t.display_name}** odadan banlandı.", True

            elif key == "kick":
                m = self.guild.get_member(tid)
                if m and m in ch.members:
                    await m.move_to(None)
                    note = f"**{t.display_name}** odadan atıldı."
                else:
                    note = "Bu üye şu an odada değil."

            elif key == "wl":
                if tid in room["whitelist"]:
                    room["whitelist"].remove(tid)
                    await set_ow(ch, t, connect=True if tid in room["mods"] else None)
                else:
                    room["whitelist"].append(tid)
                    await set_ow(ch, t, connect=True)

            elif key == "mod":
                if not top:
                    note = "Moderatör ayarını sadece oda sahibi yapabilir."
                elif tid in room["mods"]:
                    room["mods"].remove(tid)
                    reset = dict(STAFF_RESET)
                    reset["connect"] = True if tid in room["whitelist"] else None
                    await set_ow(ch, t, **reset)
                else:
                    room["mods"].append(tid)
                    await set_ow(ch, t, **STAFF)

            elif key == "transfer":
                if not top:
                    note = "Odayı sadece oda sahibi devredebilir."
                elif room_of_owner(tid, room["guild"]):
                    note = "Bu üyenin zaten kendi odası var, devredemezsin."
                else:
                    old = room["owner"]
                    room["owner"] = tid
                    for lst in ("mods", "whitelist", "banned"):
                        if tid in room[lst]:
                            room[lst].remove(tid)
                    await set_ow(ch, t, **STAFF)
                    old_user = await resolve_user(self.guild, old)
                    if old_user:
                        await set_ow(ch, old_user, **STAFF_RESET)
                    note, close = f"Oda **{t.display_name}** kişisine devredildi.", True
        except discord.HTTPException as e:
            print("Hata:", e)
            note = "İşlem başarısız oldu, botun yetkilerini kontrol et."

        save()
        await refresh_panel(ch, room)
        if close:
            await interaction.edit_original_response(content=note, view=None)
        else:
            self.build()
            await interaction.edit_original_response(
                content=(note + "\n" if note else "") + self.header(), view=self)


# ======================================================================
#  BANLANANLAR LİSTESİ
# ======================================================================
async def build_banlist(guild, ch, room):
    ids = room["banned"][:25]
    if not ids:
        return "Bu odada banlı üye yok.", None
    options = []
    for uid in ids:
        try:
            u = await bot.fetch_user(uid)
            label = f"{u.display_name} ({u.name})"
        except discord.HTTPException:
            label = str(uid)
        options.append(discord.SelectOption(label=label[:100], value=str(uid),
                                            description="Seçersen ban kaldırılır"))
    view = discord.ui.View(timeout=120)
    sel = discord.ui.Select(placeholder="Banını kaldırmak istediğin üyeyi seç", options=options)

    async def cb(interaction):
        r = db["rooms"].get(str(ch.id))
        if r is None or not can_manage(interaction.user, r):
            await interaction.response.edit_message(content="Yetkin yok veya oda yok.", view=None)
            return
        await interaction.response.defer()
        uid = int(sel.values[0])
        if uid in r["banned"]:
            r["banned"].remove(uid)
        u = await resolve_user(guild, uid)
        if u:
            try:
                await set_ow(ch, u, connect=None)
            except discord.HTTPException as e:
                print("Hata:", e)
        save()
        await refresh_panel(ch, r)
        content, new_view = await build_banlist(guild, ch, r)
        await interaction.edit_original_response(
            content=f"Ban kaldırıldı.\n{content}", view=new_view)

    sel.callback = cb
    view.add_item(sel)
    return "**Banlananlar** — banını kaldırmak için birini seç:", view


# ======================================================================
#  OLAYLAR
# ======================================================================
@bot.event
async def on_ready():
    print(f"{bot.user} olarak giriş yapıldı!")
    for cid in list(db["rooms"]):
        room = db["rooms"][cid]
        ch = bot.get_channel(int(cid))
        if ch is None:
            if bot.get_guild(room["guild"]) is not None:
                db["rooms"].pop(cid, None)
        elif not ch.members:
            schedule_delete(ch, NEW_ROOM_GRACE_SECONDS)
    save()


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel == after.channel:
        return
    if after.channel and str(after.channel.id) in db["rooms"]:
        cancel_delete(after.channel.id)
    if before.channel and str(before.channel.id) in db["rooms"] and not before.channel.members:
        schedule_delete(before.channel)


@bot.event
async def on_guild_channel_delete(channel):
    if str(channel.id) in db["rooms"]:
        db["rooms"].pop(str(channel.id), None)
        cancel_delete(channel.id)
        save()


# ======================================================================
#  KOMUTLAR: KURULUM
# ======================================================================
@tree.command(name="kurulum", description="Bu kanala 'Oda Oluştur' mesajını gönderir")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def kurulum(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        await interaction.response.send_message("Bu komutu bir yazı kanalında kullan.", ephemeral=True)
        return
    db["guilds"][str(interaction.guild_id)] = {"category_id": ch.category_id}
    save()
    embed = discord.Embed(
        title="Geçici Ses Odası",
        description="Kendi geçici ses odanı oluşturmak için aşağıdaki butona tıkla.",
        color=discord.Color.blurple())
    await ch.send(embed=embed, view=SetupView())
    await interaction.response.send_message(
        "Kurulum tamamlandı. Odalar bu kanalın bulunduğu kategoride açılacak.", ephemeral=True)


# ======================================================================
#  KOMUTLAR: MODERASYON
# ======================================================================
def hierarchy_ok(interaction, target):
    if interaction.user.id == interaction.guild.owner_id:
        return True
    return target.top_role < interaction.user.top_role


@tree.command(name="ban", description="Bir üyeyi sunucudan banlar")
@app_commands.describe(uye="Banlanacak üye", sebep="Sebep")
@app_commands.default_permissions(ban_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, uye: discord.Member, sebep: str = "Sebep belirtilmedi"):
    if uye.id == interaction.user.id or not hierarchy_ok(interaction, uye):
        await interaction.response.send_message("Bu üyeyi banlayamazsın.", ephemeral=True)
        return
    try:
        await uye.ban(reason=f"{interaction.user}: {sebep}")
    except discord.Forbidden:
        await interaction.response.send_message("Botun yetkisi bu üye için yetersiz.", ephemeral=True)
        return
    await interaction.response.send_message(f"{uye} banlandı. Sebep: {sebep}")


@tree.command(name="unban", description="Kullanıcı ID'si ile banı kaldırır")
@app_commands.describe(kullanici_id="Kullanıcının ID'si", sebep="Sebep")
@app_commands.default_permissions(ban_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, kullanici_id: str, sebep: str = "Sebep belirtilmedi"):
    try:
        uid = int(kullanici_id)
    except ValueError:
        await interaction.response.send_message("Geçerli bir ID gir.", ephemeral=True)
        return
    try:
        await interaction.guild.unban(discord.Object(id=uid), reason=f"{interaction.user}: {sebep}")
    except discord.NotFound:
        await interaction.response.send_message("Bu kullanıcı banlı değil.", ephemeral=True)
        return
    await interaction.response.send_message(f"`{uid}` kullanıcısının banı kaldırıldı.")


@tree.command(name="kick", description="Bir üyeyi sunucudan atar")
@app_commands.describe(uye="Atılacak üye", sebep="Sebep")
@app_commands.default_permissions(kick_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, uye: discord.Member, sebep: str = "Sebep belirtilmedi"):
    if uye.id == interaction.user.id or not hierarchy_ok(interaction, uye):
        await interaction.response.send_message("Bu üyeyi atamazsın.", ephemeral=True)
        return
    try:
        await uye.kick(reason=f"{interaction.user}: {sebep}")
    except discord.Forbidden:
        await interaction.response.send_message("Botun yetkisi bu üye için yetersiz.", ephemeral=True)
        return
    await interaction.response.send_message(f"{uye} atıldı. Sebep: {sebep}")


@tree.command(name="timeout", description="Bir üyeyi susturur (zaman aşımı)")
@app_commands.describe(uye="Susturulacak üye", dakika="Süre (dakika, en fazla 40320)", sebep="Sebep")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, uye: discord.Member,
                  dakika: app_commands.Range[int, 1, 40320], sebep: str = "Sebep belirtilmedi"):
    if uye.id == interaction.user.id or not hierarchy_ok(interaction, uye):
        await interaction.response.send_message("Bu üyeyi susturamazsın.", ephemeral=True)
        return
    try:
        await uye.timeout(timedelta(minutes=dakika), reason=f"{interaction.user}: {sebep}")
    except discord.Forbidden:
        await interaction.response.send_message("Botun yetkisi bu üye için yetersiz.", ephemeral=True)
        return
    await interaction.response.send_message(f"{uye} {dakika} dakika susturuldu. Sebep: {sebep}")


@tree.command(name="untimeout", description="Üyenin susturmasını kaldırır")
@app_commands.describe(uye="Susturması kaldırılacak üye")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, uye: discord.Member):
    try:
        await uye.timeout(None, reason=f"{interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("Botun yetkisi bu üye için yetersiz.", ephemeral=True)
        return
    await interaction.response.send_message(f"{uye} kişisinin susturması kaldırıldı.")


@tree.command(name="temizle", description="Kanaldaki son mesajları siler")
@app_commands.describe(adet="Silinecek mesaj sayısı (1-100)")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
async def temizle(interaction: discord.Interaction, adet: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=adet)
    await interaction.followup.send(f"{len(deleted)} mesaj silindi.", ephemeral=True)


@tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Bu komut için yetkin yok."
    else:
        msg = "Bir hata oluştu."
        print("Komut hatası:", error)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN or TOKEN == "BURAYA_TOKEN":
        raise SystemExit("DISCORD_TOKEN ortam değişkeni tanımlı değil!")
    bot.run(TOKEN)
