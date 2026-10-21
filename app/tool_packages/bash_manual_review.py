"""Explicit destructive-command rules; not a shell sandbox or script analyser."""
from __future__ import annotations

import re
import shlex


def command_manual_review_reason(command: str, *, _depth: int = 0) -> str | None:
    if _depth > 8:
        return "Nested shell command exceeds the manual-review inspection depth."
    # Shell continuation must not split an executable or one of its flags.
    command = command.replace("\\\n", "").replace("`\n", "")
    for nested in _substitutions(command):
        reason = command_manual_review_reason(nested, _depth=_depth + 1)
        if reason:
            return reason
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # Malformed/unfinished interactive text is not granted destructive authority.
        if re.search(r"\b(?:rm|remove-item|rmdir|mkfs|wipefs|diskpart)\b", command, re.IGNORECASE):
            return "Potential destructive command could not be parsed for manual review."
        return None
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token and all(ch in ";&|()\n" for ch in token):
            reason = _segment_reason(segment, _depth)
            if reason:
                return reason
            segment = []
        else:
            segment.append(token)
    return None


def _substitutions(command: str):
    """Inspect command substitutions, including ones inside double quotes."""
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif quote != "'" and command.startswith("$(", index):
            start, end, level, inner_quote = index + 2, index + 2, 1, None
            while end < len(command) and level:
                inner = command[end]
                if inner == "\\" and inner_quote != "'":
                    end += 2
                    continue
                if inner in {"'", '"'}:
                    if inner_quote is None:
                        inner_quote = inner
                    elif inner_quote == inner:
                        inner_quote = None
                elif inner == "(" and inner_quote is None:
                    level += 1
                elif inner == ")" and inner_quote is None:
                    level -= 1
                end += 1
            yield command[start:end - 1] if not level else command[start:]
            index = end
            continue
        elif quote != "'" and char == "`":
            end = command.find("`", index + 1)
            if end >= 0:
                yield command[index + 1:end]
                index = end + 1
                continue
        index += 1


def _name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _segment_reason(tokens: list[str], depth: int) -> str | None:
    while tokens and (tokens[0] in {"!", "do", "then", "{"} or re.match(r"^[A-Za-z_]\w*=", tokens[0])):
        tokens = tokens[1:]
    if not tokens:
        return None
    name, args = _name(tokens[0]), tokens[1:]
    if name == "trap" and args:
        return command_manual_review_reason(args[0], _depth=depth + 1)
    if name in {"sudo", "env", "command", "exec", "nohup", "xargs", "busybox"}:
        for index, arg in enumerate(args):
            if not arg.startswith("-"):
                reason = _segment_reason(args[index:], depth)
                if reason:
                    return reason
        return None
    if name in {"bash", "sh", "dash", "zsh", "ksh", "powershell", "pwsh", "cmd", "eval"}:
        if name == "eval":
            return command_manual_review_reason(" ".join(args), _depth=depth + 1)
        for index, arg in enumerate(args[:-1]):
            if arg.lower() in {"-command", "-c", "/c"} or (
                name in {"bash", "sh", "dash", "zsh", "ksh"} and arg.startswith("-") and "c" in arg
            ):
                script = " ".join(args[index + 1:]) if name in {"powershell", "pwsh", "cmd"} else args[index + 1]
                # shlex retains the dollar prefix of Bash ANSI-C/locale quotes.
                script = script.removeprefix("$")
                return command_manual_review_reason(script, _depth=depth + 1)
    if name in {"mkfs", "wipefs", "diskpart", "format", "shred"} or name.startswith("mkfs."):
        return "Destructive disk/secure-erasure command is on the manual-review blacklist."
    flags = []
    for arg in args:
        if arg == "--":
            break
        if arg.startswith(("-", "/")):
            flags.append(arg.lower().split(":", 1)[0])
    if name == "rm" and any(
        (flag.startswith("--recursive")) or
        (flag.startswith("-") and not flag.startswith("--") and "r" in flag[1:])
        for flag in flags
    ):
        return "Recursive deletion (rm -r/-rf) is on the manual-review blacklist."
    if name in {"remove-item", "ri", "rm", "rmdir", "rd", "del", "erase"} and any(
        flag == "/s" or (flag.startswith("-") and len(flag) > 1 and "-recurse".startswith(flag))
        for flag in flags
    ):
        return "Recursive deletion is on the manual-review blacklist."
    if name == "find":
        if "-delete" in args:
            return "Recursive find -delete is on the manual-review blacklist."
        for index, arg in enumerate(args):
            if arg in {"-exec", "-execdir"}:
                return _segment_reason(args[index + 1:], depth)
    if name == "dd" and any(arg.startswith("of=/dev/") for arg in args):
        return "Raw device overwrite is on the manual-review blacklist."
    if name == "git" and (
        ("reset" in args and "--hard" in args) or
        ("clean" in args and any(arg.startswith("-") and "f" in arg for arg in args))
    ):
        return "Discarding Git working-tree contents is on the manual-review blacklist."
    return None
