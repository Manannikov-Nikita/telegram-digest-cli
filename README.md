# Telegram Digest CLI

Локальный CLI для сбора текстовых публикаций из публичных Telegram-каналов и
создания краткого дайджеста через OpenAI-совместимый API. Требуется Python 3.11+
и один локальный пользователь.

## Установка

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
python3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполните в `.env` все пять переменных:

```dotenv
TG_API_ID=123456
TG_API_HASH=your-telegram-api-hash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model
```

`OPENAI_BASE_URL` должен быть полным HTTP(S)-адресом, заканчивающимся на `/v1`.
Telegram API ID и hash выдаются в [my.telegram.org](https://my.telegram.org/).

## Настройка и запуск

В `DIGEST.md` укажите по одному корневому публичному URL канала `https://t.me/name`
в каждой строке (комментарии разрешены). `PROMPT.md` содержит системную
инструкцию для генерации; по умолчанию она просит русский дайджест с фактами,
датами и ссылками на источники. Форматы обоих файлов — обычный UTF-8 Markdown.

Запуск за последние семь UTC-дней:

```bash
python main.py --days 7
```

При первом запуске Telethon попросит номер телефона, код из Telegram и, если
включён, пароль 2FA. Сессия сохраняется локально в `telegram.session`, поэтому
последующие запуски используют её повторно. Успешный результат записывается в
`output/YYYY-MM-DD_HH-MM-SSZ.md`; путь также печатается в консоль.

## Проверки

```bash
python -m unittest discover -s tests -v
python -m py_compile main.py
python main.py --help
git diff --check
```

Реальный вход в Telegram и credentialed сетевой smoke-тест выполняются вручную
после заполнения секретов; автоматические проверки сеть и реальные учётные данные
не используют.

## Ограничения и ошибки MVP

CLI рассчитан на публичные broadcast-каналы и текстовые сообщения. Приватные
каналы, группы, некорректные URL/настройки, отсутствие публикаций в окне и пустой
ответ модели завершаются безопасной ошибкой без создания дайджеста. При слишком
большом контексте API предложит уменьшить `--days` или число источников. В MVP
нет планировщика, веб-интерфейса, хранения данных на сервере, автоматической
публикации и поддержки приватных каналов/групп.
