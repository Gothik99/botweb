# x_ui_manager.py
from py3xui import Api
from py3xui.client import Client as XUIClientObj, Client
from py3xui.inbound import Inbound
from loguru import logger
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import uuid
import random
import asyncio
import db_helpers
from app_config import app_conf # Импортируем наш менеджер настроек

class XUIManager:
    def __init__(self):
        self.clients: Dict[int, Api] = {}

    async def get_client(self, server_settings: Dict) -> Optional[Api]:
        server_id = server_settings['id']
        if server_id in self.clients:
            try:
                status_check = self.clients[server_id].server.get_status()
                if status_check:
                    logger.debug(f"Использование существующего клиента для сервера {server_id}")
                    return self.clients[server_id]
                else:
                    logger.warning(f"Существующий клиент для сервера {server_id} вернул невалидный статус. Создаем новый.")
                    del self.clients[server_id]
            except Exception as e:
                logger.warning(f"Существующий клиент для сервера {server_id} невалиден: {e}. Создаем новый.")
                del self.clients[server_id]
        
        try:
            logger.info(f"Создание X-UI клиента для сервера {server_id} ({server_settings['name']})")
            
            url = server_settings['url']
            if not url.startswith('http'):
                url = f"https://{url}" 
            
            api_url = f"{url}:{server_settings['port']}"
            if server_settings.get('secret_path'):
                 api_url += f"/{server_settings['secret_path'].strip('/')}"
            
            logger.debug(f"API URL для {server_settings['name']}: {api_url}")
            
            client = Api(
                api_url,
                server_settings['username'],
                server_settings['password'],
                use_tls_verify=False 
            )
            
            client.login() 
            inbounds = client.inbound.get_list() 
            logger.info(f"Подключение к {server_settings['name']} успешно. Найдено {len(inbounds) if inbounds else 0} inbounds.")
            
            self.clients[server_id] = client
            return client
            
        except Exception as e:
            logger.error(f"Ошибка при создании X-UI клиента для сервера {server_id} ({server_settings['name']}): {e}")
            return None

    def _find_inbound_by_id(self, client_api: Api, inbound_id: int) -> Optional[Inbound]:
        try:
            inbound = client_api.inbound.get_by_id(inbound_id)
            if inbound:
                if not hasattr(inbound, 'settings') or not inbound.settings:
                    logger.warning(f"Inbound {inbound_id} получен, но не содержит 'settings'.")
                elif not hasattr(inbound.settings, 'clients'):
                    logger.warning(f"Inbound {inbound_id} получен, settings есть, но нет 'clients'.")
            return inbound
        except Exception as e:
            logger.error(f"Ошибка при получении inbound {inbound_id}: {e}")
            return None

    def _find_client_by_email_or_uuid(self, xui_api_client: Api, inbound_id: int, identifier: str) -> Optional[XUIClientObj]:
        try:
            if '@' in identifier:
                client_obj = xui_api_client.client.get_by_email(identifier) 
                if client_obj and client_obj.inbound_id == inbound_id:
                    return client_obj

            try:
                inbound_data = self._find_inbound_by_id(xui_api_client, inbound_id)
                if inbound_data and hasattr(inbound_data, 'settings') and inbound_data.settings and \
                   hasattr(inbound_data.settings, 'clients') and inbound_data.settings.clients:
                    for c in inbound_data.settings.clients:
                        if c.id == identifier: 
                            return c 
            except Exception as e:
                logger.debug(f"Поиск клиента по UUID {identifier} в inbound не удался: {e}")

            inbound = self._find_inbound_by_id(xui_api_client, inbound_id)
            if inbound and hasattr(inbound, 'settings') and inbound.settings and \
               hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                for client_data in inbound.settings.clients: 
                    if client_data.email == identifier or client_data.id == identifier:
                        return client_data
            return None
        except Exception as e:
            logger.error(f"Ошибка при поиске клиента '{identifier}' в inbound {inbound_id}: {e}")
            return None

    async def check_client_exists(self, server_settings: Dict, client_uuid: str) -> bool:
        """Проверяет существование клиента в X-UI по UUID."""
        client_api = await self.get_client(server_settings)
        if not client_api:
            return False # Считаем, что не существует, если сервер недоступен

        try:
            inbound_id = server_settings['inbound_id']
            # Используем внутренний метод поиска
            client_obj = self._find_client_by_email_or_uuid(client_api, inbound_id, client_uuid)
            return client_obj is not None
        except Exception as e:
            logger.error(f"Ошибка при проверке существования клиента {client_uuid} на сервере {server_settings['name']}: {e}")
            return False

    async def recreate_xui_user(self, server_settings: Dict, user_data: Dict) -> bool:
        """
        Восстанавливает пользователя в X-UI с использованием существующих данных.
        user_data должен содержать: uuid, email, expiry_timestamp_ms, telegram_id.
        """
        client_api = await self.get_client(server_settings)
        if not client_api:
            return False

        try:
            inbound_id = server_settings['inbound_id']
            inbound = self._find_inbound_by_id(client_api, inbound_id)
            if not inbound:
                logger.error(f"Inbound {inbound_id} не найден на сервере {server_settings['id']} для восстановления.")
                return False

            flow_value = ""
            if hasattr(inbound, 'stream_settings') and inbound.stream_settings:
                if hasattr(inbound.stream_settings, 'xtls_settings') and inbound.stream_settings.xtls_settings and \
                   hasattr(inbound.stream_settings.xtls_settings, 'flow') and inbound.stream_settings.xtls_settings.flow:
                    flow_value = inbound.stream_settings.xtls_settings.flow
                elif hasattr(inbound.stream_settings, 'reality_settings') and inbound.stream_settings.reality_settings:
                    flow_value = "xtls-rprx-vision"

            client_to_add = Client(
                id=user_data['uuid'],
                email=user_data['email'],
                enable=True,
                flow=flow_value,
                tg_id=str(user_data['telegram_id']),
                total_gb=0, # Восстанавливаем без лимита трафика, как и при создании
                expiry_time=user_data['expiry_timestamp_ms'],
                limit_ip=server_settings.get('default_limit_ip', 0),
                sub_id=user_data['uuid']
            )

            client_api.client.add(inbound_id=inbound_id, clients=[client_to_add])
            logger.info(f"Отправлен запрос на восстановление клиента {user_data['email']} в inbound {inbound_id}.")
            return True

        except Exception as e:
            if "client with this email already exists in this inbound" in str(e).lower():
                logger.warning(f"Попытка восстановить клиента {user_data['email']}, но он уже существует. Ошибка: {e}")
                # Считаем это успехом, т.к. цель - чтобы он был в XUI.
                return True
            logger.error(f"Ошибка при восстановлении пользователя {user_data['email']} в X-UI: {e}")
            return False

    async def create_xui_user(self, server_settings: Dict, telegram_id: int, days_valid: int, total_gb: int = 0, limit_ip: int = 0) -> Optional[Dict[str, Any]]:
        client_api = await self.get_client(server_settings)
        if not client_api:
            return None

        try:
            inbound_id = server_settings['inbound_id']
            inbound = self._find_inbound_by_id(client_api, inbound_id)
            if not inbound:
                logger.error(f"Inbound {inbound_id} не найден на сервере {server_settings['id']}")
                return None

            client_uuid = str(uuid.uuid4())
            unique_suffix = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))
            email = f"tg{telegram_id}_{unique_suffix}@{app_conf.get('email_domain', 'vpn.bot')}"
            
            expiry_time = datetime.now() + timedelta(days=days_valid)
            expiry_timestamp_ms = int(expiry_time.timestamp() * 1000)

            flow_value = ""
            if hasattr(inbound, 'stream_settings') and inbound.stream_settings:
                if hasattr(inbound.stream_settings, 'xtls_settings') and inbound.stream_settings.xtls_settings and \
                   hasattr(inbound.stream_settings.xtls_settings, 'flow') and inbound.stream_settings.xtls_settings.flow:
                    flow_value = inbound.stream_settings.xtls_settings.flow
                elif hasattr(inbound.stream_settings, 'reality_settings') and inbound.stream_settings.reality_settings:
                    flow_value = "xtls-rprx-vision" 

            new_client_obj = Client( 
                id=client_uuid,
                email=email,
                enable=True,
                flow=flow_value,
                tg_id=str(telegram_id),
                total_gb=total_gb,
                expiry_time=expiry_timestamp_ms,
                limit_ip=limit_ip if limit_ip else server_settings.get('default_limit_ip', 0),
                sub_id=client_uuid 
            )

            success = False
            retries = 2 
            for i in range(retries):
                try:
                    logger.debug(f"Попытка {i+1}/{retries} добавления клиента {email} в inbound {inbound_id}...")
                    client_api.client.add(inbound_id=inbound_id, clients=[new_client_obj])
                    success = True
                    logger.info(f"Клиент {email} (UUID: {client_uuid}) успешно создан в X-UI на сервере {server_settings['id']}.")
                    break
                except Exception as e:
                    logger.warning(f"Попытка {i+1}/{retries} добавления клиента не удалась: {e}")
                    if "client with this email already exists in this inbound" in str(e).lower():
                        logger.error(f"Клиент с email {email} уже существует. Генерация нового email не помогла.")
                        return None 
                    if i < retries - 1:
                        await asyncio.sleep(0.5) 
            
            if not success:
                logger.error(f"Не удалось создать клиента {email} в X-UI после {retries} попыток.")
                return None

            return {
                "uuid": client_uuid,
                "email": email,
                "expiry_timestamp_ms": expiry_timestamp_ms,
                "server_id": server_settings['id']
            }

        except Exception as e:
            logger.error(f"Критическая ошибка при создании X-UI пользователя для telegram_id {telegram_id}: {e}")
            logger.exception("Полный стек ошибки:")
            return None

    async def update_xui_user_subscription(self, server_settings: Dict, client_uuid: str, new_days_valid: int, current_expiry_ms: Optional[int] = None, total_gb: int = 0, limit_ip: int = 0) -> Optional[Dict[str, Any]]:
        client_api = await self.get_client(server_settings)
        if not client_api:
            return None

        try:
            inbound_id = server_settings['inbound_id']
            
            telegram_user_id_for_sub = server_settings.get('telegram_id')
            if not telegram_user_id_for_sub:
                 logger.error(f"telegram_id не передан в server_settings для обновления подписки UUID {client_uuid}")
                 return None

            user_db_data = await db_helpers.get_user(telegram_user_id_for_sub)
            if not user_db_data or not user_db_data[3]: 
                logger.error(f"Не удалось получить email клиента из БД для UUID {client_uuid} / TG ID {telegram_user_id_for_sub}")
                return None
            client_email_from_db = user_db_data[3] 

            inbound_obj = self._find_inbound_by_id(client_api, inbound_id)
            if not inbound_obj or not hasattr(inbound_obj, 'settings') or not inbound_obj.settings or \
               not hasattr(inbound_obj.settings, 'clients') or not inbound_obj.settings.clients:
                logger.error(f"Inbound {inbound_id} не найден или не содержит клиентов на сервере {server_settings['name']}.")
                return None

            client_from_xui: Optional[XUIClientObj] = None
            for c_xui in inbound_obj.settings.clients:
                if c_xui.id == client_uuid:
                    client_from_xui = c_xui
                    break
            
            if not client_from_xui:
                logger.warning(f"Клиент UUID {client_uuid} не найден в X-UI inbound {inbound_id} на сервере {server_settings['name']}. Email из БД: {client_email_from_db}")
                logger.info(f"Попытка автоматического восстановления клиента {client_uuid} в X-UI...")
                
                # Попытка восстановления клиента в XUI
                if current_expiry_ms and current_expiry_ms > datetime.now().timestamp() * 1000:
                    base_time = datetime.fromtimestamp(current_expiry_ms / 1000)
                else:
                    base_time = datetime.now()
                
                new_expiry_time = base_time + timedelta(days=new_days_valid)
                new_expiry_timestamp_ms = int(new_expiry_time.timestamp() * 1000)
                
                # Данные для восстановления клиента
                user_data_for_recreation = {
                    'uuid': client_uuid,
                    'email': client_email_from_db,
                    'expiry_timestamp_ms': new_expiry_timestamp_ms,
                    'telegram_id': telegram_user_id_for_sub
                }
                
                # Пытаемся восстановить клиента
                recreation_success = await self.recreate_xui_user(server_settings, user_data_for_recreation)
                if recreation_success:
                    logger.info(f"Клиент {client_uuid} успешно восстановлен в X-UI. Продолжаем обновление подписки...")
                    # Повторно получаем данные клиента из X-UI после восстановления
                    inbound_obj = self._find_inbound_by_id(client_api, inbound_id)
                    if inbound_obj and hasattr(inbound_obj, 'settings') and inbound_obj.settings and \
                       hasattr(inbound_obj.settings, 'clients') and inbound_obj.settings.clients:
                        for c_xui in inbound_obj.settings.clients:
                            if c_xui.id == client_uuid:
                                client_from_xui = c_xui
                                break
                
                if not client_from_xui:
                    logger.error(f"Не удалось восстановить клиента {client_uuid} в X-UI. Создание новой подписки.")
                    return None
            
            actual_uuid_from_xui = client_from_xui.id

            if current_expiry_ms and current_expiry_ms > datetime.now().timestamp() * 1000:
                base_time = datetime.fromtimestamp(current_expiry_ms / 1000)
            else:
                base_time = datetime.now()

            new_expiry_time = base_time + timedelta(days=new_days_valid)
            new_expiry_timestamp_ms = int(new_expiry_time.timestamp() * 1000)

            updated_client_obj = Client(
                id=actual_uuid_from_xui,
                email=client_from_xui.email,
                enable=True, 
                flow=client_from_xui.flow if hasattr(client_from_xui, 'flow') else "",
                tg_id=client_from_xui.tg_id if hasattr(client_from_xui, 'tg_id') else str(telegram_user_id_for_sub),
                total_gb=total_gb, 
                expiry_time=new_expiry_timestamp_ms, 
                limit_ip=limit_ip if limit_ip else (client_from_xui.limit_ip if hasattr(client_from_xui, 'limit_ip') else server_settings.get('default_limit_ip', 0)),
                sub_id=client_from_xui.sub_id if hasattr(client_from_xui, 'sub_id') else actual_uuid_from_xui,
                up=client_from_xui.up if hasattr(client_from_xui, 'up') else 0,
                down=client_from_xui.down if hasattr(client_from_xui, 'down') else 0,
                inbound_id=inbound_id
            )

            logger.info(f"Попытка обновления клиента UUID {actual_uuid_from_xui} (email={updated_client_obj.email}): Expiry до {new_expiry_time}, TotalGB={total_gb}")
            logger.debug(f"Детали обновляемого клиента: {updated_client_obj.model_dump_json(indent=2)}")
            
            try:
                client_api.client.update(client_uuid=actual_uuid_from_xui, client=updated_client_obj)
                
                logger.info(f"Клиент UUID {actual_uuid_from_xui} (email {updated_client_obj.email}) успешно обновлен на сервере {server_settings['id']}.")
                return {
                    "uuid": actual_uuid_from_xui,
                    "email": client_email_from_db,
                    "expiry_timestamp_ms": new_expiry_timestamp_ms,
                    "server_id": server_settings['id']
                }
                    
            except Exception as e:
                logger.error(f"Ошибка при вызове client.update для UUID {actual_uuid_from_xui}: {str(e)}")
                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    logger.error(f"Ответ API: {e.response.text}")
                elif hasattr(e, 'response'):
                     logger.error(f"Ответ API (без текста): {e.response}")
                return None 

        except Exception as e:
            logger.error(f"Критическая ошибка при обновлении X-UI пользователя {client_uuid}: {e}")
            logger.exception("Полный стек ошибки:")
            return None

    async def delete_xui_user(self, server_settings: Dict, client_uuid_or_email: str) -> bool:
        client_api = await self.get_client(server_settings)
        if not client_api:
            return False

        try:
            inbound_id = server_settings['inbound_id']
            logger.info(f"Попытка удаления клиента '{client_uuid_or_email}' из inbound {inbound_id} на сервере {server_settings['id']}")
            
            is_uuid = False
            try:
                uuid.UUID(client_uuid_or_email, version=4)
                is_uuid = True
            except ValueError:
                pass 

            success = False
            if is_uuid:
                try:
                    client_api.client.delete(inbound_id=inbound_id, client_uuid=client_uuid_or_email)
                    success = True
                except Exception as e:
                    logger.error(f"Ошибка при удалении клиента по UUID {client_uuid_or_email}: {e}")
                    return False
            else: 
                found_client = self._find_client_by_email_or_uuid(client_api, inbound_id, client_uuid_or_email)
                if found_client and found_client.id:
                    logger.info(f"Найден UUID {found_client.id} для email {client_uuid_or_email}. Удаляем по UUID.")
                    try:
                        client_api.client.delete(inbound_id=inbound_id, client_uuid=found_client.id)
                        success = True
                    except Exception as e:
                        logger.error(f"Ошибка при удалении клиента по UUID {found_client.id}: {e}")
                        return False
                else:
                    logger.warning(f"Не удалось найти клиента по email {client_uuid_or_email} для удаления.")
                    return False

            if success:
                logger.info(f"Клиент '{client_uuid_or_email}' успешно удален из X-UI.")
                return True
            else:
                logger.warning(f"Не удалось удалить клиента '{client_uuid_or_email}' из X-UI.")
                return False

        except Exception as e:
            logger.error(f"Ошибка при удалении X-UI пользователя '{client_uuid_or_email}': {e}")
            return False

    async def get_active_clients_count_for_inbound(self, server_settings: dict) -> Optional[int]:
        client_api = await self.get_client(server_settings)
        if not client_api:
            return None
        
        try:
            inbound_id = server_settings['inbound_id']
            inbound = self._find_inbound_by_id(client_api, inbound_id)

            if not inbound:
                logger.warning(f"Inbound {inbound_id} не найден на сервере {server_settings['name']} при подсчете клиентов.")
                return None 

            active_clients_count = 0
            if hasattr(inbound, 'settings') and inbound.settings and \
               hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                active_clients_count = sum(1 for c in inbound.settings.clients if c.enable)
            
            logger.debug(f"Сервер {server_settings['name']}, Inbound {inbound_id}: {active_clients_count} активных клиентов.")
            return active_clients_count
        except Exception as e:
            logger.error(f"Ошибка при подсчете активных клиентов для сервера {server_settings['name']}: {e}")
            return None


    async def get_server_stats(self, server_settings: dict) -> Optional[dict]:
        try:
            logger.info(f"Получение статистики для сервера {server_settings['name']}")
            client_api = await self.get_client(server_settings)
            if not client_api:
                logger.error(f"Не удалось получить клиент API для сервера {server_settings['name']}")
                return None
            
            try:
                logger.debug(f"Запрос статуса сервера {server_settings['name']}")
                status = client_api.server.get_status()
                if not status:
                    logger.warning(f"Пустой статус для сервера {server_settings['name']}")
                    return None
                
                logger.debug(f"Получен статус сервера {server_settings['name']}: {status}")
                
                active_users = await self.get_active_clients_count_for_inbound(server_settings)
                active_users_str = str(active_users) if active_users is not None else 'N/A'
                
                cpu_usage = 'N/A'
                if hasattr(status, 'cpu') and status.cpu is not None:
                    try:
                        cpu_usage = f"{float(status.cpu):.1f}"
                    except (ValueError, TypeError) as e:
                        logger.error(f"Ошибка при форматировании CPU для {server_settings['name']}: {e}")
                
                mem_usage = 'N/A'
                if hasattr(status, 'mem') and status.mem:
                    try:
                        if hasattr(status.mem, 'current') and hasattr(status.mem, 'total'):
                            if status.mem.current is not None and status.mem.total is not None and status.mem.total > 0:
                                mem_usage = f"{(status.mem.current / status.mem.total * 100):.1f}"
                    except (ValueError, TypeError, ZeroDivisionError) as e:
                        logger.error(f"Ошибка при форматировании памяти для {server_settings['name']}: {e}")

                disk_usage = 'N/A'
                if hasattr(status, 'disk') and status.disk:
                    try:
                        if hasattr(status.disk, 'current') and hasattr(status.disk, 'total'):
                            if status.disk.current is not None and status.disk.total is not None and status.disk.total > 0:
                                disk_usage = f"{(status.disk.current / status.disk.total * 100):.1f}"
                    except (ValueError, TypeError, ZeroDivisionError) as e:
                        logger.error(f"Ошибка при форматировании диска для {server_settings['name']}: {e}")

                stats = {
                    'cpu_usage': cpu_usage,
                    'memory_usage': mem_usage,
                    'disk_usage': disk_usage,
                    'active_users': active_users_str
                }
                logger.info(f"Успешно получена статистика для {server_settings['name']}: {stats}")
                return stats
                
            except Exception as e:
                logger.error(f"Ошибка при получении статуса сервера {server_settings['name']}: {e}")
                return None
                
        except Exception as e:
            logger.error(f"Общая ошибка при получении статистики сервера {server_settings['name']}: {e}")
            return None

    async def get_user_limit_ip(self, server_settings: Dict, identifier: str) -> Optional[int]:
        """
        Получить лимит устройств (limit_ip) пользователя по UUID или email из X-UI.
        """
        client_api = await self.get_client(server_settings)
        if not client_api:
            return None
        inbound_id = server_settings['inbound_id']
        client_obj = self._find_client_by_email_or_uuid(client_api, inbound_id, identifier)
        if client_obj and hasattr(client_obj, 'limit_ip'):
            return getattr(client_obj, 'limit_ip', None)
        return None

xui_manager_instance = XUIManager()