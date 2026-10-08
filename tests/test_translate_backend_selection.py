from realtime_subtitle import config


def test_backend_auto_non_macos_uses_ollama():
    backend, reason = config.choose_translate_backend(
        "auto", is_macos=False, helper_exists=True, apple_status="installed")
    assert backend == "ollama"
    assert "不是 macOS" in reason


def test_backend_auto_missing_helper_uses_ollama():
    backend, reason = config.choose_translate_backend(
        "auto", is_macos=True, helper_exists=False, apple_status="installed")
    assert backend == "ollama"
    assert "helper 不存在" in reason


def test_backend_auto_status_not_installed_uses_ollama():
    backend, reason = config.choose_translate_backend(
        "auto", is_macos=True, helper_exists=True, apple_status="supported")
    assert backend == "ollama"
    assert "supported" in reason


def test_backend_auto_all_conditions_use_apple():
    backend, reason = config.choose_translate_backend(
        "auto", is_macos=True, helper_exists=True, apple_status="installed")
    assert backend == "apple"
    assert "已安装" in reason


def test_backend_explicit_ollama_and_apple():
    assert config.choose_translate_backend(
        "ollama", is_macos=True, helper_exists=True, apple_status="installed")[0] == "ollama"
    assert config.choose_translate_backend(
        "apple", is_macos=False, helper_exists=False, apple_status=None)[0] == "apple"
