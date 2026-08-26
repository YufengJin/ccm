> 注:本文档由实际部署环境的设计稿脱敏而来;email / uuid / 主机名均为示例值。

# ccm — Claude 多账号统一管理 CLI 设计文档

- 日期：2026-08-26
- 部署/开发目标机：一台远程工作站
- 项目路径：`~/repos/ccm`

---

## 1. 背景

现状用两个 shell function 切换账号：

```bash
cca() { export CLAUDE_CONFIG_DIR=$HOME/.claude-a; ... }
ccb() { export CLAUDE_CONFIG_DIR=$HOME/.claude-b; ... }
```

存在四个问题：

1. **只影响当前 shell**。已开的其他终端、cron、脚本都不受影响，没有任何持久状态。
2. **没有可见性**。不启动 `claude` 就看不到任何一个账号还剩多少额度，切号全靠猜。
3. **共享配置靠手工 symlink**。每个账号目录下 7 条 `ln -s` 手工维护（`CLAUDE.md` / `plugins` / `projects` / `settings.json` / `settings.local.json` / `skills` / `statusline-command.sh`），加账号要重敲，断链无人察觉。
4. **`~/.claude` 身兼两职**：既是默认账号的配置目录（自带 `.credentials.json`），又是其他账号 symlink 过去的共享资源库。职责耦合导致无法干净地增删账号。

## 2. 目标

- 一套命令完成账号的增 / 删 / 改 / 查 / 切换，不再需要 `cca`/`ccb`。
- **不启动 `claude` 就能看到所有账号的实时限流用量**，并据此选号。
- 共享配置由声明式清单驱动，一条命令幂等重建，断链可检测。
- 目录布局解耦：账号身份、共享资源、CLI 自身状态三者物理分离。
- **迁移过程不中断任何正在运行的 claude 进程。**

## 3. 非目标

- 不做 API key / 第三方中转（`ANTHROPIC_BASE_URL`）账号的管理。仅覆盖 Claude 订阅的 OAuth 账号。
- 不实现 OAuth 登录流程本身 —— 交给 `claude` 自己的 `/login`，ccm 只负责把它引导到正确的配置目录。
- 不做跨机器同步（`export`/`import` 只产出可搬运的归档，不含传输与冲突合并）。
- 不代管 `~/.claude/settings.json` 的内容语义，只管它放在哪、谁链到它。

## 4. 数据模型

两层，是本设计的核心概念：

```
Account   由 oauthAccount.accountUuid 唯一确定
          —— 限流额度、订阅、用量都挂在这一层
  └─ Profile  一个配置目录
          —— 切换单位、独立的会话历史与 daemon
```

**一个 Account 可以有多个 Profile，它们共享同一份限流额度。**

目标机现状会被登记为 **3 个 profile / 2 个 account**：

| profile | 原目录 | accountUuid | email |
|---|---|---|---|
| `default` | `~/.claude` | `acct-aaaa…` | alice@example.com |
| `personal` | `~/.claude-a` | `acct-aaaa…` | alice@example.com |
| `work` | `~/.claude-b` | `acct-bbbb…` | bob@example.com |

因此：

- `ccm ls` 按 **profile** 列出，并标注同 account 的分组。
- `ccm usage` 按 **account** 去重聚合 —— 每个 account 只发一次 API 请求，避免把同一份额度显示成两份。
- `ccm best` 在 **account** 维度比较剩余额度，再映射回一个推荐 profile。

### 命名模型修订(2026-08-26,用户要求)

profile 不再用 personal/work 这类语义别名定义,改为**编码 id + email 双轨**:

- id 自动分配:`a1, a2, a3, …`(`ccm add` 不填名字即自动编号;显式起名仍允许)
- **定位符**(所有吃 profile 参数的命令通用):精确 id → 纯数字 `N`→`aN` → 精确 email
  → email 子串(≥3 字符,不区分大小写)→ account uuid 前缀(≥6 字符)
- 命中同一 account 的多个 profile → 自动挑最优(token 有效 > 默认 profile > id 序)
- 跨 account 多命中 → 报歧义并列出候选,绝不猜
- 魔法名 `default` 废除,注册表改用结构性指针 `default_profile`(标记哪个 profile 是
  `~/.claude` 的默认落点);该 profile 不可删,可改名(指针与 `~/.claude` 链接同步走)

### 身份解析顺序

profile 的 account 身份按以下顺序解析，前者缺失才回退：

1. `<profile>/.claude.json` 的 `oauthAccount`
2. 仅对 default profile：`~/.claude.json`（旧版遗留路径）
3. 用 `<profile>/.credentials.json` 的 access token 调 `GET /api/oauth/profile` 现查

目标机上 `~/.claude/.claude.json` 只有 133B 且不含 `oauthAccount`，其身份实际在 `~/.claude.json` —— 第 2 条分支必须实现，第 3 条是最终兜底。解析结果写入注册表缓存，避免每次都打网络。

## 5. 目录布局

```
~/.claude-shared/            纯共享库，唯一真实副本，不含任何账号身份
    settings.json  settings.local.json  CLAUDE.md
    plugins/  skills/  commands/  agents/  projects/
    statusline-command.sh

~/.claude-accounts/
    default/  personal/  work/
        .credentials.json  .claude.json  history.jsonl
        sessions/  session-env/  shell-snapshots/  tasks/
        daemon/  daemon.log  daemon.lock  daemon.status.json
        cache/  file-history/  plans/  jobs/  paste-cache/  backups/
        chrome/  audit/  downloads/
        mcp-needs-auth-cache.json  .last-cleanup  .last-update-result.json
        settings.json        -> ~/.claude-shared/settings.json     (ccm 生成)
        settings.local.json  -> ~/.claude-shared/settings.local.json
        CLAUDE.md            -> ~/.claude-shared/CLAUDE.md
        plugins              -> ~/.claude-shared/plugins
        skills               -> ~/.claude-shared/skills
        projects             -> ~/.claude-shared/projects
        statusline-command.sh-> ~/.claude-shared/statusline-command.sh

~/.ccm/
    profiles.json   注册表：profile 定义 + 共享清单 + 身份缓存
    state.json      当前活跃 profile
    usage.db        sqlite：用量采样历史 + jsonl 扫描游标 + token 聚合
    pricing.json    模型价格表（可选，覆盖内置默认）
    backups/        migrate 与 backup 产出的 tar.zst / tar.gz
    logs/           refresh、migrate 的操作日志

兼容层（迁移后长期保留，直到 --cleanup）
    ~/.claude    -> ~/.claude-accounts/default
    ~/.claude-a  -> ~/.claude-accounts/personal
    ~/.claude-b  -> ~/.claude-accounts/work
```

**账号私有 / 共享的划分依据**：凡是带账号身份、会话状态、进程锁的归私有；凡是与「这台机器上的 Claude 该怎么工作」有关的归共享。`projects/`（会话记录）划为共享是**沿用现状的刻意选择** —— 用户依赖跨账号 `--resume` 旧会话。

## 6. 配置文件格式

所有 ccm 自身状态位于 `~/.ccm/`。该根路径由环境变量 **`CCM_HOME`** 覆盖（未设置时才回落到 `~/.ccm`）——测试全程注入 `CCM_HOME` 指向 tmpdir，因此单测绝不触碰真实 HOME。同理 `CCM_ACCOUNTS_ROOT` / `CCM_SHARED_ROOT` 可覆盖注册表里的两个 root，仅供测试与迁移演练使用，正常运行不需要设置。

### `~/.ccm/profiles.json`

```json
{
  "version": 1,
  "shared_root": "~/.claude-shared",
  "accounts_root": "~/.claude-accounts",
  "shared": [
    "settings.json", "settings.local.json", "CLAUDE.md",
    "plugins", "skills", "commands", "agents",
    "projects", "statusline-command.sh"
  ],
  "profiles": {
    "work": {
      "path": "~/.claude-accounts/work",
      "compat_link": "~/.claude-b",
      "account_uuid": "acct-bbbb…",
      "email": "bob@example.com",
      "subscription": "max",
      "rate_limit_tier": "default_claude_max_20x",
      "identity_fetched_at": 1787744335032,
      "note": "公司账号"
    }
  }
}
```

**命名与路径约束**:profile 名必须匹配 `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`;`default` 为保留名不可 rm/rename;`shared[]` 条目必须是**单段相对名**(不含 `/`、不为 `..`);`shared_root`/`accounts_root` 不得互为祖先、不得与任一 profile 路径嵌套。注册表加载时校验,违规即 `CcmError`。

`shared[]` 是**唯一的共享真相来源**。`ccm link` 遍历它铺 symlink，`ccm doctor` 遍历它校验，`ccm add` 遍历它初始化。清单里列而 `shared_root` 下不存在的条目跳过并在 doctor 里提示（例如目前没有 `commands/`、`agents/`）。

### `~/.ccm/state.json`

```json
{"active": "work", "changed_at": 1787744335032, "changed_by": "ccm use"}
```

### `~/.ccm/pricing.json`（可选覆盖）

内置默认值（美元 / 百万 token，用于**等价折算**，见 §9）：

| model_id 前缀 | input | output |
|---|---|---|
| `claude-fable-5` | 10.00 | 50.00 |
| `claude-mythos-5` | 10.00 | 50.00 |
| `claude-opus-5` | 5.00 | 25.00 |
| `claude-opus-4-8` / `4-7` / `4-6` | 5.00 | 25.00 |
| `claude-sonnet-5` | 2.00 | 10.00 |
| `claude-sonnet-4-6` | 3.00 | 15.00 |
| `claude-haiku-4-5` | 1.00 | 5.00 |

缓存倍率（相对同模型 input 单价）：`cache_read = 0.1×`，`cache_write_5m = 1.25×`，`cache_write_1h = 2.0×`。

模型匹配按**最长前缀**，未命中的 model_id 计入 `unknown` 桶，token 数照常统计、金额记 0 并在输出里单列。价格表带 `"updated": "2026-08-26"` 字段，`ccm doctor` 在超过 180 天时提示复核。

## 7. 命令表面

### 生命周期

| 命令 | 行为 |
|---|---|
| `ccm add <name> [--note <text>]` | 在 `accounts_root` 下新建 profile 目录并按 `shared[]` 铺共享 symlink，随后提示跑 `ccm login <name>` |
| `ccm add <name> --import <dir> [--move]` | 纳管已有目录。默认**原地**纳管（注册表记录 `<dir>` 原路径，不移动任何文件）；加 `--move` 才按 §10 阶段 2 的手法搬入 `accounts_root` 并在原位留兼容 symlink |
| `ccm ls` / `ccm list [--json]` | profile 表：名称 / email / 订阅 / tier / 5h% / 7d% / token 剩余寿命 / 活跃标记 / 同 account 分组 |
| `ccm show <name>` | 单个 profile 详情：路径、身份、凭证寿命、symlink 状态、目录体积、最近使用时间 |
| `ccm rm <name> [--keep-data]` | 从注册表移除;默认强制先备份(**含凭证**,0600)再删目录,`--keep-data` 只摘注册。有活跃进程时拒绝;`default` 为保留名不可删 |
| `ccm rename <old> <new>` | 改名并同步目录名、兼容链接、state |
| `ccm login <name>` | `CLAUDE_CONFIG_DIR=<path> claude`，引导用户走 `/login`，退出后回填身份 |
| `ccm logout <name>` | 删除 `.credentials.json`,**默认不保留副本**(否则「退出」语义不完整);`--keep-backup` 显式保留(0600)。有活跃进程时拒绝 |

### 切换与使用

| 命令 | 行为 |
|---|---|
| `ccm use <name>` | 写 state；若经 shell function 调用，同时 `eval` 出 export 改当前 shell |
| `ccm use --auto` | 选当前额度最宽裕的 account 对应的 profile 并切过去 |
| `ccm current [--quiet]` | 打印当前 profile（`--quiet` 只输出名字，供脚本用） |
| `ccm run <name> -- <args…>` | `exec` 一个注入了 `CLAUDE_CONFIG_DIR` 的 claude，不改全局状态 |
| `ccm shell <name>` | 开一个注入好环境的子 shell |
| `ccm env [<name>] [--shell bash\|zsh\|fish]` | 输出 export 语句，供 `eval "$(ccm env)"` |
| `ccm init [bash\|zsh\|fish] [--print]` | 生成并（默认）写入 shell 集成片段 |
| `ccm best [--json]` | 输出当前最宽裕的 profile 名 |

### 用量与成本

| 命令 | 行为 |
|---|---|
| `ccm usage [<name>]` | 当前/指定 profile 所属 account 的实时用量 |
| `ccm usage --all` | 全部 account 并排，含重置倒计时 |
| `ccm usage --watch [--interval 60]` | 定时刷新面板 |
| `ccm usage --json` | 机器可读，供 statusline / 脚本 |
| `ccm usage --history 7d` | 从 usage.db 出趋势 |
| `ccm cost [--by model\|project\|day\|profile] [--since 7d] [--json]` | 本地 token 聚合与等价金额 |
| `ccm daemon start\|stop\|status [--interval 300]` | 后台定时采样用量入库 |

### 健康与凭证

| 命令 | 行为 |
|---|---|
| `ccm doctor [--fix]` | 全量体检（见 §12） |
| `ccm refresh [<name>\|--all] [--force]` | 主动刷新 access token（见 §8 的并发保护） |
| `ccm token <name> --yes` | 打印 access token，供脚本；无 `--yes` 拒绝执行 |

### 配置与运维

| 命令 | 行为 |
|---|---|
| `ccm link [<name>]` | 按 `shared[]` 幂等重建 symlink |
| `ccm unlink <name> <item>` | 把某共享项复制成该 profile 的独立副本 |
| `ccm shared ls\|add <item>\|rm <item>` | 维护共享清单 |
| `ccm diff <a> <b>` | 比较两 profile 的非共享配置差异 |
| `ccm backup [<name>] [--with-credentials]` | 打包到 `~/.ccm/backups/` |
| `ccm restore <archive> [--into <name>]` | 恢复。解包走安全过滤:逐成员校验规范化路径,拒绝绝对路径/`..`/symlink 穿越/设备节点(Python≥3.12 用 `tarfile.data_filter`,否则手写等价校验) |
| `ccm export <name> <file>` / `ccm import <file>` | 跨机器搬运 |
| `ccm migrate [--dry-run\|--rollback\|--cleanup]` | 布局迁移（见 §10） |
| `ccm statusline` | 输出 `work 5h:6% 7d:29%` 一行，供 Claude Code statusline |
| `ccm completion bash\|zsh\|fish` | 补全脚本 |

## 8. 切换机制

三层独立，任一层单独可用：

**1. 状态层** —— `ccm use work` 写 `~/.ccm/state.json`。所有**新开**的终端立即生效。

**2. Shell 层** —— `ccm init bash` 向 `~/.bashrc` 注入：

```bash
# >>> ccm >>>
eval "$(command ccm env --shell bash 2>/dev/null)"
ccm() {
  case "$1" in
    use|switch)
      local out; out="$(command ccm "$@" --emit-env)" || return $?
      eval "$out"
      ;;
    *) command ccm "$@" ;;
  esac
}
# <<< ccm <<<
```

启动时读 state 设好 `CLAUDE_CONFIG_DIR`；`ccm use` 时把程序吐出的 export 在当前 shell `eval` —— **当前 shell 也跟着变**，这是 `cca`/`ccb` 的等价能力，只是统一到了一个命令下。

`--emit-env` 是内部标志：让 `use` 在 stdout 只输出可 eval 的 export 语句，把人类可读的提示写到 stderr。

**3. 兜底层** —— `ccm run work -- --resume` 直接 `os.execvpe` 一个注入了环境变量的 claude。**cron 与非交互脚本一律用 `ccm run`**——它们不 source rc,state 层对其不生效(state 只影响加载了 shell 集成的交互终端)。

**rc 块的 pin 守卫**:rc 中的 `eval "$(ccm env)"` 用 `[ -z "$CCM_PROFILE_PINNED" ]` 包住;`ccm shell`/`ccm run` 注入 `CCM_PROFILE_PINNED=1`,避免子 shell 加载 rc 时被全局 state 覆盖(codex 审核发现的真 bug)。

**默认 profile 的兜底**：兼容层里 `~/.claude -> ~/.claude-accounts/default`，所以未设 `CLAUDE_CONFIG_DIR` 时 claude 仍然落到 default profile，行为与迁移前一致。

## 9. 用量查询与凭证安全

### 实时用量

- 端点：`GET https://api.anthropic.com/api/oauth/usage`
- 头：`Authorization: Bearer <accessToken>`、`anthropic-beta: oauth-2025-04-20`
- 身份端点：`GET https://api.anthropic.com/api/oauth/profile`（同样的头）

响应关键字段：`five_hour.{utilization, resets_at}`、`seven_day.{…}`、`seven_day_opus`、`limits[]`（每项含 `kind`/`percent`/`severity`/`resets_at`/`is_active`）、`extra_usage`。渲染时以 `limits[]` 为准（它是服务端给出的规范化列表），`five_hour`/`seven_day` 作为兼容回退。

按 `account_uuid` 去重：同一 account 的多个 profile 只发一次请求。

### ⚠️ 凭证刷新的并发风险（最高优先级约束）

access token 寿命只有数小时。刷新会**轮换 refresh token**。而 Claude Code 自己也在刷新同一份凭证 —— 若 ccm 与运行中的 claude 同时刷新，**其中一方持有的 refresh token 会作废，导致账号掉线**。

因此 ccm 的刷新策略按以下顺序降级：

| 条件 | 行为 |
|---|---|
| token 未过期 | 直接查询。**每次查询前重新读取 `.credentials.json`**，复用 Claude Code 刚刷新出来的 token |
| token 已过期，且该 profile **无活跃进程** | 取文件锁 `~/.ccm/locks/<profile>.lock` → 备份 credentials 到 `~/.ccm/backups/` → 刷新 → 写 tmp 再 `os.replace` 原子落盘 → 记日志 |
| token 已过期，且该 profile **有活跃进程** | **绝不刷新**。回退显示 `.claude.json` 的 `cachedUsageUtilization`，输出中明确标注「缓存数据，N 小时前」 |
| 刷新失败 | **绝不写回旧凭证**(请求一旦发出,服务端可能已轮换,旧 refresh token 视为作废);结果不确定时停止一切写入,提示 `ccm login <name>` |

`--force` 是越过活跃进程保护的唯一途径,且必须交互确认。

**P1 实现 refresh 时的附加约束**(codex 审核采纳):锁粒度按 **account**(refresh token 指纹)而非 profile——同 account 两个 profile 并发刷新会互相作废;`--all` 对同组凭证串行。写回前必须校验 `.credentials.json` 的 mtime/内容与读取时一致(CAS),变了说明 Claude Code 刚刷新过,放弃写入直接重读。进程扫描是瞬时观察,存在 TOCTOU;「无活跃进程」只是必要条件,CAS 校验才是最后防线;`/proc` 不可读等未知状态一律按「有活跃进程」处理。

**活跃进程判定**：扫描 `/proc/*/environ` 取 `CLAUDE_CONFIG_DIR`，并把值与兼容链接一起 `realpath` 后比较；同时检查 `<profile>/daemon.lock` 里记录的 pid 是否存活。

### 本地 token 统计与成本折算

数据源：`~/.claude-shared/projects/**/*.jsonl`，逐行取 `message.usage`、`message.model`、`timestamp`、`sessionId`。

**增量扫描**:usage.db 记录每个 jsonl 的 `(path, st_ino, size, mtime, byte_offset)`;`st_ino` 变化(原子替换/改名)、文件缩小或 mtime 倒退→在同一事务里删除该文件旧事件后整文件重扫。offset 只推进到**最后一个完整换行**(写入方可能留半行)。事件以 `(sessionId, requestId, uuid)` 为唯一键去重,重扫不重复计数。

**profile 归因**:主数据源是各 profile 私有的 `session-env/` —— 其**子目录名即 sessionId**(2026-08-26 在本机实测,对 projects 下 jsonl 的覆盖率 96%)。注意 `sessions/` 目录**不可用**于归因:里面是以 pid 命名的文件。由于 session-env 会被 Claude Code 定期清理(`.last-cleanup`),`sessionId → profile` 映射必须在**首次见到时持久化**进 usage.db,之后即使 session-env 条目被清理,历史归因仍然成立。查不到的记为 `unknown`;跨账号 resume 使同一 sessionId 可能先后关联多个 profile——多重映射记为 `ambiguous`,与 `unknown` 一样计入总量并单列,**绝不任选其一**。

**金额口径（重要）**：这些账号是 Max 订阅，**不按 API 计费**。`ccm cost` 输出的是「若按 API 价目表折算的等价金额」，用于横向比较模型与项目的开销结构，**不是账单**。CLI 输出与文档必须显式标注这一点。

折算公式（按 model 的最长前缀匹配价格）：

```
cost = ( input_tokens            × in_price
     + output_tokens           × out_price
     + cache_read_input_tokens × in_price × 0.1
     + cache_creation.ephemeral_5m_input_tokens × in_price × 1.25
     + cache_creation.ephemeral_1h_input_tokens × in_price × 2.0 ) / 1_000_000
```

单价是「美元/百万 token」,求和后**必须除以 1e6**;实现须附带「1M input token 恰好等于表内单价」的基准测试。

（真实记录中 `usage.cache_creation` 确实分列 `ephemeral_5m_input_tokens` 与 `ephemeral_1h_input_tokens`，可精确折算。）

## 10. 迁移

`ccm migrate` 的执行阶段：

| 阶段 | 动作 |
|---|---|
| 0 | `--dry-run`：扫描活跃进程、打印完整执行计划与预计影响，不动任何文件 |
| 1 | **预检**:`accounts_root`/`shared_root` 必须不存在或为空,三个新目标路径必须不存在,否则中止;随后全量备份三个目录到 `~/.ccm/backups/pre-migrate-<ts>.tar.gz`,权限 0600(含凭证) |
| 2 | **搬 profile**:对每个目录,先在旁边建好指向新位置的 dangling symlink,`rename` 真实目录过去,再 `rename` symlink 落回原路径;每步**先写 intent 进 journal、完成后标 done**(`~/.ccm/logs/migrate-journal.json` 逐条落盘),SIGKILL/断电后可依据 journal+实际文件状态续跑或回滚;每次 rename 后 `lstat` 复核类型,发现被抢占创建即中止。窗口 = 一次 rename |
| 3 | **拆共享库**：把 `shared[]` 各项从 default 目录 `mv` 到 `~/.claude-shared/`，原位留 symlink |
| 4 | **铺 symlink**：按清单为全部 profile 生成共享链接 |
| 5 | 写 `profiles.json` / `state.json`，安装 shell 集成 |
| 6 | 自动跑 doctor;仅当**迁移不变量类检查**(目录存在/共享链接/兼容链接/链接源/注册表-state 一致)出现 fail 才自动 `--rollback`——迁移前已存在的 warn(token 过期、遗留文件等)不触发回滚 |

**零中断的依据**（已在 目标机实测）：三个目录都在 `/dev/nvme0n1p5` 同一文件系统，`mv` 走 `rename(2)`，**inode 不变**，已打开的 fd 继续有效；旧路径经 symlink 解析到新位置，后续按路径的 `open` 也照常。Claude Code 写 `.claude.json` 用「同目录写 tmp + rename」，经 symlink 后 tmp 与目标仍在同一真实目录，原子写语义不破坏。

**残余风险**:①阶段 2/3 的两次 rename 之间存在微秒级路径空窗——「零中断」的严格含义是**对当前进程集零中断**(目标机 为零 claude 进程),一般情况下窗口内的失败语义=单次 open 得 ENOENT,不损坏数据。②tar 备份在有并发写入时不是一致性快照——前置条件「无活跃 claude 进程」满足时备份才可信,否则仅 best-effort,journal 回滚才是主保障。

**目标机的具体前置条件（迁移前必须复核）**：

- 无 `claude` CLI 进程（当前为 0，claude 甚至未安装）
- claudecodeui server (PID 2835) 使用默认 `~/.claude`，但 `/proc/2835/fd` 无 claude 文件句柄 —— 兼容 symlink 足以保证其无感
- `~/.claude/.claude.json` 无 `oauthAccount`，default 身份取自 `~/.claude.json`（§4 第 2 分支）
- 迁移**不移动** `~/.claude.json`：它是旧版遗留路径，仍被读取；由 doctor 标记为「遗留文件」并提示

**`--rollback`** 依据 journal 逆序还原并逐项校验实际状态(intent 未 done 的条目按文件系统真实状态判定),还原后删除本次迁移创建的空 root 目录。**`--cleanup`** 在长跑进程结束后删除兼容 symlink,`.bashrc` 只移除 ccm 自己的标记块——用户手写的 `cca()`/`ccb()` **不自动改**,打印精确的删除建议(含行号)由用户执行(自动编辑用户 rc 有误删风险,codex 审核采纳)。

## 11. 模块划分

```
~/repos/ccm/
  bin/ccm                 入口：exec python3 -m ccm "$@"
  ccm/
    __main__.py           python -m ccm
    cli.py                argparse 定义与派发
    config.py             路径常量、profiles.json / state.json 读写
    profiles.py           Profile 数据类与生命周期
    layout.py             共享清单、symlink 铺设与校验
    identity.py           oauthAccount / credentials 解析（含三级回退）
    oauth.py              token 寿命判断、refresh（锁+备份）、API 调用
    usage.py              用量查询、account 聚合、best 选择
    cost.py               jsonl 增量扫描、sqlite、聚合
    pricing.py            内置价格表与覆盖加载
    procs.py              /proc 扫描活跃进程
    migrate.py            迁移、回滚、cleanup
    shellinit.py          bash/zsh/fish 集成片段生成
    doctor.py             体检
    backup.py             备份/恢复/导出/导入
    render.py             表格与面板输出（检测到 rich 才升级样式）
    errors.py             异常类型
  tests/
  install.sh
  README.md
  docs/superpowers/specs/2026-08-26-ccm-design.md
```

**依赖：零**。全部标准库（`argparse` / `json` / `urllib.request` / `sqlite3` / `pathlib` / `os` / `fcntl` / `subprocess`）。检测到 `rich` 才启用彩色表格与 `--watch` 面板，否则纯文本对齐输出。目标 Python ≥ 3.9（目标机 是 3.14.4）。

## 12. 错误处理与 doctor

统一异常基类 `CcmError`，CLI 顶层捕获后打印单行人类可读信息 + 非零退出码。分类：`ProfileNotFound` / `CredentialsMissing` / `TokenExpired` / `ApiError` / `MigrationAborted` / `LockBusy` / `LayoutBroken`。

`ccm doctor` 检查项：

| 检查 | `--fix` 能否自动修 |
|---|---|
| profile 目录存在且可写 | 否（报告） |
| `.credentials.json` 存在、格式合法 | 否 |
| access token 剩余寿命 / refresh token 剩余寿命 | 否（提示 login） |
| 身份可解析（三级回退都失败则报错） | 是，**前提是 access token 仍有效**；token 已过期时降级为报告并提示 `ccm login` |
| `shared[]` 每项的 symlink 存在且指向正确 | **是**（重建） |
| symlink 指向的共享文件真实存在 | 否（报告断链） |
| 兼容链接 `~/.claude` 等指向正确 | **是** |
| API 连通性 | 否 |
| 是否有活跃进程占用（迁移前门禁） | 否 |
| 遗留 `~/.claude.json` 提示 | 否 |
| `pricing.json` 是否超过 180 天未更新 | 否 |
| `state.json` 指向的 profile 是否仍存在 | **是**（回落到 default） |

所有破坏性操作(`rm` / `logout` / `migrate` / `restore` / `unlink`)先备份、后执行,备份路径打印在输出里。

**权限与日志纪律**:`~/.ccm` 及 `backups/`/`locks/`/`logs/` 创建即 0700;含凭证的文件自首次 `os.open(…, 0o600)` 就是 0600(不走「先建后 chmod」);日志与异常不得含 accessToken/refreshToken 明文(只允许前 12 字符指纹)。`profiles.json`/`state.json` 的写入在 `~/.ccm/lock` 的 `flock` 内进行,防并发读改写丢失。

## 13. 测试策略

`tests/` 用 **unittest 风格**编写（`pytest` 存在时用 pytest 跑，不存在则 `python -m unittest`），保持零依赖。

- **fake HOME fixture**：在 tmpdir 里造一个仿真环境 —— 三个目录、假 `.credentials.json`（含各种过期状态）、7 条共享 symlink、几个含真实 `usage` 结构的 jsonl、一份 `~/.claude.json` 遗留文件。所有测试通过注入 `CCM_HOME` 环境变量指向它，绝不触碰真实 HOME。
- **单元**：注册表读写、共享清单差异计算、身份三级回退、token 寿命判断、jsonl 增量解析、价格匹配与折算、进程扫描（伪造 `/proc` 结构）。
- **迁移端到端**：仿真 HOME → `migrate` → 断言新布局正确、兼容 symlink 有效、文件内容零丢失、inode 未变；再 `--rollback` → 断言完全还原。
- **假 claude 二进制**：`tests/fixtures/fake-claude` 是个打印收到的 `CLAUDE_CONFIG_DIR` 后退出的脚本。`run` / `shell` / `env` / `init` 的注入逻辑全部据此自动化验证。目标机 未安装真 claude，这也让测试不依赖它。
- **网络层全 mock**：`urllib.request.urlopen` 打桩，单测不打真实 API。真实 API 连通性只在手动的 `ccm doctor` 中验证。
- **手动验证清单**（无法自动化的部分）：真实 OAuth `login` 流程、跨终端切换生效、statusline 集成显示。

## 14. 实现优先级

| 阶段 | 内容 |
|---|---|
| **P0** | `config` / `profiles` / `layout` / `identity` / `procs` / `migrate` + `ls` / `use` / `current` / `run` / `env` / `init` / `link` / `doctor` / `usage --all` |
| **P1** | `add` / `rm` / `rename` / `login` / `logout` · `show` · `best` / `use --auto` · `cost` · `backup` / `restore` · `statusline` · `refresh` |
| **P2** | `usage --watch` 面板 · `daemon` 采样与 `--history` · 阈值告警 · `diff` · `unlink` / `shared` · `export` / `import` · `completion` · `token` |

P0 完成即可完全替代 `cca`/`ccb` 并具备用量可见性。

## 15. 已知限制

1. **本地成本是等价折算，不是账单。** Max 订阅不按 token 计费。
2. **`projects/` 共享导致部分用量无法归因到 profile。** 归因依赖各 profile 私有的 `sessions/`、`session-env/`；查不到的记 `unknown` 并单列。这是保留跨账号 `--resume` 能力的代价。
3. **迁移存在微秒级 rename 空窗。** 无法完全消除；影响面限于单次写入。
4. **ccm 不能阻止 Claude Code 自己刷新凭证。** 只能通过「有活跃进程就不刷新」来单向避让。
5. **兼容 symlink 需长期保留。** 直到所有引用旧路径的长跑进程结束（部分进程命令行里硬编码了 `~/.claude-b/projects/...` 这类绝对路径）。
6. **`~/.claude.json` 保持原位不动。** 旧版遗留路径仍被 Claude Code 读取,由 doctor 提示但不迁移。
7. **「tmp+rename」式保存会把共享 symlink 替换成实体文件。** 任何程序(含 Claude Code)对 `settings.json` 这类共享文件做原子保存时,rename 落在 profile 目录内,会静默断开共享。这在现有手工 symlink 方案同样存在;ccm 的缓解是 doctor 检出为 conflict 并报告(绝不自动覆盖),由用户决定收编或独立。
8. **Python 版本下限以实际测试矩阵为准**(开发机与 目标机的实际版本),不对未测版本做兼容主张。
