# Discord 归档BOT

## 简介

这是一个 Discord BOT，旨在帮助服务器管理员自动管理和归档帖子。它可以根据一系列可配置的规则，如帖子不活跃天数、服务器或特定频道内的最大活跃帖子数量等，来定期清理和归档帖子，保持服务器总活跃帖子不超过规定数目，为新的帖子预留空间。

## 功能特性

* **定时审计**: BOT会每隔15分钟自动检查并审计服务器中的帖子。
* **不活跃归档**: 根据设定的天数，自动归档长时间没有新消息的帖子。
* **服务器级数量控制**: 限制整个服务器的活跃帖子总数，超过限制则归档最旧的非置顶、非锁定帖子。
* **监控指定频道**: 可以配置BOT只监控特定论坛频道或所有频道的帖子。
* **置顶帖豁免**: 在服务器级数量控制和不活跃检查中，置顶的帖子不会被自动归档，并会确保其保持活跃状态。
* **锁定帖豁免**: 在服务器级数量控制的归档候选对象中，锁定的帖子不会被选中。
* **详细日志**: 记录详细的操作日志，包括成功归档的帖子、失败的尝试以及获取消息时的错误，方便追踪和调试。日志文件保存在 `logs/archiver_bot.log`。
* **归档报告**: 在每次自动或手动执行归档操作后，向指定的通知频道发送包含操作摘要和详细信息的嵌入式消息。
* **Slash 命令管理**:
    * 动态设置归档规则（如不活跃天数、最大活跃帖子数）。
    * 手动触发对特定服务器配置的归档检查。
    * 查看当前生效的服务器归档配置。
* **灵活配置**: 通过 `bot_config.json` 文件和 `.env` 文件进行详细配置。

## 工作流程

1.  **加载配置**: BOT启动时，会从 `bot_config.json` 文件加载各个服务器的归档设置，并从 `.env` 文件加载BOT令牌等敏感信息。
2.  **周期性审计/手动触发**:
    * BOT会定时（例如每15分钟）自动对配置的服务器执行归档检查。
    * 管理员也可以通过 Slash 命令手动触发对特定服务器配置的归档流程。
3.  **处理服务器 (`process_guild_threads` 函数)**:
    * **获取活跃帖子**: 获取服务器当前所有活跃的帖子。
    * **置顶帖处理**: 遍历所有活跃帖子，如果发现置顶帖已被归档，则尝试取消其归档状态，确保置顶帖保持可见。
    * **服务器级数量控制**:
        * 计算当前服务器总活跃帖子数与配置的 `max_active_threads`（服务器最大活跃帖子数）之间的差值 (`kill_count_server_level`)。
        * 如果 `kill_count_server_level` 大于0（即需要归档帖子以满足数量限制）：
            * 筛选候选帖子：排除置顶帖和锁定的帖子。 如果 `only_archive_monitored_channels` 设置为 `true`，则只考虑监控频道列表中的帖子。
            * 获取这些候选帖子的最后一条消息时间。
            * 按最后一条消息的时间升序排序（最旧的在前）。
            * 归档排序后最前面的 `kill_count_server_level` 个帖子。
    * **不活跃归档**:
        * 如果配置了 `inactivity_days` (大于0):
            * 筛选监控频道中仍然活跃、非置顶且非锁定的帖子。
            * 获取这些帖子的最后一条消息。
            * 将当前UTC时间减去 `inactivity_days` 得到不活跃阈值日期。
            * 如果帖子的最后一条消息时间早于此阈值，则将其加入待归档列表。
            * 归档所有因不活跃而选中的帖子。
    * **发送报告**: 完成上述操作后，BOT会整理本次运行的统计数据（成功归档数、失败数等），并向配置的 `notification_thread_id` 发送一个嵌入式消息作为报告。 如果获取消息或归档过程中有错误，也会一并报告。

## 配置

### 1. 环境变量 (`.env` 文件)

在BOT运行的根目录下创建一个 `.env` 文件，并至少包含以下内容：

```env
BOT_TOKEN="YOUR_DISCORD_BOT_TOKEN_HERE"
MAIN_ADMIN_CHANNEL_ID="OPTIONAL_ADMIN_CHANNEL_ID_FOR_STARTUP_NOTIFICATIONS"
```

* `BOT_TOKEN`: **必需。** 你的 Discord BOT令牌。
* `MAIN_ADMIN_CHANNEL_ID`: **可选。** 一个 Discord 文本频道或帖子 ID，BOT启动成功后会向此频道发送通知。

### 2. 配置文件 (`bot_config.json`)

在BOT运行的根目录下创建一个 `bot_config.json` 文件。此文件定义了BOT将如何管理一个或多个服务器的帖子。

**示例结构:**

```json
{
  "guild_configurations": {
    "your_server_config_alias": {
      "guild_id": 123456789012345678,
      "only_archive_monitored_channels": false,
      "monitoring_channel_ids": [
        987654321098765432,
        987654321098765433
      ],
      "archive_category_id": null,
      "inactivity_days": 30,
      "notification_thread_id": 987654321123456789,
      "max_active_posts": 50,
      "max_active_threads": 200
    }
    // 可以添加更多服务器配置...
  }
}
```

**字段说明:**

* `guild_configurations`: 一个对象，键为服务器配置的别名（自定义，如 "main_server_archive"），值为该服务器的具体配置对象。
    * `guild_id`: (整数型) **必需。** Discord 服务器的 ID。
    * `only_archive_monitored_channels`: (布尔型) 是否只在 `monitoring_channel_ids` 列出的频道中执行归档操作（针对服务器级数量控制）。默认为 `false`，表示对服务器所有帖子（除非被豁免）进行数量控制。对于不活跃归档，则始终只检查 `monitoring_channel_ids` 中的帖子。
    * `monitoring_channel_ids`: (整数型列表) 需要监控的论坛频道 ID 列表。不活跃归档将只检查这些频道中的帖子。
    * `inactivity_days`: (整数型) 帖子在多少天没有新活动后被视为不活跃并归档。设置为 `0` 或负数表示不启用此规则。
    * `notification_thread_id`: (整数型) 用于接收BOT归档操作报告的文本频道或帖子 ID。
    * `max_active_posts`: (整数型) 通过 `/set-archive-rules` 命令可设置。用于限制单个监控频道内的最大活跃帖子数。设置为 `0` 或负数表示不启用。
    * `max_active_threads`: (整数型) 整个服务器允许的最大活跃帖子数量。如果实际活跃帖子数超过此值，BOT会尝试归档最旧的帖子（遵循豁免规则）直到满足此限制。设置为 `0` 或负数表示不启用此服务器级数量限制。

### 3. 数据文件 (`data` 目录)

BOT会自动创建和使用 `data` 目录。

* `{config_name}_notice_id.txt`: 此文件用于存储每个服务器配置最后一次发送到 `notification_thread_id` 的通知消息的ID。 这主要用于内部记录，例如更新或回复之前的通知（尽管当前代码似乎主要用于存储最后ID，而非直接编辑旧消息）。

## 命令

BOT提供以下 Slash 命令进行管理 (需要用户拥有 `管理服务器 (Manage Guild)` 权限来执行设置和手动触发命令)：

* **`/set-archive-rules`**
    * 描述: 设置指定服务器配置的归档规则。
    * 参数:
        * `config_name` (字符串): 在 `bot_config.json` 中定义的服务器配置名。
        * `inactivity_days` (整数): 帖子多少天不活跃后归档 (0表示不启用此规则)。
        * `max_active_posts` (整数): (见配置说明中关于此参数的注意点) 频道内最大活跃帖子数 (0表示不启用此规则)。
        * `max_active_threads` (整数): 整个服务器的最大活跃帖子数。
* **`/manual-guild-archive`**
    * 描述: 手动触发对指定配置的服务器进行一次归档检查。
    * 参数:
        * `config_name` (字符串): 在 `bot_config.json` 中定义的服务器配置名。
* **`/view-guild-config`**
    * 描述: 查看指定服务器或所有已加载服务器的当前归档配置。
    * 参数:
        * `config_name` (字符串, 可选): 在 `bot_config.json` 中定义的服务器配置名。如果留空，则显示所有已加载的配置。

## 安装与运行

1.  **环境准备**:
    * 确保你已安装 Python 3.8 或更高版本。
    * 克隆或下载此代码库。
2.  **安装依赖**:
    打开终端，导航到项目根目录，然后运行：
    ```bash
    pip install discord.py python-dotenv
    ```
3.  **配置BOT**:
    * 创建并填写上文所述的 `.env` 文件。
    * 创建并根据你的需求填写 `bot_config.json` 文件。
4.  **运行BOT**:
    在项目根目录下运行：
    ```bash
    python main.py
    ```
   

## 日志系统

* BOT的所有重要活动、错误和警告都会被记录下来。
* 日志文件位于项目根目录下的 `logs/archiver_bot.log`。
* 日志信息同时也会输出到控制台。

## 注意事项

* **权限**: 确保BOT拥有必要的权限来读取频道历史、查看帖子、管理帖子（编辑以进行归档）以及在通知频道发送消息。至少需要 `Read Message History`, `View Channels`, `Manage Threads`, `Send Messages`, `Embed Links`。
* **速率限制**: 代码中包含小的延时 (`asyncio.sleep`) 以尝试避免 Discord API 的速率限制，但在非常大的服务器或非常频繁的操作下仍需注意。
