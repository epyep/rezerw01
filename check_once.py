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
        "url": "https://piecstawow.pl/rezerwacje/?hotresevent=hotres_step1",
        "day_selector": ".hotres-calendar .day",       # TODO: уточнити в DevTools
        "unavailable_class": "disabled",                # TODO: уточнити в DevTools
    },
    {
        "name": "Schronisko Murowaniec",
        "url": "https://be.guestsage.com/pl/3ea3065a-f953-4ea6-91ac-98334fdaaf06"
               "?referral=murowaniec.com&hostelView=true&personsCount=1",
        "day_selector": "[data-testid='calendar-day']", # TODO: уточнити в DevTools
        "unavailable_class": "unavailable",              # TODO: уточнити в DevTools
    },
]

STATE_FILE = Path("state.json")


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
        class_attr = (await day.get_attribute("class")) or ""
        is_unavailable = target["unavailable_class"] in class_attr

        date_str = await day.get_attribute("data-date")
        if not date_str:
            date_str = (await day.inner_text()).strip()

        if not is_unavailable and date_str:
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
                msg = (
                    f"🏔 <b>{target['name']}</b>\n"
                    f"З'явились нові вільні дати:\n"
                    + "\n".join(f"• {d}" for d in new_dates)
                    + f"\n\n{target['url']}"
                )
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
