"""字幕字体自检：镜像缺中文字体时必须显式报错，而不是把整片烧成豆腐块。

现场问题：合成用 ``FontName=Noto Sans CJK SC`` 烧字幕，但镜像里只有 DejaVu，
libass 静默回退到无中文字形的字体，成片字幕全是 □。合成本身是成功的，所以
问题只在人看片时才暴露。
"""

import pytest

from novelvideo.verification import subtitle_font as sf


#: 真实 NotoSansCJK 覆盖 ASCII + 常用标点（含 ``…`` U+2026）+ CJK 基本区/全角。
#: 分隔符刻意用空格：fontconfig 实际就是空格分隔，用逗号会让回归测试失去意义。
_NOTO_LIKE_CHARSET = "20-7e 2000-206f 3000-303f 4e00-9fff f900-faff ff00-ffef"


@pytest.fixture(autouse=True)
def _clear_font_caches():
    sf.fontconfig_available.cache_clear()
    sf._match_family.cache_clear()
    yield
    sf.fontconfig_available.cache_clear()
    sf._match_family.cache_clear()


def _install_fake_fontconfig(
    monkeypatch,
    *,
    families: dict[str, str],
    charsets: dict[str, str],
    available: bool = True,
) -> None:
    """假装一个 fontconfig CLI。

    真实实现只调 ``fc-match``，并且用一次调用同时拿到文件路径与 charset
    （``-f "%{file}\\t%{charset}"``），所以这里也只模拟这一条路径。
    """

    def fake_run(command):
        if not available:
            return None
        if command[0] != "fc-match":
            return None
        if command[2] == "%{file}\t%{charset}":
            family = command[-1]
            path = families.get(family)
            if path is None:
                # fc-match 总会给个答案：退回内置默认字体，裸文件名即「没匹配上」。
                return "DejaVuSans.ttf\t20-7e"
            return f"{path}\t{charsets.get(path, '')}"
        return families.get(command[-1], "DejaVuSans.ttf")

    monkeypatch.setattr(sf, "_run", fake_run)


def test_parse_charset_reads_ranges_and_singletons():
    assert sf.parse_charset("20-7e,a0") == ((0x20, 0x7E), (0xA0, 0xA0))


def test_parse_charset_ignores_garbage_tokens():
    assert sf.parse_charset("zz,4e00") == ((0x4E00, 0x4E00),)


def test_parse_charset_handles_the_space_separated_fontconfig_output():
    """fontconfig 用空格分隔区间，不是逗号。

    这一条是回归的核心：只按逗号切会把整串当成一个 token，解析出一个假区间，
    于是所有字都显示「缺字形」，自检变成误报（甚至把合成整个拦下来）。
    """

    parsed = sf.parse_charset("20-7e a0-103 4e00-9fef")

    assert parsed == ((0x20, 0x7E), (0xA0, 0x103), (0x4E00, 0x9FEF))
    assert sf.covers(parsed, ord("意"))
    assert sf.covers(parsed, ord("A"))
    assert not sf.covers(parsed, 0x20000)


def test_covers_matches_inclusive_bounds():
    charset = sf.parse_charset("4e00-9fff")
    assert sf.covers(charset, ord("意"))
    assert not sf.covers(charset, ord("A"))


def test_matching_file_ignores_the_fontconfig_builtin_fallback(monkeypatch):
    _install_fake_fontconfig(monkeypatch, families={}, charsets={})

    assert sf.matching_file_for_family("Noto Sans CJK SC") is None


def test_resolve_returns_none_when_fontconfig_is_unavailable(monkeypatch):
    _install_fake_fontconfig(monkeypatch, families={}, charsets={}, available=False)

    assert sf.resolve_subtitle_font("今天是我的生日……") is None


def test_resolve_picks_the_first_family_that_covers_the_text(monkeypatch):
    _install_fake_fontconfig(
        monkeypatch,
        families={
            "Noto Sans CJK SC": "/fonts/NotoSansCJK.ttc",
        },
        charsets={"/fonts/NotoSansCJK.ttc": _NOTO_LIKE_CHARSET},
    )

    font = sf.resolve_subtitle_font("今天是我的生日……")

    assert font is not None
    assert font.family == "Noto Sans CJK SC"
    assert font.file == "/fonts/NotoSansCJK.ttc"
    assert font.complete


def test_resolve_skips_a_family_whose_charset_lacks_the_glyphs(monkeypatch):
    _install_fake_fontconfig(
        monkeypatch,
        families={
            "Noto Sans CJK SC": "/fonts/DejaVu.ttf",
            "WenQuanYi Zen Hei": "/fonts/wqy.ttc",
        },
        charsets={
            "/fonts/DejaVu.ttf": "20-7e",
            "/fonts/wqy.ttc": _NOTO_LIKE_CHARSET,
        },
    )

    font = sf.resolve_subtitle_font("生日快乐。")

    assert font is not None
    assert font.family == "WenQuanYi Zen Hei"


def test_resolve_raises_when_no_installed_font_has_cjk_glyphs(monkeypatch):
    _install_fake_fontconfig(
        monkeypatch,
        families={"DejaVu Sans": "/fonts/DejaVu.ttf"},
        charsets={"/fonts/DejaVu.ttf": "20-7e"},
    )

    with pytest.raises(sf.SubtitleFontError) as excinfo:
        sf.resolve_subtitle_font("今天是我的生日……")

    assert "fonts-noto-cjk" in str(excinfo.value)


def test_resolve_reports_partial_coverage_instead_of_failing(monkeypatch):
    _install_fake_fontconfig(
        monkeypatch,
        families={"Noto Sans CJK SC": "/fonts/NotoSansCJK.ttc"},
        charsets={"/fonts/NotoSansCJK.ttc": "20-7e,4e00-9fff"},
    )

    font = sf.resolve_subtitle_font("生僻字：𠀋")  # U+2000B 在扩展 B 区

    assert font is not None
    assert "𠀋" in font.missing
    assert not font.complete


def test_resolve_accepts_latin_only_text(monkeypatch):
    _install_fake_fontconfig(
        monkeypatch,
        families={"Noto Sans CJK SC": "/fonts/NotoSansCJK.ttc"},
        charsets={"/fonts/NotoSansCJK.ttc": "20-7e"},
    )

    font = sf.resolve_subtitle_font("Rain falls.")

    assert font is not None
    assert font.complete
