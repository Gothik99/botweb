import sqlite3
import json
from flask import Flask, render_template, request, redirect, url_for, flash, g, abort, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import os
import requests
import subprocess
import sys
import random
import string
from datetime import datetime, timezone, timedelta
import math
# Добавлено для корректного импорта модулей из корня проекта
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from tg_sender import send_telegram_message
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import threading
from keyboards import get_back_to_main_keyboard

from x_ui_manager import XUIManager
import db_helpers
from subscription_manager import grant_subscription, get_subscription_link
from app_config import app_conf

xui_manager_instance = XUIManager()

# --- Настройки ---
# Путь к БД должен быть относительным от корня проекта, а не от папки web_admin
DATABASE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'vpn_bot.db')
SECRET_KEY = os.urandom(24) # Генерируем случайный ключ при каждом запуске

# --- Инициализация Flask ---
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Пожалуйста, войдите, чтобы получить доступ к этой странице."
login_manager.login_message_category = "info"

# --- Управление БД (Синхронная версия для Flask) ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def execute_db(query, args=()):
    db = get_db()
    db.execute(query, args)
    db.commit()


# --- Модель пользователя для Flask-Login ---
class AdminUser(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    # У нас только один "пользователь" - админ
    return AdminUser(user_id)

# --- Маршруты (Routes) ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        password_attempt = request.form.get('password')
        admin_password_row = query_db("SELECT value FROM settings WHERE key = 'admin_web_password'", one=True)
        
        if admin_password_row and password_attempt == admin_password_row['value']:
            admin = AdminUser(id=1) # Статичный ID для админа
            login_user(admin)
            flash('Вы успешно вошли в систему!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Неверный пароль.', 'danger')
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    stats = {}
    stats['total_users'] = query_db("SELECT COUNT(*) FROM users", one=True)[0]
    stats['active_subs'] = query_db("SELECT COUNT(*) FROM users WHERE subscription_end_date > datetime('now')", one=True)[0]
    stats['trial_users'] = query_db("SELECT COUNT(*) FROM users WHERE is_trial_used = 1", one=True)[0]
    stats['successful_payments'] = query_db("SELECT COUNT(*) FROM payments WHERE status = 'succeeded'", one=True)[0]
    total_amount_row = query_db("SELECT SUM(amount) FROM payments WHERE status = 'succeeded'", one=True)
    stats['total_amount'] = total_amount_row[0] if total_amount_row and total_amount_row[0] else 0
    stats['promo_activated'] = query_db("SELECT COUNT(*) FROM promo_codes WHERE is_active = 0", one=True)[0]
    stats['promo_total'] = query_db("SELECT COUNT(*) FROM promo_codes", one=True)[0]

    return render_template('dashboard.html', stats=stats)


@app.route('/users')
@login_required
def users_list():
    page = request.args.get('page', 1, type=int)
    per_page = 15
    offset = (page - 1) * per_page
    users = query_db(
        """
        SELECT telegram_id, username, subscription_end_date, is_trial_used, current_server_id 
        FROM users 
        ORDER BY 
            CASE WHEN subscription_end_date IS NULL THEN 1 ELSE 0 END, 
            subscription_end_date DESC, 
            telegram_id DESC 
        LIMIT ? OFFSET ?
        """,
        (per_page, offset)
    )
    users_list = []
    for user in users:
        user = dict(user)
        if user['subscription_end_date']:
            try:
                dt = datetime.fromisoformat(user['subscription_end_date'])
                if dt.tzinfo is None:
                    # Если дата "наивная", считаем, что она в UTC
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    # Если дата "осведомленная", приводим к UTC
                    dt = dt.astimezone(timezone.utc)
                user['subscription_end_date'] = dt
            except Exception:
                user['subscription_end_date'] = None
        users_list.append(user)
    now = datetime.now(timezone.utc)
    total_users = query_db("SELECT COUNT(*) FROM users", one=True)[0]
    total_pages = (total_users + per_page - 1) // per_page
    # Получаем шаблоны новостей
    news_templates = query_db("SELECT id, title, body FROM news_templates ORDER BY id DESC")
    return render_template('users.html', users=users_list, page=page, total_pages=total_pages, now=now, news_templates=news_templates)


@app.route('/users/<int:telegram_id>', methods=['GET', 'POST'])
@login_required
def user_details(telegram_id):
    if request.method == 'POST':
        # Обновление статусов уведомлений
        notified_expiring = 1 if 'notified_expiring' in request.form else 0
        notified_expired = 1 if 'notified_expired' in request.form else 0
        execute_db(
            "UPDATE users SET notified_expiring = ?, notified_expired = ? WHERE telegram_id = ?",
            (notified_expiring, notified_expired, telegram_id)
        )
        flash('Статусы уведомлений обновлены.', 'success')
        return redirect(url_for('user_details', telegram_id=telegram_id))

    user = query_db("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
    if not user:
        flash(f'Пользователь с ID {telegram_id} не найден.', 'danger')
        return redirect(url_for('users_list'))
    # Получаем платежи пользователя
    payments = query_db("SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC", (telegram_id,))
    # Получаем активированные промокоды
    promo = query_db("SELECT code FROM promo_codes WHERE activated_by_telegram_id = ?", (telegram_id,))
    # Получаем список серверов для формы смены сервера
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers = json.loads(servers_row['value']) if servers_row else []
    return render_template('user_details.html', user=user, payments=payments, promo=promo, servers=servers)

@app.route('/users/<int:telegram_id>/change_server', methods=['POST'])
@login_required
def change_user_server(telegram_id):
    new_server_id = int(request.form.get('new_server_id'))
    # Получаем пользователя
    user = query_db("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
    if not user:
        flash('Пользователь не найден.', 'danger')
        return redirect(url_for('users_list'))
    if not user['subscription_end_date'] or not user['xui_client_uuid'] or not user['current_server_id']:
        flash('У пользователя нет активной подписки для переноса.', 'danger')
        return redirect(url_for('user_details', telegram_id=telegram_id))
    if user['current_server_id'] == new_server_id:
        flash('Выбран тот же сервер.', 'warning')
        return redirect(url_for('user_details', telegram_id=telegram_id))
    # Получаем конфиги серверов
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers = json.loads(servers_row['value']) if servers_row else []
    old_server = next((s for s in servers if s['id'] == user['current_server_id']), None)
    new_server = next((s for s in servers if s['id'] == new_server_id), None)
    if not new_server:
        flash('Новый сервер не найден.', 'danger')
        return redirect(url_for('user_details', telegram_id=telegram_id))
    # Перенос подписки
    import asyncio
    from datetime import datetime
    from x_ui_manager import xui_manager_instance
    from subscription_manager import get_subscription_link
    from tg_sender import send_telegram_message
    try:
        async def do_change():
            # 1. Создать нового клиента на новом сервере с тем же сроком
            expiry_dt = datetime.fromisoformat(user['subscription_end_date'])
            now = datetime.now(expiry_dt.tzinfo)
            days_left = math.ceil((expiry_dt - now).total_seconds() / 86400)
            if days_left < 1:
                days_left = 1
            xui_user = await xui_manager_instance.create_xui_user(new_server, telegram_id, days_left)
            if not xui_user:
                return False, 'Ошибка создания клиента на новом сервере.'
            # 2. Удалить старого клиента, если сервер найден
            if old_server:
                await xui_manager_instance.delete_xui_user(old_server, user['xui_client_uuid'])
                old_server_name = old_server['name']
            else:
                old_server_name = f"ID {user['current_server_id']} (удалён)"
            # 3. Обновить БД
            await db_helpers.update_user_subscription(
                telegram_id=telegram_id,
                xui_client_uuid=xui_user['uuid'],
                xui_client_email=xui_user['email'],
                subscription_end_date=expiry_dt,
                server_id=new_server_id,
                is_trial=bool(user['is_trial_used'])
            )
            # 4. Сгенерировать новую ссылку
            sub_link = get_subscription_link(new_server, xui_user['uuid'])
            # 5. Уведомить пользователя
            text = (
                'Вам был назначен новый сервер для VPN.\n'
                'Пожалуйста, замените вашу старую подписку на новую ссылку:\n'
                f'<code>{sub_link}</code>'
            )
            reply_markup = get_back_to_main_keyboard()
            await send_telegram_message(telegram_id, text, reply_markup=reply_markup)
            # 6. Логирование
            import logging
            logging.info(f"[ADMIN] Пользователь {telegram_id} перенесен с сервера {old_server_name} на {new_server['name']} до {expiry_dt}")
            return True, None
        ok, err = asyncio.run(do_change())
        if ok:
            flash('Сервер успешно изменён, пользователь уведомлён.', 'success')
        else:
            flash(f'Ошибка при смене сервера: {err}', 'danger')
    except Exception as e:
        flash(f'Критическая ошибка при смене сервера: {e}', 'danger')
    return redirect(url_for('user_details', telegram_id=telegram_id))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    settings = get_settings()
    if request.method == 'POST':
        btn_renew_sub = request.form.get('btn_renew_sub', '').strip()
        if btn_renew_sub:
            set_setting('btn_renew_sub', btn_renew_sub)
        # ... остальные настройки ...
        flash('Настройки успешно сохранены.', 'success')
        return redirect(url_for('settings'))
    return render_template('settings_form.html', settings=settings)

@app.route('/settings/general', methods=['GET', 'POST'])
@login_required
def settings_general():
    if request.method == 'POST':
        for key, value in request.form.items():
            execute_db("UPDATE settings SET value = ? WHERE key = ?", (value, key))
        flash('Основные настройки успешно обновлены!', 'success')
        return redirect(url_for('settings_general'))
    settings = query_db("SELECT key, value, description FROM settings ORDER BY key")
    general_keys = (
        'bot_token', 'project_name', 'admin_ids', 'support_link',
        'yookassa_shop_id', 'yookassa_secret_key',
        'admin_web_password',
        'email_domain', 'trial_days'
    )
    general_settings = [s for s in settings if s['key'] in general_keys]
    return render_template('settings_general.html', settings=general_settings)

@app.route('/settings/texts', methods=['GET', 'POST'])
@login_required
def settings_texts():
    if request.method == 'POST':
        for key, value in request.form.items():
            execute_db("UPDATE settings SET value = ? WHERE key = ?", (value, key))
        flash('Тексты успешно обновлены!', 'success')
        return redirect(url_for('settings_texts'))
    settings = query_db("SELECT key, value, description FROM settings ORDER BY key")
    # Группируем тексты по категориям
    grouped_settings = {
        "Тексты: Приветствие и меню": [],
        "Тексты: Оплата": [],
        "Тексты: Промокоды": [],
        "Тексты: Инструкции и прочее": [],
        "Тексты: Кнопки": [],
        "Ссылки на приложения": [],
        "Пошаговая инструкция": []  # Новая группа
    }
    for setting in settings:
        if (setting['key'].startswith('text_welcome') or
                setting['key'].startswith('text_sub') or
                setting['key'].startswith('text_no_active') or
                setting['key'] == 'text_subscription_expiring' or
                setting['key'] == 'text_subscription_expired' or
                setting['key'] == 'text_subscription_expired_main'):
            grouped_settings["Тексты: Приветствие и меню"].append(setting)
        elif setting['key'].startswith('text_payment'):
            grouped_settings["Тексты: Оплата"].append(setting)
        elif setting['key'].startswith('text_promo'):
            grouped_settings["Тексты: Промокоды"].append(setting)
        elif (setting['key'].startswith('text_android') or setting['key'].startswith('text_ios') 
              or setting['key'].startswith('text_about') or setting['key'].startswith('text_trial_success')):
            grouped_settings["Тексты: Инструкции и прочее"].append(setting)
        elif setting['key'].startswith('btn_'):
            grouped_settings["Тексты: Кнопки"].append(setting)
        elif setting['key'] in ('android_app_link', 'ios_app_link'):
            grouped_settings["Ссылки на приложения"].append(setting)
        elif setting['key'] in (
            'step_guide_1_text', 'step_guide_android_url', 'step_guide_ios_url',
            'step_guide_2_text', 'step_guide_3_text', 'step_guide_4_text', 'step_guide_5_text',
            'step_guide_btn_android', 'step_guide_btn_ios', 'step_guide_btn_next', 'step_guide_btn_back'):
            grouped_settings["Пошаговая инструкция"].append(setting)
    return render_template('settings_texts.html', grouped_settings=grouped_settings)

async def check_server_status_async(server_config):
    """Асинхронно проверяет статус одного сервера."""
    try:
        # Используем get_client, так как он уже содержит логику проверки
        client = await xui_manager_instance.get_client(server_config)
        return client is not None
    except Exception as e:
        app.logger.error(f"Ошибка при проверке статуса сервера {server_config.get('name')}: {e}")
        return False

@app.route('/settings/servers', methods=['GET', 'POST'])
@login_required
def settings_servers():
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers_list = json.loads(servers_row['value']) if servers_row else []

    async def get_all_statuses():
        await app_conf.load_settings()
        results = []
        for s in servers_list:
            status = False
            stats = None
            try:
                client = await xui_manager_instance.get_client(s)
                if client:
                    status = True
                    # Попробуем получить статистику, если реализовано
                    try:
                        stats = await xui_manager_instance.get_server_stats(s)
                    except Exception:
                        stats = None
            except Exception:
                status = False
                stats = None
            results.append((status, stats))
        return results

    # Запускаем асинхронную проверку статусов
    try:
        statuses_stats = asyncio.run(get_all_statuses())
        for server, (status, stats) in zip(servers_list, statuses_stats):
            server['status'] = status
            server['stats'] = stats
    except Exception as e:
        app.logger.error(f"Ошибка при запуске проверки статусов серверов: {e}")
        flash("Не удалось проверить статусы серверов.", "warning")
        for s in servers_list:
            s['status'] = None
            s['stats'] = None

    return render_template('settings_servers.html', servers=servers_list)

@app.route('/settings/servers/edit/<int:server_id>', methods=['GET', 'POST'])
@login_required
def edit_server(server_id):
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers_list = json.loads(servers_row['value'])
    
    server_to_edit = next((s for s in servers_list if s['id'] == server_id), None)
    if not server_to_edit:
        flash(f'Сервер с ID {server_id} не найден.', 'danger')
        return redirect(url_for('settings_servers'))

    if request.method == 'POST':
        # Обновляем данные сервера
        server_to_edit['name'] = request.form['name']
        server_to_edit['url'] = request.form['url']
        server_to_edit['port'] = int(request.form['port'])
        server_to_edit['secret_path'] = request.form['secret_path']
        server_to_edit['username'] = request.form['username']
        server_to_edit['password'] = request.form['password']
        server_to_edit['inbound_id'] = int(request.form['inbound_id'])
        server_to_edit['public_host'] = request.form['public_host']
        server_to_edit['public_port'] = int(request.form['public_port'])
        server_to_edit['sub_path_prefix'] = request.form['sub_path_prefix']
        # Новые поля для распределения
        server_to_edit['exclude_from_auto'] = bool(int(request.form.get('exclude_from_auto', 0)))
        server_to_edit['max_clients'] = int(request.form.get('max_clients', 0))
        server_to_edit['priority'] = int(request.form.get('priority', 0))
        
        # Сохраняем обновленный список
        updated_servers_json = json.dumps(servers_list, indent=4)
        execute_db("UPDATE settings SET value = ? WHERE key = 'xui_servers'", (updated_servers_json,))
        flash(f"Сервер '{server_to_edit['name']}' успешно обновлен! <b>Не забудьте перезагрузить настройки в боте</b>.", 'success')
        return redirect(url_for('settings_servers'))

    return render_template('server_form.html', server=server_to_edit, title="Редактировать сервер")


@app.route('/settings/servers/add', methods=['GET', 'POST'])
@login_required
def add_server():
    if request.method == 'POST':
        servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
        servers_list = json.loads(servers_row['value']) if servers_row else []
        
        # Находим максимальный существующий ID и добавляем 1
        new_id = max([s['id'] for s in servers_list] + [0]) + 1
        
        new_server = {
            'id': new_id,
            'name': request.form['name'],
            'url': request.form['url'],
            'port': int(request.form['port']),
            'secret_path': request.form['secret_path'],
            'username': request.form['username'],
            'password': request.form['password'],
            'inbound_id': int(request.form['inbound_id']),
            'public_host': request.form['public_host'],
            'public_port': int(request.form['public_port']),
            'sub_path_prefix': request.form['sub_path_prefix'],
            # Новые поля для распределения
            'exclude_from_auto': bool(int(request.form.get('exclude_from_auto', 0))),
            'max_clients': int(request.form.get('max_clients', 0)),
            'priority': int(request.form.get('priority', 0))
        }
        servers_list.append(new_server)
        
        updated_servers_json = json.dumps(servers_list, indent=4)
        execute_db("UPDATE settings SET value = ? WHERE key = 'xui_servers'", (updated_servers_json,))
        flash(f"Сервер '{new_server['name']}' успешно добавлен! <b>Не забудьте перезагрузить настройки в боте</b>.", 'success')
        return redirect(url_for('settings_servers'))

    return render_template('server_form.html', server={}, title="Добавить сервер")

@app.route('/settings/servers/delete/<int:server_id>', methods=['POST'])
@login_required
def delete_server(server_id):
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers_list = json.loads(servers_row['value'])
    
    server_to_delete = next((s for s in servers_list if s['id'] == server_id), None)
    if not server_to_delete:
        flash(f'Сервер с ID {server_id} не найден.', 'danger')
        return redirect(url_for('settings_servers'))
        
    servers_list = [s for s in servers_list if s['id'] != server_id]
    
    updated_servers_json = json.dumps(servers_list, indent=4)
    execute_db("UPDATE settings SET value = ? WHERE key = 'xui_servers'", (updated_servers_json,))
    flash(f"Сервер '{server_to_delete['name']}' успешно удален! <b>Не забудьте перезагрузить настройки в боте</b>.", 'success')
    return redirect(url_for('settings_servers'))

@app.route('/promo')
@login_required
def promo_list():
    page = request.args.get('page', 1, type=int)
    per_page = 20  # 20 кодов на страницу
    offset = (page - 1) * per_page

    promo_codes = query_db(
        "SELECT code, is_active, activated_by_telegram_id, activated_at, days FROM promo_codes ORDER BY is_active DESC, code ASC LIMIT ? OFFSET ?",
        (per_page, offset)
    )
    
    total_codes = query_db("SELECT COUNT(*) FROM promo_codes", one=True)[0]
    total_pages = (total_codes + per_page - 1) // per_page

    return render_template('promo_list.html', promo_codes=promo_codes, page=page, total_pages=total_pages)

@app.route('/promo/create', methods=['POST'])
@login_required
def promo_create():
    count = int(request.form.get('count', 1))
    days = int(request.form.get('days', 30))
    length = 8  # фиксированная длина
    new_codes = []
    for _ in range(count):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
        execute_db("INSERT INTO promo_codes (code, is_active, days) VALUES (?, 1, ?)", (code, days))
        new_codes.append(code)
    flash(f'Создано {count} новых промокодов на {days} дней.', 'success')
    return redirect(url_for('promo_list'))

@app.route('/promo/export')
@login_required
def promo_export():
    promo_codes = query_db("SELECT code, is_active, days FROM promo_codes")
    lines = []
    for row in promo_codes:
        status = 'Активен' if row['is_active'] else 'Использован'
        days = row['days'] if 'days' in row.keys() else 30
        lines.append(f"{row['code']}\t{status}\t{days} дней")
    codes = '\n'.join(lines)
    return codes, 200, {'Content-Type': 'text/plain; charset=utf-8', 'Content-Disposition': 'attachment; filename=promo_codes.txt'}

@app.route('/users/<int:telegram_id>/renew', methods=['POST'])
@login_required
def renew_subscription(telegram_id):
    try:
        days_to_add = int(request.form.get('days', 0))
        admin_message = request.form.get('admin_message', '').strip()
        if days_to_add <= 0:
            flash('Количество дней должно быть положительным числом.', 'danger')
            return redirect(url_for('user_details', telegram_id=telegram_id))
    except (ValueError, TypeError):
        flash('Неверное количество дней.', 'danger')
        return redirect(url_for('user_details', telegram_id=telegram_id))

    # Запускаем асинхронную задачу для обновления подписки
    async def do_renew():
        # Загружаем настройки, так как мы в новом потоке/контексте
        await app_conf.load_settings()
        
        # Получаем текущий лимит устройств пользователя
        user = await db_helpers.get_active_subscription(telegram_id)
        current_limit_ip = user.get('limit_ip', 0) if user else 0
        result = await grant_subscription(telegram_id, days_to_add, limit_ip=current_limit_ip)
        
        if result and result.get('expiry_date'):
            app.logger.info(f"Подписка для пользователя {telegram_id} успешно продлена через веб-админку.")
            return True, result['expiry_date']
        else:
            app.logger.error(f"Ошибка продления подписки для {telegram_id} через веб-админку.")
            return False, None

    try:
        # Запускаем async функцию и получаем результат
        success, new_expiry_date = asyncio.run(do_renew())
        if success:
            flash(f'Подписка для пользователя {telegram_id} успешно продлена до {new_expiry_date.strftime("%d.%m.%Y %H:%M")}.', 'success')
            # Отправляем уведомление пользователю
            text = f"Ваша подписка продлена до: <b>{new_expiry_date.strftime('%d.%m.%Y')}</b>"
            if admin_message:
                text += f"\n\nСообщение от администратора:\n{admin_message}"
            reply_markup = get_back_to_main_keyboard()
            try:
                asyncio.run(send_telegram_message(int(telegram_id), text, reply_markup=reply_markup))
            except Exception as e:
                app.logger.error(f"Ошибка отправки уведомления о продлении: {e}")
        else:
            flash(f'Произошла ошибка при продлении подписки для пользователя {telegram_id}. См. логи.', 'danger')

    except Exception as e:
        flash(f'Критическая ошибка при запуске задачи продления: {e}', 'danger')
        app.logger.error(f"Критическая ошибка в renew_subscription для {telegram_id}: {e}", exc_info=True)
        
    return redirect(url_for('user_details', telegram_id=telegram_id))

@app.route('/send_news', methods=['POST'])
@login_required
def send_news():
    user_ids = list(set(request.form.getlist('user_ids')))
    news_text = request.form.get('news_text', '').strip()
    add_renew_btn = 'add_renew_btn' in request.form
    add_promo_btn = 'add_promo_btn' in request.form
    
    if not user_ids or not news_text:
        flash('Выберите хотя бы одного пользователя и введите текст новости.', 'danger')
        return redirect(url_for('users_list'))
    
    async def send_messages():
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        import aiosqlite
        
        # Получаем активные тарифы из базы данных
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM tariffs WHERE is_active = 1 ORDER BY sort_order, id") as cursor:
                active_tariffs = await cursor.fetchall()
        
        for uid in user_ids:
            try:
                reply_markup = None
                buttons = []
                
                if add_renew_btn:
                    if active_tariffs:
                        # Добавляем кнопки для каждого активного тарифа
                        for tariff in active_tariffs:
                            price_display = int(tariff['price']) if tariff['price'].is_integer() else tariff['price']
                            tariff_name = tariff['name'] if tariff['name'] else f"{tariff['days']} дней"
                            buttons.append([InlineKeyboardButton(
                                text=f"💳 {tariff_name} - {price_display} {tariff['currency']}",
                                callback_data=f"renew_sub_{tariff['days']}_{tariff['price']}"
                            )])
                    else:
                        # Fallback к старым настройкам если нет активных тарифов
                        def get_setting(key, default=''):
                            row = query_db("SELECT value FROM settings WHERE key = ?", (key,), one=True)
                            return row['value'] if row and row['value'] else default
                        
                        sub_days = int(get_setting('subscription_days', 30))
                        sub_price = float(get_setting('subscription_price', 0.0))
                        sub_currency = get_setting('subscription_currency', 'RUB')
                        price_display = int(sub_price) if sub_price == int(sub_price) else sub_price
                        renew_btn_text = get_setting('btn_renew_sub', '🔄 Продлить подписку').format(
                            days=sub_days,
                            price=price_display,
                            currency=sub_currency
                        )
                        buttons.append([InlineKeyboardButton(text=renew_btn_text, callback_data='renew_sub')])
                
                if add_promo_btn:
                    promo_btn_text = query_db("SELECT value FROM settings WHERE key = 'btn_activate_code'", one=True)
                    promo_btn_text = promo_btn_text['value'] if promo_btn_text and promo_btn_text['value'] else '🎁 Активировать промокод'
                    buttons.append([InlineKeyboardButton(text=promo_btn_text, callback_data='activate_promo_code_prompt')])
                
                if buttons:
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
                
                await send_telegram_message(int(uid), news_text, reply_markup=reply_markup)
            except Exception as e:
                print(f'Ошибка отправки {uid}: {e}')
    
    asyncio.run(send_messages())
    flash(f'Новость отправлена {len(user_ids)} пользователям!', 'success')
    return redirect(url_for('users_list'))

@app.route('/settings/backup', methods=['GET', 'POST'])
@login_required
def settings_backup():
    # Получаем текущие настройки
    row = query_db("SELECT * FROM backup_settings LIMIT 1", one=True)
    if request.method == 'POST':
        admin_telegram_id = request.form.get('admin_telegram_id', '').strip()
        schedule = request.form.get('schedule', '').strip()
        enabled = 1 if request.form.get('enabled') == 'on' else 0
        execute_db(
            "UPDATE backup_settings SET admin_telegram_id=?, schedule=?, enabled=? WHERE id=?",
            (admin_telegram_id, schedule, enabled, row['id'])
        )
        flash('Настройки бэкапа успешно обновлены!', 'success')
        return redirect(url_for('settings_backup'))
    return render_template('settings_backup.html', backup=row)

@app.route('/manual_backup', methods=['POST'])
@login_required
def manual_backup():
    # Путь к базе данных
    db_path = DATABASE_PATH
    backup_path = db_path + '.backup'
    import shutil
    shutil.copy2(db_path, backup_path)

    # Получаем Telegram ID администратора из backup_settings
    row = query_db("SELECT * FROM backup_settings LIMIT 1", one=True)
    if not row or not row['admin_telegram_id']:
        flash('Не указан Telegram ID администратора для бэкапа!', 'danger')
        return redirect(url_for('settings_backup'))
    admin_id = int(row['admin_telegram_id'])

    # Отправляем файл в Telegram
    async def send_backup():
        from aiogram import Bot
        from aiogram.types.input_file import FSInputFile
        bot_token_row = query_db("SELECT value FROM settings WHERE key = 'bot_token'", one=True)
        bot_token = bot_token_row['value'] if bot_token_row else None
        if not bot_token:
            return False
        bot = Bot(token=bot_token)
        await bot.send_document(admin_id, FSInputFile(backup_path), caption='Бэкап базы данных')
        await bot.session.close()
        return True

    import asyncio
    try:
        ok = asyncio.run(send_backup())
        if ok:
            flash('Бэкап отправлен администратору в Telegram!', 'success')
            # Обновляем last_backup
            from datetime import datetime
            execute_db("UPDATE backup_settings SET last_backup=? WHERE id=?", (datetime.now().isoformat(sep=' ', timespec='seconds'), row['id']))
        else:
            flash('Ошибка отправки бэкапа в Telegram!', 'danger')
    except Exception as e:
        flash(f'Ошибка при отправке бэкапа: {e}', 'danger')

    return redirect(url_for('settings_backup'))

def do_auto_backup():
    try:
        print('[AUTO BACKUP] Запуск фоновой задачи...')
        # Получаем настройки
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM backup_settings LIMIT 1")
        row = cur.fetchone()
        print(f'[AUTO BACKUP] Настройки: {dict(row) if row else None}')
        if not row or not row['enabled']:
            print('[AUTO BACKUP] Автобэкап выключен или нет строки в таблице.')
            conn.close()
            return
        admin_id = row['admin_telegram_id']
        schedule = row['schedule']
        last_backup = row['last_backup']
        if not admin_id or not schedule:
            print('[AUTO BACKUP] Не указан admin_id или schedule.')
            conn.close()
            return
        # Проверяем, пора ли делать бэкап
        from datetime import datetime, time
        now = datetime.now()
        try:
            backup_time = datetime.strptime(schedule, '%H:%M').time()
        except Exception as e:
            print(f'[AUTO BACKUP] Ошибка парсинга времени: {e}')
            conn.close()
            return
        # Если уже делали бэкап сегодня — не делаем
        if last_backup:
            try:
                last_dt = datetime.strptime(last_backup[:16], '%Y-%m-%d %H:%M')
                if last_dt.date() == now.date() and now.time() < (datetime.combine(now.date(), backup_time) + timedelta(minutes=10)).time():
                    print('[AUTO BACKUP] Бэкап уже был сегодня, ждем следующего дня.')
                    conn.close()
                    return
            except Exception as e:
                print(f'[AUTO BACKUP] Ошибка парсинга last_backup: {e}')
        # Если текущее время >= времени бэкапа и < времени бэкапа + 10 минут
        if backup_time <= now.time() < (datetime.combine(now.date(), backup_time) + timedelta(minutes=10)).time():
            print('[AUTO BACKUP] Время бэкапа!')
            # Делаем бэкап
            db_path = DATABASE_PATH
            backup_path = db_path + '.backup'
            import shutil
            shutil.copy2(db_path, backup_path)
            # Получаем токен
            cur.execute("SELECT value FROM settings WHERE key = 'bot_token'")
            bot_token_row = cur.fetchone()
            bot_token = bot_token_row['value'] if bot_token_row else None
            if not bot_token:
                print('[AUTO BACKUP] Нет bot_token!')
                conn.close()
                return
            # Отправляем файл
            async def send_backup():
                from aiogram import Bot
                from aiogram.types.input_file import FSInputFile
                bot = Bot(token=bot_token)
                await bot.send_document(int(admin_id), FSInputFile(backup_path), caption='Автоматический бэкап базы данных')
                await bot.session.close()
                return True
            import asyncio
            try:
                asyncio.run(send_backup())
                print('[AUTO BACKUP] Бэкап успешно отправлен!')
                # Обновляем last_backup
                cur.execute("UPDATE backup_settings SET last_backup=? WHERE id=?", (now.isoformat(sep=' ', timespec='seconds'), row['id']))
                conn.commit()
            except Exception as e:
                print(f'[AUTO BACKUP] Ошибка отправки бэкапа: {e}')
        else:
            print(f'[AUTO BACKUP] Сейчас {now.time()}, ждем {backup_time}')
        conn.close()
    except Exception as e:
        print(f'[AUTO BACKUP] Ошибка: {e}')

@app.route('/news_templates')
@login_required
def news_templates_list():
    templates = query_db("SELECT * FROM news_templates ORDER BY id DESC")
    return render_template('news_templates_list.html', templates=templates)

@app.route('/news_templates/add', methods=['GET', 'POST'])
@login_required
def news_template_add():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        body = request.form.get('body', '').strip()
        if not title or not body:
            flash('Заполните все поля!', 'danger')
            return redirect(url_for('news_template_add'))
        execute_db(
            "INSERT INTO news_templates (title, body, created_at) VALUES (?, ?, ?)",
            (title, body, datetime.now().isoformat())
        )
        flash('Шаблон успешно добавлен!', 'success')
        return redirect(url_for('news_templates_list'))
    return render_template('news_template_form.html', template=None, action='add')

@app.route('/news_templates/edit/<int:template_id>', methods=['GET', 'POST'])
@login_required
def news_template_edit(template_id):
    template = query_db("SELECT * FROM news_templates WHERE id = ?", (template_id,), one=True)
    if not template:
        abort(404)
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        body = request.form.get('body', '').strip()
        if not title or not body:
            flash('Заполните все поля!', 'danger')
            return redirect(url_for('news_template_edit', template_id=template_id))
        execute_db(
            "UPDATE news_templates SET title = ?, body = ? WHERE id = ?",
            (title, body, template_id)
        )
        flash('Шаблон успешно обновлен!', 'success')
        return redirect(url_for('news_templates_list'))
    return render_template('news_template_form.html', template=template, action='edit')

@app.route('/news_templates/delete/<int:template_id>', methods=['POST'])
@login_required
def news_template_delete(template_id):
    template = query_db("SELECT * FROM news_templates WHERE id = ?", (template_id,), one=True)
    if not template:
        abort(404)
    execute_db("DELETE FROM news_templates WHERE id = ?", (template_id,))
    flash('Шаблон удалён.', 'success')
    return redirect(url_for('news_templates_list'))

@app.route('/api/server_statuses')
@login_required
def api_server_statuses():
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers_list = json.loads(servers_row['value']) if servers_row else []

    async def get_all_statuses():
        await app_conf.load_settings()
        results = []
        for s in servers_list:
            status = False
            stats = None
            try:
                client = await xui_manager_instance.get_client(s)
                if client:
                    status = True
                    try:
                        stats = await xui_manager_instance.get_server_stats(s)
                    except Exception:
                        stats = None
            except Exception:
                status = False
                stats = None
            results.append({
                'id': s.get('id'),
                'status': status,
                'stats': stats or {},
            })
        return results

    try:
        statuses = asyncio.run(get_all_statuses())
        return jsonify({'servers': statuses})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/migration', methods=['GET', 'POST'])
@login_required
def migration():
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers = json.loads(servers_row['value']) if servers_row else []
    users = []
    selected_from = request.args.get('from_server', type=int)
    selected_to = request.args.get('to_server', type=int)
    admin_message = request.form.get('admin_message', '') if request.method == 'POST' else ''
    migration_result = None
    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page
    total_users = 0
    total_pages = 1
    if selected_from and selected_to and request.method == 'GET':
        total_users = query_db("SELECT COUNT(*) FROM users WHERE current_server_id = ?", (selected_from,), one=True)[0]
        total_pages = (total_users + per_page - 1) // per_page
        users = query_db(
            "SELECT * FROM users WHERE current_server_id = ? LIMIT ? OFFSET ?",
            (selected_from, per_page, offset)
        )
    if request.method == 'POST':
        selected_from = int(request.form.get('from_server'))
        selected_to = int(request.form.get('to_server'))
        # Для миграции всегда берём всех пользователей (без пагинации)
        users = query_db(
            "SELECT * FROM users WHERE current_server_id = ?",
            (selected_from,)
        )
        # Миграция
        import asyncio
        from datetime import datetime
        from x_ui_manager import xui_manager_instance
        from subscription_manager import get_subscription_link
        from tg_sender import send_telegram_message
        async def do_migration():
            migrated = []
            failed = []
            error_details = []
            new_server = next((s for s in servers if s['id'] == selected_to), None)
            old_server = next((s for s in servers if s['id'] == selected_from), None)
            for user in users:
                try:
                    expiry_dt = datetime.fromisoformat(user['subscription_end_date'])
                    now = datetime.now(expiry_dt.tzinfo)
                    days_left = math.ceil((expiry_dt - now).total_seconds() / 86400)
                    if days_left < 1:
                        days_left = 1
                    try:
                        xui_user = await xui_manager_instance.create_xui_user(new_server, user['telegram_id'], days_left)
                        if not xui_user:
                            failed.append(user)
                            error_details.append(f"ID: {user['telegram_id']} | {user['username']} — create_xui_user вернул None")
                            continue
                    except Exception as e:
                        if "already exists" in str(e).lower() or "уже существует" in str(e).lower():
                            migrated.append(user)
                            continue
                        failed.append(user)
                        error_details.append(f"ID: {user['telegram_id']} | {user['username']} — {e}")
                        continue
                    if old_server:
                        await xui_manager_instance.delete_xui_user(old_server, user['xui_client_uuid'])
                    await db_helpers.update_user_subscription(
                        telegram_id=user['telegram_id'],
                        xui_client_uuid=xui_user['uuid'],
                        xui_client_email=xui_user['email'],
                        subscription_end_date=expiry_dt,
                        server_id=selected_to,
                        is_trial=bool(user['is_trial_used'])
                    )
                    sub_link = get_subscription_link(new_server, xui_user['uuid'])
                    text = (
                        'Ваш VPN перенесён на новый сервер.\n'
                        f'{admin_message}\n\n'
                        'Ваша новая ссылка:\n'
                        f'<blockquote><code>{sub_link}</code></blockquote>'
                    )
                    reply_markup = get_back_to_main_keyboard()
                    try:
                        await send_telegram_message(user['telegram_id'], text, reply_markup=reply_markup)
                    except Exception as e:
                        # Если ошибка Telegram 'chat not found', считаем перенос успешным
                        if 'chat not found' in str(e).lower():
                            migrated.append(user)
                            continue
                        failed.append(user)
                        error_details.append(f"ID: {user['telegram_id']} | {user['username']} — {e}")
                        continue
                    migrated.append(user)
                except Exception as e:
                    failed.append(user)
                    error_details.append(f"ID: {user['telegram_id']} | {user['username']} — {e}")
            return migrated, failed, error_details
        migrated, failed, error_details = asyncio.run(do_migration())
        migration_result = {'migrated': migrated, 'failed': failed, 'error_details': error_details}
    return render_template('migration.html', servers=servers, users=users, selected_from=selected_from, selected_to=selected_to, admin_message=admin_message, migration_result=migration_result, page=page, total_pages=total_pages)

@app.route('/users/<int:telegram_id>/delete', methods=['POST'])
@login_required
def delete_user(telegram_id):
    user = query_db("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
    if not user:
        flash('Пользователь не найден.', 'danger')
        return redirect(url_for('users_list'))
    # Удаляем из XUI, если есть uuid и сервер
    servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
    servers = json.loads(servers_row['value']) if servers_row else []
    old_server = next((s for s in servers if s['id'] == user['current_server_id']), None)
    try:
        if old_server and user['xui_client_uuid']:
            import asyncio
            from x_ui_manager import xui_manager_instance
            asyncio.run(xui_manager_instance.delete_xui_user(old_server, user['xui_client_uuid']))
    except Exception as e:
        # Игнорируем ошибку удаления с XUI
        pass
    # Удаляем из БД
    execute_db("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    flash('Пользователь полностью удалён.', 'success')
    return redirect(url_for('users_list'))

@app.route('/payments')
@login_required
def payments():
    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page
    status_filter = request.args.get('status', 'all')
    if status_filter == 'succeeded':
        payments = query_db("SELECT * FROM payments WHERE status = 'succeeded' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
        total = query_db("SELECT COUNT(*) FROM payments WHERE status = 'succeeded'", (), one=True)[0]
    elif status_filter == 'failed':
        payments = query_db("SELECT * FROM payments WHERE status != 'succeeded' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
        total = query_db("SELECT COUNT(*) FROM payments WHERE status != 'succeeded'", (), one=True)[0]
    else:
        payments = query_db("SELECT * FROM payments ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
        total = query_db("SELECT COUNT(*) FROM payments", (), one=True)[0]
    total_pages = (total + per_page - 1) // per_page
    # Получаем username для каждого платежа
    user_ids = [p['telegram_id'] for p in payments]
    users = {}
    if user_ids:
        q = f"SELECT telegram_id, username FROM users WHERE telegram_id IN ({','.join(['?']*len(user_ids))})"
        for u in query_db(q, user_ids):
            users[u['telegram_id']] = u['username']
    # Статистика успешных платежей
    stats = {}
    # Всего
    row = query_db("SELECT COUNT(*), SUM(amount) FROM payments WHERE status = 'succeeded'", (), one=True)
    stats['all_count'] = row[0] or 0
    stats['all_sum'] = row[1] or 0
    # За месяц
    row = query_db("SELECT COUNT(*), SUM(amount) FROM payments WHERE status = 'succeeded' AND created_at >= datetime('now', '-1 month')", (), one=True)
    stats['month_count'] = row[0] or 0
    stats['month_sum'] = row[1] or 0
    # За неделю
    row = query_db("SELECT COUNT(*), SUM(amount) FROM payments WHERE status = 'succeeded' AND created_at >= datetime('now', '-7 days')", (), one=True)
    stats['week_count'] = row[0] or 0
    stats['week_sum'] = row[1] or 0
    return render_template('payments.html', payments=payments, users=users, page=page, total_pages=total_pages, status_filter=status_filter, stats=stats)

@app.route('/api/all_user_ids')
@login_required
def api_all_user_ids():
    ids = query_db("SELECT telegram_id FROM users", ())
    return jsonify({"user_ids": [row[0] for row in ids]})

@app.route('/api/paid_user_ids')
@login_required
def api_paid_user_ids():
    ids = query_db("SELECT DISTINCT telegram_id FROM payments WHERE status = 'succeeded'", ())
    return jsonify({"user_ids": [row[0] for row in ids]})

@app.route('/dev_tools', methods=['GET', 'POST'])
@login_required
def dev_tools():
    result_message = None
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'clear_users':
            # Удаляем всех пользователей из XUI и БД
            servers_row = query_db("SELECT value FROM settings WHERE key = 'xui_servers'", one=True)
            servers = json.loads(servers_row['value']) if servers_row else []
            users = query_db("SELECT telegram_id, xui_client_uuid, current_server_id FROM users", ())
            import asyncio
            from x_ui_manager import xui_manager_instance
            deleted_xui = 0
            failed_xui = 0
            xui_errors = []
            for user in users:
                uuid = user['xui_client_uuid']
                server_id = user['current_server_id']
                if uuid and server_id:
                    server = next((s for s in servers if s['id'] == server_id), None)
                    if server:
                        try:
                            asyncio.run(xui_manager_instance.delete_xui_user(server, uuid))
                            deleted_xui += 1
                        except Exception as e:
                            failed_xui += 1
                            xui_errors.append(f"ID {user['telegram_id']}: {e}")
            execute_db("DELETE FROM users", ())
            result_message = f"Удалено пользователей из БД: {len(users)}<br>Удалено из XUI: {deleted_xui}<br>Ошибок XUI: {failed_xui}"
            if xui_errors:
                result_message += "<br>Ошибки:<br>" + '<br>'.join(xui_errors)
        elif action == 'clear_payments':
            execute_db("DELETE FROM payments", ())
            result_message = "Все платежи удалены."
        elif action == 'generate_fake_users':
            import asyncio
            asyncio.run(app_conf.load_settings())
            from datetime import datetime, timedelta
            try:
                count = int(request.form.get('fake_count', 100))
                if count < 1 or count > 1000:
                    raise ValueError
            except Exception:
                count = 100
            try:
                days = int(request.form.get('fake_days', 10))
                if days < 1 or days > 365:
                    raise ValueError
            except Exception:
                days = 10
            from subscription_manager import grant_subscription
            created = 0
            failed = 0
            for i in range(1, count+1):
                telegram_id = 900000 + i
                username = f'fakeuser{i}'
                # Сначала добавим пользователя в БД (если нет)
                execute_db('''
                    INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)
                ''', (telegram_id, username))
                # Выдаём подписку через grant_subscription (автораспределение и XUI)
                try:
                    res = asyncio.run(grant_subscription(telegram_id, days))
                    if res and res.get('expiry_date'):
                        created += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            result_message = f"Успешно создано {created} фейковых пользователей с подпиской на {days} дней.<br>Ошибок: {failed}";
    return render_template('dev_tools.html', result_message=result_message)

@app.route('/tariffs')
@login_required
def tariffs_list():
    """Список всех тарифов"""
    tariffs = query_db("SELECT * FROM tariffs ORDER BY sort_order, id")
    return render_template('tariffs_list.html', tariffs=tariffs)

@app.route('/tariffs/add', methods=['GET', 'POST'])
@login_required
def tariff_add():
    """Добавление нового тарифа"""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        days = int(request.form.get('days', 0))
        price = float(request.form.get('price', 0))
        currency = request.form.get('currency', 'RUB').strip()
        description = request.form.get('description', '').strip()
        sort_order = int(request.form.get('sort_order', 0))
        limit_ip = int(request.form.get('limit_ip', 0))
        
        if not name or days <= 0 or price <= 0:
            flash('Пожалуйста, заполните все обязательные поля корректно.', 'danger')
            return render_template('tariff_form.html', tariff={}, title="Добавить тариф")
        
        execute_db('''
            INSERT INTO tariffs (name, days, price, currency, description, sort_order, is_active, limit_ip)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        ''', (name, days, price, currency, description, sort_order, limit_ip))
        
        flash(f'Тариф "{name}" успешно добавлен!', 'success')
        return redirect(url_for('tariffs_list'))
    
    return render_template('tariff_form.html', tariff={}, title="Добавить тариф")

@app.route('/tariffs/edit/<int:tariff_id>', methods=['GET', 'POST'])
@login_required
def tariff_edit(tariff_id):
    """Редактирование тарифа"""
    tariff = query_db("SELECT * FROM tariffs WHERE id = ?", (tariff_id,), one=True)
    if not tariff:
        flash('Тариф не найден.', 'danger')
        return redirect(url_for('tariffs_list'))
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        days = int(request.form.get('days', 0))
        price = float(request.form.get('price', 0))
        currency = request.form.get('currency', 'RUB').strip()
        description = request.form.get('description', '').strip()
        sort_order = int(request.form.get('sort_order', 0))
        is_active = bool(request.form.get('is_active'))
        limit_ip = int(request.form.get('limit_ip', 0))
        
        if not name or days <= 0 or price <= 0:
            flash('Пожалуйста, заполните все обязательные поля корректно.', 'danger')
            return render_template('tariff_form.html', tariff=tariff, title="Редактировать тариф")
        
        execute_db('''
            UPDATE tariffs 
            SET name = ?, days = ?, price = ?, currency = ?, description = ?, 
                sort_order = ?, is_active = ?, limit_ip = ?
            WHERE id = ?
        ''', (name, days, price, currency, description, sort_order, int(is_active), limit_ip, tariff_id))
        
        flash(f'Тариф "{name}" успешно обновлен!', 'success')
        return redirect(url_for('tariffs_list'))
    
    return render_template('tariff_form.html', tariff=tariff, title="Редактировать тариф")

@app.route('/tariffs/delete/<int:tariff_id>', methods=['POST'])
@login_required
def tariff_delete(tariff_id):
    """Удаление тарифа"""
    tariff = query_db("SELECT * FROM tariffs WHERE id = ?", (tariff_id,), one=True)
    if not tariff:
        flash('Тариф не найден.', 'danger')
        return redirect(url_for('tariffs_list'))
    
    execute_db("DELETE FROM tariffs WHERE id = ?", (tariff_id,))
    flash(f'Тариф "{tariff["name"]}" успешно удален!', 'success')
    return redirect(url_for('tariffs_list'))

@app.route('/tariffs/toggle/<int:tariff_id>', methods=['POST'])
@login_required
def tariff_toggle(tariff_id):
    """Переключение активности тарифа"""
    tariff = query_db("SELECT * FROM tariffs WHERE id = ?", (tariff_id,), one=True)
    if not tariff:
        flash('Тариф не найден.', 'danger')
        return redirect(url_for('tariffs_list'))
    
    new_status = not tariff['is_active']
    execute_db("UPDATE tariffs SET is_active = ? WHERE id = ?", (int(new_status), tariff_id))
    
    status_text = "активирован" if new_status else "деактивирован"
    flash(f'Тариф "{tariff["name"]}" {status_text}!', 'success')
    return redirect(url_for('tariffs_list'))

# --- Запуск приложения ---
if __name__ == '__main__':
    print("="*50)
    print("Запуск веб-админки...")
    print(f"URL: http://127.0.0.1:8080")
    print("Для остановки нажмите Ctrl+C")
    print("="*50)
    # Запуск APScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(do_auto_backup, 'interval', minutes=1)
    scheduler.start()
    # Используем waitress для более стабильной работы
    from waitress import serve
    serve(app, host='0.0.0.0', port=8080) 