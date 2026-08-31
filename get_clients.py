from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time
import json

def extract_analytics_data(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        
        Stealth().apply_stealth_sync(page)

        # Словник для збереження перехоплених даних
        analytics_payloads = []

        # 1. ПЕРЕХОПЛЕННЯ МЕРЕЖЕВИХ ЗАПИТІВ
        # Аналітика завжди відправляє ці ID на сервер. Ми можемо "зловити" цей запит.
        def handle_request(request):
            # Шукаємо запити до популярних ендпоінтів аналітики
            if request.method in ["POST", "GET"] and any(kw in request.url for kw in ["analytics", "collect", "track", "events"]):
                try:
                    # Якщо дані передаються у форматі JSON
                    payload = request.post_data_json
                    if payload:
                        analytics_payloads.append(payload)
                except Exception:
                    pass # Ігноруємо, якщо це не JSON

        # Підключаємо слухач запитів до сторінки
        page.on("request", handle_request)

        print(f"Переходимо на {url}...")
        page.goto(url)
        
        # Чекаємо 5 секунд, щоб всі скрипти аналітики встигли завантажитись і відпрацювати
        page.wait_for_timeout(5000)

        # 2. ОТРИМАННЯ З LOCAL STORAGE
        # Багато систем зберігають userId та sessionId саме тут
        local_storage = page.evaluate("() => Object.assign({}, window.localStorage)")
        
        # 3. ОТРИМАННЯ З ГЛОБАЛЬНИХ ЗМІННИХ JAVASCRIPT (window)
        # Часто сайти мають об'єкти типу window.dataLayer, window.__INITIAL_STATE__ або window.analytics
        data_layer = page.evaluate("() => window.dataLayer || []")
        
        # Якщо ви знаєте точну назву змінної на сайті, можна звернутися прямо до неї:
        # custom_analytics = page.evaluate("() => window.MySiteAnalytics || {}")

        browser.close()

        return {
            "network_requests": analytics_payloads,
            "local_storage": local_storage,
            "data_layer": data_layer
        }

# Використання
url = "https://connect.vestwell.com/register"
data = extract_analytics_data(url)

# --- АНАЛІЗ РЕЗУЛЬТАТІВ ---

print("--- Дані з Local Storage ---")
for key, value in data["local_storage"].items():
    # Шукаємо ключі, що містять потрібні слова
    if any(k in key.lower() for k in ["user", "session", "pageview", "identity", "id"]):
        print(f"{key}: {value}")

print("\n--- Перехоплені мережеві запити аналітики ---")
for payload in data["network_requests"]:
    print(json.dumps(payload, indent=2))
