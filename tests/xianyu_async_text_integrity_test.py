from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = REPO_ROOT / "XianyuAutoAsync.py"
BROWSER_RUNTIME_TEST_FILE = REPO_ROOT / "tests" / "test_xianyu_async_browser_runtime.py"


def _gbk_mojibake(text: str, replace_invalid: bool = False) -> str:
    errors = "replace" if replace_invalid else "strict"
    return text.encode("utf-8").decode("gbk", errors=errors).replace("\ufffd", "?")


def _missing_tail(text: str) -> str:
    return f"{text[:-1]}?" if len(text) > 1 else text


def test_xianyu_async_contains_expected_text_literals():
    source_text = SOURCE_FILE.read_text(encoding="utf-8")

    expected_phrases = [
        "当前WebSocket已关闭，主循环将使用最新状态重新连接",
        "开始使用浏览器获取商品详情",
        "开始自动发货检查",
        "检测到滑块验证（Token刷新）",
        "概不退款。",
        "账号输入等待超时，清除等待状态",
        "已保存充值账号",
        "再次等待确认",
        "初始化鉴权冷静期剩余",
        "浏览器运行时缓存清理完成",
        "获取商品信息失败，重试次数过多",
        "无法申请账号级浏览器 runtime",
        "回退为单条发送",
        "执行清理...",
        "XianyuLive主程序已完全退出",
        "程序退出",
        "确认发货API验证失败: Session过期",
        "确认发货API验证通过: API调用成功",
        "确认发货API验证警告: 响应不明确",
        "确认发货API验证警告: 无响应",
        "同步最新详情",
        "获取缺失详情",
        "通过CookieManager重启实例...",
        "开始通过浏览器刷新Cookie...",
        "延迟触发实例重启，避免与当前处理流程直接竞争。",
        "我已小刀，待刀成",
        "实例已注册到全局字典",
        "实例已从全局字典中注销",
        "注销实例失败",
        "第一次刷新超时，使用降级策略...",
        "键 '",
        "📱 QQ通知 - QQ号码配置为空，无法发送通知",
        "📱 飞书通知 - 是否有签名密钥:",
        "📱 飞书通知 - Webhook URL配置为空，无法发送通知",
        "📱 Bark通知 - 设备密钥配置为空，无法发送通知",
        '"人脸验证" in error_message',
        '"短信验证" in error_message',
        '"二维码验证" in error_message',
        '"身份验证" in error_message',
        '"session过期" in detail',
        '"页面会话已失效" in detail',
        "闲鱼币抵扣",
        "调用亦凡API: 商户ID=",
        "处理WebSocket消息异常",
        "所有后台任务状态:",
        "心跳(已启动),",
        'log_prefix = f"【{self.account_id}】[{msg_id}]" if msg_id else f"【{self.account_id}】"',
        'log_prefix=f"【{self.account_id}】",',
        'log_prefix = f"【{self.account_id}】"',
        'logger.warning(f"【{self.account_id}】 - {detail}")',
        'logger.info(f"【{log_account_id}】 {cookie_name}: {display_value}{change_mark}")',
    ]

    for phrase in expected_phrases:
        assert phrase in source_text


def test_xianyu_async_does_not_contain_known_garbled_text():
    source_text = SOURCE_FILE.read_text(encoding="utf-8")

    unexpected_phrases = [
        "当前WebSocket已关闭，主循环将使用朢新状态重新连?",
        "缺?canonical account_id",
        "拒绝继续运?",
        "浏览?runtime",
        "程序逢?",
        "执行清?..",
        "重试次数过?",
        "????API: Session??",
        "????API: ????",
        "????API: ?????",
        "????API: ???",
        "????API: Cookie?????????",
        "????API: ?????????? - ",
        "action_text = '??????' if sync_item_details else '??????'",
        "发送知",
        "无法发送知",
        "?? ???? - ???????: {'?' if secret else '?'}",
        'elif "????" in error_message or "????" in error_message or "????" in error_message or "????" in error_message',
        'if "session??" in detail or "??????" in detail',
        "'?????' in item_config_detail",
        'logger.info(f"????API: ??ID={user_id}, ??ID={goods_id}, ????={recharge_account}, ??URL={callback_url if callback_url else \'?\'}")',
        'logger.info(f"????API: ??ID={user_id}, ??ID={goods_id}, ????={account}, ??URL={callback_url if callback_url else \'?\'}")',
        "?{self.account_id}?????????:",
        "?? ????????: ??(",
        _gbk_mojibake("滑块验证失败"),
        _missing_tail("未找到滑块容器"),
        _missing_tail("未找到登录表单"),
        _missing_tail("session过期且清理会话状态后未找到登录表单"),
        _missing_tail("session验证异常且清理会话状态后未找到登录表单"),
        _missing_tail("页面会话已失效"),
        "[{msg_time}] 【{self.account_id}?",
        "【{log_account_id}?=========================================",
        "【{target_account_id}?=========================================",
        'log_prefix = f"【{self.account_id}】[{msg_id}]" if msg_id else f"【{self.account_id}?"',
        'log_prefix=f"【{self.account_id}?",',
        'log_prefix = f"【{self.account_id}?"',
        'logger.warning(f"【{self.account_id}? - {detail}")',
        'logger.info(f"【{log_account_id}? {cookie_name}: {display_value}{change_mark}")',
        "】过CookieManager重启实例...",
        "】开始过浏览器刷新Cookie...",
        "  ?'{key}':",
        "全屢字典",
        "注锢",
        "第丢次刷新超时",
        "\u6211\u5df2\u5c0f\u5200\uff0c\u5f85\u5222",
    ]

    for phrase in unexpected_phrases:
        assert phrase not in source_text


def test_browser_runtime_test_fixtures_are_restored():
    source_text = BROWSER_RUNTIME_TEST_FILE.read_text(encoding="utf-8")

    expected_phrases = [
        '"title": "我已小刀，待刀成"',
        '"reminderContent": "手动回一句"',
    ]
    unexpected_phrases = [
        "\u5f85\u5222?",
        _gbk_mojibake("手动回一句", replace_invalid=True),
    ]

    for phrase in expected_phrases:
        assert phrase in source_text

    for phrase in unexpected_phrases:
        assert phrase not in source_text
