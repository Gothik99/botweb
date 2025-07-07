# admin.py
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
import random
import string
import os

from app_config import app_conf # Главный импорт
import db_helpers
from x_ui_manager import xui_manager_instance
from loguru import logger
from subscription_manager import get_subscription_link, grant_subscription

class AdminStates(StatesGroup):
    waiting_for_user_id_add_sub = State()
    waiting_for_days_add_sub = State()
    waiting_for_user_id_del_sub = State()
    waiting_for_user_id_info = State()
    waiting_for_user_id_delete = State()
    waiting_for_user_id_subscription = State()
    waiting_for_broadcast_message = State()
    waiting_for_renewal_broadcast = State()
    waiting_for_renewal_period = State()
    waiting_for_renewal_price = State()

def is_admin(user_id: int) -> bool:
    admin_ids = app_conf.get('admin_ids', [])
    return user_id in admin_ids

def get_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users_menu"),
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats_overview")
    )
    builder.row(
        InlineKeyboardButton(text="🖥 Серверы", callback_data="admin_servers_status")
    )
    builder.row(
        InlineKeyboardButton(text="📢 Отправить новость", callback_data="admin_broadcast")
    )
    # Новая кнопка для перезагрузки настроек
    builder.row(
        InlineKeyboardButton(text="🔄 Перезагрузить настройки", callback_data="admin_reload_settings")
    )
    # Кнопка для возврата в пользовательское меню
    builder.row(
        InlineKeyboardButton(text="🏠 На главную", callback_data="back_to_main")
    )
    return builder.as_markup()
    
def get_admin_promo_codes_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_promo_create"),
        InlineKeyboardButton(text="📋 Список кодов", callback_data="admin_promo_list_all_0")
    )
    builder.row(
        InlineKeyboardButton(text="📥 Выгрузить коды", callback_data="admin_promo_export")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_panel_main"))
    return builder.as_markup()

def get_promo_codes_list_keyboard(current_page: int, total_pages: int, status: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    filter_buttons = [
        InlineKeyboardButton(text="Все" + (" ✅" if status == 'all' else ""), callback_data="admin_promo_list_all_0"),
        InlineKeyboardButton(text="Активные" + (" ✅" if status == 'active' else ""), callback_data="admin_promo_list_active_0"),
        InlineKeyboardButton(text="Использованные" + (" ✅" if status == 'inactive' else ""), callback_data="admin_promo_list_inactive_0")
    ]
    builder.row(*filter_buttons)
    
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"admin_promo_list_{status}_{current_page-1}"))
    
    page_info_text = "Страница"
    if total_pages > 1:
        page_info_text = f"📄 {current_page+1}/{total_pages}"
    nav_buttons.append(InlineKeyboardButton(text=page_info_text, callback_data="admin_ignore"))

    if current_page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"admin_promo_list_{status}_{current_page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.row(InlineKeyboardButton(text="🔙 В меню промокодов", callback_data="admin_promo_codes_menu"))
    return builder.as_markup()

def get_admin_users_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_users_list_page_0"))
    builder.row(InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_panel_main"))
    return builder.as_markup()

def get_users_list_keyboard(current_page: int, total_pages: int, per_page: int, users_data: List[tuple]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user_tuple in users_data:
        telegram_id = user_tuple[0]
        builder.row(InlineKeyboardButton(text=f"👤 Инфо о {telegram_id}", callback_data=f"admin_user_info_{telegram_id}"))
    
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"admin_users_list_page_{current_page-1}"))
    
    page_info_text = "Страница"
    if total_pages > 1:
         page_info_text = f"📄 {current_page+1}/{total_pages}"
    nav_buttons.append(InlineKeyboardButton(text=page_info_text, callback_data="admin_ignore"))

    if current_page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"admin_users_list_page_{current_page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.row(InlineKeyboardButton(text="🔙 В меню пользователей", callback_data="admin_users_menu"))
    return builder.as_markup()

async def get_users_list_text_and_keyboard(page: int = 0, per_page: int = 5) -> tuple[str, InlineKeyboardMarkup]:
    total_users_count = await db_helpers.get_users_count()
    if total_users_count == 0:
        return "👥 Пользователей пока нет.", get_users_list_keyboard(0, 0, per_page, [])

    total_pages = (total_users_count + per_page - 1) // per_page
    page = min(max(0, page), total_pages - 1)
    
    users_data = await db_helpers.get_users_list(limit=per_page, offset=page * per_page)
    
    text = f"👥 <b>Список пользователей (Страница {page+1}/{total_pages}):</b>\n\n"
    
    if not users_data:
        text += "На этой странице нет пользователей."
    else:
        trial_days = app_conf.get('trial_days', 3)
        for user_tuple in users_data:
            telegram_id, username, sub_end_date_str, is_trial_used, server_id = user_tuple
            status_emoji = "❌"
            sub_info = "Нет подписки"
            if sub_end_date_str:
                try:
                    sub_end_date = datetime.fromisoformat(sub_end_date_str)
                    
                    # --- Устойчивость к старым данным ---
                    # Если дата в БД "наивная" (без таймзоны), делаем ее "aware",
                    # чтобы избежать ошибок сравнения.
                    if sub_end_date.tzinfo is None:
                        sub_end_date = sub_end_date.astimezone()
                    # ------------------------------------

                    now_aware = datetime.now(sub_end_date.tzinfo)
                    if sub_end_date > now_aware:
                        status_emoji = "✅"
                        sub_info = f"до {sub_end_date.strftime('%d.%m.%y %H:%M')}"
                        # Проверяем, является ли подписка недавним триалом
                        # Для этого сравниваем aware-даты
                        if is_trial_used and (sub_end_date - now_aware).days < trial_days + 1:
                            status_emoji = "🎁"
                    else:
                        sub_info = f"истекла {sub_end_date.strftime('%d.%m.%y')}"
                except ValueError:
                    sub_info = "Ошибка даты"
            
            username_display = username or "Без имени"
            server_info = f"Srv: {server_id}" if server_id else "Srv: -"
            
            text += f"👤 <code>{telegram_id}</code> - {username_display}\n"
            text += f"   {status_emoji} {sub_info} ({server_info})\n\n"
    
    text += f"\nВсего пользователей: {total_users_count}"
    keyboard = get_users_list_keyboard(page, total_pages, per_page, users_data)
    
    return text, keyboard

async def cmd_admin_panel(message_or_query: Message | CallbackQuery):
    user_id = message_or_query.from_user.id
    if not is_admin(user_id):
        if isinstance(message_or_query, Message):
            await message_or_query.answer("⛔️ У вас нет доступа к админ-панели.")
        elif isinstance(message_or_query, CallbackQuery):
            await message_or_query.answer("⛔️ Нет доступа", show_alert=True)
        return
    
    text = "👨‍💼 <b>Панель администратора</b>\n\nВыберите действие:"
    kbd = get_admin_keyboard()

    if isinstance(message_or_query, Message):
        await message_or_query.answer(text, reply_markup=kbd)
    elif isinstance(message_or_query, CallbackQuery):
        try:
            await message_or_query.message.edit_text(text, reply_markup=kbd)
            await message_or_query.answer()
        except Exception as e:
            logger.debug(f"Ошибка при редактировании сообщения в cmd_admin_panel: {e}")
            await message_or_query.answer()

async def get_overall_stats_text() -> str:
    total_users = await db_helpers.get_total_users_count()
    active_subs = await db_helpers.get_active_subscriptions_count()
    trial_users = await db_helpers.get_trial_users_count()
    total_payments = await db_helpers.get_total_payments_count()
    successful_payments = await db_helpers.get_successful_payments_count()
    total_amount = await db_helpers.get_total_payments_amount()
    activated_promo_codes = await db_helpers.get_activated_promo_codes_count()

    servers_summary = []
    active_servers_count = 0
    total_xui_clients = 0
    xui_servers = app_conf.get('xui_servers', [])

    if not xui_servers:
        servers_summary.append("Нет сконфигурированных X-UI серверов.")
    else:
        for server_conf in xui_servers:
            try:
                api_client = await xui_manager_instance.get_client(server_conf)
                if api_client:
                    num_clients = await xui_manager_instance.get_active_clients_count_for_inbound(server_conf)
                    num_clients_str = str(num_clients) if num_clients is not None else "N/A"
                    servers_summary.append(f"  - {server_conf['name']}: ✅ Онлайн, клиенты: {num_clients_str}")
                    active_servers_count += 1
                    if num_clients is not None: total_xui_clients += num_clients
                else:
                    servers_summary.append(f"  - {server_conf['name']}: ❌ Оффлайн")
            except Exception as e:
                servers_summary.append(f"  - {server_conf['name']}: ⚠️ Ошибка ({e})")
    
    servers_text = "\n".join(servers_summary)

    return (
        "📊 <b>Общая статистика бота:</b>\n\n"
        f"<b>Пользователи:</b>\n"
        f"  👥 Всего: {total_users}\n"
        f"  ✅ Активных подписок (в БД): {active_subs}\n"
        f"  🎁 Использовали триал: {trial_users}\n"
        f"  🎟️ Активировано промокодов: {activated_promo_codes}\n\n"
        f"<b>Платежи:</b>\n"
        f"  💳 Всего записей: {total_payments}\n"
        f"  💸 Успешных: {successful_payments}\n"
        f"  💰 Общая сумма (успешных): {total_amount:.2f} {app_conf.get('subscription_currency', 'RUB')}\n\n"
        f"<b>Серверы X-UI ({active_servers_count}/{len(xui_servers)} онлайн):</b>\n"
        f"{servers_text}\n"
        f"  Σ Клиентов на X-UI (активных): {total_xui_clients}"
    )

async def get_server_detailed_status_text() -> str:
    status_text = "🖥 <b>Детальный статус серверов X-UI:</b>\n\n"
    xui_servers = app_conf.get('xui_servers', [])
    if not xui_servers:
        return status_text + "Нет сконфигурированных X-UI серверов."

    for server_conf in xui_servers:
        try:
            status_text += f"<b>📍 Сервер: {server_conf.get('name', 'N/A')} (ID: {server_conf.get('id', 'N/A')})</b>\n"
            if not all(key in server_conf for key in ['url', 'port']):
                status_text += "  ⚠️ Ошибка конфигурации: отсутствуют url или port\n\n"
                continue

            try:
                stats = await xui_manager_instance.get_server_stats(server_conf)
                if stats:
                    xui_url = f"https://{server_conf['url']}:{server_conf['port']}"
                    if server_conf.get('secret_path'):
                        xui_url += f"/{server_conf['secret_path'].strip('/')}"
                    status_text += (
                        f"  Статус: ✅ Онлайн\n"
                        f"  CPU: {stats.get('cpu_usage', 'N/A')}%\n" 
                        f"  RAM: {stats.get('memory_usage', 'N/A')}%\n"
                        f"  Диск: {stats.get('disk_usage', 'N/A')}%\n"
                        f"  Активных клиентов: {stats.get('active_users', 'N/A')}\n"
                        f"  X-UI панель: <a href='{xui_url}'>{xui_url}</a>\n\n"
                    )
                else:
                    status_text += "  Статус: ❌ Оффлайн или ошибка получения данных\n\n"
            except Exception as e:
                logger.error(f"Ошибка при получении статистики сервера {server_conf.get('name', 'Unknown')}: {str(e)}")
                status_text += f"  Статус: ⚠️ Ошибка получения данных: {str(e)}\n\n"
        except Exception as e:
            logger.error(f"Ошибка при обработке сервера {server_conf.get('name', 'Unknown')}: {str(e)}")
            status_text += f"  Статус: ⚠️ Ошибка обработки: {str(e)}\n\n"
    
    return status_text

async def get_user_info_text(user_id: int) -> str:
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        return f"❌ Пользователь с ID <code>{user_id}</code> не найден в базе данных бота."
    
    tg_id, u_name, uuid, email, sub_end_str, trial_used, srv_id, *_ = user_data
    text = (
        f"👤 <b>Информация о пользователе</b> <code>{tg_id}</code>\n"
        f"Имя: {u_name or 'Не указано'}\n"
        f"Пробный период: {'Использован' if trial_used else 'Не использован'}\n"
    )

    activated_code = await db_helpers.get_activated_code_for_user(tg_id)
    if activated_code:
        text += f"Активировал промокод: <code>{activated_code}</code>\n"
    text += "\n"

    active_sub_db = await db_helpers.get_active_subscription(tg_id)
    if uuid and email and sub_end_str and srv_id:
        text += f"🔑 <b>Данные X-UI (из БД):</b>\n"
        text += f"  UUID: <code>{uuid}</code>\n"
        text += f"  Email: <code>{email}</code>\n"
        text += f"  Сервер ID: {srv_id}\n"
        
        sub_end_date_dt = datetime.fromisoformat(sub_end_str)
        status = "Активна ✅" if active_sub_db else "Истекла/Нет ❌"
        text += f"  Подписка (в БД): {status} до {sub_end_date_dt.strftime('%d.%m.%Y %H:%M %Z')}\n"
        
        server_conf = await db_helpers.get_server_config(srv_id)
        if server_conf:
            sub_link = get_subscription_link(server_conf, uuid)
            text += f"  Ссылка: <code>{sub_link}</code>\n"
        text += "\n"
    elif active_sub_db:
        text += f"⚠️ В основной записи пользователя отсутствуют полные данные X-UI, но найдена активная подписка:\n"
        text += f"   UUID: <code>{active_sub_db.get('xui_client_uuid','N/A')}</code>\n"
        text += f"   Email: <code>{active_sub_db.get('xui_client_email','N/A')}</code>\n"
        text += f"   Сервер ID: {active_sub_db.get('current_server_id','N/A')}\n"
        text += f"   Действует до: {active_sub_db['subscription_end_date'].strftime('%d.%m.%Y %H:%M %Z')}\n\n"
    else:
        text += "🤷 Нет данных о подписке X-UI в базе.\n\n"
        
    payments = await db_helpers.get_user_payments(tg_id)
    if payments:
        text += "💳 <b>История платежей (последние 5):</b>\n"
        for p in payments[:5]:
            p_created_at = datetime.fromisoformat(p[5]).strftime('%d.%m.%y %H:%M')
            meta_short = "Да" if p[6] else "Нет"
            text += f"  - {p[0][:8]}.. ({p[2]} {p[3]}) Статус: {p[4]}, Дата: {p_created_at}, Мета: {meta_short}\n"
    else:
        text += "💳 Платежей не найдено.\n"
        
    return text

def register_admin_handlers(dp: Dispatcher):
    
    # --- Новая команда для перезагрузки настроек ---
    @dp.message(Command("reload_settings"))
    async def cmd_reload_settings_msg(message: Message):
        if not is_admin(message.from_user.id): return
        await app_conf.load_settings()
        await message.answer("✅ Настройки и тексты успешно перезагружены из базы данных!")

    @dp.callback_query(F.data == "admin_reload_settings")
    async def cq_reload_settings(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await app_conf.load_settings()
        await query.answer("✅ Настройки и тексты успешно перезагружены из базы данных!", show_alert=True)
        # Обновим админ-панель на всякий случай
        await cmd_admin_panel(query)
    
    @dp.message(Command("admin"))
    async def cmd_show_admin_panel_msg(message: Message):
        await cmd_admin_panel(message)

    @dp.callback_query(F.data == "admin_panel_main")
    async def cq_show_admin_panel_cb(query: CallbackQuery):
        await cmd_admin_panel(query)

    @dp.callback_query(F.data == "admin_stats_overview")
    async def cq_admin_stats_overview(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        stats_text = await get_overall_stats_text()
        await query.message.edit_text(stats_text, reply_markup=get_admin_keyboard())
        await query.answer()
    
    @dp.callback_query(F.data == "admin_servers_status")
    async def cq_admin_servers_status(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        try:
            status_text = await get_server_detailed_status_text()
            keyboard = get_admin_keyboard()
            await query.message.edit_text(text=status_text, reply_markup=keyboard, parse_mode="HTML")
            await query.answer()
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                await query.answer("Статус серверов актуален")
            else:
                logger.error(f"Ошибка при обновлении статуса серверов: {e}")
                await query.answer("❌ Ошибка при обновлении статуса", show_alert=True)
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обновлении статуса серверов: {e}")
            await query.answer("❌ Произошла ошибка", show_alert=True)

    @dp.callback_query(F.data == "admin_users_menu")
    async def cq_admin_users_menu(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await query.message.edit_text("Меню управления пользователями:", reply_markup=get_admin_users_menu_keyboard())
        await query.answer()

    @dp.callback_query(F.data.startswith("admin_users_list_page_"))
    async def cq_admin_users_list_page(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        try:
            page = int(query.data.split("_")[-1])
        except: page = 0
        text, keyboard = await get_users_list_text_and_keyboard(page=page)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer()

    @dp.callback_query(F.data.startswith("admin_user_info_"))
    async def cq_admin_user_info_from_list(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        try: user_id = int(query.data.split("_")[-1])
        except (ValueError, IndexError): return await query.answer("❌ Ошибка: неверный ID пользователя", show_alert=True)
        await state.clear()
        info_text = await get_user_info_text(user_id)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 К списку пользователей", callback_data="admin_users_list_page_0"))
        await query.message.edit_text(info_text, reply_markup=builder.as_markup())
        await query.answer()

    @dp.callback_query(F.data == "admin_promo_codes_menu")
    async def cq_admin_promo_codes_menu(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await query.message.edit_text(app_conf.get('admin_text_promo_codes_menu', ''), reply_markup=get_admin_promo_codes_menu_keyboard())
        await query.answer()

    @dp.callback_query(F.data == "admin_promo_create")
    async def cq_admin_promo_create(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        chars = string.ascii_uppercase + string.digits
        while True:
            new_code = "BVPN-" + ''.join(random.choice(chars) for _ in range(8))
            if not await db_helpers.get_promo_code(new_code):
                break
        await db_helpers.add_promo_code(new_code)
        await query.message.edit_text(
            app_conf.get('admin_text_promo_code_created', '').format(code=new_code),
            reply_markup=get_admin_promo_codes_menu_keyboard()
        )
        await query.answer("Промокод создан!")

    @dp.callback_query(F.data.startswith("admin_promo_list_"))
    async def cq_admin_promo_list(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        parts = query.data.split('_')
        status = parts[3]
        try: page = int(parts[4])
        except (ValueError, IndexError): page = 0
            
        per_page = 10
        total_codes = await db_helpers.get_promo_codes_count(status)
        text = f"🎟️ <b>Список промокодов (фильтр: {status}, стр. {page + 1})</b>\n\n"
        
        if total_codes == 0:
            text += "Промокодов по этому фильтру нет."
            keyboard = get_promo_codes_list_keyboard(0, 0, status)
        else:
            total_pages = (total_codes + per_page - 1) // per_page
            page = min(max(0, page), total_pages - 1)
            codes_data = await db_helpers.get_promo_codes_list(status, per_page, page * per_page)
            for code, is_active, user_id, activated_at in codes_data:
                status_text = "✅ Активен" if is_active else "❌ Использован"
                text += f"<code>{code}</code> - {status_text}\n"
                if not is_active and user_id:
                    try:
                        act_date = datetime.fromisoformat(activated_at).strftime('%d.%m.%y %H:%M')
                        text += f"   └ Кем: <code>{user_id}</code>, когда: {act_date}\n"
                    except:
                        text += f"   └ Кем: <code>{user_id}</code>\n"
            text += f"\nВсего кодов: {total_codes}"
            keyboard = get_promo_codes_list_keyboard(page, total_pages, status)
            
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer()

    @dp.callback_query(F.data == "admin_promo_export")
    async def cq_admin_promo_export(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        try:
            codes_data = await db_helpers.get_promo_codes_list('all', 1000, 0)
            if not codes_data:
                return await query.answer("❌ Нет промокодов для выгрузки", show_alert=True)

            filename = f"promo_codes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            file_content = "=== Промокоды ===\n\n"
            for code, is_active, user_id, activated_at in codes_data:
                status = "Активен" if is_active else "Использован"
                file_content += f"Код: {code}\n"
                file_content += f"Статус: {status}\n"
                if not is_active and user_id:
                    try:
                        act_date = datetime.fromisoformat(activated_at).strftime('%d.%m.%y %H:%M')
                        file_content += f"Активирован: {user_id} ({act_date})\n"
                    except:
                        file_content += f"Активирован: {user_id}\n"
                file_content += "\n"
            with open(filename, 'w', encoding='utf-8') as f: f.write(file_content)
            try:
                await query.message.answer_document(document=FSInputFile(filename), caption="📥 Выгрузка промокодов")
                await query.answer("✅ Файл с промокодами отправлен")
            finally:
                if os.path.exists(filename): os.remove(filename)
        except Exception as e:
            logger.error(f"Ошибка при выгрузке промокодов: {e}")
            await query.answer("❌ Ошибка при создании файла", show_alert=True)

    @dp.message(Command("cancel"), StateFilter(AdminStates))
    async def cancel_admin_action(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        current_state = await state.get_state()
        if current_state is None: return
        logger.info(f"Админ {message.from_user.id} отменил действие в состоянии {current_state}")
        await state.clear()
        await message.answer("Действие отменено. Возврат в админ-панель.", reply_markup=get_admin_keyboard())

    @dp.callback_query(F.data == "admin_ignore")
    async def cq_admin_ignore(query: CallbackQuery):
        await query.answer()

    @dp.callback_query(F.data.startswith("admin_user_info_"))
    async def cq_admin_user_info_from_list(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        try: user_id = int(query.data.split("_")[-1])
        except ValueError: return await query.answer("❌ Ошибка: неверный ID пользователя", show_alert=True)
        info_text = await get_user_info_text(user_id)
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="🔙 К списку пользователей", callback_data="admin_users_menu"),
            InlineKeyboardButton(text="🏠 В админ-панель", callback_data="admin_panel_main")
        )
        await query.message.edit_text(info_text, reply_markup=builder.as_markup())
        await query.answer()

    @dp.callback_query(F.data == "admin_broadcast")
    async def cq_admin_broadcast(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await query.message.edit_text(
            "📢 <b>Отправка новости всем пользователям</b>\n\nВведите текст новости...",
            reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_cancel_broadcast").as_markup()
        )
        await state.set_state(AdminStates.waiting_for_broadcast_message)
        await query.answer()

    @dp.callback_query(F.data == "admin_cancel_broadcast")
    async def cq_admin_cancel_broadcast(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await state.clear()
        await query.message.edit_text("❌ Отправка новости отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
        await query.answer()

    @dp.message(AdminStates.waiting_for_broadcast_message)
    async def process_broadcast_message(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        if message.text == "/cancel":
            await state.clear()
            await message.answer("❌ Отправка новости отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
            return

        users = await db_helpers.get_all_users()
        total_users = len(users)
        if total_users == 0:
            await message.answer("❌ Нет пользователей для отправки новости")
            await state.clear()
            return

        status_message = await message.answer(f"📢 Начинаю отправку новости {total_users} пользователям...")
        success_count, error_count = 0, 0
        for user in users:
            user_id = user[0]
            username = user[1]
            try:
                await message.bot.send_message(chat_id=user_id, text=message.text, parse_mode="HTML")
                success_count += 1
            except Exception as e:
                logger.error(f"Ошибка отправки новости пользователю {user_id}: {e}")
                error_count += 1
            await asyncio.sleep(0.05)
        
        await status_message.edit_text(f"📢 Рассылка завершена!\n\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
        await state.clear()

    @dp.callback_query(F.data == "admin_renewal_broadcast")
    async def cq_admin_renewal_broadcast(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await query.message.edit_text("🎁 <b>Новость о скидке</b>\n\nВведите количество дней (напр., 30):", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_cancel_renewal_broadcast").as_markup())
        await state.set_state(AdminStates.waiting_for_renewal_period)
        await query.answer()

    @dp.message(AdminStates.waiting_for_renewal_period)
    async def process_renewal_period(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        if message.text == "/cancel":
            await state.clear()
            await message.answer("❌ Отправка отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
            return
        try:
            days = int(message.text.strip())
            if days <= 0: raise ValueError("Дни должны быть > 0")
            await state.update_data(renewal_days=days)
            await message.answer(f"Введите стоимость на {days} дней (напр., 299):", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_cancel_renewal_broadcast").as_markup())
            await state.set_state(AdminStates.waiting_for_renewal_price)
        except ValueError as e:
            await message.answer(f"❌ Неверное количество дней: {e}", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_cancel_renewal_broadcast").as_markup())

    @dp.message(AdminStates.waiting_for_renewal_price)
    async def process_renewal_price(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        if message.text == "/cancel":
            await state.clear()
            await message.answer("❌ Отправка отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
            return
        try:
            price = float(message.text.strip().replace(',', '.'))
            if price <= 0: raise ValueError("Цена должна быть > 0")
            data = await state.get_data()
            days = data['renewal_days']
            await message.answer(f"🎁 <b>Новость о продлении</b>\n\nПериод: {days} дн.\nСтоимость: {price:.2f} руб.\n\nВведите текст новости...", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_cancel_renewal_broadcast").as_markup())
            await state.update_data(renewal_price=price)
            await state.set_state(AdminStates.waiting_for_renewal_broadcast)
        except ValueError as e:
            await message.answer(f"❌ Неверная стоимость: {e}", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_cancel_renewal_broadcast").as_markup())

    @dp.message(AdminStates.waiting_for_renewal_broadcast)
    async def process_renewal_broadcast_message(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        if message.text == "/cancel":
            await state.clear()
            await message.answer("❌ Отправка отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
            return

        data = await state.get_data()
        days, price = data['renewal_days'], data['renewal_price']
        broadcast_text = message.text.replace('{days}', str(days)).replace('{price}', f"{price:.2f}")

        users = await db_helpers.get_all_users()
        total_users = len(users)
        if total_users == 0:
            await message.answer("❌ Нет пользователей для отправки")
            await state.clear()
            return

        status_message = await message.answer(f"🎁 Начинаю отправку новости о продлении {total_users} пользователям...")
        renewal_keyboard = InlineKeyboardBuilder()
        renewal_keyboard.row(InlineKeyboardButton(text=f"🔄 Продлить на {days} дней за {price:.2f} руб.", callback_data=f"renew_sub_{days}_{price}"))

        success_count, error_count = 0, 0
        for user in users:
            user_id = user[0]
            username = user[1]
            try:
                await message.bot.send_message(chat_id=user_id, text=broadcast_text, parse_mode="HTML", reply_markup=renewal_keyboard.as_markup())
                success_count += 1
            except Exception as e:
                logger.error(f"Ошибка отправки новости о продлении пользователю {user_id}: {e}")
                error_count += 1
            await asyncio.sleep(0.05)

        await status_message.edit_text(f"🎁 Рассылка завершена!\n\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
        await state.clear()

    @dp.callback_query(F.data == "admin_cancel_renewal_broadcast")
    async def cq_admin_cancel_renewal_broadcast(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await state.clear()
        await query.message.edit_text("❌ Отправка отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
        await query.answer()