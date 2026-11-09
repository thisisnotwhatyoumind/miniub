"""Demo module: text commands. Edit and run .reload tools."""

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
