"""miniUB — модульный юзербот внутри exteraGram.

Этот файл скачивается загрузчиком miniub-loader с GitHub, проверяется по
SHA-256 и выполняется внутри процесса exteraGram.

Слои:
  * system — доверенные модули из приватной папки плагина (core и т.п.)
  * user   — пользовательские модули из доступной папки; обычный Python,
             свой namespace в хранилище, команды системного слоя приоритетнее.

Команды ловятся в on_send_message_hook, входящие — в on_update_hook.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import sys
import threading
import time
import types

from base_plugin import BasePlugin, HookResult, HookStrategy
from ui.settings import Header, Input, Switch, Text

try:
    from file_utils import get_plugins_dir
except Exception:  # older SDK
    get_plugins_dir = None

try:
    from client_utils import send_text
except Exception:  # older SDK
    send_text = None


__id__ = "miniub"
__name__ = "miniUB"
__description__ = "Модульный юзербот: системные и пользовательские модули, hot reload"
__author__ = "thisisnotwhatyoumind"
__version__ = "0.5.0"
__icon__ = "exteraPlugins/1"
__app_version__ = ">=12.5.1"
__sdk_version__ = ">=1.4.4.3"
__category__ = ["tools", "chats"]


INTERNAL_MARK = "\u2063"  # invisible separator: marks plugin-sent messages
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")


def _hook_result(strategy=None, params=None):
    """Build HookResult the way the docs show (attribute assignment)."""
    result = HookResult()
    if strategy is not None:
        result.strategy = strategy
    if params is not None:
        result.params = params
    return result


# --------------------------------------------------------------------------
# module-facing API
# --------------------------------------------------------------------------

def command(name, *, aliases=(), description="", usage=""):
    def decorator(func):
        func.__mb_command__ = {
            "name": name,
            "aliases": list(aliases),
            "description": description,
            "usage": usage,
        }
        return func

    return decorator


def on_message(*, private=None):
    def decorator(func):
        func.__mb_on_message__ = {"private": private}
        return func

    return decorator


class Context:
    """Full context for system modules."""

    __slots__ = (
        "plugin", "account", "text", "args", "params", "update",
        "chat_id", "sender_id", "message_id", "module", "command",
    )

    def __init__(
        self, plugin, *, account, text="", args="", params=None, update=None,
        chat_id=None, sender_id=None, message_id=None, module="", command_name="",
    ):
        self.plugin = plugin
        self.account = account
        self.text = text
        self.args = args
        self.params = params
        self.update = update
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message_id = message_id
        self.module = module
        self.command = command_name

    def reply(self, text):
        return self.plugin._send(self.chat_id, text, self.message_id, self.account)

    def send(self, text):
        return self.plugin._send(self.chat_id, text, None, self.account)


class UserContext:
    """Reduced context for user modules: no plugin, no Telethon/Java objects."""

    __slots__ = (
        "account", "text", "args", "chat_id", "sender_id",
        "message_id", "module", "command", "_sender",
    )

    def __init__(
        self, sender, *, account, text="", args="", chat_id=None, sender_id=None,
        message_id=None, module="", command_name="",
    ):
        self._sender = sender
        self.account = account
        self.text = text
        self.args = args
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message_id = message_id
        self.module = module
        self.command = command_name

    def reply(self, text):
        return self._sender(self.chat_id, text, self.message_id, self.account)

    def send(self, text):
        return self._sender(self.chat_id, text, None, self.account)


class _RoutedStore:
    """`store` from miniub_api: namespace is derived from the calling module."""

    def __init__(self, plugin):
        self._plugin = plugin

    def _namespace(self):
        frame = None
        try:
            import inspect

            frame = inspect.currentframe()
            while frame is not None:
                name = frame.f_globals.get("__name__", "")
                if name.startswith("miniub_ext_"):
                    return name[len("miniub_ext_"):]
                if name.startswith("miniub_sys_"):
                    return "system:" + name[len("miniub_sys_"):]
                frame = frame.f_back
        finally:
            del frame
        return "shared"

    def get(self, key, default=None):
        return self._plugin._store.get(self._namespace(), key, default)

    def set(self, key, value):
        return self._plugin._store.set(self._namespace(), key, value)

    def delete(self, key):
        return self._plugin._store.delete(self._namespace(), key)


class _LogFacade:
    """Minimal logger view for modules: no plugin, no logger internals."""

    __slots__ = ("_log",)

    def __init__(self, logger):
        self._log = logger

    def debug(self, *args, **kwargs):
        return self._log.debug(*args, **kwargs)

    def info(self, *args, **kwargs):
        return self._log.info(*args, **kwargs)

    def warning(self, *args, **kwargs):
        return self._log.warning(*args, **kwargs)

    def error(self, *args, **kwargs):
        return self._log.error(*args, **kwargs)

    def exception(self, *args, **kwargs):
        return self._log.exception(*args, **kwargs)


class _Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._data = data
        except Exception:
            self._data = {}

    def _flush(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _bucket(self, namespace):
        bucket = self._data.get(namespace)
        if not isinstance(bucket, dict):
            bucket = {}
            self._data[namespace] = bucket
        return bucket

    def get(self, namespace, key, default=None):
        with self._lock:
            return self._bucket(namespace).get(key, default)

    def set(self, namespace, key, value):
        with self._lock:
            self._bucket(namespace)[key] = value
            self._flush()

    def delete(self, namespace, key):
        with self._lock:
            bucket = self._bucket(namespace)
            bucket.pop(key, None)
            self._flush()


# --------------------------------------------------------------------------
# embedded module sources
# --------------------------------------------------------------------------

SYSTEM_CORE = r'''"""miniUB core: management commands (system layer)."""

from miniub_api import command


@command("help", aliases=("h", "start"), description="Список модулей и команд")
def help_cmd(ctx):
    plugin = ctx.plugin
    prefix = plugin._prefix()
    groups = {}
    seen = set()

    with plugin._lock:
        entries = list(plugin._commands.items())

    for _, (func, meta, module_name, layer) in sorted(entries, key=lambda item: item[0]):
        if id(func) in seen:
            continue
        seen.add(id(func))
        groups.setdefault((layer, module_name), []).append(meta)

    lines = ["miniUB " + str(plugin.version), "Команды:", ""]
    for key in sorted(groups):
        layer, module_name = key
        mark = "sys" if layer == "system" else "usr"
        lines.append("[" + mark + "] " + module_name)
        for meta in sorted(groups[key], key=lambda item: item["name"]):
            usage = meta.get("usage") or (prefix + meta["name"])
            lines.append("  " + usage + " — " + meta.get("description", ""))
        lines.append("")

    lines.append("Модули: " + (", ".join(sorted(plugin._modules)) or "—"))
    return plugin._trim("\n".join(lines))


@command("ping", description="Проверить отклик")
def ping_cmd(ctx):
    return "🏓 pong"


@command("mods", aliases=("modules",), description="Загруженные модули")
def mods_cmd(ctx):
    plugin = ctx.plugin
    lines = ["Модули:"]

    with plugin._lock:
        for stem in sorted(plugin._modules):
            layer = plugin._layers.get(stem, "user")
            count = len({id(v[0]) for v in plugin._commands.values() if v[2] == stem})
            lines.append("  " + stem + " [" + layer + "] — " + str(count) + " ком.")
        failed = dict(plugin._failed)

    if failed:
        lines.append("")
        lines.append("Ошибки:")
        for name, error in failed.items():
            lines.append("  " + name + ": " + error)

    lines.append("")
    lines.append("user:   " + plugin._modules_dir)
    lines.append("system: " + plugin._system_dir)
    return plugin._trim("\n".join(lines))


@command("modpath", description="Показать папки модулей")
def modpath_cmd(ctx):
    plugin = ctx.plugin
    return plugin._trim(
        "Пользовательские:\n" + plugin._modules_dir +
        "\n\nСистемные:\n" + plugin._system_dir
    )


@command("reload", description="Перезагрузить модули", usage=".reload [модуль|all]")
def reload_cmd(ctx):
    plugin = ctx.plugin
    target = ctx.args.strip().lower()
    if target.endswith(".py"):
        target = target[:-3]

    if target in ("", "all"):
        done, failed = plugin._reload_all()
        text = "♻️ Перезагружено: " + (", ".join(done) or "—")
        if failed:
            text += "\n⚠️ " + "\n".join(name + ": " + error for name, error in failed.items())
        return plugin._trim(text)

    ok, message = plugin._reload_one(target)
    return message


@command("lm", description="Загрузить модуль", usage=".lm <имя> | ответом на .py")
def lm_cmd(ctx):
    plugin = ctx.plugin
    name = plugin._clean_stem(ctx.args)
    if plugin._has_reply(ctx.params):
        return plugin._install_reply(ctx.params, name, ctx.account)
    if not name:
        return (
            "Загрузка модуля:\n"
            ".lm <имя> — из папки модулей\n"
            ".lm — ответом на .py-файл"
        )
    return plugin._load_named(name)[1]


@command("ilm", description="Установить модуль по ссылке",
         usage=".ilm <url> | .ilm <имя> <url>")
def ilm_cmd(ctx):
    return ctx.plugin._install_url_command(ctx.args)


@command("ulm", description="Выгрузить модуль из памяти", usage=".ulm <имя> | ответом на .py")
def ulm_cmd(ctx):
    plugin = ctx.plugin
    name = plugin._clean_stem(ctx.args)
    if not name and plugin._has_reply(ctx.params):
        name = plugin._reply_stem(ctx.params, ctx.account)
    if not name:
        return "Использование: .ulm <имя>"
    return plugin._unload_named(name)[1]


@command("dlm", description="Удалить модуль", usage=".dlm <имя> | ответом на .py")
def dlm_cmd(ctx):
    plugin = ctx.plugin
    name = plugin._clean_stem(ctx.args)
    if not name and plugin._has_reply(ctx.params):
        name = plugin._reply_stem(ctx.params, ctx.account)
    if not name:
        return "Использование: .dlm <имя>"
    return plugin._delete_named(name)[1]
'''

DEMO_TOOLS = '''"""Demo module: text commands. Edit and run .reload tools."""

from miniub_api import command


@command("echo", aliases=("e",), description="Повторить текст", usage=".echo <текст>")
def echo(ctx):
    return ctx.args or "Использование: .echo <текст>"


@command("up", description="ВЕРХНИЙ РЕГИСТР", usage=".up <текст>")
def up(ctx):
    return ctx.args.upper() or None


@command("rev", description="Перевернуть текст", usage=".rev <текст>")
def rev(ctx):
    return ctx.args[::-1] or None


@command("mock", description="чЕрЕдУюЩиЙ рЕгИсТр", usage=".mock <текст>")
def mock(ctx):
    out = []
    upper = False
    for char in ctx.args:
        if char.isalpha():
            out.append(char.upper() if upper else char.lower())
            upper = not upper
        else:
            out.append(char)
    return "".join(out) or None
'''

DEMO_AFK = '''"""Demo module: AFK auto-reply and a tiny note store.

Shows incoming handlers and the `store` API. Run .reload afk after edits.
"""

import time

from miniub_api import command, on_message, store


@command("afk", description="Включить автоответчик", usage=".afk [причина]")
def afk(ctx):
    reason = ctx.args.strip() or "сейчас недоступен"
    store.set("afk", {"reason": reason, "since": time.time()})
    return "💤 AFK включён: " + reason


@command("unafk", aliases=("back",), description="Выключить автоответчик")
def unafk(ctx):
    store.delete("afk")
    store.delete("afk_cooldowns")
    return "✅ AFK выключен"


@on_message(private=True)
def afk_reply(ctx):
    state = store.get("afk")
    if not state:
        return None
    if not ctx.sender_id:
        return None

    now = time.time()
    cooldowns = store.get("afk_cooldowns", {})
    key = str(ctx.chat_id)
    if now - cooldowns.get(key, 0) < 300:
        return None

    cooldowns[key] = now
    store.set("afk_cooldowns", cooldowns)
    return "💤 AFK: " + str(state.get("reason", ""))


@command("save", description="Сохранить заметку", usage=".save <ключ> <текст>")
def save_note(ctx):
    key, _, text = ctx.args.partition(" ")
    if not key or not text.strip():
        return "Использование: .save <ключ> <текст>"
    notes = store.get("notes", {})
    notes[key.strip().lower()] = text.strip()
    store.set("notes", notes)
    return "💾 Сохранено: " + key.strip().lower()


@command("note", aliases=("get",), description="Показать заметку", usage=".note <ключ>")
def get_note(ctx):
    key = ctx.args.strip().lower()
    if not key:
        return "Использование: .note <ключ>"
    notes = store.get("notes", {})
    return notes.get(key) or "Нет заметки: " + key


@command("notes", description="Список заметок")
def list_notes(ctx):
    notes = store.get("notes", {})
    if not notes:
        return "Заметок нет."
    return "Заметки: " + ", ".join(sorted(notes))
'''

DEMO_FUN = '''"""Demo module: dice, coin, 8-ball and a random picker.

No state, no network. Run `.reload fun` after editing.
"""

import random

from miniub_api import command

COIN = ("Орёл", "Решка")
BALL = (
    "Да.",
    "Нет.",
    "Скорее да.",
    "Скорее нет.",
    "Не сейчас.",
    "Спроси позже.",
    "Определённо да.",
    "Даже не думай.",
)


@command("roll", aliases=("dice",), description="Бросить кубик",
         usage=".roll [грани]")
def roll(ctx):
    try:
        sides = int(ctx.args.strip() or 6)
    except ValueError:
        return "Использование: .roll [грани]"
    sides = max(2, min(sides, 1_000_000))
    return "🎲 " + str(random.randint(1, sides)) + " / " + str(sides)


@command("coin", aliases=("flip",), description="Подбросить монету")
def coin(ctx):
    return "🪙 " + random.choice(COIN)


@command("ball", aliases=("8ball",), description="Шар предсказаний",
         usage=".ball <вопрос>")
def ball(ctx):
    if not ctx.args.strip():
        return "Использование: .ball <вопрос>"
    return "🎱 " + random.choice(BALL)


@command("pick", description="Выбрать один вариант",
         usage=".pick вариант1, вариант2, ...")
def pick(ctx):
    raw = ctx.args.strip()
    if not raw:
        return "Использование: .pick вариант1, вариант2, ..."
    options = [part.strip() for part in raw.replace(";", ",").split(",")]
    options = [part for part in options if part]
    if len(options) < 2:
        options = [part for part in raw.split() if part]
    if len(options) < 2:
        return "Нужно минимум два варианта."
    return "👉 " + random.choice(options)
'''


# --------------------------------------------------------------------------
# plugin
# --------------------------------------------------------------------------

class MiniUBPlugin(BasePlugin):
    def create_settings(self):
        default_dir = getattr(self, "_modules_dir", None) or self._modules_dir_hint()
        return [
            Header(text="miniUB"),
            Input(
                key="prefix",
                text="Префикс команд",
                default=".",
                subtext="Например . или !",
                icon="msg_edit",
            ),
            Input(
                key="modules_dir",
                text="Папка пользовательских модулей",
                default=default_dir,
                subtext="Сюда кладутся .py-модули",
                icon="msg_info",
            ),
            Switch(
                key="incoming_enabled",
                text="Обрабатывать входящие",
                default=True,
            ),
            Switch(
                key="auto_reload",
                text="Автоперезагрузка модулей",
                default=True,
            ),
            Text(
                text="Управление",
                subtext=".lm / .ilm / .ulm / .dlm — установка и управление",
                icon="msg_info",
            ),
        ]

    # ---------- lifecycle ----------

    def on_plugin_load(self):
        if getattr(self, "_watcher_stop", None) is not None:
            self._stop_watcher()

        self._lock = threading.RLock()
        self._commands = {}
        self._message_handlers = []
        self._modules = {}
        self._layers = {}
        self._failed = {}
        self._mtimes = {}
        self._me_ids = {}
        self._watcher_stop = None
        self._watcher_thread = None

        self.version = __version__

        self._store = _Store(self._store_path())
        self._store_router = _RoutedStore(self)
        self._log_facade = _LogFacade(self.logger)
        self._api = self._build_api()

        self._system_dir = os.path.join(self._plugins_dir(), "miniub_system")
        self._modules_dir = self._resolve_modules_dir()

        self._seed_system_modules()
        self._seed_demo_modules()

        self._load_layer("system", self._system_dir)
        if "help" not in self._commands:
            # System dir may be read-only or core.py may have been damaged.
            self._load_embedded_core()
        self._load_layer("user", self._modules_dir)

        try:
            self.add_on_send_message_hook()
        except Exception:
            self.logger.exception("miniUB: не удалось повесить send hook")

        for update_name in ("TL_updateNewMessage", "TL_updateNewChannelMessage"):
            try:
                self.add_hook(update_name)
            except Exception:
                self.logger.exception("miniUB: не удалось повесить %s", update_name)

        self._start_watcher()

        self.logger.info(
            "miniUB %s: %d команд, user=%s, system=%s",
            self.version,
            len(self._commands),
            self._modules_dir,
            self._system_dir,
        )

    def on_plugin_unload(self):
        self._stop_watcher()
        for stem in list(getattr(self, "_modules", {})):
            self._unregister_module(stem)
        self._modules = {}
        self._layers = {}
        self._commands = {}
        self._message_handlers = []

    # ---------- outgoing ----------

    def on_send_message_hook(self, account: int, params) -> HookResult:
        text = getattr(params, "message", None)
        if not isinstance(text, str):
            return HookResult()

        # Messages sent by the plugin itself must never run as commands.
        if text.startswith(INTERNAL_MARK):
            params.message = text[len(INTERNAL_MARK):]
            return _hook_result(HookStrategy.MODIFY, params)

        prefix = self._prefix()
        chat_id = self._peer_id(getattr(params, "peer", None))

        if not prefix or not text.startswith(prefix):
            return HookResult()

        body = text[len(prefix):].strip()
        if not body:
            return HookResult()

        name, _, args = body.partition(" ")

        with self._lock:
            entry = self._commands.get(name.lower())
        if entry is None:
            return HookResult()

        func, meta, module_name, layer = entry

        if layer == "user":
            ctx = UserContext(
                self._send,
                account=account,
                text=body,
                args=args.strip(),
                chat_id=chat_id,
                module=module_name,
                command_name=meta["name"],
            )
        else:
            ctx = Context(
                self,
                account=account,
                text=body,
                args=args.strip(),
                params=params,
                chat_id=chat_id,
                module=module_name,
                command_name=meta["name"],
            )

        return self._invoke(func, meta, ctx, params, layer)

    # ---------- incoming ----------

    def on_update_hook(self, update_name: str, account: int, update) -> HookResult:
        if update_name not in ("TL_updateNewMessage", "TL_updateNewChannelMessage"):
            return HookResult()

        try:
            if not self.get_setting("incoming_enabled", True):
                return HookResult()
        except Exception:
            pass

        message = getattr(update, "message", None)
        if message is None or getattr(message, "out", False):
            return HookResult()

        text = getattr(message, "message", "") or ""
        if not text:
            return HookResult()

        prefix = self._prefix()
        if prefix and text.startswith(prefix):
            return HookResult()

        chat_id = self._peer_id(getattr(message, "dialog_id", None))
        if chat_id is None:
            return HookResult()

        sender_id = self._sender_id(message, chat_id)
        me = self._my_id(account)
        if me is not None and sender_id == me:
            return HookResult()

        message_id = getattr(message, "id", None)
        if message_id is not None:
            message_id = int(message_id)

        with self._lock:
            handlers = list(self._message_handlers)

        for func, handler_meta, module_name, layer in handlers:
            private = handler_meta.get("private")
            if private is not None and bool(private) != (chat_id > 0):
                continue

            if layer == "user":
                ctx = UserContext(
                    self._send,
                    account=account,
                    text=text,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    message_id=message_id,
                    module=module_name,
                )
            else:
                ctx = Context(
                    self,
                    account=account,
                    text=text,
                    update=update,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    message_id=message_id,
                    module=module_name,
                )

            try:
                result = func(ctx)
            except Exception:
                self.logger.exception("miniUB: incoming %s упал", module_name)
                continue

            if isinstance(result, str) and result:
                self._send(chat_id, result, message_id, account)

        return HookResult()

    # ---------- dispatch ----------

    def _invoke(self, func, meta, ctx, params, layer):
        try:
            result = func(ctx)
        except Exception as exc:
            self.logger.exception("miniUB: команда %s упала", meta["name"])
            params.message = "⚠️ " + meta["name"] + ": " + str(exc)
            return _hook_result(HookStrategy.MODIFY, params)

        if isinstance(result, str) and result:
            params.message = self._trim(result)
            return _hook_result(HookStrategy.MODIFY, params)
        return HookResult()

    # ---------- small helpers ----------

    def _prefix(self):
        try:
            value = self.get_setting("prefix", ".")
        except Exception:
            value = "."
        if not isinstance(value, str) or not value:
            return "."
        return value

    @staticmethod
    def _trim(text):
        return text if len(text) <= 3800 else text[:3800] + "…"

    @staticmethod
    def _peer_id(value):
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _sender_id(message, chat_id):
        from_id = getattr(message, "from_id", None)
        user_id = getattr(from_id, "user_id", None) if from_id is not None else None
        if user_id is not None:
            try:
                return int(user_id)
            except Exception:
                return None
        return chat_id if chat_id > 0 else None

    def _my_id(self, account):
        if account in self._me_ids:
            return self._me_ids[account]

        me = None
        try:
            from client_utils import get_user_config

            try:
                config = get_user_config(account)
            except TypeError:
                config = get_user_config()
            me = int(config.getCurrentUser().id)
        except Exception:
            try:
                client = self.client(account) if account is not None else self.client()
                me = int(client.get_user_config().getCurrentUser().id)
            except Exception:
                me = None

        self._me_ids[account] = me
        return me

    def _send(self, peer, text, reply_to=None, account=None):
        if send_text is None or peer is None:
            return False

        kwargs = {}
        if reply_to:
            kwargs["replyToMsg"] = reply_to
        if account is not None:
            kwargs["account"] = account

        marked = INTERNAL_MARK + str(text)

        try:
            send_text(peer, marked, **kwargs)
            return True
        except TypeError:
            kwargs.pop("account", None)
            try:
                send_text(peer, marked, **kwargs)
                return True
            except Exception:
                self.logger.exception("miniUB: не удалось отправить сообщение")
        except Exception:
            self.logger.exception("miniUB: не удалось отправить сообщение")
        return False

    # ---------- API ----------

    def _build_api(self):
        api = types.ModuleType("miniub_api")
        api.__doc__ = "miniUB module API: command, on_message, store, log"
        api.command = command
        api.on_message = on_message
        api.store = self._store_router
        api.log = self._log_facade
        sys.modules["miniub_api"] = api
        return api

    # ---------- loading ----------

    def _load_layer(self, layer, directory):
        if not os.path.isdir(directory):
            return
        for path in sorted(glob.glob(os.path.join(directory, "*.py"))):
            if os.path.basename(path).startswith("_"):
                continue
            self._load_module(path, layer)

    def _load_module(self, path, layer):
        stem = os.path.splitext(os.path.basename(path))[0]
        if layer == "user" and (
            self._layers.get(stem) == "system"
            or os.path.exists(os.path.join(self._system_dir, stem + ".py"))
        ):
            with self._lock:
                self._failed[stem] = "конфликт с системным модулем"
            self.logger.error("miniUB: пользовательский модуль %s конфликтует с системным", stem)
            return False

        prefix = "miniub_sys_" if layer == "system" else "miniub_ext_"
        module_name = prefix + stem

        try:
            module = self._exec_plain(path, module_name)
        except Exception as exc:
            with self._lock:
                self._failed[stem] = str(exc)
            self.logger.exception("miniUB: модуль %s (%s) не загрузился", stem, layer)
            return False

        self._install_module(stem, layer, module)
        try:
            self._mtimes[path] = os.path.getmtime(path)
        except Exception:
            pass
        return True

    @staticmethod
    def _exec_plain(path, module_name):
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("не удалось создать spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _load_embedded_core(self):
        module_name = "miniub_sys_core"
        module = types.ModuleType(module_name)
        module.__dict__["__name__"] = module_name
        sys.modules[module_name] = module
        try:
            exec(compile(SYSTEM_CORE, "<miniub-core>", "exec"), module.__dict__)
        except Exception:
            self.logger.exception("miniUB: встроенное ядро не загрузилось")
            return False
        self._install_module("core", "system", module)
        return True

    def _install_module(self, stem, layer, module):
        self._unregister_module(stem)

        functions = [
            value
            for value in vars(module).values()
            if callable(value)
            and (hasattr(value, "__mb_command__") or hasattr(value, "__mb_on_message__"))
        ]

        with self._lock:
            self._modules[stem] = module
            self._layers[stem] = layer
            self._failed.pop(stem, None)
            for func in functions:
                self._register_locked(stem, layer, func)

        on_load = getattr(module, "on_load", None)
        if callable(on_load):
            try:
                on_load()
            except Exception:
                self.logger.exception("miniUB: on_load %s упал", stem)

        self.logger.info("miniUB: %s-модуль %s загружен (%d функций)", layer, stem, len(functions))

    def _register_locked(self, module_name, layer, func):
        meta = getattr(func, "__mb_command__", None)
        if meta is not None:
            entry = (func, meta, module_name, layer)
            for name in {meta["name"], *meta.get("aliases", [])}:
                key = name.lower()
                existing = self._commands.get(key)
                # System layer always wins: a user module must not hijack
                # core commands such as .reload or .lm.
                if layer == "user" and existing is not None and existing[3] == "system":
                    continue
                self._commands[key] = entry

        handler_meta = getattr(func, "__mb_on_message__", None)
        if handler_meta is not None:
            self._message_handlers.append((func, handler_meta, module_name, layer))

    def _unregister_module(self, stem):
        old = self._modules.pop(stem, None)
        self._layers.pop(stem, None)

        with self._lock:
            self._commands = {
                key: value for key, value in self._commands.items() if value[2] != stem
            }
            self._message_handlers = [
                item for item in self._message_handlers if item[2] != stem
            ]

        if old is not None:
            on_unload = getattr(old, "on_unload", None)
            if callable(on_unload):
                try:
                    on_unload()
                except Exception:
                    self.logger.exception("miniUB: on_unload %s упал", stem)

    # ---------- module management ----------

    def _validate_name(self, name):
        name = (name or "").strip()
        if not name:
            return False, "Укажи имя модуля"
        if not NAME_RE.match(name):
            return False, "Имя: латиница, цифры, _, до 41 символа"
        if name == "core":
            return False, "Имя core зарезервировано"
        if self._layers.get(name) == "system" or os.path.exists(
            os.path.join(self._system_dir, name + ".py")
        ):
            return False, "Имя занято системным модулем"
        return True, name

    def _path_for(self, stem):
        for directory in (self._system_dir, self._modules_dir):
            path = os.path.join(directory, stem + ".py")
            if os.path.exists(path):
                return path
        return None

    def _load_named(self, stem):
        if stem in self._modules:
            return True, "Модуль " + stem + " уже загружен"
        path = self._path_for(stem)
        if path is None:
            return False, "⚠️ Нет файла " + stem + ".py"
        layer = "system" if path.startswith(self._system_dir) else "user"
        if self._load_module(path, layer):
            return True, "✅ Модуль " + stem + " загружен"
        return False, "⚠️ " + stem + ": " + self._failed.get(stem, "ошибка")

    def _unload_named(self, stem):
        if stem not in self._modules:
            return False, "Модуль " + stem + " не загружен"
        if self._layers.get(stem) == "system":
            return False, "⚠️ Системный модуль выгружать нельзя"
        self._unregister_module(stem)
        return True, "⏏ Модуль " + stem + " выгружен"

    def _delete_named(self, stem):
        if self._layers.get(stem) == "system":
            return False, "⚠️ Системный модуль удалять нельзя"
        path = self._path_for(stem)
        if path is None:
            return False, "⚠️ Нет файла " + stem + ".py"
        if path.startswith(self._system_dir):
            return False, "⚠️ Системный модуль удалять нельзя"
        if self._layers.get(stem) == "user":
            self._unregister_module(stem)
        try:
            os.remove(path)
        except Exception as exc:
            return False, "⚠️ Не удалось удалить: " + str(exc)
        return True, "🗑 Модуль " + stem + " удалён"

    def _reload_one(self, stem):
        path = self._path_for(stem)
        if path is None:
            return False, "⚠️ Нет файла " + stem + ".py"
        layer = "system" if path.startswith(self._system_dir) else "user"
        self._unregister_module(stem)
        if self._load_module(path, layer):
            return True, "♻️ Модуль " + stem + " перезагружен"
        return False, "⚠️ " + stem + ": " + self._failed.get(stem, "ошибка")

    def _reload_all(self):
        for stem in list(self._modules):
            self._unregister_module(stem)

        with self._lock:
            self._failed.clear()

        done = []
        failed = {}
        layers = [("system", self._system_dir), ("user", self._modules_dir)]
        for layer, directory in layers:
            for path in sorted(glob.glob(os.path.join(directory, "*.py"))):
                if os.path.basename(path).startswith("_"):
                    continue
                stem = os.path.splitext(os.path.basename(path))[0]
                if self._load_module(path, layer):
                    done.append(stem)
                else:
                    failed[stem] = self._failed.get(stem, "ошибка")

        return done, failed

    # ---------- module management ----------

    @staticmethod
    def _clean_stem(value):
        value = (value or "").strip()
        if value.lower().endswith(".py"):
            value = value[:-3]
        return value.strip()

    @staticmethod
    def _url_stem(url):
        try:
            from urllib.parse import unquote, urlparse

            base = os.path.basename(unquote(urlparse(url).path))
        except Exception:
            base = ""
        base = (base or "").strip()
        if base.lower().endswith(".py"):
            base = base[:-3]
        return base.strip()

    @staticmethod
    def _read_param(obj, names):
        if isinstance(obj, dict):
            for name in names:
                value = obj.get(name)
                if value:
                    return value
        for name in names:
            try:
                value = getattr(obj, name, None)
            except Exception:
                continue
            if value:
                return value
        try:
            from hook_utils import get_private_field

            for name in names:
                try:
                    value = get_private_field(obj, name)
                except Exception:
                    continue
                if value:
                    return value
        except Exception:
            pass
        return None

    def _has_reply(self, params):
        if params is None:
            return False
        return self._read_param(
            params,
            ("replyToMsg", "reply_to_msg", "reply_to", "reply_to_msg_id", "replyToMsgId"),
        ) is not None

    def _reply_message(self, params, account):
        if params is None:
            return None
        dialog_id = self._peer_id(self._read_param(params, ("peer",)))
        value = self._read_param(params, ("replyToMsg", "reply_to_msg", "reply_to"))
        if value is None:
            value = self._read_param(params, ("reply_to_msg_id", "replyToMsgId"))
        if value is None:
            return None
        if isinstance(value, int):
            if dialog_id is None:
                return None
            return self._fetch_message(account, dialog_id, value)
        # Some params expose a ReplyHeader instead of the full message.
        if not hasattr(value, "getDocument") and not hasattr(value, "media"):
            reply_id = self._read_param(value, ("reply_to_msg_id",))
            if reply_id is not None and dialog_id is not None:
                return self._fetch_message(account, dialog_id, reply_id)
        return value

    def _fetch_message(self, account, dialog_id, message_id):
        try:
            from client_utils import get_messages_storage

            raw = get_messages_storage(account).getMessage(
                int(dialog_id), int(message_id)
            )
        except Exception:
            try:
                from client_utils import get_client

                raw = get_client(account).get_messages_storage().getMessage(
                    int(dialog_id), int(message_id)
                )
            except Exception:
                raw = None
        if raw is None:
            try:
                from client_utils import get_messages_controller

                return get_messages_controller(account).getMessage(
                    int(dialog_id), int(message_id)
                )
            except Exception:
                return None
        try:
            from org.telegram.messenger import MessageObject

            return MessageObject(account, raw, False, False)
        except Exception:
            return raw

    def _document_bytes(self, message, account):
        if message is None:
            return None, "⚠️ Ответ не найден"
        doc = None
        if hasattr(message, "getDocument"):
            try:
                doc = message.getDocument()
            except Exception:
                doc = None
        if doc is None:
            media = getattr(message, "media", None)
            if media is not None:
                doc = getattr(media, "document", None)
        if doc is None:
            return None, "⚠️ В ответе нет файла"

        try:
            if int(doc.size) > 512 * 1024:
                return None, "⚠️ Файл больше 512 КБ"
        except Exception:
            pass

        filename = ""
        if hasattr(message, "getFileName"):
            try:
                filename = message.getFileName() or ""
            except Exception:
                filename = ""
        if not filename:
            try:
                from org.telegram.messenger import FileLoader

                filename = FileLoader.getDocumentFileName(doc) or ""
            except Exception:
                filename = ""

        if filename and not filename.lower().endswith(".py"):
            return None, "⚠️ Нужен именно .py-файл"

        try:
            from client_utils import get_file_loader

            loader = get_file_loader(account)
        except Exception:
            try:
                from client_utils import get_client

                loader = get_client(account).get_file_loader()
            except Exception as exc:
                return None, "⚠️ Нет FileLoader: " + str(exc)

        try:
            target = loader.getPathToAttach(doc)
        except TypeError:
            try:
                target = loader.getPathToAttach(doc, False)
            except Exception as exc:
                return None, "⚠️ Путь к файлу: " + str(exc)
        except Exception as exc:
            return None, "⚠️ Путь к файлу: " + str(exc)

        try:
            db = loader.getFileDatabase()
        except Exception:
            db = None

        def db_existing():
            if db is None:
                return None
            try:
                path = db.getPath(int(doc.id), int(doc.dc_id), 3, True)
            except Exception:
                return None
            if path and os.path.exists(path):
                return path
            return None

        def read_path(path):
            if not path:
                return None
            try:
                with open(path, "rb") as handle:
                    return handle.read()
            except Exception:
                return None

        data = None
        # Outgoing files often keep the original local path in attachPath.
        attach = None
        try:
            attach = getattr(message, "attachPath", None)
        except Exception:
            attach = None
        if not attach:
            try:
                owner = getattr(message, "messageOwner", None)
                if owner is not None:
                    attach = getattr(owner, "attachPath", None)
            except Exception:
                attach = None
        if attach:
            data = read_path(attach)

        if data is None and target.exists():
            data = read_path(target.getAbsolutePath())
        if data is None:
            data = read_path(db_existing())

        # FileLoader puts ordinary documents into MEDIA_DIR_FILES when
        # parentObject is a MessageObject, so the normal path may never
        # appear. The forced-cache path is stable and matches cacheType=1.
        if data is None:
            try:
                cache_target = loader.getPathToAttach(doc, True)
            except Exception:
                cache_target = target
            if cache_target.exists():
                data = read_path(cache_target.getAbsolutePath())
            if data is None:
                try:
                    loader.loadFile(doc, message, 1, 1)
                    deadline = time.time() + 30
                    while time.time() < deadline and data is None:
                        if cache_target.exists():
                            data = read_path(cache_target.getAbsolutePath())
                        if data is None:
                            data = read_path(db_existing())
                        if data is None:
                            time.sleep(0.2)
                except Exception as exc:
                    return None, "⚠️ Не скачалось: " + str(exc)

        if data is None:
            return None, "⚠️ Файл не скачался за 30 секунд"
        return data, filename

    def _reply_stem(self, params, account):
        reply = self._reply_message(params, account)
        if reply is None:
            return ""
        filename = ""
        if hasattr(reply, "getFileName"):
            try:
                filename = reply.getFileName() or ""
            except Exception:
                filename = ""
        if not filename:
            doc = None
            if hasattr(reply, "getDocument"):
                try:
                    doc = reply.getDocument()
                except Exception:
                    doc = None
            if doc is None:
                media = getattr(reply, "media", None)
                if media is not None:
                    doc = getattr(media, "document", None)
            if doc is not None:
                try:
                    from org.telegram.messenger import FileLoader

                    filename = FileLoader.getDocumentFileName(doc) or ""
                except Exception:
                    filename = ""
        return self._clean_stem(filename)

    def _install_reply(self, params, name, account):
        reply = self._reply_message(params, account)
        if reply is None:
            return "⚠️ Не вижу сообщение, на которое ты ответил"
        data, filename = self._document_bytes(reply, account)
        if data is None:
            return filename
        if len(data) > 512 * 1024:
            return "⚠️ Файл больше 512 КБ"
        if not (filename or "").lower().endswith(".py"):
            return "⚠️ Нужен именно .py-файл"
        stem = self._clean_stem(name) or self._clean_stem(filename)
        ok, error = self._validate_name(stem)
        if not ok:
            return "⚠️ " + error
        try:
            source = data.decode("utf-8-sig")
        except Exception:
            return "⚠️ Файл не в UTF-8"
        return self._install_source(stem, source)[1]

    def _install_url_command(self, args):
        args = (args or "").strip()
        if not args:
            return "Использование: .ilm <url> или .ilm <имя> <url>"
        name = ""
        url = args
        parts = args.split(None, 1)
        if len(parts) == 2 and not parts[0].startswith(("http://", "https://")):
            name = self._clean_stem(parts[0])
            url = parts[1].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return "⚠️ Нужна http(s)-ссылка"
        return self._install_from_url(url, name)

    def _install_from_url(self, url, name=""):
        if not (url.startswith("http://") or url.startswith("https://")):
            return "⚠️ Нужна http(s)-ссылка"

        name = self._clean_stem(name) or self._url_stem(url)
        ok, error = self._validate_name(name)
        if not ok:
            return "⚠️ " + error

        raw = None
        try:
            import urllib.request

            with urllib.request.urlopen(url, timeout=20) as response:
                raw = response.read(512 * 1024 + 1)
        except Exception:
            try:
                import requests

                response = requests.get(url, timeout=20)
                response.raise_for_status()
                raw = response.content[: 512 * 1024 + 1]
            except Exception as exc:
                return "⚠️ Не скачалось: " + str(exc)

        if len(raw) > 512 * 1024:
            return "⚠️ Файл больше 512 КБ"
        try:
            source = raw.decode("utf-8-sig")
        except Exception:
            return "⚠️ Файл не в UTF-8"

        return self._install_source(name, source)[1]

    def _install_source(self, name, source):
        ok, error = self._validate_name(name)
        if not ok:
            return False, "⚠️ " + error
        source = source.lstrip("\ufeff")

        path = os.path.join(self._modules_dir, name + ".py")
        try:
            os.makedirs(self._modules_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(source)
            os.replace(tmp, path)
        except Exception as exc:
            return False, "⚠️ Не записалось: " + str(exc)

        if name in self._modules:
            self._unregister_module(name)

        if self._load_module(path, "user"):
            return True, "✅ Модуль " + name + " установлен и загружен"
        return False, "⚠️ " + name + ": " + self._failed.get(name, "ошибка загрузки")

    # ---------- watcher ----------

    def _start_watcher(self):
        try:
            if not self.get_setting("auto_reload", True):
                return
        except Exception:
            pass

        self._watcher_stop = threading.Event()
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, name="miniub-watch", daemon=True
        )
        self._watcher_thread.start()

    def _stop_watcher(self):
        stop = self._watcher_stop
        if stop is not None:
            stop.set()
        thread = self._watcher_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)
        self._watcher_stop = None
        self._watcher_thread = None

    def _watch_loop(self):
        stop = self._watcher_stop
        while stop is not None and not stop.wait(2.0):
            try:
                self._check_user_mtimes()
            except Exception:
                self.logger.exception("miniUB: watcher")

    def _check_user_mtimes(self):
        for path in sorted(glob.glob(os.path.join(self._modules_dir, "*.py"))):
            if os.path.basename(path).startswith("_"):
                continue
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                continue

            old = self._mtimes.get(path)
            if old is None:
                self._mtimes[path] = mtime
                stem = os.path.splitext(os.path.basename(path))[0]
                if stem not in self._modules:
                    self._load_module(path, "user")
                continue

            if mtime != old:
                self._mtimes[path] = mtime
                stem = os.path.splitext(os.path.basename(path))[0]
                if (
                    self._layers.get(stem) == "system"
                    or os.path.exists(os.path.join(self._system_dir, stem + ".py"))
                ):
                    continue
                self._unregister_module(stem)
                self._load_module(path, "user")

    # ---------- filesystem ----------

    def _plugins_dir(self):
        if get_plugins_dir is not None:
            try:
                path = get_plugins_dir()
                if path:
                    return path
            except Exception:
                pass
        here = globals().get("__file__")
        if here:
            return os.path.dirname(os.path.abspath(here))
        return os.path.join(os.path.expanduser("~"), "miniub")

    def _store_path(self):
        return os.path.join(self._plugins_dir(), "miniub_data", "state.json")

    def _modules_dir_hint(self):
        try:
            configured = self.get_setting("modules_dir", "") or ""
        except Exception:
            configured = ""
        if configured:
            return configured
        for candidate in (
            "/storage/emulated/0/Download/miniub-plugin/modules",
            "/sdcard/Download/miniub-plugin/modules",
        ):
            if self._writable(candidate):
                return candidate
        return os.path.join(self._plugins_dir(), "miniub_modules")

    def _resolve_modules_dir(self):
        configured = ""
        try:
            configured = (self.get_setting("modules_dir", "") or "").strip()
        except Exception:
            pass

        if configured and self._writable(configured):
            return configured

        for candidate in (
            "/storage/emulated/0/Download/miniub-plugin/modules",
            "/sdcard/Download/miniub-plugin/modules",
            os.path.join(self._plugins_dir(), "miniub_modules"),
        ):
            if self._writable(candidate):
                self._remember_dir(candidate)
                return candidate

        fallback = os.path.join(self._plugins_dir(), "miniub_modules")
        os.makedirs(fallback, exist_ok=True)
        self._remember_dir(fallback)
        return fallback

    def _remember_dir(self, path):
        try:
            if self.get_setting("modules_dir", "") != path:
                try:
                    self.set_setting("modules_dir", path)
                except TypeError:
                    self.set_setting("modules_dir", path, reload_settings=False)
        except Exception:
            pass

    @staticmethod
    def _writable(path):
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".miniub_probe")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return True
        except Exception:
            return False

    def _seed_system_modules(self):
        os.makedirs(self._system_dir, exist_ok=True)
        path = os.path.join(self._system_dir, "core.py")
        try:
            # System layer is owned by the plugin, not the user.
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(SYSTEM_CORE)
        except Exception:
            self.logger.exception("miniUB: не удалось записать core.py")

    def _seed_demo_modules(self):
        marker = os.path.join(self._modules_dir, ".seeded")
        if os.path.exists(marker):
            return

        for name, source in (
            ("tools.py", DEMO_TOOLS),
            ("afk.py", DEMO_AFK),
            ("fun.py", DEMO_FUN),
        ):
            path = os.path.join(self._modules_dir, name)
            if os.path.exists(path):
                continue
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(source)
            except Exception:
                self.logger.exception("miniUB: не удалось создать %s", path)

        try:
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write(str(int(time.time())))
        except Exception:
            pass
