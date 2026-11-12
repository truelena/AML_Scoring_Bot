import os
import sqlite3
import json
import asyncio
import csv
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import FSInputFile
from openai import AsyncOpenAI

# ==========================================
# 1. ЗАГРУЗКА КЛЮЧЕЙ И НАСТРОЙКА
# ==========================================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROXY_API_KEY = os.getenv("PROXY_API_KEY")
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Инициализируем клиент OpenAI с кастомным адресом прокси-сервера
aclient = AsyncOpenAI(
    api_key=PROXY_API_KEY,
    base_url=PROXY_BASE_URL
)

# Системный промпт
aml_system_instruction = """
Ты — Senior AML/KYC аналитик в российском банке. Твоя задача: проводить первичный скоринг транзакций и выявлять признаки сомнительных операций согласно 115-ФЗ, Положению Банка России № 860-П и методическим рекомендациям ЦБ РФ (в частности, № 18-МР и № 19-МР).


ПРАВИЛА АНАЛИЗА И БИЗНЕС-ЛОГИКА:
1. Оценивай операции с точки зрения реального бизнеса. Не блокируй операцию только из-за одного формального признака, если есть логичное экономическое обоснование.
2. Ищи пересечения маркеров риска (комбо):
- Возраст бизнеса: ИП/ООО зарегистрировано менее 3-6 месяцев назад.
- Характер услуг: Нематериальные услуги (маркетинг, консалтинг, IT), результат которых сложно проверить.
- Транзитный характер: Выводятся на счета физлиц (B2C).

УРОВНИ РИСКА И ДЕЙСТВИЯ:
- Green: Низкий риск. Понятная бизнес-модель. Провести операцию.
- Yellow: Средний риск. Возможна легальная бизнес-логика. Запрос документов по 115-ФЗ.
- Red: Высокий риск. Явные признаки обнала. Ограничить ДБО, приостановить операцию, запросить документы. (В РФМ сообщать только при отказе в операции).

ФОРМАТ ОТВЕТА (строгий JSON без маркдауна):
{
  "risk_level": "Green, Yellow или Red",
  "triggers": ["Список", "сработавших", "триггеров"],
  "analysis": "Твой профессиональный анализ бизнес-логики",
  "action": "Решение по транзакции",
  "documents_to_request": ["Документ 1", "Документ 2"]
}
"""

# ==========================================
# 2. БАЗА ДАННЫХ И АВТОРИЗАЦИЯ
# ==========================================
db_path = '/data/aml_logs.db' if os.path.exists('/data') else 'aml_logs.db'

conn = sqlite3.connect(db_path)
cursor = conn.cursor()


cursor.execute('''
               CREATE TABLE IF NOT EXISTS check_logs
               (
                   id
                   INTEGER
                   PRIMARY
                   KEY
                   AUTOINCREMENT,
                   telegram_user_id
                   INTEGER,
                   request
                   TEXT,
                   risk_level
                   TEXT,
                   triggers
                   TEXT,
                   analysis
                   TEXT,
                   check_date
                   TIMESTAMP
                   DEFAULT
                   CURRENT_TIMESTAMP
               )
               ''')
conn.commit()

authorized_users = set()


# ==========================================
# 3. ФУНКЦИЯ ЗАПРОСА ЧЕРЕЗ ПРОКСИ OPENAI
# ==========================================
async def ask_gemini(text: str) -> dict:
    response = await aclient.chat.completions.create(
        model="gemini-3.1-pro-preview",
        messages=[
            {"role": "system", "content": aml_system_instruction},
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"},
        temperature=0.2
    )
    raw_json_text = response.choices[0].message.content
    return json.loads(raw_json_text)


# ==========================================
# 4. ЛОГИКА ТЕЛЕГРАМ-БОТА
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    # Проверяем, зашли ли вы (создатель)
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "✅ Добро пожаловать, создатель!\nЯ ваш личный AML-ассистент. Отправьте мне данные о транзакции.")


    elif command.args == "portfolio":
        authorized_users.add(message.from_user.id)
        await message.answer(
            "✅ Гостевой доступ разрешен!\nДобро пожаловать в AML-ассистент (демо-режим).\n\nОтправьте данные о транзакции для проверки.")

    # Блокируем случайных прохожих
    else:
        await message.answer("🔒 Это приватный бот-портфолио.\nДоступ закрыт. Используйте специальную ссылку из резюме.")


@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    user_id = message.from_user.id

    # Проверка базового доступа
    if user_id != ADMIN_ID and user_id not in authorized_users:
        await message.answer("🔒 У вас нет доступа к выгрузке отчетов.")
        return

    await message.answer("🔄 Формирую SQL-выборку высокорисковых операций (Red) за последние 7 дней...")

    try:
        # СТРОГОЕ РАЗДЕЛЕНИЕ ПРАВ ДОСТУПА (Row-Level Security)
        if user_id == ADMIN_ID:
            # SQL-запрос для руководителя: выгрузить все записи
            query = '''
                    SELECT id, check_date, request, triggers
                    FROM check_logs
                    WHERE risk_level = 'Red'
                      AND check_date >= datetime('now', '-7 days')
                    ORDER BY check_date DESC \
                    '''
            cursor.execute(query)
        else:
            # SQL-запрос для рядового аналитика (гостя): выгрузить только свои записи
            query = '''
                    SELECT id, check_date, request, triggers
                    FROM check_logs
                    WHERE risk_level = 'Red'
                      AND telegram_user_id = ?
                      AND check_date >= datetime('now', '-7 days')
                    ORDER BY check_date DESC \
                    '''
            # Передаем ID пользователя параметром в запрос для безопасности
            cursor.execute(query, (user_id,))

        rows = cursor.fetchall()

        if not rows:
            await message.answer("📊 За последние 7 дней транзакций с уровнем риска Red не зафиксировано.")
            return

        # Генерируем файл
        filename = f"aml_red_report_{datetime.now().strftime('%Y%m%d')}.csv"

        with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile, delimiter=';')
            writer.writerow(['ID', 'Дата проверки', 'Транзакция', 'Сработавшие триггеры'])
            writer.writerows(rows)

        document = FSInputFile(filename)
        await message.answer_document(
            document,
            caption="📁 Ваш отчет сформирован.\nФормат: CSV, разделитель: точка с запятой."
        )

        os.remove(filename)

    except Exception as e:
        await message.answer(f"❌ Ошибка при формировании отчета: {e}")


@dp.message(F.text)
async def analyze_transaction(message: types.Message):
    # Пропускаем только админа ИЛИ авторизованного гостя
    if message.from_user.id != ADMIN_ID and message.from_user.id not in authorized_users:
        await message.answer("🔒 Доступ закрыт. Войдите по ссылке из резюме.")
        return

    await message.answer("⏳ Анализирую транзакцию по 115-ФЗ...")

    try:
        result = await ask_gemini(message.text)

        risk = result.get('risk_level', 'Unknown')
        triggers = ", ".join(result.get('triggers', []))
        analysis = result.get('analysis', 'Нет данных')
        action = result.get('action', 'Не определено')
        docs = "\n".join(result.get('documents_to_request', []))

        cursor.execute('''
                       INSERT INTO check_logs (telegram_user_id, request, risk_level, triggers, analysis)
                       VALUES (?, ?, ?, ?, ?)
                       ''', (message.from_user.id, message.text, risk, triggers, analysis))
        conn.commit()

        reply_text = f"📊 **Уровень риска:** {risk}\n"
        reply_text += f"🚩 **Триггеры:** {triggers}\n\n"
        reply_text += f"🧠 **Анализ:** {analysis}\n\n"
        reply_text += f"🛑 **Действие:** {action}\n\n"
        reply_text += f"📄 **Запросить документы:**\n{docs}"

        await message.answer(reply_text, parse_mode="Markdown")

    except Exception as e:
        await message.answer(f"❌ Произошла ошибка при анализе.\nДетали: {e}")


# ==========================================
# ЗАПУСК БОТА
# ==========================================
async def main():
    print("Бот запущен! Ожидаю сообщений...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())