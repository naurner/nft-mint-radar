"""Точка входа для `python -m wlparser`.

Нужна потому, что каталог со скриптами pip на Windows часто отсутствует
в PATH, и команда `wlparser` тогда не находится.
"""

from .cli import app

if __name__ == "__main__":
    app()
