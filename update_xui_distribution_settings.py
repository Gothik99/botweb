import db_helpers
import json

# Пример: обновить настройки распределения для всех серверов
# Можно адаптировать под свои нужды или запускать вручную

def main():
    # Получаем текущий список серверов
    import sqlite3
    DB_PATH = 'vpn_bot.db'
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'xui_servers'")
    row = cur.fetchone()
    conn.close()
    if not row:
        print('Список серверов не найден!')
        return
    servers = json.loads(row[0])

    # Пример: обновить настройки для каждого сервера
    for s in servers:
        # Здесь можно задать нужные параметры вручную или по логике
        s['exclude_from_auto'] = False  # или True для исключения
        s['max_clients'] = 100          # лимит клиентов (0 = без лимита)
        s['priority'] = 0              # приоритет (меньше — выше)

    # Сохраняем изменения
    db_helpers.update_xui_servers_distribution_settings(servers)
    print('Настройки распределения серверов успешно обновлены!')

if __name__ == '__main__':
    main() 