"""Шаблон модуля miniUB.

Скопируй файл под именем без ведущего подчёркивания, например
`modules/hello.py`, отредактируй и выполни `.reload hello`.
Файлы, начинающиеся с `_`, загрузчик пропускает.
"""

from miniub_api import command, on_message, store  # noqa: F401


@command("hello", description="Поздороваться", usage=".hello [имя]")
def hello(ctx):
    name = ctx.args.strip() or "мир"
    return "👋 Привет, " + name + "!"


# Пример обработчика входящих сообщений:
#
# @on_message(private=True)
# def auto_reply(ctx):
#     if not store.get("enabled"):
#         return None
#     return "Автоответ"
