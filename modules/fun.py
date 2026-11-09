"""Demo module: dice, coin, 8-ball and a random picker.

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
