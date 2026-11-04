from wsjtx_autopilot.wsjtx_profile import migrate_existing_wsjtx_profile


def test_existing_wsjtx_profile_is_copied_once_without_modifying_the_source(tmp_path) -> None:
    source = tmp_path / "WSJT-X" / "WSJT-X.ini"
    source.parent.mkdir()
    source.write_text("[Configuration]\nMyCall=F4NJU\n", encoding="utf-8")

    migration = migrate_existing_wsjtx_profile(tmp_path)

    target = tmp_path / "WSJT-X - AP1" / "WSJT-X - AP1.ini"
    assert migration.state == "migrated"
    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert source.exists()


def test_existing_ap1_profile_is_never_overwritten(tmp_path) -> None:
    source = tmp_path / "WSJT-X" / "WSJT-X.ini"
    source.parent.mkdir()
    source.write_text("stock", encoding="utf-8")
    target = tmp_path / "WSJT-X - AP1" / "WSJT-X - AP1.ini"
    target.parent.mkdir()
    target.write_text("ap1", encoding="utf-8")

    migration = migrate_existing_wsjtx_profile(tmp_path)

    assert migration.state == "existing"
    assert target.read_text(encoding="utf-8") == "ap1"


def test_missing_wsjtx_profile_does_not_create_a_partial_ap1_profile(tmp_path) -> None:
    migration = migrate_existing_wsjtx_profile(tmp_path)

    assert migration.state == "nothing_to_copy"
    assert not (tmp_path / "WSJT-X - AP1" / "WSJT-X - AP1.ini").exists()

