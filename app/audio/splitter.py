"""增量分句器。

TTS 如果等整段文字生成完再合成，首字延迟会到 2-3 秒。所以这里做的是
「边收边切」：每凑出一个完整句子就立刻交给合成，让音频和文字几乎同时开始。

切分优先级：句末标点 > 逗号类弱标点 > 空白 > 硬切 40 字。
硬切时必须避开数字和英文单词的中间，否则会把 "3.5"、"Redis" 读断。
"""

from __future__ import annotations

# 句末标点：中文标点 + 英文标点 + 省略号（U+2026 与中文六点省略号）
STRONG = frozenset("。！？；!?;…\n")
# 紧跟句末标点、应当并进同一句的收尾符号
CLOSERS = frozenset("”’」』）)】》〉\"'")
# 弱标点：只在句子过长时才用来切
WEAK = frozenset("，、,:：—")

# 超过这个长度就必须硬切，避免一屏字幕配一大段音频
MAX_LEN = 40
# 硬切时最低回退位置，再短会导致句子过碎
MIN_CUT = 12


class SentenceSplitter:
    """把流式文本切成可独立合成的句子。

    用法：
        splitter.feed("你好。今天")  -> ["你好。"]
        splitter.feed("聊什么")      -> []
        splitter.flush()            -> ["今天聊什么"]
    """

    def __init__(self, max_len: int = MAX_LEN) -> None:
        self._buf = ""
        self._max_len = max_len

    @property
    def pending(self) -> str:
        return self._buf

    def feed(self, delta: str) -> list[str]:
        """喂入新文本，返回此刻已经可以送去合成的完整句子。"""
        if not delta:
            return []
        self._buf += delta
        ready: list[str] = []
        while True:
            cut = self._find_cut()
            if cut <= 0:
                break
            piece = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if self._has_content(piece):
                ready.append(piece)
        return ready

    def flush(self) -> list[str]:
        """流结束或要立刻播报时，把残留文本吐出来。"""
        rest = self._buf.strip()
        self._buf = ""
        return [rest] if self._has_content(rest) else []

    def reset(self) -> None:
        self._buf = ""

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _has_content(text: str) -> bool:
        """只剩标点的碎片不值得合成。"""
        return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text)

    def _find_cut(self) -> int:
        buf = self._buf
        length = len(buf)
        if length < 2:
            return 0

        index = 0
        while index < length:
            char = buf[index]

            # 英文句点要看上下文："3.5" 是小说的数，"end." 才是句末
            if char == ".":
                if self._is_sentence_dot(index):
                    end = self._swallow(index + 1)
                    # 标点刚好在缓冲区末尾时先不切，等后面的字确认这句话结束了
                    return end if end < length else 0
                index += 1
                continue

            if char in STRONG:
                end = self._swallow(index + 1)
                return end if end < length else 0

            index += 1

        # 没有句末标点，看是否已经长到必须切
        if length < self._max_len:
            return 0
        return self._cut_long()

    def _is_sentence_dot(self, index: int) -> bool:
        """区分句号和小数点、缩写点、域名点。"""
        buf = self._buf
        prev = buf[index - 1] if index > 0 else ""
        nxt = buf[index + 1] if index + 1 < len(buf) else ""
        if prev.isdigit() and nxt.isdigit():
            return False
        # 点后面紧跟字母或数字，说明是 Redis.Cluster、v1.2 这类标识符
        if nxt and (nxt.isalnum() or nxt in "_"):
            return False
        return True

    def _swallow(self, start: int) -> int:
        """把连续的标点和收尾引号一起吃进这一句。"""
        buf = self._buf
        end = start
        while end < len(buf) and (buf[end] in STRONG or buf[end] in WEAK):
            end += 1
        while end < len(buf) and buf[end] in CLOSERS:
            end += 1
        return end

    def _cut_long(self) -> int:
        """超长文本的切点：弱标点 > 空白 > 避开标识符后硬切。"""
        buf = self._buf
        limit = self._max_len

        for index in range(limit, MIN_CUT, -1):
            if buf[index - 1] in WEAK:
                return index

        for index in range(limit, MIN_CUT, -1):
            if buf[index - 1].isspace():
                return index

        cut = limit
        # 切点落在英文单词或数字中间时向前退，宁可这句短一点
        while cut > MIN_CUT and self._is_ident(buf[cut - 1]) and self._is_ident(buf[cut]):
            cut -= 1
        return cut

    @staticmethod
    def _is_ident(char: str) -> bool:
        return char.isascii() and (char.isalnum() or char in "_%.-")


def split_now(text: str) -> list[str]:
    """非流式场景：一次性把整段文本切好。"""
    splitter = SentenceSplitter()
    parts = splitter.feed(text)
    parts.extend(splitter.flush())
    return parts
