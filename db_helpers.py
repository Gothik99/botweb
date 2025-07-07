# db_helpers.py
import aiosqlite
from datetime import datetime, timedelta, timezone
import json
from typing import Optional, List, Dict
from loguru import logger

from config import DATABASE_NAME
# x_ui_manager импортируется внутри функции, чтобы избежать циклических зависимостей при запуске

# СЛОВАРЬ С НАСТРОЙКАМИ И ТЕКСТАМИ ПО УМОЛЧАНИЮ
# При первом запуске бота эти значения будут записаны в базу данных.
# Затем их можно будет менять через веб-админку.
# Формат: 'ключ': ('значение по умолчанию', 'описание для админки')
_DEFAULT_SETTINGS = {
       # --- Тексты: Приветствие и главное меню ---
    'text_welcome_message': (
        "👋 Привет, {user_name}!\n\n"
        "🚀 <b>{project_name}</b> — ваш надежный проводник в мир безграничного интернета!\n\n"
        "• ⚡️ Высокая скорость до 1 Гбит/с\n"
        "• 🔒 Современное шифрование VLESS + Reality\n"
        "• 🌍 Серверы в Германии\n"
        "• 🛡️ Полная анонимность без логов\n"
        "• 💰 Доступная цена\n",
        'Приветственное сообщение для новых пользователей. Переменные: {user_name}, {project_name}'
    ),
    'text_trial_already_used': ('😔 Ваша подписка закончилась, продлите по кнопке ниже ⬇️', 'Текст, если пробный период использован, а подписки нет'),
    'text_subscription_info': (
        "ℹ️ Ваша подписка:\n\n"
        "Статус: {status}\n"
        "Активна до: {expiry_date}\n\n"
        "🔗 <b>Ваша ссылка для подключения:</b>\n"
        "📋 {sub_link}\n\n"
        "Выберите в инструкции ваше устройство 📱Android или 🍎iOS",
        'Информация об активной подписке. Переменные: {status}, {expiry_date}, {sub_link}'
    ),
    'text_no_active_subscription': ('ℹ️ У вас нет активной подписки.', 'Текст, если у пользователя нет активной подписки'),

    # --- Тексты: Оплата ---
    'text_payment_prompt': (
        "💳 Для продления подписки на {days} дней за {price} {currency}, "
        "перейдите по ссылке и оплатите:\n{payment_url}\n\n"
        "После оплаты нажмите кнопку Проверить платеж",
        'Сообщение со ссылкой на оплату. Переменные: {days}, {price}, {currency}, {payment_url}'
    ),
    'text_payment_checking': ('⏳ Проверяем ваш платеж...', 'Сообщение при проверке платежа'),
    'text_payment_success': (
        "✅ Оплата прошла успешно!\n\n"
        "Ваша подписка на {days} дней продлена.\n"
        "Новая дата окончания: {expiry_date}\n\n"
        "Ссылка для подключения:\n<code>{sub_link}</code>",
        'Сообщение об успешной оплате. Переменные: {days}, {expiry_date}, {sub_link}'
    ),
    'text_payment_pending': ('⏳ Платеж все еще ожидает подтверждения. Пожалуйста, подождите немного и попробуйте проверить снова.', 'Сообщение, если платеж в ожидании'),
    'text_payment_canceled_or_failed': ('❌ Оплата не удалась или была отменена. Пожалуйста, попробуйте снова или обратитесь в поддержку.', 'Сообщение, если платеж отменен'),
    'text_payment_not_found': ('🤷 Платеж не найден. Убедитесь, что вы оплатили и попробуйте снова.', 'Сообщение, если платеж не найден'),

    # --- Тексты: Промокоды ---
    'text_promo_code_prompt': ('Введите ваш промокод:', 'Приглашение к вводу промокода'),
    'text_promo_code_success': (
        "✅ Промокод <b>{code}</b> успешно активирован!\n\n"
        "Ваша подписка продлена на {days} дней.\n"
        "Новая дата окончания: {expiry_date}",
        'Сообщение об успешной активации промокода. Переменные: {code}, {days}, {expiry_date}'
    ),
    'text_promo_code_invalid': ('❌ Такой промокод не найден. Проверьте правильность ввода.', 'Сообщение о неверном промокоде'),
    'text_promo_code_already_used': ('❌ Этот промокод уже был использован.', 'Сообщение, если промокод уже использован'),

    # --- Тексты: Ошибки ---
    'text_error_general': ('⚙️ Произошла непредвиденная ошибка. Попробуйте позже.', 'Общая ошибка'),
    'text_error_creating_user': ('🚫 Произошла ошибка при создании VPN пользователя. Пожалуйста, попробуйте позже или обратитесь в поддержку.', 'Ошибка создания пользователя в X-UI'),
    'text_error_xui_connection': ('🔌 Не удалось подключиться к VPN серверу. Пожалуйста, сообщите администратору.', 'Ошибка подключения к X-UI'),

    # --- Тексты: Инструкции и "О сервисе" ---
    'text_trial_success': (
        "🎉 Ваш пробный VPN на {days} дней успешно создан!\n\n"
        "🔗 <b>Ваша ссылка для подключения:</b>\n"
        "📋 {sub_link}\n\n"
        "Ваша подписка активна до: {expiry_date}\n\n"
        "Выберите в инструкции ваше устройство 📱Android или 🍎iOS",
        'Сообщение об успешном создании триала. Переменные: {days}, {sub_link}, {expiry_date}'
    ),
    'text_android_guide': (
        "📱 Инструкция по подключению для Android:\n\n"
        "1. Скачайте приложение V2rayTun из Google Play.\n"
        "2. Откройте приложение и нажмите на кнопку '+' в правом верхнем углу.\n"
        "3. Выберите «Импорт из буфера обмена» или «Вставить ссылку».\n"
        "4. Вставьте вашу ссылку для подключения:\n🔗 <code>{sub_link}</code>\n"
        "5. Нажмите «Сохранить».\n"
        "6. Включите VPN, нажав на круглую кнопку подключения.\n\n"
        "✅ Готово! Ваше VPN-подключение активно.",
        'Инструкция для Android. Переменная: {sub_link}'
    ),
    'text_ios_guide': (
        "🍎 Инструкция по подключению для iOS:\n\n"
        "1. Скачайте приложение V2rayTun из App Store.\n"
        "2. Откройте приложение и нажмите на кнопку '+' в правом верхнем углу.\n"
        "3. Выберите «Импорт из буфера обмена» или «Вставить ссылку».\n"
        "4. Вставьте вашу ссылку для подключения:\n🔗 <code>{sub_link}</code>\n"
        "5. Нажмите «Сохранить».\n"
        "6. Включите VPN, нажав на переключатель в верхней части экрана.\n\n"
        "✅ Готово! Ваше VPN-подключение активно.",
        'Инструкция для iOS. Переменная: {sub_link}'
    ),
    'text_about_service': (
        "🚀 {project_name} — ваш быстрый и безопасный доступ в интернет!\n\n"
        "⚡️ Молниеносная скорость:\n"
        "• До 1 Гбит/с — смотрите 4K без задержек\n"
        "• Серверы в Германии — стабильное соединение\n"
        "• VLESS + Reality — современный протокол\n\n"
        "🛡️ Максимальная защита:\n"
        "• Никаких логов — ваша приватность под защитой\n"
        "• Умное шифрование — ваши данные в безопасности\n"
        "• Защита от утечек — полная анонимность\n\n"
        "✨ Почему выбирают нас:\n"
        "• Никакой рекламы — чистый интернет\n"
        "• Настройка за 1 минуту — просто включи и пользуйся\n"
        "• Поддержка 24/7 — всегда на связи\n"
        "• Пробный период — попробуйте бесплатно\n"
        "• Доступная цена — качество без переплат",
        'Текст для раздела "О сервисе". Переменная: {project_name}'
    ),

    # --- Тексты: Названия кнопок ---
    'btn_renew_sub': ('🔄 Продлить подписку ({days} дн. - {price} {currency})', 'Кнопка продления подписки'),
    'btn_activate_code': ('🎟️ Активировать код на 30 дней', 'Кнопка активации промокода'),
    'btn_android_guide': ('📱Android', 'Кнопка инструкции для Android'),
    'btn_ios_guide': ('🍎iOS', 'Кнопка инструкции для iOS'),
    'btn_about_service': ('ℹ️ О сервисе', 'Кнопка "О сервисе"'),
    'btn_support': ('💬 Поддержка', 'Кнопка "Поддержка"'),
    'btn_check_payment': ('🔍 Проверить платеж', 'Кнопка проверки платежа'),
    'btn_back_to_main': ('⬅️ Вернуться в главное меню', 'Кнопка возврата в главное меню'),
    'btn_download_android': ('📥 Скачать V2rayTun', 'Кнопка скачивания приложения для Android'),
    'btn_download_ios': ('📥 Скачать V2rayTun', 'Кнопка скачивания приложения для iOS'),
    
    # --- Тексты: Админка ---
    'admin_text_promo_codes_menu': ('Меню управления промокодами', 'Заголовок меню промокодов в админке'),
    'admin_text_promo_code_created': ("✅ Создан новый промокод:\n\n<code>{code}</code>", 'Админ-сообщение о создании промокода. Переменная: {code}'),
    'admin_web_password': ('admin123', 'Пароль для входа в веб-админку. ОБЯЗАТЕЛЬНО СМЕНИТЕ!'),
    
    # --- Тексты: Уведомления ---
    'text_subscription_expiring': ('⏰ Ваша подписка заканчивается завтра! Не забудьте продлить, чтобы не потерять доступ.', 'Напоминание за день до окончания подписки'),
    'text_subscription_expired': ('😔 Ваша подписка истекла. Чтобы возобновить доступ, пожалуйста, продлите ее.', 'Уведомление после истечения срока подписки'),
    'text_subscription_expired_main': ('😔 Ваша подписка закончилась. Продлите её, чтобы восстановить доступ к VPN.', 'Текст на главном экране, когда подписка истекла'),

    # --- Ссылки на приложения ---
    'android_app_link': ('https://play.google.com/store/apps/details?id=com.example.v2raytun', 'Ссылка на приложение для Android'),
    'ios_app_link': ('https://apps.apple.com/app/id1234567890', 'Ссылка на приложение для iOS'),

    # --- Пошаговая инструкция (Step-by-step guide) ---
    'step_guide_1_text': (
        '<b>1️⃣ Скачайте приложение V2rayTun</b>\n\nВыберите вашу платформу и скачайте приложение. После установки нажмите «Далее».',
        'Текст для шага 1 пошаговой инструкции.'
    ),
    'step_guide_android_url': (
        'https://play.google.com/store/apps/details?id=com.v2raytun.android',
        'Ссылка на приложение для Android в шаге 1 пошаговой инструкции.'
    ),
    'step_guide_ios_url': (
        'https://apps.apple.com/ru/app/v2raytun/id6476628951',
        'Ссылка на приложение для iOS в шаге 1 пошаговой инструкции.'
    ),
    'step_guide_2_text': (
        '<b>2️⃣ Скопируйте вашу ссылку для подключения</b>\n\n🔗 <code>{sub_link}</code>\n\nСкопируйте ссылку в буфер обмена.',
        'Текст для шага 2 пошаговой инструкции. Переменная: {sub_link}'
    ),
    'step_guide_3_text': (
        '<b>3️⃣ Откройте приложение V2rayTun</b>\n\nЗапустите приложение и разрешите необходимые доступы, если потребуется.',
        'Текст для шага 3 пошаговой инструкции.'
    ),
    'step_guide_4_text': (
        '<b>4️⃣ Импортируйте ссылку</b>\n\nНажмите <b>+</b> в правом верхнем углу и выберите «Импорт из буфера обмена».',
        'Текст для шага 4 пошаговой инструкции.'
    ),
    'step_guide_5_text': (
        '<b>5️⃣ Включите VPN</b>\n\nВ списке конфигураций выберите новую и нажмите на кнопку подключения.\n\n✅ Готово! VPN подключён.',
        'Текст для шага 5 пошаговой инструкции.'
    ),
    # --- Кнопки для пошаговой инструкции ---
    'step_guide_btn_android': ('Скачать для 📱Android', 'Кнопка для скачивания Android-приложения в пошаговой инструкции'),
    'step_guide_btn_ios': ('Скачать для 🍎iOS', 'Кнопка для скачивания iOS-приложения в пошаговой инструкции'),
    'step_guide_btn_next': ('➡️ Далее', 'Кнопка "Далее" в пошаговой инструкции'),
    'step_guide_btn_back': ('⬅️ На главную', 'Кнопка "На главную" в пошаговой инструкции'),
}

async def init_db():
    async with aiosqlite.connect(DATABASE_NAME) as db:
        # Основные таблицы
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                xui_client_uuid TEXT,
                xui_client_email TEXT,
                subscription_end_date TEXT,
                is_trial_used INTEGER DEFAULT 0,
                current_server_id INTEGER,
                notified_expiring INTEGER DEFAULT 0,
                notified_expired INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                limit_ip INTEGER DEFAULT 0
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                telegram_id INTEGER,
                amount REAL,
                currency TEXT,
                status TEXT DEFAULT 'pending', -- pending, succeeded, canceled
                created_at TEXT,
                metadata_json TEXT,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                is_active INTEGER DEFAULT 1,
                activated_by_telegram_id INTEGER,
                activated_at TEXT,
                created_at TEXT,
                FOREIGN KEY (activated_by_telegram_id) REFERENCES users (telegram_id)
            )
        ''')
        # Таблица для настроек
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                description TEXT
            )
        ''')
        # Таблица для тарифов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                days INTEGER NOT NULL,
                price REAL NOT NULL,
                currency TEXT DEFAULT 'RUB',
                is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()
    
    await populate_default_settings()
    await populate_default_tariffs()
    logger.info("База данных инициализирована.")

async def populate_default_settings():
    """Заполняет таблицу настроек значениями по умолчанию, если их там еще нет."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        for key, (value, description) in _DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)",
                (key, str(value), description)
            )
        await db.commit()
    logger.info("Проверено и дополнено {} настроек по умолчанию в БД.".format(len(_DEFAULT_SETTINGS)))

async def populate_default_tariffs():
    """Заполняет таблицу тарифов значениями по умолчанию, если их там еще нет."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        # Проверяем, есть ли уже тарифы
        cursor = await db.execute("SELECT COUNT(*) FROM tariffs")
        count = (await cursor.fetchone())[0]
        
        if count == 0:
            # Создаем стандартный тариф на основе настроек
            default_days = int(_DEFAULT_SETTINGS.get('subscription_days', ('30', ''))[0])
            default_price = float(_DEFAULT_SETTINGS.get('subscription_price', ('79.00', ''))[0])
            default_currency = _DEFAULT_SETTINGS.get('subscription_currency', ('RUB', ''))[0]
            
            await db.execute('''
                INSERT INTO tariffs (name, days, price, currency, is_active, sort_order, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                f"Стандартный тариф ({default_days} дней)",
                default_days,
                default_price,
                default_currency,
                1,
                0,
                f"Стандартная подписка на {default_days} дней"
            ))
            await db.commit()
            logger.info("Создан стандартный тариф по умолчанию.")

async def load_all_settings() -> Dict[str, str]:
    """Загружает все настройки из БД в виде словаря."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT key, value FROM settings") as cursor:
            return {row[0]: row[1] for row in await cursor.fetchall()}

# ... (остальные функции get_user, add_user, etc. остаются без изменений) ...

async def get_user(telegram_id: int):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            return await cursor.fetchone()

async def add_user(telegram_id: int, username: str = None):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username)
        )
        await db.commit()

async def update_user_subscription(telegram_id: int, xui_client_uuid: str, xui_client_email: str,
                                   subscription_end_date: datetime, server_id: int, is_trial: bool = False, limit_ip: int = 0):
    # --- ЗАЩИТА ОТ НАИВНЫХ ДАТ ---
    if subscription_end_date.tzinfo is None:
        logger.warning(f"В update_user_subscription передана НАИВНАЯ дата для пользователя {telegram_id}. "
                       f"Автоматическое преобразование в дату с таймзоной.")
        subscription_end_date = subscription_end_date.astimezone()
    # --------------------------------

    end_date_str = subscription_end_date.isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute(
            """UPDATE users 
               SET xui_client_uuid = ?, xui_client_email = ?, subscription_end_date = ?, 
                   is_trial_used = CASE WHEN ? THEN 1 ELSE is_trial_used END,
                   current_server_id = ?,
                   limit_ip = ?
               WHERE telegram_id = ?""",
            (xui_client_uuid, xui_client_email, end_date_str, 1 if is_trial else 0, server_id, limit_ip, telegram_id)
        )
        await db.execute(
            """UPDATE users
               SET xui_client_uuid = ?, xui_client_email = ?, subscription_end_date = ?, current_server_id = ?, limit_ip = ?
               WHERE telegram_id = ? AND (xui_client_uuid IS NULL OR xui_client_uuid = '')""",
            (xui_client_uuid, xui_client_email, end_date_str, server_id, limit_ip, telegram_id)
        )
        if is_trial:
             await db.execute("UPDATE users SET is_trial_used = 1 WHERE telegram_id = ?", (telegram_id,))
        await db.execute(
            "UPDATE users SET notified_expiring = 0 WHERE telegram_id = ?",
            (telegram_id,)
        )
        await db.execute(
            "UPDATE users SET notified_expired = 0 WHERE telegram_id = ?",
            (telegram_id,)
        )
        await db.commit()
    logger.info(f"Подписка для {telegram_id} обновлена. UUID: {xui_client_uuid}, до: {end_date_str}, limit_ip: {limit_ip}")

async def deactivate_user(telegram_id: int):
    """Деактивирует пользователя, чтобы он не получал рассылки."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
    logger.warning(f"Пользователь {telegram_id} деактивирован (вероятно, заблокировал бота).")

async def get_active_subscription(telegram_id: int):
    user = await get_user(telegram_id)
    if user and user[4]: # subscription_end_date
        try:
            sub_end_date = datetime.fromisoformat(user[4])
            if sub_end_date > datetime.now(sub_end_date.tzinfo): # Учитываем таймзону если есть
                return {
                    "telegram_id": user[0],
                    "username": user[1],
                    "xui_client_uuid": user[2],
                    "xui_client_email": user[3],
                    "subscription_end_date": sub_end_date,
                    "is_trial_used": bool(user[5]),
                    "current_server_id": user[6],
                    "limit_ip": user[10] if len(user) > 10 else 0
                }
        except ValueError:
            logger.error(f"Некорректный формат даты подписки для пользователя {telegram_id}: {user[4]}")
    return None

async def add_payment(payment_id: str, telegram_id: int, amount: float, currency: str, metadata_json: Optional[str] = None):
    created_at_str = datetime.now(timezone.utc).isoformat() # Используем UTC для created_at
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute(
            "INSERT INTO payments (payment_id, telegram_id, amount, currency, created_at, status, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payment_id, telegram_id, amount, currency, created_at_str, 'pending', metadata_json)
        )
        await db.commit()
    logger.info(f"Платеж {payment_id} для {telegram_id} создан. Метаданные: {metadata_json}")

async def get_payment(payment_id: str):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT payment_id, telegram_id, amount, currency, status, created_at, metadata_json FROM payments WHERE payment_id = ?", (payment_id,)) as cursor:
            return await cursor.fetchone()

async def update_payment_status(payment_id: str, status: str):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
        await db.commit()
    logger.info(f"Статус платежа {payment_id} обновлен на {status}.")

async def delete_xui_user_db_record(telegram_id: int):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute(
            """UPDATE users 
               SET xui_client_uuid = NULL, xui_client_email = NULL, subscription_end_date = NULL, current_server_id = NULL
               WHERE telegram_id = ?""",
            (telegram_id,)
        )
        await db.commit()
    logger.info(f"Запись о XUI пользователе для {telegram_id} удалена из БД (но не подписка).")

async def get_pending_payments(limit: int = 100):
    """Получает список платежей со статусом 'pending'."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
                "SELECT payment_id, telegram_id, amount, currency, status, created_at, metadata_json "
                "FROM payments WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            return await cursor.fetchall()

async def get_total_users_count() -> int:
    """Получить общее количество пользователей"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_active_subscriptions_count() -> int:
    """Получить количество активных подписок"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE subscription_end_date > datetime('now')"
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_trial_users_count() -> int:
    """Получить количество пользователей, использовавших пробный период"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE is_trial_used = 1"
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_total_payments_count() -> int:
    """Получить общее количество платежей"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM payments") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_successful_payments_count() -> int:
    """Получить количество успешных платежей"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM payments WHERE status = 'succeeded'"
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_total_payments_amount() -> float:
    """Получить общую сумму успешных платежей"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            "SELECT SUM(amount) FROM payments WHERE status = 'succeeded'"
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result and result[0] else 0.0

async def get_user_payments(user_id: int) -> List[tuple]:
    """Получить историю платежей пользователя"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            "SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC",
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()

async def delete_user_subscription(user_id: int) -> bool:
    """Удалить подписку пользователя"""
    # Импортируем здесь, чтобы избежать циклической зависимости
    from x_ui_manager import xui_manager_instance
    from app_config import app_conf

    try:
        async with aiosqlite.connect(DATABASE_NAME) as db:
            async with db.execute(
                "SELECT xui_client_uuid, current_server_id FROM users WHERE telegram_id = ? AND subscription_end_date > datetime('now')",
                (user_id,)
            ) as cursor:
                sub = await cursor.fetchone()
                if not sub: return False
                
                uuid, server_id = sub
                
                server_config = next((s for s in app_conf.get('xui_servers', []) if s['id'] == server_id), None)
                if server_config:
                    await xui_manager_instance.delete_xui_user(server_config, uuid)
                
                await db.execute(
                    """UPDATE users 
                       SET xui_client_uuid = NULL, xui_client_email = NULL, 
                           subscription_end_date = NULL, current_server_id = NULL
                       WHERE telegram_id = ?""",
                    (user_id,)
                )
                await db.commit()
                return True
    except Exception as e:
        logger.error(f"Ошибка при удалении подписки пользователя {user_id}: {e}")
        return False

async def get_users_list(limit: int = 50, offset: int = 0) -> List[tuple]:
    """Получить список пользователей с пагинацией"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            """SELECT telegram_id, username, subscription_end_date, is_trial_used, current_server_id 
               FROM users 
               ORDER BY telegram_id DESC 
               LIMIT ? OFFSET ?""",
            (limit, offset)
        ) as cursor:
            return await cursor.fetchall()

async def get_users_count() -> int:
    """Получить общее количество пользователей для пагинации"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_server_config(server_id: int) -> Optional[dict]:
    from app_config import app_conf
    return next((s for s in app_conf.get('xui_servers', []) if s['id'] == server_id), None)

async def get_last_subscription(telegram_id: int):
    """Получить последнюю подписку пользователя, даже если она истекла"""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            """SELECT telegram_id, username, xui_client_uuid, xui_client_email, 
                      subscription_end_date, is_trial_used, current_server_id, limit_ip 
               FROM users 
               WHERE telegram_id = ? AND xui_client_uuid IS NOT NULL 
               ORDER BY subscription_end_date DESC 
               LIMIT 1""",
            (telegram_id,)
        ) as cursor:
            user = await cursor.fetchone()
            if user and user[2]:  # xui_client_uuid
                try:
                    sub_end_date = datetime.fromisoformat(user[4]) if user[4] else None
                    return {
                        "telegram_id": user[0],
                        "username": user[1],
                        "xui_client_uuid": user[2],
                        "xui_client_email": user[3],
                        "subscription_end_date": sub_end_date,
                        "is_trial_used": bool(user[5]),
                        "current_server_id": user[6],
                        "limit_ip": user[7] if len(user) > 7 else 0
                    }
                except ValueError:
                    logger.error(f"Некорректный формат даты подписки для пользователя {telegram_id}: {user[4]}")
            return None

async def get_all_users() -> List[tuple]:
    """Получает всех пользователей из БД (не только активных)."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT telegram_id, username, xui_client_uuid, xui_client_email, subscription_end_date, is_trial_used, current_server_id FROM users ORDER BY telegram_id") as cursor:
            return await cursor.fetchall()

async def get_all_xui_users_for_restore() -> List[Dict]:
    """
    Получает всех пользователей, у которых есть UUID в X-UI, для восстановления (включая неактивных).
    Возвращает список словарей для удобства.
    """
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT
                telegram_id,
                xui_client_uuid,
                xui_client_email,
                subscription_end_date,
                current_server_id
            FROM
                users
            WHERE
                xui_client_uuid IS NOT NULL AND xui_client_uuid != ''
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

# --- Функции для работы с промокодами (остаются без изменений) ---

async def add_promo_code(code: str) -> bool:
    created_at_str = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO promo_codes (code, created_at, is_active) VALUES (?, ?, 1)",
                (code, created_at_str)
            )
            await db.commit()
            logger.info(f"Промокод {code} успешно добавлен в базу.")
            return True
        except aiosqlite.IntegrityError:
            logger.warning(f"Попытка добавить уже существующий промокод: {code}")
            return False

async def get_promo_code(code: str) -> Optional[tuple]:
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT * FROM promo_codes WHERE code = ?", (code,)) as cursor:
            return await cursor.fetchone()

async def activate_promo_code(code: str, telegram_id: int):
    activated_at_str = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute(
            "UPDATE promo_codes SET is_active = 0, activated_by_telegram_id = ?, activated_at = ? WHERE code = ?",
            (telegram_id, activated_at_str, code)
        )
        await db.commit()
        logger.info(f"Промокод {code} активирован пользователем {telegram_id}.")

async def get_activated_promo_codes_count() -> int:
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM promo_codes WHERE is_active = 0") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_activated_code_for_user(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            "SELECT code FROM promo_codes WHERE activated_by_telegram_id = ?", (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def get_promo_codes_list(status: str, limit: int, offset: int) -> List[tuple]:
    query = "SELECT code, is_active, activated_by_telegram_id, activated_at FROM promo_codes"
    params = []
    if status == 'active': query += " WHERE is_active = 1"
    elif status == 'inactive': query += " WHERE is_active = 0"
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(query, tuple(params)) as cursor:
            return await cursor.fetchall()

async def get_promo_codes_count(status: str) -> int:
    query = "SELECT COUNT(*) FROM promo_codes"
    if status == 'active': query += " WHERE is_active = 1"
    elif status == 'inactive': query += " WHERE is_active = 0"
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(query) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_users_with_expiring_subscriptions(days_before: int = 1):
    from datetime import datetime, timedelta
    import aiosqlite
    target_date = (datetime.now() + timedelta(days=days_before)).date()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute(
            "SELECT telegram_id, subscription_end_date FROM users WHERE subscription_end_date IS NOT NULL AND (notified_expiring IS NULL OR notified_expiring = 0) AND is_active = 1"
        ) as cursor:
            result = []
            async for row in cursor:
                if row[1]:
                    try:
                        sub_end = datetime.fromisoformat(row[1])
                        if sub_end.date() == target_date:
                            result.append(row[0])
                    except Exception:
                        continue
            return result

async def get_users_with_expired_subscriptions():
    """
    Получает пользователей с истекшей подпиской, которым еще не отправляли уведомление.
    Сравнение дат происходит в Python для надежной обработки часовых поясов.
    """
    from datetime import datetime, timezone
    import aiosqlite
    
    now_utc = datetime.now(timezone.utc)

    async with aiosqlite.connect(DATABASE_NAME) as db:
        # Выбираем всех потенциальных кандидатов, проверка даты будет в коде
        async with db.execute(
            """SELECT telegram_id, subscription_end_date FROM users 
               WHERE subscription_end_date IS NOT NULL 
               AND (notified_expired IS NULL OR notified_expired = 0)
               AND is_active = 1"""
        ) as cursor:
            expired_users = []
            async for row in cursor:
                user_id, sub_end_str = row
                if not sub_end_str:
                    continue
                
                try:
                    # fromisoformat корректно парсит даты с часовым поясом
                    sub_end_date = datetime.fromisoformat(sub_end_str)
                    
                    # Если дата "наивная" (без таймзоны), делаем ее "осведомленной",
                    # предполагая, что она в локальной таймзоне сервера.
                    if sub_end_date.tzinfo is None:
                        sub_end_date = sub_end_date.astimezone()

                    # Сравнение timezone-aware datetime объектов
                    if sub_end_date < now_utc:
                        expired_users.append(user_id)
                except Exception as e:
                    logger.error(f"Не удалось обработать дату окончания подписки для пользователя {user_id}: {sub_end_str}. Ошибка: {e}")
            
            return expired_users

def update_xui_servers_distribution_settings(new_servers_list):
    """
    Массово обновляет настройки распределения серверов (exclude_from_auto, max_clients, priority и др.)
    new_servers_list — список словарей серверов с новыми полями.
    """
    import json
    import sqlite3
    DB_PATH = 'vpn_bot.db'
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE settings SET value = ? WHERE key = 'xui_servers'", (json.dumps(new_servers_list, indent=4),))
    conn.commit()
    conn.close()

async def get_active_clients_count_for_server(server_id: int) -> Optional[int]:
    """
    Возвращает количество активных клиентов (с действующей подпиской) для конкретного сервера.
    """
    try:
        async with aiosqlite.connect(DATABASE_NAME) as db:
            cursor = await db.execute(
                """SELECT COUNT(*) FROM users 
                   WHERE current_server_id = ? 
                   AND subscription_end_date IS NOT NULL 
                   AND subscription_end_date > ?""",
                (server_id, datetime.now().isoformat())
            )
            result = await cursor.fetchone()
            return result[0] if result else 0
    except Exception as e:
        logger.error(f"Ошибка при подсчёте активных клиентов для сервера {server_id}: {e}")
        return None

async def get_active_tariffs() -> List[Dict]:
    """Получает все активные тарифы, отсортированные по sort_order."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tariffs WHERE is_active = 1 ORDER BY sort_order, id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_tariff_by_id(tariff_id: int) -> Optional[Dict]:
    """Получает тариф по ID."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tariffs WHERE id = ?", (tariff_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def create_tariff(name: str, days: int, price: float, currency: str = 'RUB', 
                       description: str = '', sort_order: int = 0) -> bool:
    """Создает новый тариф."""
    try:
        async with aiosqlite.connect(DATABASE_NAME) as db:
            await db.execute('''
                INSERT INTO tariffs (name, days, price, currency, description, sort_order, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            ''', (name, days, price, currency, description, sort_order))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка создания тарифа: {e}")
        return False

async def update_tariff(tariff_id: int, name: str, days: int, price: float, 
                       currency: str = 'RUB', description: str = '', 
                       sort_order: int = 0, is_active: bool = True) -> bool:
    """Обновляет существующий тариф."""
    try:
        async with aiosqlite.connect(DATABASE_NAME) as db:
            await db.execute('''
                UPDATE tariffs 
                SET name = ?, days = ?, price = ?, currency = ?, description = ?, 
                    sort_order = ?, is_active = ?
                WHERE id = ?
            ''', (name, days, price, currency, description, sort_order, 
                  int(is_active), tariff_id))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка обновления тарифа: {e}")
        return False

async def delete_tariff(tariff_id: int) -> bool:
    """Удаляет тариф."""
    try:
        async with aiosqlite.connect(DATABASE_NAME) as db:
            await db.execute("DELETE FROM tariffs WHERE id = ?", (tariff_id,))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка удаления тарифа: {e}")
        return False

async def toggle_tariff_active(tariff_id: int) -> bool:
    """Переключает активность тарифа."""
    try:
        async with aiosqlite.connect(DATABASE_NAME) as db:
            await db.execute(
                "UPDATE tariffs SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?",
                (tariff_id,)
            )
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка переключения активности тарифа: {e}")
        return False