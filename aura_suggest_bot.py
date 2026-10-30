import asyncio
import os
import time
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, FSInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

# ================= НАСТРОЙКИ ПРЕДЛОЖ-БОТА =================
BOT_TOKEN = "9165467259:AAEBWBELEwwHwaFUFTKkVpEcg-mDMDKpPYA"
ADMIN_ID = 1094406951                          # ТВОЙ ТЕЛЕГРАМ ID (сюда падают заявки + вечный доступ)
CLUB_CHAT_ID = -1004324447753                  # ID закрытой группы AURA (куда бот постит и где проверяет подписку)

DB_NAME = "aura_club.db"                       # База данных (если лежит в одной папке с ботом оплаты — отсечет и пробников)
MAIN_IMAGE = "club_main.jpg"                   # Обложка главного меню

# ID топиков в закрытой группе
TOPICS = {
    "sos": 9,          # ID топика "🩸 Разбор ситуаций (SOS)"
    "challenge": 24,    # ID топика "⚔️ Челленджи и Режим"
    "network": 27,      # ID топика "🤝 Связи и Свои люди"
    "books": 32         # ID топика "📖 Книжный арсенал и Саммари"
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ================= МАШИНА СОСТОЯНИЙ =================
class SuggestionState(StatesGroup):
    waiting_for_content = State()
    waiting_for_admin_comment = State()

# ================= БАЗА ДАННЫХ =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS suggestions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER, "
            "category TEXT, "
            "text_content TEXT, "
            "media_id TEXT, "
            "status TEXT DEFAULT 'pending', "
            "created_at INTEGER)"
        )
        await db.commit()

# ================= ПРОВЕРКА ДОСТУПА (ПЛАН Б + ЗАЩИТА ОТ ПРОБНИКОВ) =================
async def check_active_sub(user_id: int) -> tuple[bool, str]:
    """
    Проверяет доступ к предложке:
    1. Админ проходит всегда.
    2. Если доступна общая БД с ботом оплаты и юзер на пробном часе (total_paid == 0) — отказ.
    3. План Б: проверка через Telegram API (состоит ли юзер сейчас в закрытой группе CLUB_CHAT_ID).
    """
    if user_id == ADMIN_ID:
        return True, "ok"

    now = int(time.time())

    # Шаг 1: Если боты лежат в одной папке, проверяем, не "пробник" ли это на 1 часе
    if os.path.exists(DB_NAME):
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'") as c:
                    if await c.fetchone():
                        async with db.execute("SELECT sub_end, total_paid FROM users WHERE user_id = ?", (user_id,)) as cursor:
                            row = await cursor.fetchone()
                            if row:
                                sub_end, total_paid = row[0], (row[1] or 0)
                                if sub_end > now and total_paid == 0:
                                    return False, "trial"
        except Exception as e:
            print(f"Инфо проверки БД: {e}")

    # Шаг 2: ПЛАН Б — Прямая проверка через Telegram API (состоит ли человек в чате клуба)
    try:
        member = await bot.get_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
        if member.status in ("member", "administrator", "creator"):
            return True, "ok"
        elif member.status == "restricted" and getattr(member, "is_member", False):
            return True, "ok"
    except Exception as e:
        print(f"Ошибка проверки get_chat_member для {user_id}: {e}")

    return False, "no_sub"

# ================= КЛАВИАТУРЫ =================
def suggest_category_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🩸 Разбор ситуаций (SOS)", callback_data="cat_sos")],
        [InlineKeyboardButton(text="⚔️ Отчет по челленджу", callback_data="cat_challenge")],
        [InlineKeyboardButton(text="🤝 Анкета: Связи и Свои люди", callback_data="cat_network")],
        [InlineKeyboardButton(text="📖 Книжный арсенал", callback_data="cat_books")],
    ])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к выбору категории", callback_data="back_to_cats")]
    ])

def admin_review_kb(sugg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"adm_pub_{sugg_id}")],
        [InlineKeyboardButton(text="✏️ С моим разбором", callback_data=f"adm_com_{sugg_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_rej_{sugg_id}")]
    ])

# ================= УНИВЕРСАЛЬНАЯ ОТПРАВКА В ТОПИК (БЕЗ ОШИБОК ЛИМИТА 1024 СИМВОЛА) =================
async def publish_to_club(topic_id: Optional[int], full_text: str, media_id: Optional[str]):
    if media_id:
        if len(full_text) <= 1024:
            await bot.send_photo(
                chat_id=CLUB_CHAT_ID,
                photo=media_id,
                caption=full_text,
                message_thread_id=topic_id,
                parse_mode="HTML"
            )
        else:
            # Если текст + разбор длиннее 1024 символов, отправляем фото, а следом полный текст
            await bot.send_photo(
                chat_id=CLUB_CHAT_ID,
                photo=media_id,
                message_thread_id=topic_id
            )
            await bot.send_message(
                chat_id=CLUB_CHAT_ID,
                text=full_text[:4096],
                message_thread_id=topic_id,
                parse_mode="HTML"
            )
    else:
        await bot.send_message(
            chat_id=CLUB_CHAT_ID,
            text=full_text[:4096],
            message_thread_id=topic_id,
            parse_mode="HTML"
        )

# ================= ЛОГИКА ПОЛЬЗОВАТЕЛЯ =================
MAIN_MENU_TEXT = "\n".join([
    "🏴 <b>ПРЕДЛОЖКА ОРДЕНА AURA</b>",
    "",
    "Здесь ты можешь анонимно отправить историю на разбор, скинуть отчет по челленджу или опубликовать анкету.",
    "",
    "Выбери нужную категорию ниже:"
])

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not message.from_user:
        return
    user_id = message.from_user.id

    has_access, reason = await check_active_sub(user_id)
    if not has_access:
        if reason == "trial":
            await message.answer(
                "⏳ <b>На пробном часе предложка закрыта.</b>\n\n"
                "Отправлять личные ситуации на разбор и публиковать анкеты могут только полноправные резиденты Ордена AURA.\n"
                "Оформи полную подписку в главном боте.",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                "❌ <b>Доступ закрыт. Ты не состоишь в закрытом клубе AURA.</b>\n\n"
                "Оформи подписку и вступи в группу через главного бота, чтобы отправлять кейсы на разбор.",
                parse_mode="HTML"
            )
        return

    if os.path.exists(MAIN_IMAGE):
        await message.answer_photo(FSInputFile(MAIN_IMAGE), caption=MAIN_MENU_TEXT, reply_markup=suggest_category_kb(), parse_mode="HTML")
    else:
        await message.answer(MAIN_MENU_TEXT, reply_markup=suggest_category_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "back_to_cats")
async def cb_back_to_cats(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=MAIN_MENU_TEXT, reply_markup=suggest_category_kb(), parse_mode="HTML")
        else:
            await call.message.edit_text(text=MAIN_MENU_TEXT, reply_markup=suggest_category_kb(), parse_mode="HTML")
    except TelegramBadRequest:
        await call.message.answer(MAIN_MENU_TEXT, reply_markup=suggest_category_kb(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("cat_"))
async def suggest_category_chosen(call: CallbackQuery, state: FSMContext):
    has_access, reason = await check_active_sub(call.from_user.id)
    if not has_access:
        msg = "На пробном часе предложка недоступна!" if reason == "trial" else "❌ Доступ закрыт. Ты не в клубе."
        await call.answer(msg, show_alert=True)
        return

    cat_id = call.data.split("_")[1]
    cat_names = {
        "sos": "🩸 Разбор ситуаций (SOS)", 
        "challenge": "⚔️ Челленджи и Режим", 
        "network": "🤝 Связи и Свои люди", 
        "books": "📖 Книжный арсенал и Саммари"
    }
    
    await state.update_data(category=cat_id, cat_name=cat_names[cat_id])
    
    text = "\n".join([
        f"📝 <b>Категория: {cat_names[cat_id]}</b>",
        "",
        "Отправь текст своего сообщения. Опиши ситуацию сухо и по фактам. Замажь чужие фамилии и лица, если отправляешь скриншоты.",
        "",
        "<b>Важно:</b> Прикрепляй текст и фото в ОДНОМ сообщении."
    ])
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=text, reply_markup=cancel_kb(), parse_mode="HTML")
        else:
            await call.message.edit_text(text=text, reply_markup=cancel_kb(), parse_mode="HTML")
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=cancel_kb(), parse_mode="HTML")

    await state.set_state(SuggestionState.waiting_for_content)
    await call.answer()

@dp.message(SuggestionState.waiting_for_content)
async def receive_suggestion(message: Message, state: FSMContext):
    if not message.from_user:
        return
    has_access, _ = await check_active_sub(message.from_user.id)
    if not has_access:
        await state.clear()
        await message.answer("❌ Твой доступ в клуб истек.")
        return

    data = await state.get_data()
    cat_id = data.get("category")
    cat_name = data.get("cat_name")
    
    if not cat_id:
        await message.answer("Категория не выбрана. Напиши /start")
        return

    text_content = message.html_text or ""
    media_id = None
    
    if message.photo:
        media_id = message.photo[-1].file_id
    
    if not text_content.strip():
        await message.answer("⚠️ Пожалуйста, отправь текст (можно вместе с фото). Одно фото без описания не принимается.")
        return

    now = int(time.time())
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "Без юзернейма"
    
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "INSERT INTO suggestions (user_id, category, text_content, media_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, cat_id, text_content, media_id, now)
            )
            sugg_id = cursor.lastrowid
            await db.commit()

        await message.answer(
            "✅ <b>Принято.</b> Твой материал передан в штаб Ордена. Ожидай публикации в соответствующей ветке клуба.",
            parse_mode="HTML"
        )
        await state.clear()

        # Пересылка админу
        admin_msg = (
            f"📨 <b>Новая заявка (#{sugg_id})</b>\n"
            f"Категория: <b>{cat_name}</b>\n"
            f"От: <code>{user_id}</code> ({username})\n\n"
            f"{text_content}"
        )
        if media_id:
            if len(admin_msg) <= 1024:
                await bot.send_photo(chat_id=ADMIN_ID, photo=media_id, caption=admin_msg, reply_markup=admin_review_kb(sugg_id), parse_mode="HTML")
            else:
                await bot.send_photo(chat_id=ADMIN_ID, photo=media_id)
                await bot.send_message(chat_id=ADMIN_ID, text=admin_msg[:4096], reply_markup=admin_review_kb(sugg_id), parse_mode="HTML")
        else:
            await bot.send_message(chat_id=ADMIN_ID, text=admin_msg[:4096], reply_markup=admin_review_kb(sugg_id), parse_mode="HTML")

    except Exception as e:
        await message.answer("⚠️ Произошла ошибка при отправке. Попробуй позже.")
        print(f"Ошибка сохранения предложки: {e}")

# ================= АДМИН-СТАТИСТИКА ПРЕДЛОЖКИ =================
@dp.message(Command("admin"))
async def cmd_admin_stats(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM suggestions") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM suggestions WHERE status = 'pending'") as c:
            pending = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM suggestions WHERE status = 'published'") as c:
            published = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM suggestions WHERE status = 'rejected'") as c:
            rejected = (await c.fetchone())[0]

    text = "\n".join([
        "🏴 <b>ШТАБ ПРЕДЛОЖКИ AURA</b>",
        "",
        f"📥 Всего заявок: <b>{total}</b>",
        f"⏳ Ожидают решения: <b>{pending}</b>",
        f"✅ Опубликовано: <b>{published}</b>",
        f"❌ Отклонено: <b>{rejected}</b>"
    ])
    await message.answer(text, parse_mode="HTML")

# ================= ЛОГИКА МОДЕРАЦИИ АДМИНА =================
@dp.callback_query(F.data.startswith("adm_rej_"))
async def admin_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sugg_id = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM suggestions WHERE id = ?", (sugg_id,)) as cursor:
            row = await cursor.fetchone()
        await db.execute("UPDATE suggestions SET status = 'rejected' WHERE id = ?", (sugg_id,))
        await db.commit()
    
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("❌ Заявка отклонена")

@dp.callback_query(F.data.startswith("adm_pub_"))
async def admin_publish(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sugg_id = int(call.data.split("_")[2])
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, category, text_content, media_id FROM suggestions WHERE id = ?", (sugg_id,)) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        return await call.answer("Заявка не найдена в базе", show_alert=True)
        
    author_id, cat_id, text, media_id = row
    topic_id = TOPICS.get(cat_id)
    
    headers = {
        "sos": "🩸 <b>АНОНИМНЫЙ РАЗБОР (SOS)</b>\n\n",
        "challenge": "⚔️ <b>ОТЧЕТ БОЙЦА ОРДЕНА</b>\n\n",
        "network": "🤝 <b> БАЗА СВЯЗЕЙ AURA</b>\n\n",
        "books": "📖 <b>КНИЖНЫЙ АРСЕНАЛ</b>\n\n"
    }
    header = headers.get(cat_id, "")
    full_text = f"{header}{text}"

    try:
        await publish_to_club(topic_id, full_text, media_id)
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE suggestions SET status = 'published' WHERE id = ?", (sugg_id,))
            await db.commit()
            
        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("✅ Успешно опубликовано в группу!")
        try:
            await bot.send_message(author_id, "✅ <b>Твой пост опубликован в закрытой группе AURA!</b>", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        await call.answer(f"Ошибка публикации: {e}", show_alert=True)

@dp.callback_query(F.data.startswith("adm_com_"))
async def admin_comment_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    sugg_id = int(call.data.split("_")[2])
    await state.update_data(sugg_id=sugg_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cancel_com")]
    ])
    await call.message.answer(
        f"✏️ Отправь свой разбор/комментарий для заявки <b>#{sugg_id}</b>.\nОн будет прикреплен снизу к посту и сразу опубликован в тему.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(SuggestionState.waiting_for_admin_comment)
    await call.answer()

@dp.callback_query(F.data == "adm_cancel_com")
async def admin_cancel_comment(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Добавление комментария отменено.")
    await call.answer()

@dp.message(SuggestionState.waiting_for_admin_comment)
async def admin_comment_receive(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    sugg_id = data.get("sugg_id")
    
    if not sugg_id:
        return await message.answer("Ошибка: ID заявки не найден.")

    admin_comment = message.html_text or message.text or ""
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, category, text_content, media_id FROM suggestions WHERE id = ?", (sugg_id,)) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        await state.clear()
        return await message.answer("Заявка не найдена в базе.")
        
    author_id, cat_id, text, media_id = row
    topic_id = TOPICS.get(cat_id)
    
    headers = {
        "sos": "🩸 <b>АНОНИМНЫЙ РАЗБОР (SOS)</b>\n\n",
        "challenge": "⚔️ <b>ОТЧЕТ БОЙЦА ОРДЕНА</b>\n\n",
        "network": "🤝 <b>БАЗА СВЯЗЕЙ AURA</b>\n\n",
        "books": "📖 <b>КНИЖНЫЙ АРСЕНАЛ</b>\n\n"
    }
    header = headers.get(cat_id, "")
    full_text = f"{header}{text}\n\n— — — — — — —\n🏴 <b>Разбор от AURA:</b>\n{admin_comment}"

    try:
        await publish_to_club(topic_id, full_text, media_id)
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE suggestions SET status = 'published' WHERE id = ?", (sugg_id,))
            await db.commit()
            
        await message.answer(f"✅ Заявка #{sugg_id} успешно опубликована вместе с твоим разбором!")
        try:
            await bot.send_message(author_id, "🏴 <b>Твоя ситуация разобрана и опубликована в закрытой группе AURA!</b>", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        await message.answer(f"❌ Ошибка публикации: {e}")
        
    await state.clear()

async def main():
    await init_db()
    print("✅ Бот-предложка AURA запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
