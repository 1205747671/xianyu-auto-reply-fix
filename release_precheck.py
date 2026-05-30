import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CSS_DIR = STATIC_DIR / "css"
INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "js" / "app.js"
APP_CSS = CSS_DIR / "app.css"
REPLY_SERVER = ROOT / "reply_server.py"


def _check_exists(path: Path, label: str, errors: list[str]) -> None:
    if path.exists():
        print(f"[OK] {label}: {path}")
    else:
        errors.append(f"{label} 不存在: {path}")


def _check_css_imports(errors: list[str]) -> None:
    css_text = APP_CSS.read_text(encoding="utf-8")
    imports = re.findall(r"@import\s+url\(['\"]([^'\"]+)['\"]\);", css_text)
    if not imports:
        errors.append("app.css 未找到任何 @import 子样式文件")
        return

    for relative_path in imports:
        import_path = CSS_DIR / relative_path
        if import_path.exists():
            print(f"[OK] CSS import: {relative_path}")
        else:
            errors.append(f"app.css 引用了不存在的子样式文件: {relative_path}")


def _check_index_asset_references(errors: list[str]) -> None:
    html_text = INDEX_HTML.read_text(encoding="utf-8")
    if "/static/css/app.css" in html_text:
        print("[OK] index.html 引用了 /static/css/app.css")
    else:
        errors.append("index.html 未引用 /static/css/app.css")

    if "/static/js/app.js" in html_text:
        print("[OK] index.html 引用了 /static/js/app.js")
    else:
        errors.append("index.html 未引用 /static/js/app.js")


def _check_admin_page_asset_versioning(errors: list[str]) -> None:
    source = REPLY_SERVER.read_text(encoding="utf-8")
    required_fragments = [
        "def get_file_version(",
        "def get_css_bundle_version(",
        "js_pattern = r'/static/js/app\\.js(\\?v=[^\"\\'\\s>]+)?'",
        "css_pattern = r'/static/css/app\\.css(\\?v=[^\"\\'\\s>]+)?'",
    ]
    for fragment in required_fragments:
        if fragment in source:
            print(f"[OK] admin_page asset versioning: {fragment}")
        else:
            errors.append(f"reply_server.py 缺少静态资源版本处理片段: {fragment}")


def main() -> int:
    errors: list[str] = []

    print("== release_precheck ==")
    print("说明: 当前仓库为正式版运行代码，不使用后台在线升级；本检查聚焦静态资源入口与缓存版本链路。")

    _check_exists(INDEX_HTML, "前端入口页面", errors)
    _check_exists(APP_JS, "主脚本", errors)
    _check_exists(APP_CSS, "主样式入口", errors)
    _check_exists(REPLY_SERVER, "后端入口", errors)

    if not errors:
        _check_css_imports(errors)
        _check_index_asset_references(errors)
        _check_admin_page_asset_versioning(errors)

    if errors:
        print("\n[FAIL] 发布前检查未通过:")
        for error in errors:
            print(f" - {error}")
        return 1

    print("\n[PASS] 发布前检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
