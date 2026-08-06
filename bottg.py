import json
import re
from urllib.parse import urlparse
import cloudscraper
from bs4 import BeautifulSoup
import telebot
from telebot import types

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
TOKEN = '8922084961:AAEsofBAFeqY8TrZNJR-gjtabC_UaLmZ1mE'

bot = telebot.TeleBot(TOKEN)

# Инициализируем scraper с заголовками и куки для обхода Cookie-wall OLX
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

# Куки для пропуска окон согласия OLX/Otodom
COOKIES = {
    'l_obu': '1',
    'ora_captcha': '0',
    'data_protection_consent': 'true'
}


def get_page_html(url):
    """Загрузка HTML с защитой от редиректа на главную страницу"""
    clean_url = url.split('?')[0]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
        'Referer': 'https://www.google.com/'
    }
    
    try:
        res = scraper.get(clean_url, headers=headers, cookies=COOKIES, timeout=15)
        
        # Если OLX скинул на главную страницу — пробуем с мобильным заголовком
        if 'olx.pl' in clean_url and ('Ogłoszenia - Sprzedam' in res.text or 'd/oferta/' not in res.url):
            mobile_headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1',
                'Accept-Language': 'pl-PL,pl;q=0.9'
            }
            res_m = scraper.get(clean_url, headers=mobile_headers, cookies=COOKIES, timeout=15)
            if res_m.status_code == 200:
                return res_m.text, clean_url

        if res.status_code == 200:
            return res.text, clean_url
        
        print(f"Ошибка HTTP: {res.status_code}")
        return None, clean_url
    except Exception as e:
        print(f"Ошибка запроса: {e}")
        return None, clean_url


# ==============================================================================
# ПАРСЕРЫ
# ==============================================================================
def parse_otodom(soup):
    title, price, description, photos = None, None, None, []
    script = soup.find('script', id='__NEXT_DATA__')
    if script and script.string:
        try:
            data = json.loads(script.string)
            ad = data.get('props', {}).get('pageProps', {}).get('ad', {})
            if ad:
                title = ad.get('title')
                target = ad.get('target', {})
                p_val = target.get('Price')
                p_curr = target.get('Currency', 'PLN')
                if p_val:
                    price = f"{p_val} {p_curr}"

                raw_desc = ad.get('description', '')
                description = BeautifulSoup(raw_desc, 'html.parser').get_text()

                for img in ad.get('images', []):
                    img_url = img.get('large') or img.get('medium') or img.get('small')
                    if img_url:
                        photos.append(img_url)
        except Exception as e:
            print(f"Ошибка Otodom JSON: {e}")

    return title, price, description, photos


def parse_olx(soup):
    title, price, description, photos = None, None, None, []

    # 1. Парсинг через JSON блоки OLX (__PRERENDERED_STATE__ или __NEXT_DATA__)
    for script_id in ['__PRERENDERED_STATE__', '__NEXT_DATA__']:
        script = soup.find('script', id=script_id)
        if script and script.string:
            try:
                data = json.loads(script.string)
                ad = None
                if 'ad' in data:
                    ad = data.get('ad', {}).get('ad') or data.get('ad')
                elif 'props' in data:
                    ad = data.get('props', {}).get('pageProps', {}).get('ad')

                if ad and isinstance(ad, dict):
                    title = ad.get('title')
                    price_data = ad.get('price', {})
                    if isinstance(price_data, dict):
                        price = price_data.get('displayValue') or (f"{price_data.get('value')} PLN" if price_data.get('value') else None)
                    
                    raw_desc = ad.get('description', '')
                    if raw_desc:
                        description = BeautifulSoup(raw_desc, 'html.parser').get_text()

                    for photo in ad.get('photos', []):
                        u = photo.get('link') if isinstance(photo, dict) else photo
                        if u:
                            photos.append(str(u).replace('{width}', '1000').replace('{height}', '750'))
                    break
            except Exception as e:
                print(f"Ошибка JSON OLX ({script_id}): {e}")

    # 2. Фолбэк через OpenGraph мета-теги
    og_title = soup.find('meta', property='og:title')
    og_desc = soup.find('meta', property='og:description')

    raw_title = og_title['content'] if og_title and og_title.get('content') else ''
    raw_desc = og_desc['content'] if og_desc and og_desc.get('content') else ''

    # Фильтруем заголовок главной страницы OLX
    if raw_title and 'Ogłoszenia - Sprzedam' not in raw_title and not title:
        title = raw_title

    if not description and raw_desc:
        description = raw_desc

    full_text = f"{title or ''} {description or ''} {raw_title} {raw_desc}"
    price_match = re.search(r'(\d[\d\s\.]*)\s*(zł|PLN|EUR|\$)', full_text, re.IGNORECASE)
    if price_match and not price:
        price = f"{price_match.group(1).strip()} {price_match.group(2)}"

    if not photos:
        for meta in soup.find_all('meta', property='og:image'):
            if meta.get('content'):
                photos.append(meta['content'])

    return title, price, description, photos


def parse_fallback_opengraph(soup):
    og_title = soup.find('meta', property='og:title')
    raw_title = og_title['content'] if og_title and og_title.get('content') else (soup.title.string if soup.title else '')

    if 'Ogłoszenia - Sprzedam' in raw_title:
        title = 'Без названия'
    else:
        title = raw_title or 'Без названия'

    og_desc = soup.find('meta', property='og:description') or soup.find('meta', attrs={'name': 'description'})
    description = og_desc['content'] if og_desc and og_desc.get('content') else 'Без описания'

    price_str = 'Не указана'
    price_match = re.search(r'(\d[\d\s\.]*)\s*(zł|PLN|EUR|\$)', title + ' ' + description, re.IGNORECASE)
    if price_match:
        price_str = f"{price_match.group(1).strip()} {price_match.group(2)}"

    photos = []
    for img in soup.find_all('meta', property='og:image'):
        if img.get('content'):
            photos.append(img['content'])

    return title.strip(), price_str, description.strip(), photos


def fetch_listing_data(url):
    html, clean_url = get_page_html(url)
    if not html:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    domain = urlparse(clean_url).netloc.lower()

    title, price, description, photos = None, None, None, []

    if 'otodom' in domain:
        title, price, description, photos = parse_otodom(soup)
    elif 'olx' in domain:
        title, price, description, photos = parse_olx(soup)

    if not title or title == 'Без названия':
        title, price, description, photos = parse_fallback_opengraph(soup)

    if description and len(description) > 250:
        description = description[:250] + '...'

    valid_photos = []
    for p in photos:
        if p and isinstance(p, str) and p.startswith('http') and p not in valid_photos:
            valid_photos.append(p)

    return {
        'title': title or 'Без названия',
        'price': price or 'Не указана',
        'description': description or 'Без описания',
        'photos': valid_photos,
        'url': clean_url
    }


# ==============================================================================
# TELEGRAM ОБРАБОТЧИКИ
# ==============================================================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "📥 <b>Отправьте ссылку на объявление</b>\nПоддерживаются: Otodom, OLX, Morizon, Gratka и др.", parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text and m.text.startswith(('http://', 'https://')))
def handle_link(message):
    url = message.text.strip()
    loading_msg = bot.send_message(message.chat.id, "⏳ Загрузка данных и фото...")

    data = fetch_listing_data(url)

    if not data or data['title'] == 'Без названия':
        bot.edit_message_text("❌ Не удалось загрузить данные по этой ссылке.", message.chat.id, loading_msg.message_id)
        return

    try:
        bot.delete_message(message.chat.id, loading_msg.message_id)
    except Exception:
        pass

    if data['photos']:
        media = []
        for photo_url in data['photos'][:10]:
            media.append(types.InputMediaPhoto(photo_url))

        try:
            bot.send_media_group(message.chat.id, media)
        except Exception as e:
            print(f"Ошибка отправки фото: {e}")

    text = (
        f"Статус: 🟢 <b>Активно</b>\n\n"
        f"🏠 <b>{data['title']}</b>\n"
        f"💰 <b>Цена:</b> {data['price']}\n\n"
        f"📝 <b>Описание:</b> {data['description']}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔗 Открыть на сайте", url=data['url']))
    markup.add(types.InlineKeyboardButton("🗑 Удалить из базы", callback_data="delete_item"))

    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "delete_item")
def handle_delete(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Удалено!")
    except Exception as e:
        print(f"Ошибка удаления: {e}")


if __name__ == '__main__':
    print("Бот запущен и готов к работе...")
    bot.infinity_polling()
