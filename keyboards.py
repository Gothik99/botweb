# keyboards.py
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from app_config import app_conf
import asyncio
from db_helpers import get_active_tariffs

async def get_main_keyboard(is_trial_available: bool, has_active_sub: bool):
    builder = InlineKeyboardBuilder()

    # Получаем текст кнопки из настроек
    btn_renew_sub = app_conf.get('btn_renew_sub', '🔄 Продлить')
    builder.row(InlineKeyboardButton(
        text=btn_renew_sub,
        callback_data="renew_show_tariffs"
    ))

    # Остальные кнопки (без тарифов)
    builder.row(InlineKeyboardButton(
        text=app_conf.get('btn_activate_code', '🎟️ Активировать код'),
        callback_data="activate_promo_code_prompt"
    ))

    builder.row(
        InlineKeyboardButton(
            text=app_conf.get('btn_android_guide', '📱Android'),
            callback_data="android_guide"
        ),
        InlineKeyboardButton(
            text=app_conf.get('btn_ios_guide', '🍎iOS'),
            callback_data="ios_guide"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=app_conf.get('btn_about_service', 'ℹ️ О сервисе'),
            callback_data="about_service"
        ),
        InlineKeyboardButton(
            text=app_conf.get('btn_support', '💬 Поддержка'),
            url=app_conf.get('support_link', '')
        )
    )
    
    return builder.as_markup()

def get_payment_keyboard(payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=app_conf.get('btn_check_payment', '🔍 Проверить платеж'),
        callback_data=f"check_payment_{payment_id}"
    ))
    builder.row(InlineKeyboardButton(
        text=app_conf.get('btn_back_to_main', '⬅️ Назад'),
        callback_data="back_to_main"
    ))
    return builder.as_markup()

def get_back_to_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=app_conf.get('btn_back_to_main', '⬅️ Назад'),
        callback_data="back_to_main"
    ))
    return builder.as_markup()

def get_guide_keyboard(sub_link: str, platform: str, add_step_guide_btn: bool = False):
    """Клавиатура для страницы с инструкцией"""
    builder = InlineKeyboardBuilder()
    
    # Добавляем кнопку скачивания приложения в зависимости от платформы
    if platform == "android":
        builder.row(InlineKeyboardButton(
            text=app_conf.get('btn_download_android', '📥 Скачать'),
            url="https://play.google.com/store/apps/details?id=com.v2raytun.android"
        ))
    elif platform == "ios":
        builder.row(InlineKeyboardButton(
            text=app_conf.get('btn_download_ios', '📥 Скачать'),
            url="https://apps.apple.com/ru/app/v2raytun/id6476628951"
        ))
    
    if add_step_guide_btn:
        builder.row(InlineKeyboardButton(
            text='Я новичок, хочу по шагам',
            callback_data='start_step_guide'
        ))
    
    builder.row(InlineKeyboardButton(
        text=app_conf.get('btn_back_to_main', '⬅️ Назад'),
        callback_data="back_to_main"
    ))
    return builder.as_markup()

def get_about_service_keyboard():
    """Клавиатура для страницы о сервисе"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=app_conf.get('btn_back_to_main', '⬅️ Назад'),
        callback_data="back_to_main"
    ))
    return builder.as_markup()

def get_renew_keyboard():
    # Получаем активные тарифы из базы данных
    try:
        # Создаем новый event loop для синхронного вызова асинхронной функции
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        tariffs = loop.run_until_complete(get_active_tariffs())
        loop.close()
    except Exception:
        # Fallback к старым настройкам если что-то пошло не так
        sub_days = app_conf.get('subscription_days', 30)
        sub_price = app_conf.get('subscription_price', 0.0)
        sub_currency = app_conf.get('subscription_currency', 'RUB')
        price_display = int(sub_price) if float(sub_price) == int(sub_price) else sub_price
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=app_conf.get('btn_renew_sub', '🔄 Продлить подписку').format(
                    days=sub_days,
                    price=price_display,
                    currency=sub_currency
                ),
                callback_data="renew_sub"
            )
        )
        return builder.as_markup()
    else:
        # Добавляем кнопки для каждого активного тарифа
        builder = InlineKeyboardBuilder()
        for tariff in tariffs:
            price_display = int(tariff['price']) if tariff['price'].is_integer() else tariff['price']
            tariff_name = tariff['name'] if tariff['name'] else f"{tariff['days']} дней"
            
            builder.row(InlineKeyboardButton(
                text=f"💳 {tariff_name} - {price_display} {tariff['currency']}",
                callback_data=f"renew_sub_{tariff['days']}_{tariff['price']}"
            ))
        return builder.as_markup()

def get_step_guide_button():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text='Я новичок, хочу по шагам',
        callback_data='start_step_guide'
    ))
    return builder.as_markup()

async def get_tariffs_keyboard():
    tariffs = await get_active_tariffs()
    buttons = []
    if tariffs:
        for tariff in tariffs:
            price_display = int(tariff['price']) if tariff['price'].is_integer() else tariff['price']
            tariff_name = tariff['name'] if tariff['name'] else f"{tariff['days']} дней"
            buttons.append([InlineKeyboardButton(
                text=f"💳 {tariff_name} - {price_display} {tariff['currency']}",
                callback_data=f"renew_sub_{tariff['days']}_{tariff['price']}"
            )])
    buttons.append([InlineKeyboardButton(
        text=app_conf.get('btn_back_to_main', '⬅️ Назад'),
        callback_data="back_to_main"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)