import json
import re
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
import telebot
from telebot import types

TOKEN = '8922084961:AAHlp2EmFhGIPLQ3zz8vj2eB9ORLMGMNOIs'  # Твой токен
bot = telebot.TeleBot(TOKEN)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,'
        ' like Gecko) Chrome/125.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
}


# ----------------------------------------------------------------------
# ПАРСЕРЫ С ВЫТАСКИВАНИЕМ ФОТО
# ----------------------------------------------------------------------
def parse_otodom(soup):
    script = soup.find('script', id='__NEXT_DATA__')
    if script and script.string:
        data = json.loads(script.string)
        ad = data.get('props', {}).get('pageProps', {}).get('ad', {})
        if ad:
            title = ad.get('title', 'Без названия')
            target = ad.get('target', {})
            price = target.get('Price', '')
            currency = target.get('Currency', 'PLN')
            price_str = f'{price} {currency}' if price else 'Не указана'

            raw_desc = ad.get('description', '')
            clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text()

            # Достаем все ссылки на фото
            photos = []
            for img in ad.get('images', []):
                img_url = (
                    img.get('large') or img.get('medium') or img.get('small')
                )
                if img_url:
                    photos.append(img_url)

            return title, price_str, clean_desc, photos
    return None, None, None, []


def parse_olx(soup):
    script = soup.find('script', id='__NEXT_DATA__')
    if script and script.string:
        data = json.loads(script.string)
        ad = data.get('props', {}).get('pageProps', {}).get('ad', {})
        if ad:
            title = ad.get('title', 'Без названия')
            price_data = ad.get('price', {})
            price_str = price_data.get('displayValue', 'Не указана')

            raw_desc = ad.get('description', '')
            clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text()

            # Достаем все фото OLX
            photos = []
            for photo in ad.get('photos', []):
                url = photo.get('link') if isinstance(photo, dict) else photo
                if url:
                    url = url.replace('{width}', '1000').replace(
                        '{height}', '750'
                    )
                    photos.append(url)

            return title, price_str, clean_desc, photos
    return None, None, None, []


def parse_fallback_opengraph(soup):
    og_title = soup.find('meta', property='og:title')
    title = (
        og_title['content']
        if og_title and og_title.get('content')
        else (soup.title.string if soup.title else 'Без названия')
    )

    og_desc = soup.find('meta', property='og:description') or soup.find(
        'meta', attrs={'name': 'description'}
    )
    description = (
        og_desc['content']
        if og_desc and og_desc.get('content')
        else 'Без описания'
    )

    price_str = 'Не указана'
    price_match = re.search(
        r'(\d[\d\s\.]*)\s*(zł|PLN|EUR|\$)', title + ' ' + description, re.IGNORECASE
    )
    if price_match:
        price_str = f'{price_match.group(1).strip()} {price_match.group(2)}'

    # Собираем мета-картинки
    photos = []
    og_images = soup.find_all('meta', property='og:image')
    for img in og_images:
        if img.get('content'):
            photos.append(img['content'])

    return title.strip(), price_str, description.strip(), photos


def fetch_listing_data(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')
        domain = urlparse(url).netloc.lower()

        if 'otodom' in domain:
            title, price, description, photos = parse_otodom(soup)
        elif 'olx' in domain:
            title, price, description, photos = parse_olx(soup)
        else:
            title, price, description, photos = parse_fallback_opengraph(soup)

        if not title:
            title, price, description, photos = parse_fallback_opengraph(soup)

        if description and len(description) > 200:
            description = description[:200] + '...'

        return {
            'title': title or 'Без названия',
            'price': price or 'Не указана',
            'description': description or 'Без описания',
            'photos': photos or [],
            'url': url,
        }
    except Exception as e:
        print(f'Ошибка запроса к {url}: {e}')
        return None


# ----------------------------------------------------------------------
# ХЭНДЛЕР
# ----------------------------------------------------------------------
@bot.message_handler(commands=['start', 'add'])
def send_welcome(message):
    bot.reply_to(
        message, '📥 Отправьте ссылку на объявление (Otodom, OLX и др.):'
    )


@bot.message_handler(
    func=lambda m: m.text and m.text.startswith(('http://', 'https://'))
)
def handle_link(message):
    url = message.text.strip()
    msg = bot.send_message(message.chat.id, '⏳ Загрузка данных и фото...')

    data = fetch_listing_data(url)

    if not data:
        bot.edit_message_text(
            '❌ Не удалось загрузить данные по этой ссылке.',
            message.chat.id,
            msg.message_id,
        )
        return

    bot.delete_message(message.chat.id, msg.message_id)

    # 1. Отправляем фото альбомом (максимум 10 штук из-за лимита Telegram API)
    if data['photos']:
        media = []
        for photo_url in data['photos'][:10]:
            media.append(types.InputMediaPhoto(photo_url))

        try:
            bot.send_media_group(message.chat.id, media)
        except Exception as e:
            print(f'Не удалось отправить фото: {e}')

    # 2. Отправляем карточку с текстом и кнопками
    text = (
        f'Статус: 🟢 <b>Активно</b>\n\n'
        f'🏠 <b>{data["title"]}</b>\n'
        f'💰 <b>Цена:</b> {data["price"]}\n\n'
        f'📝 <b>Описание:</b> {data["description"]}'
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('🔗 Открыть на сайте', url=data['url']))
    markup.add(
        types.InlineKeyboardButton(
            '🗑 Удалить из базы', callback_data='delete_item'
        )
    )

    bot.send_message(
        message.chat.id, text, parse_mode='HTML', reply_markup=markup
    )


if __name__ == '__main__':
    print('Бот запущен...')
    bot.infinity_polling()
