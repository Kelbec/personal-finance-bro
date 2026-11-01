import pytest

from pfb_core.updates import UpdateError, check_update, parse_version, pick_apk

ASSETS = [
    {"name": "finance-bro-v0.3.0-x86_64.apk", "browser_download_url": "https://x/x86.apk"},
    {"name": "finance-bro-v0.3.0-arm64-v8a.apk", "browser_download_url": "https://x/arm64.apk"},
    {"name": "notes.txt", "browser_download_url": "https://x/notes.txt"},
]


def release(tag, assets=ASSETS):
    return {"tag_name": tag, "html_url": "https://github.com/o/r/releases/tag/" + tag,
            "assets": assets}  # fmt: skip


def test_parse_version():
    assert parse_version("v0.10.0") == (0, 10, 0)
    assert parse_version("0.3") == (0, 3)
    assert parse_version("v1.2.3-rc1") == (1, 2, 3)
    assert parse_version("v0.10.0") > parse_version("0.9.9")  # numerico, non testuale
    with pytest.raises(UpdateError):
        parse_version("nightly")


def test_pick_apk_prefers_arm64():
    assert pick_apk(ASSETS) == "https://x/arm64.apk"
    assert pick_apk(ASSETS[:1]) == "https://x/x86.apk"
    assert pick_apk(ASSETS[2:]) is None


def test_check_update_newer_release():
    info = check_update("0.2.0", fetch=lambda url: release("v0.3.0"))
    assert info.version == "0.3.0"
    assert info.apk_url == "https://x/arm64.apk"


def test_check_update_none_when_current_or_older():
    assert check_update("0.3.0", fetch=lambda url: release("v0.3.0")) is None
    assert check_update("0.4.0", fetch=lambda url: release("v0.3.0")) is None


def test_check_update_release_without_apk_falls_back_to_page():
    info = check_update("0.2.0", fetch=lambda url: release("v0.3.0", assets=[]))
    assert info.apk_url is None
    assert info.page_url.endswith("v0.3.0")


def test_check_update_builds_api_url():
    seen = []
    check_update("0.2.0", repo="o/r", fetch=lambda url: seen.append(url) or release("v0.2.0"))
    assert seen == ["https://api.github.com/repos/o/r/releases/latest"]
