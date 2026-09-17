#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
botbot.py — Telegram-бот: GLM 5.3 Flash + генерация картинок через Seedream 5.0
Пул моделей: gpt.crax.lol (OpenAI-совместимый, бесплатный)

УМЕЕТ:
  • Отвечать на вопросы (GLM 5.3 Flash), помнит контекст диалога каждого чата
  • Рисовать: модель вызывает инструмент generate_image -> Seedream 5.0 -> фото в чат
  • /img <описание>  — картинка сразу, без вопросов
  • /reset           — очистить память диалога
  • Работать в личке, в группах (по @упоминанию / ответу на его сообщение)
    и в Guest Mode — когда тебя зовут @username в ЛЮБОМ чате (включается в BotFather)

ЗАПУСК:
  pip install -r requirements.txt
  export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."   # токен от @BotFather
  python botbot.py

ПРОВЕРКА ПАЙПЛАЙНА БЕЗ TELEGRAM:
  python botbot.py --selftest
"""
import base64
import json
import os
import re
import sys
import threading
import time
import traceback

import requests


def _load_env_file(path: str = "") -> None:
    """Читает .env, лежащий рядом с botbot.py, если есть.
    Не требует python-dotenv. Реальные переменные окружения имеют приоритет."""
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass  # файла нет — ок, берём из окружения/дефолтов


_load_env_file()

# ============================ НАСТРОЙКИ ============================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CRAX_API_BASE = os.getenv("CRAX_API_BASE", "https://gpt.crax.lol").rstrip("/")
CRAX_API_KEY = os.getenv(
    "CRAX_API_KEY",
    "crk_live_31355e2a56b661c38868799be0b26b4b82f8",  # ключ пула по умолчанию
)
CHAT_MODEL = os.getenv("BOT_CHAT_MODEL", "glm-5.3-flash")   # мозг
IMAGE_MODEL = os.getenv("BOT_IMAGE_MODEL", "seedream-5")    # художник

SYSTEM_PROMPT = (
    "Ты — botbot, дружелюбный и полезный ИИ-ассистент в Telegram. "
    "Отвечай на языке пользователя, по делу, живо и с лёгким юмором. "
    "У тебя есть инструмент generate_image: когда пользователь просит нарисовать, "
    "сгенерировать или изобразить картинку — вызови его и передай в prompt "
    "подробное описание сцены (объекты, стиль, свет, фон, ракурс)."
)

MAX_HISTORY = 12  # сколько последних сообщений помнить на каждый чат

# Эвристика: если модель забыла вызвать инструмент, но сообщение явно про картинку
IMG_REGEX = re.compile(
    r"^(?:нарисуй|сгенерируй|изобрази|сделай картинку|сделай фото|"
    r"draw(?: me)?\b|generate (?:an? )?(?:image|picture)\b)",
    re.I | re.U,
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": ("Генерирует изображение по текстовому описанию. "
                        "Вызывай, когда пользователь просит нарисовать/сгенерировать картинку."),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Подробное описание изображения: объекты, стиль, свет, фон",
                }
            },
            "required": ["prompt"],
        },
    },
}]

TG = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN
BOT_USERNAME = ""                 # заполнится через getMe
HISTORY = {}                      # chat_id -> [(role, content), ...]
HIST_LOCK = threading.Lock()
SEEN = set()                      # защита от дублей update_id

# ============================ TELEGRAM API ============================


def tg(method: str, **params) -> dict:
    """Вызов Telegram Bot API. Поддерживает multipart через files=..."""
    files = params.pop("files", None)
    last_err = "unknown"
    for attempt in range(3):
        try:
            if files is not None:
                r = requests.post(f"{TG}/{method}", data=params, files=files, timeout=180)
            else:
                r = requests.post(f"{TG}/{method}", json=params, timeout=70)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            if r.status_code == 429:  # флуд-контроль
                wait = float(data.get("parameters", {}).get("retry_after", 3))
                time.sleep(wait)
                last_err = data.get("description", "429")
                continue
            raise RuntimeError(f"{method}: {data.get('description', r.status_code)}")
        except requests.RequestException as e:
            last_err = str(e)[:150]
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{method}: {last_err}")


def safe(method: str, **params):
    """Послать и не уронить поток, если Telegram заругался."""
    try:
        return tg(method, **params)
    except Exception as e:
        print(f"[tg:{method}] {e}", flush=True)


def send_long(chat_id, text, **extra):
    """sendMessage с нарезкой под лимит 4096."""
    text = text.strip() or "…"
    for i in range(0, len(text), 3900):
        safe("sendMessage", chat_id=chat_id, text=text[i:i + 3900],
             link_preview_options={"is_disabled": True}, **extra)


def typing_loop(chat_id, stop: threading.Event, action="typing"):
    while not stop.wait(4.5):
        safe("sendChatAction", chat_id=chat_id, action=action)


# ============================ ПУЛ gpt.crax.lol ============================


def _pool_headers():
    return {
        "Authorization": f"Bearer {CRAX_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) botbot/1.0",
        "Accept": "application/json",
    }


def llm(messages, use_tools=True, tries=3) -> str:
    """Chat completions. Пул не умеет нативные tool_calls: модель отдаёт вызов
    текстом в формате <name>...</name><arguments>{...}</arguments> — парсим сами."""
    body = {"model": CHAT_MODEL, "messages": messages,
            "stream": False, "temperature": 0.6}
    if use_tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"

    last = "unknown error"
    for i in range(tries):
        try:
            r = requests.post(f"{CRAX_API_BASE}/v1/chat/completions",
                              json=body, headers=_pool_headers(), timeout=180)
            data = r.json()
            if r.status_code == 200 and data.get("choices"):
                content = ((data["choices"][0].get("message") or {}).get("content") or "")
                if "MODEL_CONCURRENCY_LIMIT" in content:
                    last = "модель занята (concurrency limit)"
                    time.sleep(8)
                    continue
                if content.strip():
                    return content
                last = "пустой ответ модели"
            else:
                err = data.get("error") or {}
                last = err.get("message", "") or f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError) as e:
            last = str(e)[:150]
        time.sleep(3)
    raise RuntimeError(last)


def gen_image(prompt: str, tries=3) -> str:
    """Seedream 5.0: /v1/images/generations -> URL или data-URI картинки."""
    last = "unknown error"
    for i in range(tries):
        try:
            r = requests.post(
                f"{CRAX_API_BASE}/v1/images/generations",
                json={"model": IMAGE_MODEL, "prompt": prompt, "n": 1, "size": "1024x1024"},
                headers=_pool_headers(), timeout=240)
            data = r.json()
            items = data.get("data") or []
            if items:
                if items[0].get("url"):
                    return items[0]["url"]
                if items[0].get("b64_json"):
                    return "data:image/png;base64," + items[0]["b64_json"]
            err = data.get("error") or {}
            last = err.get("message", "") or "пустой ответ image-API"
        except (requests.RequestException, ValueError) as e:
            last = str(e)[:150]
        time.sleep(6)
    raise RuntimeError(last)


# ============================ ПАРСИНГ TOOL-CALL ============================


def _extract_json(raw: str):
    """Достать первый JSON-объект; если обрезан — починить скобками."""
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except Exception:
                    return None
    # не закрылся — починить: закрыть строку (нечётные кавычки) и скобки
    fix = raw[start:]
    if fix.count('"') % 2 == 1:
        fix += '"'
    fix += "}" * depth
    try:
        return json.loads(fix)
    except Exception:
        return None


def parse_tool_call(text: str):
    """<name>generate_image</name><arguments>{"prompt": "..."}</arguments> -> dict.
    Терпит обрезанные теги и текст до/после. Возвращает None, если это не tool-call."""
    if not text or "<name>" not in text:
        return None
    m = re.search(r"<name>\s*generate_image\s*</name>(.*)", text, re.I | re.S)
    if not m:
        return None
    tail = m.group(1)
    am = re.search(r"<arguments>", tail, re.I)
    if am:
        tail = tail[am.end():]
    args = _extract_json(tail)
    if isinstance(args, dict) and args.get("prompt"):
        return args
    return None


# ============================ ПАМЯТЬ ДИАЛОГОВ ============================


def remember(chat_id, role, content):
    with HIST_LOCK:
        h = HISTORY.setdefault(chat_id, [])
        h.append((role, content))
        del h[:-MAX_HISTORY]


def forget(chat_id):
    with HIST_LOCK:
        HISTORY.pop(chat_id, None)


def build_context(chat_id, user_text):
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    with HIST_LOCK:
        hist = list(HISTORY.get(chat_id, []))
    msgs += [{"role": r, "content": c} for r, c in hist]
    msgs.append({"role": "user", "content": user_text})
    return msgs


# ============================ ЛОГИКА БОТА ============================


def send_photo(chat_id, src: str, caption: str = ""):
    """src — обычный URL или data:image/...;base64,...."""
    if src.startswith("data:"):
        _, b64 = src.split(",", 1)
        safe("sendPhoto", chat_id=chat_id, caption=caption[:1024],
             files={"photo": ("image.png", base64.b64decode(b64))})
    else:
        safe("sendPhoto", chat_id=chat_id, photo=src, caption=caption[:1024])


def do_image(chat_id, prompt, note="", quiet=False):
    if not quiet:
        safe("sendMessage", chat_id=chat_id,
             text=f"🎨 Рисую: «{prompt[:140]}» — займёт ~20 секунд…")
    stop = threading.Event()
    threading.Thread(target=typing_loop, args=(chat_id, stop, "upload_photo"),
                     daemon=True).start()
    try:
        src = gen_image(prompt)
        cap = f"по запросу: «{note[:100]}»" if note else f"«{prompt[:100]}»"
        send_photo(chat_id, src, caption=f"🖼 {cap}")
    except RuntimeError as e:
        send_long(chat_id, f"😔 Не получилось нарисовать: {e}")
    finally:
        stop.set()


WELCOME = (
    "Привет! Я botbot 🤖\n"
    "Мозг: {model}, художник: {artist}.\n\n"
    "• Просто пиши — отвечу (помню контекст диалога)\n"
    "• /img кот в шляпе — нарисую картинку\n"
    "• Попроси «нарисуй …» в свободной форме — тоже нарисую\n"
    "• /reset — забыть наш диалог\n"
    "{guest}"
)


def handle_message(msg: dict):
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not chat_id or not text:
        return

    # В группах и гостевом режиме отвечаем только когда обращаются к нам
    if chat_type != "private":
        low = text.lower()
        mentioned = f"@{BOT_USERNAME}".lower() in low
        reply_from = ((msg.get("reply_to_message") or {}).get("from") or {})
        replied_to_me = str(reply_from.get("username", "")).lower() == BOT_USERNAME.lower()
        if not (mentioned or replied_to_me or text.startswith("/")):
            return
        # команда чужому боту (/cmd@otherbot) — молчим
        first = text.split()[0]
        if first.startswith("/") and "@" in first:
            target = first.split("@", 1)[1]
            if target.lower() != BOT_USERNAME.lower():
                return
        text = re.sub(re.escape(f"@{BOT_USERNAME}"), "", text, flags=re.I).strip()
        if not text:
            send_long(chat_id, "Я тут 👋 Спроси что-нибудь или /img кот в шляпе")
            return

    low = text.lower()

    # ---------- команды ----------
    if low.startswith(("/start", "/help")):
        guest = ("• Guest Mode: меня можно звать @%s в любом чате\n" % BOT_USERNAME
                 if BOT_USERNAME else "")
        send_long(chat_id, WELCOME.format(model=CHAT_MODEL, artist=IMAGE_MODEL, guest=guest))
        return
    if low.startswith("/reset"):
        forget(chat_id)
        send_long(chat_id, "🧠 Память диалога очищена.")
        return
    if low.startswith("/img"):
        prompt = text[4:].strip()
        if not prompt:
            send_long(chat_id, "Опиши, что нарисовать: /img кот-космонавт, акварель")
            return
        remember(chat_id, "user", text)
        remember(chat_id, "assistant", "[нарисовал и отправил картинку]")
        do_image(chat_id, prompt)
        return

    # ---------- «нарисуй …» в обход модели ----------
    if IMG_REGEX.match(text):
        remember(chat_id, "user", text)
        remember(chat_id, "assistant", "[нарисовал и отправил картинку]")
        do_image(chat_id, text)
        return

    # ---------- обычный диалог ----------
    stop = threading.Event()
    threading.Thread(target=typing_loop, args=(chat_id, stop), daemon=True).start()
    try:
        answer = llm(build_context(chat_id, text))
    except RuntimeError as e:
        stop.set()
        send_long(chat_id, f"😔 Модель сейчас недоступна: {e}\nПопробуй ещё раз через минуту.")
        return
    finally:
        stop.set()

    tool = parse_tool_call(answer)
    if tool:
        prefix = answer.split("<name>", 1)[0].strip()
        remember(chat_id, "user", text)
        remember(chat_id, "assistant", "[вызвал generate_image и отправил картинку]")
        do_image(chat_id, tool["prompt"], note=text, quiet=bool(prefix))
        if prefix:
            send_long(chat_id, prefix)
        return

    remember(chat_id, "user", text)
    remember(chat_id, "assistant", answer)
    send_long(chat_id, answer)


def extract_message(update: dict):
    """Обычные сообщения + возможные гостевые типы Bot API 10.x (guest_message)."""
    for key in ("message", "guest_message"):
        m = update.get(key)
        if isinstance(m, dict) and (m.get("text") or m.get("caption")):
            return m
    return None


# ============================ ЦИКЛ ПОЛЛИНГА ============================


def poll():
    offset = 0
    print("🛰  Long polling запущен (Ctrl+C — стоп)", flush=True)
    while True:
        try:
            r = requests.get(
                f"{TG}/getUpdates",
                params={"timeout": 50, "offset": offset,
                        "allowed_updates": json.dumps([])},  # все типы апдейтов
                timeout=70)
            data = r.json()
            if not data.get("ok"):
                print("[getUpdates]", data.get("description", "")[:120], flush=True)
                time.sleep(3)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if upd["update_id"] in SEEN:
                    continue
                SEEN.add(upd["update_id"])
                if len(SEEN) > 10000:
                    SEEN.clear()
                msg = extract_message(upd)
                if msg:
                    threading.Thread(target=handle_message, args=(msg,),
                                     daemon=True).start()
        except requests.RequestException as e:
            print("[poll net]", str(e)[:100], flush=True)
            time.sleep(3)
        except Exception:
            traceback.print_exc()
            time.sleep(3)


# ============================ САМОТЕСТ (без Telegram) ============================


def selftest():
    print("=== 1/3: LLM ping (%s) ===" % CHAT_MODEL, flush=True)
    t0 = time.time()
    print("Ответ:", llm([{"role": "user", "content": "Скажи одним словом: работает?"}],
                        use_tools=False)[:150])
    print("OK за %.1fs\n" % (time.time() - t0), flush=True)

    print("=== 2/3: парсер tool-call ===", flush=True)
    full = '<name>generate_image</name>\n<arguments>{"prompt": "кот в шляпе"}</arguments>'
    cut = '<name>generate_image</name><arguments>{"prompt": "рыжий кот в космосе'
    noise = "Привет! Сейчас нарисую.\n<name>generate_image</name>\n<arguments>{\"prompt\": \"пёс с шариком\"}</arguments>"
    for name, s in [("полный", full), ("обрезанный", cut), ("с текстом до", noise)]:
        print(f"  {name}: {parse_tool_call(s)}")
    print("  не-tool:", parse_tool_call("Просто текст, никаких тегов"))
    print("OK\n", flush=True)

    print("=== 3/3: Seedream (%s) ===" % IMAGE_MODEL, flush=True)
    t0 = time.time()
    url = gen_image("маленький зелёный кубик на белом фоне, 3d render, мягкий свет")
    print("URL:", url[:110])
    print("OK за %.1fs" % (time.time() - t0), flush=True)
    print("\n✅ Самотест пройден — пайплайн жив. Осталось подключить Telegram-токен.")


# ============================ MAIN ============================


def main():
    global BOT_USERNAME
    if not TELEGRAM_BOT_TOKEN:
        print('❌ Не задан токен. Получи у @BotFather и запусти так:\n'
              '   export TELEGRAM_BOT_TOKEN="123456:ABC..."\n'
              '   python botbot.py')
        sys.exit(1)
    me = tg("getMe")
    BOT_USERNAME = me["username"]
    print("🤖 botbot запущен: @%s" % BOT_USERNAME)
    print("   Мозг:     %s" % CHAT_MODEL)
    print("   Художник: %s" % IMAGE_MODEL)
    print("   Пул:      %s" % CRAX_API_BASE)
    print("   Совет: включи Guest Mode в @BotFather, чтобы звать меня в любом чате.")
    poll()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
