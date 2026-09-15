from pathlib import Path


def test_services_tree_uses_the_soil3_baseline() -> None:
    services = Path(__file__).resolve().parents[1] / "services"
    prohibited = ("soil" + "1", "soil" + "2", "soil" + "_test")
    names = [path.name.lower() for path in services.rglob("*")]
    assert not any(name in prohibited for name in names)


def test_no_legacy_agent_package_is_present() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "agents").exists()
