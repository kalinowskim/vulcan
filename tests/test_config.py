import pytest

from vulcan_forwarder.config import ConfigError, Settings


def write_env(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_multiple_accounts_from_env_file(tmp_path):
    env = write_env(tmp_path, """
MAIL_TO=wspolny@x.pl
VULCAN_ACCOUNTS=mateusz, Aga
VULCAN_MATEUSZ_LOGIN=jan@x.pl
VULCAN_MATEUSZ_PASSWORD='pa$$#word'
VULCAN_MATEUSZ_MAIL_TO=jan@x.pl
VULCAN_AGA_LOGIN=anna@x.pl
VULCAN_AGA_PASSWORD=tajne
""")
    s = Settings.load(env, environ={})
    assert [a.name for a in s.accounts] == ["mateusz", "aga"]
    mateusz, aga = s.accounts
    assert mateusz.login == "jan@x.pl" and mateusz.password.get_secret_value() == "pa$$#word"
    assert mateusz.recipients == "jan@x.pl"
    assert aga.recipients == "wspolny@x.pl"  # brak VULCAN_AGA_MAIL_TO → MAIL_TO
    assert aga.state_dir(s).as_posix().endswith("state/accounts/aga")
    assert {"pa$$#word", "tajne"} <= set(s.secrets)


def test_environment_overrides_file_like_docker_env_file(tmp_path):
    env = write_env(tmp_path, "VULCAN_ACCOUNTS=aga\nVULCAN_AGA_LOGIN=z-pliku\nVULCAN_AGA_PASSWORD=x\nMAIL_TO=a@x.pl\n")
    environ = {"VULCAN_AGA_LOGIN": "ze-srodowiska", "VULCAN_AGA_MAIL_TO": "anna@x.pl; druga@x.pl"}
    aga = Settings.load(env, environ=environ).accounts[0]
    assert aga.login == "ze-srodowiska"
    assert aga.recipients == "anna@x.pl, druga@x.pl"


def test_accounts_only_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VULCAN_ACCOUNTS", "aga")
    environ = {"VULCAN_ACCOUNTS": "aga", "VULCAN_AGA_LOGIN": "l", "VULCAN_AGA_PASSWORD": "p", "VULCAN_AGA_MAIL_TO": "a@x.pl"}
    s = Settings.load(tmp_path / "brak.env", environ=environ)
    assert [a.name for a in s.accounts] == ["aga"]


def test_missing_account_fields(tmp_path):
    env = write_env(tmp_path, "VULCAN_ACCOUNTS=aga\nVULCAN_AGA_LOGIN=l\nMAIL_TO=a@x.pl\n")
    with pytest.raises(ConfigError, match="VULCAN_AGA_PASSWORD"):
        Settings.load(env, environ={})


def test_missing_recipients(tmp_path):
    env = write_env(tmp_path, "VULCAN_ACCOUNTS=aga\nVULCAN_AGA_LOGIN=l\nVULCAN_AGA_PASSWORD=p\n")
    with pytest.raises(ConfigError, match="MAIL_TO"):
        Settings.load(env, environ={})


@pytest.mark.parametrize("names", ["mateusz,mateusz", "zła nazwa"])
def test_invalid_account_names(tmp_path, names):
    env = write_env(tmp_path, f"VULCAN_ACCOUNTS={names}\nMAIL_TO=a@x.pl\n")
    with pytest.raises(ConfigError):
        Settings.load(env, environ={"VULCAN_MATEUSZ_LOGIN": "l", "VULCAN_MATEUSZ_PASSWORD": "p"})


def test_legacy_single_account(tmp_path):
    env = write_env(tmp_path, "VULCAN_LOGIN=jan@x.pl\nVULCAN_PASSWORD=p\nMAIL_TO=jan@x.pl\n")
    s = Settings.load(env, environ={})
    assert [(a.name, a.login, a.recipients) for a in s.accounts] == [("default", "jan@x.pl", "jan@x.pl")]


def test_alert_recipient_defaults_to_all_accounts(tmp_path):
    env = write_env(tmp_path, """
VULCAN_ACCOUNTS=a,b
VULCAN_A_LOGIN=l
VULCAN_A_PASSWORD=p
VULCAN_A_MAIL_TO=jan@x.pl
VULCAN_B_LOGIN=l
VULCAN_B_PASSWORD=p
VULCAN_B_MAIL_TO=anna@x.pl, jan@x.pl
""")
    assert Settings.load(env, environ={}).alert_recipient == "jan@x.pl, anna@x.pl"
