"""Demo module: AFK auto-reply and a tiny note store.

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
