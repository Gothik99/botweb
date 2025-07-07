# subscription_manager.py
"""
Этот модуль содержит основную бизнес-логику по управлению подписками.
Он вынесен в отдельный файл, чтобы избежать циклических импортов
между main.py, admin.py и web_admin/run.py.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

from loguru import logger

from app_config import app_conf
import db_helpers
from x_ui_manager import xui_manager_instance


async def choose_best_server() -> Optional[Dict]:
    """
    Выбирает лучший сервер для новой подписки с учётом:
    - exclude_from_auto: сервер исключён из автораспределения
    - max_clients: если лимит достигнут — сервер не участвует
    - priority: сортировка по приоритету (меньше — выше)
    - среди серверов с одинаковым приоритетом — по наименьшему количеству активных клиентов
    """
    xui_servers = app_conf.get('xui_servers', [])
    if not xui_servers:
        logger.error("Список XUI_SERVERS в конфигурации пуст. Невозможно выбрать сервер.")
        return None

    available_servers_with_counts = []
    for server_conf in xui_servers:
        # Пропускаем серверы, исключённые из автораспределения
        if server_conf.get('exclude_from_auto'):
            continue
        try:
            api_client = await xui_manager_instance.get_client(server_conf)
            if not api_client:
                logger.warning(f"Сервер {server_conf['name']} недоступен. Пропускаем.")
                continue

            # Получаем количество активных клиентов из БД (только с действующей подпиской)
            active_clients_count = await db_helpers.get_active_clients_count_for_server(server_conf['id'])
            
            # Пропускаем сервер, если достигнут лимит клиентов
            max_clients = server_conf.get('max_clients', 0)
            if max_clients and active_clients_count is not None and active_clients_count >= max_clients:
                logger.info(f"Сервер {server_conf['name']} достиг лимита активных клиентов ({max_clients}). Пропускаем.")
                continue
            if active_clients_count is not None:
                logger.info(f"Сервер {server_conf['name']}: {active_clients_count} активных клиентов.")
                available_servers_with_counts.append({'config': server_conf, 'count': active_clients_count})
            else:
                logger.warning(f"Не удалось получить количество активных клиентов для сервера {server_conf['name']}. Пропускаем.")
        except Exception as e:
            logger.error(f"Ошибка при проверке сервера {server_conf['name']}: {e}. Пропускаем.")
            continue

    if not available_servers_with_counts:
        logger.error("Нет доступных серверов для выбора.")
        return None

    # Сортировка: сначала по приоритету (меньше — выше), потом по количеству активных клиентов
    available_servers_with_counts.sort(key=lambda x: (x['config'].get('priority', 0), x['count']))
    best_server_data = available_servers_with_counts[0]
    logger.info(f"Выбран сервер: {best_server_data['config']['name']} с {best_server_data['count']} активными клиентами.")
    return best_server_data['config']


def get_subscription_link(server_config: dict, client_uuid: str) -> str:
    """Генерирует публичную ссылку на подписку."""
    protocol = server_config.get('public_protocol', "https")
    public_port = server_config.get('public_port')
    port_str = ""
    if public_port and public_port not in [80, 443, "80", "443", ""]:
        try:
            if int(public_port) not in [80, 443]: port_str = f":{public_port}"
        except ValueError: port_str = f":{public_port}"

    return f"{protocol}://{server_config['public_host']}{port_str}/{server_config['sub_path_prefix'].strip('/')}/{client_uuid}"


async def get_server_config(server_id: int) -> Optional[dict]:
    """Находит конфигурацию сервера по его ID."""
    xui_servers = app_conf.get('xui_servers', [])
    for s_conf in xui_servers:
        if s_conf.get('id') == server_id:
            return s_conf
    logger.warning(f"Конфигурация для server_id {server_id} не найдена.")
    return None


async def grant_subscription(user_id: int, days_to_add: int, is_trial: bool = False, limit_ip: int = 0) -> Optional[Dict]:
    """
    Универсальная функция для создания или продления подписки пользователя.
    Возвращает словарь с датой окончания и ссылкой или None в случае ошибки.
    """
    user_data = await db_helpers.get_last_subscription(user_id)

    # Продление существующей подписки
    if user_data and user_data.get('xui_client_uuid') and user_data.get('current_server_id'):
        client_uuid, server_id = user_data['xui_client_uuid'], user_data['current_server_id']
        server_config = await get_server_config(server_id)
        if not server_config:
            logger.error(f"Не найдена конфигурация сервера {server_id} для продления подписки {user_id}.")
            return None

        server_config['telegram_id'] = user_id
        current_expiry = user_data.get('subscription_end_date')
        
        # Убедимся, что дата aware перед использованием
        if current_expiry and current_expiry.tzinfo is None:
            current_expiry = current_expiry.astimezone()
            
        current_expiry_ms = int(current_expiry.timestamp() * 1000) if current_expiry else None

        # Прокидываем лимит устройств
        server_config['limit_ip'] = limit_ip

        xui_user_data = await xui_manager_instance.update_xui_user_subscription(
            server_settings=server_config, client_uuid=client_uuid, new_days_valid=days_to_add, current_expiry_ms=current_expiry_ms, total_gb=0, limit_ip=limit_ip
        )

        if xui_user_data and xui_user_data.get("uuid"):
            new_expiry_date = datetime.fromtimestamp(xui_user_data["expiry_timestamp_ms"] / 1000, tz=timezone.utc)
            await db_helpers.update_user_subscription(
                telegram_id=user_id, xui_client_uuid=xui_user_data["uuid"], xui_client_email=user_data["xui_client_email"],
                subscription_end_date=new_expiry_date, server_id=server_id, is_trial=is_trial, limit_ip=limit_ip
            )
            return {"expiry_date": new_expiry_date, "sub_link": get_subscription_link(server_config, client_uuid)}
        else:
            logger.error(f"Ошибка продления подписки в X-UI для {user_id}")
            return None
            
    # Создание новой подписки
    else:
        server_config_to_use = await choose_best_server()
        if not server_config_to_use:
            logger.error(f"Не удалось выбрать сервер для новой подписки для {user_id}.")
            return None
        
        # Прокидываем лимит устройств
        server_config_to_use['limit_ip'] = limit_ip

        xui_user_data = await xui_manager_instance.create_xui_user(
            server_settings=server_config_to_use, telegram_id=user_id, days_valid=days_to_add, total_gb=0, limit_ip=limit_ip
        )
        
        if xui_user_data and xui_user_data.get("uuid"):
            expiry_date_dt = datetime.fromtimestamp(xui_user_data["expiry_timestamp_ms"] / 1000, tz=timezone.utc)
            await db_helpers.update_user_subscription(
                telegram_id=user_id, xui_client_uuid=xui_user_data["uuid"], xui_client_email=xui_user_data["email"],
                subscription_end_date=expiry_date_dt, server_id=server_config_to_use['id'], is_trial=is_trial, limit_ip=limit_ip
            )
            sub_link = get_subscription_link(server_config_to_use, xui_user_data["uuid"])
            return {"expiry_date": expiry_date_dt, "sub_link": sub_link}
        else:
            logger.error(f"Ошибка создания новой подписки в X-UI для {user_id}")
            return None 