import shlex
from pathlib import Path

BLOCK_BEGIN = "# >>> ccm >>>"
BLOCK_END = "# <<< ccm <<<"


def env_exports(profile_path):
    return f"export CLAUDE_CONFIG_DIR={shlex.quote(str(profile_path))}"


def rc_block():
    # 绝对路径兜底:Ubuntu ~/.profile 先 source .bashrc、后把 ~/.local/bin 加进
    # PATH——source 时 `command ccm` 找不到,eval 会静默失败,新登录 shell 就
    # 「忘了」当前账号。所以块内自行解析二进制位置。
    return f"""{BLOCK_BEGIN}
# ccm shell 集成(自动生成,勿手改;重装: ccm init bash)
# 解析二进制位置有两个坑,都踩过:
#  1. Ubuntu ~/.profile 先 source .bashrc、后把 ~/.local/bin 加进 PATH ——
#     source 时 `command ccm` 找不到,eval 静默失败,新登录 shell 就「忘了」当前账号。
#  2. `command -v ccm` 在**二次 source ~/.bashrc** 时会命中下面这个同名函数,
#     _CCM_BIN 变成 "ccm",函数于是调用自己 → 无限递归 → bash 段错误。
#     所以用 `type -P`(只查 PATH,不看函数/别名),兜底也绝不写成裸 "ccm"。
if [ -x "$HOME/.local/bin/ccm" ]; then
  _CCM_BIN="$HOME/.local/bin/ccm"
else
  _CCM_BIN="$(type -P ccm 2>/dev/null)"
fi
if [ -n "$_CCM_BIN" ] && [ -z "$CCM_PROFILE_PINNED" ]; then
  eval "$("$_CCM_BIN" env 2>/dev/null)"
fi
ccm() {{
  local __ccm_bin="$_CCM_BIN"
  [ -n "$__ccm_bin" ] || __ccm_bin="$(type -P ccm 2>/dev/null)"
  if [ -z "$__ccm_bin" ]; then
    echo "ccm: 找不到 ccm 可执行文件,检查 PATH 或重装" >&2
    return 127
  fi
  case "$1" in
    use|switch)
      # -h/--help 直通:加上 --emit-env 后 argparse 仍以 0 返回帮助文本,
      # 下面的 eval 会把 "usage:" "options:" 逐行当命令执行。
      case " $* " in
        *" -h "*|*" --help "*) "$__ccm_bin" "$@"; return $? ;;
      esac
      local __ccm_out
      __ccm_out="$("$__ccm_bin" "$@" --emit-env)" || return $?
      # 只 eval 预期形状的一行 export;其他内容原样打印,绝不当代码跑
      case "$__ccm_out" in
        "export CLAUDE_CONFIG_DIR="*) eval "$__ccm_out" ;;
        "") ;;
        *) printf '%s\n' "$__ccm_out" ;;
      esac
      ;;
    *) "$__ccm_bin" "$@" ;;
  esac
}}
_ccm_completion() {{
  local IFS=$'\n'
  local __ccm_bin="${{_CCM_BIN:-$(type -P ccm 2>/dev/null)}}"
  [ -n "$__ccm_bin" ] || return 0
  COMPREPLY=($("$__ccm_bin" _complete "$COMP_CWORD" "${{COMP_WORDS[@]}}" 2>/dev/null))
}}
# 补全只对交互式 shell 有意义;非交互(ssh host cmd、脚本 source .bashrc)跳过。
# 注:上面的 env eval **故意**不加交互守卫 —— 「所有新开的终端立即生效」是 state
# 层的核心语义,ssh 会话也算终端;代价只有一次 ~35ms 的 ccm env。
case $- in
  *i*)
    complete -o nosort -F _ccm_completion ccm 2>/dev/null || \
      complete -F _ccm_completion ccm
    ;;
esac
{BLOCK_END}"""


def install_block(rc_path):
    """把标记块写入 rc 文件:已有则原位替换,没有则追加。幂等,返回是否有改动。"""
    rc_path = Path(rc_path)
    old = rc_path.read_text() if rc_path.exists() else ""
    block = rc_block()
    if BLOCK_BEGIN in old and BLOCK_END in old:
        head, _, rest = old.partition(BLOCK_BEGIN)
        _, _, tail = rest.partition(BLOCK_END)
        new = head + block + tail
    else:
        sep = "" if (not old or old.endswith("\n")) else "\n"
        new = old + sep + block + "\n"
    if new == old:
        return False
    rc_path.write_text(new)
    return True
