"""项目启动入口：

1. 创建 CookieManager，按配置文件 / 环境变量初始化账号任务
2. 在后台线程启动 FastAPI (reply_server) 提供管理与自动回复接口
3. 主协程保持运行
"""

import os
import sys
import shutil
from pathlib import Path

# 设置标准输出编码为UTF-8（Windows兼容）
def _setup_console_encoding():
    """设置控制台编码为UTF-8，避免Windows GBK编码问题"""
    if sys.platform == 'win32':
        try:
            # 方法1: 设置环境变量
            os.environ['PYTHONIOENCODING'] = 'utf-8'
            
            # 方法2: 尝试设置控制台代码页为UTF-8
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleOutputCP(65001)  # UTF-8代码页
            except Exception:
                pass
            
            # 方法3: 重新包装stdout和stderr
            try:
                if hasattr(sys.stdout, 'buffer'):
                    import io
                    # 只在编码不是UTF-8时重新包装
                    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
                        sys.stdout = io.TextIOWrapper(
                            sys.stdout.buffer, 
                            encoding='utf-8', 
                            errors='replace',
                            line_buffering=True
                        )
                    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ('utf-8', 'utf8'):
                        sys.stderr = io.TextIOWrapper(
                            sys.stderr.buffer, 
                            encoding='utf-8', 
                            errors='replace',
                            line_buffering=True
                        )
            except Exception:
                pass
        except Exception:
            pass

# 在程序启动时设置编码
_setup_console_encoding()

# 定义ASCII安全字符（备用方案）
_OK = '[OK]'
_WARN = '[WARN]'
_ERROR = '[ERROR]'
_INFO = '[INFO]'

# ==================== 在导入任何模块之前先迁移数据库 ====================
def _migrate_database_files_early():
    """在启动前检查并迁移数据库文件到data目录（使用print，因为logger还未初始化）"""
    print("检查数据库文件位置...")
    
    # 确保data目录存在
    data_dir = Path("data")
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"{_OK} 创建 data 目录")
    
    # 定义需要迁移的文件
    files_to_migrate = [
        ("xianyu_data.db", "data/xianyu_data.db", "主数据库"),
        ("user_stats.db", "data/user_stats.db", "统计数据库"),
    ]
    
    migrated_files = []
    
    # 迁移主数据库和统计数据库
    for old_path, new_path, description in files_to_migrate:
        old_file = Path(old_path)
        new_file = Path(new_path)
        
        if old_file.exists():
            if not new_file.exists():
                # 新位置不存在，移动文件
                try:
                    shutil.move(str(old_file), str(new_file))
                    print(f"{_OK} 迁移{description}: {old_path} -> {new_path}")
                    migrated_files.append(description)
                except Exception as e:
                    print(f"{_WARN} 无法迁移{description}: {e}")
                    print(f"  尝试复制文件...")
                    try:
                        shutil.copy2(str(old_file), str(new_file))
                        print(f"{_OK} 已复制{description}到新位置")
                        print(f"  请在确认数据正常后手动删除: {old_path}")
                        migrated_files.append(f"{description}(已复制)")
                    except Exception as e2:
                        print(f"{_ERROR} 复制{description}失败: {e2}")
            else:
                # 新位置已存在，检查旧文件大小
                try:
                    if old_file.stat().st_size > 0:
                        print(f"{_WARN} 发现旧{description}文件: {old_path}")
                        print(f"  新数据库位于: {new_path}")
                        print(f"  建议备份后删除旧文件")
                except:
                    pass
    
    # 迁移备份文件
    backup_files = list(Path(".").glob("xianyu_data_backup_*.db"))
    if backup_files:
        print(f"发现 {len(backup_files)} 个备份文件")
        backup_migrated = 0
        for backup_file in backup_files:
            new_backup_path = data_dir / backup_file.name
            if not new_backup_path.exists():
                try:
                    shutil.move(str(backup_file), str(new_backup_path))
                    print(f"{_OK} 迁移备份文件: {backup_file.name}")
                    backup_migrated += 1
                except Exception as e:
                    print(f"{_WARN} 无法迁移备份文件 {backup_file.name}: {e}")
        
        if backup_migrated > 0:
            migrated_files.append(f"{backup_migrated}个备份文件")
    
    # 输出迁移总结
    if migrated_files:
        print(f"{_OK} 数据库迁移完成，已迁移: {', '.join(migrated_files)}")
    else:
        print(f"{_OK} 数据库文件检查完成")
    
    return True

# 在导入 db_manager 之前先执行数据库迁移
try:
    _migrate_database_files_early()
except Exception as e:
    print(f"{_WARN} 数据库迁移检查失败: {e}")
    # 继续启动，因为可能是首次运行

# ==================== 检查 CloakBrowser runtime ====================
def _check_cloakbrowser_runtime():
    """检查 CloakBrowser provider 是否可用，并给出统一 runtime 提示。"""
    print("检查 CloakBrowser runtime...")

    try:
        import cloakbrowser
    except ImportError:
        print(f"{_WARN} 未检测到 cloakbrowser 模块。请先执行: pip install -r requirements.txt")
        print("   首次部署或运行环境重建后，请执行: python -m cloakbrowser install")
        return False

    required_exports = (
        "launch",
        "launch_async",
        "launch_context",
        "launch_context_async",
        "launch_persistent_context",
        "launch_persistent_context_async",
    )
    missing_exports = [name for name in required_exports if not hasattr(cloakbrowser, name)]
    if missing_exports:
        print(f"{_WARN} CloakBrowser 模块不完整，缺少: {', '.join(missing_exports)}")
        print("   请重新安装依赖后执行: python -m cloakbrowser install")
        return False

    module_path = getattr(cloakbrowser, "__file__", None)
    print(f"{_OK} CloakBrowser provider 已就绪")
    if module_path:
        print(f"{_INFO} 模块位置: {module_path}")
    print(f"{_INFO} 浏览器 runtime 由 CloakBrowser 管理")
    print(f"{_INFO} 如首次部署、容器重建或 runtime 缺失，请执行: python -m cloakbrowser install")
    return True


# 检查 CloakBrowser runtime
try:
    _check_cloakbrowser_runtime()
except Exception as e:
    print(f"{_WARN} CloakBrowser runtime 检查失败: {e}")
    print("   程序将继续启动，但 CloakBrowser 功能可能不可用")
    # 继续启动，不影响主程序运行

# ==================== 现在可以安全地导入其他模块 ====================
import asyncio
import threading
import uvicorn
from urllib.parse import urlparse
from loguru import logger

# 修复Linux环境下的asyncio子进程问题
if sys.platform.startswith('linux'):
    try:
        # 在程序启动时就设置正确的事件循环策略
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        logger.debug("已设置事件循环策略以支持子进程")
    except Exception as e:
        logger.debug(f"设置事件循环策略失败: {e}")

from config import AUTO_REPLY, COOKIES_LIST
import cookie_manager as cm
from db_manager import db_manager
from file_log_collector import setup_file_logging


def _start_api_server():
    """后台线程启动 FastAPI 服务"""
    api_conf = AUTO_REPLY.get('api', {})

    # 优先使用环境变量配置
    host = os.getenv('API_HOST', '0.0.0.0')  # 默认绑定所有接口
    port = int(os.getenv('API_PORT', '8090'))  # 默认端口8090

    # 如果配置文件中有特定配置，则使用配置文件
    if 'host' in api_conf:
        host = api_conf['host']
    if 'port' in api_conf:
        port = api_conf['port']

    # 兼容旧的URL配置方式
    if 'url' in api_conf and 'host' not in api_conf and 'port' not in api_conf:
        url = api_conf.get('url', 'http://0.0.0.0:8090/xianyu/reply')
        parsed = urlparse(url)
        if parsed.hostname and parsed.hostname != 'localhost':
            host = parsed.hostname
        port = parsed.port or 8090

    logger.info(f"启动Web服务器: http://{host}:{port}")
    # 在后台线程中创建独立事件循环并直接运行 server.serve()
    import uvicorn
    try:
        config = uvicorn.Config("reply_server:app", host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    except Exception as e:
        logger.error(f"uvicorn服务器启动失败: {e}")
        try:
            # 确保线程内事件循环被正确关闭
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.stop()
        except Exception:
            pass




def load_keywords_file(path: str):
    """从文件读取关键字 -> [(keyword, reply)]"""
    kw_list = []
    p = Path(path)
    if not p.exists():
        return kw_list
    with p.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '\t' in line:
                k, r = line.split('\t', 1)
            elif ' ' in line:
                k, r = line.split(' ', 1)
            elif ':' in line:
                k, r = line.split(':', 1)
            else:
                continue
            kw_list.append((k.strip(), r.strip()))
    return kw_list


async def main():
    print("开始启动主程序...")

    # 初始化文件日志收集器
    print("初始化文件日志收集器...")
    setup_file_logging()
    logger.info("文件日志收集器已启动，开始收集实时日志")

    loop = asyncio.get_running_loop()

    # 创建 CookieManager 并在全局暴露
    print("创建 CookieManager...")
    cm.manager = cm.CookieManager(loop)
    manager = cm.manager
    print("CookieManager 创建完成")

    # 1) 从数据库加载的 Cookie 已经在 CookieManager 初始化时完成
    # 为每个启用的 Cookie 启动任务
    for account_id, cookie_value in manager.cookies.items():
        # 检查账号是否启用
        if not manager.get_cookie_status(account_id):
            logger.info(f"跳过禁用的账号: {account_id}")
            continue

        try:
            # 直接启动运行时任务，不重新保存到数据库
            from db_manager import db_manager
            logger.info(f"正在获取账号详细信息: {account_id}")
            cookie_info = db_manager.get_cookie_details(account_id)
            user_id = cookie_info.get('user_id') if cookie_info else None
            logger.info(f"账号详细信息获取成功: {account_id}, user_id: {user_id}")

            logger.info(f"正在创建账号运行时任务: {account_id}")
            manager.start_runtime_task(account_id, cookie_value, user_id)
            logger.info(f"启动数据库中的账号任务: {account_id} (用户ID: {user_id})")
            logger.info(f"任务已添加到管理器，当前任务数: {len(manager.tasks)}")
        except Exception as e:
            logger.error(f"启动账号任务失败: {account_id}, {e}")
            import traceback
            logger.error(f"详细错误信息: {traceback.format_exc()}")
    
    # 2) 如果配置文件中有新的 Cookie，也加载它们
    pending_account_registrations = []
    for entry in COOKIES_LIST:
        if not isinstance(entry, dict):
            logger.warning(f"跳过非法 COOKIES 配置条目，期望对象，实际为: {type(entry).__name__}")
            continue

        account_id = str(entry.get('account_id') or '').strip()
        cookie_value = str(entry.get('value') or '').strip()
        if not account_id or not cookie_value:
            logger.warning("跳过缺少 account_id 或 value 的 COOKIES 配置条目")
            continue

        try:
            db_manager._require_account_id(account_id)
        except ValueError as account_id_error:
            logger.warning(f"跳过非法 account_id 的 COOKIES 配置条目: {account_id_error}")
            continue

        if account_id in manager.cookies:
            continue
        
        kw_file = entry.get('keywords_file')
        kw_list = load_keywords_file(kw_file) if kw_file else None
        registration = manager.add_cookie(account_id, cookie_value, kw_list)
        if isinstance(registration, asyncio.Future):
            pending_account_registrations.append(registration)
        logger.info(f"从配置文件加载账号凭证: {account_id}")

    # 3) 若环境变量提供单账号 Cookie，必须显式绑定 ACCOUNT_ID
    env_cookie = os.getenv('COOKIES_STR')
    env_account_id = str(os.getenv('ACCOUNT_ID') or '').strip()
    if env_cookie:
        if not env_account_id:
            logger.warning("检测到 COOKIES_STR，但缺少 ACCOUNT_ID，跳过环境变量账号加载")
        else:
            try:
                db_manager._require_account_id(env_account_id)
            except ValueError as account_id_error:
                logger.warning(
                    f"检测到 COOKIES_STR，但 ACCOUNT_ID 非法，跳过环境变量账号加载: {account_id_error}"
                )
            else:
                if env_account_id in manager.list_cookies():
                    logger.info(f"环境变量账号已存在，跳过重复加载: {env_account_id}")
                else:
                    registration = manager.add_cookie(env_account_id, env_cookie)
                    if isinstance(registration, asyncio.Future):
                        pending_account_registrations.append(registration)
                    logger.info(f"从环境变量加载账号 Cookie: {env_account_id}")

    if pending_account_registrations:
        logger.info(f"等待 {len(pending_account_registrations)} 个启动期账号注册完成")
        await asyncio.gather(*pending_account_registrations)

    # 启动 API 服务线程
    print("启动 API 服务线程...")
    threading.Thread(target=_start_api_server, daemon=True).start()
    print("API 服务线程已启动")

    # 阻塞保持运行
    print("主程序启动完成，保持运行...")
    await asyncio.Event().wait()


if __name__ == '__main__':
    # 避免使用被monkey patch的asyncio.run()
    # 使用原生的事件循环管理方式
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()
