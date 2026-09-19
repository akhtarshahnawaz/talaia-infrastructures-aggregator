"""Spanish connectors. Add a new region or registry by dropping a module in here."""


def load() -> None:
    from . import catalunya  # noqa: F401
    from . import spain  # noqa: F401
