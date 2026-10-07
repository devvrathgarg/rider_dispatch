"""Redis key layout. The only place in the project where key names are built.

Redis has no tables and no schema, so the way keys are named *is* the schema.
Nothing else in the codebase should concatenate a key name - if a key appears
anywhere else, this file has failed at its job.

Rider ids are strings. Keys are built from them, never the other way round:
cell sets store bare rider ids, so a key is only ever needed in the middle of
an operation, never stored.
"""

PREFIX = "dispatch"


def rider_key(rider_id: str) -> str:
    """The hash holding one rider's position, state and destination."""
    return f"{PREFIX}:rider:{rider_id}"


def cell_key(cell_id: str) -> str:
    """The set of rider ids currently inside one H3 cell."""
    return f"{PREFIX}:cell:{cell_id}"


def all_keys_pattern() -> str:
    """Matches every key this project owns.

    For SCAN only. Never pass this to KEYS: KEYS walks the entire keyspace and
    blocks Redis's single command thread for the whole scan, which is invisible
    on 500 keys and an outage on 5 million.
    """
    return f"{PREFIX}:*"


if __name__ == "__main__":
    assert rider_key("r7") == "dispatch:rider:r7"
    assert cell_key("8928308280fffff") == "dispatch:cell:8928308280fffff"

    # The two families share a prefix but can never collide, even on the same id.
    assert rider_key("x") != cell_key("x")

    # Every key starts with the prefix, so one SCAN pattern can find them all.
    assert rider_key("r7").startswith(PREFIX + ":")
    assert cell_key("abc").startswith(PREFIX + ":")

    # The pattern must match both families and nothing outside the project.
    assert all_keys_pattern() == "dispatch:*"
    assert rider_key("r7").startswith(all_keys_pattern()[:-1])
    assert cell_key("abc").startswith(all_keys_pattern()[:-1])

    print("ok")
