import shlex
from pathlib import Path

BLOCK_BEGIN = "# >>> ccm >>>"
BLOCK_END = "# <<< ccm <<<"


def env_exports(profile_path):
    return f"export CLAUDE_CONFIG_DIR={shlex.quote(str(profile_path))}"


def rc_block():
    return f"""{BLOCK_BEGIN}
# ccm shell 集成(自动生成,勿手改;重装: ccm init bash)
if [ -z "$CCM_PROFILE_PINNED" ]; then
  eval "$(command ccm env 2>/dev/null)"
fi
ccm() {{
  case "$1" in
    use|switch)
      local __ccm_out
      __ccm_out="$(command ccm "$@" --emit-env)" || return $?
      eval "$__ccm_out"
      ;;
    *) command ccm "$@" ;;
  esac
}}
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
