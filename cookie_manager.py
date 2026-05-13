from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from db_manager import db_manager

__all__ = ["CookieManager", "manager"]


class CookieManager:
    """管理账号 Cookie、关键词和对应的 XianyuLive 任务。"""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.cookies: Dict[str, str] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.keywords: Dict[str, List[Tuple[str, str]]] = {}
        self.cookie_status: Dict[str, bool] = {}
        self.auto_confirm_settings: Dict[str, bool] = {}
        self._task_locks: Dict[str, asyncio.Lock] = {}
        self.live_instances: Dict[str, Any] = {}
        self._load_from_db()

    def _load_from_db(self):
        """从数据库加载账号、关键词和状态。"""
        try:
            self.cookies = db_manager.get_all_cookies()
            self.keywords = db_manager.get_all_keywords()
            self.cookie_status = db_manager.get_all_cookie_status()
            self.auto_confirm_settings = {}

            for account_id in self.cookies.keys():
                if account_id not in self.cookie_status:
                    self.cookie_status[account_id] = True
                self.auto_confirm_settings[account_id] = db_manager.get_auto_confirm(account_id)

            logger.info(
                "从数据库加载了 %s 个账号、%s 组关键词、%s 个状态记录和 %s 个自动确认设置",
                len(self.cookies),
                len(self.keywords),
                len(self.cookie_status),
                len(self.auto_confirm_settings),
            )
        except Exception as exc:
            logger.error(f"从数据库加载数据失败: {exc}")

    def reload_from_db(self):
        """重新从数据库加载数据，并协调账号任务/运行时状态。"""
        logger.info("重新从数据库加载数据...")

        old_cookies = dict(self.cookies)
        old_status = dict(self.cookie_status)
        old_task_accounts = set(self.tasks.keys())
        old_live_accounts = set(self.live_instances.keys())
        old_lock_accounts = set(self._task_locks.keys())
        old_cookies_count = len(self.cookies)
        old_keywords_count = len(self.keywords)

        self._load_from_db()

        new_cookies_count = len(self.cookies)
        new_keywords_count = len(self.keywords)

        known_old_accounts = (
            set(old_cookies.keys())
            | set(old_status.keys())
            | old_task_accounts
            | old_live_accounts
            | old_lock_accounts
        )
        current_accounts = set(self.cookies.keys())
        removed_accounts = known_old_accounts - current_accounts

        for account_id in removed_accounts:
            self._stop_cookie_task(account_id)
            self.live_instances.pop(account_id, None)
            self._task_locks.pop(account_id, None)

        for account_id in current_accounts:
            enabled = self.cookie_status.get(account_id, True)
            had_task_or_instance = account_id in old_task_accounts or account_id in old_live_accounts
            cookie_changed = old_cookies.get(account_id) != self.cookies.get(account_id)
            had_old_cookie = account_id in old_cookies
            task_active = account_id in self.tasks and not self._prune_finished_task(account_id)

            if not enabled:
                if had_task_or_instance:
                    self._stop_cookie_task(account_id)
                continue

            if not had_old_cookie:
                self._start_cookie_task(account_id)
                continue

            if cookie_changed:
                if had_task_or_instance:
                    self.update_cookie(account_id, self.cookies[account_id], save_to_db=False)
                else:
                    self._dispatch_manager_coroutine(
                        self._invalidate_account_runtime(account_id, reason="task_restarted"),
                    )
                    self._start_cookie_task(account_id)
                continue

            if not task_active:
                if had_task_or_instance:
                    self._dispatch_manager_coroutine(
                        self._invalidate_account_runtime(account_id, reason="task_restarted"),
                    )
                self._start_cookie_task(account_id)

        logger.info(
            "数据重新加载完成: 账号 %s -> %s, 关键词组 %s -> %s",
            old_cookies_count,
            new_cookies_count,
            old_keywords_count,
            new_keywords_count,
        )
        return True

    @staticmethod
    def _get_account_browser_runtime_manager():
        from utils.account_browser_runtime import account_browser_runtime_manager

        return account_browser_runtime_manager

    async def _invalidate_account_runtime(self, account_id: str, *, reason: str):
        if not account_id:
            return

        try:
            runtime_manager = self._get_account_browser_runtime_manager()
            async_invalidated = False
            sync_invalidated = False
            async_error = None
            sync_error = None

            try:
                async_invalidated = bool(
                    await runtime_manager.invalidate_runtime(account_id, reason=reason)
                )
            except Exception as exc:
                async_error = exc

            invalidate_runtime_sync = getattr(runtime_manager, "invalidate_runtime_sync", None)
            if callable(invalidate_runtime_sync):
                try:
                    sync_invalidated = bool(
                        invalidate_runtime_sync(account_id, reason=reason)
                    )
                except Exception as exc:
                    sync_error = exc

            if async_error or sync_error:
                error_parts = []
                if async_error is not None:
                    error_parts.append(f"async={async_error}")
                if sync_error is not None:
                    error_parts.append(f"sync={sync_error}")
                logger.warning(
                    "账号运行时失效部分失败，已忽略: %s, reason=%s, error=%s",
                    account_id,
                    reason,
                    "; ".join(error_parts),
                )
                return

            logger.info(
                "账号运行时已失效: %s, reason=%s, async=%s, sync=%s",
                account_id,
                reason,
                async_invalidated,
                sync_invalidated,
            )
        except Exception as exc:
            logger.warning(
                "账号运行时失效失败，已忽略: %s, reason=%s, error=%s",
                account_id,
                reason,
                exc,
            )

    def _dispatch_manager_coroutine(self, coroutine, *, timeout: Optional[float] = None):
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop and current_loop == self.loop:
            return self.loop.create_task(coroutine)

        if hasattr(self.loop, "is_running") and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            return future.result(timeout=timeout)

        if current_loop is not None:
            raise RuntimeError("CookieManager 事件循环未运行，无法跨事件循环同步调度任务")

        return self.loop.run_until_complete(coroutine)

    def _prune_finished_task(self, account_id: str) -> bool:
        task = self.tasks.get(account_id)
        if task is None:
            return False
        if not callable(getattr(task, "done", None)) or not task.done():
            return False

        self.tasks.pop(account_id, None)
        logger.info(f"清理已完成的账号任务引用: {account_id}")
        return True

    def _handle_runtime_task_done(self, account_id: str, task: asyncio.Task):
        current_task = self.tasks.get(account_id)
        if current_task is task:
            self.tasks.pop(account_id, None)
            self.live_instances.pop(account_id, None)
            self._dispatch_manager_coroutine(
                self._invalidate_account_runtime(account_id, reason="task_exited"),
            )
            logger.info(f"账号任务已结束并移除索引: {account_id}")

    def start_runtime_task(self, account_id: str, cookie_value: str, user_id: int = None):
        task = self.loop.create_task(self._run_xianyu(account_id, cookie_value, user_id))
        self.tasks[account_id] = task
        task.add_done_callback(lambda finished_task: self._handle_runtime_task_done(account_id, finished_task))
        return task

    async def _run_xianyu(self, account_id: str, cookie_value: str, user_id: int = None):
        """在目标事件循环中启动 XianyuLive.main。"""
        logger.info(f"【{account_id}】_run_xianyu 方法开始执行...")

        try:
            from XianyuAutoAsync import XianyuLive

            live = XianyuLive(cookie_value, account_id=account_id, user_id=user_id)
            self.live_instances[account_id] = live
            logger.info(f"【{account_id}】XianyuLive 实例创建成功，开始调用 main()")
            await live.main()
            logger.warning(f"【{account_id}】XianyuLive.main() 正常退出（通常不应发生）")
        except asyncio.CancelledError:
            logger.info(f"【{account_id}】XianyuLive 任务已取消")
            raise
        except Exception as exc:
            import traceback

            logger.error(f"【{account_id}】XianyuLive 任务异常: {exc}")
            logger.error(f"【{account_id}】详细错误信息:\n{traceback.format_exc()}")
        finally:
            self.live_instances.pop(account_id, None)
            logger.info(f"【{account_id}】_run_xianyu 方法执行结束")

    async def _add_cookie_async(self, account_id: str, cookie_value: str, user_id: int = None):
        if account_id not in self._task_locks:
            self._task_locks[account_id] = asyncio.Lock()

        async with self._task_locks[account_id]:
            restart_existing_task = False
            if account_id in self.tasks:
                restart_existing_task = True
                existing_task = self.tasks.pop(account_id)
                if not existing_task.done():
                    logger.warning(f"【{account_id}】任务已存在且正在运行，先停止旧任务...")
                    existing_task.cancel()
                    try:
                        await existing_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.error(f"等待旧任务停止时出错: {account_id}, {exc}")
                    logger.info(f"【{account_id}】旧任务已停止")
                else:
                    logger.info(f"【{account_id}】旧任务已完成，已移除")

            if restart_existing_task:
                await self._invalidate_account_runtime(account_id, reason="task_restarted")

            self.cookies[account_id] = cookie_value
            db_manager.save_cookie(account_id, cookie_value, user_id)

            actual_user_id = user_id
            if actual_user_id is None:
                cookie_info = db_manager.get_cookie_details(account_id)
                if cookie_info:
                    actual_user_id = cookie_info.get("user_id")

            self.start_runtime_task(account_id, cookie_value, actual_user_id)
            logger.info(f"已启动账号任务: {account_id} (用户ID: {actual_user_id})")

    async def _remove_cookie_async(self, account_id: str):
        if account_id not in self._task_locks:
            self._task_locks[account_id] = asyncio.Lock()

        async with self._task_locks[account_id]:
            task = self.tasks.pop(account_id, None)
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"【{account_id}】等待任务停止超时（10秒），强制继续")
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.error(f"等待任务清理时出错: {account_id}, {exc}")

            await self._invalidate_account_runtime(account_id, reason="account_removed")

            self.cookies.pop(account_id, None)
            self.keywords.pop(account_id, None)
            self.cookie_status.pop(account_id, None)
            self.auto_confirm_settings.pop(account_id, None)
            self.live_instances.pop(account_id, None)
            self._task_locks.pop(account_id, None)

            db_manager.delete_cookie(account_id)
            logger.info(f"已移除账号: {account_id}")

    def add_cookie(
        self,
        account_id: str,
        cookie_value: str,
        kw_list: Optional[List[Tuple[str, str]]] = None,
        user_id: int = None,
    ):
        if kw_list is not None:
            self.keywords[account_id] = kw_list
        else:
            self.keywords.setdefault(account_id, [])
        return self._dispatch_manager_coroutine(
            self._add_cookie_async(account_id, cookie_value, user_id),
        )

    def remove_cookie(self, account_id: str):
        return self._dispatch_manager_coroutine(
            self._remove_cookie_async(account_id),
        )

    def update_cookie(self, account_id: str, new_value: str, save_to_db: bool = True):
        """替换指定账号的 Cookie，并按账号语义重启任务。"""

        async def _update():
            if account_id not in self._task_locks:
                self._task_locks[account_id] = asyncio.Lock()

            async with self._task_locks[account_id]:
                original_user_id = None
                original_keywords: List[Tuple[str, str]] = []
                original_status = True

                cookie_info = db_manager.get_cookie_details(account_id)
                if cookie_info:
                    original_user_id = cookie_info.get("user_id")

                if account_id in self.keywords:
                    original_keywords = self.keywords[account_id].copy()
                if account_id in self.cookie_status:
                    original_status = self.cookie_status[account_id]

                task = self.tasks.pop(account_id, None)
                if task:
                    logger.info(f"【{account_id}】正在停止旧任务...")
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=10.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"【{account_id}】等待旧任务停止超时（10秒），强制继续")
                    except asyncio.CancelledError:
                        logger.debug(f"【{account_id}】旧任务已取消")
                    except Exception as exc:
                        logger.error(f"等待任务清理时出错: {account_id}, {exc}")
                    logger.info(f"【{account_id}】旧任务已停止")

                await self._invalidate_account_runtime(account_id, reason="task_restarted")

                self.cookies[account_id] = new_value

                if save_to_db:
                    db_manager.save_cookie(account_id, new_value, original_user_id)

                self.keywords[account_id] = original_keywords
                self.cookie_status[account_id] = original_status

                if original_status:
                    self.start_runtime_task(account_id, new_value, original_user_id)
                    logger.info(
                        f"已更新账号 Cookie 并重启任务: {account_id} "
                        f"(用户ID: {original_user_id}, 关键词: {len(original_keywords)}条)"
                    )
                else:
                    logger.info(
                        f"已更新禁用账号 Cookie，保持停止状态: {account_id} "
                        f"(用户ID: {original_user_id}, 关键词: {len(original_keywords)}条)"
                    )

        return self._dispatch_manager_coroutine(_update())

    def update_keywords(self, account_id: str, kw_list: List[Tuple[str, str]]):
        self.keywords[account_id] = kw_list
        db_manager.save_keywords(account_id, kw_list)
        logger.info(f"更新关键字: {account_id} -> {len(kw_list)} 条")

    def list_cookies(self):
        return list(self.cookies.keys())

    def get_keywords(self, account_id: str) -> List[Tuple[str, str]]:
        return self.keywords.get(account_id, [])

    def update_cookie_status(self, account_id: str, enabled: bool):
        if account_id not in self.cookies:
            raise ValueError(f"账号 ID {account_id} 不存在")

        old_status = self.cookie_status.get(account_id, True)
        self.cookie_status[account_id] = enabled
        db_manager.save_cookie_status(account_id, enabled)
        logger.info(f"更新账号状态: {account_id} -> {'启用' if enabled else '禁用'}")

        if old_status != enabled:
            if enabled:
                self._start_cookie_task(account_id)
            else:
                self._stop_cookie_task(account_id)

    def get_cookie_status(self, account_id: str) -> bool:
        return self.cookie_status.get(account_id, True)

    def get_enabled_cookies(self) -> Dict[str, str]:
        return {
            account_id: value
            for account_id, value in self.cookies.items()
            if self.cookie_status.get(account_id, True)
        }

    def get_xianyu_instance(self, account_id: str):
        return self.live_instances.get(account_id)

    def _start_cookie_task(self, account_id: str):
        if account_id in self.tasks and not self._prune_finished_task(account_id):
            logger.warning(f"账号任务已存在，跳过启动: {account_id}")
            return

        cookie_value = self.cookies.get(account_id)
        if not cookie_value:
            logger.error(f"Cookie 值不存在，无法启动任务: {account_id}")
            return

        try:
            cookie_info = db_manager.get_cookie_details(account_id)
            user_id = cookie_info.get("user_id") if cookie_info else None
            self._dispatch_manager_coroutine(
                self._add_cookie_async(account_id, cookie_value, user_id),
                timeout=5,
            )
            logger.info(f"成功启动账号任务: {account_id}")
        except Exception as exc:
            logger.error(f"启动账号任务失败: {account_id}, {exc}")

    def _stop_cookie_task(self, account_id: str):
        async def _stop_task_async():
            try:
                task = self.tasks.get(account_id)
                if task is None:
                    logger.warning(f"账号任务不存在，仍将清理运行时: {account_id}")
                else:
                    self.tasks.pop(account_id, None)

                if task is not None and not task.done():
                    task.cancel()
                    stop_timeout_seconds = float(
                        getattr(self, "_task_stop_timeout_seconds", 10.0)
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task),
                            timeout=stop_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"【{account_id}】等待任务停止超时（{stop_timeout_seconds}秒），继续执行运行时清理"
                        )
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.error(f"等待任务清理时出错: {account_id}, {exc}")
                    logger.info(f"已取消账号任务: {account_id}")
                elif task is not None and task.done():
                    logger.info(f"账号任务已完成，直接清理: {account_id}")

                self.tasks.pop(account_id, None)
                await self._invalidate_account_runtime(account_id, reason="task_stopped")
                self.live_instances.pop(account_id, None)
                logger.info(f"成功停止账号任务: {account_id}")
            except Exception as exc:
                logger.error(f"停止账号任务失败: {account_id}, {exc}")

        try:
            return self._dispatch_manager_coroutine(_stop_task_async(), timeout=10)
        except Exception as exc:
            logger.error(f"停止账号任务失败: {account_id}, {exc}")

    def update_auto_confirm_setting(self, account_id: str, auto_confirm: bool):
        try:
            self.auto_confirm_settings[account_id] = auto_confirm
            logger.info(
                f"更新账号 {account_id} 自动确认发货设置: {'开启' if auto_confirm else '关闭'}"
            )

            if account_id in self.tasks and not self.tasks[account_id].done():
                logger.info(f"账号 {account_id} 正在运行，自动确认发货设置已实时生效")
        except Exception as exc:
            logger.error(f"更新自动确认发货设置失败: {account_id}, {exc}")

    def get_auto_confirm_setting(self, account_id: str) -> bool:
        return self.auto_confirm_settings.get(account_id, True)


manager: Optional[CookieManager] = None
