# ccm — Claude Code 多账号统一管理 CLI

> Unified account manager for Claude Code: register, switch, monitor rate-limit
> usage, and share configuration across multiple Claude subscriptions — with one
> zero-dependency Python CLI. Interface is currently in Chinese.

用一套命令管理任意多个 Claude Code 账号:注册 / 切换 / 实时限流监控 / token 统计 /
配置共享 / 凭证刷新。替代手工维护的 `CLAUDE_CONFIG_DIR` shell alias 与 symlink。

**零第三方依赖** —— 纯 Python 标准库(≥3.10),单目录即装即用。

```
$ ccm ls
profile  email              订阅          token  账号
●a1      alice@example.com  claude_max   5h     #1
 a2      alice@example.com  claude_max   已过期  #1
 a3      bob@example.com    claude_max   7h     #2

$ ccm usage
账号                profiles  5h   重置     7d    重置      来源
bob@example.com    a3        4%   4h41m   35%   38h51m   实时
alice@example.com  a1+a2     75%  11m     20%   137h01m  实时

$ ccm switch bob     # email 子串即可切换,当前终端立即生效
```

## 为什么

Claude Code 的多账号支持只有一个环境变量 `CLAUDE_CONFIG_DIR`。多账号用户通常会攒出
一堆 shell alias + 手工 symlink,然后发现:

- alias 只影响当前 shell,别的终端、cron、脚本全都感知不到
- 不启动 claude 就看不到任何账号还剩多少额度,切号全靠猜
- 每个账号目录里 7 条手工 symlink,加账号重敲一遍,断链无人察觉
- `~/.claude` 既是默认账号又是共享库,职责耦合,增删账号无从下手

ccm 把这四件事全部收编成一个 CLI。

## 核心设计

### Account / Profile 两层模型

```
Account   由 OAuth accountUuid 唯一确定 —— 限流额度、订阅挂在这里
  └─ Profile  一个配置目录 —— 切换单位,独立会话历史
```

一个 Account 可以有多个 Profile(它们**共享同一份限流额度**)。`ccm usage` 按
Account 去重聚合(每个账号只发一次请求),`ccm ls` 按 Profile 列出并标注分组。

### 编码 id + email 双轨定位

Profile 不用语义别名(什么 personal/work),注册即自动编号 `a1, a2, a3, …`;
定位靠 selector,以下写法全部有效:

```bash
ccm switch a3               # 精确 id(use 是别名)
ccm switch 3                # 纯数字
ccm switch corp             # 自定义名(ccm rename a3 corp 之后)
ccm switch bob@example.com  # 精确 email
ccm switch bob              # email 子串(≥3 字符,不区分大小写)
ccm switch 2399b3           # account uuid 前缀(≥6 字符)
ccm switch --auto           # 切到额度最宽裕的账号
```

**Tab 补全是一等公民**(`ccm init bash` 自动启用):像 git 一样按上下文补——
`ccm sw<TAB>` → `switch`;`ccm switch <TAB>` → 列出 id + email;
`ccm usage --<TAB>` → 该命令的全部选项;`ccm shared rm <TAB>` → 共享清单项。

- 同账号多 profile → 自动挑最优(token 有效 > 默认 profile > id 序)
- 跨账号歧义 → 拒绝并列出候选,**绝不猜**
- 这套定位符通用于所有吃 profile 参数的命令(`run`/`show`/`refresh`/`backup`/…)

### 目录布局

```
~/.claude-shared/        共享库(settings/CLAUDE.md/plugins/skills/projects/…)
~/.claude-accounts/      各 profile 的私有状态(凭证/会话/daemon)
    a1/  a2/  a3/            共享项 = 指向 shared 的 symlink(声明式清单驱动)
~/.ccm/                  ccm 自身(注册表/state/usage.db/备份/日志)
~/.claude → ~/.claude-accounts/a1     默认落点,永久保留
```

共享清单存在注册表里,`ccm link` 幂等重建,`ccm doctor` 检测断链,
加第 N 个账号 = `ccm add` + `ccm login aN`,不用碰任何 symlink。

### 切换三层机制

1. **状态层** —— `ccm use` 写持久 state,所有新终端生效
2. **Shell 层** —— `ccm init bash` 注入的函数让**当前终端**同步生效
3. **兜底层** —— `ccm run a3 -- --resume` 直接注入环境启动,cron/脚本用这个

## 安装

一句话装好(clone + 装入 `~/.local/bin` + shell 集成/Tab 补全):

```bash
git clone https://github.com/YufengJin/ccm ~/.local/share/ccm && ~/.local/share/ccm/install.sh && ~/.local/bin/ccm init bash
```

已有 pipx 的话也可以:

```bash
pipx install git+https://github.com/YufengJin/ccm && ccm init bash
```

装完后把现有 `~/.claude*` 布局迁入 ccm 管理(全程 journal 可回滚):

```bash
ccm migrate -n     # 先看迁移计划(dry-run)
ccm migrate        # 确认后执行
```

> **让 Claude Code 帮你装**:把下面这句直接丢给它——
>
> ```
> 按 https://github.com/YufengJin/ccm 的 README 一句话安装 ccm,
> 然后跑 ccm migrate -n 把迁移计划给我看,先不要真迁移。
> ```

### 迁移安全性

`ccm migrate` 采用「预置 dangling symlink + 两次 rename」的最小空窗算法:

- 同文件系统 rename,**inode 不变** —— 正在运行的 claude 进程无感(已打开的
  fd 与经旧路径的后续访问全部照常)
- 旧路径(`~/.claude-a` 等)留兼容 symlink,长跑任务不受影响
- 每步先写 intent journal 再执行,SIGKILL/断电后可 `--rollback` 完整还原
- 迁移前自动全量备份(0600);阶段末自动 doctor,不变量校验失败自动回滚
- 确认无旧路径依赖后 `ccm migrate --cleanup` 清掉兼容链接

## 命令参考

### 账号生命周期

| 命令 | 说明 |
|---|---|
| `ccm add [name] [--import DIR [--move]] [--note …]` | 新建(自动编号)或纳管已有目录 |
| `ccm login <sel>` | 启动 claude 引导 `/login`,退出后自动回填身份 |
| `ccm logout <sel> [--keep-backup]` | 删除凭证,默认不留副本 |
| `ccm ls [--json]` | 列出全部 profile(email/订阅/token/账号分组) |
| `ccm show <sel> [--json]` | 单个 profile 详情 |
| `ccm rm <sel> [--keep-data]` | 删除(先备份,含凭证,可恢复);默认落点不可删 |
| `ccm rename <sel> <new>` | 改名(目录、兼容链接、state、默认指针同步) |

### 切换

| 命令 | 说明 |
|---|---|
| `ccm switch <sel>` / `--auto` | 切换,别名 `use`(shell 集成下当前终端立即生效) |
| `ccm current [--quiet]` | 当前 profile |
| `ccm run <sel> -- <args…>` | 一次性注入环境启动 claude(cron/脚本用) |
| `ccm shell <sel>` | 开注入好环境的子 shell |
| `ccm env` / `ccm init bash` | export 语句 / shell 集成安装 |
| `ccm best [--json]` | 输出最宽裕账号(规则:min max(5h,7d) → min 7d) |

### 用量与成本

| 命令 | 说明 |
|---|---|
| `ccm usage [--json] [--watch] [--history 7d]` | 各账号实时 5h/7d 限流百分比 + 重置倒计时;≥80% 标 ⚠ |
| `ccm cost [--by model\|profile\|project\|day] [--since 7d]` | 本地 token 统计与**等价金额折算**(非账单) |
| `ccm daemon start\|stop\|status [--interval 300]` | 后台采样入库,供 `--history` 与 statusline |
| `ccm statusline` | 离线快速单行,可接 Claude Code statusline |

### 健康与凭证

| 命令 | 说明 |
|---|---|
| `ccm doctor [--fix] [--online]` | 体检:目录/凭证/token 寿命/symlink/state,`--fix` 自动修 |
| `ccm refresh [<sel>\|--all] [--force]` | 刷新过期 access token(见下方安全模型) |
| `ccm token <sel> --yes` | 打印 access token 供脚本 |

### 配置与运维

| 命令 | 说明 |
|---|---|
| `ccm link [<sel>]` | 按共享清单幂等重建 symlink |
| `ccm shared ls\|add <item> [--from <sel>]\|rm <item>` | 维护共享清单(add 可从某 profile 收编) |
| `ccm unlink <sel> <item>` | 把共享项复制为独立副本 |
| `ccm diff <sel> <sel>` | 比较两 profile 的非共享配置 |
| `ccm backup [<sel>] [--with-credentials]` / `restore <tar> [--into name]` | 备份/恢复(安全解包,防路径穿越) |
| `ccm export <sel> <file>` / `import <file>` | 跨机器搬运(含凭证) |
| `ccm migrate [--dry-run\|--rollback\|--cleanup]` | 布局迁移 |
| `ccm completion bash` | Tab 补全(id 和 email 都能补) |

## 凭证安全模型

**风险**:Claude Code 自己会刷新 OAuth refresh token,且刷新会轮换 —— 若 ccm 与
运行中的 claude 并发刷新同一凭证,其中一方会作废,账号掉线。

ccm 的防线(按序):

1. **能不刷就不刷**:每次查询前重读凭证文件,复用 Claude Code 刚刷出来的 token
2. **活跃进程门禁**:目标 profile 有 claude 进程在跑 → 拒绝刷新,降级显示
   缓存数据并标注年龄(`--force` 可越过)
3. **account 级锁**:按 refresh token 指纹加锁,同账号多 profile 串行
4. **双重 CAS**:持锁后重读一次;API 返回后写盘前再读一次 —— refresh token
   变了说明对方抢先,放弃写入采用对方结果
5. **失败绝不回写**:请求一旦发出,旧 refresh token 视为可能已作废;结果不确定
   时停止一切写入,提示重新登录

其他纪律:`~/.ccm` 0700;凭证文件自创建即 0600(无先建后 chmod 的泄露窗口);
日志与报错不含 token 明文;注册表写入走 flock + 原子替换。

## 成本统计口径

- Max/Pro 订阅**不按 token 计费**。`ccm cost` 输出的是按 API 价目表的
  **等价折算金额**,用于横向比较模型/项目开销结构,不是账单
- 数据来自 `projects/*.jsonl` 增量扫描(只推进到完整行,原子替换自动重扫,
  主键去重),按当前价格表在查询时折算
- 会话归因靠各 profile 私有的 `session-env/`;跨账号 resume 过的会话记
  `ambiguous`,查不到的记 `unknown` —— **绝不猜归属**,两者都单列计入总量
- 价格表可用 `~/.ccm/pricing.json` 覆盖

## 开发

```bash
python3 -m unittest discover -s tests    # 205 个测试,全部隔离(不触碰真实 HOME)
```

测试通过 `CCM_USER_HOME` / `CCM_HOME` / `CCM_PROC_ROOT` 等环境变量把一切路径注入
tmpdir:fake home 构造器复刻真实布局,`/proc` 用伪造进程树,网络层全部打桩,
迁移端到端测试断言 inode 不变、已打开 fd 存活、崩溃现场可续跑、回滚逐字节还原。

```
ccm/
  config.py    Env、注册表、state、原子写、flock
  selector.py  id/email/序号/uuid 定位
  identity.py  账号身份三级回退解析
  oauth.py     token 寿命、usage/profile/token API
  refresh.py   凭证刷新(锁+CAS)
  procs.py     /proc 活跃进程扫描
  layout.py    声明式共享 symlink
  migrate.py   迁移/journal/回滚/cleanup
  usage.py     account 聚合、best、statusline
  cost.py      jsonl 增量扫描 → sqlite → 聚合
  pricing.py   价格表与折算
  lifecycle.py add/rm/rename/login/logout/show
  backup.py    备份/恢复(安全解包)
  sharing.py   shared/unlink/diff
  daemon.py    后台采样与历史
  doctor.py    体检与 --fix
  cli.py       argparse 派发
```

设计文档见 [docs/design.md](docs/design.md)。

## License

MIT
