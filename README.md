# Telegram Digest CLI

Минимальный локальный CLI: собирает текст и подписи к медиа из публичных
Telegram-каналов за указанное число дней, отправляет их одним запросом в
OpenAI-совместимый API и сохраняет ответ модели как Markdown.

Проект рассчитан на одного локального пользователя. В нём нет базы данных,
веб-интерфейса, планировщика, скачивания медиа и автоматической публикации.

## Требования

- Git.
- Python 3.11 или новее.
- Telegram `api_id` и `api_hash` с [my.telegram.org](https://my.telegram.org/).
- Ключ и название модели OpenAI-совместимого провайдера.

Прямые Python-зависимости закреплены в `requirements.txt`:

```text
Telethon==1.44.0
openai==3.1.0
```

## 1. Скачать проект

```bash
git clone https://github.com/Manannikov-Nikita/telegram-digest-cli.git
cd telegram-digest-cli
```

## 2. Создать окружение и установить зависимости

### macOS или Linux

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

### Windows PowerShell

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip check
```

Если PowerShell запрещает запуск локального скрипта активации, разрешите его
только для текущего процесса и повторите активацию:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Проверка, что команда использует нужный Python и видит обе библиотеки:

```bash
python -c "import sys, telethon, openai; print(sys.executable); print(telethon.__version__); print(openai.__version__)"
```

Ожидаемые версии библиотек: `1.44.0` и `3.1.0`.

## 3. Настроить `.env`

Создайте локальный файл настроек.

macOS/Linux:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Заполните все пять переменных:

```dotenv
TG_API_ID=123456
TG_API_HASH=your-telegram-api-hash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=your-model-name
```

- `TG_API_ID` — целое число из раздела API Development Tools на
  [my.telegram.org](https://my.telegram.org/).
- `TG_API_HASH` — hash из того же приложения Telegram.
- `OPENAI_BASE_URL` — полный URL OpenAI-совместимого API с префиксом `/v1`.
- `OPENAI_API_KEY` — API-ключ выбранного провайдера.
- `OPENAI_MODEL` — точное имя модели, поддерживаемое этим провайдером.

Переменные, уже заданные в окружении терминала, имеют приоритет над `.env`.
Файл `.env` игнорируется Git и не должен публиковаться.

## 4. Указать Telegram-каналы

Отредактируйте `DIGEST.md`: один публичный broadcast-канал на строку. Можно
использовать короткое имя с `@` или корневую ссылку `https://t.me/...`.

```markdown
@first_public_channel
https://t.me/second_public_channel

# Эта строка является комментарием
```

Пустые строки и строки, начинающиеся с `#`, игнорируются. Дубликаты удаляются
без учёта регистра с сохранением исходного порядка. Инвайт-ссылки, ссылки на
отдельные посты, пользователей, группы, приватные каналы и megagroup не
поддерживаются.

## 5. Настроить промпт

Запишите системную инструкцию в `PROMPT.md`. Файл должен быть непустым UTF-8
Markdown. Например:

```markdown
Составь краткий дайджест на русском языке. Сгруппируй связанные новости,
сохрани важные факты и даты, а после каждого пункта укажи ссылку на источник.
Не добавляй факты, которых нет в публикациях.
```

Для быстрой настройки результата изменяйте только `PROMPT.md` и запускайте CLI
повторно.

## 6. Запустить дайджест

Если виртуальное окружение активировано:

```bash
python main.py --days 7
```

Без активации окружения на macOS/Linux:

```bash
./.venv/bin/python main.py --days 7
```

Без активации окружения в Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe main.py --days 7
```

`--days` обязателен и должен быть положительным целым числом. Скрипт использует
единое UTC-окно от текущего момента минус указанное число дней до текущего
момента включительно.

### Первый вход в Telegram

При первом успешном подключении Telethon попросит:

1. Номер телефона Telegram-аккаунта.
2. Код подтверждения из Telegram.
3. Пароль 2FA, если он включён.

`api_id` и `api_hash` идентифицируют приложение, но не заменяют пользовательскую
авторизацию. После первого входа Telethon сохраняет локальную файловую сессию
`telegram.session`; следующие запуски используют её без повторного ввода кода.
Новая сессия обычно нужна только после удаления или отзыва старой.

Не публикуйте `telegram.session`: она даёт доступ к Telegram-аккаунту. Файлы
`telegram.session*` игнорируются Git.

### Результат

После успешного ответа модели CLI печатает количество каналов, количество
постов и полный путь к файлу:

```text
Channels: 2
Posts: 42
Output: /absolute/path/output/2026-08-18_12-00-00Z.md
```

Ответ модели сохраняется без изменений в
`output/YYYY-MM-DD_HH-MM-SSZ.md`. Каталог `output/` игнорируется Git. Если
Telegram не вернул ни одного поста, модель не вызывается. При любой ошибке новый
файл не создаётся.

## Диагностика

Список всех CLI-параметров:

```bash
python main.py --help
```

Если выбранный Python не видит библиотеку, CLI назовёт отсутствующий пакет и
покажет команду установки именно для этого интерпретатора. Универсальная команда
для активированного окружения:

```bash
python -m pip install -r requirements.txt
```

Обычный режим скрывает внутренние детали Telegram/OpenAI-ошибок. Для полного
Python traceback повторите ту же команду с `--debug`:

```bash
python main.py --days 7 --debug
```

Debug-вывод может содержать локальные пути, имена источников и текст ответа API.
Проверьте и при необходимости скройте эти данные перед публикацией.

Если OpenAI сообщает о слишком большом контексте, уменьшите `--days` или число
каналов в `DIGEST.md`. Автоматического обрезания и разбиения запроса в MVP нет.

## Проверки для разработчика

Из активированного виртуального окружения:

```bash
python -m unittest discover -s tests -v
python -m py_compile main.py tests/test_main.py
python main.py --help
python -m pip check
git diff --check
```

Автоматические тесты не обращаются к сети и не используют реальные ключи. Для
ручного smoke-теста заполните `.env`, добавьте публичный канал в `DIGEST.md` и
запустите обычную команду с небольшим значением `--days`.

## Ограничения MVP

- Только публичные broadcast-каналы.
- Только текст публикаций и подписи к медиа; файлы не скачиваются.
- Один запрос Chat Completions без автоматических повторов.
- Нет чанкинга: вместимость контекста контролирует пользователь.
- Нет приватных каналов, автовступления, БД, веб-интерфейса и планировщика.
