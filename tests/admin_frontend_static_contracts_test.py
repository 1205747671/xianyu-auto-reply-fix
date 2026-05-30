from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_CSS_PATH = REPO_ROOT / "static" / "css" / "app.css"
APP_JS_PATH = REPO_ROOT / "static" / "js" / "app.js"
INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"


def _extract_function_body(source: str, function_name: str) -> str:
    pattern = re.compile(rf"(?:async\s+)?function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{")
    match = pattern.search(source)
    if not match:
        raise AssertionError(f"找不到函数定义: {function_name}")

    body_start = match.end()
    depth = 1
    index = body_start
    while index < len(source) and depth > 0:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1

    if depth != 0:
        raise AssertionError(f"函数体括号未闭合: {function_name}")

    return source[body_start:index - 1]


def _extract_brace_block_after(source: str, anchor: str) -> str:
    anchor_index = source.find(anchor)
    if anchor_index == -1:
        raise AssertionError(f"找不到代码锚点: {anchor}")

    body_start = source.find("{", anchor_index + len(anchor))
    if body_start == -1:
        raise AssertionError(f"锚点后找不到代码块起始大括号: {anchor}")

    depth = 1
    index = body_start + 1
    while index < len(source) and depth > 0:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1

    if depth != 0:
        raise AssertionError(f"代码块括号未闭合: {anchor}")

    return source[body_start + 1:index - 1]


class AdminFrontendStaticContractsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_css = APP_CSS_PATH.read_text(encoding="utf-8")
        cls.app_js = APP_JS_PATH.read_text(encoding="utf-8")
        cls.index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        cls.login_html = (REPO_ROOT / "static" / "login.html").read_text(encoding="utf-8")
        cls.register_html = (REPO_ROOT / "static" / "register.html").read_text(encoding="utf-8")
        cls.reply_server = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

    def test_app_css_imports_reference_existing_stylesheets(self):
        imports = re.findall(r"@import\s+url\('([^']+)'\);", self.app_css)
        self.assertGreater(len(imports), 0, "app.css 应该显式聚合子样式文件")

        missing_files = [
            relative_path
            for relative_path in imports
            if not (APP_CSS_PATH.parent / relative_path).exists()
        ]

        self.assertEqual(
            missing_files,
            [],
            f"app.css 存在失效的样式导入: {missing_files}",
        )

    def test_load_system_settings_only_calls_defined_top_level_helpers(self):
        defined_functions = {
            match.group(1)
            for match in re.finditer(
                r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                self.app_js,
            )
        }
        load_system_settings_body = _extract_function_body(self.app_js, "loadSystemSettings")

        bare_calls = {
            match.group(1)
            for match in re.finditer(
                r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(",
                load_system_settings_body,
            )
        }
        ignored_globals = {
            "fetch",
            "Error",
        }
        ignored_keywords = {
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "return",
        }

        missing_helpers = sorted(
            name
            for name in bare_calls
            if name not in defined_functions
            and name not in ignored_globals
            and name not in ignored_keywords
        )

        self.assertEqual(
            missing_helpers,
            [],
            f"loadSystemSettings 引用了未定义的顶层 helper: {missing_helpers}",
        )

    def test_show_section_stops_system_log_auto_refresh_with_current_helper(self):
        body = _extract_function_body(self.app_js, "showSection")
        self.assertIn("if (sectionName !== 'logs') {", body)
        self.assertIn("stopSystemLogAutoRefresh();", body)
        self.assertNotIn("window.autoRefreshInterval", body)
        self.assertNotIn("#autoRefreshText", body)

    def test_show_section_delayed_log_loaders_require_section_still_active_before_firing(self):
        body = _extract_function_body(self.app_js, "showSection")
        self.assertIn("document.getElementById('logs-section')?.classList.contains('active')", body)
        self.assertIn("document.getElementById('risk-control-logs-section')?.classList.contains('active')", body)
        self.assertLess(
            body.index("document.getElementById('logs-section')?.classList.contains('active')"),
            body.index("loadSystemLogs();"),
            "日志页 100ms 延迟自动加载前也得先确认页面还活着，别切走了还补刀发请求",
        )
        self.assertLess(
            body.index("document.getElementById('risk-control-logs-section')?.classList.contains('active')"),
            body.index("loadRiskControlLogs();"),
            "风控日志页 100ms 延迟自动加载前也得先确认页面还活着，别切走了还补刀发请求",
        )
        self.assertLess(
            body.index("document.getElementById('risk-control-logs-section')?.classList.contains('active')"),
            body.index("loadRiskLogAccountFilterOptions();"),
            "风控日志筛选器的延迟自动加载也别在页面都切走后再偷偷发请求",
        )

    def test_system_log_level_filter_scopes_badges_to_logs_section(self):
        body = _extract_function_body(self.app_js, "filterLogsByLevel")
        self.assertIn("document.querySelectorAll('#logs-section .filter-badge[data-level]')", body)
        self.assertIn("document.querySelector(`#logs-section [data-level=\"${level}\"]`)", body)
        self.assertNotIn("document.querySelectorAll('.filter-badge')", body)

    def test_system_logs_reset_summary_state_and_check_http_status_before_parsing(self):
        helper_body = _extract_function_body(self.app_js, "resetSystemLogInfo")
        load_body = _extract_function_body(self.app_js, "loadSystemLogs")

        self.assertIn("const fileNameElement = document.getElementById('logFileName');", helper_body)
        self.assertIn("const displayLinesElement = document.getElementById('logDisplayLines');", helper_body)
        self.assertIn("const lastUpdateElement = document.getElementById('logLastUpdate');", helper_body)
        self.assertIn("fileNameElement.textContent = '-';", helper_body)
        self.assertIn("displayLinesElement.textContent = '0';", helper_body)
        self.assertIn("lastUpdateElement.textContent = '最后更新: --';", helper_body)

        self.assertIn("resetSystemLogInfo();", load_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertLess(
            load_body.index("resetSystemLogInfo();"),
            load_body.index("const response = await fetch(url, {"),
            "系统日志加载前应先清空旧摘要状态",
        )
        self.assertLess(
            load_body.index("if (!response.ok) {"),
            load_body.index("const data = await response.json();"),
            "系统日志应先检查 HTTP 状态，再解析响应体",
        )

    def test_logs_raw_fetches_handle_unauthorized_before_followup_work(self):
        system_logs_body = _extract_function_body(self.app_js, "loadSystemLogs")
        log_file_list_body = _extract_function_body(self.app_js, "loadLogFileList")
        download_log_body = _extract_function_body(self.app_js, "downloadLogFile")

        for body, anchor_fragment, label in (
            (system_logs_body, "requestSequence !== systemLogRequestSequence", "系统日志加载"),
            (log_file_list_body, "requestSequence !== logFileListRequestSequence", "日志文件列表"),
            (download_log_body, "modalRequestSequence !== logFileModalRequestSequence", "日志文件下载"),
        ):
            with self.subTest(label=label):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{label} 遇到 401 得先回登录，别后面还拿未授权响应继续走旧页面逻辑",
                )

    def test_system_logs_treat_business_failure_payload_as_error_before_updating_ui(self):
        load_body = _extract_function_body(self.app_js, "loadSystemLogs")

        self.assertIn("if (data.success === false) {", load_body)
        self.assertIn("throw new Error(data.message || '加载日志失败');", load_body)
        self.assertLess(
            load_body.index("if (data.success === false) {"),
            load_body.index("loadingDiv.style.display = 'none';"),
            "系统日志接口都明确返回 success=false 了，就别装空数据继续刷 UI 了",
        )
        self.assertLess(
            load_body.index("if (data.success === false) {"),
            load_body.index("document.getElementById('logLastUpdate').textContent ="),
            "系统日志业务失败时不该继续回写最后更新时间",
        )

    def test_system_logs_http_failures_parse_detail_payloads_before_throwing_and_toasting(self):
        load_body = _extract_function_body(self.app_js, "loadSystemLogs")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("showToast(`加载日志失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        self.assertIn("if (!data || typeof data !== 'object' || (data.logs != null && !Array.isArray(data.logs))) {", load_body)
        self.assertIn("throw new Error('日志数据返回格式异常');", load_body)

        error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = load_body.index("throw new Error(errorMessage);", error_index)
        toast_index = load_body.index("showToast(`加载日志失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            error_index,
            throw_index,
            "系统日志 HTTP 失败时得先把 detail/message 解出来，别整固定 HTTP 文案糊脸",
        )
        self.assertLess(
            load_body.find("requestSequence !== systemLogRequestSequence", error_index),
            throw_index,
            "系统日志旧失败响应读完错误体后，先验 request sequence，别再往 catch 里回魂",
        )
        self.assertLess(
            load_body.find("!document.getElementById('logs-section')?.classList.contains('active')", error_index),
            throw_index,
            "都切出日志页了，旧失败响应读完错误体也别继续抛异常",
        )
        self.assertLess(
            load_body.rfind("!document.getElementById('logs-section')?.classList.contains('active')", 0, toast_index),
            toast_index,
            "都离开日志页了，旧失败请求进了 catch 也别跨页甩 toast",
        )

    def test_system_logs_distinguish_load_failures_from_empty_log_state(self):
        self.assertIn("function renderSystemLogsEmptyState(message = '暂无日志数据') {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "renderSystemLogsEmptyState")
        load_body = _extract_function_body(self.app_js, "loadSystemLogs")

        self.assertIn("const noLogsDiv = document.getElementById('noSystemLogs');", helper_body)
        self.assertIn("const messageElement = noLogsDiv.querySelector('p');", helper_body)
        self.assertIn("messageElement.textContent = message;", helper_body)
        self.assertIn("logContainer.style.display = 'none';", helper_body)

        self.assertIn("renderSystemLogsEmptyState(data.message || '暂无日志数据');", load_body)
        self.assertIn("renderSystemLogsEmptyState('加载日志失败，请稍后重试');", load_body)
        self.assertNotIn("noLogsDiv.style.display = 'block';", load_body)
        self.assertIn("updateLogInfo(data);", load_body)
        self.assertLess(
            load_body.index("updateLogInfo(data);"),
            load_body.index("if (data.logs && data.logs.length > 0) {"),
            "系统日志成功响应即便当前没命中任何日志，也得先把文件名和行数摘要回写，别又装成没加载到文件",
        )

    def test_system_log_summary_preserves_zero_line_counts(self):
        body = _extract_function_body(self.app_js, "updateLogInfo")

        self.assertIn("const fileNameElement = document.getElementById('logFileName');", body)
        self.assertIn("const displayLinesElement = document.getElementById('logDisplayLines');", body)
        self.assertIn("displayLinesElement.textContent = data.total_lines ?? '-';", body)
        self.assertNotIn("data.total_lines || '-'", body)

    def test_system_logs_parse_padded_level_columns_before_assigning_css_class(self):
        helper_body = _extract_function_body(self.app_js, "extractLogLevelTag")
        display_body = _extract_function_body(self.app_js, "displaySystemLogs")

        self.assertIn("const normalizedLine = String(logLine || '');", helper_body)
        self.assertIn("const match = normalizedLine.match(/\\|\\s*(INFO|WARNING|ERROR|DEBUG|CRITICAL)\\s*\\|/);", helper_body)
        self.assertIn("return match ? match[1] : '';", helper_body)
        self.assertIn("const normalizedLevel = extractLogLevelTag(log);", display_body)
        self.assertIn("if (normalizedLevel) {", display_body)
        self.assertIn("logLine.classList.add(normalizedLevel);", display_body)
        self.assertNotIn("if (log.includes('| INFO |')) {", display_body)
        self.assertNotIn("else if (log.includes('| ERROR |')) {", display_body)

    def test_system_logs_drop_legacy_dom_contracts_and_duplicate_helpers(self):
        legacy_fragments = [
            "window.autoRefreshInterval = null;",
            "window.allLogs = [];",
            "window.filteredLogs = [];",
            "function refreshLogs()",
            "function displayLogs()",
            "function updateLogStats()",
            "function clearLogsDisplay()",
            "function toggleAutoRefresh()",
            "function clearLogsServer()",
            "function showLogStats()",
            "document.getElementById('logContainer')",
            "document.getElementById('logCount')",
            "document.getElementById('lastUpdate')",
            "document.querySelector('#autoRefreshText')",
        ]

        for fragment in legacy_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, self.app_js)

        self.assertEqual(
            len(re.findall(r"function formatLogTimestamp\s*\(", self.app_js)),
            1,
            "formatLogTimestamp 不该保留多份旧实现",
        )

    def test_response_error_message_helper_parses_json_detail_payloads(self):
        self.assertIn("async function readResponseErrorMessage(response, fallbackMessage = '') {", self.app_js)
        body = _extract_function_body(self.app_js, "readResponseErrorMessage")
        self.assertIn("const errorText = await response.text();", body)
        self.assertIn("const errorJson = JSON.parse(errorText);", body)
        self.assertIn("return errorJson.detail || errorJson.message || errorText;", body)
        self.assertIn("return fallbackMessage || `HTTP ${response.status} ${response.statusText}`;", body)

    def test_unauthorized_response_helper_redirects_to_login_and_risk_control_fetches_reuse_it(self):
        self.assertIn("function handleUnauthorizedApiResponse(response) {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "handleUnauthorizedApiResponse")
        slider_body = _extract_function_body(self.app_js, "loadRiskControlSliderStats")
        page_body = _extract_function_body(self.app_js, "fetchRiskControlLogsPage")
        filter_body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")
        delete_body = _extract_function_body(self.app_js, "deleteRiskControlLog")
        clear_body = _extract_function_body(self.app_js, "clearRiskControlLogs")

        self.assertIn("if (response?.status !== 401) {", helper_body)
        self.assertIn("localStorage.removeItem('auth_token');", helper_body)
        self.assertIn("window.location.href = '/';", helper_body)
        self.assertIn("return true;", helper_body)

        for body, anchor_fragment, message in (
            (slider_body, "if (requestId !== currentRiskSliderStatsRequestId) {", "滑块统计 401 时应直接跳登录，别继续走旧请求状态分支"),
            (page_body, "if (!response.ok) {", "风控日志分页 401 时应直接跳登录，别继续伪造错误结果"),
            (filter_body, "if (response.ok) {", "风控日志账号筛选器 401 时应直接跳登录，别继续装成普通加载失败"),
            (delete_body, "const data = await response.json();", "删除风控日志 401 时应直接跳登录，别继续把未授权响应当业务 JSON 解析"),
            (clear_body, "const data = await response.json();", "清空风控日志 401 时应直接跳登录，别继续把未授权响应当业务 JSON 解析"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    message,
                )

    def test_log_file_list_loader_ignores_stale_async_responses(self):
        self.assertIn("let logFileListRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadLogFileList")
        self.assertIn("const requestSequence = ++logFileListRequestSequence;", body)
        self.assertIn("requestSequence !== logFileListRequestSequence", body)
        self.assertIn("return;", body)
        self.assertIn("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertLess(
            body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("error.textContent = `加载日志文件失败: ${message || response.status}`;"),
            "日志文件列表失败分支既然要异步读错误文本，就得在回写错误前防一手旧请求诈尸",
        )
        error_index = body.index("error.textContent = `加载日志文件失败: ${message || response.status}`;")
        message_index = body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        last_check_before_error = body.rfind("requestSequence !== logFileListRequestSequence", 0, error_index)
        self.assertGreater(
            last_check_before_error,
            message_index,
            "旧的日志文件列表失败请求不该在读完错误文本后再把新列表界面刷成报错",
        )

    def test_log_file_list_loader_respects_modal_session_and_hidden_logs_section(self):
        body = _extract_function_body(self.app_js, "loadLogFileList")

        self.assertIn("const modalRequestSequence = logFileModalRequestSequence;", body)
        self.assertIn("modalRequestSequence !== logFileModalRequestSequence", body)
        self.assertIn("!document.getElementById('logs-section')?.classList.contains('active')", body)

        loading_state_index = body.index("loading.classList.add('d-none');")
        self.assertLess(
            body.find("modalRequestSequence !== logFileModalRequestSequence", 0, loading_state_index),
            loading_state_index,
            "日志文件列表旧响应在改 loading 状态前得先验 modal session，别旧弹窗把新会话状态抹了",
        )
        self.assertLess(
            body.find("!document.getElementById('logs-section')?.classList.contains('active')", 0, loading_state_index),
            loading_state_index,
            "都切出日志页了，旧日志文件列表响应别回来改 modal loading 状态",
        )

        render_index = body.index("list.appendChild(item);")
        response_json_index = body.index("const data = await response.json();")
        self.assertGreater(
            body.rfind("modalRequestSequence !== logFileModalRequestSequence", 0, render_index),
            response_json_index,
            "日志文件列表读完结果后，渲染前还得再验 modal session，别旧会话把新列表糊回去",
        )
        self.assertGreater(
            body.rfind("!document.getElementById('logs-section')?.classList.contains('active')", 0, render_index),
            response_json_index,
            "都切出日志页了，旧日志文件列表成功响应也别回来往隐藏页塞内容",
        )

        error_text_index = body.index("error.textContent = `加载日志文件失败: ${message || response.status}`;")
        message_index = body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        self.assertGreater(
            body.rfind("modalRequestSequence !== logFileModalRequestSequence", 0, error_text_index),
            message_index,
            "日志文件列表失败响应读完错误文本后，先验 modal session，再决定要不要回写错误态",
        )
        self.assertGreater(
            body.rfind("!document.getElementById('logs-section')?.classList.contains('active')", 0, error_text_index),
            message_index,
            "都切出日志页了，旧日志文件列表失败响应读完错误文本也别回来刷报错",
        )

    def test_log_file_list_loader_rejects_malformed_payloads_before_rendering(self):
        body = _extract_function_body(self.app_js, "loadLogFileList")

        self.assertIn("if (!data || typeof data !== 'object' || Array.isArray(data)) {", body)
        self.assertIn("throw new Error('日志文件列表返回格式异常');", body)
        self.assertIn("if (data.success === true && data.files != null && !Array.isArray(data.files)) {", body)
        self.assertIn("const files = Array.isArray(data.files) ? data.files : [];", body)
        self.assertIn("error.textContent = `加载日志文件失败: ${err.message || '请稍后重试'}`;", body)

        format_guard_index = body.index("if (!data || typeof data !== 'object' || Array.isArray(data)) {")
        files_guard_index = body.index("if (data.success === true && data.files != null && !Array.isArray(data.files)) {")
        render_index = body.index("files.forEach(file => {")
        catch_error_index = body.index("error.textContent = `加载日志文件失败: ${err.message || '请稍后重试'}`;")

        self.assertLess(
            format_guard_index,
            render_index,
            "日志文件列表接口成功态如果回了歪 payload，先拦住再说，别一头扎进渲染逻辑里自爆",
        )
        self.assertLess(
            files_guard_index,
            render_index,
            "日志文件列表的 files 不是数组时得直接判格式异常，别等 forEach 当场翻车",
        )
        self.assertLess(
            body.index("throw new Error('日志文件列表返回格式异常');"),
            catch_error_index,
            "日志文件列表格式异常得把具体原因带到内联错误态，别又糊回统一重试文案装没事",
        )

    def test_log_file_export_modal_invalidates_inflight_list_requests_when_hidden(self):
        open_body = _extract_function_body(self.app_js, "openLogExportModal")

        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", open_body)
        self.assertIn("logFileListRequestSequence += 1;", open_body)
        self.assertIn("resetLogFileModalState();", open_body)
        self.assertIn("modalElement.dataset.logFileModalBound = 'true';", open_body)

    def test_system_logs_ignore_stale_async_responses(self):
        self.assertIn("let systemLogRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadSystemLogs")
        self.assertIn("const requestSequence = ++systemLogRequestSequence;", body)
        self.assertIn("requestSequence !== systemLogRequestSequence", body)
        self.assertIn("return;", body)
        self.assertLess(
            body.index("const data = await response.json();"),
            body.index("loadingDiv.style.display = 'none';"),
            "系统日志必须先拿到响应体，再判断这次请求是不是已经过期",
        )
        self.assertLess(
            body.index("requestSequence !== systemLogRequestSequence"),
            body.index("loadingDiv.style.display = 'none';"),
            "过期的系统日志请求不该再去改 loading/列表/空态",
        )

    def test_system_logs_are_invalidated_when_leaving_logs_section(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        stop_body = _extract_function_body(self.app_js, "stopSystemLogAutoRefresh")

        self.assertIn("systemLogRequestSequence += 1;", stop_body)
        self.assertIn("stopSystemLogAutoRefresh();", show_section_body)
        self.assertIn("const logExportModalElement = document.getElementById('exportLogModal');", show_section_body)
        self.assertIn("bootstrap.Modal.getInstance(logExportModalElement)", show_section_body)
        self.assertIn("logExportModal.hide();", show_section_body)

    def test_system_logs_do_not_update_hidden_section_or_emit_hidden_toasts(self):
        body = _extract_function_body(self.app_js, "loadSystemLogs")
        self.assertIn("!document.getElementById('logs-section')?.classList.contains('active')", body)
        self.assertLess(
            body.index("!document.getElementById('logs-section')?.classList.contains('active')"),
            body.index("loadingDiv.style.display = 'none';"),
            "都切出日志页面了，旧日志请求就别回来再改 loading/列表状态了",
        )
        self.assertLess(
            body.rfind("!document.getElementById('logs-section')?.classList.contains('active')"),
            body.index("showToast(`加载日志失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "都离开日志页了，旧失败请求不该再跨页弹日志错误 toast 烦人",
        )

    def test_risk_control_logs_recover_from_stale_offset_after_delete_or_filter_change(self):
        body = _extract_function_body(self.app_js, "loadRiskControlLogs")
        self.assertIn(
            "if (data.success && data.total > 0 && (!data.data || data.data.length === 0) && offset > 0) {",
            body,
        )
        self.assertIn(
            "const lastValidOffset = Math.max(0, Math.floor((data.total - 1) / limit) * limit);",
            body,
        )
        self.assertIn("if (lastValidOffset !== offset) {", body)
        self.assertIn("return loadRiskControlLogs(lastValidOffset);", body)
        recovery_index = body.index("return loadRiskControlLogs(lastValidOffset);")
        last_sequence_guard = body.rfind("if (requestSequence !== riskControlLogsRequestSequence) {", 0, recovery_index)
        last_hidden_guard = body.rfind("if (!document.getElementById('risk-control-logs-section')?.classList.contains('active')) {", 0, recovery_index)
        self.assertGreater(
            last_sequence_guard,
            body.index("const lastValidOffset = Math.max(0, Math.floor((data.total - 1) / limit) * limit);"),
            "风控日志旧分页请求准备回退 offset 前，先确认自己没过期，别抢当前新请求的方向盘",
        )
        self.assertGreater(
            last_hidden_guard,
            body.index("const lastValidOffset = Math.max(0, Math.floor((data.total - 1) / limit) * limit);"),
            "都切出风控日志页了，旧分页恢复逻辑就别再偷偷发回退请求了",
        )

    def test_risk_control_slider_stats_distinguish_load_failures_from_empty_state(self):
        self.assertIn(
            "function setRiskControlSliderStatsError(scopeLabel = '全部账号', message = '滑块验证统计加载失败，请稍后重试') {",
            self.app_js,
        )
        helper_body = _extract_function_body(self.app_js, "setRiskControlSliderStatsError")
        load_body = _extract_function_body(self.app_js, "loadRiskControlSliderStats")

        self.assertIn("const attemptCountElement = document.getElementById('riskSliderAttemptCount');", helper_body)
        self.assertIn("if (attemptCountElement) attemptCountElement.textContent = message;", helper_body)
        self.assertIn("if (successRateElement) successRateElement.textContent = '--';", helper_body)
        self.assertIn("if (successCountElement) successCountElement.textContent = '--';", helper_body)
        self.assertIn("if (failureCountElement) failureCountElement.textContent = '--';", helper_body)

        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("setRiskControlSliderStatsError(scopeLabel, message || '滑块验证统计加载失败，请稍后重试');", load_body)
        self.assertIn("if (!data.success) {", load_body)
        self.assertIn("setRiskControlSliderStatsError(scopeLabel, data.message || data.detail || '滑块验证统计加载失败，请稍后重试');", load_body)
        self.assertIn("renderRiskControlSliderStats(data.data || {});", load_body)
        self.assertLess(
            load_body.index("if (!response.ok) {"),
            load_body.index("renderRiskControlSliderStats(data.data || {});"),
            "风控滑块统计应先把失败分支踢出去，别把接口异常伪装成空数据",
        )
        self.assertLess(
            load_body.index("if (!data.success) {"),
            load_body.index("renderRiskControlSliderStats(data.data || {});"),
            "风控滑块统计业务失败也得先踢出去，别拿异常响应当空数据刷 UI",
        )

    def test_risk_control_logs_ignore_stale_async_responses(self):
        self.assertIn("let riskControlLogsRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadRiskControlLogs")

        self.assertIn("const requestSequence = ++riskControlLogsRequestSequence;", body)
        self.assertIn("if (requestSequence !== riskControlLogsRequestSequence) {", body)
        self.assertIn("return false;", body)
        self.assertLess(
            body.index("if (requestSequence !== riskControlLogsRequestSequence) {"),
            body.index("loadingDiv.style.display = 'none';"),
            "过期的风控日志请求不该再改 loading/表格/空态",
        )

    def test_risk_control_client_side_filter_stops_stale_followup_paging_and_surfaces_page_failures(self):
        helper_body = _extract_function_body(self.app_js, "fetchRiskControlLogsWithClientFilter")
        load_body = _extract_function_body(self.app_js, "loadRiskControlLogs")

        self.assertEqual(
            helper_body.count("if (typeof shouldStop === 'function' && shouldStop()) {"),
            2,
            "风控日志 client-side filter 在补拉分页前后都该能停下来，别请求过期了还把所有分页跑完",
        )
        self.assertIn("aborted: true,", helper_body)
        self.assertIn("if (pageData.aborted) {", helper_body)
        self.assertIn("return pageData;", helper_body)
        self.assertIn("if (pageData.success !== true) {", helper_body)
        self.assertIn("message: pageData.message || pageData.detail || '加载风控日志失败',", helper_body)
        self.assertLess(
            helper_body.index("if (pageData.success !== true) {"),
            helper_body.index("const pageLogs = Array.isArray(pageData.data) ? pageData.data : [];"),
            "风控日志 client-side filter 单页拉取失败时，不能当成空页继续往下糊弄",
        )
        self.assertIn("data = await fetchRiskControlLogsWithClientFilter(token, {", load_body)
        self.assertIn("}, () => (", load_body)
        self.assertIn("if (data?.aborted) {", load_body)

    def test_risk_control_requests_are_invalidated_when_leaving_section(self):
        self.assertIn("let riskLogAccountFilterRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("if (sectionName !== 'risk-control-logs') {", show_section_body)
        self.assertIn("currentRiskSliderStatsRequestId += 1;", show_section_body)
        self.assertIn("riskControlLogsRequestSequence += 1;", show_section_body)
        self.assertIn("riskLogAccountFilterRequestSequence += 1;", show_section_body)

    def test_risk_control_requests_do_not_update_hidden_section_or_emit_hidden_toasts(self):
        slider_body = _extract_function_body(self.app_js, "loadRiskControlSliderStats")
        logs_body = _extract_function_body(self.app_js, "loadRiskControlLogs")

        self.assertIn("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", slider_body)
        self.assertIn("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", logs_body)
        self.assertLess(
            logs_body.index("!document.getElementById('risk-control-logs-section')?.classList.contains('active')"),
            logs_body.index("loadingDiv.style.display = 'none';"),
            "都切出风控日志页面了，旧请求就别回来再改 loading/列表状态",
        )
        self.assertLess(
            logs_body.index("!document.getElementById('risk-control-logs-section')?.classList.contains('active')"),
            logs_body.index("showToast(data.message || data.detail || '加载风控日志失败', 'danger');"),
            "都离开风控日志页了，旧失败请求不该跨页弹 toast 继续烦人",
        )
        self.assertLess(
            logs_body.rfind("!document.getElementById('risk-control-logs-section')?.classList.contains('active')"),
            logs_body.rfind("showToast(`加载风控日志失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "风控日志异常分支也得在弹 toast 前先确认页面还活着",
        )

    def test_risk_control_slider_stats_error_path_does_not_update_hidden_section(self):
        body = _extract_function_body(self.app_js, "loadRiskControlSliderStats")
        error_state_index = body.index("setRiskControlSliderStatsError(scopeLabel, message || '滑块验证统计加载失败，请稍后重试');")

        self.assertLess(
            body.rfind("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", 0, error_state_index),
            error_state_index,
            "都切出风控日志页了，旧的滑块统计异常分支也别回来把隐藏页面刷成 error 态",
        )

    def test_risk_control_slider_stats_http_failures_parse_detail_payloads_before_showing_error_state(self):
        body = _extract_function_body(self.app_js, "loadRiskControlSliderStats")

        self.assertIn("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("setRiskControlSliderStatsError(scopeLabel, message || '滑块验证统计加载失败，请稍后重试');", body)
        self.assertIn("setRiskControlSliderStatsError(scopeLabel, data.message || data.detail || '滑块验证统计加载失败，请稍后重试');", body)

        http_error_index = body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        http_error_state_index = body.index("setRiskControlSliderStatsError(scopeLabel, message || '滑块验证统计加载失败，请稍后重试');", http_error_index)
        self.assertLess(
            http_error_index,
            http_error_state_index,
            "风控滑块统计 HTTP 失败时得先把 detail/message 解出来，别直接糊默认错误态",
        )
        self.assertLess(
            body.find("requestId !== currentRiskSliderStatsRequestId", http_error_index),
            http_error_state_index,
            "风控滑块统计旧失败响应读完错误体后，先验 requestId，别回魂改当前卡片",
        )
        self.assertLess(
            body.find("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", http_error_index),
            http_error_state_index,
            "都切出风控日志页了，旧滑块统计失败响应读完错误体也别再刷 error 态",
        )

    def test_risk_control_slider_stats_rejects_malformed_success_payload_before_rendering(self):
        body = _extract_function_body(self.app_js, "loadRiskControlSliderStats")

        self.assertIn("if (!data || typeof data !== 'object' || Array.isArray(data)) {", body)
        self.assertIn("throw new Error('滑块验证统计返回格式异常');", body)
        self.assertIn("if (data.success === true && (!data.data || typeof data.data !== 'object' || Array.isArray(data.data))) {", body)
        self.assertIn("setRiskControlSliderStatsError(scopeLabel, error.message || '滑块验证统计加载失败，请稍后重试');", body)

        object_guard_index = body.index("if (!data || typeof data !== 'object' || Array.isArray(data)) {")
        payload_guard_index = body.index("if (data.success === true && (!data.data || typeof data.data !== 'object' || Array.isArray(data.data))) {")
        render_index = body.index("renderRiskControlSliderStats(data.data || {});")
        catch_error_state_index = body.index("setRiskControlSliderStatsError(scopeLabel, error.message || '滑块验证统计加载失败，请稍后重试');")

        self.assertLess(
            object_guard_index,
            render_index,
            "风控滑块统计如果连最外层 JSON 都歪了，得先拦住，别还把垃圾 payload 当空数据往卡片里灌",
        )
        self.assertLess(
            payload_guard_index,
            render_index,
            "风控滑块统计 success=true 但 data 不是对象时，别继续 render，省得异常被伪装成正常空态",
        )
        self.assertLess(
            body.index("throw new Error('滑块验证统计返回格式异常');"),
            catch_error_state_index,
            "风控滑块统计格式异常既然已经识别出来了，错误态就把明确信息带出来，别又糊回默认文案装大尾巴狼",
        )

    def test_risk_control_account_filter_loader_ignores_stale_async_responses_and_hidden_section(self):
        body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")

        self.assertIn("const requestSequence = ++riskLogAccountFilterRequestSequence;", body)
        self.assertIn("requestSequence !== riskLogAccountFilterRequestSequence", body)
        self.assertIn("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== riskLogAccountFilterRequestSequence"),
            body.index("data.accounts.forEach(account => {"),
            "旧的风控账号筛选器请求不该晚回来后又把当前选项糊回去",
        )

    def test_risk_control_logs_distinguish_load_failures_from_empty_state(self):
        self.assertIn("function renderRiskControlLogsEmptyState(message = '暂无风控日志数据') {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "renderRiskControlLogsEmptyState")
        load_body = _extract_function_body(self.app_js, "loadRiskControlLogs")

        self.assertIn("const noLogsDiv = document.getElementById('noRiskLogs');", helper_body)
        self.assertIn("const messageElement = noLogsDiv.querySelector('p');", helper_body)
        self.assertIn("messageElement.textContent = message;", helper_body)
        self.assertIn("logContainer.style.display = 'none';", helper_body)

        self.assertIn("renderRiskControlLogsEmptyState();", load_body)
        self.assertIn("renderRiskControlLogsEmptyState('加载风控日志失败，请稍后重试');", load_body)
        self.assertIn("showToast(data.message || data.detail || '加载风控日志失败', 'danger');", load_body)
        self.assertIn("showToast(`加载风控日志失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        self.assertNotIn("noLogsDiv.style.display = 'block';", load_body)

    def test_risk_control_logs_catch_toast_surfaces_runtime_errors(self):
        body = _extract_function_body(self.app_js, "loadRiskControlLogs")

        self.assertIn("showToast(`加载风控日志失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("showToast('加载风控日志失败', 'danger');", body)

        catch_index = body.index("} catch (error) {")
        toast_index = body.index("showToast(`加载风控日志失败: ${error.message || '请稍后重试'}`, 'danger');", catch_index)

        self.assertLess(
            body.find("requestSequence !== riskControlLogsRequestSequence", catch_index),
            toast_index,
            "风控日志旧异常请求也得先验 request sequence，别新请求都起飞了老 toast 还回来抢戏",
        )
        self.assertLess(
            body.find("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", catch_index),
            toast_index,
            "都切出风控日志页了，旧异常 toast 也别跨页回魂吓人",
        )

    def test_risk_control_http_failures_parse_detail_payloads_before_touching_ui(self):
        page_body = _extract_function_body(self.app_js, "fetchRiskControlLogsPage")
        delete_body = _extract_function_body(self.app_js, "deleteRiskControlLog")
        clear_body = _extract_function_body(self.app_js, "clearRiskControlLogs")

        self.assertIn("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);", page_body)
        self.assertIn("message: message || `HTTP ${response.status}`,", page_body)
        self.assertLess(
            page_body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            page_body.index("return response.json();"),
            "风控日志分页 HTTP 失败时得先把 detail/message 解出来，别糊个通用错误完事",
        )

        for body, toast_fragment, message in (
            (delete_body, "showToast(message || '删除失败', 'danger');", "删除风控日志的 HTTP 失败不该只弹个笼统失败"),
            (clear_body, "showToast(message || '清空失败', 'danger');", "清空风控日志的 HTTP 失败不该只弹个笼统失败"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(toast_fragment),
                    message,
                )

    def test_risk_control_log_filter_panel_exposes_trigger_scene_date_and_session_controls(self):
        filters_body = _extract_function_body(self.app_js, "getRiskLogFilters")

        self.assertIn('id="riskLogTriggerSceneFilter"', self.index_html)
        self.assertIn('id="riskLogDateFrom"', self.index_html)
        self.assertIn('id="riskLogDateTo"', self.index_html)
        self.assertIn('id="riskLogSessionFilter"', self.index_html)
        self.assertIn('onchange="loadRiskControlLogs()"', self.index_html)
        self.assertIn('placeholder="输入链路ID筛选"', self.index_html)

        self.assertIn("triggerScene: document.getElementById('riskLogTriggerSceneFilter')?.value || '',", filters_body)
        self.assertIn("dateFrom: document.getElementById('riskLogDateFrom')?.value || '',", filters_body)
        self.assertIn("dateTo: document.getElementById('riskLogDateTo')?.value || '',", filters_body)
        self.assertIn("sessionId: (document.getElementById('riskLogSessionFilter')?.value || '').trim(),", filters_body)

    def test_risk_control_log_filter_panel_exposes_result_code_control_and_marks_it_active_filter(self):
        filters_body = _extract_function_body(self.app_js, "getRiskLogFilters")
        active_filters_body = _extract_function_body(self.app_js, "hasActiveRiskLogFilters")

        self.assertIn('id="riskLogResultCodeFilter"', self.index_html)
        self.assertIn('placeholder="输入结果代码筛选"', self.index_html)

        self.assertIn("resultCode: (document.getElementById('riskLogResultCodeFilter')?.value || '').trim(),", filters_body)
        self.assertIn("filters.resultCode", active_filters_body)

    def test_risk_control_logs_use_attribute_specific_escaping_for_titles_and_reset_account_filter_before_fetch(self):
        summary_body = _extract_function_body(self.app_js, "renderRiskLogSummaryCell")
        display_body = _extract_function_body(self.app_js, "displayRiskControlLogs")
        filter_body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")

        self.assertIn("const descriptionAttr = escapeHtmlAttribute(descriptionText);", summary_body)
        self.assertIn('title="${descriptionAttr}"', summary_body)
        self.assertNotIn('title="${description}"', summary_body)

        self.assertIn("const safeEventTypeAttr = escapeHtmlAttribute(log.event_type || '-');", display_body)
        self.assertIn("const safeTriggerSceneAttr = escapeHtmlAttribute(log.trigger_scene || '-');", display_body)
        self.assertIn("const sessionTitle = escapeHtmlAttribute(log.session_id || log.session_display || '-');", display_body)
        self.assertIn('title="原始类型: ${safeEventTypeAttr}"', display_body)
        self.assertIn('title="触发场景: ${safeTriggerSceneAttr}"', display_body)
        self.assertNotIn('title="原始类型: ${escapeHtml(log.event_type || \'-\')}"', display_body)
        self.assertNotIn("const sessionTitle = escapeHtml(log.session_id || log.session_display || '-');", display_body)

        self.assertIn("const select = document.getElementById('riskLogAccountFilter');", filter_body)
        self.assertIn("select.innerHTML = '<option value=\"\">全部账号</option>';", filter_body)
        self.assertLess(
            filter_body.index("select.innerHTML = '<option value=\"\">全部账号</option>';"),
            filter_body.index("const response = await fetch('/admin/accounts', {"),
            "风控日志账号筛选器应先清空旧选项，再发请求",
        )

    def test_risk_control_logs_account_filter_preserves_current_selection_when_still_available(self):
        body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")

        self.assertIn("const previousValue = select.value;", body)
        self.assertIn("const hasPreviousOption = previousValue && Array.from(select.options).some(option => option.value === previousValue);", body)
        self.assertIn("if (hasPreviousOption) {", body)
        self.assertIn("select.value = previousValue;", body)

    def test_risk_control_logs_account_filter_resyncs_logs_when_previous_selection_is_no_longer_available(self):
        body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")

        self.assertIn("if (!hasPreviousOption && previousValue) {", body)
        self.assertIn("select.value = '';", body)
        self.assertIn("await loadRiskControlLogs(0);", body)

    def test_risk_control_logs_account_filter_keeps_active_context_when_option_reload_fails(self):
        self.assertIn("function restoreRiskLogAccountFilterFallbackOption(select, previousValue) {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "restoreRiskLogAccountFilterFallbackOption")
        body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")

        self.assertIn("if (!select) {", helper_body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 账号列表加载失败，请稍后重试</option>';", helper_body)
        self.assertIn("const fallbackOption = document.createElement('option');", helper_body)
        self.assertIn("fallbackOption.value = previousValue;", helper_body)
        self.assertIn("fallbackOption.textContent = `${previousValue} (当前筛选账号)`;", helper_body)
        self.assertIn("select.appendChild(fallbackOption);", helper_body)
        self.assertIn("select.value = previousValue;", helper_body)

        self.assertIn("restoreRiskLogAccountFilterFallbackOption(select, previousValue);", body)

    def test_risk_control_logs_account_filter_surfaces_structured_failures_in_warning_toasts(self):
        body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("showToast(`加载账号选项失败: ${errorMessage || '请稍后重试'}`, 'warning');", body)
        self.assertIn("showToast(`加载账号选项失败: ${data.message || data.detail || '请稍后重试'}`, 'warning');", body)
        self.assertIn("showToast(`加载账号选项失败: ${error.message || '请稍后重试'}`, 'warning');", body)

        http_error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        http_toast_index = body.index("showToast(`加载账号选项失败: ${errorMessage || '请稍后重试'}`, 'warning');", http_error_index)
        self.assertLess(
            http_error_index,
            http_toast_index,
            "风控日志账号筛选器 HTTP 失败时得先把 detail/message 解出来，别闷头只会回退旧选项不吭声",
        )
        self.assertLess(
            body.find("requestSequence !== riskLogAccountFilterRequestSequence", http_error_index),
            http_toast_index,
            "风控日志账号筛选器旧失败响应读完错误体后，先验 request sequence，别回魂对当前页面乱弹 warning",
        )
        self.assertLess(
            body.find("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", http_error_index),
            http_toast_index,
            "都切出风控日志页了，旧账号筛选器失败响应读完错误体也别跨页弹 warning toast",
        )

    def test_risk_control_logs_account_filter_rejects_malformed_success_payload_before_rendering(self):
        body = _extract_function_body(self.app_js, "loadRiskLogAccountFilterOptions")

        self.assertIn("if (!data || typeof data !== 'object' || Array.isArray(data)) {", body)
        self.assertIn("throw new Error('账号选项返回格式异常');", body)
        self.assertIn("if (data.success === true && !Array.isArray(data.accounts)) {", body)

        object_guard_index = body.index("if (!data || typeof data !== 'object' || Array.isArray(data)) {")
        accounts_guard_index = body.index("if (data.success === true && !Array.isArray(data.accounts)) {")
        render_index = body.index("data.accounts.forEach(account => {")

        self.assertLess(
            object_guard_index,
            render_index,
            "风控日志账号筛选器拿到歪 JSON 时得先拦住，别还当真数据继续往下渲染",
        )
        self.assertLess(
            accounts_guard_index,
            render_index,
            "风控日志账号筛选器的 accounts 不是数组时别硬上 forEach，不然这玩意儿当场就炸",
        )

    def test_risk_control_logs_account_filter_sets_explicit_failure_option_when_reload_fails_without_selection(self):
        helper_body = _extract_function_body(self.app_js, "restoreRiskLogAccountFilterFallbackOption")

        self.assertIn("if (!previousValue) {", helper_body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 账号列表加载失败，请稍后重试</option>';", helper_body)
        self.assertLess(
            helper_body.index("if (!previousValue) {"),
            helper_body.index("const fallbackOption = document.createElement('option');"),
            "风控日志账号筛选器拉取失败且当前没选账号时，也得落成明确失败态，别伪装成正常空列表",
        )

    def test_risk_control_logs_pagination_links_prevent_hash_navigation_and_disabled_links_do_not_issue_invalid_requests(self):
        body = _extract_function_body(self.app_js, "updateRiskLogPagination")

        self.assertIn("prevLi.innerHTML = currentPage === 1", body)
        self.assertIn("? '<span class=\"page-link\">上一页</span>'", body)
        self.assertIn(": `<a class=\"page-link\" href=\"#\" onclick=\"loadRiskControlLogs(${(currentPage - 2) * limit}); return false;\">上一页</a>`;", body)
        self.assertIn('li.innerHTML = `<a class="page-link" href="#" onclick="loadRiskControlLogs(${(i - 1) * limit}); return false;">${i}</a>`;', body)
        self.assertIn("nextLi.innerHTML = currentPage === totalPages", body)
        self.assertIn("? '<span class=\"page-link\">下一页</span>'", body)
        self.assertIn(": `<a class=\"page-link\" href=\"#\" onclick=\"loadRiskControlLogs(${currentPage * limit}); return false;\">下一页</a>`;", body)

        self.assertNotIn('prevLi.innerHTML = `<a class="page-link" href="#" onclick="loadRiskControlLogs(${(currentPage - 2) * limit})">上一页</a>`;', body)
        self.assertNotIn('li.innerHTML = `<a class="page-link" href="#" onclick="loadRiskControlLogs(${(i - 1) * limit})">${i}</a>`;', body)
        self.assertNotIn('nextLi.innerHTML = `<a class="page-link" href="#" onclick="loadRiskControlLogs(${currentPage * limit})">下一页</a>`;', body)

    def test_risk_control_log_mutations_only_report_success_when_followup_reload_succeeds(self):
        load_body = _extract_function_body(self.app_js, "loadRiskControlLogs")
        delete_body = _extract_function_body(self.app_js, "deleteRiskControlLog")
        clear_body = _extract_function_body(self.app_js, "clearRiskControlLogs")

        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)

        self.assertIn("const loaded = await loadRiskControlLogs(currentRiskLogOffset);", delete_body)
        self.assertIn("if (loaded) {", delete_body)
        self.assertIn("showToast('删除成功', 'success');", delete_body)
        self.assertIn("showToast('删除成功，但风控日志列表刷新失败，请稍后手动刷新', 'warning');", delete_body)
        self.assertLess(
            delete_body.index("const loaded = await loadRiskControlLogs(currentRiskLogOffset);"),
            delete_body.index("showToast('删除成功', 'success');"),
            "删除风控日志应先确认列表刷新成功，再提示 success",
        )

        self.assertIn("const loaded = await loadRiskControlLogs(0);", clear_body)
        self.assertIn("if (loaded) {", clear_body)
        self.assertIn("showToast('风控日志已清空', 'success');", clear_body)
        self.assertIn("showToast('风控日志已清空，但风控日志列表刷新失败，请稍后手动刷新', 'warning');", clear_body)
        self.assertLess(
            clear_body.index("const loaded = await loadRiskControlLogs(0);"),
            clear_body.index("showToast('风控日志已清空', 'success');"),
            "清空风控日志应先确认列表刷新成功，再提示 success",
        )

    def test_risk_control_log_mutations_do_not_emit_cross_page_toasts_after_leaving_section(self):
        delete_body = _extract_function_body(self.app_js, "deleteRiskControlLog")
        clear_body = _extract_function_body(self.app_js, "clearRiskControlLogs")

        for body, success_fragment in (
            (delete_body, "showToast('删除成功', 'success');"),
            (clear_body, "showToast('风控日志已清空', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('risk-control-logs-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "都切出风控日志页了，旧 mutation 响应不该再跨页弹 success toast",
                )

    def test_risk_control_log_mutation_catch_toasts_surface_runtime_errors(self):
        for body, toast_fragment, legacy_fragment, label in (
            (
                _extract_function_body(self.app_js, "deleteRiskControlLog"),
                "showToast(`删除失败: ${error.message || '请稍后重试'}`, 'danger');",
                "showToast('删除失败', 'danger');",
                "删除风控日志",
            ),
            (
                _extract_function_body(self.app_js, "clearRiskControlLogs"),
                "showToast(`清空失败: ${error.message || '请稍后重试'}`, 'danger');",
                "showToast('清空失败', 'danger');",
                "清空风控日志",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn(toast_fragment, body)
                self.assertNotIn(legacy_fragment, body)

                catch_index = body.index("} catch (error) {")
                toast_index = body.index(toast_fragment, catch_index)

                self.assertLess(
                    body.find("actionRequestSequence !== riskControlLogMutationActionRequestSequence", catch_index),
                    toast_index,
                    f"同页已经发起新的{label}动作后，旧异常也别回来回魂甩红字",
                )
                self.assertLess(
                    body.find("!document.getElementById('risk-control-logs-section')?.classList.contains('active')", catch_index),
                    toast_index,
                    f"都切出风控日志页了，旧的{label}异常也别跨页弹 danger toast",
                )

    def test_risk_control_log_mutations_ignore_older_same_page_responses(self):
        self.assertIn("let riskControlLogMutationActionRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        delete_body = _extract_function_body(self.app_js, "deleteRiskControlLog")
        clear_body = _extract_function_body(self.app_js, "clearRiskControlLogs")

        self.assertIn("riskControlLogMutationActionRequestSequence += 1;", show_section_body)

        for body, anchor_fragment in (
            (delete_body, "const loaded = await loadRiskControlLogs(currentRiskLogOffset);"),
            (clear_body, "const loaded = await loadRiskControlLogs(0);"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("++riskControlLogMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== riskControlLogMutationActionRequestSequence", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== riskControlLogMutationActionRequestSequence"),
                    body.index(anchor_fragment),
                    "同页连续执行风控日志 mutation 时，旧响应不该晚回来后又触发列表刷新",
                )

    def test_online_im_account_list_clears_stale_options_and_surfaces_refresh_failures(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")
        self.assertIn("const select = document.getElementById('imAccountSelect');", body)
        self.assertIn("select.innerHTML = '<option value=\"\">-- 选择账号 --</option>';", body)
        self.assertIn("const data = await fetchJSONWithoutGlobalLoading(`${apiBase}/accounts`, {", body)
        self.assertIn("throw new Error('账号列表返回格式异常');", body)
        self.assertIn("showToast(`加载在线客服账号失败: ${error.message || '请稍后重试'}`, 'warning');", body)

    def test_online_im_non_loading_json_helper_preserves_auth_redirect_and_error_detail_parsing(self):
        body = _extract_function_body(self.app_js, "fetchJSONWithoutGlobalLoading")

        self.assertIn("requestOptions.headers['Authorization'] = `Bearer ${authToken}`;", body)
        self.assertIn("if (res.status === 401) {", body)
        self.assertIn("localStorage.removeItem('auth_token');", body)
        self.assertIn("window.location.href = '/';", body)
        self.assertIn("return null;", body)
        self.assertIn("const errorJson = JSON.parse(errorText);", body)
        self.assertIn("errorMessage = errorJson.detail || errorJson.message || errorText;", body)
        self.assertIn("showToast(err.message || '操作失败', 'danger');", body)

    def test_online_im_account_list_sets_explicit_failure_option_when_reload_fails(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 账号列表加载失败，请稍后重试</option>';", body)
        self.assertLess(
            body.rfind("select.innerHTML = '<option value=\"\">❌ 账号列表加载失败，请稍后重试</option>';"),
            body.index("showToast(`加载在线客服账号失败: ${error.message || '请稍后重试'}`, 'warning');"),
            "在线客服账号列表拉取失败时得把下拉框落成失败态，别只留个默认占位让人以为只是没选账号",
        )

    def test_online_im_account_list_keeps_active_context_when_reload_fails(self):
        self.assertIn("function restoreImAccountListFailureOption(select, previousValue, previousLabel = '', previousUsername = '', previousPasswordDisplay = '') {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "restoreImAccountListFailureOption")
        body = _extract_function_body(self.app_js, "loadImAccountList")

        self.assertIn("const previousOption = select ? select.selectedOptions[0] : null;", body)
        self.assertIn("const previousLabel = previousOption ? String(previousOption.textContent || '').trim() : '';", body)
        self.assertIn("const previousUsername = previousOption ? String(previousOption.dataset.username || '').trim() : '';", body)
        self.assertIn("const previousUsernameDisplay = usernameEl ? String(usernameEl.textContent || '').trim() : '-';", body)
        self.assertIn("const previousPasswordDisplay = passwordEl ? String(passwordEl.textContent || '').trim() : '-';", body)
        self.assertIn("restoreImAccountListFailureOption(select, previousValue, previousLabel, previousUsername, previousPasswordDisplay);", body)
        self.assertIn("usernameEl.textContent = previousValue", body)
        self.assertIn("passwordEl.textContent = previousValue", body)
        self.assertIn("const fallbackUsername = previousUsernameDisplay", body)
        self.assertIn("previousUsernameDisplay !== '-'", body)
        self.assertIn("previousUsernameDisplay !== '加载中...'", body)
        self.assertIn("const fallbackPassword = previousPasswordDisplay", body)
        self.assertIn("previousPasswordDisplay !== '加载中...'", body)
        self.assertIn("previousPasswordDisplay !== '获取失败'", body)
        self.assertIn("previousPasswordDisplay !== '未获取'", body)
        self.assertIn("usernameEl.textContent = fallbackUsername;", body)
        self.assertIn("passwordEl.textContent = fallbackPassword;", body)

        self.assertIn("const fallbackOption = document.createElement('option');", helper_body)
        self.assertIn("fallbackOption.value = previousValue;", helper_body)
        self.assertIn("fallbackOption.textContent = previousLabel || `${previousValue} (当前账号)`;", helper_body)
        self.assertIn("const normalizedPreviousUsername = String(previousUsername || '').trim();", helper_body)
        self.assertIn("fallbackOption.dataset.username = (", helper_body)
        self.assertIn("const normalizedPreviousPasswordDisplay = String(previousPasswordDisplay || '').trim();", helper_body)
        self.assertIn("fallbackOption.dataset.passwordDisplay = (", helper_body)
        self.assertIn("select.appendChild(fallbackOption);", helper_body)
        self.assertIn("select.value = previousValue;", helper_body)

    def test_online_im_account_list_preserves_visible_context_during_refresh_until_new_detail_arrives(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")

        self.assertIn("usernameEl.textContent = previousValue", body)
        self.assertIn("previousUsernameDisplay !== '获取失败'", body)
        self.assertIn("(previousValue ? '加载中...' : '-')", body)
        self.assertLess(
            body.index("usernameEl.textContent = previousValue"),
            body.index("const data = await fetchJSONWithoutGlobalLoading(`${apiBase}/accounts`, {"),
            "在线客服刷新账号列表时，如果当前还有选中账号，就别先把账号栏清成横杠，至少等新详情回来前保住当前上下文",
        )
        self.assertIn("passwordEl.textContent = previousValue", body)
        self.assertIn("previousPasswordDisplay !== '未获取'", body)
        self.assertLess(
            body.index("passwordEl.textContent = previousValue"),
            body.index("const data = await fetchJSONWithoutGlobalLoading(`${apiBase}/accounts`, {"),
            "在线客服刷新账号列表时，如果当前还有选中账号，就别先把密码栏清成横杠，至少等新详情回来前保住当前上下文",
        )

    def test_online_im_account_list_restores_password_display_when_reload_fails(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")

        password_capture_index = body.index("const previousPasswordDisplay = passwordEl ? String(passwordEl.textContent || '').trim() : '-';")
        password_restore_index = body.index("passwordEl.textContent = fallbackPassword;")
        loading_filter_index = body.index("previousPasswordDisplay !== '加载中...'", password_capture_index)

        self.assertLess(
            password_capture_index,
            password_restore_index,
            "在线客服账号列表刷新失败时，得先记住旧密码展示态，再决定怎么恢复当前账号上下文",
        )
        self.assertLess(
            loading_filter_index,
            password_restore_index,
            "旧密码展示如果还停在加载中，就别原样回填成僵尸 loading，得先兜底成稳定文案",
        )

    def test_online_im_account_list_restores_username_display_when_reload_fails_without_zombie_loading_text(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")

        username_capture_index = body.index("const previousUsernameDisplay = usernameEl ? String(usernameEl.textContent || '').trim() : '-';")
        username_restore_index = body.index("usernameEl.textContent = fallbackUsername;")
        loading_filter_index = body.index("previousUsernameDisplay !== '加载中...'", username_capture_index)

        self.assertLess(
            username_capture_index,
            username_restore_index,
            "在线客服账号列表刷新失败时，得先记住旧账号展示态，再决定怎么恢复当前账号上下文",
        )
        self.assertLess(
            loading_filter_index,
            username_restore_index,
            "旧账号展示如果还停在加载中，就别原样回填成僵尸 loading，得先兜底成稳定文案",
        )

    def test_online_im_account_list_does_not_restore_failure_placeholders_as_context(self):
        helper_body = _extract_function_body(self.app_js, "restoreImAccountListFailureOption")
        body = _extract_function_body(self.app_js, "loadImAccountList")

        self.assertIn("normalizedPreviousPasswordDisplay !== '获取失败'", helper_body)
        self.assertIn("normalizedPreviousPasswordDisplay !== '未获取'", helper_body)
        self.assertIn("previousUsernameDisplay !== '获取失败'", body)
        self.assertIn("previousPasswordDisplay !== '获取失败'", body)
        self.assertIn("previousPasswordDisplay !== '未获取'", body)
        self.assertLess(
            body.index("previousUsernameDisplay !== '获取失败'"),
            body.index("usernameEl.textContent = fallbackUsername;"),
            "在线客服账号列表刷新失败时，旧账号栏如果已经是获取失败，就别再原样回填成僵尸错误态",
        )
        self.assertLess(
            body.index("previousPasswordDisplay !== '获取失败'"),
            body.index("passwordEl.textContent = fallbackPassword;"),
            "在线客服账号列表刷新失败时，旧密码栏如果已经是获取失败，也别再原样回填成僵尸错误态",
        )

    def test_online_im_account_switch_does_not_treat_unfetched_password_placeholder_as_cached_state(self):
        body = _extract_function_body(self.app_js, "onImAccountChange")

        self.assertIn("const cachedPasswordDisplay = String(selectedOption.dataset.passwordDisplay || '').trim();", body)
        self.assertIn("cachedPasswordDisplay !== '未获取'", body)
        self.assertIn("passwordEl.textContent = (cachedPasswordDisplay && cachedPasswordDisplay !== '未获取')", body)
        self.assertLess(
            body.index("cachedPasswordDisplay !== '未获取'"),
            body.index("const details = await fetchImAccountDetails(requestedAccountId);"),
            "在线客服切账号时，'未获取' 这种占位不该被当成已知密码态直接展示，详情还没回来前应继续显示加载中",
        )

    def test_online_im_account_list_does_not_cache_unconfigured_username_placeholder_as_real_username(self):
        helper_body = _extract_function_body(self.app_js, "restoreImAccountListFailureOption")
        body = _extract_function_body(self.app_js, "loadImAccountList")

        self.assertIn("normalizedPreviousUsername !== '未配置'", helper_body)
        self.assertIn("normalizedPreviousUsername !== '未配置'", body)
        self.assertIn("normalizedPreviousUsernameDisplay !== '未配置'", body)
        dataset_assign_index = body.index("currentSelectedOption.dataset.username = (")
        self.assertLess(
            body.rfind("normalizedPreviousUsernameDisplay !== '未配置'", 0, dataset_assign_index),
            dataset_assign_index,
            "在线客服刷新账号列表时，'未配置' 这种占位别回填进下拉缓存，不然后面失败回退和复制逻辑都得被脏占位带偏",
        )

    def test_online_im_account_list_ignores_stale_async_list_responses(self):
        self.assertIn("let imAccountListRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadImAccountList")
        self.assertIn("const requestSequence = ++imAccountListRequestSequence;", body)
        self.assertIn("requestSequence !== imAccountListRequestSequence", body)
        self.assertIn("return;", body)

    def test_online_im_account_list_refresh_discards_old_inflight_detail_promises_before_reselecting_account(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")

        self.assertIn("imAccountDetailsInflight.clear();", body)
        self.assertLess(
            body.index("imAccountDetailsInflight.clear();"),
            body.index("const data = await fetchJSONWithoutGlobalLoading(`${apiBase}/accounts`, {"),
            "在线客服刷新账号列表前得先废掉旧的详情 in-flight promise，不然新一轮重选账号可能直接复用上一轮半路上的旧详情结果",
        )
        self.assertLess(
            body.index("imAccountDetailsInflight.clear();"),
            body.index("await onImAccountChange();"),
            "在线客服刷新后如果还要重选当前账号，得先清掉旧详情 promise，别让新会话拿旧请求结果回魂",
        )

    def test_online_im_account_list_preserves_current_selection_when_still_available(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")
        self.assertIn("const previousValue = select ? select.value : '';", body)
        self.assertIn("imAccountsData = (data || [])", body)
        self.assertIn("account_id: String(accountId || '').trim(),", body)
        self.assertIn("if (!accountId) {", body)
        self.assertIn("return;", body)
        self.assertIn("const hasPreviousOption = previousValue && Array.from(select.options).some(option => option.value === previousValue);", body)
        self.assertIn("if (hasPreviousOption) {", body)
        self.assertIn("select.value = previousValue;", body)
        self.assertIn("await onImAccountChange();", body)
        self.assertNotIn("option.value = getCookieDetailsAccountId(account);", body)
        self.assertNotIn("option.textContent = getCookieDetailsAccountId(account);", body)

    def test_online_im_account_list_clears_stale_display_when_previous_selection_disappears_after_refresh(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")

        self.assertIn("} else if (previousValue) {", body)
        self.assertIn("usernameEl.textContent = '-';", body)
        self.assertIn("passwordEl.textContent = '-';", body)
        self.assertLess(
            body.index("} else if (previousValue) {"),
            body.index("console.error('加载IM账号列表失败:', error);"),
            "在线客服刷新账号列表后如果当前选中账号已经不存在，就该立刻清掉旧展示，别继续挂着别的账号信息装当前态",
        )

    def test_online_im_account_list_surfaces_empty_state_when_no_valid_accounts_exist(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")
        self.assertIn("let appendedCount = 0;", body)
        self.assertIn("appendedCount += 1;", body)
        self.assertIn("if (appendedCount === 0 && select) {", body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 暂无账号，请先添加账号</option>';",
                      body)

    def test_online_im_account_list_clears_stale_display_when_refresh_finds_no_valid_accounts(self):
        body = _extract_function_body(self.app_js, "loadImAccountList")

        empty_state_index = body.index("if (appendedCount === 0 && select) {")
        self.assertIn("usernameEl.textContent = '-';", body)
        self.assertIn("passwordEl.textContent = '-';", body)
        self.assertLess(
            body.index("usernameEl.textContent = '-';", empty_state_index),
            body.index("return;", empty_state_index),
            "在线客服刷新后如果已经一个有效账号都不剩了，就别还挂着上个账号的用户名装岁月静好",
        )
        self.assertLess(
            body.index("passwordEl.textContent = '-';", empty_state_index),
            body.index("return;", empty_state_index),
            "在线客服刷新后如果已经没有有效账号，密码栏也该一起清干净，别继续拿旧掩码糊弄人",
        )

    def test_online_im_account_switch_ignores_stale_async_detail_responses(self):
        self.assertIn("let imAccountDetailsRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "onImAccountChange")
        self.assertIn("const requestSequence = ++imAccountDetailsRequestSequence;", body)
        self.assertIn("const requestedAccountId = selectedOption.value;", body)
        self.assertIn("requestSequence !== imAccountDetailsRequestSequence", body)
        self.assertIn("select.value !== requestedAccountId", body)
        self.assertIn("return;", body)

    def test_online_im_account_details_loader_uses_non_loading_json_helper(self):
        body = _extract_function_body(self.app_js, "fetchImAccountDetails")
        self.assertIn("const normalizedAccountId = String(accountId || '').trim();", body)
        self.assertIn("if (!normalizedAccountId) {", body)
        self.assertIn("if (imAccountDetailsInflight.has(normalizedAccountId)) {", body)
        self.assertIn("return await imAccountDetailsInflight.get(normalizedAccountId);", body)
        self.assertIn("const details = await fetchJSONWithoutGlobalLoading(", body)
        self.assertIn("`${apiBase}/accounts/${encodeURIComponent(normalizedAccountId)}/details?include_secrets=true&include_runtime_status=false`", body)
        self.assertIn("if (details == null) {", body)
        self.assertIn("if (!details || typeof details !== 'object' || Array.isArray(details)) {", body)
        self.assertIn("Object.prototype.hasOwnProperty.call(details, 'account_id')", body)
        self.assertIn("Object.prototype.hasOwnProperty.call(details, 'username')", body)
        self.assertIn("Object.prototype.hasOwnProperty.call(details, 'password')", body)
        self.assertIn("throw new Error('账号详情返回格式异常');", body)
        self.assertIn("return details;", body)
        self.assertIn("imAccountDetailsInflight.set(normalizedAccountId, requestPromise);", body)
        self.assertIn("imAccountDetailsInflight.delete(normalizedAccountId);", body)
        self.assertIn("suppressErrorToast: true", body)
        self.assertNotIn("const response = await fetch(", body)

    def test_online_im_account_details_loader_rejects_malformed_payloads_before_callers_treat_them_as_unconfigured(self):
        helper_body = _extract_function_body(self.app_js, "fetchImAccountDetails")
        detail_body = _extract_function_body(self.app_js, "onImAccountChange")
        copy_username_body = _extract_function_body(self.app_js, "copyImUsername")
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        self.assertIn("throw new Error('账号详情返回格式异常');", helper_body)
        self.assertIn("String(details.account_id || '').trim() !== normalizedAccountId", helper_body)
        self.assertIn("details.username != null", helper_body)
        self.assertIn("typeof details.username !== 'string'", helper_body)
        self.assertIn("details.password != null", helper_body)
        self.assertIn("typeof details.password !== 'string'", helper_body)
        self.assertLess(
            helper_body.index("throw new Error('账号详情返回格式异常');"),
            helper_body.index("return details;"),
            "在线客服详情 helper 识别到歪 payload 后得直接报格式异常，别把垃圾数据继续往调用方那边放行",
        )

        for body, anchor in (
            (detail_body, "const password = details.password || '';"),
            (copy_username_body, "const detailPassword = details.password || '';"),
            (copy_password_body, "password = details.password || '';"),
            (copy_account_info_body, "password = details.password || '';"),
        ):
            with self.subTest(anchor=anchor):
                self.assertLess(
                    body.index("const details = await fetchImAccountDetails(requestedAccountId);"),
                    body.index(anchor),
                    "调用方先走详情 helper，再处理密码字段，歪 payload 应由 helper 截住，别在这里被伪装成未配置",
                )

    def test_online_im_callers_treat_redirected_unauthorized_helper_responses_as_abort(self):
        load_list_body = _extract_function_body(self.app_js, "loadImAccountList")
        detail_body = _extract_function_body(self.app_js, "onImAccountChange")
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        self.assertIn("if (data == null) {", load_list_body)
        self.assertLess(
            load_list_body.index("if (data == null) {"),
            load_list_body.index("if (!Array.isArray(data)) {"),
            "helper 已经触发 401 跳转时，账号列表调用方得把空返回当成中止，别再反手报返回格式异常",
        )

        for body, anchor in (
            (detail_body, "const resolvedUsername = resolveImUsernameFromDetail(details, fallbackUsername);"),
            (copy_password_body, "password = details.password || '';"),
            (copy_account_info_body, "const resolvedUsername = resolveImUsernameFromDetail(details, optionUsername);"),
        ):
            with self.subTest(anchor=anchor):
                self.assertIn("if (!details) {", body)
                self.assertLess(
                    body.index("if (!details) {"),
                    body.index(anchor),
                    "helper 已经因为 401 中止时，详情调用方得先停下，别拿空详情继续解引用自爆",
                )

    def test_online_im_detail_username_helper_trusts_explicit_empty_payload(self):
        body = _extract_function_body(self.app_js, "resolveImUsernameFromDetail")

        self.assertIn("const safeDetails = details && typeof details === 'object' ? details : {};", body)
        self.assertIn("const detailUsername = String(safeDetails.username || '').trim();", body)
        self.assertIn("const detailUsernameProvided = Object.prototype.hasOwnProperty.call(safeDetails, 'username');", body)
        self.assertIn("return detailUsernameProvided ? detailUsername : String(fallbackUsername || '').trim();", body)
        self.assertNotIn("return detailUsername || String(fallbackUsername || '').trim();", body)
        self.assertLess(
            body.index("const detailUsernameProvided = Object.prototype.hasOwnProperty.call(safeDetails, 'username');"),
            body.index("return detailUsernameProvided ? detailUsername : String(fallbackUsername || '').trim();"),
            "在线客服详情里只要后端明确回了 username 字段，就该信最新值；哪怕它是空串，也别拿旧缓存硬盖回去",
        )

    def test_online_im_account_details_refresh_username_from_detail_payload(self):
        body = _extract_function_body(self.app_js, "onImAccountChange")

        self.assertIn("const currentSelectedOption = select.selectedOptions[0];", body)
        self.assertIn("const fallbackUsername = currentSelectedOption", body)
        self.assertIn("const resolvedUsername = resolveImUsernameFromDetail(details, fallbackUsername);", body)
        self.assertIn("currentSelectedOption.dataset.username = resolvedUsername;", body)
        self.assertIn("usernameEl.textContent = resolvedUsername || '未配置';", body)
        self.assertLess(
            body.index("const resolvedUsername = resolveImUsernameFromDetail(details, fallbackUsername);"),
            body.index("const password = details.password || '';"),
            "在线客服详情既然已经拿到了最新用户名，就该先把展示名和下拉缓存补正，再去更新密码掩码",
        )

    def test_online_im_account_switch_uses_loading_and_failure_placeholders_instead_of_premature_unconfigured_text(self):
        body = _extract_function_body(self.app_js, "onImAccountChange")

        self.assertIn("const cachedUsername = String(selectedOption.dataset.username || '').trim();", body)
        self.assertIn("if (usernameEl) usernameEl.textContent = cachedUsername || '加载中...';", body)
        self.assertIn("const currentSelectedOption = select.selectedOptions[0];", body)
        self.assertIn("const fallbackUsername = currentSelectedOption", body)
        self.assertIn("usernameEl.textContent = fallbackUsername || '获取失败';", body)
        self.assertNotIn("const username = selectedOption.dataset.username || '未配置';", body)
        self.assertNotIn("if (usernameEl) usernameEl.textContent = username;", body)
        self.assertLess(
            body.index("if (usernameEl) usernameEl.textContent = cachedUsername || '加载中...';"),
            body.index("const details = await fetchImAccountDetails(requestedAccountId);"),
            "在线客服切账号时，详情还没回来前应该先标成加载中，别一上来就误报未配置",
        )
        self.assertLess(
            body.index("usernameEl.textContent = fallbackUsername || '获取失败';"),
            body.index("showToast(`获取账号密码失败: ${error.message || '请稍后重试'}`, 'warning');"),
            "在线客服详情拉取失败时，也该先把账号栏修成失败态或旧缓存，再去弹 warning toast",
        )

    def test_online_im_requests_are_invalidated_when_leaving_section(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        self.assertIn("if (sectionName !== 'online-im') {", show_section_body)
        self.assertIn("imAccountListRequestSequence += 1;", show_section_body)
        self.assertIn("imAccountDetailsRequestSequence += 1;", show_section_body)
        self.assertIn("imAccountDetailsInflight.clear();", show_section_body)

    def test_online_im_copy_actions_are_invalidated_when_leaving_section(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("let imUsernameCopyActionRequestSequence = 0;", self.app_js)
        self.assertIn("let imPasswordCopyActionRequestSequence = 0;", self.app_js)
        self.assertIn("let imAccountInfoCopyActionRequestSequence = 0;", self.app_js)
        self.assertIn("imUsernameCopyActionRequestSequence += 1;", show_section_body)
        self.assertIn("imPasswordCopyActionRequestSequence += 1;", show_section_body)
        self.assertIn("imAccountInfoCopyActionRequestSequence += 1;", show_section_body)

    def test_online_im_loaders_do_not_update_hidden_section_or_emit_hidden_toasts(self):
        load_list_body = _extract_function_body(self.app_js, "loadImAccountList")
        detail_body = _extract_function_body(self.app_js, "onImAccountChange")

        self.assertIn("!document.getElementById('online-im-section')?.classList.contains('active')", load_list_body)
        self.assertIn("!document.getElementById('online-im-section')?.classList.contains('active')", detail_body)
        self.assertLess(
            load_list_body.index("!document.getElementById('online-im-section')?.classList.contains('active')"),
            load_list_body.index("showToast(`加载在线客服账号失败: ${error.message || '请稍后重试'}`, 'warning');"),
            "都切出在线客服了，旧账号列表失败请求就别再回来弹 warning 讨人嫌",
        )
        self.assertLess(
            detail_body.index("!document.getElementById('online-im-section')?.classList.contains('active')"),
            detail_body.index("showToast(`获取账号密码失败: ${error.message || '请稍后重试'}`, 'warning');"),
            "都离开在线客服了，旧详情请求失败也不该回来往当前页面脸上甩 toast",
        )

    def test_online_im_copy_actions_ignore_stale_account_switches(self):
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        self.assertIn("const requestedAccountId = select.value;", copy_password_body)
        self.assertIn("if (select.value !== requestedAccountId) {", copy_password_body)
        self.assertIn("showToast('账号已切换，请重新复制密码', 'warning');", copy_password_body)

        self.assertIn("const requestedAccountId = select.value;", copy_account_info_body)
        self.assertIn("if (select.value !== requestedAccountId) {", copy_account_info_body)
        self.assertIn("showToast('账号已切换，请重新复制账号密码', 'warning');", copy_account_info_body)
        self.assertGreaterEqual(copy_password_body.count("if (select.value !== requestedAccountId) {"), 2)
        self.assertGreaterEqual(copy_account_info_body.count("if (select.value !== requestedAccountId) {"), 2)
        self.assertLess(
            copy_password_body.find("if (select.value !== requestedAccountId) {", copy_password_body.index("await navigator.clipboard.writeText(password);")),
            copy_password_body.index("showToast('密码已复制', 'success');"),
            "复制密码时剪贴板写入都等完了，也得再验一次账号没切走，别给旧账号乱报成功",
        )
        self.assertLess(
            copy_account_info_body.find("if (select.value !== requestedAccountId) {", copy_account_info_body.index("await navigator.clipboard.writeText(copyText);")),
            copy_account_info_body.index("showToast('账号密码已复制到剪贴板', 'success');"),
            "复制账号密码时剪贴板写入都等完了，也得再验一次账号没切走，别让旧结果跨到新账号上",
        )

    def test_online_im_copy_account_info_prefers_fresh_detail_username(self):
        body = _extract_function_body(self.app_js, "copyImAccountInfo")

        self.assertIn("let username = selectedOption ? String(selectedOption.dataset.username || '').trim() : '';", body)
        self.assertIn("const currentSelectedOption = select.selectedOptions[0];", body)
        self.assertIn("const optionUsername = currentSelectedOption", body)
        self.assertIn("const resolvedUsername = resolveImUsernameFromDetail(details, optionUsername);", body)
        self.assertIn("currentSelectedOption.dataset.username = resolvedUsername;", body)
        self.assertIn("username = resolvedUsername;", body)
        self.assertIn("const copyText = `账号：${username || '未配置'}\\n密码：${password}`;", body)
        self.assertLess(
            body.index("const resolvedUsername = resolveImUsernameFromDetail(details, optionUsername);"),
            body.index("password = details.password || '';"),
            "复制账号密码时既然详情接口已经回了最新用户名，就该先用新用户名修正复制内容，再处理密码",
        )

    def test_online_im_copy_actions_rehydrate_display_from_fetched_detail_before_toasting(self):
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        self.assertIn("const usernameEl = document.getElementById('imDisplayUsername');", copy_password_body)
        self.assertIn("const passwordEl = document.getElementById('imDisplayPassword');", copy_password_body)
        self.assertIn("const currentSelectedOption = select.selectedOptions[0];", copy_password_body)
        self.assertIn("const resolvedUsername = resolveImUsernameFromDetail(details, fallbackUsername);", copy_password_body)
        self.assertIn("currentSelectedOption.dataset.username = resolvedUsername;", copy_password_body)
        self.assertIn("usernameEl.textContent = resolvedUsername || '未配置';", copy_password_body)
        self.assertIn("const passwordDisplay = password ? '••••••••' : '未配置';", copy_password_body)
        self.assertIn("currentSelectedOption.dataset.passwordDisplay = passwordDisplay;", copy_password_body)
        self.assertIn("passwordEl.textContent = passwordDisplay;", copy_password_body)
        self.assertLess(
            copy_password_body.index("passwordEl.textContent = passwordDisplay;"),
            copy_password_body.index("showToast('密码未配置', 'warning');"),
            "复制密码重新拿到详情后，应先把页面上的密码显示修正，再决定是否提示未配置",
        )
        self.assertLess(
            copy_password_body.index("usernameEl.textContent = resolvedUsername || '未配置';"),
            copy_password_body.index("showToast('密码已复制', 'success');"),
            "复制密码成功前也该把页面上的账号显示修正，别密码都复制好了账号栏还挂着旧值",
        )
        self.assertLess(
            copy_password_body.index("passwordEl.textContent = passwordDisplay;"),
            copy_password_body.index("showToast('密码已复制', 'success');"),
            "复制密码成功时也该先修正页面显示，别 toast 都弹完了页面还挂着旧的获取失败状态",
        )

        self.assertIn("const usernameEl = document.getElementById('imDisplayUsername');", copy_account_info_body)
        self.assertIn("const passwordEl = document.getElementById('imDisplayPassword');", copy_account_info_body)
        self.assertIn("usernameEl.textContent = username || '未配置';", copy_account_info_body)
        self.assertIn("const passwordDisplay = password ? '••••••••' : '未配置';", copy_account_info_body)
        self.assertIn("currentSelectedOption.dataset.passwordDisplay = passwordDisplay;", copy_account_info_body)
        self.assertIn("passwordEl.textContent = passwordDisplay;", copy_account_info_body)
        self.assertLess(
            copy_account_info_body.index("usernameEl.textContent = username || '未配置';"),
            copy_account_info_body.index("showToast('该账号未配置用户名和密码', 'warning');"),
            "复制账号密码重新拿到详情后，应先修正账号显示，再决定是否提示空凭证",
        )
        self.assertLess(
            copy_account_info_body.index("passwordEl.textContent = passwordDisplay;"),
            copy_account_info_body.index("showToast('账号密码已复制到剪贴板', 'success');"),
            "复制账号密码成功时也该先修正页面上的密码显示，别成功了 UI 还停在旧错误态",
        )

    def test_online_im_copy_failure_toasts_ignore_stale_account_switches(self):
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        self.assertLess(
            copy_password_body.find("if (select.value !== requestedAccountId) {", copy_password_body.index("} catch (error) {")),
            copy_password_body.index("showToast(`获取密码失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "旧账号的取密失败回调不该在已经切到新账号后还回来对着新账号页面甩旧错误",
        )
        self.assertLess(
            copy_account_info_body.find("if (select.value !== requestedAccountId) {", copy_account_info_body.index("} catch (error) {")),
            copy_account_info_body.index("showToast(`获取账号密码失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "旧账号的账号密码获取失败回调不该在已经切到新账号后还回来对着新账号页面甩旧错误",
        )

    def test_online_im_empty_credential_warnings_ignore_stale_account_switches(self):
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        password_warning_index = copy_password_body.index("showToast('密码未配置', 'warning');")
        password_post_fetch_guard_index = copy_password_body.rfind(
            "if (!document.getElementById('online-im-section')?.classList.contains('active')) {",
            0,
            password_warning_index,
        )
        password_switch_guard_index = copy_password_body.find(
            "if (select.value !== requestedAccountId) {",
            password_post_fetch_guard_index,
        )
        self.assertGreater(
            password_switch_guard_index,
            password_post_fetch_guard_index,
            "复制密码在取到旧账号结果后，空密码 warning 分支前必须再验一次账号没切走",
        )
        self.assertLess(
            password_switch_guard_index,
            password_warning_index,
            "旧账号的空密码 warning 不该在已经切到新账号后还回来对着新账号页面弹旧提示",
        )

        account_info_warning_index = copy_account_info_body.index("showToast('该账号未配置用户名和密码', 'warning');")
        account_info_post_fetch_guard_index = copy_account_info_body.rfind(
            "if (!document.getElementById('online-im-section')?.classList.contains('active')) {",
            0,
            account_info_warning_index,
        )
        account_info_switch_guard_index = copy_account_info_body.find(
            "if (select.value !== requestedAccountId) {",
            account_info_post_fetch_guard_index,
        )
        self.assertGreater(
            account_info_switch_guard_index,
            account_info_post_fetch_guard_index,
            "复制账号密码在取到旧账号结果后，空凭证 warning 分支前必须再验一次账号没切走",
        )
        self.assertLess(
            account_info_switch_guard_index,
            account_info_warning_index,
            "旧账号的空账号密码 warning 不该在已经切到新账号后还回来对着新账号页面弹旧提示",
        )

    def test_online_im_copy_actions_do_not_emit_toasts_after_leaving_section(self):
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        for body, toast_fragment in (
            (copy_password_body, "showToast(`获取密码失败: ${error.message || '请稍后重试'}`, 'danger');"),
            (copy_password_body, "showToast('密码已复制', 'success');"),
            (copy_account_info_body, "showToast(`获取账号密码失败: ${error.message || '请稍后重试'}`, 'danger');"),
            (copy_account_info_body, "showToast('账号密码已复制到剪贴板', 'success');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('online-im-section')?.classList.contains('active')", body)
                self.assertIn("return;", body)
                self.assertLess(
                    body.index("!document.getElementById('online-im-section')?.classList.contains('active')"),
                    body.index(toast_fragment),
                    "都切出在线客服页了，旧的复制账号密码结果不该再跨页弹 toast 刷存在感",
                )

    def test_online_im_copy_actions_ignore_older_same_page_responses(self):
        copy_username_body = _extract_function_body(self.app_js, "copyImUsername")
        copy_password_body = _extract_function_body(self.app_js, "copyImPassword")
        copy_account_info_body = _extract_function_body(self.app_js, "copyImAccountInfo")

        self.assertIn("const actionRequestSequence = ++imUsernameCopyActionRequestSequence;", copy_username_body)
        self.assertIn("const actionRequestSequence = ++imPasswordCopyActionRequestSequence;", copy_password_body)
        self.assertIn("const actionRequestSequence = ++imAccountInfoCopyActionRequestSequence;", copy_account_info_body)

        self.assertLess(
            copy_username_body.find(
                "if (actionRequestSequence !== imUsernameCopyActionRequestSequence) {",
                copy_username_body.index("await navigator.clipboard.writeText(username);"),
            ),
            copy_username_body.index("showToast('账号已复制', 'success');"),
            "同页连续复制账号时，旧的剪贴板成功回调别回头给当前页面乱报成功",
        )
        self.assertLess(
            copy_username_body.find(
                "if (actionRequestSequence !== imUsernameCopyActionRequestSequence) {",
                copy_username_body.index("} catch (error) {"),
            ),
            copy_username_body.index("fallbackCopy(username, '账号已复制', '复制失败');"),
            "同页连续复制账号时，旧的 fallbackCopy 失败分支别回魂污染当前交互",
        )

        self.assertLess(
            copy_password_body.find(
                "if (actionRequestSequence !== imPasswordCopyActionRequestSequence) {",
                copy_password_body.index("const details = await fetchImAccountDetails(requestedAccountId);"),
            ),
            copy_password_body.index("password = details.password || '';"),
            "同页连续复制密码时，旧详情响应不该晚回来后还继续推进当前复制流程",
        )
        self.assertLess(
            copy_password_body.find(
                "if (actionRequestSequence !== imPasswordCopyActionRequestSequence) {",
                copy_password_body.index("} catch (error) {"),
            ),
            copy_password_body.index("showToast(`获取密码失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "同页连续复制密码时，旧失败响应别回头给当前页面甩红字",
        )
        self.assertLess(
            copy_password_body.rfind(
                "if (actionRequestSequence !== imPasswordCopyActionRequestSequence) {",
                0,
                copy_password_body.index("showToast('密码未配置', 'warning');"),
            ),
            copy_password_body.index("showToast('密码未配置', 'warning');"),
            "同页连续复制密码时，旧空密码 warning 不该在新动作之后再回魂弹出",
        )
        self.assertLess(
            copy_password_body.find(
                "if (actionRequestSequence !== imPasswordCopyActionRequestSequence) {",
                copy_password_body.index("await navigator.clipboard.writeText(password);"),
            ),
            copy_password_body.index("showToast('密码已复制', 'success');"),
            "同页连续复制密码时，旧的剪贴板成功回调别回头给当前页面乱报成功",
        )

        self.assertLess(
            copy_account_info_body.find(
                "if (actionRequestSequence !== imAccountInfoCopyActionRequestSequence) {",
                copy_account_info_body.index("const details = await fetchImAccountDetails(requestedAccountId);"),
            ),
            copy_account_info_body.index("password = details.password || '';"),
            "同页连续复制账号密码时，旧详情响应不该晚回来后还继续推进当前复制流程",
        )
        self.assertLess(
            copy_account_info_body.find(
                "if (actionRequestSequence !== imAccountInfoCopyActionRequestSequence) {",
                copy_account_info_body.index("} catch (error) {"),
            ),
            copy_account_info_body.index("showToast(`获取账号密码失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "同页连续复制账号密码时，旧失败响应别回头给当前页面甩红字",
        )
        self.assertLess(
            copy_account_info_body.rfind(
                "if (actionRequestSequence !== imAccountInfoCopyActionRequestSequence) {",
                0,
                copy_account_info_body.index("showToast('该账号未配置用户名和密码', 'warning');"),
            ),
            copy_account_info_body.index("showToast('该账号未配置用户名和密码', 'warning');"),
            "同页连续复制账号密码时，旧空凭证 warning 不该在新动作之后再回魂弹出",
        )
        self.assertLess(
            copy_account_info_body.find(
                "if (actionRequestSequence !== imAccountInfoCopyActionRequestSequence) {",
                copy_account_info_body.index("await navigator.clipboard.writeText(copyText);"),
            ),
            copy_account_info_body.index("showToast('账号密码已复制到剪贴板', 'success');"),
            "同页连续复制账号密码时，旧的剪贴板成功回调别回头给当前页面乱报成功",
        )

    def test_online_im_copy_username_rechecks_selected_account_after_clipboard_write(self):
        body = _extract_function_body(self.app_js, "copyImUsername")
        self.assertIn("const select = document.getElementById('imAccountSelect');", body)
        self.assertIn("const requestedAccountId = select ? select.value : '';", body)
        self.assertIn("const details = await fetchImAccountDetails(requestedAccountId);", body)
        self.assertIn("showToast('账号已切换，请重新复制账号', 'warning');", body)
        self.assertLess(
            body.find("if (requestedAccountId && select?.value !== requestedAccountId) {", body.index("await navigator.clipboard.writeText(username);")),
            body.index("showToast('账号已复制', 'success');"),
            "复制账号时剪贴板写入都等完了，也得再验一次账号没切走，别给旧账号乱报成功",
        )
        self.assertLess(
            body.find("if (requestedAccountId && select?.value !== requestedAccountId) {", body.index("if (!document.getElementById('online-im-section')?.classList.contains('active')) {")),
            body.index("fallbackCopy(username, '账号已复制', '复制失败');"),
            "复制账号时如果切到新账号了，就别再拿旧账号去走 fallbackCopy 了",
        )

    def test_online_im_copy_username_rehydrates_missing_display_from_latest_detail_before_warning_or_success(self):
        body = _extract_function_body(self.app_js, "copyImUsername")

        self.assertIn("let username = usernameEl ? String(usernameEl.textContent || '').trim() : '';", body)
        self.assertIn("const passwordEl = document.getElementById('imDisplayPassword');", body)
        self.assertIn("if (requestedAccountId) {", body)
        self.assertIn("const details = await fetchImAccountDetails(requestedAccountId);", body)
        self.assertIn("const currentSelectedOption = select?.selectedOptions?.[0];", body)
        self.assertIn("const detailPassword = details.password || '';", body)
        self.assertIn("const resolvedUsername = resolveImUsernameFromDetail(details, fallbackUsername);", body)
        self.assertIn("username = resolvedUsername;", body)
        self.assertIn("usernameEl.textContent = resolvedUsername || '未配置';", body)
        self.assertIn("const passwordDisplay = detailPassword ? '••••••••' : '未配置';", body)
        self.assertIn("passwordEl.textContent = passwordDisplay;", body)
        self.assertIn("showToast(`获取账号失败: ${error.message || '请稍后重试'}`, 'warning');", body)
        self.assertLess(
            body.index("const details = await fetchImAccountDetails(requestedAccountId);"),
            body.index("showToast('账号未配置', 'warning');"),
            "复制账号时应先拉一遍最新详情，再决定要不要提示账号未配置，别拿旧显示直接下结论",
        )
        self.assertLess(
            body.index("passwordEl.textContent = passwordDisplay;"),
            body.index("showToast('账号已复制', 'success');"),
            "复制账号成功前也该顺手把密码显示修成最新状态，别账号栏恢复了密码栏还挂着旧错误态",
        )
        self.assertLess(
            body.index("usernameEl.textContent = resolvedUsername || '未配置';"),
            body.index("showToast('账号已复制', 'success');"),
            "复制账号成功前应先把页面上的账号显示修成最新值，别 toast 都弹完了 UI 还挂着旧占位",
        )

    def test_online_im_copy_username_falls_back_to_current_visible_value_when_detail_refresh_fails(self):
        body = _extract_function_body(self.app_js, "copyImUsername")

        self.assertIn("console.error('获取账号失败:', error);", body)
        self.assertIn("if (!username || username === '未配置' || username === '-' || username === '获取失败' || username === '加载中...') {", body)
        self.assertIn("showToast(`获取账号失败: ${error.message || '请稍后重试'}`, 'warning');", body)
        catch_index = body.index("} catch (error) {")
        catch_warning_index = body.index("showToast(`获取账号失败: ${error.message || '请稍后重试'}`, 'warning');", catch_index)
        self.assertLess(
            body.index("if (!username || username === '未配置' || username === '-' || username === '获取失败' || username === '加载中...') {", catch_index),
            catch_warning_index,
            "复制账号在详情刷新失败时，只有当前页面上也没有可用用户名时才该直接 warning 终止，别有现成显示值还硬拦用户",
        )
        self.assertLess(
            catch_warning_index,
            body.index("showToast('账号未配置', 'warning');"),
            "详情刷新失败 warning 只该兜底空账号场景；已有当前显示值时，后面应继续走复制逻辑",
        )

    def test_online_im_copy_username_never_treats_loading_or_error_placeholders_as_copyable_values(self):
        body = _extract_function_body(self.app_js, "copyImUsername")

        self.assertIn("username === '获取失败'", body)
        self.assertIn("username === '加载中...'", body)
        self.assertLess(
            body.find("username === '获取失败'"),
            body.index("showToast(`获取账号失败: ${error.message || '请稍后重试'}`, 'warning');"),
            "复制账号在详情刷新失败后，'获取失败' 这种错误占位不该被当成可复制值继续放行",
        )
        self.assertLess(
            body.rfind("username === '加载中...'", 0, body.index("showToast('账号未配置', 'warning');")),
            body.index("showToast('账号未配置', 'warning');"),
            "复制账号在最终兜底分支里也不该把 '加载中...' 当成真实账号名跳过去",
        )

    def test_online_im_fallback_copy_only_reports_success_when_execcommand_returns_true(self):
        body = _extract_function_body(self.app_js, "fallbackCopy")

        self.assertIn("const copied = document.execCommand('copy');", body)
        self.assertIn("if (copied) {", body)
        self.assertIn("showToast(successMsg, 'success');", body)
        self.assertIn("showToast(failMsg, 'danger');", body)
        self.assertNotIn("document.execCommand('copy');\n        showToast(successMsg, 'success');", body)

    def test_online_im_new_window_prefers_current_iframe_url_and_falls_back_to_shared_target(self):
        self.assertIn("function getOnlineImTargetUrl() {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "getOnlineImTargetUrl")
        refresh_body = _extract_function_body(self.app_js, "refreshImIframe")
        open_body = _extract_function_body(self.app_js, "openGoofishImNewWindow")
        load_body = _extract_function_body(self.app_js, "loadOnlineIm")

        self.assertIn("const iframe = document.getElementById('goofishImIframe');", helper_body)
        self.assertIn("const configuredUrl = iframe ? String(iframe.dataset.src || '').trim() : '';", helper_body)
        self.assertIn("const normalizedConfiguredUrl = normalizeSafeHttpUrl(configuredUrl);", helper_body)
        self.assertIn("return normalizedConfiguredUrl || 'https://www.goofish.com/im';", helper_body)
        self.assertNotIn("return configuredUrl || 'https://www.goofish.com/im';", helper_body)

        self.assertIn("const iframe = document.getElementById('goofishImIframe');", open_body)
        self.assertIn("const currentSrc = String(iframe?.src || '').trim();", open_body)
        self.assertIn("const normalizedCurrentSrc = normalizeSafeHttpUrl(currentSrc);", open_body)
        self.assertIn("const popup = window.open(normalizedCurrentSrc || getOnlineImTargetUrl(), '_blank', 'noopener,noreferrer');", open_body)
        self.assertIn("showToast('新窗口打开失败，请检查浏览器拦截设置', 'warning');", open_body)

        self.assertIn("const currentSrc = String(iframe?.src || '').trim();", load_body)
        self.assertIn("const normalizedCurrentSrc = normalizeSafeHttpUrl(currentSrc);", load_body)
        self.assertIn("iframe.src = getOnlineImTargetUrl();", load_body)
        self.assertIn("if (iframe && !normalizedCurrentSrc) {", load_body)
        self.assertNotIn("if (iframe && iframe.src === 'about:blank') {", load_body)
        self.assertIn("const normalizedCurrentSrc = normalizeSafeHttpUrl(currentSrc);", refresh_body)
        self.assertIn("iframe.src = normalizedCurrentSrc || getOnlineImTargetUrl();", refresh_body)
        self.assertNotIn("iframe.src = currentSrc && currentSrc !== 'about:blank' ? currentSrc : getOnlineImTargetUrl();", refresh_body)

    def test_item_search_stops_before_network_requests_when_no_account_is_selected(self):
        self.assertIn('data-menu-id="item-search"', self.index_html)
        self.assertIn("showSection('item-search')", self.index_html)

        body = _extract_function_body(self.app_js, "handleItemSearch")
        self.assertIn("const accountId = getItemSearchAccountId();", body)
        self.assertIn("if (!accountId) {", body)
        self.assertIn("hideSearchResults();", body)
        self.assertIn("showSearchStatus(false);", body)
        self.assertIn("showSearchStatus(true);", body)
        self.assertLess(
            body.index("if (!accountId) {"),
            body.index("showSearchStatus(true);"),
            "没选账号时应该先停下，别傻乎乎继续发搜索请求",
        )
        self.assertLess(
            body.index("hideSearchResults();"),
            body.index("showSearchStatus(true);"),
            "没选账号就别继续挂着上次的搜索结果装当前态，先把旧结果收起来",
        )
        no_account_index = body.index("if (!accountId) {")
        no_account_hide_status_index = body.index("showSearchStatus(false);", no_account_index)
        self.assertLess(
            no_account_hide_status_index,
            body.index("return;", no_account_index),
            "没选账号时也得顺手把旧搜索状态收掉，别让上一轮 spinner 继续挂着装忙",
        )

    def test_item_search_uses_dedicated_account_selector_and_initializes_options_on_section_open(self):
        self.assertIn('id="itemSearchAccountFilter"', self.index_html)

        get_account_body = _extract_function_body(self.app_js, "getItemSearchAccountId")
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("const select = document.getElementById('itemSearchAccountFilter');", get_account_body)
        self.assertIn("const accountId = select?.value || '';", get_account_body)
        self.assertNotIn("document.getElementById('itemAccountFilter')?.value", get_account_body)
        self.assertNotIn("document.getElementById('itemReplayAccountFilter')?.value", get_account_body)
        self.assertIn("const options = Array.from(select?.options || []);", get_account_body)
        self.assertIn("const hasSelectableAccount = options.some(option => option.value);", get_account_body)
        self.assertIn("options.length <= 1", get_account_body)
        self.assertIn("showToast('搜索账号列表加载中，请稍候', 'info');", get_account_body)
        self.assertIn("options.some(option => String(option.textContent || '').includes('加载失败'))", get_account_body)
        self.assertIn("showToast('搜索账号列表加载失败，请稍后重试', 'danger');", get_account_body)
        self.assertIn("showToast('暂无可用账号，请先添加账号', 'warning');", get_account_body)
        self.assertIn("showToast('请先选择账号', 'warning');", get_account_body)

        self.assertIn("case 'item-search':", show_section_body)
        self.assertIn("loadAccountOptions('itemSearchAccountFilter', '请选择账号');", show_section_body)

    def test_item_search_empty_keyword_submission_hides_stale_results_before_warning(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")
        keyword_warning = "showToast('请输入搜索关键词', 'warning');"
        keyword_branch_index = body.index("if (!keyword) {")
        branch_hide_index = body.find("hideSearchResults();", keyword_branch_index)
        branch_hide_status_index = body.find("showSearchStatus(false);", keyword_branch_index)

        self.assertIn(keyword_warning, body)
        self.assertGreater(
            branch_hide_index,
            keyword_branch_index,
            "空关键词分支里应该自己收起旧结果，别拿前面别的分支的 hideSearchResults 糊弄测试",
        )
        self.assertLess(
            branch_hide_index,
            body.index(keyword_warning),
            "搜索关键词为空时也该先把旧结果收起来，别让上一轮结果继续挂着装当前态",
        )
        self.assertGreater(
            branch_hide_status_index,
            keyword_branch_index,
            "空关键词分支里应该自己收起旧搜索状态，别靠别的分支碰运气",
        )
        self.assertLess(
            branch_hide_status_index,
            body.index(keyword_warning),
            "搜索关键词为空时也该先把旧搜索状态收掉，别让上一轮 spinner 继续挂着装忙",
        )

    def test_item_search_request_sequence_starts_only_after_validation(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")
        request_sequence = "const requestSequence = ++itemSearchRequestSequence;"

        self.assertIn(request_sequence, body)
        self.assertLess(
            body.index("if (!accountId) {"),
            body.index(request_sequence),
            "账号都没选就别先递增 requestSequence，别拿一次无效提交把正在跑的正常搜索顶成 stale",
        )
        self.assertLess(
            body.index("if (!keyword) {"),
            body.index(request_sequence),
            "关键词都没填就别先开新搜索序号，别让空提交把上一轮有效搜索结果搅黄了",
        )
        self.assertLess(
            body.index("if (!Number.isInteger(totalPages) || totalPages < 1 || totalPages > 20) {"),
            body.index(request_sequence),
            "查询页数都不合法了还先递增 requestSequence，这不是防并发，是自己给自己添堵",
        )
        self.assertLess(
            body.index(request_sequence),
            body.index("showSearchStatus(true);"),
            "前端校验都过完了再启动 requestSequence，别一上来就瞎抢序号",
        )

    def test_item_search_rejects_total_pages_outside_documented_range_before_network_requests(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")
        pages_warning = "showToast('查询总页数需在 1 到 20 之间', 'warning');"
        pages_branch_index = body.index("if (!Number.isInteger(totalPages) || totalPages < 1 || totalPages > 20) {")
        branch_hide_index = body.find("hideSearchResults();", pages_branch_index)
        branch_hide_status_index = body.find("showSearchStatus(false);", pages_branch_index)

        self.assertIn(pages_warning, body)
        self.assertGreater(
            branch_hide_index,
            pages_branch_index,
            "非法查询页数分支里应该自己收起旧结果，别拿前面别的分支糊弄过去",
        )
        self.assertLess(
            branch_hide_index,
            body.index(pages_warning),
            "查询总页数超出 1-20 时应先收起旧结果，别让上一轮结果继续挂着装当前态",
        )
        self.assertGreater(
            branch_hide_status_index,
            pages_branch_index,
            "非法查询页数分支里应该自己收起旧搜索状态，别让旧 spinner 继续装忙",
        )
        self.assertLess(
            branch_hide_status_index,
            body.index(pages_warning),
            "查询总页数超出 1-20 时应先收掉旧搜索状态，再提示用户",
        )
        self.assertLess(
            pages_branch_index,
            body.index("showSearchStatus(true);"),
            "查询总页数都越界了，就别继续往后发搜索请求装正常",
        )

    def test_item_search_ignores_stale_async_responses_and_hidden_section_updates(self):
        self.assertIn("let itemSearchRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "handleItemSearch")

        self.assertIn("if (sectionName !== 'item-search') {", show_section_body)
        self.assertIn("itemSearchRequestSequence += 1;", show_section_body)
        self.assertIn("stopCaptchaSessionMonitor();", show_section_body)
        self.assertIn("const captchaVerifyModalElement = document.getElementById('captchaVerifyModal');", show_section_body)
        self.assertIn("captchaVerifyModal.hide();", show_section_body)
        self.assertIn("showSearchStatus(false);", show_section_body)

        self.assertIn("const requestSequence = ++itemSearchRequestSequence;", body)
        self.assertIn("requestSequence !== itemSearchRequestSequence", body)
        self.assertIn("!document.getElementById('item-search-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== itemSearchRequestSequence"),
            body.index("searchResultsData = data.data || [];"),
            "旧的商品搜索响应不该晚回来后把当前搜索结果覆盖掉",
        )
        self.assertLess(
            body.rfind("!document.getElementById('item-search-section')?.classList.contains('active')", 0, body.index("showToast(`搜索商品失败: ${error.message || '请稍后重试'}`, 'danger');")),
            body.index("showToast(`搜索商品失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "都离开商品搜索页了，旧搜索失败结果就别跨页弹 danger toast 了",
        )

        session_checker_index = body.index("sessionChecker = setInterval(async () => {")
        captcha_modal_index = body.index("showCaptchaVerificationModal(session.session_id);")
        self.assertLess(
            body.find("requestSequence !== itemSearchRequestSequence", session_checker_index),
            captcha_modal_index,
            "旧搜索留下的会话检查器不该在新搜索开始后还继续弹滑块验证框",
        )
        self.assertLess(
            body.rfind("!document.getElementById('item-search-section')?.classList.contains('active')", session_checker_index, body.index("showToast('🎨 检测到滑块验证，请完成验证', 'warning');")),
            body.index("showToast('🎨 检测到滑块验证，请完成验证', 'warning');"),
            "都切出商品搜索页了，旧会话检查器就别再跨页弹滑块提示了",
        )

    def test_item_search_session_checker_ignores_sessions_that_already_existed_before_current_search(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")

        self.assertIn("const existingCaptchaSessionIds = new Set();", body)
        self.assertIn("const initialCaptchaSessionsResponse = await fetch('/api/captcha/sessions');", body)
        self.assertIn("if (!session?.session_id || existingCaptchaSessionIds.has(session.session_id)) {", body)
        self.assertIn("existingCaptchaSessionIds.add(session.session_id);", body)

        self.assertLess(
            body.index("const initialCaptchaSessionsResponse = await fetch('/api/captcha/sessions');"),
            body.index("const fetchPromise = fetch('/items/search_multiple', {"),
            "当前搜索发请求前得先记住已有验证码会话，别把历史遗留会话误认成本轮新会话",
        )

        session_checker_index = body.index("sessionChecker = setInterval(async () => {")
        self.assertLess(
            body.index("if (!session?.session_id || existingCaptchaSessionIds.has(session.session_id)) {", session_checker_index),
            body.index("showCaptchaVerificationModal(session.session_id);"),
            "旧验证码会话都已经在搜索开始前存在了，就别让本轮商品搜索把它们又弹出来吓人",
        )
        self.assertLess(
            body.index("existingCaptchaSessionIds.add(session.session_id);", session_checker_index),
            body.index("showCaptchaVerificationModal(session.session_id);"),
            "本轮刚识别到的新验证码会话得先记账，别后面轮询一圈又把同一个会话重复弹窗",
        )

    def test_item_search_captcha_session_fetches_handle_unauthorized_before_consuming_responses(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")

        self.assertIn("if (handleUnauthorizedApiResponse(initialCaptchaSessionsResponse)) {", body)
        self.assertIn("if (handleUnauthorizedApiResponse(checkResponse)) {", body)

        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(initialCaptchaSessionsResponse)) {"),
            body.index("if (initialCaptchaSessionsResponse.ok) {"),
            "预取验证码会话时先兜住 401，别登录态都没了还装没事继续摸数据",
        )

        session_checker_index = body.index("sessionChecker = setInterval(async () => {")
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(checkResponse)) {", session_checker_index),
            body.index("const checkData = await checkResponse.json();", session_checker_index),
            "轮询验证码会话时也得先处理 401，别上来就 json() 把后端真实响应吞了",
        )

    def test_switching_away_from_item_search_stops_captcha_monitor_and_closes_modal(self):
        body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("if (sectionName !== 'item-search') {", body)
        self.assertIn("stopCaptchaSessionMonitor();", body)
        self.assertIn("const captchaVerifyModalElement = document.getElementById('captchaVerifyModal');", body)
        self.assertIn("const captchaVerifyModal = captchaVerifyModalElement", body)
        self.assertIn("captchaVerifyModal.hide();", body)
        self.assertLess(
            body.index("stopCaptchaSessionMonitor();"),
            body.index("showSearchStatus(false);"),
            "切出商品搜索页时先把滑块监控和弹窗收口，再去清搜索状态，别让旧验证跨页跳脸",
        )

    def test_captcha_monitor_queues_new_sessions_instead_of_silently_dropping_them(self):
        self.assertIn("let activeCaptchaSessionId = '';", self.app_js)
        self.assertIn("let queuedCaptchaSessionIds = [];", self.app_js)

        monitor_body = _extract_function_body(self.app_js, "startCaptchaSessionMonitor")
        show_modal_body = _extract_function_body(self.app_js, "showCaptchaVerificationModal")
        stop_body = _extract_function_body(self.app_js, "stopCaptchaSessionMonitor")

        self.assertIn("if (activeCaptchaModal && activeCaptchaSessionId && activeCaptchaSessionId !== session.session_id) {", monitor_body)
        self.assertIn("enqueueCaptchaSession(session.session_id);", monitor_body)
        self.assertIn("removeQueuedCaptchaSession(session.session_id);", monitor_body)
        self.assertIn("queuedCaptchaSessionIds = [];", stop_body)
        self.assertIn("if (activeCaptchaSessionId && activeCaptchaSessionId !== sessionId) {", show_modal_body)
        self.assertIn("showNextQueuedCaptchaSession();", show_modal_body)
        self.assertLess(
            show_modal_body.index("removeQueuedCaptchaSession(sessionId);"),
            show_modal_body.index("modal.show();"),
            "轮到当前滑块会话真正展示前，得先把它从排队队列里摘掉，别后面又被自己重复弹一次",
        )

    def test_check_captcha_completion_distinguishes_completed_timeout_and_user_cancel_hides(self):
        check_body = _extract_function_body(self.app_js, "checkCaptchaCompletion")
        show_modal_body = _extract_function_body(self.app_js, "showCaptchaVerificationModal")
        auto_monitor_body = _extract_function_body(self.app_js, "startCheckCaptchaCompletion")

        self.assertIn("const modalElement = document.getElementById('captchaVerifyModal');", check_body)
        self.assertIn("const handleHidden = () => {", check_body)
        self.assertIn("if (modalElement?.dataset.captchaSessionId !== sessionId || settled) {", check_body)
        self.assertIn("const closeReason = modalElement?.dataset.captchaCloseReason || '';", check_body)
        self.assertIn("if (closeReason === 'completed') {", check_body)
        self.assertIn("resolve(true);", check_body)
        self.assertIn("if (closeReason === 'timeout') {", check_body)
        self.assertIn("reject(new Error('验证超时'));", check_body)
        self.assertIn("reject(new Error('验证已取消'));", check_body)
        self.assertIn("modalElement.removeEventListener('hidden.bs.modal', handleHidden);", check_body)
        self.assertIn("if (data.completed || (data.session_exists === false && data.success)) {", check_body)
        self.assertLess(
            check_body.index("if (modalElement?.dataset.captchaSessionId !== sessionId || settled) {"),
            check_body.index("reject(new Error('验证已取消'));"),
            "别的滑块会话先关掉时，当前 Promise 不该被误杀；只有同一会话的弹窗真关了才该判取消",
        )
        self.assertLess(
            check_body.index("if (closeReason === 'completed') {"),
            check_body.index("reject(new Error('验证已取消'));"),
            "系统已经确认同一会话验证成功时，Promise 应直接 resolve，别反手当成用户取消",
        )
        self.assertLess(
            check_body.index("if (closeReason === 'timeout') {"),
            check_body.index("reject(new Error('验证已取消'));"),
            "验证超时得保留 timeout 语义，别和用户手动取消混成一锅粥",
        )

        self.assertGreaterEqual(
            show_modal_body.count("modalElement.dataset.captchaCloseReason = '';"),
            2,
            "滑块弹窗打开和收尾时都得重置 close reason，不然上一轮状态会串到下一轮",
        )
        self.assertLess(
            show_modal_body.index("modalElement.dataset.captchaCloseReason = '';"),
            show_modal_body.index("modal.show();"),
            "新弹窗展示前得先把 close reason 清干净，别拿上一轮的完成态糊当前会话",
        )
        self.assertIn("modalElement.dataset.captchaCloseReason = 'completed';", auto_monitor_body)
        self.assertIn("modalElement.dataset.captchaCloseReason = 'timeout';", auto_monitor_body)
        self.assertLess(
            auto_monitor_body.index("modalElement.dataset.captchaCloseReason = 'completed';"),
            auto_monitor_body.index("modal.hide();"),
            "自动监控确认验证成功时，得先标记 completed 再关弹窗，不然 Promise 收到的只有一嘴取消",
        )

    def test_item_search_treats_account_precheck_http_failures_as_real_failures(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")

        self.assertIn("if (handleUnauthorizedApiResponse(accountsCheckResponse)) {", body)
        self.assertIn("if (!accountsCheckResponse.ok) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(accountsCheckResponse, `HTTP ${accountsCheckResponse.status}`);", body)
        self.assertIn("showToast(`搜索前检查账号状态失败: ${errorMessage || '请稍后重试'}`, 'danger');", body)
        self.assertIn("showSearchStatus(false);", body)
        self.assertIn("showNoSearchResults();", body)
        precheck_error_index = body.index("const errorMessage = await readResponseErrorMessage(accountsCheckResponse, `HTTP ${accountsCheckResponse.status}`);")
        precheck_stale_index = body.index("requestSequence !== itemSearchRequestSequence", precheck_error_index)
        precheck_toast_index = body.index("showToast(`搜索前检查账号状态失败: ${errorMessage || '请稍后重试'}`, 'danger');", precheck_error_index)
        self.assertLess(
            precheck_error_index,
            precheck_stale_index,
            "账号预检查 HTTP 挂了时，先把后端错误体读出来，别消息还没看见就让 stale guard 把线索掐死",
        )
        self.assertLess(
            precheck_stale_index,
            precheck_toast_index,
            "账号预检查错误体读完后还得再验一次请求是否过期，别旧错误跨页乱弹",
        )
        self.assertLess(
            body.index("if (!accountsCheckResponse.ok) {"),
            body.index("const token = localStorage.getItem('auth_token');"),
            "账号预检查接口都 HTTP 挂了，就别继续往下发商品搜索请求装正常",
        )

    def test_item_search_http_failures_parse_backend_error_before_stale_guard(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("showToast(`搜索失败: ${errorMessage || '未知错误'}`, 'danger');", body)

        response_error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        response_stale_index = body.index("requestSequence !== itemSearchRequestSequence", response_error_index)
        response_toast_index = body.index("showToast(`搜索失败: ${errorMessage || '未知错误'}`, 'danger');", response_error_index)
        self.assertLess(
            response_error_index,
            response_stale_index,
            "商品搜索主请求 HTTP 失败时先把错误体读干净，别还没拿到 detail 就让 stale guard 把锅端走",
        )
        self.assertLess(
            response_stale_index,
            response_toast_index,
            "商品搜索主请求错误体读完后得先复验请求活性，再决定要不要弹错误 toast",
        )

    def test_item_search_retry_http_failures_parse_backend_error_before_stale_guard(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")

        self.assertIn("if (handleUnauthorizedApiResponse(retryResponse)) {", body)
        self.assertIn("const retryErrorMessage = await readResponseErrorMessage(retryResponse, `HTTP ${retryResponse.status}`);", body)
        self.assertIn("showToast(`验证后搜索失败: ${retryErrorMessage || '未知错误'}`, 'danger');", body)

        retry_error_index = body.index("const retryErrorMessage = await readResponseErrorMessage(retryResponse, `HTTP ${retryResponse.status}`);")
        retry_stale_index = body.index("requestSequence !== itemSearchRequestSequence", retry_error_index)
        retry_toast_index = body.index("showToast(`验证后搜索失败: ${retryErrorMessage || '未知错误'}`, 'danger');", retry_error_index)
        self.assertLess(
            retry_error_index,
            retry_stale_index,
            "验证后重试搜索挂了时也先把错误体读出来，别后端明明说人话你前端还装聋",
        )
        self.assertLess(
            retry_stale_index,
            retry_toast_index,
            "验证后重试的错误体读完后也得先验请求是不是旧的，别新搜索都开了旧失败还回来吓人",
        )

    def test_item_search_captcha_retry_zero_results_follow_normal_empty_state_contract(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")
        retry_data_index = body.index("const retryData = await retryResponse.json();")
        retry_branch_end = body.index("} catch (error) {", retry_data_index)
        retry_branch = body[retry_data_index:retry_branch_end]

        self.assertIn("if (searchResultsData.length > 0) {", retry_branch)
        self.assertIn("displaySearchResults();", retry_branch)
        self.assertIn("updateSearchStats(retryData);", retry_branch)
        self.assertIn("showNoSearchResults();", retry_branch)

        positive_branch_index = retry_branch.index("if (searchResultsData.length > 0) {")
        display_index = retry_branch.index("displaySearchResults();", positive_branch_index)
        stats_index = retry_branch.index("updateSearchStats(retryData);", display_index)
        empty_state_index = retry_branch.index("showNoSearchResults();", positive_branch_index)

        self.assertLess(
            positive_branch_index,
            display_index,
            "验证码后重试拿到结果时，先走结果渲染分支，别把空态和结果态搅成一锅",
        )
        self.assertLess(
            display_index,
            stats_index,
            "验证码后重试有结果时，先把卡片渲出来，再补统计，顺序别乱套",
        )
        self.assertLess(
            stats_index,
            empty_state_index,
            "验证码后重试结果为空时应该单独走空态，别前面刚 showNoSearchResults 后面又把统计卡片亮出来",
        )

    def test_item_search_second_captcha_prompt_after_retry_falls_back_to_empty_state(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")
        second_captcha_index = body.index("if (retryData.need_captcha || retryData.status === 'need_verification') {")
        second_captcha_toast = body.index("showToast('验证后仍需要滑块，请联系管理员', 'danger');", second_captcha_index)
        second_captcha_empty_state = body.index("showNoSearchResults();", second_captcha_toast)

        self.assertLess(
            body.index("showSearchStatus(false);", second_captcha_index),
            second_captcha_toast,
            "验证后又被后端要求滑块时，先把搜索状态收掉，别 spinner 还挂着装忙",
        )
        self.assertLess(
            second_captcha_toast,
            second_captcha_empty_state,
            "验证后重试还要求滑块时，不能只甩个 toast 就跑，页面至少得落到空态，别留一片空白恶心人",
        )

    def test_item_search_captcha_retry_failures_surface_runtime_error_messages(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")
        self.assertNotIn("showToast('滑块验证失败或超时', 'danger');", body)
        self.assertIn("showToast(`滑块验证失败或超时: ${error.message || '请稍后重试'}`, 'danger');", body)
        toast_index = body.index("showToast(`滑块验证失败或超时: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            body.rfind("requestSequence !== itemSearchRequestSequence", 0, toast_index),
            toast_index,
            "滑块验证这类旧异常也得先验搜索请求是不是还活着，别换了一轮搜索还回来甩红字",
        )
        self.assertLess(
            body.rfind("!document.getElementById('item-search-section')?.classList.contains('active')", 0, toast_index),
            toast_index,
            "都切出商品搜索页了，旧滑块异常就别跨页弹 danger toast 了",
        )
        self.assertLess(
            body.rfind("showSearchStatus(false);", 0, toast_index),
            toast_index,
            "滑块验证失败时先把搜索状态收掉，再弹错误提示，别 spinner 还杵那儿装忙",
        )
        self.assertLess(
            toast_index,
            body.find("showNoSearchResults();", toast_index),
            "滑块验证失败提示完了得落到无结果空态，别让页面停在半吊子状态",
        )

    def test_item_search_no_valid_account_branch_surfaces_empty_state(self):
        body = _extract_function_body(self.app_js, "handleItemSearch")
        invalid_account_toast = "showToast('搜索失败：系统中不存在有效的账户信息。请先在账号管理中添加有效的闲鱼账户。', 'warning');"

        self.assertIn(invalid_account_toast, body)
        no_valid_accounts_index = body.index("if (!accountsData.hasValidAccounts) {")
        branch_no_results_index = body.index("showNoSearchResults();", no_valid_accounts_index)
        self.assertLess(
            no_valid_accounts_index,
            branch_no_results_index,
            "没有效账号的分支里应该明确显示无结果/空态，别让用户对着一片空白发呆",
        )
        self.assertLess(
            body.index(invalid_account_toast),
            branch_no_results_index,
            "商品搜索发现系统里压根没有效账号时，不能只弹个 toast 就跑，得把空态区域也落出来",
        )

    def test_item_search_pagination_links_prevent_hash_navigation_jump(self):
        update_body = _extract_function_body(self.app_js, "updateSearchPagination")
        page_numbers_body = _extract_function_body(self.app_js, "generateSearchPageNumbers")

        self.assertIn('onclick="changeSearchPage(${currentSearchPage - 1}); return false;"', update_body)
        self.assertIn('onclick="changeSearchPage(${currentSearchPage + 1}); return false;"', update_body)
        self.assertIn('onclick="changeSearchPage(${i}); return false;"', page_numbers_body)
        self.assertNotIn('onclick="changeSearchPage(${currentSearchPage - 1})"', update_body)
        self.assertNotIn('onclick="changeSearchPage(${currentSearchPage + 1})"', update_body)
        self.assertNotIn('onclick="changeSearchPage(${i})"', page_numbers_body)

    def test_account_management_remarks_escape_html_and_inline_js_payloads(self):
        load_accounts_body = _extract_function_body(self.app_js, "loadAccounts")
        edit_remark_body = _extract_function_body(self.app_js, "editRemark")

        self.assertIn("const safeRemarkForJs = escapeInlineJsSingleQuotedString(cookie.remark || '');", load_accounts_body)
        self.assertIn("const safeRemarkHtml = escapeHtml(cookie.remark || '');", load_accounts_body)
        self.assertNotIn("${cookie.remark || '<i class=\"bi bi-plus-circle text-muted\"></i> 添加备注'}", load_accounts_body)
        self.assertNotIn(".replace(/'/g, '&#39;')", load_accounts_body)
        self.assertIn("const safeRemarkForJs = escapeInlineJsSingleQuotedString(newRemark);", edit_remark_body)
        self.assertIn("const safeRemarkHtml = escapeHtml(newRemark);", edit_remark_body)
        self.assertNotIn("newRemark.replace(/'/g, '&#39;')", edit_remark_body)

    def test_copy_cookie_does_not_emit_cross_page_toasts_after_leaving_accounts(self):
        body = _extract_function_body(self.app_js, "copyCookie")
        self.assertIn("suppressErrorToast: true", body)

        for toast_fragment in (
            "showToast(`账号 \"${id}\" 的Cookie已复制到剪贴板`, 'success');",
            "showToast('复制失败，请手动复制', 'error');",
            "showToast(`获取Cookie详情失败: ${error.message || '请稍后重试'}`, 'danger');",
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    "都切出账号页了，旧的复制 Cookie 结果就别再跨页刷 toast 了",
                )

    def test_copy_cookie_catch_failures_surface_runtime_error_messages(self):
        body = _extract_function_body(self.app_js, "copyCookie")

        self.assertNotIn("showToast('获取Cookie详情失败，请稍后重试', 'danger');", body)
        self.assertIn("showToast(`获取Cookie详情失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        toast_index = body.index("showToast(`获取Cookie详情失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, toast_index),
            toast_index,
            "复制 Cookie catch 里的旧异常在弹 toast 前也得先验当前页面，别切页后还回来刷红字",
        )

    def test_account_management_fetchjson_callers_abort_when_unauthorized_redirect_returns_no_payload(self):
        diagnostics_body = _extract_function_body(self.app_js, "loadAboutDiagnostics")
        refresh_cookie_body = _extract_function_body(self.app_js, "refreshRealCookie")
        copy_body = _extract_function_body(self.app_js, "copyCookie")
        delete_body = _extract_function_body(self.app_js, "deleteAccount")
        open_editor_body = _extract_function_body(self.app_js, "openAccountEditor")
        open_modal_body = _extract_function_body(self.app_js, "openAccountEditModal")
        save_editor_body = _extract_function_body(self.app_js, "saveAccountEdit")

        self.assertIn("if (!accounts) {", diagnostics_body)
        self.assertLess(
            diagnostics_body.index("if (!accounts) {"),
            diagnostics_body.index("aboutDiagnosticsAccounts = Array.isArray(accounts) ? accounts : [];"),
            "账号诊断列表在 fetchJSON 因 401 跳转返回空结果后，应直接收手，别把空结果伪装成“暂无账号”",
        )

        self.assertIn("if (!currentCookie) {", refresh_cookie_body)
        self.assertLess(
            refresh_cookie_body.index("if (!currentCookie) {"),
            refresh_cookie_body.index("if (!currentCookie.value) {"),
            "真实 Cookie 刷新前的详情请求在 helper 因 401 跳转返回空结果后，应直接中止，别冒充成“未找到有效 Cookie”",
        )

        self.assertIn("if (!details) {", copy_body)
        self.assertLess(
            copy_body.index("if (!details) {"),
            copy_body.index("const value = details?.value || '';"),
            "复制 Cookie 详情请求在 helper 因 401 跳转返回空结果后，应直接中止，别再把空结果当成“暂无 Cookie”",
        )

        self.assertIn("if (!details) {", open_editor_body)
        self.assertLess(
            open_editor_body.index("if (!details) {"),
            open_editor_body.index("return await openAccountEditModal(details, requestSequence);"),
            "账号编辑详情请求在 helper 因 401 跳转返回空结果后，应直接收手，别再拿空详情去开编辑弹窗",
        )

        self.assertIn("const deleteResult = await fetchJSON(`${apiBase}/accounts/${encodedAccountId}`, {", delete_body)
        self.assertIn("if (!deleteResult) {", delete_body)
        self.assertLess(
            delete_body.index("if (!deleteResult) {"),
            delete_body.index("const accountsLoaded = await loadAccounts();"),
            "账号删除请求在 helper 因 401 跳转返回空结果后，应直接中止，别还去刷新列表再假装删除成功",
        )

        self.assertIn("if (!proxyData) {", open_modal_body)
        self.assertLess(
            open_modal_body.index("if (!proxyData) {"),
            open_modal_body.index("if (proxyData && proxyData.data) {"),
            "代理配置请求在 helper 因 401 跳转返回空结果后，应直接中止，别回退成默认值把编辑弹窗装得像正常打开",
        )

        self.assertIn("const accountInfoUpdated = await fetchJSON(`${apiBase}/accounts/${encodedAccountId}/account-info`, {", save_editor_body)
        self.assertIn("if (!accountInfoUpdated) {", save_editor_body)
        self.assertLess(
            save_editor_body.index("if (!accountInfoUpdated) {"),
            save_editor_body.index("const proxyConfigUpdated = await fetchJSON(`${apiBase}/accounts/${encodedAccountId}/proxy`, {"),
            "账号基础信息保存在 helper 因 401 跳转返回空结果后，应直接中止，别继续发代理保存请求添乱",
        )

        self.assertIn("const proxyConfigUpdated = await fetchJSON(`${apiBase}/accounts/${encodedAccountId}/proxy`, {", save_editor_body)
        self.assertIn("if (!proxyConfigUpdated) {", save_editor_body)
        self.assertLess(
            save_editor_body.index("if (!proxyConfigUpdated) {"),
            save_editor_body.index("const modalElement = document.getElementById('accountEditModal');"),
            "账号代理保存在 helper 因 401 跳转返回空结果后，应直接中止，别继续关弹窗刷成功提示装没事",
        )

    def test_account_inline_edit_flows_escape_account_ids_for_selectors_and_path_segments(self):
        self.assertIn("function escapeCssAttributeSelectorValue(value) {", self.app_js)
        edit_remark_body = _extract_function_body(self.app_js, "editRemark")
        edit_pause_body = _extract_function_body(self.app_js, "editPauseDuration")

        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", edit_remark_body)
        self.assertIn("document.querySelector(`[data-account-id=\"${selectorAccountId}\"] .remark-display`)", edit_remark_body)
        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", edit_remark_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/remark`", edit_remark_body)
        self.assertNotIn("document.querySelector(`[data-account-id=\"${accountId}\"] .remark-display`)", edit_remark_body)
        self.assertNotIn("fetch(`${apiBase}/accounts/${accountId}/remark`", edit_remark_body)

        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", edit_pause_body)
        self.assertIn("document.querySelector(`[data-account-id=\"${selectorAccountId}\"] .pause-duration-display`)", edit_pause_body)
        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", edit_pause_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/pause-duration`", edit_pause_body)
        self.assertNotIn("document.querySelector(`[data-account-id=\"${accountId}\"] .pause-duration-display`)", edit_pause_body)
        self.assertNotIn("fetch(`${apiBase}/accounts/${accountId}/pause-duration`", edit_pause_body)

    def test_load_accounts_marks_account_detail_load_failures_in_ui(self):
        body = _extract_function_body(self.app_js, "loadAccounts")

        self.assertIn("const loadError = Boolean(cookie.load_error);", body)
        self.assertIn("load_error: loadError,", body)
        self.assertIn("keywordCountLoadFailed: keywordCountLoadFailed || loadError,", body)
        self.assertIn("defaultReplyLoadFailed: defaultReplyLoadFailed || loadError,", body)
        self.assertIn("aiReplyLoadFailed: aiReplyLoadFailed || loadError", body)
        self.assertIn("const accountLoadErrorBadge = loadError", body)
        self.assertIn("账号详情读取失败", body)
        self.assertLess(
            body.index("const loadError = Boolean(cookie.load_error);"),
            body.index("keywordCountLoadFailed: keywordCountLoadFailed || loadError,"),
            "账号详情读取失败时，关键词/默认回复/AI 状态也得同步降级，别继续装正常数据",
        )

    def test_account_inline_edit_async_saves_do_not_emit_cross_page_toasts_or_restore_hidden_dom(self):
        edit_remark_body = _extract_function_body(self.app_js, "editRemark")
        edit_pause_body = _extract_function_body(self.app_js, "editPauseDuration")

        for body, success_fragment, failure_fragment, restore_fragment in (
            (
                edit_remark_body,
                "showToast('备注更新成功', 'success');",
                "showToast('备注更新失败', 'danger');",
                "remarkCell.innerHTML = originalContent;",
            ),
            (
                edit_pause_body,
                "showToast('暂停时间更新成功', 'success');",
                "showToast('暂停时间更新失败', 'danger');",
                "pauseCell.innerHTML = originalContent;",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出账号页了，旧的内联编辑成功结果不该再跨页弹 success toast",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "都切出账号页了，旧的内联编辑失败结果不该再跨页弹 danger toast",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(restore_fragment)),
                    body.index(restore_fragment),
                    "都切出账号页了，旧的内联编辑失败回调也别再去回写隐藏页 DOM",
                )

    def test_account_status_and_cooldown_flows_encode_account_ids_for_paths_and_selectors(self):
        show_cooldown_body = _extract_function_body(self.app_js, "showCooldownStatus")
        reset_cooldown_body = _extract_function_body(self.app_js, "resetCooldownTime")
        toggle_status_body = _extract_function_body(self.app_js, "toggleAccountStatus")
        update_status_body = _extract_function_body(self.app_js, "updateAccountRowStatus")
        toggle_confirm_body = _extract_function_body(self.app_js, "toggleAutoConfirm")
        update_confirm_body = _extract_function_body(self.app_js, "updateAutoConfirmRowStatus")
        toggle_comment_body = _extract_function_body(self.app_js, "toggleAutoComment")
        update_comment_body = _extract_function_body(self.app_js, "updateAutoCommentRowStatus")

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", show_cooldown_body)
        self.assertIn("fetch(`${apiBase}/qr-login/cooldown-status/${encodedAccountId}`", show_cooldown_body)
        self.assertNotIn("fetch(`${apiBase}/qr-login/cooldown-status/${accountId}`", show_cooldown_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", reset_cooldown_body)
        self.assertIn("fetch(`${apiBase}/qr-login/reset-cooldown/${encodedAccountId}`", reset_cooldown_body)
        self.assertNotIn("fetch(`${apiBase}/qr-login/reset-cooldown/${accountId}`", reset_cooldown_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", toggle_status_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/status`", toggle_status_body)
        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", toggle_status_body)
        self.assertIn("document.querySelector(`input[onchange*=\"${selectorAccountId}\"]`)", toggle_status_body)
        self.assertNotIn("fetch(`${apiBase}/accounts/${accountId}/status`", toggle_status_body)

        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", update_status_body)
        self.assertIn("document.querySelector(`input[onchange*=\"${selectorAccountId}\"]`)", update_status_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", toggle_confirm_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/auto-confirm`", toggle_confirm_body)
        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", toggle_confirm_body)
        self.assertIn("document.querySelector(`input[onchange*=\"toggleAutoConfirm('${selectorAccountId}'\"]`)", toggle_confirm_body)
        self.assertNotIn("fetch(`${apiBase}/accounts/${accountId}/auto-confirm`", toggle_confirm_body)

        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", update_confirm_body)
        self.assertIn("document.querySelector(`tr:has(input[onchange*=\"toggleAutoConfirm('${selectorAccountId}'\"])`)", update_confirm_body)
        self.assertIn("row.querySelector(`input[onchange*=\"toggleAutoConfirm('${selectorAccountId}'\"]`)", update_confirm_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", toggle_comment_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/auto-comment`", toggle_comment_body)
        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", toggle_comment_body)
        self.assertIn("document.querySelector(`input[onchange*=\"toggleAutoComment('${selectorAccountId}'\"]`)", toggle_comment_body)
        self.assertNotIn("fetch(`${apiBase}/accounts/${accountId}/auto-comment`", toggle_comment_body)

        self.assertIn("const selectorAccountId = escapeCssAttributeSelectorValue(accountId);", update_comment_body)
        self.assertIn("document.querySelector(`tr:has(input[onchange*=\"toggleAutoComment('${selectorAccountId}'\"])`)", update_comment_body)
        self.assertIn("row.querySelector(`input[onchange*=\"toggleAutoComment('${selectorAccountId}'\"]`)", update_comment_body)

    def test_account_management_action_buttons_escape_account_id_for_js_and_attributes(self):
        body = _extract_function_body(self.app_js, "loadAccounts")
        self.assertIn("const safeAccountIdForJs = escapeInlineJsSingleQuotedString(accountId);", body)
        self.assertIn("const safeAccountIdAttr = escapeHtmlAttribute(accountId);", body)
        self.assertIn("onchange=\"toggleAccountStatus('${safeAccountIdForJs}', this.checked)\"", body)
        self.assertIn("onchange=\"toggleAutoConfirm('${safeAccountIdForJs}', this.checked)\"", body)
        self.assertIn("onchange=\"toggleAutoComment('${safeAccountIdForJs}', this.checked)\"", body)
        self.assertIn("onclick=\"showCommentTemplates('${safeAccountIdForJs}')\"", body)
        self.assertIn("data-account-id=\"${safeAccountIdAttr}\"", body)
        self.assertIn("onclick=\"showFaceVerification('${safeAccountIdForJs}')\"", body)
        self.assertIn("onclick=\"openAccountEditor('${safeAccountIdForJs}')\"", body)
        self.assertIn("onclick=\"goToAutoReply('${safeAccountIdForJs}')\"", body)
        self.assertIn("onclick=\"configAIReply('${safeAccountIdForJs}')\"", body)
        self.assertIn("onclick=\"polishAccountItems('${safeAccountIdForJs}')\"", body)
        self.assertIn("onclick=\"openPolishScheduleModal('${safeAccountIdForJs}')\"", body)
        self.assertIn("onclick=\"deleteAccount('${safeAccountIdForJs}')\"", body)
        self.assertNotIn("toggleAccountStatus('${accountId}', this.checked)", body)
        self.assertNotIn("toggleAutoConfirm('${accountId}', this.checked)", body)
        self.assertNotIn("toggleAutoComment('${accountId}', this.checked)", body)
        self.assertNotIn("showFaceVerification('${accountId}')", body)
        self.assertNotIn("openAccountEditor('${accountId}')", body)
        self.assertNotIn("goToAutoReply('${accountId}')", body)
        self.assertNotIn("deleteAccount('${accountId}')", body)
        self.assertNotIn("data-account-id=\"${accountId}\"", body)

    def test_go_to_auto_reply_delayed_account_selection_requires_auto_reply_section_still_active(self):
        self.assertIn("let pendingAutoReplyAccountId = '';", self.app_js)
        body = _extract_function_body(self.app_js, "goToAutoReply")
        self.assertIn("showSection('auto-reply');", body)
        self.assertIn("const requestedAccountId = String(accountId || '').trim();", body)
        self.assertIn("pendingAutoReplyAccountId = requestedAccountId;", body)
        self.assertIn("setTimeout(() => {", body)
        self.assertIn("if (!document.getElementById('auto-reply-section')?.classList.contains('active')) {", body)
        self.assertIn("const hasTargetOption = Array.from(accountSelect.options).some(option => option.value === requestedAccountId);", body)
        self.assertIn("if (!hasTargetOption) {", body)
        self.assertIn("return;", body)
        self.assertLess(
            body.index("if (!document.getElementById('auto-reply-section')?.classList.contains('active')) {"),
            body.index("accountSelect.value = requestedAccountId;"),
            "都切出自动回复页了，goToAutoReply 的延迟回调就别再偷偷改隐藏页选中账号了",
        )
        self.assertLess(
            body.index("if (!document.getElementById('auto-reply-section')?.classList.contains('active')) {"),
            body.index("loadAccountKeywords();"),
            "都切出自动回复页了，goToAutoReply 的旧延迟回调就别再发关键词加载请求了",
        )
        self.assertLess(
            body.index("const hasTargetOption = Array.from(accountSelect.options).some(option => option.value === requestedAccountId);"),
            body.index("accountSelect.value = requestedAccountId;"),
            "goToAutoReply 先确认目标账号选项已经刷出来，再去设置选中值，别拿 100ms 当玄学同步器",
        )
        self.assertLess(
            body.index("if (!hasTargetOption) {"),
            body.index("loadAccountKeywords();"),
            "目标账号选项都还没出来，就别急着触发关键词加载，省得 UI 自己骗自己说选中了",
        )
        self.assertLess(
            body.index("pendingAutoReplyAccountId = requestedAccountId;"),
            body.index("showSection('auto-reply');"),
            "跳转自动回复前先记住目标账号，等列表异步回来后才能稳稳落选中，不然又跟网速赌命",
        )

    def test_go_to_auto_reply_pending_account_selection_is_replayed_after_account_list_refresh(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        refresh_body = _extract_function_body(self.app_js, "refreshAccountList")

        self.assertIn("pendingAutoReplyAccountId = '';", show_section_body)
        self.assertIn("const hasPendingAutoReplyAccount = pendingAutoReplyAccountId", refresh_body)
        self.assertIn("select.value = pendingAutoReplyAccountId;", refresh_body)
        self.assertIn("loadAccountKeywords();", refresh_body)

        pending_branch_index = refresh_body.index("const hasPendingAutoReplyAccount = pendingAutoReplyAccountId")
        previous_value_restore_index = refresh_body.index("previousValue && accountsWithKeywords.some(account => getCookieDetailsAccountId(account) === previousValue)")
        pending_clear_index = refresh_body.index("pendingAutoReplyAccountId = '';", pending_branch_index)
        pending_load_index = refresh_body.index("loadAccountKeywords();", pending_branch_index)

        self.assertLess(
            pending_branch_index,
            previous_value_restore_index,
            "带着 goToAutoReply 的待选账号回来时，账号列表刷新后应该优先落目标账号，别先拿 previousValue 把它顶飞了",
        )
        self.assertLess(
            pending_clear_index,
            pending_load_index,
            "待选账号一旦成功落地就得先清掉 pending，再触发关键词加载，别留着脏状态后面乱覆盖用户选择",
        )

    def test_account_management_child_fetch_failures_do_not_masquerade_as_disabled_defaults(self):
        body = _extract_function_body(self.app_js, "loadAccounts")

        self.assertIn("let keywordCountLoadFailed = keywordsResponseResult.status !== 'fulfilled';", body)
        self.assertIn("if (!keywordsResponse || !keywordsResponse.ok) {", body)
        self.assertIn("keywordCountLoadFailed = true;", body)

        self.assertIn("let defaultReplyLoadFailed = defaultReplyResponseResult.status !== 'fulfilled';", body)
        self.assertIn("if (!defaultReplyResponse || !defaultReplyResponse.ok) {", body)
        self.assertIn("defaultReplyLoadFailed = true;", body)

        self.assertIn("let aiReplyLoadFailed = aiReplyResponseResult.status !== 'fulfilled';", body)
        self.assertIn("if (!aiReplyResponse || !aiReplyResponse.ok) {", body)
        self.assertIn("aiReplyLoadFailed = true;", body)

        self.assertIn("keywordCountLoadFailed: keywordCountLoadFailed || loadError,", body)
        self.assertIn("defaultReplyLoadFailed: defaultReplyLoadFailed || loadError,", body)
        self.assertIn("aiReplyLoadFailed: aiReplyLoadFailed || loadError", body)

        self.assertNotIn("let keywordCount = 0;\n            if (keywordsResponse.ok) {", body)
        self.assertNotIn("let defaultReply = { enabled: false, reply_content: '' };\n            if (defaultReplyResponse.ok) {", body)
        self.assertNotIn("let aiReply = { ai_enabled: false, model_name: 'qwen-plus' };\n            if (aiReplyResponse.ok) {", body)

    def test_accounts_loader_fetchjson_and_child_requests_abort_on_unauthorized_redirects(self):
        body = _extract_function_body(self.app_js, "loadAccounts")

        self.assertIn("if (!cookieDetails) {", body)
        self.assertLess(
            body.index("if (!cookieDetails) {"),
            body.index("if (cookieDetails.length === 0) {"),
            "账号列表主请求在 fetchJSON 因 401 跳转返回空结果后，应直接收手，别再对 undefined.length 动刀子",
        )

        self.assertIn("let childFetchUnauthorized = false;", body)
        self.assertGreaterEqual(body.count("childFetchUnauthorized = true;"), 3)

        for unauthorized_fragment, anchor_fragment in (
            ("if (keywordsResponse && handleUnauthorizedApiResponse(keywordsResponse)) {", "let keywordCountLoadFailed = keywordsResponseResult.status !== 'fulfilled';"),
            ("if (defaultReplyResponse && handleUnauthorizedApiResponse(defaultReplyResponse)) {", "let defaultReplyLoadFailed = defaultReplyResponseResult.status !== 'fulfilled';"),
            ("if (aiReplyResponse && handleUnauthorizedApiResponse(aiReplyResponse)) {", "let aiReplyLoadFailed = aiReplyResponseResult.status !== 'fulfilled';"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "账号列表这些子请求遇到 401 得先滚去登录，别把未授权糊成“加载失败”徽章装正常",
                )

        self.assertIn("if (childFetchUnauthorized) {", body)
        self.assertLess(
            body.index("if (childFetchUnauthorized) {"),
            body.index("accountsWithKeywords.forEach(cookie => {"),
            "子请求已经触发未授权跳转后，账号列表别继续拿 null 结果往表格里灌",
        )

    def test_account_management_row_badges_surface_child_fetch_failures_instead_of_fake_disabled_states(self):
        body = _extract_function_body(self.app_js, "loadAccounts")

        self.assertIn("const keywordCountLoadFailed = Boolean(cookie.keywordCountLoadFailed);", body)
        self.assertIn("const defaultReplyLoadFailed = Boolean(cookie.defaultReplyLoadFailed);", body)
        self.assertIn("const aiReplyLoadFailed = Boolean(cookie.aiReplyLoadFailed);", body)

        self.assertIn("const keywordCountBadge = keywordCountLoadFailed", body)
        self.assertIn("'<span class=\"badge bg-warning text-dark\">加载失败</span>'", body)
        self.assertIn("const defaultReplyBadge = defaultReplyLoadFailed", body)
        self.assertIn("const aiReplyBadge = aiReplyLoadFailed", body)

        self.assertNotIn("const defaultReplyBadge = cookie.defaultReply.enabled ?", body)
        self.assertNotIn("const aiReplyBadge = cookie.aiReply.ai_enabled ?", body)
        self.assertNotIn("<span class=\"badge ${cookie.keywordCount > 0 ? 'bg-success' : 'bg-secondary'}\">", body)

    def test_refresh_cookie_status_views_escape_runtime_username_and_message(self):
        self.assertIn("const safeUsername = escapeHtml(username || '');", self.app_js)
        self.assertIn("已配置用户名: ${safeUsername}", self.app_js)
        self.assertNotIn("已配置用户名: ${username}", self.app_js)

        body = _extract_function_body(self.app_js, "updateRefreshCookieStatus")
        self.assertIn("const safeMessage = escapeHtml(message || '');", body)
        self.assertIn("${safeMessage}</span>", body)
        self.assertNotIn("${message}</span>", body)

    def test_account_management_detail_requests_encode_account_ids_in_path_segments(self):
        load_accounts_body = _extract_function_body(self.app_js, "loadAccounts")
        self.assertIn("fetch(`${apiBase}/keywords/counts`", load_accounts_body)
        self.assertIn("fetch(`${apiBase}/default-replies`", load_accounts_body)
        self.assertIn("fetch(`${apiBase}/ai-reply-settings`", load_accounts_body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${accountId}`", load_accounts_body)
        self.assertNotIn("fetch(`${apiBase}/default-replies/${accountId}`", load_accounts_body)
        self.assertNotIn("fetch(`${apiBase}/ai-reply-settings/${accountId}`", load_accounts_body)

        face_verification_body = _extract_function_body(self.app_js, "showFaceVerification")
        self.assertIn("fetch(`${apiBase}/face-verification/screenshot/${encodeURIComponent(accountId)}`", face_verification_body)
        self.assertNotIn("fetch(`${apiBase}/face-verification/screenshot/${accountId}`", face_verification_body)

    def test_account_management_edit_and_delete_flows_encode_account_ids_and_only_report_success_after_reload(self):
        open_editor_body = _extract_function_body(self.app_js, "openAccountEditor")
        open_modal_body = _extract_function_body(self.app_js, "openAccountEditModal")
        save_body = _extract_function_body(self.app_js, "saveAccountEdit")
        delete_body = _extract_function_body(self.app_js, "deleteAccount")
        refresh_real_cookie_body = _extract_function_body(self.app_js, "refreshRealCookie")

        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodeURIComponent(id)}/details?include_secrets=true`", open_editor_body)
        self.assertNotIn("fetchJSON(apiBase + `/accounts/${id}/details?include_secrets=true`)", open_editor_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", open_modal_body)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodedAccountId}/proxy?include_secret=true`", open_modal_body)
        self.assertNotIn("fetchJSON(apiBase + `/accounts/${accountId}/proxy?include_secret=true`)", open_modal_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(id);", save_body)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodedAccountId}/account-info`", save_body)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodedAccountId}/proxy`", save_body)
        self.assertIn("const accountsLoaded = await loadAccounts();", save_body)
        self.assertIn("if (accountsLoaded === true) {", save_body)
        self.assertIn("} else if (accountsLoaded === false) {", save_body)
        self.assertIn("showToast(`账号 \"${id}\" 信息已更新`, 'success');", save_body)
        self.assertIn("showToast(`账号 \"${id}\" 信息已更新，但账号列表刷新失败，请稍后手动刷新`, 'warning');", save_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(id);", delete_body)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodedAccountId}`", delete_body)
        self.assertIn("const accountsLoaded = await loadAccounts();", delete_body)
        self.assertIn("if (accountsLoaded === true) {", delete_body)
        self.assertIn("} else if (accountsLoaded === false) {", delete_body)
        self.assertIn("showToast(`账号 \"${id}\" 已删除`, 'success');", delete_body)
        self.assertIn("showToast(`账号 \"${id}\" 已删除，但账号列表刷新失败，请稍后手动刷新`, 'warning');", delete_body)

        self.assertIn("const accountsLoaded = await loadAccounts();", refresh_real_cookie_body)
        self.assertIn("if (accountsLoaded === true) {", refresh_real_cookie_body)
        self.assertIn("} else if (accountsLoaded === false) {", refresh_real_cookie_body)
        self.assertIn("showToast(`账号 \"${accountId}\" 真实Cookie刷新成功`, 'success');", refresh_real_cookie_body)
        self.assertIn("showToast(`账号 \"${accountId}\" 真实Cookie刷新成功，但账号列表刷新失败，请稍后手动刷新`, 'warning');", refresh_real_cookie_body)

    def test_accounts_loader_ignores_stale_async_responses_and_hidden_section_updates(self):
        self.assertIn("let accountsRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_body = _extract_function_body(self.app_js, "loadAccounts")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("accountsRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++accountsRequestSequence;", load_body)
        self.assertIn("suppressErrorToast: true", load_body)
        self.assertIn("requestSequence !== accountsRequestSequence", load_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("requestSequence !== accountsRequestSequence"),
            load_body.index("tbody.appendChild(tr);"),
            "旧的账号列表请求不该晚回来后把当前账号表格再糊回旧数据",
        )
        self.assertLess(
            load_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, load_body.index("showToast(`加载账号列表失败: ${err.message || '请稍后重试'}`, 'danger');")),
            load_body.index("showToast(`加载账号列表失败: ${err.message || '请稍后重试'}`, 'danger');"),
            "都切出账号页了，旧的账号列表失败不该再跨页弹 generic 红字",
        )

    def test_accounts_loader_catch_failures_surface_runtime_error_messages(self):
        body = _extract_function_body(self.app_js, "loadAccounts")

        self.assertNotIn("showToast('加载账号列表失败', 'danger');", body)
        self.assertIn("showToast(`加载账号列表失败: ${err.message || '请稍后重试'}`, 'danger');", body)
        toast_index = body.index("showToast(`加载账号列表失败: ${err.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            body.rfind("requestSequence !== accountsRequestSequence", 0, toast_index),
            toast_index,
            "账号列表 catch 里的旧异常在弹 toast 前也得先验 stale，别旧请求回来乱抽风",
        )
        self.assertLess(
            body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, toast_index),
            toast_index,
            "都切出账号页了，旧的账号列表 catch 失败也别跨页回来甩红字",
        )

    def test_accounts_loader_and_editor_flows_finally_do_not_clear_newer_loading_state(self):
        for function_name, guard_fragment in (
            ("loadAccounts", "requestSequence !== accountsRequestSequence"),
            ("openAccountEditor", "requestSequence !== accountEditRequestSequence"),
            ("saveAccountEdit", "requestSequence !== accountEditRequestSequence"),
        ):
            body = _extract_function_body(self.app_js, function_name)
            with self.subTest(function_name=function_name):
                self.assertIn("} finally {", body)
                finally_block = body.split("} finally {", 1)[1]
                self.assertIn(guard_fragment, finally_block)
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", finally_block)
                self.assertLess(
                    finally_block.index(guard_fragment),
                    finally_block.index("toggleLoading(false);"),
                    "同页已经切到更新的账号加载/编辑会话后，旧 finally 不该把当前 loading 先给掐灭",
                )
                self.assertIn("toggleLoading(false);", finally_block)

    def test_account_edit_modal_ignores_stale_async_responses_and_hidden_modal_state(self):
        self.assertIn("let accountEditRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        open_editor_body = _extract_function_body(self.app_js, "openAccountEditor")
        open_modal_body = _extract_function_body(self.app_js, "openAccountEditModal")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("accountEditRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++accountEditRequestSequence;", open_editor_body)
        self.assertIn("requestSequence !== accountEditRequestSequence", open_editor_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", open_editor_body)
        self.assertIn("return null;", open_editor_body)

        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", open_modal_body)
        self.assertIn("accountEditRequestSequence += 1;", open_modal_body)
        self.assertIn("modalElement.dataset.accountEditModalBound = 'true';", open_modal_body)
        self.assertIn("requestSequence !== accountEditRequestSequence", open_modal_body)
        self.assertLess(
            open_modal_body.index("requestSequence !== accountEditRequestSequence"),
            open_modal_body.index("document.getElementById('editAccountCookie').value = accountData.value || '';"),
            "旧的账号编辑请求不该晚回来后把当前编辑弹窗内容改成别的账号",
        )
        proxy_catch_index = open_modal_body.index("} catch (err) {")
        proxy_default_reset_index = open_modal_body.index("document.getElementById('editProxyType').value = 'none';", proxy_catch_index)
        self.assertLess(
            open_modal_body.find("requestSequence !== accountEditRequestSequence", proxy_catch_index),
            proxy_default_reset_index,
            "代理配置子请求失败时也得先验 modal 会话没过期，别把当前弹窗的代理字段清成旧请求的默认值",
        )
        self.assertIn("setTimeout(() => {", open_modal_body)
        delayed_block = open_modal_body.split("setTimeout(() => {", 1)[1]
        self.assertIn("requestSequence !== accountEditRequestSequence", delayed_block)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", delayed_block)
        tooltip_guard_index = delayed_block.index("requestSequence !== accountEditRequestSequence")
        self.assertLess(
            tooltip_guard_index,
            delayed_block.index("initTooltips();"),
            "账号编辑弹窗都切会话或切页了，延迟 tooltip 初始化就别再回来补刀摸当前 DOM 了",
        )

    def test_account_edit_modal_proxy_load_failures_surface_runtime_error_and_block_silent_proxy_wipes(self):
        open_modal_body = _extract_function_body(self.app_js, "openAccountEditModal")
        save_body = _extract_function_body(self.app_js, "saveAccountEdit")

        self.assertIn("modalElement.dataset.proxyConfigLoadState = 'loading';", open_modal_body)
        self.assertIn("modalElement.dataset.proxyConfigLoadState = 'loaded';", open_modal_body)
        self.assertIn("modalElement.dataset.proxyConfigLoadState = 'failed';", open_modal_body)
        self.assertIn("showToast(`加载代理配置失败: ${err.message || '请稍后重试'}`, 'danger');", open_modal_body)
        proxy_toast_index = open_modal_body.index("showToast(`加载代理配置失败: ${err.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            open_modal_body.rfind("requestSequence !== accountEditRequestSequence", 0, proxy_toast_index),
            proxy_toast_index,
            "代理配置 catch 里的旧异常在弹 toast 前也得先验 stale，别旧弹窗请求回来乱甩红字",
        )
        self.assertLess(
            open_modal_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, proxy_toast_index),
            proxy_toast_index,
            "都切出账号页了，旧的代理配置 catch 失败别跨页回来刷 toast",
        )

        self.assertIn("const accountEditModalElement = document.getElementById('accountEditModal');", save_body)
        self.assertIn("if (accountEditModalElement?.dataset.proxyConfigLoadState === 'failed') {", save_body)
        self.assertIn("showToast('代理配置加载失败，请重新打开编辑窗口后重试', 'warning');", save_body)
        self.assertLess(
            save_body.index("if (accountEditModalElement?.dataset.proxyConfigLoadState === 'failed') {"),
            save_body.index("const proxyType = document.getElementById('editProxyType').value;"),
            "代理配置都没拉下来时就别继续把默认值当真去走保存了，不然纯属拿用户现有代理祭天",
        )
        self.assertLess(
            save_body.index("showToast('代理配置加载失败，请重新打开编辑窗口后重试', 'warning');"),
            save_body.index("const proxyConfigUpdated = await fetchJSON(`${apiBase}/accounts/${encodedAccountId}/proxy`, {"),
            "代理配置加载失败时应先拦住保存，别继续把默认 none 发给后端洗掉原配置",
        )

    def test_account_edit_save_mutation_respects_modal_request_sequence_and_hidden_section_before_hiding_or_toasting(self):
        open_modal_body = _extract_function_body(self.app_js, "openAccountEditModal")
        save_body = _extract_function_body(self.app_js, "saveAccountEdit")

        self.assertIn("if (modalElement.dataset.accountEditIgnoreNextHidden === 'true') {", open_modal_body)
        self.assertIn("modalElement.dataset.accountEditIgnoreNextHidden = 'false';", open_modal_body)

        self.assertIn("const requestSequence = accountEditRequestSequence;", save_body)
        self.assertIn("requestSequence !== accountEditRequestSequence", save_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", save_body)
        self.assertIn("suppressErrorToast: true", save_body)
        self.assertIn("modalElement.dataset.accountEditIgnoreNextHidden = 'true';", save_body)
        self.assertIn("return null;", save_body)
        self.assertLess(
            save_body.index("requestSequence !== accountEditRequestSequence"),
            save_body.index("modal.hide();"),
            "旧的账号编辑保存响应不该回来把已经重开的编辑弹窗又关掉",
        )
        self.assertLess(
            save_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, save_body.index("showToast(`账号 \"${id}\" 信息已更新`, 'success');")),
            save_body.index("showToast(`账号 \"${id}\" 信息已更新`, 'success');"),
            "都切出账号页了，旧的账号编辑保存成功响应别回来跨页弹 success toast",
        )

    def test_account_mutation_actions_are_invalidated_when_leaving_accounts_and_ignore_older_same_page_responses(self):
        self.assertIn("let accountMutationActionRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        delete_body = _extract_function_body(self.app_js, "deleteAccount")
        refresh_body = _extract_function_body(self.app_js, "refreshRealCookie")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("accountMutationActionRequestSequence += 1;", show_section_body)

        for body, anchor_fragment in (
            (delete_body, "const accountsLoaded = await loadAccounts();"),
            (refresh_body, "const accountsLoaded = await loadAccounts();"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("++accountMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== accountMutationActionRequestSequence", body)
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== accountMutationActionRequestSequence"),
                    body.index(anchor_fragment),
                    "旧的账号操作响应不该在新一轮操作开始后还回来触发账号列表刷新",
                )

    def test_account_delete_and_refresh_mutations_do_not_emit_cross_page_toasts_after_leaving_accounts(self):
        delete_body = _extract_function_body(self.app_js, "deleteAccount")
        refresh_body = _extract_function_body(self.app_js, "refreshRealCookie")

        for body, success_fragment in (
            (delete_body, "showToast(`账号 \"${id}\" 已删除`, 'success');"),
            (refresh_body, "showToast(`账号 \"${accountId}\" 真实Cookie刷新成功`, 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('accounts-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "都离开账号页了，旧的账号操作响应不该再跨页弹 success toast",
                )

        refresh_finally_block = refresh_body.split("} finally {", 1)[1]
        self.assertIn("actionRequestSequence !== accountMutationActionRequestSequence", refresh_finally_block)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", refresh_finally_block)
        self.assertLess(
            refresh_finally_block.index("actionRequestSequence !== accountMutationActionRequestSequence"),
            refresh_finally_block.index("button.disabled = false;"),
            "刷新真实Cookie旧请求的 finally 不该在新动作开始后还把当前按钮 disabled 状态回写回去",
        )
        self.assertLess(
            refresh_finally_block.index("actionRequestSequence !== accountMutationActionRequestSequence"),
            refresh_finally_block.index("button.innerHTML = originalContent;"),
            "刷新真实Cookie旧请求的 finally 也不该在新动作开始后把当前按钮文案还原成老状态",
        )

    def test_account_toggle_mutations_keep_selector_context_for_error_recovery_and_ignore_stale_responses(self):
        toggle_status_body = _extract_function_body(self.app_js, "toggleAccountStatus")
        toggle_confirm_body = _extract_function_body(self.app_js, "toggleAutoConfirm")
        toggle_comment_body = _extract_function_body(self.app_js, "toggleAutoComment")

        for body, selector_fragment, anchor_fragment in (
            (
                toggle_status_body,
                "const selectorAccountId = escapeCssAttributeSelectorValue(accountId);",
                "showToast(result.message || `账号 \"${accountId}\" 已${enabled ? '启用' : '禁用'}`, 'success');",
            ),
            (
                toggle_confirm_body,
                "const selectorAccountId = escapeCssAttributeSelectorValue(accountId);",
                "showToast(result.message, 'success');",
            ),
            (
                toggle_comment_body,
                "const selectorAccountId = escapeCssAttributeSelectorValue(accountId);",
                "showToast(result.message, 'success');",
            ),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("++accountMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== accountMutationActionRequestSequence", body)
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertIn(selector_fragment, body)
                self.assertLess(
                    body.index(selector_fragment),
                    body.index("try {"),
                    "错误恢复用到的 selector 变量别塞 try 里，真异常了 catch 连变量都摸不着，笑死人",
                )
                self.assertLess(
                    body.index("actionRequestSequence !== accountMutationActionRequestSequence"),
                    body.index(anchor_fragment),
                    "旧的账号开关操作响应不该在新一轮操作开始后还回来乱弹 toast / 乱回写 UI",
                )

    def test_account_mutation_finally_blocks_do_not_clear_newer_loading_state(self):
        for function_name in (
            "polishAccountItems",
            "toggleAccountStatus",
            "toggleAutoConfirm",
            "toggleAutoComment",
        ):
            body = _extract_function_body(self.app_js, function_name)
            with self.subTest(function_name=function_name):
                self.assertIn("} finally {", body)
                finally_block = body.split("} finally {", 1)[1]
                self.assertIn("actionRequestSequence !== accountMutationActionRequestSequence", finally_block)
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", finally_block)
                self.assertLess(
                    finally_block.index("actionRequestSequence !== accountMutationActionRequestSequence"),
                    finally_block.index("toggleLoading(false);"),
                    "同页已经切到更新的账号 mutation 会话后，旧 finally 不该把当前 loading 先给掐灭",
                )
                self.assertIn("toggleLoading(false);", finally_block)

    def test_account_polish_and_cooldown_actions_ignore_stale_async_responses_and_hidden_accounts_section(self):
        polish_body = _extract_function_body(self.app_js, "polishAccountItems")
        show_cooldown_body = _extract_function_body(self.app_js, "showCooldownStatus")
        reset_cooldown_body = _extract_function_body(self.app_js, "resetCooldownTime")

        self.assertIn("const actionRequestSequence = ++accountMutationActionRequestSequence;", polish_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", polish_body)
        self.assertIn("return null;", polish_body)
        self.assertLess(
            polish_body.index("actionRequestSequence !== accountMutationActionRequestSequence"),
            polish_body.index("showToast(`擦亮完成: ${data.polished}/${data.total} 个商品成功`, 'success');"),
            "旧的一键擦亮响应不该晚回来后在隐藏页上乱报成功",
        )
        self.assertLess(
            polish_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, polish_body.index("showToast(`擦亮失败: ${data.message}`, 'danger');")),
            polish_body.index("showToast(`擦亮失败: ${data.message}`, 'danger');"),
            "都离开账号页了，旧的一键擦亮失败响应别回来甩 danger toast",
        )

        self.assertIn("const actionRequestSequence = ++accountMutationActionRequestSequence;", show_cooldown_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", show_cooldown_body)
        self.assertIn("return null;", show_cooldown_body)
        self.assertLess(
            show_cooldown_body.index("actionRequestSequence !== accountMutationActionRequestSequence"),
            show_cooldown_body.index("if (confirm(statusMessage)) {"),
            "旧的冷却状态响应不该晚回来还弹 confirm 让人一脸懵",
        )
        self.assertLess(
            show_cooldown_body.index("actionRequestSequence !== accountMutationActionRequestSequence"),
            show_cooldown_body.index("alert(statusMessage);"),
            "都切页了，旧的冷却状态响应不该再弹 alert 刷存在感",
        )

        self.assertIn("const actionRequestSequence = ++accountMutationActionRequestSequence;", reset_cooldown_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", reset_cooldown_body)
        self.assertIn("return null;", reset_cooldown_body)
        self.assertLess(
            reset_cooldown_body.index("actionRequestSequence !== accountMutationActionRequestSequence"),
            reset_cooldown_body.index("showToast(message, 'success');"),
            "旧的重置冷却时间响应不该晚回来后在隐藏页上乱报成功",
        )
        self.assertLess(
            reset_cooldown_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, reset_cooldown_body.index("showToast(`重置冷却时间失败: ${result.message}`, 'danger');")),
            reset_cooldown_body.index("showToast(`重置冷却时间失败: ${result.message}`, 'danger');"),
            "都离开账号页了，旧的重置冷却时间失败响应别回来甩 danger toast",
        )

    def test_account_cooldown_actions_handle_unauthorized_before_followup_work(self):
        show_cooldown_body = _extract_function_body(self.app_js, "showCooldownStatus")
        reset_cooldown_body = _extract_function_body(self.app_js, "resetCooldownTime")

        for body, unauthorized_fragment, anchor_fragment in (
            (show_cooldown_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (reset_cooldown_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "账号冷却状态/重置这两条 raw fetch 遇到 401 得先去登录，别后面还继续弹业务失败提示",
                )

    def test_account_cooldown_failure_actions_read_structured_error_messages(self):
        show_cooldown_body = _extract_function_body(self.app_js, "showCooldownStatus")
        reset_cooldown_body = _extract_function_body(self.app_js, "resetCooldownTime")

        for body, error_fragment, toast_fragment, label in (
            (show_cooldown_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`获取冷却状态失败: ${errorMessage}`, 'danger');", "查看冷却状态"),
            (reset_cooldown_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`重置冷却时间失败: ${errorMessage}`, 'danger');", "重置冷却时间"),
        ):
            with self.subTest(label=label):
                self.assertIn(error_fragment, body)
                self.assertLess(
                    body.index(error_fragment),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再直接吃 json 结构碰运气",
                )

    def test_account_cooldown_failures_recheck_state_after_error_body_read(self):
        show_cooldown_body = _extract_function_body(self.app_js, "showCooldownStatus")
        reset_cooldown_body = _extract_function_body(self.app_js, "resetCooldownTime")

        for body, error_fragment, toast_fragment, label in (
            (show_cooldown_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`获取冷却状态失败: ${errorMessage}`, 'danger');", "查看冷却状态"),
            (reset_cooldown_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`重置冷却时间失败: ${errorMessage}`, 'danger');", "重置冷却时间"),
        ):
            with self.subTest(label=label):
                error_index = body.index(error_fragment)
                toast_index = body.index(toast_fragment)
                self.assertLess(
                    body.find("actionRequestSequence !== accountMutationActionRequestSequence", error_index),
                    toast_index,
                    f"{label}读完错误体后也得先验 stale，别旧错误回来乱弹 toast",
                )
                self.assertLess(
                    body.find("!document.getElementById('accounts-section')?.classList.contains('active')", error_index),
                    toast_index,
                    f"都切出账号页了，旧的{label}失败结果别再跨页甩 danger toast",
                )

    def test_account_polish_and_refresh_real_cookie_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        polish_body = _extract_function_body(self.app_js, "polishAccountItems")
        refresh_body = _extract_function_body(self.app_js, "refreshRealCookie")

        for body, unauthorized_fragment, anchor_fragment in (
            (polish_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (refresh_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "账号擦亮 / 真实 Cookie 刷新这两条 raw fetch 遇到 401 得先去登录，别后面还继续读业务结果",
                )

    def test_account_polish_and_refresh_real_cookie_failures_read_structured_error_messages(self):
        polish_body = _extract_function_body(self.app_js, "polishAccountItems")
        refresh_body = _extract_function_body(self.app_js, "refreshRealCookie")

        for body, error_fragment, toast_fragment, label in (
            (polish_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`擦亮失败: ${errorMessage}`, 'danger');", "一键擦亮"),
            (refresh_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`真实Cookie刷新失败: ${errorMessage}`, 'danger');", "刷新真实 Cookie"),
        ):
            with self.subTest(label=label):
                self.assertIn(error_fragment, body)
                self.assertLess(
                    body.index(error_fragment),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再直接吞 response.json() 的侥幸结构",
                )

    def test_account_polish_and_refresh_real_cookie_failures_recheck_state_after_error_body_read(self):
        polish_body = _extract_function_body(self.app_js, "polishAccountItems")
        refresh_body = _extract_function_body(self.app_js, "refreshRealCookie")

        for body, error_fragment, toast_fragment, label in (
            (polish_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`擦亮失败: ${errorMessage}`, 'danger');", "一键擦亮"),
            (refresh_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`真实Cookie刷新失败: ${errorMessage}`, 'danger');", "刷新真实 Cookie"),
        ):
            with self.subTest(label=label):
                error_index = body.index(error_fragment)
                toast_index = body.index(toast_fragment)
                self.assertLess(
                    body.find("actionRequestSequence !== accountMutationActionRequestSequence", error_index),
                    toast_index,
                    f"{label}读完错误体后也得先验 stale，别旧错误回来乱弹 toast",
                )
                self.assertLess(
                    body.find("!document.getElementById('accounts-section')?.classList.contains('active')", error_index),
                    toast_index,
                    f"都切出账号页了，旧的{label}失败结果别再跨页甩 danger toast",
                )

    def test_account_toggle_status_raw_fetch_action_handles_unauthorized_before_followup_work(self):
        body = _extract_function_body(self.app_js, "toggleAccountStatus")
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (response.ok) {"),
            "账号启用/禁用这条 raw fetch 遇到 401 得先去登录，别后面还继续装成功甚至本地模拟状态",
        )

    def test_account_toggle_status_failure_reads_structured_error_messages_instead_of_local_simulation(self):
        body = _extract_function_body(self.app_js, "toggleAccountStatus")
        self.assertIn("const result = await response.json().catch(() => ({}));", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("showToast(`账号状态更新失败: ${errorMessage}`, 'danger');", body)
        self.assertLess(
            body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("showToast(`账号状态更新失败: ${errorMessage}`, 'danger');"),
            "账号状态切换失败时先统一解析错误体，别再把后端真失败硬伪装成前端本地成功",
        )
        self.assertNotIn("后端暂不支持账号状态切换，使用前端模拟", body)
        self.assertNotIn("(前端模拟)", body)
        self.assertNotIn("(本地模拟)", body)

    def test_account_toggle_status_failures_recheck_state_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "toggleAccountStatus")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        toast_index = body.index("showToast(`账号状态更新失败: ${errorMessage}`, 'danger');", error_index)
        self.assertLess(
            body.find("actionRequestSequence !== accountMutationActionRequestSequence", error_index),
            toast_index,
            "账号状态切换读完错误体后也得先验 stale，别旧错误回来乱弹 toast",
        )
        self.assertLess(
            body.find("!document.getElementById('accounts-section')?.classList.contains('active')", error_index),
            toast_index,
            "都切出账号页了，旧的账号状态切换失败结果别再跨页甩 danger toast",
        )

    def test_account_toggle_auto_confirm_and_comment_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        toggle_confirm_body = _extract_function_body(self.app_js, "toggleAutoConfirm")
        toggle_comment_body = _extract_function_body(self.app_js, "toggleAutoComment")

        for body, label in (
            (toggle_confirm_body, "自动确认发货"),
            (toggle_comment_body, "自动好评"),
        ):
            with self.subTest(label=label):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index("if (response.ok) {"),
                    f"{label}这条 raw fetch 遇到 401 得先去登录，别后面还继续读业务结果、弹 toast、回写开关状态",
                )

    def test_account_toggle_auto_confirm_and_comment_failures_read_structured_error_messages(self):
        toggle_confirm_body = _extract_function_body(self.app_js, "toggleAutoConfirm")
        toggle_comment_body = _extract_function_body(self.app_js, "toggleAutoComment")

        for body, toast_fragment, label in (
            (toggle_confirm_body, "showToast(error || '更新自动确认发货设置失败', 'error');", "自动确认发货"),
            (toggle_comment_body, "showToast(error || '更新自动好评设置失败', 'error');", "自动好评"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再拿裸 json.detail 撞大运",
                )
                self.assertNotIn("const error = await response.json();", body)

    def test_account_toggle_auto_confirm_and_comment_failures_recheck_state_after_error_body_read(self):
        toggle_confirm_body = _extract_function_body(self.app_js, "toggleAutoConfirm")
        toggle_comment_body = _extract_function_body(self.app_js, "toggleAutoComment")

        for body, toast_fragment, label in (
            (toggle_confirm_body, "showToast(error || '更新自动确认发货设置失败', 'error');", "自动确认发货"),
            (toggle_comment_body, "showToast(error || '更新自动好评设置失败', 'error');", "自动好评"),
        ):
            with self.subTest(label=label):
                error_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                toast_index = body.index(toast_fragment, error_index)
                self.assertLess(
                    body.find("actionRequestSequence !== accountMutationActionRequestSequence", error_index),
                    toast_index,
                    f"{label}读完错误体后也得先验 stale，别旧错误回来乱弹 toast",
                )
                self.assertLess(
                    body.find("!document.getElementById('accounts-section')?.classList.contains('active')", error_index),
                    toast_index,
                    f"都切出账号页了，旧的{label}失败结果别再跨页甩 danger toast",
                )

    def test_account_toggle_auto_confirm_and_comment_catch_failures_surface_runtime_error_messages(self):
        for body, legacy_toast, runtime_toast, label in (
            (
                _extract_function_body(self.app_js, "toggleAutoConfirm"),
                "showToast('网络错误，请稍后重试', 'error');",
                "showToast(`切换自动确认发货状态失败: ${error.message || '请稍后重试'}`, 'error');",
                "自动确认发货",
            ),
            (
                _extract_function_body(self.app_js, "toggleAutoComment"),
                "showToast('网络错误，请稍后重试', 'error');",
                "showToast(`切换自动好评状态失败: ${error.message || '请稍后重试'}`, 'error');",
                "自动好评",
            ),
        ):
            with self.subTest(label=label):
                self.assertNotIn(legacy_toast, body)
                self.assertIn(runtime_toast, body)
                toast_index = body.index(runtime_toast)
                self.assertLess(
                    body.rfind("actionRequestSequence !== accountMutationActionRequestSequence", 0, toast_index),
                    toast_index,
                    f"{label} catch 里的旧异常在弹 toast 前也得先验 stale，别换页换会话后还回来抽风",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, toast_index),
                    toast_index,
                    f"都切出账号页了，旧的{label} catch 失败也别跨页回来甩红字",
                )

    def test_account_toggle_status_catch_failures_surface_runtime_error_messages(self):
        body = _extract_function_body(self.app_js, "toggleAccountStatus")

        self.assertNotIn("showToast('切换账号状态失败，请稍后重试', 'danger');", body)
        self.assertIn("showToast(`切换账号状态失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        toast_index = body.index("showToast(`切换账号状态失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            body.rfind("actionRequestSequence !== accountMutationActionRequestSequence", 0, toast_index),
            toast_index,
            "账号状态切换 catch 里的旧异常在弹 toast 前也得先验 stale，别旧请求回来乱甩红字",
        )
        self.assertLess(
            body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, toast_index),
            toast_index,
            "都切出账号页了，旧的账号状态切换 catch 失败也别跨页回来刷红字",
        )

    def test_account_polish_requires_account_id_before_loading_or_action_sequence(self):
        body = _extract_function_body(self.app_js, "polishAccountItems")
        self.assertIn("if (!accountId) {", body)
        self.assertIn("showToast('缺少账号ID', 'warning');", body)
        self.assertLess(
            body.index("if (!accountId) {"),
            body.index("const actionRequestSequence = ++accountMutationActionRequestSequence;"),
            "缺少账号ID时只是前端校验，别先把账号 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (!accountId) {"),
            body.index("toggleLoading(true);"),
            "缺少账号ID时连 loading 都不该亮，别一上来就装忙",
        )

    def test_refresh_real_cookie_action_sequence_starts_only_after_preflight_checks(self):
        body = _extract_function_body(self.app_js, "refreshRealCookie")
        action_index = body.index("actionRequestSequence = ++accountMutationActionRequestSequence;")

        self.assertLess(
            body.index("if (!currentCookie) {"),
            action_index,
            "登录态都没了时真实 Cookie 刷新应直接中止，别先把账号 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (!currentCookie.value) {"),
            action_index,
            "当前账号连有效 Cookie 都没有时只是前置检查，别先把账号 mutation action sequence 顶掉别的正常动作",
        )
        confirm_return_index = body.index("return;", body.index("if (!confirm(`确定要刷新账号"))
        self.assertLess(
            confirm_return_index,
            action_index,
            "用户都取消真实 Cookie 刷新了，就别先把账号 mutation action sequence 顶掉别的正常动作",
        )

    def test_password_login_verification_callers_preserve_fallback_verification_url_when_screenshot_exists(self):
        for function_name in ("checkManualCookieImportStatus", "startRefreshCookiePolling", "checkPasswordLoginStatus"):
            body = _extract_function_body(self.app_js, function_name)
            with self.subTest(function_name=function_name):
                self.assertIn("showPasswordLoginQRCode(", body)
                self.assertIn("data.verification_url || data.qr_code_url", body)
                self.assertNotIn("data.screenshot_path || data.verification_url", body)
                self.assertNotIn("data.screenshot_path || data.verification_url || data.qr_code_url", body)

    def test_manual_cookie_import_modal_close_cancels_backend_session_instead_of_only_resetting_frontend_state(self):
        self.assertIn("async function cancelManualCookieImportSession(sessionId) {", self.app_js)
        self.assertIn("fetch(`${apiBase}/manual-cookie-import/cancel/${encodeURIComponent(sessionId)}`", self.app_js)

        modal_events_body = _extract_function_body(self.app_js, "bindPasswordLoginQRModalEvents")
        self.assertIn("mode: 'session'", self.app_js)
        self.assertIn("if (passwordLoginQRModalState.mode === 'preview') {", modal_events_body)
        self.assertIn("passwordLoginQRModalState.mode = 'session';", modal_events_body)
        self.assertIn("const activeSessionId = manualCookieImportPollingState.sessionId;", modal_events_body)
        self.assertIn("void cancelManualCookieImportSession(activeSessionId);", modal_events_body)
        self.assertLess(
            modal_events_body.index("if (passwordLoginQRModalState.mode === 'preview') {"),
            modal_events_body.index("if (manualCookieImportPollingState.sessionId && !manualCookieImportPollingState.completed) {"),
            "纯截图预览模式关窗时就别去顺手取消导入会话了，别整这阴间串杀",
        )
        self.assertNotIn("showToast('已停止当前导入验证流程', 'info');", modal_events_body)

        self.assertIn('@app.post("/manual-cookie-import/cancel/{session_id}")', self.reply_server)
        self.assertIn("async def cancel_manual_cookie_import(", self.reply_server)
        self.assertIn("MANUAL_COOKIE_IMPORT_TERMINAL_STATUSES = {'success', 'failed', 'cancelled'}", self.reply_server)
        self.assertIn("if status == 'cancelled':", self.reply_server)
        self.assertIn("'status': 'cancelled'", self.reply_server)

    def test_account_login_and_cookie_import_success_flows_distinguish_reload_failures_from_success(self):
        manual_success_body = _extract_function_body(self.app_js, "handleManualCookieImportSuccess")
        password_success_body = _extract_function_body(self.app_js, "handlePasswordLoginSuccess")
        manual_poll_body = _extract_function_body(self.app_js, "checkManualCookieImportStatus")
        password_poll_body = _extract_function_body(self.app_js, "checkPasswordLoginStatus")
        refresh_poll_body = _extract_function_body(self.app_js, "startRefreshCookiePolling")

        self.assertIn("async function handleManualCookieImportSuccess(data) {", self.app_js)
        self.assertIn("const accountsLoaded = await loadAccounts();", manual_success_body)
        self.assertIn("if (accountsLoaded === false) {", manual_success_body)
        self.assertIn("showToast(`账号 ${data.account_id} 导入并验证成功，但账号列表刷新失败，请稍后手动刷新`, 'warning');", manual_success_body)
        self.assertIn("} else {", manual_success_body)
        self.assertIn("showToast(`账号 ${data.account_id} 导入并验证成功`, 'success');", manual_success_body)

        self.assertIn("async function handlePasswordLoginSuccess(data) {", self.app_js)
        self.assertIn("const accountsLoaded = await loadAccounts();", password_success_body)
        self.assertIn("if (accountsLoaded === false) {", password_success_body)
        self.assertIn("showToast(`账号 ${data.account_id} 登录成功，但账号列表刷新失败，请稍后手动刷新`, 'warning');", password_success_body)
        self.assertIn("} else {", password_success_body)
        self.assertIn("showToast(`账号 ${data.account_id} 登录成功！`, 'success');", password_success_body)

        self.assertIn("await handleManualCookieImportSuccess(data);", manual_poll_body)
        self.assertIn("await handlePasswordLoginSuccess(data);", password_poll_body)

        self.assertIn("const accountsLoaded = await loadAccounts();", refresh_poll_body)
        self.assertIn("if (accountsLoaded === false) {", refresh_poll_body)
        self.assertIn("showToast(`账号 ${accountId} Cookie刷新成功，但账号列表刷新失败，请稍后手动刷新`, 'warning');", refresh_poll_body)
        self.assertIn("} else {", refresh_poll_body)
        self.assertIn("showToast(`账号 ${accountId} Cookie刷新成功！`, 'success');", refresh_poll_body)

    def test_account_login_and_cookie_import_success_flows_do_not_emit_cross_page_toasts_after_leaving_accounts(self):
        manual_success_body = _extract_function_body(self.app_js, "handleManualCookieImportSuccess")
        password_success_body = _extract_function_body(self.app_js, "handlePasswordLoginSuccess")
        refresh_poll_body = _extract_function_body(self.app_js, "startRefreshCookiePolling")

        for body, toast_fragment in (
            (manual_success_body, "showToast(`账号 ${data.account_id} 导入并验证成功`, 'success');"),
            (password_success_body, "showToast(`账号 ${data.account_id} 登录成功！`, 'success');"),
            (refresh_poll_body, "showToast(`账号 ${accountId} Cookie刷新成功！`, 'success');"),
            (refresh_poll_body, "showToast(`刷新失败: ${data.message || data.error || '未知错误'}`, 'danger');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    "都切出账号页了，旧的登录/导入/刷新 Cookie 结果就别再跨页弹 toast 了",
                )

        wait_index = refresh_poll_body.index("await new Promise(resolve => setTimeout(resolve, 400));")
        self.assertIn("refreshCookiePollingState.sessionId !== sessionId", refresh_poll_body)
        post_wait_block = refresh_poll_body[wait_index:]
        self.assertIn("refreshCookiePollingState.sessionId !== sessionId", post_wait_block)
        self.assertLess(
            post_wait_block.index("refreshCookiePollingState.sessionId !== sessionId"),
            post_wait_block.index("closePasswordLoginQRModal();"),
            "旧的刷新Cookie成功回调在 400ms 等待后也得先确认当前还是同一会话，别回来把新弹窗关掉",
        )

    def test_account_login_and_cookie_import_failure_flows_do_not_emit_cross_page_toasts_after_leaving_accounts(self):
        manual_failure_body = _extract_function_body(self.app_js, "handleManualCookieImportFailure")
        password_failure_body = _extract_function_body(self.app_js, "handlePasswordLoginFailure")
        manual_poll_body = _extract_function_body(self.app_js, "checkManualCookieImportStatus")
        password_poll_body = _extract_function_body(self.app_js, "checkPasswordLoginStatus")
        refresh_poll_body = _extract_function_body(self.app_js, "startRefreshCookiePolling")

        for body, toast_fragment in (
            (manual_failure_body, "showToast(data.message || data.error || 'Cookie 导入验证失败', 'danger');"),
            (password_failure_body, "showToast(errorMessage, 'danger');"),
            (manual_poll_body, "showToast(data.message || 'Cookie 导入验证检查失败', 'danger');"),
            (manual_poll_body, "showToast(errorMessage, 'danger');"),
            (manual_poll_body, "showToast('网络错误，请重试', 'danger');"),
            (password_poll_body, "showToast(data.message || '登录已取消', 'info');"),
            (password_poll_body, "showToast(data.message || '登录检查失败', 'danger');"),
            (password_poll_body, "showToast(errorMessage || '登录检查失败', 'danger');"),
            (password_poll_body, "showToast('网络错误，请重试', 'danger');"),
            (refresh_poll_body, "showToast('刷新Cookie超时，请重试', 'warning');"),
            (refresh_poll_body, "showToast(data.message || '刷新Cookie已取消', 'info');"),
            (refresh_poll_body, "showToast(`刷新失败: ${data.message || data.error || '未知错误'}`, 'danger');"),
            (refresh_poll_body, "showToast(errorMessage || '刷新检查失败', 'danger');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    "都切出账号页了，旧的登录/导入/刷新 Cookie 失败结果就别再跨页弹 toast 了",
                )

    def test_account_login_submit_flows_do_not_start_or_report_after_leaving_accounts(self):
        manual_body = _extract_function_body(self.app_js, "handleManualCookieImport")
        password_body = _extract_function_body(self.app_js, "handlePasswordLogin")
        refresh_body = _extract_function_body(self.app_js, "handleRefreshCookie")

        self.assertIn("const submitRequestSequence = ++manualCookieImportSubmitRequestSequence;", manual_body)
        self.assertIn("submitRequestSequence !== manualCookieImportSubmitRequestSequence", manual_body)
        self.assertLess(
            manual_body.index("submitRequestSequence !== manualCookieImportSubmitRequestSequence"),
            manual_body.index("manualCookieImportSessionId = data.session_id;"),
            "手动导入旧启动响应不该在同页重开表单后还回来偷偷启动旧轮询",
        )

        self.assertIn("const submitRequestSequence = ++passwordLoginSubmitRequestSequence;", password_body)
        self.assertIn("submitRequestSequence !== passwordLoginSubmitRequestSequence", password_body)
        self.assertLess(
            password_body.index("submitRequestSequence !== passwordLoginSubmitRequestSequence"),
            password_body.index("passwordLoginSessionId = data.session_id;"),
            "密码登录旧启动响应不该在同页重开表单后还回来偷偷启动旧轮询",
        )

        self.assertIn("const submitRequestSequence = ++refreshCookieSubmitRequestSequence;", refresh_body)
        self.assertIn("submitRequestSequence !== refreshCookieSubmitRequestSequence", refresh_body)
        self.assertLess(
            refresh_body.index("submitRequestSequence !== refreshCookieSubmitRequestSequence"),
            refresh_body.index("startRefreshCookiePolling(data.session_id, accountId);"),
            "刷新Cookie旧启动响应不该在同页重开表单后还回来偷偷启动旧轮询",
        )

        for body, toast_fragment in (
            (manual_body, "showToast(errorMessage || 'Cookie 导入验证失败', 'danger');"),
            (manual_body, "showToast(data.message || 'Cookie 导入验证失败', 'danger');"),
            (manual_body, "showToast('网络错误，请重试', 'danger');"),
            (password_body, "showToast(errorMessage || '登录失败，请检查账号密码是否正确', 'danger');"),
            (password_body, "showToast(data.message || '登录失败，请检查账号密码是否正确', 'danger');"),
            (password_body, "showToast('网络错误，请重试', 'danger');"),
            (refresh_body, "showToast('正在验证账号并刷新Cookie，请稍候...', 'info');"),
            (refresh_body, "showToast(errorMessage || '启动刷新失败', 'danger');"),
            (refresh_body, "showToast(data.message || '启动刷新失败', 'danger');"),
            (refresh_body, "showToast('刷新Cookie失败: ' + error.message, 'danger');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    "都切出账号页了，旧的表单提交结果就别再跨页弹 toast 了",
                )

        self.assertLess(
            refresh_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, refresh_body.index("startRefreshCookiePolling(data.session_id, accountId);")),
            refresh_body.index("startRefreshCookiePolling(data.session_id, accountId);"),
            "都切出账号页了，旧的刷新 Cookie 启动响应不该再继续开启轮询",
        )

        self.assertIn("document.getElementById('manualInputForm')?.style.display === 'none'", manual_body)
        self.assertLess(
            manual_body.index("document.getElementById('manualInputForm')?.style.display === 'none'"),
            manual_body.index("manualCookieImportSessionId = data.session_id;"),
            "手动导入表单都切走了，旧响应不该再偷偷启动导入验证轮询",
        )
        self.assertLess(
            manual_body.index("document.getElementById('manualInputForm')?.style.display === 'none'"),
            manual_body.index("showToast(data.message || 'Cookie 导入验证失败', 'danger');"),
            "手动导入表单都切走了，旧失败响应也别再对当前页甩 danger toast",
        )

        self.assertIn("document.getElementById('passwordLoginForm')?.style.display === 'none'", password_body)
        self.assertLess(
            password_body.index("document.getElementById('passwordLoginForm')?.style.display === 'none'"),
            password_body.index("passwordLoginSessionId = data.session_id;"),
            "密码登录表单都切走了，旧响应不该再偷偷启动密码登录轮询",
        )
        self.assertLess(
            password_body.index("document.getElementById('passwordLoginForm')?.style.display === 'none'"),
            password_body.index("showToast(data.message || '登录失败，请检查账号密码是否正确', 'danger');"),
            "密码登录表单都切走了，旧失败响应也别再对当前页甩 danger toast",
        )

        self.assertIn("document.getElementById('refreshCookieForm')?.style.display === 'none'", refresh_body)
        self.assertLess(
            refresh_body.index("document.getElementById('refreshCookieForm')?.style.display === 'none'"),
            refresh_body.index("showToast('正在验证账号并刷新Cookie，请稍候...', 'info');"),
            "刷新Cookie表单都切走了，旧启动响应不该再回来弹提示并开启后续轮询",
        )
        self.assertLess(
            refresh_body.index("document.getElementById('refreshCookieForm')?.style.display === 'none'"),
            refresh_body.index("showToast(data.message || '启动刷新失败', 'danger');"),
            "刷新Cookie表单都切走了，旧失败响应也别再对当前页甩 danger toast",
        )

    def test_account_verification_cancel_callbacks_do_not_emit_cross_page_toasts_after_leaving_accounts(self):
        manual_cancel_body = _extract_function_body(self.app_js, "cancelManualCookieImportSession")
        password_cancel_body = _extract_function_body(self.app_js, "cancelPasswordLoginSession")

        self.assertIn("fetch(`${apiBase}/manual-cookie-import/cancel/${encodeURIComponent(sessionId)}`", manual_cancel_body)
        self.assertIn("fetch(`${apiBase}/password-login/cancel/${encodeURIComponent(sessionId)}`", password_cancel_body)
        self.assertNotIn("fetch(`${apiBase}/password-login/cancel/${sessionId}`", password_cancel_body)

        for body, toast_fragment in (
            (manual_cancel_body, "showToast(errorMessage || '已停止当前导入验证流程', 'warning');"),
            (manual_cancel_body, "showToast(data.message || '已停止当前导入验证流程', 'warning');"),
            (manual_cancel_body, "showToast(data.message || '已停止当前导入验证流程', 'info');"),
            (manual_cancel_body, "showToast('已停止当前导入验证流程，请稍后重试', 'warning');"),
            (password_cancel_body, "showToast(errorMessage || `已停止当前${flowLabel}轮询`, 'warning');"),
            (password_cancel_body, "showToast(data.message || `已停止当前${flowLabel}轮询`, 'warning');"),
            (password_cancel_body, "showToast(data.message || `${flowLabel}已取消`, 'info');"),
            (password_cancel_body, "showToast(`已停止当前${flowLabel}轮询，请稍后重试`, 'warning');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    "都切出账号页了，旧的取消会话结果就别再跨页弹 toast 了",
                )

    def test_account_raw_fetch_flows_handle_unauthorized_before_followup_work(self):
        manual_body = _extract_function_body(self.app_js, "handleManualCookieImport")
        manual_poll_body = _extract_function_body(self.app_js, "checkManualCookieImportStatus")
        load_refresh_body = _extract_function_body(self.app_js, "loadRefreshCookieAccountList")
        refresh_body = _extract_function_body(self.app_js, "handleRefreshCookie")
        refresh_poll_body = _extract_function_body(self.app_js, "startRefreshCookiePolling")
        password_body = _extract_function_body(self.app_js, "handlePasswordLogin")
        password_poll_body = _extract_function_body(self.app_js, "checkPasswordLoginStatus")
        manual_cancel_body = _extract_function_body(self.app_js, "cancelManualCookieImportSession")
        password_cancel_body = _extract_function_body(self.app_js, "cancelPasswordLoginSession")

        for body, unauthorized_fragment, anchor_fragment in (
            (manual_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (manual_poll_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (load_refresh_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (refresh_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (refresh_poll_body, "if (handleUnauthorizedApiResponse(response)) {", "const data = await response.json();"),
            (password_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (password_poll_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (manual_cancel_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (password_cancel_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "账号管理这几条 raw fetch 长链路碰到 401 得先滚去登录，别后面还继续开轮询、读错误体、弹业务 toast",
                )

    def test_account_failure_flows_read_structured_error_messages(self):
        manual_body = _extract_function_body(self.app_js, "handleManualCookieImport")
        manual_poll_body = _extract_function_body(self.app_js, "checkManualCookieImportStatus")
        load_refresh_body = _extract_function_body(self.app_js, "loadRefreshCookieAccountList")
        refresh_body = _extract_function_body(self.app_js, "handleRefreshCookie")
        refresh_poll_body = _extract_function_body(self.app_js, "startRefreshCookiePolling")
        password_body = _extract_function_body(self.app_js, "handlePasswordLogin")
        password_poll_body = _extract_function_body(self.app_js, "checkPasswordLoginStatus")
        manual_cancel_body = _extract_function_body(self.app_js, "cancelManualCookieImportSession")
        password_cancel_body = _extract_function_body(self.app_js, "cancelPasswordLoginSession")

        for body, error_fragment, toast_fragment, label in (
            (manual_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || 'Cookie 导入验证失败', 'danger');", "手动导入 Cookie 启动"),
            (manual_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage, 'danger');", "手动导入 Cookie 轮询"),
            (refresh_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || '启动刷新失败', 'danger');", "刷新 Cookie 启动"),
            (refresh_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || '刷新检查失败', 'danger');", "刷新 Cookie 轮询"),
            (password_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || '登录失败，请检查账号密码是否正确', 'danger');", "账号密码登录启动"),
            (password_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || '登录检查失败', 'danger');", "账号密码登录轮询"),
            (manual_cancel_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || '已停止当前导入验证流程', 'warning');", "手动导入 Cookie 取消"),
            (password_cancel_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || `已停止当前${flowLabel}轮询`, 'warning');", "账号登录/刷新 Cookie 取消"),
        ):
            with self.subTest(label=label):
                self.assertIn(error_fragment, body)
                self.assertLess(
                    body.index(error_fragment),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再拿裸 json/catch 分叉瞎糊用户",
                )

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_refresh_body)
        self.assertIn("throw new Error(errorMessage);", load_refresh_body)
        self.assertIn("showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');", load_refresh_body)
        self.assertLess(
            load_refresh_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            load_refresh_body.index("throw new Error(errorMessage);"),
            "刷新 Cookie 账号下拉接口失败时先把 detail/message 解出来，再往 catch 里抛，别拿固定死文案糊弄人",
        )

        self.assertNotIn("const errorData = await response.json();", password_poll_body)
        self.assertNotIn("showToast('登录检查失败，请重试', 'danger');", password_poll_body)

    def test_account_failure_toasts_recheck_state_after_error_body_read(self):
        manual_body = _extract_function_body(self.app_js, "handleManualCookieImport")
        manual_poll_body = _extract_function_body(self.app_js, "checkManualCookieImportStatus")
        load_refresh_body = _extract_function_body(self.app_js, "loadRefreshCookieAccountList")
        refresh_body = _extract_function_body(self.app_js, "handleRefreshCookie")
        refresh_poll_body = _extract_function_body(self.app_js, "startRefreshCookiePolling")
        password_body = _extract_function_body(self.app_js, "handlePasswordLogin")
        password_poll_body = _extract_function_body(self.app_js, "checkPasswordLoginStatus")
        manual_cancel_body = _extract_function_body(self.app_js, "cancelManualCookieImportSession")
        password_cancel_body = _extract_function_body(self.app_js, "cancelPasswordLoginSession")

        for body, error_fragment, guard_fragment, toast_fragment, label in (
            (manual_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "submitRequestSequence !== manualCookieImportSubmitRequestSequence", "showToast(errorMessage || 'Cookie 导入验证失败', 'danger');", "手动导入 Cookie 启动 stale guard"),
            (manual_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "document.getElementById('manualInputForm')?.style.display === 'none'", "showToast(errorMessage || 'Cookie 导入验证失败', 'danger');", "手动导入 Cookie 启动 hidden guard"),
            (manual_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "manualCookieImportPollingState.sessionId !== sessionId", "showToast(errorMessage, 'danger');", "手动导入 Cookie 轮询 session guard"),
            (manual_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "!document.getElementById('accounts-section')?.classList.contains('active')", "showToast(errorMessage, 'danger');", "手动导入 Cookie 轮询 active guard"),
            (refresh_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "submitRequestSequence !== refreshCookieSubmitRequestSequence", "toggleLoading(false);", "刷新 Cookie 启动 stale guard"),
            (refresh_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "document.getElementById('refreshCookieForm')?.style.display === 'none'", "showToast(errorMessage || '启动刷新失败', 'danger');", "刷新 Cookie 启动 hidden guard"),
            (refresh_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "refreshCookiePollingState.sessionId !== sessionId", "showToast(errorMessage || '刷新检查失败', 'danger');", "刷新 Cookie 轮询 session guard"),
            (refresh_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "!document.getElementById('accounts-section')?.classList.contains('active')", "showToast(errorMessage || '刷新检查失败', 'danger');", "刷新 Cookie 轮询 active guard"),
            (password_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "submitRequestSequence !== passwordLoginSubmitRequestSequence", "showToast(errorMessage || '登录失败，请检查账号密码是否正确', 'danger');", "账号密码登录启动 stale guard"),
            (password_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "document.getElementById('passwordLoginForm')?.style.display === 'none'", "showToast(errorMessage || '登录失败，请检查账号密码是否正确', 'danger');", "账号密码登录启动 hidden guard"),
            (password_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "passwordLoginPollingState.sessionId !== sessionId", "showToast(errorMessage || '登录检查失败', 'danger');", "账号密码登录轮询 session guard"),
            (password_poll_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "!document.getElementById('accounts-section')?.classList.contains('active')", "showToast(errorMessage || '登录检查失败', 'danger');", "账号密码登录轮询 active guard"),
            (manual_cancel_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "!document.getElementById('accounts-section')?.classList.contains('active')", "showToast(errorMessage || '已停止当前导入验证流程', 'warning');", "手动导入 Cookie 取消 active guard"),
            (password_cancel_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "!document.getElementById('accounts-section')?.classList.contains('active')", "showToast(errorMessage || `已停止当前${flowLabel}轮询`, 'warning');", "账号登录/刷新 Cookie 取消 active guard"),
        ):
            with self.subTest(label=label):
                error_index = body.index(error_fragment)
                toast_index = body.index(toast_fragment, error_index)
                self.assertLess(
                    body.find(guard_fragment, error_index),
                    toast_index,
                    f"{label}没在读完错误体后复验当前状态，旧响应回来就容易乱弹 toast / 乱收当前 UI",
                )

        load_refresh_error_index = load_refresh_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        load_refresh_stale_index = load_refresh_body.index("requestSequence !== refreshCookieAccountListRequestSequence", load_refresh_error_index)
        load_refresh_throw_index = load_refresh_body.index("throw new Error(errorMessage);", load_refresh_error_index)
        load_refresh_toast_index = load_refresh_body.index("showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            load_refresh_error_index,
            load_refresh_stale_index,
            "刷新 Cookie 账号下拉接口失败时先把错误体读出来，别还没看见 detail 就先让 stale guard 把线索闷死",
        )
        self.assertLess(
            load_refresh_stale_index,
            load_refresh_throw_index,
            "刷新 Cookie 账号下拉错误体读完后也得先验请求活性，别旧错误晚回来污染当前下拉框会话",
        )
        self.assertLess(
            load_refresh_body.find("refreshForm && refreshForm.style.display === 'none'", load_refresh_error_index),
            load_refresh_toast_index,
            "刷新 Cookie 账号下拉都藏起来了，旧失败响应读完错误体也别再回来跨表单甩红字",
        )

    def test_account_face_verification_loader_ignores_stale_async_responses_and_hidden_accounts_state(self):
        self.assertIn("let faceVerificationRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        self.assertIn("faceVerificationRequestSequence += 1;", show_section_body)

        body = _extract_function_body(self.app_js, "showFaceVerification")
        self.assertIn("const requestSequence = ++faceVerificationRequestSequence;", body)
        self.assertIn("if (!document.getElementById('accounts-section')?.classList.contains('active')) {", body)
        self.assertIn("if (requestSequence !== faceVerificationRequestSequence) {", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("if (requestSequence !== faceVerificationRequestSequence) {"),
            body.index("showAccountFaceVerificationModal(accountId, data.screenshot);"),
            "旧的验证截图响应不该晚回来后把当前弹窗内容给顶掉",
        )
        self.assertLess(
            body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index("showToast(data.message || '未找到验证截图', 'warning');")),
            body.index("showToast(data.message || '未找到验证截图', 'warning');"),
            "都切出账号页了，旧的验证截图空结果就别再跨页弹 warning 了",
        )
        self.assertLess(
            body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index("showToast('获取验证截图失败: ' + error.message, 'danger');")),
            body.index("showToast('获取验证截图失败: ' + error.message, 'danger');"),
            "都切出账号页了，旧的验证截图失败结果就别再跨页弹 danger 了",
        )

    def test_account_face_verification_and_inline_edit_raw_fetch_flows_handle_unauthorized_before_followup_work(self):
        face_body = _extract_function_body(self.app_js, "showFaceVerification")
        edit_remark_body = _extract_function_body(self.app_js, "editRemark")
        edit_pause_body = _extract_function_body(self.app_js, "editPauseDuration")

        for body, anchor_fragment, label in (
            (face_body, "if (!response.ok) {", "验证截图"),
            (edit_remark_body, "if (response.ok) {", "账号备注"),
            (edit_pause_body, "if (response.ok) {", "暂停时间"),
        ):
            with self.subTest(label=label):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{label}这条 raw fetch 遇到 401 得先滚去登录，别后面还继续读业务错误、改 DOM、弹 toast",
                )

    def test_account_face_verification_and_inline_edit_failures_read_structured_error_messages(self):
        face_body = _extract_function_body(self.app_js, "showFaceVerification")
        edit_remark_body = _extract_function_body(self.app_js, "editRemark")
        edit_pause_body = _extract_function_body(self.app_js, "editPauseDuration")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", face_body)
        self.assertIn("throw new Error(errorMessage);", face_body)
        self.assertNotIn("throw new Error('获取验证截图失败');", face_body)
        self.assertLess(
            face_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            face_body.index("throw new Error(errorMessage);"),
            "验证截图失败时得先把 detail/message 解出来，再决定怎么往 catch 里抛，别拿死板常量把后端错误全吃没了",
        )

        for body, toast_fragment, label in (
            (edit_remark_body, "showToast(`备注更新失败: ${errorMessage}`, 'danger');", "账号备注"),
            (edit_pause_body, "showToast(`暂停时间更新失败: ${errorMessage}`, 'danger');", "暂停时间"),
        ):
            with self.subTest(label=label):
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再拿裸 json 细节字段自己拼一套土味分支",
                )
                self.assertNotIn("const errorData = await response.json();", body)

    def test_account_face_verification_and_inline_edit_failures_recheck_state_after_error_body_read(self):
        face_body = _extract_function_body(self.app_js, "showFaceVerification")
        edit_remark_body = _extract_function_body(self.app_js, "editRemark")
        edit_pause_body = _extract_function_body(self.app_js, "editPauseDuration")

        face_error_index = face_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        face_throw_index = face_body.index("throw new Error(errorMessage);", face_error_index)
        self.assertLess(
            face_body.find("if (!document.getElementById('accounts-section')?.classList.contains('active')) {", face_error_index),
            face_throw_index,
            "验证截图失败读完错误体后还得先看账号页是不是已经切走了，别旧请求回魂后继续往 catch 里扔锅",
        )
        self.assertLess(
            face_body.find("if (requestSequence !== faceVerificationRequestSequence) {", face_error_index),
            face_throw_index,
            "验证截图失败读完错误体后还得先验 requestSequence，别旧请求晚回来后继续污染当前弹窗状态",
        )

        for body, toast_fragment, label in (
            (edit_remark_body, "showToast(`备注更新失败: ${errorMessage}`, 'danger');", "账号备注"),
            (edit_pause_body, "showToast(`暂停时间更新失败: ${errorMessage}`, 'danger');", "暂停时间"),
        ):
            with self.subTest(label=label):
                error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                toast_index = body.index(toast_fragment, error_index)
                self.assertLess(
                    body.find("if (!document.getElementById('accounts-section')?.classList.contains('active')) {", error_index),
                    toast_index,
                    f"{label}读完错误体后还得再验一次页面状态，别都切页了还拿旧失败响应乱弹 toast",
                )

    def test_refresh_cookie_account_selector_loader_ignores_stale_async_responses_and_hidden_form_state(self):
        self.assertIn("let refreshCookieAccountListRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        toggle_body = _extract_function_body(self.app_js, "toggleRefreshCookieForm")
        load_body = _extract_function_body(self.app_js, "loadRefreshCookieAccountList")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("refreshCookieAccountListRequestSequence += 1;", show_section_body)
        self.assertIn("refreshCookieAccountListRequestSequence += 1;", toggle_body)

        self.assertIn("const requestSequence = ++refreshCookieAccountListRequestSequence;", load_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        self.assertIn("requestSequence !== refreshCookieAccountListRequestSequence", load_body)
        self.assertIn("refreshForm && refreshForm.style.display === 'none'", load_body)
        self.assertIn("return null;", load_body)
        self.assertIn("`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`", load_body)
        self.assertLess(
            load_body.index("requestSequence !== refreshCookieAccountListRequestSequence"),
            load_body.index("select.appendChild(option);"),
            "旧的刷新Cookie账号列表请求不该晚回来后把当前下拉框再糊回旧账号选项",
        )
        error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        stale_index = load_body.index("requestSequence !== refreshCookieAccountListRequestSequence", error_index)
        toast_index = load_body.index("showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            error_index,
            stale_index,
            "刷新Cookie账号下拉 HTTP 挂了时先把后端错误体读出来，别消息还没看见就先让 stale guard 抢跑",
        )
        self.assertLess(
            stale_index,
            toast_index,
            "刷新Cookie账号下拉错误体读完后也得先复验请求/表单状态，别旧错误跨会话回来弹 toast",
        )

    def test_account_form_toggles_cancel_inflight_sessions_when_switching_modes(self):
        self.assertIn("function stopActiveManualCookieImportSession() {", self.app_js)
        self.assertIn("function stopActivePasswordLoginSession() {", self.app_js)
        self.assertIn("function stopActiveRefreshCookieSession() {", self.app_js)
        self.assertIn("let manualCookieImportSubmitRequestSequence = 0;", self.app_js)
        self.assertIn("let passwordLoginSubmitRequestSequence = 0;", self.app_js)
        self.assertIn("let refreshCookieSubmitRequestSequence = 0;", self.app_js)

        manual_stop_body = _extract_function_body(self.app_js, "stopActiveManualCookieImportSession")
        password_stop_body = _extract_function_body(self.app_js, "stopActivePasswordLoginSession")
        refresh_stop_body = _extract_function_body(self.app_js, "stopActiveRefreshCookieSession")

        self.assertIn("manualCookieImportSubmitRequestSequence += 1;", manual_stop_body)
        self.assertIn("passwordLoginSubmitRequestSequence += 1;", password_stop_body)
        self.assertIn("refreshCookieSubmitRequestSequence += 1;", refresh_stop_body)
        self.assertIn("void cancelManualCookieImportSession(activeSessionId);", manual_stop_body)
        self.assertIn("void cancelPasswordLoginSession(activeSessionId, '登录');", password_stop_body)
        self.assertIn("stopRefreshCookiePolling(activeSessionId);", refresh_stop_body)
        self.assertIn("void cancelPasswordLoginSession(activeSessionId, '刷新Cookie');", refresh_stop_body)
        self.assertIn("toggleLoading(false);", refresh_stop_body)

        toggle_manual_body = _extract_function_body(self.app_js, "toggleManualInput")
        toggle_password_body = _extract_function_body(self.app_js, "togglePasswordLogin")
        toggle_refresh_body = _extract_function_body(self.app_js, "toggleRefreshCookieForm")

        for body, helper_calls in (
            (toggle_manual_body, ("stopActivePasswordLoginSession();", "stopActiveRefreshCookieSession();", "stopActiveManualCookieImportSession();")),
            (toggle_password_body, ("stopActiveManualCookieImportSession();", "stopActiveRefreshCookieSession();", "stopActivePasswordLoginSession();")),
            (toggle_refresh_body, ("stopActiveManualCookieImportSession();", "stopActivePasswordLoginSession();", "stopActiveRefreshCookieSession();")),
        ):
            for helper_call in helper_calls:
                with self.subTest(helper_call=helper_call):
                    self.assertIn(helper_call, body)

    def test_accounts_section_switch_cancels_inflight_login_forms_and_closes_verification_modal(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("stopActiveManualCookieImportSession();", show_section_body)
        self.assertIn("stopActivePasswordLoginSession();", show_section_body)
        self.assertIn("stopActiveRefreshCookieSession();", show_section_body)
        self.assertIn("closePasswordLoginQRModal();", show_section_body)
        self.assertLess(
            show_section_body.index("stopActiveManualCookieImportSession();"),
            show_section_body.index("closePasswordLoginQRModal();"),
            "切出账号页时应先废掉旧登录/导入/刷新会话，再把验证弹窗收口，别留着半截状态诈尸",
        )

    def test_switching_away_from_accounts_closes_account_management_modals(self):
        body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("if (sectionName !== 'accounts') {", body)
        self.assertIn("closeQRCodeLoginModal(0);", body)
        self.assertIn("const accountEditModalElement = document.getElementById('accountEditModal');", body)
        self.assertIn("const defaultReplyModalElement = document.getElementById('defaultReplyModal');", body)
        self.assertIn("const editDefaultReplyModalElement = document.getElementById('editDefaultReplyModal');", body)
        self.assertIn("const aiReplyConfigModalElement = document.getElementById('aiReplyConfigModal');", body)
        self.assertIn("const commentTemplatesModalElement = document.getElementById('commentTemplatesModal');", body)
        self.assertIn("commentTemplatesModal.hide();", body)
        self.assertIn("closePolishScheduleModal();", body)
        self.assertLess(
            body.index("closePasswordLoginQRModal();"),
            body.index("closePolishScheduleModal();"),
            "账号页都切走了，定时擦亮这些弹窗还在那赖着不走，纯属给后面页面添堵",
        )
        self.assertLess(
            body.index("const accountEditModalElement = document.getElementById('accountEditModal');"),
            body.index("closePolishScheduleModal();"),
            "切出账号页时账号编辑、默认回复、AI 配置这些框都得先收口，别让遮罩跨页串场",
        )
        self.assertLess(
            body.index("const commentTemplatesModalElement = document.getElementById('commentTemplatesModal');"),
            body.index("closePolishScheduleModal();"),
            "切出账号页时动态插到 body 里的好评模板框也得收口，不然跨页赖着不走纯恶心人",
        )

    def test_refresh_cookie_startup_stale_responses_do_not_clear_newer_loading_state(self):
        body = _extract_function_body(self.app_js, "handleRefreshCookie")
        stale_guards = list(re.finditer(r"if \(submitRequestSequence !== refreshCookieSubmitRequestSequence\) \{", body))
        self.assertGreaterEqual(len(stale_guards), 3)

        for match in stale_guards[:3]:
            stale_branch = body[match.start():body.index("return null;", match.start())]
            self.assertNotIn(
                "toggleLoading(false);",
                stale_branch,
                "刷新Cookie旧启动响应已经 stale 了，就别顺手把当前更新会话的 loading 给关掉",
            )

    def test_cards_table_escapes_name_description_and_multi_spec_fields(self):
        body = _extract_function_body(self.app_js, "renderCardsList")
        self.assertIn("const safeCardName = escapeHtml(card.name || '');", body)
        self.assertIn("const safeCardDescription = escapeHtml(card.description || '');", body)
        self.assertIn("const safeSpecName = escapeHtml(card.spec_name || '');", body)
        self.assertIn("const safeSpecValue = escapeHtml(card.spec_value || '');", body)
        self.assertIn("const safeSpecName2 = escapeHtml(card.spec_name_2 || '');", body)
        self.assertIn("const safeSpecValue2 = escapeHtml(card.spec_value_2 || '');", body)
        self.assertIn('<div class="fw-bold">${safeCardName}</div>', body)
        self.assertIn('${card.description ? `<small class="text-muted">${safeCardDescription}</small>` : \'\'}', body)
        self.assertIn("let specInfo = `${safeSpecName}: ${safeSpecValue}`;", body)
        self.assertIn("specInfo += `<br>${safeSpecName2}: ${safeSpecValue2}`;", body)
        self.assertNotIn("${card.name}</div>", body)
        self.assertNotIn("${card.description}</small>", body)
        self.assertNotIn("${card.spec_name}: ${card.spec_value}", body)

    def test_cards_table_prefers_backend_data_count_without_requiring_full_data_payload(self):
        body = _extract_function_body(self.app_js, "renderCardsList")
        self.assertIn("if (card.type === 'data' && Number.isFinite(Number(card.data_count))) {", body)
        self.assertIn("dataCount = Number(card.data_count);", body)
        self.assertIn("} else if (card.type === 'data' && card.data_content) {", body)
        self.assertLess(
            body.index("if (card.type === 'data' && Number.isFinite(Number(card.data_count))) {"),
            body.index("} else if (card.type === 'data' && card.data_content) {"),
            "卡券列表既然已经拿到了后端汇总的数据量，就别还强依赖整包 data_content 才能显示数量，白白把大 payload 往前端拖",
        )

    def test_delivery_rules_table_escapes_keyword_description_card_name_and_spec_fields(self):
        body = _extract_function_body(self.app_js, "renderDeliveryRulesList")
        self.assertIn("const safeKeyword = escapeHtml(rule.keyword || '');", body)
        self.assertIn("const safeDescription = escapeHtml(rule.description || '');", body)
        self.assertIn("const safeCardName = escapeHtml(rule.card_name || '未知卡券');", body)
        self.assertIn("const safeSpecName = escapeHtml(rule.spec_name || '');", body)
        self.assertIn("const safeSpecValue = escapeHtml(rule.spec_value || '');", body)
        self.assertIn("const safeSpecName2 = escapeHtml(rule.spec_name_2 || '');", body)
        self.assertIn("const safeSpecValue2 = escapeHtml(rule.spec_value_2 || '');", body)
        self.assertIn('<div class="fw-bold">${safeKeyword}</div>', body)
        self.assertIn('${rule.description ? `<small class="text-muted">${safeDescription}</small>` : \'\'}', body)
        self.assertIn('<span class="badge bg-primary">${safeCardName}</span>', body)
        self.assertIn('${safeSpecName}: ${safeSpecValue}', body)
        self.assertIn('${safeSpecName2}: ${safeSpecValue2}', body)
        self.assertNotIn("${rule.keyword}</div>", body)
        self.assertNotIn("${rule.description}</small>", body)
        self.assertNotIn("${rule.card_name || '未知卡券'}", body)
        self.assertNotIn("${rule.spec_name}: ${rule.spec_value}", body)

    def test_delivery_rules_table_marks_enabled_rule_with_disabled_card_as_unavailable(self):
        body = _extract_function_body(self.app_js, "renderDeliveryRulesList")
        self.assertIn("const effectiveCardEnabled = rule.card_enabled !== false && Boolean(rule.card_name);", body)
        self.assertIn("const effectiveRuleEnabled = Boolean(rule.enabled) && effectiveCardEnabled;", body)
        self.assertIn("rule.enabled", body)
        self.assertIn("卡券不可用", body)
        self.assertLess(
            body.index("const effectiveRuleEnabled = Boolean(rule.enabled) && effectiveCardEnabled;"),
            body.index("const statusBadge = effectiveRuleEnabled ?"),
            "发货规则当前如果还是启用的，但关联卡券已经禁用/删除了，就别继续亮个绿色启用徽章骗人",
        )

    def test_delivery_rules_table_marks_enabled_rule_with_missing_card_as_unavailable(self):
        body = _extract_function_body(self.app_js, "renderDeliveryRulesList")
        self.assertIn("Boolean(rule.card_name)", body)
        self.assertLess(
            body.index("Boolean(rule.card_name)"),
            body.index("const statusBadge = effectiveRuleEnabled ?"),
            "发货规则关联卡券如果已经被删了，也得算不可用，别光盯着 enabled 字段演绿灯",
        )

    def test_delivery_rule_stats_exclude_rules_whose_cards_are_unavailable(self):
        body = _extract_function_body(self.app_js, "updateDeliveryStats")
        self.assertIn("const activeRules = rules.filter(rule => Boolean(rule.enabled) && rule.card_enabled !== false && Boolean(rule.card_name)).length;", body)
        self.assertNotIn("const activeRules = rules.filter(rule => Boolean(rule.enabled) && rule.card_enabled !== false).length;", body)
        self.assertNotIn("const activeRules = rules.filter(rule => rule.enabled).length;", body)

    def test_edit_delivery_rule_keeps_current_disabled_card_available_in_edit_selector(self):
        edit_rule_body = _extract_function_body(self.app_js, "editDeliveryRule")
        self.assertIn("const cardOptionsLoaded = await loadCardsForEditSelect(rule, requestSequence);", edit_rule_body)
        self.assertNotIn("await loadCardsForEditSelect();", edit_rule_body)

        edit_select_body = _extract_function_body(self.app_js, "loadCardsForEditSelect")
        self.assertIn("async function loadCardsForEditSelect(selectedCard = null, requestSequence = 0) {", self.app_js)
        self.assertIn("const isSelectedDisabledCard = Boolean(", edit_select_body)
        self.assertIn("card.enabled || isSelectedDisabledCard", edit_select_body)
        self.assertIn("case 'yifan_api':", edit_select_body)
        self.assertIn("typeText = '亦凡卡劵API';", edit_select_body)
        self.assertIn("if (isSelectedDisabledCard) {", edit_select_body)
        self.assertIn("displayText += ' [已禁用但当前规则仍在使用]';", edit_select_body)

    def test_edit_delivery_rule_keeps_current_missing_card_available_in_edit_selector(self):
        edit_select_body = _extract_function_body(self.app_js, "loadCardsForEditSelect")
        self.assertIn("const currentCardId = String(selectedCard?.card_id || selectedCard?.id || '').trim();", edit_select_body)
        self.assertIn("const hasCurrentCardOption = currentCardId", edit_select_body)
        self.assertIn("option.textContent = `${currentCardName} [卡券不存在但当前规则仍在使用]`;", edit_select_body)
        self.assertLess(
            edit_select_body.index("const hasCurrentCardOption = currentCardId"),
            edit_select_body.index("if (appendedCount === 0 && select) {"),
            "发货规则当前引用的卡券如果已经被删了，编辑弹窗也得先补个占位选项，别直接把旧规则的卡券关系抹成空气",
        )

    def test_delivery_rule_card_select_loaders_clear_stale_options_and_label_yifan_api_consistently(self):
        add_select_body = _extract_function_body(self.app_js, "loadCardsForSelect")
        edit_select_body = _extract_function_body(self.app_js, "loadCardsForEditSelect")

        for body in (add_select_body, edit_select_body):
            with self.subTest(function_body="loadCardsForSelect" if body is add_select_body else "loadCardsForEditSelect"):
                self.assertIn("const select = document.getElementById(", body)
                self.assertIn("select.innerHTML = '<option value=\"\">请选择卡券</option>';", body)
                self.assertIn("case 'yifan_api':", body)
                self.assertIn("typeText = '亦凡卡劵API';", body)
                self.assertIn("showToast(`加载卡券选项失败: ${error.message || '请稍后重试'}`, 'warning');", body)

    def test_delivery_rule_card_select_loaders_treat_http_failures_as_real_failures(self):
        add_select_body = _extract_function_body(self.app_js, "loadCardsForSelect")
        edit_select_body = _extract_function_body(self.app_js, "loadCardsForEditSelect")

        for body, json_fragment in (
            (add_select_body, "const cards = await response.json();"),
            (edit_select_body, "const cards = await response.json();"),
        ):
            with self.subTest(function_body="loadCardsForSelect" if body is add_select_body else "loadCardsForEditSelect"):
                self.assertIn("if (!response.ok) {", body)
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertIn("throw new Error(errorMessage);", body)
                self.assertLess(
                    body.index("if (!response.ok) {"),
                    body.index(json_fragment),
                    "卡券下拉接口都 HTTP 挂了，就别再装作没事去读 JSON 了",
                )
                self.assertLess(
                    body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index("throw new Error(errorMessage);"),
                    "卡券下拉接口 HTTP 失败时得先把 detail/message 解出来，再往 catch 里抛，别直接端个统一报错糊人",
                )
                self.assertLess(
                    body.index("throw new Error(errorMessage);"),
                    body.index("showToast(`加载卡券选项失败: ${error.message || '请稍后重试'}`, 'warning');"),
                    "卡券下拉接口非 2xx 时也得走统一失败提示，别静默假成功",
                )

        self.assertNotIn("return false;\n    }\n    return true;", edit_select_body)

    def test_delivery_rule_card_select_loaders_set_explicit_failure_option_when_reload_fails(self):
        add_select_body = _extract_function_body(self.app_js, "loadCardsForSelect")
        edit_select_body = _extract_function_body(self.app_js, "loadCardsForEditSelect")

        for body in (add_select_body, edit_select_body):
            with self.subTest(function_body="loadCardsForSelect" if body is add_select_body else "loadCardsForEditSelect"):
                self.assertIn("select.innerHTML = '<option value=\"\">❌ 卡券列表加载失败，请稍后重试</option>';",
                              body)
                self.assertLess(
                    body.rfind("select.innerHTML = '<option value=\"\">❌ 卡券列表加载失败，请稍后重试</option>';"),
                    body.index("showToast(`加载卡券选项失败: ${error.message || '请稍后重试'}`, 'warning');"),
                    "卡券下拉拉取失败时得落成失败态，别只留个“请选择卡券”装作只是还没选",
                )

    def test_delivery_rule_card_select_loaders_surface_empty_enabled_card_state(self):
        add_select_body = _extract_function_body(self.app_js, "loadCardsForSelect")
        edit_select_body = _extract_function_body(self.app_js, "loadCardsForEditSelect")

        self.assertIn("let appendedCount = 0;", add_select_body)
        self.assertIn("appendedCount += 1;", add_select_body)
        self.assertIn("if (appendedCount === 0 && select) {", add_select_body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 暂无可用卡券，请先添加并启用卡券</option>';",
                      add_select_body)

        self.assertIn("let appendedCount = 0;", edit_select_body)
        self.assertIn("appendedCount += 1;", edit_select_body)
        self.assertIn("if (appendedCount === 0 && select) {", edit_select_body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 暂无可用卡券，请先添加并启用卡券</option>';",
                      edit_select_body)

    def test_cards_loader_resets_stale_table_and_stats_before_fetch_and_on_failure(self):
        reset_body = _extract_function_body(self.app_js, "resetCardsView")
        load_body = _extract_function_body(self.app_js, "loadCards")

        self.assertIn("const tbody = document.getElementById('cardsTableBody');", reset_body)
        self.assertIn("document.getElementById('totalCards').textContent = '0';", reset_body)
        self.assertIn("document.getElementById('apiCards').textContent = '0';", reset_body)
        self.assertIn("document.getElementById('textCards').textContent = '0';", reset_body)
        self.assertIn("document.getElementById('dataCards').textContent = '0';", reset_body)
        self.assertIn("${escapeHtml(message)}", reset_body)

        self.assertIn("resetCardsView();", load_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("resetCardsView(error.message || '加载卡券列表失败');", load_body)
        self.assertIn("showToast(`加载卡券列表失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("resetCardsView();"),
            load_body.index("const response = await fetch(`${apiBase}/cards`, {"),
            "卡券列表重新加载前应先清掉旧表格和旧统计，别让失败状态还挂着陈年数据装成功",
        )
        self.assertLess(
            load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            load_body.index("throw new Error(errorMessage);"),
            "卡券列表 HTTP 失败时得先把 detail/message 解出来，别就剩个状态码在那装深沉",
        )
        self.assertLess(
            load_body.index("throw new Error(errorMessage);"),
            load_body.index("showToast(`加载卡券列表失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "卡券列表 HTTP 失败应把真实后端错误带进 toast，别统一甩一句失败完事",
        )

    def test_cards_and_delivery_raw_fetches_redirect_401_before_followup_work(self):
        cases = (
            ("loadCards", _extract_function_body(self.app_js, "loadCards"), "if (!response.ok) {"),
            ("saveCard", _extract_function_body(self.app_js, "saveCard"), "if (response.ok) {"),
            ("editCard", _extract_function_body(self.app_js, "editCard"), "if (!response.ok) {"),
            ("updateCard", _extract_function_body(self.app_js, "updateCard"), "if (response.ok) {"),
            ("updateCardWithImage", _extract_function_body(self.app_js, "updateCardWithImage"), "if (response.ok) {"),
            ("deleteCard", _extract_function_body(self.app_js, "deleteCard"), "if (response.ok) {"),
            ("loadDeliveryRules", _extract_function_body(self.app_js, "loadDeliveryRules"), "if (!response.ok) {"),
            ("refreshTodayDeliveryCount", _extract_function_body(self.app_js, "refreshTodayDeliveryCount"), "if (response.ok) {"),
            ("loadCardsForSelect", _extract_function_body(self.app_js, "loadCardsForSelect"), "if (!response.ok) {"),
            ("editDeliveryRule", _extract_function_body(self.app_js, "editDeliveryRule"), "if (!response.ok) {"),
            ("loadCardsForEditSelect", _extract_function_body(self.app_js, "loadCardsForEditSelect"), "if (!response.ok) {"),
            ("saveDeliveryRule", _extract_function_body(self.app_js, "saveDeliveryRule"), "if (response.ok) {"),
            ("updateDeliveryRule", _extract_function_body(self.app_js, "updateDeliveryRule"), "if (response.ok) {"),
            ("deleteDeliveryRule", _extract_function_body(self.app_js, "deleteDeliveryRule"), "if (response.ok) {"),
        )

        for function_name, body, anchor_fragment in cases:
            with self.subTest(function_name=function_name):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{function_name} 遇到 401 时应直接跳登录，别继续把未授权响应往后当正常数据折腾",
                )

    def test_cards_initial_empty_state_spans_all_visible_columns(self):
        match = re.search(
            r'id="cards-section".*?<thead>\s*<tr>(.*?)</tr>\s*</thead>\s*<tbody id="cardsTableBody">\s*<tr>\s*<td colspan="(\d+)"',
            self.index_html,
            re.S,
        )
        self.assertIsNotNone(match, "卡券表格初始空态结构丢了，页面一开就得有个明确占位")

        header_count = len(re.findall(r"<th\b", match.group(1)))
        self.assertEqual(
            str(header_count),
            match.group(2),
            f"卡券表格初始空态 colspan={match.group(2)}，但表头明明有 {header_count} 列，别一打开页面就歪着站岗",
        )

    def test_delivery_rules_empty_states_span_all_visible_columns(self):
        match = re.search(
            r'id="auto-delivery-section".*?<thead>\s*<tr>(.*?)</tr>\s*</thead>\s*<tbody id="deliveryRulesTableBody">\s*<tr>\s*<td colspan="(\d+)"',
            self.index_html,
            re.S,
        )
        self.assertIsNotNone(match, "发货规则表格初始空态结构丢了，页面一开就得有个明确占位")

        visible_headers_html = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.S)
        header_count = len(re.findall(r"<th\b", visible_headers_html))
        reset_body = _extract_function_body(self.app_js, "resetDeliveryRulesView")
        render_body = _extract_function_body(self.app_js, "renderDeliveryRulesList")

        self.assertEqual(
            str(header_count),
            match.group(2),
            f"发货规则表格初始空态 colspan={match.group(2)}，但表头明明有 {header_count} 列，别一打开页面就歪着站岗",
        )
        self.assertIn(f'<td colspan="{header_count}" class="text-center py-4 text-muted">', reset_body)
        self.assertIn(f'<td colspan="{header_count}" class="text-center py-4 text-muted">', render_body)

    def test_card_mutations_only_report_success_when_list_reload_succeeds(self):
        save_body = _extract_function_body(self.app_js, "saveCard")
        update_body = _extract_function_body(self.app_js, "updateCard")
        update_image_body = _extract_function_body(self.app_js, "updateCardWithImage")
        delete_body = _extract_function_body(self.app_js, "deleteCard")

        for body, success_message, warning_message in (
            (save_body, "卡券保存成功", "卡券保存成功，但列表刷新失败，请稍后手动刷新"),
            (update_body, "卡券更新成功", "卡券更新成功，但列表刷新失败，请稍后手动刷新"),
            (update_image_body, "卡券更新成功", "卡券更新成功，但列表刷新失败，请稍后手动刷新"),
            (delete_body, "卡券删除成功", "卡券删除成功，但列表刷新失败，请稍后手动刷新"),
        ):
            with self.subTest(success_message=success_message):
                self.assertIn("const cardsLoaded = await loadCards();", body)
                self.assertIn("if (cardsLoaded === true) {", body)
                self.assertIn("} else if (cardsLoaded === false) {", body)
                self.assertIn(f"showToast('{success_message}', 'success');", body)
                self.assertIn(f"showToast('{warning_message}', 'warning');", body)

    def test_card_save_surfaces_auto_delivery_rule_partial_failures_instead_of_false_success(self):
        body = _extract_function_body(self.app_js, "saveCard")

        self.assertIn("const result = await response.json();", body)
        self.assertIn("const deliveryRuleGenerationFailed = result?.delivery_rule_generated === false;", body)
        self.assertIn("const deliveryRuleErrorMessage = result?.delivery_rule_error || '对应发货规则生成失败，请稍后在自动发货中手动创建';", body)
        self.assertIn("showToast(`卡券保存成功，但对应发货规则生成失败: ${deliveryRuleErrorMessage}`, 'warning');", body)
        self.assertIn("showToast(`卡券保存成功，但对应发货规则生成失败: ${deliveryRuleErrorMessage}，且列表刷新失败，请稍后手动刷新`, 'warning');", body)
        self.assertLess(
            body.index("const result = await response.json();"),
            body.index("const modalElement = document.getElementById('addCardModal');"),
            "卡券创建成功后得先把后端返回的自动发货规则结果读出来，别模态框都关完了还不知道其实只是半成功",
        )
        self.assertLess(
            body.index("const deliveryRuleGenerationFailed = result?.delivery_rule_generated === false;"),
            body.index("showToast('卡券保存成功', 'success');"),
            "勾了“生成对应发货规则”却生成失败时，别还弹纯绿 success 把人往沟里带",
        )

    def test_card_mutations_do_not_emit_cross_page_toasts_after_leaving_section(self):
        save_body = _extract_function_body(self.app_js, "saveCard")
        update_body = _extract_function_body(self.app_js, "updateCard")
        update_image_body = _extract_function_body(self.app_js, "updateCardWithImage")
        delete_body = _extract_function_body(self.app_js, "deleteCard")

        for body, success_fragment in (
            (save_body, "showToast('卡券保存成功', 'success');"),
            (update_body, "showToast('卡券更新成功', 'success');"),
            (update_image_body, "showToast('卡券更新成功', 'success');"),
            (delete_body, "showToast('卡券删除成功', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('cards-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('cards-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "卡券操作在离开页面后不该再跨页弹 success toast",
                )

    def test_card_delete_actions_ignore_older_same_page_responses(self):
        self.assertIn("let cardMutationActionRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        delete_body = _extract_function_body(self.app_js, "deleteCard")

        self.assertIn("cardMutationActionRequestSequence += 1;", show_section_body)
        self.assertIn("++cardMutationActionRequestSequence", delete_body)
        self.assertIn("actionRequestSequence !== cardMutationActionRequestSequence", delete_body)
        self.assertIn("return null;", delete_body)
        self.assertLess(
            delete_body.index("actionRequestSequence !== cardMutationActionRequestSequence"),
            delete_body.index("const cardsLoaded = await loadCards();"),
            "同页连续删除卡券时，旧响应不该晚回来后又触发列表刷新和旧结果 toast",
        )

    def test_card_delete_failure_toast_rechecks_stale_state_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "deleteCard")
        error_text_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        danger_toast_index = body.index("showToast(`删除失败: ${error}`, 'danger');")

        self.assertLess(
            body.find("actionRequestSequence !== cardMutationActionRequestSequence", error_text_index),
            danger_toast_index,
            "同页已经发起新的卡券删除动作后，旧失败响应读完错误文本也别再回魂甩红字",
        )
        self.assertLess(
            body.find("!document.getElementById('cards-section')?.classList.contains('active')", error_text_index),
            danger_toast_index,
            "都切出卡券页了，旧删除失败响应读完错误文本也别再跨页弹 danger toast",
        )

    def test_card_create_action_sequence_starts_only_after_sync_validation(self):
        body = _extract_function_body(self.app_js, "saveCard")
        self.assertIn("const delaySeconds = Number.parseInt(document.getElementById('cardDelaySeconds').value, 10);", body)
        self.assertIn("if (!Number.isInteger(delaySeconds) || delaySeconds < 0 || delaySeconds > 3600) {", body)
        self.assertIn("showToast('延时发货时间需在 0 到 3600 秒之间', 'warning');", body)
        self.assertIn("delay_seconds: delaySeconds,", body)
        self.assertIn("const apiTimeout = Number.parseInt(document.getElementById('apiTimeout').value, 10);", body)
        self.assertIn("if (!Number.isInteger(apiTimeout) || apiTimeout < 1 || apiTimeout > 60) {", body)
        self.assertIn("showToast('API 超时时间需在 1 到 60 秒之间', 'warning');", body)
        self.assertIn("timeout: apiTimeout,", body)
        self.assertLess(
            body.index("if (!cardType || !cardName) {"),
            body.index("cardMutationActionRequestSequence"),
            "卡券必填字段没过时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (isMultiSpec && (!specName || !specValue)) {"),
            body.index("cardMutationActionRequestSequence"),
            "多规格校验没过时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('延时发货时间需在 0 到 3600 秒之间', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "卡券延时发货时间越界时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('请求头格式错误，请输入有效的JSON', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "API 请求头 JSON 非法时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('请求参数格式错误，请输入有效的JSON', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "API 请求参数 JSON 非法时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('API 超时时间需在 1 到 60 秒之间', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "API 超时时间越界时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (!yifanUserId || !yifanUserKey || !yifanGoodsId) {"),
            body.index("cardMutationActionRequestSequence"),
            "亦凡卡券必填字段没过时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (!imageFile) {"),
            body.index("cardMutationActionRequestSequence"),
            "图片卡券没选文件时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )

    def test_card_update_action_sequence_starts_only_after_sync_validation(self):
        body = _extract_function_body(self.app_js, "updateCard")
        self.assertIn("const delaySeconds = Number.parseInt(document.getElementById('editCardDelaySeconds').value, 10);", body)
        self.assertIn("if (!Number.isInteger(delaySeconds) || delaySeconds < 0 || delaySeconds > 3600) {", body)
        self.assertIn("showToast('延时发货时间需在 0 到 3600 秒之间', 'warning');", body)
        self.assertIn("delay_seconds: delaySeconds,", body)
        self.assertIn("const apiTimeout = Number.parseInt(document.getElementById('editApiTimeout').value, 10);", body)
        self.assertIn("if (!Number.isInteger(apiTimeout) || apiTimeout < 1 || apiTimeout > 60) {", body)
        self.assertIn("showToast('API 超时时间需在 1 到 60 秒之间', 'warning');", body)
        self.assertIn("timeout: apiTimeout,", body)
        self.assertLess(
            body.index("if (!cardType || !cardName) {"),
            body.index("cardMutationActionRequestSequence"),
            "编辑卡券必填字段没过时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (isMultiSpec && (!specName || !specValue)) {"),
            body.index("cardMutationActionRequestSequence"),
            "编辑多规格校验没过时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('延时发货时间需在 0 到 3600 秒之间', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "编辑卡券延时发货时间越界时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('请求头格式错误，请输入有效的JSON', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "编辑卡券时 API 请求头 JSON 非法只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('请求参数格式错误，请输入有效的JSON', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "编辑卡券时 API 请求参数 JSON 非法只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("showToast('API 超时时间需在 1 到 60 秒之间', 'warning');"),
            body.index("cardMutationActionRequestSequence"),
            "编辑卡券时 API 超时时间越界只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (!editYifanUserId || !editYifanUserKey || !editYifanGoodsId) {"),
            body.index("cardMutationActionRequestSequence"),
            "编辑亦凡卡券必填字段没过时只是前端校验，别先把卡券 mutation action sequence 顶掉别的正常动作",
        )

    def test_card_update_mutations_respect_edit_modal_request_sequence_before_hiding_or_toasting(self):
        update_body = _extract_function_body(self.app_js, "updateCard")
        update_image_body = _extract_function_body(self.app_js, "updateCardWithImage")

        for body in (update_body, update_image_body):
            with self.subTest(body=body[:60]):
                self.assertIn("const requestSequence = cardEditRequestSequence;", body)
                self.assertIn("requestSequence !== cardEditRequestSequence", body)
                self.assertIn("modalElement.dataset.cardEditIgnoreNextHidden = 'true';", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("requestSequence !== cardEditRequestSequence"),
                    body.index("modal.hide();"),
                    "旧的卡券更新响应不该回来把已经重开的编辑弹窗又关掉",
                )
                self.assertLess(
                    body.index("modalElement.dataset.cardEditIgnoreNextHidden = 'true';"),
                    body.index("modal.hide();"),
                    "编辑卡券成功时得先标记忽略本次程序化隐藏，再关弹窗，不然自己会把自己判 stale",
                )

    def test_card_update_mutations_ignore_older_same_modal_actions(self):
        update_body = _extract_function_body(self.app_js, "updateCard")
        update_image_body = _extract_function_body(self.app_js, "updateCardWithImage")

        for body, failure_fragment in (
            (update_body, "showToast(`更新失败: ${error}`, 'danger');"),
            (update_image_body, "showToast(`更新失败: ${error}`, 'danger');"),
        ):
            with self.subTest(body=body[:60]):
                self.assertIn("cardMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== cardMutationActionRequestSequence", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== cardMutationActionRequestSequence"),
                    body.index("modal.hide();"),
                    "同一卡券编辑弹窗里第二次保存已经发出后，第一次响应不该回来把当前弹窗关掉",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== cardMutationActionRequestSequence", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "同一卡券编辑弹窗里旧的失败响应不该晚回来后拿旧错误糊当前会话一脸",
                )

    def test_card_edit_modal_ignores_stale_async_responses_and_hidden_modal_state(self):
        self.assertIn("let cardEditRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "editCard")

        self.assertIn("if (sectionName !== 'cards') {", show_section_body)
        self.assertIn("cardEditRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++cardEditRequestSequence;", body)
        self.assertIn("requestSequence !== cardEditRequestSequence", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("if (modalElement.dataset.cardEditIgnoreNextHidden === 'true') {", body)
        self.assertIn("modalElement.dataset.cardEditIgnoreNextHidden = 'false';", body)
        self.assertIn("cardEditRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.cardEditModalBound = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== cardEditRequestSequence"),
            body.index("document.getElementById('editCardName').value = card.name;"),
            "旧的卡券详情请求不该晚回来后把当前编辑卡券弹窗改成别的卡券",
        )
        self.assertIn("setTimeout(() => {", body)
        delayed_block = body.split("setTimeout(() => {", 1)[1]
        self.assertIn("requestSequence !== cardEditRequestSequence", delayed_block)
        self.assertIn("!document.getElementById('cards-section')?.classList.contains('active')", delayed_block)
        self.assertLess(
            delayed_block.index("requestSequence !== cardEditRequestSequence"),
            delayed_block.index("toggleEditMultiSpecFields();"),
            "卡券编辑弹窗都切会话或切页了，延迟多规格字段初始化就别再回来补刀摸当前弹窗了",
        )

    def test_card_and_delivery_edit_helpers_parse_http_failure_details_before_toasting(self):
        card_body = _extract_function_body(self.app_js, "editCard")
        delivery_body = _extract_function_body(self.app_js, "editDeliveryRule")

        for body, toast_fragment, stale_fragment, label in (
            (
                card_body,
                "showToast(`获取卡券详情失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== cardEditRequestSequence",
                "卡券详情",
            ),
            (
                delivery_body,
                "showToast(`获取发货规则详情失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== deliveryRuleEditRequestSequence",
                "发货规则详情",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertIn("throw new Error(errorMessage);", body)
                self.assertIn(toast_fragment, body)
                error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                throw_index = body.index("throw new Error(errorMessage);", error_index)
                toast_index = body.index(toast_fragment)
                self.assertLess(
                    error_index,
                    throw_index,
                    f"{label} HTTP 失败时得先把 detail/message 解出来，别固定甩一句详情获取失败装没事",
                )
                self.assertLess(
                    body.find(stale_fragment, error_index),
                    throw_index,
                    f"{label} 旧失败响应读完错误体后，先验当前弹窗会话还活着，再决定要不要抛错",
                )
                self.assertLess(
                    throw_index,
                    toast_index,
                    f"{label} HTTP 失败应把真实后端错误带进 toast，别统一红字糊弄人",
                )

    def test_delivery_rules_loader_resets_stale_table_and_stats_before_fetch_and_on_failure(self):
        reset_body = _extract_function_body(self.app_js, "resetDeliveryRulesView")
        load_body = _extract_function_body(self.app_js, "loadDeliveryRules")
        update_stats_body = _extract_function_body(self.app_js, "updateDeliveryStats")
        refresh_today_body = _extract_function_body(self.app_js, "refreshTodayDeliveryCount")

        self.assertIn("const tbody = document.getElementById('deliveryRulesTableBody');", reset_body)
        self.assertIn("document.getElementById('totalRules').textContent = '0';", reset_body)
        self.assertIn("document.getElementById('activeRules').textContent = '0';", reset_body)
        self.assertIn("document.getElementById('todayDeliveries').textContent = '0';", reset_body)
        self.assertIn("document.getElementById('totalDeliveries').textContent = '0';", reset_body)
        self.assertIn("${escapeHtml(message)}", reset_body)

        self.assertIn("resetDeliveryRulesView();", load_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("resetDeliveryRulesView(error.message || '加载发货规则失败');", load_body)
        self.assertIn("showToast(`加载发货规则失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        self.assertIn("const [statsLoaded, cardOptionsLoaded] = await Promise.all([", load_body)
        self.assertIn("updateDeliveryStats(rules, requestSequence)", load_body)
        self.assertIn("loadCardsForSelect(requestSequence, 'list')", load_body)
        self.assertIn("if (statsLoaded === null || cardOptionsLoaded === null) {", load_body)
        self.assertIn("if (statsLoaded === false || cardOptionsLoaded === false) {", load_body)
        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("resetDeliveryRulesView();"),
            load_body.index("const response = await fetch(`${apiBase}/delivery-rules`, {"),
            "发货规则重新加载前也得先把旧表格和旧统计清掉，不然失败时界面还在骗你一切正常",
        )
        self.assertLess(
            load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            load_body.index("throw new Error(errorMessage);"),
            "发货规则列表 HTTP 失败时也得先把 detail/message 解出来，别只会抛状态码装高冷",
        )
        self.assertLess(
            load_body.index("throw new Error(errorMessage);"),
            load_body.index("showToast(`加载发货规则失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "发货规则列表 HTTP 失败应把真实后端错误带进 toast，别统一甩一句失败完事",
        )

        self.assertIn("async function updateDeliveryStats(rules, requestSequence = 0) {", self.app_js)
        self.assertIn("requestSequence !== 0", update_stats_body)
        self.assertIn("requestSequence !== deliveryRulesRequestSequence", update_stats_body)
        self.assertIn("!document.getElementById('auto-delivery-section')?.classList.contains('active')", update_stats_body)
        self.assertIn("const todayLoaded = await refreshTodayDeliveryCount(requestSequence);", update_stats_body)
        self.assertIn("if (todayLoaded === false) {", update_stats_body)
        self.assertLess(
            update_stats_body.index("requestSequence !== deliveryRulesRequestSequence"),
            update_stats_body.index("document.getElementById('totalRules').textContent = totalRules;"),
            "旧的发货统计刷新不该晚回来后把隐藏页统计再改一遍",
        )
        self.assertLess(
            update_stats_body.index("if (todayLoaded === false) {"),
            update_stats_body.index("return todayLoaded;"),
            "今日发货统计都失败了，就别把 updateDeliveryStats 当成功往外报了",
        )

        self.assertIn("async function refreshTodayDeliveryCount(requestSequence = 0) {", self.app_js)
        self.assertIn("requestSequence !== 0", refresh_today_body)
        self.assertIn("requestSequence !== deliveryRulesRequestSequence", refresh_today_body)
        self.assertIn("!document.getElementById('auto-delivery-section')?.classList.contains('active')", refresh_today_body)
        self.assertLess(
            refresh_today_body.index("requestSequence !== deliveryRulesRequestSequence"),
            refresh_today_body.index("todayEl.textContent = stats.today_delivery_count || 0;"),
            "旧的今日发货统计请求不该晚回来后把隐藏页数字偷偷改掉",
        )
        self.assertLess(
            load_body.index("if (statsLoaded === false || cardOptionsLoaded === false) {"),
            load_body.index("return true;"),
            "发货规则列表后续统计或卡券下拉有一项失败，都别整成完整刷新成功糊弄上层调用方",
        )

    def test_delivery_rule_mutations_only_report_success_when_list_reload_succeeds(self):
        save_body = _extract_function_body(self.app_js, "saveDeliveryRule")
        update_body = _extract_function_body(self.app_js, "updateDeliveryRule")
        delete_body = _extract_function_body(self.app_js, "deleteDeliveryRule")

        for body, success_message, warning_message in (
            (save_body, "发货规则保存成功", "发货规则保存成功，但列表刷新失败，请稍后手动刷新"),
            (update_body, "发货规则更新成功", "发货规则更新成功，但列表刷新失败，请稍后手动刷新"),
            (delete_body, "发货规则删除成功", "发货规则删除成功，但列表刷新失败，请稍后手动刷新"),
        ):
            with self.subTest(success_message=success_message):
                self.assertIn("const rulesLoaded = await loadDeliveryRules();", body)
                self.assertIn("if (rulesLoaded === true) {", body)
                self.assertIn("} else if (rulesLoaded === false) {", body)
                self.assertIn(f"showToast('{success_message}', 'success');", body)
                self.assertIn(f"showToast('{warning_message}', 'warning');", body)

    def test_delivery_rule_mutations_do_not_emit_cross_page_toasts_after_leaving_section(self):
        save_body = _extract_function_body(self.app_js, "saveDeliveryRule")
        update_body = _extract_function_body(self.app_js, "updateDeliveryRule")
        delete_body = _extract_function_body(self.app_js, "deleteDeliveryRule")

        for body, success_fragment in (
            (save_body, "showToast('发货规则保存成功', 'success');"),
            (update_body, "showToast('发货规则更新成功', 'success');"),
            (delete_body, "showToast('发货规则删除成功', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('auto-delivery-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('auto-delivery-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "发货规则操作在离开页面后不该再跨页弹 success toast",
                )

    def test_delivery_rule_delete_actions_ignore_older_same_page_responses(self):
        self.assertIn("let deliveryRuleMutationActionRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        delete_body = _extract_function_body(self.app_js, "deleteDeliveryRule")

        self.assertIn("deliveryRuleMutationActionRequestSequence += 1;", show_section_body)
        self.assertIn("++deliveryRuleMutationActionRequestSequence", delete_body)
        self.assertIn("actionRequestSequence !== deliveryRuleMutationActionRequestSequence", delete_body)
        self.assertIn("return null;", delete_body)
        self.assertLess(
            delete_body.index("actionRequestSequence !== deliveryRuleMutationActionRequestSequence"),
            delete_body.index("const rulesLoaded = await loadDeliveryRules();"),
            "同页连续删除发货规则时，旧响应不该晚回来后又触发列表刷新和旧结果 toast",
        )

    def test_delivery_rule_delete_failure_toast_rechecks_stale_state_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "deleteDeliveryRule")
        error_text_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        danger_toast_index = body.index("showToast(`删除失败: ${error}`, 'danger');")

        self.assertLess(
            body.find("actionRequestSequence !== deliveryRuleMutationActionRequestSequence", error_text_index),
            danger_toast_index,
            "同页已经发起新的发货规则删除动作后，旧失败响应读完错误文本也别再回魂甩红字",
        )
        self.assertLess(
            body.find("!document.getElementById('auto-delivery-section')?.classList.contains('active')", error_text_index),
            danger_toast_index,
            "都切出发货规则页了，旧删除失败响应读完错误文本也别再跨页弹 danger toast",
        )

    def test_delivery_rule_create_action_sequence_starts_only_after_validation(self):
        body = _extract_function_body(self.app_js, "saveDeliveryRule")
        self.assertLess(
            body.index("if (!keyword || !cardId) {"),
            body.index("deliveryRuleMutationActionRequestSequence"),
            "发货规则必填字段没过时只是前端校验，别先把发货规则 mutation action sequence 顶掉别的正常动作",
        )

    def test_delivery_rule_update_action_sequence_starts_only_after_validation(self):
        body = _extract_function_body(self.app_js, "updateDeliveryRule")
        self.assertLess(
            body.index("if (!keyword || !cardId) {"),
            body.index("deliveryRuleMutationActionRequestSequence"),
            "编辑发货规则必填字段没过时只是前端校验，别先把发货规则 mutation action sequence 顶掉别的正常动作",
        )

    def test_delivery_rule_update_mutation_respects_edit_modal_request_sequence_before_hiding_or_toasting(self):
        body = _extract_function_body(self.app_js, "updateDeliveryRule")
        self.assertIn("const requestSequence = deliveryRuleEditRequestSequence;", body)
        self.assertIn("requestSequence !== deliveryRuleEditRequestSequence", body)
        self.assertIn("modalElement.dataset.deliveryRuleEditIgnoreNextHidden = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== deliveryRuleEditRequestSequence"),
            body.index("modal.hide();"),
            "旧的发货规则更新响应不该回来把已经重开的编辑弹窗又关掉",
        )
        self.assertLess(
            body.index("modalElement.dataset.deliveryRuleEditIgnoreNextHidden = 'true';"),
            body.index("modal.hide();"),
            "编辑发货规则成功时得先标记忽略本次程序化隐藏，再关弹窗，不然自己会把自己判 stale",
        )

    def test_delivery_rule_update_mutation_ignores_older_same_modal_actions(self):
        body = _extract_function_body(self.app_js, "updateDeliveryRule")
        self.assertIn("deliveryRuleMutationActionRequestSequence", body)
        self.assertIn("actionRequestSequence !== deliveryRuleMutationActionRequestSequence", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("actionRequestSequence !== deliveryRuleMutationActionRequestSequence"),
            body.index("modal.hide();"),
            "同一发货规则编辑弹窗里第二次保存已经发出后，第一次响应不该回来把当前弹窗关掉",
        )
        self.assertLess(
            body.rfind("actionRequestSequence !== deliveryRuleMutationActionRequestSequence", 0, body.index("showToast(`更新失败: ${error}`, 'danger');")),
            body.index("showToast(`更新失败: ${error}`, 'danger');"),
            "同一发货规则编辑弹窗里旧的失败响应不该晚回来后拿旧错误糊当前会话一脸",
        )

    def test_delivery_rule_edit_modal_and_card_options_ignore_stale_async_responses(self):
        self.assertIn("let deliveryRuleEditRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        edit_body = _extract_function_body(self.app_js, "editDeliveryRule")
        options_body = _extract_function_body(self.app_js, "loadCardsForEditSelect")

        self.assertIn("if (sectionName !== 'auto-delivery') {", show_section_body)
        self.assertIn("deliveryRuleEditRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++deliveryRuleEditRequestSequence;", edit_body)
        self.assertIn("requestSequence !== deliveryRuleEditRequestSequence", edit_body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", edit_body)
        self.assertIn("if (modalElement.dataset.deliveryRuleEditIgnoreNextHidden === 'true') {", edit_body)
        self.assertIn("modalElement.dataset.deliveryRuleEditIgnoreNextHidden = 'false';", edit_body)
        self.assertIn("deliveryRuleEditRequestSequence += 1;", edit_body)
        self.assertIn("modalElement.dataset.deliveryRuleEditModalBound = 'true';", edit_body)
        self.assertIn("const cardOptionsLoaded = await loadCardsForEditSelect(rule, requestSequence);", edit_body)
        self.assertIn("if (cardOptionsLoaded === null) {", edit_body)
        self.assertIn("if (cardOptionsLoaded === false) {", edit_body)
        self.assertIn("return null;", edit_body)
        self.assertLess(
            edit_body.index("if (cardOptionsLoaded === false) {"),
            edit_body.index("document.getElementById('editSelectedCard').value = rule.card_id;"),
            "编辑发货规则时卡券下拉都没加载成，就别继续把弹窗往下糊出来装作能编辑",
        )

        self.assertIn("requestSequence !== 0", options_body)
        self.assertIn("requestSequence !== deliveryRuleEditRequestSequence", options_body)
        self.assertIn("!document.getElementById('auto-delivery-section')?.classList.contains('active')", options_body)
        self.assertIn("return null;", options_body)
        self.assertLess(
            options_body.index("requestSequence !== deliveryRuleEditRequestSequence"),
            options_body.index("select.appendChild(option);"),
            "旧的发货规则编辑卡券选项请求不该晚回来后把当前弹窗的卡券下拉框再糊回旧内容",
        )

    def test_add_delivery_rule_modal_card_options_ignore_stale_async_responses(self):
        self.assertIn("let deliveryRuleCreateRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        modal_body = _extract_function_body(self.app_js, "showAddDeliveryRuleModal")
        options_body = _extract_function_body(self.app_js, "loadCardsForSelect")

        self.assertIn("if (sectionName !== 'auto-delivery') {", show_section_body)
        self.assertIn("deliveryRuleCreateRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++deliveryRuleCreateRequestSequence;", modal_body)
        self.assertIn("loadCardsForSelect(requestSequence);", modal_body)
        self.assertIn("deliveryRuleCreateRequestSequence += 1;", modal_body)

        self.assertIn("async function loadCardsForSelect(requestSequence = 0, requestSequenceType = 'create') {", self.app_js)
        self.assertIn("requestSequence !== 0", options_body)
        self.assertIn("requestSequence !== deliveryRuleCreateRequestSequence", options_body)
        self.assertIn("requestSequenceType === 'create'", options_body)
        self.assertIn("!document.getElementById('auto-delivery-section')?.classList.contains('active')", options_body)
        self.assertIn("return null;", options_body)
        self.assertLess(
            options_body.index("requestSequence !== deliveryRuleCreateRequestSequence"),
            options_body.index("select.appendChild(option);"),
            "旧的新增发货规则卡券选项请求不该晚回来后把当前弹窗的下拉框再糊回旧内容",
        )

        self.assertIn("requestSequenceType === 'list'", options_body)
        self.assertIn("requestSequence !== deliveryRulesRequestSequence", options_body)

    def test_cards_and_delivery_rule_loaders_ignore_stale_async_responses_when_sections_change(self):
        self.assertIn("let cardsRequestSequence = 0;", self.app_js)
        self.assertIn("let deliveryRulesRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        cards_body = _extract_function_body(self.app_js, "loadCards")
        delivery_body = _extract_function_body(self.app_js, "loadDeliveryRules")

        self.assertIn("if (sectionName !== 'cards') {", show_section_body)
        self.assertIn("cardsRequestSequence += 1;", show_section_body)
        self.assertIn("if (sectionName !== 'auto-delivery') {", show_section_body)
        self.assertIn("deliveryRulesRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++cardsRequestSequence;", cards_body)
        self.assertIn("requestSequence !== cardsRequestSequence", cards_body)
        self.assertIn("!document.getElementById('cards-section')?.classList.contains('active')", cards_body)
        self.assertIn("return null;", cards_body)
        self.assertLess(
            cards_body.index("requestSequence !== cardsRequestSequence"),
            cards_body.index("renderCardsList(cards);"),
            "旧的卡券列表请求不该晚回来后把当前卡券表格再糊回旧数据",
        )

        self.assertIn("const requestSequence = ++deliveryRulesRequestSequence;", delivery_body)
        self.assertIn("requestSequence !== deliveryRulesRequestSequence", delivery_body)
        self.assertIn("!document.getElementById('auto-delivery-section')?.classList.contains('active')", delivery_body)
        self.assertIn("return null;", delivery_body)
        self.assertLess(
            delivery_body.index("requestSequence !== deliveryRulesRequestSequence"),
            delivery_body.index("renderDeliveryRulesList(rules);"),
            "旧的发货规则请求不该晚回来后把当前规则表格再糊回旧数据",
        )

    def test_items_table_uses_attribute_and_inline_js_specific_escaping_for_row_actions(self):
        body = _extract_function_body(self.app_js, "displayCurrentPageItems")
        self.assertIn("const itemId = String(item.item_id || '');", body)
        self.assertIn("const itemTitle = String(item.item_title || '未设置');", body)
        self.assertIn("const itemDetailText = getItemDetailText(item.item_detail || '');", body)
        self.assertIn("const safeAccountIdAttr = escapeHtmlAttribute(itemAccountId);", body)
        self.assertIn("const safeItemIdAttr = escapeHtmlAttribute(itemId);", body)
        self.assertIn("const safeItemTitleAttr = escapeHtmlAttribute(itemTitle);", body)
        self.assertIn("const safeItemDetailAttr = escapeHtmlAttribute(itemDetailText);", body)
        self.assertIn("const safeAccountIdForJs = escapeInlineJsSingleQuotedString(itemAccountId);", body)
        self.assertIn("const safeItemIdForJs = escapeInlineJsSingleQuotedString(itemId);", body)
        self.assertIn("const safeItemTitleForJs = escapeInlineJsSingleQuotedString(item.item_title || item.item_id || '');", body)
        self.assertIn('data-account-id="${safeAccountIdAttr}"', body)
        self.assertIn('data-item-id="${safeItemIdAttr}"', body)
        self.assertIn('title="${safeItemTitleAttr}"', body)
        self.assertIn('title="${safeItemDetailAttr}"', body)
        self.assertIn('onclick="editItem(\'${safeAccountIdForJs}\', \'${safeItemIdForJs}\')"', body)
        self.assertIn('onclick="deleteItem(\'${safeAccountIdForJs}\', \'${safeItemIdForJs}\', \'${safeItemTitleForJs}\')"', body)
        self.assertIn('onclick="toggleItemMultiSpec(\'${safeAccountIdForJs}\', \'${safeItemIdForJs}\', ${!isMultiSpec})"', body)
        self.assertIn('onclick="toggleItemMultiQuantityDelivery(\'${safeAccountIdForJs}\', \'${safeItemIdForJs}\', ${!isMultiQuantityDelivery})"', body)
        self.assertNotIn('data-account-id="${escapeHtml(itemAccountId)}"', body)
        self.assertNotIn('data-item-id="${escapeHtml(item.item_id)}"', body)
        self.assertNotIn("onclick=\"editItem('${escapeHtml(itemAccountId)}', '${escapeHtml(item.item_id)}')\"", body)
        self.assertNotIn("onclick=\"deleteItem('${escapeHtml(itemAccountId)}', '${escapeHtml(item.item_id)}', '${escapeHtml(item.item_title || item.item_id)}')\"", body)

    def test_order_rows_use_attribute_specific_escaping_for_titles_checkbox_values_and_action_datasets(self):
        body = _extract_function_body(self.app_js, "createOrderRow")
        self.assertIn("const orderId = String(order.order_id || '');", body)
        self.assertIn("const itemId = String(order.item_id ?? '').trim() || '-';", body)
        self.assertIn("const buyerId = String(order.buyer_id ?? '').trim() || '-';", body)
        self.assertIn("const buyerNick = String(order.buyer_nick ?? '').trim() || '-';", body)
        self.assertIn("const rawAccountId = String(order.account_id ?? '').trim();", body)
        self.assertIn("const accountId = rawAccountId || '-';", body)
        self.assertIn("const safeOrderId = escapeHtml(orderId);", body)
        self.assertIn("const safeOrderIdAttr = escapeHtmlAttribute(orderId);", body)
        self.assertIn("const safeItemId = escapeHtml(itemId);", body)
        self.assertIn("const safeItemIdAttr = escapeHtmlAttribute(itemId);", body)
        self.assertIn("const safeBuyerId = escapeHtml(buyerId);", body)
        self.assertIn("const safeBuyerIdAttr = escapeHtmlAttribute(buyerId);", body)
        self.assertIn("const safeBuyerNick = escapeHtml(buyerNick);", body)
        self.assertIn("const safeBuyerNickAttr = escapeHtmlAttribute(buyerNick);", body)
        self.assertIn("const safeAccountId = escapeHtml(accountId);", body)
        self.assertIn("const safeAccountIdAttr = escapeHtmlAttribute(accountId);", body)
        self.assertIn("const safeAccountIdDataAttr = escapeHtmlAttribute(rawAccountId);", body)
        self.assertIn("const normalizedSpecName = String(order.spec_name ?? '').trim();", body)
        self.assertIn("const normalizedSpecValue = String(order.spec_value ?? '').trim();", body)
        self.assertIn("const normalizedSpecName2 = String(order.spec_name_2 ?? '').trim();", body)
        self.assertIn("const normalizedSpecValue2 = String(order.spec_value_2 ?? '').trim();", body)
        self.assertIn("const normalizedQuantity = String(order.quantity ?? '').trim();", body)
        self.assertIn("const quantity = escapeHtml(normalizedQuantity || '-');", body)
        self.assertIn('value="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}"', body)
        self.assertIn('title="${safeOrderIdAttr}"', body)
        self.assertIn('title="${itemId === \'-\' ? \'\' : safeItemIdAttr}"', body)
        self.assertIn('title="${buyerId === \'-\' ? \'\' : safeBuyerIdAttr}"', body)
        self.assertIn('title="${buyerNick === \'-\' ? \'\' : safeBuyerNickAttr}"', body)
        self.assertIn('title="${accountId === \'-\' ? \'\' : safeAccountIdAttr}"', body)
        self.assertIn('data-order-id="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}"', body)
        self.assertNotIn('value="${orderId}" data-account-id="${accountId}"', body)
        self.assertNotIn('data-order-id="${orderId}" data-account-id="${accountId}"', body)

    def test_order_filters_trim_search_keyword_and_match_against_normalized_visible_fields(self):
        body = _extract_function_body(self.app_js, "filterOrders")

        self.assertIn("const searchKeyword = (document.getElementById('orderSearchInput')?.value || '').trim().toLowerCase();", body)
        self.assertIn("const normalizedOrderId = String(order.order_id ?? '').trim().toLowerCase();", body)
        self.assertIn("const normalizedItemId = String(order.item_id ?? '').trim().toLowerCase();", body)
        self.assertIn("const normalizedBuyerId = String(order.buyer_id ?? '').trim().toLowerCase();", body)
        self.assertIn("const normalizedBuyerNick = String(order.buyer_nick ?? '').trim().toLowerCase();", body)
        self.assertIn("const normalizedOrderAccountId = String(order.account_id ?? '').trim();", body)
        self.assertIn("normalizedOrderId.includes(searchKeyword)", body)
        self.assertIn("normalizedItemId.includes(searchKeyword)", body)
        self.assertIn("normalizedBuyerId.includes(searchKeyword)", body)
        self.assertIn("normalizedBuyerNick.includes(searchKeyword)", body)
        self.assertIn("const matchesAccount = !accountFilter || normalizedOrderAccountId === accountFilter;", body)
        self.assertNotIn("order.order_id && order.order_id.toLowerCase().includes(searchKeyword)", body)
        self.assertNotIn("const matchesAccount = !accountFilter || order.account_id === accountFilter;", body)

        self.assertLess(
            body.index("const normalizedOrderId = String(order.order_id ?? '').trim().toLowerCase();"),
            body.index("normalizedOrderId.includes(searchKeyword)"),
            "订单搜索应该匹配用户眼睛能看到的标准化值，别列表里 trim 完了，搜索却还拿原始脏值比，整得搜不到自己刚看到的订单",
        )

    def test_order_rows_trim_whitespace_identifiers_before_rendering_placeholders_and_action_datasets(self):
        row_body = _extract_function_body(self.app_js, "createOrderRow")
        detail_body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("const itemId = String(order.item_id ?? '').trim() || '-';", row_body)
        self.assertIn("const buyerId = String(order.buyer_id ?? '').trim() || '-';", row_body)
        self.assertIn("const buyerNick = String(order.buyer_nick ?? '').trim() || '-';", row_body)
        self.assertIn("const rawAccountId = String(order.account_id ?? '').trim();", row_body)
        self.assertIn("const accountId = rawAccountId || '-';", row_body)
        self.assertIn("const normalizedBuyerId = String(order.buyer_id ?? '').trim();", detail_body)
        self.assertIn("const normalizedBuyerNick = String(order.buyer_nick ?? '').trim();", detail_body)
        self.assertIn("const safeBuyerId = escapeHtml(normalizedBuyerId || '未知');", detail_body)
        self.assertIn("const safeBuyerNick = escapeHtml(normalizedBuyerNick || '未知');", detail_body)

        self.assertLess(
            row_body.index("const rawAccountId = String(order.account_id ?? '').trim();"),
            row_body.index('data-order-id="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}"'),
            "订单列表里的账号ID如果全是空白，得先 trim 成占位值，别把空白字符串直接塞进操作按钮 dataset，后面点按钮全乱套",
        )
        self.assertLess(
            detail_body.index("const normalizedBuyerId = String(order.buyer_id ?? '').trim();"),
            detail_body.index("const safeBuyerId = escapeHtml(normalizedBuyerId || '未知');"),
            "订单详情里的买家ID如果只有空白字符，也该先标准化成未知，别展示成一块空气让人以为页面抽风",
        )

    def test_order_spec_fields_trim_whitespace_before_rendering_in_list_and_detail_modal(self):
        row_body = _extract_function_body(self.app_js, "createOrderRow")
        detail_body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("const normalizedSpecName = String(order.spec_name ?? '').trim();", row_body)
        self.assertIn("const normalizedSpecValue = String(order.spec_value ?? '').trim();", row_body)
        self.assertIn("const normalizedSpecName2 = String(order.spec_name_2 ?? '').trim();", row_body)
        self.assertIn("const normalizedSpecValue2 = String(order.spec_value_2 ?? '').trim();", row_body)
        self.assertIn("if (normalizedSpecName && normalizedSpecValue) {", row_body)
        self.assertIn("if (normalizedSpecName2 && normalizedSpecValue2) {", row_body)
        self.assertNotIn("if (order.spec_name && order.spec_value) {", row_body)

        self.assertIn("const normalizedSpecName = String(order.spec_name ?? '').trim();", detail_body)
        self.assertIn("const normalizedSpecValue = String(order.spec_value ?? '').trim();", detail_body)
        self.assertIn("const safeSpecName = escapeHtml(normalizedSpecName || '无');", detail_body)
        self.assertIn("const safeSpecValue = escapeHtml(normalizedSpecValue || '无');", detail_body)
        self.assertIn("const safeSpecName2 = escapeHtml(normalizedSpecName2 || '无');", detail_body)
        self.assertIn("const safeSpecValue2 = escapeHtml(normalizedSpecValue2 || '无');", detail_body)
        self.assertNotIn("const safeSpecName = escapeHtml(order.spec_name || '无');", detail_body)

        self.assertLess(
            row_body.index("const normalizedSpecName = String(order.spec_name ?? '').trim();"),
            row_body.index("if (normalizedSpecName && normalizedSpecValue) {"),
            "订单列表里的规格字段如果全是空白，得先 trim 再决定有没有规格，别让空白字符串把规格栏装成有内容",
        )
        self.assertLess(
            detail_body.index("const normalizedSpecName = String(order.spec_name ?? '').trim();"),
            detail_body.index("const safeSpecName = escapeHtml(normalizedSpecName || '无');"),
            "订单详情里的规格字段也该先标准化，空白字符串要回到'无'，别展示成空白单元格恶心人",
        )

    def test_order_rows_keep_raw_account_id_out_of_mutation_datasets_and_disable_actions_when_missing(self):
        body = _extract_function_body(self.app_js, "createOrderRow")

        self.assertIn("const rawAccountId = String(order.account_id ?? '').trim();", body)
        self.assertIn("const hasAccountId = Boolean(rawAccountId);", body)
        self.assertIn("const safeAccountIdDataAttr = escapeHtmlAttribute(rawAccountId);", body)
        self.assertIn('data-account-id="${safeAccountIdDataAttr}" ${hasAccountId ? \'\' : \'disabled\'}', body)
        self.assertIn('data-order-action="deliver" data-order-id="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}" title="手动发货" ${(canDeliver && hasAccountId) ? \'\' : \'disabled\'}', body)
        self.assertIn('data-order-action="refresh" data-order-id="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}" title="刷新状态" ${hasAccountId ? \'\' : \'disabled\'}', body)
        self.assertIn('data-order-action="detail" data-order-id="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}" title="查看详情"', body)
        self.assertIn('data-order-action="delete" data-order-id="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}" title="删除" ${hasAccountId ? \'\' : \'disabled\'}', body)
        self.assertNotIn('data-account-id="${safeAccountIdAttr}" ${hasAccountId ? \'\' : \'disabled\'}', body)

        self.assertLess(
            body.index("const safeAccountIdDataAttr = escapeHtmlAttribute(rawAccountId);"),
            body.index('data-order-action="detail" data-order-id="${safeOrderIdAttr}" data-account-id="${safeAccountIdDataAttr}" title="查看详情"'),
            "订单详情按钮该拿原始账号ID参数，不该拿给用户看的 '-' 占位去找订单，不然缺账号ID的行点详情都直接装死",
        )

    def test_order_status_normalization_trims_whitespace_before_falling_back_to_unknown(self):
        normalize_body = _extract_function_body(self.app_js, "normalizeOrderStatus")
        row_body = _extract_function_body(self.app_js, "createOrderRow")
        detail_body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("const value = String(status || '').trim().toLowerCase();", normalize_body)
        self.assertNotIn("const value = String(status || '').toLowerCase();", normalize_body)
        self.assertIn("const statusClass = getOrderStatusClass(order.order_status);", row_body)
        self.assertIn("const statusText = getOrderStatusText(order.order_status);", row_body)
        self.assertIn("const safeStatusText = escapeHtml(getOrderStatusText(order.order_status));", detail_body)

        self.assertLess(
            normalize_body.index("const value = String(status || '').trim().toLowerCase();"),
            normalize_body.index("return aliasMap[value] || value || 'unknown';"),
            "订单状态如果只有空白字符，得先 trim 再做归一化，不然前端会把一团空气当状态文本塞进 badge 里装正常",
        )

    def test_order_amount_display_rejects_null_like_and_currency_only_garbage_before_prefixing_symbol(self):
        body = _extract_function_body(self.app_js, "formatOrderAmountDisplay")
        row_body = _extract_function_body(self.app_js, "createOrderRow")
        detail_body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("const amountText = String(rawAmount).trim();", body)
        self.assertIn("['none', 'null', 'nan'].includes(amountText.toLowerCase())", body)
        self.assertIn("const normalizedNumeric = amountText.replace(/[^\\d.-]/g, '');", body)
        self.assertIn("if (!normalizedNumeric || normalizedNumeric === '-' || normalizedNumeric === '.' || normalizedNumeric === '-.') {", body)
        self.assertIn("return '-';", body)
        self.assertIn("const amountDisplay = escapeHtml(formatOrderAmountDisplay(order.amount));", row_body)
        self.assertIn("const safeAmount = escapeHtml(formatOrderAmountDisplay(order.amount));", detail_body)

        self.assertLess(
            body.index("const normalizedNumeric = amountText.replace(/[^\\d.-]/g, '');"),
            body.index("if (/[¥￥$]/.test(amountText)) {"),
            "订单金额如果根本提不出数字，就得先落成 '-'，别把 '¥'、'null' 这种脏文本当正常金额继续往下展示",
        )

    def test_order_time_helpers_skip_invalid_null_like_timestamps_before_falling_back(self):
        sales_body = _extract_function_body(self.app_js, "getEffectiveOrderSalesTime")
        sort_body = _extract_function_body(self.app_js, "getOrderPrimarySortTime")

        for body in (sales_body, sort_body):
            self.assertIn("const timeCandidates = [", body)
            self.assertIn("const normalizedTime = String(candidate || '').trim();", body)
            self.assertIn("if (!normalizedTime) {", body)
            self.assertIn("if (parseUtcDateTime(normalizedTime)) {", body)
            self.assertIn("return normalizedTime;", body)
            self.assertIn("return null;", body)

        self.assertIn("order?.platform_paid_at,", sales_body)
        self.assertIn("order?.platform_created_at,", sales_body)
        self.assertIn("order?.created_at,", sales_body)
        self.assertIn("order?.platform_created_at,", sort_body)
        self.assertIn("order?.created_at,", sort_body)
        self.assertNotIn("if (platformPaidAt) return platformPaidAt;", sales_body)
        self.assertNotIn("if (platformCreatedAt) {", sort_body)

        self.assertLess(
            sales_body.index("if (parseUtcDateTime(normalizedTime)) {"),
            sales_body.index("return normalizedTime;"),
            "订单销售时间候选值如果只是 'null' 这种脏文本，得先验能不能解析成时间，再决定是否采用，别把 created_at 的有效回退挡住",
        )
        self.assertLess(
            sort_body.index("if (parseUtcDateTime(normalizedTime)) {"),
            sort_body.index("return normalizedTime;"),
            "订单主排序时间也得先跳过无效时间文本，否则 platform_created_at='null' 会把 created_at 的有效排序时间顶没了",
        )

    def test_order_detail_item_lookup_encodes_account_and_item_path_segments(self):
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        self.assertIn("const encodedAccountId = encodeURIComponent(normalizedAccountId);", body)
        self.assertIn("const encodedItemId = encodeURIComponent(String(itemId || '').trim());", body)
        self.assertIn("fetch(`${apiBase}/items/${encodedAccountId}/${encodedItemId}?${params.toString()}`", body)
        self.assertNotIn("fetch(`${apiBase}/items/${normalizedAccountId}/${itemId}?${params.toString()}`", body)

    def test_order_detail_modal_normalizes_item_detail_text_before_rendering(self):
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        self.assertIn("const itemDetailText = getItemDetailText(item.item_detail || '');", body)
        self.assertIn("const safeDetail = escapeHtml(itemDetailText);", body)
        self.assertIn("${itemDetailText ? `", body)
        self.assertNotIn("const safeDetail = escapeHtml(item.item_detail || '');", body)
        self.assertNotIn("${item.item_detail ? `", body)

    def test_order_detail_item_loader_ignores_stale_async_responses(self):
        self.assertIn("let orderDetailItemRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        self.assertIn("const requestSequence = ++orderDetailItemRequestSequence;", body)
        self.assertIn("if (requestSequence !== orderDetailItemRequestSequence) {", body)
        self.assertIn("return;", body)

    def test_order_detail_modal_invalidates_pending_item_detail_requests_when_closed(self):
        body = _extract_function_body(self.app_js, "showOrderDetail")
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("orderDetailItemRequestSequence += 1;", body)
        self.assertIn("modalElement.remove();", body)

    def test_order_detail_modal_invalidates_previous_item_detail_session_before_replacing_modal(self):
        body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("orderDetailItemRequestSequence += 1;", body)
        self.assertLess(
            body.index("orderDetailItemRequestSequence += 1;"),
            body.index("const existingModal = document.getElementById('orderDetailModal');"),
            "切到另一条订单详情前要先废掉旧商品详情请求，不然旧请求会趁新弹窗还没发新查询时回魂篡位",
        )
        self.assertLess(
            body.index("orderDetailItemRequestSequence += 1;"),
            body.index("if (normalizedOrderItemId && normalizedOrderAccountId) {"),
            "订单详情切换到没有商品ID的新订单时，也得先作废旧详情请求，别让前一条订单的商品详情把空态顶掉",
        )

    def test_order_detail_modal_shows_empty_state_when_item_id_is_missing(self):
        body = _extract_function_body(self.app_js, "showOrderDetail")
        self.assertIn("const normalizedOrderItemId = String(order.item_id || '').trim();", body)
        self.assertIn("const normalizedOrderAccountId = String(normalizedAccountId || order.account_id || '').trim();", body)
        self.assertIn("if (normalizedOrderItemId && normalizedOrderAccountId) {", body)
        self.assertIn("const emptyStateMessage = normalizedOrderItemId", body)
        self.assertIn("暂无商品ID，无法加载商品详情", body)
        self.assertLess(
            body.index("if (normalizedOrderItemId && normalizedOrderAccountId) {"),
            body.index("暂无商品ID，无法加载商品详情"),
            "订单本来就没商品ID时，详情弹窗别让那个加载 spinner 在那儿死撑，得给个明白空态",
        )

    def test_order_detail_modal_treats_whitespace_item_id_as_missing_instead_of_firing_bogus_detail_request(self):
        body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("const normalizedOrderItemId = String(order.item_id || '').trim();", body)
        self.assertIn("const normalizedOrderAccountId = String(normalizedAccountId || order.account_id || '').trim();", body)
        self.assertIn("const safeItemId = escapeHtml(normalizedOrderItemId || '未知');", body)
        self.assertIn("if (normalizedOrderItemId && normalizedOrderAccountId) {", body)
        self.assertIn("loadItemDetailForOrder(normalizedOrderItemId, normalizedOrderAccountId);", body)

        normalize_index = body.index("const normalizedOrderItemId = String(order.item_id || '').trim();")
        load_index = body.index("loadItemDetailForOrder(normalizedOrderItemId, normalizedOrderAccountId);")
        empty_state_index = body.index("暂无商品ID，无法加载商品详情")

        self.assertLess(
            normalize_index,
            load_index,
            "订单详情里的商品ID如果全是空白字符，得先 trim 再决定要不要查详情，别拿空串拼请求地址恶心人",
        )
        self.assertLess(
            normalize_index,
            empty_state_index,
            "订单详情判断商品ID缺失前应先标准化，空白字符串也该落到明确空态，别假装有商品去发无效请求",
        )

    def test_order_detail_modal_shows_empty_state_when_account_id_is_missing_even_with_item_id(self):
        body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("const normalizedOrderAccountId = String(normalizedAccountId || order.account_id || '').trim();", body)
        self.assertIn("const safeAccountId = escapeHtml(normalizedOrderAccountId || '未知');", body)
        self.assertIn("if (normalizedOrderItemId && normalizedOrderAccountId) {", body)
        self.assertIn("const emptyStateMessage = normalizedOrderItemId", body)
        self.assertIn("暂无账号ID，无法加载商品详情", body)
        self.assertIn("loadItemDetailForOrder(normalizedOrderItemId, normalizedOrderAccountId);", body)

        account_normalize_index = body.index("const normalizedOrderAccountId = String(normalizedAccountId || order.account_id || '').trim();")
        empty_message_index = body.index("暂无账号ID，无法加载商品详情")
        load_index = body.index("loadItemDetailForOrder(normalizedOrderItemId, normalizedOrderAccountId);")

        self.assertLess(
            account_normalize_index,
            load_index,
            "订单详情在决定是否查商品详情前，得先把账号ID标准化，别拿空白账号ID去拼无效详情请求",
        )
        self.assertLess(
            account_normalize_index,
            empty_message_index,
            "订单详情碰到缺失账号ID时应直接落空态，别把本该提示的数据缺失场景伪装成商品详情接口失败",
        )

    def test_order_detail_modal_uses_trimmed_quantity_and_does_not_fake_single_item_default(self):
        row_body = _extract_function_body(self.app_js, "createOrderRow")
        detail_body = _extract_function_body(self.app_js, "showOrderDetail")

        self.assertIn("const normalizedQuantity = String(order.quantity ?? '').trim();", row_body)
        self.assertIn("const quantity = escapeHtml(normalizedQuantity || '-');", row_body)
        self.assertIn("const normalizedQuantity = String(order.quantity ?? '').trim();", detail_body)
        self.assertIn("const safeQuantity = escapeHtml(normalizedQuantity || '-');", detail_body)
        self.assertNotIn("const safeQuantity = escapeHtml(order.quantity || '1');", detail_body)

        self.assertLess(
            detail_body.index("const normalizedQuantity = String(order.quantity ?? '').trim();"),
            detail_body.index("const safeQuantity = escapeHtml(normalizedQuantity || '-');"),
            "订单详情里的数量展示得先 trim 再决定怎么回退，空白值别硬给用户装成买了 1 件",
        )

    def test_order_detail_item_loader_does_not_update_hidden_modal_content(self):
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        self.assertIn("!document.getElementById('orderDetailModal')", body)
        self.assertLess(
            body.index("!document.getElementById('orderDetailModal')"),
            body.index("content.innerHTML = `"),
            "订单详情弹窗都关了，商品详情旧请求就别回来往隐藏内容区域塞东西",
        )

    def test_order_detail_item_loader_handles_unauthorized_before_followup_work(self):
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (requestSequence !== orderDetailItemRequestSequence) {"),
            "订单详情商品查询这条 raw fetch 遇到 401 得先滚去登录，别后面还继续验 session、写 modal 内容",
        )

    def test_order_detail_item_loader_failure_reads_structured_error_messages_before_rendering(self):
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        self.assertIn("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("无法获取商品详情信息：${escapeHtml(message)}", body)
        self.assertLess(
            body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("无法获取商品详情信息：${escapeHtml(message)}"),
            "订单详情商品查询失败时得先把 detail/message 解出来，别糊个固定文案把真原因吞了",
        )

    def test_order_detail_item_loader_rejects_malformed_success_payload_before_rendering(self):
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        self.assertIn("Array.isArray(data)", body)
        self.assertIn("Array.isArray(data.item)", body)
        self.assertIn("throw new Error('商品详情返回格式异常');", body)
        self.assertLess(
            body.index("throw new Error('商品详情返回格式异常');"),
            body.index("const item = data.item;"),
            "订单详情接口就算回了 200，也得先验 item 结构，别拿脏 payload 硬解引用把 modal 整崩",
        )
        self.assertLess(
            body.index("Array.isArray(data.item)"),
            body.index("const item = data.item;"),
            "订单详情接口把 item 回成数组时也得判格式异常，别把列表错当单对象往详情 modal 里塞",
        )

    def test_order_detail_item_loader_failure_rechecks_modal_session_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "loadItemDetailForOrder")
        error_index = body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        warning_index = body.index("无法获取商品详情信息：${escapeHtml(message)}")
        self.assertLess(
            body.find("if (requestSequence !== orderDetailItemRequestSequence) {", error_index),
            warning_index,
            "旧的订单详情失败响应读完错误文本后，先验当前 session，别回魂篡位新弹窗",
        )
        self.assertLess(
            body.find("if (!document.getElementById('orderDetailModal')) {", error_index),
            warning_index,
            "订单详情弹窗都关了，旧失败响应读完错误文本也别再往隐藏 modal 里塞警告块",
        )

    def test_orders_refresh_only_reports_success_when_reload_succeeds_and_failed_reload_clears_stale_data(self):
        load_all_body = _extract_function_body(self.app_js, "loadAllOrders")
        refresh_data_body = _extract_function_body(self.app_js, "refreshOrdersData")
        refresh_body = _extract_function_body(self.app_js, "refreshOrders")

        self.assertIn("allOrdersData = [];", load_all_body)
        self.assertIn("filteredOrdersData = [];", load_all_body)
        self.assertIn("updateOrdersDisplay();", load_all_body)
        self.assertIn("return true;", load_all_body)
        self.assertIn("return false;", load_all_body)

        self.assertIn("const ordersLoaded = await loadAllOrders();", refresh_data_body)
        self.assertIn("return ordersLoaded;", refresh_data_body)

        self.assertIn("const ordersLoaded = await refreshOrdersData();", refresh_body)
        self.assertIn("if (ordersLoaded) {", refresh_body)
        self.assertIn("showToast('订单列表已刷新', 'success');", refresh_body)
        self.assertIn("} else if (ordersLoaded === false) {", refresh_body)
        self.assertIn("showToast('订单列表刷新失败，请稍后手动刷新', 'warning');", refresh_body)

    def test_orders_empty_pagination_resets_total_pages_and_avoids_negative_ranges(self):
        display_body = _extract_function_body(self.app_js, "updateOrdersDisplay")
        pagination_body = _extract_function_body(self.app_js, "updateOrdersPagination")

        self.assertIn("totalOrdersPages = computedTotalPages;", display_body)
        self.assertLess(
            display_body.index("totalOrdersPages = computedTotalPages;"),
            display_body.index("if (computedTotalPages === 0) {"),
            "订单筛空以后先把总页数归零，别让旧 total pages 赖着不走继续骗分页控件",
        )

        self.assertIn("if (filteredOrdersData.length === 0) {", pagination_body)
        self.assertIn("pageInfo.textContent = '显示第 0-0 条，共 0 条记录';", pagination_body)
        self.assertLess(
            pagination_body.index("if (filteredOrdersData.length === 0) {"),
            pagination_body.index("const startIndex = (currentOrdersPage - 1) * ordersPerPage + 1;"),
            "订单列表都空了还先算 1-0 这种邪门区间，这分页文案属实整得埋汰，得先走空态分支",
        )

    def test_orders_requests_are_invalidated_when_leaving_orders_section(self):
        self.assertIn("let ordersListRequestSequence = 0;", self.app_js)
        self.assertIn("let orderAccountFilterRequestSequence = 0;", self.app_js)
        self.assertIn("let orderRefreshActionRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("if (sectionName !== 'orders') {", show_section_body)
        self.assertIn("ordersListRequestSequence += 1;", show_section_body)
        self.assertIn("orderAccountFilterRequestSequence += 1;", show_section_body)
        self.assertIn("orderRefreshActionRequestSequence += 1;", show_section_body)

    def test_leaving_orders_section_invalidates_inflight_order_mutation_actions(self):
        self.assertIn("let orderMutationActionRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("if (sectionName !== 'orders') {", show_section_body)
        self.assertIn("orderMutationActionRequestSequence += 1;", show_section_body)
        self.assertLess(
            show_section_body.index("orderMutationActionRequestSequence += 1;"),
            show_section_body.index("stopOrdersStream();"),
            "切出 orders 再切回来时，旧的订单操作响应不能还认旧会话，得先把 action sequence 作废",
        )

    def test_orders_stream_sessions_are_invalidated_when_leaving_orders_or_reconnecting(self):
        self.assertIn("let ordersStreamSessionRequestSequence = 0;", self.app_js)
        stop_body = _extract_function_body(self.app_js, "stopOrdersStream")
        start_body = _extract_function_body(self.app_js, "startOrdersStream")
        consume_body = _extract_function_body(self.app_js, "consumeOrdersStream")

        self.assertIn("ordersStreamSessionRequestSequence += 1;", stop_body)
        self.assertIn("const streamSessionRequestSequence = ++ordersStreamSessionRequestSequence;", start_body)
        self.assertIn("await consumeOrdersStream(response, controller, streamSessionRequestSequence);", start_body)
        self.assertIn("handleOrdersStreamEvent(eventName, dataLines.join('\\n'), streamSessionRequestSequence);", consume_body)
        self.assertLess(
            start_body.index("const streamSessionRequestSequence = ++ordersStreamSessionRequestSequence;"),
            start_body.index("await consumeOrdersStream(response, controller, streamSessionRequestSequence);"),
            "订单实时流重连或重开后得换一套 stream session，会话号都没推进就别指望挡住旧连接回魂",
        )

    def test_orders_stream_raw_fetch_handles_unauthorized_before_non_ok_branch(self):
        body = _extract_function_body(self.app_js, "startOrdersStream")
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "订单实时流这条 raw fetch 遇到 401 得先去登录，别后面还继续装成普通连流失败然后死命重连",
        )

    def test_orders_stream_event_handlers_ignore_stale_or_hidden_stream_sessions(self):
        handle_body = _extract_function_body(self.app_js, "handleOrdersStreamEvent")
        update_body = _extract_function_body(self.app_js, "applyRealtimeOrderUpdate")

        self.assertIn("streamSessionRequestSequence !== ordersStreamSessionRequestSequence", handle_body)
        self.assertIn("!ordersStreamShouldRun", handle_body)
        self.assertIn("!isOrdersSectionActive()", handle_body)
        self.assertIn("applyRealtimeOrderUpdate(payload.order, streamSessionRequestSequence);", handle_body)
        self.assertLess(
            handle_body.index("streamSessionRequestSequence !== ordersStreamSessionRequestSequence"),
            handle_body.index("applyRealtimeOrderUpdate(payload.order, streamSessionRequestSequence);"),
            "订单实时流老连接都 stale 了，就别让晚到的事件继续往当前 orders 会话里灌数据",
        )

        self.assertIn("streamSessionRequestSequence !== ordersStreamSessionRequestSequence", update_body)
        self.assertIn("!ordersStreamShouldRun", update_body)
        self.assertIn("!isOrdersSectionActive()", update_body)
        self.assertIn("refreshOrdersData();", update_body)
        self.assertIn("filterOrders(false);", update_body)
        self.assertLess(
            update_body.index("streamSessionRequestSequence !== ordersStreamSessionRequestSequence"),
            update_body.index("refreshOrdersData();"),
            "订单实时流旧会话如果发现本地没这单，也不能再偷偷触发整表刷新去污染当前页",
        )
        self.assertLess(
            update_body.index("streamSessionRequestSequence !== ordersStreamSessionRequestSequence"),
            update_body.index("filterOrders(false);"),
            "订单实时流旧会话不该在切页或重连后还回来改当前 orders 列表",
        )

    def test_load_orders_wrapper_does_not_emit_cross_page_failure_toasts(self):
        body = _extract_function_body(self.app_js, "loadOrders")
        toast_fragment = "showToast(`加载订单列表失败: ${error.message || '请稍后重试'}`, 'danger');"

        self.assertIn("!document.getElementById('orders-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("!document.getElementById('orders-section')?.classList.contains('active')"),
            body.index(toast_fragment),
            "都切出订单页了，旧的订单加载失败就别再跨页甩 danger toast 了",
        )

    def test_order_module_catch_failures_surface_runtime_error_messages(self):
        for body, legacy_toast, runtime_toast, guard_fragments, label in (
            (
                _extract_function_body(self.app_js, "loadOrders"),
                "showToast('加载订单列表失败', 'danger');",
                "showToast(`加载订单列表失败: ${error.message || '请稍后重试'}`, 'danger');",
                ("!document.getElementById('orders-section')?.classList.contains('active')",),
                "订单加载包装层",
            ),
            (
                _extract_function_body(self.app_js, "openOrderHistorySyncModal"),
                "showToast('加载历史同步配置失败', 'danger');",
                "showToast(`加载历史同步配置失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== orderHistorySyncModalRequestSequence",
                    "!isOrdersSectionActive()",
                ),
                "历史订单同步弹窗",
            ),
            (
                _extract_function_body(self.app_js, "showOrderDetail"),
                "showToast('显示订单详情失败', 'danger');",
                "showToast(`显示订单详情失败: ${error.message || '请稍后重试'}`, 'danger');",
                (),
                "订单详情弹窗",
            ),
            (
                _extract_function_body(self.app_js, "deleteOrder"),
                "showToast('删除订单失败', 'danger');",
                "showToast(`删除订单失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence && actionRequestSequence !== orderMutationActionRequestSequence",
                    "!isOrdersSectionActive()",
                ),
                "删除订单",
            ),
            (
                _extract_function_body(self.app_js, "batchDeleteOrders"),
                "showToast('批量删除订单失败', 'danger');",
                "showToast(`批量删除订单失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence && actionRequestSequence !== orderMutationActionRequestSequence",
                    "!isOrdersSectionActive()",
                ),
                "批量删除订单",
            ),
        ):
            with self.subTest(label=label):
                self.assertNotIn(legacy_toast, body)
                self.assertIn(runtime_toast, body)
                toast_index = body.index(runtime_toast)
                for guard_fragment in guard_fragments:
                    self.assertIn(guard_fragment, body)
                    self.assertLess(
                        body.rfind(guard_fragment, 0, toast_index),
                        toast_index,
                        f"{label} catch 里的旧异常在弹 toast 前也得先过会话/页面活性校验，别 stale 了还回来犯病",
                    )

    def test_load_orders_wrapper_stops_when_account_filter_loader_aborts(self):
        body = _extract_function_body(self.app_js, "loadOrders")

        self.assertIn("const [accountOptionsLoaded, ordersLoaded] = await Promise.all([", body)
        self.assertIn("loadOrderAccountFilterOptions()", body)
        self.assertIn("refreshOrdersData({ deferFilter: true })", body)
        self.assertIn("if (accountOptionsLoaded !== true || ordersLoaded !== true) {", body)
        self.assertLess(
            body.index("if (accountOptionsLoaded !== true || ordersLoaded !== true) {"),
            body.index("startOrdersStream();"),
            "订单 root loader 发现账号筛选器已经因为 401/切页/失败中止后，就别继续刷订单列表了",
        )
        self.assertLess(
            body.index("if (accountOptionsLoaded !== true || ordersLoaded !== true) {"),
            body.index("startOrdersStream();"),
            "订单账号筛选器都没活着回来时，root loader 也别继续把 SSE 拉起来自娱自乐",
        )

    def test_load_orders_wrapper_stops_when_order_data_refresh_aborts(self):
        body = _extract_function_body(self.app_js, "loadOrders")

        self.assertIn("refreshOrdersData({ deferFilter: true })", body)
        self.assertIn("if (ordersLoaded !== true) {", body)
        self.assertLess(
            body.index("if (ordersLoaded !== true) {"),
            body.index("startOrdersStream();"),
            "订单主列表刷新都失败、401 或 stale 了，root loader 就别继续把 SSE 拉起来装后续链路正常",
        )
        self.assertIn("filterOrders(false);", body)
        self.assertLess(
            body.index("filterOrders(false);"),
            body.index("startOrdersStream();"),
            "订单 root loader 等筛选项和订单数据都到位后，应先按当前筛选条件重刷表格，再拉 SSE",
        )

    def test_refresh_orders_does_not_emit_cross_page_toasts_after_leaving_orders(self):
        body = _extract_function_body(self.app_js, "refreshOrders")
        toast_fragment = "showToast('订单列表已刷新', 'success');"

        self.assertIn("!document.getElementById('orders-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("!document.getElementById('orders-section')?.classList.contains('active')"),
            body.index(toast_fragment),
            "都切出订单页了，旧的刷新成功结果就别再跨页弹 success toast 了",
        )

    def test_refresh_orders_uses_action_sequence_to_ignore_older_same_page_results(self):
        body = _extract_function_body(self.app_js, "refreshOrders")

        self.assertIn("const actionRequestSequence = ++orderRefreshActionRequestSequence;", body)
        self.assertIn("actionRequestSequence !== orderRefreshActionRequestSequence", body)
        self.assertLess(
            body.index("actionRequestSequence !== orderRefreshActionRequestSequence"),
            body.index("showToast('订单列表已刷新', 'success');"),
            "同页连续手动刷新订单时，旧请求既然已经 stale 了，就别回来冒充刷新失败或刷新成功",
        )
        self.assertLess(
            body.index("actionRequestSequence !== orderRefreshActionRequestSequence"),
            body.index("showToast('订单列表刷新失败，请稍后手动刷新', 'warning');"),
            "同页新的刷新已经接管后，旧的 refreshOrders 包装层不该再把 stale 结果当成真实失败弹 warning",
        )

    def test_orders_list_loader_ignores_stale_async_responses_and_hidden_section(self):
        body = _extract_function_body(self.app_js, "loadAllOrders")

        self.assertIn("const requestSequence = ++ordersListRequestSequence;", body)
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("if (!response.ok) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("requestSequence !== ordersListRequestSequence", body)
        self.assertIn("!document.getElementById('orders-section')?.classList.contains('active')", body)
        self.assertIn("return false;", body)
        self.assertLess(
            body.index("requestSequence !== ordersListRequestSequence"),
            body.index("allOrdersData = data.data || [];"),
            "过期的订单列表请求不该晚回来后把当前订单表又糊成旧数据",
        )

    def test_orders_list_loader_handles_unauthorized_before_followup_work(self):
        body = _extract_function_body(self.app_js, "loadAllOrders")
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "订单列表加载这条 raw fetch 遇到 401 得先滚去登录，别后面还继续做 stale 判断、读错体、改页面状态",
        )

    def test_orders_list_loader_failure_reads_structured_error_messages_before_throwing(self):
        body = _extract_function_body(self.app_js, "loadAllOrders")
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertLess(
            body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("throw new Error(errorMessage);"),
            "订单列表 HTTP 失败时先把 detail/message 解出来，别整句固定异常把后端真报错吞没了",
        )

    def test_orders_list_loader_failure_rechecks_state_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "loadAllOrders")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);")
        self.assertLess(
            body.find("requestSequence !== ordersListRequestSequence", error_index),
            throw_index,
            "订单列表旧失败响应读完错误文本后，先验 request sequence，别回魂把新页面流转打断",
        )
        self.assertLess(
            body.find("!document.getElementById('orders-section')?.classList.contains('active')", error_index),
            throw_index,
            "都切出 orders 页面了，旧失败响应读完错误文本也别再跨页往 catch 里丢异常",
        )

    def test_order_account_filter_loader_ignores_stale_async_responses_and_hidden_section(self):
        body = _extract_function_body(self.app_js, "loadOrderAccountFilterOptions")

        self.assertIn("const requestSequence = ++orderAccountFilterRequestSequence;", body)
        self.assertIn("requestSequence !== orderAccountFilterRequestSequence", body)
        self.assertIn("!document.getElementById('orders-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== orderAccountFilterRequestSequence"),
            body.index("renderOrderAccountOptions(select, accounts, { includeAllOption: true });"),
            "旧的订单账号筛选器请求不该晚回来后把当前筛选选项再糊回去",
        )

    def test_order_account_filter_loader_resets_stale_options_before_fetch_and_on_failure(self):
        helper_body = _extract_function_body(self.app_js, "restoreOrderAccountFilterFailureOption")
        body = _extract_function_body(self.app_js, "loadOrderAccountFilterOptions")

        self.assertIn("const select = document.getElementById('orderAccountFilter');", body)
        self.assertIn("select.innerHTML = '<option value=\"\">所有账号</option>';", body)
        self.assertLess(
            body.index("const select = document.getElementById('orderAccountFilter');"),
            body.index("try {"),
            "订单账号筛选器失败分支也要用到 select，别把它塞进 try 作用域里让 catch 一失败先炸 ReferenceError",
        )
        self.assertLess(
            body.index("select.innerHTML = '<option value=\"\">所有账号</option>';"),
            body.index("const accounts = await fetchOrderSyncAccounts(true);"),
            "订单账号筛选器重新加载前得先清掉旧选项，失败时别挂着陈年账号装正常",
        )
        self.assertIn("if (accounts == null) {", body)
        self.assertLess(
            body.index("if (accounts == null) {"),
            body.index("renderOrderAccountOptions(select, accounts, { includeAllOption: true });"),
            "订单账号筛选器如果已经被 401 helper 中止了，就别再拿 null 账号列表往后硬怼",
        )
        self.assertIn("function restoreOrderAccountFilterFailureOption(select, previousValue) {", self.app_js)
        self.assertIn("select.innerHTML = '<option value=\"\">账号列表加载失败，请稍后重试</option>';", helper_body)
        self.assertIn("if (!previousValue) {", helper_body)
        self.assertIn("const fallbackOption = document.createElement('option');", helper_body)
        self.assertIn("fallbackOption.value = previousValue;", helper_body)
        self.assertIn("fallbackOption.textContent = `${previousValue} (当前筛选账号)`;", helper_body)
        self.assertIn("select.appendChild(fallbackOption);", helper_body)
        self.assertIn("select.value = previousValue;", helper_body)
        self.assertIn("restoreOrderAccountFilterFailureOption(select, previousValue);", body)
        self.assertIn("return true;", body)
        self.assertIn("return false;", body)
        self.assertLess(
            body.index("restoreOrderAccountFilterFailureOption(select, previousValue);"),
            body.index("console.error('加载订单账号选项失败:', error);"),
            "订单账号筛选器加载失败时得明确落成失败态，别只打日志不改界面",
        )

    def test_order_sync_account_loader_handles_unauthorized_and_reads_structured_error_messages(self):
        body = _extract_function_body(self.app_js, "fetchOrderSyncAccounts")
        self.assertIn("`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`", body)
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "历史同步账号列表这条 raw fetch 遇到 401 得先去登录，别后面还继续拿未授权响应拼异常",
        )
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertLess(
            body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("throw new Error(errorMessage);"),
            "历史同步账号列表 HTTP 失败时先把 detail/message 解出来，别只抛个裸状态码糊弄人",
        )

    def test_order_sync_account_loader_rejects_malformed_payloads_before_callers_treat_them_as_empty_lists(self):
        helper_body = _extract_function_body(self.app_js, "fetchOrderSyncAccounts")
        filter_body = _extract_function_body(self.app_js, "loadOrderAccountFilterOptions")
        modal_body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")
        failure_helper_body = _extract_function_body(self.app_js, "restoreOrderAccountFilterFailureOption")

        self.assertIn("if (!Array.isArray(accounts)) {", helper_body)
        self.assertIn("accounts.some(account => !account || typeof account !== 'object' || Array.isArray(account) || !getCookieDetailsAccountId(account))", helper_body)
        self.assertIn("throw new Error('订单账号列表返回格式异常');", helper_body)
        self.assertIn("orderHistorySyncAccounts = accounts;", helper_body)
        self.assertNotIn("orderHistorySyncAccounts = Array.isArray(accounts) ? accounts : [];", helper_body)
        self.assertIn("select.innerHTML = '<option value=\"\">账号列表加载失败，请稍后重试</option>';", failure_helper_body)
        self.assertIn("restoreOrderAccountFilterFailureOption(select, previousValue);", filter_body)
        self.assertIn("restoreOrderHistorySyncAccountFailureOption(select, failureFallbackValue);", modal_body)

        self.assertLess(
            helper_body.index("if (!Array.isArray(accounts)) {"),
            helper_body.index("orderHistorySyncAccounts = accounts;"),
            "订单账号列表接口如果回了歪 payload，helper 得先报格式异常，别把它缓存成空列表让筛选器和历史同步弹窗一起假装没账号",
        )

    def test_order_account_filter_loader_preserves_current_selection_when_reload_fails(self):
        helper_body = _extract_function_body(self.app_js, "restoreOrderAccountFilterFailureOption")
        body = _extract_function_body(self.app_js, "loadOrderAccountFilterOptions")

        self.assertIn("const previousValue = select ? select.value : '';", body)
        self.assertIn("restoreOrderAccountFilterFailureOption(select, previousValue);", body)
        self.assertIn("fallbackOption.value = previousValue;", helper_body)
        self.assertIn("fallbackOption.textContent = `${previousValue} (当前筛选账号)`;", helper_body)
        self.assertIn("select.value = previousValue;", helper_body)

        self.assertLess(
            body.index("const previousValue = select ? select.value : '';", body.index("const select = document.getElementById('orderAccountFilter');")),
            body.index("restoreOrderAccountFilterFailureOption(select, previousValue);"),
            "订单账号筛选器刷新失败时得先记住当前筛选账号，再回填失败态，不然用户一刷新过滤条件就被洗没了",
        )

    def test_order_history_sync_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        for body, anchor_fragment, label in (
            (
                _extract_function_body(self.app_js, "startOrderHistorySync"),
                "const result = await response.json().catch(() => ({}));",
                "创建历史订单同步任务",
            ),
            (
                _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus"),
                "const result = await response.json().catch(() => ({}));",
                "查询历史订单同步状态",
            ),
            (
                _extract_function_body(self.app_js, "cancelOrderHistorySync"),
                "const result = await response.json().catch(() => ({}));",
                "取消历史订单同步任务",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{label}这条 raw fetch 遇到 401 得先去登录，别后面还继续读 JSON、改状态、弹错提示装作没事",
                )

    def test_order_history_sync_actions_reject_malformed_job_payloads_before_using_job_data(self):
        for body, anchor_fragment, label in (
            (
                _extract_function_body(self.app_js, "startOrderHistorySync"),
                "activeOrderHistorySyncJobId = result.data.job_id;",
                "创建历史订单同步任务",
            ),
            (
                _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus"),
                "const job = result.data;",
                "查询历史订单同步状态",
            ),
            (
                _extract_function_body(self.app_js, "cancelOrderHistorySync"),
                "renderOrderHistorySyncJob(result.data);",
                "取消历史订单同步任务",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn("typeof result.data !== 'object'", body)
                self.assertIn("Array.isArray(result.data)", body)
                self.assertIn("!String(result.data.job_id || '').trim()", body)
                self.assertLess(
                    body.index("typeof result.data !== 'object'"),
                    body.index(anchor_fragment),
                    f"{label}如果 job payload 歪了，前端得先当格式异常拦住，别还继续解 job_id/渲染状态面板装正常",
                )

    def test_order_history_sync_actions_parse_structured_http_errors_before_throwing(self):
        start_body = _extract_function_body(self.app_js, "startOrderHistorySync")
        status_body = _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus")
        cancel_body = _extract_function_body(self.app_js, "cancelOrderHistorySync")

        for body, throw_fragment, label in (
            (start_body, "throw new Error(errorMessage);", "创建历史订单同步任务"),
            (status_body, "throw new Error(errorMessage);", "查询历史订单同步状态"),
            (cancel_body, "throw new Error(errorMessage);", "取消历史订单同步任务"),
        ):
            with self.subTest(label=label):
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(throw_fragment),
                    f"{label}这条 raw fetch 遇到 HTTP 失败时得先把 detail/message 解出来，别又糊回笼统失败",
                )

        start_error_index = start_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        self.assertLess(
            start_body.find("actionRequestSequence !== orderHistorySyncActionRequestSequence", start_error_index),
            start_body.index("throw new Error(errorMessage);", start_error_index),
            "历史同步创建任务旧失败响应读完错误文本后，先验 action sequence，别回魂给当前弹窗乱甩错",
        )

        status_error_index = status_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        self.assertLess(
            status_body.find("activeOrderHistorySyncJobId && activeOrderHistorySyncJobId !== requestedJobId", status_error_index),
            status_body.index("throw new Error(errorMessage);", status_error_index),
            "历史同步状态旧失败响应读完错误文本后，先验 requested job 还是不是当前任务，别串台后还清空面板或乱报错",
        )
        self.assertLess(
            status_body.find("if (response.status === 404) {", status_error_index),
            status_body.index("throw new Error(errorMessage);", status_error_index),
            "历史同步状态 404 时应先按当前会话安全地清理任务锚点，再把真实错误抛出去",
        )

        cancel_error_index = cancel_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        self.assertLess(
            cancel_body.find("requestedJobId !== activeOrderHistorySyncJobId", cancel_error_index),
            cancel_body.index("throw new Error(errorMessage);", cancel_error_index),
            "取消历史同步旧失败响应读完错误文本后，先验当前任务还没切走，别拿老任务错误去污染新任务面板",
        )

    def test_order_history_sync_status_and_cancel_require_response_job_id_to_match_requested_job(self):
        status_body = _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus")
        cancel_body = _extract_function_body(self.app_js, "cancelOrderHistorySync")

        self.assertIn("String(result.data.job_id || '').trim() !== requestedJobId", status_body)
        self.assertIn("String(result.data.job_id || '').trim() !== requestedJobId", cancel_body)
        self.assertLess(
            status_body.index("String(result.data.job_id || '').trim() !== requestedJobId"),
            status_body.index("const job = result.data;"),
            "查询历史同步状态如果后端回了别的 job_id，前端得先当响应串台拦住，别把别的任务状态糊到当前弹窗上",
        )
        self.assertLess(
            cancel_body.index("String(result.data.job_id || '').trim() !== requestedJobId"),
            cancel_body.index("renderOrderHistorySyncJob(result.data);"),
            "取消历史同步如果回了别的 job_id，也得先拦住，别把别的任务结果当成这次取消结果往当前面板上灌",
        )

    def test_order_history_sync_status_loader_ignores_stale_job_responses(self):
        body = _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus")
        self.assertIn("const requestedJobId = String(jobId || '').trim();", body)
        self.assertIn("fetch(`${apiBase}/api/orders/history-sync/${requestedJobId}`", body)
        self.assertIn("activeOrderHistorySyncJobId && activeOrderHistorySyncJobId !== requestedJobId", body)
        self.assertIn("return null;", body)
        self.assertNotIn("fetch(`${apiBase}/api/orders/history-sync/${jobId}`", body)

    def test_order_history_sync_terminal_status_refreshes_orders_even_when_toasts_are_silent(self):
        body = _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus")
        self.assertIn("const shouldRefreshOrders = orderHistorySyncNotifiedJobId !== job.job_id;", body)
        self.assertIn("if (shouldRefreshOrders) {", body)
        self.assertIn("if (!silentToast) {", body)
        self.assertIn("await refreshOrdersData();", body)
        self.assertNotIn("if (!silentToast && orderHistorySyncNotifiedJobId !== job.job_id) {", body)

    def test_order_history_sync_terminal_notification_marker_is_only_committed_after_refresh_survives_current_modal_session(self):
        status_body = _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus")
        cancel_body = _extract_function_body(self.app_js, "cancelOrderHistorySync")

        self.assertLess(
            status_body.index("const ordersLoaded = await refreshOrdersData();"),
            status_body.index("orderHistorySyncNotifiedJobId = job.job_id;"),
            "历史同步终态如果还没熬过订单列表刷新，就别先把 notified job 标记占坑，免得关窗/切页后重开再也不补刷新",
        )
        self.assertLess(
            status_body.rfind("|| orderHistorySyncModalRequestSequence !== modalRequestSequence", 0, status_body.index("orderHistorySyncNotifiedJobId = job.job_id;")),
            status_body.index("orderHistorySyncNotifiedJobId = job.job_id;"),
            "历史同步终态的 notified 标记至少得放在刷新后的会话校验后面，别让旧 modal 半路跑路还提前记账",
        )
        self.assertLess(
            cancel_body.index("const ordersLoaded = await refreshOrdersData();"),
            cancel_body.index("orderHistorySyncNotifiedJobId = result.data.job_id || orderHistorySyncNotifiedJobId;"),
            "取消历史同步后如果订单列表刷新还没走完，就别先把 notified job 占上，省得重开 modal 时该补的刷新补不回来",
        )

    def test_order_history_sync_terminal_actions_report_order_reload_failures(self):
        status_body = _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus")
        cancel_body = _extract_function_body(self.app_js, "cancelOrderHistorySync")

        self.assertIn("const ordersLoaded = await refreshOrdersData();", status_body)
        self.assertIn("if (ordersLoaded === false) {", status_body)
        self.assertIn("showToast(`${job.message || '历史订单同步完成'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');", status_body)
        self.assertIn("showToast(`${job.error || job.message || '历史订单同步失败'}，但订单列表刷新失败，请稍后手动刷新`, 'danger');", status_body)
        self.assertIn("showToast(`${job.message || '历史订单同步已取消'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');", status_body)
        self.assertLess(
            status_body.index("const ordersLoaded = await refreshOrdersData();"),
            status_body.index("showToast(`${job.message || '历史订单同步完成'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');"),
            "历史同步到了终态既然还会刷新订单列表，就别把刷新失败闷声吞了",
        )

        self.assertIn("const ordersLoaded = await refreshOrdersData();", cancel_body)
        self.assertIn("if (ordersLoaded === false) {", cancel_body)
        self.assertIn("showToast(`${result.data.message || '历史订单同步已取消'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');", cancel_body)
        self.assertLess(
            cancel_body.index("const ordersLoaded = await refreshOrdersData();"),
            cancel_body.index("showToast(`${result.data.message || '历史订单同步已取消'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');"),
            "取消历史同步后跟进刷新如果翻车，前端得明说，别还装列表是新的",
        )

    def test_order_history_sync_status_loader_stops_when_orders_section_leaves_or_modal_closes(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        stop_body = _extract_function_body(self.app_js, "stopOrderHistorySyncPolling")
        status_body = _extract_function_body(self.app_js, "fetchOrderHistorySyncStatus")

        self.assertIn("stopOrderHistorySyncPolling();", show_section_body)
        self.assertIn("clearTimeout(orderHistorySyncPollingTimer);", stop_body)
        self.assertIn("const modalRequestSequence = orderHistorySyncModalRequestSequence;", status_body)
        self.assertIn("modalRequestSequence !== orderHistorySyncModalRequestSequence", status_body)
        self.assertIn("!isOrdersSectionActive()", status_body)
        self.assertIn("return null;", status_body)
        self.assertLess(
            status_body.index("modalRequestSequence !== orderHistorySyncModalRequestSequence"),
            status_body.index("renderOrderHistorySyncJob(job);"),
            "历史同步状态旧请求不该在 modal 已关或切页后还回来覆盖状态面板",
        )
        self.assertLess(
            status_body.rfind("modalRequestSequence !== orderHistorySyncModalRequestSequence"),
            status_body.index("await refreshOrdersData();"),
            "历史同步状态旧请求不该在 modal 已关后还回来刷新订单列表",
        )

    def test_order_history_sync_polling_timer_requires_current_modal_and_orders_section_before_fetching_status(self):
        body = _extract_function_body(self.app_js, "scheduleOrderHistorySyncPolling")
        self.assertIn("const modalRequestSequence = orderHistorySyncModalRequestSequence;", body)
        self.assertIn("modalRequestSequence !== orderHistorySyncModalRequestSequence", body)
        self.assertIn("!isOrdersSectionActive()", body)
        self.assertIn("activeOrderHistorySyncJobId && activeOrderHistorySyncJobId !== jobId", body)
        self.assertIn("return;", body)
        self.assertLess(
            body.index("modalRequestSequence !== orderHistorySyncModalRequestSequence"),
            body.index("fetchOrderHistorySyncStatus(jobId).catch(error => {"),
            "历史同步轮询定时器到点后也得先确认 modal/页面还是当前会话，别旧 timer 还继续补刀发状态请求",
        )

    def test_order_history_sync_modal_open_ignores_stale_async_responses(self):
        self.assertIn("let orderHistorySyncModalRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")
        self.assertIn("const requestSequence = ++orderHistorySyncModalRequestSequence;", body)
        self.assertIn("requestSequence !== orderHistorySyncModalRequestSequence", body)
        self.assertIn("return null;", body)

    def test_order_history_sync_modal_account_options_reset_before_fetch_and_on_failure(self):
        helper_body = _extract_function_body(self.app_js, "restoreOrderHistorySyncAccountFailureOption")
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")

        self.assertIn("const select = document.getElementById('orderHistorySyncAccountId');", body)
        self.assertIn("select.innerHTML = '<option value=\"\">所有账号</option>';", body)
        self.assertLess(
            body.index("select.innerHTML = '<option value=\"\">所有账号</option>';"),
            body.index("const accounts = await fetchOrderSyncAccounts(true);"),
            "打开历史同步弹窗前得先清掉旧账号选项，别让上次的账号列表在新请求失败时继续装正常",
        )
        self.assertIn("if (accounts == null) {", body)
        self.assertLess(
            body.index("if (accounts == null) {"),
            body.index("renderOrderAccountOptions(select, accounts, { includeAllOption: true });"),
            "历史同步弹窗的账号列表如果已经被 401 helper 中止，就别继续拿 null 结果往下渲染表单了",
        )
        self.assertIn("function restoreOrderHistorySyncAccountFailureOption(select, previousValue) {", self.app_js)
        self.assertIn("select.innerHTML = '<option value=\"\">账号列表加载失败，请稍后重试</option>';", helper_body)
        self.assertIn("if (!previousValue) {", helper_body)
        self.assertIn("const fallbackOption = document.createElement('option');", helper_body)
        self.assertIn("fallbackOption.value = previousValue;", helper_body)
        self.assertIn("fallbackOption.textContent = `${previousValue} (当前同步账号)`;", helper_body)
        self.assertIn("select.appendChild(fallbackOption);", helper_body)
        self.assertIn("select.value = previousValue;", helper_body)
        self.assertIn("restoreOrderHistorySyncAccountFailureOption(select, failureFallbackValue);", body)
        self.assertLess(
            body.index("restoreOrderHistorySyncAccountFailureOption(select, failureFallbackValue);"),
            body.index("console.error('打开历史订单同步弹窗失败:', error);"),
            "历史同步弹窗拉账号失败时得落成失败态，别只弹个红字让旧选项继续坑人",
        )
        self.assertIn("orderHistorySyncModalInstance.show();", body)

    def test_order_history_sync_modal_preserves_page_filter_or_existing_selection_when_account_reload_fails(self):
        helper_body = _extract_function_body(self.app_js, "restoreOrderHistorySyncAccountFailureOption")
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")

        self.assertIn("const previousValue = select ? select.value : '';", body)
        self.assertIn("const pageFilterValue = document.getElementById('orderAccountFilter')?.value || '';", body)
        self.assertIn("const failureFallbackValue = previousValue || pageFilterValue;", body)
        self.assertIn("restoreOrderHistorySyncAccountFailureOption(select, failureFallbackValue);", body)
        self.assertIn("fallbackOption.value = previousValue;", helper_body)
        self.assertIn("fallbackOption.textContent = `${previousValue} (当前同步账号)`;", helper_body)
        self.assertIn("select.value = previousValue;", helper_body)

        self.assertLess(
            body.index("const failureFallbackValue = previousValue || pageFilterValue;"),
            body.index("restoreOrderHistorySyncAccountFailureOption(select, failureFallbackValue);"),
            "历史同步弹窗拉账号失败时得优先保住当前同步账号，其次保住页面筛选账号，别一失败就把用户刚选的上下文洗没",
        )

    def test_order_history_sync_modal_still_opens_and_refreshes_progress_when_account_list_load_fails(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")

        self.assertIn("if (activeOrderHistorySyncJobId) {", body)
        self.assertIn("await fetchOrderHistorySyncStatus(activeOrderHistorySyncJobId, { silentToast: true });", body)
        self.assertIn("console.warn('账号列表加载失败后刷新历史同步状态失败:', statusError);", body)
        self.assertIn("} else {", body)
        self.assertIn("resetOrderHistorySyncProgress();", body)
        self.assertIn("orderHistorySyncModalInstance.show();", body)

        self.assertLess(
            body.index("restoreOrderHistorySyncAccountFailureOption(select, failureFallbackValue);"),
            body.index("orderHistorySyncModalInstance.show();", body.index("restoreOrderHistorySyncAccountFailureOption(select, failureFallbackValue);")),
            "历史同步弹窗拉账号失败时，至少得把失败态和进度面板弹出来，别用户连重试入口都看不着",
        )
        self.assertLess(
            body.index("await fetchOrderHistorySyncStatus(activeOrderHistorySyncJobId, { silentToast: true });"),
            body.index("orderHistorySyncModalInstance.show();", body.index("await fetchOrderHistorySyncStatus(activeOrderHistorySyncJobId, { silentToast: true });")),
            "历史同步弹窗拉账号失败但任务还在跑时，应先尽量刷新当前任务进度，再把弹窗弹出来，别只剩个失败下拉框让人蒙圈",
        )

    def test_order_history_sync_modal_failure_without_active_job_still_backfills_default_form_values(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")
        else_index = body.index("} else {", body.index("if (activeOrderHistorySyncJobId) {"))
        reset_index = body.index("resetOrderHistorySyncProgress();", else_index)

        self.assertIn("const startDateInput = document.getElementById('orderHistorySyncStartDate');", body)
        self.assertIn("const endDateInput = document.getElementById('orderHistorySyncEndDate');", body)
        self.assertIn("const maxOrdersInput = document.getElementById('orderHistorySyncMaxOrders');", body)
        self.assertIn("const fetchDetailsInput = document.getElementById('orderHistorySyncFetchDetails');", body)
        self.assertLess(
            body.index("startDateInput.value = getRelativeBeijingDateInputValue(-30);", else_index),
            reset_index,
            "历史同步弹窗拉账号失败且当前没活动任务时，也得先把开始日期默认值补上，不然用户进来就一堆空字段发懵",
        )
        self.assertLess(
            body.index("endDateInput.value = getRelativeBeijingDateInputValue(0);", else_index),
            reset_index,
            "历史同步弹窗拉账号失败且当前没活动任务时，也得把结束日期默认值补上，别要求用户先自己猜今天日期",
        )
        self.assertLess(
            body.index("maxOrdersInput.value = '120';", else_index),
            reset_index,
            "历史同步弹窗拉账号失败但没活动任务时，最多同步单数的默认值也别丢，不然表单像半残废一样",
        )
        self.assertLess(
            body.index("fetchDetailsInput.checked = true;", else_index),
            reset_index,
            "历史同步弹窗拉账号失败但没活动任务时，详情同步开关也该保默认开启，别因为账号列表挂了就把整套表单状态一起搞丢",
        )

    def test_order_history_sync_modal_prefers_existing_selection_before_falling_back_to_page_filter(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")

        self.assertIn("const previousValue = select ? select.value : '';", body)
        self.assertIn("const hasPreviousOption = previousValue && Array.from(select.options).some(option => option.value === previousValue);", body)
        self.assertIn("const hasPageFilterOption = pageFilterValue && Array.from(select.options).some(option => option.value === pageFilterValue);", body)
        self.assertIn("select.value = hasPreviousOption", body)
        self.assertNotIn("select.value = hasPageFilterOption ? pageFilterValue : '';", body)

        self.assertLess(
            body.index("const hasPreviousOption = previousValue && Array.from(select.options).some(option => option.value === previousValue);"),
            body.index("const hasPageFilterOption = pageFilterValue && Array.from(select.options).some(option => option.value === pageFilterValue);"),
            "历史同步弹窗重开时应先看当前弹窗自己选过的账号还在不在，别一上来就被页面筛选账号顶掉",
        )
        self.assertLess(
            body.index("const hasPreviousOption = previousValue && Array.from(select.options).some(option => option.value === previousValue);"),
            body.index("select.value = hasPreviousOption"),
            "历史同步弹窗应优先保住用户刚才在弹窗里选的账号，别成功刷新一次账号列表就把选择改回页面筛选值",
        )

    def test_order_history_sync_modal_only_restores_page_filter_account_when_option_still_exists(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")
        self.assertIn("const hasPageFilterOption = pageFilterValue && Array.from(select.options).some(option => option.value === pageFilterValue);", body)
        self.assertIn(": (hasPageFilterOption ? pageFilterValue : '');", body)
        self.assertNotIn("select.value = pageFilterValue || '';", body)

    def test_order_history_sync_modal_clears_stale_active_job_and_backfills_idle_defaults_when_status_refresh_404s(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")
        status_refresh_index = body.index("await fetchOrderHistorySyncStatus(activeOrderHistorySyncJobId, { silentToast: true });")
        fallback_idle_index = body.index("if (!activeOrderHistorySyncJobId && !restoredIdleState) {")

        self.assertIn("let restoredIdleState = false;", body)
        self.assertIn("restoredIdleState = true;", body)
        self.assertIn("if (!activeOrderHistorySyncJobId && !restoredIdleState) {", body)
        self.assertLess(
            status_refresh_index,
            fallback_idle_index,
            "历史同步弹窗拉账号失败后，如果顺手发现活动任务其实已经 404 清掉了，也得补一轮 idle 默认表单，别弹窗打开了还是半残状态",
        )

    def test_order_history_sync_status_render_preserves_requested_account_even_if_option_list_no_longer_contains_it(self):
        helper_body = _extract_function_body(self.app_js, "ensureOrderHistorySyncAccountOption")
        render_body = _extract_function_body(self.app_js, "renderOrderHistorySyncJob")

        self.assertIn("function ensureOrderHistorySyncAccountOption(select, accountId) {", self.app_js)
        self.assertIn("const normalizedAccountId = String(accountId || '').trim();", helper_body)
        self.assertIn("if (Array.from(select.options).some(option => option.value === normalizedAccountId)) {", helper_body)
        self.assertIn("const fallbackOption = document.createElement('option');", helper_body)
        self.assertIn("fallbackOption.value = normalizedAccountId;", helper_body)
        self.assertIn("fallbackOption.textContent = `${normalizedAccountId} (当前同步账号)`;", helper_body)
        self.assertIn("select.appendChild(fallbackOption);", helper_body)

        self.assertIn("const requestedAccountId = String(request.account_id || '').trim();", render_body)
        self.assertIn("ensureOrderHistorySyncAccountOption(accountSelect, requestedAccountId);", render_body)
        self.assertIn("accountSelect.value = requestedAccountId || '';", render_body)

        self.assertLess(
            render_body.index("ensureOrderHistorySyncAccountOption(accountSelect, requestedAccountId);"),
            render_body.index("accountSelect.value = requestedAccountId || '';"),
            "历史同步状态面板在回填任务账号前，得先保证下拉里有这个账号选项，别任务还在跑，账号选择器先自己掉成空白",
        )

    def test_order_history_sync_modal_requests_are_invalidated_when_closing_or_leaving_orders(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("orderHistorySyncModalRequestSequence += 1;", show_section_body)
        self.assertIn("orderHistorySyncModalInstance.hide();", show_section_body)
        self.assertIn("orderHistorySyncModal.addEventListener('hidden.bs.modal', () => {", self.app_js)
        self.assertIn("orderHistorySyncModalRequestSequence += 1;", self.app_js)

    def test_orders_section_switch_closes_order_detail_modal_and_invalidates_item_detail_requests(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        self.assertIn("orderDetailItemRequestSequence += 1;", show_section_body)
        self.assertIn("const orderDetailModalElement = document.getElementById('orderDetailModal');", show_section_body)
        self.assertIn("orderDetailModal.hide();", show_section_body)

    def test_order_history_sync_modal_loader_ignores_hidden_orders_section(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")

        self.assertIn("!isOrdersSectionActive()", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("!isOrdersSectionActive()"),
            body.index("orderHistorySyncModalInstance.show();"),
            "订单页都切走了，历史同步弹窗的旧请求就别晚回来再自己弹出来吓人",
        )

    def test_order_history_sync_modal_only_restores_page_filter_account_when_option_still_exists(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")
        self.assertIn("const hasPageFilterOption = pageFilterValue && Array.from(select.options).some(option => option.value === pageFilterValue);", body)
        self.assertIn(": (hasPageFilterOption ? pageFilterValue : '');", body)
        self.assertNotIn("select.value = pageFilterValue || '';", body)

    def test_order_history_sync_modal_default_field_backfill_respects_current_modal_session(self):
        body = _extract_function_body(self.app_js, "openOrderHistorySyncModal")
        self.assertLess(
            body.rfind("requestSequence !== orderHistorySyncModalRequestSequence", 0, body.index("startDateInput.value = getRelativeBeijingDateInputValue(-30);")),
            body.index("startDateInput.value = getRelativeBeijingDateInputValue(-30);"),
            "历史同步弹窗旧请求不该晚回来后把当前会话刚改过的开始日期又糊回默认值",
        )
        self.assertLess(
            body.rfind("requestSequence !== orderHistorySyncModalRequestSequence", 0, body.index("endDateInput.value = getRelativeBeijingDateInputValue(0);")),
            body.index("endDateInput.value = getRelativeBeijingDateInputValue(0);"),
            "历史同步弹窗旧请求不该晚回来后把当前会话刚改过的结束日期又糊回默认值",
        )
        self.assertLess(
            body.rfind("requestSequence !== orderHistorySyncModalRequestSequence", 0, body.index("maxOrdersInput.value = '120';")),
            body.index("maxOrdersInput.value = '120';"),
            "历史同步弹窗旧请求不该晚回来后把当前会话刚改过的最大同步单数又糊回默认值",
        )
        self.assertLess(
            body.rfind("requestSequence !== orderHistorySyncModalRequestSequence", 0, body.index("fetchDetailsInput.checked = true;")),
            body.index("fetchDetailsInput.checked = true;"),
            "历史同步弹窗旧请求不该晚回来后把当前会话刚改过的详情同步开关又糊回默认值",
        )

    def test_order_history_sync_start_request_ignores_hidden_orders_section_and_closed_modal(self):
        body = _extract_function_body(self.app_js, "startOrderHistorySync")

        self.assertIn("const modalRequestSequence = orderHistorySyncModalRequestSequence;", body)
        self.assertIn("modalRequestSequence !== orderHistorySyncModalRequestSequence", body)
        self.assertIn("!isOrdersSectionActive()", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("modalRequestSequence !== orderHistorySyncModalRequestSequence"),
            body.index("renderOrderHistorySyncJob(result.data);"),
            "历史同步创建任务的旧响应不该在 modal 已关后还回来覆盖进度面板",
        )
        self.assertLess(
            body.index("modalRequestSequence !== orderHistorySyncModalRequestSequence"),
            body.index("showToast('历史订单同步已开始', 'success');"),
            "历史同步创建任务的旧响应不该在 modal 已关后还回来弹成功 toast",
        )
        finally_block = body.split("} finally {", 1)[1]
        self.assertIn("actionRequestSequence !== orderHistorySyncActionRequestSequence", finally_block)
        self.assertIn("modalRequestSequence !== orderHistorySyncModalRequestSequence", finally_block)
        self.assertIn("!isOrdersSectionActive()", finally_block)
        self.assertLess(
            finally_block.index("actionRequestSequence !== orderHistorySyncActionRequestSequence"),
            finally_block.index("startBtn.innerHTML = '<i class=\"bi bi-play-circle\"></i> 开始同步';"),
            "历史同步旧请求的 finally 不该在新会话或切页后还把当前按钮文案还原成老状态",
        )

    def test_order_history_sync_start_request_does_not_let_older_responses_overwrite_newer_job_anchor(self):
        body = _extract_function_body(self.app_js, "startOrderHistorySync")
        self.assertIn("let orderHistorySyncActionRequestSequence = 0;", self.app_js)
        self.assertIn("actionRequestSequence = ++orderHistorySyncActionRequestSequence;", body)
        self.assertIn("actionRequestSequence !== orderHistorySyncActionRequestSequence", body)
        self.assertLess(
            body.index("actionRequestSequence !== orderHistorySyncActionRequestSequence"),
            body.index("activeOrderHistorySyncJobId = result.data.job_id;"),
            "旧的历史同步创建响应不该在更新任务锚点前插队，把更新发起的新任务ID顶掉",
        )

    def test_order_history_sync_start_request_does_not_let_closed_or_reopened_modal_overwrite_job_anchor(self):
        body = _extract_function_body(self.app_js, "startOrderHistorySync")
        anchor_line = "activeOrderHistorySyncJobId = result.data.job_id;"

        self.assertIn("modalRequestSequence !== orderHistorySyncModalRequestSequence", body)
        self.assertIn("!isOrdersSectionActive()", body)
        self.assertIn(anchor_line, body)
        self.assertLess(
            body.rfind("modalRequestSequence !== orderHistorySyncModalRequestSequence", 0, body.index(anchor_line)),
            body.index(anchor_line),
            "旧 modal 会话的历史同步创建响应不该在界面都关了或重开后，还偷偷把当前任务锚点改回去",
        )
        self.assertLess(
            body.rfind("!isOrdersSectionActive()", 0, body.index(anchor_line)),
            body.index(anchor_line),
            "切出 orders 之后，旧的历史同步创建响应也不该再改当前任务锚点",
        )

    def test_order_history_sync_start_action_sequence_starts_only_after_validation(self):
        body = _extract_function_body(self.app_js, "startOrderHistorySync")
        self.assertLess(
            body.index("if (!startDate || !endDate) {"),
            body.index("actionRequestSequence = ++orderHistorySyncActionRequestSequence;"),
            "开始或结束日期都没填时只是前端校验，别先把历史同步 action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (startDate > endDate) {"),
            body.index("actionRequestSequence = ++orderHistorySyncActionRequestSequence;"),
            "日期范围都不合法了，就别提前推进历史同步 action sequence 乱作废别的请求",
        )
        self.assertLess(
            body.index("if (!Number.isFinite(maxOrders) || maxOrders < 1 || maxOrders > 500) {"),
            body.index("actionRequestSequence = ++orderHistorySyncActionRequestSequence;"),
            "最多同步单数只是前端校验，别还没发请求就先把历史同步 action sequence 顶掉",
        )

    def test_order_history_sync_cancel_request_ignores_hidden_orders_section_and_stale_job_identity(self):
        body = _extract_function_body(self.app_js, "cancelOrderHistorySync")

        self.assertIn("const requestedJobId = activeOrderHistorySyncJobId;", body)
        self.assertIn("fetch(`${apiBase}/api/orders/history-sync/${requestedJobId}/cancel`", body)
        self.assertIn("const modalRequestSequence = orderHistorySyncModalRequestSequence;", body)
        self.assertIn("requestedJobId !== activeOrderHistorySyncJobId", body)
        self.assertIn("modalRequestSequence !== orderHistorySyncModalRequestSequence", body)
        self.assertIn("!isOrdersSectionActive()", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestedJobId !== activeOrderHistorySyncJobId"),
            body.index("renderOrderHistorySyncJob(result.data);"),
            "取消历史同步的旧响应不该在任务已切换后还回来覆盖当前状态面板",
        )
        self.assertLess(
            body.index("modalRequestSequence !== orderHistorySyncModalRequestSequence"),
            body.index("showToast(result.data.message || '历史订单同步已取消', 'warning');"),
            "取消历史同步的旧响应不该在 modal 已关后还回来弹提示",
        )

    def test_order_history_sync_cancel_request_uses_action_sequence_to_ignore_older_responses(self):
        body = _extract_function_body(self.app_js, "cancelOrderHistorySync")
        self.assertIn("const actionRequestSequence = ++orderHistorySyncActionRequestSequence;", body)
        self.assertIn("actionRequestSequence !== orderHistorySyncActionRequestSequence", body)
        self.assertLess(
            body.index("actionRequestSequence !== orderHistorySyncActionRequestSequence"),
            body.index("renderOrderHistorySyncJob(result.data);"),
            "旧的取消响应不该在新一轮历史同步操作之后还回来覆盖当前状态面板",
        )

    def test_search_result_item_cards_handle_missing_titles_and_only_use_safe_http_detail_urls(self):
        body = _extract_function_body(self.app_js, "createItemCard")
        self.assertIn("const itemTitle = String(item.title || '未命名商品');", body)
        self.assertIn("const imageUrl = normalizeSafeHttpUrl(item.main_image || item.image_url) || 'https://via.placeholder.com/200x200?text=图片加载失败';", body)
        self.assertIn("const safeImageUrl = escapeHtmlAttribute(imageUrl);", body)
        self.assertIn("const detailUrl = normalizeSafeHttpUrl(item.item_url || item.url) || 'about:blank';", body)
        self.assertIn("const safeDetailUrl = escapeHtmlAttribute(detailUrl);", body)
        self.assertIn("const safeItemTitleAttr = escapeHtmlAttribute(itemTitle);", body)
        self.assertIn('alt="${safeItemTitleAttr}"', body)
        self.assertIn('title="${safeItemTitleAttr}"', body)
        self.assertIn("${escapeHtml(itemTitle.length > 50 ? itemTitle.substring(0, 50) + '...' : itemTitle)}", body)
        self.assertIn('href="${safeDetailUrl}"', body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertNotIn("item.title.length > 50 ? item.title.substring(0, 50) + '...' : item.title", body)
        self.assertNotIn("const imageUrl = item.main_image || item.image_url || 'https://via.placeholder.com/200x200?text=图片加载失败';", body)
        self.assertNotIn('href="${escapeHtml(item.item_url || item.url)}"', body)

    def test_item_search_export_uses_current_detail_and_image_field_mappings(self):
        body = _extract_function_body(self.app_js, "exportSearchResults")
        self.assertIn("'商品链接': normalizeSafeHttpUrl(item.item_url || item.url) || ''", body)
        self.assertIn("'图片链接': item.main_image || item.image_url || ''", body)
        self.assertNotIn("'商品链接': item.url,", body)
        self.assertNotIn("'图片链接': item.image_url", body)

    def test_item_search_export_escapes_double_quotes_in_csv_cells(self):
        body = _extract_function_body(self.app_js, "exportSearchResults")
        self.assertIn("const toCsvCell = (value) => `\"${String(value == null ? '' : value).replace(/\"/g, '\"\"')}\"`;", body)
        self.assertIn("headers.map(header => toCsvCell(row[header])).join(',')", body)
        self.assertNotIn("headers.map(header => `\"${row[header] || ''}\"`).join(',')", body)

    def test_item_search_export_neutralizes_spreadsheet_formula_prefixes(self):
        body = _extract_function_body(self.app_js, "exportSearchResults")
        self.assertIn("const sanitizeCsvFormulaValue = (value) => {", body)
        self.assertIn("return /^[=+\\-@]/.test(stringValue) ? `'${stringValue}` : stringValue;", body)
        self.assertIn("const sanitizedExportData = exportData.map(row => Object.fromEntries(", body)
        self.assertIn("Object.entries(row).map(([key, value]) => [key, sanitizeCsvFormulaValue(value)])", body)
        self.assertIn("const headers = Object.keys(sanitizedExportData[0]);", body)
        self.assertIn("...sanitizedExportData.map(row => headers.map(header => toCsvCell(row[header])).join(','))", body)

    def test_item_search_export_revokes_blob_url_after_download(self):
        body = _extract_function_body(self.app_js, "exportSearchResults")
        self.assertIn("const url = URL.createObjectURL(blob);", body)
        self.assertIn("URL.revokeObjectURL(url);", body)
        self.assertLess(
            body.index("document.body.removeChild(link);"),
            body.index("URL.revokeObjectURL(url);"),
            "商品搜索结果导出完得把 Blob URL 释放掉，别导一次漏一次对象 URL",
        )

    def test_export_download_filenames_use_beijing_calendar_dates(self):
        item_search_body = _extract_function_body(self.app_js, "exportSearchResults")
        export_keywords_body = _extract_function_body(self.app_js, "exportKeywords")
        export_table_body = _extract_function_body(self.app_js, "exportTableData")

        self.assertIn("`商品搜索结果_${getBeijingDateKey(new Date())}.csv`", item_search_body)
        self.assertNotIn("`商品搜索结果_${new Date().toISOString().slice(0, 10)}.csv`", item_search_body)

        self.assertIn("`关键词数据_${requestedAccountId}_${getBeijingDateKey(new Date())}.xlsx`", export_keywords_body)
        self.assertNotIn("`关键词数据_${requestedAccountId}_${new Date().toISOString().slice(0, 10)}.xlsx`", export_keywords_body)

        self.assertIn("let downloadName = `${exportTable}_${getBeijingDateKey(new Date())}.xlsx`;", export_table_body)
        self.assertNotIn("let downloadName = `${exportTable}_${new Date().toISOString().slice(0, 10)}.xlsx`;", export_table_body)

    def test_item_search_export_catch_failures_surface_runtime_error_messages(self):
        body = _extract_function_body(self.app_js, "exportSearchResults")
        self.assertNotIn("showToast('导出搜索结果失败', 'danger');", body)
        self.assertIn("showToast(`导出搜索结果失败: ${error.message || '请稍后重试'}`, 'danger');", body)

    def test_account_management_inline_edit_flows_do_not_leave_debug_console_logs(self):
        for function_name in ("loadAccounts", "editRemark", "editPauseDuration"):
            body = _extract_function_body(self.app_js, function_name)
            self.assertNotIn("console.log(", body)

    def test_account_diagnostics_runtime_refresh_ignores_stale_async_responses(self):
        self.assertIn("let aboutRuntimeRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadAboutRuntimeStatus")
        self.assertIn("suppressErrorToast: true", body)
        self.assertIn("const result = await fetchJSONWithoutGlobalLoading(`${apiBase}/accounts/${encodeURIComponent(normalizedAccountId)}/runtime-status`, {", body)
        self.assertNotIn("const result = await fetchJSON(`${apiBase}/accounts/${encodeURIComponent(normalizedAccountId)}/runtime-status`, {", body)
        self.assertIn("const requestSequence = ++aboutRuntimeRequestSequence;", body)
        self.assertIn("if (requestSequence !== aboutRuntimeRequestSequence || getAboutSelectedAccountId() !== normalizedAccountId) {", body)
        self.assertIn("return false;", body)

    def test_account_diagnostics_root_loader_ignores_stale_async_responses_and_hidden_accounts_section(self):
        self.assertIn("let aboutDiagnosticsLoadRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "loadAboutDiagnostics")

        self.assertIn("suppressErrorToast: true", body)
        self.assertIn("const accounts = await fetchJSON(`${apiBase}/accounts/details?summary_only=true`, {", body)
        self.assertIn("aboutDiagnosticsLoadRequestSequence += 1;", show_section_body)
        self.assertIn("const requestSequence = ++aboutDiagnosticsLoadRequestSequence;", body)
        self.assertIn("requestSequence !== aboutDiagnosticsLoadRequestSequence", body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== aboutDiagnosticsLoadRequestSequence"),
            body.index("aboutDiagnosticsAccounts = Array.isArray(accounts) ? accounts : [];"),
            "旧的账号诊断列表请求不该晚回来后把当前隐藏页的诊断账号选项重新糊回去",
        )
        self.assertLess(
            body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index("await loadAboutRuntimeStatus(nextAccountId);")),
            body.index("await loadAboutRuntimeStatus(nextAccountId);"),
            "都切出账号页了，旧诊断根加载不该继续触发运行态子加载",
        )

    def test_account_diagnostics_root_loader_clears_stale_state_when_account_list_refresh_fails(self):
        body = _extract_function_body(self.app_js, "loadAboutDiagnostics")

        self.assertIn("aboutDiagnosticsAccounts = [];", body)
        self.assertIn("accountSelect.disabled = true;", body)
        self.assertIn("accountSelect.innerHTML = '<option value=\"\">加载失败，请重试</option>';", body)
        self.assertIn("aboutRuntimeRequestSequence += 1;", body)
        self.assertIn("renderAboutAccountMeta(null);", body)
        self.assertIn("renderAboutRuntimePlaceholder('加载账号保活诊断失败', '请稍后重试。');", body)
        self.assertIn("renderAboutHistoryPlaceholder('暂无历史消息', '账号保活诊断加载失败，请稍后重试。');", body)
        self.assertIn("return false;", body)

    def test_account_diagnostics_requests_are_invalidated_when_leaving_accounts(self):
        self.assertIn("let aboutRuntimeRequestSequence = 0;", self.app_js)
        self.assertIn("let aboutKeepaliveActionRequestSequence = 0;", self.app_js)
        self.assertIn("let aboutConversationHistoryRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("aboutRuntimeRequestSequence += 1;", show_section_body)
        self.assertIn("aboutKeepaliveActionRequestSequence += 1;", show_section_body)
        self.assertIn("aboutConversationHistoryRequestSequence += 1;", show_section_body)

    def test_account_diagnostics_refresh_only_reports_success_when_runtime_reload_succeeds(self):
        load_body = _extract_function_body(self.app_js, "loadAboutRuntimeStatus")
        refresh_body = _extract_function_body(self.app_js, "refreshAboutDiagnosticsStatus")

        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("const loaded = await loadAboutRuntimeStatus(accountId);", refresh_body)
        self.assertIn("if (loaded) {", refresh_body)
        self.assertIn("showToast(`账号 \"${accountId}\" 运行态已刷新`, 'success');", refresh_body)
        self.assertNotIn("await loadAboutRuntimeStatus(accountId);\n        showToast(`账号 \"${accountId}\" 运行态已刷新`, 'success');", refresh_body)

    def test_account_diagnostics_refresh_does_not_emit_cross_page_toasts_after_leaving_accounts(self):
        body = _extract_function_body(self.app_js, "refreshAboutDiagnosticsStatus")
        toast_fragment = "showToast(`账号 \"${accountId}\" 运行态已刷新`, 'success');"

        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("!document.getElementById('accounts-section')?.classList.contains('active')"),
            body.index(toast_fragment),
            "都切出账号页了，旧的运行态刷新成功就别再跨页弹 success toast 了",
        )

        self.assertIn("getAboutSelectedAccountId() !== accountId", body)
        self.assertLess(
            body.index("getAboutSelectedAccountId() !== accountId"),
            body.index("refreshButton.disabled = false;"),
            "账号都切走了，旧的诊断刷新 finally 就别把当前按钮状态回写回去了",
        )
        self.assertLess(
            body.index("getAboutSelectedAccountId() !== accountId"),
            body.index("refreshButton.innerHTML = originalHtml;"),
            "账号都切走了，旧的诊断刷新 finally 也别把当前按钮文案还原成老会话的内容",
        )

    def test_account_diagnostics_keepalive_and_history_ignore_stale_account_switches_and_hidden_state(self):
        keepalive_body = _extract_function_body(self.app_js, "triggerAboutSessionKeepalive")
        history_body = _extract_function_body(self.app_js, "loadAboutConversationHistory")

        self.assertIn("const result = await fetchJSONWithoutGlobalLoading(`${apiBase}/accounts/${encodeURIComponent(accountId)}/session-keepalive`, {", keepalive_body)
        self.assertNotIn("const result = await fetchJSON(`${apiBase}/accounts/${encodeURIComponent(accountId)}/session-keepalive`, {", keepalive_body)
        self.assertIn("const requestedAccountId = accountId;", keepalive_body)
        self.assertIn("const actionRequestSequence = ++aboutKeepaliveActionRequestSequence;", keepalive_body)
        self.assertIn("actionRequestSequence !== aboutKeepaliveActionRequestSequence", keepalive_body)
        self.assertIn("getAboutSelectedAccountId() !== requestedAccountId", keepalive_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", keepalive_body)
        self.assertIn("return null;", keepalive_body)
        self.assertLess(
            keepalive_body.index("actionRequestSequence !== aboutKeepaliveActionRequestSequence"),
            keepalive_body.index("renderAboutAccountMeta(targetAccount);"),
            "旧的轻保活响应不该晚回来后把当前诊断账号的运行态又改回去",
        )
        self.assertLess(
            keepalive_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, keepalive_body.index("showToast(result?.message || '轻保活已执行', result?.success ? 'success' : 'warning');")),
            keepalive_body.index("showToast(result?.message || '轻保活已执行', result?.success ? 'success' : 'warning');"),
            "都切出账号页了，旧的轻保活结果不该再跨页弹 toast",
        )
        self.assertIn("suppressErrorToast: true", keepalive_body)
        self.assertIn("showToast(error?.message || '执行轻保活失败', 'danger');", keepalive_body)
        keepalive_finally_block = keepalive_body.split("} finally {", 1)[1]
        self.assertIn("actionRequestSequence !== aboutKeepaliveActionRequestSequence", keepalive_finally_block)
        self.assertIn("getAboutSelectedAccountId() !== requestedAccountId", keepalive_finally_block)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", keepalive_finally_block)
        self.assertLess(
            keepalive_finally_block.index("actionRequestSequence !== aboutKeepaliveActionRequestSequence"),
            keepalive_finally_block.index("keepaliveButton.disabled = false;"),
            "同页已经发起了更新的轻保活动作后，旧 finally 就别把当前按钮 disabled 状态回写回去了",
        )
        self.assertLess(
            keepalive_finally_block.index("actionRequestSequence !== aboutKeepaliveActionRequestSequence"),
            keepalive_finally_block.index("keepaliveButton.innerHTML = originalHtml;"),
            "同页已经发起了更新的轻保活动作后，旧 finally 也别把当前按钮文案还原成老会话的内容",
        )

        self.assertIn("const result = await fetchJSONWithoutGlobalLoading(", history_body)
        self.assertNotIn("const result = await fetchJSON(", history_body)
        self.assertIn("const requestedAccountId = accountId;", history_body)
        self.assertIn("const requestedConversationId = conversationId;", history_body)
        self.assertIn("const requestSequence = ++aboutConversationHistoryRequestSequence;", history_body)
        self.assertIn("requestSequence !== aboutConversationHistoryRequestSequence", history_body)
        self.assertIn("getAboutSelectedAccountId() !== requestedAccountId", history_body)
        self.assertIn("conversationInput?.value?.trim() !== requestedConversationId", history_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", history_body)
        self.assertIn("return null;", history_body)
        self.assertIn("suppressErrorToast: true", history_body)
        self.assertLess(
            history_body.index("requestSequence !== aboutConversationHistoryRequestSequence"),
            history_body.index("renderAboutConversationHistory(result?.messages || [], {"),
            "旧的历史消息查询结果不该晚回来后把当前会话记录糊回去",
        )
        self.assertLess(
            history_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, history_body.index("showToast(`账号 \"${accountId}\" 历史消息查询完成`, 'success');")),
            history_body.index("showToast(`账号 \"${accountId}\" 历史消息查询完成`, 'success');"),
            "都切出账号页了，旧的历史消息查询成功不该再跨页弹 success toast",
        )
        history_finally_block = history_body.split("} finally {", 1)[1]
        self.assertIn("requestSequence !== aboutConversationHistoryRequestSequence", history_finally_block)
        self.assertIn("getAboutSelectedAccountId() !== requestedAccountId", history_finally_block)
        self.assertIn("conversationInput?.value?.trim() !== requestedConversationId", history_finally_block)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", history_finally_block)
        self.assertLess(
            history_finally_block.index("requestSequence !== aboutConversationHistoryRequestSequence"),
            history_finally_block.index("historyButton.disabled = false;"),
            "同会话里连续按 Enter 重查时，旧 finally 不该抢先把当前查询按钮恢复可点",
        )
        self.assertLess(
            history_finally_block.index("getAboutSelectedAccountId() !== requestedAccountId"),
            history_finally_block.index("historyButton.disabled = false;"),
            "账号都切走了，旧的历史消息查询 finally 就别把当前按钮 disabled 状态回写回去了",
        )
        self.assertLess(
            history_finally_block.index("getAboutSelectedAccountId() !== requestedAccountId"),
            history_finally_block.index("historyButton.innerHTML = originalHtml;"),
            "账号都切走了，旧的历史消息查询 finally 也别把当前按钮文案还原成老会话的内容",
        )

    def test_system_settings_admin_visibility_uses_centralized_helper_and_safe_failure_fallback(self):
        helper_body = _extract_function_body(self.app_js, "setSystemSettingsAdminVisibility")
        load_system_settings_body = _extract_function_body(self.app_js, "loadSystemSettings")

        for element_id in (
            "api-security-settings",
            "login-info-settings",
            "outgoing-configs",
            "backup-management",
            "system-restart-btn",
            "dashboardHotUpdateGroup",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f"document.getElementById('{element_id}')", helper_body)

        self.assertIn("setSystemSettingsAdminVisibility(isAdmin);", load_system_settings_body)
        self.assertIn("setSystemSettingsAdminVisibility(false);", load_system_settings_body)
        self.assertIn("if (!response.ok) {", load_system_settings_body)

    def test_load_system_settings_verify_uses_unauthorized_helper_and_structured_error_messages(self):
        body = _extract_function_body(self.app_js, "loadSystemSettings")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');", body)

        unauthorized_index = body.index("if (handleUnauthorizedApiResponse(response)) {")
        response_ok_index = body.index("if (!response.ok) {")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);", error_index)
        toast_index = body.index("showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');")

        self.assertLess(
            unauthorized_index,
            response_ok_index,
            "系统设置权限校验碰到 401 得先走统一未授权处理，别在这儿自己装大拿瞎兜底",
        )
        self.assertLess(
            error_index,
            throw_index,
            "系统设置权限校验 HTTP 失败时得先把 detail/message 解出来，别继续闷头吞后端错误",
        )
        self.assertLess(
            throw_index,
            toast_index,
            "系统设置权限校验得把真实后端错误带进 catch toast，别又给抹成一坨固定红字",
        )

    def test_load_system_settings_rechecks_request_state_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "loadSystemSettings")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);", error_index)

        self.assertLess(
            body.find("requestSequence !== systemSettingsLoadRequestSequence", error_index),
            throw_index,
            "系统设置权限校验旧失败响应读完错误体后，先验当前 load request 还活着，别让过期结果回来诈尸",
        )
        self.assertLess(
            body.find("!isSystemSettingsSectionActive()", error_index),
            throw_index,
            "系统设置权限校验读完错误体后得先看页面还在不在，别切页了还回来甩 danger toast",
        )

    def test_load_system_settings_does_not_leave_debug_console_logs(self):
        body = _extract_function_body(self.app_js, "loadSystemSettings")
        self.assertNotIn("console.log(", body)

    def test_system_settings_child_loaders_reset_stale_values_when_loading_fails(self):
        api_security_body = _extract_function_body(self.app_js, "loadAPISecuritySettings")
        registration_body = _extract_function_body(self.app_js, "loadRegistrationSettings")
        login_info_body = _extract_function_body(self.app_js, "loadLoginInfoSettings")
        debounce_body = _extract_function_body(self.app_js, "loadDebounceDelay")
        outgoing_body = _extract_function_body(self.app_js, "loadOutgoingConfigs")

        self.assertIn("resetApiSecuritySettingsFields();", api_security_body)
        self.assertIn("if (!response.ok) {", api_security_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", api_security_body)
        self.assertIn("throw new Error(errorMessage);", api_security_body)

        self.assertIn("resetRegistrationSettingsField();", registration_body)
        self.assertIn("if (!response.ok) {", registration_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", registration_body)
        self.assertIn("throw new Error(errorMessage);", registration_body)

        self.assertIn("resetLoginInfoSettingsFields();", login_info_body)
        self.assertIn("if (!response.ok) {", login_info_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", login_info_body)
        self.assertIn("throw new Error(errorMessage);", login_info_body)

        self.assertIn("resetDebounceDelayField();", debounce_body)
        self.assertIn("if (!response.ok) {", debounce_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", debounce_body)
        self.assertIn("throw new Error(errorMessage);", debounce_body)

        self.assertIn("renderOutgoingConfigsLoadFailure();", outgoing_body)
        self.assertIn("if (!response.ok) {", outgoing_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", outgoing_body)
        self.assertIn("throw new Error(errorMessage);", outgoing_body)

    def test_registration_and_login_info_loaders_do_not_fail_silently_on_http_errors(self):
        api_security_body = _extract_function_body(self.app_js, "loadAPISecuritySettings")
        registration_body = _extract_function_body(self.app_js, "loadRegistrationSettings")
        login_info_body = _extract_function_body(self.app_js, "loadLoginInfoSettings")
        outgoing_body = _extract_function_body(self.app_js, "loadOutgoingConfigs")

        for body, toast_fragment, label in (
            (api_security_body, "showToast(`加载API安全设置失败: ${error.message || '请稍后重试'}`, 'danger');", "API 安全设置"),
            (registration_body, "showToast(`加载注册设置失败: ${error.message || '请稍后重试'}`, 'danger');", "注册设置"),
            (login_info_body, "showToast(`加载登录信息设置失败: ${error.message || '请稍后重试'}`, 'danger');", "登录信息设置"),
            (outgoing_body, "showToast(`加载外发配置失败: ${error.message || '请稍后重试'}`, 'danger');", "外发配置"),
        ):
            with self.subTest(label=label):
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertIn("throw new Error(errorMessage);", body)
                self.assertIn(toast_fragment, body)

    def test_debounce_delay_loader_does_not_fail_silently_on_http_errors(self):
        body = _extract_function_body(self.app_js, "loadDebounceDelay")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("showToast(`加载防抖延迟设置失败: ${error.message || '请稍后重试'}`, 'danger');", body)

        toast_index = body.index("showToast(`加载防抖延迟设置失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            body.rfind("requestSequence !== systemSettingsLoadRequestSequence", 0, toast_index),
            toast_index,
            "防抖延迟加载都变旧了，就别回魂甩 danger toast 吓当前页面",
        )
        self.assertLess(
            body.rfind("actionRequestSequence !== systemSettingsMutationActionRequestSequence", 0, toast_index),
            toast_index,
            "防抖延迟加载失败进 catch 前也得先验 action sequence，别新保存都发了旧异常还回来抢戏",
        )
        self.assertLess(
            body.rfind("!isSystemSettingsSectionActive()", 0, toast_index),
            toast_index,
            "都切出系统设置页了，旧的防抖延迟加载失败也别跨页回来甩红字",
        )

    def test_system_settings_failed_loaders_recheck_request_state_after_error_body_read(self):
        for function_name in (
            "loadAPISecuritySettings",
            "loadRegistrationSettings",
            "loadLoginInfoSettings",
            "loadDebounceDelay",
            "loadOutgoingConfigs",
        ):
            with self.subTest(function_name=function_name):
                body = _extract_function_body(self.app_js, function_name)
                error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                throw_index = body.index("throw new Error(errorMessage);", error_index)
                self.assertLess(
                    body.find("requestSequence !== systemSettingsLoadRequestSequence", error_index),
                    throw_index,
                    f"{function_name} 读完错误体后得先复验 load request，别让过期失败响应继续往 catch 里诈尸",
                )
                self.assertLess(
                    body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
                    throw_index,
                    f"{function_name} 读完错误体后得先复验 mutation action，别让旧失败响应抢当前页面的话筒",
                )
                self.assertLess(
                    body.find("!isSystemSettingsSectionActive()", error_index),
                    throw_index,
                    f"{function_name} 读完错误体后得先看当前页还在不在，别切页了还硬往下抛异常",
                )

    def test_load_system_settings_refreshes_debounce_delay_with_other_admin_subsections(self):
        body = _extract_function_body(self.app_js, "loadSystemSettings")
        self.assertIn("const [apiSecurityLoaded, registrationLoaded, loginInfoLoaded, debounceLoaded, outgoingConfigsLoaded] = await Promise.all([", body)
        self.assertIn("loadDebounceDelay(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadOutgoingConfigs(requestSequence, actionRequestSequence)", body)

    def test_load_system_settings_does_not_report_full_success_when_admin_subsection_refresh_fails(self):
        body = _extract_function_body(self.app_js, "loadSystemSettings")
        self.assertIn("let adminSettingsLoaded = true;", body)
        self.assertIn("const actionRequestSequence = systemSettingsMutationActionRequestSequence;", body)
        self.assertIn("const [apiSecurityLoaded, registrationLoaded, loginInfoLoaded, debounceLoaded, outgoingConfigsLoaded] = await Promise.all([", body)
        self.assertIn("loadAPISecuritySettings(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadRegistrationSettings(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadLoginInfoSettings(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadDebounceDelay(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadOutgoingConfigs(requestSequence, actionRequestSequence)", body)
        self.assertIn("adminSettingsLoaded = [", body)
        self.assertIn("].every(result => result === true);", body)
        self.assertIn("return adminSettingsLoaded;", body)

    def test_system_settings_requests_are_invalidated_when_leaving_section(self):
        self.assertIn("let systemSettingsLoadRequestSequence = 0;", self.app_js)
        self.assertIn("function isSystemSettingsSectionActive() {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "isSystemSettingsSectionActive")
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("document.getElementById('system-settings-section')?.classList.contains('active')", helper_body)
        self.assertIn("if (sectionName !== 'system-settings') {", show_section_body)
        self.assertIn("systemSettingsLoadRequestSequence += 1;", show_section_body)

    def test_load_system_settings_ignores_stale_verify_responses_and_hidden_section(self):
        body = _extract_function_body(self.app_js, "loadSystemSettings")

        self.assertIn("const requestSequence = ++systemSettingsLoadRequestSequence;", body)
        self.assertIn("const actionRequestSequence = systemSettingsMutationActionRequestSequence;", body)
        self.assertIn("requestSequence !== systemSettingsLoadRequestSequence", body)
        self.assertIn("!isSystemSettingsSectionActive()", body)
        self.assertIn("return null;", body)
        self.assertIn("const [apiSecurityLoaded, registrationLoaded, loginInfoLoaded, debounceLoaded, outgoingConfigsLoaded] = await Promise.all([", body)
        self.assertIn("loadAPISecuritySettings(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadRegistrationSettings(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadLoginInfoSettings(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadDebounceDelay(requestSequence, actionRequestSequence)", body)
        self.assertIn("loadOutgoingConfigs(requestSequence, actionRequestSequence)", body)
        self.assertLess(
            body.index("requestSequence !== systemSettingsLoadRequestSequence"),
            body.index("setSystemSettingsAdminVisibility(isAdmin);"),
            "系统设置权限校验晚回来时，不该再去改当前管理员面板显隐状态",
        )

    def test_system_settings_child_loaders_ignore_stale_async_responses_and_hidden_section(self):
        checks = (
            ("loadAPISecuritySettings", "qqReplySecretKeyInput.value = qqReplySecretKey;"),
            ("loadRegistrationSettings", "checkbox.checked = data.enabled;"),
            ("loadLoginInfoSettings", "checkbox.checked = settings.show_default_login_info === 'true';"),
            ("loadDebounceDelay", "input.value = parseInt(val) || 3;"),
            ("loadOutgoingConfigs", "renderOutgoingConfigs(settings);"),
        )

        for function_name, ui_write_fragment in checks:
            with self.subTest(function_name=function_name):
                body = _extract_function_body(self.app_js, function_name)
                self.assertIn("requestSequence !== null && (", body)
                self.assertIn("requestSequence !== systemSettingsLoadRequestSequence", body)
                self.assertIn("!isSystemSettingsSectionActive()", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("requestSequence !== systemSettingsLoadRequestSequence"),
                    body.index(ui_write_fragment),
                    f"{function_name} 的旧请求不该晚回来后再覆盖当前系统设置界面",
                )

    def test_system_settings_child_loaders_ignore_newer_mutation_actions_before_overwriting_ui(self):
        checks = (
            ("loadAPISecuritySettings", "qqReplySecretKeyInput.value = qqReplySecretKey;"),
            ("loadRegistrationSettings", "checkbox.checked = data.enabled;"),
            ("loadLoginInfoSettings", "checkbox.checked = settings.show_default_login_info === 'true';"),
            ("loadDebounceDelay", "input.value = parseInt(val) || 3;"),
        )

        for function_name, ui_write_fragment in checks:
            with self.subTest(function_name=function_name):
                body = _extract_function_body(self.app_js, function_name)
                self.assertIn(
                    f"async function {function_name}(requestSequence = null, actionRequestSequence = null) {{",
                    self.app_js,
                )
                self.assertIn("actionRequestSequence !== null && (", body)
                self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== systemSettingsMutationActionRequestSequence"),
                    body.index(ui_write_fragment),
                    f"{function_name} 的旧加载不该在用户已经保存新设置后再把当前值糊回去",
                )

    def test_system_settings_child_loaders_ignore_stale_request_or_action_before_resetting_ui(self):
        checks = (
            ("loadAPISecuritySettings", "resetApiSecuritySettingsFields();"),
            ("loadRegistrationSettings", "resetRegistrationSettingsField();"),
            ("loadLoginInfoSettings", "resetLoginInfoSettingsFields();"),
            ("loadDebounceDelay", "resetDebounceDelayField();"),
            ("loadOutgoingConfigs", "container.innerHTML = '';"),
        )

        for function_name, reset_fragment in checks:
            with self.subTest(function_name=function_name):
                body = _extract_function_body(self.app_js, function_name)
                self.assertIn("requestSequence !== null && (", body)
                self.assertIn("requestSequence !== systemSettingsLoadRequestSequence", body)
                self.assertIn("actionRequestSequence !== null && (", body)
                self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", body)
                self.assertLess(
                    body.index("requestSequence !== systemSettingsLoadRequestSequence"),
                    body.index(reset_fragment),
                    f"{function_name} 的旧请求都过期了，就别先进来把当前系统设置界面清空",
                )
                self.assertLess(
                    body.index("actionRequestSequence !== systemSettingsMutationActionRequestSequence"),
                    body.index(reset_fragment),
                    f"{function_name} 的旧加载都被新保存顶掉了，就别先把当前系统设置界面抹了再装无事发生",
                )

    def test_load_system_settings_stops_admin_child_refreshes_after_newer_mutation_starts(self):
        body = _extract_function_body(self.app_js, "loadSystemSettings")
        first_state_merge_index = body.index("adminSettingsLoaded = apiSecurityLoaded === true && adminSettingsLoaded;")

        self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", body)
        self.assertLess(
            body.rfind("actionRequestSequence !== systemSettingsMutationActionRequestSequence", 0, first_state_merge_index),
            first_state_merge_index,
            "系统设置页旧加载在管理员子配置返回后，得先确认期间没冒出新的保存动作，再决定是否继续合并旧结果",
        )

    def test_api_security_settings_reset_helper_clears_stale_secret_and_status(self):
        self.assertIn("function resetApiSecuritySettingsFields() {", self.app_js)
        body = _extract_function_body(self.app_js, "resetApiSecuritySettingsFields")
        self.assertIn("const qqReplySecretKeyInput = document.getElementById('qqReplySecretKey');", body)
        self.assertIn("const statusDiv = document.getElementById('qqReplySecretStatus');", body)
        self.assertIn("const statusText = document.getElementById('qqReplySecretStatusText');", body)
        self.assertIn("qqReplySecretKeyInput.value = '';", body)
        self.assertIn("statusText.textContent = '';", body)
        self.assertIn("statusDiv.style.display = 'none';", body)

    def test_update_qq_reply_secret_key_clears_stale_status_before_request_and_on_failure(self):
        body = _extract_function_body(self.app_js, "updateQQReplySecretKey")
        reset_body = _extract_function_body(self.app_js, "resetApiSecuritySettingsFields")

        self.assertIn("let qqReplySecretStatusHideTimer = null;", self.app_js)
        self.assertIn("if (qqReplySecretStatusHideTimer) {", reset_body)
        self.assertIn("clearTimeout(qqReplySecretStatusHideTimer);", reset_body)
        self.assertIn("qqReplySecretStatusHideTimer = null;", reset_body)
        self.assertIn("const statusDiv = document.getElementById('qqReplySecretStatus');", body)
        self.assertIn("const statusText = document.getElementById('qqReplySecretStatusText');", body)
        self.assertIn("if (qqReplySecretStatusHideTimer) {", body)
        self.assertIn("clearTimeout(qqReplySecretStatusHideTimer);", body)
        self.assertIn("qqReplySecretStatusHideTimer = setTimeout(() => {", body)
        self.assertIn("qqReplySecretStatusHideTimer = null;", body)
        self.assertIn("statusText.textContent = '';", body)
        self.assertIn("statusDiv.style.display = 'none';", body)
        self.assertGreater(
            body.index("statusDiv.style.display = 'none';"),
            body.index("const statusDiv = document.getElementById('qqReplySecretStatus');"),
            "秘钥保存前应先把旧成功提示清掉，别失败了还挂着上一轮的成功状态",
        )

    def test_update_qq_reply_secret_key_action_sequence_starts_only_after_validation(self):
        body = _extract_function_body(self.app_js, "updateQQReplySecretKey")
        self.assertLess(
            body.index("if (!qqReplySecretKey) {"),
            body.index("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;"),
            "QQ回复消息秘钥为空时只是前端校验，别先把系统设置 action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (qqReplySecretKey.length < 8) {"),
            body.index("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;"),
            "QQ回复消息秘钥长度不够时只是前端校验，别还没发请求就先把系统设置 action sequence 顶掉",
        )

    def test_update_qq_reply_secret_key_handles_unauthorized_and_structured_error_messages(self):
        body = _extract_function_body(self.app_js, "updateQQReplySecretKey")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("showToast(`更新QQ回复消息API秘钥失败: ${error}`, 'danger');", body)
        self.assertIn("showToast(`更新QQ回复消息API秘钥失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("const errorData = await response.json();", body)
        self.assertNotIn("showToast('更新QQ回复消息秘钥失败', 'danger');", body)

        unauthorized_index = body.index("if (handleUnauthorizedApiResponse(response)) {")
        response_ok_index = body.index("if (!response.ok) {")
        error_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        toast_index = body.index("showToast(`更新QQ回复消息API秘钥失败: ${error}`, 'danger');", error_index)

        self.assertLess(
            unauthorized_index,
            response_ok_index,
            "QQ秘钥保存遇到 401 得先滚去登录，别后面还继续走成功/失败分支瞎折腾",
        )
        self.assertLess(
            error_index,
            toast_index,
            "QQ秘钥保存失败得先把 detail/message 解出来，别后端都交代了前端还在那儿装聋",
        )
        self.assertLess(
            body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
            toast_index,
            "QQ秘钥保存旧失败响应读完错误体后先验 action sequence，别新动作都发了老 toast 还回来抢戏",
        )
        self.assertLess(
            body.find("!isSystemSettingsSectionActive()", error_index),
            toast_index,
            "都切出系统设置页了，旧的 QQ 秘钥失败响应别再跨页回来甩 danger toast",
        )

    def test_update_login_info_settings_clears_stale_status_before_request_and_on_failure(self):
        body = _extract_function_body(self.app_js, "updateLoginInfoSettings")
        reset_body = _extract_function_body(self.app_js, "resetLoginInfoSettingsFields")

        self.assertIn("let loginInfoStatusHideTimer = null;", self.app_js)
        self.assertIn("if (loginInfoStatusHideTimer) {", reset_body)
        self.assertIn("clearTimeout(loginInfoStatusHideTimer);", reset_body)
        self.assertIn("loginInfoStatusHideTimer = null;", reset_body)
        self.assertIn("const statusDiv = document.getElementById('loginInfoStatus');", body)
        self.assertIn("const statusText = document.getElementById('loginInfoStatusText');", body)
        self.assertIn("if (loginInfoStatusHideTimer) {", body)
        self.assertIn("clearTimeout(loginInfoStatusHideTimer);", body)
        self.assertIn("loginInfoStatusHideTimer = setTimeout(() => {", body)
        self.assertIn("loginInfoStatusHideTimer = null;", body)
        self.assertIn("statusText.textContent = '';", body)
        self.assertIn("statusDiv.style.display = 'none';", body)
        self.assertGreater(
            body.index("statusDiv.style.display = 'none';"),
            body.index("const statusDiv = document.getElementById('loginInfoStatus');"),
            "登录/注册设置保存前应先把旧成功提示清掉，别失败了还挂着上一轮的成功状态",
        )

    def test_update_login_info_settings_handles_unauthorized_and_structured_error_messages(self):
        body = _extract_function_body(self.app_js, "updateLoginInfoSettings")

        for response_name, anchor_fragment, error_fragment, toast_fragment in (
            (
                "regResponse",
                "if (regResponse.ok) {",
                "const error = await readResponseErrorMessage(regResponse, `HTTP ${regResponse.status}`);",
                "showToast(`更新注册设置失败: ${error}`, 'danger');",
            ),
            (
                "response",
                "if (response.ok) {",
                "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);",
                "showToast(`更新默认登录信息设置失败: ${error}`, 'danger');",
            ),
            (
                "captchaResponse",
                "if (captchaResponse.ok) {",
                "const error = await readResponseErrorMessage(captchaResponse, `HTTP ${captchaResponse.status}`);",
                "showToast(`更新登录验证码设置失败: ${error}`, 'danger');",
            ),
        ):
            with self.subTest(response_name=response_name):
                unauthorized_fragment = f"if (handleUnauthorizedApiResponse({response_name})) {{"
                self.assertIn(unauthorized_fragment, body)
                self.assertIn(error_fragment, body)
                self.assertIn(toast_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "登录信息这串 raw fetch 碰到 401 得先滚去登录，别后面还继续串行改配置装作没事",
                )
                error_index = body.index(error_fragment)
                toast_index = body.index(toast_fragment, error_index)
                self.assertLess(
                    error_index,
                    toast_index,
                    "登录信息设置失败得先把 detail/message 解出来，别后端给了原因前端还在那儿硬装聋",
                )
                self.assertLess(
                    body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
                    toast_index,
                    "登录信息设置旧失败响应读完错误体后先验 action sequence，别新动作都发了老 toast 还回来抢话筒",
                )
                self.assertLess(
                    body.find("!isSystemSettingsSectionActive()", error_index),
                    toast_index,
                    "都切出系统设置页了，旧的登录信息失败响应别再跨页回来甩 danger toast",
                )

        self.assertIn("showToast(`更新登录信息设置失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("const errorData = await regResponse.json();", body)
        self.assertNotIn("const errorData = await response.json();", body)
        self.assertNotIn("const errorData = await captchaResponse.json();", body)
        self.assertNotIn("showToast('更新登录信息设置失败', 'danger');", body)

    def test_system_settings_mutations_do_not_emit_cross_page_toasts_or_reopen_hidden_status_after_leaving_section(self):
        theme_body = _extract_function_body(self.app_js, "saveThemeSettings")
        debounce_body = _extract_function_body(self.app_js, "saveDebounceDelay")
        secret_body = _extract_function_body(self.app_js, "updateQQReplySecretKey")
        outgoing_body = _extract_function_body(self.app_js, "saveOutgoingConfigs")
        login_info_body = _extract_function_body(self.app_js, "updateLoginInfoSettings")

        for body, success_fragment in (
            (theme_body, "showToast('主题设置保存成功', 'success');"),
            (debounce_body, "showToast('防抖延迟已保存', 'success');"),
            (secret_body, "showToast('QQ回复消息API秘钥更新成功', 'success');"),
            (outgoing_body, "showToast('外发配置保存成功', 'success');"),
            (login_info_body, "showToast('设置保存成功', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!isSystemSettingsSectionActive()", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!isSystemSettingsSectionActive()"),
                    body.index(success_fragment),
                    "系统设置保存操作在离开页面后不该再跨页弹 success toast",
                )

        self.assertLess(
            secret_body.index("!isSystemSettingsSectionActive()"),
            secret_body.index("statusDiv.style.display = 'block';"),
            "QQ秘钥保存旧响应不该在切页后还把隐藏状态条重新撑开",
        )
        self.assertLess(
            login_info_body.index("!isSystemSettingsSectionActive()"),
            login_info_body.index("statusDiv.style.display = 'block';"),
            "登录信息设置旧响应不该在切页后还把隐藏状态条重新撑开",
        )

    def test_system_settings_mutations_ignore_older_same_page_responses(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        theme_body = _extract_function_body(self.app_js, "saveThemeSettings")
        debounce_body = _extract_function_body(self.app_js, "saveDebounceDelay")
        secret_body = _extract_function_body(self.app_js, "updateQQReplySecretKey")
        outgoing_load_body = _extract_function_body(self.app_js, "loadOutgoingConfigs")
        outgoing_save_body = _extract_function_body(self.app_js, "saveOutgoingConfigs")
        login_info_body = _extract_function_body(self.app_js, "updateLoginInfoSettings")
        reload_cache_body = _extract_function_body(self.app_js, "reloadSystemCache")

        self.assertIn("let systemSettingsMutationActionRequestSequence = 0;", self.app_js)
        self.assertIn("systemSettingsMutationActionRequestSequence += 1;", show_section_body)

        for body, anchor_fragment in (
            (theme_body, "applyThemeColor(normalizedThemeColor);"),
            (debounce_body, "showToast('防抖延迟已保存', 'success');"),
            (secret_body, "statusDiv.style.display = 'block';"),
            (reload_cache_body, "reloadSucceeded = Boolean(await loadAccountKeywords()) && reloadSucceeded;"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", body)
                self.assertLess(
                    body.index("actionRequestSequence !== systemSettingsMutationActionRequestSequence"),
                    body.index(anchor_fragment),
                    "同页都已经点了新的系统设置动作，旧响应就别回来改当前 UI 了",
                )

        self.assertIn("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;", outgoing_save_body)
        self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", outgoing_save_body)
        self.assertLess(
            outgoing_save_body.index("actionRequestSequence !== systemSettingsMutationActionRequestSequence"),
            outgoing_save_body.index("if (!response.ok) {"),
            "同页外发配置已经发起了新的保存动作，旧请求就别继续往下跑了",
        )
        self.assertIn("const loaded = await loadOutgoingConfigs(requestSequence, actionRequestSequence);", outgoing_save_body)
        self.assertIn("async function loadOutgoingConfigs(requestSequence = null, actionRequestSequence = null) {", self.app_js)
        self.assertIn("actionRequestSequence !== null && (", outgoing_load_body)
        self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", outgoing_load_body)
        self.assertLess(
            outgoing_load_body.index("actionRequestSequence !== systemSettingsMutationActionRequestSequence"),
            outgoing_load_body.index("renderOutgoingConfigs(settings);"),
            "旧的外发配置刷新结果不该晚回来后把当前表单又盖回去",
        )

        self.assertIn("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;", login_info_body)
        self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", login_info_body)
        self.assertLess(
            login_info_body.index("actionRequestSequence !== systemSettingsMutationActionRequestSequence"),
            login_info_body.index("const response = await fetch('/login-info-settings', {"),
            "同页已经点了新的登录信息保存，旧请求不该继续串行更新后续配置",
        )
        self.assertLess(
            login_info_body.rfind("actionRequestSequence !== systemSettingsMutationActionRequestSequence", 0, login_info_body.index("const captchaResponse = await fetch('/login-captcha-settings', {")),
            login_info_body.index("const captchaResponse = await fetch('/login-captcha-settings', {"),
            "同页保存链路变旧以后，连登录验证码设置也别继续代人执行了",
        )

    def test_debounce_delay_loader_resets_stale_value_when_loading_fails(self):
        self.assertIn("function resetDebounceDelayField() {", self.app_js)
        reset_body = _extract_function_body(self.app_js, "resetDebounceDelayField")
        load_body = _extract_function_body(self.app_js, "loadDebounceDelay")

        self.assertIn("const input = document.getElementById('debounceDelay');", reset_body)
        self.assertIn("input.value = '3';", reset_body)

        self.assertIn("resetDebounceDelayField();", load_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)

    def test_debounce_delay_save_surfaces_structured_errors_and_rechecks_hidden_state(self):
        body = _extract_function_body(self.app_js, "saveDebounceDelay")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("showToast(`保存防抖延迟失败: ${error}`, 'danger');", body)
        self.assertIn("showToast(`保存防抖延迟失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("showToast('保存防抖延迟失败', 'danger');", body)

        error_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        toast_index = body.index("showToast(`保存防抖延迟失败: ${error}`, 'danger');", error_index)

        self.assertLess(
            error_index,
            toast_index,
            "防抖延迟保存失败得先把 detail/message 解出来，别后端都说人话了前端还在那儿嗯甩固定红字",
        )
        self.assertLess(
            body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
            toast_index,
            "旧的防抖延迟失败响应读完错误体后先验 action sequence，别新动作都发了老 toast 还回来抢戏",
        )
        self.assertLess(
            body.find("!isSystemSettingsSectionActive()", error_index),
            toast_index,
            "都切出系统设置页了，旧的防抖延迟失败响应别再跨页回来甩 danger toast",
        )

    def test_outgoing_config_save_handles_unauthorized_and_structured_error_messages(self):
        body = _extract_function_body(self.app_js, "saveOutgoingConfigs")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("showToast(`保存外发配置失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("throw new Error(`保存${key}失败`);", body)
        self.assertNotIn("showToast('保存外发配置失败: ' + error.message, 'danger');", body)

        unauthorized_index = body.index("if (handleUnauthorizedApiResponse(response)) {")
        response_ok_index = body.index("if (!response.ok) {")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);", error_index)
        toast_index = body.index("showToast(`保存外发配置失败: ${error.message || '请稍后重试'}`, 'danger');")

        self.assertLess(
            unauthorized_index,
            response_ok_index,
            "外发配置保存遇到 401 得先滚去登录，别后面还继续循环保存把场面搅得稀碎",
        )
        self.assertLess(
            error_index,
            throw_index,
            "外发配置保存失败得先把 detail/message 解出来，别继续只会抛个半截错误名糊弄人",
        )
        self.assertLess(
            body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
            throw_index,
            "外发配置旧失败响应读完错误体后先验 action sequence，别新动作都发了老异常还回来诈尸",
        )
        self.assertLess(
            body.find("!isSystemSettingsSectionActive()", error_index),
            throw_index,
            "都切出系统设置页了，旧的外发配置失败响应别再跨页抛异常再去 catch 里吵吵",
        )
        self.assertLess(
            throw_index,
            toast_index,
            "外发配置保存失败应把真实后端错误带进 catch toast，别又给吞回固定红字",
        )

    def test_menu_settings_mutations_require_successful_http_responses_before_reporting_success(self):
        save_body = _extract_function_body(self.app_js, "saveMenuSettings")
        reset_body = _extract_function_body(self.app_js, "resetMenuSettings")

        self.assertIn("const response = await fetch(`${apiBase}/user-settings/menu-settings/replace`", save_body)
        self.assertIn("visibility,", save_body)
        self.assertIn("order,", save_body)
        self.assertIn("if (!response.ok) {", save_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", save_body)
        self.assertIn("throw new Error(errorMessage);", save_body)
        self.assertNotIn("/user-settings/menu_visibility", save_body)
        self.assertNotIn("/user-settings/menu_order", save_body)
        self.assertIn("showToast(`保存菜单设置失败: ${error.message || '请稍后重试'}`, 'danger');", save_body)

        self.assertIn("const response = await fetch(`${apiBase}/user-settings/menu-settings/replace`", reset_body)
        self.assertIn("visibility: {},", reset_body)
        self.assertIn("order: [],", reset_body)
        self.assertIn("if (!response.ok) {", reset_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", reset_body)
        self.assertIn("throw new Error(errorMessage);", reset_body)
        self.assertNotIn("/user-settings/menu_visibility", reset_body)
        self.assertNotIn("/user-settings/menu_order", reset_body)
        self.assertIn("showToast(`重置菜单设置失败: ${error.message || '请稍后重试'}`, 'danger');", reset_body)

    def test_menu_settings_mutations_read_structured_error_messages_before_throwing_and_toasting(self):
        save_body = _extract_function_body(self.app_js, "saveMenuSettings")
        reset_body = _extract_function_body(self.app_js, "resetMenuSettings")

        for body, toast_fragment, label in (
            (
                save_body,
                "showToast(`保存菜单设置失败: ${error.message || '请稍后重试'}`, 'danger');",
                "保存菜单设置",
            ),
            (
                reset_body,
                "showToast(`重置菜单设置失败: ${error.message || '请稍后重试'}`, 'danger');",
                "重置菜单设置",
            ),
        ):
            with self.subTest(label=label):
                error_fragment = "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"
                error_index = body.index(error_fragment)
                throw_index = body.index("throw new Error(errorMessage);", error_index)
                toast_index = body.index(toast_fragment)

                self.assertLess(
                    error_index,
                    throw_index,
                    f"{label}失败得先把 detail/message 解出来，别前端还在那儿只会看状态码装没事",
                )
                self.assertLess(
                    body.find("actionRequestSequence !== menuSettingsActionRequestSequence", error_index),
                    throw_index,
                    f"{label}旧失败响应读完错误体后先验 action sequence，别新动作都发了老异常还回来诈尸",
                )
                self.assertLess(
                    body.find("!isSystemSettingsSectionActive()", error_index),
                    throw_index,
                    f"{label}读完错误体后得先看当前页还在不在，别切页了还回 catch 里甩红字",
                )
                self.assertLess(
                    throw_index,
                    toast_index,
                    f"{label}应把真实后端错误带进 catch toast，别又给吞回固定红字",
                )

    def test_menu_settings_mutations_ignore_older_same_page_responses_and_suppress_hidden_section_toasts(self):
        save_body = _extract_function_body(self.app_js, "saveMenuSettings")
        reset_body = _extract_function_body(self.app_js, "resetMenuSettings")

        self.assertIn("let menuSettingsActionRequestSequence = 0;", self.app_js)

        for body, state_fragment, success_fragment in (
            (save_body, "menuSettings = visibility;", "showToast('菜单设置保存成功', 'success');"),
            (reset_body, "menuSettings = {};", "showToast('菜单设置已恢复默认', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++menuSettingsActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== menuSettingsActionRequestSequence", body)
                self.assertLess(
                    body.index("actionRequestSequence !== menuSettingsActionRequestSequence"),
                    body.index(state_fragment),
                    "同页都已经发起了新的菜单设置动作，旧响应就别回来覆盖当前 sidebar 状态了",
                )
                self.assertIn("!isSystemSettingsSectionActive()", body)
                self.assertLess(
                    body.rfind("!isSystemSettingsSectionActive()", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出系统设置页了，旧菜单设置结果就别再跨页刷 success toast 了",
                )

        self.assertIn("if (isSystemSettingsSectionActive()) {", reset_body)
        self.assertIn("initMenuManagement();", reset_body)
        self.assertLess(
            reset_body.index("applyMenuSettings();"),
            reset_body.index("initMenuManagement();"),
            "菜单恢复默认后应该先把 sidebar 更新好，再决定是否回刷系统设置里的菜单管理 UI",
        )

    def test_system_settings_entry_refreshes_menu_settings_and_menu_management_ui(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_body = _extract_function_body(self.app_js, "loadMenuSettings")

        self.assertIn("case 'system-settings':", self.app_js)
        self.assertIn("loadUserSettings();", show_section_body)
        self.assertIn("loadMenuSettings();", show_section_body)
        self.assertNotIn(
            "initMenuManagement();",
            show_section_body,
            "系统设置页菜单配置还没加载回来时，别先拿默认 DOM 把菜单管理 UI 画出来糊用户一脸",
        )
        self.assertIn("applyMenuSettings();", load_body)
        self.assertIn("if (isSystemSettingsSectionActive()) {", load_body)
        self.assertIn("initMenuManagement();", load_body)
        self.assertLess(
            load_body.index("applyMenuSettings();"),
            load_body.index("initMenuManagement();"),
            "菜单设置异步回来后，应该先把最新数据落到 sidebar，再回刷系统设置里的菜单管理 UI",
        )

    def test_menu_management_default_items_cover_all_regular_sidebar_entries_in_order(self):
        regular_sidebar_html = self.index_html.split("<!-- 管理员专用菜单 -->", 1)[0]
        regular_menu_ids = re.findall(r'data-menu-id="([^"]+)"', regular_sidebar_html)
        default_menu_block = re.search(r"const DEFAULT_MENU_ITEMS = \[(.*?)\];", self.app_js, re.S)

        self.assertIsNotNone(default_menu_block, "找不到 DEFAULT_MENU_ITEMS 配置块")
        default_menu_ids = re.findall(r"id:\s*'([^']+)'", default_menu_block.group(1))

        self.assertEqual(
            regular_menu_ids,
            default_menu_ids,
            "菜单管理默认项得把普通侧边栏菜单一项不落地接住，顺序也得对齐，别把“商品搜索”这种活菜单漏在管理外头装没看见",
        )

    def test_system_settings_entry_reloads_user_settings_and_invalidates_stale_user_setting_loads(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "loadUserSettings")

        self.assertIn("let userSettingsLoadRequestSequence = 0;", self.app_js)
        self.assertIn("loadUserSettings();", show_section_body)
        self.assertIn("if (sectionName !== 'system-settings') {", show_section_body)
        self.assertIn("userSettingsLoadRequestSequence += 1;", show_section_body)
        self.assertIn("const requestSequence = ++userSettingsLoadRequestSequence;", body)
        self.assertIn("const actionRequestSequence = systemSettingsMutationActionRequestSequence;", body)
        self.assertIn("requestSequence !== userSettingsLoadRequestSequence", body)
        self.assertIn("actionRequestSequence !== systemSettingsMutationActionRequestSequence", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== userSettingsLoadRequestSequence"),
            body.index("applyThemeColor(color);"),
            "新的用户设置加载已经发起后，旧响应别再回来把当前主题色糊回陈年老值",
        )
        self.assertLess(
            body.rfind("actionRequestSequence !== systemSettingsMutationActionRequestSequence", 0, body.index("applyThemeColor(color);")),
            body.index("applyThemeColor(color);"),
            "主题设置保存动作都开始了，旧的用户设置加载结果别再回来把刚改好的主题色盖回去",
        )

    def test_load_menu_settings_resets_stale_state_before_fetch_and_when_backend_returns_no_values(self):
        body = _extract_function_body(self.app_js, "loadMenuSettings")

        self.assertIn("menuSettings = {};", body)
        self.assertIn("menuOrder = [];", body)
        self.assertLess(
            body.index("menuSettings = {};"),
            body.index("const response = await fetch(`${apiBase}/user-settings`, {"),
            "菜单设置重新加载前得先清掉旧显隐状态，别后端没回值时还挂着上轮菜单配置装正确",
        )
        self.assertLess(
            body.index("menuOrder = [];"),
            body.index("const response = await fetch(`${apiBase}/user-settings`, {"),
            "菜单设置重新加载前得先清掉旧排序，别后端没回值时 sidebar 还沿用陈年顺序",
        )

    def test_load_menu_settings_failure_falls_back_to_default_menu_state_and_reapplies_ui(self):
        body = _extract_function_body(self.app_js, "loadMenuSettings")

        self.assertIn("if (!response.ok) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("applyMenuSettings();", body)
        self.assertIn("if (isSystemSettingsSectionActive()) {", body)
        self.assertIn("initMenuManagement();", body)
        self.assertIn("showToast(`加载菜单设置失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertIn("return false;", body)
        self.assertLess(
            body.index("if (!response.ok) {"),
            body.index("console.error('加载菜单设置失败:', error);"),
            "菜单设置 HTTP 失败至少得先把错误抛出来走统一兜底，别闷头吞了还让用户以为配置真没了",
        )

    def test_load_menu_settings_http_failures_read_structured_error_messages_before_throwing_and_toasting(self):
        body = _extract_function_body(self.app_js, "loadMenuSettings")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);", error_index)
        toast_index = body.index("showToast(`加载菜单设置失败: ${error.message || '请稍后重试'}`, 'danger');")

        self.assertLess(
            error_index,
            throw_index,
            "菜单设置加载失败得先把 detail/message 解出来，别后端都说明白了前端还搁那儿装聋",
        )
        self.assertLess(
            body.find("requestSequence !== menuSettingsLoadRequestSequence", error_index),
            throw_index,
            "新的菜单加载已经发起后，旧失败响应读完错误体就该原地闭嘴，别再抛去 catch 里回魂",
        )
        self.assertLess(
            body.find("actionRequestSequence !== menuSettingsActionRequestSequence", error_index),
            throw_index,
            "菜单保存或恢复默认已经开始后，旧失败响应读完错误体也别再抛异常搅当前 sidebar",
        )
        self.assertLess(
            throw_index,
            toast_index,
            "菜单设置加载失败应把真实后端错误带进 catch toast，别又给吞成一坨固定红字",
        )

    def test_menu_settings_save_waits_for_latest_menu_config_load_before_collecting_dom_state(self):
        load_body = _extract_function_body(self.app_js, "loadMenuSettings")
        save_body = _extract_function_body(self.app_js, "saveMenuSettings")

        self.assertIn("let menuSettingsUiReady = false;", self.app_js)
        self.assertIn("menuSettingsUiReady = false;", load_body)
        self.assertLess(
            load_body.index("menuSettingsUiReady = false;"),
            load_body.index("const response = await fetch(`${apiBase}/user-settings`, {"),
            "菜单配置重新加载刚开始时，得先把 UI ready 标记拉下来，别旧界面还让人继续拿去保存",
        )
        self.assertIn("menuSettingsUiReady = true;", load_body)
        self.assertLess(
            load_body.index("menuSettingsUiReady = true;"),
            load_body.index("applyMenuSettings();"),
            "菜单配置只有在当前加载结果落定后，才能重新放行 sidebar 和菜单管理 UI 的刷新",
        )

        self.assertIn("if (!menuSettingsUiReady) {", save_body)
        self.assertIn("showToast('菜单设置加载中，请稍候', 'info');", save_body)
        self.assertLess(
            save_body.index("if (!menuSettingsUiReady) {"),
            save_body.index("const visibility = {};"),
            "菜单配置还没拿到最新结果时，保存逻辑别先把空 DOM 当成用户的新菜单设置提交上去",
        )
        self.assertLess(
            save_body.index("if (!menuSettingsUiReady) {"),
            save_body.index("const order = getCurrentMenuOrder();"),
            "菜单管理列表都还没准备好，就别先收集空排序再把用户配置覆盖成默认了",
        )

    def test_load_menu_settings_ignores_older_loads_and_newer_menu_mutations_before_applying_sidebar(self):
        body = _extract_function_body(self.app_js, "loadMenuSettings")

        self.assertIn("let menuSettingsLoadRequestSequence = 0;", self.app_js)
        self.assertIn("const requestSequence = ++menuSettingsLoadRequestSequence;", body)
        self.assertIn("const actionRequestSequence = menuSettingsActionRequestSequence;", body)
        self.assertIn("requestSequence !== menuSettingsLoadRequestSequence", body)
        self.assertIn("actionRequestSequence !== menuSettingsActionRequestSequence", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== menuSettingsLoadRequestSequence"),
            body.index("applyMenuSettings();"),
            "同页已经触发了新的菜单设置加载，旧响应就别回来覆盖当前 sidebar 状态了",
        )
        self.assertLess(
            body.rfind("actionRequestSequence !== menuSettingsActionRequestSequence", 0, body.index("applyMenuSettings();")),
            body.index("applyMenuSettings();"),
            "菜单保存或恢复默认已经发起后，旧的菜单加载结果不该再回魂把 sidebar 状态盖回去",
        )
        self.assertLess(
            body.rfind("requestSequence !== menuSettingsLoadRequestSequence", 0, body.index("console.error('加载菜单设置失败:', error);")),
            body.index("console.error('加载菜单设置失败:', error);"),
            "新的菜单加载已经发起后，旧失败请求就别再回头按默认状态重刷 sidebar 了",
        )
        self.assertLess(
            body.rfind("actionRequestSequence !== menuSettingsActionRequestSequence", 0, body.index("console.error('加载菜单设置失败:', error);")),
            body.index("console.error('加载菜单设置失败:', error);"),
            "菜单保存或恢复默认已经开始后，旧失败请求别再回来按默认状态把当前 sidebar 搅黄了",
        )

    def test_switching_away_from_system_settings_invalidates_menu_settings_loads(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("menuSettingsLoadRequestSequence += 1;", show_section_body)
        self.assertLess(
            show_section_body.index("menuSettingsLoadRequestSequence += 1;"),
            show_section_body.index("const restartConfirmModalElement = document.getElementById('restartConfirmModal');"),
            "切出系统设置页时，旧菜单配置加载也得先作废，别失败回退晚回来把当前 sidebar 又刷回默认",
        )

    def test_theme_settings_save_requires_successful_http_response_before_reporting_success(self):
        self.assertIn("async function saveThemeSettings(event) {", self.app_js)
        body = _extract_function_body(self.app_js, "saveThemeSettings")
        self.assertIn("event.preventDefault();", body)
        self.assertIn("const response = await fetch(`${apiBase}/user-settings/theme_color`", body)
        self.assertIn("if (!response.ok) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("showToast('主题设置保存成功', 'success');", body)
        self.assertIn("showToast(`主题设置失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("showToast('主题设置失败', 'danger');", body)

    def test_theme_settings_save_reads_structured_errors_before_throwing_and_toasting(self):
        body = _extract_function_body(self.app_js, "saveThemeSettings")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);", error_index)
        toast_index = body.index("showToast(`主题设置失败: ${error.message || '请稍后重试'}`, 'danger');")

        self.assertLess(
            error_index,
            throw_index,
            "主题设置保存失败得先把 detail/message 解出来，别后端都说明白了前端还装哑巴",
        )
        self.assertLess(
            body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
            throw_index,
            "主题设置旧失败响应读完错误体后先验 action sequence，别新动作都发了老异常还回来诈尸",
        )
        self.assertLess(
            body.find("!isSystemSettingsSectionActive()", error_index),
            throw_index,
            "都切出系统设置页了，旧的主题设置失败响应别再跨页抛异常回 catch 里吵人",
        )
        self.assertLess(
            throw_index,
            toast_index,
            "主题设置失败应把真实后端错误带进 catch toast，别又给吞成一句空泛红字",
        )

    def test_theme_settings_save_validates_hex_color_before_submitting_or_applying(self):
        body = _extract_function_body(self.app_js, "saveThemeSettings")
        self.assertIn("const normalizedThemeColor = String(themeColor || '').trim();", body)
        self.assertIn("if (!/^#[0-9A-Fa-f]{6}$/.test(normalizedThemeColor)) {", body)
        self.assertIn("showToast('主题颜色格式无效，请输入 #RRGGBB 格式', 'warning');", body)
        self.assertIn("value: normalizedThemeColor,", body)
        self.assertIn("applyThemeColor(normalizedThemeColor);", body)
        self.assertLess(
            body.index("if (!/^#[0-9A-Fa-f]{6}$/.test(normalizedThemeColor)) {"),
            body.index("const response = await fetch(`${apiBase}/user-settings/theme_color`, {"),
            "主题色保存前先把格式校验做了，别什么妖魔鬼怪都往后端塞",
        )

    def test_system_settings_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        load_user_body = _extract_function_body(self.app_js, "loadUserSettings")
        theme_body = _extract_function_body(self.app_js, "saveThemeSettings")
        debounce_body = _extract_function_body(self.app_js, "saveDebounceDelay")
        secret_body = _extract_function_body(self.app_js, "updateQQReplySecretKey")
        outgoing_save_body = _extract_function_body(self.app_js, "saveOutgoingConfigs")
        login_info_body = _extract_function_body(self.app_js, "updateLoginInfoSettings")
        save_menu_body = _extract_function_body(self.app_js, "saveMenuSettings")
        reset_menu_body = _extract_function_body(self.app_js, "resetMenuSettings")
        load_menu_body = _extract_function_body(self.app_js, "loadMenuSettings")
        reload_cache_body = _extract_function_body(self.app_js, "reloadSystemCache")
        restart_body = _extract_function_body(self.app_js, "doRestartSystem")
        password_submit_body = _extract_brace_block_after(
            self.app_js,
            "passwordForm.addEventListener('submit', async function(e)",
        )

        for body, unauthorized_fragment, anchor_fragment in (
            (load_user_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (theme_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (debounce_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (secret_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (outgoing_save_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (login_info_body, "if (handleUnauthorizedApiResponse(regResponse)) {", "if (regResponse.ok) {"),
            (login_info_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (login_info_body, "if (handleUnauthorizedApiResponse(captchaResponse)) {", "if (captchaResponse.ok) {"),
            (save_menu_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (reset_menu_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (load_menu_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (reload_cache_body, "if (handleUnauthorizedApiResponse(response)) {", "const result = await response.json().catch(() => null);"),
            (restart_body, "if (handleUnauthorizedApiResponse(response)) {", "const result = await response.json().catch(() => null);"),
            (password_submit_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "系统设置这些 raw fetch 遇到 401 得先滚去登录，别后面还继续走回退、保存、改密码这些后续流程",
                )

    def test_password_update_submit_uses_error_payload_helper_on_failure(self):
        body = _extract_brace_block_after(
            self.app_js,
            "passwordForm.addEventListener('submit', async function(e)",
        )

        self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertNotIn("const error = await response.text();", body)
        self.assertLess(
            body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("showToast(`密码更新失败: ${error}`, 'danger');"),
            "改管理员密码失败时别拿裸文本糊弄，先走统一错误体解析再弹 toast",
        )

    def test_password_update_submit_starts_action_sequence_after_validation_and_suppresses_stale_followups(self):
        body = _extract_brace_block_after(
            self.app_js,
            "passwordForm.addEventListener('submit', async function(e)",
        )

        self.assertIn("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;", body)
        self.assertLess(
            body.index("if (newPassword !== confirmPassword) {"),
            body.index("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;"),
            "管理员密码前端校验没过时别先把系统设置 action sequence 顶掉，纯属给自己找别扭",
        )
        self.assertLess(
            body.index("if (newPassword.length < 6) {"),
            body.index("const actionRequestSequence = ++systemSettingsMutationActionRequestSequence;"),
            "管理员密码长度校验没过时也别先开系统设置动作序号，没发请求就别抢占会话",
        )
        self.assertIn("!isSystemSettingsSectionActive()", body)
        self.assertLess(
            body.rfind("actionRequestSequence !== systemSettingsMutationActionRequestSequence", 0, body.index("showToast('密码更新成功，请重新登录', 'success');")),
            body.index("showToast('密码更新成功，请重新登录', 'success');"),
            "都切出系统设置页或同页发了新动作后，旧的改密码成功响应别再跨页弹 success toast",
        )
        self.assertLess(
            body.rfind("!isSystemSettingsSectionActive()", 0, body.index("localStorage.removeItem('auth_token');")),
            body.index("localStorage.removeItem('auth_token');"),
            "人都切页了，旧的延迟跳转就别回魂把当前登录态给抹了",
        )

    def test_password_update_submit_surfaces_catch_errors_and_rechecks_state_after_error_body_read(self):
        body = _extract_brace_block_after(
            self.app_js,
            "passwordForm.addEventListener('submit', async function(e)",
        )

        self.assertIn("showToast(`密码更新失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("showToast('密码更新失败', 'danger');", body)

        error_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        toast_index = body.index("showToast(`密码更新失败: ${error}`, 'danger');", error_index)

        self.assertLess(
            body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
            toast_index,
            "管理员密码旧失败响应读完错误体后先验 action sequence，别新动作都发了老 toast 还回来抢戏",
        )
        self.assertLess(
            body.find("!isSystemSettingsSectionActive()", error_index),
            toast_index,
            "都切出系统设置页了，旧的改密码失败响应别再跨页回来甩 danger toast",
        )

    def test_backend_rejects_invalid_theme_color_user_settings_values(self):
        self.assertIn("if key == 'theme_color':", self.reply_server)
        self.assertIn("normalized_value = str(value or '').strip()", self.reply_server)
        self.assertIn("if not re.fullmatch(r'^#[0-9A-Fa-f]{6}$', normalized_value):", self.reply_server)
        self.assertIn("raise HTTPException(status_code=400, detail='无效主题颜色值')", self.reply_server)

    def test_backend_user_setting_validation_does_not_wrap_http_400_into_500(self):
        match = re.search(
            r"@app\.put\('/user-settings/\{key\}'\)(.*?)@app\.get\('/user-settings/\{key\}'\)",
            self.reply_server,
            re.S,
        )
        self.assertIsNotNone(match, "找不到 update_user_setting 的路由定义片段")
        route_block = match.group(1)
        self.assertIn("except HTTPException:", route_block)
        self.assertIn("raise", route_block)
        self.assertLess(
            route_block.index("except HTTPException:"),
            route_block.index("except Exception as e:"),
            "用户设置校验抛出的 HTTPException 不该被兜底异常吞掉再包成 500",
        )

    def test_load_user_settings_only_resets_theme_controls_when_backend_has_no_saved_color(self):
        body = _extract_function_body(self.app_js, "loadUserSettings")

        self.assertIn("function resetThemeSettingsFields(defaultColor = '#4f46e5') {", self.app_js)
        self.assertIn("if (settings.theme_color && settings.theme_color.value) {", body)
        self.assertIn("} else {", body)
        self.assertIn("resetThemeSettingsFields();", body)
        self.assertLess(
            body.index("const response = await fetch(`${apiBase}/user-settings`, {"),
            body.index("resetThemeSettingsFields();"),
            "用户主题设置应该先等后端确认没有保存主题色，再回退默认主题，别一进页面就先闪回默认蓝",
        )

    def test_load_user_settings_http_failures_read_structured_errors_and_only_toast_when_system_settings_active(self):
        body = _extract_function_body(self.app_js, "loadUserSettings")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);", error_index)
        toast_index = body.index("showToast(`加载用户设置失败: ${error.message || '请稍后重试'}`, 'danger');")

        self.assertIn("if (!response.ok) {", body)
        self.assertIn("if (isSystemSettingsSectionActive()) {", body)
        self.assertLess(
            error_index,
            throw_index,
            "用户设置加载失败得先把后端错误体读出来，别状态码一红前端就开始装傻",
        )
        self.assertLess(
            body.find("requestSequence !== userSettingsLoadRequestSequence", error_index),
            throw_index,
            "新的用户设置加载已经发起后，旧失败响应读完错误体就该闭嘴，别再回 catch 里诈尸",
        )
        self.assertLess(
            body.find("actionRequestSequence !== systemSettingsMutationActionRequestSequence", error_index),
            throw_index,
            "主题设置保存动作已经开始后，旧失败响应别再回来把当前系统设置页搅黄",
        )
        self.assertLess(
            throw_index,
            toast_index,
            "用户设置加载失败得把真实后端错误带进 toast，别又给吞成一句空泛红字",
        )
        self.assertLess(
            body.rfind("if (isSystemSettingsSectionActive()) {", 0, toast_index),
            toast_index,
            "不在系统设置页时，用户设置加载失败就别跨页甩 danger toast 了",
        )

    def test_reset_theme_settings_fields_rehydrates_picker_hex_preset_and_css_defaults(self):
        body = _extract_function_body(self.app_js, "resetThemeSettingsFields")

        self.assertIn("const picker = document.getElementById('themeColorPicker');", body)
        self.assertIn("const hex = document.getElementById('themeColorHex');", body)
        self.assertIn("if (picker) picker.value = defaultColor;", body)
        self.assertIn("if (hex) hex.value = defaultColor;", body)
        self.assertIn("applyThemeColor(defaultColor);", body)
        self.assertIn("updatePresetSelection(defaultColor);", body)
        self.assertIn("localStorage.removeItem('themeColor');", body)

    def test_backup_import_refresh_only_reports_success_when_followup_reload_steps_succeed(self):
        body = _extract_function_body(self.app_js, "importBackup")
        self.assertIn("let reloadSucceeded = true;", body)
        self.assertIn("let reloadAttempted = false;", body)
        self.assertIn("reloadSucceeded = Boolean(await loadAccountKeywords());", body)
        self.assertIn("reloadSucceeded = Boolean(await loadDashboard()) && reloadSucceeded;", body)
        self.assertIn("reloadSucceeded = Boolean(await loadAccounts()) && reloadSucceeded;", body)
        self.assertIn("if (!reloadAttempted) {", body)
        self.assertIn("} else if (reloadSucceeded) {", body)
        self.assertIn("showToast('备份导入成功，请按需刷新相关页面查看最新数据', 'success');", body)
        self.assertIn("showToast('数据刷新完成！', 'success');", body)
        self.assertIn("showToast('备份导入成功，但数据刷新失败，请手动刷新页面', 'warning');", body)
        self.assertNotIn("await loadAccountKeywords();", body)

    def test_backup_import_only_refreshes_auto_reply_keywords_when_auto_reply_section_is_active(self):
        body = _extract_function_body(self.app_js, "importBackup")
        self.assertIn("const shouldReloadAutoReplyKeywords = currentAccountId && document.getElementById('auto-reply-section')?.classList.contains('active');", body)
        self.assertIn("if (shouldReloadAutoReplyKeywords) {", body)
        self.assertIn("reloadAttempted = true;", body)
        self.assertIn("reloadSucceeded = Boolean(await loadAccountKeywords());", body)
        self.assertLess(
            body.index("if (shouldReloadAutoReplyKeywords) {"),
            body.index("reloadAttempted = true;"),
            "导入备份后的关键词刷新只该在自动回复页当前真的活着时才触发，别拿历史 currentAccountId 乱报 warning",
        )
        self.assertLess(
            body.index("reloadAttempted = true;"),
            body.index("reloadSucceeded = Boolean(await loadAccountKeywords());"),
            "导入备份确认真有页面需要回刷后，再去记账 reloadAttempted，别让假完成提示继续糊人",
        )

    def test_backup_import_followup_refresh_targets_are_not_blocked_by_system_settings_hidden_guard(self):
        helper_body = _extract_function_body(self.app_js, "isBackupImportFollowupTargetActive")
        body = _extract_function_body(self.app_js, "importBackup")

        self.assertIn("isSystemSettingsSectionActive()", helper_body)
        self.assertIn("document.getElementById('auto-reply-section')?.classList.contains('active')", helper_body)
        self.assertIn("document.getElementById('dashboard-section')?.classList.contains('active')", helper_body)
        self.assertIn("document.getElementById('accounts-section')?.classList.contains('active')", helper_body)
        self.assertIn("!isBackupImportFollowupTargetActive()", body)

        callback_prefix = body.split("setTimeout(async () => {", 1)[1].split(
            "const shouldReloadAutoReplyKeywords = currentAccountId && document.getElementById('auto-reply-section')?.classList.contains('active');",
            1,
        )[0]
        self.assertNotIn(
            "!isSystemSettingsSectionActive()",
            callback_prefix,
            "导入备份的延迟回刷链路别还没检查 auto-reply/dashboard/accounts 的活跃状态，就先被 system-settings hidden guard 直接堵死",
        )

    def test_backup_file_upload_validators_normalize_extension_case_before_rejecting_uploads(self):
        upload_database_backup_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        import_backup_body = _extract_function_body(self.app_js, "importBackup")

        self.assertIn("const fileInput = document.getElementById('databaseFile');", upload_database_backup_body)
        self.assertIn("if (!fileInput) {", upload_database_backup_body)
        self.assertIn("showToast('找不到数据库文件选择控件，请刷新页面后重试', 'danger');", upload_database_backup_body)

        self.assertIn("const fileInput = document.getElementById('backupFile');", import_backup_body)
        self.assertIn("if (!fileInput) {", import_backup_body)
        self.assertIn("showToast('找不到备份文件选择控件，请刷新页面后重试', 'danger');", import_backup_body)

        self.assertIn("const normalizedFileName = (file.name || '').toLowerCase();", upload_database_backup_body)
        self.assertIn("if (!normalizedFileName.endsWith('.db')) {", upload_database_backup_body)

        self.assertIn("const normalizedFileName = (file.name || '').toLowerCase();", import_backup_body)
        self.assertIn("if (!normalizedFileName.endsWith('.json')) {", import_backup_body)

        self.assertIn("normalized_filename = (backup_file.filename or '').lower()", self.reply_server)
        self.assertIn("if not normalized_filename.endswith('.db'):", self.reply_server)
        self.assertIn("normalized_filename = (file.filename or '').lower()", self.reply_server)
        self.assertIn("if not normalized_filename.endswith('.json'):", self.reply_server)

    def test_backup_upload_and_import_actions_do_not_emit_cross_page_toasts_after_leaving_system_settings(self):
        upload_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        import_body = _extract_function_body(self.app_js, "importBackup")

        for body, toast_fragment in (
            (upload_body, "showToast(`数据库恢复成功！包含 ${result.user_count} 个用户`, 'success');"),
            (upload_body, "showToast(`恢复失败: ${error}`, 'danger');"),
            (upload_body, "showToast(`上传数据库备份失败: ${error.message || '请稍后重试'}`, 'danger');"),
            (import_body, "showToast('备份导入成功！正在刷新数据...', 'success');"),
            (import_body, "showToast(`导入失败: ${error}`, 'danger');"),
            (import_body, "showToast(`导入备份失败: ${error.message || '请稍后重试'}`, 'danger');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!isSystemSettingsSectionActive()", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!isSystemSettingsSectionActive()"),
                    body.index(toast_fragment),
                    "都切出系统设置页了，旧备份上传/导入请求不该再跨页弹 toast",
                )

        self.assertIn("setTimeout(() => {", upload_body)
        upload_confirm_index = upload_body.index("if (confirm('数据库已恢复，建议刷新页面以加载新数据。是否立即刷新？')) {")
        self.assertLess(
            upload_body.rfind("!isSystemSettingsSectionActive()", 0, upload_confirm_index),
            upload_confirm_index,
            "都切出系统设置页了，数据库恢复后的延迟确认框不该再跳出来吓人",
        )

        self.assertIn("setTimeout(async () => {", import_body)
        import_neutral_success_index = import_body.index("showToast('备份导入成功，请按需刷新相关页面查看最新数据', 'success');")
        import_success_index = import_body.index("showToast('数据刷新完成！', 'success');")
        import_warning_index = import_body.index("showToast('备份导入成功，但数据刷新失败，请手动刷新页面', 'warning');")
        self.assertLess(
            import_body.rfind("!isSystemSettingsSectionActive()", 0, import_neutral_success_index),
            import_neutral_success_index,
            "都切出系统设置页了，导入备份后的无页面回刷成功提示也别追着人弹",
        )
        self.assertLess(
            import_body.rfind("!isSystemSettingsSectionActive()", 0, import_success_index),
            import_success_index,
            "都切出系统设置页了，导入备份后的延迟成功 toast 不该再追着人跑",
        )
        self.assertLess(
            import_body.rfind("!isSystemSettingsSectionActive()", 0, import_warning_index),
            import_warning_index,
            "都切出系统设置页了，导入备份后的延迟 warning toast 也不该再回魂",
        )

    def test_backup_upload_and_import_actions_ignore_stale_same_page_responses(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        upload_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        import_body = _extract_function_body(self.app_js, "importBackup")

        self.assertIn("let backupManagementActionRequestSequence = 0;", self.app_js)
        self.assertIn("backupManagementActionRequestSequence += 1;", show_section_body)

        for body, clear_fragment, success_fragment in (
            (
                upload_body,
                "fileInput.value = '';",
                "showToast(`数据库恢复成功！包含 ${result.user_count} 个用户`, 'success');",
            ),
            (
                import_body,
                "fileInput.value = '';",
                "showToast('备份导入成功！正在刷新数据...', 'success');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++backupManagementActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== backupManagementActionRequestSequence", body)
                self.assertLess(
                    body.index("actionRequestSequence !== backupManagementActionRequestSequence"),
                    body.index(clear_fragment),
                    "同页已经发起了新的备份动作，旧请求就别再把当前文件选择给清空了",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== backupManagementActionRequestSequence", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "同页已经发起了新的备份动作，旧成功响应就别再回来抢戏了",
                )

    def test_backup_management_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        download_body = _extract_function_body(self.app_js, "downloadDatabaseBackup")
        upload_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        export_body = _extract_function_body(self.app_js, "exportBackup")
        import_body = _extract_function_body(self.app_js, "importBackup")

        for body, anchor_fragment in (
            (download_body, "if (response.ok) {"),
            (upload_body, "if (response.ok) {"),
            (export_body, "if (response.ok) {"),
            (import_body, "if (response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    "备份相关 raw fetch 遇到 401 得先滚去登录，别后面还继续走陈年 guard 和成功/失败分支",
                )

    def test_backup_management_failure_actions_read_structured_error_messages(self):
        download_body = _extract_function_body(self.app_js, "downloadDatabaseBackup")
        upload_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        export_body = _extract_function_body(self.app_js, "exportBackup")
        import_body = _extract_function_body(self.app_js, "importBackup")

        for body, toast_fragment in (
            (download_body, "showToast(`下载失败: ${error}`, 'danger');"),
            (upload_body, "showToast(`恢复失败: ${error}`, 'danger');"),
            (export_body, "showToast(`导出失败: ${error}`, 'danger');"),
            (import_body, "showToast(`导入失败: ${error}`, 'danger');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(toast_fragment),
                    "备份操作失败别再拿裸文本瞎糊，先统一走错误体解析再弹 toast",
                )

    def test_backup_upload_and_import_failures_recheck_stale_state_after_error_body_read(self):
        upload_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        import_body = _extract_function_body(self.app_js, "importBackup")

        for body, error_fragment in (
            (upload_body, "showToast(`恢复失败: ${error}`, 'danger');"),
            (import_body, "showToast(`导入失败: ${error}`, 'danger');"),
        ):
            with self.subTest(error_fragment=error_fragment):
                error_text_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                self.assertLess(
                    body.find("actionRequestSequence !== backupManagementActionRequestSequence", error_text_index),
                    body.index(error_fragment),
                    "同页都已经点了新的备份动作，旧失败响应读完错误体后也别回来诈尸甩红字",
                )
                self.assertLess(
                    body.find("!isSystemSettingsSectionActive()", error_text_index),
                    body.index(error_fragment),
                    "都切出系统设置页了，旧失败响应读完错误体后也别再跨页弹 danger toast",
                )

    def test_backup_upload_and_import_followup_callbacks_stop_after_newer_backup_actions(self):
        upload_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        import_body = _extract_function_body(self.app_js, "importBackup")

        upload_confirm_index = upload_body.index("if (confirm('数据库已恢复，建议刷新页面以加载新数据。是否立即刷新？')) {")
        self.assertLess(
            upload_body.rfind("actionRequestSequence !== backupManagementActionRequestSequence", 0, upload_confirm_index),
            upload_confirm_index,
            "同页都已经点了新的备份动作，旧数据库恢复请求的延迟确认框就别诈尸了",
        )

        for fragment in (
            "reloadSucceeded = Boolean(await loadAccountKeywords());",
            "reloadSucceeded = Boolean(await loadDashboard()) && reloadSucceeded;",
            "reloadSucceeded = Boolean(await loadAccounts()) && reloadSucceeded;",
            "showToast('备份导入成功，请按需刷新相关页面查看最新数据', 'success');",
            "showToast('数据刷新完成！', 'success');",
            "showToast('备份导入成功，但数据刷新失败，请手动刷新页面', 'warning');",
        ):
            with self.subTest(fragment=fragment):
                self.assertLess(
                    import_body.rfind("actionRequestSequence !== backupManagementActionRequestSequence", 0, import_body.index(fragment)),
                    import_body.index(fragment),
                    "同页都已经发起新的备份动作了，旧导入链路就别继续刷新数据和弹 toast 了",
                )

    def test_reload_system_cache_requires_backend_ack_and_only_reports_success_after_keyword_reload(self):
        body = _extract_function_body(self.app_js, "reloadSystemCache")
        self.assertIn("const result = await response.json().catch(() => null);", body)
        self.assertIn("if (!response.ok || !result || result.success === false) {", body)
        self.assertIn("throw new Error(result?.detail || result?.message || `HTTP ${response.status}`);", body)
        self.assertIn("clearKeywordCache();", body)
        self.assertIn("let reloadSucceeded = true;", body)
        self.assertIn("let reloadAttempted = false;", body)
        self.assertIn("reloadSucceeded = Boolean(await loadAccountKeywords()) && reloadSucceeded;", body)
        self.assertIn("if (!reloadAttempted) {", body)
        self.assertIn("} else if (reloadSucceeded) {", body)
        self.assertIn("showToast('系统缓存刷新成功，请按需刷新相关页面查看最新数据', 'success');", body)
        self.assertIn("showToast('系统缓存刷新成功！关键字等数据已更新', 'success');", body)
        self.assertIn("showToast('系统缓存刷新成功，但当前页面数据刷新失败，请稍后手动刷新', 'warning');", body)
        self.assertNotIn("setTimeout(() => {", body)
        self.assertLess(
            body.index("reloadSucceeded = Boolean(await loadAccountKeywords()) && reloadSucceeded;"),
            body.index("showToast('系统缓存刷新成功！关键字等数据已更新', 'success');"),
            "缓存刷新不该后端刚回 200 就先报喜，当前页面数据没刷新完之前先别装成功",
        )

    def test_reload_system_cache_only_refreshes_auto_reply_keywords_when_auto_reply_section_is_active(self):
        body = _extract_function_body(self.app_js, "reloadSystemCache")
        self.assertIn("const shouldReloadAutoReplyKeywords = currentAccountId && document.getElementById('auto-reply-section')?.classList.contains('active');", body)
        self.assertIn("if (shouldReloadAutoReplyKeywords) {", body)
        self.assertIn("reloadAttempted = true;", body)
        self.assertIn("reloadSucceeded = Boolean(await loadAccountKeywords()) && reloadSucceeded;", body)
        self.assertLess(
            body.index("if (shouldReloadAutoReplyKeywords) {"),
            body.index("reloadAttempted = true;"),
            "缓存刷新只该在自动回复页当前真的活着时才触发关键词刷新，别拿旧账号上下文乱报假 warning",
        )
        self.assertLess(
            body.index("reloadAttempted = true;"),
            body.index("reloadSucceeded = Boolean(await loadAccountKeywords()) && reloadSucceeded;"),
            "缓存刷新确认真有页面需要回刷后，再去记账 reloadAttempted，别把没刷任何页面也吹成全量刷新成功",
        )

    def test_reload_system_cache_followup_keyword_refresh_is_not_blocked_by_system_settings_hidden_guard(self):
        helper_body = _extract_function_body(self.app_js, "isSystemCacheRefreshFollowupTargetActive")
        body = _extract_function_body(self.app_js, "reloadSystemCache")

        self.assertIn("isSystemSettingsSectionActive()", helper_body)
        self.assertIn("document.getElementById('auto-reply-section')?.classList.contains('active')", helper_body)
        self.assertIn("!isSystemCacheRefreshFollowupTargetActive()", body)

        response_to_followup_slice = body[
            body.index("const result = await response.json().catch(() => null);"):
            body.index("const shouldReloadAutoReplyKeywords = currentAccountId && document.getElementById('auto-reply-section')?.classList.contains('active');")
        ]
        self.assertNotIn(
            "!isSystemSettingsSectionActive()",
            response_to_followup_slice,
            "缓存刷新确认成功后，别还没检查 auto-reply 页是否活着，就先被 system-settings hidden guard 把关键词回刷链路掐死",
        )

    def test_reload_system_cache_does_not_emit_cross_page_toasts_after_leaving_system_settings(self):
        body = _extract_function_body(self.app_js, "reloadSystemCache")

        for toast_fragment in (
            "showToast('系统缓存刷新成功，请按需刷新相关页面查看最新数据', 'success');",
            "showToast('系统缓存刷新成功！关键字等数据已更新', 'success');",
            "showToast('系统缓存刷新成功，但当前页面数据刷新失败，请稍后手动刷新', 'warning');",
            "showToast(error.message || '刷新系统缓存失败', 'danger');",
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!isSystemSettingsSectionActive()", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!isSystemSettingsSectionActive()"),
                    body.index(toast_fragment),
                    "都切出系统设置页了，旧缓存刷新结果就别再跨页刷 toast 了",
                )

    def test_system_settings_backup_and_cache_actions_do_not_fake_refresh_completion_when_no_target_page_is_active(self):
        import_body = _extract_function_body(self.app_js, "importBackup")
        reload_body = _extract_function_body(self.app_js, "reloadSystemCache")

        for body, neutral_success_fragment, false_refresh_fragment in (
            (
                import_body,
                "showToast('备份导入成功，请按需刷新相关页面查看最新数据', 'success');",
                "showToast('数据刷新完成！', 'success');",
            ),
            (
                reload_body,
                "showToast('系统缓存刷新成功，请按需刷新相关页面查看最新数据', 'success');",
                "showToast('系统缓存刷新成功！关键字等数据已更新', 'success');",
            ),
        ):
            with self.subTest(neutral_success_fragment=neutral_success_fragment):
                self.assertIn("let reloadAttempted = false;", body)
                self.assertIn("if (!reloadAttempted) {", body)
                self.assertIn(neutral_success_fragment, body)
                self.assertIn("} else if (reloadSucceeded) {", body)
                self.assertLess(
                    body.index("if (!reloadAttempted) {"),
                    body.index(neutral_success_fragment),
                    "没有任何目标页面真的参与回刷时，应该老老实实报“按需手动刷新”，别假装已完成页面刷新",
                )
                self.assertLess(
                    body.index(neutral_success_fragment),
                    body.index(false_refresh_fragment),
                    "无页面参与回刷的兜底成功提示要先于“刷新完成”分支，别继续拿假完成文案糊人",
                )

    def test_system_restart_requires_successful_backend_ack_before_reporting_success(self):
        body = _extract_function_body(self.app_js, "doRestartSystem")
        self.assertIn("const result = await response.json().catch(() => null);", body)
        self.assertIn("if (!response.ok || !result || result.success === false) {", body)
        self.assertIn("throw new Error(result?.detail || result?.message || `HTTP ${response.status}`);", body)
        self.assertIn("showToast('系统正在重启，请稍候刷新页面...', 'success');", body)
        self.assertIn("showToast(error.message || '重启系统失败，请检查网络连接', 'danger');", body)
        self.assertLess(
            body.index("if (!response.ok || !result || result.success === false) {"),
            body.index("showToast('系统正在重启，请稍候刷新页面...', 'success');"),
            "重启系统不该只看 HTTP 200 就先报喜，后端 success:false 也得当失败处理",
        )

        restart_route_start = self.reply_server.index("@app.post('/api/update/restart')")
        restart_route_end = self.reply_server.index("# ==================== 一键擦亮API ====================")
        restart_route_block = self.reply_server[restart_route_start:restart_route_end]
        self.assertIn('raise HTTPException(status_code=500, detail=f"重启应用失败: {str(e)}")', restart_route_block)
        self.assertNotIn('"success": False', restart_route_block)

    def test_system_restart_ignores_older_same_page_responses_and_suppresses_hidden_section_toasts(self):
        body = _extract_function_body(self.app_js, "doRestartSystem")

        self.assertIn("let systemRestartActionRequestSequence = 0;", self.app_js)
        self.assertIn("const actionRequestSequence = ++systemRestartActionRequestSequence;", body)
        self.assertIn("actionRequestSequence !== systemRestartActionRequestSequence", body)
        self.assertLess(
            body.index("actionRequestSequence !== systemRestartActionRequestSequence"),
            body.index("showToast('系统正在重启，请稍候刷新页面...', 'success');"),
            "同页都已经发起了新的重启动作，旧成功响应就别回来重复报喜和挂 reload 定时器了",
        )
        self.assertLess(
            body.rfind("actionRequestSequence !== systemRestartActionRequestSequence", 0, body.index("window.location.reload();")),
            body.index("window.location.reload();"),
            "同页都已经发起了新的重启动作，旧成功响应就别再偷偷挂页面 reload 定时器了",
        )
        self.assertIn("if (isSystemSettingsSectionActive()) {", body)
        self.assertLess(
            body.index("if (isSystemSettingsSectionActive()) {"),
            body.index("showToast('系统正在重启，请稍候刷新页面...', 'success');"),
            "都切出系统设置页了，重启成功提示就别再跨页刷存在感",
        )
        self.assertIn("!isSystemSettingsSectionActive()", body)
        self.assertLess(
            body.index("!isSystemSettingsSectionActive()"),
            body.index("showToast(error.message || '重启系统失败，请检查网络连接', 'danger');"),
            "都切出系统设置页了，旧重启失败提示也别再跨页甩红字",
        )

    def test_switching_away_from_system_settings_closes_restart_confirm_modal(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("const restartConfirmModalElement = document.getElementById('restartConfirmModal');", show_section_body)
        self.assertIn("restartConfirmModal.hide();", show_section_body)
        self.assertIn("restartConfirmModalElement.remove();", show_section_body)

    def test_switching_away_from_system_settings_invalidates_restart_followups(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("systemRestartActionRequestSequence += 1;", show_section_body)
        self.assertLess(
            show_section_body.index("systemRestartActionRequestSequence += 1;"),
            show_section_body.index("const restartConfirmModalElement = document.getElementById('restartConfirmModal');"),
            "切走系统设置页时，旧重启请求和延迟 reload 定时器都得先作废，别等模态框都拆了还让它们回魂",
        )

    def test_outgoing_config_fields_escape_dynamic_values_before_rendering(self):
        body = _extract_function_body(self.app_js, "generateOutgoingFieldHtml")
        for fragment in (
            "const safeValue = escapeHtml(String(value || ''));",
            "const safePlaceholder = escapeHtml(field.placeholder || '');",
            "const safeFieldId = escapeHtml(field.id || '');",
            "const safeOptionValue = escapeHtml(option.value || '');",
            "const safeOptionText = escapeHtml(option.text || '');",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, body)

    def test_outgoing_config_save_only_reports_success_after_reload_succeeds(self):
        load_body = _extract_function_body(self.app_js, "loadOutgoingConfigs")
        save_body = _extract_function_body(self.app_js, "saveOutgoingConfigs")

        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("const loaded = await loadOutgoingConfigs(requestSequence, actionRequestSequence);", save_body)
        self.assertIn("if (loaded) {", save_body)
        self.assertIn("showToast('外发配置保存成功', 'success');", save_body)
        self.assertIn("showToast('外发配置保存成功，但配置界面刷新失败，请稍后手动刷新', 'warning');", save_body)

    def test_dashboard_resets_stale_cards_when_account_summary_load_fails(self):
        reset_body = _extract_function_body(self.app_js, "resetDashboardOverviewState")
        load_dashboard_body = _extract_function_body(self.app_js, "loadDashboard")

        for fragment in (
            "renderDashboardAccountOverview([], 0);",
            "updateDashboardOrderMetrics({",
            "showSalesErrorState(document.getElementById('dashboardTodaySales'), '加载失败');",
            "showSalesErrorState(document.getElementById('dashboardWeekSales'), '加载失败');",
            "showSalesErrorState(document.getElementById('dashboardMonthSales'), '加载失败');",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, reset_body)

        self.assertIn("document.getElementById('dashboardSalesUpdateTime')", reset_body)
        self.assertIn("updateTimeEl.textContent = '--';", reset_body)

        self.assertIn("resetDashboardOverviewState();", load_dashboard_body)
        self.assertIn("if (handleUnauthorizedApiResponse(cookiesResponse)) {", load_dashboard_body)
        self.assertIn("if (!cookiesResponse.ok) {", load_dashboard_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(cookiesResponse, `HTTP ${cookiesResponse.status}`);", load_dashboard_body)
        self.assertIn("throw new Error(errorMessage);", load_dashboard_body)
        self.assertIn("showToast(`加载仪表盘数据失败: ${error.message || '请稍后重试'}`, 'danger');", load_dashboard_body)

        error_index = load_dashboard_body.index("const errorMessage = await readResponseErrorMessage(cookiesResponse, `HTTP ${cookiesResponse.status}`);")
        stale_index = load_dashboard_body.index("requestSequence !== dashboardLoadRequestSequence", error_index)
        throw_index = load_dashboard_body.index("throw new Error(errorMessage);", error_index)
        toast_index = load_dashboard_body.index("showToast(`加载仪表盘数据失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            error_index,
            stale_index,
            "仪表盘账号汇总接口 HTTP 挂了时先把后端 detail/message 抠出来，别还没看见线索就让 stale guard 把锅端了",
        )
        self.assertLess(
            stale_index,
            throw_index,
            "仪表盘账号汇总错误体读完后得先复验请求序号，别旧错误跨页回来把当前总览也掀了",
        )
        self.assertLess(
            load_dashboard_body.find("if (requestSequence !== dashboardLoadRequestSequence) {", load_dashboard_body.index("} catch (error) {")),
            toast_index,
            "都 stale 了的仪表盘主加载失败别再回来弹旧 toast，净添堵",
        )

    def test_dashboard_resource_helper_redirects_unauthorized_before_fallback(self):
        body = _extract_function_body(self.app_js, "fetchDashboardResource")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("return fallbackValue;", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "仪表盘子资源遇到 401 得先统一跳登录，别装成普通 fallback 把未授权静默吞了",
        )

    def test_dashboard_account_enrichment_uses_batch_summary_endpoints(self):
        body = _extract_function_body(self.app_js, "enrichDashboardAccounts")

        self.assertIn("fetchDashboardResource('/keywords/counts', {})", body)
        self.assertIn("fetchDashboardResource('/default-replies', {})", body)
        self.assertIn("fetchDashboardResource('/ai-reply-settings', {})", body)
        self.assertNotIn("fetchDashboardResource(`/keywords/${encodeURIComponent(accountId)}`", body)
        self.assertNotIn("fetchDashboardResource(`/default-replies/${encodeURIComponent(accountId)}`", body)
        self.assertNotIn("fetchDashboardResource(`/ai-reply-settings/${encodeURIComponent(accountId)}`", body)
        self.assertIn("keywordCount: Number(keywordCounts[accountId] || 0),", body)
        self.assertIn("defaultReply: defaultReplies[accountId] || { enabled: false, reply_content: '' },", body)
        self.assertIn("aiReply: aiReplySettings[accountId] || { ai_enabled: false, model_name: 'qwen-plus' },", body)

    def test_dashboard_item_count_loader_does_not_mask_http_failures_as_zero(self):
        body = _extract_function_body(self.app_js, "loadItemsCount")

        self.assertIn("const response = await fetch(`${apiBase}/items/count`, {", body)
        self.assertNotIn("const response = await fetch(`${apiBase}/items`, {", body)
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("const count = Number(data.count);", body)
        self.assertIn("throw new Error('商品总数返回格式异常');", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("throw error;", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "商品总数接口遇到 401 得先跳登录，别继续把未授权当成普通失败糊成 0",
        )
        self.assertLess(
            body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("throw new Error(errorMessage);"),
            "商品总数接口 HTTP 挂了得先把后端错误体读出来，别张嘴就是个没营养的固定报错",
        )
        self.assertLess(
            body.index("throw new Error(errorMessage);"),
            body.index("throw error;"),
            "商品总数接口真失败时得往上抛，别在本地 catch 里硬装成 0 个商品把问题埋了",
        )

    def test_dashboard_sales_summary_clears_stale_update_time_before_loading_and_on_failure(self):
        body = _extract_function_body(self.app_js, "loadSalesSummary")

        self.assertIn("const updateTimeEl = document.getElementById('dashboardSalesUpdateTime');", body)
        self.assertIn("updateTimeEl.textContent = '--';", body)
        self.assertLess(
            body.index("updateTimeEl.textContent = '--';"),
            body.index("const response = await fetch('/api/sales/summary', {"),
            "销售额摘要刷新前应先清空旧更新时间，别让失败时继续挂着上一次的时间装新鲜",
        )

    def test_dashboard_sales_summary_preserves_auth_redirect_and_reads_http_error_payload_before_stale_guard(self):
        body = _extract_function_body(self.app_js, "loadSalesSummary")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("if (!response.ok) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "销售额摘要接口都 401 了就该先跳登录，别还搁那儿往后读响应体自个儿演异常",
        )

        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        stale_index = body.index("requestSequence !== salesSummaryRequestSequence", error_index)
        throw_index = body.index("throw new Error(errorMessage);", error_index)
        self.assertLess(
            error_index,
            stale_index,
            "销售额摘要 HTTP 失败时先把后端 detail/message 读出来，别还没看明白就让 stale guard 把证据抹了",
        )
        self.assertLess(
            stale_index,
            throw_index,
            "销售额摘要错误体读完后得先验请求序号，别旧错误晚回来把当前摘要状态又糊一遍",
        )

    def test_dashboard_sales_summary_ignores_stale_async_responses(self):
        self.assertIn("let salesSummaryRequestSequence = 0;", self.app_js)
        load_body = _extract_function_body(self.app_js, "loadSalesSummary")
        stop_body = _extract_function_body(self.app_js, "stopSalesSummaryRefreshTimer")

        self.assertIn("const requestSequence = ++salesSummaryRequestSequence;", load_body)
        self.assertIn("if (requestSequence !== salesSummaryRequestSequence) {", load_body)
        self.assertIn("return;", load_body)
        self.assertIn("salesSummaryRequestSequence += 1;", stop_body)

    def test_dashboard_delivery_logs_distinguish_load_failures_from_empty_state(self):
        self.assertIn("function renderDashboardDeliveryLogsEmptyState(message = '暂无发货日志') {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "renderDashboardDeliveryLogsEmptyState")
        load_body = _extract_function_body(self.app_js, "loadDashboardDeliveryLogs")

        self.assertIn("const tbody = document.getElementById('dashboardDeliveryLogsList');", helper_body)
        self.assertIn("${escapeHtml(message)}", helper_body)
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("renderDashboardDeliveryLogsEmptyState(`发货日志加载失败: ${error.message || '请稍后重试'}`);", load_body)
        self.assertIn("renderDashboardDeliveryLogs(logs);", load_body)
        self.assertNotIn("tbody.innerHTML = `", load_body)

        error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        stale_index = load_body.index("requestSequence !== dashboardDeliveryLogsRequestSequence", error_index)
        failure_index = load_body.index("renderDashboardDeliveryLogsEmptyState(`发货日志加载失败: ${error.message || '请稍后重试'}`);", error_index)
        self.assertLess(
            error_index,
            stale_index,
            "发货日志接口 HTTP 挂了时先把后端错误体读出来，别还没拿到 detail 就让 hidden/stale guard 把线索闷死",
        )
        self.assertLess(
            stale_index,
            failure_index,
            "发货日志错误体读完后得先复验请求活性，再决定要不要把失败态灌回表格",
        )

    def test_dashboard_delivery_logs_ignore_stale_async_responses(self):
        self.assertIn("let dashboardDeliveryLogsRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadDashboardDeliveryLogs")

        self.assertIn("const requestSequence = ++dashboardDeliveryLogsRequestSequence;", body)
        self.assertIn("requestSequence !== dashboardDeliveryLogsRequestSequence", body)
        self.assertIn("return;", body)

    def test_dashboard_delivery_logs_requests_are_invalidated_when_leaving_dashboard_and_stop_updating_hidden_section(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        reset_body = _extract_function_body(self.app_js, "resetDashboardOverviewState")
        body = _extract_function_body(self.app_js, "loadDashboardDeliveryLogs")

        self.assertIn("dashboardDeliveryLogsRequestSequence += 1;", show_section_body)
        self.assertIn("dashboardDeliveryLogsRequestSequence += 1;", reset_body)
        self.assertIn("!document.getElementById('dashboard-section')?.classList.contains('active')", body)

        self.assertLess(
            body.index("!document.getElementById('dashboard-section')?.classList.contains('active')"),
            body.index("renderDashboardDeliveryLogs(logs);"),
            "都切出 dashboard 了，旧发货日志请求别回来往隐藏表格里灌旧数据",
        )
        self.assertLess(
            body.rfind("!document.getElementById('dashboard-section')?.classList.contains('active')", 0, body.index("renderDashboardDeliveryLogsEmptyState(`发货日志加载失败: ${error.message || '请稍后重试'}`);")),
            body.index("renderDashboardDeliveryLogsEmptyState(`发货日志加载失败: ${error.message || '请稍后重试'}`);"),
            "都离开 dashboard 了，旧发货日志失败回调也别回来把隐藏页状态乱改一通",
        )

    def test_dashboard_sales_refresh_timer_stops_when_leaving_dashboard(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        timer_body = _extract_function_body(self.app_js, "startSalesSummaryRefreshTimer")
        reset_body = _extract_function_body(self.app_js, "resetDashboardOverviewState")

        self.assertIn("if (sectionName !== 'dashboard' && dashboardRuntimeRetryTimer) {", show_section_body)
        self.assertIn("salesChartRequestSequence += 1;", show_section_body)
        self.assertIn("stopSalesSummaryRefreshTimer();", show_section_body)
        self.assertIn("hideChartLoading();", show_section_body)
        self.assertIn("setDateRangePickerVisible(false);", show_section_body)
        self.assertIn("salesChartRequestSequence += 1;", reset_body)
        self.assertIn("stopSalesSummaryRefreshTimer();", reset_body)
        self.assertIn("hideChartLoading();", reset_body)
        self.assertIn("setDateRangePickerVisible(false);", reset_body)

        self.assertIn("const timerRequestSequence = salesSummaryRequestSequence;", timer_body)
        self.assertIn("if (!document.getElementById('dashboard-section')?.classList.contains('active')) {", timer_body)
        self.assertIn("stopSalesSummaryRefreshTimer();", timer_body)
        self.assertIn("timerRequestSequence !== salesSummaryRequestSequence", timer_body)
        self.assertLess(
            timer_body.index("timerRequestSequence !== salesSummaryRequestSequence"),
            timer_body.index("updateDashboardSalesMetrics(data.data);"),
            "dashboard 销售额定时刷新旧会话都 stale 了，就别回来把当前摘要数字又糊成老数据",
        )
        self.assertIn("salesSummaryRefreshTimer = null;", _extract_function_body(self.app_js, "stopSalesSummaryRefreshTimer"))

    def test_dashboard_sales_refresh_timer_preserves_auth_redirect_and_reads_http_error_payload_before_stale_guard(self):
        body = _extract_function_body(self.app_js, "startSalesSummaryRefreshTimer")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("if (!response.ok) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertGreater(
            body.find("stopSalesSummaryRefreshTimer();", body.index("if (handleUnauthorizedApiResponse(response)) {")),
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            "销售额摘要定时刷新遇到 401 得先停表再跳登录，别后台定时器还在那瞎转",
        )
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "销售额摘要定时刷新接口 401 了就直接跳登录，别继续把未授权响应当普通失败折腾",
        )

        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        stale_index = body.index("timerRequestSequence !== salesSummaryRequestSequence", error_index)
        console_index = body.index("console.error('定时刷新销售额摘要失败:', error);")
        self.assertLess(
            error_index,
            stale_index,
            "销售额摘要定时刷新 HTTP 挂了时先把错误体读出来，别还没看见 detail 就让 stale guard 把事盖过去",
        )
        self.assertLess(
            stale_index,
            console_index,
            "销售额摘要定时刷新错误体读完后也得先验当前会话还活着，别旧失败回来污染当前日志",
        )

    def test_dashboard_sales_chart_ignores_stale_or_hidden_async_responses(self):
        self.assertIn("let salesChartRequestSequence = 0;", self.app_js)
        load_week_body = _extract_function_body(self.app_js, "loadSalesChart")
        load_custom_body = _extract_function_body(self.app_js, "loadCustomSalesChart")
        show_section_body = _extract_function_body(self.app_js, "showSection")
        reset_body = _extract_function_body(self.app_js, "resetDashboardOverviewState")

        self.assertIn("const requestSequence = ++salesChartRequestSequence;", load_week_body)
        self.assertIn("if (requestSequence !== salesChartRequestSequence) {", load_week_body)
        self.assertIn("!document.getElementById('dashboard-section')?.classList.contains('active')", load_week_body)
        self.assertIn("return;", load_week_body)
        self.assertLess(
            load_week_body.index("!document.getElementById('dashboard-section')?.classList.contains('active')"),
            load_week_body.index("renderSalesChart(data.data.sales, period);"),
            "切出 dashboard 后，旧的销售额图表请求不该回来把当前图表又画回老数据",
        )
        self.assertLess(
            load_week_body.rfind("!document.getElementById('dashboard-section')?.classList.contains('active')", 0, load_week_body.index("showToast(`加载销售额数据失败: ${error.message || '请稍后重试'}`, 'danger');")),
            load_week_body.index("showToast(`加载销售额数据失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "都离开 dashboard 了，旧的销售额图表失败请求别再跨页甩 danger toast",
        )

        self.assertIn("const requestSequence = ++salesChartRequestSequence;", load_custom_body)
        self.assertIn("if (requestSequence !== salesChartRequestSequence) {", load_custom_body)
        self.assertIn("!document.getElementById('dashboard-section')?.classList.contains('active')", load_custom_body)
        self.assertIn("return;", load_custom_body)
        self.assertLess(
            load_custom_body.index("!document.getElementById('dashboard-section')?.classList.contains('active')"),
            load_custom_body.index("renderSalesChart(data.data.sales, 'custom');"),
            "切出 dashboard 后，旧的自定义图表请求不该回来把当前图表又改回旧范围",
        )

        self.assertIn("salesChartRequestSequence += 1;", show_section_body)
        self.assertIn("salesChartRequestSequence += 1;", reset_body)

    def test_dashboard_sales_chart_preset_ranges_use_beijing_calendar_dates(self):
        body = _extract_function_body(self.app_js, "loadSalesChart")

        self.assertIn("const startDateStr = getBeijingDateKey(startDate);", body)
        self.assertIn("const endDateStr = getBeijingDateKey(now);", body)
        self.assertNotIn("startDate.toISOString().split('T')[0]", body)
        self.assertNotIn("now.toISOString().split('T')[0]", body)
        self.assertLess(
            body.index("const startDateStr = getBeijingDateKey(startDate);"),
            body.index("const response = await fetch(`/api/sales?start_date=${startDateStr}&end_date=${endDateStr}`, {"),
            "dashboard 的快捷时间范围得按北京时间取 YYYY-MM-DD，别半夜一过就被 UTC 日期给带偏一天",
        )

    def test_dashboard_sales_chart_requests_preserve_auth_redirect_and_parse_backend_errors(self):
        load_week_body = _extract_function_body(self.app_js, "loadSalesChart")
        load_custom_body = _extract_function_body(self.app_js, "loadCustomSalesChart")

        for body, function_name, render_fragment in (
            (load_week_body, "loadSalesChart", "renderSalesChart(data.data.sales, period);"),
            (load_custom_body, "loadCustomSalesChart", "renderSalesChart(data.data.sales, 'custom');"),
        ):
            with self.subTest(function_name=function_name):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertIn("if (!response.ok) {", body)
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertIn("throw new Error(errorMessage);", body)
                self.assertIn("throw new Error(data.message || '加载销售额数据失败');", body)
                self.assertIn("showToast(`加载销售额数据失败: ${error.message || '请稍后重试'}`, 'danger');", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index("if (!response.ok) {"),
                    f"{function_name} 遇到 401 时应先跳登录，别还搁那儿把未授权响应往后当正常请求读",
                )

                error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                hidden_index = body.find("!document.getElementById('dashboard-section')?.classList.contains('active')", error_index)
                toast_index = body.index("showToast(`加载销售额数据失败: ${error.message || '请稍后重试'}`, 'danger');")
                self.assertGreater(
                    hidden_index,
                    error_index,
                    f"{function_name} 失败时先把后端错误体读出来，别 hidden guard 抢跑把线索吞了",
                )
                self.assertLess(
                    hidden_index,
                    toast_index,
                    f"{function_name} 错误体读完后得先确认页面还活着，再决定要不要弹旧 toast 烦人",
                )
                self.assertLess(
                    body.index("throw new Error(data.message || '加载销售额数据失败');"),
                    toast_index,
                    f"{function_name} 后端就算返回 200，只要 success=false 也得抛错，别让旧图表继续装新数据",
                )

    def test_dashboard_runtime_snapshot_refresh_ignores_stale_async_responses(self):
        self.assertIn("let dashboardRuntimeSnapshotRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "refreshDashboardRuntimeSnapshots")

        self.assertIn("suppressErrorToast: true", body)
        self.assertIn("const cookieDetails = await fetchJSONWithoutGlobalLoading(`${apiBase}/accounts/details?summary_only=true`, {", body)
        self.assertNotIn("const cookieDetails = await fetchJSON(`${apiBase}/accounts/details?summary_only=true`, {", body)
        self.assertIn("const requestSequence = ++dashboardRuntimeSnapshotRequestSequence;", body)
        self.assertIn("if (requestSequence !== dashboardRuntimeSnapshotRequestSequence) {", body)
        self.assertIn("return;", body)

    def test_dashboard_runtime_snapshot_refresh_is_invalidated_when_dashboard_context_changes(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_dashboard_body = _extract_function_body(self.app_js, "loadDashboard")
        reset_body = _extract_function_body(self.app_js, "resetDashboardOverviewState")

        self.assertIn("dashboardRuntimeSnapshotRequestSequence += 1;", show_section_body)
        self.assertIn("dashboardRuntimeSnapshotRequestSequence += 1;", load_dashboard_body)
        self.assertIn("dashboardRuntimeSnapshotRequestSequence += 1;", reset_body)

    def test_dashboard_order_metrics_clear_stale_values_and_distinguish_failures_from_zero_state(self):
        self.assertIn("function showDashboardOrderMetricsLoadingState() {", self.app_js)
        self.assertIn("function showDashboardOrderMetricsErrorState(message = '加载失败') {", self.app_js)

        loading_body = _extract_function_body(self.app_js, "showDashboardOrderMetricsLoadingState")
        error_body = _extract_function_body(self.app_js, "showDashboardOrderMetricsErrorState")
        load_body = _extract_function_body(self.app_js, "loadOrderDashboardMetrics")

        self.assertIn("salesAmountEl.textContent = '￥--';", loading_body)
        self.assertIn("completionRateEl.textContent = '--';", loading_body)
        self.assertIn("totalOrdersEl.textContent = '--';", loading_body)
        self.assertIn("todayOrdersEl.textContent = '--';", loading_body)

        self.assertIn("salesAmountEl.textContent = message;", error_body)
        self.assertIn("completionRateEl.textContent = message;", error_body)
        self.assertIn("totalOrdersEl.textContent = message;", error_body)
        self.assertIn("todayOrdersEl.textContent = message;", error_body)

        self.assertIn("showDashboardOrderMetricsLoadingState();", load_body)
        self.assertLess(
            load_body.index("showDashboardOrderMetricsLoadingState();"),
            load_body.index("const response = await fetch('/api/orders', {"),
            "订单看板刷新前得先清掉旧数字，别让失败时继续挂着上一轮销量装没事",
        )
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", load_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("showDashboardOrderMetricsErrorState(error.message || '加载失败');", load_body)
        self.assertNotIn("updateDashboardOrderMetrics(defaultMetrics);", load_body)

        error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        stale_index = load_body.index("requestSequence !== dashboardOrderMetricsRequestSequence", error_index)
        error_state_index = load_body.index("showDashboardOrderMetricsErrorState(error.message || '加载失败');")
        self.assertLess(
            error_index,
            stale_index,
            "订单看板接口 HTTP 挂了时先把 detail/message 解出来，别旧毛病又是读都不读就直接糊个 HTTP 状态码",
        )
        self.assertLess(
            stale_index,
            error_state_index,
            "订单看板错误体读完后也得先验请求序号，别旧错误晚回来把当前指标卡重新糊成失败态",
        )

    def test_dashboard_order_metrics_ignore_stale_async_responses(self):
        self.assertIn("let dashboardOrderMetricsRequestSequence = 0;", self.app_js)
        load_body = _extract_function_body(self.app_js, "loadOrderDashboardMetrics")
        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_dashboard_body = _extract_function_body(self.app_js, "loadDashboard")
        reset_body = _extract_function_body(self.app_js, "resetDashboardOverviewState")

        self.assertIn("const requestSequence = ++dashboardOrderMetricsRequestSequence;", load_body)
        self.assertIn("if (requestSequence !== dashboardOrderMetricsRequestSequence) {", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("dashboardOrderMetricsRequestSequence += 1;", show_section_body)
        self.assertIn("dashboardOrderMetricsRequestSequence += 1;", load_dashboard_body)
        self.assertIn("dashboardOrderMetricsRequestSequence += 1;", reset_body)

    def test_dashboard_account_summary_load_ignores_stale_async_responses(self):
        self.assertIn("let dashboardLoadRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadDashboard")

        self.assertIn("const cookiesResponse = await fetch(`${apiBase}/accounts/details?summary_only=true&include_behavior_settings=true`, {", body)
        self.assertIn("const requestSequence = ++dashboardLoadRequestSequence;", body)
        self.assertIn("if (requestSequence !== dashboardLoadRequestSequence) {", body)
        self.assertIn("dashboardData.accounts = accountsWithKeywords;", body)
        self.assertLess(
            body.index("if (requestSequence !== dashboardLoadRequestSequence) {"),
            body.index("dashboardData.accounts = accountsWithKeywords;"),
            "仪表盘主加载得先挡住旧请求，再写账号总览，不然老数据晚到就把新页面糊回去了",
        )
        self.assertIn("renderDashboardAccountOverview(accountsWithKeywords, totalItems);", body)
        render_index = body.index("renderDashboardAccountOverview(accountsWithKeywords, totalItems);")
        self.assertLess(
            body.rfind("if (requestSequence !== dashboardLoadRequestSequence) {", 0, render_index),
            render_index,
            "渲染前也得再验一次请求序号，别让旧总览在最后一脚把新页面踹回去",
        )

    def test_dashboard_account_summary_load_is_invalidated_when_dashboard_context_changes(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        reset_body = _extract_function_body(self.app_js, "resetDashboardOverviewState")

        self.assertIn("dashboardLoadRequestSequence += 1;", show_section_body)
        self.assertIn("dashboardLoadRequestSequence += 1;", reset_body)

    def test_message_notifications_modal_supports_multiple_channel_selection(self):
        self.assertIn('id="notificationChannel" multiple', self.index_html)

        body = _extract_function_body(self.app_js, "configAccountNotification")
        self.assertIn("const currentChannelIds = new Set(currentNotifications.map(notification => notification.channel_id));", body)
        self.assertIn("if (currentChannelIds.has(channel.id)) {", body)
        self.assertNotIn("const currentNotification = currentNotifications.length > 0 ? currentNotifications[0] : null;", body)

    def test_message_notification_config_modal_preserves_currently_selected_disabled_channels(self):
        body = _extract_function_body(self.app_js, "configAccountNotification")

        self.assertIn("const selectableChannels = [...enabledChannels];", body)
        self.assertIn("currentNotifications.forEach(notification => {", body)
        self.assertIn("const matchedChannel = channels.find(channel => channel.id === notification.channel_id);", body)
        self.assertIn("if (!matchedChannel || matchedChannel.enabled) {", body)
        self.assertIn("if (!selectableChannels.some(channel => channel.id === matchedChannel.id)) {", body)
        self.assertIn("selectableChannels.push(matchedChannel);", body)
        self.assertIn("selectableChannels.forEach(channel => {", body)
        self.assertIn("option.textContent = formatNotificationChannelSelectLabel(channel) + (channel.enabled ? '' : '（渠道已禁用）');", body)
        self.assertLess(
            body.index("const selectableChannels = [...enabledChannels];"),
            body.index("selectableChannels.forEach(channel => {"),
            "账号通知配置弹窗别一打开一保存就把已选但全局禁用的渠道偷偷删了，至少得先把它们带出来给人看见",
        )

    def test_save_account_notification_replaces_account_channel_set_with_selected_channels(self):
        body = _extract_function_body(self.app_js, "saveAccountNotification")
        self.assertIn("const selectedChannelIds = Array.from(document.getElementById('notificationChannel').selectedOptions)", body)
        self.assertIn("await fetch(`${apiBase}/message-notifications/${encodedAccountId}/replace`", body)
        self.assertIn("channel_ids: selectedChannelIds.map(channelId => parseInt(channelId, 10)),", body)
        self.assertNotIn("await fetch(`${apiBase}/message-notifications/account/${encodedAccountId}`", body)
        self.assertNotIn("for (const channelId of selectedChannelIds)", body)

    def test_message_notifications_table_escapes_account_and_channel_labels(self):
        body = _extract_function_body(self.app_js, "renderMessageNotifications")
        self.assertIn("const safeAccountId = escapeHtml(accountId);", body)
        self.assertIn("const safeChannelName = escapeHtml(n.channel_name || '未命名渠道');", body)
        self.assertNotIn("${accountId}</strong>", body)
        self.assertNotIn("${n.channel_name}</span>", body)

    def test_message_notifications_table_marks_globally_disabled_channels_as_disabled(self):
        body = _extract_function_body(self.app_js, "renderMessageNotifications")

        self.assertIn("const effectiveEnabled = Boolean(n.enabled) && Boolean(n.channel_enabled);", body)
        self.assertIn("const disabledSuffix = n.channel_enabled ? '' : '（渠道已禁用）';", body)
        self.assertIn("accountNotifications.some(n => Boolean(n.enabled) && Boolean(n.channel_enabled))", body)
        self.assertNotIn("accountNotifications.some(n => n.enabled)", body)
        self.assertLess(
            body.index("const effectiveEnabled = Boolean(n.enabled) && Boolean(n.channel_enabled);"),
            body.index("return `<span class=\"badge bg-${effectiveEnabled ? 'success' : 'secondary'} me-1\">${safeChannelName}${disabledSuffix}</span>`;"),
            "消息通知列表里渠道全局禁用了也得明确标出来，别还拿旧启用态冒充能发通知",
        )

    def test_message_notifications_loader_rejects_blank_account_ids_before_rendering_rows(self):
        body = _extract_function_body(self.app_js, "loadMessageNotifications")

        self.assertIn("accounts.some(accountId => typeof accountId !== 'string' || !accountId.trim())", body)
        self.assertLess(
            body.index("accounts.some(accountId => typeof accountId !== 'string' || !accountId.trim())"),
            body.index("renderMessageNotifications(accounts, notifications);"),
            "消息通知账号列表里如果混进空白账号ID，前端得先当格式异常拦住，别把空账号或 [object Object] 硬渲染进通知配置表格里装正常",
        )

    def test_message_notifications_loader_rejects_malformed_notification_items_before_rendering_rows(self):
        body = _extract_function_body(self.app_js, "loadMessageNotifications")

        self.assertIn("!Number.isFinite(Number(notification.channel_id))", body)
        self.assertIn("typeof notification.channel_name !== 'string'", body)
        self.assertIn("typeof notification.enabled !== 'boolean'", body)
        self.assertIn("typeof notification.channel_enabled !== 'boolean'", body)
        self.assertLess(
            body.index("!Number.isFinite(Number(notification.channel_id))"),
            body.index("renderMessageNotifications(accounts, notifications);"),
            "消息通知配置里如果混进坏通知项，前端得先当格式异常拦住，别把坏项伪装成“未命名渠道/禁用”硬渲染进表格糊弄人",
        )

    def test_message_notification_loaders_do_not_treat_fetch_failures_as_empty_configuration(self):
        load_body = _extract_function_body(self.app_js, "loadMessageNotifications")
        config_body = _extract_function_body(self.app_js, "configAccountNotification")

        self.assertIn("if (!accountsResponse.ok) {", load_body)
        self.assertIn("const accountsErrorMessage = await readResponseErrorMessage(accountsResponse, `HTTP ${accountsResponse.status}`);", load_body)
        self.assertIn("throw new Error(accountsErrorMessage);", load_body)
        self.assertIn("if (!Array.isArray(accounts)) {", load_body)
        self.assertIn("accounts.some(accountId => typeof accountId !== 'string' || !accountId.trim())", load_body)
        self.assertIn("throw new Error('账号列表返回格式异常');", load_body)
        self.assertIn("if (!notificationsResponse.ok) {", load_body)
        self.assertIn("const notificationsErrorMessage = await readResponseErrorMessage(notificationsResponse, `HTTP ${notificationsResponse.status}`);", load_body)
        self.assertIn("throw new Error(notificationsErrorMessage);", load_body)
        self.assertIn("if (!notifications || typeof notifications !== 'object' || Array.isArray(notifications)) {", load_body)
        self.assertIn("Object.values(notifications).some(group => !Array.isArray(group) || group.some(notification =>", load_body)
        self.assertIn("!Number.isFinite(Number(notification.channel_id))", load_body)
        self.assertIn("typeof notification.channel_name !== 'string'", load_body)
        self.assertIn("typeof notification.enabled !== 'boolean'", load_body)
        self.assertIn("typeof notification.channel_enabled !== 'boolean'", load_body)
        self.assertIn("throw new Error('消息通知配置返回格式异常');", load_body)
        self.assertNotIn("let notifications = {};", load_body)
        self.assertNotIn("if (notificationsResponse.ok) {", load_body)

        self.assertIn("if (!channelsResponse.ok) {", config_body)
        self.assertIn("const channelsErrorMessage = await readResponseErrorMessage(channelsResponse, `HTTP ${channelsResponse.status}`);", config_body)
        self.assertIn("throw new Error(channelsErrorMessage);", config_body)
        self.assertIn("if (!Array.isArray(channels)) {", config_body)
        self.assertIn("channels.some(channel =>", config_body)
        self.assertIn("typeof channel.enabled !== 'boolean'", config_body)
        self.assertIn("typeof channel.name !== 'string'", config_body)
        self.assertIn("typeof channel.config !== 'string'", config_body)
        self.assertIn("throw new Error('通知渠道列表返回格式异常');", config_body)
        self.assertIn("if (!notificationResponse.ok) {", config_body)
        self.assertIn("const notificationErrorMessage = await readResponseErrorMessage(notificationResponse, `HTTP ${notificationResponse.status}`);", config_body)
        self.assertIn("throw new Error(notificationErrorMessage);", config_body)
        self.assertIn("if (!Array.isArray(currentNotifications)) {", config_body)
        self.assertIn("currentNotifications.some(notification =>", config_body)
        self.assertIn("typeof notification.channel_name !== 'string'", config_body)
        self.assertIn("typeof notification.enabled !== 'boolean'", config_body)
        self.assertIn("typeof notification.channel_enabled !== 'boolean'", config_body)
        self.assertIn("throw new Error('账号通知配置返回格式异常');", config_body)
        self.assertNotIn("let currentNotifications = [];", config_body)
        self.assertNotIn("if (notificationResponse.ok) {", config_body)

    def test_message_notification_multi_stage_loaders_stop_before_followup_fetch_when_request_turns_stale(self):
        load_body = _extract_function_body(self.app_js, "loadMessageNotifications")
        config_body = _extract_function_body(self.app_js, "configAccountNotification")

        accounts_json_index = load_body.index("const accounts = await accountsResponse.json();")
        notifications_fetch_index = load_body.index("fetch(`${apiBase}/message-notifications`, {")
        stale_guard_after_accounts = load_body.find("requestSequence !== messageNotificationsRequestSequence", accounts_json_index)
        self.assertLess(
            notifications_fetch_index,
            accounts_json_index,
            "消息通知列表的账号和配置互不依赖，别拿到账户列表后才慢吞吞补第二个请求",
        )
        self.assertGreater(
            stale_guard_after_accounts,
            accounts_json_index,
            "消息通知加载拿到账户列表后，先确认请求没过期，再决定要不要继续打通知配置接口",
        )
        self.assertLess(
            stale_guard_after_accounts,
            load_body.index("if (!notificationsResponse.ok) {"),
            "消息通知旧请求不该在账户列表都过期后还继续往下处理通知配置响应",
        )

        channels_json_index = config_body.index("const channels = await channelsResponse.json();")
        notification_fetch_index = config_body.index("fetch(`${apiBase}/message-notifications/${encodedAccountId}`, {")
        stale_guard_after_channels = config_body.find("requestSequence !== accountNotificationConfigRequestSequence", channels_json_index)
        self.assertLess(
            notification_fetch_index,
            channels_json_index,
            "账号通知配置的渠道列表和当前账号配置互不依赖，别拿到渠道后才慢吞吞补第二个请求",
        )
        self.assertGreater(
            stale_guard_after_channels,
            channels_json_index,
            "账号通知配置拿到通知渠道后，先确认当前弹窗会话还活着，再决定要不要继续请求账号配置",
        )
        self.assertLess(
            stale_guard_after_channels,
            config_body.index("if (!notificationResponse.ok) {"),
            "旧的账号通知配置请求不该在渠道列表已经过期后还继续往下处理账号配置响应",
        )

    def test_message_notification_config_modal_ignores_stale_async_responses_and_hidden_modal_state(self):
        self.assertIn("let accountNotificationConfigRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "configAccountNotification")

        self.assertIn("if (sectionName !== 'message-notifications') {", show_section_body)
        self.assertIn("accountNotificationConfigRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++accountNotificationConfigRequestSequence;", body)
        self.assertIn("requestSequence !== accountNotificationConfigRequestSequence", body)
        self.assertIn("!document.getElementById('message-notifications-section')?.classList.contains('active')", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("accountNotificationConfigRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.accountNotificationConfigModalBound = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== accountNotificationConfigRequestSequence"),
            body.index("document.getElementById('configAccountId').value = accountId;"),
            "旧的账号通知配置请求不该晚回来后把当前弹窗内容改成别的账号",
        )
        modal_show_index = body.index("modal.show();")
        self.assertLess(
            body.rfind("requestSequence !== accountNotificationConfigRequestSequence", 0, modal_show_index),
            modal_show_index,
            "都切页或关弹窗了，旧请求不该再回来把配置弹窗重新弹出来",
        )

    def test_message_notification_loader_and_config_failures_surface_structured_error_messages(self):
        load_body = _extract_function_body(self.app_js, "loadMessageNotifications")
        config_body = _extract_function_body(self.app_js, "configAccountNotification")

        self.assertIn("const accountsErrorMessage = await readResponseErrorMessage(accountsResponse, `HTTP ${accountsResponse.status}`);", load_body)
        self.assertIn("throw new Error(accountsErrorMessage);", load_body)
        accounts_error_index = load_body.index("const accountsErrorMessage = await readResponseErrorMessage(accountsResponse, `HTTP ${accountsResponse.status}`);")
        accounts_throw_index = load_body.index("throw new Error(accountsErrorMessage);")
        self.assertLess(
            accounts_error_index,
            accounts_throw_index,
            "消息通知账号列表 HTTP 失败时得先把 detail/message 解出来，别直接抛固定文案",
        )
        self.assertLess(
            load_body.find("requestSequence !== messageNotificationsRequestSequence", accounts_error_index),
            accounts_throw_index,
            "消息通知旧账号列表失败响应读完错误文本后，先验 request sequence，别回魂打断新页面流转",
        )

        self.assertIn("const notificationsErrorMessage = await readResponseErrorMessage(notificationsResponse, `HTTP ${notificationsResponse.status}`);", load_body)
        self.assertIn("throw new Error(notificationsErrorMessage);", load_body)
        self.assertIn("showToast(`加载消息通知配置失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        notifications_error_index = load_body.index("const notificationsErrorMessage = await readResponseErrorMessage(notificationsResponse, `HTTP ${notificationsResponse.status}`);")
        notifications_throw_index = load_body.index("throw new Error(notificationsErrorMessage);")
        self.assertLess(
            notifications_error_index,
            notifications_throw_index,
            "消息通知配置接口失败时得先把 detail/message 解出来，别固定报个加载失败完事",
        )
        self.assertLess(
            load_body.find("requestSequence !== messageNotificationsRequestSequence", notifications_error_index),
            notifications_throw_index,
            "消息通知旧配置失败响应读完错误文本后，先验 request sequence，别回魂抛异常",
        )
        self.assertLess(
            load_body.find("!document.getElementById('message-notifications-section')?.classList.contains('active')", notifications_error_index),
            notifications_throw_index,
            "都切出消息通知页了，旧配置失败响应读完错误文本也别再往 catch 里丢异常",
        )

        self.assertIn("const channelsErrorMessage = await readResponseErrorMessage(channelsResponse, `HTTP ${channelsResponse.status}`);", config_body)
        self.assertIn("throw new Error(channelsErrorMessage);", config_body)
        channels_error_index = config_body.index("const channelsErrorMessage = await readResponseErrorMessage(channelsResponse, `HTTP ${channelsResponse.status}`);")
        channels_throw_index = config_body.index("throw new Error(channelsErrorMessage);")
        self.assertLess(
            channels_error_index,
            channels_throw_index,
            "账号通知配置拉通知渠道失败时得先把 detail/message 解出来，别固定抛个获取失败",
        )
        self.assertLess(
            config_body.find("requestSequence !== accountNotificationConfigRequestSequence", channels_error_index),
            channels_throw_index,
            "账号通知配置旧渠道失败响应读完错误文本后，先验当前弹窗会话，别回魂继续折腾",
        )

        self.assertIn("const notificationErrorMessage = await readResponseErrorMessage(notificationResponse, `HTTP ${notificationResponse.status}`);", config_body)
        self.assertIn("throw new Error(notificationErrorMessage);", config_body)
        self.assertIn("showToast(`配置账号通知失败: ${error.message || '请稍后重试'}`, 'danger');", config_body)
        notification_error_index = config_body.index("const notificationErrorMessage = await readResponseErrorMessage(notificationResponse, `HTTP ${notificationResponse.status}`);")
        notification_throw_index = config_body.index("throw new Error(notificationErrorMessage);")
        self.assertLess(
            notification_error_index,
            notification_throw_index,
            "账号通知配置拉当前账号详情失败时得先把 detail/message 解出来，别把后端真报错吞了",
        )
        self.assertLess(
            config_body.find("requestSequence !== accountNotificationConfigRequestSequence", notification_error_index),
            notification_throw_index,
            "账号通知配置旧详情失败响应读完错误文本后，先验当前弹窗会话，别回魂篡位",
        )
        self.assertLess(
            config_body.find("!document.getElementById('message-notifications-section')?.classList.contains('active')", notification_error_index),
            notification_throw_index,
            "都切出消息通知页了，旧详情失败响应读完错误文本也别再跨页甩异常",
        )

    def test_default_reply_loader_does_not_treat_fetch_failures_as_empty_configuration(self):
        body = _extract_function_body(self.app_js, "loadDefaultReplies")
        self.assertIn("if (!accountsResponse.ok) {", body)
        self.assertIn("const accountsErrorMessage = await readResponseErrorMessage(accountsResponse, `HTTP ${accountsResponse.status}`);", body)
        self.assertIn("throw new Error(accountsErrorMessage);", body)
        self.assertIn("if (!Array.isArray(accounts)) {", body)
        self.assertIn("throw new Error('账号列表返回格式异常');", body)
        self.assertIn("if (!repliesResponse.ok) {", body)
        self.assertIn("const repliesErrorMessage = await readResponseErrorMessage(repliesResponse, `HTTP ${repliesResponse.status}`);", body)
        self.assertIn("throw new Error(repliesErrorMessage);", body)
        self.assertIn("if (!defaultReplies || typeof defaultReplies !== 'object' || Array.isArray(defaultReplies)) {", body)
        self.assertIn("throw new Error('默认回复配置返回格式异常');", body)
        self.assertNotIn("let defaultReplies = {};", body)
        self.assertNotIn("if (repliesResponse.ok) {", body)

    def test_default_reply_loader_http_failures_parse_detail_payloads_before_throwing_and_toasting(self):
        body = _extract_function_body(self.app_js, "loadDefaultReplies")

        self.assertIn("showToast(`加载默认回复列表失败: ${error.message || '请稍后重试'}`, 'danger');", body)

        accounts_error_index = body.index("const accountsErrorMessage = await readResponseErrorMessage(accountsResponse, `HTTP ${accountsResponse.status}`);")
        accounts_throw_index = body.index("throw new Error(accountsErrorMessage);", accounts_error_index)
        self.assertLess(
            accounts_error_index,
            accounts_throw_index,
            "默认回复 root loader 拉账号失败时得先把 detail/message 解出来，别整固定错误糊弄人",
        )
        self.assertLess(
            body.find("requestSequence !== defaultRepliesLoadRequestSequence", accounts_error_index),
            accounts_throw_index,
            "默认回复 root loader 账号失败响应读完错误体后，先验请求还活着，再决定要不要往 catch 里抛",
        )

        replies_error_index = body.index("const repliesErrorMessage = await readResponseErrorMessage(repliesResponse, `HTTP ${repliesResponse.status}`);")
        replies_throw_index = body.index("throw new Error(repliesErrorMessage);", replies_error_index)
        self.assertLess(
            replies_error_index,
            replies_throw_index,
            "默认回复 root loader 拉配置失败时也得先把 detail/message 解出来，别整固定错误糊弄人",
        )
        self.assertLess(
            body.find("requestSequence !== defaultRepliesLoadRequestSequence", replies_error_index),
            replies_throw_index,
            "默认回复 root loader 配置失败响应读完错误体后，先验请求还活着，再决定要不要往 catch 里抛",
        )

    def test_default_reply_table_escapes_account_reply_content_and_inline_actions(self):
        body = _extract_function_body(self.app_js, "renderDefaultRepliesList")
        self.assertIn("const safeAccountId = escapeHtml(accountId);", body)
        self.assertIn("const safeAccountIdForJs = escapeInlineJsSingleQuotedString(accountId);", body)
        self.assertIn("const safeReplyContentAttr = escapeHtmlAttribute(replySettings.reply_content || '');", body)
        self.assertIn("const safeContentPreview = escapeHtml(contentPreview);", body)
        self.assertIn('<strong class="text-primary">${safeAccountId}</strong>', body)
        self.assertIn('title="${safeReplyContentAttr}"', body)
        self.assertIn("${safeContentPreview}", body)
        self.assertIn("onclick=\"editDefaultReply('${safeAccountIdForJs}')\"", body)
        self.assertIn("onclick=\"clearDefaultReplyRecords('${safeAccountIdForJs}')\"", body)
        self.assertNotIn("${accountId}</strong>", body)
        self.assertNotIn('title="${replySettings.reply_content || \'\'}"', body)
        self.assertNotIn("${contentPreview}", body)
        self.assertNotIn("onclick=\"editDefaultReply('${accountId}')\"", body)
        self.assertNotIn("onclick=\"clearDefaultReplyRecords('${accountId}')\"", body)

    def test_default_reply_editor_requests_encode_account_ids_in_path_segments(self):
        edit_body = _extract_function_body(self.app_js, "editDefaultReply")
        save_body = _extract_function_body(self.app_js, "saveDefaultReply")
        clear_body = _extract_function_body(self.app_js, "clearDefaultReplyRecords")

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", edit_body)
        self.assertIn("fetch(`${apiBase}/default-replies/${encodedAccountId}`", edit_body)
        self.assertNotIn("fetch(`${apiBase}/default-replies/${accountId}`", edit_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", save_body)
        self.assertIn("fetch(`${apiBase}/default-replies/${encodedAccountId}`", save_body)
        self.assertNotIn("fetch(`${apiBase}/default-replies/${accountId}`", save_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", clear_body)
        self.assertIn("fetch(`${apiBase}/default-replies/${encodedAccountId}/clear-records`", clear_body)
        self.assertNotIn("fetch(`${apiBase}/default-replies/${accountId}/clear-records`", clear_body)

    def test_default_reply_mutations_only_report_success_when_followup_reload_succeeds(self):
        save_body = _extract_function_body(self.app_js, "saveDefaultReply")
        clear_body = _extract_function_body(self.app_js, "clearDefaultReplyRecords")

        self.assertIn("const repliesLoaded = await loadDefaultReplies();", save_body)
        self.assertIn("const accountsLoaded = await loadAccounts();", save_body)
        self.assertIn("if (repliesLoaded === true && accountsLoaded === true) {", save_body)
        self.assertIn("} else if (repliesLoaded === false || accountsLoaded === false) {", save_body)
        self.assertIn("showToast('默认回复设置保存成功', 'success');", save_body)
        self.assertIn("showToast('默认回复设置保存成功，但列表或账号状态刷新失败，请稍后手动刷新', 'warning');", save_body)

        self.assertIn("const repliesLoaded = await loadDefaultReplies();", clear_body)
        self.assertIn("if (repliesLoaded) {", clear_body)
        self.assertIn("showToast(`账号 \"${accountId}\" 的默认回复记录已清空`, 'success');", clear_body)
        self.assertIn("showToast(`账号 \"${accountId}\" 的默认回复记录已清空，但列表刷新失败，请稍后手动刷新`, 'warning');", clear_body)

    def test_clear_default_reply_records_ignore_older_same_page_responses(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "clearDefaultReplyRecords")

        self.assertIn("let accountMutationActionRequestSequence = 0;", self.app_js)
        self.assertIn("accountMutationActionRequestSequence += 1;", show_section_body)
        self.assertIn("const actionRequestSequence = ++accountMutationActionRequestSequence;", body)
        self.assertIn("actionRequestSequence !== accountMutationActionRequestSequence", body)
        self.assertLess(
            body.index("actionRequestSequence !== accountMutationActionRequestSequence"),
            body.index("const repliesLoaded = await loadDefaultReplies();"),
            "同页已经发起新的默认回复记录清空动作后，旧响应不该再回来触发列表刷新",
        )
        self.assertLess(
            body.rfind(
                "actionRequestSequence !== accountMutationActionRequestSequence",
                0,
                body.index("showToast(`账号 \"${accountId}\" 的默认回复记录已清空`, 'success');"),
            ),
            body.index("showToast(`账号 \"${accountId}\" 的默认回复记录已清空`, 'success');"),
            "同页已经发起新的默认回复记录清空动作后，旧成功响应别再回来刷 success toast",
        )

    def test_clear_default_reply_records_failure_toast_rechecks_stale_state_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "clearDefaultReplyRecords")
        error_text_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        danger_toast_index = body.index("showToast(`清空失败: ${error}`, 'danger');")

        self.assertLess(
            body.find("actionRequestSequence !== accountMutationActionRequestSequence", error_text_index),
            danger_toast_index,
            "同页已经发起新的默认回复记录清空动作后，旧失败响应读完错误文本也别再回魂甩红字",
        )
        self.assertLess(
            body.find("!document.getElementById('accounts-section')?.classList.contains('active')", error_text_index),
            danger_toast_index,
            "都切出账号页了，旧清空默认回复记录失败响应读完错误文本也别再跨页弹 danger toast",
        )

    def test_default_reply_root_loader_and_manager_ignore_stale_async_responses_and_hidden_accounts(self):
        self.assertIn("let defaultRepliesLoadRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        open_body = _extract_function_body(self.app_js, "openDefaultReplyManager")
        load_body = _extract_function_body(self.app_js, "loadDefaultReplies")

        self.assertIn("defaultRepliesLoadRequestSequence += 1;", show_section_body)
        self.assertIn("const requestSequence = ++defaultRepliesLoadRequestSequence;", load_body)
        self.assertIn("requestSequence !== defaultRepliesLoadRequestSequence", load_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("requestSequence !== defaultRepliesLoadRequestSequence"),
            load_body.index("renderDefaultRepliesList(accounts, defaultReplies);"),
            "旧的默认回复列表请求不该晚回来后把隐藏页表格再糊回去",
        )
        replies_fetch_index = load_body.index("fetch(`${apiBase}/default-replies`, {")
        accounts_json_index = load_body.index("const accounts = await accountsResponse.json();")
        self.assertLess(
            replies_fetch_index,
            accounts_json_index,
            "默认回复 root loader 的账号列表和配置本来就互不依赖，别傻等账号列表回来后再串行打第二枪",
        )
        self.assertLess(
            load_body.find("requestSequence !== defaultRepliesLoadRequestSequence", accounts_json_index),
            load_body.index("if (!repliesResponse.ok) {"),
            "默认回复 root loader 在账号列表这一步已经 stale 后，不该继续往下处理默认回复配置响应",
        )
        self.assertLess(
            load_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, load_body.index("showToast(`加载默认回复列表失败: ${error.message || '请稍后重试'}`, 'danger');")),
            load_body.index("showToast(`加载默认回复列表失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "都切出账号页了，旧的默认回复列表失败就别再跨页甩 danger toast 了",
        )

        self.assertIn("const repliesLoaded = await loadDefaultReplies();", open_body)
        self.assertIn("if (repliesLoaded !== true) {", open_body)
        self.assertLess(
            open_body.index("if (repliesLoaded !== true) {"),
            open_body.index("modal.show();"),
            "默认回复 root loader 都失败或 stale 了，就别再把管理弹窗硬弹出来装正常",
        )

    def test_default_reply_editor_and_mutations_ignore_stale_async_responses_and_hidden_state(self):
        self.assertIn("let defaultReplyEditorRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        edit_body = _extract_function_body(self.app_js, "editDefaultReply")
        save_body = _extract_function_body(self.app_js, "saveDefaultReply")
        clear_body = _extract_function_body(self.app_js, "clearDefaultReplyRecords")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("defaultReplyEditorRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++defaultReplyEditorRequestSequence;", edit_body)
        self.assertIn("requestSequence !== defaultReplyEditorRequestSequence", edit_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", edit_body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", edit_body)
        self.assertIn("defaultReplyEditorRequestSequence += 1;", edit_body)
        self.assertIn("modalElement.dataset.defaultReplyEditorModalBound = 'true';", edit_body)
        self.assertIn("return null;", edit_body)
        self.assertLess(
            edit_body.index("requestSequence !== defaultReplyEditorRequestSequence"),
            edit_body.index("document.getElementById('editDefaultReplyAccountId').value = accountId;"),
            "旧的默认回复详情请求不该晚回来后把当前编辑弹窗改成别的账号配置",
        )
        modal_show_index = edit_body.index("modal.show();")
        self.assertLess(
            edit_body.rfind("requestSequence !== defaultReplyEditorRequestSequence", 0, modal_show_index),
            modal_show_index,
            "都切页或关弹窗了，旧的默认回复详情请求不该再回来把弹窗重新弹出来",
        )

        self.assertIn("const requestSequence = defaultReplyEditorRequestSequence;", save_body)
        self.assertIn("requestSequence !== defaultReplyEditorRequestSequence", save_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", save_body)
        self.assertIn("modalElement.dataset.defaultReplyEditorIgnoreNextHidden = 'true';", save_body)
        self.assertIn("return null;", save_body)
        self.assertLess(
            save_body.index("requestSequence !== defaultReplyEditorRequestSequence"),
            save_body.index("modal.hide();"),
            "旧的默认回复保存响应不该回来把已经重开的编辑弹窗又关掉",
        )

        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", clear_body)
        self.assertIn("return null;", clear_body)
        self.assertLess(
            clear_body.index("!document.getElementById('accounts-section')?.classList.contains('active')"),
            clear_body.index("showToast(`账号 \"${accountId}\" 的默认回复记录已清空`, 'success');"),
            "都离开账号页了，旧的清空默认回复记录响应就别跨页弹 success toast 烦人",
        )

    def test_default_reply_and_account_ai_raw_fetches_redirect_on_401_before_followup_processing(self):
        load_body = _extract_function_body(self.app_js, "loadDefaultReplies")
        edit_body = _extract_function_body(self.app_js, "editDefaultReply")
        save_default_body = _extract_function_body(self.app_js, "saveDefaultReply")
        clear_body = _extract_function_body(self.app_js, "clearDefaultReplyRecords")
        save_ai_body = _extract_function_body(self.app_js, "saveAIReplyConfig")
        test_ai_body = _extract_function_body(self.app_js, "testAIReply")

        self.assertIn("if (handleUnauthorizedApiResponse(accountsResponse)) {", load_body)
        self.assertLess(
            load_body.index("if (handleUnauthorizedApiResponse(accountsResponse)) {"),
            load_body.index("if (!accountsResponse.ok) {"),
            "默认回复列表在账号接口 401 时应先跳登录，别继续装成普通加载失败",
        )
        self.assertIn("if (handleUnauthorizedApiResponse(repliesResponse)) {", load_body)
        self.assertLess(
            load_body.index("if (handleUnauthorizedApiResponse(repliesResponse)) {"),
            load_body.index("if (!repliesResponse.ok) {"),
            "默认回复列表在配置接口 401 时应先跳登录，别继续装成普通加载失败",
        )

        for body, anchor_fragment, function_name in (
            (edit_body, "if (!response.ok) {", "editDefaultReply"),
            (save_default_body, "if (response.ok) {", "saveDefaultReply"),
            (clear_body, "if (response.ok) {", "clearDefaultReplyRecords"),
            (save_ai_body, "if (response.ok) {", "saveAIReplyConfig"),
            (test_ai_body, "if (response.ok) {", "testAIReply"),
        ):
            with self.subTest(function_name=function_name):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{function_name} 遇到 401 时应先跳登录，别继续把未授权响应往后当正常流程折腾",
                )

    def test_default_reply_editor_and_account_ai_failures_parse_error_payloads_with_helper(self):
        edit_body = _extract_function_body(self.app_js, "editDefaultReply")
        save_default_body = _extract_function_body(self.app_js, "saveDefaultReply")
        clear_body = _extract_function_body(self.app_js, "clearDefaultReplyRecords")
        save_ai_body = _extract_function_body(self.app_js, "saveAIReplyConfig")
        test_ai_body = _extract_function_body(self.app_js, "testAIReply")

        self.assertIn("if (!response.ok) {", edit_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", edit_body)
        self.assertIn("throw new Error(errorMessage);", edit_body)
        self.assertIn("showToast(`获取默认回复设置失败: ${error.message || '请稍后重试'}`, 'danger');", edit_body)
        edit_error_index = edit_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        edit_throw_index = edit_body.index("throw new Error(errorMessage);", edit_error_index)
        edit_toast_index = edit_body.index("showToast(`获取默认回复设置失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            edit_body.index("if (!response.ok) {"),
            edit_body.index("document.getElementById('editDefaultReplyAccountId').value = accountId;"),
            "默认回复详情接口都失败了，就别继续拿空配置把编辑弹窗硬弹出来装正常",
        )
        self.assertLess(
            edit_error_index,
            edit_throw_index,
            "默认回复详情接口 HTTP 失败时得先把 detail/message 解出来，别整句固定错误糊弄人",
        )
        self.assertLess(
            edit_body.find("requestSequence !== defaultReplyEditorRequestSequence", edit_error_index),
            edit_throw_index,
            "默认回复详情旧失败响应读完错误体后，先验弹窗会话还活着，再决定要不要继续抛错",
        )
        self.assertLess(
            edit_throw_index,
            edit_toast_index,
            "默认回复详情抛出结构化错误后，再由 catch 统一带上真实后端消息提示用户",
        )

        for body, failure_fragment, label in (
            (save_default_body, "showToast(`保存失败: ${error}`, 'danger');", "默认回复保存"),
            (clear_body, "showToast(`清空失败: ${error}`, 'danger');", "默认回复记录清空"),
            (save_ai_body, "showToast(`保存失败: ${error}`, 'danger');", "AI回复配置保存"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(failure_fragment),
                    f"{label}失败时得先把 detail/message 解出来，别把 JSON 原文直接糊用户脸上",
                )

        self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", test_ai_body)
        self.assertIn("const safeReply = escapeHtml(result.reply || '').replace(/\\n/g, '<br>');", test_ai_body)
        self.assertIn("const safeError = escapeHtml(error);", test_ai_body)
        self.assertIn("const safeErrorMessage = escapeHtml(error.message || '未知错误');", test_ai_body)
        self.assertLess(
            test_ai_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            test_ai_body.index("showToast(`测试失败: ${error}`, 'danger');"),
            "AI 回复测试失败时也得先把 detail/message 解出来，再决定怎么提示用户",
        )
        self.assertLess(
            test_ai_body.index("const safeReply = escapeHtml(result.reply || '').replace(/\\n/g, '<br>');"),
            test_ai_body.index("testReplyContent.innerHTML = safeReply;"),
            "AI 回复测试成功内容回写到 innerHTML 前得先转义并保留换行，别让模型输出顺手插脚本",
        )
        self.assertLess(
            test_ai_body.index("const safeError = escapeHtml(error);"),
            test_ai_body.index("testReplyContent.innerHTML = `<span class=\"text-danger\">测试失败: ${safeError}</span>`;"),
            "AI 回复测试失败信息回写到 innerHTML 前得先转义，别让后端错误消息顺手插脚本",
        )

    def test_default_reply_and_account_ai_catch_failures_surface_runtime_error_messages(self):
        for body, legacy_toast, legacy_toast_count, runtime_toast, guard_fragments, label in (
            (
                _extract_function_body(self.app_js, "openDefaultReplyManager"),
                "showToast('打开默认回复管理器失败', 'danger');",
                0,
                "showToast(`打开默认回复管理器失败: ${error.message || '请稍后重试'}`, 'danger');",
                ("!document.getElementById('accounts-section')?.classList.contains('active')",),
                "打开默认回复管理器",
            ),
            (
                _extract_function_body(self.app_js, "saveDefaultReply"),
                "showToast('保存默认回复设置失败', 'danger');",
                0,
                "showToast(`保存默认回复设置失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== defaultReplyEditorRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "保存默认回复设置",
            ),
            (
                _extract_function_body(self.app_js, "clearDefaultReplyRecords"),
                "showToast('清空默认回复记录失败', 'danger');",
                0,
                "showToast(`清空默认回复记录失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== accountMutationActionRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "清空默认回复记录",
            ),
            (
                _extract_function_body(self.app_js, "configAIReply"),
                "showToast('获取AI回复设置失败', 'danger');",
                1,
                "showToast(`获取AI回复设置失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== aiReplyConfigRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "打开 AI 回复配置弹窗",
            ),
            (
                _extract_function_body(self.app_js, "saveAIReplyConfig"),
                "showToast('保存AI回复配置失败', 'danger');",
                0,
                "showToast(`保存AI回复配置失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== aiReplyConfigRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "保存 AI 回复配置",
            ),
            (
                _extract_function_body(self.app_js, "testAIReply"),
                "showToast('测试AI回复失败', 'danger');",
                0,
                "showToast(`测试AI回复失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== aiReplyConfigRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "测试 AI 回复",
            ),
            (
                _extract_function_body(self.app_js, "saveCurrentAsPreset"),
                "showToast('保存预设失败', 'danger');",
                0,
                "showToast(`保存预设失败: ${e.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== aiReplyConfigRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "保存 AI 预设",
            ),
            (
                _extract_function_body(self.app_js, "deleteSelectedPreset"),
                "showToast('删除预设失败', 'danger');",
                0,
                "showToast(`删除预设失败: ${e.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== aiReplyConfigRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "删除 AI 预设",
            ),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    body.count(legacy_toast),
                    legacy_toast_count,
                    f"{label} 别再把 catch 兜底写回固定红字了，用户明明能拿到运行时异常",
                )
                self.assertIn(runtime_toast, body)
                toast_index = body.index(runtime_toast)
                for guard_fragment in guard_fragments:
                    self.assertIn(guard_fragment, body)
                    self.assertLess(
                        body.rfind(guard_fragment, 0, toast_index),
                        toast_index,
                        f"{label} catch 里的旧异常在弹 toast 前也得先过会话/页面活性校验，别 stale 了还回来犯病",
                    )

        test_ai_body = _extract_function_body(self.app_js, "testAIReply")
        self.assertLess(
            test_ai_body.index("testReplyContent.innerHTML = `<span class=\"text-danger\">测试失败: ${safeErrorMessage}</span>`;"),
            test_ai_body.index("showToast(`测试AI回复失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "AI 回复测试 catch 分支也得先把安全错误态回写到面板，再弹 toast，别界面还挂着旧结果装没事",
        )

    def test_account_ai_fetchjson_flows_abort_when_unauthorized_redirect_returns_no_payload(self):
        config_body = _extract_function_body(self.app_js, "configAIReply")
        load_presets_body = _extract_function_body(self.app_js, "loadAIPresets")
        save_preset_body = _extract_function_body(self.app_js, "saveCurrentAsPreset")
        delete_preset_body = _extract_function_body(self.app_js, "deleteSelectedPreset")

        self.assertIn("if (!settings) {", config_body)
        self.assertLess(
            config_body.index("if (!settings) {"),
            config_body.index("document.getElementById('aiConfigAccountId').value = accountId;"),
            "AI 配置详情在 fetchJSON 因 401 跳转返回空结果后，应直接收手，别继续拿 undefined 往表单里怼",
        )

        self.assertIn("if (presets == null) {", load_presets_body)
        self.assertLess(
            load_presets_body.index("if (presets == null) {"),
            load_presets_body.index("_aiPresets = presets || [];"),
            "AI 预设列表在 401 跳转返回空结果后应直接收手，别继续把空结果当成功列表处理",
        )

        self.assertIn("const saveResult = await fetchJSON(`${apiBase}/ai-config-presets`, {", save_preset_body)
        self.assertIn("if (!saveResult) {", save_preset_body)
        self.assertLess(
            save_preset_body.index("if (!saveResult) {"),
            save_preset_body.index("const presetsLoaded = await loadAIPresets(requestSequence);"),
            "AI 预设保存在 401 跳转返回空结果后应直接收手，别继续刷成功或 warning 提示",
        )

        self.assertIn("const deleteResult = await fetchJSON(`${apiBase}/ai-config-presets/${presetId}`, {", delete_preset_body)
        self.assertIn("if (!deleteResult) {", delete_preset_body)
        self.assertLess(
            delete_preset_body.index("if (!deleteResult) {"),
            delete_preset_body.index("const presetsLoaded = await loadAIPresets(requestSequence);"),
            "AI 预设删除在 401 跳转返回空结果后应直接收手，别继续刷成功或 warning 提示",
        )

    def test_ai_reply_requests_encode_account_ids_in_path_segments(self):
        config_body = _extract_function_body(self.app_js, "configAIReply")
        save_body = _extract_function_body(self.app_js, "saveAIReplyConfig")
        test_body = _extract_function_body(self.app_js, "testAIReply")

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", config_body)
        self.assertIn("fetchJSON(`${apiBase}/ai-reply-settings/${encodedAccountId}`, {", config_body)
        self.assertIn("suppressErrorToast: true", config_body)
        self.assertNotIn("fetchJSON(`${apiBase}/ai-reply-settings/${accountId}`)", config_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", save_body)
        self.assertIn("fetch(`${apiBase}/ai-reply-settings/${encodedAccountId}`", save_body)
        self.assertNotIn("fetch(`${apiBase}/ai-reply-settings/${accountId}`", save_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", test_body)
        self.assertIn("fetch(`${apiBase}/ai-reply-test/${encodedAccountId}`", test_body)
        self.assertNotIn("fetch(`${apiBase}/ai-reply-test/${accountId}`", test_body)

    def test_ai_reply_mutations_only_report_success_when_followup_reload_succeeds(self):
        save_body = _extract_function_body(self.app_js, "saveAIReplyConfig")
        save_preset_body = _extract_function_body(self.app_js, "saveCurrentAsPreset")
        delete_preset_body = _extract_function_body(self.app_js, "deleteSelectedPreset")

        self.assertIn("const accountsLoaded = await loadAccounts();", save_body)
        self.assertIn("if (accountsLoaded === true) {", save_body)
        self.assertIn("} else if (accountsLoaded === false) {", save_body)
        self.assertIn("showToast('AI回复配置保存成功', 'success');", save_body)
        self.assertIn("showToast('AI回复配置保存成功，但账号列表刷新失败，请稍后手动刷新', 'warning');", save_body)

        self.assertIn("const presetsLoaded = await loadAIPresets(requestSequence);", save_preset_body)
        self.assertIn("if (presetsLoaded) {", save_preset_body)
        self.assertIn("showToast('预设保存成功', 'success');", save_preset_body)
        self.assertIn("showToast('预设保存成功，但预设列表刷新失败，请稍后手动刷新', 'warning');", save_preset_body)

        self.assertIn("const presetsLoaded = await loadAIPresets(requestSequence);", delete_preset_body)
        self.assertIn("if (presetsLoaded) {", delete_preset_body)
        self.assertIn("showToast('预设已删除', 'success');", delete_preset_body)
        self.assertIn("showToast('预设已删除，但预设列表刷新失败，请稍后手动刷新', 'warning');", delete_preset_body)

    def test_ai_reply_config_modal_and_preset_actions_ignore_stale_async_responses_and_hidden_state(self):
        self.assertIn("let aiReplyConfigRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        config_body = _extract_function_body(self.app_js, "configAIReply")
        load_presets_body = _extract_function_body(self.app_js, "loadAIPresets")
        save_body = _extract_function_body(self.app_js, "saveAIReplyConfig")
        save_preset_body = _extract_function_body(self.app_js, "saveCurrentAsPreset")
        delete_preset_body = _extract_function_body(self.app_js, "deleteSelectedPreset")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("aiReplyConfigRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++aiReplyConfigRequestSequence;", config_body)
        self.assertIn("requestSequence !== aiReplyConfigRequestSequence", config_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", config_body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", config_body)
        self.assertIn("aiReplyConfigRequestSequence += 1;", config_body)
        self.assertIn("modalElement.dataset.aiReplyConfigModalBound = 'true';", config_body)
        self.assertIn("const presetsLoaded = await loadAIPresets(requestSequence);", config_body)
        self.assertIn("return null;", config_body)
        self.assertLess(
            config_body.index("requestSequence !== aiReplyConfigRequestSequence"),
            config_body.index("document.getElementById('aiConfigAccountId').value = accountId;"),
            "旧的 AI 回复配置请求不该晚回来后把当前编辑弹窗改成别的账号配置",
        )
        modal_show_index = config_body.index("modal.show();")
        self.assertLess(
            config_body.rfind("requestSequence !== aiReplyConfigRequestSequence", 0, modal_show_index),
            modal_show_index,
            "都切页或关弹窗了，旧的 AI 回复配置请求不该再回来把弹窗重新弹出来",
        )

        self.assertIn("async function loadAIPresets(requestSequence = 0) {", self.app_js)
        self.assertIn("suppressErrorToast: true", load_presets_body)
        self.assertIn("requestSequence !== 0", load_presets_body)
        self.assertIn("requestSequence !== aiReplyConfigRequestSequence", load_presets_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", load_presets_body)
        self.assertIn("return null;", load_presets_body)
        self.assertLess(
            load_presets_body.index("requestSequence !== aiReplyConfigRequestSequence"),
            load_presets_body.index("select.innerHTML = '<option value=\"\">-- 选择预设 --</option>';"),
            "旧的 AI 预设列表请求不该晚回来把当前弹窗里的预设下拉框再糊回旧状态",
        )

        self.assertIn("const requestSequence = aiReplyConfigRequestSequence;", save_body)
        self.assertIn("requestSequence !== aiReplyConfigRequestSequence", save_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", save_body)
        self.assertIn("modalElement.dataset.aiReplyConfigIgnoreNextHidden = 'true';", save_body)
        self.assertIn("return null;", save_body)
        self.assertLess(
            save_body.index("requestSequence !== aiReplyConfigRequestSequence"),
            save_body.index("modal.hide();"),
            "旧的 AI 回复保存响应不该回来把已经重开的配置弹窗又关掉",
        )

        for body, success_fragment in (
            (save_preset_body, "showToast('预设保存成功', 'success');"),
            (delete_preset_body, "showToast('预设已删除', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const requestSequence = aiReplyConfigRequestSequence;", body)
                self.assertIn("requestSequence !== aiReplyConfigRequestSequence", body)
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("const presetsLoaded = await loadAIPresets(requestSequence);", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("requestSequence !== aiReplyConfigRequestSequence"),
                    body.index(success_fragment),
                    "都离开账号页或换过弹窗会话了，旧的 AI 预设操作响应就别再跨状态弹 success toast 了",
                )

        self.assertIn("suppressErrorToast: true", save_preset_body)
        self.assertIn("suppressErrorToast: true", delete_preset_body)

    def test_ai_reply_config_modal_reports_single_warning_when_preset_list_refresh_fails(self):
        body = _extract_function_body(self.app_js, "configAIReply")
        self.assertIn("if (presetsLoaded === false) {", body)
        self.assertIn("showToast('AI预设加载失败，请稍后重试', 'warning');", body)

    def test_ai_preset_loader_resets_stale_select_and_cached_presets_before_fetch(self):
        body = _extract_function_body(self.app_js, "loadAIPresets")
        self.assertIn("_aiPresets = [];", body)
        self.assertIn("const select = document.getElementById('aiPresetSelect');", body)
        self.assertIn("const deleteBtn = document.getElementById('deletePresetBtn');", body)
        self.assertIn("select.innerHTML = '<option value=\"\">-- 选择预设 --</option>';",
                      body)
        self.assertIn("deleteBtn.style.display = 'none';", body)
        self.assertLess(
            body.index("_aiPresets = [];"),
            body.index("const presets = await fetchJSON(`${apiBase}/ai-config-presets`, {"),
            "AI 预设重新加载前就该先清掉旧缓存，别接口一挂还拿上一轮的预设装正常",
        )
        self.assertLess(
            body.index("select.innerHTML = '<option value=\"\">-- 选择预设 --</option>';"),
            body.index("const presets = await fetchJSON(`${apiBase}/ai-config-presets`, {"),
            "AI 预设重新加载前应先把下拉框重置成默认空态，别失败后继续挂陈年 option 误导用户",
        )

    def test_ai_reply_test_action_ignores_stale_modal_session_and_hidden_accounts_state(self):
        body = _extract_function_body(self.app_js, "testAIReply")

        self.assertIn("const requestSequence = aiReplyConfigRequestSequence;", body)
        self.assertIn("requestSequence !== aiReplyConfigRequestSequence", body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== aiReplyConfigRequestSequence"),
            body.index("testReplyContent.innerHTML = safeReply;"),
            "旧的 AI 测试响应不该晚回来后把当前弹窗的测试结果面板改成老会话内容",
        )
        self.assertLess(
            body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index("showToast('AI回复测试成功', 'success');")),
            body.index("showToast('AI回复测试成功', 'success');"),
            "都切出账号页了，旧的 AI 测试成功不该再跨页弹 success toast",
        )
        finally_block = body.split("} finally {", 1)[1]
        self.assertIn("requestSequence !== aiReplyConfigRequestSequence", finally_block)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", finally_block)
        self.assertLess(
            finally_block.index("requestSequence !== aiReplyConfigRequestSequence"),
            finally_block.index("testBtn.disabled = false;"),
            "AI 测试会话都切走了，旧 finally 就别把当前弹窗按钮 disabled 状态回写回去了",
        )

    def test_message_notifications_loader_resets_stale_table_before_fetch_and_returns_status(self):
        load_body = _extract_function_body(self.app_js, "loadMessageNotifications")
        reset_body = _extract_function_body(self.app_js, "resetMessageNotificationsTable")

        self.assertIn("const tbody = document.getElementById('notificationsTableBody');", reset_body)
        self.assertIn("${escapeHtml(message)}", reset_body)
        self.assertIn("resetMessageNotificationsTable();", load_body)
        self.assertIn("if (!accountsResponse.ok) {", load_body)
        self.assertIn("const accountsErrorMessage = await readResponseErrorMessage(accountsResponse, `HTTP ${accountsResponse.status}`);", load_body)
        self.assertIn("throw new Error(accountsErrorMessage);", load_body)
        self.assertIn("if (!notificationsResponse.ok) {", load_body)
        self.assertIn("const notificationsErrorMessage = await readResponseErrorMessage(notificationsResponse, `HTTP ${notificationsResponse.status}`);", load_body)
        self.assertIn("throw new Error(notificationsErrorMessage);", load_body)
        self.assertIn("resetMessageNotificationsTable('加载消息通知配置失败');", load_body)
        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("resetMessageNotificationsTable();"),
            load_body.index("const [accountsResponse, notificationsResponse] = await Promise.all(["),
            "消息通知重新加载前也得先清掉旧列表，失败时别继续拿老数据糊人",
        )

    def test_message_notification_mutations_only_report_success_when_reload_succeeds(self):
        save_body = _extract_function_body(self.app_js, "saveAccountNotification")
        delete_body = _extract_function_body(self.app_js, "deleteAccountNotification")

        self.assertIn("const notificationsLoaded = await loadMessageNotifications();", save_body)
        self.assertIn("if (notificationsLoaded === true) {", save_body)
        self.assertIn("} else if (notificationsLoaded === false) {", save_body)
        self.assertIn("showToast('通知配置保存成功', 'success');", save_body)
        self.assertIn("showToast('通知配置保存成功，但列表刷新失败，请稍后手动刷新', 'warning');", save_body)

        self.assertIn("const notificationsLoaded = await loadMessageNotifications();", delete_body)
        self.assertIn("if (notificationsLoaded === true) {", delete_body)
        self.assertIn("} else if (notificationsLoaded === false) {", delete_body)
        self.assertIn("showToast('通知配置删除成功', 'success');", delete_body)
        self.assertIn("showToast('通知配置删除成功，但列表刷新失败，请稍后手动刷新', 'warning');", delete_body)

    def test_message_notification_mutations_do_not_emit_cross_page_toasts_after_leaving_section(self):
        save_body = _extract_function_body(self.app_js, "saveAccountNotification")
        delete_body = _extract_function_body(self.app_js, "deleteAccountNotification")

        for body, success_fragment in (
            (save_body, "showToast('通知配置保存成功', 'success');"),
            (delete_body, "showToast('通知配置删除成功', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('message-notifications-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('message-notifications-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "消息通知操作在离开页面后不该再跨页弹 success toast",
                )

    def test_account_notification_save_respects_config_modal_request_sequence_before_hiding_or_toasting(self):
        config_body = _extract_function_body(self.app_js, "configAccountNotification")
        body = _extract_function_body(self.app_js, "saveAccountNotification")
        self.assertIn("if (modalElement.dataset.accountNotificationConfigIgnoreNextHidden === 'true') {", config_body)
        self.assertIn("modalElement.dataset.accountNotificationConfigIgnoreNextHidden = 'false';", config_body)
        self.assertIn("const requestSequence = accountNotificationConfigRequestSequence;", body)
        self.assertIn("requestSequence !== accountNotificationConfigRequestSequence", body)
        self.assertIn("modalElement.dataset.accountNotificationConfigIgnoreNextHidden = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== accountNotificationConfigRequestSequence"),
            body.index("modal.hide();"),
            "账号通知保存的旧响应不该回来把已经重开的配置弹窗又关掉",
        )

    def test_account_notification_save_actions_ignore_older_same_modal_responses(self):
        body = _extract_function_body(self.app_js, "saveAccountNotification")

        self.assertIn("++messageNotificationMutationActionRequestSequence", body)
        self.assertIn("actionRequestSequence !== messageNotificationMutationActionRequestSequence", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("actionRequestSequence !== messageNotificationMutationActionRequestSequence"),
            body.index("modal.hide();"),
            "同一账号通知配置弹窗里第二次保存已经发出后，第一次响应不该回来把当前弹窗关掉",
        )
        self.assertLess(
            body.rfind("actionRequestSequence !== messageNotificationMutationActionRequestSequence", 0, body.index("showToast(`保存失败: ${error}`, 'danger');")),
            body.index("showToast(`保存失败: ${error}`, 'danger');"),
            "同一账号通知配置弹窗里旧的失败响应不该晚回来后拿旧错误糊当前会话一脸",
        )

    def test_message_notification_delete_actions_ignore_older_same_page_responses(self):
        self.assertIn("let messageNotificationMutationActionRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        delete_body = _extract_function_body(self.app_js, "deleteAccountNotification")

        self.assertIn("messageNotificationMutationActionRequestSequence += 1;", show_section_body)
        self.assertIn("++messageNotificationMutationActionRequestSequence", delete_body)
        self.assertIn("actionRequestSequence !== messageNotificationMutationActionRequestSequence", delete_body)
        self.assertIn("return null;", delete_body)
        self.assertLess(
            delete_body.index("actionRequestSequence !== messageNotificationMutationActionRequestSequence"),
            delete_body.index("const notificationsLoaded = await loadMessageNotifications();"),
            "同页连续删除通知配置时，旧响应不该晚回来后又触发列表刷新和旧结果 toast",
        )
        error_text_index = delete_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        failure_toast_index = delete_body.index("showToast(`删除失败: ${error}`, 'danger');")
        self.assertLess(
            delete_body.find("actionRequestSequence !== messageNotificationMutationActionRequestSequence", error_text_index),
            failure_toast_index,
            "同页连续删除通知配置时，读完错误体后还得再验一次 stale，别让旧失败响应回魂刷红字",
        )

    def test_message_notification_mutation_catch_failures_surface_runtime_error_messages(self):
        save_body = _extract_function_body(self.app_js, "saveAccountNotification")
        delete_body = _extract_function_body(self.app_js, "deleteAccountNotification")

        for body, legacy_toast, failure_toast, extra_guard, action_name in (
            (
                save_body,
                "showToast('保存通知配置失败', 'danger');",
                "showToast(`保存通知配置失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== accountNotificationConfigRequestSequence",
                "保存账号通知配置",
            ),
            (
                delete_body,
                "showToast('删除通知配置失败', 'danger');",
                "showToast(`删除通知配置失败: ${error.message || '请稍后重试'}`, 'danger');",
                None,
                "删除账号通知配置",
            ),
        ):
            with self.subTest(action_name=action_name):
                self.assertNotIn(legacy_toast, body)
                self.assertIn(failure_toast, body)
                failure_toast_index = body.index(failure_toast)
                self.assertLess(
                    body.rfind("actionRequestSequence !== messageNotificationMutationActionRequestSequence", 0, failure_toast_index),
                    failure_toast_index,
                    f"{action_name} catch 兜底前得先验 mutation action sequence，别让旧异常跨会话回魂乱喷红字",
                )
                if extra_guard is not None:
                    self.assertLess(
                        body.rfind(extra_guard, 0, failure_toast_index),
                        failure_toast_index,
                        f"{action_name} catch 兜底前得先验当前配置弹窗会话，别拿旧异常糊当前窗口",
                    )
                self.assertLess(
                    body.rfind("!document.getElementById('message-notifications-section')?.classList.contains('active')", 0, failure_toast_index),
                    failure_toast_index,
                    f"都切出消息通知页了，{action_name} 的旧异常就别跨页回来刷 danger toast 了",
                )

    def test_message_notification_mutation_action_sequence_starts_only_after_validation_or_confirmation(self):
        save_body = _extract_function_body(self.app_js, "saveAccountNotification")
        delete_body = _extract_function_body(self.app_js, "deleteAccountNotification")

        self.assertLess(
            save_body.index("if (selectedChannelIds.length === 0) {"),
            save_body.index("const actionRequestSequence = ++messageNotificationMutationActionRequestSequence;"),
            "没选通知渠道时只是前端校验，别先把消息通知 mutation action sequence 顶掉别的正常动作",
        )

        delete_confirm_return_index = delete_body.index("return;", delete_body.index("if (!confirm("))
        self.assertLess(
            delete_confirm_return_index,
            delete_body.index("const actionRequestSequence = ++messageNotificationMutationActionRequestSequence;"),
            "用户都取消删除通知配置了，就别先把消息通知 mutation action sequence 顶掉别的正常动作",
        )

    def test_message_notification_config_uses_native_multi_select_validity_before_submit(self):
        body = _extract_function_body(self.app_js, "saveAccountNotification")

        self.assertIn("const notificationChannelSelect = document.getElementById('notificationChannel');", body)
        self.assertIn("if (notificationChannelSelect && !notificationChannelSelect.checkValidity()) {", body)
        self.assertIn("if (typeof notificationChannelSelect.reportValidity === 'function') {", body)
        self.assertIn("notificationChannelSelect.reportValidity();", body)
        self.assertIn("showToast('请选择通知渠道', 'warning');", body)
        self.assertLess(
            body.index("if (notificationChannelSelect && !notificationChannelSelect.checkValidity()) {"),
            body.index("const selectedChannelIds = Array.from(document.getElementById('notificationChannel').selectedOptions)"),
            "多选通知渠道原生必填都没过，就别继续往下读 selectedOptions 装没事",
        )
        self.assertLess(
            body.index("if (notificationChannelSelect && !notificationChannelSelect.checkValidity()) {"),
            body.index("const actionRequestSequence = ++messageNotificationMutationActionRequestSequence;"),
            "多选通知渠道必填没过时只是前端校验，别先把消息通知 mutation action sequence 顶掉",
        )

    def test_message_notifications_and_templates_are_invalidated_when_leaving_section(self):
        self.assertIn("let messageNotificationsRequestSequence = 0;", self.app_js)
        self.assertIn("let notificationTemplateRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        notifications_body = _extract_function_body(self.app_js, "loadMessageNotifications")
        templates_body = _extract_function_body(self.app_js, "loadNotificationTemplates")

        self.assertIn("if (sectionName !== 'message-notifications') {", show_section_body)
        self.assertIn("messageNotificationsRequestSequence += 1;", show_section_body)
        self.assertIn("notificationTemplateRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++messageNotificationsRequestSequence;", notifications_body)
        self.assertIn("requestSequence !== messageNotificationsRequestSequence", notifications_body)
        self.assertIn("!document.getElementById('message-notifications-section')?.classList.contains('active')", notifications_body)
        self.assertIn("return null;", notifications_body)

        self.assertIn("const requestSequence = ++notificationTemplateRequestSequence;", templates_body)
        self.assertIn("requestSequence !== notificationTemplateRequestSequence", templates_body)
        self.assertIn("!document.getElementById('message-notifications-section')?.classList.contains('active')", templates_body)
        self.assertIn("return null;", templates_body)
        success_index = templates_body.index("showToast('通知模板加载成功', 'success');")
        self.assertLess(
            templates_body.rfind("!document.getElementById('message-notifications-section')?.classList.contains('active')", 0, success_index),
            success_index,
            "都切出消息通知页了，旧模板请求别回来装成功弹个 toast 刷存在感",
        )

    def test_message_notification_account_requests_encode_path_segments(self):
        config_body = _extract_function_body(self.app_js, "configAccountNotification")
        delete_body = _extract_function_body(self.app_js, "deleteAccountNotification")
        save_body = _extract_function_body(self.app_js, "saveAccountNotification")

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", config_body)
        self.assertIn("fetch(`${apiBase}/message-notifications/${encodedAccountId}`", config_body)
        self.assertNotIn("fetch(`${apiBase}/message-notifications/${accountId}`", config_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", delete_body)
        self.assertIn("fetch(`${apiBase}/message-notifications/account/${encodedAccountId}`", delete_body)
        self.assertNotIn("fetch(`${apiBase}/message-notifications/account/${accountId}`", delete_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", save_body)
        self.assertIn("fetch(`${apiBase}/message-notifications/${encodedAccountId}/replace`", save_body)
        self.assertNotIn("fetch(`${apiBase}/message-notifications/account/${encodedAccountId}`", save_body)
        self.assertNotIn("fetch(`${apiBase}/message-notifications/${encodedAccountId}`", save_body)
        self.assertNotIn("fetch(`${apiBase}/message-notifications/account/${accountId}`", save_body)
        self.assertNotIn("fetch(`${apiBase}/message-notifications/${accountId}`", save_body)

    def test_notification_template_loader_backfills_all_supported_template_tabs(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        for template_type in (
            "message",
            "token_refresh",
            "delivery",
            "slider_success",
            "face_verify",
            "password_login_success",
            "cookie_refresh_success",
        ):
            with self.subTest(template_type=template_type):
                self.assertIn(f"'{template_type}'", body)

    def test_notification_template_loader_clears_stale_editor_values_before_refill(self):
        reset_body = _extract_function_body(self.app_js, "resetNotificationTemplateEditors")
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        self.assertIn("const editor = document.getElementById(`${type}-template-editor`);", reset_body)
        self.assertIn("editor.value = '';", reset_body)
        self.assertIn("const preview = document.getElementById(`${type}-template-preview`);", reset_body)
        self.assertIn("preview.textContent = '';", reset_body)
        self.assertIn("const supportedTemplateTypes = [", body)
        self.assertIn("resetNotificationTemplateEditors(supportedTemplateTypes);", body)

    def test_notification_template_loader_waits_for_latest_response_before_clearing_editors(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        response_json_index = body.index("const data = await response.json();")
        templates_index = body.index("const templates = data.templates || [];")
        reset_index = body.index("resetNotificationTemplateEditors(supportedTemplateTypes);")
        refill_index = body.index("templates.forEach(template => {")

        self.assertGreater(
            reset_index,
            response_json_index,
            "通知模板旧请求还没证明自己有效前，别先把编辑器清空，不然用户未保存内容容易被并发刷新洗没",
        )
        self.assertGreater(
            reset_index,
            templates_index,
            "通知模板要先拿到当前响应的数据，再清空旧编辑器内容，别提前把现场砸了",
        )
        self.assertLess(
            reset_index,
            refill_index,
            "通知模板编辑器还是得在当前响应回填前先清空，别让旧内容和新模板糊成一锅粥",
        )

    def test_notification_template_loader_preserves_current_active_tab_on_refresh(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        self.assertIn("const tabList = document.getElementById('notificationTemplateTabs');", body)
        self.assertIn("const activeTabButton = tabList ? tabList.querySelector('.nav-link.active') : null;", body)
        self.assertIn("const activePaneSelector = activeTabButton?.getAttribute('data-bs-target') || '#message-template';", body)
        self.assertIn("const activePane = tabContent.querySelector(activePaneSelector);", body)
        self.assertIn("const activeTab = tabList.querySelector(`[data-bs-target=\"${activePaneSelector}\"]`);", body)
        self.assertNotIn("const firstPane = tabContent.querySelector('#message-template');", body)
        self.assertNotIn("const firstTab = tabList.querySelector('#message-template-tab');", body)

    def test_notification_template_loader_restores_previous_editor_state_when_refresh_fails(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        self.assertIn("const previousEditorState = supportedTemplateTypes.map(type => ({", body)
        self.assertIn("value: document.getElementById(`${type}-template-editor`)?.value || '',", body)
        self.assertIn("preview: document.getElementById(`${type}-template-preview`)?.textContent || '',", body)
        self.assertIn("previousEditorState.forEach(state => {", body)
        self.assertIn("const editor = document.getElementById(`${state.type}-template-editor`);", body)
        self.assertIn("if (editor) {", body)
        self.assertIn("editor.value = state.value;", body)
        self.assertIn("const preview = document.getElementById(`${state.type}-template-preview`);", body)
        self.assertIn("if (preview) {", body)
        self.assertIn("preview.textContent = state.preview;", body)
        self.assertLess(
            body.index("previousEditorState.forEach(state => {"),
            body.index("showToast(`加载通知模板失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "通知模板刷新失败时得先把用户原来的编辑内容和预览恢复回来，再报错，别把未保存内容整没了",
        )

    def test_notification_template_loader_and_mutation_failures_surface_structured_error_messages(self):
        load_body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        save_body = _extract_function_body(self.app_js, "saveNotificationTemplate")
        reset_body = _extract_function_body(self.app_js, "resetNotificationTemplate")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("showToast(`加载通知模板失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        load_error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        load_throw_index = load_body.index("throw new Error(errorMessage);")
        self.assertLess(
            load_error_index,
            load_throw_index,
            "通知模板加载 HTTP 失败时得先把 detail/message 解出来，别直接抛固定文案",
        )
        self.assertLess(
            load_body.find("requestSequence !== notificationTemplateRequestSequence", load_error_index),
            load_throw_index,
            "通知模板旧失败响应读完错误文本后，先验 request sequence，别回魂抛异常",
        )
        self.assertLess(
            load_body.find("!document.getElementById('message-notifications-section')?.classList.contains('active')", load_error_index),
            load_throw_index,
            "都切出消息通知页了，旧模板失败响应读完错误文本也别再往 catch 里丢异常",
        )

        for body, toast_fragment, label in (
            (save_body, "showToast(`保存模板失败: ${error.message || '请稍后重试'}`, 'danger');", "通知模板保存"),
            (reset_body, "showToast(`重置模板失败: ${error.message || '请稍后重试'}`, 'danger');", "通知模板重置"),
        ):
            with self.subTest(label=label):
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertIn("throw new Error(errorMessage);", body)
                self.assertIn(toast_fragment, body)
                error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                throw_index = body.index("throw new Error(errorMessage);")
                self.assertLess(
                    error_index,
                    throw_index,
                    f"{label}失败时得先把 detail/message 解出来，别固定甩个失败完事",
                )
                self.assertLess(
                    body.find("requestSequence !== notificationTemplateActionRequestSequence", error_index),
                    throw_index,
                    f"{label}旧失败响应读完错误文本后，先验当前 action sequence，别回魂抛异常",
                )
                self.assertLess(
                    body.find("!document.getElementById('message-notifications-section')?.classList.contains('active')", error_index),
                    throw_index,
                    f"都切出消息通知页了，旧的{label}失败响应读完错误文本也别再跨页甩异常",
                )

    def test_notification_template_loader_waits_for_default_backfill_before_reporting_success(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        self.assertIn("await Promise.all(supportedTemplateTypes.map(async (type) => {", body)
        self.assertIn("if (editor && !editor.value) {", body)
        self.assertIn("await loadDefaultTemplate(type, requestSequence);", body)
        self.assertNotIn("supportedTemplateTypes.forEach(async (type) => {", body)
        self.assertLess(
            body.index("await Promise.all(supportedTemplateTypes.map(async (type) => {"),
            body.index("showToast('通知模板加载成功', 'success');"),
            "默认模板回填应该完成后再提示加载成功",
        )

    def test_notification_template_loader_stops_before_default_backfill_fetches_when_request_turns_stale(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        backfill_call_index = body.index("await loadDefaultTemplate(type, requestSequence);")
        promise_map_index = body.index("await Promise.all(supportedTemplateTypes.map(async (type) => {")
        stale_guard_after_map = body.find("requestSequence !== notificationTemplateRequestSequence", promise_map_index)

        self.assertGreater(
            stale_guard_after_map,
            promise_map_index,
            "通知模板默认回填开始前，先确认当前请求还活着，别旧请求继续偷偷发默认模板接口",
        )
        self.assertLess(
            stale_guard_after_map,
            backfill_call_index,
            "都切页或来了新请求了，旧模板加载不该继续逐个打默认模板接口",
        )

    def test_notification_default_template_loader_rejects_malformed_success_payload_before_overwriting_editor(self):
        body = _extract_function_body(self.app_js, "loadDefaultTemplate")

        self.assertIn("if (", body)
        self.assertIn("typeof data !== 'object'", body)
        self.assertIn("Array.isArray(data)", body)
        self.assertIn("Object.prototype.hasOwnProperty.call(data, 'type')", body)
        self.assertIn("typeof data.template !== 'string'", body)
        self.assertIn("throw new Error('默认通知模板返回格式异常');", body)
        self.assertLess(
            body.index("throw new Error('默认通知模板返回格式异常');"),
            body.index("editor.value = data.template;"),
            "默认通知模板接口如果回了歪 payload，前端得先当格式异常拦住，别把 undefined 直接塞进编辑器当模板内容",
        )

    def test_notification_template_loader_does_not_report_full_success_when_default_backfill_fails(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")
        self.assertIn("const defaultBackfillResults = await Promise.all(supportedTemplateTypes.map(async (type) => {", body)
        self.assertIn("const hasDefaultBackfillFailure = defaultBackfillResults.some(result => result === false);", body)
        self.assertIn("if (hasDefaultBackfillFailure) {", body)
        self.assertIn("showToast('通知模板加载完成，但部分默认模板回填失败，请稍后重试', 'warning');", body)
        self.assertLess(
            body.index("const hasDefaultBackfillFailure = defaultBackfillResults.some(result => result === false);"),
            body.index("showToast('通知模板加载成功', 'success');"),
            "默认模板回填都失败了还报全成功，这就纯属糊弄人了",
        )

    def test_notification_template_loader_treats_null_default_backfill_results_as_abort_before_success_or_warning(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")

        self.assertIn("if (defaultBackfillResults.some(result => result === null)) {", body)
        self.assertLess(
            body.index("if (defaultBackfillResults.some(result => result === null)) {"),
            body.index("if (hasDefaultBackfillFailure) {"),
            "默认模板回填如果已经因为 401 helper 或 stale 中止了，这轮加载就别再往下装成功或部分失败，先整体当成中止收手",
        )

    def test_notification_template_loader_rejects_malformed_success_payload_before_resetting_editors(self):
        body = _extract_function_body(self.app_js, "loadNotificationTemplates")

        self.assertIn("if (!data || typeof data !== 'object' || Array.isArray(data)) {", body)
        self.assertIn("!Object.prototype.hasOwnProperty.call(data, 'templates') || !Array.isArray(data.templates)", body)
        self.assertIn("const templates = data.templates;", body)
        self.assertIn("templates.some(template =>", body)
        self.assertIn("!supportedTemplateTypes.includes(String(template.type || '').trim())", body)
        self.assertIn("typeof template.template !== 'string'", body)
        self.assertIn("(template.is_default != null && typeof template.is_default !== 'boolean')", body)
        self.assertIn("throw new Error('通知模板列表返回格式异常');", body)
        self.assertLess(
            body.index("throw new Error('通知模板列表返回格式异常');"),
            body.index("resetNotificationTemplateEditors(supportedTemplateTypes);"),
            "通知模板列表接口如果回了歪 payload，前端得先当格式异常拦住，别先把编辑器清空再拿脏模板往里灌",
        )

    def test_notification_template_actions_are_invalidated_when_leaving_section(self):
        self.assertIn("let notificationTemplateActionRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        save_body = _extract_function_body(self.app_js, "saveNotificationTemplate")
        reset_body = _extract_function_body(self.app_js, "resetNotificationTemplate")
        test_body = _extract_function_body(self.app_js, "testNotificationTemplate")

        self.assertIn("if (sectionName !== 'message-notifications') {", show_section_body)
        self.assertIn("notificationTemplateActionRequestSequence += 1;", show_section_body)

        for body, function_name in (
            (save_body, "saveNotificationTemplate"),
            (reset_body, "resetNotificationTemplate"),
            (test_body, "testNotificationTemplate"),
        ):
            with self.subTest(function_name=function_name):
                self.assertIn("const requestSequence = ++notificationTemplateActionRequestSequence;", body)
                self.assertIn("requestSequence !== notificationTemplateActionRequestSequence", body)
                self.assertIn("!document.getElementById('message-notifications-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)

    def test_notification_template_actions_do_not_update_hidden_section_or_emit_hidden_toasts(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationTemplate")
        reset_body = _extract_function_body(self.app_js, "resetNotificationTemplate")
        test_body = _extract_function_body(self.app_js, "testNotificationTemplate")

        self.assertLess(
            save_body.index("requestSequence !== notificationTemplateActionRequestSequence"),
            save_body.index("showToast('模板保存成功', 'success');"),
            "都切出消息通知页了，旧保存请求就别回来弹成功 toast 装完成了",
        )

        self.assertLess(
            reset_body.index("requestSequence !== notificationTemplateActionRequestSequence"),
            reset_body.index("editor.value = data.template.template;"),
            "旧的模板重置请求不该晚回来后把当前编辑器内容改回去",
        )
        self.assertLess(
            reset_body.rfind("requestSequence !== notificationTemplateActionRequestSequence", 0, reset_body.index("showToast('模板已恢复默认', 'success');")),
            reset_body.index("showToast('模板已恢复默认', 'success');"),
            "都切页了，旧模板重置请求别回来刷成功 toast",
        )

        self.assertLess(
            test_body.rfind("requestSequence !== notificationTemplateActionRequestSequence", 0, test_body.index("showToast(data.message || '测试通知发送成功', 'success');")),
            test_body.index("showToast(data.message || '测试通知发送成功', 'success');"),
            "都切页了，旧测试发送请求别回来刷成功 toast",
        )
        self.assertLess(
            test_body.rfind("requestSequence !== notificationTemplateActionRequestSequence", 0, test_body.index("showToast(errorMessage || '测试通知发送失败', 'danger');")),
            test_body.index("showToast(errorMessage || '测试通知发送失败', 'danger');"),
            "都切页了，旧测试发送失败请求也别回来跨页甩 danger toast",
        )

    def test_notification_template_reset_rejects_malformed_success_payload_before_overwriting_editor(self):
        body = _extract_function_body(self.app_js, "resetNotificationTemplate")

        self.assertIn("typeof data !== 'object'", body)
        self.assertIn("Array.isArray(data)", body)
        self.assertIn("typeof data.template !== 'object'", body)
        self.assertIn("Array.isArray(data.template)", body)
        self.assertIn("Object.prototype.hasOwnProperty.call(data.template, 'type')", body)
        self.assertIn("typeof data.template.template !== 'string'", body)
        self.assertIn("throw new Error('通知模板重置结果返回格式异常');", body)
        self.assertLess(
            body.index("throw new Error('通知模板重置结果返回格式异常');"),
            body.index("editor.value = data.template.template;"),
            "通知模板重置接口如果回了歪 payload，前端得先当格式异常拦住，别把 undefined 或脏对象直接塞进编辑器还弹成功 toast",
        )

    def test_notification_template_action_sequence_starts_only_after_validation_or_confirmation(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationTemplate")
        reset_body = _extract_function_body(self.app_js, "resetNotificationTemplate")
        test_body = _extract_function_body(self.app_js, "testNotificationTemplate")

        self.assertLess(
            save_body.index("if (!editor) {"),
            save_body.index("const requestSequence = ++notificationTemplateActionRequestSequence;"),
            "编辑器都不存在时只是前端校验，别先把通知模板 action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            save_body.index("if (!template.trim()) {"),
            save_body.index("const requestSequence = ++notificationTemplateActionRequestSequence;"),
            "模板内容为空时只是前端校验，别先把通知模板 action sequence 顶掉别的正常动作",
        )

        reset_confirm_return_index = reset_body.index("return;", reset_body.index("if (!confirm("))
        self.assertLess(
            reset_confirm_return_index,
            reset_body.index("const requestSequence = ++notificationTemplateActionRequestSequence;"),
            "用户都取消重置模板了，就别先把通知模板 action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            test_body.index("if (!editor) {"),
            test_body.index("const requestSequence = ++notificationTemplateActionRequestSequence;"),
            "编辑器都不存在时只是前端校验，别先把通知模板测试 action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            test_body.index("if (!template.trim()) {"),
            test_body.index("const requestSequence = ++notificationTemplateActionRequestSequence;"),
            "模板内容为空时只是前端校验，别先把通知模板测试 action sequence 顶掉别的正常动作",
        )

    def test_notification_template_save_request_sequence_stays_visible_to_catch_guards(self):
        body = _extract_function_body(self.app_js, "saveNotificationTemplate")
        self.assertLess(
            body.index("const requestSequence = ++notificationTemplateActionRequestSequence;"),
            body.index("try {"),
            "通知模板保存既然在 catch 里还要验 requestSequence，就别把它声明在 try 里面给自己埋作用域雷",
        )

    def test_notification_template_test_action_surfaces_partial_channel_failures_as_warning(self):
        body = _extract_function_body(self.app_js, "testNotificationTemplate")
        self.assertIn("if (data.failed_channels && data.failed_channels.length > 0) {", body)
        self.assertIn("showToast((data.message || '测试通知发送成功') + '，但部分渠道发送失败，请检查通知渠道配置', 'warning');", body)
        self.assertIn("} else {", body)
        self.assertIn("showToast(data.message || '测试通知发送成功', 'success');", body)

    def test_notification_template_test_action_rejects_malformed_success_payload_before_branching_on_failed_channels(self):
        body = _extract_function_body(self.app_js, "testNotificationTemplate")

        self.assertIn("if (!data || typeof data !== 'object' || Array.isArray(data)) {", body)
        self.assertIn("if (data.failed_channels != null && !Array.isArray(data.failed_channels)) {", body)
        self.assertIn("throw new Error('通知模板测试结果返回格式异常');", body)
        self.assertLess(
            body.index("if (data.failed_channels != null && !Array.isArray(data.failed_channels)) {"),
            body.index("if (data.failed_channels && data.failed_channels.length > 0) {"),
            "通知模板测试接口如果把 failed_channels 回歪了，前端得先当格式异常拦住，别拿对象/字符串去硬走部分失败 warning 分支自爆",
        )

    def test_notification_template_test_action_catch_failures_surface_runtime_error_messages(self):
        body = _extract_function_body(self.app_js, "testNotificationTemplate")

        self.assertNotIn("showToast('发送测试通知失败', 'danger');", body)
        self.assertIn("showToast(`发送测试通知失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        failure_toast_index = body.index("showToast(`发送测试通知失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            body.rfind("requestSequence !== notificationTemplateActionRequestSequence", 0, failure_toast_index),
            failure_toast_index,
            "测试通知 catch 兜底前得先验当前 action sequence，别让旧异常回魂把新会话喷一脸",
        )
        self.assertLess(
            body.rfind("!document.getElementById('message-notifications-section')?.classList.contains('active')", 0, failure_toast_index),
            failure_toast_index,
            "都切出消息通知页了，旧的测试通知异常就别跨页回来刷 danger toast 了",
        )

    def test_notification_channel_message_notification_and_template_fetches_redirect_on_401_before_followup_processing(self):
        for function_name, body, anchor_fragment in (
            ("saveNotificationChannel", _extract_function_body(self.app_js, "saveNotificationChannel"), "if (response.ok) {"),
            ("loadNotificationChannels", _extract_function_body(self.app_js, "loadNotificationChannels"), "if (!response.ok) {"),
            ("deleteNotificationChannel", _extract_function_body(self.app_js, "deleteNotificationChannel"), "if (response.ok) {"),
            ("editNotificationChannel", _extract_function_body(self.app_js, "editNotificationChannel"), "if (!response.ok) {"),
            ("updateNotificationChannel", _extract_function_body(self.app_js, "updateNotificationChannel"), "if (response.ok) {"),
            ("loadNotificationTemplates", _extract_function_body(self.app_js, "loadNotificationTemplates"), "if (!response.ok) {"),
            ("loadDefaultTemplate", _extract_function_body(self.app_js, "loadDefaultTemplate"), "if (response.ok) {"),
            ("saveNotificationTemplate", _extract_function_body(self.app_js, "saveNotificationTemplate"), "if (!response.ok) {"),
            ("resetNotificationTemplate", _extract_function_body(self.app_js, "resetNotificationTemplate"), "if (!response.ok) {"),
            ("testNotificationTemplate", _extract_function_body(self.app_js, "testNotificationTemplate"), "if (!response.ok) {"),
        ):
            with self.subTest(function_name=function_name):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{function_name} 遇到 401 时应先跳登录，别继续把未授权响应往后当正常流程折腾",
                )

        load_notifications_body = _extract_function_body(self.app_js, "loadMessageNotifications")
        config_notification_body = _extract_function_body(self.app_js, "configAccountNotification")
        save_notification_body = _extract_function_body(self.app_js, "saveAccountNotification")
        delete_notification_body = _extract_function_body(self.app_js, "deleteAccountNotification")

        self.assertIn("if (handleUnauthorizedApiResponse(accountsResponse)) {", load_notifications_body)
        self.assertLess(
            load_notifications_body.index("if (handleUnauthorizedApiResponse(accountsResponse)) {"),
            load_notifications_body.index("if (!accountsResponse.ok) {"),
            "消息通知加载在账号列表接口 401 时应先跳登录，别继续装成普通加载失败",
        )
        self.assertIn("if (handleUnauthorizedApiResponse(notificationsResponse)) {", load_notifications_body)
        self.assertLess(
            load_notifications_body.index("if (handleUnauthorizedApiResponse(notificationsResponse)) {"),
            load_notifications_body.index("if (!notificationsResponse.ok) {"),
            "消息通知加载在配置接口 401 时应先跳登录，别继续装成普通加载失败",
        )

        self.assertIn("if (handleUnauthorizedApiResponse(channelsResponse)) {", config_notification_body)
        self.assertLess(
            config_notification_body.index("if (handleUnauthorizedApiResponse(channelsResponse)) {"),
            config_notification_body.index("if (!channelsResponse.ok) {"),
            "账号通知配置在渠道列表接口 401 时应先跳登录，别继续开弹窗装没事",
        )
        self.assertIn("if (handleUnauthorizedApiResponse(notificationResponse)) {", config_notification_body)
        self.assertLess(
            config_notification_body.index("if (handleUnauthorizedApiResponse(notificationResponse)) {"),
            config_notification_body.index("if (!notificationResponse.ok) {"),
            "账号通知配置在账号详情接口 401 时应先跳登录，别继续把未授权响应当配置 JSON 解析",
        )

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", delete_notification_body)
        self.assertLess(
            delete_notification_body.index("if (handleUnauthorizedApiResponse(response)) {"),
            delete_notification_body.index("if (response.ok) {"),
            "删除通知配置遇到 401 时应先跳登录，别继续往后刷 toast",
        )

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", save_notification_body)
        self.assertLess(
            save_notification_body.index("if (handleUnauthorizedApiResponse(response)) {"),
            save_notification_body.index("if (!response.ok) {"),
            "保存通知配置原子替换请求遇到 401 时应先跳登录，别继续把未授权响应当业务失败提示",
        )

    def test_notification_channel_message_notification_and_template_failures_parse_error_payloads_with_helper(self):
        save_channel_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_channel_body = _extract_function_body(self.app_js, "updateNotificationChannel")
        delete_channel_body = _extract_function_body(self.app_js, "deleteNotificationChannel")
        save_notification_body = _extract_function_body(self.app_js, "saveAccountNotification")
        delete_notification_body = _extract_function_body(self.app_js, "deleteAccountNotification")
        test_template_body = _extract_function_body(self.app_js, "testNotificationTemplate")

        for body, failure_fragment, label in (
            (save_channel_body, "showToast(`添加失败: ${error}`, 'danger');", "通知渠道新增"),
            (update_channel_body, "showToast(`更新失败: ${error}`, 'danger');", "通知渠道更新"),
            (delete_channel_body, "showToast(`删除失败: ${error}`, 'danger');", "通知渠道删除"),
            (delete_notification_body, "showToast(`删除失败: ${error}`, 'danger');", "通知配置删除"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(failure_fragment),
                    f"{label}失败时得先把 detail/message 解出来，别把 JSON 原文直接糊用户脸上",
                )

        self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", save_notification_body)
        self.assertLess(
            save_notification_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            save_notification_body.index("showToast(`保存失败: ${error}`, 'danger');"),
            "通知配置原子替换失败时得先把 detail/message 解出来，别直接甩 JSON 原文",
        )

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", test_template_body)
        self.assertLess(
            test_template_body.index("if (!response.ok) {"),
            test_template_body.index("const data = await response.json();"),
            "测试通知失败时得先处理非成功响应，别上来就硬读 JSON 把自己炸了",
        )
        self.assertLess(
            test_template_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            test_template_body.index("showToast(errorMessage || '测试通知发送失败', 'danger');"),
            "测试通知失败时也得先把 detail/message 解出来，再决定怎么提示用户",
        )

    def test_notification_template_test_endpoint_does_not_log_raw_channel_secrets(self):
        self.assertNotIn('logger.info(f"获取到的通知渠道: {channels}")', self.reply_server)
        self.assertNotIn('logger.info(f"已启用的通知渠道: {enabled_channels}")', self.reply_server)
        self.assertNotIn('logger.info(f"处理通知渠道: name={channel_name}, type={channel_type}, config={config_str}")', self.reply_server)
        self.assertNotIn('logger.info(f"解析后的配置: {config_data}")', self.reply_server)
        self.assertNotIn('logger.info(f"发送飞书通知: {payload}")', self.reply_server)
        self.assertIn('def summarize_notification_channel_for_log(channel: Dict[str, Any]) -> Dict[str, Any]:', self.reply_server)
        self.assertIn('logger.info(f"通知模板测试使用渠道: {channel_summary}")', self.reply_server)

    def test_auto_reply_keyword_rendering_escapes_reply_keywords_items_and_image_urls(self):
        render_body = _extract_function_body(self.app_js, "renderKeywordsList")
        edit_group_body = _extract_function_body(self.app_js, "editGroupReply")

        required_render_fragments = [
            "const safeImageUrl = escapeHtml(imageUrl);",
            "const safeImageUrlForJs = escapeInlineJsSingleQuotedString(imageUrl);",
            "const safeReplyHtml = escapeHtml(group.reply || '');",
            "const safeKeyword = escapeHtml(kwInfo.keyword);",
            "const safeGroupIdForJs = escapeInlineJsSingleQuotedString(group.id);",
            "const safeDisplayText = escapeHtml(displayText);",
        ]
        for fragment in required_render_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, render_body)

        self.assertNotIn("${group.reply || '<span class=\"text-muted\">（空回复，不自动回复）</span>'}", render_body)
        self.assertNotIn("${kw}", render_body)
        self.assertNotIn("${displayText}", render_body)
        self.assertIn("const safeReplyText = escapeHtml(replyText);", edit_group_body)
        self.assertNotIn(">${replyText}</textarea>", edit_group_body)

    def test_auto_reply_keyword_rendering_restores_grouped_text_keyword_edit_entry(self):
        render_body = _extract_function_body(self.app_js, "renderKeywordsList")

        self.assertIn("const keywordEntries = Array.isArray(group.keywordEntries) && group.keywordEntries.length > 0", render_body)
        self.assertIn("const editKeywordButton = !isImageType ? `", render_body)
        self.assertIn("editSpecificKeyword('${safeGroupIdForJs}', ${kwIndex})", render_body)
        self.assertIn("${editKeywordButton}", render_body)
        self.assertIn("title=\"编辑此关键词配置\"", render_body)
        self.assertNotIn("onclick=\"editKeyword(", render_body)

    def test_auto_reply_keyword_grouping_tracks_keyword_entries_and_source_indices(self):
        body = _extract_function_body(self.app_js, "groupKeywordsByReply")

        self.assertIn("keywordEntries: [],", body)
        self.assertIn("group.keywordEntries.push({", body)
        self.assertIn("indices: [index]", body)
        self.assertIn("group.keywordEntries[existingKeywordIndex].indices.push(index);", body)

    def test_auto_reply_grouped_keyword_edit_prefills_all_related_configurations(self):
        body = _extract_function_body(self.app_js, "editSpecificKeyword")

        self.assertIn("const keywordEntries = Array.isArray(group.keywordEntries) ? group.keywordEntries : [];", body)
        self.assertIn("let editingIndices = Array.isArray(keywordInfo?.indices) ? [...keywordInfo.indices] : [];", body)
        self.assertIn("document.getElementById('newKeyword').value = targetKeyword;", body)
        self.assertIn("document.getElementById('newReply').value = group.reply || '';", body)
        self.assertIn("const hasGeneralItemScope = editingIndices.some(index => (keywords[index]?.item_id || '') === '');", body)
        self.assertIn("const selectedItemIds = Array.from(new Set(", body)
        self.assertIn("opt.selected = opt.value === '' ? hasGeneralItemScope : selectedItemIds.includes(opt.value);", body)
        self.assertIn("window.editingKeywordIndices = editingIndices;", body)
        self.assertIn("setPrimaryKeywordAddButtonEditing(true);", body)
        self.assertIn("showCancelEditButton();", body)

    def test_auto_reply_keyword_add_edit_mode_removes_all_old_indices_and_keeps_general_scope_selection(self):
        body = _extract_function_body(self.app_js, "addKeyword")

        self.assertIn("const includesGeneralItemScope = selectedOptions.some(opt => opt.value === '');", body)
        self.assertIn("let itemIds = Array.from(new Set(", body)
        self.assertIn("} else if (includesGeneralItemScope) {", body)
        self.assertIn("itemIds.unshift('');", body)
        self.assertIn("const effectiveEditingIndices = Array.from(new Set(", body)
        self.assertIn("currentKeywords = currentKeywords.filter((item, index) => !effectiveEditingIndices.includes(index));", body)
        self.assertIn("allKeywords = allKeywords.filter((item, index) => !effectiveEditingIndices.includes(index));", body)
        self.assertNotIn("currentKeywords.splice(window.editingIndex, 1);", body)

    def test_auto_reply_keyword_group_header_uses_actual_configuration_count_instead_of_cross_product(self):
        render_body = _extract_function_body(self.app_js, "renderKeywordsList")

        self.assertIn("${group.indices.length}条配置", render_body)
        self.assertNotIn("${group.keywords.length * group.items.length}条配置", render_body)

    def test_image_preview_modal_escapes_image_url_before_inserting_html(self):
        body = _extract_function_body(self.app_js, "showImageModal")
        self.assertIn("const safeImageUrl = escapeHtml(imageUrl || '');", body)
        self.assertNotIn('<img src="${imageUrl}"', body)

    def test_image_keyword_item_loader_uses_account_scoped_items_endpoint(self):
        body = _extract_function_body(self.app_js, "loadItemsListForImageKeyword")
        self.assertIn("fetch(`${apiBase}/items/account/${encodeURIComponent(currentAccountId)}`", body)
        self.assertNotIn("fetch(`${apiBase}/items/${currentAccountId}`", body)

    def test_image_keyword_item_loader_ignores_stale_account_switches_and_hidden_auto_reply_section(self):
        self.assertIn("let imageKeywordItemsRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "loadItemsListForImageKeyword")

        self.assertIn("imageKeywordItemsRequestSequence += 1;", show_section_body)
        self.assertIn("const requestedAccountId = currentAccountId;", body)
        self.assertIn("const requestSequence = ++imageKeywordItemsRequestSequence;", body)
        self.assertIn("requestSequence !== imageKeywordItemsRequestSequence", body)
        self.assertIn("currentAccountId !== requestedAccountId", body)
        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        success_reset_index = body.rfind("selectElement.innerHTML = '<option value=\"\">选择商品或留空表示通用关键词</option>';")
        self.assertLess(
            body.index("requestSequence !== imageKeywordItemsRequestSequence"),
            success_reset_index,
            "图片关键词商品下拉的旧请求不该晚回来后把当前账号的商品选项糊回去",
        )

    def test_image_keyword_item_loader_resets_stale_options_before_fetch_and_on_failure(self):
        body = _extract_function_body(self.app_js, "loadItemsListForImageKeyword")

        self.assertIn("const selectElement = document.getElementById('imageItemIdSelect');", body)
        self.assertIn("selectElement.innerHTML = '<option value=\"\">选择商品或留空表示通用关键词</option>';",
                      body)
        self.assertLess(
            body.index("selectElement.innerHTML = '<option value=\"\">选择商品或留空表示通用关键词</option>';"),
            body.index("const response = await fetch(`${apiBase}/items/account/${encodeURIComponent(currentAccountId)}`, {"),
            "图片关键词商品下拉重新加载前应先清空旧选项，失败时别挂着上次账号的商品装正常",
        )
        self.assertIn("selectElement.innerHTML = '<option value=\"\">商品列表加载失败，请稍后重试</option>';",
                      body)
        self.assertIn("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');", body)
        self.assertLess(
            body.rfind("!document.getElementById('auto-reply-section')?.classList.contains('active')", 0, body.index("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');")),
            body.index("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');"),
            "图片关键词商品下拉旧失败回调都已经切页了，就别跨页甩红字恶心人",
        )

    def test_image_keyword_item_loader_skips_fetch_when_no_account_selected(self):
        body = _extract_function_body(self.app_js, "loadItemsListForImageKeyword")

        self.assertIn("if (!requestedAccountId) {", body)
        self.assertIn("selectElement.innerHTML = '<option value=\"\">请先选择账号</option>';", body)
        self.assertIn("return false;", body)
        self.assertLess(
            body.index("if (!requestedAccountId) {"),
            body.index("const response = await fetch(`${apiBase}/items/account/${encodeURIComponent(currentAccountId)}`, {"),
            "没选账号就别去打空商品请求，前端别自己制造失败弹窗",
        )

    def test_image_keyword_modal_invalidates_item_requests_when_hidden(self):
        body = _extract_function_body(self.app_js, "showAddImageKeywordModal")
        self.assertIn("const modalElement = document.getElementById('addImageKeywordModal');", body)
        self.assertIn("if (modalElement && modalElement.dataset.imageKeywordModalBound !== 'true') {", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("imageKeywordItemsRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.imageKeywordModalBound = 'true';", body)
        self.assertLess(
            body.index("modalElement.addEventListener('hidden.bs.modal', () => {"),
            body.index("modal.show();"),
            "图片关键词弹窗关闭后也得废掉商品下拉请求，别让旧请求回来污染隐藏 modal",
        )

    def test_image_keyword_validation_callbacks_ignore_stale_file_selection_and_hidden_section(self):
        self.assertIn("let imageKeywordValidationRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        modal_body = _extract_function_body(self.app_js, "showAddImageKeywordModal")
        init_body = _extract_function_body(self.app_js, "initImageKeywordEventListeners")
        validate_body = _extract_function_body(self.app_js, "validateImageDimensions")
        preview_body = _extract_function_body(self.app_js, "showImagePreview")

        self.assertIn("imageKeywordValidationRequestSequence += 1;", show_section_body)
        self.assertIn("imageKeywordValidationRequestSequence += 1;", modal_body)
        self.assertIn("const requestSequence = ++imageKeywordValidationRequestSequence;", init_body)
        self.assertIn("validateImageDimensions(file, e.target, requestSequence);", init_body)

        self.assertIn("function validateImageDimensions(file, inputElement, requestSequence = 0) {", self.app_js)
        self.assertIn("requestSequence !== imageKeywordValidationRequestSequence", validate_body)
        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", validate_body)
        self.assertIn("showImagePreview(file, requestSequence);", validate_body)
        self.assertLess(
            validate_body.index("requestSequence !== imageKeywordValidationRequestSequence"),
            validate_body.index("showImagePreview(file, requestSequence);"),
            "旧的图片尺寸校验回调不该晚回来后继续更新当前预览",
        )
        self.assertLess(
            validate_body.rfind("!document.getElementById('auto-reply-section')?.classList.contains('active')", 0, validate_body.index("showToast('❌ 无法读取图片文件，请选择有效的图片', 'warning');")),
            validate_body.index("showToast('❌ 无法读取图片文件，请选择有效的图片', 'warning');"),
            "都切出自动回复页了，旧图片校验失败回调也别再跨页甩 warning toast",
        )

        self.assertIn("function showImagePreview(file, requestSequence = 0) {", self.app_js)
        self.assertIn("requestSequence !== imageKeywordValidationRequestSequence", preview_body)
        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", preview_body)

    def test_comment_template_inline_edit_actions_escape_js_arguments(self):
        body = _extract_function_body(self.app_js, "showCommentTemplates")
        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", body)
        self.assertIn("const safeAccountIdForJs = escapeInlineJsSingleQuotedString(accountId);", body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates`", body)
        self.assertIn("const safeTemplateNameForJs = escapeInlineJsSingleQuotedString(template.name);", body)
        self.assertIn("const safeTemplateContentForJs = escapeInlineJsSingleQuotedString(template.content);", body)
        self.assertIn("const safeTemplateNameDisplay = escapeHtml(template.name);", body)
        self.assertIn("const safeTemplateContentDisplay = escapeHtml(template.content);", body)
        self.assertIn("onclick=\"activateCommentTemplate('${safeAccountIdForJs}', ${template.id})\"", body)
        self.assertIn("onclick=\"deleteCommentTemplate('${safeAccountIdForJs}', ${template.id})\"", body)
        self.assertNotIn("editCommentTemplate(${template.id}, '${escapeHtml(template.name)}', '${escapeHtml(template.content)}')", body)
        self.assertNotIn("fetch(`${apiBase}/accounts/${accountId}/comment-templates`", body)

    def test_comment_template_mutations_encode_account_ids_and_only_report_success_after_reload(self):
        add_body = _extract_function_body(self.app_js, "addCommentTemplate")
        edit_body = _extract_function_body(self.app_js, "saveEditCommentTemplate")
        delete_body = _extract_function_body(self.app_js, "deleteCommentTemplate")
        activate_body = _extract_function_body(self.app_js, "activateCommentTemplate")
        show_body = _extract_function_body(self.app_js, "showCommentTemplates")

        self.assertIn("return true;", show_body)
        self.assertIn("return false;", show_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(currentCommentTemplateAccountId);", add_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates`", add_body)
        self.assertIn("const templatesLoaded = await showCommentTemplates(currentCommentTemplateAccountId);", add_body)
        self.assertIn("if (templatesLoaded === true) {", add_body)
        self.assertIn("} else if (templatesLoaded === false) {", add_body)
        self.assertIn("showToast('添加好评模板成功', 'success');", add_body)
        self.assertIn("showToast('添加好评模板成功，但模板列表刷新失败，请稍后手动刷新', 'warning');", add_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(currentCommentTemplateAccountId);", edit_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates/${templateId}`", edit_body)
        self.assertIn("const templatesLoaded = await showCommentTemplates(currentCommentTemplateAccountId);", edit_body)
        self.assertIn("if (templatesLoaded === true) {", edit_body)
        self.assertIn("} else if (templatesLoaded === false) {", edit_body)
        self.assertIn("showToast('更新好评模板成功', 'success');", edit_body)
        self.assertIn("showToast('更新好评模板成功，但模板列表刷新失败，请稍后手动刷新', 'warning');", edit_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", delete_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates/${templateId}`", delete_body)
        self.assertIn("const templatesLoaded = await showCommentTemplates(accountId);", delete_body)
        self.assertIn("if (templatesLoaded === true) {", delete_body)
        self.assertIn("} else if (templatesLoaded === false) {", delete_body)
        self.assertIn("showToast('删除好评模板成功', 'success');", delete_body)
        self.assertIn("showToast('删除好评模板成功，但模板列表刷新失败，请稍后手动刷新', 'warning');", delete_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", activate_body)
        self.assertIn("fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates/${templateId}/activate`", activate_body)
        self.assertIn("const templatesLoaded = await showCommentTemplates(accountId);", activate_body)
        self.assertIn("if (templatesLoaded === true) {", activate_body)
        self.assertIn("} else if (templatesLoaded === false) {", activate_body)
        self.assertIn("showToast('已切换使用此模板', 'success');", activate_body)
        self.assertIn("showToast('已切换使用此模板，但模板列表刷新失败，请稍后手动刷新', 'warning');", activate_body)

    def test_comment_templates_modal_ignores_stale_async_responses_and_hidden_modal_state(self):
        self.assertIn("let commentTemplatesRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "showCommentTemplates")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("commentTemplatesRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++commentTemplatesRequestSequence;", body)
        self.assertIn("requestSequence !== commentTemplatesRequestSequence", body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("commentTemplatesRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.commentTemplatesModalBound = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== commentTemplatesRequestSequence"),
            body.index("const templatesList = existingModalEl.querySelector('#templatesList');"),
            "旧的好评模板请求不该晚回来后把当前弹窗模板列表改成别的账号",
        )

    def test_comment_templates_existing_modal_reopens_and_updates_title_for_new_account(self):
        body = _extract_function_body(self.app_js, "showCommentTemplates")

        self.assertIn("const modalTitle = existingModalEl.querySelector('#commentTemplatesModalLabel');", body)
        self.assertIn("modalTitle.innerHTML = `<i class=\"bi bi-star-fill text-warning me-2\"></i>好评模板管理 - ${safeAccountId}`;", body)
        self.assertIn("const existingModal = bootstrap.Modal.getInstance(existingModalEl) || new bootstrap.Modal(existingModalEl);", body)
        self.assertIn("existingModal.show();", body)
        self.assertLess(
            body.index("const modalTitle = existingModalEl.querySelector('#commentTemplatesModalLabel');"),
            body.index("existingModal.show();"),
            "好评模板弹窗复用旧 DOM 时，先把标题切到当前账号，再重新弹出来，别挂着旧账号名糊弄人",
        )

    def test_comment_templates_loader_finally_does_not_clear_newer_loading_state(self):
        body = _extract_function_body(self.app_js, "showCommentTemplates")
        self.assertIn("} finally {", body)
        finally_block = body.split("} finally {", 1)[1]
        self.assertIn("requestSequence !== commentTemplatesRequestSequence", finally_block)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", finally_block)
        self.assertLess(
            finally_block.index("requestSequence !== commentTemplatesRequestSequence"),
            finally_block.index("toggleLoading(false);"),
            "同页已经切到更新的好评模板加载会话后，旧 finally 不该把当前 loading 先给掐灭",
        )
        self.assertIn("toggleLoading(false);", finally_block)

    def test_comment_template_mutation_wrappers_ignore_stale_account_switches_and_cross_page_toasts(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        add_body = _extract_function_body(self.app_js, "addCommentTemplate")
        edit_body = _extract_function_body(self.app_js, "saveEditCommentTemplate")
        delete_body = _extract_function_body(self.app_js, "deleteCommentTemplate")
        activate_body = _extract_function_body(self.app_js, "activateCommentTemplate")

        self.assertIn("let commentTemplateActionRequestSequence = 0;", self.app_js)
        self.assertIn("commentTemplateActionRequestSequence += 1;", show_section_body)

        for body, requested_fragment, refresh_fragment, success_fragment in (
            (
                add_body,
                "const requestedAccountId = currentCommentTemplateAccountId;",
                "const templatesLoaded = await showCommentTemplates(currentCommentTemplateAccountId);",
                "showToast('添加好评模板成功', 'success');",
            ),
            (
                edit_body,
                "const requestedAccountId = currentCommentTemplateAccountId;",
                "const templatesLoaded = await showCommentTemplates(currentCommentTemplateAccountId);",
                "showToast('更新好评模板成功', 'success');",
            ),
            (
                delete_body,
                "const requestedAccountId = accountId;",
                "const templatesLoaded = await showCommentTemplates(accountId);",
                "showToast('删除好评模板成功', 'success');",
            ),
            (
                activate_body,
                "const requestedAccountId = accountId;",
                "const templatesLoaded = await showCommentTemplates(accountId);",
                "showToast('已切换使用此模板', 'success');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn(requested_fragment, body)
                self.assertIn("const actionRequestSequence = ++commentTemplateActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== commentTemplateActionRequestSequence", body)
                self.assertIn("requestedAccountId !== currentCommentTemplateAccountId", body)
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== commentTemplateActionRequestSequence"),
                    body.index(refresh_fragment),
                    "同页已经切到别的账号模板会话了，旧模板 mutation 不该再回来刷新当前模板列表",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出账号页了，旧的好评模板 mutation 不该再跨页弹 success toast",
                )

    def test_comment_template_loader_and_mutations_handle_unauthorized_before_followup_work(self):
        show_body = _extract_function_body(self.app_js, "showCommentTemplates")
        add_body = _extract_function_body(self.app_js, "addCommentTemplate")
        edit_body = _extract_function_body(self.app_js, "saveEditCommentTemplate")
        delete_body = _extract_function_body(self.app_js, "deleteCommentTemplate")
        activate_body = _extract_function_body(self.app_js, "activateCommentTemplate")

        for body, anchor_fragment, label in (
            (show_body, "if (!response.ok) {", "好评模板加载"),
            (add_body, "if (response.ok) {", "新增好评模板"),
            (edit_body, "if (response.ok) {", "编辑好评模板"),
            (delete_body, "if (response.ok) {", "删除好评模板"),
            (activate_body, "if (response.ok) {", "激活好评模板"),
        ):
            with self.subTest(label=label):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{label}这条 raw fetch 遇到 401 得先去登录，别后面还继续解析业务响应、弹 toast、刷新模板列表",
                )

    def test_comment_template_loader_and_mutation_failures_read_structured_error_messages(self):
        show_body = _extract_function_body(self.app_js, "showCommentTemplates")
        add_body = _extract_function_body(self.app_js, "addCommentTemplate")
        edit_body = _extract_function_body(self.app_js, "saveEditCommentTemplate")
        delete_body = _extract_function_body(self.app_js, "deleteCommentTemplate")
        activate_body = _extract_function_body(self.app_js, "activateCommentTemplate")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", show_body)
        self.assertIn("throw new Error(errorMessage);", show_body)
        self.assertNotIn("throw new Error('获取好评模板列表失败');", show_body)
        self.assertLess(
            show_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            show_body.index("throw new Error(errorMessage);"),
            "好评模板加载失败时先把 detail/message 解出来，再往 catch 里抛，别拿固定文案把后端错误全吃没",
        )

        for body, toast_fragment, label in (
            (add_body, "showToast(error || '添加好评模板失败', 'error');", "新增好评模板"),
            (edit_body, "showToast(error || '更新好评模板失败', 'error');", "编辑好评模板"),
            (delete_body, "showToast(error || '删除好评模板失败', 'error');", "删除好评模板"),
            (activate_body, "showToast(error || '切换模板失败', 'error');", "激活好评模板"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再拿裸 json.detail 硬拼提示",
                )
                self.assertNotIn("const error = await response.json();", body)

    def test_comment_template_loader_and_mutation_failures_recheck_state_after_error_body_read(self):
        show_body = _extract_function_body(self.app_js, "showCommentTemplates")
        add_body = _extract_function_body(self.app_js, "addCommentTemplate")
        edit_body = _extract_function_body(self.app_js, "saveEditCommentTemplate")
        delete_body = _extract_function_body(self.app_js, "deleteCommentTemplate")
        activate_body = _extract_function_body(self.app_js, "activateCommentTemplate")

        show_error_index = show_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        show_throw_index = show_body.index("throw new Error(errorMessage);", show_error_index)
        self.assertLess(
            show_body.find("requestSequence !== commentTemplatesRequestSequence", show_error_index),
            show_throw_index,
            "好评模板加载读完错误体后还得先验 requestSequence，别旧请求晚回来后继续往 catch 里扔锅",
        )
        self.assertLess(
            show_body.find("!document.getElementById('accounts-section')?.classList.contains('active')", show_error_index),
            show_throw_index,
            "都切出账号页了，旧的好评模板加载失败结果别再继续往 catch 里送",
        )

        for body, toast_fragment, label in (
            (add_body, "showToast(error || '添加好评模板失败', 'error');", "新增好评模板"),
            (edit_body, "showToast(error || '更新好评模板失败', 'error');", "编辑好评模板"),
            (delete_body, "showToast(error || '删除好评模板失败', 'error');", "删除好评模板"),
            (activate_body, "showToast(error || '切换模板失败', 'error');", "激活好评模板"),
        ):
            with self.subTest(label=label):
                error_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                toast_index = body.index(toast_fragment, error_index)
                self.assertLess(
                    body.find("requestedAccountId !== currentCommentTemplateAccountId", error_index),
                    toast_index,
                    f"{label}读完错误体后也得先验账号上下文，别旧账号模板请求回来糊当前弹窗一脸",
                )
                self.assertLess(
                    body.find("actionRequestSequence !== commentTemplateActionRequestSequence", error_index),
                    toast_index,
                    f"{label}读完错误体后也得先验 stale，别旧错误回来乱弹 toast",
                )
                self.assertLess(
                    body.find("!document.getElementById('accounts-section')?.classList.contains('active')", error_index),
                    toast_index,
                    f"都切出账号页了，旧的{label}失败结果别再跨页甩 danger toast",
                )

    def test_comment_template_catch_failures_surface_runtime_error_messages(self):
        for body, legacy_toast, runtime_toast, guard_fragments, label in (
            (
                _extract_function_body(self.app_js, "showCommentTemplates"),
                "showToast('获取好评模板失败: ' + error.message, 'error');",
                "showToast(`获取好评模板失败: ${error.message || '请稍后重试'}`, 'error');",
                (
                    "requestSequence !== commentTemplatesRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "加载好评模板",
            ),
            (
                _extract_function_body(self.app_js, "addCommentTemplate"),
                "showToast('网络错误，请稍后重试', 'error');",
                "showToast(`添加好评模板失败: ${error.message || '请稍后重试'}`, 'error');",
                (
                    "requestedAccountId !== currentCommentTemplateAccountId",
                    "actionRequestSequence !== commentTemplateActionRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "新增好评模板",
            ),
            (
                _extract_function_body(self.app_js, "saveEditCommentTemplate"),
                "showToast('网络错误，请稍后重试', 'error');",
                "showToast(`更新好评模板失败: ${error.message || '请稍后重试'}`, 'error');",
                (
                    "requestedAccountId !== currentCommentTemplateAccountId",
                    "actionRequestSequence !== commentTemplateActionRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "编辑好评模板",
            ),
            (
                _extract_function_body(self.app_js, "deleteCommentTemplate"),
                "showToast('网络错误，请稍后重试', 'error');",
                "showToast(`删除好评模板失败: ${error.message || '请稍后重试'}`, 'error');",
                (
                    "requestedAccountId !== currentCommentTemplateAccountId",
                    "actionRequestSequence !== commentTemplateActionRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "删除好评模板",
            ),
            (
                _extract_function_body(self.app_js, "activateCommentTemplate"),
                "showToast('网络错误，请稍后重试', 'error');",
                "showToast(`切换模板失败: ${error.message || '请稍后重试'}`, 'error');",
                (
                    "requestedAccountId !== currentCommentTemplateAccountId",
                    "actionRequestSequence !== commentTemplateActionRequestSequence",
                    "!document.getElementById('accounts-section')?.classList.contains('active')",
                ),
                "激活好评模板",
            ),
        ):
            with self.subTest(label=label):
                self.assertNotIn(legacy_toast, body)
                self.assertIn(runtime_toast, body)
                toast_index = body.index(runtime_toast)
                for guard_fragment in guard_fragments:
                    self.assertIn(guard_fragment, body)
                    self.assertLess(
                        body.rfind(guard_fragment, 0, toast_index),
                        toast_index,
                        f"{label} catch 里的旧异常在弹 toast 前也得先过上下文活性校验，别 stale 了还回来犯病",
                    )

    def test_comment_template_action_sequence_starts_only_after_validation(self):
        add_body = _extract_function_body(self.app_js, "addCommentTemplate")
        edit_body = _extract_function_body(self.app_js, "saveEditCommentTemplate")

        for body in (add_body, edit_body):
            with self.subTest(body=body[:80]):
                self.assertLess(
                    body.index("if (!name) {"),
                    body.index("const actionRequestSequence = ++commentTemplateActionRequestSequence;"),
                    "模板名称为空时只是前端校验，别先把好评模板 action sequence 顶掉别的正常动作",
                )
                self.assertLess(
                    body.index("if (!content) {"),
                    body.index("const actionRequestSequence = ++commentTemplateActionRequestSequence;"),
                    "模板内容为空时只是前端校验，别先把好评模板 action sequence 顶掉别的正常动作",
                )

    def test_comment_template_mutations_release_loading_state_in_finally(self):
        for function_name in (
            "addCommentTemplate",
            "saveEditCommentTemplate",
            "deleteCommentTemplate",
            "activateCommentTemplate",
        ):
            body = _extract_function_body(self.app_js, function_name)
            with self.subTest(function_name=function_name):
                self.assertIn("} finally {", body)
                finally_block = body.split("} finally {", 1)[1]
                self.assertIn("requestedAccountId !== currentCommentTemplateAccountId", finally_block)
                self.assertIn("actionRequestSequence !== commentTemplateActionRequestSequence", finally_block)
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", finally_block)
                self.assertLess(
                    finally_block.index("actionRequestSequence !== commentTemplateActionRequestSequence"),
                    finally_block.index("toggleLoading(false);"),
                    "同页已经切到更新的模板 mutation 会话后，旧 finally 不该把当前 loading 先给掐灭",
                )
                self.assertIn("toggleLoading(false);", finally_block)

    def test_qr_login_feedback_views_escape_runtime_supplied_message_and_urls(self):
        self.assertIn("function escapeHtmlAttribute(value) {", self.app_js)

        error_body = _extract_function_body(self.app_js, "showQRCodeError")
        self.assertIn("const safeMessage = escapeHtml(message || '未知错误');", error_body)
        self.assertNotIn("<p>${message}</p>", error_body)

        verification_body = _extract_function_body(self.app_js, "showVerificationRequired")
        self.assertIn("const safeVerificationUrl = escapeHtmlAttribute(verificationUrl);", verification_body)
        self.assertIn("const safeScreenshotPath = escapeHtmlAttribute(`${normalizeStaticAssetPath(screenshotPath)}?t=${Date.now()}`);", verification_body)
        self.assertNotIn('<img src="${normalizeStaticAssetPath(screenshotPath)}?t=${Date.now()}"', verification_body)
        self.assertNotIn('<a href="${verificationUrl}"', verification_body)

    def test_face_verification_modal_escapes_runtime_supplied_account_and_screenshot_fields(self):
        body = _extract_function_body(self.app_js, "showAccountFaceVerificationModal")
        self.assertIn("const safeAccountId = escapeHtml(accountId || '');", body)
        self.assertIn("const safeScreenshotPath = escapeHtmlAttribute(`${normalizeStaticAssetPath(screenshot.path || '')}?t=${new Date().getTime()}`);", body)
        self.assertIn("const safeCreatedTime = escapeHtml(screenshot.created_time_str || '-');", body)
        self.assertIn("bindPasswordLoginQRModalEvents(modal);", body)
        self.assertIn("passwordLoginQRModalState.mode = 'preview';", body)
        self.assertIn("modalTitle.innerHTML = `<i class=\"bi bi-shield-exclamation text-warning me-2\"></i>账号验证 - 账号 ${safeAccountId}`;", body)
        self.assertIn("statusText.innerHTML = `请根据下方验证截图在手机闲鱼APP中完成验证<br><small class=\"text-muted\">创建时间: ${safeCreatedTime}</small>`;", body)
        self.assertNotIn("账号验证 - 账号 ${accountId}", body)
        self.assertNotIn("${screenshot.path}?t=${new Date().getTime()}", body)
        self.assertNotIn("${screenshot.created_time_str}", body)

    def test_password_login_qr_modal_switches_back_to_session_mode_for_real_verification_flows(self):
        body = _extract_function_body(self.app_js, "showPasswordLoginQRCode")
        self.assertIn("passwordLoginQRModalState.mode = 'session';", body)

    def test_verification_link_renderers_only_accept_normalized_http_urls(self):
        self.assertIn("function normalizeSafeHttpUrl(url) {", self.app_js)

        qr_body = _extract_function_body(self.app_js, "showVerificationRequired")
        self.assertIn("const verificationUrl = normalizeSafeHttpUrl(data.verification_url || '');", qr_body)
        self.assertIn("const safeVerificationUrl = escapeHtmlAttribute(verificationUrl);", qr_body)
        self.assertNotIn("const verificationUrl = data.verification_url || '';", qr_body)

    def test_qr_login_status_and_success_handlers_do_not_emit_cross_page_toasts_after_leaving_accounts(self):
        status_body = _extract_function_body(self.app_js, "checkQRCodeStatus")
        success_body = _extract_function_body(self.app_js, "handleQRCodeSuccess")
        close_body = _extract_function_body(self.app_js, "closeQRCodeLoginModal")

        for body, toast_fragment in (
            (status_body, "showToast(data.message || '扫码登录失败', 'danger');"),
            (success_body, "showToast(successMessage, 'warning');"),
            (success_body, "showToast(successMessage, 'success');"),
            (success_body, "showToast(data.message || '扫码登录已完成，账号信息已同步', 'success');"),
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    "都切出账号页了，旧的扫码登录结果就别再跨页弹 toast 了",
                )

        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", close_body)
        self.assertLess(
            close_body.index("!document.getElementById('accounts-section')?.classList.contains('active')"),
            close_body.index("loadAccounts();"),
            "都切出账号页了，扫码登录关闭回调就别再悄悄刷新隐藏的账号列表了",
        )
        self.assertIn("const activeSessionId = qrCodeVerificationState.activeSessionId;", close_body)
        self.assertIn("if (activeSessionId !== qrCodeVerificationState.activeSessionId) {", close_body)
        self.assertLess(
            close_body.index("if (activeSessionId !== qrCodeVerificationState.activeSessionId) {"),
            close_body.index("modal.hide();"),
            "旧的扫码登录延迟关闭回调不该在新会话已经启动后还回来把当前二维码弹窗关掉",
        )

        password_body = _extract_function_body(self.app_js, "showPasswordLoginQRCode")
        self.assertIn("verificationUrl = normalizeSafeHttpUrl(verificationUrl);", password_body)
        self.assertIn("showVerificationLinkButton = Boolean(showVerificationLinkButton) && Boolean(verificationUrl);", password_body)
        self.assertIn("linkButton.removeAttribute('href');", password_body)
        self.assertNotIn("linkButton.href = '#';", password_body)

    def test_qr_login_raw_fetch_flows_handle_unauthorized_before_followup_work(self):
        generate_body = _extract_function_body(self.app_js, "generateQRCode")
        status_body = _extract_function_body(self.app_js, "checkQRCodeStatus")

        for body, unauthorized_fragment, anchor_fragment in (
            (generate_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (status_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "扫码登录这俩 raw fetch 碰到 401 得先滚去登录，别后面还继续生成二维码、读状态、弹业务提示",
                )

    def test_qr_login_failure_actions_read_structured_error_messages(self):
        generate_body = _extract_function_body(self.app_js, "generateQRCode")
        status_body = _extract_function_body(self.app_js, "checkQRCodeStatus")

        for body, error_fragment, toast_fragment, label in (
            (generate_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showQRCodeError(errorMessage || '生成二维码失败');", "扫码登录二维码生成"),
            (status_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(errorMessage || '扫码登录失败', 'danger');", "扫码登录状态轮询"),
        ):
            with self.subTest(label=label):
                self.assertIn(error_fragment, body)
                self.assertLess(
                    body.index(error_fragment),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再拿裸 HTTP 状态把人糊弄过去",
                )

    def test_qr_login_async_flows_ignore_stale_or_hidden_modal_responses(self):
        generate_body = _extract_function_body(self.app_js, "generateQRCode")
        status_body = _extract_function_body(self.app_js, "checkQRCodeStatus")
        clear_body = _extract_function_body(self.app_js, "clearQRCodeCheck")

        self.assertIn("let qrCodeLoginRequestSequence = 0;", self.app_js)
        self.assertIn("const requestSequence = ++qrCodeLoginRequestSequence;", generate_body)
        self.assertIn("qrCodeLoginRequestSequence += 1;", clear_body)

        self.assertIn("requestSequence !== qrCodeLoginRequestSequence", generate_body)
        self.assertIn("document.getElementById('qrCodeLoginModal')?.classList.contains('show')", generate_body)
        self.assertLess(
            generate_body.index("requestSequence !== qrCodeLoginRequestSequence"),
            generate_body.index("startQRCodeCheck();"),
            "同一个扫码弹窗里已经发起新的二维码生成后，旧响应别再回来偷开轮询",
        )
        self.assertLess(
            generate_body.rfind("requestSequence !== qrCodeLoginRequestSequence", 0, generate_body.index("showQRCodeError(errorMessage || '生成二维码失败');")),
            generate_body.index("showQRCodeError(errorMessage || '生成二维码失败');"),
            "扫码二维码生成失败读完错误体后也得先确认当前还是同一轮请求，别旧错误回来把新弹窗糊一脸",
        )

        self.assertIn("const requestSequence = qrCodeLoginRequestSequence;", status_body)
        self.assertIn("requestSequence !== qrCodeLoginRequestSequence", status_body)
        self.assertLess(
            status_body.index("requestSequence !== qrCodeLoginRequestSequence"),
            status_body.index("const data = await response.json();"),
            "扫码状态轮询旧响应不该晚回来后还继续消费当前会话的状态正文",
        )
        self.assertLess(
            status_body.rfind("requestSequence !== qrCodeLoginRequestSequence", 0, status_body.index("showToast(errorMessage || '扫码登录失败', 'danger');")),
            status_body.index("showToast(errorMessage || '扫码登录失败', 'danger');"),
            "扫码状态轮询读完错误体后也得先验 stale，别旧错误回魂乱弹 toast",
        )
        finally_block = status_body[status_body.index("} finally {"):]
        self.assertIn("requestSequence === qrCodeLoginRequestSequence", finally_block)
        self.assertIn("requestSessionId === qrCodeVerificationState.activeSessionId", finally_block)
        self.assertLess(
            finally_block.index("requestSequence === qrCodeLoginRequestSequence"),
            finally_block.index("qrCodeVerificationState.inFlight = false;"),
            "扫码状态旧 finally 不该把新会话的 inFlight 状态先给抹掉",
        )

    def test_notification_channels_table_escapes_name_type_and_config_summary(self):
        body = _extract_function_body(self.app_js, "renderNotificationChannels")
        self.assertIn("const normalizedChannelId = Number(channel.id);", body)
        self.assertIn("const safeChannelId = escapeHtml(Number.isFinite(normalizedChannelId) ? String(normalizedChannelId) : '-');", body)
        self.assertIn("const channelName = typeof channel.name === 'string' ? channel.name : '';", body)
        self.assertIn("const safeChannelName = escapeHtml(channelName || '未命名渠道');", body)
        self.assertIn("const safeTypeDisplay = escapeHtml(typeDisplay || '未知类型');", body)
        self.assertIn("if (!configData || typeof configData !== 'object' || Array.isArray(configData)) {", body)
        self.assertIn("safeConfigDisplay = configEntries.map(([key, value]) => {", body)
        self.assertIn("const safeKey = escapeHtml(String(key || ''));", body)
        self.assertIn("const normalizedKey = String(key || '').toLowerCase();", body)
        self.assertIn("const normalizedValue = String(value ?? '');", body)
        self.assertIn("const safeDisplayValue = escapeHtml(displayValue);", body)
        self.assertIn("normalizedKey.includes('key')", body)
        self.assertIn("normalizedKey === 'webhook_url'", body)
        self.assertIn("normalizedKey === 'api_url'", body)
        self.assertIn("if (normalizedKey === 'headers') {", body)
        self.assertIn("return `${safeKey}: 已配置`;", body)
        self.assertIn("safeConfigDisplay = escapeHtml('已配置（旧格式）');", body)
        self.assertNotIn("${channel.name}", body)
        self.assertNotIn("${typeDisplay}", body)
        self.assertNotIn("${configDisplay}</small>", body)
        self.assertIn('<td><strong class="text-primary">${safeChannelId}</strong></td>', body)
        self.assertIn('onclick="editNotificationChannel(${normalizedChannelId})"', body)
        self.assertIn('onclick="deleteNotificationChannel(${normalizedChannelId})"', body)

    def test_notification_loaders_reset_tables_when_primary_fetch_fails(self):
        load_channels_body = _extract_function_body(self.app_js, "loadNotificationChannels")
        load_notifications_body = _extract_function_body(self.app_js, "loadMessageNotifications")
        reset_channels_body = _extract_function_body(self.app_js, "resetNotificationChannelsTable")
        reset_notifications_body = _extract_function_body(self.app_js, "resetMessageNotificationsTable")

        self.assertIn("function resetNotificationChannelsTable(message = '暂无通知渠道数据') {", self.app_js)
        self.assertIn("const tbody = document.getElementById('channelsTableBody');", reset_channels_body)
        self.assertIn("tbody.innerHTML = `", reset_channels_body)
        self.assertIn("${escapeHtml(message)}", reset_channels_body)
        self.assertIn("resetNotificationChannelsTable('加载通知渠道失败');", load_channels_body)
        self.assertIn("if (!response.ok) {", load_channels_body)

        self.assertIn("function resetMessageNotificationsTable(message = '暂无消息通知数据') {", self.app_js)
        self.assertIn("const tbody = document.getElementById('notificationsTableBody');", reset_notifications_body)
        self.assertIn("tbody.innerHTML = `", reset_notifications_body)
        self.assertIn("${escapeHtml(message)}", reset_notifications_body)
        self.assertIn("resetMessageNotificationsTable('加载消息通知配置失败');", load_notifications_body)
        self.assertIn("if (!accountsResponse.ok) {", load_notifications_body)

    def test_notification_channels_loader_resets_stale_table_before_fetch_and_returns_status(self):
        load_body = _extract_function_body(self.app_js, "loadNotificationChannels")
        reset_body = _extract_function_body(self.app_js, "resetNotificationChannelsTable")

        self.assertIn("const tbody = document.getElementById('channelsTableBody');", reset_body)
        self.assertIn("${escapeHtml(message)}", reset_body)
        self.assertIn("resetNotificationChannelsTable();", load_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("resetNotificationChannelsTable('加载通知渠道失败');", load_body)
        self.assertIn("showToast(`加载通知渠道失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("resetNotificationChannelsTable();"),
            load_body.index("const response = await fetch(`${apiBase}/notification-channels`, {"),
            "通知渠道重新加载前应先清掉旧列表，失败后别让陈年数据继续站台",
        )

    def test_notification_channels_loader_rejects_malformed_payloads_before_rendering(self):
        body = _extract_function_body(self.app_js, "loadNotificationChannels")

        self.assertIn("const response = await fetch(`${apiBase}/notification-channels`, {", body)
        self.assertIn("if (!Array.isArray(channels)) {", body)
        self.assertIn("channels.some(channel =>", body)
        self.assertIn("typeof channel.enabled !== 'boolean'", body)
        self.assertIn("typeof channel.name !== 'string'", body)
        self.assertIn("typeof channel.config !== 'string'", body)
        self.assertIn("throw new Error('通知渠道列表返回格式异常');", body)
        self.assertLess(
            body.index("channels.some(channel =>"),
            body.index("renderNotificationChannels(channels);"),
            "通知渠道列表接口如果回了坏渠道项，前端得先拦住，别把 undefined id/type 渲染成一堆残缺按钮和脏行再装列表正常",
        )

    def test_notification_channel_mutations_only_report_success_when_reload_succeeds(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_body = _extract_function_body(self.app_js, "updateNotificationChannel")
        delete_body = _extract_function_body(self.app_js, "deleteNotificationChannel")

        for body, success_message, warning_message in (
            (save_body, "通知渠道添加成功", "通知渠道添加成功，但列表刷新失败，请稍后手动刷新"),
            (update_body, "通知渠道更新成功", "通知渠道更新成功，但列表刷新失败，请稍后手动刷新"),
            (delete_body, "通知渠道删除成功", "通知渠道删除成功，但列表刷新失败，请稍后手动刷新"),
        ):
            with self.subTest(success_message=success_message):
                self.assertIn("const channelsLoaded = await loadNotificationChannels();", body)
                self.assertIn("if (channelsLoaded === true) {", body)
                self.assertIn("} else if (channelsLoaded === false) {", body)
                self.assertIn(f"showToast('{success_message}', 'success');", body)
                self.assertIn(f"showToast('{warning_message}', 'warning');", body)

    def test_notification_channel_mutations_do_not_emit_cross_page_toasts_after_leaving_section(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_body = _extract_function_body(self.app_js, "updateNotificationChannel")
        delete_body = _extract_function_body(self.app_js, "deleteNotificationChannel")

        for body, success_fragment in (
            (save_body, "showToast('通知渠道添加成功', 'success');"),
            (update_body, "showToast('通知渠道更新成功', 'success');"),
            (delete_body, "showToast('通知渠道删除成功', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('notification-channels-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('notification-channels-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "通知渠道操作在离开页面后不该再跨页弹 success toast",
                )

    def test_switching_away_from_notification_sections_closes_open_modals(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("const addChannelModalElement = document.getElementById('addChannelModal');", show_section_body)
        self.assertIn("addChannelModal.hide();", show_section_body)
        self.assertIn("const editChannelModalElement = document.getElementById('editChannelModal');", show_section_body)
        self.assertIn("editChannelModal.hide();", show_section_body)
        self.assertIn("const configNotificationModalElement = document.getElementById('configNotificationModal');", show_section_body)
        self.assertIn("configNotificationModal.hide();", show_section_body)

    def test_notification_channel_add_modal_save_respects_modal_session_before_hiding_or_toasting(self):
        self.assertIn("let notificationChannelAddRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        show_add_body = _extract_function_body(self.app_js, "showAddChannelModal")
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")

        self.assertIn("if (sectionName !== 'notification-channels') {", show_section_body)
        self.assertIn("notificationChannelAddRequestSequence += 1;", show_section_body)

        self.assertIn("if (modalElement.dataset.notificationChannelAddIgnoreNextHidden === 'true') {", show_add_body)
        self.assertIn("modalElement.dataset.notificationChannelAddIgnoreNextHidden = 'false';", show_add_body)
        self.assertIn("notificationChannelAddRequestSequence += 1;", show_add_body)

        self.assertIn("const requestSequence = notificationChannelAddRequestSequence;", save_body)
        self.assertIn("requestSequence !== notificationChannelAddRequestSequence", save_body)
        self.assertIn("modalElement.dataset.notificationChannelAddIgnoreNextHidden = 'true';", save_body)
        self.assertIn("return null;", save_body)
        self.assertLess(
            save_body.index("requestSequence !== notificationChannelAddRequestSequence"),
            save_body.index("modal.hide();"),
            "旧的通知渠道新增响应不该回来把已经重开的新增弹窗又关掉",
        )

    def test_notification_channel_save_actions_ignore_older_same_modal_responses(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_body = _extract_function_body(self.app_js, "updateNotificationChannel")

        for body, failure_fragment in (
            (save_body, "showToast(`添加失败: ${error}`, 'danger');"),
            (update_body, "showToast(`更新失败: ${error}`, 'danger');"),
        ):
            with self.subTest(failure_fragment=failure_fragment):
                self.assertIn("++notificationChannelMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== notificationChannelMutationActionRequestSequence", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== notificationChannelMutationActionRequestSequence"),
                    body.index("modal.hide();"),
                    "同一通知渠道弹窗里第二次保存已经发出后，第一次响应不该回来把当前弹窗关掉",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== notificationChannelMutationActionRequestSequence", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "同一通知渠道弹窗里旧的失败响应不该晚回来后拿旧错误糊当前会话一脸",
                )

    def test_notification_channel_update_save_respects_edit_modal_request_sequence_before_hiding_or_toasting(self):
        edit_body = _extract_function_body(self.app_js, "editNotificationChannel")
        body = _extract_function_body(self.app_js, "updateNotificationChannel")
        self.assertIn("if (modalElement.dataset.notificationChannelEditIgnoreNextHidden === 'true') {", edit_body)
        self.assertIn("modalElement.dataset.notificationChannelEditIgnoreNextHidden = 'false';", edit_body)
        self.assertIn("const requestSequence = notificationChannelEditRequestSequence;", body)
        self.assertIn("requestSequence !== notificationChannelEditRequestSequence", body)
        self.assertIn("modalElement.dataset.notificationChannelEditIgnoreNextHidden = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== notificationChannelEditRequestSequence"),
            body.index("modal.hide();"),
            "编辑通知渠道保存的旧响应不该回来把已经重开的编辑弹窗又关掉",
        )

    def test_notification_channel_delete_actions_ignore_older_same_page_responses(self):
        self.assertIn("let notificationChannelMutationActionRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        delete_body = _extract_function_body(self.app_js, "deleteNotificationChannel")

        self.assertIn("notificationChannelMutationActionRequestSequence += 1;", show_section_body)
        self.assertIn("++notificationChannelMutationActionRequestSequence", delete_body)
        self.assertIn("actionRequestSequence !== notificationChannelMutationActionRequestSequence", delete_body)
        self.assertIn("return null;", delete_body)
        self.assertLess(
            delete_body.index("actionRequestSequence !== notificationChannelMutationActionRequestSequence"),
            delete_body.index("const channelsLoaded = await loadNotificationChannels();"),
            "同页连续删除通知渠道时，旧响应不该晚回来后又触发列表刷新和旧结果 toast",
        )
        error_text_index = delete_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        failure_toast_index = delete_body.index("showToast(`删除失败: ${error}`, 'danger');")
        self.assertLess(
            delete_body.find("actionRequestSequence !== notificationChannelMutationActionRequestSequence", error_text_index),
            failure_toast_index,
            "同页连续删除通知渠道时，读完错误体后还得再验一次 stale，别让旧失败响应回魂刷红字",
        )

    def test_notification_channel_mutation_catch_failures_surface_runtime_error_messages(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_body = _extract_function_body(self.app_js, "updateNotificationChannel")
        delete_body = _extract_function_body(self.app_js, "deleteNotificationChannel")

        for body, legacy_toast, failure_toast, extra_guard, action_name in (
            (
                save_body,
                "showToast('添加通知渠道失败', 'danger');",
                "showToast(`添加通知渠道失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== notificationChannelAddRequestSequence",
                "新增通知渠道",
            ),
            (
                update_body,
                "showToast('更新通知渠道失败', 'danger');",
                "showToast(`更新通知渠道失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== notificationChannelEditRequestSequence",
                "更新通知渠道",
            ),
            (
                delete_body,
                "showToast('删除通知渠道失败', 'danger');",
                "showToast(`删除通知渠道失败: ${error.message || '请稍后重试'}`, 'danger');",
                None,
                "删除通知渠道",
            ),
        ):
            with self.subTest(action_name=action_name):
                self.assertNotIn(legacy_toast, body)
                self.assertIn(failure_toast, body)
                failure_toast_index = body.index(failure_toast)
                self.assertLess(
                    body.rfind("actionRequestSequence !== notificationChannelMutationActionRequestSequence", 0, failure_toast_index),
                    failure_toast_index,
                    f"{action_name} catch 兜底前得先验 mutation action sequence，别让旧异常跨会话回魂乱喷红字",
                )
                if extra_guard is not None:
                    self.assertLess(
                        body.rfind(extra_guard, 0, failure_toast_index),
                        failure_toast_index,
                        f"{action_name} catch 兜底前得先验当前弹窗会话，别拿旧异常糊当前操作",
                    )
                self.assertLess(
                    body.rfind("!document.getElementById('notification-channels-section')?.classList.contains('active')", 0, failure_toast_index),
                    failure_toast_index,
                    f"都切出通知渠道页了，{action_name} 的旧异常就别跨页回来刷 danger toast 了",
                )

    def test_notification_channel_mutation_action_sequence_starts_only_after_validation_or_confirmation(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_body = _extract_function_body(self.app_js, "updateNotificationChannel")
        delete_body = _extract_function_body(self.app_js, "deleteNotificationChannel")

        for body in (save_body, update_body):
            with self.subTest(body=body[:80]):
                self.assertLess(
                    body.index("if (!name.trim()) {"),
                    body.index("const actionRequestSequence = ++notificationChannelMutationActionRequestSequence;"),
                    "渠道名称为空时只是前端校验，别先把通知渠道 mutation action sequence 顶掉别的正常动作",
                )
                self.assertLess(
                    body.index("if (!config) {"),
                    body.index("const actionRequestSequence = ++notificationChannelMutationActionRequestSequence;"),
                    "渠道类型无效时只是前端校验，别先把通知渠道 mutation action sequence 顶掉别的正常动作",
                )
                self.assertLess(
                    body.index("if (hasError) return;"),
                    body.index("const actionRequestSequence = ++notificationChannelMutationActionRequestSequence;"),
                    "字段缺失/控件异常时只是前端校验，别先把通知渠道 mutation action sequence 顶掉别的正常动作",
                )
                self.assertLess(
                    body.index("if (!element.checkValidity()) {"),
                    body.index("const actionRequestSequence = ++notificationChannelMutationActionRequestSequence;"),
                    "动态字段原生格式校验没过时只是前端校验，别先把通知渠道 mutation action sequence 顶掉别的正常动作",
                )

        delete_confirm_return_index = delete_body.index("return;", delete_body.index("if (!confirm("))
        self.assertLess(
            delete_confirm_return_index,
            delete_body.index("const actionRequestSequence = ++notificationChannelMutationActionRequestSequence;"),
            "用户都取消删除通知渠道了，就别先把通知渠道 mutation action sequence 顶掉别的正常动作",
        )

    def test_notification_channel_edit_modal_ignores_stale_async_responses_and_hidden_modal_state(self):
        self.assertIn("let notificationChannelEditRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "editNotificationChannel")

        self.assertIn("if (sectionName !== 'notification-channels') {", show_section_body)
        self.assertIn("notificationChannelEditRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++notificationChannelEditRequestSequence;", body)
        self.assertIn("requestSequence !== notificationChannelEditRequestSequence", body)
        self.assertIn("!document.getElementById('notification-channels-section')?.classList.contains('active')", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("notificationChannelEditRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.notificationChannelEditModalBound = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== notificationChannelEditRequestSequence"),
            body.index("document.getElementById('editChannelId').value = channel.id;"),
            "旧的通知渠道编辑请求不该晚回来后把当前弹窗改成别的渠道",
        )
        modal_show_index = body.index("modal.show();")
        self.assertLess(
            body.rfind("requestSequence !== notificationChannelEditRequestSequence", 0, modal_show_index),
            modal_show_index,
            "都切页或关弹窗了，旧请求不该再回来把编辑通知渠道弹窗重新弹出来",
        )

    def test_notification_channel_edit_modal_fetches_single_channel_detail_instead_of_full_secret_list(self):
        body = _extract_function_body(self.app_js, "editNotificationChannel")

        self.assertIn("const response = await fetch(`${apiBase}/notification-channels/${channelId}`, {", body)
        self.assertIn("const channel = await response.json();", body)
        self.assertNotIn("const response = await fetch(`${apiBase}/notification-channels`, {", body)
        self.assertNotIn("const channels = await response.json();", body)
        self.assertNotIn("channels.find(c => c.id === channelId)", body)

    def test_notification_channel_edit_modal_rejects_malformed_detail_payload_before_prefilling_form(self):
        body = _extract_function_body(self.app_js, "editNotificationChannel")

        self.assertIn("if (", body)
        self.assertIn("Array.isArray(channel)", body)
        self.assertIn("!Number.isFinite(Number(channel.id))", body)
        self.assertIn("typeof channel.name !== 'string'", body)
        self.assertIn("!String(channel.type || '').trim()", body)
        self.assertIn("typeof channel.enabled !== 'boolean'", body)
        self.assertIn("typeof channel.config !== 'string'", body)
        self.assertIn("throw new Error('通知渠道详情返回格式异常');", body)
        self.assertLess(
            body.index("throw new Error('通知渠道详情返回格式异常');"),
            body.index("let channelType = normalizeNotificationChannelType(channel.type);"),
            "通知渠道详情接口如果回了歪 payload，前端得先当格式异常拦住，别把 undefined 类型误提示成“不支持的渠道类型”继续糊弄人",
        )

    def test_notification_channel_edit_modal_preserves_legacy_plain_url_configs_for_webhook_like_types(self):
        body = _extract_function_body(self.app_js, "editNotificationChannel")

        self.assertIn("const rawConfigText = channel.config;", body)
        self.assertIn("let parsedConfig = null;", body)
        self.assertIn("parsedConfig = JSON.parse(rawConfigText || '{}');", body)
        self.assertIn("if (parsedConfig && typeof parsedConfig === 'object' && !Array.isArray(parsedConfig)) {", body)
        self.assertIn("const legacyConfigValue = typeof parsedConfig === 'string' ? parsedConfig : rawConfigText;", body)
        self.assertIn("} else if (channel.type === 'webhook') {", body)
        self.assertIn("} else if (channel.type === 'wechat') {", body)
        self.assertIn("configData = { webhook_url: legacyConfigValue };", body)
        self.assertLess(
            body.index("} else if (channel.type === 'webhook') {"),
            body.index("} else if (channel.type === 'bark') {"),
            "旧版 webhook/企业微信渠道如果配置不是对象，编辑弹窗也得把它回填进 webhook_url，别一打开就把老配置整没了",
        )

    def test_notification_channel_edit_modal_stringifies_config_field_values_before_prefill(self):
        body = _extract_function_body(self.app_js, "editNotificationChannel")

        self.assertIn("if (element && configData[field.id]) {", body)
        self.assertIn("element.value = String(configData[field.id]);", body)
        self.assertNotIn("element.value = configData[field.id];", body)
        self.assertLess(
            body.index("element.value = String(configData[field.id]);"),
            body.index("const modal = new bootstrap.Modal(modalElement);"),
            "通知渠道编辑弹窗回填配置字段时得先统一转成字符串，别把数字/布尔值直接塞进 input 让不同浏览器自己瞎解释",
        )

    def test_notification_channel_loader_and_editor_failures_surface_structured_error_messages(self):
        load_body = _extract_function_body(self.app_js, "loadNotificationChannels")
        edit_body = _extract_function_body(self.app_js, "editNotificationChannel")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("showToast(`加载通知渠道失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        load_error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        load_throw_index = load_body.index("throw new Error(errorMessage);")
        self.assertLess(
            load_error_index,
            load_throw_index,
            "通知渠道列表 HTTP 失败时得先把 detail/message 解出来，别直接抛固定文案糊弄人",
        )
        self.assertLess(
            load_body.find("requestSequence !== notificationChannelsRequestSequence", load_error_index),
            load_throw_index,
            "通知渠道列表旧失败响应读完错误文本后，先验 request sequence，别回魂打断当前页面",
        )
        self.assertLess(
            load_body.find("!document.getElementById('notification-channels-section')?.classList.contains('active')", load_error_index),
            load_throw_index,
            "都切出通知渠道页了，旧失败响应读完错误文本也别再往 catch 里丢异常",
        )

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", edit_body)
        self.assertIn("throw new Error(errorMessage);", edit_body)
        self.assertIn("showToast(`编辑通知渠道失败: ${error.message || '请稍后重试'}`, 'danger');", edit_body)
        edit_error_index = edit_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        edit_throw_index = edit_body.index("throw new Error(errorMessage);")
        self.assertLess(
            edit_error_index,
            edit_throw_index,
            "通知渠道编辑弹窗拉取失败时得先把 detail/message 解出来，别把后端真报错吞没了",
        )
        self.assertLess(
            edit_body.find("requestSequence !== notificationChannelEditRequestSequence", edit_error_index),
            edit_throw_index,
            "通知渠道编辑弹窗旧失败响应读完错误文本后，先验当前 modal session，别回魂篡位",
        )
        self.assertLess(
            edit_body.find("!document.getElementById('notification-channels-section')?.classList.contains('active')", edit_error_index),
            edit_throw_index,
            "都切出通知渠道页了，旧编辑失败响应读完错误文本也别再跨页甩异常",
        )

    def test_message_notification_config_modal_rejects_malformed_payloads_before_prefilling_selection(self):
        body = _extract_function_body(self.app_js, "configAccountNotification")

        self.assertIn("if (!Array.isArray(channels)) {", body)
        self.assertIn("channels.some(channel =>", body)
        self.assertIn("typeof channel.enabled !== 'boolean'", body)
        self.assertIn("typeof channel.name !== 'string'", body)
        self.assertIn("typeof channel.config !== 'string'", body)
        self.assertIn("throw new Error('通知渠道列表返回格式异常');", body)
        self.assertIn("if (!Array.isArray(currentNotifications)) {", body)
        self.assertIn("currentNotifications.some(notification =>", body)
        self.assertIn("typeof notification.channel_name !== 'string'", body)
        self.assertIn("typeof notification.enabled !== 'boolean'", body)
        self.assertIn("typeof notification.channel_enabled !== 'boolean'", body)
        self.assertIn("throw new Error('账号通知配置返回格式异常');", body)
        self.assertLess(
            body.index("channels.some(channel =>"),
            body.index("const enabledChannels = channels.filter(channel => channel.enabled);"),
            "账号通知配置弹窗如果通知渠道 payload 歪了，前端得先当格式异常拦住，别等 filter/map 自爆后再给人一嘴莫名其妙的 JS 错",
        )
        self.assertLess(
            body.index("currentNotifications.some(notification =>"),
            body.index("const currentChannelIds = new Set(currentNotifications.map(notification => notification.channel_id));"),
            "账号通知配置 payload 如果不是数组，也得先报格式异常，别把脏结构硬塞进当前选中渠道集合里恶心人",
        )

    def test_notification_channels_loader_ignores_stale_async_responses_and_hidden_section_updates(self):
        self.assertIn("let notificationChannelsRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_body = _extract_function_body(self.app_js, "loadNotificationChannels")

        self.assertIn("if (sectionName !== 'notification-channels') {", show_section_body)
        self.assertIn("notificationChannelsRequestSequence += 1;", show_section_body)
        self.assertIn("const requestSequence = ++notificationChannelsRequestSequence;", load_body)
        self.assertIn("requestSequence !== notificationChannelsRequestSequence", load_body)
        self.assertIn("!document.getElementById('notification-channels-section')?.classList.contains('active')", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("requestSequence !== notificationChannelsRequestSequence"),
            load_body.index("renderNotificationChannels(channels);"),
            "通知渠道旧请求不该在用户切页后再回来重绘表格",
        )
        self.assertLess(
            load_body.rfind("!document.getElementById('notification-channels-section')?.classList.contains('active')"),
            load_body.index("showToast(`加载通知渠道失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "都离开通知渠道页了，旧失败请求就别跨页回来乱弹 danger toast",
        )

    def test_shared_helpers_do_not_keep_multiple_conflicting_definitions(self):
        for function_name in ("escapeHtml", "exportKeywords", "refreshQRCode"):
            with self.subTest(function_name=function_name):
                occurrences = len(re.findall(rf"(?:async\s+)?function\s+{re.escape(function_name)}\s*\(", self.app_js))
                self.assertEqual(occurrences, 1, f"{function_name} 不该在 app.js 里定义多次")

    def test_remaining_debug_console_logs_are_removed_from_user_facing_flows(self):
        for function_name in (
            "toggleEditMultiSpecFields",
            "editCard",
            "initItemsSearch",
            "startRefreshCookiePolling",
            "checkPasswordLoginStatus",
            "handlePasswordLoginFailure",
        ):
            body = _extract_function_body(self.app_js, function_name)
            self.assertNotIn("console.log(", body)

    def test_navigation_and_auto_reply_flows_do_not_leave_debug_console_logs(self):
        for function_name in (
            "showSection",
            "refreshAccountList",
            "refreshKeywordsList",
            "loadAccountKeywords",
            "loadItemsList",
            "loadItemsListForImageKeyword",
            "renderKeywordsList",
            "loadUserManagement",
            "loadDataManagement",
        ):
            body = _extract_function_body(self.app_js, function_name)
            self.assertNotIn("console.log(", body)

    def test_item_search_and_captcha_helpers_do_not_leave_debug_console_logs(self):
        for function_name in (
            "handleItemSearch",
            "createItemCard",
            "startCaptchaSessionMonitor",
            "stopCaptchaSessionMonitor",
            "testCaptchaSessionMonitor",
            "testShowCaptchaModal",
            "showCaptchaVerificationModal",
            "startCheckCaptchaCompletion",
        ):
            body = _extract_function_body(self.app_js, function_name)
            self.assertNotIn("console.log(", body)

    def test_order_actions_forward_account_scope_to_backend(self):
        required_fragments = [
            "async function deleteOrder(orderId, accountId)",
            "async function manualDeliverOrder(orderId, accountId)",
            "async function refreshOrderStatus(orderId, accountId)",
            "manualDeliverOrder(orderId, accountId);",
            "refreshOrderStatus(orderId, accountId);",
            "deleteOrder(orderId, accountId);",
            "const scopedUrl = `${apiBase}/api/orders/${orderId}?account_id=${encodeURIComponent(accountId)}`;",
            "const scopedUrl = `${apiBase}/api/orders/${orderId}/deliver?account_id=${encodeURIComponent(accountId)}`;",
            "const scopedUrl = `${apiBase}/api/orders/${orderId}/refresh?account_id=${encodeURIComponent(accountId)}`;",
            "for (const { orderId, accountId } of selectedOrders)",
            "const scopedUrl = `${apiBase}/api/orders/${orderId}/refresh?account_id=${encodeURIComponent(accountId)}`;",
        ]

        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.app_js)

    def test_order_mutations_only_report_success_when_followup_reload_succeeds(self):
        delete_body = _extract_function_body(self.app_js, "deleteOrder")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteOrders")
        batch_refresh_body = _extract_function_body(self.app_js, "batchRefreshOrders")
        deliver_body = _extract_function_body(self.app_js, "manualDeliverOrder")
        refresh_body = _extract_function_body(self.app_js, "refreshOrderStatus")

        self.assertIn("const ordersLoaded = await refreshOrdersData();", delete_body)
        self.assertIn("if (ordersLoaded) {", delete_body)
        self.assertIn("showToast('订单删除成功', 'success');", delete_body)
        self.assertIn("showToast('订单删除成功，但订单列表刷新失败，请稍后手动刷新', 'warning');", delete_body)

        self.assertIn("const ordersLoaded = await refreshOrdersData();", batch_delete_body)
        self.assertIn("if (ordersLoaded) {", batch_delete_body)
        self.assertIn("showToast(`成功删除 ${successCount} 个订单${failCount > 0 ? `，${failCount} 个失败` : ''}`", batch_delete_body)
        self.assertIn("showToast('批量删除已完成，但订单列表刷新失败，请稍后手动刷新', 'warning');", batch_delete_body)

        self.assertIn("const ordersLoaded = await refreshOrdersData();", deliver_body)
        self.assertIn("if (ordersLoaded) {", deliver_body)
        self.assertIn("showToast(`发货成功！\\n${result.message}`, 'success');", deliver_body)
        self.assertIn("showToast('发货成功，但订单列表刷新失败，请稍后手动刷新', 'warning');", deliver_body)

        self.assertIn("const ordersLoaded = await refreshOrdersData();", refresh_body)
        self.assertIn("if (ordersLoaded) {", refresh_body)
        self.assertIn("showToast(`订单状态已更新: ${getOrderStatusText(result.new_status)}`, 'success');", refresh_body)
        self.assertIn("showToast('订单状态已更新，但订单列表刷新失败，请稍后手动刷新', 'warning');", refresh_body)

        self.assertIn("const ordersLoaded = await refreshOrdersData();", batch_refresh_body)
        self.assertIn("if (ordersLoaded) {", batch_refresh_body)
        self.assertIn("showToast(`成功刷新 ${successCount} 个订单状态`, 'success');", batch_refresh_body)
        self.assertIn("showToast('订单列表刷新失败，请稍后手动刷新', 'warning');", batch_refresh_body)

    def test_order_single_action_non_success_branches_report_followup_reload_failures(self):
        deliver_body = _extract_function_body(self.app_js, "manualDeliverOrder")
        refresh_body = _extract_function_body(self.app_js, "refreshOrderStatus")

        self.assertIn("const ordersLoaded = await refreshOrdersData();", deliver_body)
        self.assertIn("if (ordersLoaded === false) {", deliver_body)
        self.assertIn("showToast(`发货失败: ${result.message || '未知原因'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');", deliver_body)
        self.assertLess(
            deliver_body.index("const ordersLoaded = await refreshOrdersData();"),
            deliver_body.index("showToast(`发货失败: ${result.message || '未知原因'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');"),
            "手动发货没发成时既然还会跟进刷新订单列表，就别把刷新失败悄悄咽了",
        )

        self.assertIn("const ordersLoaded = await refreshOrdersData();", refresh_body)
        self.assertIn("if (ordersLoaded === false) {", refresh_body)
        self.assertIn("showToast(`${result.message || '订单状态无变化'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');", refresh_body)
        self.assertLess(
            refresh_body.index("const ordersLoaded = await refreshOrdersData();"),
            refresh_body.index("showToast(`${result.message || '订单状态无变化'}，但订单列表刷新失败，请稍后手动刷新`, 'warning');"),
            "订单状态没变化时跟进刷新如果也翻车，前端得把这事说明白，别装作列表还是新的",
        )

    def test_order_single_mutation_actions_do_not_emit_cross_page_toasts_after_leaving_orders(self):
        delete_body = _extract_function_body(self.app_js, "deleteOrder")
        deliver_body = _extract_function_body(self.app_js, "manualDeliverOrder")
        refresh_body = _extract_function_body(self.app_js, "refreshOrderStatus")

        for body, success_fragment in (
            (delete_body, "showToast('订单删除成功', 'success');"),
            (deliver_body, "showToast(`发货成功！\\n${result.message}`, 'success');"),
            (refresh_body, "showToast(`订单状态已更新: ${getOrderStatusText(result.new_status)}`, 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!isOrdersSectionActive()", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!isOrdersSectionActive()"),
                    body.index(success_fragment),
                    "订单单项操作在离开 orders 页面后不该再跨页弹 success toast",
                )

    def test_order_batch_actions_do_not_emit_cross_page_results_after_leaving_orders(self):
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteOrders")
        batch_refresh_body = _extract_function_body(self.app_js, "batchRefreshOrders")

        for body, result_fragment in (
            (batch_delete_body, "showToast(`成功删除 ${successCount} 个订单${failCount > 0 ? `，${failCount} 个失败` : ''}`"),
            (batch_refresh_body, "showToast(`成功刷新 ${successCount} 个订单状态`, 'success');"),
        ):
            with self.subTest(result_fragment=result_fragment):
                self.assertIn("!isOrdersSectionActive()", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!isOrdersSectionActive()"),
                    body.index(result_fragment),
                    "订单批量操作在离开 orders 页面后不该再跨页汇报结果",
                )

    def test_order_mutation_actions_use_action_sequence_to_ignore_older_same_page_responses(self):
        self.assertIn("let orderMutationActionRequestSequence = 0;", self.app_js)
        delete_body = _extract_function_body(self.app_js, "deleteOrder")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteOrders")
        deliver_body = _extract_function_body(self.app_js, "manualDeliverOrder")
        refresh_body = _extract_function_body(self.app_js, "refreshOrderStatus")
        batch_refresh_body = _extract_function_body(self.app_js, "batchRefreshOrders")

        for body, anchor_fragment in (
            (delete_body, "const ordersLoaded = await refreshOrdersData();"),
            (batch_delete_body, "const ordersLoaded = await refreshOrdersData();"),
            (deliver_body, "const ordersLoaded = await refreshOrdersData();"),
            (refresh_body, "const ordersLoaded = await refreshOrdersData();"),
            (batch_refresh_body, "const ordersLoaded = await refreshOrdersData();"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("++orderMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== orderMutationActionRequestSequence", body)
                self.assertLess(
                    body.index("actionRequestSequence !== orderMutationActionRequestSequence"),
                    body.index(anchor_fragment),
                    "旧的订单操作响应不该在新一轮操作开始后还回来触发列表刷新",
                )

    def test_order_mutation_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        delete_body = _extract_function_body(self.app_js, "deleteOrder")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteOrders")
        deliver_body = _extract_function_body(self.app_js, "manualDeliverOrder")
        refresh_body = _extract_function_body(self.app_js, "refreshOrderStatus")
        batch_refresh_body = _extract_function_body(self.app_js, "batchRefreshOrders")

        for body, anchor_fragment in (
            (delete_body, "if (response.ok) {"),
            (batch_delete_body, "if (response.ok) {"),
            (deliver_body, "const result = await response.json();"),
            (refresh_body, "const result = await response.json();"),
            (batch_refresh_body, "if (response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    "订单模块这些 raw fetch 遇到 401 得先滚去登录，别后面还继续删单、读 JSON、算批量结果",
                )

    def test_order_mutation_failures_read_structured_error_messages_before_toasts(self):
        delete_body = _extract_function_body(self.app_js, "deleteOrder")
        deliver_body = _extract_function_body(self.app_js, "manualDeliverOrder")
        refresh_body = _extract_function_body(self.app_js, "refreshOrderStatus")

        for body, error_fragment, toast_fragment, label in (
            (delete_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`删除失败: ${error}`, 'danger');", "删除订单"),
            (deliver_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`发货失败: ${error}`, 'danger');", "手动发货"),
            (refresh_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`刷新失败: ${error}`, 'danger');", "刷新订单状态"),
        ):
            with self.subTest(label=label):
                self.assertIn(error_fragment, body)
                self.assertLess(
                    body.index(error_fragment),
                    body.index(toast_fragment),
                    f"{label}失败时得先把 detail/message 解出来，别上来就拿固定红字糊脸",
                )

    def test_order_batch_delete_all_failures_surface_first_backend_or_runtime_error_message(self):
        body = _extract_function_body(self.app_js, "batchDeleteOrders")
        runtime_toast = "showToast(`批量删除失败: ${firstFailureMessage || '请稍后重试'}`, 'danger');"

        self.assertNotIn("showToast('批量删除失败', 'danger');", body)
        self.assertIn("let firstFailureMessage = '';", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("firstFailureMessage = errorMessage || `HTTP ${response.status}`;", body)
        self.assertIn("firstFailureMessage = error.message || '请稍后重试';", body)
        self.assertIn(runtime_toast, body)

        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        fail_count_index = body.index("failCount++;", error_index)
        toast_index = body.index(runtime_toast)

        self.assertLess(
            error_index,
            fail_count_index,
            "订单批量删除遇到非 2xx 时先把后端错误体读出来，再决定记失败计数，别最后只剩一句笼统红字糊脸",
        )
        self.assertLess(
            body.find("if (actionRequestSequence !== orderMutationActionRequestSequence) {", error_index),
            fail_count_index,
            "同页已经发起新的批量删单动作时，旧失败响应读完错误文本后也别再回魂改失败统计",
        )
        self.assertLess(
            body.find("if (!isOrdersSectionActive()) {", error_index),
            fail_count_index,
            "都切出 orders 页面了，旧的批量删单失败响应读完错误文本也别再回来改失败统计",
        )
        self.assertLess(
            fail_count_index,
            toast_index,
            "订单批量删除全失败时得把记下来的首个真实错误带进最终 toast，别统计完又装聋作哑",
        )

    def test_order_batch_refresh_all_failures_surface_first_backend_or_runtime_error_message(self):
        body = _extract_function_body(self.app_js, "batchRefreshOrders")
        runtime_toast = "showToast(`批量刷新失败: ${firstFailureMessage || '请稍后重试'}`, 'danger');"
        reload_failure_toast = "showToast(`批量刷新失败: ${firstFailureMessage || '请稍后重试'}，且订单列表刷新失败，请稍后手动刷新`, 'warning');"

        self.assertIn("let firstFailureMessage = '';", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("firstFailureMessage = errorMessage || `HTTP ${response.status}`;", body)
        self.assertIn("firstFailureMessage = error.message || '请稍后重试';", body)
        self.assertIn(runtime_toast, body)
        self.assertIn(reload_failure_toast, body)
        self.assertIn("} else if (ordersLoaded === false) {", body)

        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        fail_count_index = body.index("failCount++;", error_index)
        toast_index = body.index(runtime_toast)
        reload_failure_index = body.index(reload_failure_toast)

        self.assertLess(
            error_index,
            fail_count_index,
            "订单批量刷新遇到非 2xx 时先把后端错误体读出来，再决定记失败计数，别最后只会报几个失败数糊人",
        )
        self.assertLess(
            body.find("if (actionRequestSequence !== orderMutationActionRequestSequence) {", error_index),
            fail_count_index,
            "同页已经发起新的批量刷新动作时，旧失败响应读完错误文本后也别再回魂改失败统计",
        )
        self.assertLess(
            body.find("if (!isOrdersSectionActive()) {", error_index),
            fail_count_index,
            "都切出 orders 页面了，旧的批量刷新失败响应读完错误文本也别再回来改失败统计",
        )
        self.assertLess(
            fail_count_index,
            toast_index,
            "订单批量刷新全失败时得把记下来的首个真实错误带进最终 toast，别统计完还装总结能力挺强",
        )
        self.assertLess(
            body.index("} else if (ordersLoaded === false) {"),
            reload_failure_index,
            "订单批量刷新 followup reload 只有真返回 false 时才该补那句列表刷新失败，别把 null/401 也硬当普通失败",
        )

    def test_order_batch_refresh_treats_http_200_business_failures_as_failures_instead_of_successes(self):
        body = _extract_function_body(self.app_js, "batchRefreshOrders")

        self.assertIn("const result = await response.json();", body)
        self.assertIn("if (result.success === false) {", body)
        self.assertIn("firstFailureMessage = result.message || '请稍后重试';", body)

        json_index = body.index("const result = await response.json();")
        success_false_index = body.index("if (result.success === false) {", json_index)
        fail_count_index = body.index("failCount++;", success_false_index)
        success_count_index = body.index("successCount++;", success_false_index)

        self.assertLess(
            json_index,
            success_false_index,
            "订单批量刷新遇到 200 响应时也得先看业务结果，别连 JSON 都不读就先自我感动算成功",
        )
        self.assertLess(
            success_false_index,
            fail_count_index,
            "后端 200 里明确回了 success false 时，前端得先记失败，别拿 HTTP 壳子当免死金牌",
        )
        self.assertLess(
            fail_count_index,
            success_count_index,
            "业务失败分支得挡在 successCount 前头，别先记成功再回头装作自己会看 result.success",
        )
        self.assertLess(
            body.find("if (actionRequestSequence !== orderMutationActionRequestSequence) {", json_index),
            success_false_index,
            "批量刷新读完 200 响应 JSON 后也得先验 action sequence，别旧请求回魂篡改成败统计",
        )
        self.assertLess(
            body.find("if (!isOrdersSectionActive()) {", json_index),
            success_false_index,
            "都切出 orders 页面了，旧的批量刷新 200 响应也别再回来改统计结果",
        )

    def test_order_mutation_failure_toasts_recheck_stale_state_after_error_body_read(self):
        delete_body = _extract_function_body(self.app_js, "deleteOrder")
        deliver_body = _extract_function_body(self.app_js, "manualDeliverOrder")
        refresh_body = _extract_function_body(self.app_js, "refreshOrderStatus")

        for body, error_fragment, toast_fragment, label in (
            (delete_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`删除失败: ${error}`, 'danger');", "删除订单"),
            (deliver_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`发货失败: ${error}`, 'danger');", "手动发货"),
            (refresh_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`刷新失败: ${error}`, 'danger');", "刷新订单状态"),
        ):
            with self.subTest(label=label):
                error_index = body.index(error_fragment)
                toast_index = body.index(toast_fragment)
                self.assertLess(
                    body.find("if (actionRequestSequence !== orderMutationActionRequestSequence) {", error_index),
                    toast_index,
                    f"同页都发起新的{label}动作了，旧失败响应读完错误文本也别回魂甩红字",
                )
                self.assertLess(
                    body.find("if (!isOrdersSectionActive()) {", error_index),
                    toast_index,
                    f"都切出 orders 页面了，旧的{label}失败响应读完错误文本也别跨页弹 danger toast",
                )

    def test_order_delete_failure_toast_rechecks_stale_state_after_error_body_read(self):
        body = _extract_function_body(self.app_js, "deleteOrder")
        error_text_index = body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        danger_toast_index = body.index("showToast(`删除失败: ${error}`, 'danger');")

        self.assertLess(
            body.find("if (actionRequestSequence !== orderMutationActionRequestSequence) {", error_text_index),
            danger_toast_index,
            "同页已经发起新的订单删除动作后，旧失败响应读完错误文本也别再回魂甩红字",
        )
        self.assertLess(
            body.find("if (!isOrdersSectionActive()) {", error_text_index),
            danger_toast_index,
            "都切出 orders 页面了，旧删除失败响应读完错误文本也别再跨页弹 danger toast",
        )

    def test_item_sync_buttons_do_not_depend_on_implicit_window_event(self):
        js_fragments = [
            "async function getAllItemsFromAccount(event) {",
            "async function getAllItemsFromAccountAll(event) {",
            "const button = event?.currentTarget || event?.target;",
        ]
        html_fragments = [
            'onclick="getAllItemsFromAccount(event)"',
            'onclick="getAllItemsFromAccountAll(event)"',
        ]

        for fragment in js_fragments:
            with self.subTest(js_fragment=fragment):
                self.assertIn(fragment, self.app_js)

        self.assertNotIn("const button = event.target;", self.app_js)

        for fragment in html_fragments:
            with self.subTest(html_fragment=fragment):
                self.assertIn(fragment, self.index_html)

    def test_account_cookie_helpers_do_not_depend_on_implicit_window_event(self):
        required_fragments = [
            "async function refreshRealCookie(accountId, event) {",
            "const button = event?.currentTarget || event?.target?.closest?.('button') || null;",
        ]

        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.app_js)

        self.assertNotIn("const button = event.target.closest('button');", self.app_js)

    def test_notification_channel_cards_cover_all_supported_channel_types(self):
        config_block_match = re.search(
            r"const channelTypeConfigs = \{(.*?)\n\};",
            self.app_js,
            re.S,
        )
        self.assertIsNotNone(config_block_match, "找不到 channelTypeConfigs 定义")

        supported_types = re.findall(
            r"^\s*([a-zA-Z_][\w-]*)\s*:\s*\{",
            config_block_match.group(1),
            re.M,
        )
        uncommented_html = re.sub(r"<!--.*?-->", "", self.index_html, flags=re.S)
        card_types = re.findall(
            r"showAddChannelModal\('([^']+)'\)",
            uncommented_html,
        )

        self.assertEqual(
            sorted(card_types),
            sorted(supported_types),
            f"通知渠道入口与支持类型不一致: cards={card_types}, supported={supported_types}",
        )

    def test_show_add_channel_modal_resets_previous_form_state(self):
        show_add_channel_modal_body = _extract_function_body(self.app_js, "showAddChannelModal")
        self.assertIn("const form = document.getElementById('addChannelForm');", show_add_channel_modal_body)
        self.assertIn("form.reset();", show_add_channel_modal_body)

    def test_message_notification_channel_select_does_not_expose_raw_channel_config(self):
        config_account_notification_body = _extract_function_body(self.app_js, "configAccountNotification")
        self.assertNotIn("option.textContent = `${channel.name} (${channel.config})`;", config_account_notification_body)
        self.assertIn("option.textContent = formatNotificationChannelSelectLabel(channel) + (channel.enabled ? '' : '（渠道已禁用）');", config_account_notification_body)

    def test_message_notification_config_requires_at_least_one_enabled_channel_before_opening_modal(self):
        body = _extract_function_body(self.app_js, "configAccountNotification")
        self.assertIn("const enabledChannels = channels.filter(channel => channel.enabled);", body)
        self.assertIn("if (selectableChannels.length === 0) {", body)
        self.assertIn("showToast(channels.length === 0 ? '请先添加通知渠道' : '请先启用至少一个通知渠道', 'warning');", body)
        self.assertIn("const selectableChannels = [...enabledChannels];", body)
        self.assertIn("selectableChannels.forEach(channel => {", body)
        self.assertNotIn("if (channel.enabled) {", body)

    def test_message_notification_config_modal_allows_disabled_current_channels_before_empty_state_warning(self):
        body = _extract_function_body(self.app_js, "configAccountNotification")
        self.assertIn("currentNotifications.forEach(notification => {", body)
        self.assertIn("if (selectableChannels.length === 0) {", body)
        self.assertLess(
            body.index("currentNotifications.forEach(notification => {"),
            body.index("if (selectableChannels.length === 0) {"),
            "账号通知配置弹窗得先把当前已绑定但全局禁用的渠道补回来，再决定是不是真要提示“请先启用至少一个通知渠道”",
        )

    def test_notification_channel_update_validates_dynamic_edit_fields_before_reading_values(self):
        body = _extract_function_body(self.app_js, "updateNotificationChannel")
        self.assertIn("const element = document.getElementById('edit_' + field.id);", body)
        self.assertIn("if (!element) {", body)
        self.assertIn("showToast(`找不到${field.label}输入框`, 'danger');", body)
        self.assertIn("hasError = true;", body)
        self.assertLess(
            body.index("if (!element) {"),
            body.index("const value = element.value.trim();"),
            "编辑通知渠道时动态字段都没渲染出来，就别直接 value.trim() 把页面干炸了",
        )

    def test_notification_channel_dynamic_fields_use_native_input_validity_before_submit(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_body = _extract_function_body(self.app_js, "updateNotificationChannel")

        for body, field_prefix in (
            (save_body, "add_"),
            (update_body, "edit_"),
        ):
            with self.subTest(field_prefix=field_prefix):
                self.assertIn("if (!element.checkValidity()) {", body)
                self.assertIn("if (typeof element.reportValidity === 'function') {", body)
                self.assertIn("element.reportValidity();", body)
                self.assertIn("showToast(`${field.label}格式无效，请检查后重试`, 'warning');", body)
                self.assertLess(
                    body.index("if (!element.checkValidity()) {"),
                    body.index("if (value) {"),
                    "动态渠道字段格式都不合法了，就别继续把脏值塞进配置对象里装正常",
                )

    def test_notification_channel_webhook_headers_require_valid_json_before_submit(self):
        save_body = _extract_function_body(self.app_js, "saveNotificationChannel")
        update_body = _extract_function_body(self.app_js, "updateNotificationChannel")

        for body in (save_body, update_body):
            self.assertIn("if (field.id === 'headers' && value) {", body)
            self.assertIn("JSON.parse(value);", body)
            self.assertIn("showToast(`${field.label}必须是合法JSON`, 'warning');", body)
            self.assertLess(
                body.index("if (field.id === 'headers' && value) {"),
                body.index("if (value) {"),
                "Webhook 自定义请求头都不是合法 JSON 了，就别继续保存成一坨脏字符串回头静默失效",
            )

    def test_auto_reply_primary_add_button_restores_full_default_label(self):
        self.assertIn(
            "const KEYWORD_ADD_BUTTON_DEFAULT_HTML = '<i class=\"bi bi-plus-lg\"></i>添加文本关键词';",
            self.app_js,
        )
        add_keyword_body = _extract_function_body(self.app_js, "addKeyword")
        cancel_edit_body = _extract_function_body(self.app_js, "cancelEdit")

        self.assertIn("setPrimaryKeywordAddButtonEditing(false);", add_keyword_body)
        self.assertIn("setPrimaryKeywordAddButtonEditing(false);", cancel_edit_body)
        self.assertNotIn("addBtn.innerHTML = '<i class=\"bi bi-plus-lg\"></i>添加';", add_keyword_body)
        self.assertNotIn("addBtn.innerHTML = '<i class=\"bi bi-plus-lg\"></i>添加';", cancel_edit_body)

    def test_auto_reply_keyword_edit_state_clears_group_editing_indices_on_reset_cancel_and_success(self):
        add_keyword_body = _extract_function_body(self.app_js, "addKeyword")
        cancel_edit_body = _extract_function_body(self.app_js, "cancelEdit")
        reset_body = _extract_function_body(self.app_js, "resetAutoReplyKeywordComposerState")

        self.assertIn("delete window.editingKeywordIndices;", add_keyword_body)
        self.assertIn("delete window.editingKeywordIndices;", cancel_edit_body)
        self.assertIn("delete window.editingKeywordIndices;", reset_body)

    def test_auto_reply_delayed_keyword_input_focus_requires_current_section_and_account_context(self):
        add_body = _extract_function_body(self.app_js, "addKeyword")
        edit_body = _extract_function_body(self.app_js, "editKeyword")

        add_delayed_block = add_body.split("setTimeout(() => {", 1)[1]
        self.assertIn("requestedAccountId !== currentAccountId", add_delayed_block)
        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", add_delayed_block)
        self.assertLess(
            add_delayed_block.index("requestedAccountId !== currentAccountId"),
            add_delayed_block.index("keywordInputEl.focus();"),
            "自动回复页都切走或账号切换了，添加关键词成功后的延迟聚焦不该再回来抢当前输入焦点",
        )

        self.assertIn("const requestedAccountId = currentAccountId;", edit_body)
        edit_delayed_block = edit_body.split("setTimeout(() => {", 1)[1]
        self.assertIn("requestedAccountId !== currentAccountId", edit_delayed_block)
        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", edit_delayed_block)
        self.assertLess(
            edit_delayed_block.index("requestedAccountId !== currentAccountId"),
            edit_delayed_block.index("keywordInput.focus();"),
            "自动回复页都切走或账号切换了，编辑关键词的延迟 focus/select 也不该再回来摸隐藏页输入框",
        )

    def test_auto_reply_immediate_focus_helpers_require_active_section(self):
        show_add_body = _extract_function_body(self.app_js, "showAddKeywordForm")
        focus_body = _extract_function_body(self.app_js, "focusKeywordInput")

        self.assertIn("document.getElementById('auto-reply-section')?.classList.contains('active')", show_add_body)
        self.assertLess(
            show_add_body.index("document.getElementById('auto-reply-section')?.classList.contains('active')"),
            show_add_body.index("document.getElementById('newKeyword').focus();"),
            "自动回复页都没活着时，showAddKeywordForm 不该还硬去抢隐藏页输入框焦点",
        )

        self.assertIn("document.getElementById('auto-reply-section')?.classList.contains('active')", focus_body)
        self.assertLess(
            focus_body.index("document.getElementById('auto-reply-section')?.classList.contains('active')"),
            focus_body.index("document.getElementById('newKeyword').focus();"),
            "自动回复页都没活着时，focusKeywordInput 也不该继续摸隐藏页输入框",
        )

    def test_auto_reply_keyword_loader_ignores_stale_async_account_switches(self):
        self.assertIn("let autoReplyKeywordsRequestSequence = 0;", self.app_js)
        load_body = _extract_function_body(self.app_js, "loadAccountKeywords")
        refresh_body = _extract_function_body(self.app_js, "refreshKeywordsList")

        self.assertIn("const requestSequence = ++autoReplyKeywordsRequestSequence;", load_body)
        self.assertIn("const requestedAccountId = accountId;", load_body)
        self.assertIn("requestSequence !== autoReplyKeywordsRequestSequence", load_body)
        self.assertIn("document.getElementById('accountSelect').value !== requestedAccountId", load_body)

        self.assertIn("const requestedAccountId = currentAccountId;", refresh_body)
        self.assertIn("requestSequence !== autoReplyKeywordsRequestSequence", refresh_body)
        self.assertIn("currentAccountId !== requestedAccountId", refresh_body)

    def test_auto_reply_keyword_loader_resets_stale_keyword_management_before_fetching_new_account(self):
        reset_body = _extract_function_body(self.app_js, "resetAutoReplyKeywordComposerState")
        load_body = _extract_function_body(self.app_js, "loadAccountKeywords")

        self.assertIn("keywordManagement.style.display = 'none';", reset_body)
        self.assertIn("renderKeywordsList([]);", reset_body)
        self.assertIn("selectElement.innerHTML = '<option value=\"\">选择商品或留空表示通用关键词</option>';", reset_body)
        self.assertIn("delete window.editingKeywordIndices;", reset_body)
        self.assertIn("delete window.editingIndex;", reset_body)
        self.assertIn("setPrimaryKeywordAddButtonEditing(false);", reset_body)
        self.assertIn("cancelBtn.remove();", reset_body)

        reset_index = load_body.find("resetAutoReplyKeywordComposerState();", load_body.index("currentAccountId = accountId;"))
        self.assertGreater(
            reset_index,
            load_body.index("currentAccountId = accountId;"),
            "切换自动回复账号时要先把旧关键词面板和编辑态收干净，别让 A 的残影挂着给 B 当替身",
        )
        self.assertLess(
            reset_index,
            load_body.index("const accountResponse = await fetch(`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`, {"),
            "自动回复切账号时先清旧关键词面板再发新请求，别让旧 DOM 继续顶着新 currentAccountId 瞎操作",
        )

    def test_auto_reply_keyword_refresh_failure_paths_ignore_stale_account_switches_before_toasting(self):
        body = _extract_function_body(self.app_js, "refreshKeywordsList")
        error_branch = body.split("} else {", 1)[1].split("} catch (error) {", 1)[0]
        catch_branch = body.split("} catch (error) {", 1)[1]

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", error_branch)
        self.assertIn("requestSequence !== autoReplyKeywordsRequestSequence", error_branch)
        self.assertIn("currentAccountId !== requestedAccountId", error_branch)
        self.assertLess(
            error_branch.find("requestSequence !== autoReplyKeywordsRequestSequence",
                              error_branch.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")),
            error_branch.index("throw new Error(errorMessage);"),
            "旧的关键词刷新失败响应读完错误体后，账号都切走了就别再回来抛旧错误污染当前页面流程",
        )

        self.assertIn("const errorMessage = error?.message || error || '请稍后重试';", catch_branch)
        self.assertIn("requestSequence !== autoReplyKeywordsRequestSequence", catch_branch)
        self.assertIn("currentAccountId !== requestedAccountId", catch_branch)
        self.assertLess(
            catch_branch.index("requestSequence !== autoReplyKeywordsRequestSequence"),
            catch_branch.index("showToast(`刷新关键词列表失败: ${errorMessage}`, 'danger');"),
            "旧的关键词刷新异常回调不该在账号已经切走后还回来对着新账号页面弹旧 toast",
        )

    def test_auto_reply_keyword_loader_stops_before_keyword_fetch_when_account_status_probe_turns_stale(self):
        body = _extract_function_body(self.app_js, "loadAccountKeywords")

        account_fetch_index = body.index("const accountResponse = await fetch(`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`, {")
        keyword_fetch_index = body.index("const response = await fetch(`${apiBase}/keywords-with-item-id/${encodedAccountId}`, {")
        stale_guard_after_account_probe = body.find("requestSequence !== autoReplyKeywordsRequestSequence", account_fetch_index)

        self.assertGreater(
            stale_guard_after_account_probe,
            account_fetch_index,
            "自动回复关键词加载拿到账户状态探测结果后，先确认请求还活着，再决定要不要继续请求关键词列表",
        )
        self.assertLess(
            stale_guard_after_account_probe,
            keyword_fetch_index,
            "旧的自动回复关键词加载请求不该在账号状态探测这一步已经 stale 后还继续打关键词接口",
        )

    def test_auto_reply_keyword_loader_also_checks_stale_path_when_account_status_probe_is_not_ok(self):
        body = _extract_function_body(self.app_js, "loadAccountKeywords")

        account_status_branch_end = body.index("const response = await fetch(`${apiBase}/keywords-with-item-id/${encodedAccountId}`, {")
        last_guard_before_keyword_fetch = body.rfind("requestSequence !== autoReplyKeywordsRequestSequence", 0, account_status_branch_end)
        self.assertGreater(
            last_guard_before_keyword_fetch,
            body.index("let accountStatus = true; // 默认启用"),
            "就算账号状态探测接口没返回 200，自动回复关键词加载在继续打关键词接口前也得先验一遍 stale",
        )
        stale_guard_occurrences = len(re.findall("requestSequence !== autoReplyKeywordsRequestSequence", body[:account_status_branch_end]))
        self.assertGreaterEqual(
            stale_guard_occurrences,
            2,
            "自动回复关键词加载在账号状态探测阶段前后至少要有两道 stale 校验，别让旧请求越过分支继续打关键词接口",
        )

    def test_auto_reply_keyword_item_select_loader_resets_stale_options_before_fetch_and_on_failure(self):
        body = _extract_function_body(self.app_js, "loadItemsList")

        self.assertIn("const selectElement = document.getElementById('newItemIdSelect');", body)
        self.assertIn("selectElement.innerHTML = '<option value=\"\">选择商品或留空表示通用关键词</option>';",
                      body)
        self.assertLess(
            body.index("selectElement.innerHTML = '<option value=\"\">选择商品或留空表示通用关键词</option>';"),
            body.index("const response = await fetch(`${apiBase}/items/account/${encodeURIComponent(accountId)}`, {"),
            "文本关键词商品下拉重新加载前应先清空旧选项，失败时别挂着上次账号的商品装正常",
        )
        self.assertIn("selectElement.innerHTML = '<option value=\"\">商品列表加载失败，请稍后重试</option>';",
                      body)
        self.assertIn("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');", body)
        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", body)
        self.assertLess(
            body.rfind("!document.getElementById('auto-reply-section')?.classList.contains('active')", 0, body.index("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');")),
            body.index("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');"),
            "都切出自动回复页了，旧的商品下拉失败就别再跨页甩 danger toast 了",
        )

    def test_auto_reply_keyword_loader_treats_item_select_failure_as_overall_reload_failure(self):
        body = _extract_function_body(self.app_js, "loadAccountKeywords")

        items_load_index = body.index("const itemsLoaded = await loadItemsList(accountId, { requestSequence });")
        failed_child_guard_index = body.index("if (itemsLoaded !== true) {", items_load_index)
        badge_update_index = body.index("updateAccountBadge(accountId, accountStatus);")
        return_true_index = body.index("return true;")

        self.assertLess(
            items_load_index,
            failed_child_guard_index,
            "自动回复关键词主加载得先拿到商品下拉加载结果，再决定是不是能继续往成功态走",
        )
        self.assertLess(
            failed_child_guard_index,
            badge_update_index,
            "商品下拉都刷挂了，就别还更新账号徽章、撑开管理面板装整套关键词都加载成功",
        )
        self.assertLess(
            failed_child_guard_index,
            return_true_index,
            "商品下拉子加载失败时，loadAccountKeywords 应该回 false，别让导入等后续成功提示瞎报喜",
        )

    def test_auto_reply_keyword_requests_encode_account_ids_in_path_segments(self):
        load_body = _extract_function_body(self.app_js, "loadAccountKeywords")
        refresh_body = _extract_function_body(self.app_js, "refreshKeywordsList")
        add_body = _extract_function_body(self.app_js, "addKeyword")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        delete_specific_keyword_body = _extract_function_body(self.app_js, "deleteSpecificKeyword")
        delete_specific_item_body = _extract_function_body(self.app_js, "deleteSpecificItem")
        import_body = _extract_function_body(self.app_js, "importKeywords")
        export_body = _extract_function_body(self.app_js, "exportKeywords")

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", load_body)
        self.assertIn("fetch(`${apiBase}/keywords-with-item-id/${encodedAccountId}`", load_body)
        self.assertNotIn("fetch(`${apiBase}/keywords-with-item-id/${accountId}`", load_body)

        self.assertIn("const encodedRequestedAccountId = encodeURIComponent(requestedAccountId);", refresh_body)
        self.assertIn("fetch(`${apiBase}/keywords-with-item-id/${encodedRequestedAccountId}`", refresh_body)
        self.assertNotIn("fetch(`${apiBase}/keywords-with-item-id/${requestedAccountId}`", refresh_body)

        for body, account_var in (
            (add_body, "currentAccountId"),
            (save_group_body, "currentAccountId"),
            (import_body, "currentAccountId"),
            (export_body, "currentAccountId"),
        ):
            with self.subTest(function_body=account_var):
                self.assertIn("const encodedCurrentAccountId = encodeURIComponent(currentAccountId);", body)

        self.assertIn("fetch(`${apiBase}/keywords-with-item-id/${encodedCurrentAccountId}`", add_body)
        self.assertIn("fetch(`${apiBase}/keywords/${encodedCurrentAccountId}/image-batch`", add_image_body)
        self.assertIn("fetch(`${apiBase}/keywords-with-item-id/${encodedCurrentAccountId}`", save_group_body)
        self.assertIn("fetch(`${apiBase}/keywords-import/${encodedCurrentAccountId}`", import_body)
        self.assertIn("fetch(`${apiBase}/keywords-export/${encodedCurrentAccountId}`", export_body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${currentAccountId}/image-batch`", add_image_body)

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", delete_body)
        self.assertIn("fetch(`${apiBase}/keywords/${encodedAccountId}/${index}`", delete_body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${accountId}/${index}`", delete_body)

        self.assertIn("fetch(`${apiBase}/keywords/${encodedCurrentAccountId}/${index}`", delete_specific_keyword_body)
        self.assertIn("fetch(`${apiBase}/keywords/${encodedCurrentAccountId}/${index}`", delete_specific_item_body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${currentAccountId}/${index}`", delete_specific_keyword_body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${currentAccountId}/${index}`", delete_specific_item_body)

    def test_auto_reply_keyword_mutations_only_report_success_when_followup_reload_succeeds(self):
        add_body = _extract_function_body(self.app_js, "addKeyword")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        delete_specific_keyword_body = _extract_function_body(self.app_js, "deleteSpecificKeyword")
        delete_specific_item_body = _extract_function_body(self.app_js, "deleteSpecificItem")
        import_body = _extract_function_body(self.app_js, "importKeywords")

        self.assertIn("const keywordsLoaded = await refreshKeywordsList();", add_body)
        self.assertIn("if (keywordsLoaded) {", add_body)
        self.assertIn("showToast(`✨ ${keywordText} ${actionText}成功！（共${totalAdded}条配置，应用于${itemText}）`, 'success');", add_body)
        self.assertIn("showToast(`${keywordText} ${actionText}成功，但关键词列表刷新失败，请稍后手动刷新`, 'warning');", add_body)

        self.assertIn("const keywordsLoaded = await refreshKeywordsList();", add_image_body)
        self.assertIn("if (keywordsLoaded) {", add_image_body)
        self.assertIn("showToast(`✨ ${keywordText} 添加成功！（共${totalCount}条配置，应用于${itemText}）`, 'success');", add_image_body)
        self.assertIn("showToast(`${keywordText} 添加成功，但关键词列表刷新失败，请稍后手动刷新`, 'warning');", add_image_body)
        self.assertIn("showToast(`⚠️ 部分添加成功：成功${successCount}条，失败${failCount}条`, 'warning');", add_image_body)
        self.assertIn("showToast(`⚠️ 部分添加成功：成功${successCount}条，失败${failCount}条，但关键词列表刷新失败，请稍后手动刷新`, 'warning');", add_image_body)

        self.assertIn("const keywordsLoaded = await refreshKeywordsList();", save_group_body)
        self.assertIn("if (keywordsLoaded) {", save_group_body)
        self.assertIn("showToast(`回复内容已更新（影响${group.indices.length}条配置）`, 'success');", save_group_body)
        self.assertIn("showToast('回复内容已更新，但关键词列表刷新失败，请稍后手动刷新', 'warning');", save_group_body)

        self.assertIn("const keywordsLoaded = await refreshKeywordsList();", delete_body)
        self.assertIn("if (keywordsLoaded) {", delete_body)
        self.assertIn("showToast('关键词删除成功', 'success');", delete_body)
        self.assertIn("showToast('关键词删除成功，但关键词列表刷新失败，请稍后手动刷新', 'warning');", delete_body)

        self.assertIn("const keywordsLoaded = await refreshKeywordsList();", delete_specific_keyword_body)
        self.assertIn("showToast(`✅ 关键词 \"${targetKeyword}\" 已删除（${indicesToDelete.length}条配置）`, 'success');", delete_specific_keyword_body)
        self.assertIn("showToast(`关键词 \"${targetKeyword}\" 已删除，但关键词列表刷新失败，请稍后手动刷新`, 'warning');", delete_specific_keyword_body)

        self.assertIn("const keywordsLoaded = await refreshKeywordsList();", delete_specific_item_body)
        self.assertIn("showToast(`✅ ${itemName} 的配置已删除（${indicesToDelete.length}条）`, 'success');", delete_specific_item_body)
        self.assertIn("showToast(`${itemName} 的配置已删除，但关键词列表刷新失败，请稍后手动刷新`, 'warning');", delete_specific_item_body)
        self.assertIn("const targetConfigCount = Array.isArray(targetItem.indices) ? targetItem.indices.length : 0;", delete_specific_item_body)
        self.assertIn("将删除该商品下的 ${targetConfigCount} 条配置。", delete_specific_item_body)
        self.assertNotIn("将删除该商品下的 ${group.keywords.length} 个关键词。", delete_specific_item_body)

        self.assertIn("const keywordsLoaded = await loadAccountKeywords();", import_body)
        self.assertIn("if (keywordsLoaded) {", import_body)
        self.assertIn("showToast(`导入成功！新增: ${result.added}, 更新: ${result.updated}`, 'success');", import_body)
        self.assertIn("showToast(`导入成功！新增: ${result.added}, 更新: ${result.updated}，但关键词列表刷新失败，请稍后手动刷新`, 'warning');", import_body)

    def test_auto_reply_image_keyword_all_failures_surface_duplicate_details_before_generic_fallback(self):
        body = _extract_function_body(self.app_js, "addImageKeyword")

        self.assertIn("const duplicateFailures = Array.isArray(result.duplicates) ? result.duplicates.filter(Boolean) : [];", body)
        self.assertIn("} else if (duplicateFailures.length > 0) {", body)
        self.assertIn("showToast(`以下关键词已存在：\\n${duplicateFailures.join('\\n')}\\n请修改后重试`, 'warning');", body)
        self.assertIn("const failureMessage = result.message || result.msg || `所有图片关键词添加失败（失败${failCount}条）`;", body)
        self.assertIn("showToast(`❌ ${failureMessage}`, 'danger');", body)
        self.assertLess(
            body.index("} else if (duplicateFailures.length > 0) {"),
            body.index("const failureMessage = result.message || result.msg || `所有图片关键词添加失败（失败${failCount}条）`;"),
            "图片关键词全量失败时先把后端返回的重复明细吐给用户，别一上来就端个没信息量的通用红字",
        )

    def test_auto_reply_image_keyword_partial_success_surfaces_duplicate_details_when_available(self):
        body = _extract_function_body(self.app_js, "addImageKeyword")

        partial_failure_branch_index = body.index("} else {", body.index("if (failCount === 0) {"))
        partial_duplicate_index = body.index("if (duplicateFailures.length > 0) {", partial_failure_branch_index)
        generic_partial_toast_index = body.index("showToast(`⚠️ 部分添加成功：成功${successCount}条，失败${failCount}条`, 'warning');")

        self.assertIn("showToast(`⚠️ 部分添加成功：成功${successCount}条，失败${failCount}条\\n以下关键词已存在：\\n${duplicateFailures.join('\\n')}\\n请修改后重试`, 'warning');", body)
        self.assertIn("showToast(`⚠️ 部分添加成功：成功${successCount}条，失败${failCount}条\\n以下关键词已存在：\\n${duplicateFailures.join('\\n')}\\n请修改后重试\\n关键词列表刷新失败，请稍后手动刷新`, 'warning');", body)
        self.assertLess(
            partial_duplicate_index,
            generic_partial_toast_index,
            "图片关键词都部分成功了，后端还把重复明细带回来了，就别只会甩个成功几条失败几条的空心 warning",
        )

    def test_auto_reply_import_modal_hidden_invalidates_pending_session_and_resets_form_state(self):
        show_modal_body = _extract_function_body(self.app_js, "showImportModal")
        reset_body = _extract_function_body(self.app_js, "resetImportKeywordsModalState")

        self.assertIn("let importKeywordsModalRequestSequence = 0;", self.app_js)
        self.assertIn("const modalElement = document.getElementById('importKeywordsModal');", show_modal_body)
        self.assertIn("if (modalElement && modalElement.dataset.importKeywordsModalBound !== 'true') {", show_modal_body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", show_modal_body)
        self.assertIn("importKeywordsModalRequestSequence += 1;", show_modal_body)
        self.assertIn("resetImportKeywordsModalState();", show_modal_body)
        self.assertIn("modalElement.dataset.importKeywordsModalBound = 'true';", show_modal_body)
        self.assertLess(
            show_modal_body.index("modalElement.addEventListener('hidden.bs.modal', () => {"),
            show_modal_body.index("modal.show();"),
            "导入关键词弹窗关闭后也得废掉当前 modal 会话，别让旧请求回来继续回刷列表和弹 toast",
        )

        self.assertIn("const fileInput = document.getElementById('importFileInput');", reset_body)
        self.assertIn("fileInput.value = '';", reset_body)
        self.assertIn("const progressDiv = document.getElementById('importProgress');", reset_body)
        self.assertIn("progressDiv.style.display = 'none';", reset_body)
        self.assertIn("progressBar.style.width = '0%';", reset_body)

    def test_import_keywords_respects_modal_session_before_hiding_or_toasting(self):
        import_body = _extract_function_body(self.app_js, "importKeywords")

        self.assertIn("const modalRequestSequence = importKeywordsModalRequestSequence;", import_body)
        self.assertIn("modalRequestSequence !== importKeywordsModalRequestSequence", import_body)
        self.assertIn("modalElement.dataset.importKeywordsModalIgnoreNextHidden = 'true';", import_body)
        self.assertIn("resetImportKeywordsModalState();", import_body)
        self.assertLess(
            import_body.index("modalRequestSequence !== importKeywordsModalRequestSequence"),
            import_body.index("modal.hide();"),
            "旧的关键词导入响应不该在弹窗已经关掉或重开后，还回来把当前 modal 会话又关掉并弹成功提示",
        )

    def test_auto_reply_image_keyword_save_respects_modal_session_before_hiding_or_toasting(self):
        show_modal_body = _extract_function_body(self.app_js, "showAddImageKeywordModal")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")

        self.assertIn("let imageKeywordModalRequestSequence = 0;", self.app_js)
        self.assertIn("imageKeywordModalRequestSequence += 1;", show_modal_body)
        self.assertIn("const requestSequence = imageKeywordModalRequestSequence;", add_image_body)
        self.assertIn("requestSequence !== imageKeywordModalRequestSequence", add_image_body)
        self.assertIn("modalElement.dataset.imageKeywordModalIgnoreNextHidden = 'true';", add_image_body)
        self.assertLess(
            add_image_body.index("requestSequence !== imageKeywordModalRequestSequence"),
            add_image_body.index("modal.hide();"),
            "旧的图片关键词保存响应不该回来把已经重开的新增图片关键词弹窗又关掉",
        )
        self.assertLess(
            add_image_body.find(
                "requestSequence !== imageKeywordModalRequestSequence",
                add_image_body.index("if (handleUnauthorizedApiResponse(uploadResponse)) {"),
            ),
            add_image_body.index("const batchResponse = await fetch(`${apiBase}/keywords/${encodedCurrentAccountId}/image-batch`, {"),
            "图片关键词弹窗会话都切了，旧上传结果就别再继续往后打批量保存接口了",
        )

    def test_text_keyword_mutations_do_not_reference_image_keyword_modal_session_state(self):
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_specific_keyword_body = _extract_function_body(self.app_js, "deleteSpecificKeyword")

        self.assertNotIn("requestSequence !== imageKeywordModalRequestSequence", save_group_body)
        self.assertNotIn("requestSequence !== imageKeywordModalRequestSequence", delete_specific_keyword_body)
        self.assertNotIn("imageKeywordModalRequestSequence", save_group_body)
        self.assertNotIn("imageKeywordModalRequestSequence", delete_specific_keyword_body)

    def test_auto_reply_keyword_mutations_do_not_emit_cross_page_toasts_after_leaving_section(self):
        add_body = _extract_function_body(self.app_js, "addKeyword")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        delete_specific_keyword_body = _extract_function_body(self.app_js, "deleteSpecificKeyword")
        delete_specific_item_body = _extract_function_body(self.app_js, "deleteSpecificItem")

        for body, success_fragment in (
            (add_body, "showToast(`✨ ${keywordText} ${actionText}成功！（共${totalAdded}条配置，应用于${itemText}）`, 'success');"),
            (add_image_body, "showToast(`✨ ${keywordText} 添加成功！（共${totalCount}条配置，应用于${itemText}）`, 'success');"),
            (save_group_body, "showToast(`回复内容已更新（影响${group.indices.length}条配置）`, 'success');"),
            (delete_body, "showToast('关键词删除成功', 'success');"),
            (delete_specific_keyword_body, "showToast(`✅ 关键词 \"${targetKeyword}\" 已删除（${indicesToDelete.length}条配置）`, 'success');"),
            (delete_specific_item_body, "showToast(`✅ ${itemName} 的配置已删除（${indicesToDelete.length}条）`, 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "自动回复关键词操作在离开页面后不该再跨页弹 success toast",
                )

    def test_auto_reply_import_and_export_do_not_emit_cross_page_toasts_after_leaving_section(self):
        import_body = _extract_function_body(self.app_js, "importKeywords")
        export_body = _extract_function_body(self.app_js, "exportKeywords")

        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", import_body)
        self.assertIn("return null;", import_body)
        self.assertLess(
            import_body.index("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
            import_body.index("showToast(`导入成功！新增: ${result.added}, 更新: ${result.updated}`, 'success');"),
            "自动回复关键词导入在离开页面后不该再跨页弹成功 toast",
        )

        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", export_body)
        self.assertIn("return null;", export_body)
        self.assertLess(
            export_body.index("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
            export_body.index("showToast('✅ 关键词导出成功', 'success');"),
            "自动回复关键词导出在离开页面后不该再跨页弹成功 toast",
        )

    def test_auto_reply_keyword_mutations_ignore_older_same_page_responses(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        add_body = _extract_function_body(self.app_js, "addKeyword")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        delete_specific_keyword_body = _extract_function_body(self.app_js, "deleteSpecificKeyword")
        delete_specific_item_body = _extract_function_body(self.app_js, "deleteSpecificItem")
        import_body = _extract_function_body(self.app_js, "importKeywords")
        export_body = _extract_function_body(self.app_js, "exportKeywords")

        self.assertIn("let autoReplyKeywordActionRequestSequence = 0;", self.app_js)
        self.assertIn("autoReplyKeywordActionRequestSequence += 1;", show_section_body)

        for body, anchor_fragment, success_fragment in (
            (
                add_body,
                "keywordInputEl.value = '';",
                "showToast(`✨ ${keywordText} ${actionText}成功！（共${totalAdded}条配置，应用于${itemText}）`, 'success');",
            ),
            (
                add_image_body,
                "const keywordsLoaded = await refreshKeywordsList();",
                "showToast(`✨ ${keywordText} 添加成功！（共${totalCount}条配置，应用于${itemText}）`, 'success');",
            ),
            (
                save_group_body,
                "const keywordsLoaded = await refreshKeywordsList();",
                "showToast(`回复内容已更新（影响${group.indices.length}条配置）`, 'success');",
            ),
            (
                delete_body,
                "const keywordsLoaded = await refreshKeywordsList();",
                "showToast('关键词删除成功', 'success');",
            ),
            (
                delete_specific_keyword_body,
                "const keywordsLoaded = await refreshKeywordsList();",
                "showToast(`✅ 关键词 \"${targetKeyword}\" 已删除（${indicesToDelete.length}条配置）`, 'success');",
            ),
            (
                delete_specific_item_body,
                "const keywordsLoaded = await refreshKeywordsList();",
                "showToast(`✅ ${itemName} 的配置已删除（${indicesToDelete.length}条）`, 'success');",
            ),
            (
                import_body,
                "const keywordsLoaded = await loadAccountKeywords();",
                "showToast(`导入成功！新增: ${result.added}, 更新: ${result.updated}`, 'success');",
            ),
            (
                export_body,
                "const blob = await response.blob();",
                "showToast('✅ 关键词导出成功', 'success');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== autoReplyKeywordActionRequestSequence", body)
                self.assertLess(
                    body.index("actionRequestSequence !== autoReplyKeywordActionRequestSequence"),
                    body.index(anchor_fragment),
                    "同页已经发起了新的自动回复关键词动作，旧响应不该再回来继续操作当前会话",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== autoReplyKeywordActionRequestSequence", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "同页已经发起了新的自动回复关键词动作，旧成功响应别再回来刷 success toast",
                )

        self.assertIn("const requestedAccountId = currentAccountId;", add_body)
        self.assertLess(
            add_body.index("requestedAccountId !== currentAccountId"),
            add_body.index("keywordInputEl.value = '';"),
            "账号都切走了，旧的关键词保存响应不该再回来把当前输入框清空",
        )

        self.assertIn("const requestedAccountId = currentAccountId;", add_image_body)
        self.assertLess(
            add_image_body.index("requestedAccountId !== currentAccountId"),
            add_image_body.index("const keywordsLoaded = await refreshKeywordsList();"),
            "账号都切走了，旧的图片关键词响应不该再回来刷新当前关键词列表",
        )

        self.assertIn("const requestedAccountId = currentAccountId;", save_group_body)
        self.assertLess(
            save_group_body.index("requestedAccountId !== currentAccountId"),
            save_group_body.index("const keywordsLoaded = await refreshKeywordsList();"),
            "账号都切走了，旧的分组回复保存响应不该再回来刷新当前关键词列表",
        )

        self.assertIn("const requestedAccountId = accountId;", delete_body)
        self.assertIn("const requestedAccountId = currentAccountId;", delete_specific_keyword_body)
        self.assertIn("const requestedAccountId = currentAccountId;", delete_specific_item_body)
        self.assertIn("const requestedAccountId = currentAccountId;", import_body)
        self.assertIn("const requestedAccountId = currentAccountId;", export_body)

        self.assertLess(
            import_body.index("actionRequestSequence !== autoReplyKeywordActionRequestSequence"),
            import_body.index("resetImportKeywordsModalState();"),
            "同页已经发起了新的导入动作，旧导入回调不该再回来清空当前导入进度和文件选择",
        )
        self.assertLess(
            import_body.rfind("actionRequestSequence !== autoReplyKeywordActionRequestSequence", 0, import_body.index("showToast(`导入失败: ${error}`, 'danger');")),
            import_body.index("showToast(`导入失败: ${error}`, 'danger');"),
            "同页已经发起了新的导入动作，旧失败响应不该再回来甩旧错误",
        )
        self.assertLess(
            export_body.rfind("actionRequestSequence !== autoReplyKeywordActionRequestSequence", 0, export_body.index("showToast(`导出关键词失败: ${error}`, 'danger');")),
            export_body.index("showToast(`导出关键词失败: ${error}`, 'danger');"),
            "同页已经发起了新的导出动作，旧失败响应读完错误文本后不该再回来甩红字",
        )

    def test_auto_reply_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        refresh_accounts_body = _extract_function_body(self.app_js, "refreshAccountList")
        refresh_keywords_body = _extract_function_body(self.app_js, "refreshKeywordsList")
        load_keywords_body = _extract_function_body(self.app_js, "loadAccountKeywords")
        load_items_body = _extract_function_body(self.app_js, "loadItemsList")
        image_load_items_body = _extract_function_body(self.app_js, "loadItemsListForImageKeyword")
        add_body = _extract_function_body(self.app_js, "addKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        import_body = _extract_function_body(self.app_js, "importKeywords")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        export_body = _extract_function_body(self.app_js, "exportKeywords")

        for body, unauthorized_fragment, anchor_fragment in (
            (refresh_accounts_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (refresh_accounts_body, "if (handleUnauthorizedApiResponse(keywordsResponse)) {", "const accountsWithKeywords = accounts.map(account => {"),
            (refresh_keywords_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (load_keywords_body, "if (handleUnauthorizedApiResponse(accountResponse)) {", "let accountStatus = true; // 默认启用"),
            (load_keywords_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (load_items_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (image_load_items_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (add_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (save_group_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (delete_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (import_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (add_image_body, "if (handleUnauthorizedApiResponse(uploadResponse)) {", "if (!uploadResponse.ok) {"),
            (add_image_body, "if (handleUnauthorizedApiResponse(batchResponse)) {", "if (batchResponse.ok) {"),
            (export_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "自动回复这些 raw fetch 遇到 401 得先滚去登录，别后面还继续拉账号、刷关键词、导入导出、改配置",
                )

    def test_auto_reply_failure_actions_read_structured_error_messages(self):
        add_body = _extract_function_body(self.app_js, "addKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        import_body = _extract_function_body(self.app_js, "importKeywords")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        export_body = _extract_function_body(self.app_js, "exportKeywords")

        for body, error_fragment, toast_fragment, label in (
            (add_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`❌ ${error}`, 'danger');", "文本关键词保存"),
            (save_group_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`更新回复内容失败: ${error}`, 'danger');", "分组回复更新"),
            (delete_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`关键词删除失败: ${error}`, 'danger');", "文本关键词删除"),
            (import_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`导入失败: ${error}`, 'danger');", "关键词导入"),
            (add_image_body, "const uploadError = await readResponseErrorMessage(uploadResponse, `HTTP ${uploadResponse.status}`);", "showToast(`❌ 图片上传失败: ${uploadError}`, 'danger');", "图片上传"),
            (add_image_body, "const error = await readResponseErrorMessage(batchResponse, `HTTP ${batchResponse.status}`);", "showToast(`❌ 添加图片关键词失败: ${error}`, 'danger');", "图片关键词批量保存"),
            (export_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`导出关键词失败: ${error}`, 'danger');", "关键词导出"),
        ):
            with self.subTest(label=label):
                self.assertIn(error_fragment, body)
                self.assertLess(
                    body.index(error_fragment),
                    body.index(toast_fragment),
                    f"{label}失败时先统一解析错误体，别再拿裸 JSON / text 瞎糊用户",
                )

        self.assertNotIn("const errorData = await response.json();", add_body)
        self.assertNotIn("const errorText = await response.text();", add_body)

    def test_auto_reply_keyword_loaders_read_structured_error_messages(self):
        refresh_body = _extract_function_body(self.app_js, "refreshKeywordsList")
        load_body = _extract_function_body(self.app_js, "loadAccountKeywords")
        load_items_body = _extract_function_body(self.app_js, "loadItemsList")
        refresh_accounts_body = _extract_function_body(self.app_js, "refreshAccountList")
        image_load_items_body = _extract_function_body(self.app_js, "loadItemsListForImageKeyword")

        for body, error_fragment, anchor_fragment, label in (
            (refresh_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "throw new Error(errorMessage);", "关键词刷新"),
            (load_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "throw new Error(errorMessage);", "账号关键词加载"),
            (load_items_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "throw new Error(errorMessage);", "关键词商品下拉加载"),
            (refresh_accounts_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "throw new Error(errorMessage);", "自动回复账号列表刷新"),
            (image_load_items_body, "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "throw new Error(errorMessage);", "图片关键词商品下拉加载"),
        ):
            with self.subTest(label=label):
                self.assertIn(error_fragment, body)
                self.assertLess(
                    body.index(error_fragment),
                    body.index(anchor_fragment),
                    f"{label}失败时先统一解析错误体，别拿个裸状态码就糊弄过去",
                )

        self.assertIn("showToast(`刷新关键词列表失败: ${errorMessage}`, 'danger');", refresh_body)
        self.assertIn("showToast(`加载关键词失败: ${errorMessage}`, 'danger');", load_body)
        self.assertIn("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');", load_items_body)
        self.assertIn("showToast(`刷新账号列表失败: ${errorMessage}`, 'danger');", refresh_accounts_body)
        self.assertIn("showToast(`加载商品列表失败: ${errorMessage}`, 'danger');", image_load_items_body)

    def test_auto_reply_batch_delete_actions_handle_unauthorized_and_surface_backend_errors(self):
        delete_specific_keyword_body = _extract_function_body(self.app_js, "deleteSpecificKeyword")
        delete_specific_item_body = _extract_function_body(self.app_js, "deleteSpecificItem")

        for body, toast_fragment, label in (
            (delete_specific_keyword_body, "showToast(`删除关键词失败: ${errorMessage}`, 'danger');", "批量删关键词"),
            (delete_specific_item_body, "showToast(`删除商品配置失败: ${errorMessage}`, 'danger');", "批量删商品配置"),
        ):
            with self.subTest(label=label):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index("if (!response.ok) {"),
                    "自动回复批量删除遇到 401 得先滚回登录，别后面还继续抛删配置错误吓人",
                )
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                throw_index = body.index("throw new Error(errorMessage);", error_index)
                self.assertLess(
                    body.find("requestedAccountId !== currentAccountId", error_index),
                    throw_index,
                    "账号都切走了，旧的批量删除失败响应读完错误体也别回来抛旧错误",
                )
                self.assertLess(
                    body.find("actionRequestSequence !== autoReplyKeywordActionRequestSequence", error_index),
                    throw_index,
                    "同页已经发起新的批量删除动作后，旧失败响应读完错误体也别回来抛旧错误",
                )
                self.assertIn("const errorMessage = error?.message || error || '请稍后重试';", body)
                self.assertIn(toast_fragment, body)

    def test_auto_reply_mutation_catch_toasts_surface_runtime_errors(self):
        add_body = _extract_function_body(self.app_js, "addKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        import_body = _extract_function_body(self.app_js, "importKeywords")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        export_body = _extract_function_body(self.app_js, "exportKeywords")

        for body, toast_fragment, guard_fragment, label in (
            (add_body, "showToast(`添加关键词失败: ${errorMessage}`, 'danger');", "actionRequestSequence !== autoReplyKeywordActionRequestSequence", "文本关键词保存"),
            (save_group_body, "showToast(`更新回复内容失败: ${errorMessage}`, 'danger');", "actionRequestSequence !== autoReplyKeywordActionRequestSequence", "分组回复更新"),
            (delete_body, "showToast(`删除关键词失败: ${errorMessage}`, 'danger');", "actionRequestSequence !== autoReplyKeywordActionRequestSequence", "文本关键词删除"),
            (import_body, "showToast(`导入关键词失败: ${errorMessage}`, 'danger');", "modalRequestSequence !== importKeywordsModalRequestSequence", "关键词导入"),
            (add_image_body, "showToast(`添加图片关键词失败: ${errorMessage}`, 'danger');", "requestSequence !== imageKeywordModalRequestSequence", "图片关键词保存"),
            (export_body, "showToast(`导出关键词失败: ${errorMessage}`, 'danger');", "actionRequestSequence !== autoReplyKeywordActionRequestSequence", "关键词导出"),
        ):
            with self.subTest(label=label):
                self.assertIn("const errorMessage = error?.message || error || '请稍后重试';", body)
                self.assertIn(toast_fragment, body)
                self.assertLess(
                    body.rfind(guard_fragment, 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    f"{label}运行期异常时也得先尊重当前会话状态，别旧 catch 回调回来乱弹红字",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('auto-reply-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    f"都切出自动回复页了，旧的{label} catch 回调别跨页弹 danger toast",
                )

    def test_auto_reply_failure_toasts_recheck_stale_state_after_error_body_read(self):
        add_body = _extract_function_body(self.app_js, "addKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_body = _extract_function_body(self.app_js, "deleteKeyword")
        import_body = _extract_function_body(self.app_js, "importKeywords")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        export_body = _extract_function_body(self.app_js, "exportKeywords")

        add_error_index = add_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        duplicate_branch_index = add_body.index("if (error.includes('关键词已存在')")
        self.assertLess(
            add_body.find("actionRequestSequence !== autoReplyKeywordActionRequestSequence", add_error_index),
            duplicate_branch_index,
            "同页已经发起新的文本关键词保存后，旧失败响应读完错误体也别再回来走重复/失败提示分支",
        )
        self.assertLess(
            add_body.find("requestedAccountId !== currentAccountId", add_error_index),
            duplicate_branch_index,
            "账号都切走了，旧文本关键词保存失败响应读完错误体也别回来乱弹提示",
        )

        for body, error_fragment, toast_fragment, label in (
            (save_group_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`更新回复内容失败: ${error}`, 'danger');", "分组回复更新"),
            (delete_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`关键词删除失败: ${error}`, 'danger');", "文本关键词删除"),
            (import_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`导入失败: ${error}`, 'danger');", "关键词导入"),
            (export_body, "const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", "showToast(`导出关键词失败: ${error}`, 'danger');", "关键词导出"),
        ):
            with self.subTest(label=label):
                error_index = body.index(error_fragment)
                toast_index = body.index(toast_fragment)
                self.assertLess(
                    body.find("actionRequestSequence !== autoReplyKeywordActionRequestSequence", error_index),
                    toast_index,
                    f"同页已经发起新的{label}动作后，旧失败响应读完错误体也别回来甩红字",
                )
                self.assertLess(
                    body.find("!document.getElementById('auto-reply-section')?.classList.contains('active')", error_index),
                    toast_index,
                    f"都切出自动回复页了，旧的{label}失败响应读完错误体也别跨页弹 danger toast",
                )

        import_error_index = import_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        import_toast_index = import_body.index("showToast(`导入失败: ${error}`, 'danger');")
        self.assertLess(
            import_body.find("modalRequestSequence !== importKeywordsModalRequestSequence", import_error_index),
            import_toast_index,
            "导入弹窗会话都切了，旧失败响应读完错误体也别回来顶当前 modal 状态",
        )

        upload_error_index = add_image_body.index("const uploadError = await readResponseErrorMessage(uploadResponse, `HTTP ${uploadResponse.status}`);")
        upload_toast_index = add_image_body.index("showToast(`❌ 图片上传失败: ${uploadError}`, 'danger');")
        self.assertLess(
            add_image_body.find("actionRequestSequence !== autoReplyKeywordActionRequestSequence", upload_error_index),
            upload_toast_index,
            "同页已经发起新的图片关键词动作后，旧上传失败响应读完错误体也别回来甩红字",
        )
        self.assertLess(
            add_image_body.find("requestSequence !== imageKeywordModalRequestSequence", upload_error_index),
            upload_toast_index,
            "图片关键词弹窗会话都切了，旧上传失败响应读完错误体也别回来顶当前 modal 状态",
        )
        self.assertLess(
            add_image_body.find("!document.getElementById('auto-reply-section')?.classList.contains('active')", upload_error_index),
            upload_toast_index,
            "都切出自动回复页了，旧图片上传失败响应读完错误体也别跨页弹 danger toast",
        )

        batch_error_index = add_image_body.index("const error = await readResponseErrorMessage(batchResponse, `HTTP ${batchResponse.status}`);")
        batch_toast_index = add_image_body.index("showToast(`❌ 添加图片关键词失败: ${error}`, 'danger');")
        self.assertLess(
            add_image_body.find("actionRequestSequence !== autoReplyKeywordActionRequestSequence", batch_error_index),
            batch_toast_index,
            "同页已经发起新的图片关键词动作后，旧批量保存失败响应读完错误体也别回来甩红字",
        )
        self.assertLess(
            add_image_body.find("requestSequence !== imageKeywordModalRequestSequence", batch_error_index),
            batch_toast_index,
            "图片关键词弹窗会话都切了，旧批量保存失败响应读完错误体也别回来顶当前 modal 状态",
        )
        self.assertLess(
            add_image_body.find("!document.getElementById('auto-reply-section')?.classList.contains('active')", batch_error_index),
            batch_toast_index,
            "都切出自动回复页了，旧图片关键词批量保存失败响应读完错误体也别跨页弹 danger toast",
        )

    def test_auto_reply_mutation_action_sequence_starts_only_after_validation_or_confirmation(self):
        add_body = _extract_function_body(self.app_js, "addKeyword")
        add_image_body = _extract_function_body(self.app_js, "addImageKeyword")
        save_group_body = _extract_function_body(self.app_js, "saveGroupReply")
        delete_specific_keyword_body = _extract_function_body(self.app_js, "deleteSpecificKeyword")
        delete_specific_item_body = _extract_function_body(self.app_js, "deleteSpecificItem")
        import_body = _extract_function_body(self.app_js, "importKeywords")

        self.assertLess(
            add_body.index("if (!keywordInput) {"),
            add_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "关键词为空时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            add_body.index("if (!currentAccountId) {"),
            add_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "没选账号时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            add_body.index("if (keywords.length === 0) {"),
            add_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "解析后没有有效关键词时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            add_body.index("if (duplicates.length > 0) {"),
            add_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "关键词重复时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            save_group_body.index("if (!group) {"),
            save_group_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "找不到关键词分组时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )

        delete_specific_keyword_confirm_return_index = delete_specific_keyword_body.index("return;", delete_specific_keyword_body.index("if (!confirm("))
        self.assertLess(
            delete_specific_keyword_body.index("if (!group) {"),
            delete_specific_keyword_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "找不到关键词分组时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            delete_specific_keyword_confirm_return_index,
            delete_specific_keyword_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "用户都取消删除关键词分组了，就别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )

        delete_specific_item_confirm_return_index = delete_specific_item_body.index("return;", delete_specific_item_body.index("if (!confirm("))
        self.assertLess(
            delete_specific_item_body.index("if (!group) {"),
            delete_specific_item_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "找不到商品分组时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            delete_specific_item_confirm_return_index,
            delete_specific_item_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "用户都取消删除商品配置了，就别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            import_body.index("if (!file) {"),
            import_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "导入文件都没选时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            add_image_body.index("if (!keywordInput) {"),
            add_image_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "图片关键词为空时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            add_image_body.index("if (!file) {"),
            add_image_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "图片文件没选时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            add_image_body.index("if (keywords.length === 0) {"),
            add_image_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "解析后没有有效图片关键词时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            add_image_body.index("if (!currentAccountId) {"),
            add_image_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "没选账号时只是前端校验，别先把图片关键词 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            add_image_body.index("if (duplicates.length > 0) {"),
            add_image_body.index("const actionRequestSequence = ++autoReplyKeywordActionRequestSequence;"),
            "图片关键词重复时只是前端校验，别先把自动回复 mutation action sequence 顶掉别的正常动作",
        )

    def test_auto_reply_keyword_mutation_finally_always_releases_its_loading_slot(self):
        for function_name, forbidden_guards in (
            ("addKeyword", ()),
            ("saveGroupReply", ()),
            ("deleteKeyword", ()),
            ("deleteSpecificKeyword", ()),
            ("deleteSpecificItem", ()),
            (
                "addImageKeyword",
                ("requestSequence !== imageKeywordModalRequestSequence",),
            ),
            ("exportKeywords", ()),
        ):
            body = _extract_function_body(self.app_js, function_name)
            with self.subTest(function_name=function_name):
                self.assertIn("} finally {", body)
                finally_block = body.split("} finally {", 1)[1]
                self.assertIn("toggleLoading(false);", finally_block)
                self.assertNotIn(
                    "return null;",
                    finally_block,
                    "自动回复 mutation 在 finally 里必须无条件归还自己的 loading 计数，别切个账号就把全局 loading 卡死",
                )
                self.assertNotIn(
                    "requestedAccountId !== currentAccountId",
                    finally_block,
                    "自动回复 mutation 的 stale guard 该挡住 toast 和 DOM 回写，不该挡住 finally 里的 loading 回收",
                )
                self.assertNotIn(
                    "actionRequestSequence !== autoReplyKeywordActionRequestSequence",
                    finally_block,
                    "自动回复 mutation 的 loading 是引用计数，旧请求 finally 也得归还自己的那一份",
                )
                self.assertNotIn(
                    "!document.getElementById('auto-reply-section')?.classList.contains('active')",
                    finally_block,
                    "离开自动回复页面后旧请求不该再改界面，但仍然必须在 finally 回收 loading",
                )
                for forbidden_guard in forbidden_guards:
                    self.assertNotIn(
                        forbidden_guard,
                        finally_block,
                        "弹窗会话换了也只是别再碰 UI，别把 finally 里的 loading 回收也一并掐掉",
                    )

    def test_auto_reply_keyword_validation_does_not_clear_unrelated_loading_state(self):
        body = _extract_function_body(self.app_js, "addKeyword")
        empty_keywords_branch = body[body.index("if (keywords.length === 0) {"):body.index("}", body.index("if (keywords.length === 0) {"))]
        self.assertNotIn(
            "toggleLoading(false);",
            empty_keywords_branch,
            "前端校验发现没有有效关键词时，别顺手把别的会话正在显示的 loading 也给掐灭",
        )

    def test_import_keywords_uses_async_deferred_reload_callback(self):
        body = _extract_function_body(self.app_js, "importKeywords")
        self.assertIn("setTimeout(async () => {", body)
        self.assertIn("const keywordsLoaded = await loadAccountKeywords();", body)
        self.assertNotIn("setTimeout(() => {", body)

    def test_auto_reply_account_list_refresh_preserves_or_clears_current_selection_consistently(self):
        body = _extract_function_body(self.app_js, "refreshAccountList")
        self.assertIn("const previousValue = select ? select.value : '';", body)
        self.assertIn("const keywordManagement = document.getElementById('keywordManagement');", body)
        self.assertIn("if (previousValue && accountsWithKeywords.some(account => getCookieDetailsAccountId(account) === previousValue)) {", body)
        self.assertIn("select.value = previousValue;", body)
        self.assertIn("} else if (previousValue && currentAccountId === previousValue) {", body)
        self.assertIn("currentAccountId = '';", body)
        self.assertIn("keywordManagement.style.display = 'none';", body)

    def test_auto_reply_account_list_resets_stale_options_before_fetch_and_on_failure(self):
        body = _extract_function_body(self.app_js, "refreshAccountList")
        self.assertIn("const select = document.getElementById('accountSelect');", body)
        self.assertIn("select.innerHTML = '<option value=\"\">🔍 请选择一个账号开始配置...</option>';", body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 账号列表加载失败，请稍后重试</option>';", body)
        self.assertLess(
            body.index("select.innerHTML = '<option value=\"\">🔍 请选择一个账号开始配置...</option>';"),
            body.index("const response = await fetch(`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`, {"),
            "自动回复账号下拉刷新前应先清掉旧选项，失败时别继续挂着陈年账号装正常",
        )

    def test_auto_reply_account_list_keyword_fetches_encode_account_ids_and_surface_load_failures(self):
        body = _extract_function_body(self.app_js, "refreshAccountList")

        self.assertIn("const accountId = getCookieDetailsAccountId(account);", body)
        self.assertIn("if (!accountId) {", body)
        self.assertIn("fetch(`${apiBase}/keywords/counts`", body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${encodedAccountId}`", body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${getCookieDetailsAccountId(account)}`", body)

        self.assertIn("let keywordCountLoadFailed = false;", body)
        self.assertIn("if (!keywordsResponse.ok) {", body)
        self.assertIn("keywordCountLoadFailed = true;", body)
        self.assertIn("keywordCount: keywordCountLoadFailed ? 0 : Number(keywordCounts[accountId] || 0),", body)
        self.assertIn("keywordCountLoadFailed: keywordCountLoadFailed,", body)

        self.assertIn("if (account.keywordCountLoadFailed) {", body)
        self.assertIn("status = ' (关键词加载失败)';", body)
        self.assertIn("status = ' (关键词加载失败) [已禁用]';", body)
        self.assertNotIn("return {\n                ...account,\n                keywordCount: 0\n                };", body)

    def test_auto_reply_account_list_surfaces_empty_state_when_no_valid_account_ids_exist(self):
        body = _extract_function_body(self.app_js, "refreshAccountList")
        self.assertIn("let appendedCount = 0;", body)
        self.assertIn("appendedCount += 1;", body)
        self.assertIn("if (appendedCount === 0) {", body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 暂无账号，请先添加账号</option>';", body)
        empty_state_branch = body.split("if (appendedCount === 0) {", 1)[1].split("}", 1)[0]
        self.assertIn("currentAccountId = '';", empty_state_branch)
        self.assertIn("keywordManagement.style.display = 'none';", empty_state_branch)

    def test_auto_reply_account_list_resets_current_state_when_backend_returns_no_accounts(self):
        body = _extract_function_body(self.app_js, "refreshAccountList")
        self.assertIn("if (accountsWithKeywords.length === 0) {", body)
        empty_accounts_branch = body.split("if (accountsWithKeywords.length === 0) {", 1)[1].split("}", 1)[0]
        self.assertIn("currentAccountId = '';", empty_accounts_branch)
        self.assertIn("keywordManagement.style.display = 'none';", empty_accounts_branch)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 暂无账号，请先添加账号</option>';", empty_accounts_branch)

    def test_account_keyword_count_helper_encodes_account_ids_in_path_segments(self):
        body = _extract_function_body(self.app_js, "getAccountKeywordCount")
        self.assertIn("Object.prototype.hasOwnProperty.call(accountKeywordCache, accountId)", body)
        self.assertIn("fetch(`${apiBase}/keywords/counts`", body)
        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${encodedAccountId}`", body)
        self.assertNotIn("fetch(`${apiBase}/keywords/${accountId}`", body)

    def test_auto_reply_badge_and_account_modals_escape_runtime_account_ids(self):
        badge_body = _extract_function_body(self.app_js, "updateAccountBadge")
        comment_templates_body = _extract_function_body(self.app_js, "showCommentTemplates")
        polish_schedule_body = _extract_function_body(self.app_js, "openPolishScheduleModal")

        self.assertIn("const safeAccountId = escapeHtml(accountId);", badge_body)
        self.assertNotIn("${accountId}", badge_body)
        self.assertIn("${safeAccountId}", badge_body)

        self.assertIn("const safeAccountId = escapeHtml(accountId);", comment_templates_body)
        self.assertIn("好评模板管理 - ${safeAccountId}", comment_templates_body)
        self.assertNotIn("好评模板管理 - ${accountId}", comment_templates_body)

        self.assertIn("const safeAccountId = escapeHtml(accountId);", polish_schedule_body)
        self.assertIn("const safeAccountIdAttr = escapeHtmlAttribute(accountId);", polish_schedule_body)
        self.assertIn("定时擦亮 - ${safeAccountId}", polish_schedule_body)
        self.assertIn('value="${safeAccountIdAttr}"', polish_schedule_body)
        self.assertNotIn("定时擦亮 - ${accountId}", polish_schedule_body)
        self.assertNotIn('value="${accountId}"', polish_schedule_body)

    def test_polish_schedule_modal_ignores_stale_async_responses_and_hidden_accounts_state(self):
        self.assertIn("let polishScheduleModalRequestSequence = 0;", self.app_js)
        self.assertIn("let polishScheduleMutationActionRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        open_body = _extract_function_body(self.app_js, "openPolishScheduleModal")

        self.assertIn("if (sectionName !== 'accounts') {", show_section_body)
        self.assertIn("polishScheduleModalRequestSequence += 1;", show_section_body)
        self.assertIn("polishScheduleMutationActionRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++polishScheduleModalRequestSequence;", open_body)
        self.assertIn("requestSequence !== polishScheduleModalRequestSequence", open_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", open_body)
        self.assertIn("return null;", open_body)
        self.assertLess(
            open_body.index("requestSequence !== polishScheduleModalRequestSequence"),
            open_body.index("const existingModal = document.getElementById('polishScheduleModal');"),
            "旧的定时擦亮加载请求不该晚回来后把当前弹窗拆掉重建成旧账号内容",
        )
        self.assertIn("if (modalElement.dataset.polishScheduleIgnoreNextHidden === 'true') {", open_body)
        self.assertIn("modalElement.dataset.polishScheduleIgnoreNextHidden = 'false';", open_body)
        self.assertIn("polishScheduleModalRequestSequence += 1;", open_body)

    def test_polish_schedule_save_respects_modal_session_before_closing_or_toasting(self):
        open_body = _extract_function_body(self.app_js, "openPolishScheduleModal")
        save_body = _extract_function_body(self.app_js, "savePolishSchedule")

        self.assertIn("const requestSequence = polishScheduleModalRequestSequence;", save_body)
        self.assertIn("const actionRequestSequence = ++polishScheduleMutationActionRequestSequence;", save_body)
        self.assertIn("requestSequence !== polishScheduleModalRequestSequence", save_body)
        self.assertIn("actionRequestSequence !== polishScheduleMutationActionRequestSequence", save_body)
        self.assertIn("modalElement.dataset.polishScheduleIgnoreNextHidden = 'true';", save_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", save_body)
        self.assertIn("return null;", save_body)
        self.assertLess(
            save_body.index("requestSequence !== polishScheduleModalRequestSequence"),
            save_body.index("showToast(successMessage, 'success');"),
            "定时擦亮保存的旧响应不该回来给当前账号乱报成功",
        )
        self.assertLess(
            save_body.index("requestSequence !== polishScheduleModalRequestSequence"),
            save_body.index("closePolishScheduleModal();"),
            "定时擦亮保存的旧响应不该回来把已经重开的弹窗关掉",
        )
        self.assertIn("showToast(`保存定时擦亮设置失败: ${error.message || '请稍后重试'}`, 'danger');", save_body)
        self.assertIn("if (modalElement.dataset.polishScheduleIgnoreNextHidden === 'true') {", open_body)

    def test_polish_schedule_action_sequence_starts_only_after_validation(self):
        body = _extract_function_body(self.app_js, "savePolishSchedule")
        self.assertLess(
            body.index("if (!accountId) {"),
            body.index("const actionRequestSequence = ++polishScheduleMutationActionRequestSequence;"),
            "缺少账号ID时只是前端校验，别先把定时擦亮 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            body.index("if (!Number.isInteger(runHour) || runHour < 0 || runHour > 23) {"),
            body.index("const actionRequestSequence = ++polishScheduleMutationActionRequestSequence;"),
            "擦亮时间非法时只是前端校验，别先把定时擦亮 mutation action sequence 顶掉别的正常动作",
        )

    def test_polish_schedule_modal_does_not_open_default_state_when_task_load_fails(self):
        load_body = _extract_function_body(self.app_js, "loadScheduledTasks")
        open_body = _extract_function_body(self.app_js, "openPolishScheduleModal")

        self.assertIn("showToast(`加载定时任务失败: ${data.message || '未知错误'}`, 'danger');", load_body)
        self.assertIn("return null;", load_body)
        self.assertNotIn("return [];", load_body)

        self.assertIn("if (tasks === null) {", open_body)
        self.assertIn("return null;", open_body)
        self.assertLess(
            open_body.index("if (tasks === null) {"),
            open_body.index("const task = getPolishScheduledTask(tasks, accountId);"),
            "定时任务加载失败时不该把空数组当成“无配置”继续开默认弹窗，容易误导用户覆盖已有设置",
        )

    def test_polish_schedule_failure_toasts_surface_structured_errors_and_respect_hidden_state(self):
        load_body = _extract_function_body(self.app_js, "loadScheduledTasks")
        open_body = _extract_function_body(self.app_js, "openPolishScheduleModal")
        save_body = _extract_function_body(self.app_js, "savePolishSchedule")

        load_toast = "showToast(`加载定时任务失败: ${error.message || '请稍后重试'}`, 'danger');"
        open_toast = "showToast(`打开定时擦亮设置失败: ${error.message || '请稍后重试'}`, 'danger');"
        save_toast = "showToast(`保存定时擦亮设置失败: ${error.message || '请稍后重试'}`, 'danger');"

        self.assertIn(load_toast, load_body)
        self.assertIn(open_toast, open_body)
        self.assertIn(save_toast, save_body)

        self.assertLess(
            load_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, load_body.index(load_toast)),
            load_body.index(load_toast),
            "账号页都切走了，旧的定时任务加载失败别再跨页甩 danger toast",
        )
        self.assertLess(
            open_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, open_body.index(open_toast)),
            open_body.index(open_toast),
            "账号页都切走了，旧的定时擦亮弹窗打开失败别再跨页甩 danger toast",
        )
        self.assertLess(
            save_body.rfind("!document.getElementById('accounts-section')?.classList.contains('active')", 0, save_body.index(save_toast)),
            save_body.index(save_toast),
            "账号页都切走了，旧的定时擦亮保存失败别再跨页甩 danger toast",
        )
        self.assertLess(
            save_body.rfind("requestSequence !== polishScheduleModalRequestSequence", 0, save_body.index(save_toast)),
            save_body.index(save_toast),
            "定时擦亮弹窗都换会话了，旧保存失败响应别再回魂甩红字",
        )
        self.assertLess(
            save_body.rfind("actionRequestSequence !== polishScheduleMutationActionRequestSequence", 0, save_body.index(save_toast)),
            save_body.index(save_toast),
            "同页已经开始新的定时擦亮保存动作后，旧失败响应别再回头污染当前会话",
        )

    def test_polish_schedule_task_loader_suppresses_fetchjson_default_toast_and_hidden_replays(self):
        load_body = _extract_function_body(self.app_js, "loadScheduledTasks")
        open_body = _extract_function_body(self.app_js, "openPolishScheduleModal")

        self.assertIn("async function loadScheduledTasks(requestSequence = null) {", self.app_js)
        self.assertIn("const data = await fetchJSON(`${apiBase}/scheduled-tasks`, {", load_body)
        self.assertIn("suppressErrorToast: true", load_body)
        self.assertIn("requestSequence !== null && (", load_body)
        self.assertIn("requestSequence !== polishScheduleModalRequestSequence", load_body)
        self.assertIn("!document.getElementById('accounts-section')?.classList.contains('active')", load_body)
        self.assertIn("return null;", load_body)
        self.assertIn("const tasks = await loadScheduledTasks(requestSequence);", open_body)
        self.assertNotIn("const tasks = await loadScheduledTasks();", open_body)

    def test_polish_schedule_save_helpers_suppress_fetchjson_default_toast_and_defer_errors_to_caller(self):
        create_body = _extract_function_body(self.app_js, "createScheduledTask")
        update_body = _extract_function_body(self.app_js, "updateScheduledTask")
        save_body = _extract_function_body(self.app_js, "savePolishSchedule")

        self.assertIn("async function createScheduledTask(accountId, runHour, enabled = true, options = {}) {", self.app_js)
        self.assertIn("async function updateScheduledTask(taskId, payload, options = {}) {", self.app_js)
        self.assertIn("const { suppressErrorToast = false } = options;", create_body)
        self.assertIn("const { suppressErrorToast = false } = options;", update_body)
        self.assertIn("suppressErrorToast", create_body)
        self.assertIn("suppressErrorToast", update_body)
        self.assertIn("data = await updateScheduledTask(taskId, {", save_body)
        self.assertIn("suppressErrorToast: true", save_body)
        self.assertIn("data = await createScheduledTask(accountId, runHour, enabled, { suppressErrorToast: true });", save_body)

    def test_polish_schedule_fetchjson_callers_abort_when_unauthorized_redirect_returns_no_payload(self):
        load_body = _extract_function_body(self.app_js, "loadScheduledTasks")
        save_body = _extract_function_body(self.app_js, "savePolishSchedule")

        self.assertIn("if (data == null) {", load_body)
        self.assertLess(
            load_body.index("if (data == null) {"),
            load_body.index("if (data.success) {"),
            "定时任务列表 helper 在 401 跳转后返回空值时，调用方得先收手，别上来就解引用 data.success 把自己整崩了",
        )

        self.assertIn("if (!data) {", save_body)
        self.assertLess(
            save_body.index("if (!data) {"),
            save_body.index("if (!data.success) {"),
            "定时擦亮保存 helper 在 401 跳转后返回空值时，调用方得把它当成中止，别再拿空值硬怼 success",
        )

    def test_auto_reply_account_list_refresh_ignores_stale_async_responses(self):
        self.assertIn("let autoReplyAccountListRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "refreshAccountList")
        self.assertIn("const requestSequence = ++autoReplyAccountListRequestSequence;", body)
        self.assertIn("requestSequence !== autoReplyAccountListRequestSequence", body)
        self.assertIn("return;", body)

    def test_auto_reply_account_list_refresh_stops_before_keyword_count_fanout_when_request_turns_stale(self):
        body = _extract_function_body(self.app_js, "refreshAccountList")

        accounts_json_index = body.index("const accounts = await response.json();")
        keyword_counts_fetch_index = body.index("const keywordsResponse = await fetch(`${apiBase}/keywords/counts`, {")
        stale_guard_after_accounts = body.find("requestSequence !== autoReplyAccountListRequestSequence", accounts_json_index)

        self.assertGreater(
            stale_guard_after_accounts,
            accounts_json_index,
            "自动回复账号列表拿到账户列表后，先确认请求还活着，再决定要不要去拉每个账号的关键词数量",
        )
        self.assertLess(
            stale_guard_after_accounts,
            keyword_counts_fetch_index,
            "旧的自动回复账号列表请求不该在已经 stale 后还继续去拉关键词数量汇总",
        )

    def test_auto_reply_account_list_refresh_aborts_previous_request_and_reuses_same_signal(self):
        self.assertIn("let autoReplyAccountListAbortController = null;", self.app_js)
        body = _extract_function_body(self.app_js, "refreshAccountList")

        previous_abort_index = body.index("if (autoReplyAccountListAbortController) {")
        new_controller_index = body.index("const controller = new AbortController();")
        accounts_fetch_index = body.index("const response = await fetch(`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`, {")
        keywords_fetch_index = body.index("const keywordsResponse = await fetch(`${apiBase}/keywords/counts`, {")
        accounts_signal_index = body.index("signal: controller.signal", accounts_fetch_index)
        keywords_signal_index = body.index("signal: controller.signal", keywords_fetch_index)
        finally_index = body.index("} finally {")
        controller_cleanup_index = body.index("if (autoReplyAccountListAbortController === controller) {", finally_index)
        null_cleanup_index = body.index("autoReplyAccountListAbortController = null;", controller_cleanup_index)

        self.assertLess(
            previous_abort_index,
            new_controller_index,
            "刷新自动回复账号列表前，得先把上一轮请求 abort 掉，别让旧请求在后台瞎跑",
        )
        self.assertLess(
            new_controller_index,
            accounts_fetch_index,
            "AbortController 得在主账号列表请求发出去前建好，不然 signal 挂了个寂寞",
        )
        self.assertIn("autoReplyAccountListAbortController = controller;", body)
        self.assertGreater(
            accounts_signal_index,
            accounts_fetch_index,
            "主账号列表请求得挂上 controller.signal，别嘴上说 abort，身体很诚实地继续裸奔",
        )
        self.assertGreater(
            keywords_signal_index,
            keywords_fetch_index,
            "关键词数量 fan-out 也得复用同一个 signal，不然主请求停了，子请求还在那儿满地乱窜",
        )
        self.assertLess(
            controller_cleanup_index,
            null_cleanup_index,
            "账号列表请求收尾时得把 controller 清掉，不然下次刷新又拿着过期 controller 硬怼",
        )

    def test_auto_reply_requests_are_invalidated_when_leaving_auto_reply_section(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        self.assertIn("if (sectionName !== 'auto-reply') {", show_section_body)
        abort_index = show_section_body.index("autoReplyAccountListAbortController.abort();")
        clear_index = show_section_body.index("autoReplyAccountListAbortController = null;")
        invalidate_index = show_section_body.index("autoReplyAccountListRequestSequence += 1;")
        self.assertLess(
            abort_index,
            invalidate_index,
            "离开自动回复页时，先把在飞的账号列表请求掐掉，再失效化序号，别让旧请求吊着半口气回来作妖",
        )
        self.assertLess(
            clear_index,
            invalidate_index,
            "离开自动回复页时 controller 得当场清空，别留个僵尸引用在那儿恶心后续刷新",
        )
        self.assertIn("autoReplyAccountListRequestSequence += 1;", show_section_body)
        self.assertIn("autoReplyKeywordsRequestSequence += 1;", show_section_body)
        self.assertIn("importKeywordsModalRequestSequence += 1;", show_section_body)

    def test_auto_reply_account_list_refresh_suppresses_abort_error_toast(self):
        body = _extract_function_body(self.app_js, "refreshAccountList")

        abort_guard_index = body.rfind("if (controller.signal.aborted || error?.name === 'AbortError') {")
        toast_index = body.index("showToast(`刷新账号列表失败: ${errorMessage}`, 'danger');")

        self.assertLess(
            abort_guard_index,
            toast_index,
            "账号列表请求是主动 abort 的，就老老实实闭嘴返回，别再弹失败 toast 膈应人",
        )

    def test_switching_away_from_auto_reply_closes_open_keyword_modals(self):
        body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("const importKeywordsModalElement = document.getElementById('importKeywordsModal');", body)
        self.assertIn("importKeywordsModal.hide();", body)
        self.assertIn("const addImageKeywordModalElement = document.getElementById('addImageKeywordModal');", body)
        self.assertIn("addImageKeywordModal.hide();", body)
        self.assertIn("const imageViewModalElement = document.getElementById('imageViewModal');", body)
        self.assertIn("imageViewModal.hide();", body)
        self.assertIn("imageViewModalElement.remove();", body)

    def test_auto_reply_account_and_keyword_loaders_do_not_update_hidden_section_or_emit_hidden_toasts(self):
        refresh_accounts_body = _extract_function_body(self.app_js, "refreshAccountList")
        refresh_keywords_body = _extract_function_body(self.app_js, "refreshKeywordsList")
        load_keywords_body = _extract_function_body(self.app_js, "loadAccountKeywords")

        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", refresh_accounts_body)
        self.assertLess(
            refresh_accounts_body.index("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
            refresh_accounts_body.index("select.appendChild(option);"),
            "自动回复账号列表旧请求不该在切页后再回来往隐藏下拉框塞账号",
        )
        self.assertLess(
            refresh_accounts_body.rfind("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
            refresh_accounts_body.index("showToast(`刷新账号列表失败: ${errorMessage}`, 'danger');"),
            "都离开自动回复页了，旧账号列表失败请求不该跨页弹 toast 烦人",
        )

        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", refresh_keywords_body)
        self.assertLess(
            refresh_keywords_body.index("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
            refresh_keywords_body.index("renderKeywordsList(data);"),
            "自动回复关键词列表旧请求不该在切页后还回来覆盖隐藏页面内容",
        )

        self.assertIn("!document.getElementById('auto-reply-section')?.classList.contains('active')", load_keywords_body)
        self.assertLess(
            load_keywords_body.index("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
            load_keywords_body.index("keywordManagement.style.display = 'block';"),
            "自动回复账号关键词旧请求不该在切页后还回来把隐藏配置区再撑开",
        )
        self.assertLess(
            load_keywords_body.index("!document.getElementById('auto-reply-section')?.classList.contains('active')"),
            load_keywords_body.index("showToast(`加载关键词失败: ${errorMessage}`, 'danger');"),
            "都离开自动回复页了，旧关键词失败请求不该跨页弹 toast",
        )

    def test_load_account_keywords_uses_explicit_keyword_management_lookup_before_showing_panel(self):
        body = _extract_function_body(self.app_js, "loadAccountKeywords")

        lookup_index = body.index("const keywordManagement = document.getElementById('keywordManagement');")
        show_guard_index = body.index("if (keywordManagement) {")
        show_panel_index = body.index("keywordManagement.style.display = 'block';")

        self.assertLess(
            lookup_index,
            show_guard_index,
            "加载账号关键词时先把 keywordManagement 元素拿到手，别指望浏览器拿 id 偷偷给你挂个全局变量续命",
        )
        self.assertLess(
            show_guard_index,
            show_panel_index,
            "关键词配置区显示前得先判空，不然 DOM 一变就直接 ReferenceError，属实给自己找不痛快",
        )

    def test_item_reply_account_change_wraps_fetch_in_try_block(self):
        body = _extract_function_body(self.app_js, "onAccountChangeForReply")
        self.assertRegex(
            body,
            r"try\s*\{[\s\S]*const response = await fetch",
            "onAccountChangeForReply 应该在 try 内处理 fetch 失败",
        )

    def test_item_management_mutations_only_report_success_when_list_reload_succeeds(self):
        toggle_multi_spec_body = _extract_function_body(self.app_js, "toggleItemMultiSpec")
        toggle_multi_quantity_body = _extract_function_body(self.app_js, "toggleItemMultiQuantityDelivery")
        sync_page_body = _extract_function_body(self.app_js, "getAllItemsFromAccount")
        sync_all_pages_body = _extract_function_body(self.app_js, "getAllItemsFromAccountAll")

        for body, success_message, warning_message in (
            (toggle_multi_spec_body, "${isMultiSpec ? '开启' : '关闭'}多规格成功", "商品列表刷新失败，请稍后手动刷新"),
            (toggle_multi_quantity_body, "${multiQuantityDelivery ? '开启' : '关闭'}多数量发货成功", "商品列表刷新失败，请稍后手动刷新"),
            (sync_page_body, "成功同步第${pageNumber}页 ${data.current_count} 个商品，最新详情已更新", "同步成功，但商品列表刷新失败，请稍后手动刷新"),
            (sync_all_pages_body, "成功同步 ${data.total_count} 个商品（共${data.total_pages}页），最新详情已更新", "同步成功，但商品列表刷新失败，请稍后手动刷新"),
        ):
            with self.subTest(success_message=success_message):
                self.assertIn("const itemsLoaded = await refreshItemsData();", body)
                self.assertIn("if (itemsLoaded === true) {", body)
                self.assertIn("} else if (itemsLoaded === false) {", body)
                self.assertIn(f"showToast(`{success_message}`, 'success');", body)
                self.assertIn(f"showToast('{warning_message}', 'warning');", body)

    def test_refresh_items_and_item_reply_wrappers_do_not_emit_cross_page_toasts(self):
        refresh_items_body = _extract_function_body(self.app_js, "refreshItems")
        refresh_item_reply_body = _extract_function_body(self.app_js, "refreshItemReplayS")

        for body, section_id, success_fragment, warning_fragment in (
            (
                refresh_items_body,
                "items-section",
                "showToast('本地商品列表已刷新', 'success');",
                "showToast('商品列表刷新失败，请稍后重试', 'warning');",
            ),
            (
                refresh_item_reply_body,
                "items-reply-section",
                "showToast('商品列表已刷新', 'success');",
                "showToast('商品列表刷新失败，请稍后重试', 'warning');",
            ),
        ):
            with self.subTest(section_id=section_id):
                self.assertIn(f"!document.getElementById('{section_id}')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index(f"!document.getElementById('{section_id}')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "都切页了，旧刷新成功结果不该再跨页弹 success toast",
                )
                self.assertLess(
                    body.rfind(f"!document.getElementById('{section_id}')?.classList.contains('active')", 0, body.index(warning_fragment)),
                    body.index(warning_fragment),
                    "都切页了，旧刷新失败结果也别再跨页弹 warning toast",
                )

    def test_items_and_item_reply_refresh_data_wrappers_do_not_emit_cross_page_fallback_toasts(self):
        refresh_items_data_body = _extract_function_body(self.app_js, "refreshItemsData")
        refresh_item_reply_data_body = _extract_function_body(self.app_js, "refreshItemsReplayData")

        for body, section_id in (
            (refresh_items_data_body, "items-section"),
            (refresh_item_reply_data_body, "items-reply-section"),
        ):
            with self.subTest(section_id=section_id):
                self.assertIn(f"!document.getElementById('{section_id}')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind(f"!document.getElementById('{section_id}')?.classList.contains('active')", 0, body.index("showToast('刷新商品数据失败', 'danger');")),
                    body.index("showToast('刷新商品数据失败', 'danger');"),
                    "都切页了，refresh wrapper 自己的 fallback danger toast 也别跨页回魂",
                )

    def test_item_sync_mutations_ignore_older_same_page_responses_and_hidden_section_toasts(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        sync_page_body = _extract_function_body(self.app_js, "getAllItemsFromAccount")
        sync_all_pages_body = _extract_function_body(self.app_js, "getAllItemsFromAccountAll")

        self.assertIn("let itemMutationActionRequestSequence = 0;", self.app_js)
        self.assertIn("itemMutationActionRequestSequence += 1;", show_section_body)

        for body, refresh_fragment, success_fragment, failure_fragment in (
            (
                sync_page_body,
                "const itemsLoaded = await refreshItemsData();",
                "showToast(`成功同步第${pageNumber}页 ${data.current_count} 个商品，最新详情已更新`, 'success');",
                "showToast(data.message || '同步商品信息失败', 'danger');",
            ),
            (
                sync_all_pages_body,
                "const itemsLoaded = await refreshItemsData();",
                "showToast(`成功同步 ${data.total_count} 个商品（共${data.total_pages}页），最新详情已更新`, 'success');",
                "showToast(data.message || '同步商品信息失败', 'danger');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++itemMutationActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== itemMutationActionRequestSequence", body)
                self.assertIn("!document.getElementById('items-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== itemMutationActionRequestSequence"),
                    body.index(refresh_fragment),
                    "同页已经发起了新的商品同步动作，旧响应不该再回来触发列表刷新",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('items-section')?.classList.contains('active')", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出商品页了，旧商品同步成功响应不该再跨页弹 success toast",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== itemMutationActionRequestSequence", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "同页已经发起了新的商品同步动作，旧失败响应不该再回来糊 danger toast",
                )

        self.assertLess(
            sync_page_body.rfind("actionRequestSequence !== itemMutationActionRequestSequence", 0, sync_page_body.index("showToast('同步商品信息失败', 'danger');")),
            sync_page_body.index("showToast('同步商品信息失败', 'danger');"),
            "商品同步指定页的旧异常响应不该在新动作发起后继续甩通用失败 toast",
        )
        self.assertLess(
            sync_all_pages_body.rfind("actionRequestSequence !== itemMutationActionRequestSequence", 0, sync_all_pages_body.index("showToast('同步商品信息失败', 'danger');")),
            sync_all_pages_body.index("showToast('同步商品信息失败', 'danger');"),
            "商品同步所有页的旧异常响应不该在新动作发起后继续甩通用失败 toast",
        )
        for body in (sync_page_body, sync_all_pages_body):
            with self.subTest(body=body[:60]):
                finally_block = body.split("} finally {", 1)[1]
                self.assertIn("actionRequestSequence !== itemMutationActionRequestSequence", finally_block)
                self.assertIn("!document.getElementById('items-section')?.classList.contains('active')", finally_block)
                self.assertLess(
                    finally_block.index("actionRequestSequence !== itemMutationActionRequestSequence"),
                    finally_block.index("button.disabled = false;"),
                    "商品同步旧请求的 finally 不该在新动作开始后还把当前按钮 disabled 状态回写回去",
                )
                self.assertLess(
                    finally_block.index("actionRequestSequence !== itemMutationActionRequestSequence"),
                    finally_block.index("button.innerHTML = originalText;"),
                    "商品同步旧请求的 finally 也不该在新动作开始后把当前按钮文案还原成老状态",
                )

    def test_item_and_item_reply_raw_fetches_redirect_on_401_before_followup_processing(self):
        for function_name, body, anchor_fragment in (
            ("loadAccountOptions", _extract_function_body(self.app_js, "loadAccountOptions"), "if (!response.ok) {"),
            ("loadAllItems", _extract_function_body(self.app_js, "loadAllItems"), "if (response.ok) {"),
            ("loadItemsByAccount", _extract_function_body(self.app_js, "loadItemsByAccount"), "if (response.ok) {"),
            ("getAllItemsFromAccount", _extract_function_body(self.app_js, "getAllItemsFromAccount"), "if (response.ok) {"),
            ("getAllItemsFromAccountAll", _extract_function_body(self.app_js, "getAllItemsFromAccountAll"), "if (response.ok) {"),
            ("editItem", _extract_function_body(self.app_js, "editItem"), "if (response.ok) {"),
            ("saveItemDetail", _extract_function_body(self.app_js, "saveItemDetail"), "if (response.ok) {"),
            ("deleteItem", _extract_function_body(self.app_js, "deleteItem"), "if (response.ok) {"),
            ("batchDeleteItems", _extract_function_body(self.app_js, "batchDeleteItems"), "if (response.ok) {"),
            ("loadAllItemReplays", _extract_function_body(self.app_js, "loadAllItemReplays"), "if (response.ok) {"),
            ("loadItemsReplayByAccount", _extract_function_body(self.app_js, "loadItemsReplayByAccount"), "if (response.ok) {"),
            ("onAccountChangeForReply", _extract_function_body(self.app_js, "onAccountChangeForReply"), "if (response.ok) {"),
            ("editItemReply", _extract_function_body(self.app_js, "editItemReply"), "if (response.ok) {"),
            ("saveItemReply", _extract_function_body(self.app_js, "saveItemReply"), "if (response.ok) {"),
            ("deleteItemReply", _extract_function_body(self.app_js, "deleteItemReply"), "if (response.ok) {"),
            ("batchDeleteItemReplies", _extract_function_body(self.app_js, "batchDeleteItemReplies"), "if (response.ok) {"),
        ):
            with self.subTest(function_name=function_name):
                self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
                self.assertLess(
                    body.index("if (handleUnauthorizedApiResponse(response)) {"),
                    body.index(anchor_fragment),
                    f"{function_name} 遇到 401 时应先跳登录，别继续把未授权响应往后当正常数据折腾",
                )

    def test_item_and_item_reply_mutation_failures_parse_error_payloads_with_helper(self):
        for body, failure_fragment, label in (
            (_extract_function_body(self.app_js, "saveItemDetail"), "showToast(`更新失败: ${error}`, 'danger');", "商品详情更新"),
            (_extract_function_body(self.app_js, "deleteItem"), "showToast(`删除失败: ${error}`, 'danger');", "商品删除"),
            (_extract_function_body(self.app_js, "batchDeleteItems"), "showToast(`批量删除失败: ${error}`, 'danger');", "商品批量删除"),
            (_extract_function_body(self.app_js, "saveItemReply"), "showToast(`保存失败: ${error}`, 'danger');", "商品回复保存"),
            (_extract_function_body(self.app_js, "deleteItemReply"), "showToast(`删除失败: ${error}`, 'danger');", "商品回复删除"),
            (_extract_function_body(self.app_js, "batchDeleteItemReplies"), "showToast(`批量删除失败: ${error}`, 'danger');", "商品回复批量删除"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(failure_fragment),
                    f"{label}失败时得先把 detail/message 解出来，别把 JSON 原文直接甩给用户",
                )

    def test_item_and_item_reply_mutation_catch_failures_surface_runtime_error_messages(self):
        for body, legacy_toast, runtime_toast, guard_fragments, label in (
            (
                _extract_function_body(self.app_js, "saveItemDetail"),
                "showToast('更新商品详情失败', 'danger');",
                "showToast(`更新商品详情失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== itemEditorRequestSequence",
                    "actionRequestSequence !== itemMutationActionRequestSequence",
                    "!document.getElementById('items-section')?.classList.contains('active')",
                ),
                "商品详情更新",
            ),
            (
                _extract_function_body(self.app_js, "deleteItem"),
                "showToast('删除商品信息失败', 'danger');",
                "showToast(`删除商品信息失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== itemMutationActionRequestSequence",
                    "!document.getElementById('items-section')?.classList.contains('active')",
                ),
                "商品删除",
            ),
            (
                _extract_function_body(self.app_js, "batchDeleteItems"),
                "showToast('批量删除商品信息失败', 'danger');",
                "showToast(`批量删除商品信息失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== itemMutationActionRequestSequence",
                    "!document.getElementById('items-section')?.classList.contains('active')",
                ),
                "商品批量删除",
            ),
            (
                _extract_function_body(self.app_js, "saveItemReply"),
                "showToast('保存商品回复失败', 'danger');",
                "showToast(`保存商品回复失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "requestSequence !== itemReplyEditorRequestSequence",
                    "actionRequestSequence !== itemReplyMutationActionRequestSequence",
                    "!document.getElementById('items-reply-section')?.classList.contains('active')",
                ),
                "商品回复保存",
            ),
            (
                _extract_function_body(self.app_js, "deleteItemReply"),
                "showToast('删除商品回复失败', 'danger');",
                "showToast(`删除商品回复失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== itemReplyMutationActionRequestSequence",
                    "!document.getElementById('items-reply-section')?.classList.contains('active')",
                ),
                "商品回复删除",
            ),
            (
                _extract_function_body(self.app_js, "batchDeleteItemReplies"),
                "showToast('批量删除商品回复失败', 'danger');",
                "showToast(`批量删除商品回复失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== itemReplyMutationActionRequestSequence",
                    "!document.getElementById('items-reply-section')?.classList.contains('active')",
                ),
                "商品回复批量删除",
            ),
        ):
            with self.subTest(label=label):
                self.assertNotIn(legacy_toast, body)
                self.assertIn(runtime_toast, body)
                toast_index = body.index(runtime_toast)
                for guard_fragment in guard_fragments:
                    self.assertLess(
                        body.rfind(guard_fragment, 0, toast_index),
                        toast_index,
                        f"{label} catch 里的旧异常在弹 toast 前也得先过活性校验，别切页/换会话了还回来发癫",
                    )

    def test_item_toggle_mutations_ignore_older_same_page_responses_and_hidden_section_toasts(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        toggle_multi_spec_body = _extract_function_body(self.app_js, "toggleItemMultiSpec")
        toggle_multi_quantity_body = _extract_function_body(self.app_js, "toggleItemMultiQuantityDelivery")

        self.assertIn("let itemMutationActionRequestSequence = 0;", self.app_js)
        self.assertIn("itemMutationActionRequestSequence += 1;", show_section_body)

        for body, refresh_fragment, success_fragment, failure_fragment in (
            (
                toggle_multi_spec_body,
                "const itemsLoaded = await refreshItemsData();",
                "showToast(`${isMultiSpec ? '开启' : '关闭'}多规格成功`, 'success');",
                "showToast(`切换多规格状态失败: ${error.message}`, 'danger');",
            ),
            (
                toggle_multi_quantity_body,
                "const itemsLoaded = await refreshItemsData();",
                "showToast(`${multiQuantityDelivery ? '开启' : '关闭'}多数量发货成功`, 'success');",
                "showToast(`切换多数量发货状态失败: ${error.message}`, 'danger');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++itemMutationActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== itemMutationActionRequestSequence", body)
                self.assertIn("!document.getElementById('items-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== itemMutationActionRequestSequence"),
                    body.index(refresh_fragment),
                    "同页已经发起了新的商品切换动作，旧响应不该再回来触发列表刷新",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('items-section')?.classList.contains('active')", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出商品页了，旧切换成功响应不该再跨页弹 success toast",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== itemMutationActionRequestSequence", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "同页已经发起了新的商品切换动作，旧失败响应不该再回来糊 danger toast",
                )

    def test_item_mutation_action_sequence_starts_only_after_validation_or_confirmation(self):
        sync_page_body = _extract_function_body(self.app_js, "getAllItemsFromAccount")
        sync_all_pages_body = _extract_function_body(self.app_js, "getAllItemsFromAccountAll")
        save_body = _extract_function_body(self.app_js, "saveItemDetail")
        delete_body = _extract_function_body(self.app_js, "deleteItem")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteItems")

        self.assertLess(
            sync_page_body.index("if (!selectedAccountId) {"),
            sync_page_body.index("const actionRequestSequence = ++itemMutationActionRequestSequence;"),
            "没选账号就只是前端校验，别先把商品 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            sync_page_body.index("if (pageNumber < 1) {"),
            sync_page_body.index("const actionRequestSequence = ++itemMutationActionRequestSequence;"),
            "页码非法时只是前端校验，别先把商品 mutation action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            sync_all_pages_body.index("if (!selectedAccountId) {"),
            sync_all_pages_body.index("const actionRequestSequence = ++itemMutationActionRequestSequence;"),
            "没选账号时只是前端校验，别先把全量同步 action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            save_body.index("if (!itemDetail) {"),
            save_body.index("const actionRequestSequence = ++itemMutationActionRequestSequence;"),
            "商品详情为空时只是前端校验，别先把商品 mutation action sequence 顶掉别的正常动作",
        )

        delete_confirm_return_index = delete_body.index("return;", delete_body.index("if (!confirmed) {"))
        self.assertLess(
            delete_confirm_return_index,
            delete_body.index("const actionRequestSequence = ++itemMutationActionRequestSequence;"),
            "用户都取消删除了，就别先把商品 mutation action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            batch_delete_body.index("if (checkboxes.length === 0) {"),
            batch_delete_body.index("const actionRequestSequence = ++itemMutationActionRequestSequence;"),
            "批量删除没选商品时只是前端校验，别先把商品 mutation action sequence 顶掉别的正常动作",
        )
        batch_delete_confirm_return_index = batch_delete_body.index("return;", batch_delete_body.index("if (!confirmed) {"))
        self.assertLess(
            batch_delete_confirm_return_index,
            batch_delete_body.index("const actionRequestSequence = ++itemMutationActionRequestSequence;"),
            "用户都取消批量删除了，就别先把商品 mutation action sequence 顶掉别的正常动作",
        )

    def test_item_reply_account_change_ignores_stale_async_item_list_responses(self):
        self.assertIn("let itemReplyAccountItemsRequestSequence = 0;", self.app_js)

        body = _extract_function_body(self.app_js, "onAccountChangeForReply")
        self.assertIn("const accountSelect = document.getElementById('editReplyAccountIdSelect');", body)
        self.assertIn("const requestSequence = ++itemReplyAccountItemsRequestSequence;", body)
        self.assertIn("const requestedAccountId = accountId;", body)
        self.assertIn(
            "requestSequence !== itemReplyAccountItemsRequestSequence",
            body,
        )
        self.assertIn(
            "accountSelect.value !== requestedAccountId",
            body,
        )
        self.assertIn(
            "!document.getElementById('items-reply-section')?.classList.contains('active')",
            body,
        )
        self.assertLess(
            body.rfind("!document.getElementById('items-reply-section')?.classList.contains('active')", 0, body.index("showToast(`加载商品列表失败: ${error.message || '请稍后重试'}`, 'danger');")),
            body.index("showToast(`加载商品列表失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "都切出商品回复页了，旧的商品下拉失败请求不该再跨页甩 danger toast",
        )

    def test_item_reply_editor_modal_ignores_stale_async_responses_and_hidden_modal_state(self):
        self.assertIn("let itemReplyEditorRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "editItemReply")

        self.assertIn("if (sectionName !== 'items-reply') {", show_section_body)
        self.assertIn("itemReplyEditorRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++itemReplyEditorRequestSequence;", body)
        self.assertIn("requestSequence !== itemReplyEditorRequestSequence", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("itemReplyEditorRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.itemReplyEditorModalBound = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== itemReplyEditorRequestSequence"),
            body.index("accountSelect.value = data.account_id || accountId;"),
            "旧的商品回复编辑请求不该晚回来后把当前弹窗改成别的商品回复",
        )
        modal_show_index = body.index("modal.show();")
        self.assertLess(
            body.rfind("requestSequence !== itemReplyEditorRequestSequence", 0, modal_show_index),
            modal_show_index,
            "都切页或关弹窗了，旧的商品回复编辑请求不该再回来把弹窗重新弹出来",
        )

    def test_item_detail_editor_modal_ignores_stale_async_responses_and_hidden_modal_state(self):
        self.assertIn("let itemEditorRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "editItem")

        self.assertIn("if (sectionName !== 'items') {", show_section_body)
        self.assertIn("itemEditorRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++itemEditorRequestSequence;", body)
        self.assertIn("requestSequence !== itemEditorRequestSequence", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("itemEditorRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.itemEditorModalBound = 'true';", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== itemEditorRequestSequence"),
            body.index("document.getElementById('editItemAccountId').value = item.account_id;"),
            "旧的商品详情请求不该晚回来后把当前商品详情弹窗改成别的商品",
        )
        modal_show_index = body.index("modal.show();")
        self.assertLess(
            body.rfind("requestSequence !== itemEditorRequestSequence", 0, modal_show_index),
            modal_show_index,
            "都切页或关弹窗了，旧的商品详情请求不该再回来把详情弹窗重新弹出来",
        )

    def test_item_loaders_and_editors_parse_http_failure_details_before_toasting(self):
        for body, toast_fragment, stale_fragment, label in (
            (
                _extract_function_body(self.app_js, "loadAccountOptions"),
                "showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== accountOptionsRequestSequences[id]",
                "账号筛选器",
            ),
            (
                _extract_function_body(self.app_js, "loadAllItems"),
                "showToast(`加载商品列表失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== itemsRequestSequence",
                "商品全量列表",
            ),
            (
                _extract_function_body(self.app_js, "loadItemsByAccount"),
                "showToast(`加载商品列表失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== itemsRequestSequence",
                "商品按账号列表",
            ),
            (
                _extract_function_body(self.app_js, "loadAllItemReplays"),
                "showToast(`加载商品列表失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== itemReplaysRequestSequence",
                "商品回复全量列表",
            ),
            (
                _extract_function_body(self.app_js, "loadItemsReplayByAccount"),
                "showToast(`加载商品列表失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== itemReplaysRequestSequence",
                "商品回复按账号列表",
            ),
            (
                _extract_function_body(self.app_js, "editItem"),
                "showToast(`获取商品详情失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== itemEditorRequestSequence",
                "商品详情编辑器",
            ),
            (
                _extract_function_body(self.app_js, "onAccountChangeForReply"),
                "showToast(`加载商品列表失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== itemReplyAccountItemsRequestSequence",
                "商品回复账号切换下拉",
            ),
            (
                _extract_function_body(self.app_js, "editItemReply"),
                "showToast(`获取商品回复失败: ${error.message || '请稍后重试'}`, 'danger');",
                "requestSequence !== itemReplyEditorRequestSequence",
                "商品回复编辑器",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
                self.assertIn("throw new Error(errorMessage);", body)
                throw_index = body.index("throw new Error(errorMessage);", error_index)
                toast_index = body.index(toast_fragment)
                self.assertLess(
                    error_index,
                    throw_index,
                    f"{label} HTTP 失败时得先把 detail/message 解出来，别继续固定甩一句通用错误装镇定",
                )
                self.assertLess(
                    body.find(stale_fragment, error_index),
                    throw_index,
                    f"{label} 旧失败响应读完错误体后，先验当前会话/页面还活着，再决定要不要抛错",
                )
                self.assertLess(
                    throw_index,
                    toast_index,
                    f"{label} 应把真实后端错误带进 toast，别又吞成统一红字",
                )

    def test_switching_away_from_item_sections_closes_open_editor_modals(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("const editItemModalElement = document.getElementById('editItemModal');", show_section_body)
        self.assertIn("const editItemModal = editItemModalElement", show_section_body)
        self.assertIn("editItemModal.hide();", show_section_body)
        self.assertLess(
            show_section_body.index("itemEditorRequestSequence += 1;"),
            show_section_body.index("editItemModal.hide();"),
            "切出 items 时先废掉编辑会话再收 modal，别让隐藏事件反过来把当前判断顺序搅乱",
        )

        self.assertIn("const editItemReplyModalElement = document.getElementById('editItemReplyModal');", show_section_body)
        self.assertIn("const editItemReplyModal = editItemReplyModalElement", show_section_body)
        self.assertIn("editItemReplyModal.hide();", show_section_body)
        self.assertLess(
            show_section_body.index("itemReplyEditorRequestSequence += 1;"),
            show_section_body.index("editItemReplyModal.hide();"),
            "切出 items-reply 时也得把编辑弹窗收了，不然旧 modal 还挂在别的菜单上糊脸",
        )

    def test_item_detail_mutations_ignore_stale_modal_sessions_and_same_page_actions(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        edit_body = _extract_function_body(self.app_js, "editItem")
        save_body = _extract_function_body(self.app_js, "saveItemDetail")
        delete_body = _extract_function_body(self.app_js, "deleteItem")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteItems")

        self.assertIn("let itemMutationActionRequestSequence = 0;", self.app_js)
        self.assertIn("itemMutationActionRequestSequence += 1;", show_section_body)
        self.assertIn("if (modalElement.dataset.itemEditorIgnoreNextHidden === 'true') {", edit_body)
        self.assertIn("modalElement.dataset.itemEditorIgnoreNextHidden = 'false';", edit_body)

        self.assertIn("const requestSequence = itemEditorRequestSequence;", save_body)
        self.assertIn("const actionRequestSequence = ++itemMutationActionRequestSequence;", save_body)
        self.assertIn("modalElement.dataset.itemEditorIgnoreNextHidden = 'true';", save_body)
        self.assertLess(
            save_body.index("requestSequence !== itemEditorRequestSequence"),
            save_body.index("modal.hide();"),
            "旧的商品详情保存响应不该回来把已经重开的详情弹窗又关掉",
        )
        self.assertLess(
            save_body.rfind("actionRequestSequence !== itemMutationActionRequestSequence", 0, save_body.index("showToast('商品详情更新成功', 'success');")),
            save_body.index("showToast('商品详情更新成功', 'success');"),
            "同页已经发起了新的商品详情动作，旧成功响应别再回来刷 success toast",
        )
        self.assertLess(
            save_body.rfind("actionRequestSequence !== itemMutationActionRequestSequence", 0, save_body.index("showToast(`更新失败: ${error}`, 'danger');")),
            save_body.index("showToast(`更新失败: ${error}`, 'danger');"),
            "同页已经发起了新的商品详情动作，旧失败响应读完错误文本后也别再回魂甩红字",
        )

        for body, refresh_fragment, success_fragment, failure_fragment in (
            (
                delete_body,
                "const itemsLoaded = await refreshItemsData();",
                "showToast('商品信息删除成功', 'success');",
                "showToast(`删除失败: ${error}`, 'danger');",
            ),
            (
                batch_delete_body,
                "const itemsLoaded = await refreshItemsData();",
                "showToast(`批量删除完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个`, 'success');",
                "showToast(`批量删除失败: ${error}`, 'danger');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++itemMutationActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== itemMutationActionRequestSequence", body)
                self.assertIn("!document.getElementById('items-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== itemMutationActionRequestSequence"),
                    body.index(refresh_fragment),
                    "同页连续执行商品 mutation 时，旧响应不该晚回来后又触发列表刷新",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('items-section')?.classList.contains('active')", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出商品页了，旧 mutation 响应不该再跨页弹 success toast",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== itemMutationActionRequestSequence", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "同页连续执行商品 mutation 时，旧失败响应读完错误文本后不该再回魂糊红字",
                )

    def test_item_reply_loaders_reset_stale_table_state_when_fetch_fails(self):
        self.assertIn("function resetItemReplaysTable(message = '暂无商品数据') {", self.app_js)

        helper_body = _extract_function_body(self.app_js, "resetItemReplaysTable")
        load_all_body = _extract_function_body(self.app_js, "loadAllItemReplays")
        load_by_account_body = _extract_function_body(self.app_js, "loadItemsReplayByAccount")
        display_body = _extract_function_body(self.app_js, "displayItemReplays")

        self.assertIn("const tbody = document.getElementById('itemReplaysTableBody');", helper_body)
        self.assertIn("${escapeHtml(message)}", helper_body)
        self.assertIn("resetItemReplySelectionState();", helper_body)
        self.assertIn("resetItemReplaysTable();", load_all_body)
        self.assertLess(
            load_all_body.index("resetItemReplaysTable();"),
            load_all_body.index("const response = await fetch(`${apiBase}/itemReplays`, {"),
            "商品回复全量加载前先把旧表格清掉，别让上一次账号的数据挂着冒充当前请求的结果",
        )
        self.assertIn("resetItemReplaysTable();", load_by_account_body)
        self.assertLess(
            load_by_account_body.index("resetItemReplaysTable();"),
            load_by_account_body.index("const response = await fetch(`${apiBase}/itemReplays/account/${encodeURIComponent(accountId)}`, {"),
            "商品回复按账号加载前也得先清表，不然切账号时旧回复会继续挂着糊弄人",
        )
        self.assertIn("resetItemReplaysTable(error.message || '加载商品列表失败');", load_all_body)
        self.assertIn("resetItemReplaysTable(error.message || '加载商品列表失败');", load_by_account_body)
        self.assertIn("resetItemReplaysTable();", display_body)

    def test_item_reply_save_does_not_log_sensitive_reply_content(self):
        body = _extract_function_body(self.app_js, "saveItemReply")
        self.assertNotIn("console.log(accountId)", body)
        self.assertNotIn("console.log(itemId)", body)
        self.assertNotIn("console.log(replyContent)", body)

    def test_items_loaders_and_refresh_only_report_success_when_followup_reload_succeeds(self):
        load_all_body = _extract_function_body(self.app_js, "loadAllItems")
        load_by_account_body = _extract_function_body(self.app_js, "loadItemsByAccount")
        refresh_body = _extract_function_body(self.app_js, "refreshItemsData")
        refresh_list_body = _extract_function_body(self.app_js, "refreshItems")
        reset_body = _extract_function_body(self.app_js, "resetItemsView")
        save_detail_body = _extract_function_body(self.app_js, "saveItemDetail")
        delete_body = _extract_function_body(self.app_js, "deleteItem")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteItems")

        self.assertIn("const tbody = document.getElementById('itemsTableBody');", reset_body)
        self.assertIn("allItemsData = [];", reset_body)
        self.assertIn("filteredItemsData = [];", reset_body)
        self.assertIn("currentItemsPage = 1;", reset_body)
        self.assertIn("totalItemsPages = 0;", reset_body)
        self.assertIn("${escapeHtml(message)}", reset_body)

        self.assertIn("return true;", load_all_body)
        self.assertIn("return false;", load_all_body)
        self.assertIn("return null;", load_all_body)
        self.assertIn("return true;", load_by_account_body)
        self.assertIn("return false;", load_by_account_body)
        self.assertIn("return null;", load_by_account_body)
        self.assertIn("return await loadItemsByAccount({", refresh_body)
        self.assertIn("accountId: selectedAccountId,", refresh_body)
        self.assertIn("deferDisplay", refresh_body)
        self.assertIn("return await loadAllItems({ deferDisplay });", refresh_body)
        self.assertIn("resetItemsView();", load_all_body)
        self.assertIn("resetItemsView(error.message || '加载商品列表失败');", load_all_body)
        self.assertIn("resetItemsView();", load_by_account_body)
        self.assertIn("resetItemsView(error.message || '加载商品列表失败');", load_by_account_body)
        self.assertIn("const itemsLoaded = await refreshItemsData();", refresh_list_body)
        self.assertIn("if (itemsLoaded === true) {", refresh_list_body)
        self.assertIn("} else if (itemsLoaded === false) {", refresh_list_body)
        self.assertIn("showToast('本地商品列表已刷新', 'success');", refresh_list_body)
        self.assertIn("showToast('商品列表刷新失败，请稍后重试', 'warning');", refresh_list_body)

        for body, success_message, warning_message in (
            (save_detail_body, "商品详情更新成功", "商品详情更新成功，但商品列表刷新失败，请稍后手动刷新"),
            (delete_body, "商品信息删除成功", "商品信息删除成功，但商品列表刷新失败，请稍后手动刷新"),
            (batch_delete_body, "批量删除完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个", "批量删除已完成，但商品列表刷新失败，请稍后手动刷新"),
        ):
            with self.subTest(success_message=success_message):
                self.assertIn("const itemsLoaded = await refreshItemsData();", body)
                self.assertIn("if (itemsLoaded === true) {", body)
                self.assertIn("} else if (itemsLoaded === false) {", body)
                self.assertIn(f"showToast(`{success_message}`, 'success');" if "${" in success_message else f"showToast('{success_message}', 'success');", body)
                self.assertIn(f"showToast('{warning_message}', 'warning');", body)

    def test_account_filter_loader_resets_stale_options_before_fetch_and_on_failure(self):
        body = _extract_function_body(self.app_js, "loadAccountOptions")
        self.assertIn("const select = document.getElementById(id);", body)
        self.assertIn("select.innerHTML = `<option value=\"\">${emptyLabel}</option>`;", body)
        self.assertIn("select.innerHTML = `<option value=\"\">${emptyLabel}</option>`;", body)
        self.assertIn("const currentValue = select.value;", body)
        self.assertLess(
            body.index("select.innerHTML = `<option value=\"\">${emptyLabel}</option>`;"),
            body.index("const response = await fetch(`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`, {"),
            "加载账号筛选器前应先清掉旧选项，失败时别挂着上次的账号列表装正常",
        )
        self.assertIn("showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertIn("`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`", body)

    def test_account_filter_loader_treats_http_failures_as_real_failures(self):
        body = _extract_function_body(self.app_js, "loadAccountOptions")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("if (!response.ok) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertLess(
            body.index("if (!response.ok) {"),
            body.index("const accounts = await response.json();"),
            "账号下拉接口都 HTTP 挂了，就别再装作没事去读 JSON 了，直接走失败分支",
        )
        self.assertLess(
            body.index("if (handleUnauthorizedApiResponse(response)) {"),
            body.index("if (!response.ok) {"),
            "账号下拉接口 401 时应先跳登录，别继续把未授权响应往后当正常错误处理",
        )
        self.assertLess(
            body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            body.index("throw new Error(errorMessage);"),
            "账号下拉接口 HTTP 失败时也得先把 detail/message 解出来，再决定往 catch 怎么扔",
        )
        self.assertLess(
            body.index("throw new Error(errorMessage);"),
            body.index("showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "账号下拉接口返回非 2xx 时也得把真实错误带进 toast，别静默留个空下拉糊弄人",
        )

    def test_account_filter_loader_surfaces_empty_state_when_no_valid_account_ids_exist(self):
        body = _extract_function_body(self.app_js, "loadAccountOptions")
        self.assertIn("let appendedCount = 0;", body)
        self.assertIn("appendedCount += 1;", body)
        self.assertIn("if (appendedCount === 0) {", body)
        self.assertIn("select.innerHTML = '<option value=\"\">❌ 暂无可用账号，请先添加账号</option>';",
                      body)

    def test_account_filter_loader_ignores_stale_async_responses_and_hidden_sections(self):
        self.assertIn("let accountOptionsRequestSequences = {};", self.app_js)
        self.assertIn("function getAccountOptionsOwnerSectionId(id) {", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "loadAccountOptions")

        self.assertIn("accountOptionsRequestSequences.itemAccountFilter = (accountOptionsRequestSequences.itemAccountFilter || 0) + 1;", show_section_body)
        self.assertIn("accountOptionsRequestSequences.itemReplayAccountFilter = (accountOptionsRequestSequences.itemReplayAccountFilter || 0) + 1;", show_section_body)
        self.assertIn("accountOptionsRequestSequences.editReplyAccountIdSelect = (accountOptionsRequestSequences.editReplyAccountIdSelect || 0) + 1;", show_section_body)
        self.assertIn("accountOptionsRequestSequences.itemSearchAccountFilter = (accountOptionsRequestSequences.itemSearchAccountFilter || 0) + 1;", show_section_body)
        self.assertIn("if (id === 'itemSearchAccountFilter') {", self.app_js)
        self.assertIn("return 'item-search-section';", self.app_js)

        self.assertIn("const requestSequence = (accountOptionsRequestSequences[id] || 0) + 1;", body)
        self.assertIn("accountOptionsRequestSequences[id] = requestSequence;", body)
        self.assertIn("const ownerSectionId = getAccountOptionsOwnerSectionId(id);", body)
        self.assertIn("requestSequence !== accountOptionsRequestSequences[id]", body)
        self.assertIn("ownerSectionId && !document.getElementById(ownerSectionId)?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("requestSequence !== accountOptionsRequestSequences[id]"),
            body.index("enabledAccounts.forEach(account => {"),
            "旧的账号筛选器请求不该晚回来后把当前页面的筛选下拉框重新糊回去",
        )
        self.assertLess(
            body.rfind("ownerSectionId && !document.getElementById(ownerSectionId)?.classList.contains('active')"),
            body.index("showToast(`加载账号列表失败: ${error.message || '请稍后重试'}`, 'danger');"),
            "都切页了，旧的账号筛选器失败请求就别跨页回来弹 danger toast 了",
        )

    def test_item_reply_mutations_only_report_success_when_followup_reload_succeeds(self):
        load_all_body = _extract_function_body(self.app_js, "loadAllItemReplays")
        load_by_account_body = _extract_function_body(self.app_js, "loadItemsReplayByAccount")
        refresh_body = _extract_function_body(self.app_js, "refreshItemsReplayData")
        save_body = _extract_function_body(self.app_js, "saveItemReply")
        delete_body = _extract_function_body(self.app_js, "deleteItemReply")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteItemReplies")

        self.assertIn("return true;", load_all_body)
        self.assertIn("return false;", load_all_body)
        self.assertIn("return null;", load_all_body)
        self.assertIn("return true;", load_by_account_body)
        self.assertIn("return false;", load_by_account_body)
        self.assertIn("return null;", load_by_account_body)
        self.assertIn("return await loadItemsReplayByAccount();", refresh_body)
        self.assertIn("return await loadAllItemReplays();", refresh_body)

        self.assertIn("const itemRepliesLoaded = await refreshItemsReplayData();", save_body)
        self.assertIn("if (itemRepliesLoaded === true) {", save_body)
        self.assertIn("} else if (itemRepliesLoaded === false) {", save_body)
        self.assertIn("showToast('商品回复保存成功', 'success');", save_body)
        self.assertIn("showToast('商品回复保存成功，但商品回复列表刷新失败，请稍后手动刷新', 'warning');", save_body)

        self.assertIn("const itemRepliesLoaded = await refreshItemsReplayData();", delete_body)
        self.assertIn("if (itemRepliesLoaded === true) {", delete_body)
        self.assertIn("} else if (itemRepliesLoaded === false) {", delete_body)
        self.assertIn("showToast('商品回复删除成功', 'success');", delete_body)
        self.assertIn("showToast('商品回复删除成功，但商品回复列表刷新失败，请稍后手动刷新', 'warning');", delete_body)

        self.assertIn("const itemRepliesLoaded = await refreshItemsReplayData();", batch_delete_body)
        self.assertIn("if (itemRepliesLoaded === true) {", batch_delete_body)
        self.assertIn("} else if (itemRepliesLoaded === false) {", batch_delete_body)
        self.assertIn("showToast(`批量删除回复完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个`, 'success');", batch_delete_body)
        self.assertIn("showToast('批量删除回复已完成，但商品回复列表刷新失败，请稍后手动刷新', 'warning');", batch_delete_body)

    def test_item_and_item_reply_batch_delete_http_200_all_failures_do_not_report_success(self):
        items_body = _extract_function_body(self.app_js, "batchDeleteItems")
        item_reply_body = _extract_function_body(self.app_js, "batchDeleteItemReplies")

        for body, success_fragment, partial_fragment, all_fail_fragment, reload_fail_fragment, label in (
            (
                items_body,
                "showToast(`批量删除完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个`, 'success');",
                "showToast(`批量删除完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个`, 'warning');",
                "showToast(`批量删除失败: 0 个成功，${failedCount} 个失败`, 'danger');",
                "showToast('批量删除失败，且商品列表刷新失败，请稍后手动刷新', 'warning');",
                "商品批量删除",
            ),
            (
                item_reply_body,
                "showToast(`批量删除回复完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个`, 'success');",
                "showToast(`批量删除回复完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个`, 'warning');",
                "showToast(`批量删除失败: 0 个成功，${failedCount} 个失败`, 'danger');",
                "showToast('批量删除失败，且商品回复列表刷新失败，请稍后手动刷新', 'warning');",
                "商品回复批量删除",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn("const successCount = Number(result.success_count || 0);", body)
                self.assertIn("const failedCount = Number(result.failed_count || 0);", body)
                self.assertIn("if (successCount === 0) {", body)
                self.assertIn("} else if (failedCount > 0) {", body)
                self.assertIn(all_fail_fragment, body)
                self.assertIn(partial_fragment, body)
                self.assertIn(reload_fail_fragment, body)
                self.assertLess(
                    body.index("if (successCount === 0) {"),
                    body.index(success_fragment),
                    f"{label}接口就算返回 HTTP 200，但一条都没删掉时也别硬弹 success 糊弄人",
                )
                self.assertLess(
                    body.index("} else if (failedCount > 0) {"),
                    body.index(success_fragment),
                    f"{label}有失败项时应该降级成 warning，别一股脑全绿当满血成功",
                )

    def test_item_reply_mutations_ignore_stale_modal_sessions_and_same_page_actions(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        edit_body = _extract_function_body(self.app_js, "editItemReply")
        save_body = _extract_function_body(self.app_js, "saveItemReply")
        delete_body = _extract_function_body(self.app_js, "deleteItemReply")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteItemReplies")

        self.assertIn("let itemReplyMutationActionRequestSequence = 0;", self.app_js)
        self.assertIn("itemReplyMutationActionRequestSequence += 1;", show_section_body)
        self.assertIn("if (modalElement.dataset.itemReplyEditorIgnoreNextHidden === 'true') {", edit_body)
        self.assertIn("modalElement.dataset.itemReplyEditorIgnoreNextHidden = 'false';", edit_body)

        self.assertIn("const requestSequence = itemReplyEditorRequestSequence;", save_body)
        self.assertIn("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;", save_body)
        self.assertIn("modalElement.dataset.itemReplyEditorIgnoreNextHidden = 'true';", save_body)
        self.assertLess(
            save_body.index("requestSequence !== itemReplyEditorRequestSequence"),
            save_body.index("modal.hide();"),
            "旧的商品回复保存响应不该回来把已经重开的回复弹窗又关掉",
        )
        self.assertLess(
            save_body.rfind("actionRequestSequence !== itemReplyMutationActionRequestSequence", 0, save_body.index("showToast('商品回复保存成功', 'success');")),
            save_body.index("showToast('商品回复保存成功', 'success');"),
            "同页已经发起了新的商品回复动作，旧成功响应别再回来刷 success toast",
        )
        self.assertLess(
            save_body.rfind("actionRequestSequence !== itemReplyMutationActionRequestSequence", 0, save_body.index("showToast(`保存失败: ${error}`, 'danger');")),
            save_body.index("showToast(`保存失败: ${error}`, 'danger');"),
            "同页已经发起了新的商品回复动作，旧失败响应读完错误文本后也别再回魂甩红字",
        )

        for body, refresh_fragment, success_fragment, failure_fragment in (
            (
                delete_body,
                "const itemRepliesLoaded = await refreshItemsReplayData();",
                "showToast('商品回复删除成功', 'success');",
                "showToast(`删除失败: ${error}`, 'danger');",
            ),
            (
                batch_delete_body,
                "const itemRepliesLoaded = await refreshItemsReplayData();",
                "showToast(`批量删除回复完成: 成功 ${result.success_count} 个，失败 ${result.failed_count} 个`, 'success');",
                "showToast(`批量删除失败: ${error}`, 'danger');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== itemReplyMutationActionRequestSequence", body)
                self.assertIn("!document.getElementById('items-reply-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== itemReplyMutationActionRequestSequence"),
                    body.index(refresh_fragment),
                    "同页连续执行商品回复 mutation 时，旧响应不该晚回来后又触发列表刷新",
                )
                self.assertLess(
                    body.rfind("!document.getElementById('items-reply-section')?.classList.contains('active')", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出商品回复页了，旧 mutation 响应不该再跨页弹 success toast",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== itemReplyMutationActionRequestSequence", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "同页连续执行商品回复 mutation 时，旧失败响应读完错误文本后不该再回魂糊红字",
                )

    def test_item_reply_mutation_action_sequence_starts_only_after_validation_or_confirmation(self):
        save_body = _extract_function_body(self.app_js, "saveItemReply")
        delete_body = _extract_function_body(self.app_js, "deleteItemReply")
        batch_delete_body = _extract_function_body(self.app_js, "batchDeleteItemReplies")

        self.assertLess(
            save_body.index("if (!accountId) {"),
            save_body.index("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;"),
            "没选账号就只是前端校验，别先把商品回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            save_body.index("if (!itemId) {"),
            save_body.index("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;"),
            "没选商品就只是前端校验，别先把商品回复 mutation action sequence 顶掉别的正常动作",
        )
        self.assertLess(
            save_body.index("if (!replyContent) {"),
            save_body.index("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;"),
            "回复内容为空时只是前端校验，别先把商品回复 mutation action sequence 顶掉别的正常动作",
        )

        delete_confirm_return_index = delete_body.index("return;", delete_body.index("if (!confirmed)"))
        self.assertLess(
            delete_confirm_return_index,
            delete_body.index("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;"),
            "用户都取消删除商品回复了，就别先把商品回复 mutation action sequence 顶掉别的正常动作",
        )

        self.assertLess(
            batch_delete_body.index("if (checkboxes.length === 0) {"),
            batch_delete_body.index("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;"),
            "批量删除商品回复没选中数据时只是前端校验，别先把商品回复 mutation action sequence 顶掉别的正常动作",
        )
        batch_delete_confirm_return_index = batch_delete_body.index("return;", batch_delete_body.index("if (!confirmed)"))
        self.assertLess(
            batch_delete_confirm_return_index,
            batch_delete_body.index("const actionRequestSequence = ++itemReplyMutationActionRequestSequence;"),
            "用户都取消批量删除商品回复了，就别先把商品回复 mutation action sequence 顶掉别的正常动作",
        )

    def test_item_and_item_reply_loaders_ignore_stale_async_responses_when_sections_change(self):
        self.assertIn("let itemsRequestSequence = 0;", self.app_js)
        self.assertIn("let itemReplaysRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_all_items_body = _extract_function_body(self.app_js, "loadAllItems")
        load_items_by_account_body = _extract_function_body(self.app_js, "loadItemsByAccount")
        load_all_replies_body = _extract_function_body(self.app_js, "loadAllItemReplays")
        load_replies_by_account_body = _extract_function_body(self.app_js, "loadItemsReplayByAccount")

        self.assertIn("if (sectionName !== 'items') {", show_section_body)
        self.assertIn("itemsRequestSequence += 1;", show_section_body)
        self.assertIn("if (sectionName !== 'items-reply') {", show_section_body)
        self.assertIn("itemReplaysRequestSequence += 1;", show_section_body)

        for body, sequence_name, section_id, render_fragment in (
            (load_all_items_body, "itemsRequestSequence", "items-section", "displayItems(data.items);"),
            (load_items_by_account_body, "itemsRequestSequence", "items-section", "displayItems(data.items);"),
            (load_all_replies_body, "itemReplaysRequestSequence", "items-reply-section", "displayItemReplays(data.items);"),
            (load_replies_by_account_body, "itemReplaysRequestSequence", "items-reply-section", "displayItemReplays(data.items);"),
        ):
            with self.subTest(sequence_name=sequence_name, section_id=section_id):
                self.assertIn(f"const requestSequence = ++{sequence_name};", body)
                self.assertIn(f"requestSequence !== {sequence_name}", body)
                self.assertIn(f"!document.getElementById('{section_id}')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index(f"requestSequence !== {sequence_name}"),
                    body.index(render_fragment),
                    "旧列表请求不该晚回来后把新页面的数据表格糊回旧内容",
                )

    def test_item_root_loaders_stop_before_followup_refresh_when_account_filters_turn_stale(self):
        items_body = _extract_function_body(self.app_js, "loadItems")
        item_reply_body = _extract_function_body(self.app_js, "loadItemsReplay")

        self.assertIn("const preservedAccountId = document.getElementById('itemAccountFilter')?.value || '';", items_body)
        self.assertIn("const [accountOptionsLoaded, itemsLoaded] = await Promise.all([", items_body)
        self.assertIn("loadAccountOptions('itemAccountFilter')", items_body)
        self.assertIn("refreshItemsData({ preferredAccountId: preservedAccountId, deferDisplay: true })", items_body)
        self.assertIn("if (accountOptionsLoaded !== true || itemsLoaded !== true) {", items_body)
        self.assertLess(
            items_body.index("if (accountOptionsLoaded !== true || itemsLoaded !== true) {"),
            items_body.index("displayItems(allItemsData);"),
            "商品 root loader 发现账号筛选器请求已经 stale 后，就别再继续打商品列表刷新了",
        )
        self.assertIn("const selectedAccountId = document.getElementById('itemAccountFilter')?.value || '';", items_body)
        self.assertIn("if (String(selectedAccountId || '').trim() !== String(preservedAccountId || '').trim()) {", items_body)
        self.assertIn("const refreshedItemsLoaded = await refreshItemsData();", items_body)
        self.assertIn("!document.getElementById('items-section')?.classList.contains('active')", items_body)
        self.assertLess(
            items_body.rfind("!document.getElementById('items-section')?.classList.contains('active')", 0, items_body.index("showToast('加载商品列表失败', 'danger');")),
            items_body.index("showToast('加载商品列表失败', 'danger');"),
            "都切出商品页了，旧 root loader 失败就别再跨页甩 danger toast 了",
        )

        self.assertIn("const [filterOptionsLoaded, editorOptionsLoaded] = await Promise.all([", item_reply_body)
        self.assertIn("loadAccountOptions('itemReplayAccountFilter')", item_reply_body)
        self.assertIn("loadAccountOptions('editReplyAccountIdSelect', '选择账号')", item_reply_body)
        self.assertIn("if (filterOptionsLoaded !== true || editorOptionsLoaded !== true) {", item_reply_body)
        self.assertLess(
            item_reply_body.index("if (filterOptionsLoaded !== true || editorOptionsLoaded !== true) {"),
            item_reply_body.index("await refreshItemsReplayData();"),
            "商品回复 root loader 发现账号下拉已经 stale 后，就别再继续打回复列表刷新了",
        )
        self.assertIn("!document.getElementById('items-reply-section')?.classList.contains('active')", item_reply_body)
        self.assertLess(
            item_reply_body.rfind("!document.getElementById('items-reply-section')?.classList.contains('active')", 0, item_reply_body.index("showToast('加载商品列表失败', 'danger');")),
            item_reply_body.index("showToast('加载商品列表失败', 'danger');"),
            "都切出商品回复页了，旧 root loader 失败就别再跨页甩 danger toast 了",
        )

    def test_item_root_loaders_reset_stale_tables_before_loading_account_filters(self):
        items_body = _extract_function_body(self.app_js, "loadItems")
        item_reply_body = _extract_function_body(self.app_js, "loadItemsReplay")

        self.assertIn("resetItemsView();", items_body)
        self.assertLess(
            items_body.index("resetItemsView();"),
            items_body.index("const [accountOptionsLoaded, itemsLoaded] = await Promise.all(["),
            "商品 root loader 拉账号下拉前应先清掉旧表格，别让失败时继续挂着陈年商品数据装正常",
        )

        self.assertIn("resetItemReplaysTable();", item_reply_body)
        self.assertLess(
            item_reply_body.index("resetItemReplaysTable();"),
            item_reply_body.index("const [filterOptionsLoaded, editorOptionsLoaded] = await Promise.all(["),
            "商品回复 root loader 拉账号下拉前也得先清掉旧表格，别让失败时继续挂着陈年回复数据糊弄人",
        )

    def test_item_reply_add_modal_clears_stale_item_options(self):
        body = _extract_function_body(self.app_js, "showItemReplayEdit")
        self.assertIn("resetItemReplyEditorForm('add');", body)

    def test_item_reply_add_modal_invalidates_stale_edit_sessions_before_showing(self):
        body = _extract_function_body(self.app_js, "showItemReplayEdit")

        self.assertIn("itemReplyEditorRequestSequence += 1;", body)
        self.assertIn("itemReplyAccountItemsRequestSequence += 1;", body)
        self.assertLess(
            body.index("itemReplyEditorRequestSequence += 1;"),
            body.index("modal.show();"),
            "打开商品回复新增弹窗前应该先废掉旧编辑请求，别让老响应回来篡位",
        )
        self.assertLess(
            body.index("itemReplyAccountItemsRequestSequence += 1;"),
            body.index("modal.show();"),
            "打开商品回复新增弹窗前应该先废掉旧商品下拉请求，别让老商品列表回写当前新增会话",
        )

    def test_item_reply_add_modal_invalidates_account_item_requests_when_hidden(self):
        body = _extract_function_body(self.app_js, "showItemReplayEdit")

        self.assertIn("const modalElement = document.getElementById('editItemReplyModal');", body)
        self.assertIn("if (modalElement.dataset.itemReplyEditorModalBound !== 'true') {", body)
        self.assertIn("modalElement.addEventListener('hidden.bs.modal', () => {", body)
        self.assertIn("if (modalElement.dataset.itemReplyEditorIgnoreNextHidden === 'true') {", body)
        self.assertIn("modalElement.dataset.itemReplyEditorIgnoreNextHidden = 'false';", body)
        self.assertIn("itemReplyEditorRequestSequence += 1;", body)
        self.assertIn("itemReplyAccountItemsRequestSequence += 1;", body)
        self.assertIn("modalElement.dataset.itemReplyEditorModalBound = 'true';", body)
        self.assertLess(
            body.index("modalElement.addEventListener('hidden.bs.modal', () => {"),
            body.index("modal.show();"),
            "新增商品回复弹窗关闭后也得废掉会话，别让旧商品下拉请求回来污染隐藏弹窗",
        )

    def test_item_reply_table_uses_attribute_and_inline_js_specific_escaping_for_titles_and_actions(self):
        body = _extract_function_body(self.app_js, "displayItemReplays")
        self.assertIn("const itemId = String(item.item_id || '');", body)
        self.assertIn("const itemTitle = String(item.item_title || '未设置');", body)
        self.assertIn("const itemDetailText = getItemDetailText(item.item_detail || '');", body)
        self.assertIn("const normalizedItemDetailText = itemDetailText || '未设置';", body)
        self.assertIn("const replyContent = String(item.reply_content || '未设置');", body)
        self.assertIn("const safeAccountIdAttr = escapeHtmlAttribute(itemAccountId);", body)
        self.assertIn("const safeItemIdAttr = escapeHtmlAttribute(itemId);", body)
        self.assertIn("const safeItemTitleAttr = escapeHtmlAttribute(itemTitle);", body)
        self.assertIn("const safeItemDetailAttr = escapeHtmlAttribute(normalizedItemDetailText);", body)
        self.assertIn("const safeReplyContentAttr = escapeHtmlAttribute(replyContent);", body)
        self.assertIn("const safeAccountIdForJs = escapeInlineJsSingleQuotedString(itemAccountId);", body)
        self.assertIn("const safeItemIdForJs = escapeInlineJsSingleQuotedString(itemId);", body)
        self.assertIn("const safeItemTitleForJs = escapeInlineJsSingleQuotedString(item.item_title || item.item_id || '');", body)
        self.assertIn('data-account-id="${safeAccountIdAttr}"', body)
        self.assertIn('data-item-id="${safeItemIdAttr}"', body)
        self.assertIn('title="${safeItemTitleAttr}"', body)
        self.assertIn('title="${safeItemDetailAttr}"', body)
        self.assertIn('title="${safeReplyContentAttr}"', body)
        self.assertIn('<td title="${safeReplyContentAttr}">${escapeHtml(replyContent)}</td>', body)
        self.assertIn('onclick="editItemReply(\'${safeAccountIdForJs}\', \'${safeItemIdForJs}\')"', body)
        self.assertIn('onclick="deleteItemReply(\'${safeAccountIdForJs}\', \'${safeItemIdForJs}\', \'${safeItemTitleForJs}\')"', body)
        self.assertNotIn('data-account-id="${escapeHtml(itemAccountId)}"', body)
        self.assertNotIn('data-item-id="${escapeHtml(item.item_id)}"', body)
        self.assertNotIn("const itemDetailText = String(item.item_detail || '未设置');", body)
        self.assertNotIn("const detail = JSON.parse(item.item_detail);", body)
        self.assertNotIn('<td title="${safeReplyContentAttr}">${escapeHtml(item.reply_content)}</td>', body)
        self.assertNotIn("onclick=\"editItemReply('${escapeHtml(itemAccountId)}', '${escapeHtml(item.item_id)}')\"", body)
        self.assertNotIn("onclick=\"deleteItemReply('${escapeHtml(itemAccountId)}', '${escapeHtml(item.item_id)}', '${escapeHtml(item.item_title || item.item_id)}')\"", body)

    def test_item_reply_editor_resets_stale_form_state_and_preserves_missing_current_item_context(self):
        self.assertIn("function resetItemReplyEditorForm(mode = 'add') {", self.app_js)
        self.assertIn("function ensureItemReplyOptionExists(itemSelect, itemId, itemTitle = '', suffix = '') {", self.app_js)

        reset_body = _extract_function_body(self.app_js, "resetItemReplyEditorForm")
        ensure_option_body = _extract_function_body(self.app_js, "ensureItemReplyOptionExists")
        add_body = _extract_function_body(self.app_js, "showItemReplayEdit")
        edit_body = _extract_function_body(self.app_js, "editItemReply")

        self.assertIn("titleElement.textContent = mode === 'edit' ? '编辑商品回复' : '添加商品回复';", reset_body)
        self.assertIn("itemSelect.innerHTML = '<option value=\"\">选择商品</option>';",
                      reset_body)
        self.assertIn("replyTextarea.value = '';", reset_body)

        self.assertIn("const normalizedItemId = String(itemId || '').trim();", ensure_option_body)
        self.assertIn("const optionExists = Array.from(itemSelect.options || []).some(option => option.value === normalizedItemId);", ensure_option_body)
        self.assertIn("option.textContent = `${normalizedItemId} - ${displayTitle}${suffix}`;", ensure_option_body)
        self.assertIn("itemSelect.disabled = false;", ensure_option_body)

        self.assertIn("resetItemReplyEditorForm('add');", add_body)
        self.assertIn("resetItemReplyEditorForm('edit');", edit_body)
        self.assertIn("ensureItemReplyOptionExists(itemSelect, data.item_id, data.item_title || data.item_id, ' [商品不存在但当前回复仍在使用]');", edit_body)
        self.assertIn("resetItemReplyEditorForm('add');", edit_body)
        self.assertIn("ensureItemReplyOptionExists(itemSelect, itemId, itemId, ' [当前回复不存在，可直接新增]');", edit_body)
        self.assertIn("showToast('该商品回复不存在，可直接新增', 'warning');", edit_body)

    def test_add_card_modal_hides_stale_image_preview(self):
        body = _extract_function_body(self.app_js, "showAddCardModal")
        self.assertIn("hideCardImagePreview();", body)

    def test_switching_away_from_cards_and_delivery_sections_closes_open_modals(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("const addCardModalElement = document.getElementById('addCardModal');", show_section_body)
        self.assertIn("addCardModal.hide();", show_section_body)
        self.assertIn("const editCardModalElement = document.getElementById('editCardModal');", show_section_body)
        self.assertIn("editCardModal.hide();", show_section_body)
        self.assertIn("const addDeliveryRuleModalElement = document.getElementById('addDeliveryRuleModal');", show_section_body)
        self.assertIn("addDeliveryRuleModal.hide();", show_section_body)
        self.assertIn("const editDeliveryRuleModalElement = document.getElementById('editDeliveryRuleModal');", show_section_body)
        self.assertIn("editDeliveryRuleModal.hide();", show_section_body)

    def test_add_card_and_delivery_rule_save_flows_respect_add_modal_sessions_before_hiding_or_toasting(self):
        self.assertIn("let cardCreateRequestSequence = 0;", self.app_js)
        self.assertIn("let deliveryRuleCreateRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        show_add_card_body = _extract_function_body(self.app_js, "showAddCardModal")
        save_card_body = _extract_function_body(self.app_js, "saveCard")
        show_add_delivery_body = _extract_function_body(self.app_js, "showAddDeliveryRuleModal")
        save_delivery_body = _extract_function_body(self.app_js, "saveDeliveryRule")

        self.assertIn("if (sectionName !== 'cards') {", show_section_body)
        self.assertIn("cardCreateRequestSequence += 1;", show_section_body)
        self.assertIn("if (sectionName !== 'auto-delivery') {", show_section_body)
        self.assertIn("deliveryRuleCreateRequestSequence += 1;", show_section_body)

        self.assertIn("if (modalElement.dataset.cardCreateIgnoreNextHidden === 'true') {", show_add_card_body)
        self.assertIn("modalElement.dataset.cardCreateIgnoreNextHidden = 'false';", show_add_card_body)
        self.assertIn("cardCreateRequestSequence += 1;", show_add_card_body)

        self.assertIn("const requestSequence = cardCreateRequestSequence;", save_card_body)
        self.assertIn("requestSequence !== cardCreateRequestSequence", save_card_body)
        self.assertIn("modalElement.dataset.cardCreateIgnoreNextHidden = 'true';", save_card_body)
        self.assertIn("return null;", save_card_body)
        self.assertLess(
            save_card_body.index("requestSequence !== cardCreateRequestSequence"),
            save_card_body.index("modal.hide();"),
            "旧的卡券新增响应不该回来把已经重开的新增弹窗又关掉",
        )
        self.assertLess(
            save_card_body.rfind("requestSequence !== cardCreateRequestSequence", 0, save_card_body.index("showToast(`保存失败: ${errorMessage}`, 'danger');")),
            save_card_body.index("showToast(`保存失败: ${errorMessage}`, 'danger');"),
            "旧的卡券新增失败响应不该在已经重开的弹窗会话里乱弹 danger toast",
        )

        self.assertIn("if (modalElement.dataset.deliveryRuleCreateIgnoreNextHidden === 'true') {", show_add_delivery_body)
        self.assertIn("modalElement.dataset.deliveryRuleCreateIgnoreNextHidden = 'false';", show_add_delivery_body)
        self.assertIn("deliveryRuleCreateRequestSequence += 1;", show_add_delivery_body)

        self.assertIn("const requestSequence = deliveryRuleCreateRequestSequence;", save_delivery_body)
        self.assertIn("requestSequence !== deliveryRuleCreateRequestSequence", save_delivery_body)
        self.assertIn("modalElement.dataset.deliveryRuleCreateIgnoreNextHidden = 'true';", save_delivery_body)
        self.assertIn("return null;", save_delivery_body)
        self.assertLess(
            save_delivery_body.index("requestSequence !== deliveryRuleCreateRequestSequence"),
            save_delivery_body.index("modal.hide();"),
            "旧的发货规则新增响应不该回来把已经重开的新增弹窗又关掉",
        )
        self.assertLess(
            save_delivery_body.rfind("requestSequence !== deliveryRuleCreateRequestSequence", 0, save_delivery_body.index("showToast(`保存失败: ${error}`, 'danger');")),
            save_delivery_body.index("showToast(`保存失败: ${error}`, 'danger');"),
            "旧的发货规则新增失败响应不该在已经重开的弹窗会话里乱弹 danger toast",
        )

    def test_add_card_and_delivery_rule_save_actions_ignore_older_same_modal_responses(self):
        save_card_body = _extract_function_body(self.app_js, "saveCard")
        save_delivery_body = _extract_function_body(self.app_js, "saveDeliveryRule")

        for body, sequence_name, hide_fragment, failure_fragment in (
            (
                save_card_body,
                "cardMutationActionRequestSequence",
                "modal.hide();",
                "showToast(`保存失败: ${errorMessage}`, 'danger');",
            ),
            (
                save_delivery_body,
                "deliveryRuleMutationActionRequestSequence",
                "modal.hide();",
                "showToast(`保存失败: ${error}`, 'danger');",
            ),
        ):
            with self.subTest(sequence_name=sequence_name):
                self.assertIn(f"++{sequence_name}", body)
                self.assertIn(f"actionRequestSequence !== {sequence_name}", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index(f"actionRequestSequence !== {sequence_name}"),
                    body.index(hide_fragment),
                    "同一新增弹窗里第二次保存已经发出后，第一次响应不该回来把当前弹窗关掉",
                )
                self.assertLess(
                    body.rfind(f"actionRequestSequence !== {sequence_name}", 0, body.index(failure_fragment)),
                    body.index(failure_fragment),
                    "同一新增弹窗里旧的失败响应不该晚回来后拿旧错误糊当前会话一脸",
                )

    def test_card_and_delivery_mutations_parse_error_payloads_with_helper_before_toasting(self):
        save_card_body = _extract_function_body(self.app_js, "saveCard")
        update_card_body = _extract_function_body(self.app_js, "updateCard")
        update_card_image_body = _extract_function_body(self.app_js, "updateCardWithImage")
        delete_card_body = _extract_function_body(self.app_js, "deleteCard")
        save_delivery_body = _extract_function_body(self.app_js, "saveDeliveryRule")
        update_delivery_body = _extract_function_body(self.app_js, "updateDeliveryRule")
        delete_delivery_body = _extract_function_body(self.app_js, "deleteDeliveryRule")

        self.assertIn("const errorMessage = await readResponseErrorMessage(uploadResponse, `HTTP ${uploadResponse.status}`);", save_card_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", save_card_body)
        self.assertLess(
            save_card_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
            save_card_body.index("showToast(`保存失败: ${errorMessage}`, 'danger');"),
            "卡券新增失败时得先把 detail/message 解出来，别把 JSON 原文直接甩给人看",
        )

        for body, failure_fragment, label in (
            (update_card_body, "showToast(`更新失败: ${error}`, 'danger');", "卡券更新"),
            (update_card_image_body, "showToast(`更新失败: ${error}`, 'danger');", "图片卡券更新"),
            (delete_card_body, "showToast(`删除失败: ${error}`, 'danger');", "卡券删除"),
            (save_delivery_body, "showToast(`保存失败: ${error}`, 'danger');", "发货规则新增"),
            (update_delivery_body, "showToast(`更新失败: ${error}`, 'danger');", "发货规则更新"),
            (delete_delivery_body, "showToast(`删除失败: ${error}`, 'danger');", "发货规则删除"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(failure_fragment),
                    f"{label}失败时得先把 detail/message 解出来，别把 JSON 原文直接甩给人看",
                )

    def test_card_and_delivery_mutation_catch_failures_surface_runtime_error_messages(self):
        for body, legacy_toast, legacy_toast_count, runtime_toast, guard_fragments, label in (
            (
                _extract_function_body(self.app_js, "saveCard"),
                "showToast(`网络错误: ${error.message}`, 'danger');",
                0,
                "showToast(`保存卡券失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== cardMutationActionRequestSequence",
                    "requestSequence !== cardCreateRequestSequence",
                    "!document.getElementById('cards-section')?.classList.contains('active')",
                ),
                "保存卡券",
            ),
            (
                _extract_function_body(self.app_js, "updateCard"),
                "showToast('更新卡券失败', 'danger');",
                0,
                "showToast(`更新卡券失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "(actionRequestSequence && actionRequestSequence !== cardMutationActionRequestSequence)",
                    "requestSequence !== cardEditRequestSequence",
                    "!document.getElementById('cards-section')?.classList.contains('active')",
                ),
                "更新卡券",
            ),
            (
                _extract_function_body(self.app_js, "updateCardWithImage"),
                "showToast('更新卡券失败', 'danger');",
                0,
                "showToast(`更新卡券失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== cardMutationActionRequestSequence",
                    "requestSequence !== cardEditRequestSequence",
                    "!document.getElementById('cards-section')?.classList.contains('active')",
                ),
                "更新图片卡券",
            ),
            (
                _extract_function_body(self.app_js, "deleteCard"),
                "showToast('删除卡券失败', 'danger');",
                0,
                "showToast(`删除卡券失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== cardMutationActionRequestSequence",
                    "!document.getElementById('cards-section')?.classList.contains('active')",
                ),
                "删除卡券",
            ),
            (
                _extract_function_body(self.app_js, "saveDeliveryRule"),
                "showToast('保存发货规则失败', 'danger');",
                0,
                "showToast(`保存发货规则失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== deliveryRuleMutationActionRequestSequence",
                    "requestSequence !== deliveryRuleCreateRequestSequence",
                    "!document.getElementById('auto-delivery-section')?.classList.contains('active')",
                ),
                "保存发货规则",
            ),
            (
                _extract_function_body(self.app_js, "updateDeliveryRule"),
                "showToast('更新发货规则失败', 'danger');",
                0,
                "showToast(`更新发货规则失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== deliveryRuleMutationActionRequestSequence",
                    "requestSequence !== deliveryRuleEditRequestSequence",
                    "!document.getElementById('auto-delivery-section')?.classList.contains('active')",
                ),
                "更新发货规则",
            ),
            (
                _extract_function_body(self.app_js, "deleteDeliveryRule"),
                "showToast('删除发货规则失败', 'danger');",
                0,
                "showToast(`删除发货规则失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== deliveryRuleMutationActionRequestSequence",
                    "!document.getElementById('auto-delivery-section')?.classList.contains('active')",
                ),
                "删除发货规则",
            ),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    body.count(legacy_toast),
                    legacy_toast_count,
                    f"{label} catch 别再甩固定红字了，真实异常都拿到了还装聋就挺离谱",
                )
                self.assertIn(runtime_toast, body)
                toast_index = body.index(runtime_toast)
                for guard_fragment in guard_fragments:
                    self.assertIn(guard_fragment, body)
                    self.assertLess(
                        body.rfind(guard_fragment, 0, toast_index),
                        toast_index,
                        f"{label} catch 里的旧异常在弹 toast 前也得先过会话/页面活性校验，别 stale 了还回来抽风",
                    )

    def test_toggle_card_type_fields_does_not_force_text_when_type_is_empty(self):
        body = _extract_function_body(self.app_js, "toggleCardTypeFields")
        self.assertNotIn("?.value || 'text'", body)

    def test_delivery_rules_do_not_expose_placeholder_test_action(self):
        self.assertNotIn("onclick=\"testDeliveryRule(${rule.id})\"", self.app_js)
        self.assertNotIn("function testDeliveryRule(", self.app_js)

    def test_data_management_prefers_hidden_admin_rowid_for_delete_actions(self):
        display_body = _extract_function_body(self.app_js, "displayTableData")
        delete_body = _extract_function_body(self.app_js, "deleteRecord")

        self.assertIn("const recordId = row.__admin_rowid ||", display_body)
        self.assertIn("currentDeleteId = record.__admin_rowid ||", delete_body)

    def test_data_management_resets_stale_state_before_permission_check(self):
        load_body = _extract_function_body(self.app_js, "loadDataManagement")

        self.assertIn("currentTable = '';", load_body)
        self.assertIn("currentData = [];", load_body)
        self.assertIn("showNoTableSelected();", load_body)
        self.assertIn("tableSelect.value = '';", load_body)
        self.assertLess(
            load_body.index("currentTable = '';"),
            load_body.index("const response = await fetch(`${apiBase}/verify`, {"),
            "数据管理做权限校验前应先清空旧表状态，别让无权限页面继续挂着陈年数据",
        )

    def test_data_management_table_titles_and_delete_preview_escape_attribute_and_html_contexts(self):
        display_body = _extract_function_body(self.app_js, "displayTableData")
        delete_body = _extract_function_body(self.app_js, "deleteRecord")

        self.assertIn("const safeTitleAttr = escapeHtmlAttribute(String(value));", display_body)
        self.assertIn('value = `<span title="${safeTitleAttr}">${escapeHtml(value.substring(0, 50))}...</span>`;', display_body)
        self.assertNotIn('title="${escapeHtml(value)}"', display_body)

        self.assertIn("const safeKey = escapeHtml(String(key || ''));", delete_body)
        self.assertIn("const safeValue = escapeHtml(String(record[key] ?? '-'));", delete_body)
        self.assertIn("div.innerHTML = `<strong>${safeKey}:</strong> ${safeValue}`;", delete_body)
        self.assertNotIn("div.innerHTML = `<strong>${key}:</strong> ${record[key] || '-'}`;", delete_body)

    def test_data_management_failed_or_empty_load_resets_stale_summary_and_disables_clear_action(self):
        no_data_body = _extract_function_body(self.app_js, "showNoData")
        load_body = _extract_function_body(self.app_js, "loadTableData")

        self.assertIn("document.getElementById('recordCount').textContent = '-';", no_data_body)
        self.assertIn("document.getElementById('tableTitle').innerHTML = '<i class=\"bi bi-table\"></i> 数据表';", no_data_body)
        self.assertIn("document.getElementById('exportBtn').disabled = true;", no_data_body)
        self.assertIn("document.getElementById('clearBtn').disabled = true;", no_data_body)
        self.assertIn("currentData = [];", no_data_body)

        self.assertIn("currentData = [];", load_body)
        self.assertLess(
            load_body.index("currentData = [];"),
            load_body.index("const response = await fetch(`/admin/data/${selectedTable}`, {"),
            "数据管理重新加载前应先清空旧 currentData，避免失败后残留旧表数据",
        )

    def test_data_management_show_loading_disables_clear_action_and_resets_summary(self):
        body = _extract_function_body(self.app_js, "showLoading")
        self.assertIn("document.getElementById('recordCount').textContent = '-';", body)
        self.assertIn("document.getElementById('tableTitle').innerHTML = '<i class=\"bi bi-table\"></i> 数据表';", body)
        self.assertIn("document.getElementById('exportBtn').disabled = true;", body)
        self.assertIn("document.getElementById('clearBtn').disabled = true;", body)

    def test_data_management_toolbar_exposes_export_action_and_enables_it_only_when_data_is_ready(self):
        no_table_body = _extract_function_body(self.app_js, "showNoTableSelected")
        update_body = _extract_function_body(self.app_js, "updateTableInfo")

        self.assertIn('onclick="exportTableData()"', self.index_html)
        self.assertIn('id="exportBtn"', self.index_html)
        self.assertIn("document.getElementById('exportBtn').disabled = true;", no_table_body)
        self.assertIn("document.getElementById('exportBtn').disabled = false;", update_body)
        self.assertLess(
            self.index_html.index('id="exportBtn"'),
            self.index_html.index('id="clearBtn"'),
            "数据管理卡片既然先导出再清空，按钮顺序就别反着摆，省得手滑直接把表扬了",
        )

    def test_data_management_users_table_keeps_clear_disabled_and_blocks_bulk_clear(self):
        update_body = _extract_function_body(self.app_js, "updateTableInfo")
        clear_body = _extract_function_body(self.app_js, "clearTableData")

        self.assertIn("document.getElementById('clearBtn').disabled = tableName === 'users';", update_body)
        self.assertNotIn("document.getElementById('clearBtn').disabled = false;", update_body)

        self.assertIn("if (clearTable === 'users') {", clear_body)
        self.assertIn("showToast('用户表不支持整表清空，请使用用户管理或逐条删除', 'warning');", clear_body)
        self.assertLess(
            clear_body.index("if (clearTable === 'users') {"),
            clear_body.index("const confirmed = confirm("),
            "users 表都明令禁止整表清空了，就别还先弹确认框吓唬人，整得跟逗闷子似的",
        )
        self.assertLess(
            clear_body.index("if (clearTable === 'users') {"),
            clear_body.index("fetch(`/admin/data/${clearTable}`, {"),
            "users 表前端得先把整表清空拦住，别还把必失败请求照样往后端怼",
        )

    def test_data_management_no_table_selected_state_clears_stale_current_table_and_cached_rows(self):
        no_table_body = _extract_function_body(self.app_js, "showNoTableSelected")
        load_body = _extract_function_body(self.app_js, "loadTableData")

        self.assertIn("currentTable = '';", no_table_body)
        self.assertIn("currentData = [];", no_table_body)

        no_selection_index = load_body.index("if (!selectedTable) {")
        self.assertLess(
            load_body.index("showNoTableSelected();", no_selection_index),
            load_body.index("return false;", no_selection_index),
            "数据表下拉切回空选项时不能只把界面装成未选择，得先走空状态重置，不然 refresh 还会拿上一张表回魂",
        )

    def test_data_management_load_ignores_stale_async_table_responses(self):
        self.assertIn("let dataTableRequestSequence = 0;", self.app_js)
        body = _extract_function_body(self.app_js, "loadTableData")

        self.assertIn("const requestSequence = ++dataTableRequestSequence;", body)
        self.assertLess(
            body.index("const requestSequence = ++dataTableRequestSequence;"),
            body.index("if (!selectedTable) {"),
            "切到空表或别的表时也得先让旧请求失效，别让慢请求回来瞎改页面",
        )
        self.assertIn("if (requestSequence !== dataTableRequestSequence || tableSelect.value !== selectedTable) {", body)
        self.assertIn("return false;", body)
        self.assertLess(
            body.index("if (requestSequence !== dataTableRequestSequence || tableSelect.value !== selectedTable) {"),
            body.index("currentData = data.data;"),
            "过期的表数据请求不该再去覆盖 currentData 和表格内容",
        )

    def test_data_management_permission_gate_ignores_stale_verify_responses_after_section_switch(self):
        self.assertIn("let dataManagementLoadRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_body = _extract_function_body(self.app_js, "loadDataManagement")

        self.assertIn("if (sectionName !== 'data-management') {", show_section_body)
        self.assertIn("dataManagementLoadRequestSequence += 1;", show_section_body)
        self.assertIn("dataTableRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++dataManagementLoadRequestSequence;", load_body)
        self.assertIn("requestSequence !== dataManagementLoadRequestSequence", load_body)
        self.assertIn("!document.getElementById('data-management-section')?.classList.contains('active')", load_body)
        self.assertIn("return null;", load_body)
        self.assertLess(
            load_body.index("requestSequence !== dataManagementLoadRequestSequence"),
            load_body.index("showSection('dashboard');"),
            "数据管理权限校验晚回来时，不该把已经切走的人硬拽回 dashboard",
        )

    def test_data_management_root_loader_does_not_emit_cross_page_permission_failure_toasts(self):
        body = _extract_function_body(self.app_js, "loadDataManagement")
        toast_fragment = "showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');"

        self.assertIn("!document.getElementById('data-management-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        catch_index = body.index("} catch (error) {")
        catch_toast_index = body.rfind(toast_fragment)
        self.assertLess(
            body.find("!document.getElementById('data-management-section')?.classList.contains('active')", catch_index),
            catch_toast_index,
            "都切出数据管理页了，旧的权限校验失败就别再跨页甩 danger toast 了",
        )

    def test_data_management_root_loader_handles_unauthorized_and_reads_structured_permission_errors(self):
        body = _extract_function_body(self.app_js, "loadDataManagement")

        self.assertIn("if (handleUnauthorizedApiResponse(response)) {", body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
        self.assertIn("throw new Error(errorMessage);", body)
        self.assertIn("showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');", body)

        unauthorized_index = body.index("if (handleUnauthorizedApiResponse(response)) {")
        response_ok_index = body.index("if (response.ok) {")
        error_index = body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = body.index("throw new Error(errorMessage);", error_index)
        toast_index = body.index("showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');")

        self.assertLess(
            unauthorized_index,
            response_ok_index,
            "数据管理权限校验遇到 401 得先走统一未授权处理，别后面还继续装模作样做管理员判断",
        )
        self.assertLess(
            error_index,
            throw_index,
            "数据管理权限校验 HTTP 失败时得先把 detail/message 解出来，别固定甩一句权限验证失败糊弄人",
        )
        self.assertLess(
            body.find("requestSequence !== dataManagementLoadRequestSequence", error_index),
            throw_index,
            "数据管理权限校验旧失败响应读完错误体后，先验 root loader 会话还活着，再决定要不要抛错",
        )
        self.assertLess(
            throw_index,
            toast_index,
            "数据管理权限校验应把真实后端错误带进 catch toast，别把错误体又吞回固定红字",
        )

    def test_data_management_table_loader_ignores_hidden_section_and_http_failures(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_body = _extract_function_body(self.app_js, "loadTableData")

        self.assertIn("dataTableRequestSequence += 1;", show_section_body)
        self.assertIn("if (!response.ok) {", load_body)
        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("!document.getElementById('data-management-section')?.classList.contains('active')", load_body)
        self.assertLess(
            load_body.index("!document.getElementById('data-management-section')?.classList.contains('active')"),
            load_body.index("currentData = data.data;"),
            "数据管理页面都切走了，旧表数据请求就别回来继续往隐藏页面灌了",
        )

    def test_data_management_table_loader_reads_structured_http_errors_before_throwing_and_toasting(self):
        load_body = _extract_function_body(self.app_js, "loadTableData")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("showToast(`加载数据失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        self.assertIn("if (!data || typeof data !== 'object') {", load_body)
        self.assertIn("if (data.success && (!Array.isArray(data.data) || !Array.isArray(data.columns))) {", load_body)
        self.assertIn("throw new Error('数据表返回格式异常');", load_body)

        error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        throw_index = load_body.index("throw new Error(errorMessage);", error_index)
        toast_index = load_body.index("showToast(`加载数据失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            error_index,
            throw_index,
            "数据管理表格 HTTP 失败时得先把 detail/message 解出来，别整固定报错装大尾巴狼",
        )
        self.assertLess(
            load_body.find("requestSequence !== dataTableRequestSequence || tableSelect.value !== selectedTable", error_index),
            throw_index,
            "数据管理旧失败响应读完错误体后，先验 table request，别让过期请求再往 catch 里钻",
        )
        self.assertLess(
            load_body.find("!document.getElementById('data-management-section')?.classList.contains('active')", error_index),
            throw_index,
            "数据管理页面都切走了，旧失败响应读完错误体也别继续抛异常刷存在感",
        )
        self.assertLess(
            load_body.rfind("if (requestSequence !== dataTableRequestSequence || tableSelect.value !== selectedTable) {", 0, toast_index),
            toast_index,
            "数据管理旧失败响应进了 catch 以后，也得先复验表选择还对不对，别跨会话乱弹红字",
        )
        self.assertLess(
            load_body.rfind("if (!document.getElementById('data-management-section')?.classList.contains('active')) {", 0, toast_index),
            toast_index,
            "都切出数据管理页了，旧失败响应进了 catch 也别再跨页甩 danger toast",
        )

    def test_data_management_refresh_only_reports_success_when_reload_succeeds(self):
        load_body = _extract_function_body(self.app_js, "loadTableData")
        refresh_body = _extract_function_body(self.app_js, "refreshTableData")

        self.assertIn("return true;", load_body)
        self.assertIn("return false;", load_body)
        self.assertIn("const loaded = await loadTableData();", refresh_body)
        self.assertIn("if (loaded) {", refresh_body)
        self.assertIn("showToast('数据已刷新', 'success');", refresh_body)
        self.assertIn("} else if (loaded === false) {", refresh_body)
        self.assertIn("showToast('数据刷新失败，请稍后重试', 'danger');", refresh_body)
        self.assertNotIn("loadTableData();\n        showToast('数据已刷新', 'success');", refresh_body)

    def test_refresh_table_data_does_not_emit_cross_page_toasts_after_leaving_data_management(self):
        body = _extract_function_body(self.app_js, "refreshTableData")
        toast_fragment = "showToast('数据已刷新', 'success');"

        self.assertIn("!document.getElementById('data-management-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        self.assertLess(
            body.index("!document.getElementById('data-management-section')?.classList.contains('active')"),
            body.index(toast_fragment),
            "都切出数据管理页了，旧的刷新成功结果就别再跨页弹 success toast 了",
        )

    def test_data_management_export_uses_requested_table_identity_through_async_download(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        body = _extract_function_body(self.app_js, "exportTableData")

        self.assertIn("let dataManagementExportActionRequestSequence = 0;", self.app_js)
        self.assertIn("dataManagementExportActionRequestSequence += 1;", show_section_body)
        self.assertIn("const exportTable = currentTable;", body)
        self.assertIn("const actionRequestSequence = ++dataManagementExportActionRequestSequence;", body)
        self.assertIn("fetch(`/admin/data/${exportTable}/export`, {", body)
        self.assertIn("let downloadName = `${exportTable}_${getBeijingDateKey(new Date())}.xlsx`;", body)
        self.assertIn("const contentDisposition = response.headers.get('content-disposition');", body)
        self.assertIn("a.download = downloadName;", body)
        self.assertIn("actionRequestSequence !== dataManagementExportActionRequestSequence", body)
        self.assertIn("currentTable !== exportTable", body)
        self.assertNotIn("a.download = `${currentTable}_${getBeijingDateKey(new Date())}.xlsx`;", body)
        self.assertLess(
            body.index("actionRequestSequence !== dataManagementExportActionRequestSequence"),
            body.index("const blob = await response.blob();"),
            "同页已经点了新的数据导出动作，旧响应就别继续展开下载内容了",
        )

    def test_data_management_export_does_not_emit_cross_page_toasts_after_leaving_section(self):
        body = _extract_function_body(self.app_js, "exportTableData")

        for toast_fragment in (
            "showToast('数据导出成功', 'success');",
            "showToast(`导出失败: ${error}`, 'danger');",
            "showToast(`导出数据失败: ${error.message || '请稍后重试'}`, 'danger');",
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('data-management-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.rfind("!document.getElementById('data-management-section')?.classList.contains('active')", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    "都切出数据管理页了，旧的导出结果不该再跨页弹 toast 刷存在感",
                )

    def test_data_management_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        load_body = _extract_function_body(self.app_js, "loadDataManagement")
        export_body = _extract_function_body(self.app_js, "exportTableData")
        clear_body = _extract_function_body(self.app_js, "clearTableData")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        for body, unauthorized_fragment, anchor_fragment in (
            (load_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (export_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (clear_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (delete_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "数据管理这些 raw fetch 遇到 401 得先滚去登录，别后面还继续做权限判断、导出、清空、删记录",
                )

    def test_data_management_failure_actions_read_structured_error_messages(self):
        export_body = _extract_function_body(self.app_js, "exportTableData")
        clear_body = _extract_function_body(self.app_js, "clearTableData")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        for body, toast_fragment, label in (
            (export_body, "showToast(`导出失败: ${error}`, 'danger');", "导出数据表"),
            (clear_body, "showToast(`清空失败: ${error}`, 'danger');", "清空数据表"),
            (delete_body, "showToast(`删除失败: ${error}`, 'danger');", "删除数据记录"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(toast_fragment),
                    f"{label}失败时得先把错误体解明白，别拿个固定失败提示在那糊弄鬼",
                )

    def test_data_management_failure_toasts_recheck_stale_state_after_error_body_read(self):
        export_body = _extract_function_body(self.app_js, "exportTableData")
        clear_body = _extract_function_body(self.app_js, "clearTableData")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        export_error_index = export_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        export_toast_index = export_body.index("showToast(`导出失败: ${error}`, 'danger');")
        self.assertLess(
            export_body.find("actionRequestSequence !== dataManagementExportActionRequestSequence", export_error_index),
            export_toast_index,
            "同页已经点了新的导出动作后，旧失败响应读完错误体也别回来回魂甩红字",
        )
        self.assertLess(
            export_body.find("currentTable !== exportTable", export_error_index),
            export_toast_index,
            "导出目标表都变了，旧失败响应读完错误体也别再对着新表乱弹错误",
        )
        self.assertLess(
            export_body.find("!document.getElementById('data-management-section')?.classList.contains('active')", export_error_index),
            export_toast_index,
            "都切出数据管理页了，旧导出失败响应读完错误体也别跨页弹 danger toast",
        )

        clear_error_index = clear_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        clear_toast_index = clear_body.index("showToast(`清空失败: ${error}`, 'danger');")
        self.assertLess(
            clear_body.find("actionRequestSequence !== dataManagementMutationActionRequestSequence", clear_error_index),
            clear_toast_index,
            "同页已经发起新的清空动作后，旧失败响应读完错误体也别回来回魂甩红字",
        )
        self.assertLess(
            clear_body.find("currentTable !== clearTable", clear_error_index),
            clear_toast_index,
            "清空目标表都切了，旧失败响应读完错误体也别对着新表乱弹错误",
        )
        self.assertLess(
            clear_body.find("!document.getElementById('data-management-section')?.classList.contains('active')", clear_error_index),
            clear_toast_index,
            "都切出数据管理页了，旧清空失败响应读完错误体也别跨页弹 danger toast",
        )

        delete_error_index = delete_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        delete_toast_index = delete_body.index("showToast(`删除失败: ${error}`, 'danger');")
        self.assertLess(
            delete_body.find("actionRequestSequence !== dataManagementMutationActionRequestSequence", delete_error_index),
            delete_toast_index,
            "同页已经发起新的删记录动作后，旧失败响应读完错误体也别回来诈尸甩红字",
        )
        self.assertLess(
            delete_body.find("requestSequence !== dataDeleteModalRequestSequence", delete_error_index),
            delete_toast_index,
            "删除确认弹窗都换会话了，旧失败响应读完错误体也别回来顶掉新会话结果",
        )
        self.assertLess(
            delete_body.find("currentTable !== deleteTable", delete_error_index),
            delete_toast_index,
            "删记录目标表都切了，旧失败响应读完错误体也别对着新表乱弹错误",
        )
        self.assertLess(
            delete_body.find("!document.getElementById('data-management-section')?.classList.contains('active')", delete_error_index),
            delete_toast_index,
            "都切出数据管理页了，旧删记录失败响应读完错误体也别跨页弹 danger toast",
        )

    def test_data_management_mutations_capture_table_identity_before_async_roundtrip(self):
        clear_body = _extract_function_body(self.app_js, "clearTableData")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        self.assertIn("const clearTable = currentTable;", clear_body)
        self.assertIn("const description = tableDescriptions[clearTable] || clearTable;", clear_body)
        self.assertIn("fetch(`/admin/data/${clearTable}`, {", clear_body)
        self.assertIn("currentTable !== clearTable", clear_body)
        self.assertNotIn("fetch(`/admin/data/${currentTable}`, {", clear_body)
        self.assertLess(
            clear_body.index("currentTable !== clearTable"),
            clear_body.index("const loaded = await loadTableData();"),
            "切到别的表后，旧的清空响应别再回来刷新当前表格",
        )

        self.assertIn("const deleteTable = currentTable;", delete_body)
        self.assertIn("const deleteId = currentDeleteId;", delete_body)
        self.assertIn("const url = `/admin/data/${deleteTable}/${deleteId}`;", delete_body)
        self.assertIn("currentTable !== deleteTable", delete_body)
        self.assertNotIn("const url = `/admin/data/${currentTable}/${currentDeleteId}`;", delete_body)
        self.assertLess(
            delete_body.index("currentTable !== deleteTable"),
            delete_body.index("deleteRecordModal.hide();"),
            "切到别的表后，旧的删记录响应不该回来把当前会话的弹窗又关掉",
        )

    def test_download_flows_share_resilient_content_disposition_filename_parser(self):
        self.assertIn("function resolveDownloadFileName(contentDisposition, fallbackName) {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "resolveDownloadFileName")
        backup_body = _extract_function_body(self.app_js, "downloadDatabaseBackup")
        export_keywords_body = _extract_function_body(self.app_js, "exportKeywords")
        export_table_body = _extract_function_body(self.app_js, "exportTableData")
        download_log_body = _extract_function_body(self.app_js, "downloadLogFile")

        self.assertIn("const encodedMatch = contentDisposition.match(/filename\\*=UTF-8''([^;]+)/i);", helper_body)
        self.assertIn('const quotedMatch = contentDisposition.match(/filename="([^"]+)"/i);', helper_body)
        self.assertIn("const plainMatch = contentDisposition.match(/filename=([^;]+)/i);", helper_body)
        self.assertIn("return decodeURIComponent(rawName);", helper_body)
        self.assertIn("return rawName;", helper_body)

        self.assertIn("const filename = resolveDownloadFileName(contentDisposition, 'xianyu_backup.db');", backup_body)
        self.assertIn("let fileName = resolveDownloadFileName(contentDisposition,", export_keywords_body)
        self.assertIn("downloadName = resolveDownloadFileName(contentDisposition, downloadName);", export_table_body)
        self.assertIn("downloadName = resolveDownloadFileName(contentDisposition, downloadName);", download_log_body)

    def test_log_file_download_does_not_emit_cross_page_toasts_after_leaving_logs(self):
        body = _extract_function_body(self.app_js, "downloadLogFile")
        self.assertIn("!document.getElementById('logs-section')?.classList.contains('active')", body)
        self.assertIn("return;", body)
        self.assertLess(
            body.index("!document.getElementById('logs-section')?.classList.contains('active')"),
            body.index("showToast(`日志下载失败: ${message || response.status}`, 'danger');"),
            "都切出日志页了，旧下载失败请求不该跨页回来甩 danger toast",
        )
        self.assertLess(
            body.rfind("!document.getElementById('logs-section')?.classList.contains('active')", 0, body.index("showToast('日志下载成功', 'success');")),
            body.index("showToast('日志下载成功', 'success');"),
            "都切出日志页了，旧下载成功请求也别回来刷 success toast",
        )
        finally_block = body.split("} finally {", 1)[1]
        self.assertIn("!document.getElementById('logs-section')?.classList.contains('active')", finally_block)
        self.assertLess(
            finally_block.index("!document.getElementById('logs-section')?.classList.contains('active')"),
            finally_block.index("buttonEl.disabled = false;"),
            "都切出日志页了，旧下载 finally 就别把当前按钮 disabled 状态回写回去了",
        )
        self.assertLess(
            finally_block.index("!document.getElementById('logs-section')?.classList.contains('active')"),
            finally_block.index("buttonEl.innerHTML = originalHtml || '<i class=\"bi bi-download me-1\"></i>下载';"),
            "都切出日志页了，旧下载 finally 也别把当前按钮文案还原成老状态",
        )

    def test_log_file_download_respects_modal_session_before_triggering_download_or_toasts(self):
        self.assertIn("let logFileModalRequestSequence = 0;", self.app_js)
        open_body = _extract_function_body(self.app_js, "openLogExportModal")
        body = _extract_function_body(self.app_js, "downloadLogFile")

        self.assertGreaterEqual(
            open_body.count("logFileModalRequestSequence += 1;"),
            2,
            "日志导出弹窗开关都得推进独立 session，不然旧下载请求会在重开弹窗后回魂",
        )
        self.assertIn("const modalRequestSequence = logFileModalRequestSequence;", body)
        self.assertIn("modalRequestSequence !== logFileModalRequestSequence", body)

        error_text_index = body.index("const message = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        danger_toast_index = body.index("showToast(`日志下载失败: ${message || response.status}`, 'danger');")
        self.assertLess(
            body.find("modalRequestSequence !== logFileModalRequestSequence", error_text_index),
            danger_toast_index,
            "日志导出弹窗都关了又重开了，旧失败响应读完错误文本也别回来甩红字",
        )

        blob_index = body.index("const blob = await response.blob();")
        download_side_effect_index = body.index("const url = window.URL.createObjectURL(blob);")
        self.assertLess(
            body.find("modalRequestSequence !== logFileModalRequestSequence", blob_index),
            download_side_effect_index,
            "日志导出弹窗都切到新会话了，旧成功响应读完 blob 也别真把文件给你落下来",
        )

        success_toast_index = body.index("showToast('日志下载成功', 'success');")
        self.assertLess(
            body.rfind("modalRequestSequence !== logFileModalRequestSequence", 0, success_toast_index),
            success_toast_index,
            "日志导出弹窗重开后，旧下载成功响应别回来刷 success toast 装自己还活着",
        )

        finally_block = body.split("} finally {", 1)[1]
        self.assertIn("modalRequestSequence !== logFileModalRequestSequence", finally_block)
        self.assertLess(
            finally_block.index("modalRequestSequence !== logFileModalRequestSequence"),
            finally_block.index("buttonEl.disabled = false;"),
            "日志导出弹窗都换 session 了，旧下载 finally 别碰新会话的按钮状态",
        )

    def test_log_file_download_catch_toast_surfaces_runtime_errors(self):
        body = _extract_function_body(self.app_js, "downloadLogFile")

        self.assertIn("showToast(`下载日志文件失败: ${error.message || '请稍后重试'}`, 'danger');", body)
        self.assertNotIn("showToast('下载日志文件失败，请稍后重试', 'danger');", body)

        catch_index = body.index("} catch (error) {")
        toast_index = body.index("showToast(`下载日志文件失败: ${error.message || '请稍后重试'}`, 'danger');", catch_index)

        self.assertLess(
            body.find("modalRequestSequence !== logFileModalRequestSequence", catch_index),
            toast_index,
            "日志导出弹窗都换 session 了，旧异常别回来甩固定红字",
        )
        self.assertLess(
            body.find("!document.getElementById('logs-section')?.classList.contains('active')", catch_index),
            toast_index,
            "都切出日志页了，旧下载异常也别跨页弹 danger toast",
        )

    def test_backup_download_actions_do_not_emit_cross_page_toasts_after_leaving_system_settings(self):
        download_body = _extract_function_body(self.app_js, "downloadDatabaseBackup")
        export_body = _extract_function_body(self.app_js, "exportBackup")

        for body, error_fragment, success_fragment in (
            (
                download_body,
                "showToast(`下载失败: ${error}`, 'danger');",
                "showToast('数据库备份下载成功', 'success');",
            ),
            (
                export_body,
                "showToast(`导出失败: ${error}`, 'danger');",
                "showToast('备份导出成功', 'success');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!isSystemSettingsSectionActive()", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!isSystemSettingsSectionActive()"),
                    body.index(error_fragment),
                    "都切出系统设置页了，旧下载失败请求不该跨页回来甩 danger toast",
                )
                self.assertLess(
                    body.rfind("!isSystemSettingsSectionActive()", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "都切出系统设置页了，旧下载成功请求也别回来刷 success toast",
                )

    def test_backup_management_catch_toasts_surface_runtime_error_messages(self):
        download_body = _extract_function_body(self.app_js, "downloadDatabaseBackup")
        upload_body = _extract_function_body(self.app_js, "uploadDatabaseBackup")
        export_body = _extract_function_body(self.app_js, "exportBackup")
        import_body = _extract_function_body(self.app_js, "importBackup")

        for body, console_fragment, toast_fragment, stale_fragment, label in (
            (
                download_body,
                "console.error('下载数据库备份失败:', error);",
                "showToast(`下载数据库备份失败: ${error.message || '请稍后重试'}`, 'danger');",
                "actionRequestSequence !== backupManagementActionRequestSequence",
                "下载数据库备份",
            ),
            (
                upload_body,
                "console.error('上传数据库备份失败:', error);",
                "showToast(`上传数据库备份失败: ${error.message || '请稍后重试'}`, 'danger');",
                "actionRequestSequence !== backupManagementActionRequestSequence",
                "上传数据库备份",
            ),
            (
                export_body,
                "console.error('导出备份失败:', error);",
                "showToast(`导出备份失败: ${error.message || '请稍后重试'}`, 'danger');",
                "actionRequestSequence !== backupManagementActionRequestSequence",
                "导出备份",
            ),
            (
                import_body,
                "console.error('导入备份失败:', error);",
                "showToast(`导入备份失败: ${error.message || '请稍后重试'}`, 'danger');",
                "actionRequestSequence !== backupManagementActionRequestSequence",
                "导入备份",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn(toast_fragment, body)
                self.assertLess(
                    body.index(console_fragment),
                    body.index(toast_fragment),
                    f"{label} catch 里别再甩固定红字了，运行时异常也得把真实错误带出来",
                )
                self.assertLess(
                    body.rfind(stale_fragment, 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    f"{label}旧异常响应也得先验 action sequence，别新动作都开了老 toast 还回来抢戏",
                )
                self.assertLess(
                    body.rfind("!isSystemSettingsSectionActive()", 0, body.index(toast_fragment)),
                    body.index(toast_fragment),
                    f"都切出系统设置页了，{label}的旧异常 toast 也别跨页回魂",
                )

    def test_backup_download_actions_ignore_older_same_page_responses(self):
        show_section_body = _extract_function_body(self.app_js, "showSection")
        download_body = _extract_function_body(self.app_js, "downloadDatabaseBackup")
        export_body = _extract_function_body(self.app_js, "exportBackup")

        self.assertIn("let backupManagementActionRequestSequence = 0;", self.app_js)
        self.assertIn("backupManagementActionRequestSequence += 1;", show_section_body)

        for body, payload_fragment, success_fragment in (
            (
                download_body,
                "const blob = await response.blob();",
                "showToast('数据库备份下载成功', 'success');",
            ),
            (
                export_body,
                "const backupData = await response.json();",
                "showToast('备份导出成功', 'success');",
            ),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("const actionRequestSequence = ++backupManagementActionRequestSequence;", body)
                self.assertIn("actionRequestSequence !== backupManagementActionRequestSequence", body)
                self.assertLess(
                    body.index("actionRequestSequence !== backupManagementActionRequestSequence"),
                    body.index(payload_fragment),
                    "同页已经点了新的备份下载/导出动作，旧响应就别继续展开下载内容了",
                )
                self.assertLess(
                    body.rfind("actionRequestSequence !== backupManagementActionRequestSequence", 0, body.index(success_fragment)),
                    body.index(success_fragment),
                    "同页已经点了新的备份下载/导出动作，旧成功响应别再回来刷存在感",
                )

    def test_backup_download_actions_ignore_older_same_page_failures_after_reading_error_text(self):
        download_body = _extract_function_body(self.app_js, "downloadDatabaseBackup")
        export_body = _extract_function_body(self.app_js, "exportBackup")

        for body, error_fragment in (
            (download_body, "showToast(`下载失败: ${error}`, 'danger');"),
            (export_body, "showToast(`导出失败: ${error}`, 'danger');"),
        ):
            with self.subTest(error_fragment=error_fragment):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.rfind("actionRequestSequence !== backupManagementActionRequestSequence", 0, body.index(error_fragment)),
                    body.index(error_fragment),
                    "同页都已经发起新的备份动作了，旧失败响应读完错误文本后也别再回魂甩红字",
                )

    def test_data_management_mutations_only_report_success_when_followup_reload_succeeds(self):
        clear_body = _extract_function_body(self.app_js, "clearTableData")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        self.assertIn("const loaded = await loadTableData();", clear_body)
        self.assertIn("if (loaded) {", clear_body)
        self.assertIn("showToast(data.message || '数据清空成功', 'success');", clear_body)
        self.assertIn("showToast('数据清空成功，但表格刷新失败，请稍后手动刷新', 'warning');", clear_body)

        self.assertIn("const loaded = await loadTableData();", delete_body)
        self.assertIn("if (loaded) {", delete_body)
        self.assertIn("showToast(data.message || '删除成功', 'success');", delete_body)
        self.assertIn("showToast('删除成功，但表格刷新失败，请稍后手动刷新', 'warning');", delete_body)

    def test_data_management_mutations_do_not_emit_cross_page_toasts_after_leaving_section(self):
        clear_body = _extract_function_body(self.app_js, "clearTableData")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        for body, success_fragment in (
            (clear_body, "showToast(data.message || '数据清空成功', 'success');"),
            (delete_body, "showToast(data.message || '删除成功', 'success');"),
        ):
            with self.subTest(success_fragment=success_fragment):
                self.assertIn("!document.getElementById('data-management-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('data-management-section')?.classList.contains('active')"),
                    body.index(success_fragment),
                    "都切出数据管理页了，旧 mutation 响应不该再跨页弹 success toast",
                )

    def test_data_management_mutations_ignore_older_same_page_responses(self):
        self.assertIn("let dataManagementMutationActionRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        clear_body = _extract_function_body(self.app_js, "clearTableData")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        self.assertIn("dataManagementMutationActionRequestSequence += 1;", show_section_body)

        for body, anchor_fragment in (
            (clear_body, "const loaded = await loadTableData();"),
            (delete_body, "const loaded = await loadTableData();"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("++dataManagementMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== dataManagementMutationActionRequestSequence", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== dataManagementMutationActionRequestSequence"),
                    body.index(anchor_fragment),
                    "同页连续执行数据管理 mutation 时，旧响应不该晚回来后又触发表格刷新",
                )

    def test_data_management_clear_action_sequence_starts_only_after_confirmation(self):
        body = _extract_function_body(self.app_js, "clearTableData")
        confirm_return_index = body.index("return;", body.index("if (!confirmed)"))
        self.assertLess(
            confirm_return_index,
            body.index("++dataManagementMutationActionRequestSequence"),
            "用户都取消清空数据了，就别先把数据管理 mutation action sequence 顶掉别的正常动作",
        )

    def test_data_management_delete_modal_delete_flow_respects_modal_session_before_hiding_or_toasting(self):
        self.assertIn("let dataDeleteModalRequestSequence = 0;", self.app_js)

        init_modal_body = _extract_function_body(self.app_js, "initDeleteRecordModal")
        delete_body = _extract_function_body(self.app_js, "deleteRecord")
        confirm_body = _extract_function_body(self.app_js, "confirmDeleteRecord")

        self.assertIn("const requestSequence = ++dataDeleteModalRequestSequence;", delete_body)
        self.assertIn("if (deleteRecordModalElement.dataset.dataDeleteModalIgnoreNextHidden === 'true') {", init_modal_body)
        self.assertIn("deleteRecordModalElement.dataset.dataDeleteModalIgnoreNextHidden = 'false';", init_modal_body)
        self.assertIn("dataDeleteModalRequestSequence += 1;", init_modal_body)

        self.assertIn("const requestSequence = dataDeleteModalRequestSequence;", confirm_body)
        self.assertIn("requestSequence !== dataDeleteModalRequestSequence", confirm_body)
        self.assertIn("deleteRecordModalElement.dataset.dataDeleteModalIgnoreNextHidden = 'true';", confirm_body)
        self.assertIn("currentDeleteId = null;", confirm_body)
        self.assertIn("return null;", confirm_body)
        self.assertLess(
            confirm_body.index("requestSequence !== dataDeleteModalRequestSequence"),
            confirm_body.index("deleteRecordModal.hide();"),
            "旧的删记录响应不该回来把已经重开的删除确认框又关掉",
        )

    def test_switching_away_from_data_management_closes_delete_record_modal(self):
        body = _extract_function_body(self.app_js, "showSection")

        self.assertIn("if (sectionName !== 'data-management') {", body)
        self.assertIn("const deleteRecordModalElement = document.getElementById('deleteRecordModal');", body)
        self.assertIn("const activeDeleteRecordModal = deleteRecordModalElement", body)
        self.assertIn("bootstrap.Modal.getInstance(deleteRecordModalElement)", body)
        self.assertIn("activeDeleteRecordModal.hide();", body)
        self.assertLess(
            body.index("dataDeleteModalRequestSequence += 1;"),
            body.index("activeDeleteRecordModal.hide();"),
            "都切出数据管理页了，删记录确认框还杵在别的菜单上，这不是给自己找骂么",
        )

    def test_user_management_escapes_username_in_inline_actions(self):
        self.assertIn("function escapeInlineJsSingleQuotedString(value) {", self.app_js)
        create_user_card_body = _extract_function_body(self.app_js, "createUserCard")
        self.assertIn("const safeUsername = escapeInlineJsSingleQuotedString(user.username);", create_user_card_body)
        self.assertIn("toggleUserAdmin('${user.id}', '${safeUsername}',", create_user_card_body)
        self.assertIn("deleteUser('${user.id}', '${safeUsername}')", create_user_card_body)

    def test_user_management_self_card_detection_normalizes_string_and_numeric_user_ids(self):
        create_user_card_body = _extract_function_body(self.app_js, "createUserCard")

        self.assertIn("currentUserId = String(userInfo.user_id ?? '').trim();", create_user_card_body)
        self.assertIn("const normalizedUserId = String(user.id ?? '').trim();", create_user_card_body)
        self.assertIn("const isSelf = normalizedUserId !== '' && normalizedUserId === currentUserId;", create_user_card_body)
        self.assertNotIn("const isSelf = user.id === currentUserId;", create_user_card_body)

    def test_user_management_resets_stale_stats_and_lists_before_permission_check(self):
        self.assertIn("function resetUserManagementView() {", self.app_js)
        reset_body = _extract_function_body(self.app_js, "resetUserManagementView")
        load_body = _extract_function_body(self.app_js, "loadUserManagement")

        self.assertIn("document.getElementById('totalUsers').textContent = '-';", reset_body)
        self.assertIn("document.getElementById('totalUserCookies').textContent = '-';", reset_body)
        self.assertIn("document.getElementById('totalUserCards').textContent = '-';", reset_body)
        self.assertIn("usersListDiv.innerHTML = '';", reset_body)
        self.assertIn("usersListDiv.style.display = 'none';", reset_body)
        self.assertIn("noUsersDiv.style.display = 'none';", reset_body)
        self.assertIn("loadingDiv.style.display = 'none';", reset_body)

        self.assertIn("resetUserManagementView();", load_body)
        self.assertLess(
            load_body.index("resetUserManagementView();"),
            load_body.index("const response = await fetch(`${apiBase}/verify`, {"),
            "用户管理在做权限校验前应先清掉旧统计和旧列表，免得失败后挂着陈年数据装活人",
        )

    def test_user_management_distinguishes_load_failures_from_empty_user_state(self):
        self.assertIn("function renderUserManagementEmptyState(message = '暂无用户') {", self.app_js)
        helper_body = _extract_function_body(self.app_js, "renderUserManagementEmptyState")
        load_users_body = _extract_function_body(self.app_js, "loadUsers")

        self.assertIn("const noUsersDiv = document.getElementById('noUsers');", helper_body)
        self.assertIn("const usersListDiv = document.getElementById('usersList');", helper_body)
        self.assertIn("const messageElement = noUsersDiv.querySelector('p');", helper_body)
        self.assertIn("messageElement.textContent = message;", helper_body)
        self.assertIn("usersListDiv.style.display = 'none';", helper_body)

        self.assertIn("renderUserManagementEmptyState();", load_users_body)
        self.assertIn("renderUserManagementEmptyState(error.message || '加载用户列表失败，请稍后重试');", load_users_body)
        self.assertIn("showToast(`加载用户列表失败: ${error.message || '请稍后重试'}`, 'danger');", load_users_body)
        self.assertNotIn("noUsersDiv.style.display = 'block';", load_users_body)

    def test_user_management_loader_http_failures_parse_detail_payloads_before_throwing_and_toasting(self):
        load_body = _extract_function_body(self.app_js, "loadUserManagement")
        stats_body = _extract_function_body(self.app_js, "loadUserSystemStats")
        users_body = _extract_function_body(self.app_js, "loadUsers")

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", load_body)
        self.assertIn("throw new Error(errorMessage);", load_body)
        self.assertIn("showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');", load_body)
        load_error_index = load_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        load_throw_index = load_body.index("throw new Error(errorMessage);", load_error_index)
        load_toast_index = load_body.index("showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            load_error_index,
            load_throw_index,
            "用户管理权限校验 HTTP 失败时得先把 detail/message 解出来，别固定甩一句权限验证失败装没事",
        )
        self.assertLess(
            load_body.find("requestSequence !== userManagementLoadRequestSequence", load_error_index),
            load_throw_index,
            "用户管理权限校验旧失败响应读完错误体后，先验 root loader 会话还活着，再决定要不要抛错",
        )
        self.assertLess(
            load_throw_index,
            load_toast_index,
            "用户管理权限校验应把真实后端错误带进 catch toast，别又吞成统一红字",
        )

        self.assertIn("const errorMessage = await readResponseErrorMessage(statsResponse, `HTTP ${statsResponse.status}`);", stats_body)
        self.assertIn("throw new Error(errorMessage);", stats_body)
        stats_error_index = stats_body.index("const errorMessage = await readResponseErrorMessage(statsResponse, `HTTP ${statsResponse.status}`);")
        stats_throw_index = stats_body.index("throw new Error(errorMessage);", stats_error_index)
        self.assertLess(
            stats_error_index,
            stats_throw_index,
            "用户统计接口 HTTP 失败时也得先把 detail/message 解出来，别只剩个状态码在日志里晃悠",
        )
        self.assertLess(
            stats_body.find("requestSequence !== userManagementStatsRequestSequence", stats_error_index),
            stats_throw_index,
            "用户统计旧失败响应读完错误体后，先验统计请求还活着，再决定要不要抛错",
        )

        self.assertIn("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);", users_body)
        self.assertIn("throw new Error(errorMessage);", users_body)
        self.assertIn("showToast(`加载用户列表失败: ${error.message || '请稍后重试'}`, 'danger');", users_body)
        users_error_index = users_body.index("const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        users_throw_index = users_body.index("throw new Error(errorMessage);", users_error_index)
        users_toast_index = users_body.index("showToast(`加载用户列表失败: ${error.message || '请稍后重试'}`, 'danger');")
        self.assertLess(
            users_error_index,
            users_throw_index,
            "用户列表 HTTP 失败时得先把 detail/message 解出来，别固定报获取用户列表失败糊弄人",
        )
        self.assertLess(
            users_body.find("requestSequence !== userManagementListRequestSequence", users_error_index),
            users_throw_index,
            "用户列表旧失败响应读完错误体后，先验列表请求还活着，再决定要不要抛错",
        )
        self.assertLess(
            users_throw_index,
            users_toast_index,
            "用户列表应把真实后端错误带进 catch toast，别把错误体吞干净只剩统一红字",
        )

    def test_refresh_users_only_reports_success_when_stats_and_list_reload_succeed(self):
        stats_body = _extract_function_body(self.app_js, "loadUserSystemStats")
        load_users_body = _extract_function_body(self.app_js, "loadUsers")
        refresh_body = _extract_function_body(self.app_js, "refreshUsers")

        self.assertIn("return true;", stats_body)
        self.assertIn("return false;", stats_body)
        self.assertIn("return null;", stats_body)
        self.assertIn("return true;", load_users_body)
        self.assertIn("return false;", load_users_body)
        self.assertIn("return null;", load_users_body)

        self.assertIn("const [statsLoaded, usersLoaded] = await Promise.all([", refresh_body)
        self.assertIn("loadUserSystemStats()", refresh_body)
        self.assertIn("loadUsers()", refresh_body)
        self.assertIn("if (statsLoaded === true && usersLoaded === true) {", refresh_body)
        self.assertIn("showToast('用户列表已刷新', 'success');", refresh_body)
        self.assertIn("} else if (usersLoaded === true && statsLoaded === false) {", refresh_body)
        self.assertIn("showToast('用户统计信息刷新失败，请稍后重试', 'warning');", refresh_body)
        self.assertIn("} else if (usersLoaded === false) {", refresh_body)
        self.assertIn("showToast('用户列表刷新失败，请稍后重试', 'danger');", refresh_body)

    def test_refresh_users_does_not_emit_cross_page_toasts_after_leaving_user_management(self):
        body = _extract_function_body(self.app_js, "refreshUsers")

        for toast_fragment in (
            "showToast('用户列表已刷新', 'success');",
            "showToast('用户统计信息刷新失败，请稍后重试', 'warning');",
        ):
            with self.subTest(toast_fragment=toast_fragment):
                self.assertIn("!document.getElementById('user-management-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("!document.getElementById('user-management-section')?.classList.contains('active')"),
                    body.index(toast_fragment),
                    "都切出用户管理页了，旧刷新结果就别跨页弹 toast 刷存在感",
                )

    def test_refresh_users_ignores_older_same_page_runs(self):
        self.assertIn("let userManagementRefreshActionRequestSequence = 0;", self.app_js)

        reset_body = _extract_function_body(self.app_js, "resetUserManagementView")
        show_section_body = _extract_function_body(self.app_js, "showSection")
        refresh_body = _extract_function_body(self.app_js, "refreshUsers")

        self.assertIn("userManagementRefreshActionRequestSequence += 1;", reset_body)
        self.assertIn("userManagementRefreshActionRequestSequence += 1;", show_section_body)
        self.assertIn("const actionRequestSequence = ++userManagementRefreshActionRequestSequence;", refresh_body)
        self.assertIn("actionRequestSequence !== userManagementRefreshActionRequestSequence", refresh_body)
        self.assertIn("return null;", refresh_body)

        stats_index = refresh_body.index("loadUserSystemStats()")
        users_index = refresh_body.index("loadUsers()")
        success_toast_index = refresh_body.index("showToast('用户列表已刷新', 'success');")

        self.assertLess(
            refresh_body.rfind("actionRequestSequence !== userManagementRefreshActionRequestSequence", 0, success_toast_index),
            success_toast_index,
            "同页已经开始新的用户列表刷新后，旧刷新结果别回来刷 success toast 装自己没过期",
        )
        self.assertLess(
            stats_index,
            users_index,
            "用户统计和列表刷新都在并行 Promise.all 里启动，至少别把 loadUsers 排到统计调用前面搞得阅读像迷魂阵",
        )

    def test_user_management_mutations_do_not_report_success_before_followup_reload_finishes(self):
        toggle_body = _extract_function_body(self.app_js, "toggleUserAdmin")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteUser")

        self.assertIn("const usersLoaded = await loadUsers();", toggle_body)
        self.assertIn("if (usersLoaded === true) {", toggle_body)
        self.assertIn("} else if (usersLoaded === false) {", toggle_body)
        self.assertIn("showToast(data.message || `用户已${action}`, 'success');", toggle_body)
        self.assertIn("showToast('用户列表刷新失败，请稍后重试', 'warning');", toggle_body)
        self.assertLess(
            toggle_body.index("const usersLoaded = await loadUsers();"),
            toggle_body.index("showToast(data.message || `用户已${action}`, 'success');"),
            "切换管理员状态不该先吹成功，再慢吞吞去刷新列表",
        )

        self.assertIn("const [statsLoaded, usersLoaded] = await Promise.all([", delete_body)
        self.assertIn("loadUserSystemStats()", delete_body)
        self.assertIn("loadUsers()", delete_body)
        self.assertIn("if (statsLoaded === true && usersLoaded === true) {", delete_body)
        self.assertIn("} else if (statsLoaded === false || usersLoaded === false) {", delete_body)
        self.assertIn("showToast(data.message || '用户删除成功', 'success');", delete_body)
        self.assertIn("showToast('用户删除成功，但统计或列表刷新失败，请稍后重试', 'warning');", delete_body)
        self.assertLess(
            delete_body.index("const [statsLoaded, usersLoaded] = await Promise.all(["),
            delete_body.index("showToast(data.message || '用户删除成功', 'success');"),
            "删除用户不该先报喜，再让后续刷新失败把脸打肿",
        )

    def test_load_user_management_surfaces_partial_stats_failure_when_user_list_still_loads(self):
        body = _extract_function_body(self.app_js, "loadUserManagement")
        self.assertIn("const [statsLoaded, usersLoaded] = await Promise.all([", body)
        self.assertIn("if (statsLoaded === false && usersLoaded === true) {", body)
        self.assertIn("showToast('用户统计信息加载失败，请稍后重试', 'warning');", body)

    def test_user_management_stats_and_list_ignore_stale_async_responses(self):
        self.assertIn("let userManagementStatsRequestSequence = 0;", self.app_js)
        self.assertIn("let userManagementListRequestSequence = 0;", self.app_js)

        reset_body = _extract_function_body(self.app_js, "resetUserManagementView")
        show_section_body = _extract_function_body(self.app_js, "showSection")
        stats_body = _extract_function_body(self.app_js, "loadUserSystemStats")
        users_body = _extract_function_body(self.app_js, "loadUsers")

        self.assertIn("userManagementStatsRequestSequence += 1;", reset_body)
        self.assertIn("userManagementListRequestSequence += 1;", reset_body)
        self.assertIn("if (sectionName !== 'user-management') {", show_section_body)
        self.assertIn("userManagementStatsRequestSequence += 1;", show_section_body)
        self.assertIn("userManagementListRequestSequence += 1;", show_section_body)

        self.assertIn("const requestSequence = ++userManagementStatsRequestSequence;", stats_body)
        self.assertIn("requestSequence !== userManagementStatsRequestSequence", stats_body)
        self.assertIn("!document.getElementById('user-management-section')?.classList.contains('active')", stats_body)
        self.assertIn("return null;", stats_body)
        self.assertLess(
            stats_body.index("requestSequence !== userManagementStatsRequestSequence"),
            stats_body.index("document.getElementById('totalUsers').textContent = statsData.users.total;"),
            "旧的用户统计请求不该晚回来把新的统计数字再糊回去",
        )

        self.assertIn("const requestSequence = ++userManagementListRequestSequence;", users_body)
        self.assertIn("requestSequence !== userManagementListRequestSequence", users_body)
        self.assertIn("!document.getElementById('user-management-section')?.classList.contains('active')", users_body)
        self.assertIn("return null;", users_body)
        self.assertLess(
            users_body.index("requestSequence !== userManagementListRequestSequence"),
            users_body.index("loadingDiv.style.display = 'none';"),
            "旧的用户列表请求不该回来后再改 loading/空态/列表显示",
        )

    def test_user_management_loaders_reject_malformed_payloads_before_touching_ui(self):
        stats_body = _extract_function_body(self.app_js, "loadUserSystemStats")
        users_body = _extract_function_body(self.app_js, "loadUsers")

        self.assertIn("if (!statsData || typeof statsData !== 'object' || !statsData.users || !statsData.cookies || !statsData.cards) {", stats_body)
        self.assertIn("throw new Error('用户统计返回格式异常');", stats_body)
        self.assertLess(
            stats_body.index("throw new Error('用户统计返回格式异常');"),
            stats_body.index("document.getElementById('totalUsers').textContent = statsData.users.total;"),
            "用户统计返回结构不对时，先抛清楚错误，别拿半截脏数据硬往卡片里灌",
        )

        self.assertIn("if (!data || !Array.isArray(data.users)) {", users_body)
        self.assertIn("throw new Error('用户列表返回格式异常');", users_body)
        self.assertLess(
            users_body.index("throw new Error('用户列表返回格式异常');"),
            users_body.index("loadingDiv.style.display = 'none';"),
            "用户列表返回结构不对时，别先把 loading 收了再拿脏数据渲染，容易让人误以为真没用户",
        )

    def test_user_management_mutations_ignore_older_same_page_responses_and_hidden_state(self):
        self.assertIn("let userManagementMutationActionRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        toggle_body = _extract_function_body(self.app_js, "toggleUserAdmin")
        confirm_delete_body = _extract_function_body(self.app_js, "confirmDeleteUser")

        self.assertIn("userManagementMutationActionRequestSequence += 1;", show_section_body)

        for body, anchor_fragment in (
            (toggle_body, "const usersLoaded = await loadUsers();"),
            (confirm_delete_body, "const [statsLoaded, usersLoaded] = await Promise.all(["),
        ):
            with self.subTest(anchor_fragment=anchor_fragment):
                self.assertIn("++userManagementMutationActionRequestSequence", body)
                self.assertIn("actionRequestSequence !== userManagementMutationActionRequestSequence", body)
                self.assertIn("!document.getElementById('user-management-section')?.classList.contains('active')", body)
                self.assertIn("return null;", body)
                self.assertLess(
                    body.index("actionRequestSequence !== userManagementMutationActionRequestSequence"),
                    body.index(anchor_fragment),
                    "同页连续执行用户管理操作时，旧响应不该晚回来后再刷新列表或统计",
                )

    def test_user_management_mutation_action_sequence_starts_only_after_confirmation(self):
        body = _extract_function_body(self.app_js, "toggleUserAdmin")
        confirm_return_index = body.index("return;", body.index("if (!confirm("))
        self.assertLess(
            confirm_return_index,
            body.index("const actionRequestSequence = ++userManagementMutationActionRequestSequence;"),
            "用户都取消切换管理员权限了，就别先把用户管理 mutation action sequence 顶掉别的正常动作",
        )

    def test_delete_user_modal_delete_flow_respects_modal_session_before_hiding_or_toasting(self):
        self.assertIn("let userDeleteModalRequestSequence = 0;", self.app_js)

        show_section_body = _extract_function_body(self.app_js, "showSection")
        delete_user_body = _extract_function_body(self.app_js, "deleteUser")
        confirm_delete_body = _extract_function_body(self.app_js, "confirmDeleteUser")

        self.assertIn("const deleteUserModalElement = document.getElementById('deleteUserModal');", show_section_body)
        self.assertIn("bootstrap.Modal.getInstance(deleteUserModalElement)", show_section_body)
        self.assertIn("activeDeleteUserModal.hide();", show_section_body)
        self.assertIn("const requestSequence = ++userDeleteModalRequestSequence;", delete_user_body)
        self.assertIn("if (deleteUserModalElement.dataset.userDeleteModalIgnoreNextHidden === 'true') {", delete_user_body)
        self.assertIn("deleteUserModalElement.dataset.userDeleteModalIgnoreNextHidden = 'false';", delete_user_body)
        self.assertIn("userDeleteModalRequestSequence += 1;", delete_user_body)

        self.assertIn("const requestSequence = userDeleteModalRequestSequence;", confirm_delete_body)
        self.assertIn("requestSequence !== userDeleteModalRequestSequence", confirm_delete_body)
        self.assertIn("deleteUserModalElement.dataset.userDeleteModalIgnoreNextHidden = 'true';", confirm_delete_body)
        self.assertIn("return null;", confirm_delete_body)
        self.assertLess(
            confirm_delete_body.index("requestSequence !== userDeleteModalRequestSequence"),
            confirm_delete_body.index("deleteUserModal.hide();"),
            "旧的删除用户响应不该回来把已经重开的删除确认弹窗又关掉",
        )

    def test_user_management_callers_distinguish_stale_reloads_from_real_failures(self):
        load_body = _extract_function_body(self.app_js, "loadUserManagement")
        refresh_body = _extract_function_body(self.app_js, "refreshUsers")
        toggle_body = _extract_function_body(self.app_js, "toggleUserAdmin")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteUser")

        self.assertIn("if (statsLoaded === false && usersLoaded === true) {", load_body)
        self.assertNotIn("if (!statsLoaded && usersLoaded) {", load_body)

        self.assertIn("if (statsLoaded === true && usersLoaded === true) {", refresh_body)
        self.assertIn("} else if (usersLoaded === true && statsLoaded === false) {", refresh_body)

        self.assertIn("if (usersLoaded === true) {", toggle_body)
        self.assertIn("} else if (usersLoaded === false) {", toggle_body)

        self.assertIn("if (statsLoaded === true && usersLoaded === true) {", delete_body)
        self.assertIn("} else if (statsLoaded === false || usersLoaded === false) {", delete_body)

    def test_user_management_root_loader_ignores_stale_permission_checks_and_section_changes(self):
        self.assertIn("let userManagementLoadRequestSequence = 0;", self.app_js)
        show_section_body = _extract_function_body(self.app_js, "showSection")
        load_body = _extract_function_body(self.app_js, "loadUserManagement")

        self.assertIn("userManagementLoadRequestSequence += 1;", show_section_body)
        self.assertIn("const requestSequence = ++userManagementLoadRequestSequence;", load_body)
        self.assertIn("requestSequence !== userManagementLoadRequestSequence", load_body)
        self.assertIn("return null;", load_body)
        self.assertIn("!document.getElementById('user-management-section')?.classList.contains('active')", load_body)
        self.assertLess(
            load_body.index("requestSequence !== userManagementLoadRequestSequence"),
            load_body.index("if (!result.is_admin) {"),
            "用户管理权限校验晚到时，不该在用户已经切页后还跳出来装大爷说你没权限",
        )
        stats_index = load_body.index("const [statsLoaded, usersLoaded] = await Promise.all([")
        self.assertLess(
            load_body.rfind("requestSequence !== userManagementLoadRequestSequence", 0, stats_index),
            stats_index,
            "用户管理总加载在拉统计和列表前也得再验一次请求序号，别旧请求回来又开工",
        )

    def test_user_management_root_loader_does_not_emit_cross_page_permission_failure_toasts(self):
        body = _extract_function_body(self.app_js, "loadUserManagement")
        toast_fragment = "showToast(`权限验证失败: ${error.message || '请稍后重试'}`, 'danger');"

        self.assertIn("!document.getElementById('user-management-section')?.classList.contains('active')", body)
        self.assertIn("return null;", body)
        catch_index = body.index("} catch (error) {")
        catch_toast_index = body.rfind(toast_fragment)
        self.assertLess(
            body.find("!document.getElementById('user-management-section')?.classList.contains('active')", catch_index),
            catch_toast_index,
            "都切出用户管理页了，旧的权限校验失败就别再跨页甩 danger toast 了",
        )

    def test_user_management_raw_fetch_actions_handle_unauthorized_before_followup_work(self):
        load_body = _extract_function_body(self.app_js, "loadUserManagement")
        stats_body = _extract_function_body(self.app_js, "loadUserSystemStats")
        users_body = _extract_function_body(self.app_js, "loadUsers")
        toggle_body = _extract_function_body(self.app_js, "toggleUserAdmin")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteUser")

        for body, unauthorized_fragment, anchor_fragment in (
            (load_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (stats_body, "if (handleUnauthorizedApiResponse(statsResponse)) {", "if (!statsResponse.ok) {"),
            (users_body, "if (handleUnauthorizedApiResponse(response)) {", "if (!response.ok) {"),
            (toggle_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
            (delete_body, "if (handleUnauthorizedApiResponse(response)) {", "if (response.ok) {"),
        ):
            with self.subTest(anchor_fragment=anchor_fragment, unauthorized_fragment=unauthorized_fragment):
                self.assertIn(unauthorized_fragment, body)
                self.assertLess(
                    body.index(unauthorized_fragment),
                    body.index(anchor_fragment),
                    "用户管理这些 raw fetch 遇到 401 得先滚去登录，别后面还继续做权限判断、拉列表、改权限、删用户",
                )

    def test_user_management_failure_actions_read_structured_error_messages(self):
        toggle_body = _extract_function_body(self.app_js, "toggleUserAdmin")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteUser")

        for body, toast_fragment, label in (
            (toggle_body, "showToast(`操作失败: ${error}`, 'danger');", "切换用户管理员状态"),
            (delete_body, "showToast(`删除失败: ${error}`, 'danger');", "删除用户"),
        ):
            with self.subTest(label=label):
                self.assertIn("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);", body)
                self.assertLess(
                    body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);"),
                    body.index(toast_fragment),
                    f"{label}失败时得先把错误体解明白，别硬啃 JSON 然后拿 detail 碰运气",
                )

    def test_user_management_failure_toasts_recheck_stale_state_after_error_body_read(self):
        toggle_body = _extract_function_body(self.app_js, "toggleUserAdmin")
        delete_body = _extract_function_body(self.app_js, "confirmDeleteUser")

        toggle_error_index = toggle_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        toggle_toast_index = toggle_body.index("showToast(`操作失败: ${error}`, 'danger');")
        self.assertLess(
            toggle_body.find("actionRequestSequence !== userManagementMutationActionRequestSequence", toggle_error_index),
            toggle_toast_index,
            "同页已经点了新的用户权限操作后，旧失败响应读完错误体也别回来回魂甩红字",
        )
        self.assertLess(
            toggle_body.find("!document.getElementById('user-management-section')?.classList.contains('active')", toggle_error_index),
            toggle_toast_index,
            "都切出用户管理页了，旧权限修改失败响应读完错误体也别跨页弹 danger toast",
        )

        delete_error_index = delete_body.index("const error = await readResponseErrorMessage(response, `HTTP ${response.status}`);")
        delete_toast_index = delete_body.index("showToast(`删除失败: ${error}`, 'danger');")
        self.assertLess(
            delete_body.find("actionRequestSequence !== userManagementMutationActionRequestSequence", delete_error_index),
            delete_toast_index,
            "同页已经点了新的删除用户动作后，旧失败响应读完错误体也别回来诈尸甩红字",
        )
        self.assertLess(
            delete_body.find("requestSequence !== userDeleteModalRequestSequence", delete_error_index),
            delete_toast_index,
            "删除用户的旧失败响应读完错误体后，也别把新一轮弹窗会话的结果给顶了",
        )
        self.assertLess(
            delete_body.find("!document.getElementById('user-management-section')?.classList.contains('active')", delete_error_index),
            delete_toast_index,
            "都切出用户管理页了，旧删除失败响应读完错误体也别跨页弹 danger toast",
        )

    def test_user_management_mutation_catch_failures_surface_runtime_error_messages(self):
        for body, legacy_toast, runtime_toast, guard_fragments, label in (
            (
                _extract_function_body(self.app_js, "toggleUserAdmin"),
                "showToast('更新用户权限失败', 'danger');",
                "showToast(`更新用户权限失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== userManagementMutationActionRequestSequence",
                    "!document.getElementById('user-management-section')?.classList.contains('active')",
                ),
                "更新用户权限",
            ),
            (
                _extract_function_body(self.app_js, "confirmDeleteUser"),
                "showToast('删除用户失败', 'danger');",
                "showToast(`删除用户失败: ${error.message || '请稍后重试'}`, 'danger');",
                (
                    "actionRequestSequence !== userManagementMutationActionRequestSequence",
                    "requestSequence !== userDeleteModalRequestSequence",
                    "!document.getElementById('user-management-section')?.classList.contains('active')",
                ),
                "删除用户",
            ),
        ):
            with self.subTest(label=label):
                self.assertNotIn(legacy_toast, body)
                self.assertIn(runtime_toast, body)
                toast_index = body.index(runtime_toast)
                for guard_fragment in guard_fragments:
                    self.assertIn(guard_fragment, body)
                    self.assertLess(
                        body.rfind(guard_fragment, 0, toast_index),
                        toast_index,
                        f"{label} catch 里的旧异常在弹 toast 前也得先过会话/页面活性校验，别 stale 了还回来抽风",
                    )

    def test_data_management_delete_flow_does_not_leave_debug_console_logs(self):
        for function_name in ("deleteRecordByIndex", "deleteRecord", "confirmDeleteRecord"):
            body = _extract_function_body(self.app_js, function_name)
            self.assertNotIn("console.log(", body)

    def test_delete_user_modal_shows_target_username(self):
        self.assertIn('id="deleteUserNameText"', self.index_html)
        self.assertIn("function updateDeleteUserModalContent(username = '') {", self.app_js)
        self.assertIn("function resetDeleteUserModalState() {", self.app_js)
        delete_user_body = _extract_function_body(self.app_js, "deleteUser")
        confirm_delete_user_body = _extract_function_body(self.app_js, "confirmDeleteUser")
        reset_delete_user_modal_body = _extract_function_body(self.app_js, "resetDeleteUserModalState")
        self.assertIn("currentDeleteUserId = null;", reset_delete_user_modal_body)
        self.assertIn("currentDeleteUserName = null;", reset_delete_user_modal_body)
        self.assertIn("updateDeleteUserModalContent('');", reset_delete_user_modal_body)
        self.assertIn("updateDeleteUserModalContent(username);", delete_user_body)
        self.assertIn("document.getElementById('deleteUserModal').addEventListener('hidden.bs.modal', () => {", delete_user_body)
        self.assertIn("resetDeleteUserModalState();", delete_user_body)
        self.assertIn("deleteUserModal.hide();", confirm_delete_user_body)
        self.assertNotIn("resetDeleteUserModalState();", confirm_delete_user_body)
        self.assertNotIn("finally {", confirm_delete_user_body)

    def test_check_auth_persists_user_info_and_logout_clears_it(self):
        check_auth_body = _extract_function_body(self.app_js, "checkAuth")
        logout_body = _extract_function_body(self.app_js, "logout")

        self.assertIn("localStorage.setItem('user_info', JSON.stringify({", check_auth_body)
        self.assertIn("localStorage.removeItem('user_info');", check_auth_body)
        self.assertIn("localStorage.removeItem('user_info');", logout_body)

    def test_card_save_and_update_do_not_log_debug_payloads(self):
        save_body = _extract_function_body(self.app_js, "saveCard")
        update_body = _extract_function_body(self.app_js, "updateCard")

        for body in (save_body, update_body):
            self.assertNotIn("[DEBUG]", body)
            self.assertNotIn("console.log(", body)

    def test_card_backend_routes_do_not_leave_debug_spec_logs(self):
        self.assertNotIn("[DEBUG] 创建卡券", self.reply_server)
        self.assertNotIn("[DEBUG] 更新卡券", self.reply_server)

    def test_default_reply_manager_does_not_expose_placeholder_test_action(self):
        self.assertNotIn("onclick=\"testDefaultReply('${accountId}')\"", self.app_js)
        self.assertNotIn("function testDefaultReply(", self.app_js)

    def test_login_page_public_setting_fetches_check_http_status_before_parsing(self):
        self.assertIn(
            "async function readResponseErrorMessage(response, fallbackMessage = '') {",
            self.login_html,
        )

        for function_name, json_anchor in (
            ("checkLoginCaptchaEnabled", "const result = await response.json();"),
            ("checkRegistrationStatus", "const data = await response.json();"),
            ("checkLoginInfoStatus", "const result = await response.json();"),
        ):
            with self.subTest(function_name=function_name):
                body = _extract_function_body(self.login_html, function_name)
                self.assertIn("if (!response.ok) {", body)
                self.assertIn(
                    "const errorMessage = await readResponseErrorMessage(response, `HTTP ${response.status}`);",
                    body,
                )
                self.assertIn("throw new Error(errorMessage);", body)
                self.assertLess(
                    body.index("if (!response.ok) {"),
                    body.index(json_anchor),
                    f"{function_name} 先判断 HTTP 状态，再解析 JSON，别把 500 当成功配置吃下去",
                )

    def test_admin_pages_define_favicon_to_avoid_404_requests(self):
        favicon_fragment = '<link rel="icon" href="data:,">'
        for html in (self.index_html, self.login_html, self.register_html):
            self.assertIn(favicon_fragment, html)


if __name__ == "__main__":
    unittest.main()
