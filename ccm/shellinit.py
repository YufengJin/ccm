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
if [ -x "$HOME/.local/bin/ccm" ]; then
  _CCM_BIN="$HOME/.local/bin/ccm"
else
  _CCM_BIN="$(command -v ccm 2>/dev/null)"
fi
if [ -n "$_CCM_BIN" ] && [ -z "$CCM_PROFILE_PINNED" ]; then
  eval "$("$_CCM_BIN" env 2>/dev/null)"
fi
ccm() {{
  local __ccm_bin="${{_CCM_BIN:-ccm}}"
  case "$1" in
    use|switch)
      local __ccm_out
      __ccm_out="$("$__ccm_bin" "$@" --emit-env)" || return $?
      eval "$__ccm_out"
      ;;
    *) "$__ccm_bin" "$@" ;;
  esac
}}
_ccm_completion() {{
  local IFS=$'\n'
  COMPREPLY=($("${{_CCM_BIN:-ccm}}" _complete "$COMP_CWORD" "${{COMP_WORDS[@]}}" 2>/dev/null))
}}
complete -o nosort -F _ccm_completion ccm 2>/dev/null || complete -F _ccm_completion ccm
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
