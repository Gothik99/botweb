# main.py
# Основной файл Telegram-бота для управления VPN подписками через X-UI и YooKassa

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone 
import uuid as py_uuid
import json
from typing import Optional, Dict
import pytz

# Импортируем необходимые модули aiogram для работы с Telegram Bot API
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, StateFilter
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.markdown import hcode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramAPIError

# Импортируем YooKassa для работы с платежами
from yookassa import Configuration as YKConfig, Payment as YKPayment
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder

# Импортируем внутренние модули проекта
from app_config import app_conf # Менеджер настроек
import keyboards # Клавиатуры для Telegram
import db_helpers # Работа с базой данных
from x_ui_manager import xui_manager_instance # Работа с X-UI
import admin # Админские команды и обработчики
from subscription_manager import grant_subscription, get_subscription_link, get_server_config

from loguru import logger
import aiosqlite

# --- Инициализация бота и диспетчера ---
bot_token = os.getenv("BOT_TOKEN", app_conf.get('bot_token', ''))
bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage, bot=bot)

# Словарь для хранения активных задач проверки платежей
active_payment_checkers = {}

# --- Состояния FSM для aiogram ---
class PromoCodeActivation(StatesGroup):
    waiting_for_code = State()

class StepByStepGuide(StatesGroup):
    step1 = State()
    step2 = State()
    step3 = State()
    step4 = State()
    step5 = State()

# --- Вспомогательные функции ---
async def process_successful_payment(telegram_user_id: int, payment_id: str, payment_metadata: Optional[dict] = None):
    logger.info(f"Обработка успешного платежа {payment_id} для пользователя {telegram_user_id}")
    db_payment_info = await db_helpers.get_payment(payment_id)
    if db_payment_info and db_payment_info[4] == "succeeded":
        logger.info(f"Платеж {payment_id} уже был обработан как 'succeeded'.")
        # Повторно отправляем сообщение об успехе, если пользователь нажал кнопку проверки еще раз
        active_sub = await db_helpers.get_active_subscription(telegram_user_id)
        if active_sub:
            server_conf = await get_server_config(active_sub['current_server_id'])
            sub_link = "N/A"
            if server_conf and active_sub['xui_client_uuid']:
                sub_link = get_subscription_link(server_conf, active_sub['xui_client_uuid'])
            
            days_paid = app_conf.get('subscription_days', 30)
            if payment_metadata and 'subscription_days' in payment_metadata:
                days_paid = payment_metadata['subscription_days']

            expiry_date = active_sub['subscription_end_date']
            moscow = pytz.timezone('Europe/Moscow')
            local_expiry_date = expiry_date.astimezone(moscow) if expiry_date.tzinfo else expiry_date
            await bot.send_message(
                telegram_user_id,
                app_conf.get('text_payment_success').format(
                    days=days_paid, expiry_date=local_expiry_date.strftime('%d.%m.%Y %H:%M %Z'),
                    sub_link=hcode(sub_link) if sub_link != "N/A" else sub_link
                ),
                reply_markup=keyboards.get_back_to_main_keyboard()
            )
        return True

    await db_helpers.update_payment_status(payment_id, "succeeded")
    
    days_to_add = app_conf.get('subscription_days', 30)
    price_to_use = None
    limit_ip = 0
    if payment_metadata and 'subscription_days' in payment_metadata:
        days_to_add = payment_metadata['subscription_days']
        price_to_use = float(payment_metadata.get('price', 0)) if 'price' in payment_metadata else None
        # Получаем лимит устройств из тарифа
        active_tariffs = await db_helpers.get_active_tariffs()
        if price_to_use is not None:
            for tariff in active_tariffs:
                if tariff['days'] == days_to_add and float(tariff['price']) == price_to_use:
                    limit_ip = tariff.get('limit_ip', 0)
                    break
        else:
            for tariff in active_tariffs:
                if tariff['days'] == days_to_add:
                    limit_ip = tariff.get('limit_ip', 0)
                    break
    
    subscription_data = await grant_subscription(telegram_user_id, days_to_add, is_trial=False, limit_ip=limit_ip)
    
    if subscription_data:
        moscow = pytz.timezone('Europe/Moscow')
        local_expiry_date = subscription_data['expiry_date'].astimezone(moscow)
        
        # Отправляем основное сообщение об успешном платеже
        await bot.send_message(
            telegram_user_id,
            app_conf.get('text_payment_success').format(
                days=days_to_add, expiry_date=local_expiry_date.strftime('%d.%m.%Y %H:%M %Z'),
                sub_link=hcode(subscription_data['sub_link'])
            ),
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        
        return True
    else:
        logger.error(f"Не удалось выдать подписку после успешного платежа {payment_id}")
        await bot.send_message(telegram_user_id, app_conf.get('text_error_creating_user'))
        return False

async def show_main_menu(message_or_query: Message | CallbackQuery, edit_message: bool = False):
    user_id = message_or_query.from_user.id
    user_name = message_or_query.from_user.first_name

    await db_helpers.add_user(user_id, user_name) 
    user_db_data = await db_helpers.get_user(user_id)
    
    active_sub = await db_helpers.get_active_subscription(user_id)
    has_active_sub = active_sub is not None
    is_trial_used = bool(user_db_data[5]) if user_db_data else True 

    kbd = await keyboards.get_main_keyboard(not is_trial_used and not has_active_sub, has_active_sub)

    text_to_send = app_conf.get('text_welcome_message').format(user_name=user_name, project_name=app_conf.get('project_name'))
    
    if active_sub:
        server_conf = await get_server_config(active_sub['current_server_id'])
        sub_link = "N/A"
        if server_conf and active_sub['xui_client_uuid']:
            sub_link = get_subscription_link(server_conf, active_sub['xui_client_uuid'])

        expiry_date = active_sub['subscription_end_date']
        moscow = pytz.timezone('Europe/Moscow')
        local_expiry_date = expiry_date.astimezone(moscow) if expiry_date.tzinfo else expiry_date
        text_to_send += "\n\n" + app_conf.get('text_subscription_info').format(
            status="Активна ✅", expiry_date=local_expiry_date.strftime('%d.%m.%Y %H:%M %Z'),
            sub_link=hcode(sub_link) if sub_link != "N/A" else sub_link
        )

        # Лимит устройств теперь из БД
        limit_ip = active_sub.get('limit_ip', 0) if isinstance(active_sub, dict) else 0
        text_to_send += f"\n\n<b>Лимит устройств:</b> {limit_ip if limit_ip > 0 else 'Без лимита'}"
    elif is_trial_used and not has_active_sub:
         # Показываем стандартный текст для пользователей без активной подписки
         text_to_send += "\n\n" + app_conf.get('text_subscription_expired_main')
    elif not is_trial_used and not has_active_sub:
        text_to_send += "\n\n" + "🎁 Вы можете получить пробный период, если еще не использовали его, или приобрести подписку."

    target_message = message_or_query.message if isinstance(message_or_query, CallbackQuery) else message_or_query
    
    if edit_message and isinstance(message_or_query, CallbackQuery):
        try:
            await target_message.edit_text(text_to_send, reply_markup=kbd)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"Не удалось отредактировать сообщение для {user_id}: {e}. Отправка нового.")
                await bot.send_message(user_id, text_to_send, reply_markup=kbd)
    else:
        await target_message.answer(text_to_send, reply_markup=kbd)

    if isinstance(message_or_query, CallbackQuery):
        try: await message_or_query.answer()
        except: pass

@dp.message(CommandStart())
async def handle_start(message: Message):
    logger.info(f"Пользователь {message.from_user.id} ({message.from_user.username}) нажал /start")
    await db_helpers.add_user(message.from_user.id, message.from_user.first_name)
    user_db_data = await db_helpers.get_user(message.from_user.id)
    
    is_trial_used = bool(user_db_data[5]) if user_db_data else True
    active_sub = await db_helpers.get_active_subscription(message.from_user.id)
    
    if not is_trial_used and not active_sub:
        logger.info(f"Попытка выдать триал для нового пользователя {message.from_user.id}")
        waiting_msg = await message.answer("⏳ Идет регистрация пробного периода, пожалуйста подождите...")
        trial_days = app_conf.get('trial_days', 3)
        # limit_ip=1 для триала
        subscription_data = await grant_subscription(message.from_user.id, trial_days, is_trial=True, limit_ip=1)
        
        if subscription_data:
            moscow = pytz.timezone('Europe/Moscow')
            local_expiry_date = subscription_data['expiry_date'].astimezone(moscow)
            await waiting_msg.edit_text(
                app_conf.get('text_trial_success').format(
                    days=trial_days, sub_link=hcode(subscription_data['sub_link']),
                    expiry_date=local_expiry_date.strftime('%d.%m.%Y %H:%M %Z')
                ),
                reply_markup=keyboards.get_back_to_main_keyboard()
            )
            return 
        else:
            logger.error(f"Не удалось создать пробный XUI пользователя для {message.from_user.id} при /start")
            await waiting_msg.edit_text(app_conf.get('text_error_creating_user'), reply_markup=keyboards.get_back_to_main_keyboard())
    
    await show_main_menu(message)

@dp.callback_query(F.data == "back_to_main")
async def cq_back_to_main(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_menu(query, edit_message=True)

@dp.callback_query(F.data == "android_guide")
async def cq_android_guide(query: CallbackQuery):
    active_sub = await db_helpers.get_active_subscription(query.from_user.id)
    sub_link = "ВАША_ССЫЛКА_БУДЕТ_ЗДЕСЬ_ПОСЛЕ_АКТИВАЦИИ_ПОДПИСКИ"
    if active_sub:
        server_conf = await get_server_config(active_sub['current_server_id'])
        if server_conf and active_sub['xui_client_uuid']:
            sub_link = get_subscription_link(server_conf, active_sub['xui_client_uuid'])
    
    await query.message.edit_text(
        app_conf.get('text_android_guide').format(sub_link=hcode(sub_link)),
        reply_markup=keyboards.get_guide_keyboard(sub_link, "android", add_step_guide_btn=True),
        disable_web_page_preview=True
    )
    await query.answer()

@dp.callback_query(F.data == "ios_guide")
async def cq_ios_guide(query: CallbackQuery):
    active_sub = await db_helpers.get_active_subscription(query.from_user.id)
    sub_link = "ВАША_ССЫЛКА_БУДЕТ_ЗДЕСЬ_ПОСЛЕ_АКТИВАЦИИ_ПОДПИСКИ"
    if active_sub:
        server_conf = await get_server_config(active_sub['current_server_id'])
        if server_conf and active_sub['xui_client_uuid']:
            sub_link = get_subscription_link(server_conf, active_sub['xui_client_uuid'])
    
    await query.message.edit_text(
        app_conf.get('text_ios_guide').format(sub_link=hcode(sub_link)),
        reply_markup=keyboards.get_guide_keyboard(sub_link, "ios", add_step_guide_btn=True),
        disable_web_page_preview=True
    )
    await query.answer()

@dp.callback_query(F.data == "about_service")
async def cq_about_service(query: CallbackQuery):
    await query.message.edit_text(
        app_conf.get('text_about_service').format(project_name=app_conf.get('project_name')),
        reply_markup=keyboards.get_about_service_keyboard()
    )
    await query.answer()

async def auto_check_payment_status(payment_id: str, user_id: int, payment_metadata: dict):
    start_time = datetime.now(timezone.utc)
    max_duration = timedelta(minutes=5)
    poll_interval = 30
    
    logger.info(f"Запуск автопроверки для платежа {payment_id}, пользователя {user_id}.")

    try:
        while datetime.now(timezone.utc) - start_time < max_duration:
            db_payment_info = await db_helpers.get_payment(payment_id)
            if not db_payment_info or db_payment_info[4] != "pending":
                logger.info(f"Автопроверка: Платеж {payment_id} больше не 'pending'. Остановка.")
                return

            try:
                payment_info_yk = YKPayment.find_one(payment_id)
            except Exception as e:
                logger.error(f"Автопроверка: Ошибка API Yookassa для {payment_id}: {e}")
                await asyncio.sleep(poll_interval * 2)
                continue

            if not payment_info_yk:
                await asyncio.sleep(poll_interval)
                continue
            
            if payment_info_yk.status == "succeeded":
                logger.info(f"Автопроверка: Платеж {payment_id} УСПЕШЕН!")
                await process_successful_payment(user_id, payment_id, payment_metadata)
                return 
            
            elif payment_info_yk.status == "canceled":
                logger.info(f"Автопроверка: Платеж {payment_id} ОТМЕНЕН.")
                await db_helpers.update_payment_status(payment_id, "canceled")
                try: 
                    await bot.send_message(user_id, app_conf.get('text_payment_canceled_or_failed'), reply_markup=keyboards.get_back_to_main_keyboard())
                except Exception as send_err:
                    logger.error(f"Автопроверка: Не удалось уведомить {user_id} об отмене платежа: {send_err}")
                return 

            await asyncio.sleep(poll_interval)
        
        logger.warning(f"Автопроверка: Таймаут для платежа {payment_id}. Остался 'pending'.")

    finally:
        if payment_id in active_payment_checkers: del active_payment_checkers[payment_id]
        logger.info(f"Автопроверка для платежа {payment_id} завершена.")

@dp.callback_query(F.data == "activate_promo_code_prompt")
async def cq_activate_promo_code_prompt(query: CallbackQuery, state: FSMContext):
    await query.message.edit_text(app_conf.get('text_promo_code_prompt'), reply_markup=keyboards.get_back_to_main_keyboard())
    await state.set_state(PromoCodeActivation.waiting_for_code)
    await query.answer()

@dp.message(PromoCodeActivation.waiting_for_code)
async def process_promo_code_activation(message: Message, state: FSMContext):
    await state.clear()
    code = message.text.strip().upper()
    promo_data = await db_helpers.get_promo_code(code)

    if not promo_data:
        return await message.answer(app_conf.get('text_promo_code_invalid'), reply_markup=keyboards.get_back_to_main_keyboard())
    if not promo_data[1]: # is_active
        return await message.answer(app_conf.get('text_promo_code_already_used'), reply_markup=keyboards.get_back_to_main_keyboard())

    # promo_data: (code, is_active, activated_by_telegram_id, activated_at, created_at, days)
    days_to_add = promo_data[5] if len(promo_data) > 5 and promo_data[5] else app_conf.get('promo_code_subscription_days', 30)
    # Получаем текущий лимит устройств пользователя
    user = await db_helpers.get_active_subscription(message.from_user.id)
    current_limit_ip = user.get('limit_ip', 0) if user else 0
    subscription_data = await grant_subscription(message.from_user.id, days_to_add, is_trial=False, limit_ip=current_limit_ip)

    if subscription_data:
        await db_helpers.activate_promo_code(code, message.from_user.id)
        moscow = pytz.timezone('Europe/Moscow')
        local_expiry_date = subscription_data['expiry_date'].astimezone(moscow)
        await message.answer(
            app_conf.get('text_promo_code_success').format(
                code=code, days=days_to_add, expiry_date=local_expiry_date.strftime('%d.%m.%Y %H:%M %Z')
            ),
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        await show_main_menu(message)
    else:
        await message.answer(app_conf.get('text_error_creating_user'), reply_markup=keyboards.get_back_to_main_keyboard())

@dp.callback_query(F.data.startswith("check_payment_"))
async def cq_check_payment(query: CallbackQuery):
    payment_id = query.data.split("_")[2]
    await query.answer(app_conf.get('text_payment_checking'), show_alert=False)

    payment_db_data = await db_helpers.get_payment(payment_id)
    payment_metadata_from_db = json.loads(payment_db_data[6]) if payment_db_data and payment_db_data[6] else None

    try:
        payment_info_yk = YKPayment.find_one(payment_id)
    except Exception as e:
        logger.error(f"Ошибка ручного запроса Yookassa для {payment_id}: {e}")
        return await query.answer(app_conf.get('text_error_general') + " Попробуйте позже.", show_alert=True)

    if not payment_info_yk:
        return await query.message.edit_text(app_conf.get('text_payment_not_found'), reply_markup=keyboards.get_back_to_main_keyboard())

    if payment_info_yk.status == "succeeded":
        await process_successful_payment(query.from_user.id, payment_id, payment_metadata_from_db)
    elif payment_info_yk.status == "pending":
        await query.answer(app_conf.get('text_payment_pending'), show_alert=True) 
    elif payment_info_yk.status in ["canceled", "failed"]:
        await db_helpers.update_payment_status(payment_id, "canceled")
        await query.message.edit_text(app_conf.get('text_payment_canceled_or_failed'), reply_markup=keyboards.get_back_to_main_keyboard())

@dp.callback_query(F.data.startswith("renew_sub"))
async def cq_renew_subscription(query: CallbackQuery):
    user_id = query.from_user.id
    idempotence_key = str(py_uuid.uuid4())

    parts = query.data.split('_')
    is_custom_renewal = len(parts) == 4 # renew_sub_days_price
    
    if is_custom_renewal:
        try:
            days = int(parts[2])
            price = float(parts[3])
            # Получаем валюту и лимит устройств из активных тарифов
            active_tariffs = await db_helpers.get_active_tariffs()
            currency = app_conf.get('subscription_currency', 'RUB')  # fallback
            limit_ip = 0
            for tariff in active_tariffs:
                if tariff['days'] == days and tariff['price'] == price:
                    currency = tariff['currency']
                    limit_ip = tariff.get('limit_ip', 0)
                    break
        except (ValueError, IndexError):
            logger.error(f"Неверный формат callback_data для кастомного продления: {query.data}")
            return await query.answer("Ошибка в параметрах кнопки", show_alert=True)
    else:
        days = app_conf.get('subscription_days', 30)
        price = app_conf.get('subscription_price', 0.0)
        currency = app_conf.get('subscription_currency', 'RUB')
        limit_ip = 0
        active_tariffs = await db_helpers.get_active_tariffs()
        for tariff in active_tariffs:
            if tariff['days'] == days and tariff['price'] == price:
                limit_ip = tariff.get('limit_ip', 0)
                break

    last_sub = await db_helpers.get_last_subscription(user_id)
    # Продлевать можно даже без активной подписки, если есть старый UUID
    current_uuid = last_sub['xui_client_uuid'] if last_sub else None
    current_server_id = last_sub['current_server_id'] if last_sub else None

    payment_metadata = {
        "telegram_user_id": user_id, "subscription_days": days,
        "price": price,
        "bot_payment_uuid": idempotence_key, "is_renewal": bool(last_sub),
        "current_uuid": current_uuid, "current_server_id": current_server_id
    }

    builder = PaymentRequestBuilder()
    builder.set_amount({"value": f"{price:.2f}", "currency": currency}) \
        .set_capture(True) \
        .set_confirmation({"type": "redirect", "return_url": f"https://t.me/{(await bot.get_me()).username}?start=payment_success"}) \
        .set_description(f"Продление VPN подписки на {days} дней") \
        .set_metadata(payment_metadata)
    
    try:
        payment_response = YKPayment.create(builder.build(), idempotence_key)
        if payment_response.confirmation and payment_response.confirmation.confirmation_url:
            yk_payment_id = payment_response.id
            await db_helpers.add_payment(
                payment_id=yk_payment_id, telegram_id=user_id, amount=price,
                currency=currency, metadata_json=json.dumps(payment_metadata)
            )
            price_str = int(price) if price == int(price) else f"{price:.2f}"
            await query.message.edit_text(
                app_conf.get('text_payment_prompt').format(
                    days=days, price=price_str, currency=currency,
                    payment_url=payment_response.confirmation.confirmation_url
                ),
                reply_markup=keyboards.get_payment_keyboard(yk_payment_id),
                disable_web_page_preview=True
            )
            if yk_payment_id not in active_payment_checkers:
                task = asyncio.create_task(auto_check_payment_status(yk_payment_id, user_id, payment_metadata))
                active_payment_checkers[yk_payment_id] = task
        else:
            raise Exception("No confirmation URL in YK response")
    except Exception as e:
        logger.error(f"Ошибка создания платежа: {e}")
        await query.message.edit_text(app_conf.get('text_error_general'), reply_markup=keyboards.get_back_to_main_keyboard())
        await query.answer("Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
    
    await query.answer()

# --- Основная фоновая задача: напоминание о скором завершении подписки ---
async def notify_expiring_subscriptions():
    """
    Периодически проверяет пользователей, у которых подписка заканчивается через 1 день,
    и отправляет им напоминание в Telegram с кнопками продления.
    """
    while True:
        users = await db_helpers.get_users_with_expiring_subscriptions(days_before=1)
        for user_id in users:
            try:
                # Получаем активные тарифы для создания клавиатуры
                active_tariffs = await db_helpers.get_active_tariffs()
                
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                buttons = []
                
                if active_tariffs:
                    # Добавляем кнопки для каждого активного тарифа
                    for tariff in active_tariffs:
                        price_display = int(tariff['price']) if tariff['price'].is_integer() else tariff['price']
                        tariff_name = tariff['name'] if tariff['name'] else f"{tariff['days']} дней"
                        buttons.append([InlineKeyboardButton(
                            text=f"💳 {tariff_name} - {price_display} {tariff['currency']}",
                            callback_data=f"renew_sub_{tariff['days']}_{tariff['price']}"
                        )])
                    
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
                else:
                    # Fallback к старой клавиатуре если нет активных тарифов
                    reply_markup = keyboards.get_renew_keyboard()
                
                await bot.send_message(
                    user_id,
                    app_conf.get('text_subscription_expiring', "⏰ Ваша подписка заканчивается завтра! Не забудьте продлить, чтобы не потерять доступ."),
                    reply_markup=reply_markup
                )
                # Отметить, что напоминание отправлено
                async with aiosqlite.connect('vpn_bot.db') as db:
                    await db.execute("UPDATE users SET notified_expiring = 1 WHERE telegram_id = ?", (user_id,))
                    await db.commit()
            except TelegramAPIError as e:
                error_text = str(e).lower()
                if 'chat not found' in error_text or 'bot was blocked' in error_text or 'user is deactivated' in error_text:
                    logger.warning(f"Пользователь {user_id} заблокировал бота или удален. Деактивация (в notify_expiring).")
                    await db_helpers.deactivate_user(user_id)
                else:
                    logger.error(f'Ошибка Telegram API при отправке напоминания {user_id}: {e}')
            except Exception as e:
                logger.error(f'Неизвестная ошибка при отправке напоминания {user_id}: {e}')
        await asyncio.sleep(24 * 60 * 60)  # Проверять раз в сутки

# --- Фоновая задача: уведомление об истекшей подписке ---
async def notify_expired_subscriptions():
    """
    Периодически (каждую минуту) проверяет пользователей, у которых подписка истекла,
    и отправляет им уведомление, если оно не было отправлено ранее.
    """
    while True:
        try:
            users = await db_helpers.get_users_with_expired_subscriptions()
            for user_id in users:
                try:
                    # Получаем активные тарифы для создания клавиатуры
                    active_tariffs = await db_helpers.get_active_tariffs()
                    
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    buttons = []
                    
                    if active_tariffs:
                        # Добавляем кнопки для каждого активного тарифа
                        for tariff in active_tariffs:
                            price_display = int(tariff['price']) if tariff['price'].is_integer() else tariff['price']
                            tariff_name = tariff['name'] if tariff['name'] else f"{tariff['days']} дней"
                            buttons.append([InlineKeyboardButton(
                                text=f"💳 {tariff_name} - {price_display} {tariff['currency']}",
                                callback_data=f"renew_sub_{tariff['days']}_{tariff['price']}"
                            )])
                        
                        reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
                    else:
                        # Fallback к старой клавиатуре если нет активных тарифов
                        reply_markup = keyboards.get_renew_keyboard()
                    
                    await bot.send_message(
                        user_id,
                        app_conf.get('text_subscription_expired', "😔 Ваша подписка истекла. Чтобы возобновить доступ, пожалуйста, продлите ее."),
                        reply_markup=reply_markup
                    )
                    # Отметить, что уведомление об истечении отправлено
                    async with aiosqlite.connect('vpn_bot.db') as db:
                        await db.execute("UPDATE users SET notified_expired = 1 WHERE telegram_id = ?", (user_id,))
                        await db.commit()
                    logger.info(f"Отправлено уведомление об истечении подписки пользователю {user_id}")
                except TelegramAPIError as e:
                    error_text = str(e).lower()
                    if 'chat not found' in error_text or 'bot was blocked' in error_text or 'user is deactivated' in error_text:
                        logger.warning(f"Пользователь {user_id} заблокировал бота или удален. Деактивация (в notify_expired).")
                        await db_helpers.deactivate_user(user_id)
                    else:
                        logger.error(f'Ошибка Telegram API при отправке уведомления об истечении {user_id}: {e}')
                except Exception as e:
                    logger.error(f'Неизвестная ошибка при отправке уведомления об истечении {user_id}: {e}')
        except Exception as e:
            logger.error(f"Глобальная ошибка в задаче notify_expired_subscriptions: {e}")
        
        await asyncio.sleep(60) # Проверять раз в минуту

@dp.callback_query(F.data == "start_step_guide")
async def start_step_guide(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_android', 'Скачать для 📱Android'), url=app_conf.get('step_guide_android_url', 'https://play.google.com/store/apps/details?id=com.v2raytun.android'))],
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_ios', 'Скачать для 🍎iOS'), url=app_conf.get('step_guide_ios_url', 'https://apps.apple.com/ru/app/v2raytun/id6476628951'))],
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_next', '➡️ Далее'), callback_data="step_guide_2")],
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_back', '⬅️ На главную'), callback_data="back_to_main")]
    ])
    await call.message.edit_text(
        app_conf.get('step_guide_1_text', '<b>1️⃣ Скачайте приложение V2rayTun</b>\n\nВыберите вашу платформу и скачайте приложение. После установки нажмите «Далее».'),
        reply_markup=kb
    )
    await state.set_state(StepByStepGuide.step1)

@dp.callback_query(F.data == "step_guide_2")
async def step_guide_2(call: CallbackQuery, state: FSMContext):
    active_sub = await db_helpers.get_active_subscription(call.from_user.id)
    sub_link = "ВАША_ССЫЛКА_БУДЕТ_ЗДЕСЬ_ПОСЛЕ_АКТИВАЦИИ_ПОДПИСКИ"
    if active_sub:
        server_conf = await get_server_config(active_sub['current_server_id'])
        if server_conf and active_sub['xui_client_uuid']:
            sub_link = get_subscription_link(server_conf, active_sub['xui_client_uuid'])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_next', '➡️ Далее'), callback_data="step_guide_3")],
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_back', '⬅️ На главную'), callback_data="back_to_main")]
    ])
    await call.message.edit_text(
        app_conf.get('step_guide_2_text', '<b>2️⃣ Скопируйте вашу ссылку для подключения</b>\n\n🔗 <code>{sub_link}</code>\n\nСкопируйте ссылку в буфер обмена.').format(sub_link=sub_link),
        reply_markup=kb
    )
    await state.set_state(StepByStepGuide.step2)

@dp.callback_query(F.data == "step_guide_3")
async def step_guide_3(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_next', '➡️ Далее'), callback_data="step_guide_4")],
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_back', '⬅️ На главную'), callback_data="back_to_main")]
    ])
    await call.message.edit_text(
        app_conf.get('step_guide_3_text', '<b>3️⃣ Откройте приложение V2rayTun</b>\n\nЗапустите приложение и разрешите необходимые доступы, если потребуется.'),
        reply_markup=kb
    )
    await state.set_state(StepByStepGuide.step3)

@dp.callback_query(F.data == "step_guide_4")
async def step_guide_4(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_next', '➡️ Далее'), callback_data="step_guide_5")],
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_back', '⬅️ На главную'), callback_data="back_to_main")]
    ])
    await call.message.edit_text(
        app_conf.get('step_guide_4_text', '<b>4️⃣ Импортируйте ссылку</b>\n\nНажмите <b>+</b> в правом верхнем углу и выберите «Импорт из буфера обмена».'),
        reply_markup=kb
    )
    await state.set_state(StepByStepGuide.step4)

@dp.callback_query(F.data == "step_guide_5")
async def step_guide_5(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=app_conf.get('step_guide_btn_back', '⬅️ На главную'), callback_data="back_to_main")]
    ])
    await call.message.edit_text(
        app_conf.get('step_guide_5_text', '<b>5️⃣ Включите VPN</b>\n\nВ списке конфигураций выберите новую и нажмите на кнопку подключения.\n\n✅ Готово! VPN подключён.'),
        reply_markup=kb
    )
    await state.clear()

@dp.callback_query(F.data == "renew_show_tariffs")
async def cq_show_tariffs(query: CallbackQuery):
    reply_markup = await keyboards.get_tariffs_keyboard()
    await query.message.edit_text(
        "Выберите тариф для продления:",
        reply_markup=reply_markup
    )
    await query.answer()

# --- Событие запуска бота ---
async def on_startup(dispatcher: Dispatcher):
    """
    Выполняется при запуске бота:
    - Инициализация базы данных
    - Загрузка настроек из базы
    - Настройка YooKassa
    - Проверка подключения к X-UI серверам
    - Возобновление проверки ожидающих платежей
    """
    global bot
    await db_helpers.init_db()
    await app_conf.load_settings()
    # Пересоздаём bot, если токен изменился после загрузки настроек
    new_token = os.getenv("BOT_TOKEN", app_conf.get('bot_token', ''))
    if new_token and new_token != bot.token:
        bot_instance = Bot(token=new_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dispatcher.bot = bot_instance
        bot = bot_instance
    YKConfig.account_id = os.getenv("YOOKASSA_SHOP_ID", app_conf.get('yookassa_shop_id', ''))
    YKConfig.secret_key = os.getenv("YOOKASSA_SECRET_KEY", app_conf.get('yookassa_secret_key', ''))
    bot_info = await bot.get_me()
    logger.success(f"Бот @{bot_info.username} запущен!")
    xui_servers = app_conf.get('xui_servers', [])
    for server_conf in xui_servers:
        client = await xui_manager_instance.get_client(server_conf)
        if client: logger.info(f"Успешное подключение к X-UI: {server_conf.get('name')}")
        else: logger.error(f"Не удалось подключиться к X-UI: {server_conf.get('name')}")
    pending_payments = await db_helpers.get_pending_payments()
    for p in pending_payments:
        pid, uid, _, _, _, created_at_str, meta_str = p
        meta = json.loads(meta_str) if meta_str else {}
        created_at = datetime.fromisoformat(created_at_str).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created_at < timedelta(minutes=15):
            logger.info(f"Возобновление автопроверки для платежа {pid}")
            task = asyncio.create_task(auto_check_payment_status(pid, uid, meta))
            active_payment_checkers[pid] = task

# --- Событие остановки бота ---
async def on_shutdown(dispatcher: Dispatcher):
    """
    Выполняется при остановке бота:
    - Отмена всех фоновых задач
    - Завершение работы
    """
    logger.info("Бот останавливается...")
    for task in active_payment_checkers.values():
        task.cancel()
    await asyncio.sleep(1)
    logger.info("Бот остановлен.")

# --- Главная точка входа ---
async def main():
    """
    Главная функция запуска бота:
    - Регистрирует события запуска и остановки
    - Регистрирует админские обработчики
    - Загружает настройки из базы
    - Запускает фоновую задачу напоминаний
    - Запускает polling aiogram
    """
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    admin.register_admin_handlers(dp)
    try:
        await app_conf.load_settings()  # Загружаем настройки из базы
        asyncio.create_task(notify_expiring_subscriptions())  # Запускаем напоминания о подписке
        asyncio.create_task(notify_expired_subscriptions()) # Запускаем уведомления об истекших подписках
        await dp.start_polling(bot)  # Запускаем polling aiogram
    finally:
        if bot and bot.session:
            await bot.session.close()

# --- Запуск скрипта ---
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    asyncio.run(main())