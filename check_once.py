"""
Одноразова перевірка наявності місць — версія для GitHub Actions.
 
На відміну від monitor.py (безкінечний цикл для власного сервера),
цей скрипт:
  - робить ОДНУ перевірку і завершується (GitHub Actions сам запускає
    його за розкладом, наприклад раз на 15 хв),
  - читає/пише state.json у поточній директорії (workflow далі
    закомітить цей файл назад у репозиторій, щоб пам'ятати попередній стан),
  - бере токен і chat_id зі змінних оточення (передаються через
    GitHub Secrets, у коді їх нема).
"""
 
import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
 
from playwright.async_api import async_playwright
import httpx
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hut-bot")
 
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
 
TARGETS = [
    {
        "name": "Schronisko nad Pięcioma Stawami",
        # звертаємось напряму до iframe з календарем (Hotres.pl), а не до
        # батьківської сторінки piecstawow.pl — так простіше і надійніше
        "url": "https://booking.hotres.pl/v4_step1?ref=piecstawow.pl&clear=1&oid=3956&lang=pl",
        "day_selector": ".cSel[data-date]",
        "mode": "data-avb",  # доступність визначаємо через атрибут data-avb (>0 = є місця)
    },
    {
        "name": "Schronisko Murowaniec",
        "url": "https://be.guestsage.com/pl/3ea3065a-f953-4ea6-91ac-98334fdaaf06"
               "?referral=murowaniec.com&hostelView=true&personsCount=1",
        "day_selector": "[data-testid='calendar-day']", # TODO: уточнити в DevTools окремо
        "unavailable_class": "unavailable",              # TODO: уточнити в DevTools окремо
        "mode": "class",
    },
]
 
STATE_FILE = Path("state.json")
 
# Захист від хибних "доступних" дат далеко в майбутньому (сайти на кшталт
# Hotres.pl іноді віддають ненульовий data-avb навіть для місяців поза
# реальним вікном бронювання). ~9 місяців вперед — з запасом покриває
# заявлений на сайті період; онови число, якщо схроніско відкриє
# бронювання на довший термін.
MAX_MONTHS_AHEAD = 9
MAX_DATE = date.today() + timedelta(days=MAX_MONTHS_AHEAD * 30)
 
 
async def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        resp.raise_for_status()
 
 
async def get_available_dates(page, target: dict) -> set[str]:
    await page.goto(target["url"], wait_until="networkidle", timeout=45000)
    await page.wait_for_timeout(3000)
 
    try:
        await page.wait_for_selector(target["day_selector"], timeout=15000)
    except Exception:
        log.warning("[%s] Календар не з'явився за відведений час", target["name"])
        return set()
 
    days = await page.query_selector_all(target["day_selector"])
    available = set()
 
    for day in days:
        date_str = await day.get_attribute("data-date")
        if not date_str:
            date_str = (await day.inner_text()).strip()
        if not date_str:
            continue
 
        if target.get("mode") == "data-avb":
            # Hotres.pl: доступність = кількість вільних місць у data-avb
            avb_raw = await day.get_attribute("data-avb")
            try:
                is_available = int(avb_raw or "0") > 0
            except ValueError:
                is_available = False
        else:
            # GuestSage та інші: доступність визначаємо за відсутністю
            # "недоступного" класу
            class_attr = (await day.get_attribute("class")) or ""
            is_available = target["unavailable_class"] not in class_attr
 
        if not is_available:
            continue
 
        # Ігноруємо дати, що вже минули — календар віддає для них
        # data-avb з чисто історичним значенням, бронювання на них
        # неможливе фізично
        try:
            day_date = date.fromisoformat(date_str)
        except ValueError:
            continue  # не змогли розпарсити — краще пропустити, ніж помилково зарахувати
 
        if day_date < date.today() or day_date > MAX_DATE:
            continue
 
        available.add(date_str)
 
    return available
 
 
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}
 
 
def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
 
 
async def main() -> None:
    state = load_state()
    any_error = False
 
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
 
        for target in TARGETS:
            page = await browser.new_page()
            try:
                available_now = await get_available_dates(page, target)
            except Exception as e:
                log.error("[%s] Помилка перевірки: %s", target["name"], e)
                any_error = True
                await page.close()
                continue
            await page.close()
 
            previous = set(state.get(target["name"], []))
            new_dates = sorted(available_now - previous)
 
            if new_dates:
                MAX_DATES_IN_MESSAGE = 30
                shown_dates = new_dates[:MAX_DATES_IN_MESSAGE]
                extra_count = len(new_dates) - len(shown_dates)
 
                lines = [f"🏔 <b>{target['name']}</b>", "З'явились нові вільні дати:"]
                lines += [f"• {d}" for d in shown_dates]
                if extra_count > 0:
                    lines.append(f"…і ще {extra_count} дат")
                lines.append("")
                lines.append(target["url"])
                msg = "\n".join(lines)
 
                log.info("Знайдено нові дати для %s: %s", target["name"], new_dates)
                await send_telegram_message(msg)
            else:
                log.info("[%s] Без змін (%d доступних дат)", target["name"], len(available_now))
 
            state[target["name"]] = sorted(available_now)
 
        await browser.close()
 
    save_state(state)
 
    # Не валимо весь workflow через тимчасову помилку одного сайту,
    # але лишаємо код виходу ненульовим, щоб було видно в лозі Actions.
    if any_error:
        sys.exit(1)
 
 
if __name__ == "__main__":
    asyncio.run(main())
 
