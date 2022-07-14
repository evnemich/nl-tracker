import argparse
import collections
import json
import logging
import os.path
import time
from datetime import datetime
from typing import List

import coloredlogs
import telegram
import telegram.ext
import pytz
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.firefox.service import Service as FFService
from selenium.common.exceptions import NoSuchElementException


def read_config():
    path = 'config.json'
    # to simplify development
    if os.path.exists('ind.config.json'):
        path = 'ind.config.json'
    with open(path, 'r') as f:
        return json.loads(f.read())


CONFIG = read_config()
URL = CONFIG.get('url', '')
CITY = CONFIG.get('city', ' ')
VISA_CATEGORY = CONFIG.get('visa', 'MVV – visa for long stay (>90 days)')
NO_DATES_MARKER = CONFIG.get('empty_marker', 'No date(s) available for appointment')

USER_AGENT = CONFIG.get('user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' \
                            '(KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36')


logger = logging.getLogger(__name__)


def get_chrome_driver(path, headless=True, scale_factor=2.0):
    path = os.path.abspath(path)

    options = webdriver.ChromeOptions()
    options.add_argument(f'--user-agent={USER_AGENT}')
    options.add_argument('window-size=1024,768')
    options.add_argument(f'high-dpi-support={scale_factor}')
    options.add_argument(f'force-device-scale-factor={scale_factor}')
    options.add_argument('--log-level=3')  # disable logs

    if headless:
        options.add_argument('--headless')
        options.add_argument('--disable-gpu')

    return webdriver.Chrome(path, options=options)


def get_firefox_driver(path, headless=True, scale_factor=2.0):
    path = os.path.abspath(path)

    options = webdriver.FirefoxOptions()
    options.headless = headless
    options.set_preference('layout.css.devPixelsPerPx''', str(scale_factor))

    service = FFService(path)

    driver = webdriver.Firefox(service=service, options=options)

    driver.set_window_position(0, 0)
    driver.set_window_size(1280, 1080)

    driver.set_page_load_timeout(30)
    driver.implicitly_wait(10)

    return driver


def get_driver_loader(driver_type):
    if driver_type == 'firefox':
        return get_firefox_driver
    elif driver_type == 'chrome':
        return get_chrome_driver
    else:
        raise RuntimeError('Unknown driver type: %s' % driver_type)


def ensure_dir(path):
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)


def get_time_prefix():
    now = datetime.now()
    return now.strftime('%Y-%m-%d %H-%M-%S-%f')


def get_screenshot_path(name='default'):
    path = f'./artifacts/screenshots/{get_time_prefix()}-{name}.png'
    ensure_dir(path)
    return path


def save_page_source(page_source, stage):
    path = f'./artifacts/pages/{get_time_prefix()}-{stage}.html'
    ensure_dir(path)
    with open(path, 'w') as f:
        f.write(page_source)


class SlotsCheckResults:
    def __init__(self, available_dates: collections.OrderedDict, screenshots: List[bytes]):
        self.available_dates = available_dates
        self.screenshots = screenshots


def page_trace(driver, checkpoint, screenshot=True):
    save_page_source(driver.page_source, checkpoint)

    if screenshot:
        driver.save_screenshot(get_screenshot_path(checkpoint))


def find_element_safe(driver, by, value):
    try:
        return driver.find_element(by, value)
    except NoSuchElementException:  # spelling error making this code not work as expected
        return None


def parse_available_dates(calendar_element):
    month: str = calendar_element.find_elements(By.TAG_NAME, 'tr')[0].text
    # month = month.replace('>>', '').replace('<<', '').strip()
    days = []
    for day_element in calendar_element.find_elements(By.CLASS_NAME, 'available'):
        days.append(int(day_element.text))
    return month, days


def get_available_slots_diff(baseline: collections.OrderedDict, current: collections.OrderedDict):
    diff = collections.OrderedDict()

    for month in baseline:
        removed_dates = set(baseline[month]) - set(current.get(month, []))
        if removed_dates:
            diff.setdefault(month, {})['removed'] = sorted(removed_dates)

    for month in current:
        added_dates = set(current[month]) - set(baseline.get(month, []))
        if added_dates:
            diff.setdefault(month, {})['added'] = sorted(added_dates)

    return diff


def check_available_slots(driver):
    driver.get(URL)

    page_trace(driver, 'loaded')

    city_picker = driver.find_element(By.ID, 'desk')
    city_picker_select = Select(city_picker)
    city_picker_select.select_by_visible_text(CITY)

    for x in range(1): # number of relatives
        city_submit_btn = driver.find_element(By.CLASS_NAME, 'number-up')
        city_submit_btn.click()

    page_trace(driver, 'city-submitted')

    logger.debug('Getting the calendar')

    calendar_screenshots = []
    available_dates = collections.OrderedDict()

    for x in range(4):
        calendar_table = driver.find_element(By.CLASS_NAME, 'date-picker')

        month, days = parse_available_dates(calendar_table)
        available_dates[month] = days

        if len(days) > 0:
            calendar_screenshot = calendar_table.screenshot_as_png
            calendar_screenshots.append(calendar_screenshot)

        next_month_link = driver.find_element(By.CSS_SELECTOR, 'button.btn.btn-default.btn-secondary.btn-sm.pull-right')
        next_month_link.click()

        page_trace(driver, 'calendar')

    logger.debug('available dates: %s', available_dates)

    return SlotsCheckResults(available_dates, calendar_screenshots)



def read_state():
    path = 'state.json'
    if not os.path.exists(path):
        return {}
    with open('state.json', 'r') as f:
        return json.loads(f.read())


def save_state(state):
    with open('state.json', 'w') as f:
        f.write(json.dumps(state))


def require_config_key(config, config_key):
    if config_key not in config:
        raise RuntimeError('"%s" config key expected')
    return config[config_key]


def check_once():
    logger.debug('starting')

    driver = None

    try:
        logger.debug('config: %s', CONFIG)

        driver_path = require_config_key(CONFIG, 'driver_path')

        driver_loader_fn = get_driver_loader(CONFIG.get('driver_type', 'firefox').lower())
        driver = driver_loader_fn(driver_path)

        telegram_chat_id = require_config_key(CONFIG, 'telegram_chat_id')
        telegram_bot_token = require_config_key(CONFIG, 'telegram_bot_api_token')

        bot = telegram.ext.ExtBot(telegram_bot_token, defaults=telegram.ext.Defaults(
            timeout=10,
        ))

        state = read_state()
        result = check_available_slots(driver)

        prev_available_dates = state.get('available_dates', {})

        if prev_available_dates != result.available_dates:
            logger.info('notifying about state change')

            if result.available_dates:
                if not prev_available_dates:
                    notification_text = '🔥 Found available slots!'
                else:
                    notification_text = '⚡ Available slots changed!'

                media = []
                for screenshot in result.screenshots:
                    media.append(telegram.InputMediaPhoto(screenshot))

                # add the diff
                diff = get_available_slots_diff(prev_available_dates, result.available_dates)
                diff_description = ''
                for month in diff:
                    for day in diff[month].get('removed', []):
                        diff_description += '❌ %s %s\n' % (day, month)
                    for day in diff[month].get('added', []):
                        diff_description += '🟢 %s %s\n' % (day, month)

                notification_text += '\n\n' + diff_description
                notification_text += '\n' + URL

                # attach text to the first screenshot to be displayed
                media[0].caption = notification_text

                bot.send_media_group(chat_id=telegram_chat_id, media=media)
            else:  # no slots found
                bot.send_message(chat_id=telegram_chat_id, text='🙅 No more slots available...')
        else:
            logger.debug('State did not change, do not notify')

        status_message_id = CONFIG.get('telegram_status_message_id')
        if status_message_id:
            tz = pytz.timezone('Europe/Moscow')
            now = datetime.now(tz)
            now_string = now.strftime('%H:%M on %b %d')
            status = '⚡ Last checked at %s (Moscow time)' % now_string
            bot.edit_message_text(chat_id=telegram_chat_id, message_id=status_message_id, text=status)

        save_state(dict(state, available_dates=result.available_dates, timestamp=time.time()))

        logger.debug('done')
    except Exception:
        if driver:
            driver.save_screenshot(get_screenshot_path('error'))
        logger.exception('An error occurred')
    finally:
        logger.debug('closing driver')
        if driver:
            driver.close()


def monitor(period_seconds):
    while True:
        check_once()
        time.sleep(period_seconds)


if __name__ == '__main__':
    logging.basicConfig(
        filename='app.log',
        format='%(asctime)s %(levelname)s:%(message)s',
        level=logging.DEBUG)
    coloredlogs.install(level=logging.DEBUG)

    parser = argparse.ArgumentParser()
    parser.add_argument('--log-level', type=str, default='INFO', required=False)

    subparsers = parser.add_subparsers()

    check_parser = subparsers.add_parser('check')
    check_parser.set_defaults(command='check')

    monitor_parser = subparsers.add_parser('monitor')
    monitor_parser.add_argument('--period-seconds', type=int, default=11*60, required=False)
    monitor_parser.set_defaults(command='monitor')

    args = parser.parse_args()

    log_level = args.log_level.upper()

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    coloredlogs.set_level(log_level)

    logger.info('parsed args: %s', args)

    if args.command == 'check':
        check_once()
    else:
        monitor(period_seconds=args.period_seconds)
