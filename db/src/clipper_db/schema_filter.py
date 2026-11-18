import re

_PARTITION = re.compile(r"^chat_messages_p\d{8}$")


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Alembic autogenerate filter: daily chat partitions are created at runtime, not migrations."""
    return not (type_ == "table" and name and _PARTITION.match(name))
