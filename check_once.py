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
 
Дві мети перевіряються різними способами:
  - Pięć Stawów (Hotres.pl) — через headless-браузер (Playwright), бо
    прямого JSON API знайти не вдалось, парсимо DOM календаря.
  - Murowaniec (GuestSage) — напряму через HTTP до їхнього JSON API,
    без браузера взагалі — швидше й надійніше.
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
 
STATE_FILE = Path("state.json")
 
# Захист від хибних "доступних" дат далеко в майбутньому (сайти на кшталт
# Hotres.pl іноді віддають ненульовий data-avb навіть для місяців поза
# реальним вікном бронювання). ~9 місяців вперед — з запасом покриває
# заявлений на сайті період; онови число, якщо схроніско відкриє
# бронювання на довший термін.
MAX_MONTHS_AHEAD = 9
MAX_DATE = date.today() + timedelta(days=MAX_MONTHS_AHEAD * 30)
 
HOTRES_TARGET = {
    "name": "Schronisko nad Pięcioma Stawami",
    "url": "https://booking.hotres.pl/v4_step1?ref=piecstawow.pl&clear=1&oid=3956&lang=pl",
    "day_selector": ".cSel[data-date]",
}
 
GUESTSAGE_TARGET = {
    "name": "Schronisko Murowaniec",
    "url": "https://be.guestsage.com/pl/3ea3065a-f953-4ea6-91ac-98334fdaaf06"
           "?referral=murowaniec.com&hostelView=true&personsCount=1",
    "api_url": "https://be.guestsage.com/bookingengine/api/guestsage/availability",
    "hotel_id": "3ea3065a-f953-4ea6-91ac-98334fdaaf06",
}
 
 
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
 
 
def build_message(name: str, url: str, new_dates: list[str]) -> str:
    MAX_DATES_IN_MESSAGE = 30
    shown_dates = new_dates[:MAX_DATES_IN_MESSAGE]
    extra_count = len(new_dates) - len(shown_dates)
 
    lines = [f"🏔 <b>{name}</b>", "З'явились нові вільні дати:"]
    lines += [f"• {d}" for d in shown_dates]
    if extra_count > 0:
        lines.append(f"…і ще {extra_count} дат")
    lines.append("")
    lines.append(url)
    return "\n".join(lines)
 
 
# ---------- Pięć Stawów / Hotres.pl (Playwright, парсинг DOM) ----------
 
async def get_hotres_available_dates(page, target: dict) -> set[str]:
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
            continue
 
        avb_raw = await day.get_attribute("data-avb")
        try:
            is_available = int(avb_raw or "0") > 0
        except ValueError:
            is_available = False
 
        if not is_available:
            continue
 
        try:
            day_date = date.fromisoformat(date_str)
        except ValueError:
            continue
 
        if day_date < date.today() or day_date > MAX_DATE:
            continue
 
        available.add(date_str)
 
    return available
 
 
# ---------- Murowaniec / GuestSage (пряме HTTP до JSON API) ----------
 
async def get_guestsage_available_dates(target: dict) -> set[str]:
    params = {
        "ageCounts": "[]",
        "hotelId": target["hotel_id"],
        "arrivalDate": date.today().isoformat(),
        "departureDate": MAX_DATE.isoformat(),
        "numNights": "1",
        "personsCount": "1",
        "currency": "PLN",
        "minPriceAll": "true",
        "notValidOffers": "true",
        "device": "desktop",
        "botNum": "854321",
        "noAvailability": "false",
    }
    headers = {
        "Accept": "application/json",
        "Referer": target["url"],
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    }
 
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(target["api_url"], params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
 
    available = set()
    for day in data.get("offerDays", []):
        date_str = day.get("date", "")[:10]  # "2026-08-09T00:00:00" -> "2026-08-09"
        if not date_str:
            continue
 
        offers = day.get("alloffers") or []
        is_available = any(
            (offer.get("numAvailable") or 0) > 0
            for group in offers
            for offer in (group.get("offers") or [])
        )
        if not is_available:
            continue
 
        try:
            day_date = date.fromisoformat(date_str)
        except ValueError:
            continue
 
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
 
 
async def process_result(state: dict, name: str, url: str, available_now: set[str]) -> None:
    previous = set(state.get(name, []))
    new_dates = sorted(available_now - previous)
 
    if new_dates:
        msg = build_message(name, url, new_dates)
        log.info("Знайдено нові дати для %s: %s", name, new_dates)
        await send_telegram_message(msg)
    else:
        log.info("[%s] Без змін (%d доступних дат)", name, len(available_now))
 
    state[name] = sorted(available_now)
 
 
async def main() -> None:
    state = load_state()
    any_error = False
 
    # --- Murowaniec (GuestSage) — легкий HTTP-запит, без браузера ---
    try:
        guestsage_dates = await get_guestsage_available_dates(GUESTSAGE_TARGET)
        await process_result(state, GUESTSAGE_TARGET["name"], GUESTSAGE_TARGET["url"], guestsage_dates)
    except Exception as e:
        log.error("[%s] Помилка перевірки: %s", GUESTSAGE_TARGET["name"], e)
        any_error = True
 
    # --- Pięć Stawów (Hotres.pl) — через headless-браузер ---
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            hotres_dates = await get_hotres_available_dates(page, HOTRES_TARGET)
            await process_result(state, HOTRES_TARGET["name"], HOTRES_TARGET["url"], hotres_dates)
        except Exception as e:
            log.error("[%s] Помилка перевірки: %s", HOTRES_TARGET["name"], e)
            any_error = True
        finally:
            await page.close()
            await browser.close()
 
    save_state(state)
 
    if any_error:
        sys.exit(1)
 
 
if __name__ == "__main__":
    asyncio.run(main())
