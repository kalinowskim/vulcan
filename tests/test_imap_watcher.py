from vulcan_forwarder.config import Settings
from vulcan_forwarder.imap_watcher import from_criteria


def test_single_sender():
    assert from_criteria(["noreply@vulcan.net.pl"]) == ["FROM", "noreply@vulcan.net.pl"]


def test_multiple_senders_or_chain():
    assert from_criteria(["a", "b", "c"]) == ["OR", ["FROM", "a"], ["OR", ["FROM", "b"], ["FROM", "c"]]]


def test_no_filter():
    assert from_criteria([]) == []


def test_sender_filters_parsing():
    s = Settings(_env_file=None, notification_sender_filter=" noreply@vulcan.net.pl, ja@gmail.com ;inny@x.pl,")
    assert s.sender_filters == ["noreply@vulcan.net.pl", "ja@gmail.com", "inny@x.pl"]
