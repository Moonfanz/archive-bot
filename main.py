import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import (
     CategoryChannel,
    Color,
    Embed,
    ForumChannel,
    Guild,
    Intents,
    Message,
    TextChannel,
    Thread,
    app_commands,
)
from discord.ext import commands, tasks
from dotenv import load_dotenv

# --- 全局配置与常量 ---
load_dotenv()

CONFIG_FILENAME = "bot_config.json"
LOG_DIRECTORY = Path("logs")
LOG_FILENAME = LOG_DIRECTORY / "archiver_bot.log"
DATA_DIRECTORY = Path("data")

# 定时巡检间隔（分钟），可在 .env 中通过 AUDIT_INTERVAL_MINUTES 覆盖，默认 15 分钟
try:
    AUDIT_INTERVAL_MINUTES = max(1, int(os.environ.get("AUDIT_INTERVAL_MINUTES", "15")))
except ValueError:
    AUDIT_INTERVAL_MINUTES = 15

# 并发请求信号量（控制同时拉取历史记录与归档操作的并发度，避免触发速率限制）
HISTORY_FETCH_CONCURRENCY = 10
ARCHIVE_EDIT_CONCURRENCY = 5


# --- 日志配置 ---
def setup_logging() -> logging.Logger:
    """配置日志记录器（防止重复挂载 Handler）"""
    LOG_DIRECTORY.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")

    discord_logger = logging.getLogger("discord")
    discord_logger.setLevel(logging.INFO)

    bot_logger = logging.getLogger("archiver_bot")
    bot_logger.setLevel(logging.DEBUG)

    if not bot_logger.handlers:
        file_handler = logging.FileHandler(filename=LOG_FILENAME, encoding="utf-8", mode="a")
        file_handler.setFormatter(formatter)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)

        discord_logger.addHandler(file_handler)
        discord_logger.addHandler(stream_handler)

        bot_logger.addHandler(file_handler)
        bot_logger.addHandler(stream_handler)

    return bot_logger


bot_log = setup_logging()


# --- 自定义数据类 ---
class GuildArchiveSettings:
    """封装单个服务器的归档设置（黑名单模式）"""

    def __init__(
        self,
        guild_id: int,
        config_name: str,
        blacklist_channel_ids: list[int],
        archive_category_id: int | None,
        inactivity_days: int,
        notification_thread_id: int | None,
        max_active_posts: int,
        max_active_threads: int,
        last_notice_message_id: int | None = None,
        pinned_thread_moderation: dict | None = None,
        enable_channel_limit: bool = False,
    ):
        self.guild_id = int(guild_id)
        self.config_name = config_name
        # 黑名单频道：这些频道中的帖子计入活跃总数，但不会被自动归档
        self.blacklist_channel_ids = [int(cid) for cid in (blacklist_channel_ids or [])]
        self.archive_category_id = int(archive_category_id) if archive_category_id else None
        self.inactivity_days = int(inactivity_days)
        self.notification_thread_id = int(notification_thread_id) if notification_thread_id else None
        self.max_active_posts = int(max_active_posts)
        self.max_active_threads = int(max_active_threads)
        self.last_notice_message_id = last_notice_message_id
        # 显式控制单频道帖子上限是否启用，防止旧配置默认值 100 误归档大型卡区
        self.enable_channel_limit = bool(enable_channel_limit)

        mod_settings = pinned_thread_moderation or {}
        self.pinned_mod_enabled = bool(mod_settings.get("enabled", False))
        self.allowed_role_ids = [int(role_id) for role_id in mod_settings.get("allowed_role_ids", [])]
        self.allowed_user_ids = [int(user_id) for user_id in mod_settings.get("allowed_user_ids", [])]

    def to_dict(self) -> dict:
        """将设置转换为字典以便保存到 JSON"""
        return {
            "guild_id": self.guild_id,
            "blacklist_channel_ids": self.blacklist_channel_ids,
            "archive_category_id": self.archive_category_id,
            "inactivity_days": self.inactivity_days,
            "notification_thread_id": self.notification_thread_id,
            "max_active_posts": self.max_active_posts,
            "enable_channel_limit": self.enable_channel_limit,
            "max_active_threads": self.max_active_threads,
            "last_notice_message_id": self.last_notice_message_id,
            "pinned_thread_moderation": {
                "enabled": self.pinned_mod_enabled,
                "allowed_role_ids": [str(role_id) for role_id in self.allowed_role_ids],
                "allowed_user_ids": [str(user_id) for user_id in self.allowed_user_ids],
            },
        }

    @classmethod
    def from_dict(cls, guild_id: int, config_name: str, data: dict) -> "GuildArchiveSettings":
        """从字典创建 GuildArchiveSettings 实例"""
        return cls(
            guild_id=guild_id,
            config_name=config_name,
            blacklist_channel_ids=data.get("blacklist_channel_ids", []),
            archive_category_id=data.get("archive_category_id"),
            inactivity_days=data.get("inactivity_days", 0),
            notification_thread_id=data.get("notification_thread_id"),
            max_active_posts=data.get("max_active_posts", 0),
            max_active_threads=data.get("max_active_threads", 800),
            last_notice_message_id=data.get("last_notice_message_id"),
            pinned_thread_moderation=data.get("pinned_thread_moderation"),
            enable_channel_limit=data.get("enable_channel_limit", False),
        )


@dataclass
class ThreadActivityInfo:
    """记录单个帖子的最后活跃时间及来源状态"""

    thread: discord.Thread
    last_active_at: datetime  # UTC aware datetime
    is_fallback_created_at: bool = False  # 若帖子内无现存消息，退化为建帖时间
    from_cache: bool = False


@dataclass
class ArchivePlanItem:
    """待归档帖子及其触发原因"""

    info: ThreadActivityInfo
    reason_tag: str  # "inactivity" | "channel_cap" | "server_cap"
    reason_label: str


# --- 辅助函数 ---
def ensure_utc(dt: datetime | None) -> datetime:
    """确保 datetime 为 UTC 时区感知对象"""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def safe_add_embed_field(embed: Embed, name: str, value: str, inline: bool = False):
    """安全添加 Embed 字段，自动处理长度限制以防 Discord 400 报错"""
    if not value or not value.strip():
        return
    trimmed_name = name[:256]
    trimmed_value = value if len(value) <= 1024 else value[:1010] + "\n...(已截断)"

    # 检查 Embed 总字符上限 (6000)，预留安全余量
    current_total = len(embed.title or "") + len(embed.description or "")
    if embed.author and embed.author.name:
        current_total += len(embed.author.name)
    if embed.footer and embed.footer.text:
        current_total += len(embed.footer.text)
    for f in embed.fields:
        current_total += len(f.name) + len(f.value)

    if len(embed.fields) < 25 and (current_total + len(trimmed_name) + len(trimmed_value) <= 5800):
        embed.add_field(name=trimmed_name, value=trimmed_value, inline=inline)


# --- 机器人核心类 ---
class ThreadArchiverBot(commands.Bot):
    def __init__(self):
        intents = Intents.default()
        intents.guilds = True

        super().__init__(command_prefix=commands.when_mentioned_or("!archiver "), intents=intents)

        self.global_config = {}
        self.guild_settings_map: dict[int, GuildArchiveSettings] = {}
        self.bot_token: str | None = None
        self.operation_lock = asyncio.Lock()

        DATA_DIRECTORY.mkdir(exist_ok=True)

        # 置顶帖保活记录
        self.BUMP_RECORDS_FILE = DATA_DIRECTORY / "pinned_thread_bump_records.json"
        self.bump_records: dict[int, dict] = {}
        self.bump_records_lock = asyncio.Lock()

        # 置顶帖最后消息审计记录
        self.PINNED_MESSAGES_FILE = DATA_DIRECTORY / "pinned_thread_last_messages.json"
        self.pinned_last_messages: dict[int, int] = {}
        self.pinned_messages_lock = asyncio.Lock()

        # 活跃帖最后活跃时间缓存: {thread_id: {"last_message_id": int | None, "last_active_ts": float, "is_fallback": bool}}
        self.ACTIVITY_CACHE_FILE = DATA_DIRECTORY / "thread_activity_cache.json"
        self.thread_activity_cache: dict[int, dict] = {}
        self.activity_cache_lock = asyncio.Lock()

    # ---------------- 配置与持久化 ----------------
    async def load_configuration(self):
        """加载配置（从环境变量 GUILD_CONFIGS_JSON）"""
        self.bot_token = os.environ.get("BOT_TOKEN")
        if not self.bot_token:
            bot_log.critical("未能从环境变量加载 BOT_TOKEN。请检查 .env 文件。")
            sys.exit(1)

        raw_json = os.environ.get("GUILD_CONFIGS_JSON")
        if not raw_json:
            bot_log.critical("未能从环境变量加载 GUILD_CONFIGS_JSON。请在 .env 中配置服务器参数。")
            sys.exit(1)

        try:
            guild_configurations = json.loads(raw_json)
            if not isinstance(guild_configurations, dict):
                raise ValueError("GUILD_CONFIGS_JSON 必须是一个 JSON 对象，其键为服务器配置名。")

            self.global_config = {"guild_configurations": guild_configurations}
            self.guild_settings_map.clear()

            for config_name, settings_data in guild_configurations.items():
                guild_id = settings_data.get("guild_id")
                if not guild_id:
                    bot_log.warning(f"配置项 '{config_name}' 缺少 'guild_id'，已跳过。")
                    continue

                guild_id = int(guild_id)
                notice_id_file = DATA_DIRECTORY / f"{config_name}_notice_id.txt"
                last_notice_id = None

                if notice_id_file.exists():
                    try:
                        last_notice_id = int(notice_id_file.read_text(encoding="utf-8").strip())
                    except ValueError:
                        bot_log.warning(f"无法解析 {notice_id_file} 中的消息ID。")

                settings_data["last_notice_message_id"] = last_notice_id
                guild_setting = GuildArchiveSettings.from_dict(guild_id, config_name, settings_data)
                self.guild_settings_map[guild_id] = guild_setting
                bot_log.info(f"已加载服务器 '{config_name}' (ID: {guild_id}) 的配置。")

        except json.JSONDecodeError:
            bot_log.critical("环境变量 GUILD_CONFIGS_JSON 的 JSON 格式错误。")
            sys.exit(1)
        except Exception as e:
            bot_log.critical(f"加载 GUILD_CONFIGS_JSON 时发生未知错误: {e}", exc_info=True)
            sys.exit(1)

    async def save_guild_setting(self, guild_id: int):
        """保存单个服务器的配置（写回 .env 中的 GUILD_CONFIGS_JSON，并更新本地通知ID文件）"""
        if guild_id not in self.guild_settings_map:
            bot_log.error(f"尝试保存未知的服务器配置: {guild_id}")
            return

        setting = self.guild_settings_map[guild_id]
        try:
            self._save_guild_configs_to_env()
            bot_log.info(".env 中的 GUILD_CONFIGS_JSON 已更新。")
        except Exception as e:
            bot_log.error(f"写入 .env 中的 GUILD_CONFIGS_JSON 失败: {e}", exc_info=True)

        if setting.last_notice_message_id is not None:
            notice_id_file = DATA_DIRECTORY / f"{setting.config_name}_notice_id.txt"
            try:
                notice_id_file.write_text(str(setting.last_notice_message_id), encoding="utf-8")
                bot_log.info(f"已更新 {setting.config_name} 的通知消息ID到 {notice_id_file}")
            except Exception as e:
                bot_log.error(f"写入 {notice_id_file} 失败: {e}", exc_info=True)

    def _save_guild_configs_to_env(self):
        """将当前内存中的服务器配置安全写回到 .env 的 GUILD_CONFIGS_JSON 变量"""
        guild_configurations: dict[str, dict] = {}
        for setting in self.guild_settings_map.values():
            guild_configurations[setting.config_name] = setting.to_dict()

        self.global_config = {"guild_configurations": guild_configurations}
        new_json_str = json.dumps(guild_configurations, ensure_ascii=False)
        # 同步更新当前进程环境变量
        os.environ["GUILD_CONFIGS_JSON"] = new_json_str

        # 若无单引号则使用单引号包裹，避免 python-dotenv 因空格或 # 截断 JSON
        if "'" not in new_json_str:
            new_line = f"GUILD_CONFIGS_JSON='{new_json_str}'"
        else:
            new_line = f"GUILD_CONFIGS_JSON={new_json_str}"

        env_path = Path(".env")
        existing_content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = existing_content.splitlines() if existing_content else []

        updated = False
        for idx, line in enumerate(lines):
            if line.strip().startswith("GUILD_CONFIGS_JSON="):
                lines[idx] = new_line
                updated = True
                break

        if not updated:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(new_line)

        new_content = ("\n".join(lines) + "\n") if lines else (new_line + "\n")
        env_path.write_text(new_content, encoding="utf-8")

    def _load_bump_records(self):
        """同步加载刷新记录文件到内存"""
        try:
            with open(self.BUMP_RECORDS_FILE, "r", encoding="utf-8") as f:
                records_from_file = json.load(f)
                for thread_id, data in records_from_file.items():
                    self.bump_records[int(thread_id)] = {
                        "last_bumped_utc": ensure_utc(datetime.fromisoformat(data["last_bumped_utc"]))
                    }
                bot_log.info(f"成功从 {self.BUMP_RECORDS_FILE} 加载了 {len(self.bump_records)} 条置顶帖刷新记录。")
        except FileNotFoundError:
            bot_log.info(f"刷新记录文件 {self.BUMP_RECORDS_FILE} 未找到，将创建一个新的。")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            bot_log.error(f"加载刷新记录文件失败，文件可能已损坏: {e}", exc_info=True)

    async def _save_bump_records(self):
        """将内存中的刷新记录保存到文件"""
        async with self.bump_records_lock:
            try:
                records_to_save = {
                    str(tid): {"last_bumped_utc": data["last_bumped_utc"].isoformat()}
                    for tid, data in self.bump_records.items()
                }
                with open(self.BUMP_RECORDS_FILE, "w", encoding="utf-8") as f:
                    json.dump(records_to_save, f, indent=4)
            except Exception as e:
                bot_log.error(f"保存刷新记录到 {self.BUMP_RECORDS_FILE} 失败: {e}", exc_info=True)

    def _load_pinned_last_messages(self):
        """同步加载置顶帖最后消息ID记录"""
        try:
            with open(self.PINNED_MESSAGES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.pinned_last_messages = {int(k): int(v) for k, v in data.items() if v is not None}
                bot_log.info(f"成功加载了 {len(self.pinned_last_messages)} 条置顶帖消息记录。")
        except FileNotFoundError:
            bot_log.info("置顶帖消息记录文件未找到，将创建新的。")
        except Exception as e:
            bot_log.error(f"加载置顶帖消息记录失败: {e}", exc_info=True)

    async def _save_pinned_last_messages(self):
        """保存置顶帖最后消息ID记录到文件"""
        async with self.pinned_messages_lock:
            try:
                data = {str(k): v for k, v in self.pinned_last_messages.items()}
                with open(self.PINNED_MESSAGES_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                bot_log.error(f"保存置顶帖消息记录失败: {e}", exc_info=True)

    def _load_thread_activity_cache(self):
        """加载活跃帖最后活跃时间缓存"""
        try:
            with open(self.ACTIVITY_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                for tid_str, entry in raw.items():
                    if isinstance(entry, dict) and "last_active_ts" in entry:
                        self.thread_activity_cache[int(tid_str)] = {
                            "last_message_id": entry.get("last_message_id"),
                            "last_active_ts": float(entry["last_active_ts"]),
                            "is_fallback": bool(entry.get("is_fallback", False)),
                        }
                bot_log.info(f"成功加载了 {len(self.thread_activity_cache)} 条帖子活跃时间缓存。")
        except FileNotFoundError:
            bot_log.info("帖子活跃时间缓存文件未找到，将在首次运行后自动生成。")
        except Exception as e:
            bot_log.warning(f"加载帖子活跃时间缓存失败，将重新构建: {e}")

    async def _save_thread_activity_cache(self, keep_thread_ids: set[int] | None = None):
        """保存并按需清理过期的帖子活跃时间缓存"""
        async with self.activity_cache_lock:
            try:
                if keep_thread_ids is not None:
                    # 仅保留当前仍在受管服务器活跃池中的帖子，防止缓存无限膨胀
                    self.thread_activity_cache = {
                        tid: val for tid, val in self.thread_activity_cache.items() if tid in keep_thread_ids
                    }
                serializable = {str(tid): val for tid, val in self.thread_activity_cache.items()}
                with open(self.ACTIVITY_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(serializable, f)
            except Exception as e:
                bot_log.warning(f"保存帖子活跃时间缓存失败: {e}")

    # ---------------- 生命周期与事件 ----------------
    async def setup_hook(self):
        """Bot启动时的异步设置"""
        self._load_bump_records()
        self._load_pinned_last_messages()
        self._load_thread_activity_cache()
        await self.load_configuration()
        await self.add_cog(ArchiveManagerCog(self))

        try:
            synced = await self.tree.sync()
            bot_log.info(f"已同步 {len(synced)} 个应用命令。")
        except Exception as e:
            bot_log.error(f"同步应用命令失败: {e}", exc_info=True)

        self.periodic_thread_audit.start()

    def _is_user_exempt(
        self, author: discord.Member | discord.User, thread: discord.Thread, settings: GuildArchiveSettings
    ) -> bool:
        """检查用户是否在置顶帖中拥有消息豁免权"""
        if thread.owner_id and author.id == thread.owner_id:
            return True

        if isinstance(author, discord.Member):
            if author.guild_permissions.administrator:
                return True
            author_role_ids = {role.id for role in author.roles}
            if not author_role_ids.isdisjoint(settings.allowed_role_ids):
                return True

        if author.id in settings.allowed_user_ids:
            return True

        return False

    async def on_ready(self):
        if not self.user:
            bot_log.error("机器人未能正确初始化 self.user。")
            return

        bot_log.info(
            f"机器人 '{self.user.name}' (ID: {self.user.id}) 已成功登录！"
            f"当前管理 {len(self.guild_settings_map)} 个服务器配置，巡检间隔: {AUDIT_INTERVAL_MINUTES} 分钟。"
        )

        main_admin_channel_id_str = os.environ.get("MAIN_ADMIN_CHANNEL_ID")
        if main_admin_channel_id_str and main_admin_channel_id_str.isdigit():
            main_admin_channel_id = int(main_admin_channel_id_str)
            try:
                channel = await self.fetch_channel(main_admin_channel_id)
                if isinstance(channel, (TextChannel, Thread)):
                    await channel.send(
                        f"✅ **{self.user.name} 已启动** "
                        f"({datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 巡检间隔: {AUDIT_INTERVAL_MINUTES}min)"
                    )
            except discord.NotFound:
                bot_log.warning(f"配置的 MAIN_ADMIN_CHANNEL_ID: {main_admin_channel_id} 未找到。")
            except discord.Forbidden:
                bot_log.warning(f"无权向 MAIN_ADMIN_CHANNEL_ID: {main_admin_channel_id} 发送消息。")
            except Exception as e:
                bot_log.error(f"发送启动通知时出错: {e}", exc_info=True)

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        if not thread.flags.pinned:
            return

        settings = self.guild_settings_map.get(message.guild.id)
        if not settings or not settings.pinned_mod_enabled:
            return

        if self._is_user_exempt(message.author, thread, settings):
            return

        try:
            await message.delete()
            bot_log.debug(f"已删除用户 {message.author.name} ({message.author.id}) 在置顶帖 '{thread.name}' 中的消息。")
        except discord.Forbidden:
            parent_name = thread.parent.name if thread.parent else "未知频道"
            bot_log.warning(f"无法删除消息：缺少在频道 '{parent_name}' 中 '管理消息' 的权限。")
        except discord.NotFound:
            pass
        except Exception as e:
            bot_log.error(f"删除置顶帖非豁免消息时发生错误: {e}", exc_info=True)

    # ---------------- 定时任务 ----------------
    @tasks.loop(minutes=AUDIT_INTERVAL_MINUTES)
    async def periodic_thread_audit(self):
        """定时检查并处理各服务器活跃帖子"""
        if not self.guild_settings_map:
            return

        async with self.operation_lock:
            bot_log.info("开始执行周期性帖子审计...")
            all_known_active_ids: set[int] = set()

            for guild_id, settings in list(self.guild_settings_map.items()):
                guild = self.get_guild(guild_id)
                if not guild:
                    bot_log.warning(f"审计：找不到服务器 {guild_id}，跳过。")
                    continue

                try:
                    bot_log.info(f"正在审计服务器: '{guild.name}' (ID: {guild_id})")
                    remaining_ids = await self.process_guild_threads(guild, settings, manual=False)
                    if remaining_ids is not None:
                        all_known_active_ids.update(remaining_ids)
                except Exception as e:
                    bot_log.error(f"审计服务器 '{guild.name}' (ID: {guild_id}) 时发生未捕获异常: {e}", exc_info=True)

                await asyncio.sleep(3)

            # 持久化并顺便清理已不再活跃的旧缓存条目
            if all_known_active_ids:
                await self._save_thread_activity_cache(keep_thread_ids=all_known_active_ids)
            bot_log.info("周期性帖子审计完成。")

    @periodic_thread_audit.before_loop
    async def before_periodic_audit(self):
        await self.wait_until_ready()
        bot_log.info(f"定时审计任务已准备就绪（每 {AUDIT_INTERVAL_MINUTES} 分钟执行一次）。")

    @periodic_thread_audit.error
    async def on_periodic_audit_error(self, error: BaseException):
        bot_log.error(f"定时审计循环捕获顶层异常（循环将继续保持运行）: {error}", exc_info=True)

    # ---------------- 核心归档流水线 ----------------
    async def process_guild_threads(
        self, guild: Guild, settings: GuildArchiveSettings, manual: bool = False
    ) -> set[int] | None:
        """
        单次全流程处理：
        1. 拉取全服活跃帖 -> 2. 置顶帖保活与审计 -> 3. 候选帖权限与黑名单过滤 ->
        4. 缓存优先获取最后活跃时间 -> 5. 统一规划归档（超时 -> 单频道上限 -> 全服上限） ->
        6. 并发归档 -> 7. 生成多维统计日志与监控面板报告
        """
        global_start_time = time.time()
        now_utc = datetime.now(timezone.utc)
        current_run_timestamp_str = now_utc.astimezone(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S UTC+8"
        )
        run_hash_value = hashlib.md5(f"{guild.id}-{global_start_time}".encode()).hexdigest()[:8]

        mode_str = "手动清理" if manual else "自动清理"
        log_lines = [
            f"> 不活跃 **{settings.inactivity_days}** 天归档: {'开启' if settings.inactivity_days > 0 else '关闭'}",
            (
                f"已配置 {len(settings.blacklist_channel_ids)} 个黑名单频道，这些频道中的帖子不会被自动归档。"
                if settings.blacklist_channel_ids
                else "未配置黑名单频道，将对所有有权限频道执行归档策略。"
            ),
            f"\n<== 开始{mode_str} ==> 服务器: {settings.config_name} ({guild.name}) | 日志索引: {run_hash_value}",
            f"服务器级活跃帖目标: {settings.max_active_threads}",
        ]

        # --- 步骤 1: 获取服务器所有活跃帖子 ---
        try:
            all_server_active_threads_list = await guild.active_threads()
        except discord.Forbidden:
            bot_log.error(f"无权获取服务器 {guild.name} 的活跃帖子。", exc_info=True)
            return None
        except Exception as e:
            bot_log.error(f"获取服务器 {guild.name} 活跃帖子失败: {e}", exc_info=True)
            return None

        current_total_server_threads = len(all_server_active_threads_list)

        # --- 步骤 2: 置顶帖保活处理与审计 ---
        pinned_threads_set_server_wide: set[int] = set()
        log_lines.append("置顶帖处理 (全服务器):")
        self_trust_duration = timedelta(hours=48)
        bump_changed = False
        pinned_msg_changed = False

        for thread_obj in all_server_active_threads_list:
            if not thread_obj.flags.pinned:
                continue

            pinned_threads_set_server_wide.add(thread_obj.id)
            try:
                if thread_obj.archived:
                    await thread_obj.edit(archived=False, reason="[保活] 发现已归档的置顶帖，进行激活")
                    log_lines.append(f"  [已取消置顶帖归档] {thread_obj.name} (ID: {thread_obj.id})")
                    continue

                record = self.bump_records.get(thread_obj.id)
                if record and (now_utc - record["last_bumped_utc"] < self_trust_duration):
                    continue

                reason_for_bump = "记录不存在" if not record else "记录已过期"
                log_lines.append(f"  [需要保活] {thread_obj.name} (原因: {reason_for_bump})")

                prev_stored_msg_id = self.pinned_last_messages.get(thread_obj.id)
                prev_thread_msg_id = thread_obj.last_message_id

                if not thread_obj.locked:
                    temp_message = await thread_obj.send("置顶帖保活，稍后删除")
                    await temp_message.delete()
                    log_lines.append("    -> 已通过消息刷新。")
                    # 若保活前没有未审计的新消息，同步更新审计指针，避免保活消息本身触发无意义的 50 条历史拉取
                    if prev_stored_msg_id is not None and prev_stored_msg_id == prev_thread_msg_id:
                        self.pinned_last_messages[thread_obj.id] = temp_message.id
                        pinned_msg_changed = True
                else:
                    await thread_obj.edit(locked=True, reason="[保活] 刷新锁定的置顶帖活跃度")
                    log_lines.append("    -> 已通过 Edit 刷新。")

                self.bump_records[thread_obj.id] = {"last_bumped_utc": now_utc}
                bump_changed = True
                await asyncio.sleep(2)
            except Exception as e:
                log_lines.append(f"  [保活失败] 处理 {thread_obj.name} 时发生错误: {e}")

        if bump_changed:
            await self._save_bump_records()
        if pinned_msg_changed:
            await self._save_pinned_last_messages()

        pinned_server_wide_count = len(pinned_threads_set_server_wide)
        log_lines.append(f"全服务器置顶帖子数: **{pinned_server_wide_count}**")

        audit_embed_items: list[tuple[str, str]] = []
        if settings.pinned_mod_enabled and pinned_server_wide_count > 0:
            audit_log, audit_embed_items = await self._audit_pinned_thread_messages(
                guild, settings, all_server_active_threads_list, pinned_threads_set_server_wide
            )
            log_lines.append(audit_log)

        # --- 步骤 3: 候选帖子分类与前置权限过滤 ---
        blacklisted_parent_ids = set(settings.blacklist_channel_ids)
        me = guild.me

        candidate_threads: list[discord.Thread] = []
        locked_count = 0
        blacklisted_thread_count = 0
        no_perm_threads: list[discord.Thread] = []
        no_perm_parent_ids: set[int] = set()

        for t_obj in all_server_active_threads_list:
            if t_obj.id in pinned_threads_set_server_wide:
                continue
            if t_obj.locked:
                locked_count += 1
                continue
            if t_obj.parent_id in blacklisted_parent_ids:
                blacklisted_thread_count += 1
                continue

            # 前置检查管理帖子与读取历史权限，避免无权限频道每轮固定抢占名额并报错
            if me is not None:
                perms = t_obj.permissions_for(me)
                if not (perms.manage_threads and perms.view_channel and perms.read_message_history):
                    no_perm_threads.append(t_obj)
                    if t_obj.parent_id:
                        no_perm_parent_ids.add(t_obj.parent_id)
                    continue

            candidate_threads.append(t_obj)

        log_lines.append(
            f"候选池统计: 总活跃={current_total_server_threads} | 置顶={pinned_server_wide_count} | "
            f"锁定={locked_count} | 黑名单保护={blacklisted_thread_count} | "
            f"无权限跳过={len(no_perm_threads)} | 可归档候选={len(candidate_threads)}"
        )

        # --- 步骤 4: 单次获取所有候选帖最后活跃时间（内存缓存优先） ---
        get_msg_start = time.time()
        activity_infos, fetch_stats, get_msg_error_lines = await self._resolve_threads_last_activity(candidate_threads)
        get_msg_elapsed = time.time() - get_msg_start

        if manual:
            # 手动触发时也即时落盘缓存
            await self._save_thread_activity_cache()

        log_lines.append(
            f"获取候选帖活跃时间耗时: {get_msg_elapsed:.3f}s "
            f"(缓存命中:{fetch_stats['cache_hit']} | API拉取成功:{fetch_stats['api_ok']} | "
            f"空帖兜底:{fetch_stats['empty_fallback']} | 异常跳过:{fetch_stats['skipped_error']})"
        )

        # 按最后活跃时间从最老到最新排序
        activity_infos.sort(key=lambda item: item.last_active_at)

        # --- 步骤 5: 统一规划待归档列表（严格去重） ---
        planned_map: dict[int, ArchivePlanItem] = {}

        # 5.1 阶段一：按不活跃天数筛选
        inactivity_planned_count = 0
        if settings.inactivity_days > 0:
            inactivity_cutoff = now_utc - timedelta(days=settings.inactivity_days)
            for info in activity_infos:
                if info.last_active_at < inactivity_cutoff:
                    planned_map[info.thread.id] = ArchivePlanItem(
                        info=info,
                        reason_tag="inactivity",
                        reason_label=f">{settings.inactivity_days}天未活跃",
                    )
                    inactivity_planned_count += 1

        # 5.2 阶段二：单频道活跃上限筛选（仅在显式开启 enable_channel_limit 且 max_active_posts > 0 时生效）
        channel_cap_planned_count = 0
        if settings.enable_channel_limit and settings.max_active_posts > 0:
            # 统计扣除阶段一归档帖后，各非黑名单父频道剩余的活跃帖（含该频道内置顶/锁定帖）
            channel_active_counts: Counter[int] = Counter()
            for t_obj in all_server_active_threads_list:
                if t_obj.parent_id and t_obj.parent_id not in blacklisted_parent_ids:
                    if t_obj.id not in planned_map:
                        channel_active_counts[t_obj.parent_id] += 1

            # 从老到新遍历尚未归档的候选帖，若所在父频道超限则加入归档计划
            for info in activity_infos:
                tid = info.thread.id
                pid = info.thread.parent_id
                if tid in planned_map or not pid:
                    continue
                if channel_active_counts[pid] > settings.max_active_posts:
                    planned_map[tid] = ArchivePlanItem(
                        info=info,
                        reason_tag="channel_cap",
                        reason_label=f"单频道超限(>{settings.max_active_posts})",
                    )
                    channel_active_counts[pid] -= 1
                    channel_cap_planned_count += 1

        # 5.3 阶段三：全服活跃总数上限控制（挤压归档）
        server_cap_planned_count = 0
        if settings.max_active_threads > 0:
            projected_remaining_total = current_total_server_threads - len(planned_map)
            kill_needed_server_level = projected_remaining_total - settings.max_active_threads
            if kill_needed_server_level > 0:
                for info in activity_infos:
                    if kill_needed_server_level <= 0:
                        break
                    if info.thread.id in planned_map:
                        continue
                    planned_map[info.thread.id] = ArchivePlanItem(
                        info=info,
                        reason_tag="server_cap",
                        reason_label=f"全服超限挤压(>{settings.max_active_threads})",
                    )
                    kill_needed_server_level -= 1
                    server_cap_planned_count += 1

        planned_items = list(planned_map.values())
        # 保持按最后活跃时间从老到新排序执行
        planned_items.sort(key=lambda x: x.info.last_active_at)

        log_lines.append(
            f"归档计划汇总: 共需归档 {len(planned_items)} 个 "
            f"(超时归档={inactivity_planned_count}, 频道超限={channel_cap_planned_count}, 全服挤压={server_cap_planned_count})"
        )

        # --- 步骤 6: 执行归档 ---
        archive_start_time = time.time()
        archived_ok_items, archived_fail_items = await self._archive_thread_batch(planned_items, now_utc)
        archive_elapsed = time.time() - archive_start_time

        succeed_count = len(archived_ok_items)
        fail_count = len(archived_fail_items)
        archived_ok_ids = {item.info.thread.id for item, _ in archived_ok_items}

        # 按原因统计实际成功归档数
        ok_by_reason = Counter(item.reason_tag for item, _ in archived_ok_items)

        if planned_items:
            log_lines.append(f"归档操作耗时: {archive_elapsed:.3f}s (成功:{succeed_count}, 失败:{fail_count})")
        else:
            log_lines.append("当前无需归档任何帖子。")

        # 写入详细文件日志
        total_elapsed = time.time() - global_start_time
        bot_log.info("\n".join(log_lines))

        if archived_ok_items:
            detail_log = "\n".join(log_line for _, log_line in archived_ok_items)
            bot_log.info(f"\n--- 归档成功详情 (索引: {run_hash_value}) ---\n{detail_log}")
        if get_msg_error_lines:
            bot_log.warning(f"\n--- 获取消息异常详情 (索引: {run_hash_value}) ---\n" + "\n".join(get_msg_error_lines))
        if archived_fail_items:
            fail_log = "\n".join(err_line for _, err_line, _ in archived_fail_items)
            bot_log.error(f"\n--- 归档失败详情 (索引: {run_hash_value}) ---\n{fail_log}")

        bot_log.info(
            f"\n--- 运行总结 (索引: {run_hash_value}) ---\n"
            f"清理前活跃: {current_total_server_threads} -> 清理后预计活跃: {current_total_server_threads - succeed_count}\n"
            f"总计成功归档: {succeed_count} (超时:{ok_by_reason['inactivity']}, 频道超限:{ok_by_reason['channel_cap']}, 全服挤压:{ok_by_reason['server_cap']})\n"
            f"总计归档失败: {fail_count} | 总耗时: {total_elapsed:.3f}s"
        )

        # --- 步骤 7: 构建增强型监控室 Embed 报告 ---
        remaining_total = current_total_server_threads - succeed_count
        headroom_before = 1000 - current_total_server_threads
        headroom_after = 1000 - remaining_total

        desc_lines = []
        if current_total_server_threads >= 950:
            desc_lines.append(
                f"⚠️ **高水位预警**：清理前活跃帖达 **{current_total_server_threads}/1000**（仅余 {headroom_before} 缓冲）！"
            )

        inactivity_rule_str = f"开启 (**{settings.inactivity_days}** 天)" if settings.inactivity_days > 0 else "关闭"
        channel_rule_str = (
            f"开启 (**{settings.max_active_posts}** 帖/频道)"
            if (settings.enable_channel_limit and settings.max_active_posts > 0)
            else "未启用"
        )

        desc_lines.extend(
            [
                f"> **归档策略**：不活跃归档 {inactivity_rule_str} ｜ 全服目标 **{settings.max_active_threads}** ｜ 单频道上限 {channel_rule_str}",
                f"> **活跃池水位**：**{current_total_server_threads}** ➔ **{remaining_total}** / 1000（当前余量 **{headroom_after}**）",
                f"> **特殊帖占比**：置顶 **{pinned_server_wide_count}** ｜ 锁定 **{locked_count}** ｜ 黑名单保护 **{blacklisted_thread_count}**"
                + (f" ｜ 无权限跳过 **{len(no_perm_threads)}**" if no_perm_threads else ""),
                f"> **本次归档明细**：成功 **{succeed_count}** / 失败 **{fail_count}**"
                f"（超时归档 **{ok_by_reason['inactivity']}** ｜ 全服挤压 **{ok_by_reason['server_cap']}**"
                + (f" ｜ 频道超限 **{ok_by_reason['channel_cap']}**" if ok_by_reason["channel_cap"] else "")
                + "）",
                f"> **巡检耗时**：**{total_elapsed:.2f}s**（缓存命中 {fetch_stats['cache_hit']} / API拉取 {fetch_stats['api_ok']}）",
            ]
        )
        if manual:
            desc_lines.append("-# (本次由管理员手动触发)")

        if fail_count > 0 or current_total_server_threads >= 980:
            embed_color = Color.orange()
        elif succeed_count > 0:
            embed_color = Color.green()
        else:
            embed_color = Color.blue()

        final_embed = Embed(
            title=f"归档报告: {settings.config_name}",
            description="\n".join(desc_lines),
            color=embed_color,
        )
        final_embed.set_author(name=f"{current_run_timestamp_str} | 索引: {run_hash_value}")

        # 字段 A：清理后剩余活跃池健康度（时间分布 + Top 5 频道占比）
        surviving_infos = [info for info in activity_infos if info.thread.id not in archived_ok_ids]
        pool_summary_lines = []
        if surviving_infos:
            b_24h = b_1_3d = b_3_7d = b_gt7d = 0
            for info in surviving_infos:
                age_days = (now_utc - info.last_active_at).total_seconds() / 86400
                if age_days <= 1:
                    b_24h += 1
                elif age_days <= 3:
                    b_1_3d += 1
                elif age_days <= 7:
                    b_3_7d += 1
                else:
                    b_gt7d += 1
            pool_summary_lines.append(
                f"**可归档活跃帖时间分布**（共 {len(surviving_infos)} 帖）：\n"
                f"`<24h`: **{b_24h}** ｜ `1~3天`: **{b_1_3d}** ｜ `3~7天`: **{b_3_7d}** ｜ `>7天`: **{b_gt7d}**"
            )

        # 统计清理后全服所有剩余活跃帖的父频道占比 Top 5
        surviving_parent_counter: Counter[int] = Counter()
        for t_obj in all_server_active_threads_list:
            if t_obj.id not in archived_ok_ids and t_obj.parent_id:
                surviving_parent_counter[t_obj.parent_id] += 1

        if surviving_parent_counter and remaining_total > 0:
            top_channels_str = []
            for pid, cnt in surviving_parent_counter.most_common(5):
                pct = cnt / remaining_total * 100
                mark = " *(黑名单)*" if pid in blacklisted_parent_ids else ""
                top_channels_str.append(f"<#{pid}>: **{cnt}** ({pct:.1f}%){mark}")
            pool_summary_lines.append("**活跃帖 Top 5 频道**：\n" + " ｜ ".join(top_channels_str))

        if pool_summary_lines:
            safe_add_embed_field(final_embed, "📊 当前活跃池分布（清理后）", "\n".join(pool_summary_lines), inline=False)

        # 字段 B：本次归档时间跨度与首尾样本（避免只看前 10 个老帖造成误判）
        if archived_ok_items:
            oldest_item = archived_ok_items[0][0]
            newest_item = archived_ok_items[-1][0]
            oldest_days = (now_utc - oldest_item.info.last_active_at).total_seconds() / 86400
            newest_days = (now_utc - newest_item.info.last_active_at).total_seconds() / 86400
            oldest_str = oldest_item.info.last_active_at.strftime("%Y-%m-%d %H:%M")
            newest_str = newest_item.info.last_active_at.strftime("%Y-%m-%d %H:%M")

            span_header = (
                f"**最后活跃跨度**：最老 `{oldest_str}` (**{oldest_days:.1f}天前**) "
                f"➔ 最新 `{newest_str}` (**{newest_days:.1f}天前**)\n"
            )

            def _fmt_sample(idx: int, plan_item: ArchivePlanItem) -> str:
                d_ago = (now_utc - plan_item.info.last_active_at).total_seconds() / 86400
                t_str = plan_item.info.last_active_at.strftime("%m-%d %H:%M")
                fb = "(无消息按建帖)" if plan_item.info.is_fallback_created_at else ""
                return f"`#{idx}` {plan_item.info.thread.mention} — `{t_str}` (**{d_ago:.1f}天前**{fb}) [{plan_item.reason_label}]"

            sample_lines = [span_header]
            total_ok = len(archived_ok_items)
            if total_ok <= 8:
                for i, (p_item, _) in enumerate(archived_ok_items, start=1):
                    sample_lines.append(_fmt_sample(i, p_item))
            else:
                sample_lines.append("*▼ 最早未活跃样本（前 4 条）：*")
                for i in range(4):
                    sample_lines.append(_fmt_sample(i + 1, archived_ok_items[i][0]))
                sample_lines.append(f"*... 中间省略 {total_ok - 8} 个归档帖 ...*")
                sample_lines.append("*▲ 临界线附近最新样本（最后 4 条）：*")
                for i in range(total_ok - 4, total_ok):
                    sample_lines.append(_fmt_sample(i + 1, archived_ok_items[i][0]))

            safe_add_embed_field(
                final_embed, f"🗂️ 本次归档跨度与样本（共 {total_ok} 帖）", "\n".join(sample_lines), inline=False
            )

        # 字段 C：无管理权限频道提示（帮助管理快速定位应加黑名单或补权限的频道）
        if no_perm_parent_ids:
            ch_mentions = ", ".join(f"<#{pid}>" for pid in list(no_perm_parent_ids)[:10])
            perm_tip = (
                f"共跳过 **{len(no_perm_threads)}** 个缺少 `管理帖子/查看历史` 权限的活跃子区（涉及频道：{ch_mentions}）。\n"
                f"-# 如为投诉工单或管理内部频道，可加入 `blacklist_channel_ids`；如需自动归档，请赋予 Bot 管理帖子权限。"
            )
            safe_add_embed_field(final_embed, "🔒 权限跳过提示", perm_tip, inline=False)

        # 字段 D：置顶帖消息审计记录
        if audit_embed_items:
            audit_lines = [f"**{k}**\n{v}" for k, v in audit_embed_items[:5]]
            safe_add_embed_field(final_embed, "📌 置顶帖消息审计", "\n".join(audit_lines), inline=False)

        # 发送通知到监控频道
        if settings.notification_thread_id:
            try:
                notif_channel = await self.fetch_channel(settings.notification_thread_id)
                if isinstance(notif_channel, (TextChannel, Thread)):
                    sent_msg = await notif_channel.send(embed=final_embed)
                    settings.last_notice_message_id = sent_msg.id

                    if archived_fail_items:
                        err_desc = "\n".join(embed_err for _, _, embed_err in archived_fail_items[:15])
                        error_embed = Embed(
                            title="错误: 部分帖子归档操作失败",
                            description=err_desc[:4000],
                            color=Color.red(),
                        )
                        await notif_channel.send(embed=error_embed)
            except Exception as e:
                bot_log.error(f"发送归档报告到频道 {settings.notification_thread_id} 失败: {e}")

        # 返回当前仍处于活跃状态的帖子 ID 集合，用于维护缓存体积
        remaining_active_ids = {
            t_obj.id for t_obj in all_server_active_threads_list if t_obj.id not in archived_ok_ids
        }
        return remaining_active_ids

    # ---------------- 获取最后活跃时间（带缓存与并发控制） ----------------
    async def _resolve_threads_last_activity(
        self, thread_list: list[discord.Thread]
    ) -> tuple[list[ThreadActivityInfo], dict[str, int], list[str]]:
        """
        优先使用 (thread.id, thread.last_message_id) 内存缓存；
        仅对新增或 last_message_id 发生变化的帖子并发拉取 history(limit=5)。
        """
        stats = {
            "cache_hit": 0,
            "api_ok": 0,
            "empty_fallback": 0,
            "skipped_error": 0,
        }
        error_lines: list[str] = []
        results: list[ThreadActivityInfo] = []
        to_fetch: list[discord.Thread] = []

        for thread in thread_list:
            cached = self.thread_activity_cache.get(thread.id)
            if cached is not None and cached.get("last_message_id") == thread.last_message_id:
                stats["cache_hit"] += 1
                results.append(
                    ThreadActivityInfo(
                        thread=thread,
                        last_active_at=datetime.fromtimestamp(cached["last_active_ts"], tz=timezone.utc),
                        is_fallback_created_at=cached.get("is_fallback", False),
                        from_cache=True,
                    )
                )
            else:
                to_fetch.append(thread)

        if not to_fetch:
            return results, stats, error_lines

        sem = asyncio.Semaphore(HISTORY_FETCH_CONCURRENCY)

        async def _fetch_one(t_obj: discord.Thread) -> ThreadActivityInfo | None:
            async with sem:
                try:
                    async for msg in t_obj.history(limit=5):
                        if msg is not None:
                            dt = ensure_utc(msg.created_at)
                            self.thread_activity_cache[t_obj.id] = {
                                "last_message_id": t_obj.last_message_id,
                                "last_active_ts": dt.timestamp(),
                                "is_fallback": False,
                            }
                            stats["api_ok"] += 1
                            return ThreadActivityInfo(
                                thread=t_obj, last_active_at=dt, is_fallback_created_at=False, from_cache=False
                            )

                    # 帖子内所有消息均已被删除，回退使用帖子创建时间并写入缓存避免每轮重复请求
                    fallback_dt = ensure_utc(t_obj.created_at or discord.utils.snowflake_time(t_obj.id))
                    self.thread_activity_cache[t_obj.id] = {
                        "last_message_id": t_obj.last_message_id,
                        "last_active_ts": fallback_dt.timestamp(),
                        "is_fallback": True,
                    }
                    stats["empty_fallback"] += 1
                    return ThreadActivityInfo(
                        thread=t_obj, last_active_at=fallback_dt, is_fallback_created_at=True, from_cache=False
                    )

                except discord.NotFound:
                    # 帖子在扫描期间已被删除，直接忽略
                    self.thread_activity_cache.pop(t_obj.id, None)
                    return None
                except discord.Forbidden:
                    stats["skipped_error"] += 1
                    error_lines.append(f"  > 帖子 {t_obj.name} (ID:{t_obj.id}) 无权限读取历史消息，已跳过")
                    return None
                except Exception as e:
                    # 网络波动或未知异常：跳过本轮，绝不伪造建帖时间以防误杀热帖
                    stats["skipped_error"] += 1
                    error_lines.append(f"  > 帖子 {t_obj.name} (ID:{t_obj.id}) 获取消息异常，本轮跳过: {e}")
                    return None

        fetched_list = await asyncio.gather(*(_fetch_one(t) for t in to_fetch))
        for item in fetched_list:
            if item is not None:
                results.append(item)

        return results, stats, error_lines

    # ---------------- 批量执行归档 ----------------
    async def _archive_thread_batch(
        self, plan_items: list[ArchivePlanItem], now_utc: datetime
    ) -> tuple[list[tuple[ArchivePlanItem, str]], list[tuple[ArchivePlanItem, str, str]]]:
        """并发执行帖子归档，返回 (成功列表, 失败列表)"""
        if not plan_items:
            return [], []

        sem = asyncio.Semaphore(ARCHIVE_EDIT_CONCURRENCY)
        ok_results: list[tuple[ArchivePlanItem, str]] = []
        fail_results: list[tuple[ArchivePlanItem, str, str]] = []

        async def _do_one(idx: int, item: ArchivePlanItem):
            thread = item.info.thread
            last_str = item.info.last_active_at.strftime("%Y-%m-%d %H:%M")
            hours_ago = (now_utc - item.info.last_active_at).total_seconds() / 3600

            async with sem:
                try:
                    if not thread.archived:
                        await thread.edit(archived=True, reason=f"自动归档: {item.reason_label}")
                    log_line = (
                        f"  - [{idx}] {thread.name} | ID:{thread.id} | "
                        f"原因:{item.reason_label} | 最后活跃:{last_str} ({hours_ago:.1f}h前)"
                    )
                    ok_results.append((item, log_line))
                except discord.NotFound:
                    # 归档前帖子已被作者或版务删除，视为已清理，不计入失败报错
                    self.thread_activity_cache.pop(thread.id, None)
                except Exception as e:
                    log_err = (
                        f"  - [失败] {thread.name} (ID:{thread.id}) | "
                        f"原因:{item.reason_label} | 最后活跃:{last_str} | 错误:{e}"
                    )
                    embed_err = f"• {thread.mention} (`{thread.id}`) — `{str(e)[:120]}`"
                    fail_results.append((item, log_err, embed_err))
                    bot_log.error(f"归档帖子 {thread.name} (ID:{thread.id}) 失败: {e}")

        await asyncio.gather(*(_do_one(idx, item) for idx, item in enumerate(plan_items, start=1)))
        # 恢复按活跃时间从老到新顺序（并发完成顺序可能微调）
        ok_results.sort(key=lambda pair: pair[0].info.last_active_at)
        return ok_results, fail_results

    # ---------------- 置顶帖消息审计 ----------------
    async def _audit_pinned_thread_messages(
        self,
        guild: Guild,
        settings: GuildArchiveSettings,
        active_threads: list[discord.Thread],
        pinned_thread_ids: set[int],
    ) -> tuple[str, list[tuple[str, str]]]:
        """审计置顶帖中的漏监听消息（带单轮成员查询缓存）"""
        log_lines = ["置顶帖消息审计:"]
        deleted_count = 0
        checked_count = 0
        audit_details: list[tuple[str, str]] = []
        member_cache: dict[int, discord.Member | None] = {}
        records_dirty = False

        pinned_threads = [
            t for t in active_threads if t.id in pinned_thread_ids and not t.locked
        ]
        log_lines.append(f"  找到 {len(pinned_threads)} 个非锁定置顶帖进行审计")

        now_utc = datetime.now(timezone.utc)
        three_days_ago = now_utc - timedelta(days=3)

        for thread in pinned_threads:
            try:
                checked_count += 1
                current_last_message_id = thread.last_message_id
                stored_last_message_id = self.pinned_last_messages.get(thread.id)

                if stored_last_message_id is not None and current_last_message_id == stored_last_message_id:
                    continue

                log_lines.append(f"  [新消息检测] {thread.name} (ID: {thread.id})")
                audit_key = f"[审计] {thread.name}"
                audit_desc_lines = [
                    "> 首次检测，拉取最新消息审计" if stored_last_message_id is None else "> 检测到新消息，拉取审计"
                ]

                recent_messages: list[discord.Message] = []
                try:
                    async for message in thread.history(limit=50):
                        recent_messages.append(message)
                except discord.Forbidden:
                    log_lines.append("    -> 无权限访问历史记录")
                    audit_desc_lines.append("> 无权限访问历史记录")
                    audit_details.append((audit_key, "\n".join(audit_desc_lines)))
                    continue
                except Exception as e:
                    log_lines.append(f"    -> 获取历史记录失败: {e}")
                    audit_desc_lines.append(f"> 获取历史记录失败: {e}")
                    audit_details.append((audit_key, "\n".join(audit_desc_lines)))
                    continue

                deleted_this_thread = 0
                for message in recent_messages:
                    if message.author.bot or ensure_utc(message.created_at) < three_days_ago:
                        continue

                    author = message.author
                    if self._is_user_exempt(author, thread, settings):
                        continue

                    if not isinstance(author, discord.Member):
                        if author.id not in member_cache:
                            try:
                                member_cache[author.id] = await guild.fetch_member(author.id)
                            except discord.NotFound:
                                member_cache[author.id] = None
                            except discord.HTTPException as e:
                                bot_log.warning(f"无法确认用户 {author.id} 成员权限，跳过消息 {message.id}: {e}")
                                continue
                        fetched_member = member_cache[author.id]
                        if fetched_member is not None and self._is_user_exempt(fetched_member, thread, settings):
                            continue

                    try:
                        await message.delete()
                        deleted_count += 1
                        deleted_this_thread += 1
                        log_lines.append(f"    [已删除] 用户 {author.name} 的消息 (ID: {message.id})")
                    except discord.Forbidden:
                        log_lines.append(f"    [删除失败] 无权限删除消息 (ID: {message.id})")
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        log_lines.append(f"    [删除失败] 消息 {message.id}: {e}")

                if deleted_this_thread > 0:
                    audit_desc_lines.append(f"> 已清理 {deleted_this_thread} 条非豁免消息")
                    audit_details.append((audit_key, "\n".join(audit_desc_lines)))

                if current_last_message_id:
                    self.pinned_last_messages[thread.id] = current_last_message_id
                    records_dirty = True

            except Exception as e:
                log_lines.append(f"  [审计失败] 处理 {thread.name} 时发生错误: {e}")

        if records_dirty:
            await self._save_pinned_last_messages()

        log_lines.append(f"  审计完成: 检查了 {checked_count} 个置顶帖，删除了 {deleted_count} 条消息")
        return "\n".join(log_lines), audit_details


# --- 命令管理 ---
class ArchiveManagerCog(commands.Cog):
    def __init__(self, bot: ThreadArchiverBot):
        self.bot = bot

    async def _config_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        choices = []
        for setting in self.bot.guild_settings_map.values():
            if current.lower() in setting.config_name.lower():
                choices.append(app_commands.Choice(name=setting.config_name, value=setting.config_name))
        return choices[:25]

    @app_commands.command(name="set-archive-rules", description="修改服务器归档规则（仅需填写要更改的参数，未填项保持不变）。")
    @app_commands.describe(
        config_name="在 GUILD_CONFIGS_JSON 中定义的服务器配置名",
        inactivity_days="多少天无发言自动归档（0 表示关闭按天归档）",
        max_active_threads="整个服务器允许的最大活跃帖子数目标（建议设为 750~850 留出缓冲）",
        max_active_posts="单频道最大活跃帖子数（需配合 enable_channel_limit 开启）",
        enable_channel_limit="是否启用单频道最大活跃帖子数限制",
    )
    @app_commands.autocomplete(config_name=_config_name_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_archive_rules_cmd(
        self,
        interaction: discord.Interaction,
        config_name: str,
        inactivity_days: int | None = None,
        max_active_threads: int | None = None,
        max_active_posts: int | None = None,
        enable_channel_limit: bool | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        target_setting: GuildArchiveSettings | None = None
        guild_id_to_update: int | None = None

        for gid, setting_obj in self.bot.guild_settings_map.items():
            if setting_obj.config_name == config_name:
                target_setting = setting_obj
                guild_id_to_update = gid
                break

        if not target_setting or not guild_id_to_update:
            await interaction.followup.send(f"错误：未找到名为 `{config_name}` 的服务器配置。", ephemeral=True)
            return

        if inactivity_days is not None and inactivity_days >= 0:
            target_setting.inactivity_days = inactivity_days
        if max_active_threads is not None and max_active_threads >= 0:
            target_setting.max_active_threads = max_active_threads
        if max_active_posts is not None and max_active_posts >= 0:
            target_setting.max_active_posts = max_active_posts
        if enable_channel_limit is not None:
            target_setting.enable_channel_limit = enable_channel_limit

        await self.bot.save_guild_setting(guild_id_to_update)

        channel_limit_desc = (
            f"已启用 (**{target_setting.max_active_posts}** 帖/频道)"
            if (target_setting.enable_channel_limit and target_setting.max_active_posts > 0)
            else f"未启用 (预留值: {target_setting.max_active_posts})"
        )

        embed = Embed(title="✅ 归档规则已更新", color=Color.green())
        embed.description = (
            f"服务器配置 **{config_name}** 当前生效规则：\n"
            f"- 不活跃归档天数: **{f'{target_setting.inactivity_days} 天' if target_setting.inactivity_days > 0 else '未启用'}**\n"
            f"- 全服最大活跃帖目标: **{target_setting.max_active_threads if target_setting.max_active_threads > 0 else '未启用'}**\n"
            f"- 单频道活跃帖上限: **{channel_limit_desc}**"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        bot_log.info(f"用户 {interaction.user} 更新了 '{config_name}' 的归档规则: {target_setting.to_dict()}")

    @app_commands.command(name="manual-guild-archive", description="手动触发一次归档审计（不活跃归档 + 容量控制）。")
    @app_commands.describe(config_name="在 GUILD_CONFIGS_JSON 中配置的服务器别名")
    @app_commands.autocomplete(config_name=_config_name_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def manual_guild_archive_cmd(self, interaction: discord.Interaction, config_name: str):
        await interaction.response.defer(ephemeral=True, thinking=True)

        target_setting: GuildArchiveSettings | None = None
        guild_id_to_process: int | None = None

        for gid, setting_obj in self.bot.guild_settings_map.items():
            if setting_obj.config_name == config_name:
                target_setting = setting_obj
                guild_id_to_process = gid
                break

        if not target_setting or not guild_id_to_process:
            await interaction.followup.send(f"错误：未找到名为 `{config_name}` 的服务器配置。", ephemeral=True)
            return

        guild = self.bot.get_guild(guild_id_to_process)
        if not guild:
            await interaction.followup.send(f"错误：机器人当前无法访问服务器 ID `{guild_id_to_process}`。", ephemeral=True)
            return

        if self.bot.operation_lock.locked():
            await interaction.followup.send("当前有其他归档操作正在进行中，请稍后再试。", ephemeral=True)
            return

        async with self.bot.operation_lock:
            bot_log.info(f"用户 {interaction.user} 手动触发对 '{config_name}' 的归档。")
            await interaction.followup.send(
                f"正在开始对服务器配置 `{config_name}` (服务器: {guild.name}) 进行手动归档检查...", ephemeral=True
            )
            await self.bot.process_guild_threads(guild, target_setting, manual=True)
            await interaction.edit_original_response(
                content=f"✅ 对服务器配置 `{config_name}` 的手动归档检查已完成，详情请查看监控通知频道。"
            )

    @app_commands.command(name="view-guild-config", description="查看已加载的服务器归档配置。")
    @app_commands.describe(config_name="在 GUILD_CONFIGS_JSON 中定义的服务器配置名（留空查看所有）")
    @app_commands.autocomplete(config_name=_config_name_autocomplete)
    @app_commands.guild_only()
    async def view_guild_config_cmd(self, interaction: discord.Interaction, config_name: str | None = None):
        await interaction.response.defer(ephemeral=True)
        embeds_to_send = []

        if config_name:
            found_setting = next(
                (s for s in self.bot.guild_settings_map.values() if s.config_name == config_name), None
            )
            if not found_setting:
                await interaction.followup.send(f"未找到名为 `{config_name}` 的服务器配置。", ephemeral=True)
                return
            settings_list = [found_setting]
        else:
            settings_list = list(self.bot.guild_settings_map.values())
            if not settings_list:
                await interaction.followup.send("当前没有已加载的服务器配置。", ephemeral=True)
                return

        for setting in settings_list:
            embed = Embed(title=f"配置: {setting.config_name}", color=Color.blue())
            embed.add_field(name="服务器 ID", value=str(setting.guild_id), inline=True)
            embed.add_field(name="巡检间隔", value=f"{AUDIT_INTERVAL_MINUTES} 分钟", inline=True)
            embed.add_field(
                name="黑名单频道 ID",
                value=", ".join(f"`{cid}`" for cid in setting.blacklist_channel_ids) or "未设置",
                inline=False,
            )

            inactivity_days_str = f"{setting.inactivity_days} 天" if setting.inactivity_days > 0 else "未启用"
            max_active_threads_str = str(setting.max_active_threads) if setting.max_active_threads > 0 else "未启用"
            channel_limit_str = (
                f"已启用 ({setting.max_active_posts} 帖)"
                if (setting.enable_channel_limit and setting.max_active_posts > 0)
                else "未启用"
            )

            embed.add_field(name="不活跃归档天数", value=inactivity_days_str, inline=True)
            embed.add_field(name="服务器活跃帖目标", value=max_active_threads_str, inline=True)
            embed.add_field(name="单频道活跃帖上限", value=channel_limit_str, inline=True)
            embed.add_field(
                name="通知频道/帖子 ID",
                value=f"<#{setting.notification_thread_id}> (`{setting.notification_thread_id}`)"
                if setting.notification_thread_id
                else "未设置",
                inline=False,
            )

            if setting.last_notice_message_id:
                embed.add_field(name="上次通知消息 ID", value=str(setting.last_notice_message_id), inline=False)
            embeds_to_send.append(embed)

        for i in range(0, len(embeds_to_send), 10):
            await interaction.followup.send(embeds=embeds_to_send[i : i + 10], ephemeral=True)


# --- 主程序入口 ---
def main_bot_runner():
    bot = ThreadArchiverBot()
    bot.bot_token = os.environ.get("BOT_TOKEN")

    if not bot.bot_token:
        bot_log.critical("未能从环境变量 BOT_TOKEN 读取机器人令牌。请在 .env 中配置 BOT_TOKEN。")
        return

    try:
        bot.run(bot.bot_token)
    except discord.LoginFailure:
        bot_log.critical("Discord 登录失败：无效的 Token。")
    except Exception as e:
        bot_log.critical(f"机器人运行时发生致命错误: {e}", exc_info=True)
    finally:
        bot_log.info("机器人已关闭。")


if __name__ == "__main__":
    main_bot_runner()
