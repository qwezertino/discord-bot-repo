from pathlib import Path

from app.database import Database


def test_message_and_reaction_role_round_trip(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()

    message_id = database.add_message(
        1,
        2,
        3,
        {"guild_id": 1, "channel_id": 2, "content": "hello"},
    )
    message = database.get_message(message_id)
    assert message is not None
    assert message["message_id"] == "3"
    assert message["payload"]["content"] == "hello"

    rule_id = database.add_reaction_role(1, 2, 3, "unicode:✅", "✅", 4, True)
    rule = database.find_reaction_role(3, "unicode:✅")
    assert rule is not None
    assert rule["id"] == rule_id
    assert rule["remove_on_unreact"] == 1

    database.delete_reaction_roles_for_message(3)
    assert database.get_reaction_role(rule_id) is None

    updated_id, created = database.upsert_message(
        1,
        2,
        3,
        {"guild_id": "1", "channel_id": "2", "content": "updated"},
    )
    assert updated_id == message_id
    assert created is False
    assert database.get_message(message_id)["payload"]["content"] == "updated"
