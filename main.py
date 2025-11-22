import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
import hashlib

import discord
from discord.ext import commands, tasks
from discord import app_commands, Intents, Guild, TextChannel, Thread, Message, Embed, Color, ForumChannel, CategoryChannel 

# --- 全局配置与常量 ---
CONFIG_FILENAME = "bot_config.json"
LOG_DIRECTORY = Path("logs")
LOG_FILENAME = LOG_DIRECTORY / "archiver_bot.log"
DATA_DIRECTORY = Path("data")

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

# --- 日志配置 ---
def setup_logging():
    """配置日志记录器"""
    LOG_DIRECTORY.mkdir(exist_ok=True)
    logger = logging.getLogger('discord')
    logger.setLevel(logging.INFO)

    bot_logger = logging.getLogger('archiver_bot')
    bot_logger.setLevel(logging.DEBUG)

    handler = logging.FileHandler(filename=LOG_FILENAME, encoding='utf-8', mode='a')
    formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    bot_logger.addHandler(handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    bot_logger.addHandler(stream_handler)
    logger.addHandler(stream_handler)

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
    ):
        self.guild_id = guild_id
        self.config_name = config_name
        # 黑名单频道：这些频道中的帖子计入活跃总数，但不会被自动归档
        self.blacklist_channel_ids = blacklist_channel_ids or []
        self.archive_category_id = archive_category_id
        self.inactivity_days = inactivity_days
        self.notification_thread_id = notification_thread_id
        self.max_active_posts = max_active_posts
        self.max_active_threads = max_active_threads
        self.last_notice_message_id = last_notice_message_id

        mod_settings = pinned_thread_moderation or {}
        self.pinned_mod_enabled = mod_settings.get("enabled", False)
        self.allowed_role_ids = [int(role_id) for role_id in mod_settings.get("allowed_role_ids", [])]
        self.allowed_user_ids = [int(user_id) for user_id in mod_settings.get("allowed_user_ids", [])]

    def to_dict(self) -> dict:
        """将设置转换为字典以便保存到JSON"""
        return {
            "guild_id": self.guild_id,
            "blacklist_channel_ids": self.blacklist_channel_ids,
            "archive_category_id": self.archive_category_id,
            "inactivity_days": self.inactivity_days,
            "notification_thread_id": self.notification_thread_id,
            "max_active_posts": self.max_active_posts,
            "max_active_threads": self.max_active_threads,
            "last_notice_message_id": self.last_notice_message_id,
            "pinned_thread_moderation": {
                "enabled": self.pinned_mod_enabled,
                "allowed_role_ids": [str(role_id) for role_id in self.allowed_role_ids],
                "allowed_user_ids": [str(user_id) for user_id in self.allowed_user_ids],
            },
        }

    @classmethod
    def from_dict(cls, guild_id: int, config_name: str, data: dict) -> 'GuildArchiveSettings':
        """从字典创建 GuildArchiveSettings 实例"""
        return cls(
            guild_id=guild_id,
            config_name=config_name,
            blacklist_channel_ids=data.get("blacklist_channel_ids", []),
            archive_category_id=data.get("archive_category_id"),
            inactivity_days=data.get("inactivity_days", 0),
            notification_thread_id=data.get("notification_thread_id"),
            max_active_posts=data.get("max_active_posts", 100),
            max_active_threads=data.get("max_active_threads", 900),
            last_notice_message_id=data.get("last_notice_message_id"),
            pinned_thread_moderation=data.get("pinned_thread_moderation"),
        )

class ErrorMessage: 
    """
    自定义对象: 模拟 discord.Message 对象, 仅包含 created_at 属性
    """
    def __init__(self, created_at: datetime): 
        self.created_at = created_at 

class ThreadMessage: 
    """
    自定义对象: 储存帖子对象 thread 和最后一条消息对象 last_message
    """
    def __init__(self, thread: discord.Thread, last_message: discord.Message | ErrorMessage):
        self.thread = thread
        self.last_message = last_message

# --- 机器人核心类 ---
class ThreadArchiverBot(commands.Bot):
    def __init__(self):
        intents = Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(command_prefix=commands.when_mentioned_or("!archiver "), intents=intents)

        self.global_config = {}
        self.guild_settings_map: dict[int, GuildArchiveSettings] = {}
        self.bot_token = None
        self.operation_lock = asyncio.Lock()

        DATA_DIRECTORY.mkdir(exist_ok=True)
        
        self.BUMP_RECORDS_FILE = DATA_DIRECTORY / "pinned_thread_bump_records.json"
        self.bump_records: dict[int, dict] = {}
        self.bump_records_lock = asyncio.Lock()

        self.PINNED_MESSAGES_FILE = DATA_DIRECTORY / "pinned_thread_last_messages.json"
        self.pinned_last_messages: dict[int, int] = {}
        self.pinned_messages_lock = asyncio.Lock()

        self.succeed_count = 0
        self.fail_count = 0
        self.message_succeed_count = 0
        self.not_found_error_count = 0
        self.log_get_message_error_details = ""
        self.log_archived_info_details = ""
        self.log_archived_error_details = ""
        self.archive_run_details_for_embed = {}

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
            # 约定：GUILD_CONFIGS_JSON 是一个 JSON 对象，键为配置名，值为服务器配置
            guild_configurations = json.loads(raw_json)
            if not isinstance(guild_configurations, dict):
                raise ValueError("GUILD_CONFIGS_JSON 必须是一个 JSON 对象，其键为服务器配置名。")

            # 为兼容原有结构，仍然保留 self.global_config["guild_configurations"]
            self.global_config = {"guild_configurations": guild_configurations}

            for config_name, settings_data in guild_configurations.items():
                guild_id = settings_data.get("guild_id")

                if not guild_id:
                    bot_log.warning(f"配置项 '{config_name}' 缺少 'guild_id'，已跳过。")
                    continue

                notice_id_file = DATA_DIRECTORY / f"{config_name}_notice_id.txt"
                last_notice_id = None

                if notice_id_file.exists():
                    try:
                        last_notice_id = int(notice_id_file.read_text().strip())
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
                notice_id_file.write_text(str(setting.last_notice_message_id))
                bot_log.info(f"已更新 {setting.config_name} 的通知消息ID到 {notice_id_file}")
            except Exception as e:
                bot_log.error(f"写入 {notice_id_file} 失败: {e}", exc_info=True)

    def _save_guild_configs_to_env(self):
        """将当前内存中的服务器配置写回到 .env 的 GUILD_CONFIGS_JSON 变量"""
        # 先从 GuildArchiveSettings 对象重建配置字典，确保与运行时状态一致
        guild_configurations: dict[str, dict] = {}
        for setting in self.guild_settings_map.values():
            guild_configurations[setting.config_name] = setting.to_dict()

        self.global_config = {"guild_configurations": guild_configurations}

        new_json_str = json.dumps(guild_configurations, ensure_ascii=False)

        env_path = Path(".env")
        existing_content = ""
        if env_path.exists():
            existing_content = env_path.read_text(encoding="utf-8")

        lines = existing_content.splitlines() if existing_content else []
        new_line = f"GUILD_CONFIGS_JSON={new_json_str}"

        updated = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("GUILD_CONFIGS_JSON="):
                lines[idx] = new_line
                updated = True
                break

        if not updated:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(new_line)

        if lines:
            new_content = "\n".join(lines) + "\n"
        else:
            new_content = new_line + "\n"

        env_path.write_text(new_content, encoding="utf-8")
    
    
    def _load_bump_records(self):
        """同步加载刷新记录文件到内存"""
        try:
            with open(self.BUMP_RECORDS_FILE, 'r', encoding='utf-8') as f:
                records_from_file = json.load(f)
                # 将ISO格式的时间字符串转换回datetime对象
                for thread_id, data in records_from_file.items():
                    self.bump_records[int(thread_id)] = {
                        "last_bumped_utc": datetime.fromisoformat(data["last_bumped_utc"])
                    }
                bot_log.info(f"成功从 {self.BUMP_RECORDS_FILE} 加载了 {len(self.bump_records)} 条置顶帖刷新记录。")
        except FileNotFoundError:
            bot_log.info(f"刷新记录文件 {self.BUMP_RECORDS_FILE} 未找到，将创建一个新的。")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            bot_log.error(f"加载刷新记录文件失败，文件可能已损坏: {e}", exc_info=True)

    def _load_pinned_last_messages(self):
        """同步加载置顶帖最后消息ID记录"""
        try:
            with open(self.PINNED_MESSAGES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.pinned_last_messages = {int(k): int(v) for k, v in data.items()}
                bot_log.info(f"成功加载了 {len(self.pinned_last_messages)} 条置顶帖消息记录。")
        except FileNotFoundError:
            bot_log.info(f"置顶帖消息记录文件未找到，将创建新的。")
        except Exception as e:
            bot_log.error(f"加载置顶帖消息记录失败: {e}", exc_info=True)

    async def _save_pinned_last_messages(self):
        """保存置顶帖最后消息ID记录到文件"""
        async with self.pinned_messages_lock:
            try:
                data = {str(k): v for k, v in self.pinned_last_messages.items()}
                with open(self.PINNED_MESSAGES_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                bot_log.error(f"保存置顶帖消息记录失败: {e}", exc_info=True)

    async def _save_bump_records(self):
        """将内存中的刷新记录保存到文件"""
        async with self.bump_records_lock:
            try:
                # 准备要写入的数据，将datetime对象转换为ISO格式的字符串
                records_to_save = {}
                for thread_id, data in self.bump_records.items():
                    records_to_save[str(thread_id)] = {
                        "last_bumped_utc": data["last_bumped_utc"].isoformat()
                    }
                
                with open(self.BUMP_RECORDS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(records_to_save, f, indent=4)
            except Exception as e:
                bot_log.error(f"保存刷新记录到 {self.BUMP_RECORDS_FILE} 失败: {e}", exc_info=True)
    
    
    async def setup_hook(self):
        """Bot启动时的异步设置"""
        self._load_bump_records()
        self._load_pinned_last_messages()
        await self.load_configuration()
        await self.add_cog(ArchiveManagerCog(self))
    
        try:
            synced = await self.tree.sync()
            bot_log.info(f"已同步 {len(synced)} 个应用命令。")
        except Exception as e:
            bot_log.error(f"同步应用命令失败: {e}", exc_info=True)

        self.periodic_thread_audit.start()

    def _is_user_exempt(self, author: discord.Member | discord.User, thread: discord.Thread, settings: GuildArchiveSettings) -> bool:
        """
        检查用户是否在置顶帖中拥有消息豁免权。
        返回 True 表示用户豁免，不应删除其消息。
        """

        # 帖主 豁免
        if thread.owner_id and author.id == thread.owner_id:
            return True

        # 只有 discord.Member 对象才有权限和角色信息，需要进行类型检查
        if isinstance(author, discord.Member):
            # 服务器管理员 (Administrator) 豁免
            if author.guild_permissions.administrator:
                return True
            
            # 在配置中的豁免角色组ID (allowed_role_ids)
            author_role_ids = {role.id for role in author.roles}
            if not author_role_ids.isdisjoint(settings.allowed_role_ids):
                return True

        # 在配置中的豁免用户ID (allowed_user_ids)
        if author.id in settings.allowed_user_ids:
            return True
        
        # 如果以上规则都不满足，则用户不豁免
        return False

    async def on_ready(self):
        if not self.user:
            bot_log.error("机器人未能正确初始化 self.user。")
            return
            
        bot_log.info(f"机器人 '{self.user.name}' (ID: {self.user.id}) 已成功登录并准备就绪！")
        bot_log.info(f"当前管理 {len(self.guild_settings_map)} 个服务器配置。")

        main_admin_channel_id_str = os.environ.get("MAIN_ADMIN_CHANNEL_ID")

        if main_admin_channel_id_str and main_admin_channel_id_str.isdigit():
            main_admin_channel_id = int(main_admin_channel_id_str)
            try:
                channel = await self.fetch_channel(main_admin_channel_id)

                if isinstance(channel, (TextChannel, Thread)):
                    await channel.send(f"✅ **{self.user.name} 已启动** ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")

            except discord.NotFound:
                bot_log.warning(f"配置的 main_admin_channel_id: {main_admin_channel_id} 未找到。")

            except discord.Forbidden:
                bot_log.warning(f"无权向 main_admin_channel_id: {main_admin_channel_id} 发送消息。")

            except Exception as e:
                bot_log.error(f"发送启动通知时出错: {e}", exc_info=True)

    async def on_message(self, message: discord.Message):
        # 忽略机器人自身的消息和私信
        if message.author.bot or not message.guild:
            return

        # 检查消息是否在帖子(Thread)中
        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        
        # 检查帖子是否被置顶
        if not thread.flags.pinned:
            return

        # 获取当前服务器的配置
        settings = self.guild_settings_map.get(message.guild.id)
        if not settings or not settings.pinned_mod_enabled:
            return # 如果没有配置或功能未启用，则不做任何事

        # 检查用户是否在豁免名单中
        author = message.author
        
        if self._is_user_exempt(author, thread, settings):
            return
        
        # 如果用户不在豁免名单，则删除消息
        try:
            await message.delete()
            bot_log.debug(f"已删除用户 {author.name} ({author.id}) 在置顶帖 '{thread.name}' 中的消息。")
        except discord.Forbidden:
            parent_name = thread.parent.name if thread.parent else "未知频道"
            bot_log.warning(f"无法删除消息：缺少在频道 '{parent_name}' 中 '管理消息' 的权限。")
        except discord.NotFound:
            # 消息可能已经被用户自己删除了
            pass
        except Exception as e:
            bot_log.error(f"删除消息时发生未知错误: {e}", exc_info=True)

    @tasks.loop(minutes=15)
    async def periodic_thread_audit(self):
        """定时检查并处理不活跃的帖子"""
        if not self.guild_settings_map:
            return

        async with self.operation_lock:
            bot_log.info("开始执行周期性帖子审计...")
            for guild_id, settings in self.guild_settings_map.items():
                guild = self.get_guild(guild_id)

                if not guild:
                    bot_log.warning(f"审计：找不到服务器 {guild_id}，跳过。")
                    continue

                bot_log.info(f"正在审计服务器: '{guild.name}' (ID: {guild_id})")
                await self.process_guild_threads(guild, settings, manual=False)
                await asyncio.sleep(10) 
            bot_log.info("周期性帖子审计完成。")

    @periodic_thread_audit.before_loop
    async def before_periodic_audit(self):
        await self.wait_until_ready()
        bot_log.info("定时审计任务已准备就绪。")

    async def process_guild_threads(self, guild: Guild, settings: GuildArchiveSettings, manual: bool = False):

        now_utc_timestamp = time.time()
        current_run_timestamp_str = datetime.fromtimestamp(now_utc_timestamp, tz=timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S UTC+8')
        hash_input = str(now_utc_timestamp)
        run_hash_value = hashlib.md5(hash_input.encode()).hexdigest()[:8]

        self.succeed_count = 0
        self.fail_count = 0
        self.message_succeed_count = 0
        self.not_found_error_count = 0
        self.log_get_message_error_details = ""
        self.log_archived_info_details = ""
        self.log_archived_error_details = ""
        self.archive_run_details_for_embed = {}

        initial_log_info = ""
        overall_summary_embed_description = ""
        global_start_time = time.time()
        overall_summary_embed_description += f"> 不活跃 **{settings.inactivity_days}** 天归档：**{'开启' if settings.inactivity_days > 0 else '关闭'}**\n"
        initial_log_info += f"> 不活跃 **{settings.inactivity_days}** 天归档: {'开启' if settings.inactivity_days > 0 else '关闭'}\n"

        blacklist_count = len(settings.blacklist_channel_ids)
        if blacklist_count > 0:
            overall_summary_embed_description += f"> 黑名单频道数: **{blacklist_count}**（这些频道中的帖子不会被自动归档）\n"
            initial_log_info += f"已配置 {blacklist_count} 个黑名单频道，这些频道中的帖子不会被自动归档。\n"
        else:
            overall_summary_embed_description += f"> 未配置黑名单频道，默认对所有频道执行归档策略\n"
            initial_log_info += "未配置黑名单频道，将对所有频道执行归档策略。\n"

        overall_summary_embed_description += "\n"

        if manual:
            initial_log_info += f"\n<== 手动开始清理 (服务器级优先) ==> 服务器: {settings.config_name} ({guild.name}) | 日志索引: {run_hash_value}"

        else:
            initial_log_info += f"\n<== 自动开始清理 (服务器级优先) ==> 服务器: {settings.config_name} ({guild.name}) | 日志索引: {run_hash_value}"

        initial_log_info += f"\n服务器级活跃帖目标: {settings.max_active_threads}"
        overall_summary_embed_description += f"> 服务器活跃帖目标: **{settings.max_active_threads}**\n"

        # --- 步骤 1: 获取服务器所有活跃帖子 ---
        all_server_active_threads_list = []
        try:
            all_server_active_threads_list = await guild.active_threads()
        except discord.Forbidden:
            bot_log.error(f"无权获取服务器 {guild.name} 的活跃帖子。", exc_info=True)

        except Exception as e:
            bot_log.error(f"获取或筛选服务器 {guild.name} 活跃帖子失败: {e}", exc_info=True)
            initial_log_info += f"\n错误：获取或筛选服务器活跃帖子失败: {e}"
            return

        current_total_server_threads = len(all_server_active_threads_list)
        overall_summary_embed_description += f"> 当前服务器总活跃帖: **{current_total_server_threads}**\n"

        # --- 步骤 2: 置顶帖处理 (全服务器范围) ---
        pinned_threads_set_server_wide = set()
        initial_log_info += f"\n置顶帖处理 (全服务器):"
        now_utc = datetime.now(timezone.utc)
        # 48 小时前的刷新操作已经可能失效
        self_trust_duration = timedelta(hours=48)

        for thread_obj in all_server_active_threads_list:
            if not thread_obj.flags.pinned:
                continue # 只处理置顶帖

            pinned_threads_set_server_wide.add(thread_obj.id)
            
            try:
                # 如果帖子意外被归档，立即激活
                if thread_obj.archived:
                    await thread_obj.edit(archived=False, reason="[保活] 发现已归档的置顶帖，进行激活")
                    initial_log_info += f"\n  [已取消置顶帖归档] {thread_obj.name} (ID: {thread_obj.id})"
                    continue

                # 检查内存中的持久化记录
                record = self.bump_records.get(thread_obj.id)
                if record and (now_utc - record["last_bumped_utc"] < self_trust_duration):
                    # 如果我们在48小时内刷新过它，就跳过它，不进行刷新
                    continue

                # 执行保活操作
                reason_for_bump = "记录不存在" if not record else "记录已过期"
                initial_log_info += f"\n  [需要保活] {thread_obj.name} (原因: {reason_for_bump})。"
                
                action_taken = False
                if not thread_obj.locked:
                    temp_message = await thread_obj.send(f"置顶帖保活，稍后删除")
                    await temp_message.delete()
                    action_taken = True
                    initial_log_info += f" -> 已通过消息刷新。"
                else:
                    await thread_obj.edit(locked=True, reason="[保活] 刷新锁定的置顶帖活跃度")
                    action_taken = True
                    initial_log_info += f" -> 已通过Edit刷新。"
                
                # 更新内存记录并异步保存到文件
                if action_taken:
                    self.bump_records[thread_obj.id] = { "last_bumped_utc": now_utc }
                    await self._save_bump_records()
                    initial_log_info += f" 等待4秒..."
                    await asyncio.sleep(4) # 为防止API速率限制，在每次成功刷新后等待

            except Exception as e:
                initial_log_info += f"\n  [保活失败] 处理 {thread_obj.name} 时发生未知错误: {e}"

        pinned_server_wide_count = len(pinned_threads_set_server_wide)
        initial_log_info += f"\n全服务器置顶帖子数: **{pinned_server_wide_count}**"
        overall_summary_embed_description += f"> 全服置顶帖: {pinned_server_wide_count}\n"
        
        # --- 置顶帖消息审计 ---
        if settings.pinned_mod_enabled and pinned_server_wide_count > 0:
            audit_log = await self._audit_pinned_thread_messages(guild, settings, pinned_threads_set_server_wide)
            initial_log_info += audit_log
        
        # --- 步骤 3: 服务器级数量控制 ---
        kill_count_server_level = current_total_server_threads - settings.max_active_threads
        initial_log_info += f"\n计算服务器级归档数: (总活跃 {current_total_server_threads}) - (目标 {settings.max_active_threads}) = {kill_count_server_level}"

        threads_archived_this_run = 0

        if kill_count_server_level > 0:
            overall_summary_embed_description += f"> 服务器需归档数量: **{kill_count_server_level}**\n"
            initial_log_info += f"\n服务器级需归档数量: {kill_count_server_level}。开始筛选候选帖子..."

            candidate_threads_for_server_kill = []

            # 黑名单频道内的帖子计入活跃总数，但不会作为归档候选
            blacklisted_parent_ids_set = set(settings.blacklist_channel_ids)

            for thread_obj in all_server_active_threads_list:
                if (
                    thread_obj.id not in pinned_threads_set_server_wide
                    and not thread_obj.locked
                    and thread_obj.parent_id not in blacklisted_parent_ids_set
                ):
                    candidate_threads_for_server_kill.append(thread_obj)

            initial_log_info += f"\n  可用于服务器级归档的候选帖子数(排除置顶/锁定/黑名单频道): {len(candidate_threads_for_server_kill)}"

            if candidate_threads_for_server_kill:
                get_msg_start = time.time()
                thread_message_obj_list_server_level = await self._get_last_message_task(candidate_threads_for_server_kill)
                get_msg_time = time.time() - get_msg_start
                initial_log_info += f"\n  获取候选帖子 最后一条消息 耗时: {get_msg_time:.3f}s (S:{self.message_succeed_count}/F:{self.not_found_error_count})"

                thread_message_obj_list_server_level.sort(key=lambda tm_obj: tm_obj.last_message.created_at)

                threads_to_actually_archive_server_level = thread_message_obj_list_server_level[:kill_count_server_level]
                initial_log_info += f"\n  将实际归档 (服务器级): {len(threads_to_actually_archive_server_level)} 个帖子"

                # 执行归档 (服务器级)
                archive_task_start_time_sl = time.time()

                if threads_to_actually_archive_server_level:
                    initial_succeed_count = self.succeed_count
                    initial_fail_count = self.fail_count
                    await self._archive_thread_task(threads_to_actually_archive_server_level, settings)
                    threads_archived_this_run = self.succeed_count - initial_succeed_count
                archive_task_time_sl = time.time() - archive_task_start_time_sl
                initial_log_info += f"\n  服务器级归档操作耗时: {archive_task_time_sl:.3f}s (成功:{self.succeed_count - initial_succeed_count}, 失败:{self.fail_count - initial_fail_count})"
        else:
            initial_log_info += f"\n服务器活跃帖数在目标 ({settings.max_active_threads}) 之内，无需归档操作。"
            overall_summary_embed_description += f"> 服务器活跃帖数在目标内，无需归档操作。\n"

        bot_log.info(initial_log_info)

        # --- 步骤 4: 基于频道的不活跃天数归档---
        if settings.inactivity_days > 0:
            log_inactivity_phase = "\n开始检查各非黑名单频道中的不活跃帖子..."

            blacklisted_forum_channel_ids_set = set(settings.blacklist_channel_ids)

            # 从之前获取的全服务器活跃帖子中筛选出仍在非黑名单频道内且仍然活跃（未被服务器级归档）的帖子
            active_threads_for_inactivity_check = []
            for t_obj in all_server_active_threads_list:
                if (
                    t_obj.parent_id not in blacklisted_forum_channel_ids_set
                    and t_obj.id not in pinned_threads_set_server_wide
                    and not t_obj.archived
                    and not t_obj.locked
                ):
                    active_threads_for_inactivity_check.append(t_obj)

            if active_threads_for_inactivity_check:
                log_inactivity_phase += f"\n  找到 {len(active_threads_for_inactivity_check)} 个在非黑名单频道中的活跃、非置顶帖进行不活跃检查"

                get_msg_start_ia = time.time()
                current_msg_succeed = self.message_succeed_count
                current_msg_fail = self.not_found_error_count
                thread_message_obj_list_inactivity = await self._get_last_message_task(active_threads_for_inactivity_check)
                get_msg_time_ia = time.time() - get_msg_start_ia
                log_inactivity_phase += f"\n  获取不活跃检查帖子的最后一条消息耗时: {get_msg_time_ia:.3f}s (S:{self.message_succeed_count-current_msg_succeed}/F:{self.not_found_error_count-current_msg_fail})"

                threads_to_archive_due_to_inactivity = []
                now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                inactivity_threshold_date = now_utc_naive - timedelta(days=settings.inactivity_days)

                for tm_obj in thread_message_obj_list_inactivity:
                    last_activity_naive = tm_obj.last_message.created_at.replace(tzinfo=None)

                    if last_activity_naive < inactivity_threshold_date:
                        threads_to_archive_due_to_inactivity.append(tm_obj)

                log_inactivity_phase += f"\n  找到 {len(threads_to_archive_due_to_inactivity)} 个帖子因不活跃需要归档"

                if threads_to_archive_due_to_inactivity:
                    archive_task_start_time_ia = time.time()
                    initial_succeed_count_ia = self.succeed_count
                    initial_fail_count_ia = self.fail_count
                    await self._archive_thread_task(threads_to_archive_due_to_inactivity, settings)
                    threads_archived_this_run += (self.succeed_count - initial_succeed_count_ia)
                    archive_task_time_ia = time.time() - archive_task_start_time_ia
                    log_inactivity_phase += f"\n  不活跃帖子归档操作耗时: {archive_task_time_ia:.3f}s (成功:{self.succeed_count - initial_succeed_count_ia}, 失败:{self.fail_count - initial_fail_count_ia})"
            else:
                log_inactivity_phase += "\n  没有在监控频道中找到需要进行不活跃检查的帖子。"

            bot_log.info(log_inactivity_phase)

        # --- 步骤 5: 日志与面板信息整理---
        global_finish_time = time.time()
        log_result_summary = f"\n--- 运行总结 (索引: {run_hash_value}) ---"
        log_result_summary += f"\n总计成功归档帖子: {self.succeed_count}"
        log_result_summary += f"\n总计归档失败: {self.fail_count}"
        log_result_summary += f"\n总计获取消息成功: {self.message_succeed_count}"
        log_result_summary += f"\n总计获取消息失败: {self.not_found_error_count}"
        log_result_summary += f"\n总运行耗时: {global_finish_time - global_start_time:.3f}秒"

        if self.log_archived_info_details:
            bot_log.info(f"\n--- 归档成功详情 (索引: {run_hash_value}) ---{self.log_archived_info_details}")
        if self.log_get_message_error_details:
            bot_log.warning(f"\n--- 获取消息失败详情 (索引: {run_hash_value}) ---{self.log_get_message_error_details}")
        if self.log_archived_error_details:
            bot_log.error(f"\n--- 归档失败详情 (索引: {run_hash_value}) ---{self.log_archived_error_details}")
        bot_log.info(log_result_summary)

        overall_summary_embed_description += f"\n> 总计归档成功/失败: **{self.succeed_count}** / **{self.fail_count}**\n"
        if manual: overall_summary_embed_description += f"-# (手动触发)\n"

        final_embed_color = Color.orange() if self.fail_count > 0 or self.not_found_error_count > 0 else Color.green()
        if threads_archived_this_run == 0 and kill_count_server_level <=0 :
             final_embed_color = Color.blue()

        final_embed = Embed(title=f"归档报告: {settings.config_name}", description=overall_summary_embed_description, color=final_embed_color)
        final_embed.set_author(name=current_run_timestamp_str)

        if self.archive_run_details_for_embed:
            details_text_parts = []
            current_length = 0
            max_field_length = 1000

            for title, desc in self.archive_run_details_for_embed.items():
                part = f"**{title}**\n{desc}\n"
                if current_length + len(part) > max_field_length and details_text_parts:
                    final_embed.add_field(name="部分归档详情", value="".join(details_text_parts), inline=False)
                    details_text_parts = [part]
                    current_length = len(part)
                else:
                    details_text_parts.append(part)
                    current_length += len(part)

            if details_text_parts: 
                 final_embed.add_field(name="部分归档详情 (续)" if final_embed.fields else "部分归档详情", value="".join(details_text_parts), inline=False)


        if settings.notification_thread_id:
            try:
                notif_channel = await self.fetch_channel(settings.notification_thread_id)

                if isinstance(notif_channel, (TextChannel, Thread)):
                    await notif_channel.send(embed=final_embed)

                    if self.log_get_message_error_details:
                        error_embed = Embed(title=f"警告: 获取消息出错↓", description=self.log_get_message_error_details[:4000], color=Color.yellow())
                        await notif_channel.send(embed=error_embed)

                    if self.log_archived_error_details:
                        error_embed = Embed(title=f"错误: 归档操作出错↓", description=self.log_archived_error_details[:4000], color=Color.red())
                        await notif_channel.send(embed=error_embed)

            except Exception as e:
                bot_log.error(f"发送最终通知到频道 {settings.notification_thread_id} 失败: {e}")

    async def _get_last_message_task(self, thread_list: list[discord.Thread]) -> list[ThreadMessage]:
        tasks_list = []

        for thread_to_check in thread_list:
            task = asyncio.create_task(self._get_last_message(thread_to_check))
            tasks_list.append(task)
            await asyncio.sleep(0.05) # 避免速率限制

        results = await asyncio.gather(*tasks_list)

        thread_obj_list = [
            ThreadMessage(thread, message)
            for thread, message in zip(thread_list, results)
            if message is not None
        ]
        return thread_obj_list

    async def _get_last_message(self, thread: discord.Thread) -> discord.Message | ErrorMessage:
        try:

            async for message_in_history in thread.history(limit=5):
                if message_in_history:
                    self.message_succeed_count += 1
                    return message_in_history

            # 如果循环结束没有找到消息
            self.not_found_error_count += 1
            error_detail = f"\n  > 帖子 {thread.mention} 中未能找到消息"

            self.log_get_message_error_details += error_detail #
            bot_log.warning(f"获取帖子 {thread.name} (ID:{thread.id}) 的最后消息失败: history()迭代未返回消息")
            return ErrorMessage(thread.created_at or datetime.now(timezone.utc))

        except discord.Forbidden:
            self.not_found_error_count += 1
            error_detail = f"\n  > 帖子 {thread.mention} 无权限访问其历史记录"
            self.log_get_message_error_details += error_detail
            bot_log.warning(f"获取帖子 {thread.name} (ID:{thread.id}) 的最后消息失败: 无权限(Forbidden)。")
            return ErrorMessage(thread.created_at or datetime.now(timezone.utc))

        except Exception as e:
            self.not_found_error_count += 1
            error_detail = f"\n  > 帖子 {thread.mention} 获取其消息时发生错误↙\n{e}"
            self.log_get_message_error_details += error_detail
            bot_log.error(f"获取帖子 {thread.name} (ID:{thread.id}) 的最后消息时发生异常: {e}", exc_info=False)
            return ErrorMessage(thread.created_at or datetime.now(timezone.utc))

    async def _archive_thread_task(self, thread_obj_list: list[ThreadMessage], settings: GuildArchiveSettings): #
        tasks_list = []

        for tm_obj in thread_obj_list:
            task = asyncio.create_task(self._archive_thread(tm_obj.thread, tm_obj.last_message, settings)) #
            tasks_list.append(task)
            await asyncio.sleep(0.05)

        await asyncio.gather(*tasks_list)

    async def _audit_pinned_thread_messages(self, guild: Guild, settings: GuildArchiveSettings, pinned_thread_ids: set[int]) -> str:
        """审计置顶帖中的漏监听消息"""
        log_info = f"\n置顶帖消息审计:"
        deleted_count = 0
        checked_count = 0
        audit_details = []
        
        # 获取所有置顶帖
        pinned_threads = []
        for thread_id in pinned_thread_ids:
            try:
                thread = guild.get_thread(thread_id)
                if thread and not thread.locked:  # 只处理非锁定的置顶帖
                    pinned_threads.append(thread)
            except Exception as e:
                bot_log.warning(f"获取置顶帖 {thread_id} 失败: {e}")
        
        log_info += f"\n  找到 {len(pinned_threads)} 个非锁定置顶帖进行审计"
        
        for thread in pinned_threads:
            try:
                checked_count += 1
                current_last_message_id = thread.last_message_id
                stored_last_message_id = self.pinned_last_messages.get(thread.id)
                
                # 如果没有记录或当前ID与记录不一致，说明有新消息
                if stored_last_message_id is None or current_last_message_id != stored_last_message_id:
                    log_info += f"\n  [新消息检测] {thread.name} (ID: {thread.id})"
                    
                    # 添加到embed详情
                    audit_key = f"[审计] {thread.name}"
                    if stored_last_message_id is None:
                        audit_desc = f"> 首次检测，拉取最新消息进行审计"
                    else:
                        audit_desc = f"> 检测到变化，拉取最新消息审计"
                    
                    # 拉取最新的一批消息（固定数量）
                    recent_messages = []
                    try:
                        async for message in thread.history(limit=50):
                            recent_messages.append(message)
                    except discord.Forbidden:
                        log_info += f" -> 无权限访问历史记录"
                        audit_desc += "\n> 无权限访问历史记录"
                        audit_details.append((audit_key, audit_desc))
                        continue
                    except Exception as e:
                        log_info += f" -> 获取历史记录失败: {e}"
                        audit_desc += f"\n> 获取历史记录失败: {e}"
                        audit_details.append((audit_key, audit_desc))
                        continue
                    
                    if recent_messages:
                        log_info += f" -> 拉取了 {len(recent_messages)} 条最新消息"
                        audit_desc += f"\n> 拉取了 {len(recent_messages)} 条最新消息"
                        
                        # 筛选需要删除的消息（3天内的消息）
                        now_utc = datetime.now(timezone.utc)
                        three_days_ago = now_utc - timedelta(days=3)
                        
                        deleted_messages_info = []
                        processed_count = 0
                        for message in recent_messages:
                            # 检查消息时间是否在3天内
                            if message.created_at < three_days_ago:
                                continue
                            
                            processed_count += 1
                            
                            # 检查用户是否在白名单中
                            author = message.author
                            
                            if self._is_user_exempt(author, thread, settings):
                                continue
                            
                            # 删除消息
                            try:
                                await message.delete()
                                deleted_count += 1
                                log_info += f"\n    [已删除] 用户 {author.name} 的消息 (ID: {message.id})"
                                deleted_messages_info.append(f"用户 {author.name} 的消息 (ID: {message.id})")
                            except discord.Forbidden:
                                log_info += f"\n    [删除失败] 无权限删除消息 (ID: {message.id})"
                                deleted_messages_info.append(f"无权限删除消息 (ID: {message.id})")
                            except discord.NotFound:
                                # 消息可能已经被删除
                                pass
                            except Exception as e:
                                log_info += f"\n    [删除失败] 消息 {message.id}: {e}"
                                deleted_messages_info.append(f"删除失败: {message.id} ({e})")
                        
                        if processed_count > 0:
                            audit_desc += f"\n> 检查了 {processed_count} 条3天内消息"
                        if deleted_messages_info:
                            deleted_lines = "\n> ".join(deleted_messages_info)
                            audit_desc += "\n> 删除操作: " + deleted_lines
                    
                    # 更新最后消息ID记录
                    if current_last_message_id:
                        self.pinned_last_messages[thread.id] = current_last_message_id
                        await self._save_pinned_last_messages()
                        log_info += f" -> 已更新最后消息ID: {current_last_message_id}"
                        audit_desc += f"\n> 已更新最后消息ID: {current_last_message_id}"
                    
                    audit_details.append((audit_key, audit_desc))
                
            except Exception as e:
                log_info += f"\n  [审计失败] 处理 {thread.name} 时发生错误: {e}"
                audit_key = f"[审计失败] {thread.name}"
                audit_desc = f"> 处理时发生错误: {e}"
                audit_details.append((audit_key, audit_desc))
        
        # 将审计详情添加到embed字段
        for audit_key, audit_desc in audit_details:
            if len(self.archive_run_details_for_embed) < 10:
                self.archive_run_details_for_embed[audit_key] = audit_desc
        
        log_info += f"\n  审计完成: 检查了 {checked_count} 个置顶帖，删除了 {deleted_count} 条消息"
        return log_info

    async def _archive_thread(self, thread: discord.Thread, last_msg_obj: discord.Message | ErrorMessage, settings: GuildArchiveSettings):
        archive_reason = f"自动归档"

        if isinstance(last_msg_obj, ErrorMessage):
            created_at_str = thread.created_at.strftime('%Y-%m-%d %H:%M') if thread.created_at else "未知时间"
            last_message_time_str = f"未知(帖子创建于 {created_at_str})"
            hours_diff_str = "未知"

        else:
            last_message_time_str = last_msg_obj.created_at.strftime('%Y-%m-%d %H:%M')
            now_utc = datetime.now(timezone.utc)
            time_diff = now_utc - last_msg_obj.created_at
            hours_diff = time_diff.total_seconds() / 3600
            hours_diff_str = f"{hours_diff:.2f} 小时前"
            days_diff = time_diff.total_seconds() / 86400
            days_diff_str = f"{days_diff:.2f} 天前"

        archive_category = None
        if settings.archive_category_id:
            archive_category = thread.guild.get_channel(settings.archive_category_id)
            if not isinstance(archive_category, CategoryChannel):
                archive_category = None

        try:
            action_taken = False
            start_time = time.time()

            # 实际归档操作
            if not thread.archived:
                await thread.edit(archived=True, reason=archive_reason)
                action_taken = True

            if action_taken or thread.archived:
                self.succeed_count += 1
                log_line = f"\n  - [{self.succeed_count}] {thread.name} | {thread.id} | 最后活跃时间: {last_message_time_str} ({hours_diff_str})"
                self.log_archived_info_details += log_line

                embed_title_key = f"[T{self.succeed_count}] 归档成功↓"
                embed_value_desc = f"> {thread.mention}\n> 最后活跃时间: {last_message_time_str} ({days_diff_str})"

                if len(self.archive_run_details_for_embed) < 10:
                    self.archive_run_details_for_embed[embed_title_key] = embed_value_desc #

        except Exception as e:
            self.fail_count += 1
            log_line = f"\n  - [E{self.fail_count}] {thread.name} (ID:{thread.id}) | 最后一条消息: {last_message_time_str} ({hours_diff_str}) | 错误: {e}"
            self.log_archived_error_details += log_line
            embed_title_key = f"[E{self.fail_count}] 归档失败"
            embed_value_desc = f"- ID:{thread.id} {thread.mention}\n- 最后一条消息: {last_message_time_str} ({hours_diff_str})\n- 错误: {str(e)[:100]}"

            if len(self.archive_run_details_for_embed) < 10:
                 self.archive_run_details_for_embed[embed_title_key] = embed_value_desc

            bot_log.error(f"归档帖子 {thread.name} (ID:{thread.id}) 失败: {e}", exc_info=False)

# --- 命令管理 ---
class ArchiveManagerCog(commands.Cog):
    def __init__(self, bot: ThreadArchiverBot):
        self.bot = bot

    @app_commands.command(name="set-archive-rules", description="设置当前服务器的归档规则（不活跃天数、活跃帖数量等）。")
    @app_commands.describe(
        config_name="在环境变量 GUILD_CONFIGS_JSON 中定义的服务器配置名",
        inactivity_days="帖子多少天不活跃后自动归档 (0 表示关闭按天归档)",
        max_active_posts="频道内最大活跃帖子数上限 (0 表示不限制；当前仍未在逻辑中使用，仅预留)",
        max_active_threads="整个服务器允许的最大活跃帖子数上限"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_archive_rules_cmd(self, interaction: discord.Interaction,
                                    config_name: str, inactivity_days: int, max_active_posts: int, max_active_threads: int):
        await interaction.response.defer(ephemeral=True)

        target_setting: GuildArchiveSettings | None = None
        guild_id_to_update: int | None = None

        for gid, setting_obj in self.bot.guild_settings_map.items():
            if setting_obj.config_name == config_name:
                target_setting = setting_obj
                guild_id_to_update = gid
                break

        if not target_setting or not guild_id_to_update:
            await interaction.followup.send(f"错误：未找到名为 '{config_name}' 的服务器配置。", ephemeral=True)
            return

        target_setting.inactivity_days = inactivity_days if inactivity_days >= 0 else target_setting.inactivity_days
        target_setting.max_active_posts = max_active_posts if max_active_posts >= 0 else target_setting.max_active_posts

        await self.bot.save_guild_setting(guild_id_to_update)

        embed = Embed(title="归档规则已更新", color=Color.green())
        embed.description = (
            f"服务器配置 **{config_name}** 的规则已更新：\n"
            f"- 不活跃归档天数: **{target_setting.inactivity_days if target_setting.inactivity_days > 0 else '未启用'}** 天\n"
            f"- 最大活跃帖子数: **{target_setting.max_active_posts if target_setting.max_active_posts > 0 else '未启用'}**"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        bot_log.info(f"用户 {interaction.user} 更新了 '{config_name}' 的归档规则。")

    @app_commands.command(name="manual-guild-archive", description="手动触发一次归档审计（服务器级 + 不活跃检查）。")
    @app_commands.describe(config_name="在 GUILD_CONFIGS_JSON 中配置的服务器别名")
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
            await interaction.followup.send(f"错误：未找到名为 '{config_name}' 的服务器配置。", ephemeral=True)
            return

        guild = self.bot.get_guild(guild_id_to_process)
        if not guild:
            await interaction.followup.send(f"错误：机器人当前无法访问服务器ID {guild_id_to_process}。", ephemeral=True)
            return

        if self.bot.operation_lock.locked():
            await interaction.followup.send("当前有其他归档操作正在进行中，请稍后再试。", ephemeral=True)
            return

        async with self.bot.operation_lock:
            bot_log.info(f"用户 {interaction.user} 手动触发对 '{config_name}' 的归档。")
            await interaction.followup.send(f"正在开始对服务器配置 '{config_name}' (服务器: {guild.name}) 进行手动归档检查...", ephemeral=True)
            await self.bot.process_guild_threads(guild, target_setting, manual=True)
            await interaction.edit_original_response(content=f"对服务器配置 '{config_name}' 的手动归档检查已完成。详情请查看通知频道。")

    @app_commands.command(name="view-guild-config", description="查看已加载的服务器归档配置。")
    @app_commands.describe(config_name="在 GUILD_CONFIGS_JSON 中定义的服务器配置名（留空查看所有）")
    @app_commands.guild_only()
    async def view_guild_config_cmd(self, interaction: discord.Interaction, config_name: str | None = None):
        await interaction.response.defer(ephemeral=True)
        embeds_to_send = []

        if config_name:
            found_setting: GuildArchiveSettings | None = None
            for setting in self.bot.guild_settings_map.values():
                if setting.config_name == config_name:
                    found_setting = setting
                    break

            if not found_setting:
                await interaction.followup.send(f"未找到名为 '{config_name}' 的服务器配置。", ephemeral=True)
                return

            settings_list = [found_setting]

        else:
            settings_list = list(self.bot.guild_settings_map.values())

            if not settings_list:
                await interaction.followup.send("当前没有已加载的服务器配置。", ephemeral=True)
                return

        for setting in settings_list:
            embed = Embed(title=f"配置: {setting.config_name}", color=Color.blue())
            embed.add_field(name="服务器ID", value=str(setting.guild_id), inline=False)
            embed.add_field(name="黑名单频道ID", value=", ".join(map(str, setting.blacklist_channel_ids)) or "未设置", inline=False)
            embed.add_field(name="归档分类ID", value=str(setting.archive_category_id) if setting.archive_category_id else "未设置", inline=False)

            inactivity_days_str = f"{setting.inactivity_days} 天" if setting.inactivity_days > 0 else "未启用"
            max_posts_str = str(setting.max_active_posts) if setting.max_active_posts > 0 else "未启用"
            max_active_threads = str(setting.max_active_threads) if setting.max_active_threads > 0 else "未启用"

            embed.add_field(name="不活跃天数", value=inactivity_days_str, inline=True)
            embed.add_field(name="最大活跃帖数", value=max_posts_str, inline=True)
            embed.add_field(name="服务器最大活跃帖数", value=max_active_threads, inline=True)
            embed.add_field(name="通知频道/帖子ID", value=str(setting.notification_thread_id) if setting.notification_thread_id else "未设置", inline=False)

            if setting.last_notice_message_id:
                embed.add_field(name="上次通知消息ID", value=str(setting.last_notice_message_id), inline=False)
            embeds_to_send.append(embed)

        if embeds_to_send:
            for i in range(0, len(embeds_to_send), 10):
                await interaction.followup.send(embeds=embeds_to_send[i:i+10], ephemeral=True)
        else:
            await interaction.followup.send("未能生成配置信息。",ephemeral=True)

# --- 主程序入口 ---
def main_bot_runner():
    bot = ThreadArchiverBot()

    # 优先从环境变量读取 BOT_TOKEN（由 .env 提供）
    bot.bot_token = os.environ.get("BOT_TOKEN")

    if not bot.bot_token:
        bot_log.critical("未能从环境变量 BOT_TOKEN 读取机器人令牌。请在 .env 中配置 BOT_TOKEN。")
        return

    try:
        bot.run(bot.bot_token)
    except discord.LoginFailure:
        bot_log.critical("Discord登录失败：无效的Token。")
    except Exception as e:
        bot_log.critical(f"机器人运行时发生致命错误: {e}", exc_info=True)
    finally:
        bot_log.info("机器人已关闭。")

if __name__ == "__main__":
    main_bot_runner()
